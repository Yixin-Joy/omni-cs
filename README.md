# 多渠道 AI 客服（微信 / WhatsApp / Instagram / Facebook）

把四個渠道的客戶消息統一接進來，落地資料庫、集中在一個網頁（客服工作台）查看與回覆，
並提供 **AI 四層自動回覆**（FAQ → 業務表格 → 知識庫 RAG → 未命中降人工）。

> 這份 README 描述的是**新架構（雙服務）**。舊單體版本（單一 `app/`、單一 `main.py`）仍保留在
> 根目錄，為過渡期上線服務；新程式碼在 `connector/` 與 `crm/` 兩個子專案。

---

## 一、架構總覽（重要）

系統拆成**兩個獨立服務**（monorepo 內兩個子專案，各自有自己的 `main.py` / `.env` / `requirements.txt`）：

```
客戶 ──▶ 平台伺服器 ──▶ Connector（純通道，8010）──▶ CRM（大腦，8020）
                         收訊息 / 存訊息              AI 引擎 / 知識庫 / 工作台
                         發訊息 / 存媒體              建出站任務 / 內網 API
                                 ▲                          │
                                 └────── 內網 API（HTTP）────┘
                                        claim / ai-inbound / update-status ...
```

| 服務 | 埠 | 角色 | 資料庫 | 啟動（在該目錄下） |
|---|---|---|---|---|
| **Connector** | 8010 | 純通道：收訊息、存訊息、發訊息、存媒體 | `platform_forwarder` | `uvicorn main:app --host 127.0.0.1 --port 8010` |
| **CRM** | 8020 | 大腦：AI 引擎 + RAG 知識庫 + 工作台 + 管理後台 + 運維頁 | `crm_db` | `uvicorn main:app --host 127.0.0.1 --port 8020` |

- **為什麼拆兩個**：Connector 只做「訊息與媒體的搬運」，不含任何 AI / 知識庫依賴，可獨立擴容、
  貼近渠道部署；CRM 才是決策大腦。兩者靠 `X-Internal-Token` 認證的**內網 API** 溝通。
- **為什麼兩個 package 都叫 `app`**：這是刻意的，讓 `from app.rag import …` 這類 import
  在搬遷時一行都不用改。代價是**同一個 Python 進程不能同時 import 兩邊**，所以自檢用
  「起兩個服務 + 子進程驅動」的方式（見 `tests/`）。
- **兩個 DB 都有 `kb_*` 表**：這是舊單體殘留，實際上線的是 `crm_db` 那一份。

> 對外只有 Connector 的 webhook 對公網開放；CRM 完全不對外（只綁 `127.0.0.1`）。

---

## 二、功能一覽

| 渠道 | 接收消息 | 工作台查看 | 工作台回覆 | AI 自動回覆 |
|---|---|:---:|:---:|:---:|
| 微信公眾號 | 文字 / 圖片 / 語音 / 影片 | ✅ | ⚠️ 需認證公眾號 | ✗（未認證號發不出） |
| WhatsApp | 文字 / 圖片 / 文件 / 語音等 | ✅ | ✅ | ✅ 私訊 |
| Instagram | 私訊 | ✅ | ✅ | ✅ 私訊 |
| Instagram | 貼文留言 | ✅ | ✅（需權限） | ✅ 留言 |
| Facebook | 私訊 | ✅ | ✅ | ✅ 私訊 |
| Facebook | 貼文留言 | ✅ | ✅ | ✅ 留言 |

工作台另提供：**未讀紅點**（分平台 + 分會話 + 分商家）、**標為已處理**、**貼文留言「已回 / 已忽略」**、
**檔案與圖片傳輸**、**貼文特殊配置**（單一貼文覆寫自動回覆設定）、**手動重新整理（補拉歷史）**，
以及 **AI 四層自動回覆**。

---

## 三、AI 自動回覆（四層決策）

每個渠道的**私訊**與**貼文留言**各自一把開關（在後台設定）。決策順序唯一來源是
`reply_engine.ALL_LAYERS`：

