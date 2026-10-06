# 多渠道客服中轉服務（微信 / WhatsApp / Instagram / Facebook）

把四個渠道收到的客戶消息，統一接進來，做兩件事：

1. **轉發**：微信、WhatsApp 的消息轉成約定的 JSON，轉發給目標業務系統（9033）。
2. **客服工作台**：微信 / WhatsApp / IG / FB 四個渠道的會話，集中在一個網頁上查看與回覆。

---

## 一、這個服務解決什麼問題

微信公眾號、WhatsApp、Instagram、Facebook 都**不能直接把消息轉發給第三方系統**，
必須有個服務站在中間接住消息、轉換格式。本服務就是這個中轉層：

```
   客戶                   平台伺服器                  本服務                      目標系統 / 客服
 ────────              ─────────────              ─────────────              ──────────────────
 微信使用者    ──▶   微信伺服器        ──▶   /wechat            ──▶   目標業務系統 9033
 WhatsApp 使用者 ──▶  Meta 伺服器       ──▶   /whatsapp/webhook  ──▶   目標業務系統 9033
 IG 使用者      ──▶  Meta 伺服器       ──▶   /webhook           ──▶   客服工作台（本機）
 FB 使用者      ──▶  Meta 伺服器       ──▶   /webhook           ──▶   客服工作台（本機）

                     ↓ 各自解析、轉成同一種 JSON 後再分流
```

- **微信 / WhatsApp** → 轉發給目標系統 9033（既有的自動化流程）。
- **IG / FB** → 只進本機客服工作台查看與回覆，**不轉發** 9033。

---

## 二、功能一覽

| 渠道 | 接收消息 | 轉發 9033 | 工作台查看 | 工作台回覆 | AI 自動回復 |
|---|---|:---:|:---:|:---:|:---:|
| 微信公眾號 | 文字 / 圖片 / 語音 / 影片 | ✅ | ✅ | ⚠️ 需認證公眾號 | ✗（未認證號發不出） |
| WhatsApp | 文字 / 圖片 / 文件 / 語音等 | ✅ | ✅ | ✅ | ✅ 私訊 |
| Instagram | 私訊 | ✗ | ✅ | ✅ | ✅ 私訊 |
| Instagram | 貼文留言 | ✗ | ✅ | ✅（需權限，見下） | ✅ 留言 |
| Facebook | 私訊 | ✗ | ✅ | ✅ | ✅ 私訊 |
| Facebook | 貼文留言 | ✗ | ✅ | ✅ | ✅ 留言 |

工作台另提供：**未讀紅點**（分平台 + 分會話）、**右鍵標為已處理**、**貼文留言「已回 / 已忽略」標記**、**檔案與圖片傳輸**，以及 **AI 自動回復**（見第四節）。

---

## 三、AI 自動回復

每個渠道的**私訊**與**貼文留言**各自一把開關，在客服工作台左側直接切換（齒輪圖示進完整設定）。

### 三層決策邏輯

```
客戶來訊
  │
  ├─ 第一層：FAQ 問答對 ──► 規則匹配命中 → 直接回該答案（不呼叫 AI）
  │        （整層可停用；每個問答對也可單獨停用）
  │        未命中且開啟改寫 → 交給 DeepSeek 改寫問句後再匹配一次
  │
  ├─ 第二層：業務表格 ──► 把表格內容交給 DeepSeek 理解後生成答案
  │        （整層可停用）
  │        ① 先用客戶原句查一次
  │        ② 查不到 → 帶入該會話最近 5 則對話，請模型把問句改寫成
  │           「可獨立理解」的完整問句，再查一次
  │        ③ 還是查不到 → 視為未命中
  │
  └─ 第三層：降級人工回復
           不發送任何訊息，該則訊息在工作台標為
           「自動回復未命中」＋「待處理」
```

- 兩層都停用（或都未命中）＝ 直接降級人工，不會發出任何訊息。
- **FAQ 命中方式**：繁簡正規化（opencc）+ jieba 分詞 + 字元 bigram + 序列相似度，多信號加權打分，
  達門檻（預設 0.45，`FAQ_MATCH_THRESHOLD`）即命中。每個問答對可填「關鍵詞」欄擴充其他問法。
