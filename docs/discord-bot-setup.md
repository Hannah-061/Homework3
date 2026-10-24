# Discord Bot 設定教學

這份教學帶你在 Discord 開發者後台建立 bot、取得 Bot Token、安全地存放它，並把 bot 邀請進你自己的 Server。

**做這份之前**，請先完成 [`discord-server-setup.md`](discord-server-setup.md)：你需要已經有自己的 Server、專用頻道，並且已經取得 `CHANNEL_ID`。

## 怎麼讀這份文件

- 操作位置、按鈕名稱一律用**粗體**；英文介面名稱放在括號裡或是直接寫英文。開發者後台**主要是英文介面**，中文版 Discord 的開發者後台也大多是英文，所以這份文件以英文名稱為主。
- 每個步驟前面有標記：
  - ✅ 已對照 Discord 官方文件確認
  - ⚠️ 依一般使用經驗寫成，**未能在官方頁面逐字確認**。如果畫面跟文字不同，請以實際畫面為準。
- 本文件寫於 2026-10-25，操作環境是 macOS。
- 查證說明放在文件最後。

> [!CAUTION]
> **Bot Token 是機密，等同於這個 bot 的密碼。**
> - **絕對不能**貼給任何人。
> - **絕對不能**貼進 ChatGPT、Claude 或任何 AI 對話框。
> - **絕對不能** commit 進 Git，也不能寫進程式碼、README、作業報告、截圖。
> - 拿到 Token 的人可以完全控制你的 bot：用它發訊息、讀頻道、做任何 bot 能做的事。
>
> 如果 Token 不小心外流，**立刻**照第 12 節重設。

---

## 1. 進入 Discord Developer Portal

Discord Developer Portal（開發者後台）是 Discord 提供給開發者管理 bot 的網站。

1. 打開瀏覽器（Safari、Chrome 都可以）。
2. 前往 <https://discord.com/developers/applications> ✅。
3. 如果出現登入畫面，用你建立 Server 的**同一個 Discord 帳號**登入。
4. 登入後會看到 **Applications** 頁面，列出你的應用程式（一開始是空的）。

---

## 2. 建立 Application

Application（應用程式）是 Discord 裡「一個開發者專案」的名稱，bot 會附在它底下。

1. 在 **Applications** 頁面右上角，點 **New Application** ⚠️（藍色按鈕）。
2. 在 **Name** 欄位輸入名字，例如 `票券記錄 Agent`。這個名字會是 bot 的預設顯示名字，之後可以改。
3. 勾選同意 Discord 的開發者條款 ⚠️（有出現這個勾選框才需要）。
4. 點 **Create** ✅。
5. 你會進入這個 Application 的 **General Information** 頁面 ✅。

在 **General Information** 頁面上，你會看到 **Application ID**（一串數字）。**先點旁邊的 Copy 複製，貼到暫時的記事本**，第 9 步產生邀請連結時可能會用到。Application ID 不是機密。

---

## 3. 建立並設定 Bot

現在的 Developer Portal 建立 Application 時，一般會自動附帶一個 bot 帳號 ⚠️，不需要另外「新增 bot」。

1. 在頁面**左側選單**，點 **Bot** ✅。
2. 往下看到 **Public Bot** 這個開關 ⚠️：**關掉（Off）**。
   - 原因：關掉之後，只有你（Application 擁有者）能把這個 bot 邀請進伺服器，別人拿到你的 Application ID 也不能自己拉去用。
3. 如果有 **Requires OAuth2 Code Grant** 這個選項 ⚠️，保持**關閉**。
4. 先不要離開這個 **Bot** 頁面，下一步要在這裡開啟 Intent。

---

## 4. 開啟 Message Content Intent

### Intent 是什麼？
Intent（意圖）是 bot 向 Discord 申請的「我想收到哪一類資料」。Discord 為了保護隱私，預設不會把「訊息內容」給 bot，要主動申請、在後台打開。其中「訊息內容」被歸類為「特殊權限」（Privileged Intent）。