```
客戶來訊
  │
  ├─ 第一層 FAQ 問答對 ──► 規則匹配命中 → 直接回該答案（不呼叫 AI）
  │        （未命中且開啟改寫 → DeepSeek 改寫問句再匹配一次）
  │
  ├─ 第二層 業務表格 ──► 表格內容交給 DeepSeek 生成答案
  │
  ├─ 第三層 知識庫 RAG ──► 向量 + 字面混合檢索，命中帶圖可一併送出
  │
  └─ 未命中 ──► 不發訊息，工作台標「自動回復未命中」＋「待處理」
```

- **開關是兩層 AND**：`business_ai_switch`（商家 × 平台 × 場景）AND `business_platform_account`
  的帳號層開關。兩者都開才跑。**全域不是總開關**，而是「渠道+場景 > 渠道 > 全域 > 預設關閉」的優先序。
- **一則訊息只回一次**：`autoreply_log.claim_key`（唯一索引）做原子認領，擋掉 Meta / 微信的
  webhook 重複投遞。
- **命中後不直發**：CRM 命中會**建一筆出站任務**（`crm_outbound_task`），由 Connector 的
  worker 認領後才真正送出。圖文送出只有 WhatsApp 私訊會帶 caption（文字塞圖一起、只一條），
  其餘平台文字先、圖後。
- 完整細節見 `docs/RAG_DESIGN.md`（RAG 第三層設計，權威）與 `docs/MIGRATION_PLAN.md`（搬遷計畫）。

---

## 四、快速開始（VSCode 起兩個服務）

### 1. 環境需求

- Python 3.11+（本專案開發環境為 3.14）
- PostgreSQL 16+（本專案為 18；兩個庫：`platform_forwarder` 與 `crm_db`）
- 一條公網隧道（`ngrok` 或 `natapp`）**指向 Connector 8010**，因為微信 / Meta 要回調你的
  服務（專案根附了 `ngrok.exe`，用法見第六節）
- （可選）DeepSeek API Key，第二層表格與 FAQ 改寫要用

### 2. 安裝依賴

兩個子專案共用**根目錄的 `.venv`**（依賴是根 `requirements.txt` 的超集）：

```bash
cd Multi-CS
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
```

> ⚠️ 若 `.venv` 是在**含中文的路徑**下建立的（本機就是），`.venv\Scripts\python.exe` /
> `pip.exe` 會失效（報 `failed to open pyvenv.cfg`）。改用系統 Python 安裝：
> `<你的 python> -m pip install -r requirements.txt`，跑服務同理（見第 5 步）。

> `connector/requirements.txt` 與 `crm/requirements.txt` 是各自的最小依賴清單（用於獨立部署）；
> 本機開發直接裝根 `requirements.txt` 即可，兩邊都涵蓋。

### 3. 建立兩個資料庫

先在 PostgreSQL 建兩個庫，再跑初始化：

```bash
# 1) 建庫（庫名寫死，見 .env 的 DATABASE_URL）
createdb platform_forwarder
createdb crm_db

# 2) 渠道側的表（forward_log / igfb_dm / igfb_comment ...）
.venv\Scripts\python scripts\create_db.py

# 3) CRM 全部表 + 種子資料（含預設商家、可選 admin 帳號）
.venv\Scripts\python scripts\init_crm_db.py --admin-account admin --admin-password 'ChangeMe123'
```

> `init_crm_db.py` 會建 `crm_db`（若不存在）、建全部 29+ 張表、種預設商家，並可用
> `--admin-account/--admin-password` 建立登入帳號。兩個服務啟動時也會各自
> `init_schema()` 補齊表，升級既有環境重啟即可。

### 4. 填寫設定

```bash
cp connector/.env.example connector/.env
cp crm/.env.example crm/.env
```

最少要填：

