# MIGRATION_PLAN.md — Connector / CRM 分離改造落地計劃

> **依據**：`docs/ARCHITECTURE_CONNECTOR_CRM_v2.md`（v2 設計案，以下簡稱「設計案」，章節號沿用設計案）
> **現況基準**：feature/rag 分支、`app/` 14,100 行、`ui/console_ui.html` 5,467 行、DB `platform_forwarder` 16 張表
> **狀態**：已 review 定案（2026-10-13）。
> **產出日期**：2026-10-13

---

## 0. 決策總表（已全部定案 2026-10-13）

| # | 議題 | 決策 |
|---|---|---|
| D1 | 專案形態 | **monorepo**：`Multi-CS/connector` + `Multi-CS/crm` 兩個獨立子專案 |
| D2 | 現有資料 | **全量遷移**，歸入預設商家 `Epro_test` |
| D3 | 兩庫對齊鍵 | **平台原生 ID** `(platform, platform_business_account_id)`（見 §1） |
| D4 | 第一步交付 | 本文檔（已完成） |
| D5 | 倉庫落點 | `Multi-CS/connector` + `Multi-CS/crm` |
| D6 | Redis | **不裝** —— 用 PostgreSQL 本身當佇列（`FOR UPDATE SKIP LOCKED`），Connector 輪詢 claim。實測見 §9 |
| D7 | 預設商家名 | `Epro_test` |
| D8 | 設計案歸檔 | 已歸檔 `docs/ARCHITECTURE_CONNECTOR_CRM_v2.md` |

---

## 1. 兩庫一致性 —— 回答「原生 ID 會不會兩庫資訊脫軌對不上」

**結論：不會脫軌，前提是「對齊鍵」選對、且同步機制做足。** 關鍵在於：

### 1.1 為什麼會脫軌？—— 因為自增 PK 是「庫內」的，不是「業務」的

如果 Connector 表存 CRM 的 `crm_business_platform_account.id`（bigserial），那麼：
- 這個 id 由 CRM 產生，Connector 拿不到就寫不了資料 → Connector 每次收訊都要問 CRM（違背「純通道」定位）；
- 一旦 CRM 重建 / 換庫 / 換環境，id 會變 → 兩庫**必然對不上**。

所以：**絕對不要把 CRM 的自增 PK 當對齊鍵寫進 Connector。**

### 1.2 正確做法：用「平台原生 ID」當對齊鍵

平台原生 ID（WhatsApp `phone_number_id`、IG account id、FB page id、微信 mp id）有三個特性：
**平台給的、不可變、全域唯一** —— 這才是天然的對齊鍵。

```
對齊鍵 = (platform, platform_business_account_id)
```

- CRM 側：`crm_business_platform_account` 對 `(platform, platform_business_account_id)` 加 **UNIQUE**
  → 保證「一個平台帳號只屬於一個商家」。
- Connector 側：維護一張本地映射表 `connector_account_map`，鍵也是 `(platform, platform_business_account_id)`
  → 兩庫用**同一把業務鍵**對齊，而不是用自增 id。

### 1.3 同步機制（保證不脫軌的三道防線）

```
CRM（唯一真相）                         Connector（快取）
crm_business_platform_account   ──①推送──▶  connector_account_map
        │                                    (platform, native_id, business_id, bp_account_id, enabled, synced_at)
        └────────②60s 輪詢拉取──────────────▶
```

| 防線 | 機制 |
|---|---|
| ① **主動推送** | CRM 在 `platform_account_bind` / `unbind` / 改 `ai_auto_reply_enabled` 時，呼叫 Connector 內網 API `POST /internal/api/account-map/sync`，單筆 upsert（冪等） |
| ② **定時拉取兜底** | Connector 每 60s 呼叫 CRM `GET /internal/api/account-map?since=<ts>`，全量/增量 upsert（冪等），補推送遺漏 |
| ③ **過期告警** | Connector 記 `account_map_last_sync_at`；超過 5 分鐘未成功同步 → `crm_alert(alert_type='account_map_stale')` |

### 1.4 收訊時的判斷（不會猜、寧可丟）

```
Connector 收到 webhook → 取出 platform + platform_business_account_id
  ├─ 在 connector_account_map 命中且 enabled=true
  │    → 落地訊息表，寫 platform_business_account_id(text) + business_id(bigint 快取)
  └─ 未命中 / enabled=false
       → 不寫 DB，丟棄，進 crm_alert(unregistered_account)
```

**最壞情況**（CRM 剛綁定、Connector 還沒同步到）：訊息被判「未註冊」丟棄 → 有告警可見 →
管理員手動觸發一次全量重推 → 恢復。這正是設計案 §16.1 已接受的風險，本計劃把它**控制在可觀測、可補救**。