### 為什麼這個 agent 需要它？
依 `docs/architecture.md` 第 3.3 節，agent 需要讀取以下內容：

| 需要讀的東西 | 用在哪裡 |
|---|---|
| 訊息的**附件**（票券照片、PDF） | 這是整個 agent 的核心：沒有照片就沒辦法辨識 |
| `!我的票券`、`!刪除 編號` 這類**指令文字** | 查詢和刪除功能 |
| 上傳者回覆的**更正文字**（「金額應該是 1200」）、以及回覆「確定」 | 打字更正和刪除確認 |

### 不開啟會發生什麼事？
依 Discord 官方說明，沒有 Message Content Intent 時，bot 收到的訊息**內容欄位是空的** ✅，附件、嵌入等資訊也可能一併拿不到 ⚠️。實際上你會看到：

- bot 連得上線（成員列表顯示在線），但**對任何訊息都沒反應**，指令不會回應。
- 照片傳進來，agent 當作沒看到。
- 如果程式有要求這個 intent，但後台沒打開，agent 啟動時會直接報錯並停下來（discord.py 的錯誤名稱是 `PrivilegedIntentsRequired`）。

這是初學者最常遇到的「bot 為什麼沒反應」原因。

### 操作步驟 ✅
1. 在 **Bot** 頁面往下捲，找到 **Privileged Gateway Intents** 區塊。
2. 把 **Message Content Intent** 的開關**打開**（變成藍色或綠色）。
3. **另外兩個保持關閉**：**Presence Intent**、**Server Members Intent**。我們用不到，開了沒有好處。
4. 頁面下方會跳出一條提示，點 **Save Changes** 儲存。**一定要按儲存**，不然剛剛的開關會恢復。

> [!NOTE]
> 只有「在 100 個以上伺服器裡」的 bot，才需要向 Discord 申請審核才能用這個 intent。你的 bot 只在自己的 Server 裡，不受影響。

---

## 5. 取得 Bot Token

Bot Token 是 bot 登入 Discord 時使用的「密碼」。你的 agent 之後就是用它連上 Discord。

> [!CAUTION]
> 在做這一步之前，請先確認：你旁邊沒有人在看你的螢幕、你沒有在錄影、也沒有在分享畫面（例如 Zoom、Teams、Discord 直播）。

1. 仍在 **Bot** 頁面，找到 **Token** 區塊。
2. 點 **Reset Token** ✅。
3. 跳出確認視窗，點 **Yes, do it!** ⚠️（按鈕文字可能略有不同，意思是確認重設）。
4. 如果 Discord 要求輸入密碼或兩步驟驗證碼，照著輸入 ⚠️。
5. 畫面會出現一串很長的字（Token）。**馬上點旁邊的 Copy 複製**。
6. **接著立刻跳到第 6 步**，把它存進 `.env`，不要貼到別的地方。

> [!WARNING]
> 官方文件明講：**Token 只會顯示這一次**，離開或重新整理頁面後就看不到了 ✅。如果沒複製到就離開了，只要再點一次 **Reset Token** 重新產生就好（舊的 Token 同時失效）。
>
> 也請不要對 Token 截圖。

---

## 6. 安全地保存 Token

### 做法：放在專案根目錄的 `.env` 檔

`.env` 是一個純文字檔，專門放「設定與機密」，**不寫進程式碼**。程式啟動時才去讀它。你的 `.gitignore` 已經寫了 `.env` 和 `.env.*`，會擋掉不讓它被 commit（`.env.example` 範本檔例外，那個檔案只放欄位名稱，不放真值）。

**請你自己在 Terminal 操作**（下面是 macOS 的「終端機」/ Terminal App）：

1. 打開 **Terminal**（按 `Command + 空白鍵`，輸入 `Terminal`，按 Enter）。
2. 進到專案資料夾：
   ```bash
   cd ~/Homework3
   ```
3. 建立 `.env`，並設成**只有你自己能讀寫**：
   ```bash
   touch .env
   chmod 600 .env
   ```
4. 用 nano（Terminal 內建的簡單文字編輯器）打開它：
   ```bash
   nano .env
   ```
