# CLAUDE.md

## 專案
在我電腦上執行的 Discord agent：指定頻道有人傳票券照片，就用 `claude -p` 辨識、記進 Excel、存照片，並回覆上傳者辨識結果與他自己的累計。

## 開始任何工作前
**先讀 `docs/architecture.md`**，再讀 `docs/requirements.md`。設計決定與理由都在裡面，不要憑印象做。若程式與文件不一致，先跟我確認要改哪一邊。

## 四段防護
`docs/architecture.md` 第 12 節定義了四段防護。每一段都要在程式裡用清楚的註解標出開始與結束，格式固定（名稱不可改，全形空格 `　` 在「開始」前）：

```
# ==== 防護 1：Claude 的檔案範圍隔離　開始 ====
...
# ==== 防護 1 結束 ====
```

| 編號 | 名稱 | 內容 | 位置 |
|---|---|---|---|
| 防護 1 | Claude 的檔案範圍隔離 | `claude -p` 不能讀到 `DATA_DIR`：不給任何工具、照片用 stdin 傳、關掉 MCP／技能／個人設定、dontAsk、deny＋fence、空的暫存工作目錄、環境變數白名單、每次檢查工具清單 | `claude_runner.py` |
| 防護 2 | 使用者輸入隔離 | 使用者文字只當資料：不用 shell、只傳參數清單、文字只走 stdin、包在隨機分隔區塊、欄位白名單 | `claude_runner.py`（緊接防護 1） |
| 防護 3 | 檔名清洗 | 由 AI 結果或使用者輸入產生的檔名，先清掉 `/`、`\`、`..`、`:` 等，並驗證最終路徑仍在 `photos/` 內 | `photos.py` |
| 防護 4 | Excel 公式注入防護 | 寫進 Excel 的文字若以 `=`、`+`、`-`、`@`（或 Tab、CR）開頭，一律存成純文字 | `excel_store.py` |

規則：
- **一開始四段全部啟用。** 不要因為方便測試、除錯或「之後再補」就關掉、註解掉或省略。
- 每一段是**一個連續區塊**、一組標記。每一段要能被單獨拿掉而程式仍可執行（之後教學會逐段關掉做對照），所以不要把防護的邏輯散在標記範圍之外。
- 防護必須有**實際的程式碼和測試**，不能只在文件裡描述。測試 `tests/test_guards_present.py` 會檢查剛好四組標記、成對、名稱正確。
- 不要自己新增第五段、合併或改編號；新的一般性防護放在 `docs/architecture.md` 12.3 列的範圍，不加標記。
- 某段防護若在某功能不適用，在文件說明原因，不要為了湊數放無效的程式。
- 改動 `claude_runner.py` 的參數後，要重新用合成資料驗證（見 `docs/architecture.md` 12.4），不能只靠讀文件。

## 不可以 commit 的東西
- **`.env`**（內有 `DISCORD_TOKEN`）與 `.env.*`。只能 commit 沒有真值的 `.env.example`。**不要讀取或印出 `.env` 的內容。**
- **`DATA_DIR` 裡的所有東西**：照片、`tickets.xlsx`、備份、log。`DATA_DIR` 必須在專案資料夾之外。
- **所有圖片與 PDF**：`.gitignore` 預設忽略。唯一例外是 `tests/fixtures/synthetic/`，只能放**合成的、假的**測試檔案（不得是真實票券）。
- 這些都已寫在 `.gitignore`。不要用 `git add -f` 硬加被忽略的檔案；`git add` 之前先看 `git status`，確認沒有上面這些東西。
- 實驗、除錯用的暫存檔放在系統暫存區或 scratchpad，不要放進專案。
- 不要把 token、票號、使用者的照片內容印進 log、測試資料或 commit 訊息。
- 若 token 不小心進了 Git 紀錄，要先提醒我，由我到 Discord Developer Portal 重設 token。