- **第二層模型**：`deepseek-flash`（可用 `DEEPSEEK_MODEL` 換成 `deepseek-chat` / `deepseek-reasoner`）。
  模型若回覆 `__NO_ANSWER__` 標記，代表表格答不出來 → 進入改寫流程。
- **問句改寫（救回追問）**：客服對話常出現「那個多少錢？」「有貨嗎？」這類指代不明的短句，
  直接查表格鐵定查不到。此時系統會把**該會話最近 5 則對話**（客戶與我方交替）餵給模型，
  讓它把代名詞、被省略的主體還原成完整問句，再查一次表格。
  例：客戶先問「你們有賣耳機嗎？」→ 我方回「有 A 款藍牙耳機」→ 客戶問「那個多少錢？」，
  改寫成「A 款藍牙耳機多少錢？」後命中。
  改寫次數由 `TABLE_REWRITE_HISTORY_TURNS` 控制（預設 5，設 0 = 關閉改寫）。
  - 改寫**只用於查詢**；表格回答本身仍只依表格內容，不會拿對話裡的舊資訊作答。
  - 改寫後仍查不到才降級人工；若模型改寫出跟原句一樣的問句，會直接降級、不重複查。
  - 命中後標記仍是「**自動回復-表格命中**」，另在說明裡註明「**改寫後命中**」，
    設定面板的成效表另有「**改寫救回**」欄位統計救回次數。
- **標記**：所有自動回復的內容都會在**工作台與日誌**標上
  「**自動回復-FAQ命中**」或「**自動回復-表格命中**」；未命中標「**自動回復未命中**」。
  標記不會附加到發給客戶的訊息裡（客戶只看到乾淨的回答）。
- **只處理文字訊息**：圖片、語音、貼圖、檔案一律留給人工。
- **防護**：同一會話冷卻 `AUTOREPLY_COOLDOWN_SECONDS` 秒、每日每會話上限 `AUTOREPLY_MAX_PER_DAY` 則。
- **一則訊息只回一次**：Meta / 微信 都會對同一個 webhook 事件**重複投遞**（沒在時限內收到 200 就重投），
  兩份幾乎同時到達時，冷卻檢查（先查再寫）擋不住併發，會導致同一則客戶訊息被回兩條。
  因此改用**原子認領**：處理前先用「渠道＋場景＋訊息id」往 `autoreply_log.claim_key`
  （唯一索引）插一列，只有插得進去的處理者才繼續，其餘直接略過。
  - 訊息 id 優先用平台給的 `message_id` / `comment_id`；兩者都沒有時（部分微訊訊息）
    退回「渠道＋場景＋會話＋文字指紋」。
  - 認領列就是該次處理的日誌列（後續改寫它），所以仍是**一則訊息一列**。
  - 重複投遞會記在 log 的 `reason=duplicate`，可從紀錄分頁追查。
- **不會回覆自己**：我方送出的訊息（Meta 的 echo 事件）與我方自己的留言都會被擋掉，避免無限循環。
- 送出失敗（例如微信 48001）會標成「自動回復失敗，待人工處理」，不會靜默消失。

### 工作台操作

1. 左側選到某個渠道 + 私訊／貼文留言，用該列的按鈕切換此組合的自動回復。
2. 點齒輪進設定面板，四個分頁：
   - **自動回復**：全域總開關、各渠道開關、FAQ／表格層停啟用、DeepSeek 狀態、防護值、**試跑**（不發訊息）、各渠道成效。
   - **FAQ 問答對**：表格直接編輯（問題／答案／關鍵詞／啟用）、單筆或整批儲存、新增、刪除，
     上傳 **Excel / CSV** 匯入（可選清空後匯入或附加），下載**範本檔**，匯出目前 FAQ。
   - **業務表格**：新增表、編輯欄位、逐列編輯、刪列、停啟用，上傳 Excel / CSV 匯入（第一列為欄位名），匯出、範本檔。
   - **紀錄**：每次自動回復的處置（命中哪一層、未命中原因、送出結果）。
3. 未命中的訊息在工作台列表標「未命中 N」，對話裡每則訊息也會有對應標記。

### 範本檔

設定面板內可直接下載，欄位格式：

