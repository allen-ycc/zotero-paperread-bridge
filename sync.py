#!/usr/bin/env python3
"""Zotero ⇄ PaperRead 同步。

- Zotero 裡帶 PDF 的條目 → 匯入 PaperRead（不自動翻譯，可在 config.json 改）。
- PaperRead 自己匯入的論文 → 在 Zotero 建條目並附上 PDF。
- Zotero 沒有 PDF 但有 DOI → PaperRead 找開放取用 PDF 匯入，並把 PDF 附回 Zotero（config.json 的 fetch_pdf_by_doi，預設關閉）。
- 分類雙向同步：Zotero 收藏夾「A/B」 ⇄ PaperRead 分類「A/B」。
  config.json 的 sync_tags 打開時，Zotero 標籤「x」 ⇄ PaperRead 分類「#x」也會同步。
  用上一次同步的結果做三方合併，所以兩邊各自新增或移除都會傳到另一邊。
- Zotero 刪除（或移到垃圾桶）的條目 → PaperRead 移到回收站（可在 config.json 關掉）。
- PaperRead 刪除的論文不會刪 Zotero 條目，只是之後不再同步它。

需要：Zotero 10 以上並開啟「允許此電腦上的其他應用程式與 Zotero 通訊」，PaperRead 開著。
只用 Python 標準函式庫。

  py sync.py --authorize   第一次用：向 Zotero 申請寫入權限（Zotero 會跳出視窗，選「永遠允許」）
  py sync.py --dry-run     只列出會做的事，不改任何東西
  py sync.py               同步一次
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOCK_PATH = HERE / ".sync.lock"
LOG_PATH = HERE / "sync.log"
PR_HOME = Path(os.environ.get("EASYREAD_HOME") or Path.home() / "PaperRead")

DEFAULTS = {
    "zotero_url": "http://127.0.0.1:23119",
    "zotero_api_key": "",
    "translate_on_import": False,      # 匯入 PaperRead 後自動翻譯（會用到 Claude 額度）
    "trash_when_zotero_deletes": True,  # Zotero 刪除 → PaperRead 移到回收站
    "create_in_zotero": True,          # PaperRead 自己匯入的論文 → 建到 Zotero
    "fetch_pdf_by_doi": False,         # Zotero 沒有 PDF 時，請 PaperRead 用 DOI 找開放取用版本
    "sync_tags": False,               # Zotero 標籤也同步（關掉時只同步收藏夾，Zotero 標籤不動）
    "tag_prefix": "#",                 # sync_tags 開著時：PaperRead 分類開頭是這個的，對應 Zotero 標籤
    "collection_separator": "/",       # 子收藏夾在 PaperRead 分類名裡的分隔符
    "sync_automatic_tags": False,      # Zotero 自動標籤（匯入時帶進來的關鍵字）也同步
    "max_new_per_run": 10,             # 每次最多新增幾篇（每個方向），避免第一次同步一口氣塞爆
    "mass_delete_guard": 5,            # 一次要刪超過這個數量且超過 20% 時先停手
}

log = logging.getLogger("zpsync")


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- 基本工具

def http(method: str, url: str, body=None, headers: dict | None = None, timeout: float = 120):
    """回傳 (狀態碼, headers, 內容 bytes)；HTTP 錯誤不丟例外，交給呼叫端看狀態碼。"""
    h = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        save_json(CONFIG_PATH, DEFAULTS)
        cfg = {}
    return {**DEFAULTS, **cfg}


def setup_logging(verbose: bool) -> None:
    log.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.INFO)
    log.addHandler(fh)
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        log.addHandler(sh)


YEAR_RE = re.compile(r"(19|20)\d{2}")
ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def split_authors(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    parts = re.split(r"\s*(?:;|,|、|，|\band\b|&)\s*", s)
    return [p.strip() for p in parts if p.strip() and p.strip().lower() not in ("et al", "et al.")]


def creators_from(authors: str) -> list[dict]:
    out = []
    for name in split_authors(authors)[:100]:
        words = name.split()
        if CJK_RE.search(name) or len(words) == 1:
            out.append({"creatorType": "author", "name": name})
        else:
            out.append({"creatorType": "author", "firstName": " ".join(words[:-1]), "lastName": words[-1]})
    return out


def meta_from_zotero(d: dict) -> dict:
    names = []
    for c in d.get("creators") or []:
        if c.get("creatorType", "author") not in ("author", "presenter", "programmer", "inventor"):
            continue
        names.append(c.get("name") or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x))
    date = d.get("date") or ""
    year = YEAR_RE.search(date)
    doi = d.get("DOI") or ""
    if not doi:
        m = re.search(r"^DOI:\s*(\S+)", d.get("extra") or "", re.M | re.I)
        doi = m.group(1) if m else ""
    arxiv = ""
    for src in (d.get("archiveID") or "", d.get("url") or "", d.get("extra") or ""):
        if "arxiv" in src.lower():
            m = ARXIV_RE.search(src)
            if m:
                arxiv = m.group(1)
                break
    venue = next((d.get(k) for k in ("publicationTitle", "proceedingsTitle", "conferenceName", "bookTitle",
                                     "repository", "university", "websiteTitle") if d.get(k)), "")
    meta = {"title_en": d.get("title") or "", "authors": ", ".join(n for n in names if n), "date": date,
            "year": year.group(0) if year else "", "venue": venue, "doi": doi, "url": d.get("url") or "",
            "arxiv": arxiv}
    return {k: v for k, v in meta.items() if v}


def lookup_meta(arxiv: str, doi: str) -> dict:
    """PaperRead 沒抓到書目資料時（例如 Semantic Scholar 限流），改查 arXiv／Crossref。查不到回傳空 dict。"""
    ua = {"User-Agent": "PaperRead-Zotero-Sync/1.0 (mailto:none)"}
    try:
        if arxiv:
            import xml.etree.ElementTree as ET
            status, _, raw = http("GET", f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv)}", headers=ua, timeout=30)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            entry = ET.fromstring(raw).find("a:entry", ns) if status == 200 else None
            if entry is not None and entry.findtext("a:title", "", ns).strip():
                return {"title_en": " ".join(entry.findtext("a:title", "", ns).split()),
                        "authors": ", ".join(a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)),
                        "date": entry.findtext("a:published", "", ns)[:10], "arxiv": arxiv,
                        "url": f"https://arxiv.org/abs/{arxiv}"}
        if doi:
            status, _, raw = http("GET", f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", headers=ua, timeout=30)
            if status == 200:
                m = json.loads(raw)["message"]
                parts = (m.get("issued") or {}).get("date-parts") or [[]]
                return {k: v for k, v in {
                    "title_en": (m.get("title") or [""])[0],
                    "authors": ", ".join(" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
                                         for a in m.get("author") or []),
                    "date": "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts[0])),
                    "venue": (m.get("container-title") or [""])[0], "doi": doi}.items() if v}
    except (OSError, ValueError, KeyError) as e:
        log.debug("查書目資料失敗：%s", e)
    return {}


def still_preparing(it: dict) -> bool:
    """PaperRead 還在處理這篇。狀態卡住超過 30 分鐘的（PaperRead 偶爾會留下過期的 queued）不再等。"""
    job = it.get("job") or {}
    if job.get("state") not in ("queued", "running"):
        return False
    try:
        from datetime import datetime
        return time.time() - datetime.fromisoformat(job.get("updated") or "").timestamp() < 1800
    except ValueError:
        return True


def digest(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- Zotero 本機 API

class Zotero:
    def __init__(self, base: str, key: str, dry: bool):
        self.base = base.rstrip("/") + "/api"
        self.key = key
        self.dry = dry
        self.server_id = ""

    def req(self, method: str, path: str, body=None, headers: dict | None = None, write: bool = False,
            timeout: float = 120):
        h = {"Zotero-API-Version": "3"}
        if write and not self.server_id:  # 寫入一定要帶 Zotero-Server-ID，它在任何一次讀取的回應裡
            self.ping()
        if self.server_id:
            h["Zotero-Server-ID"] = self.server_id
        if write and self.key:
            h["Zotero-API-Key"] = self.key
        h.update(headers or {})
        status, hd, raw = http(method, self.base + path, body, h, timeout)
        if hd and hd.get("Zotero-Server-ID"):
            self.server_id = hd.get("Zotero-Server-ID")
        if write and status == 401:
            raise SyncError("Zotero 拒絕寫入：寫入權限失效了，請重新執行 py sync.py --authorize")
        return status, hd, raw

    def ping(self) -> None:
        try:
            status, _, raw = self.req("GET", "/users/0/collections?limit=1&format=json")
        except OSError as e:
            raise SyncError(f"連不到 Zotero（{e}）：Zotero 沒開？") from None
        if status == 403:
            raise SyncError("Zotero 本機 API 沒開：Zotero 設定 → 進階 → 勾選「允許此電腦上的其他應用程式與 Zotero 通訊」")
        if status != 200:
            raise SyncError(f"Zotero 回應 {status}：{raw[:200]!r}")

    def get_all(self, path: str, **params) -> list:
        out, start = [], 0
        while True:
            q = urllib.parse.urlencode({**params, "format": "json", "limit": 100, "start": start})
            status, _, raw = self.req("GET", f"{path}?{q}")
            if status != 200:
                raise SyncError(f"讀 Zotero {path} 失敗：{status} {raw[:200]!r}")
            page = json.loads(raw)
            out += page
            if len(page) < 100:
                return out
            start += 100

    def pdf_bytes(self, att: dict) -> bytes | None:
        status, _, raw = self.req("GET", f"/users/0/items/{att['key']}/file/view/url")
        path = None
        if status == 200 and raw.strip().startswith(b"file:"):
            u = urllib.parse.urlparse(raw.decode("utf-8").strip())
            path = Path(urllib.request.url2pathname(urllib.parse.unquote(u.path)))
        elif att["data"].get("linkMode") == "linked_file" and att["data"].get("path"):
            path = Path(att["data"]["path"])
        if not path or not path.exists():
            return None
        data = path.read_bytes()
        return data if data.startswith(b"%PDF") else None

    def write_json(self, method: str, path: str, body, headers: dict | None = None):
        status, _, raw = self.req(method, path, body, headers, write=True)
        return status, (json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw)

    def create(self, kind: str, obj: dict) -> str:
        status, res = self.write_json("POST", f"/users/0/{kind}", [obj])
        if status != 200 or not isinstance(res, dict) or "0" not in (res.get("success") or res.get("successful") or {}):
            raise SyncError(f"在 Zotero 建立 {kind} 失敗：{status} {res}")
        ok = res.get("success") or {}
        return ok["0"] if "0" in ok else res["successful"]["0"]["key"]

    def upload_pdf(self, att_key: str, data: bytes, filename: str) -> None:
        form = urllib.parse.urlencode({"md5": hashlib.md5(data).hexdigest(), "filename": filename,
                                       "filesize": len(data), "mtime": int(time.time() * 1000)}).encode()
        hdr = {"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}
        status, res = self.write_json("POST", f"/users/0/items/{att_key}/file", form, hdr)
        if status != 200 or not isinstance(res, dict):
            raise SyncError(f"Zotero 上傳授權失敗：{status} {res}")
        if res.get("exists"):
            return
        body = (res.get("prefix") or "").encode("latin-1") + data + (res.get("suffix") or "").encode("latin-1")
        up_status, _, up_raw = http("POST", res["url"], body, {"Content-Type": res.get("contentType") or "application/pdf"})
        if up_status not in (200, 201, 204):
            raise SyncError(f"上傳 PDF 到 Zotero 失敗：{up_status} {up_raw[:200]!r}")
        status, res = self.write_json("POST", f"/users/0/items/{att_key}/file",
                                      urllib.parse.urlencode({"upload": res["uploadKey"]}).encode(), hdr)
        if status not in (200, 204):
            raise SyncError(f"Zotero 登記上傳失敗：{status} {res}")


# ---------------------------------------------------------------- PaperRead 本機服務

class PaperRead:
    def __init__(self, dry: bool):
        self.dry = dry
        info = load_json(PR_HOME / ".server.json", {}) or {}
        self.url = (info.get("url") or "").rstrip("/")
        if not self.url:
            raise SyncError("找不到 PaperRead 服務：PaperRead 沒開？")
        try:
            status, _, raw = http("GET", self.url + "/api/library", timeout=10)
        except OSError:
            raise SyncError("連不到 PaperRead：PaperRead 沒開？") from None
        if status != 200:
            raise SyncError(f"PaperRead 回應 {status}")
        lib = json.loads(raw)
        self.token = lib["token"]
        self.items = {it["id"]: it for it in lib["items"]}

    def post(self, path: str, body) -> dict:
        h = {"X-Token": self.token}
        if isinstance(body, (bytes, bytearray)):
            h["Content-Type"] = "application/pdf"
        status, _, raw = http("POST", self.url + path, body, h)
        if status != 200:
            try:
                msg = json.loads(raw).get("error") or ""
            except ValueError:
                msg = raw[:300].decode("utf-8", "replace")
            raise SyncError(f"PaperRead {path.split('?')[0]} 失敗（{status}）：{msg}")
        return json.loads(raw)

    def import_pdf(self, data: bytes, name: str, translate: bool) -> dict:
        q = urllib.parse.urlencode({"name": name, "translate": "1" if translate else "0"})
        return self.post(f"/api/import?{q}", data)

    def patch(self, pid: str, fields: dict) -> None:
        self.post(f"/api/p/{pid}/item", fields)

    def trash(self, pid: str) -> None:
        self.post(f"/api/p/{pid}/delete", {})

    def pdf(self, pid: str) -> bytes:
        status, _, raw = http("GET", f"{self.url}/p/{pid}/source.pdf")
        if status != 200 or not raw.startswith(b"%PDF"):
            raise SyncError(f"讀不到 PaperRead 論文 {pid} 的 PDF（{status}）")
        return raw


# ---------------------------------------------------------------- 同步本體

class Sync:
    def __init__(self, cfg: dict, dry: bool, allow_mass_delete: bool):
        self.cfg, self.dry, self.allow_mass_delete = cfg, dry, allow_mass_delete
        self.prefix, self.sep = cfg["tag_prefix"], cfg["collection_separator"]
        self.state = load_json(STATE_PATH, {}) or {}
        self.links: dict = self.state.setdefault("links", {})            # Zotero key → {pr, sets, zmeta}
        self.pr_gone: list = self.state.setdefault("pr_deleted", [])     # 在 PaperRead 刪掉的：不再匯入
        self.counts = {k: 0 for k in ("imported", "linked", "created_in_zotero", "trashed", "zotero_updates",
                                      "paperread_updates", "skipped_no_pdf", "deferred", "errors")}

    def act(self, msg: str) -> bool:
        """記錄一個動作；dry-run 時只列出來，回傳 False 讓呼叫端跳過實際寫入。"""
        log.info(("[試跑] " if self.dry else "") + msg)
        return not self.dry

    # ---- Zotero 收藏夾 ↔ 分類路徑
    def load_collections(self) -> None:
        cols = {c["key"]: c["data"] for c in self.z.get_all("/users/0/collections") if not c["data"].get("deleted")}
        self.col_path: dict[str, str] = {}
        for key in cols:
            names, k, seen = [], key, set()
            while k and k in cols and k not in seen:
                seen.add(k)
                names.append(cols[k]["name"])
                k = cols[k].get("parentCollection") or None
            self.col_path[key] = self.sep.join(reversed(names))
        self.col_key = {p: k for k, p in self.col_path.items()}

    def ensure_collection(self, path: str) -> str | None:
        if path in self.col_key:
            return self.col_key[path]
        parts = path.split(self.sep)
        parent = self.ensure_collection(self.sep.join(parts[:-1])) if len(parts) > 1 else False
        if not self.act(f"Zotero 新增收藏夾「{path}」"):
            self.col_key[path] = f"dry:{path}"
            return self.col_key[path]
        key = self.z.create("collections", {"name": parts[-1], "parentCollection": parent or False})
        self.col_key[path], self.col_path[key] = key, path
        return key

    # ---- 一篇論文在兩邊的分類集合
    def is_tag(self, name: str) -> bool:
        return self.cfg["sync_tags"] and name.startswith(self.prefix)

    def zotero_set(self, item: dict) -> set[str]:
        d = item["data"]
        out = {self.col_path[k] for k in d.get("collections") or [] if k in self.col_path}
        if self.cfg["sync_tags"]:
            for t in d.get("tags") or []:
                if t.get("type", 0) == 0 or self.cfg["sync_automatic_tags"]:
                    out.add(self.prefix + t["tag"])
        return out

    def write_zotero(self, item: dict, want: set[str]) -> None:
        d = item["data"]
        patch = {"collections": [k for k in (self.ensure_collection(p) for p in sorted(want) if not self.is_tag(p)) if k]}
        if self.cfg["sync_tags"]:
            tags = [{"tag": p[len(self.prefix):]} for p in sorted(want) if self.is_tag(p)]
            if not self.cfg["sync_automatic_tags"]:
                tags += [t for t in d.get("tags") or [] if t.get("type", 0) == 1]
            patch["tags"] = tags
        title = (d.get("title") or item["key"])[:60]
        if not self.act(f"Zotero 更新「{title}」的收藏夾 → {sorted(want)}"):
            return
        status, res = self.z.write_json("PATCH", f"/users/0/items/{item['key']}", patch,
                                        {"If-Unmodified-Since-Version": str(item.get("version") or d.get("version", 0))})
        if status not in (200, 204):
            raise SyncError(f"Zotero 更新失敗（{status}）：{res}")
        self.counts["zotero_updates"] += 1

    def write_paperread(self, pid: str, current: list[str], want: set[str], title: str) -> None:
        new = [t for t in current if t in want] + sorted(want - set(current))
        if not self.act(f"PaperRead 更新「{title[:60]}」的分類 → {new}"):
            return
        self.pr.patch(pid, {"tags": new})
        self.counts["paperread_updates"] += 1

    # ---- 主流程
    def run(self) -> None:
        self.pr = PaperRead(self.dry)
        self.z = Zotero(self.cfg["zotero_url"], self.cfg["zotero_api_key"], self.dry)
        self.z.ping()
        if not self.cfg["zotero_api_key"] and not self.dry:
            raise SyncError("還沒有 Zotero 寫入權限：先執行 py sync.py --authorize")
        self.load_collections()

        top = [i for i in self.z.get_all("/users/0/items/top") if i["data"]["itemType"] not in ("note", "annotation")]
        zitems = {i["key"]: i for i in top}
        pdfs: dict[str, list] = {}
        for a in self.z.get_all("/users/0/items", itemType="attachment"):
            d = a["data"]
            if d.get("contentType") == "application/pdf" and d.get("linkMode") in ("imported_file", "imported_url", "linked_file"):
                pdfs.setdefault(d.get("parentItem") or a["key"], []).append(a)
        log.debug("Zotero：%d 篇條目、%d 個收藏夾；PaperRead：%d 篇", len(zitems), len(self.col_path), len(self.pr.items))

        self.handle_deletions(zitems)
        self.import_from_zotero(zitems, pdfs)
        self.merge_all(zitems)
        if self.cfg["create_in_zotero"]:
            self.create_in_zotero()
        if not self.dry:
            save_json(STATE_PATH, self.state)
        summary = "，".join(f"{k}={v}" for k, v in self.counts.items() if v)
        log.info("同步完成%s", f"：{summary}" if summary else "，兩邊一致")

    def handle_deletions(self, zitems: dict) -> None:
        gone_z = [k for k in self.links if k not in zitems]
        gone_pr = [k for k, l in self.links.items() if k in zitems and l["pr"] not in self.pr.items]
        for k in gone_pr:  # 使用者在 PaperRead 刪掉：記住，之後不再從 Zotero 匯回來
            self.act(f"PaperRead 已刪除 {self.links[k]['pr']}，之後不再同步 Zotero 條目 {k}")
            if not self.dry:
                self.pr_gone.append(k)
                del self.links[k]
        if not gone_z:
            return
        limit = self.cfg["mass_delete_guard"]
        if len(gone_z) > limit and len(gone_z) > 0.2 * len(self.links) and not self.allow_mass_delete:
            log.warning("Zotero 少了 %d 篇已同步的條目，數量異常，這次先不刪 PaperRead 的論文。"
                        "確定要刪的話執行 py sync.py --allow-mass-delete", len(gone_z))
            return
        for k in gone_z:
            pid = self.links[k]["pr"]
            if pid in self.pr.items and self.cfg["trash_when_zotero_deletes"]:
                title = self.pr.items[pid].get("title_en") or pid
                if still_preparing(self.pr.items[pid]):
                    # PaperRead 處理中途搬走資料夾會讓它的背景執行緒當掉、之後的翻譯全卡在排隊中：等下一輪
                    log.info("「%s」PaperRead 還在處理，下一輪再移到回收站", title[:60])
                    continue
                if self.act(f"Zotero 已刪除 → PaperRead 把「{title[:60]}」移到回收站"):
                    self.pr.trash(pid)
                    self.pr.items.pop(pid, None)
                    self.counts["trashed"] += 1
            if not self.dry:
                del self.links[k]

    def import_from_zotero(self, zitems: dict, pdfs: dict) -> None:
        linked_pr = {l["pr"] for l in self.links.values()}
        by_doi = {it["doi"].lower(): pid for pid, it in self.pr.items.items() if it.get("doi") and pid not in linked_pr}
        budget = self.cfg["max_new_per_run"]
        for key, item in zitems.items():
            if key in self.links or key in self.pr_gone:
                continue
            d = item["data"]
            meta = meta_from_zotero(d)
            atts = pdfs.get(key) or []
            if d["itemType"] == "attachment":
                atts = atts or ([item] if d.get("contentType") == "application/pdf" else [])
            if meta.get("doi") and meta["doi"].lower() in by_doi:  # PaperRead 已有同一篇（不同 PDF 檔）：直接配對
                pid = by_doi.pop(meta["doi"].lower())
                if self.act(f"配對 Zotero「{meta.get('title_en', key)[:60]}」與 PaperRead {pid}（DOI 相同）"):
                    self.links[key] = {"pr": pid, "zmeta": digest(meta)}
                    linked_pr.add(pid)
                    self.counts["linked"] += 1
                continue
            data = None
            for att in atts:
                data = self.z.pdf_bytes(att)
                if data:
                    break
            if not data:
                if self.cfg["fetch_pdf_by_doi"] and budget > 0 and self.import_by_doi(key, item, meta):
                    budget -= 1
                    if key in self.links:
                        linked_pr.add(self.links[key]["pr"])
                elif key not in self.links:
                    self.counts["skipped_no_pdf"] += 1
                continue
            pid = hashlib.sha256(data).hexdigest()[:12]
            if pid in self.pr.items:
                if pid in linked_pr:
                    continue  # 同一個 PDF 已配給另一個 Zotero 條目（重複條目）
                if self.act(f"配對 Zotero「{meta.get('title_en', key)[:60]}」與 PaperRead {pid}（同一個 PDF）"):
                    self.links[key] = {"pr": pid, "zmeta": digest(meta)}
                    linked_pr.add(pid)
                    self.counts["linked"] += 1
                continue
            if budget <= 0:
                self.counts["deferred"] += 1
                continue
            budget -= 1
            if not self.act(f"匯入 PaperRead：「{meta.get('title_en', key)[:60]}」"):
                continue
            name = re.sub(r'[\\/:*?"<>|]+', " ", meta.get("title_en") or "paper")[:80] + ".pdf"
            res = self.pr.import_pdf(data, name, self.cfg["translate_on_import"])
            pid = res["id"]
            sets = self.zotero_set(item)
            self.pr.patch(pid, {"meta_override": meta, "tags": sorted(sets)})
            self.pr.items[pid] = {"id": pid, "tags": sorted(sets), "title_en": meta.get("title_en", ""),
                                  "meta_override": meta}
            self.links[key] = {"pr": pid, "sets": sorted(sets), "zmeta": digest(meta)}
            linked_pr.add(pid)
            self.counts["imported"] += 1

    def import_by_doi(self, key: str, item: dict, meta: dict) -> bool:
        """Zotero 條目沒有 PDF：請 PaperRead 用 DOI 找開放取用版本，找到就把 PDF 也附回 Zotero。"""
        doi = meta.get("doi")
        if not doi:
            return False
        failed = self.state.setdefault("doi_failed", {})
        if time.time() - failed.get(key, 0) < 86400:  # 找不到公開 PDF 的（或 Semantic Scholar 限流），隔天再試
            return False
        title = meta.get("title_en", key)[:60]
        if not self.act(f"用 DOI 下載 PDF 並匯入 PaperRead：「{title}」"):
            return True
        try:
            res = self.pr.post("/api/import-url", {"ref": doi, "translate": self.cfg["translate_on_import"]})
        except SyncError as e:
            failed[key] = time.time()
            log.info("  找不到公開 PDF，明天再試（%s）", str(e).split("：", 1)[-1][:160])
            return False
        failed.pop(key, None)
        pid = res["id"]
        sets = self.zotero_set(item)
        self.pr.patch(pid, {"meta_override": meta, "tags": sorted(sets)})
        self.pr.items[pid] = {"id": pid, "tags": sorted(sets), "title_en": meta.get("title_en", ""), "meta_override": meta}
        self.links[key] = {"pr": pid, "sets": sorted(sets), "zmeta": digest(meta)}
        self.counts["imported"] += 1
        try:
            data = self.pr.pdf(pid)
            filename = re.sub(r'[\\/:*?"<>|]+', " ", meta.get("title_en") or "paper")[:80].strip() + ".pdf"
            att = self.z.create("items", {"itemType": "attachment", "parentItem": key, "linkMode": "imported_file",
                                          "title": "PDF", "contentType": "application/pdf", "filename": filename,
                                          "tags": [], "relations": {}})
            self.z.upload_pdf(att, data, filename)
            log.info("  已把 PDF 附回 Zotero")
        except SyncError as e:
            self.counts["errors"] += 1
            log.error("  PDF 附回 Zotero 失敗：%s", e)
        return True

    def merge_all(self, zitems: dict) -> None:
        for key, link in list(self.links.items()):
            item, pr = zitems.get(key), self.pr.items.get(link["pr"])
            if not item or not pr:
                continue
            try:
                self.merge_one(key, link, item, pr)
            except SyncError as e:
                self.counts["errors"] += 1
                log.error("%s", e)

    def merge_one(self, key: str, link: dict, item: dict, pr: dict) -> None:
        # Zotero 改了書目資料 → 更新 PaperRead 顯示的標題／作者等
        meta = meta_from_zotero(item["data"])
        if "zmeta" in link and link["zmeta"] != digest(meta):
            if self.act(f"PaperRead 更新「{meta.get('title_en', key)[:60]}」的書目資料（Zotero 有修改）"):
                self.pr.patch(pr["id"], {"meta_override": {**(pr.get("meta_override") or {}), **meta}})
        if not self.dry:
            link["zmeta"] = digest(meta)

        sz, sp = self.zotero_set(item), set(pr.get("tags") or [])
        base = set(link["sets"]) if "sets" in link else None
        if base is None:
            want = sz | sp
        else:
            want = (base & sz & sp) | (sz - base) | (sp - base)
        title = pr.get("title_en") or pr.get("title_zh") or item["data"].get("title") or key
        if want != sp:
            self.write_paperread(pr["id"], pr.get("tags") or [], want, title)
        if want != sz:
            self.write_zotero(item, want)
        if not self.dry:
            link["sets"] = sorted(want)

    def create_in_zotero(self) -> None:
        linked_pr = {l["pr"] for l in self.links.values()}
        budget = self.cfg["max_new_per_run"]
        for pid, it in self.pr.items.items():
            if pid in linked_pr:
                continue
            if still_preparing(it):
                continue  # PaperRead 還在解析這篇（標題、作者還沒出來），下一輪再建
            if budget <= 0:
                self.counts["deferred"] += 1
                continue
            budget -= 1
            title = it.get("title_en") or it.get("title_zh") or pid
            tags = set(it.get("tags") or [])
            if not self.act(f"Zotero 新增條目：「{title[:60]}」（含 PDF）"):
                continue
            try:
                key = self.create_zotero_item(pid, it, tags)
            except SyncError as e:
                self.counts["errors"] += 1
                log.error("%s", e)
                continue
            self.links[key] = {"pr": pid, "sets": sorted(tags)}
            self.counts["created_in_zotero"] += 1

    def create_zotero_item(self, pid: str, it: dict, tags: set[str]) -> str:
        arxiv = ARXIV_RE.search(it.get("arxiv") or "")
        arxiv = arxiv.group(1) if arxiv else ""
        if not it.get("authors") and (arxiv or it.get("doi")):  # PaperRead 沒抓到書目資料：自己補，也補回 PaperRead
            found = lookup_meta(arxiv, it.get("doi") or "")
            if found:
                it = {**it, **found, "year": (YEAR_RE.search(found.get("date", "")) or [it.get("year")])[0]}
                self.pr.patch(pid, {"meta_override": {**(it.get("meta_override") or {}), **found}})
                log.info("  已從 %s 補上書目資料", "arXiv" if arxiv else "Crossref")
        kind = "preprint" if arxiv and not it.get("venue") else "journalArticle"
        obj = {"itemType": kind, "title": it.get("title_en") or it.get("title_zh") or pid,
               "creators": creators_from(it.get("authors") or ""),
               "date": it.get("date") or str(it.get("year") or ""), "url": it.get("url") or "", "DOI": it.get("doi") or "",
               "collections": [k for k in (self.ensure_collection(p) for p in sorted(tags) if not self.is_tag(p)) if k],
               "tags": [{"tag": p[len(self.prefix):]} for p in sorted(tags) if self.is_tag(p)]}
        if kind == "preprint":
            obj.update({"repository": "arXiv", "archiveID": f"arXiv:{arxiv}"})
        else:
            obj["publicationTitle"] = it.get("venue") or ""
        key = self.z.create("items", obj)
        data = self.pr.pdf(pid)
        filename = re.sub(r'[\\/:*?"<>|]+', " ", obj["title"])[:80].strip() + ".pdf"
        att = self.z.create("items", {"itemType": "attachment", "parentItem": key, "linkMode": "imported_file",
                                      "title": "PDF", "contentType": "application/pdf", "filename": filename,
                                      "tags": [], "relations": {}})
        self.z.upload_pdf(att, data, filename)
        return key


# ---------------------------------------------------------------- 申請 Zotero 寫入權限

def authorize(cfg: dict) -> None:
    z = Zotero(cfg["zotero_url"], "", False)
    z.ping()
    print("已向 Zotero 申請寫入權限，請切到 Zotero 視窗按「永遠允許」……")
    try:  # Zotero 等使用者在對話框按下去才回應
        status, _, raw = z.req("POST", "/local/authorize", {"appName": "PaperRead 同步"}, timeout=600)
    except TimeoutError:
        raise SyncError("等了 10 分鐘還沒在 Zotero 按允許，請再執行一次 --authorize") from None
    try:
        res = json.loads(raw)
    except ValueError:
        res = {}
    key = res.get("key") or res.get("apiKey") if isinstance(res, dict) else None
    if status != 200 or not key:
        raise SyncError(f"沒拿到寫入權限（{status}）：{raw[:300]!r}")
    cfg = {**load_json(CONFIG_PATH, {}), "zotero_api_key": key}
    save_json(CONFIG_PATH, cfg)
    print("完成，已存進 config.json。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Zotero ⇄ PaperRead 同步")
    ap.add_argument("--authorize", action="store_true", help="向 Zotero 申請寫入權限")
    ap.add_argument("--dry-run", action="store_true", help="只列出會做的事")
    ap.add_argument("--allow-mass-delete", action="store_true", help="Zotero 一次刪很多篇時也照樣同步刪除")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    setup_logging(a.verbose)
    cfg = load_config()
    try:
        if a.authorize:
            authorize(cfg)
            return 0
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 1800:
            log.info("上一次同步還在跑，這次略過")
            return 0
        LOCK_PATH.write_text(str(os.getpid()))
        try:
            Sync(cfg, a.dry_run, a.allow_mass_delete).run()
        finally:
            LOCK_PATH.unlink(missing_ok=True)
        return 0
    except SyncError as e:
        log.warning("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