### 1.5 對設計案 §6.1 的修正建議 ★

設計案寫 `business_platform_account_id bigint NOT NULL`，但這個名字在 CRM 側是 **text**（平台原生 ID）、
在 Connector 側卻被寫成 **bigint**，型別打架。建議改成兩個語義清楚的欄位：

```sql
-- forward_log / igfb_dm / igfb_comment / igfb_post 各加：
ALTER TABLE forward_log ADD COLUMN platform_business_account_id text;  -- ★對齊鍵（平台原生 ID）
ALTER TABLE forward_log ADD COLUMN business_id bigint;                 -- CRM PK 快取，真相在 CRM
ALTER TABLE forward_log ADD COLUMN bp_account_id bigint;               -- crm_business_platform_account.id 快取
-- 先可 NULL → 回填（§3.3）→ 再 SET NOT NULL
```

> 若你堅持要「Connector 完全不落地 business_id」（最純的通道），也可只留 `platform_business_account_id`，
> `business_id` 由 CRM 收到 `ai/inbound` 後自己解析。我建議**先存快取**（除錯/告警/未來限流方便），
> 但明確標註「快取值，真相在 CRM」。

---

## 2. 專案結構

### 2.1 定案：同一 repo 內兩個獨立子專案（monorepo）

理由：① 知識庫 / 引擎代碼**原地搬**不用重建；② git 歷史與身分（小jo/its-admin@localhost）沿用；
③ 兩邊共用文檔與契約；④ 兩個服務**仍可獨立部署、獨立依賴、獨立啟動**。

```
Multi-CS/  (現有 repo)
├── docs/
│   ├── ARCHITECTURE_CONNECTOR_CRM_v2.md   ← v2 設計案（已歸檔，權威）
│   ├── MIGRATION_PLAN.md                  ← 本文檔
│   ├── RAG_DESIGN.md / META_SETUP.md      (既有)
│   └── INTERNAL_API.md                    ← ★ 內網 API 契約（階段二已實作完成）
│
├── connector/                             ← 新：Connector 服務（獨立 requirements / 入口 / 部署）
│   ├── requirements.txt                   (fastapi / uvicorn / httpx / psycopg2 / python-dotenv)
│   ├── .env.example
│   ├── main.py                            (FastAPI 入口：webhook + /internal/api/*)
│   ├── app/                               ← package 名沿用 `app`，搬 code 不改 import
│   │   ├── core/config.py                 (只含渠道憑證、MEDIA_DIR、DB URL_C)
│   │   ├── core/db.py                     (連 platform_forwarder)
│   │   ├── channels/                      ← 從 app/channels/ 搬（wechat/whatsapp/igfb/*_send）
│   │   ├── internal/                      (media / account-map / outbound-attempt 內網 API)
│   │   ├── worker/                        (claim 輪詢 + 發送 + 回調 + 逾時、callback_queue)
│   │   └── account_map.py                 (同步與查詢)
│   └── tests/
│
├── crm/                                   ← 新：CRM 服務（獨立 requirements / 入口 / 部署）
│   ├── requirements.txt                   (+ fastembed / onnxruntime / jieba / pdfplumber)
│   ├── .env.example
│   ├── main.py                            (FastAPI 入口：工作台 + /internal/api/*)
│   ├── app/                               ← package 名沿用 `app`
│   │   ├── core/{config,db,schema,security}.py   (連 crm_db)
│   │   ├── auth/                          (session、RBAC、CSRF)
│   │   ├── admin/                         (商家/平台帳號/員工/權限/AI開關)
│   │   ├── desk/                          (會話列表、對話、回覆、紅點、審計)
│   │   ├── ai/                            ← 從 app/autoreply/ + app/llm/ 搬（四層引擎）
│   │   ├── rag/                           ← 從 app/rag/ 搬（kb_*）
│   │   ├── ops/                           (失敗任務、重試參數、告警)
│   │   ├── worker/                        (ai_inbound_queue 消費 + 逾時清理 + session 清理)
│   │   └── internal/                      (claim / update-status / ai-inbound / ops-config / account-map)
│   ├── ui/console_ui.html                 ← 從 ui/ 搬，擴充 admin / ops 分頁
│   └── tests/
│
└── legacy/                                ← 舊 app/ ui/ main.py 搬進來（參考用，最終刪）
```

> **D1-a 替代方案**：若你要真的兩個獨立 repo，把 `connector/` 與 `crm/` 各自 `git init` 即可，
> 本文檔其餘內容不變。我建議先用 monorepo 跑起來，之後要拆隨時可拆。

---

## 3. 表歸屬與 migration

### 3.1 16 張表的歸屬