5. 輸入以下四行。**等號兩邊不要有空格，不要加引號**：
   ```
   DISCORD_TOKEN=貼上剛剛複製的 Token
   CHANNEL_ID=貼上 discord-server-setup.md 複製的頻道 ID
   CLAUDE_MODEL=
   DATA_DIR=
   ```
   - `DISCORD_TOKEN`：貼 Token（`Command + V`）。
   - `CHANNEL_ID`：貼頻道 ID。
   - `CLAUDE_MODEL`、`DATA_DIR`：現在先留空，等開始寫程式時再填。`DATA_DIR` 建議放在專案資料夾**外面**，例如 `/Users/你的帳號/ticket-data`。
6. 存檔離開：按 `Control + O` → 按 `Enter` → 按 `Control + X`。

### 存好之後，確認 Git 不會把它收進去
```bash
git check-ignore -v .env
```
- 預期看到一行以 `.gitignore:` 開頭、結尾是 `.env` 的輸出。這代表 `.env` 有被忽略。
- 如果**什麼都沒印出來**，代表沒被擋，**請先不要 `git add`**，回頭檢查 `.gitignore`。

再看一次：
```bash
git status
```
- 輸出裡**不該出現** `.env`。

> [!WARNING]
> **保存 Token 的規則**
> - ✅ 放在 `.env`（本機、權限 600、被 `.gitignore` 擋住）。
> - ❌ **不要寫死在程式碼裡**（例如 `token = "MTA..."`）。程式碼會被 commit、被同學看到、被貼進對話。之後的程式會用環境變數的方式讀 `.env`（`docs/architecture.md` 第 3.8 節與第 11 項）。
> - ❌ **不要**用 `git add -f` 強制加入 `.env`。
> - ❌ **不要**把 `.env` 的內容貼進 Claude Code、ChatGPT 或任何對話。也**不要請 AI 幫你「打開 .env 看看」**，那會讓 Token 進到對話內容裡。需要檢查時，用上面的 `git check-ignore` 指令就好，它不會印出內容。
> - ❌ **不要**把 `.env` 傳到雲端硬碟、LINE、email 或截圖。
> - 想備份的話，放進密碼管理器（例如 macOS 的「密碼」App 或 1Password），不要放在一般的筆記。

---

## 7. 設定必要的 Bot 權限

權限的原則是**最小化**：只給 agent 真正需要的。依 `docs/architecture.md` 第 3.3 節，只需要三個：

| 權限（英文名稱） | 為什麼需要 |
|---|---|
| **View Channels** | 才看得到頻道（舊版名稱可能叫 Read Messages / View Channels） |
| **Read Message History** | 要知道使用者「回覆」的是哪一則訊息（更正、刪除確認會用到） |
| **Send Messages** | 回覆「處理中」、辨識結果、錯誤提示，以及編輯自己發的訊息 |

> [!WARNING]
> **不要勾 Administrator（管理員）**，也不要勾 Manage Messages（管理訊息）、Manage Channels、Kick/Ban Members 等。我們不刪使用者的訊息（architecture.md 第 14 節），不需要這些。給太多權限，萬一 Token 外流，傷害會更大。

### 操作步驟（在 Installation 頁面設定）✅
1. 在左側選單點 **Installation** ✅。
2. 找到 **Installation Contexts** 區塊 ✅：
   - 勾選 **Guild Install**（安裝到伺服器）。
   - **User Install** 取消勾選 ⚠️（我們不需要安裝到個人帳號）。
3. 往下找到 **Install Link** 區塊 ✅，下拉選單選 **Discord Provided Link** ✅。
4. 往下找到 **Default Install Settings** ✅，看 **Guild Install** 那一組：
   - **Scopes**：加入 **bot** ✅。（Scope 是「授權範圍」，`bot` 代表「把 bot 帳號加進伺服器」。`applications.commands` 這個 agent 用不到，若欄位裡已經自動有，留著也沒關係。）
   - **Permissions**：勾選上面的三個：**View Channels**、**Read Message History**、**Send Messages**。