| 檔案 | 欄位 |
|---|---|
| FAQ | `問題`、`答案`、`關鍵詞(選填,逗號分隔)`、`啟用(是/否)` |
| 業務表格 | 第一列自行定義欄位名（例如 `產品名稱`、`價格`、`庫存`、`規格`），其後每列一筆資料 |

CSV 匯出皆帶 UTF-8 BOM，Excel 直開不亂碼；上傳的 CSV 會自動嘗試 UTF-8 / Big5 / GBK 等編碼。

---

## 四、快速開始

### 1. 環境需求

- Python 3.11+（開發環境為 3.13）
- PostgreSQL（存轉發日誌與 IG/FB 收件匣資料）
- 一個能讓外網訪問的位址（ngrok 或固定域名），因為微信 / Meta 需要回調你的服務

### 2. 安裝依賴

```bash
cd wechat-forwarder
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
```

### 3. 建立資料庫與資料表

先在 PostgreSQL 建好一個庫，把連線字串填進 `.env` 的 `DATABASE_URL`，然後：

```bash
.venv\Scripts\python create_db.py
```

會建立（若不存在）`forward_log`（轉發日誌）、`igfb_dm` / `igfb_comment` / `igfb_post`（IG/FB 收件匣），
以及 AI 自動回復用的 `autoreply_faq` / `autoreply_business_table` / `autoreply_business_row` /
`autoreply_setting` / `autoreply_log`。此腳本可重複執行，不會清掉既有資料。

> 服務啟動時也會自動補齊這些表與新欄位（`main.py` 的 lifespan 會呼叫 `autoreply_db.init_schema()`），
> 升級既有環境只要重啟服務即可，不一定要跑 `create_db.py`。
> **資料庫固定為 `platform_forwarder`**，不會動到其他庫。

### 4. 填寫設定

```bash
cp .env.example .env
```

編輯 `.env`，四個渠道的憑證都放在這裡。**欄位完整說明見第八節**；最少要先填：

- `DATABASE_URL`、`TARGET_WEBHOOK_URL`、`PUBLIC_BASE_URL`
- 微信：`WECHAT_TOKEN`、`WECHAT_APPID` / `WECHAT_APPSECRET`
- WhatsApp：`WHATSAPP_ACCESS_TOKEN`、`WHATSAPP_PHONE_NUMBER_ID`、`WHATSAPP_VERIFY_TOKEN`
- IG / FB：`META_PAGE_ID`、`META_PAGE_ACCESS_TOKEN`、`META_IG_USER_ID`、`META_VERIFY_TOKEN` 等
- 要用 AI 自動回復：`DEEPSEEK_API_KEY`（其餘自動回復設定見第八節）

### 5. 啟動

```bash
.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

服務同時掛了微信、WhatsApp、IG/FB 三組端點，**一條 ngrok 隧道就夠**：

```bash
ngrok http 8001
```

把 ngrok 給的 `https://xxxx.ngrok-free.dev` 填進 `.env` 的 `PUBLIC_BASE_URL`，重啟服務。

> **ngrok 免費版限制**：只有 1 個域名、同時只能 1 條隧道。
> 若已有其他服務佔用，會報 `ERR_NGROK_334`。可改用付費版或 cloudflared（免費多域名）。

### 6. 打開客服工作台

瀏覽器開 **`http://127.0.0.1:8001/console`**（對外即 `https://你的域名/console`）。

---

## 五、客服工作台使用說明

### 導覽結構

```
私訊                         貼文留言
├─ 微信     ┐               ├─ Instagram
├─ WhatsApp │ 以「使用者」為單位  └─ Facebook     以「貼文」為單位
├─ Instagram│               │
└─ Facebook ┘               └─ 點貼文 → 右側貼文詳情頁 + 留言
```

一級分「**私訊**」「**貼文留言**」兩大塊；進來預設是私訊的微信。分頁右側的**紅點數字**代表該渠道未處理的訊息數。

### 私訊

- 左側列表**以使用者為單位**：頭像、名稱、最後一句話、時間，右上角紅點顯示未處理數。
- 點進去看完整對話串（客戶在左、我方在右），底部輸入框可回覆文字或上傳檔案 / 圖片。
- **紅點規則**：客戶來訊即視為「未處理」；**你回覆後**，或**右鍵選「標為已處理」**，紅點才消除。

