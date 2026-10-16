# zotero-paperread-bridge

在 [Zotero](https://www.zotero.org/) 管理文獻，在 [PaperRead](https://github.com/YuriyLin/PaperRead) 閱讀、翻譯、做筆記。
這支腳本讓兩邊的論文和分類自動保持一致：在 Zotero 存一篇論文，它就會出現在 PaperRead。

只用 Python 標準函式庫，透過兩邊的本機 API 溝通，資料不會離開你的電腦。

## 會同步什麼

| 動作 | 結果 |
|---|---|
| Zotero 存了帶 PDF 的論文 | 匯入 PaperRead，帶入標題、作者、年份、期刊、DOI（預設不自動翻譯） |
| Zotero 存了沒有 PDF 的論文 | 先略過，等 Zotero 附上 PDF 後下一輪自動匯入 |
| 直接匯入 PaperRead 的論文 | 在 Zotero 建條目並附上 PDF；書目資料不完整時從 arXiv／Crossref 補齊 |
| Zotero 收藏夾「A/B」 ⇄ PaperRead 分類「A/B」 | 雙向：任何一邊新增或移除，另一邊都會跟著改（用上次同步結果做三方合併） |
| Zotero 修改書目資料 | 更新 PaperRead 顯示的標題、作者等 |
| Zotero 刪除論文 | PaperRead 移到回收站（可還原） |
| PaperRead 刪除論文 | Zotero 不動，之後也不再同步這篇 |

同一個 PDF 不會重複匯入（PaperRead 以 PDF 的 SHA-256 當 ID），DOI 相同的論文會直接配對。

## 需求

- Windows（其他平台腳本本身可用，排程需自行設定）
- Python 3.10 以上
- Zotero 10 以上（本機 API 需支援寫入）
- PaperRead 桌面版

## 安裝

1. 下載這個儲存庫，放在固定的資料夾。
2. Zotero → 設定 → 進階，勾選 **允許此電腦上的其他應用程式與 Zotero 通訊**。
3. 打開 Zotero 和 PaperRead，申請寫入權限（Zotero 會跳出對話框，選「永遠允許」）：

   ```
   py sync.py --authorize
   ```

   第一次執行會從預設值產生 `config.json`，金鑰也存在這裡。`config.example.json` 是欄位說明用的範本。
4. 先試跑，確認會做的事符合預期：

   ```
   py sync.py --dry-run -v
   ```

5. 正式同步一次：

   ```
   py sync.py
   ```

## 自動同步（Windows 工作排程器）

在這個資料夾開 PowerShell 執行，之後每分鐘在背景同步一次，不會跳出視窗。Zotero 或 PaperRead 沒開時會直接略過。

```powershell
$pyw = (py -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))")
$action = New-ScheduledTaskAction -Execute $pyw -Argument "-I `"$PWD\sync.py`"" -WorkingDirectory $PWD
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "PaperRead Zotero Sync" -Action $action -Trigger $trigger -Settings $settings
```

停用：

```
schtasks /Delete /TN "PaperRead Zotero Sync" /F
```

## 設定（config.json）

| 欄位 | 預設 | 說明 |
|---|---|---|
| `translate_on_import` | `false` | 匯入 PaperRead 後自動翻譯（會用到模型額度） |
| `trash_when_zotero_deletes` | `true` | Zotero 刪除 → PaperRead 移到回收站 |
| `create_in_zotero` | `true` | PaperRead 自己匯入的論文建到 Zotero |
| `fetch_pdf_by_doi` | `false` | Zotero 沒有 PDF 時，請 PaperRead 用 DOI 找開放取用版本並附回 Zotero |
| `sync_tags` | `false` | 同步 Zotero 標籤，對應 PaperRead 的「#標籤」分類 |
| `sync_automatic_tags` | `false` | 連 Zotero 的自動標籤也同步 |
| `collection_separator` | `/` | 子收藏夾在 PaperRead 分類名中的分隔符 |
| `max_new_per_run` | `10` | 每輪最多新增幾篇（每個方向） |
| `mass_delete_guard` | `5` | 一次要刪超過這個數量且超過 20% 時先停手，需加 `--allow-mass-delete` |

執行紀錄寫在 `sync.log`；`state.json` 記錄兩邊的對應關係，刪掉的話下次會重新配對。

## 注意事項

- PaperRead 的分類是單層的，子收藏夾會變成「父/子」這樣的分類名；在 PaperRead 改分類名，Zotero 會建新收藏夾，舊的空收藏夾不會自動刪除。
- 從 PubMed 等來源匯入的 Zotero 條目常帶大量關鍵字標籤，所以 `sync_tags` 預設關閉。
- 出版社網站多半有防機器人驗證，`fetch_pdf_by_doi` 只抓得到開放取用版本。訂閱期刊請用瀏覽器加 Zotero Connector 存 PDF，同步會接手。
- PaperRead 正在處理某篇論文時不會把它移到回收站（處理途中搬走資料夾會讓 PaperRead 的翻譯佇列卡住），會等下一輪。