- **Connector**（`connector/.env`）：`DATABASE_URL`（指到 `platform_forwarder`）、
  `CRM_INTERNAL_BASE=http://127.0.0.1:8020`、`INTERNAL_API_TOKEN`（兩邊要一致）、
  各渠道**握手 token**（微信 / WhatsApp / Meta，見第八節）。
  ⚠️ **業務憑證**（AppSecret / Access Token / EncodingAESKey）**不在 .env** —— 改在 admin
  後台「商家 → 平台帳號 → 憑證」填（存 CRM 資料庫，Connector 收發時自動取用）。
- **CRM**（`crm/.env`）：`DATABASE_URL`（指到 `crm_db`）、
  `CONNECTOR_INTERNAL_BASE=http://127.0.0.1:8010`、`INTERNAL_API_TOKEN`（同上）、
  `DEEPSEEK_API_KEY`（要用 AI 自動回覆）。

### 5. 啟動（開兩個終端）

**最簡單**：在專案根目錄執行（或雙擊）兩個啟動腳本：

```
start_connector.bat     # Connector -> http://127.0.0.1:8010
start_crm.bat           # CRM       -> http://127.0.0.1:8020
```

> ⚠️ **不要用 `.venv\Scripts\python.exe`** —— 本專案的 `.venv` 是搬到這裡之前、在含
> 中文的路徑下建立的，其 launcher 已失效（`failed to open pyvenv.cfg`）。但
> `.venv\Lib\site-packages` 本身完好，所以兩個 `.bat` 改用系統 Python
> （`pythoncore-3.14`）＋ `PYTHONPATH` 指過去啟動，效果與 venv 相同。

**手動（等價，開兩個終端）**：

```bat
REM 終端 1：Connector
cd C:\Users\its-admin\Desktop\Multi-CS\connector
set PYTHONPATH=C:\Users\its-admin\Desktop\Multi-CS\connector;C:\Users\its-admin\Desktop\Multi-CS\.venv\Lib\site-packages
C:\Users\its-admin\AppData\Local\Python\pythoncore-3.14-64\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8010

REM 終端 2：CRM
cd C:\Users\its-admin\Desktop\Multi-CS\crm
set PYTHONPATH=C:\Users\its-admin\Desktop\Multi-CS\crm;C:\Users\its-admin\Desktop\Multi-CS\.venv\Lib\site-packages
C:\Users\its-admin\AppData\Local\Python\pythoncore-3.14-64\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8020
```

> 兩個服務各自的 `.env` 由 `python-dotenv` 從**啟動時的 cwd**載入，所以一定要 `cd` 到
> 對應子目錄再啟動，否則會讀錯設定（`.bat` 已幫你 `cd`）。
> 若要用**替代埠**（例如想同時保留舊實例）：兩個 `.bat` 內的 `--port` 改掉即可，但
> Connector/CRM 的 `*_INTERNAL_BASE` 要跟著改。

### 6. 打開頁面

| 頁面 | 位址 | 用途 |
|---|---|---|
| 登入 | `http://127.0.0.1:8020/login` | 員工登入（兩步：選身分 → ticket） |
| 客服工作台 | `http://127.0.0.1:8020/agent` | 會話 / 自動回覆 / 命中測試 / 回覆紀錄 |
| 管理員後台 | `http://127.0.0.1:8020/admin` | 商家 / 平台帳號 / 員工與角色 / AI 開關 |
| 運維頁 | `http://127.0.0.1:8020/ops` | ops 配置 / 失敗任務重跑 / 告警 |

登入後依身分（admin / agent / ops）自動分流到對應頁面；`/` 也會依 `active_role` 分流。

---

## 五、客服工作台使用說明

工作台（`/agent`）四個分頁：

1. **會話**：左側會話列表（以使用者為單位，紅點＝未處理），點進看完整對話串，
   底部可回覆文字 / 上傳檔案圖片。AI 留痕顯示在每則客戶訊息旁（這句 AI 怎麼處理的、
   命中哪一層、分數多少）。