| 現表 | 去向 | 備註 |
|---|---|---|
| `forward_log` | **Connector** | 微信/WhatsApp 訊息，不動欄位語義 |
| `igfb_dm` | **Connector** | IG/FB 私訊 |
| `igfb_comment` | **Connector** | IG/FB 留言 |
| `igfb_post` | **Connector** | IG/FB 貼文 |
| `autoreply_faq` | **CRM** | 加 `business_id` |
| `autoreply_business_table` | **CRM** | 加 `business_id` |
| `autoreply_business_row` | **CRM** | 跟著 table 走 |
| `autoreply_setting` | **廢棄** | 由 `business_ai_switch` + `crm_business_platform_account.ai_auto_reply_enabled` 取代 |
| `autoreply_log` | **CRM** | 加 `business_id`；★設計案 §7.1.1 把它列進「讀 Connector 的外部表」是筆誤，它本身就在 CRM |
| `kb_collection` | **CRM** | 加 `business_id` |
| `kb_document` | **CRM** | 跟 collection |
| `kb_chunk` | **CRM** | 跟 doc |
| `kb_job` | **CRM** | 入庫佇列 |
| `kb_param` | **CRM** | 參數覆寫 |
| `kb_asset` | **CRM** | 圖片資產 |
| `kb_asset_chunk` | **CRM** | 關聯 |

**Connector 新增**：`connector_outbound_attempt`(§6.2)、`connector_callback_queue`(§6.3)、`connector_account_map`(§1.2)
**CRM 新增**：設計案 §5 全部 `crm_*` + `business_*_binding` + `post_*` 表。

### 3.2 兩庫職責

| | Connector DB `platform_forwarder` | CRM DB `crm_db` |
|---|---|---|
| 存什麼 | 原始渠道訊息、發送嘗試、回調佇列、帳號映射快取 | 員工/商家/權限/會話任務/AI 紀錄/知識庫/FAQ/表格/審計/運維 |
| 誰寫 | 只有 Connector | 只有 CRM |
| 讀對方 | 不讀（憑 mapping 快取自足） | 讀渠道訊息走內網 API（業務層禁止 fdw） |

### 3.3 全量遷移步驟（可在一次維護窗口完成）

> **技術手段**：同一個 PG 實例 → 用 `postgres_fdw` 做一次性搬遷（CRM 建 Connector 外部表 → `INSERT ... SELECT` → 搬完拆掉），
> 比 `pg_dump | psql` 可控（能在 SELECT 時補欄位值）。

```
步驟 0  備份：pg_dump 兩個庫（platform_forwarder 全量）
步驟 1  建 crm_db，跑設計案 §5 全部 DDL
步驟 2  種子資料：
        - crm_business：一筆預設商家 `Epro_test`
        - crm_business_platform_account：現有在用的平台帳號
            （微信 mp_id、WhatsApp phone_number_id、IG account id、FB page id）
            platform_business_account_id 填**平台原生 ID**
        - crm_staff：一筆 admin
步驟 3  遷 AI 資料（kb_* 7 張 + autoreply_faq/table/row/log 4 張）：
        - CRM 建外部表指向 platform_forwarder
        - INSERT INTO crm_db.<t> (..., business_id) SELECT ..., <預設商家id> FROM ext.<t>
        - 搬完 DROP FOREIGN TABLE
步驟 4  遷開關語義：
        - 讀舊 autoreply_setting，用現有 effective_scope_mode() 逐把算出「哪個渠道/場景是開的」
        - 寫入 business_ai_switch(預設商家, platform, scope, enabled)
        - crm_business_platform_account.ai_auto_reply_enabled 照舊值
步驟 5  回填 Connector 訊息表（★最容易卡住的一步）：
        - 先加可 NULL 欄位 → 回填 → 再 SET NOT NULL
        - 因現階段每個平台只有一個帳號，按 platform 對應即可：
            UPDATE forward_log SET platform_business_account_id = <該平台原生id>,
                                   business_id = <預設商家id>, bp_account_id = <bp.id>
            WHERE platform = 'wechat' / 'whatsapp';
            UPDATE igfb_dm/igfb_comment SET platform_business_account_id = <ig 或 fb 原生id>, ...;
        - 建 connector_account_map（從 crm_business_platform_account 導出）
步驟 6  校驗：行數比對、抽樣比對、FK 完整性
步驟 7  切換：停舊服務 → 起 connector + crm → 冒煙測試
```

### 3.4 歷史回覆資料

現有回覆紀錄在 `forward_log(direction='outbound')` 與 `igfb_dm(direction='out')`。
**不遷到 `crm_outbound_task`**（任務表只記新流程）—— 歷史回覆留在訊息表，工作台照舊能顯示。

---

## 4. 內網 API 契約（細化設計案 §7.2 / §7.3）