5. 設定會自動儲存；如果頁面有 **Save Changes**，就點一下 ⚠️。

---

## 8. 產生邀請連結

Install Link 就是邀請連結。

1. 仍在 **Installation** 頁面，**Install Link** 區塊下方會顯示一條網址 ✅。
2. 點旁邊的 **Copy** 複製它。
3. 這個連結本身**不含 Token**，不是機密。但因為你已經把 **Public Bot 關掉**，只有你自己能用它邀請 bot，所以也不用到處分享。

### 備用方法（Install Link 不能用時）⚠️
在左側 **OAuth2** → **OAuth2 URL Generator** 區塊，勾 **Scopes** 的 `bot`，再勾上面三個 **Bot Permissions**，頁面下方會產生一條網址。三個權限的權限代碼加總是 `68608`（`View Channels` 1024 + `Send Messages` 2048 + `Read Message History` 65536），可以拿來核對網址裡的 `permissions=` 數字對不對。

---

## 9. 把 Bot 邀請到你自己的 Server

1. 把剛剛複製的連結貼到瀏覽器網址列，按 Enter ✅。
2. 瀏覽器會打開 Discord 的授權頁面。如果被問要不要開 Discord App，選允許或「在瀏覽器繼續」都可以。
3. 在 **Add to server** / **加入伺服器** 的下拉選單 ⚠️，選你剛剛建立的那個 Server。
   - 你必須是那個 Server 的擁有者，或有「管理伺服器」權限，才會在清單裡看到它。
4. 點 **Continue** / **繼續** ⚠️。
5. 畫面列出 bot 要求的權限，**核對只有你剛剛勾的三項**（View Channels、Read Message History、Send Messages）。如果看到管理員或其他不認識的權限，**先不要授權**，回第 7 步檢查。
6. 點 **Authorize** / **授權** ⚠️。
7. 如果出現「我不是機器人」驗證，完成它。
8. 看到「已授權」或類似的成功畫面，就完成了。

---

## 10. 確認 Bot 已經加入伺服器

1. 回到 Discord，進入你的 Server。
2. 看右邊的**成員列表**（若沒顯示，點右上角的人形圖示 **顯示成員名單** / **Show Member List**）。
3. 你應該在成員列表看到 bot 的名字，旁邊有 **BOT** / **機器人** 標籤 ⚠️，而且狀態是**離線**（灰色）。
   - **離線是正常的**，因為你的 agent 還沒啟動。bot 只有在 agent 程式開著、連上 Discord 時才會顯示在線。
4. 在 `# 票券記錄` 頻道，通常也會看到一則系統訊息，內容類似「某某 bot 加入了這個伺服器」⚠️。
5. 也可以到 **伺服器設定** / Server Settings → **整合** / Integrations（⚠️）看到這個 bot。

### 如果頻道是私人頻道（可選）⚠️
如果你在上一份教學把 `# 票券記錄` 設成私人頻道，bot 預設是看不到的，要手動加：
1. 在頻道名稱旁點**齒輪圖示**（**編輯頻道** / Edit Channel）。
2. 選 **權限** / **Permissions**。
3. 在 **成員** / **Members** 或角色 / Roles 區塊點 **＋**，選你的 bot。
4. 確認 **檢視頻道**、**傳送訊息**、**讀取訊息歷史** 三項是綠色勾勾。
5. 按 **儲存變更** / Save Changes。

---

## 11. 常見問題

| 狀況 | 可能原因與處理 |
|---|---|
| 成員列表看不到 bot | 授權時選錯 Server，或沒有按到 Authorize。回第 9 步重做 |
| bot 一直是離線 | 正常，agent 沒啟動。agent 啟動、連線成功後才會變在線 |
| agent 啟動時報 `PrivilegedIntentsRequired` | Message Content Intent 沒打開，或打開後沒按 **Save Changes**。回第 4 步 |
| bot 在線，但指令、照片都沒反應 | 同上；或 `.env` 的 `CHANNEL_ID` 填錯（可能複製成 Server ID 了）；或 bot 看不到頻道（私人頻道要手動加，見第 10 步） |
| agent 啟動時報登入失敗 / `LoginFailure` / Improper token | Token 貼錯、前後有多餘空白或引號，或 Token 已被重設而失效。回第 5 步重新產生，再更新 `.env` |
| bot 能收訊息但不能回覆（403 / Missing Permissions） | 權限不足。回第 7 步檢查 Send Messages 等三項 |

