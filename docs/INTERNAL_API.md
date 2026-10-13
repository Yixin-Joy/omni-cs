# INTERNAL_API.md — Connector ⇄ CRM 內網 API 契約

> **狀態**：階段二已實作並端到端驗證（`connector/tests/phase2_check.py`，44 項全過）。
> **依據**：`docs/ARCHITECTURE_CONNECTOR_CRM_v2.md` §7、`docs/MIGRATION_PLAN.md` §4。
> **最後更新**：2026-10-13

---

## 0. 通則

| 項目 | 規定 |
|---|---|
| 認證 | 每個請求帶 `X-Internal-Token: <INTERNAL_API_TOKEN>`；不符回 **401**。**沒設定 token 時全部拒絕**（fail closed） |
| 網路 | 兩服務各監聽不同 port（connector 8010 / crm 8020）；`/internal/api/*` **只綁 127.0.0.1 或容器內網**，禁止公網 |
| 錯誤格式 | `{"ok": false, "error": "<訊息>"}`，HTTP status 表達語義（400 / 401 / 404 / 409） |
| 成功格式 | `{"ok": true, ...}` |
| 跨庫 | **禁止**業務層直接 fdw JOIN；所有跨庫讀寫一律走本文件的 HTTP 介面 |

```
┌───────────────┐   Connector → CRM（claim / 回調 / 入站 / 參數 / 告警）   ┌───────────┐
│  Connector    │ ───────────────────────────────────────────────────▶ │    CRM    │
│  8010         │ ◀─────────────────────────────────────────────────── │   8020    │
└───────────────┘   CRM → Connector（映射推送 / 媒體 / 發送紀錄 / 訊息查詢）└───────────┘
```

---

## 1. CRM 提供給 Connector

### 1.1 `GET /internal/api/ping` — 探活

```json
{ "ok": true, "service": "crm" }
```

### 1.2 `GET /internal/api/ops/config` — 讀運維參數

`?` 無參數。回傳巢狀 + 扁平兩份（扁平方便程式取值，巢狀對應設計案 §4.1）。

```json
{
  "ok": true,
  "config": {
    "outbound": { "max_retry": 3, "backoff_seconds": 1, "claim_batch": 50,
                  "per_business_limit": 10, "processing_timeout": 300 },
    "callback": { "max_retry": 5 },
    "alert": { "mass_failure_threshold": 20, "email_recipients": [] },
    "media": { "kb_cache_ttl_days": 7 }
  },
  "flat": { "outbound.max_retry": 3, "...": "..." }
}
```

### 1.3 `GET /internal/api/account-map` — 拉帳號映射（同步兜底）

| 參數 | 說明 |
|---|---|
| `since` | 選填，ISO 時間；省略＝**全量**（建議用全量，見下方註） |

```json
{
  "ok": true,
  "items": [
    { "platform": "wechat", "platform_business_account_id": "<平台原生 ID>",
      "business_id": 1, "bp_account_id": 7, "display_name": "客服一號",
      "enabled": true, "updated_at": "2026-10-13T14:33:49.358864+08:00" }
  ]
}
```

> `enabled = 帳號.enabled AND 商家.enabled`。**ai_auto_reply_enabled 不影響這裡** ——
> 那是「要不要跑 AI」，不是「要不要收訊息」。
> 建議 Connector 直接拉全量（幾十列、成本可忽略），可完全避開 `updated_at` 漏更新的風險。

### 1.4 `GET /internal/api/outbound-task/claim` — ★ 原子認領

| 參數 | 說明 |
|---|---|
| `limit` | 選填；省略＝取 `outbound.claim_batch`（50） |
| `per_business_limit` | 選填；省略＝取 `outbound.per_business_limit`（10），**公平限流：每個商家每次最多 K 筆** |

```json
{
  "ok": true, "count": 2,
  "items": [
    { "task_id": 28, "business_id": 1, "platform": "wechat",
      "platform_business_account_id": "<平台原生 ID>", "bp_account_id": 14,
      "end_user_sender_id": "oXXXX", "message_content": {"text": "你好", "assets": []},
      "retry_count": 0, "max_retry": 3, "backoff_seconds": 1, "source": "manual" }
  ]
}
```

**語義**：呼叫即代表「認領」。CRM 在同一事務內把這些任務由 `pending` 改為 `processing`。

核心 SQL（`crm/app/internal/repo.py::_CLAIM_SQL`）：

```sql
WITH locked AS (          -- FOR UPDATE 不可與視窗函式同層 → 先鎖候選列
    SELECT t.id, t.business_id, t.created_at
      FROM crm_outbound_task t
     WHERE t.status = 'pending'
       AND (t.next_retry_at IS NULL OR t.next_retry_at <= now())
     ORDER BY t.created_at, t.id LIMIT :scan
     FOR UPDATE SKIP LOCKED
), picked AS (            -- 再按商家分組取前 K 筆（公平性）
    SELECT id FROM (SELECT id, row_number() OVER (
        PARTITION BY business_id ORDER BY created_at, id) AS rn FROM locked) s
    WHERE s.rn <= :per_biz LIMIT :limit
)
UPDATE crm_outbound_task t SET status='processing', processing_at=now()
  FROM picked p WHERE t.id = p.id
RETURNING t.id, ...;
```