2. **自動回覆**：兩層開關（商家 × 平台 × 場景 + 帳號層）、FAQ / 表格 / 知識庫管理、
   檢索參數與提示詞、防護值（冷卻 / 上限）。
3. **命中測試**：不發訊息的試跑，看四層引擎對某問句的處置與命中的知識庫 chunk。
4. **回覆紀錄**：每次自動回覆的處置（命中哪一層、未命中原因、送出結果、命中的 chunk）。

貼文留言另有**貼文特殊配置**：對單一貼文覆寫自動回覆開關（全開 / 全關 / 自訂勾選層級）。

### 右鍵：只作用在「單條」訊息 / 留言上

右鍵**只掛在還沒被處理的那一則**上，**不會**掛在會話（用戶聊天框）上：

| 右鍵位置 | 選單 |
|---|---|
| 私訊的一則**客戶來訊氣泡**（未處理、有 message_id） | 由自動回復引擎回覆這一則 ／ **標記這一則已處理** |
| 貼文留言的**留言內容框**（頂層、未回、未忽略） | 由自動回復引擎給出回覆 ／ 標記為已處理（忽略） |

- 「由自動回復引擎…」＝ `POST /console/agent/draft`：**只產生草稿填進回覆框、不自動送出**。
  不檢查自動回復開關、不套冷卻與每日上限（人工判斷優先）；未命中會留一筆 `manual_no_match` 紀錄。
- 「標記這一則已處理」＝ `POST /console/agent/read` 帶 `message_id` → **只標這一則**，
  不會順手清掉整個會話的紅點。
- 我方回覆氣泡、已處理過的訊息、子留言都不掛右鍵。微信不掛（未認證號發不出客服消息）。

### 重新整理（補拉歷史）

「客服工作台」標題旁有一顆 **⟳** 按鈕。公網隧道（ngrok）斷掉時 webhook 推不進來，
那段時間的訊息不會入庫 —— 按一下這顆按鈕，CRM 會驅動 Connector 主動打平台 API，
把最近的會話 / 訊息 / 貼文 / 留言撈回來補齊。

- **只支援 Instagram / Facebook**：WhatsApp Cloud API 與微信客服消息都沒有可回溯
  的歷史端點，按鈕會回報「N 個帳號不支援補拉」（不是壞掉，是平台沒有）。
- ★★ **增量補拉，不重複處理**：水位取自**資料庫現有的最後一則**
  （會話 `updated_time` 不比它新 → 整個會話跳過，連訊息都不拉；訊息/留言的
  `created_at` 不比它新 → 不處理）。所以：
  - **只從「我們收到的最後一則」之後開始拉**，不會全量重刷。
  - **之前已經標了「已處理」或「已忽略」的，刷新後不會被翻回未處理**。
  - 沒有新訊息時會直接提示「已是最新」，不會看起來像按了沒反應。
- **紅點規則**：補進來的**新**入站私訊，若它之後（同一會話、時間更晚）已有我方回覆
  → 記為已處理（**不補紅點**）；之後沒有任何回覆 → **補紅點**。
- **留言**：只重算「這次真的**新增**留言」的討論串 ——
  已存在的留言完全不觸發重算，人工標的「已忽略」不會被蓋掉。
  （有新留言進來的討論串本來就該重新亮紅點。）
- 回覆後自動重載商家列紅點、會話列表與平台未處理數。
- 每次按鈕都會寫一筆審計（`operate_type = sync_history`，含帳號數與統計）。

---

## 六、對外接入（ngrok + 各渠道後台設定）

對外只需要**一條公網隧道，指向 Connector（8010）** —— webhook 收訊在那裡；CRM（8020）不對外。

### 1. 開隧道（ngrok → 8010）

專案根目錄附了 `ngrok.exe`：

```bash
ngrok http 8010
```

拿到形如 `https://<隨機字>.ngrok-free.dev` 的公網網址（免費版每次重啟會變）。