---

## 12. Token 外流了怎麼辦

1. **立刻**回到 Developer Portal → **Bot** → 點 **Reset Token**。舊的 Token 當下就失效 ✅，別人手上的就沒用了。
2. 把新的 Token 更新到 `.env`。
3. 如果 Token 曾經被 commit 或 push 到 GitHub：**光是重設 Token 並刪除檔案還不夠**，因為舊紀錄還在 Git 歷史裡。重設 Token 之後，舊的就是失效的，風險就解除了，但請告訴老師或助教，一起處理 Git 歷史。
4. 檢查 Server 裡有沒有不認識的訊息、成員或頻道變動。

---

## 完成後的確認清單

- [ ] 我已建立 Application，Public Bot 已關閉。
- [ ] **Message Content Intent** 已開啟，並且按過 **Save Changes**；另外兩個 Privileged Intent 保持關閉。
- [ ] 我取得 Bot Token，並且**只**存在 `.env` 裡，沒有貼到任何其他地方。
- [ ] `git check-ignore -v .env` 有輸出，`git status` 沒有顯示 `.env`。
- [ ] 權限只有 View Channels、Read Message History、Send Messages，沒有 Administrator。
- [ ] bot 已出現在我的 Server 成員列表（離線是正常的）。
- [ ] `.env` 裡的 `CHANNEL_ID` 填的是**頻道 ID**，不是 Server ID。

---

## 查證說明

| 內容 | 查證結果 |
|---|---|
| 建立 Application（輸入名稱 → **Create** → 進入 **General Information**）、**Bot** 頁面的 **Reset Token**、Token 只顯示一次、**Installation** 頁面的 **Installation Contexts**、**Install Link** → **Discord Provided Link**、**Default Install Settings** 的 Guild Install（scopes 與 permissions） | 已讀取官方文件 [Discord Developer Docs — Getting Started](https://docs.discord.com/developers/quick-start/getting-started)（舊網址 `discord.com/developers/docs/quick-start/getting-started` 會轉址到這裡）。 |
| Message Content Intent 的位置（**Bot** → **Privileged Gateway Intents**）、沒開時訊息內容為空、需 **Save Changes**、100 伺服器以上才需審核 | 取自官方說明的搜尋結果摘要（[Discord Developer Docs — Gateway](https://docs.discord.com/developers/gateway/you-might-not-need-a-privileged-intent)）以及社群文件，**我沒有逐字讀過官方整頁**。附件是否一併被隱藏，我標了 ⚠️，請以實際測試為準。 |
| **New Application** 按鈕、同意條款勾選、**Public Bot** 與 **Requires OAuth2 Code Grant** 開關、確認重設 Token 的按鈕文字、授權頁面的按鈕、備用的 OAuth2 URL Generator、權限代碼 `68608` | **沒有查到可讀取的官方頁面**，是依一般使用經驗與 Discord 權限位元（1024、2048、65536）計算寫成，標 ⚠️。介面可能已改版，請以實際畫面為準。 |
| 成員列表、整合頁面、私人頻道權限的中文名稱 | 同樣標 ⚠️，Discord 中文翻譯偶爾會調整。 |
| `.env` 與 Git 的安全做法 | 依本專案的 `CLAUDE.md` 與 `.gitignore` 實際內容撰寫，已用 `git check-ignore` 驗證過規則有效。 |

官方文件：

- 開發者後台快速入門：<https://docs.discord.com/developers/quick-start/getting-started>
- Gateway 與 Privileged Intents：<https://docs.discord.com/developers/gateway/you-might-not-need-a-privileged-intent>
