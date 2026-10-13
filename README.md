# omni-cs — 多渠道客服中轉服務（微信 / WhatsApp / Instagram / Facebook）

> 把微信公眾號、WhatsApp、Instagram、Facebook 四個渠道的客戶訊息統一接進來，
> **全部落地本機 PostgreSQL**，集中在一個網頁（客服工作台）上查看與回覆，
> 並提供四層 AI 自動回復：**FAQ 問答對 → 業務表格 → RAG 知識庫 → 降級人工**。

---

## 目錄

- [一、這個專案解決什麼問題](#一這個專案解決什麼問題)
- [二、功能一覽](#二功能一覽)
- [三、AI 自動回復（四層決策）](#三ai-自動回復四層決策)
- [四、快速開始](#四快速開始)
- [五、各渠道接入](#五各渠道接入)
- [六、對外接口](#六對外接口)
- [七、環境變數](#七環境變數)
- [八、客服工作台使用說明](#八客服工作台使用說明)
- [九、專案結構](#九專案結構)
- [十、疑難排解](#十疑難排解)
- [十一、已知限制](#十一已知限制)
- [十二、隱私與安全](#十二隱私與安全)
- [十三、授權](#十三授權)

---

## 一、這個專案解決什麼問題

微信公眾號、WhatsApp、Instagram、Facebook 都**不能直接把消息轉發給第三方系統**，
必須有個服務站在中間接住消息、轉換格式。本專案就是這個中轉層：

```
客戶 ──▶ 平台伺服器 ──▶ 本服務 webhook ──▶ 本機 PostgreSQL ──▶ 客服工作台 /console
                                                                     │
                                                          回覆 / AI 自動回復
                                                                     │
                              平台 API（微信客服消息 / WhatsApp Cloud API / Graph API）
                                                                     │
                                                                   客戶
```

| 渠道 | 平台伺服器 | 本服務 webhook | 對外隧道 |
|---|---|---|---|
| 微信公眾號 | 微信伺服器 | `POST /wechat` | natapp |
| WhatsApp | Meta 伺服器 | `POST /whatsapp/webhook` | ngrok |
| Instagram / Facebook | Meta 伺服器 | `POST /whatsapp/webhook`（同一個） | ngrok |

- 四個渠道**一視同仁**：落地本機 DB → 工作台查看 → 工作台回覆。
- 回覆由工作台直接呼叫平台 API 發送（微信走客服消息接口、WhatsApp 走 Cloud API、IG/FB 走 Graph API）。
- 本服務**不對外轉發**任何訊息，也沒有任何外部系統可以打進來要求發送。

---

## 二、功能一覽

| 渠道 | 接收消息 | 工作台查看 | 工作台回覆 | AI 自動回復 |
|---|---|:---:|:---:|:---:|
| 微信公眾號 | 文字 / 圖片 / 語音 / 影片 | ✅ | ⚠️ 需認證公眾號 | ✗（未認證號發不出） |
| WhatsApp | 文字 / 圖片 / 文件 / 語音等 | ✅ | ✅ | ✅ 私訊 |
| Instagram | 私訊 | ✅ | ✅ | ✅ 私訊 |
| Instagram | 貼文留言 | ✅ | ✅（需權限，見下） | ✅ 留言 |
| Facebook | 私訊 | ✅ | ✅ | ✅ 私訊 |
| Facebook | 貼文留言 | ✅ | ✅ | ✅ 留言 |

工作台另提供：

- **未讀紅點**（分平台 + 分會話）、**右鍵標為已處理**
- **貼文留言「已回 / 已忽略」標記**
- **檔案與圖片傳輸**（客戶來訊檔可下載，回覆可帶附件）
- **AI 自動回復**（第三節），含 FAQ 問答對管理、業務表格管理、
  RAG 知識庫（上傳 PDF / Word / Excel → 自動解析、分塊、向量化 → 混合檢索）
- **紀錄與溯源**：每次自動回復的處置、命中哪一層、未命中原因、召回哪些知識塊

---

## 三、AI 自動回復（四層決策）

每個渠道的**私訊**與**貼文留言**各自一把開關，在客服工作台左側直接切換（齒輪圖示進完整設定）。

```
客戶來訊（只處理文字）
  │
  ├─ 第一層：FAQ 問答對 ──► 規則匹配命中 → 直接回該答案（不呼叫 AI）
  │        （整層可停用；每個問答對也可單獨停用）
  │
  ├─ 第二層：業務表格 ──► 把表格內容交給模型理解後生成答案
  │        （整層可停用）
  │        ① 先用客戶原句查一次
  │        ② 查不到 → 帶入該會話最近 N 則對話，請模型把問句改寫成
  │           「可獨立理解」的完整問句，再查一次（N = TABLE_REWRITE_HISTORY_TURNS）
  │        ③ 還是查不到 → 視為未命中
  │
  ├─ 第三層：RAG 知識庫 ──► 混合檢索（向量 + 字面）取回知識塊，交給模型依內容作答
  │        （整層可停用；模型可否決命中與否由 RAG_ANSWER_GATE 決定）
  │
  └─ 第四層：降級人工回復
           不發送任何訊息，該則訊息在工作台標為
           「自動回復未命中」＋「待處理」
```

### 命中與改寫

- **FAQ 命中方式**：繁簡正規化（opencc）+ jieba 分詞 + 字元 bigram + 序列相似度，多信號加權打分，
  達門檻（`FAQ_MATCH_THRESHOLD`，預設 `0.45`）即命中。每個問答對可填「關鍵詞」欄擴充其他問法。
- **問句改寫（救回追問）**：客服對話常出現「那個多少錢？」「有貨嗎？」這類指代不明的短句，
  直接查表格鐵定查不到。此時系統會把**該會話最近 N 則對話**餵給模型，讓它把代名詞、
  被省略的主體還原成完整問句，再查一次。
  例：客戶先問「你們有賣耳機嗎？」→ 我方回「有 A 款藍牙耳機」→ 客戶問「那個多少錢？」，
  改寫成「A 款藍牙耳機多少錢？」後命中。
  - 改寫**只用於查詢**；回答本身仍只依據表格／知識庫內容，不會拿對話裡的舊資訊作答。
  - 改寫後仍查不到才降級人工；若模型改寫出跟原句一樣的問句，會直接降級、不重複查。
  - 改寫次數由 `TABLE_REWRITE_HISTORY_TURNS` 控制（預設 5，設 0 = 關閉）。
  - 知識庫層的多查詢改寫由 `RAG_QUERY_REWRITE` 控制，**一次改寫、三層共用**。
- **標記**：所有自動回復的內容都會在**工作台與日誌**標上
  「**自動回復-FAQ命中**」「**自動回復-表格命中**」或「**自動回復-知識庫命中**」；
  未命中標「**自動回復未命中**」。標記不會附加到發給客戶的訊息裡（客戶只看到乾淨的回答）。

### 防護機制

- **只處理文字訊息**：圖片、語音、貼圖、檔案一律留給人工。
- **冷卻與上限**：同一會話冷卻 `AUTOREPLY_COOLDOWN_SECONDS` 秒、每日每會話上限
  `AUTOREPLY_MAX_PER_DAY` 則。
- **一則訊息只回一次**：Meta / 微信 都會對同一個 webhook 事件**重複投遞**
  （沒在時限內收到 200 就重投），兩份幾乎同時到達時，冷卻檢查（先查再寫）擋不住併發，
  會導致同一則客戶訊息被回兩條。因此改用**原子認領**：處理前先用「渠道＋場景＋訊息 id」
  往 `autoreply_log.claim_key`（唯一索引）插一列，只有插得進去的處理者才繼續，其餘直接略過。
  - 訊息 id 優先用平台給的 `message_id` / `comment_id`；兩者都沒有時
    退回「渠道＋場景＋會話＋文字指紋」。
  - 認領列就是該次處理的日誌列（後續改寫它），所以仍是**一則訊息一列**。
  - 重複投遞會記在 log 的 `reason=duplicate`，可從紀錄分頁追查。
- **不會回覆自己**：我方送出的訊息（Meta 的 echo 事件）與我方自己的留言都會被擋掉，避免無限循環。
- **送出失敗有紀錄**：例如微信 48001，會標成「自動回復失敗，待人工處理」，不會靜默消失。

### 工作台操作

1. 左側選到某個渠道 + 私訊／貼文留言，用該列的按鈕切換此組合的自動回復。
2. 點齒輪進設定面板，分頁包含：
   - **自動回復**：全域預設、各渠道＋場景開關、各層停啟用、模型狀態、防護值、
     **試跑**（不發訊息）、各渠道成效。
   - **FAQ 問答對**：表格直接編輯（問題／答案／關鍵詞／啟用）、單筆或整批儲存、新增、刪除，
     上傳 **Excel / CSV** 匯入（可選清空後匯入或附加），下載**範本檔**，匯出目前 FAQ。
   - **業務表格**：新增表、編輯欄位、逐列編輯、刪列、停啟用，
     上傳 Excel / CSV 匯入（第一列為欄位名），匯出、範本檔。
   - **知識庫（RAG）**：上傳 PDF / Word / Excel / Markdown → 解析、分塊、向量化；
     知識庫分「文檔庫」與「圖片庫」兩型；提供檢索參數、分塊參數、提示詞三組可調參數。
   - **紀錄**：每次自動回復的處置（命中哪一層、未命中原因、送出結果、召回明細）。
3. 未命中的訊息在工作台列表標「未命中 N」，對話裡每則訊息也會有對應標記。

> ⚠️ **「全域」不是總開關**：優先序是「渠道＋場景 > 渠道 > 全域 > 預設關閉」，
> 全域只是「沒單獨設定」時的預設值。

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
- PostgreSQL（存轉發日誌、IG/FB 收件匣、自動回復設定與知識庫）
- 一個能讓外網訪問的位址（ngrok 或固定域名），因為微信 / Meta 需要回調你的服務
- Node.js（**只有** `tests/ui_check.py` 前端自檢需要，不影響服務運行）

### 2. 安裝依賴

```bash
git clone <你的 repo 網址> omni-cs
cd omni-cs
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# macOS / Linux
source .venv/bin/activate && pip install -r requirements.txt
```

> RAG 知識庫用的本地 embedding 模型（`jinaai/jina-embeddings-v2-base-zh`，約 640MB）
> **不在版控內**，首次使用知識庫時會自動下載到 `models/fastembed/`。
> 若不需要知識庫，可以完全不碰它。

### 3. 建立資料庫與資料表

先在 PostgreSQL 建好一個**可連線的帳號**，把連線字串填進 `.env` 的 `DATABASE_URL`，然後：

```bash
.venv\Scripts\python scripts\create_db.py
```

會建立（若不存在）`forward_log`（轉發日誌）、`igfb_dm` / `igfb_comment` / `igfb_post`
（IG/FB 收件匣），以及自動回復用的 `autoreply_faq` / `autoreply_business_table` /
`autoreply_business_row` / `autoreply_setting` / `autoreply_log` 與知識庫的 `kb_*` 表。
此腳本可重複執行，不會清掉既有資料。

> 服務啟動時也會自動補齊這些表與新欄位（`app/main.py` 的 lifespan 會呼叫
> `autoreply_db.init_schema()`），升級既有環境只要重啟服務即可，不一定要跑 `scripts/create_db.py`。
> **資料庫固定為 `platform_forwarder`**，不會動到其他庫。

### 4. 填寫設定

```bash
cp .env.example .env
```

編輯 `.env`，四個渠道的憑證都放在這裡。**欄位完整說明見第七節**；最少要先填：

- `DATABASE_URL`、`PUBLIC_BASE_URL`
- 微信：`WECHAT_TOKEN`、`WECHAT_APPID` / `WECHAT_APPSECRET`
- WhatsApp：`WHATSAPP_ACCESS_TOKEN`、`WHATSAPP_PHONE_NUMBER_ID`、`WHATSAPP_VERIFY_TOKEN`
- IG / FB：`META_PAGE_ID`、`META_PAGE_ACCESS_TOKEN`、`META_IG_USER_ID`、`META_VERIFY_TOKEN` 等
- 要用 AI 自動回復：`DEEPSEEK_API_KEY`（其餘自動回復設定見第七節）

### 5. 啟動

```bash
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

> 根目錄的 `main.py` 是相容殼，`uvicorn main:app` 也仍然可用。

### 6. 對外隧道

服務同時掛了微信、WhatsApp、IG/FB 三組端點，本專案的實際配置是兩條隧道：

| 渠道 | 隧道 | 說明 |
|---|---|---|
| 微信公眾號 | **natapp** | `mp.weixin.qq.com` 的「伺服器配置 URL」填 natapp 位址，指向 `/wechat` |
| WhatsApp / IG / FB | **ngrok** | Meta 後台的 Callback URL 填 ngrok 位址，指向 `/whatsapp/webhook` 或 `/webhook` |

兩條隧道都轉到本機同一個埠（`8001`），只是分開對外：

```bash
ngrok http 8001        # Meta 用（WhatsApp / IG / FB）
natapp -authtoken=xxx  # 微信用（設定檔在 natapp.ini）
```

把 ngrok 給的 `https://xxxx.ngrok-free.app` 填進 `.env` 的 `PUBLIC_BASE_URL`，重啟服務。

> `PUBLIC_BASE_URL` 的用途是**拼接媒體下載連結**（`<PUBLIC_BASE_URL>/files/xxx`），
> 供 Meta 抓檔用；所以填 ngrok 那一條即可，跟微信走哪條隧道無關。
>
> **ngrok 免費版限制**：只有 1 個域名、同時只能 1 條隧道。
> 若已有其他服務佔用，會報 `ERR_NGROK_334`。可改用付費版或 cloudflared（免費多域名）。

### 7. 打開客服工作台

瀏覽器開 **`http://127.0.0.1:8001/console`**（對外即 `https://你的域名/console`）。

---

## 五、各渠道接入

### 微信公眾號

1. `mp.weixin.qq.com` → 設置與開發 → 基本配置，勾選成為開發者。
2. 伺服器配置 → 修改配置：
   - **URL**：`https://你的域名/wechat`
   - **Token**：與 `.env` 的 `WECHAT_TOKEN` 一致
   - **EncodingAESKey**：點隨機生成，填進 `.env` 的 `WECHAT_ENCODING_AES_KEY`
   - **消息加解密方式**：明文 / 安全 / 兼容皆支援
3. 提交 → 微信會 GET 驗證 → 通過後點「啟用」。

> **回覆限制**：微信客服消息接口需要**認證公眾號**權限。未認證訂閱號回覆會失敗（錯誤碼 48001），
> 只能接收、不能回。

### WhatsApp（Meta Cloud API）

1. [Meta for Developers](https://developers.facebook.com/) 建 App，開通 WhatsApp，
   綁定電話號碼，拿到 `phone_number_id` 與 `access_token`。
2. WhatsApp → Configuration → Webhook：
   - **Callback URL**：`https://你的域名/whatsapp/webhook`
   - **Verify token**：與 `.env` 的 `WHATSAPP_VERIFY_TOKEN` 一致
   - 訂閱 `messages` 欄位

### Instagram / Facebook

見 **[docs/META_SETUP.md](docs/META_SETUP.md)** —— 完整的後台設定 checklist，
涵蓋 webhook 驗證、訂閱欄位、帳號授權、權限開通與權杖續期。

---

## 六、對外接口

本服務**不對外提供任何「發送」接口** —— 沒有任何外部系統可以打進來要求「送訊息給某個客戶」。

回覆客戶只有一條路徑：**客服工作台**（`POST /console/reply`），再由
`wechat_send.py` / `whatsapp_send.py` / `igfb.py` 呼叫平台 API 送出。

程式對外只開四個入口，全部是「接收平台推來的訊息」或「驗證」：

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/wechat` | 微信伺服器驗證（回傳 echostr） |
| POST | `/wechat` | 接收微信訊息（XML） |
| GET | `/whatsapp/webhook`、`/webhook` | Meta webhook 驗證（WhatsApp / IG / FB 共用） |
| POST | `/whatsapp/webhook`、`/webhook` | 接收 Meta 訊息（JSON） |

靜態路徑：

| 路徑 | 內容 | 是否公開 |
|---|---|---|
| `/files/*` | `MEDIA_DIR`（客戶來訊檔案、我方回覆附件） | ⚠️ 公開、無鑑權（供 Meta 抓檔用） |
| `/media/*` | `RAG_MEDIA_DIR`（知識庫圖片，**只放圖片**） | ⚠️ 公開、無鑑權 |

> `/console` 與 `/console/*` 為工作台與其 API，**目前沒有內建身分驗證**。
> 部署到公網前請務必加上一層反向代理驗證或限制來源 IP（見[第十二節](#十二隱私與安全)）。

---

## 七、環境變數

完整範本與註解見 **[.env.example](.env.example)**。以下為常用欄位。

### 通用

| 欄位 | 說明 |
|---|---|
| `DATABASE_URL` | PostgreSQL 連線字串 |
| `PUBLIC_BASE_URL` | 本服務對外 https 位址（拼接媒體下載連結給 Meta 抓檔） |
| `MEDIA_DIR` | 媒體落地目錄（預設 `media/`） |

### 微信

| 欄位 | 說明 |
|---|---|
| `WECHAT_TOKEN` | 後台「伺服器配置」填的 Token |
| `WECHAT_APPID` / `WECHAT_APPSECRET` | 開發者 ID，下載語音 / 影片用 |
| `WECHAT_ENCRYPT_MODE` | `plain` / `compatible` / `safe`（預設 `plain`） |
| `WECHAT_ENCODING_AES_KEY` | 安全 / 兼容模式的 43 字元金鑰 |
| `CHANNEL` / `CHANNEL_IDENTITY_ID` | 寫進來訊 payload 的渠道標識（純識別用，不對外送出） |

### WhatsApp

| 欄位 | 說明 |
|---|---|
| `WHATSAPP_VERIFY_TOKEN` | Meta 後台 webhook 驗證 token |
| `WHATSAPP_ACCESS_TOKEN` | Meta App access token（下載 / 發送媒體） |
| `WHATSAPP_PHONE_NUMBER_ID` | 電話號碼 ID（發送回覆用） |
| `WHATSAPP_CHANNEL` / `WHATSAPP_CHANNEL_IDENTITY_ID` | 寫進 payload 的渠道標識（純識別用，不對外送出） |

### Instagram / Facebook

| 欄位 | 說明 |
|---|---|
| `META_APP_ID` / `META_APP_SECRET` | App 編號與密鑰（延長 / 衍生權杖時需要） |
| `META_VERIFY_TOKEN` | webhook 驗證 token（Meta 後台自行設定） |
| `META_VERIFY_SIGNATURE` | 是否強制驗證 webhook 簽章（預設 `false`） |
| `META_GRAPH_VERSION` / `GRAPH_API_VERSION` | Graph API 版本 |
| `META_PAGE_ID` | Facebook 粉絲專頁 ID |
| `META_PAGE_ACCESS_TOKEN` | 粉專權杖（FB 私訊 / 貼文 / 留言 + IG 貼文 / 留言） |
| `META_USER_ACCESS_TOKEN` | 長效 user token（衍生 / 延長粉專權杖用，程式本身不使用） |
| `META_IG_USER_ID` | IG 專業帳號 ID（辨識「我方」、讀寫 IG 貼文留言） |
| `META_IG_ACCESS_TOKEN` | Instagram 登入路徑權杖（IGAA…，IG 私訊用） |
| `META_IG_GRAPH_HOST` | IG 發送主機（留空 = 依權杖前綴自動判斷） |

> 有兩組 Meta 權杖是正常的：**粉專權杖**走 `graph.facebook.com`（管 FB 與 IG 貼文留言），
> **IG 登入權杖**（IGAA…）走 `graph.instagram.com`（管 IG 私訊）。兩者是不同路徑，別搞混。

### AI 自動回復（模型與防護）

| 欄位 | 預設 | 說明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | （空） | **留空則表格層與知識庫層停用**，直接降級人工 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 位址（任何 OpenAI 相容端點皆可） |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 模型 ID |
| `DEEPSEEK_TIMEOUT` | `30` | 單次呼叫逾時（秒） |
| `DEEPSEEK_AUX_TIMEOUT` | `5` | 輔助呼叫（改寫問句）逾時（秒），短、不重試 |
| `DEEPSEEK_MAX_CONCURRENCY` | `2` | 同時送出的模型呼叫上限 |
| `DEEPSEEK_VISION_TIMEOUT` | `90` | 看圖生描述的逾時（秒） |
| `FAQ_MATCH_THRESHOLD` | `0.45` | FAQ 規則匹配門檻（0~1，越高越嚴格） |
| `TABLE_CONTEXT_MAX_ROWS` | `200` | 餵給模型的表格列數上限（0 = 不限） |
| `TABLE_CONTEXT_MAX_CHARS` | `12000` | 餵給模型的表格字數上限（0 = 不限） |
| `TABLE_REWRITE_HISTORY_TURNS` | `5` | 業務表格查不到時，帶入最近幾則對話做 query 改寫（0 = 關閉） |
| `AUTOREPLY_COOLDOWN_SECONDS` | `30` | 同一會話的自動回復冷卻秒數（0 = 不限制） |
| `AUTOREPLY_MAX_PER_DAY` | `30` | 同一會話每日自動回復上限（0 = 不限制） |

**提示詞**可直接用環境變數覆寫（留空 = 用 `app/core/config.py` 內建預設值）：

| Key | 用途 | 必須保留的佔位符 |
|---|---|---|
| `DEEPSEEK_SYSTEM_PROMPT` | 基本人設，所有 AI 回覆都用 | — |
| `DEEPSEEK_COMMENT_PROMPT` | 公開留言（`scope=comment`）的附加規則 | — |
| `DEEPSEEK_TABLE_PROMPT` | 業務表格回答 | `{context}`、`{sentinel}` |

**寫法（重要）**：多行提示詞必須用**雙引號**包起來，換行寫 `\n`：

```dotenv
DEEPSEEK_SYSTEM_PROMPT="你是一位專業、友善的客服助手。\n請用簡潔的繁體中文回答，不要提及自己是 AI。"
```

不加引號的話 `\n` 會變成人看得見的「反斜線 + n」，提示詞不會換行。
`DEEPSEEK_TABLE_PROMPT` 是用 Python `.format()` 帶入佔位符，提示詞裡若要寫字面上的大括號，
得寫成 `{{` 與 `}}`，否則會報錯。

> ⚠️ 「查詢改寫」提示詞**不在 `.env` 的提示詞清單裡**：三層（FAQ／業務表格／知識庫）共用同一條，
> 它是可在頁面編輯的參數 —— 工作台「知識庫 → 參數配置 → C. 提示詞」。

### RAG 知識庫

`.env` 的值只是**內建預設**；工作台「知識庫 → 參數配置」寫入的值存在資料庫（`kb_param`），
執行時 DB 優先、約 5 秒生效。**分塊參數按知識庫存，檢索參數與提示詞一律全域。**

| 類別 | 欄位 | 預設 | 說明 |
|---|---|---|---|
| 模型 | `RAG_EMBED_MODEL` | `jinaai/jina-embeddings-v2-base-zh` | 本地 embedding 模型（onnxruntime，不需 torch） |
| 模型 | `RAG_EMBED_DIM` / `RAG_EMBED_MAX_TOKENS` | `768` / `8192` | 向量維度與模型 token 上限 |
| 模型 | `RAG_HF_ENDPOINT` | `https://hf-mirror.com` | 模型下載鏡像 |
| 模型 | `RAG_HF_OFFLINE` | `auto` | 已下載則離線，失敗才回線上 |
| 模型 | `RAG_EMBED_THREADS` / `RAG_EMBED_BATCH` | `6` / `16` | 推論執行緒與批次 |
| 模型 | `RAG_RERANK_MODEL` / `RAG_RERANK_TIMEOUT` | 介面預留 | 第一版不裝 reranker |
| 路徑 | `RAG_MODEL_DIR` / `RAG_KB_DIR` | `models/fastembed` / `media/kb` | 模型快取與上傳來源檔（**不對外**） |
| 路徑 | `RAG_MEDIA_DIR` / `RAG_MEDIA_URL_PREFIX` | `media/kb_assets` / `/media` | 知識庫圖片公開目錄與網址前綴 |
| 檢索 | `RAG_TOP_K` | `6` | 送進模型的知識塊數 |
| 檢索 | `RAG_MIN_SCORE` | `0.28` | 入選門檻（**實測校正值**，過高會讓第三層形同關閉） |
| 檢索 | `RAG_MAX_PER_DOC` | `3` | 單一文件最多入選幾塊 |
| 檢索 | `RAG_MAX_CONTEXT_CHARS` | `6000` | 拼給模型的總字數 |
| 檢索 | `RAG_VECTOR_WEIGHT` / `RAG_LEXICAL_WEIGHT` | `0.65` / `0.35` | 向量與字面分數權重 |
| 檢索 | `RAG_QUERY_REWRITE` / `RAG_QUERY_REWRITE_N` | `false` / `2` | 多查詢改寫開關與條數（三層共用） |
| 檢索 | `RAG_ANSWER_GATE` | `image` | 模型可否決已過門檻的命中：`image` / `on` / `off` |
| 分塊 | `RAG_CHUNK_STRATEGY` | `auto` | `auto` / `paragraph` / `heading` / `fixed` / `table_row` |
| 分塊 | `RAG_CHUNK_SIZE` / `MIN` / `MAX` | `500` / `200` / `600` | 目標塊長與上下限（字） |
| 分塊 | `RAG_CHUNK_OVERLAP` / `RAG_CHUNK_PREFIX` | `80` / `true` | 重疊字數、每塊是否加「文件 › 章節」前綴 |
| 上傳 | `RAG_MAX_UPLOAD_MB` | `50` | 單檔上傳大小上限 |
| 提示詞 | `DEEPSEEK_RAG_PROMPT` | 內建預設 | 知識庫問答提示詞，必留 `{context}`、`{sentinel}` |
| 提示詞 | `DEEPSEEK_RAG_REWRITE_PROMPT` | 內建預設 | 查詢改寫提示詞，必留 `{question}`、`{n}` |
| 提示詞 | `DEEPSEEK_IMAGE_CAPTION_PROMPT` | 內建預設 | 「AI 描述」按鈕用的圖片描述提示詞 |

> 改完 `.env` 要**重啟服務**才會生效（服務未帶 `--reload`）。
> 改動分塊參數後需要重新分塊 + 重跑 embedding，該知識庫才會生效。

---

## 八、客服工作台使用說明

### 導覽結構

```
私訊                         貼文留言
├─ 微信     ┐               ├─ Instagram
├─ WhatsApp │ 以「使用者」為單位  └─ Facebook     以「貼文」為單位
├─ Instagram│               │
└─ Facebook ┘               └─ 點貼文 → 右側貼文詳情頁 + 留言
```

一級分「**私訊**」「**貼文留言**」兩大塊；進來預設是私訊的微信。
分頁右側的**紅點數字**代表該渠道未處理的訊息數。

### 私訊

- 左側列表**以使用者為單位**：頭像、名稱、最後一句話、時間，右上角紅點顯示未處理數。
- 點進去看完整對話串（客戶在左、我方在右），底部輸入框可回覆文字或上傳檔案 / 圖片。
- **回覆管道**：微信 / WhatsApp 由工作台直接送出；IG / FB 走 `igfb_console`
  （用 `/files/` + `PUBLIC_BASE_URL` 讓 Meta 抓附件）。

### 貼文留言

- 列表**以貼文為單位**：貼文摘要（caption）、留言數、未處理留言數（「N 新」標記）。
- 點進去看貼文詳情（媒體圖、caption、留言總數）與留言列表，每條留言下可「回覆」。
- **回覆**過的留言標「**已回**」；**右鍵「標為已處理（忽略）」**的留言標「**已忽略**」。
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

## 九、專案結構

```
omni-cs/
├─ main.py                      # 相容入口：`uvicorn main:app` 仍可用（真正入口在 app/main.py）
├─ requirements.txt
├─ .env.example                 # 設定範本（複製為 .env 後填寫）
├─ README.md
│
├─ app/                         # 業務程式碼（分層）
│  ├─ main.py                   # 服務入口：webhook 接收與分派、靜態檔案、自動回復鉤子
│  ├─ core/                     # 基礎設施
│  │  ├─ config.py              # 集中讀取環境變數
│  │  └─ db.py                  # 轉發日誌寫入（forward_log）
│  ├─ llm/                      # AI 層
│  │  ├─ deepseek_client.py     # OpenAI 相容的非同步客戶端
│  │  ├─ faq_matcher.py         # FAQ 匹配算法（繁簡正規化 + 分詞 + bigram + 相似度加權）
│  │  └─ reply_engine.py        # 四層決策引擎：FAQ → 業務表格 → 知識庫 → 未命中
│  ├─ rag/                      # 知識庫（RAG）
│  │  ├─ rag_config.py          # 檢索參數與提示詞（單一真相）
│  │  ├─ kb_parse.py            # 文件解析（PDF / Word / Excel / Markdown）→ 帶座標的 Block
│  │  ├─ kb_chunk.py            # 分塊（含重疊）
│  │  ├─ kb_embed.py            # embedding（fastembed / onnxruntime）
│  │  ├─ kb_rank.py             # rerank（介面預留）
│  │  ├─ kb_ingest.py           # 入庫狀態機（可中斷續跑）
│  │  ├─ kb_search.py           # 混合檢索（向量 + 字面）
│  │  ├─ kb_db.py               # 知識庫資料層
│  │  └─ kb_console.py          # 知識庫 API（/console/kb）
│  ├─ channels/                 # 外部平台適配
│  │  ├─ wechat.py              # 微信：簽名驗證、XML 解析、轉換
│  │  ├─ wechat_send.py         # 微信：客服消息發送
│  │  ├─ whatsapp.py            # WhatsApp：webhook 解析、轉換
│  │  ├─ whatsapp_send.py       # WhatsApp：訊息與媒體發送
│  │  ├─ igfb.py                # Meta：Graph API 讀寫 + webhook 事件解析
│  │  └─ igfb_db.py             # Meta：IG/FB 收件匣資料讀寫 + 歷史同步
│  ├─ autoreply/                # 自動回復
│  │  ├─ autoreply.py           # 編排：開關、防護、發送、標記未命中
│  │  ├─ autoreply_db.py        # 資料層：建表 + FAQ / 表格 / 開關 / 日誌 CRUD
│  │  ├─ autoreply_console.py   # 設定介面 API（/console/autoreply）
│  │  └─ backfill.py            # 批次補做「開啟前累積的未處理訊息」
│  ├─ api/                      # Web / API 層
│  │  ├─ console.py             # 工作台後台接口（微信 / WhatsApp）
│  │  └─ igfb_console.py        # Meta 工作台後台接口
│  └─ utils/                    # 工具
│     ├─ forwarder.py           # 媒體下載（media_id / Graph API → media/）
│     ├─ token_refresh.py       # IG 登入權杖自動續期（啟動時 + 每日）
│     └─ xlsx_io.py             # Excel / CSV 解析與範本產生
│
├─ ui/
│  └─ console_ui.html           # 工作台前端（單檔、自帶樣式與邏輯）
│
├─ scripts/                     # 人工執行的維運腳本
│  ├─ create_db.py              # 建立資料表（可重複執行，讀 .env 的 DATABASE_URL）
│  ├─ token_tool.py             # 權杖工具：check / extend / derive / renew
│  └─ edition_change.py         # Meta changelog 變更檢查（需另裝 selenium / bs4）
│
├─ tests/                       # 可重複執行的自檢腳本
│  ├─ engine_check.py           # 四層引擎自檢
│  ├─ kb_check.py               # 知識庫入庫管道自檢
│  ├─ kb_search_check.py        # 檢索命中率與分數分佈
│  ├─ kb_api_check.py           # 知識庫 HTTP 端點自檢
│  ├─ kb_asset_check.py         # 知識庫附圖（圖文一起發）自檢
│  ├─ kb_scope_check.py         # 跨知識庫範圍 / 門檻 / 索引成本
│  ├─ model_check.py            # AI 模型連線與對話自檢
│  ├─ rag_model_check.py        # 本地 embedding 模型自檢
│  └─ ui_check.py               # 工作台前端渲染自檢（需 Node.js）
│
└─ docs/
   ├─ RAG_DESIGN.md             # RAG 第三層設計稿
   └─ META_SETUP.md             # Meta 後台設定 checklist
```

自檢腳本一律**從專案根目錄**執行，例如：

```bash
.venv\Scripts\python tests\engine_check.py
set UI_NODE=<node 執行檔路徑> && .venv\Scripts\python tests\ui_check.py   # 服務要開著
```

> 執行時會產生 `media/`（媒體落地目錄）與 `models/`（模型快取）。
> 兩者皆為執行期資料，已排除在版控外。

---

## 十、疑難排解

| 現象 | 原因與處理 |
|---|---|
| 服務啟動即報埠被佔用 | 先前的實例還活著。找出佔用 `8001` 的進程結束掉再啟動。 |
| 改了程式碼但行為沒變 | 服務未帶 `--reload`，**改完後端要重啟**；前端頁面按 `Ctrl+Shift+R` 強制刷新。 |
| Meta webhook 驗證失敗 | Callback URL 要能被外網訪問；`Verify token` 要與 `.env` 完全一致。 |
| 回覆留言 / 私訊回 **502** | 多半是 **Meta 權杖失效**。跑 `python scripts/token_tool.py check` 確認，詳見 `docs/META_SETUP.md` 第六節。 |
| 按「同步最新資料」卻沒有新貼文 | 同上，權杖失效時同步會靜默失敗。 |
| 微信後台「啟用」失敗 | URL / Token 與 `.env` 不符，或服務沒在跑、natapp 隧道沒開。 |
| 媒體檔下載不到 | `.env` 的 `PUBLIC_BASE_URL` 沒填或填錯；微信個人號下載媒體權限需實測。 |
| IG 留言回覆回 502 | 缺 `instagram_manage_comments` 權限（詳見 `docs/META_SETUP.md`）。 |
| 留言者姓名顯示「用戶」 | 平台隱私限制，非程式問題（見第十一節）。 |
| 自動回復完全沒動作 | 三個地方都要開：① `.env` 有 `DEEPSEEK_API_KEY` ② 設定面板的**全域**值 ③ 該**渠道＋場景**的開關。另外微信不支援自動回復。 |
| 自動回復都掉到人工 | 看設定面板的**紀錄**分頁，`reason` 會寫明原因：`no_match`、`no_api_key`、`llm_error`、`throttled`（冷卻／上限）等。 |
| 自動回復重複回同一則 | 已修（見第三節「一則訊息只回一次」）。若仍重複，檢查紀錄分頁是否同一 `message_id` 有兩列 `send_status=sent`；有的話代表跑的是舊版程式碼。 |
| 追問句都查不到（「那個多少錢？」） | 確認 `TABLE_REWRITE_HISTORY_TURNS` > 0，且該會話真的有待處理的歷史訊息（新會話沒有上下文可改寫）。 |
| 自動回復回了不相關的答案 | 提高 `FAQ_MATCH_THRESHOLD`、把該 FAQ 停用、或在關鍵詞欄補上更精確的問法。 |
| 自動回復太慢 | 第二、三層要呼叫模型（每次約 1~5 秒）。自動回復是背景執行，不影響 webhook 回應速度；要更快就讓第一層 FAQ 涵蓋更多常見問題。 |
| 知識庫「命中了卻不回復」 | 檢查 `RAG_MIN_SCORE`（建議 0.30~0.32；太低會誤命中、太高會全被擋下）與 `RAG_ANSWER_GATE`。 |
| 改了 FAQ／表格／參數但沒生效 | 設定介面改動會即時清快取；知識庫參數約 5 秒生效；分塊參數要重建知識庫。 |
| `ui_check.py` 說找不到 node | 安裝 Node.js，或設 `UI_NODE=<node 執行檔完整路徑>`。 |

---

## 十一、已知限制

- **微信回覆**：需認證公眾號；未認證號只能收不能回（48001）。因此**微信的 AI 自動回復目前一定會失敗**，
  系統會標成「自動回復失敗，待人工處理」，不會靜默消失。故設定介面不提供微信的自動回復開關。
- **IG 留言回覆**：需 `instagram_manage_comments` 權限。未開通前能看能讀，回覆會被 Meta 擋（自動回復同理）。
- **留言者姓名**：FB / IG 對陌生留言者的姓名有平台隱私限制，`from.name` / `username` 可能為空，
  工作台顯示「用戶」。這是平台限制，非程式問題。
- **FB 貼文圖片**：圖片連結是 Meta 的簽名 CDN 網址、會過期，但每次同步都會刷新。
- **事件 / 狀態消息**（微信關注取關、WhatsApp 已讀 / 送達回執）：目前忽略，不入庫。
- **超時**：微信 / Meta 要求 5 秒內回應。本服務落地寫入是背景寫 DB，來訊一律立即回 200，
  寫入失敗只記 log，不影響對方。自動回復丟到背景執行，不會拖慢 webhook 回應。
- **自動回復只處理文字**：圖片、語音、貼圖、檔案一律留給人工。
- **AI 可能出錯**：表格層與知識庫層由模型生成答案，仍可能理解偏差。
  建議定期看設定面板的「紀錄」，把答不好的問題補進第一層 FAQ（規則命中比模型生成更穩定）。
- **表格規模**：業務表格內容會整份餵給模型，受 `TABLE_CONTEXT_MAX_ROWS` / `TABLE_CONTEXT_MAX_CHARS` 限制。
- **知識庫檢索門檻**：預設 `RAG_MIN_SCORE=0.28` 是在特定語料上實測校正出來的。
  換到你的語料時，建議用 `tests/kb_search_check.py` 重跑一次再定。

---

## 十二、隱私與安全

這個專案會處理真實客戶的訊息與檔案，部署前請先讀完這一節。

### 版控與憑證

- **所有憑證只放 `.env`**：`.gitignore` 已排除 `.env`、`.env.*`（`.env.example` 除外）、
  `*.pem` / `*.key` / `*.p12` 等，請**不要**把任何真實金鑰寫進程式碼或文件。
- **簽入前自我檢查**：建議在 push 前跑一次
  `git grep -nE "sk-[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{30,}|postgresql://[^ ]*:[^ ]*@"`
  確認沒有殘留憑證（含歷史紀錄）。
- **已排除的執行期資料**：`media/`（客戶來訊檔案與上傳來源文件）、`models/`、
  `.workbuddy/`（開發輔助工具的工作目錄）皆不進版控。
  `media/` 內含個資，**不要**為了方便而改成納入版控。

### 執行期的資料處理

- **訊息只落地本機資料庫**，不對外轉發給任何第三方系統。
- **`/files` 與 `/media` 是公開、無鑑權的靜態路徑**：
  - `/files` 指向 `MEDIA_DIR`，內含**客戶來訊的檔案與我方回覆的附件** ——
    只要能猜到檔名就能下載（檔名為平台產生的隨機字串），這是讓 Meta 抓檔的必要設計。
  - `/media` 只指向 `RAG_MEDIA_DIR`（知識庫圖片），**不要**把它指到整個 `media/`。
  - 對公網部署時，建議在反向代理層對 `/files`、`/console` 加上來源限制或驗證。
- **`/console` 工作台目前沒有內建身分驗證**：等同「誰能連到這個埠，誰就能讀客戶對話、
  用你的名義回覆」。請只綁定 `127.0.0.1`，或放在 VPN / 反向代理驗證之後，
  **不要直接裸露在公網**。
- **webhook 簽章驗證**：Meta 側建議設 `META_VERIFY_SIGNATURE=true` 並填好 `META_APP_SECRET`，
  確保來訊真的來自 Meta。
- **資料庫權限**：建議為本服務建一個**專用資料庫帳號**（不要用 superuser），
  並讓 PostgreSQL 只監聽本機或內網。
- **日誌**：服務啟動時已把 `httpx` 的日誌壓到 `WARNING`，
  避免把 query string 裡的 `appsecret` 等敏感值寫進 log。
- **AI 供應商**：自動回復會把客戶訊息（以及命中的業務表格 / 知識庫內容）送到你設定的
  `DEEPSEEK_BASE_URL`。若客戶資料不能出境或不能離開你的環境，
  請改用自架或符合規範的端點。
- **對外一律走 HTTPS**：微信與 Meta 都要求回調網址是 https（ngrok / cloudflared / 正式憑證皆可）。

### 上線前的檢查清單

1. `DATABASE_URL` 用的是專用帳號，不是 superuser。
2. `PUBLIC_BASE_URL` 是最終對外網址。
3. 所有 `*_TOKEN` / `*_SECRET` / `*_API_KEY` 都填了，且**沒有**被 commit。
4. `.env` 的備份也不要放進任何雲端硬碟的同步目錄。
5. `/console` 與 `/files` 已加上反向代理層的存取控制。

---

## 十三、授權

本專案以 **MIT License** 釋出，詳見 [LICENSE](LICENSE)。

### 免責聲明

本軟體為工具性質，依「現狀」提供，不附任何明示或默示之擔保。
使用者需自行確保其使用方式符合各平台（微信 / Meta）的開發者政策與當地法規
（包含客戶個人資料的蒐集、處理與利用）。因使用本軟體所產生之任何直接或間接損害，
作者不負賠償責任。

> 註：本專案在文件解析上刻意採用 MIT 授權的 `pdfplumber` 而非 AGPL 授權的 PyMuPDF，
> 以降低商用授權風險。