> 全部走內網，`X-Internal-Token` 認證，禁止公網。以下為 request/response 草案。

### 4.1 CRM 暴露給 Connector

| 方法 | 路徑 | 用途 | 主要參數 / 回傳 |
|---|---|---|---|
| GET | `/internal/api/account-map` | 拉帳號映射（兜底同步） | `?since=<ts>` → `[{platform, platform_business_account_id, business_id, bp_account_id, enabled, updated_at}]` |
| GET | `/internal/api/outbound-task/claim` | **原子認領** | `?limit=50` → `[{task_id, business_id, platform, platform_business_account_id, end_user_sender_id, message_content, retry_count}]`；內部 `UPDATE ... WHERE status='pending' ... RETURNING *`（含 §11.3 每商家上限） |
| POST | `/internal/api/outbound-task/update-status` | 回調更新 | `{task_id, status:'sent'\|'failed'\|'uncertain', fail_reason, is_temp_error}` |
| POST | `/internal/api/ai/inbound` | 通知跑 AI（非同步） | `{message_id, platform, platform_business_account_id, end_user_sender_id, scope:'dm'\|'comment', content:{text, assets:[media_key]}, post_id?}` → 立即 202 |
| GET | `/internal/api/ops/config` | 讀運維參數 | → `{outbound:{max_retry,backoff_seconds,claim_batch,per_business_limit,processing_timeout}, callback:{max_retry}, ...}` |

### 4.2 Connector 暴露給 CRM

| 方法 | 路徑 | 用途 | 主要參數 / 回傳 |
|---|---|---|---|
| POST | `/internal/api/account-map/sync` | 收 CRM 推送的映射（冪等 upsert） | `[{platform, platform_business_account_id, business_id, bp_account_id, enabled}]` |
| POST | `/internal/api/media/upload` | CRM 推二進制（附件 / KB 圖） | multipart：`file` + `?scope=kb_cache\|outbound` → `{media_key, sha256, size}` |
| GET | `/internal/api/media/fetch` | CRM 代理取客戶來訊媒體 | `?media_key=` → 二進制流 |
| GET | `/internal/api/outbound-attempt` | 查發送嘗試（任務詳情 / ops） | `?crm_task_id=` → `[{sub_seq, status, platform_msg_id, error, attempted_at}]` |
| POST | `/internal/api/outbound-attempt/batch` | 批次查（ops 批量看 failed） | `{task_ids:[...]}` → 依 task 分組 |
| GET | `/internal/api/messages/query` | 封裝跨庫查詢訊息 | `?platform=&platform_business_account_id=&end_user_sender_id=&before=&limit=` → 會話訊息 |
| POST | `/internal/api/alert` | Connector 上報告警 | `{alert_type, severity, title, detail}` → CRM 寫 `crm_alert` |

### 4.3 認證

- 共用一把內部 token（≥32 字元隨機），兩邊 `.env` 各配 `INTERNAL_API_TOKEN`。
- 每個請求帶 `X-Internal-Token`；不符回 401。
- **部署層**：`connector` 與 `crm` 監聽不同 port（如 8010 / 8020），`/internal/api/*` 只綁 `127.0.0.1` 或 Docker 內網，
  webhook 路由才對公網開。

---

## 5. 五階段落地（細化設計案 §17）

> 每階段結束都有「可驗收的東西」，不是做完才驗。

### 階段一：資料基礎層
**目標**：兩個庫與骨架起來，能登入、能進管理員後台。
- 建 `crm_db` + 全部 DDL；種子資料（§3.3 步驟 2）
- `connector/`、`crm/` 骨架（各自 main.py、config、db、requirements）
- CRM：登錄 / Session（DB）/ CSRF / RBAC 中介層
- CRM 管理員後台：商家、平台帳號綁定、員工與角色、商家授權、AI 開關（兩層）
- **驗收**：兩個服務各自啟動；admin 能登入、能建商家/員工、能綁平台帳號並看到映射推送到 Connector

### 階段二：異步任務與事件 —— ✅ 完成（2026-10-13）
**目標**：任務鏈路打通（先用一個「假任務」測）。
- `crm_outbound_task` + claim API（`FOR UPDATE SKIP LOCKED` 原子認領 + 公平限流）
- Connector worker：輪詢 claim → 發送 → 寫 attempt → 回調；`connector_callback_queue` 重試
- 逾時清理（§11.4 三種情境）
- 媒體內網接口（upload / fetch）
- **驗收**：手動 INSERT 一筆 pending → Connector 在 1 個輪詢週期內認領發出 → 回調 sent；
  同時手動跑兩份 worker，確認同一筆不會被重複發送（§9 已實測）