> ★ **一定要指向 8010（Connector）**，不是 8020（CRM）、也不是舊單體 8001。
> 指向錯的地方，客戶來訊不會進新架構。

### 2. 把公網網址寫進 `connector/.env`

```
PUBLIC_BASE_URL=https://<你的域名>.ngrok-free.dev
```

用途：把客戶來訊的媒體檔拼成公開網址，讓 Meta 抓得到。ngrok 網址變了要跟著改（見第八節）。

### 3. 各渠道後台填 Callback URL

| 渠道 | 後台位置 | URL 填 | token 對應 |
|---|---|---|---|
| 微信公眾號 | `mp.weixin.qq.com` → 設定與開發 → 伺服器配置 | `https://<域名>/wechat` | `WECHAT_TOKEN` |
| WhatsApp | Meta App → WhatsApp → Configuration | `https://<域名>/whatsapp/webhook` | `WHATSAPP_VERIFY_TOKEN` |
| Instagram / Facebook | Meta App → Webhooks | `https://<域名>/webhook` | `META_VERIFY_TOKEN` |

完整 Meta checklist 見 `docs/META_SETUP.md`。

> 本機實測域名：`https://slam-shivering-revival.ngrok-free.dev`（2026-10-14）。
> 填好後可 `curl https://<域名>/health` 確認隧道通到 Connector（回 `{"ok":true,...}`）。

### 4. 平台憑證放哪（★ 不在 .env）

按商家區分的**業務憑證**（AppSecret / Access Token / EncodingAESKey 等）**存 CRM 資料庫**，
由 admin 在後台「商家 → 平台帳號 → 憑證」填寫，經帳號映射同步給 Connector 收發使用。
`.env` 只留**握手 token**（webhook 接入驗證用，見第八節）。

> 既有環境一鍵匯入：`crm/scripts/import_credentials.py`（從根 `.env` 讀已調通的憑證寫進 DB）。
> 匯入後跑 `connector/tests/credentials_check.py` 驗證四平台連通（唯讀、不發訊息）。

> **多租戶 URL（可選）**：若要按商家用不同憑證，改用帶原生 ID 的路徑 ——
> 微信 `/wechat/{AppID}`、Meta `/webhook/{phone_number_id|page_id|ig_user_id}`。
> 不帶 ID 的舊路徑仍保留（走 `.env` 憑證，適合單商家過渡）。

> ✅ **webhook 接收與正規化已於 2026-10-13 搬到 Connector**：客戶來訊現在由 Connector 收到
> → 正規化 → 落地 `platform_forwarder` → 非同步打 CRM `/internal/api/ai/inbound` 入隊。
> 整條鏈（收訊 → CRM 引擎 → 建任務 → Connector 發送 → 回調）已打通，自檢 `webhook_check.py`
> 53 項全綠。**舊單體（8001）的 webhook 端點仍保留並行**（過渡期），等你把隧道指向 8010
> 後再停掉舊的。

---

## 七、對外 / 內網接口

**對外（Connector，公網）**：webhook 接收 —— `/wechat`、`/whatsapp/webhook`、`/webhook`
（及其 `/{native_id}` 變體）；客戶來訊媒體走 `/files/*` 靜態路徑。

**內網（Connector ↔ CRM，走 `X-Internal-Token`）**：契約見 `docs/INTERNAL_API.md`，主要：

| 方法 | 路徑 | 提供方 | 用途 |
|---|---|---|---|
| POST | `/internal/api/outbound/claim` | CRM | Connector 認領出站任務（`FOR UPDATE SKIP LOCKED`） |
| POST | `/internal/api/outbound/update-status` | CRM | Connector 回報發送結果 |
| POST | `/internal/api/ai/inbound` | CRM | Connector 把來訊快照入隊（AI 決策） |
| POST | `/internal/api/account-map/sync` | Connector | CRM 推送平台帳號映射 |
| POST | `/internal/api/media/upload` | Connector | CRM 上傳媒體 |
| GET | `/internal/api/conversations` | Connector | CRM 查會話列表（工作台） |
| GET | `/internal/api/pending-count` | Connector | 單帳號未處理數（平台 tab 紅點） |
| POST | `/internal/api/sync-history` | Connector | 主動補拉歷史（工作台「重新整理」） |
| POST | `/console/agent/draft` | CRM | 右鍵「由自動回復引擎產生回覆」（只出草稿、不送出） |

