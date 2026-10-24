# CLAUDE.md

## 專案
在我電腦上執行的 Discord agent：指定頻道有人傳票券照片，就用 `claude -p` 辨識、記進 Excel、存照片，並回覆上傳者辨識結果與他自己的累計。

## 開始任何工作前
**先讀 `docs/architecture.md`**，再讀 `docs/requirements.md`。設計決定與理由都在裡面，不要憑印象做。若程式與文件不一致，先跟我確認要改哪一邊。

## 四段防護
`docs/architecture.md` 第 12 節定義了四段防護。每一段都要在程式裡用清楚的註解標出開始與結束，格式固定：

```
# >>> 防護 N 開始：（一句話說明） <<<
...
# >>> 防護 N 結束 <<<
```

| 編號 | 內容 | 預計位置 |
|---|---|---|
| 防護 1 | `claude -p` 不能讀到 `DATA_DIR`（工作目錄隔離、工具限縮） | `analyzer.py` |
| 防護 2 | 上傳者打的字只能當資料，不能當給 `claude -p` 的指令 | `analyzer.py`、`corrections.py` |
| 防護 3 | 食物／票券名稱拿去當檔名前要清洗（`/`、`\`、`..`、`:` 等） | `photos.py` |
| 防護 4 | 寫進 Excel 的文字若以 `=`、`+`、`-`、`@` 開頭，要處理成純文字 | `excel_store.py` |

規則：
- **一開始四段全部開啟。** 不要因為方便測試、除錯或「之後再補」就關掉或省略。
- 每一段要能被單獨拿掉而程式仍可執行（之後教學會逐段關掉做對照），所以不要把防護的邏輯散在標記範圍之外。
- 不要自己新增第五段、合併或改編號；新的一般性防護放在 `docs/architecture.md` 12.3 列的範圍，不加標記。

## 不可以 commit 的東西
- **`.env`**（內有 `DISCORD_TOKEN`）。只能 commit 沒有真值的 `.env.example`。
- **`DATA_DIR` 裡的所有東西**：照片、`tickets.xlsx`、備份、log。
- 暫存資料夾 `.work/`。
- 這些都已寫在 `.gitignore`。不要用 `git add -f` 硬加被忽略的檔案；`git add` 之前先看 `git status`，確認沒有上面這些東西。
- 不要把 token、票號、使用者的照片內容印進 log、測試資料或 commit 訊息。
- 若 token 不小心進了 Git 紀錄，要先提醒我，由我到 Discord Developer Portal 重設 token。