**實作落點**：
- CRM：`app/internal/{auth,repo,router,connector_client}.py`（claim / update-status / ai-inbound /
  ops-config / account-map / alert）、`app/worker/{sweep,runner}.py`（逾時清理 + session 清理）、
  `main.py` 掛載內網 router 與背景迴圈。
- Connector：`app/internal/{auth,router}.py`（account-map sync / media upload·fetch /
  outbound-attempt / messages query）、`app/account_map.py`、`app/media.py`、
  `app/channels/{base,wechat,whatsapp}.py`（可插拔 sender；`OUTBOUND_SEND_MODE=mock` 可離線）、
  `app/worker/{client,outbound,callback,account_sync,runner}.py`。
- 契約文檔：`docs/INTERNAL_API.md`（request/response + 狀態機 + 錯誤語義）。
- **自檢**：`connector/tests/phase2_check.py`（真的起兩個服務，10 組 44 項全過，跑完自動清理）。
  輔助驅動：`connector/tests/_connector_driver.py`、`crm/tests/_crm_driver.py`
  （因兩個服務的 package 都叫 `app`，同進程無法同時 import）。

### 階段三：AI 引擎與知識庫搬遷
**目標**：四層引擎在 CRM 跑，AI 回覆走任務流。
- [x] `app/autoreply` + `app/llm` + `app/rag` 搬進 CRM。★ **落點刻意不改目錄名**：實際是
      `crm/app/{rag,llm,autoreply}`（而非計劃書寫的 `crm/crm/ai`）—— 這樣 `from app.rag import …`
      這類 import **一行都不用改**，風險最低。`rag_config` / `deepseek_client` / `faq_matcher` 零改；
      `autoreply.py` 重寫（命中 → **建任務**，不再直發）；⚠️ `autoreply_console.py` 仍是舊簽名，待階段四接。
- [x] `crm_ai_inbound_queue` + 背景 worker（單一序列）：`crm/app/worker/ai_queue.py`；
      `runner.py` 拆兩條迴圈（AI 1s ／ 逾時清理 60s，後者順帶 `reap_stuck()`）。
      自檢 `crm/tests/phase3_ai_queue_check.py`（30 項）。
- [x] 知識庫 / FAQ / 表格遷移（§3.3 步驟 3）；`business_id` 上線 —— **已完成**：
      `scripts/migrate_ai_data.py`（`postgres_fdw`：`kb_*` 7 表 + `autoreply_faq` / `business_table` /
      `business_row` / `log` → `crm_db`，`business_id` 填 `Epro_test`）。三個「不做就靜默失效」的坑一併處理：
  - ★ **補 `business_*_binding`**：依 §9.2「空集 = 不跑」，只搬資料不補綁定 = 三層全部沉默。
    這裡＝把搬過來的每一項都綁上（`enabled` 沿用該項自己的值）。**舊 `autoreply_setting` 的
    `channel:scope`/`channel`/`global` 優先序鏈**則展開成 `平台 × 場景` 的完整笛卡兒積，
    寫進 `business_ai_switch` —— 逐格行為與舊系統相同（實測：`whatsapp:dm`/`ig:dm`/`fb:comment` 開，
    其餘關）。層開關寫 `business_layer_switch`。
  - ★ **收養未分組文件**（`--no-adopt-ungrouped` 可關）：舊檢索是全域的，`collection_id IS NULL`
    的文件也查得到；新模型只檢索**已綁定**的知識庫（`kb_db.load_ready_chunks` 明文「未分組不屬於
    任何商家 → 不會被撈到」），不處理就**憑空消失**。實測本次影響 4 份 ready 文件（9 個已向量化
    chunk）。處理方式＝收進一個明講是遷移產物的知識庫「未分組文件（遷移）」再綁定，
    文件搬進哪個庫完全可見、隨時可在 UI 重組。`draft` 不動（本來就檢索不到）。
  - ★ **補 `crm_business_platform_account`**（`--seed-accounts`，預設開）：第二層開關
    `platform_account_enabled()` 對查不到的帳號回 **False** → 帳號表空著，兩層 AND 會擋掉全部來訊。
    ⚠️ 這裡用的原生 ID 慣例（wechat→`APPID`、whatsapp→`PHONE_NUMBER_ID`、fb→`PAGE_ID`、
    ig→`IG_USER_ID`）**必須與 Connector webhook 正規化一致**。
  - 一併處理的欄位轉型：`kb_param.value` **TEXT → jsonb**（`'auto'` 這種非法 JSON 字面量要包成
    JSON 字串，否則 `InvalidTextRepresentation`）、`autoreply_log` 欄名全異
    （`channel`→`platform`、`reply_scope`→`scope`、`conversation_key`→`end_user_sender_id`；
    `history_used` int→bool；舊 `reason` 併進 `detail` 不丟字）、`kb_document.source_path`
    指向**別的機器**的死路徑 → 改寫成本機 `media/kb/<sha>/`。保留舊 id → 逐表 `setval` 序列。
  - 用法：`--dry-run` / `--verify`（32 項對帳）/ `--force`（清空重搬；`TRUNCATE` 需 `CASCADE`，
    因為 `post_*` 指名引用 `kb_collection`/`autoreply_faq`）/ `--copy-media`
    （`media/kb_assets` → `crm/media/kb_assets`，`RAG_MEDIA_DIR=media` 是相對 cwd）。
    自檢結果：`--verify` **32/32 全通過**；引擎實跑（`售後政策`/`門市資訊`/`保固` 三問皆命中且
    top1 0.35~0.43 > 門檻 0.25，無關問句 `below_threshold`；FAQ 層 `怎麼退貨？`→ tier=faq、0 次模型呼叫；
    商家隔離 `business_id=999` → `empty_index` 0 命中）。