- **不重複派發**：`FOR UPDATE SKIP LOCKED` + 同事務 UPDATE（已實測 4 併發無重派）。
- **不餓死小商家**：`row_number() OVER (PARTITION BY business_id)`。
- Connector **不需要** Redis／通知；每 `POLL_SECONDS`（1 秒）輪詢一次即可。

### 1.5 `POST /internal/api/outbound-task/update-status` — 回調任務狀態

```json
{ "task_id": 28, "status": "sent", "fail_reason": null, "is_temp_error": null }
```

| `status` | CRM 行為 |
|---|---|
| `sent` | 終態，`finished_at=now()` |
| `uncertain` | 終態（**可能已發出**，等人工確認） |
| `failed` + `is_temp_error=true` 且 `retry_count < max_retry` | **退回 `pending`**，`retry_count+1`，`next_retry_at = now() + backoff × 2^(n-1)` |
| `failed` 其他 | 終態 |

回應：`{"ok": true, "task_id": 28, "status": "sent", "retried": false}`

> 404＝任務不存在；400＝`status` 不合法。

### 1.6 `POST /internal/api/ai/inbound` — 通知跑 AI（非同步）

```json
{ "message_id": "wx_abc123", "platform": "wechat",
  "platform_business_account_id": "<平台原生 ID>", "end_user_sender_id": "oXXXX",
  "scope": "dm", "content": { "text": "有現貨嗎", "assets": [] }, "post_id": null }
```

回應（**立即**，不跑 AI）：

```json
{ "ok": true, "queued": true, "id": 3, "business_id": 10, "bp_account_id": 14,
  "duplicate": false }
```

| 情境 | 回應 |
|---|---|
| 已註冊帳號，首次 | `queued: true` |
| 同一 `message_id` 重送 | `duplicate: true`（冪等，不重複入隊） |
| **未註冊帳號** | `{"ok": true, "queued": false, "reason": "unregistered_account"}` → Connector 據此上報告警 |

> `content` 是**訊息快照**（含 `scope`、`post_id`），CRM 之後就算聯絡不上 Connector 也能跑 AI。

### 1.7 `POST /internal/api/alert` — Connector 上報告警

```json
{ "alert_type": "unregistered_account", "severity": "warning",
  "title": "收到未綁定帳號的訊息", "detail": { "platform": "wechat", "native_id": "..." },
  "dedupe_key": "wechat:xxxx", "increment": 1 }
```

回應：`{"ok": true, "alert_id": 12}`

> **同一 `(alert_type, dedupe_key)` 且未確認**的告警會合併成一條：累加 `count`、更新 `last_seen_at`。
> 這正是設計案要的「同一帳號只顯示一條，持續更新首次/最後/累計」。

---

## 2. Connector 提供給 CRM

### 2.1 `GET /internal/api/ping` — 探活

### 2.2 `POST /internal/api/account-map/sync` — 收 CRM 推送的映射（冪等 upsert）

```json
{ "items": [ { "platform": "wechat", "platform_business_account_id": "<原生 ID>",
               "business_id": 1, "bp_account_id": 7, "enabled": true } ] }
```

回應：`{"ok": true, "upserted": 1}`

> 這是**加速器**（綁定/解綁後即時生效）；**保證**來自 Connector 每 60s 的全量拉取。

### 2.3 `GET /internal/api/account-map` — 本地快取現況（除錯用）

```json
{ "ok": true, "last_sync_at": "2026-10-13T14:33:49+08:00", "items": [ ... ] }
```

### 2.4 `POST /internal/api/media/upload` — CRM 推二進制

`multipart/form-data`：`file` + query `scope`（`kb_cache` | `outbound`）

```json
{ "ok": true, "media_key": "kb_cache/3e26804ab4f6d028.png",
  "sha256": "3e26804ab4f6d028...", "size": 138 }
```

- **sha256 去重**：相同內容重複上傳回同一 `media_key`。
- `kb_cache` 由 Connector 定期清理（預設 7 天，`KB_CACHE_TTL_DAYS`）。

### 2.5 `GET /internal/api/media/fetch` — 取媒體

`?media_key=kb_cache/xxxx.png` → 二進制流。
**路徑穿越防護**：含 `..`、絕對路徑、非法 scope → 400；不存在 → 404。

### 2.6 `GET /internal/api/outbound-attempt` — 查發送嘗試

`?crm_task_id=28`

```json
{ "ok": true, "items": [
  { "sub_seq": 0, "status": "sent", "platform": "wechat", "target_id": "oXXXX",
    "platform_msg_id": "mock-t-123", "error": null,
    "attempted_at": "2026-10-13T14:33:50+08:00" } ] }
```

> **CRM 的逾時清理靠這支判斷「到底發出去了沒」**（見 §3）。

