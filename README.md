# Homework3：票券記錄 Agent

在我電腦上執行的 Discord bot：指定頻道有人傳票券照片，就用 `claude -p` 辨識、記進 Excel、存照片，並回覆上傳者辨識結果與他自己的累計。

**狀態：已實作完成。** 驗證分三層，請不要混在一起看：① 自動化測試（假的 Discord 事件、合成資料；每次修改都跑完整測試套件）；② 真實 `claude -p` 搭配合成資料的測試（預設跳過，`--run-live` 才執行）；③ 真實 Discord 測試（已由使用者與朋友測試；哪些有紀錄、哪些是使用者回報、哪些**沒有**在真實 Discord 驗證，見 `docs/architecture.md` 12.4 的 D4）。尚未正式部署（只在本機執行）。

## 文件
- [`docs/requirements.md`](docs/requirements.md)：需求說明
- [`docs/architecture.md`](docs/architecture.md)：架構、技術選擇、安全防護與實測結果（動手前先讀）
- [`docs/discord-server-setup.md`](docs/discord-server-setup.md)、[`docs/discord-bot-setup.md`](docs/discord-bot-setup.md)：Discord 設定教學
- [`CLAUDE.md`](CLAUDE.md)：給 Claude Code 的專案規則

## 使用說明（在指定的 Discord 頻道裡）

**傳票券**：直接在頻道傳照片或 PDF 電子票券（JPG、PNG、WebP、PDF；每個檔案 10 MB 以內）。agent 先回「⏳ 處理中」，處理完把那則訊息改成結果，並附上**你自己**的當月與歷史累計。

**限制**（這些數字寫在 `docs/architecture.md` 第 7 節，可以調整）：
- 一則訊息**最多處理 5 張**照片，多的不處理並提醒。
- 佇列**最多排 50 張**，滿了會回覆「目前排隊的照片太多，請稍後再傳」。
- 每人每分鐘 8 次（上限）：**每張照片各算一次**，更正和有效的指令也各算一次；格式錯誤的指令、不支援的附件不計次。超過會回覆「傳太快了」，同一個視窗只提醒一次。
- 照片一次處理一張，依序進行。

**指令**：
| 指令 | 作用 |
|---|---|
| `!我的票券` | 列出你本月的紀錄（有流水號），附累計 |
| `!我的票券 2026-09`、`!我的票券 全部` | 指定月份、全部 |
| `!刪除 編號` | 先請你回覆「確定」（60 秒內）才會刪；只是標記，Excel 那列和照片都保留 |
| `!說明` | 顯示功能與用法說明（固定文字，不讀 Excel、不用 Claude）。後面不能加其他內容；和其他指令共用每人每分鐘 8 次的限流 |

**更正**：回覆 bot 的**結果訊息**，用打字說要改什麼，例如「金額應該是 1500」。只有原上傳者可以更正。可以改：票券種類、名稱、日期時間、地點、座位或票種、金額、幣別；也可以說「這不是票券」撤回這一筆。

**指令優先於更正**：回覆結果訊息時，如果你的內容是 `!` 開頭的指令（例如 `!我的票券`），agent 會把它當成**指令**執行，**不會**當成更正文字。要更正請直接寫要改的內容，不要以 `!` 開頭。

**票號**：票號收在 Excel「其他資訊」欄的最前面，回覆和查詢**都不會顯示**。票號**不能在 Discord 更正**（你提到票號時，agent 會回覆「票號為了安全，不能在 Discord 更正」，請你直接編輯 Excel）；重複上傳同一張票時，已經記錄的票號也不會被覆蓋或刪除。

## 安裝與設定
需要 macOS、[uv](https://docs.astral.sh/uv/)、已登入的 `claude` 命令列。

```bash
uv sync                                  # 安裝套件（Python 3.12）
cp .env.example .env && chmod 600 .env   # 然後用編輯器填 .env（不要貼進任何對話）
uv run ticket-agent --check              # 只檢查設定，不顯示任何值（不連線）
uv run pytest                            # 跑測試（不呼叫真正的 claude）
uv run pytest tests/test_live_claude.py --run-live   # 真的呼叫 claude -p 驗證隔離（只用合成資料，會用掉一點額度）
```

`.env` 的項目：`DISCORD_TOKEN`（**機密**）、`CHANNEL_ID`、`CLAUDE_MODEL`、`DATA_DIR`（必須在專案資料夾之外）。**`.env` 絕對不能 commit。**

## 啟動 agent

```bash
uv run ticket-agent                      # 檢查設定 → 連上 Discord → 監看指定頻道，Ctrl+C 結束
```

- 會連到真正的 Discord，並用掉 Claude 額度，所以**請先確認 `docs/discord-server-setup.md`、`docs/discord-bot-setup.md` 都做完**（尤其是 Message Content Intent 已開啟並儲存）。
- 只處理 `CHANNEL_ID` 那一個頻道；私訊、其他頻道、討論串、其他 bot 的訊息一律不處理。
- 同一個 `DATA_DIR` 同一時間只能有一個 agent；開第二個會直接報錯。
- **終端機的提示**：連線成功且找得到指定頻道時，會顯示「已連線，開始監看指定的頻道。」；如果找不到可用的文字頻道，會顯示「⚠️ 警告：…目前不會處理任何訊息」，請先處理再傳照片。提示不含 Token、頻道編號或任何訊息內容；詳細紀錄在 `DATA_DIR/logs/agent.log`。
- 連線失敗時會說明原因（Token 不正確、沒開 Message Content Intent、另一個 agent 正在執行），不會印出 Token。
- 只申請 3 個 Gateway intents：伺服器、伺服器裡的訊息、訊息內容。不要成員名單、上線狀態、私訊。
- 真實 Discord 測試的範圍與沒有驗證的項目（例如各種安全攻擊並沒有在真實 Discord 逐項測），列在 `docs/architecture.md` 12.4 的 D4。請先在測試用的 Server、用合成票券試。

## 四段安全防護的位置
程式裡每一段防護都用固定格式的註解標出開始與結束，一開始全部啟用。細節與原因見 `docs/architecture.md` 第 12 節。

| 防護 | 內容 | 檔案 | 約略行數 |
|---|---|---|---|
| 1 | Claude 的檔案範圍隔離 | `src/ticket_agent/claude_runner.py` | 第 118–191 行（約 74 行）✅ 已實作，有測試與真實 claude 驗證 |
| 2 | 使用者輸入隔離 | `src/ticket_agent/claude_runner.py`（緊接防護 1） | 第 194–228 行（約 35 行）✅ 已實作，有測試與真實 claude 驗證 |
| 3 | 檔名清洗 | `src/ticket_agent/photos.py` | 第 41–111 行（約 71 行）✅ 已實作並有測試 |
| 4 | Excel 公式注入防護 | `src/ticket_agent/excel_store.py` | 第 55–86 行（約 32 行）✅ 已實作並有測試 |

**即時的實際位置與行數**：`uv run ticket-agent --guards`。這張表的行數會在每一段完成後更新。

## 私人資料
- `.gitignore` 預設忽略 `.env`、`.env.*`（`.env.example` 例外）、所有圖片與 PDF、`*.xlsx`、`logs/`。
- 唯一例外：`tests/fixtures/synthetic/`，只能放**合成的假**測試檔案，不得放真實票券。
- Excel 與照片存在 `DATA_DIR`（專案之外），不進 Git。