- [x] 開關兩層 AND 生效；基準啟用集 + 貼文覆寫集：`autoreply.is_enabled()` 兩層 AND、
      `autoreply_db.resolve_scope_set()`、`reply_engine.answer(scope_set=…)`。
      自檢 `crm/tests/phase3_scope_check.py`（29 項）。
- [ ] **驗收**：真訊息進來 → CRM 跑引擎 → 命中建任務 → Connector 送出；`autoreply_log` 有完整留痕
      （後半段已通；前半段待 Connector webhook 接收與正規化接上）。
- [ ] 階段四前置（搬遷時發現）：`app/autoreply/autoreply_console.py` 仍 import
      `app.utils.xlsx_io` —— **CRM 沒有 `app/utils/`**。接線時要把根 `app/utils/xlsx_io.py` 一起搬過來
      （或改寫表格匯入/匯出）。目前它沒被 `main.py` 引入，服務照常啟動。

#### 階段三自檢總表（實測結果，2026-10-13）

| 檢查 | 位置 | 結果 | 覆蓋 |
|---|---|---|---|
| 認證 / RBAC / 審計 / 身分切換 | `crm/tests/auth_check.py` | **全通過** | 兩步登入（`need_role` → ticket）、`active_role`、角色切換、審計含 `role_switch` |
| PG 佇列與跨庫通知 | `crm/tests/pg_queue_check.py` | **全通過** | `FOR UPDATE SKIP LOCKED` 認領；`LISTEN/NOTIFY` **跨 database 收不到**（設計依據） |
| AI 入站佇列 → 引擎 → 建任務 | `crm/tests/phase3_ai_queue_check.py` | **30 / 30** | 入隊冪等（`claim_key` 二道鎖）、兩層 AND 生效於佇列鏈路、命中留痕、逾時回收、worker 崩潰重排、jsonb 純量防守 |
| 兩層 AND / 基準啟用集 / 貼文覆寫集 | `crm/tests/phase3_scope_check.py` | **29 / 29** | 四象限、上傳即進基準集（三層）、空集＝不跑、層開關、`post_ai_config` 三態 |
| RAG 表結構完整性（真跑每條寫入路徑） | `crm/tests/phase3_rag_schema_check.py` | **51 / 51** | 抓出 `kb_job.started_at/finished_at`、`kb_collection.updated_at`、`kb_param` 具名約束、7 條 FK 的 `ON DELETE`、`kb_param.value` jsonb 寫入 |
| AI/RAG 全量資料遷移對帳 | `scripts/migrate_ai_data.py --verify` | **32 / 32** | 行數逐表對平、0 孤兒、key 欄位轉型、開關展開、綁定覆蓋、帳號層、向量數一致、媒體檔案齊備 |
| 前端（無真瀏覽器，Node + 最小 DOM 樁） | `crm/tests/ui_check.py` | **全通過** | `confirm()` 閘門、`infotip`、子頁跳轉、登入頁身分選擇。**未覆蓋**：真渲染 / CSS / 事件 |

**尚未移植到 CRM 的自檢**（階段四收尾時一併處理）：
根 `tests/` 的品質校準類 —— `kb_search_check.py`（**召回分數分佈校準，`min_score` 就是它定的**）、
`kb_check.py`（[1b] 分塊閘門）、`kb_asset_check.py`、`engine_check.py`、`rag_model_check.py`。
這些目前還跑在根 `app/`（舊單體）上；搬家後要改成 import `app.rag` 的 CRM 版才會驗到真正上線的程式。

### 階段四：客服工作台與運維
- 工作台：會話列表（走 Connector API）、紅點、人工回覆（建任務）、審計
- 貼文特殊配置彈窗（沿用舊 UI 機制）
- ops 運維頁：失敗任務、重試參數、手動重跑、告警清單、郵件告警
- **驗收**：全流程功能 + 異常場景（網路斷、回調失敗、逾時、PG 重啟）測試