---

## 八、環境變數說明（.env）

### Connector（`connector/.env`）

| 欄位 | 說明 |
|---|---|
| `DATABASE_URL` | 指到 `platform_forwarder` |
| `PORT` / `INTERNAL_BIND_HOST` | 8010 / 127.0.0.1 |
| `CRM_INTERNAL_BASE` | CRM 內網位址（`http://127.0.0.1:8020`） |
| `INTERNAL_API_TOKEN` | 內網認證 token（兩邊一致） |
| `PUBLIC_BASE_URL` | ngrok 公網網址（**指向 8010**；拼媒體下載連結給 Meta 抓檔，ngrok 網址變了要跟著改） |
| `MEDIA_DIR` / `KB_CACHE_DIR` | 媒體 / 知識庫快取目錄 |
| `OUTBOUND_SEND_MODE` | `live`（真發）或 `mock`（離線自檢用） |
| `WECHAT_TOKEN` / `WHATSAPP_VERIFY_TOKEN` / `META_VERIFY_TOKEN` / `META_APP_SECRET` | **握手 token**（webhook 接入驗證用；驗證階段不知商家，故留 .env） |

### CRM（`crm/.env`）

| 欄位 | 說明 |
|---|---|
| `DATABASE_URL` | 指到 `crm_db` |
| `PORT` / `INTERNAL_BIND_HOST` | 8020 / 127.0.0.1 |
| `CONNECTOR_INTERNAL_BASE` | Connector 內網位址（`http://127.0.0.1:8010`） |
| `INTERNAL_API_TOKEN` | 內網認證 token（兩邊一致） |
| `RAG_MEDIA_DIR` / `RAG_MEDIA_URL_PREFIX` | RAG 媒體目錄（`media` / `/media`） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | AI 供應商（只認這幾個欄位） |
| `DEEPSEEK_TIMEOUT` / `DEEPSEEK_AUX_TIMEOUT` / `DEEPSEEK_MAX_CONCURRENCY` | 主呼叫 / 輔助（改寫）逾時與併發 |
| `FAQ_MATCH_THRESHOLD` 等 | 自動回覆防護與匹配參數（同舊版） |
| `CRM_ADMIN_PASSWORD` | （選用）後台預設密碼 |
| `SESSION_TTL_HOURS` / `LOGIN_MAX_FAIL` / `LOGIN_LOCK_MINUTES` | 登入安全 |
| `WORKER_ENABLED` / `AI_WORKER_POLL_SECONDS` / `TIMEOUT_SWEEP_SECONDS` | 背景 worker |

> AI 提示詞相關（`DEEPSEEK_SYSTEM_PROMPT` 等）寫法同舊版：多行用雙引號、換行寫 `\n`，
> 佔位符大括號要寫成 `{{` `}}`。

### 平台憑證（業務，存 DB 而非 .env）

多租戶設計：**按商家區分**的業務憑證存 `crm_business_platform_account.credential`（JSONB），
由 admin 在後台「商家 → 平台帳號」填寫，經帳號映射同步到 Connector 的 `connector_account_map`
快取，Connector 收發時用 `account_map.credential_of()` 取用。

| 平台 | 業務憑證欄位（存 DB） | 原生 ID 取自 |
|---|---|---|
| 微信 | `appid`、`appsecret`、`encoding_aes_key` | **AppID** |
| WhatsApp | `access_token`、`phone_number_id` | **Phone Number ID** |
| Facebook | `page_id`、`page_access_token` | **Page ID** |
| Instagram | `ig_user_id`、`ig_access_token`、`page_access_token` | **IG 專業帳號 ID** |