### 貼文留言

- 列表**以貼文為單位**：貼文摘要（caption）、留言數、未處理留言數（「N 新」標記）。
- 點進去看貼文詳情（媒體圖、caption、留言總數）與留言列表，每條留言下可「回覆」。
- **回覆**過的留言標「**已回**」；**右鍵「標為已處理（忽略）」**的留言標「**已忽略**」（同一位置）。
- 未回覆、也未標已處理的留言，計入「N 新」。

### 關於紅點與「已處理」

紅點判斷的是**每一條客戶訊息是否已處理**，規則很單純：

| 動作 | 結果 |
|---|---|
| 客戶發來新訊息 | 該訊息 = 未處理 → 出現紅點 |
| 你回覆該對話 | 該對話的客戶訊息全部 = 已處理 → 紅點消除 |
| 右鍵「標為已處理」 | 同上，但**不發送任何訊息**（適合不需回覆的情況） |

> 首次接入時，歷史同步進來的舊訊息一律視為「未處理」，紅點數字會比較大；
> 逐一「標為已處理」清掉即可，之後只會顯示新增量。

---

## 六、各渠道接入

### 微信公眾號

1. `mp.weixin.qq.com` → 設置與開發 → 基本配置，勾選成為開發者。
2. 伺服器配置 → 修改配置：
   - **URL**：`https://你的域名/wechat`
   - **Token**：與 `.env` 的 `WECHAT_TOKEN` 一致
   - **EncodingAESKey**：點隨機生成，填進 `.env` 的 `WECHAT_ENCODING_AES_KEY`
   - **消息加解密方式**：明文 / 安全 / 兼容皆支援
3. 提交 → 微信會 GET 驗證 → 通過後點「啟用」。

> **回覆限制**：微信客服消息接口需要**認證公眾號**權限。未認證訂閱號回覆會失敗（錯誤碼 48001），只能接收、不能回。

### WhatsApp（Meta Cloud API）