### 階段五（可選）：拆不同主機
- 底層 fdw/內網 → HTTP，業務代碼不動。

---

## 6. 前端改造要點（延續舊風格）

| 項目 | 做法 |
|---|---|
| 框架 | 沿用單檔 HTML、無 build；`crm/ui/console_ui.html` |
| 角色首頁 | admin → 管理員後台；agent → 客服工作台；ops → 運維頁。多角色＝分頁合併 |
| 分頁 | 在現有六分頁（自動回復/FAQ/表格/知識庫/命中測試/紀錄）外加 admin、ops 分頁，按角色顯示 |
| 禁止 `confirm()` | 二選一給按鈕/單選（`ui_check` 已有閘門） |
| 說明文字 | 一律收進 `infotip()`「i」圓標 |
| 彈窗 | 靜態節點，不進輪詢重繪區 |
| 圖片渲染 | 每一處顯示回覆/內文都走 `mdImages()` |
| 知識庫 | 沿用 `kbView` 多頁跳轉、`kbForgetDoc()`、麵包屑規則 |

> 現有 `ui_check.py` 的閘門要跟著搬到 `crm/tests/`，並補 admin/ops 分頁的渲染檢查。

---

## 7. 風險與回退

| 風險 | 影響 | 對策 |
|---|---|---|
| Connector 訊息表加 NOT NULL 失敗 | 遷移失敗 | 先 NULL → 回填 → 才 SET NOT NULL（§3.3 步驟 5） |
| 兩庫映射脫軌 | 訊息誤丟 | §1.3 三道防線 + 一鍵全量重推 + 過期告警 |
| 知識庫 640MB 模型拖慢 CRM 啟動 | 啟動慢 | 背景預熱（沿用現況 `_warmup_kb`）；載入失敗 AI 停用 + 告警 |
| 搬運期間雙寫/漏寫 | 資料不一致 | 遷移在維護窗口做，舊服務停寫 |
| fdw 效能 | 查詢慢 | 業務走內網 API；fdw 只用在一次性 migration |
| 純輪詢的派發延遲 | 回覆慢 0~2 秒 | 間隔 1 秒（可配）；PG 本身已是關鍵路徑，加 Redis 並不能救 PG 掛掉 |
| 前端 5.4k 行擴充失控 | 難維護 | 分頁按角色拆、沿用現有 render 契約（`renderSettingsBody` 唯一寫入點） |
| 回退 | — | 全程保留 `pg_dump` 備份；舊 `app/` 進 `legacy/`；新舊可並行一週 |

---

## 8. 決策記錄（已定案 2026-10-13）

1. **倉庫落點**：monorepo —— `Multi-CS/connector` + `Multi-CS/crm`。
2. **兩庫對齊鍵**：平台原生 ID + `connector_account_map` 同步（§1）。
3. **設計案歸檔**：`docs/ARCHITECTURE_CONNECTOR_CRM_v2.md`（已歸檔）。
4. **Redis**：**不裝**。PG 自身即可當可靠佇列，Connector 輪詢 claim（§9 已實測）。
5. **預設商家命名**：`Epro_test`。

---

## 9. Redis 的必要性 —— 結論：不需要，用 PostgreSQL 即可（D6）

**一句話**：`crm_outbound_task` 表**本身就是佇列**，`status='pending'` 就是佇列元素；
Redis 在這裡唯一的作用是「踢一腳通知」，而這一腳 PG 用輪詢就能自己完成。

### 9.1 為什麼 Redis 是多餘的

設計案把兩件事綁在 Redis 上，但只有一件是它真正在做的：

| 職責 | 誰在做 | Redis 是否必要 |
|---|---|---|
| **任務持久化 / 不丟失** | `crm_outbound_task` 表 | ❌ 表已承擔，Redis Stream 是**重複的** |
| **不重複派發（併發安全）** | claim API 的 `UPDATE ... FOR UPDATE SKIP LOCKED` | ❌ 靠 DB 行鎖，與 Redis 無關 |
| **新任務的低延遲通知** | Redis Stream 事件 | ⚠️ 只影響「延遲」，不影響「正確性」 |

也就是說：**Redis 掛了只會慢 1~2 秒，不會丟任務、不會重複發**。既然如此，多加一個常駐服務
（要裝、要開機自啟、要監控、要處理重連）換來的只是那 1~2 秒 —— 不划算。
設計案 §0 自己就寫了「**0 個 Redis 的關鍵路徑**」，本節把它貫徹到底。

### 9.2 那 PG 原生的 LISTEN/NOTIFY 呢？—— 有個硬限制