> 欄位單一真相：CRM `config.CREDENTIAL_FIELDS` ↔ Connector `account_map.CREDENTIAL_KEYS`。

**★ 原生 ID 不用另外填**（`config.NATIVE_ID_FIELD`）：兩個服務的對齊鍵
`platform_business_account_id` 在各平台**就是上面某一個憑證欄位**，新增綁定時由後端
從憑證推導，後台帳號清單再統一以「原生 ID」一欄呈現。

**★ 憑證是必填、不是選填**（`config.CREDENTIAL_REQUIRED`）：想真的連上平台收發就必須有，
缺任何一項新增直接 400（錯誤訊息指名缺哪個欄位）。不允許先建空殼——
空殼帳號會讓 webhook 取不到憑證而靜默失敗。

---

## 九、自檢（可重複執行）

新架構的自檢在兩個子專案的 `tests/`，都是「真 DB + 真引擎」、跑完自動清理（資料掛在
`__CK__` / 哨兵前綴的獨立商家下）：

```bash
# 從專案根，用底層 python（.venv 的 python.exe 在中路徑會起不來，見下）
PYTHONPATH='<root>\.venv\Lib\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  <底層 python> crm/tests/kb_search_check.py
```

CRM 側主要自檢：

| 自檢 | 驗證 |
|---|---|
| `phase3_ai_queue_check.py` | AI 入站佇列 → 引擎 → 建任務（30 項） |
| `phase3_scope_check.py` | 兩層 AND / 基準啟用集 / 貼文覆寫集（29 項） |
| `phase3_rag_schema_check.py` | RAG 表結構完整性（51 項） |
| `engine_check.py` | 四層引擎 |
| `kb_check.py` / `kb_search_check.py` / `kb_asset_check.py` / `kb_api_check.py` / `kb_scope_check.py` | 知識庫品質校準 |
| `rag_model_check.py` / `model_check.py` | 本地 embedding / AI 模型連線 |
| `ui_check.py` / `console_render_check.py` | 前端靜態 / 渲染（Node + 最小 DOM 樁） |

Connector 側：`webhook_check.py`（收訊：驗證 / 正規化 / 落地 / notify / 憑證，53 項）、
`phase2_check.py`（端到端，真的起兩個服務）、`phase4_conversations_check.py`（會話列表）。

> ⚠️ **環境坑**：`.venv` 的 `python.exe` 在「路徑含中文」時會起不來，自檢一律用底層 python
> （見 `.venv/pyvenv.cfg` 的 `home =`）＋ `PYTHONPATH` 指到 `.venv\Lib\site-packages`。
> 詳細見 `docs/MIGRATION_PLAN.md` 與 `MEMORY.md`。

---

## 十、常見問題排查

| 現象 | 原因與處理 |
|---|---|
| 起服務報埠被佔用 | 先前的實例還活著。找出佔用 8010 / 8020 的進程結束掉再啟動。 |
| 改了程式碼但行為沒變 | 服務沒帶 `--reload`，改完後端要重啟；前端按 `Ctrl+Shift+R` 強制刷新。 |
| CRM 讀錯設定 / 起不來 | 啟動時 cwd 不對，`python-dotenv` 讀到別處的 `.env`。要 `cd` 到對應子目錄。 |
| 內網 API 401 | 兩邊 `INTERNAL_API_TOKEN` 不一致。 |
| 工作台看不到新訊息 | 收訊已搬到 Connector（8010）。檢查：① 隧道是否指向 8010；② 該平台帳號是否已在後台綁定且啟用（未註冊帳號不會入隊）；③ 兩邊 `INTERNAL_API_TOKEN` 是否一致。 |
| 自動回復都掉人工 | 看「回覆紀錄」分頁的 `reason`（`no_match` / `no_api_key` / `llm_error` / `throttled` ...）。 |
| AI 回覆空字串 | `DEEPSEEK_MODEL` 是 reasoning 模型時 `max_tokens` 太小，思考吃掉額度。調大。 |