### 2.7 `POST /internal/api/outbound-attempt/batch` — 批次查（ops）

`{"task_ids": [1,2,3]}` → `{"ok": true, "items": {"1": [...], "2": [...], ...}}`

### 2.8 `GET /internal/api/messages/query` — 封裝跨庫訊息查詢

| 參數 | 說明 |
|---|---|
| `platform` | `wechat` / `whatsapp`（查 `forward_log`）；`ig` / `fb`（查 `igfb_dm`） |
| `platform_business_account_id` | 平台原生 ID |
| `end_user_sender_id` | 終端用戶 ID |
| `scope` | `dm` / `comment`（目前 dm 走 `forward_log` / `igfb_dm`） |
| `before` | 選填，ISO 時間（向前翻頁） |
| `limit` | 預設 50，上限 200 |

### 2.9 `POST /internal/api/messages/mark` — 把 AI 處置寫回來訊那筆訊息

自動回復跑完後，CRM 要把結果**回填到「客戶那則來訊」上**（工作台據此顯示「已自動回覆」／
「已轉人工」紅點）。訊息表歸 Connector 管，所以這一步也得走內網 API。

```json
{
  "platform": "whatsapp",
  "message_id": "wamid.xxx",
  "state": "answered",          // 自動回復狀態
  "tier": "faq",                // faq | table | rag | miss | claim | error
  "detail": "FAQ #33「怎麼退貨？」分數 1.00",
  "handled": true
}
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `platform` | ✔ | `wechat` / `whatsapp` / `ig` / `fb`（其他 → 400） |
| `message_ids` | | 批次；或下面單筆定位 |
| `message_id` | | 單筆 |
| `comment_id` | | `ig`/`fb` 留言（走 `igfb_comment`） |
| `conversation_id` | | `wechat`/`whatsapp` 會話 |
| `state` / `tier` / `detail` / `handled` | | 只寫**有給**的欄位；四個都沒給 → `updated: 0`（不是錯誤） |

定位欄位至少要給一個，否則 400 `缺少 message_id / message_ids / comment_id / conversation_id`。

**表歸屬**：`wechat` / `whatsapp` → `forward_log`；`ig` / `fb` → `igfb_dm`
（有 `comment_id` 則 `igfb_comment`）。回 `{"ok": true, "updated": n}`。

---

## 3. 任務狀態機與逾時清理

```
pending ──claim──▶ processing ──回調 sent──▶ sent
                       │
                       ├──回調 failed(temp) 且還有額度──▶ pending（退避重排）
                       ├──回調 failed(永久)──────────▶ failed
                       └──回調 uncertain─────────────▶ uncertain
```

**CRM 逾時清理**（`crm/app/worker/sweep.py`，每 `TIMEOUT_SWEEP_SECONDS` 一輪）
—— `processing` 超過 `outbound.processing_timeout`（300s）卻沒收到回調時，依發送紀錄判斷：

| 發送紀錄 | 處理 | 理由 |
|---|---|---|
| 無任何紀錄 | → `pending` | 確定沒發出，可安全重排 |
| 部分子項 `sent` | → `uncertain` | 可能已發出，不可重試 |
| 全部子項 `sent`、且無 `failed` | → `sent` | 其實成功了，補記 |
| **取不到紀錄**（Connector 不可達） | → `uncertain` | **保守**：寧可人工確認，也不冒重複發送的風險 |

---

## 4. 發送子項與冪等

一個任務的 `message_content` 拆成子項（`sub_seq`）：

| `sub_seq` | 內容 |
|---|---|
| 0（若文字非空） | 文字 |
| 1..n | 每個 `assets[]` 附件一個 |

`connector_outbound_attempt` 的 `UNIQUE(crm_task_id, sub_seq)` 保證**子項冪等**：
worker 發送前會跳過已 `sent` 的子項，所以任務被重排（例如逾時退回）也不會重複發送。

---

## 5. 錯誤語義（決定要不要重試）

| 情境 | Connector 回報 | CRM 行為 |
|---|---|---|
| 平台 4xx（參數錯、無權限） | `failed` + `is_temp_error=false` | 終態 `failed`，不重試 |
| 連線失敗、平台 5xx / 429 | `failed` + `is_temp_error=true` | 還有額度就退避重排 |
| 送出後逾時、無法判定 | `uncertain` | 終態，等人工確認 |
| 回調 CRM 失敗 | 落 `connector_callback_queue` | 指數退避重試（1s→5s→30s→2min→5min，上限 5） |

---

## 6. 驗證方式

```bash
# 從專案根（路徑含中文，.venv 的 python.exe 起不來，用底層 python + PYTHONPATH）
PYTHONPATH='<root>\.venv\Lib\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  /c/Users/its-admin/AppData/Local/Python/pythoncore-3.14-64/python.exe \
  connector/tests/phase2_check.py
```

會**真的起兩個服務**（8110 / 8111）並跑完全部 10 組驗證後自動關閉與清理。
（原因：兩個服務的 package 都叫 `app`，同一進程不可能同時 import。）