直覺會想「PG 有內建 pub/sub，拿它取代 Redis 就好」。**但這套架構下它用不了**：
`LISTEN`/`NOTIFY` 的頻道是 **database 範圍**的，**同一 PG 實例的不同 database 之間不互通**。
而這套架構恰好是兩個 database（`crm_db` / `platform_forwarder`）、兩條獨立連線。

**實測（2026-10-13，PG 18.6）**：

| 情境 | 結果 |
|---|---|
| 同庫 `crm_db → crm_db` | ✅ 收到 `['hello']` |
| 跨庫 `crm_db → platform_forwarder` | ❌ 收到 `[]`（等 2 秒無反應） |

> 硬要用的話，只能讓 Connector 多開一條連到 `crm_db` 的「純 LISTEN」連線 —— 但那等於
> 破例讓 Connector 直連 CRM 庫，違背設計案「業務層一律走內網 API」的原則，不划算。

### 9.3 最終方案：Connector 輪詢 claim（雙保險收斂成單保險）

```
CRM 建任務（同一事務寫 crm_outbound_task + audit）
        │
        ▼
Connector worker 每 N 秒 GET /internal/api/outbound-task/claim?limit=50
        │
        ▼
CRM 端執行原子認領：
   UPDATE crm_outbound_task SET status='processing', processing_at=now()
   WHERE id IN (SELECT id FROM crm_outbound_task
                WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at <= now())
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT N)
   RETURNING *
```

- **間隔**：`connector.poll_seconds`，建議 **1 秒**（可配）。人工回覆體感延遲 ≈ 0~1 秒，看不出來。
- **公平性**：按商家分組限流（每商家每次最多 K 筆）在 claim SQL 內做（§11.3）。
- **併發安全**：`FOR UPDATE SKIP LOCKED` —— 多開幾份 worker 也不會重複派發（**已實測，見下**）。
- **Redis 的位置**：保留 `REDIS_URL` 設定項但**留空**。未來真需要亞秒級派發再加「通知加速器」，
  內網 API 契約不變、輪詢永遠保留當兜底。

### 9.4 實測證據（`crm/tests/pg_queue_check.py`，可重跑）

```
[A] 併發原子認領（FOR UPDATE SKIP LOCKED）
  送出 40 筆，4 個 worker 併發認領，耗時 0.07s
  PASS  全部任務都被認領（40/40）
  PASS  沒有任何一筆被重複認領（無重派）
  PASS  佇列已清空（剩 0 筆 pending）

[B] LISTEN/NOTIFY 跨 database
  同庫 crm_db → crm_db：收到 ['hello']          PASS
  跨庫 platform_forwarder ← crm_db：收到 []      PASS
```

跑法（從專案根；專案路徑含中文，`.venv` python.exe 起不來，用底層 python）：
```bash
PYTHONPATH='<root>\.venv\Lib\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  /c/Users/its-admin/AppData/Local/Python/pythoncore-3.14-64/python.exe crm/tests/pg_queue_check.py
```

### 9.5 什麼情況才回頭加 Redis

- 派發延遲要壓到 **< 100ms**（客服場景 1 秒輪詢完全夠）。
- Connector 擴到**多實例**、想減少空輪詢的 DB 壓力（屆時改用「長輪詢」API 也行，一樣不必上 Redis）。

---

## 附：現有 API → 新歸屬對照

| 現端點 | 新歸屬 | 說明 |
|---|---|---|
| `GET/POST /wechat`、`GET/POST /webhook`、`/whatsapp/webhook` | **Connector** | webhook 接收 |
| `/files/*` | **Connector** | 客戶來訊媒體（永久） |
| `/media/*` | **CRM** | 知識庫圖片公開目錄（KB 在 CRM） |
| `/console/conversations`、`/conversation/*/messages`、`/conversation/read` | **CRM** | 會話渲染，內部走 Connector `messages/query` |
| `/console/reply` | **CRM** | 建 `crm_outbound_task` |
| `/console/igfb/dm/*`、`/posts`、`/post/*`、`/comment/*` | **CRM** | 讀取走 Connector；回覆建任務 |
| `/console/igfb/refresh-ig-token`、`/sync` | **Connector** | 憑證與拉取（憑證只在 Connector） |
| `/console/autoreply/*`（settings/faq/table/test/logs/stats/draft/pending/backfill） | **CRM** | AI 引擎與資料 |
| `/console/kb/*`（collections/documents/chunks/assets/search/params） | **CRM** | 知識庫 |
| （新增）`/console/admin/*` | **CRM** | 商家/員工/權限/AI 開關 |
| （新增）`/console/ops/*` | **CRM** | 運維 |
| （新增）`/internal/api/*` 兩組 | **各自** | §4 |