---

## 十一、專案結構

```
Multi-CS/
├─ connector/                   # 純通道服務（8010）
│  ├─ main.py                   # 入口：內網 API + webhook + /files 靜態掛載
│  ├─ app/
│  │  ├─ internal/              # 內網 API（auth / router）
│  │  ├─ webhook.py             # 對外收訊（微信 / WhatsApp / IG / FB）→ 落地 → 通知 CRM
│  │  ├─ channels/              # 收：*_recv（wechat / whatsapp / igfb）；發：base / wechat / whatsapp
│  │  ├─ forwarder.py           # 媒體下載（微信 media_id / Meta Graph）
│  │  ├─ worker/                # outbound（claim→發送→回調）/ account_sync / callback
│  │  ├─ media.py / account_map.py
│  │  └─ core/                  # config / db
│  └─ tests/                    # webhook / phase2 / phase4 自檢
│
├─ crm/                         # 大腦服務（8020）
│  ├─ main.py                   # 入口：工作台 / 後台 / 運維 / 內網 API + 背景 worker
│  ├─ app/
│  │  ├─ auth/                  # 登入（Session/CSRF/RBAC/審計）
│  │  ├─ admin/                 # 管理員後台
│  │  ├─ agent/                 # 客服工作台（console_router / repo）
│  │  ├─ ops/                   # 運維頁
│  │  ├─ internal/              # 內網 API（claim / update-status / ai-inbound ...）
│  │  ├─ autoreply/             # 四層引擎編排（命中→建任務）
│  │  ├─ llm/                   # deepseek_client / reply_engine / faq_matcher
│  │  ├─ rag/                   # 知識庫（kb_* 模組鏈）
│  │  ├─ worker/                # ai_queue（單一序列）/ sweep / runner
│  │  ├─ core/ / utils/
│  │  └─ ui/                    # login.html / console_ui.html / admin.html / ops.html
│  └─ tests/                    # 全部品質校準 + 渲染自檢
│
├─ app/                         # ★ 舊單體（8001，仍在上線服務，勿刪）
├─ ui/                          # ★ 舊單體工作台前端
├─ scripts/                     # 維運腳本（init_crm_db / migrate_ai_data / token_tool ...）
├─ docs/                        # MIGRATION_PLAN.md / RAG_DESIGN.md / META_SETUP.md / INTERNAL_API.md
└─ tests/                       # ★ 舊單體自檢（勿刪，驗舊單體 console）
```

---

## 十二、目前進度與已知限制

**進度**（詳細見 `docs/MIGRATION_PLAN.md`）：

- ✅ 階段一（資料基礎層）／階段二（異步任務與事件）／階段四（客服工作台與運維）／
  T25（品質校準自檢移植到 CRM）全部完成。
- ✅ 階段三：AI 引擎 / 知識庫已搬，**webhook 收訊（塊A）+ 靜態掛載（塊B）已於 2026-10-13
  搬到 Connector**（客戶來訊現在由 Connector 收）。
- ✅ 平台憑證進 DB（多租戶）：業務憑證存 `crm_business_platform_account.credential`，
  admin 後台填；握手 token 留 `.env`。
- ⚠️ **唯一未完成項：知識庫管理 console（塊C）尚未遷進 CRM** —— 舊單體的「知識庫 / FAQ /
  業務表格內容管理」那一套 UI + API 還在 8001；CRM 目前只有客服工作台（會話 / 自動回覆配置 /
  命中測試 / 回覆紀錄），缺內容管理頁。**這塊做完才能安全刪掉舊單體。**
- 其餘「未接線」為刻意保留：舊 `autoreply_console.py` / `kb_console.py`（改走新的
  `console_router.py`）、`scripts/backfill.py` 舊簽名。

**已知限制**：同舊版（微信需認證公眾號才能回、IG 留言需權限、只處理文字訊息、AI 可能出錯等）。