1. [Meta for Developers](https://developers.facebook.com/) 建 App，開通 WhatsApp，綁定電話號碼，拿到 `phone_number_id` 與 `access_token`。
2. WhatsApp → Configuration → Webhook：
   - **Callback URL**：`https://你的域名/whatsapp/webhook`
   - **Verify token**：與 `.env` 的 `WHATSAPP_VERIFY_TOKEN` 一致
   - 訂閱 `messages` 欄位

### Instagram / Facebook

見 **`META_SETUP.md`**——那份是完整的後台設定 checklist，涵蓋 webhook 驗證、訂閱欄位、帳號授權、權限開通。

---

## 七、對接方接口（給目標系統 9033）

目標系統要回覆 WhatsApp 使用者時，POST 到 `POST /whatsapp/reply`，本服務會轉成 WhatsApp 格式發送。

**完整報文格式、欄位說明、支援的檔案類型見 `api-doc.md`。**

---

## 八、環境變數說明（.env）

### 通用

| 欄位 | 說明 |
|---|---|
| `DATABASE_URL` | PostgreSQL 連線字串 |
| `TARGET_WEBHOOK_URL` | 目標系統的 webhook 位址（微信 / WhatsApp 轉發用） |
| `PUBLIC_BASE_URL` | 本服務對外 https 位址（用於拼接媒體下載連結） |
| `MEDIA_DIR` | 媒體落地目錄（預設 `media/`） |

### 微信

| 欄位 | 說明 |
|---|---|
| `WECHAT_TOKEN` | 後台「伺服器配置」填的 Token |
| `WECHAT_APPID` / `WECHAT_APPSECRET` | 開發者 ID，下載語音 / 影片用 |
| `WECHAT_ENCODING_AES_KEY` | 安全 / 兼容模式的 43 字元金鑰 |
| `CHANNEL` / `CHANNEL_IDENTITY_ID` | 微信渠道在目標系統的標識（**需與目標系統方確認**） |

### WhatsApp

| 欄位 | 說明 |
|---|---|
| `WHATSAPP_VERIFY_TOKEN` | Meta 後台 webhook 驗證 token |
| `WHATSAPP_ACCESS_TOKEN` | Meta App access token（下載 / 發送媒體） |
| `WHATSAPP_PHONE_NUMBER_ID` | 電話號碼 ID（發送回覆用） |
| `WHATSAPP_CHANNEL` / `WHATSAPP_CHANNEL_IDENTITY_ID` | WhatsApp 渠道在目標系統的標識（**需確認**） |

### Instagram / Facebook

| 欄位 | 說明 |
|---|---|
| `META_APP_ID` / `META_APP_SECRET` | App 編號與密鑰（延長 / 衍生權杖時需要） |
| `META_VERIFY_TOKEN` | webhook 驗證 token（Meta 後台自行設定） |
| `META_APP_SECRET` | （選用）webhook 簽章驗證用的 App Secret |
| `META_VERIFY_SIGNATURE` | 是否強制驗證簽章（預設 `false`） |
| `META_GRAPH_VERSION` / `GRAPH_API_VERSION` | Graph API 版本 |
| `META_PAGE_ID` | Facebook 粉絲專頁 ID |
| `META_PAGE_ACCESS_TOKEN` | 粉專權杖（FB 私訊 / 貼文 / 留言 + IG 貼文 / 留言） |
| `META_IG_USER_ID` | IG 專業帳號 ID（辨識「我方」、讀寫 IG 貼文留言） |
| `META_IG_ACCESS_TOKEN` | Instagram 登入路徑權杖（IGAA…，IG 私訊用） |
| `META_IG_GRAPH_HOST` | IG 發送主機（留空 = 依權杖前綴自動判斷） |

> 有兩組 Meta 權杖是正常的：**粉專權杖**走 `graph.facebook.com`（管 FB 與 IG 貼文留言），
> **IG 登入權杖**（IGAA…）走 `graph.instagram.com`（管 IG 私訊）。兩者是不同路徑，別搞混。

### AI 自動回復（DeepSeek）

| 欄位 | 預設 | 說明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | （空） | 你的 DeepSeek Key。**留空則第二層（業務表格）與 FAQ 改寫停用**，直接降級人工 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 位址 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 模型 ID（也可用 `deepseek-chat` / `deepseek-reasoner`） |
| `DEEPSEEK_TIMEOUT` | `30` | 單次呼叫逾時（秒） |
| `FAQ_REWRITE_MODE` | `on_miss` | `off` = 不呼叫 AI 改寫；`on_miss` = 規則未命中時才改寫問句再匹配一次 |
| `FAQ_MATCH_THRESHOLD` | `0.45` | FAQ 規則匹配門檻（0~1，越高越嚴格） |
| `TABLE_CONTEXT_MAX_ROWS` | `200` | 餵給模型的表格列數上限（0 = 不限） |
| `TABLE_CONTEXT_MAX_CHARS` | `12000` | 餵給模型的表格字數上限（0 = 不限） |
| `TABLE_REWRITE_HISTORY_TURNS` | `5` | 業務表格第一次沒查到時，帶入最近幾則對話做 query 改寫（0 = 關閉改寫） |
| `AUTOREPLY_COOLDOWN_SECONDS` | `30` | 同一會話的自動回復冷卻秒數（0 = 不限制） |
| `AUTOREPLY_MAX_PER_DAY` | `30` | 同一會話每日自動回復上限（0 = 不限制） |

提示詞全部走環境變數，**直接在 `.env` 編輯即可**（留空 = 用 `config.py` 內建預設值）。
五個 key 與用途：

| Key | 用途 | 必須保留的佔位符 |
|---|---|---|
| `DEEPSEEK_SYSTEM_PROMPT` | 基本人設，所有 AI 回覆都用 | — |
| `DEEPSEEK_COMMENT_PROMPT` | 公開留言（`scope=comment`）的附加規則 | — |
| `DEEPSEEK_TABLE_PROMPT` | 業務表格回答 | `{context}`、`{sentinel}` |
| `DEEPSEEK_REWRITE_PROMPT` | FAQ 問句改寫 | `{faq_list}` |
| `DEEPSEEK_TABLE_REWRITE_PROMPT` | 表格查不到的 query 改寫 | `{history}`、`{question}` |

**寫法（重要）**：多行提示詞必須用**雙引號**包起來，換行寫 `\n`：

```dotenv
DEEPSEEK_SYSTEM_PROMPT="你是一位專業、友善的客服助手。\n請用簡潔的繁體中文回答，不要提及自己是 AI。"
```

不加引號的話 `\n` 會變成人看得見的「反斜線 + n」，提示詞不會換行。單行提示詞加不加引號都可以。
`DEEPSEEK_TABLE_PROMPT` 是用 Python `.format()` 帶入佔位符，若提示詞裡要寫**字面上的大括號**，得寫成 `{{` 與 `}}`，否則會報錯。
`.env.example` 底部有可直接複製的五組範例。

> 改完 `.env` 要**重啟服務**才會生效（服務未帶 `--reload`）。

---

## 九、常見問題排查

| 現象 | 原因與處理 |
|---|---|
| 服務啟動即報埠被佔用 | 先前的實例還活著。找出佔用 `8001` 的進程結束掉再啟動。 |
| 改了程式碼但行為沒變 | 服務未帶 `--reload`，**改完後端要重啟**；前端頁面按 `Ctrl+Shift+R` 強制刷新，避免瀏覽器快取舊頁面。 |
| 網頁一直顯示舊介面 | 同上，瀏覽器快取。開發者工具 → Network → 勾「Disable cache」再刷新。 |
| Meta webhook 驗證失敗 | Callback URL 要能被外網訪問；`Verify token` 要與 `.env` 完全一致。 |
| 回覆留言 / 私訊回 **502** | 多半是 **Meta 權杖失效**。跑 `python token_tool.py check` 確認，詳見 `META_SETUP.md` 第六節。 |
| 按「同步最新資料」卻沒有新貼文 | 同上，權杖失效時同步會靜默失敗。 |
| 微信後台「啟用」失敗 | URL / Token 與 `.env` 不符，或服務沒在跑、ngrok 沒開。 |
| 媒體檔下載不到 | `.env` 的 `PUBLIC_BASE_URL` 沒填或填錯；微信個人號下載媒體權限需實測。 |
| IG 留言回覆回 502 | 缺 `instagram_manage_comments` 權限（詳見 `META_SETUP.md`）。 |
| 留言者姓名顯示「用戶」 | 平台隱私限制，非程式問題（見下方已知限制）。 |
| 自動回復完全沒動作 | 三個地方都要開：① `.env` 有 `DEEPSEEK_API_KEY`（第二層才會有答案）② 設定面板的**全域總開關** ③ 該**渠道＋場景**的開關。另外微信不支援自動回復。 |
| 自動回復都掉到人工 | 看設定面板的**紀錄**分頁，`reason` 會寫明原因：`no_match`（真的都没命中）、`no_api_key`、`llm_error`、`table_layer_disabled`、`throttled`（冷卻／上限）。詳情欄也會寫「改寫成什麼問句後仍未命中」。 |
| 自動回復重複回同一則 | 已修（見第三節「一則訊息只回一次」）。若仍重複，檢查紀錄分頁是否同一 `message_id` 有兩列 `send_status=sent`；有的話代表跑的是舊版程式碼，需重啟服務。 |
| 追問句都查不到（「那個多少錢？」） | 這是第二層改寫要解的：確認 `TABLE_REWRITE_HISTORY_TURNS` > 0，且該會話真的有待處理的歷史訊息（新會話沒有上下文可改寫）。紀錄分頁若顯示「改寫後問句無變化」，代表模型沒能從對話補出主體，可考慮把這類問法補進 FAQ。 |
| 自動回復回了不相關的答案 | 提高 `FAQ_MATCH_THRESHOLD`、把該 FAQ 停用、或在關鍵詞欄補上更精確的問法；也可把 `FAQ_REWRITE_MODE` 設成 `off` 避免改寫後誤命中。 |
| 自動回復太慢 | 第二層要呼叫 DeepSeek（約 1~5 秒）。自動回復是背景執行，不影響 webhook 回應速度；要更快就讓第一層 FAQ 涵蓋更多常見問題。 |
| 自動回復重複回同一則 | 檢查 `AUTOREPLY_COOLDOWN_SECONDS`；正常同一會話在冷卻期內不會重複送出。 |
| 改了 FAQ／表格但沒生效 | 設定介面改動會即時清快取；若直接改資料庫，最多等 5 秒（快取 TTL）。 |

---

## 十、已知限制

- **微信回覆**：需認證公眾號；未認證號只能收不能回（48001）。因此**微信的 AI 自動回復目前一定會失敗**，
  系統會標成「自動回復失敗，待人工處理」，不會靜默消失。故設定介面不提供微信的自動回復開關。
- **IG 留言回覆**：需 `instagram_manage_comments` 權限。未開通前能看能讀，回覆會被 Meta 擋（自動回復同理）。
- **留言者姓名**：FB / IG 對陌生留言者的姓名有平台隱私限制，`from.name` / `username` 可能為空，工作台顯示「用戶」。這是平台限制，非程式問題。
- **FB 貼文圖片**：正常顯示（取 `full_picture`，取不到時退回 `attachments`）。圖片連結是 Meta 的簽名 CDN 網址、會過期，但每次同步都會刷新，開著頁面時不需理會。
- **事件 / 狀態消息**（微信關注取關、WhatsApp 已讀 / 送達回執）：目前忽略，不轉發。
- **超時**：微信 / Meta 要求 5 秒內回應。本服務轉發超時 5 秒，失敗仍回 200（避免對方無限重試），失敗會記 log。
  自動回復丟到背景執行，不會拖慢 webhook 回應。
- **自動回復只處理文字**：圖片、語音、貼圖、檔案一律留給人工。
- **AI 可能出錯**：第二層由模型依表格生成答案，仍可能理解偏差。
  建議定期看設定面板的「紀錄」，把答不好的問題補進第一層 FAQ（規則命中比模型生成更穩定）。
- **表格規模**：業務表格內容會整份餵給模型，受 `TABLE_CONTEXT_MAX_ROWS` / `TABLE_CONTEXT_MAX_CHARS` 限制；
  表格很大時應拆表或精簡欄位，否則會被截斷。

---

## 十一、專案結構

```
wechat-forwarder/
├─ main.py              # 服務入口：webhook 接收與分派、靜態檔案、自動回復鉤子
├─ config.py            # 集中讀取環境變數（含 DeepSeek 與自動回復設定）
├─ create_db.py         # 建立資料表（可重複執行）
├─ requirements.txt
├─ .env.example         # 設定範本（複製為 .env 後填寫）
│
├─ wechat.py            # 微信：簽名驗證、XML 解析、轉換
├─ wechat_send.py       # 微信：客服消息發送
├─ whatsapp.py          # WhatsApp：webhook 解析、轉換
├─ whatsapp_send.py     # WhatsApp：訊息與媒體發送
├─ forwarder.py         # 轉發到目標系統 + 媒體下載
├─ db.py                # 轉發日誌寫入（forward_log）
│
├─ faq_matcher.py       # FAQ 匹配算法（繁簡正規化 + 分詞 + bigram + 相似度加權）
├─ deepseek_client.py   # DeepSeek（OpenAI 相容）非同步客戶端
├─ reply_engine.py      # 三層決策引擎：FAQ → 業務表格 → 未命中
├─ autoreply.py         # 自動回復編排：開關、防護、發送、標記未命中
├─ autoreply_db.py      # 自動回復資料層：建表 + FAQ/表格/開關/日誌 CRUD
├─ autoreply_console.py # 自動回復設定介面 API（/console/autoreply）
├─ xlsx_io.py           # Excel / CSV 解析與範本產生
│
├─ igfb.py              # Meta：Graph API 讀寫 + webhook 事件解析
├─ igfb_db.py           # Meta：IG/FB 收件匣資料讀寫 + 歷史同步
├─ igfb_console.py      # Meta：工作台後台接口
├─ token_refresh.py     # IG 登入權杖自動續期（啟動時 + 每日）
├─ token_tool.py        # 權杖工具：check / extend / derive / renew
│
├─ console.py           # 工作台後台接口（微信 / WhatsApp）
├─ console_ui.html      # 工作台前端（單檔、自帶樣式與邏輯）
│
├─ README.md            # 本文件
├─ META_SETUP.md        # Meta 後台設定 checklist
└─ api-doc.md           # 給對接方的 WhatsApp 回覆接口說明
```

> 執行時會產生 `media/`（媒體落地目錄）。此目錄為執行期資料，交付時可清空。
