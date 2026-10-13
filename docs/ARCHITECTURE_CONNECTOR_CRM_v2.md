# Connector-CRM 分離架構完整設計方案（最終版 v2）

> **歸檔說明**：本檔為小jo 提供之原始設計案原文，2026-10-13 歸檔於此，作為本次改造的權威依據。
> 落地計劃另見 `docs/MIGRATION_PLAN.md`。

## 文檔元資訊

- **基於原始系統**：多渠道 AI 客服系統（feature/rag 分支，16 張表、四層決策引擎、RAG 知識庫）
- **架構模式**：Connector 服務（純通道 + 媒體存儲）+ CRM 服務（大腦 + 資料 + 工作台）
- **業務背景**：客服外包服務公司，處理多個外包商家客戶的多渠道消息回覆
- **文檔狀態**：本版為經多輪評審後的最終設計，所有議題已定案或標註「建議值，可調」
- **文檔版本**：2026-10-13 v2

> **給 AI 使用者的提示**：本文件描述的是**最終設計**，不是現況。凡標註「⚠️ 已知風險」「第一版不做」「建議值，可調」者，都是刻意的取捨。
>
> **核心架構約束（最容易被忽略的四條）**：
> 1. **AI 引擎與知識庫全部在 CRM 側**；Connector 只做「收訊息、存訊息、發訊息、存媒體」。
> 2. **各平台會話完全隔離**，不做任何跨渠道身份聚合。
> 3. **跨庫禁止寫入**；fdw 僅做底層只讀，業務層一律走內網 API。
> 4. **UI 保持簡潔**：非必要的說明一律收進 `infotip()` 的「i」圓標，不在界面直接顯示。

---

## 目錄

| # | 章節 |
|---|---|
| 0 | [TL;DR](#0-tldr) |
| 1 | [業務背景與核心約束](#1-業務背景與核心約束) |
| 2 | [術語定義](#2-術語定義) |
| 3 | [服務邊界定義](#3-服務邊界定義) |
| 4 | [系統架構總覽](#4-系統架構總覽) |
| 5 | [CRM DB 完整建表 SQL](#5-crm-db-完整建表-sql) |
| 6 | [Connector DB 調整](#6-connector-db-調整) |
| 7 | [跨庫與內網 API](#7-跨庫與內網-api) |
| 8 | [權限模型](#8-權限模型) |
| 9 | [開關層級與知識庫綁定](#9-開關層級與知識庫綁定) |
| 10 | [核心業務流程](#10-核心業務流程) |
| 11 | [任務狀態機與冪等](#11-任務狀態機與冪等) |
| 12 | [媒體架構](#12-媒體架構) |
| 13 | [鑑權與登錄](#13-鑑權與登錄) |
| 14 | [前端設計原則](#14-前端設計原則) |
| 15 | [運維與告警](#15-運維與告警) |
| 16 | [已知風險與第一版不做](#16-已知風險與第一版不做) |
| 17 | [迭代落地階段計劃](#17-迭代落地階段計劃) |

---

## 0. TL;DR

**一句話**：把原系統拆成「Connector（純通道）+ CRM（大腦）」，兩個服務共用一個 PostgreSQL 實例的兩個 Database；AI 引擎、知識庫、業務表格、FAQ 全部移到 CRM；支援外包客服的多商家、多坐席、多角色權限；平台會話完全隔離，不做跨渠道聚合。

**關鍵數字**：

- 2 個服務：Connector、CRM
- 2 個 Database：`platform_forwarder`（渠道訊息）、`crm_db`（其他全部）
- 1 個 PostgreSQL 實例（第一版同機）
- 0 個 Redis 的關鍵路徑（Session 存 DB，Redis 只做通知加速器，掛了靠輪詢兜底）
- 3 種角色：admin / agent / ops
- 4 層決策鏈不變：FAQ → 表格 → RAG → 降級人工

---

## 1. 業務背景與核心約束

### 1.1 業務背景

我方為**客服外包服務公司**，系統用於處理**多個外包商家客戶**的多渠道消息回覆。

- 全部員工賬號（admin / agent / ops）屬於**外包公司全局賬號池**，商家自身**沒有**登錄賬號。
- 一個商家可綁定多個第三方平台業務賬號（微信公眾號、WhatsApp、IG、FB）。
- 一個商家在同一個平台**可能有多個業務賬號**（例如兩個 WhatsApp 號碼、兩個 IG 帳號）。
- Agent 坐席按**商家**分配訪問權限；超級坐席可看全部數據。

### 1.2 核心約束（這幾條決定了所有架構選擇）

| 約束 | 體現 |
|---|---|
| **各平台會話完全隔離** | 不做任何跨渠道身份聚合。`business_id + platform + business_platform_account_id + end_user_sender_id` 即為一個獨立會話單元 |
| **平台之間紅點互不干涉** | 任意坐席回覆，僅清除該平台該會話的未處理紅點，其他平台不受影響 |
| **AI 引擎與知識庫全部在 CRM** | Connector 降級為純通道 + 媒體存儲 |
| **登入鑑權** | Session-ID + CRM DB 儲存，HttpOnly Cookie，支援即時踢下線；開啟 CSRF 防護 |
| **軟刪除** | 全部業務表軟刪除 `enabled=false`，不物理刪除；審計日誌只插入，不更新刪除 |
| **UI 簡潔優先** | 設計風格沿用舊項目；非必要備註收進 `infotip()` 的「i」圓標 |
| **跨庫禁止寫入** | fdw 僅做底層只讀封裝；業務層一律走內網 API |
| **第一版同機** | 同一個 PostgreSQL 實例，兩個 Database；拆分不同主機為未來選項 |

---

## 2. 術語定義

| 術語 | 說明 |
|---|---|
| **商家（business）** | 我方外包業務客戶，一個商家可綁定多個第三方平台業務賬號 |
| **商家平台賬號（business_platform_account）** | 某商家在 IG/FB/WhatsApp/微信的某個具體業務賬號 |
| **end_user_sender_id** | 終端消費者在對應平台的用戶 ID；**不同平台 ID 互相獨立，不做聚合** |
| **會話單元** | `business_id + platform + business_platform_account_id + end_user_sender_id`，最小業務會話單元，平台之間完全隔離 |
| **staff 系統員工** | 外包公司內部人員，全局賬號池；支援單賬號多角色 |
| **admin** | 全局管理員，管「人與商家」，**不管知識庫** |
| **agent** | 外包客服坐席；可分配可訪問的商家 |
| **ops** | 運維人員；預設僅可查看系統運維數據，無會話訪問權限；可複合 agent 角色 |
| **is_super_agent** | agent 的旗標，跳過商家白名單，可訪問全部商家 |
| **is_kb_editor** | agent 的旗標，可建立/刪除知識庫 collection、編輯 FAQ/表格/知識庫內容 |
| **基準啟用集** | 商家層級的「知識庫/表格/FAQ 啟用清單」，私訊與未配置的貼文都用這組 |
| **貼文覆寫集** | 貼文層級的特殊配置；只有做過配置的貼文才有 |
| **發送快取** | 知識庫圖片在 Connector 的臨時副本，7 天清理 |

---

## 3. 服務邊界定義

### 3.1 Connector 服務（對外公網暴露 webhook；內網接口禁止公網訪問）

> **真相源**：渠道原始消息、平台 access_token、所有平台出站發送邏輯、媒體檔案實體存儲

1. 接收微信、WhatsApp、IG、FB webhook；**收到 webhook 立刻返回 HTTP 200**，耗時邏輯丟背景任務。
2. 訊息正規化，落地寫入 Connector DB；每一條消息攜帶標籤：`business_id`、`business_platform_account_id`、`platform`、`end_user_sender_id`。
3. 落地成功後，**非同步**呼叫 CRM 內網 API `POST /internal/api/ai/inbound`，通知 CRM 跑 AI（不等待返回）。
4. 提供僅內網可訪問 API：
   - 媒體上傳接收接口（CRM 推送客服上傳附件、知識庫圖片）
   - 媒體下載代理接口（CRM 代理獲取客戶來訊媒體）
   - 跨庫查詢封裝接口（替代業務層直接 fdw join）
   - 發送嘗試紀錄查詢接口（給 CRM 任務詳情 / ops 查看）
5. 消費回覆任務：
   - 訂閱 Redis Stream 事件通知（加速器）
   - 定時輪詢 CRM 內網 `claim` API 拉取**已被 CRM 原子認領**的任務（兜底）
6. 執行向第三方平台發送消息；每發一條寫入 `connector_outbound_attempt`；完成後呼叫 CRM 回調接口更新任務狀態。
7. 未註冊的平台業務賬號：訊息**不寫入 DB，直接丟棄**，進 CRM 運維頁告警清單。
8. 媒體檔案實體存儲：客戶來訊媒體（永久）、知識庫圖片發送快取（7 天清理）。
9. 系統異常累計達到閾值，觸發郵件告警。

> **約束**：Connector **禁止**直接修改 CRM DB 任何業務表；所有寫入操作走 CRM 提供的 HTTP 回調接口；fdw 僅做底層只讀封裝，業務代碼不直接使用 fdw。

### 3.2 CRM 服務（員工工作台）

> **真相源**：員工賬號、RBAC 權限、商家資訊、平台賬號綁定、**AI 四層決策引擎、RAG 知識庫、業務表格、FAQ**、回覆任務、審計日誌、運維參數配置

1. 登錄頁、RBAC 權限管理、管理員後台、客服工作台、運維監控頁。
2. admin 維護商家資訊、商家平台賬號、員工賬號與角色、權限分配、**AI 開關**。
3. agent 維護知識庫內容（需 `is_kb_editor`）、配置啟用集（有商家權限即可）、客服回覆。
4. 客服回覆流程：後端權限校驗 → 寫入 `crm_outbound_task(pending)` + `crm_audit_log`（同一事務）→ 推送 Redis Stream 事件通知 Connector。
5. 提供內網接口給 Connector：任務認領、任務狀態回調、AI 入站佇列接收、運維配置讀取。
6. 媒體流程：
   - 客戶來訊媒體：CRM 做權限校驗後，內網代理向 Connector 取檔返回瀏覽器。
   - 客服上傳附件 / 知識庫圖片：CRM 透過內網 API 推送二進制至 Connector；CRM 只存 `media_key`。
7. 會話數據渲染：透過封裝跨庫接口拉取 Connector DB 原始消息；**CRM 不修改 `forward_log` 等原始渠道表**。
8. **AI 引擎駐留 CRM**：消費 `crm_ai_inbound_queue`，執行四層決策 + RAG，命中則建立 `crm_outbound_task(source='ai')`。
9. 權限強制後端校驗，不能僅依賴前端隱藏 UI。
10. ops 角色：可調整重試參數、手動重跑失敗任務；本身看不到業務會話；賬號同時攜帶 agent 角色才擁有坐席權限；禁止發送消息、禁止修改員工與權限配置。
11. 告警：運維頁展示告警清單，達到異常閾值觸發郵件通知。

---

## 4. 系統架構總覽

### 4.1 分層圖

```
┌──────────────────────────────────────────────────────────────────┐
│  外部平台（微信 / WhatsApp / IG / FB）                            │
└───────────┬──────────────────────────────────┬───────────────────┘
            │ webhook                          │ API 發送
            ▼                                  ▲
┌──────────────────────────────────────────────────────────────────┐
│  Connector 服務（純通道 + 媒體存儲）                              │
│  - webhook 接收、簽章驗證、正規化、落地 Connector DB              │
│  - 非同步通知 CRM 跑 AI                                           │
│  - 消費回覆任務（claim → 發送 → 回調 CRM）                        │
│  - 媒體實體存儲（客戶來訊永久 / 知識庫發送快取 7 天）              │
│  - 憑證只存在這裡                                                 │
└───────────┬──────────────────────────────────┬───────────────────┘
            │ 寫                                │ 讀（fdw / 內網 API）
            ▼                                  │
┌──────────────────────────────┐               │
│  Connector DB                │               │
│  platform_forwarder          │               │
│  - forward_log / igfb_dm     │               │
│  - igfb_comment / igfb_post  │               │
│  - connector_outbound_attempt│               │
└──────────────────────────────┘               │
                                               │
┌──────────────────────────────────────────────┴───────────────────┐
│  CRM 服務（大腦 + 資料 + 工作台）                                 │
│  - 登錄 / 權限 / 商家 / 平台賬號綁定                              │
│  - AI 四層決策引擎 + RAG 知識庫 + 業務表格 + FAQ                  │
│  - 消費 crm_ai_inbound_queue（背景 asyncio worker）               │
│  - 任務認領（原子 UPDATE）                                        │
│  - 會話渲染、人工回覆、審計、運維                                 │
└───────────┬──────────────────────────────────────────────────────┘
            │ 寫 / 讀
            ▼
┌──────────────────────────────────────────────────────────────────┐
│  CRM DB  crm_db                                                   │
│  - crm_business / crm_business_platform_account                   │
│  - crm_staff / crm_staff_business_permission / crm_session        │
│  - crm_outbound_task / crm_ai_inbound_queue / crm_audit_log       │
│  - kb_collection / kb_document / kb_chunk / kb_asset / kb_job     │
│  - autoreply_faq / autoreply_business_table / autoreply_business_row│
│  - autoreply_log / business_ai_switch / business_*_binding        │
│  - post_ai_config / post_*_binding                                │
│  - crm_ops_config / crm_alert                                      │
└──────────────────────────────────────────────────────────────────┘

Redis（僅加速器，掛了靠輪詢兜底）
  └─ 任務建立事件通知
```

### 4.2 三條獨立的資料流

| 流 | 觸發 | 做什麼 | 失敗影響 |
|---|---|---|---|
| **① 落地流** | webhook | Connector 寫訊息 + 通知 CRM 跑 AI | 只記 log，不影響對平台回應 |
| **② AI 自動回復流** | CRM 消費 `crm_ai_inbound_queue` | 四層引擎 → 命中建任務 → Connector 發送 | 完全獨立，失敗標記待人工 |
| **③ 人工回覆流** | 客服按送出 | CRM 建任務 → Connector 發送 | 直接回錯誤給前端 |

三條流共用 `crm_outbound_task` 作為發送統一入口。

---

## 5. CRM DB 完整建表 SQL

> 全部表 `enabled` 預設 true，軟禁用設為 false。所有刪除邏輯採軟刪除。

### 5.1 商家與平台賬號

```sql
-- 商家（外包客戶）
CREATE TABLE crm_business (
    id bigserial primary key,
    business_name text not null,
    business_memo text,
    enabled boolean default true,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

-- 商家綁定的各平台業務賬號
CREATE TABLE crm_business_platform_account (
    id bigserial primary key,
    business_id bigint references crm_business(id),
    platform text not null,                       -- wechat / whatsapp / ig / fb
    platform_business_account_id text not null,   -- 平台方的業務賬號 ID
    display_name text,
    ai_auto_reply_enabled boolean default true,   -- 第二層 AI 開關
    enabled boolean default true,
    created_at timestamptz default now(),
    unique(platform, platform_business_account_id)
);

-- 商家 × 平台 × 場景 AI 開關（第一層）
CREATE TABLE business_ai_switch (
    id bigserial primary key,
    business_id bigint references crm_business(id),
    platform text not null,                       -- wechat / whatsapp / ig / fb
    scope text not null,                          -- dm / comment
    enabled boolean not null default true,
    updated_at timestamptz default now(),
    UNIQUE(business_id, platform, scope)
);
```

### 5.2 員工與權限

```sql
-- 系統員工（全局賬號池）
CREATE TABLE crm_staff (
    id bigserial primary key,
    login_account text unique not null,
    password_hash text not null,
    staff_name text not null,
    roles text[] not null,                        -- array['admin','agent','ops']
    is_super_agent boolean default false,
    is_kb_editor boolean default false,
    enabled boolean default true,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

-- 坐席可訪問哪些商家
CREATE TABLE crm_staff_business_permission (
    id bigserial primary key,
    staff_id bigint references crm_staff(id),
    business_id bigint references crm_business(id),
    created_at timestamptz default now(),
    unique(staff_id, business_id)
);

-- Session（取代 Redis 儲存）
CREATE TABLE crm_session (
    session_id text primary key,
    staff_id bigint references crm_staff(id),
    roles text[],                                 -- 快照
    is_super_agent boolean,                       -- 快照
    is_kb_editor boolean,                         -- 快照
    csrf_token text not null,
    created_at timestamptz default now(),
    expires_at timestamptz not null,
    last_seen_at timestamptz default now()
);
CREATE INDEX idx_crm_session_staff ON crm_session(staff_id);
CREATE INDEX idx_crm_session_expires ON crm_session(expires_at);
```

### 5.3 任務與佇列

```sql
-- 回覆出站任務真相源（人工與 AI 共用）
CREATE TABLE crm_outbound_task (
    id bigserial primary key,
    business_id bigint references crm_business(id),
    staff_id bigint references crm_staff(id),     -- AI 任務為 NULL
    source text not null default 'manual' check (source in ('manual','ai')),
    platform text not null,
    business_platform_account_id bigint references crm_business_platform_account(id),
    end_user_sender_id text not null,
    message_content jsonb not null,               -- {text, assets:[{media_key,caption}]}
    status text not null check (status in ('pending','processing','sent','failed','uncertain')),
    is_temp_error boolean,
    retry_count int default 0,
    max_retry int not null default 3,
    backoff_seconds int not null default 1,
    fail_reason text,
    processing_at timestamptz,
    created_at timestamptz default now(),
    next_retry_at timestamptz,
    finished_at timestamptz null
);
CREATE INDEX idx_outbound_task_status ON crm_outbound_task(status, created_at);
CREATE INDEX idx_outbound_task_business ON crm_outbound_task(business_id, status);

-- AI 入站佇列（CRM 自給自足，存訊息快照）
CREATE TABLE crm_ai_inbound_queue (
    id bigserial primary key,
    message_id text not null,                     -- 對應 Connector DB 訊息 id
    business_id bigint not null,
    platform text not null,
    business_platform_account_id bigint not null,
    end_user_sender_id text not null,
    content jsonb not null,                       -- 文字 + media_key 清單快照
    status text not null default 'pending' check (status in ('pending','processing','done','failed')),
    retry_count int default 0,
    fail_reason text,
    created_at timestamptz default now(),
    processed_at timestamptz,
    UNIQUE(message_id)
);
CREATE INDEX idx_ai_queue_status ON crm_ai_inbound_queue(status, created_at);
```

### 5.4 審計與運維

```sql
-- 審計日誌（只 insert）
CREATE TABLE crm_audit_log (
    id bigserial primary key,
    business_id bigint,
    staff_id bigint,
    staff_name text,                              -- 快照
    staff_roles text[],                           -- 快照
    operate_type text not null,
    platform text,
    business_platform_account_id bigint,
    end_user_sender_id text,
    payload jsonb,
    operate_at timestamptz default now()
);
CREATE INDEX idx_audit_business ON crm_audit_log(business_id, operate_at);
CREATE INDEX idx_audit_staff ON crm_audit_log(staff_id, operate_at);
CREATE INDEX idx_audit_type ON crm_audit_log(operate_type, operate_at);

-- 運維可配置參數
CREATE TABLE crm_ops_config (
    id bigserial primary key,
    config_key text unique not null,
    config_value jsonb not null,
    updated_at timestamptz default now()
);

-- 告警清單（未綁定賬號、大量失敗等）
CREATE TABLE crm_alert (
    id bigserial primary key,
    alert_type text not null,                     -- unregistered_account / mass_failure / ...
    severity text not null,                       -- info / warning / critical
    title text not null,
    detail jsonb,
    first_seen_at timestamptz default now(),
    last_seen_at timestamptz default now(),
    count int default 1,
    acknowledged boolean default false,
    acknowledged_by bigint,
    acknowledged_at timestamptz
);
CREATE INDEX idx_alert_open ON crm_alert(acknowledged, last_seen_at);
```

### 5.5 知識庫與 AI 資料

```sql
-- 知識庫（歸屬商家）
CREATE TABLE kb_collection (
    id bigserial primary key,
    business_id bigint not null references crm_business(id),
    name text not null,
    description text,
    kind text not null check (kind in ('doc','image')),
    image_of bigint,
    enabled boolean default true,
    sort_order int default 0,
    created_at timestamptz default now()
);
CREATE INDEX idx_kb_collection_business ON kb_collection(business_id, enabled);

-- 文件（入庫狀態機）
CREATE TABLE kb_document (
    id bigserial primary key,
    collection_id bigint references kb_collection(id),
    title text, filename text, mime text, size_bytes bigint, sha256 text,
    source_path text, doc_type text, version int default 1, superseded_by bigint,
    status text not null default 'draft'
        check (status in ('draft','parsing','chunking','embedding','ready','failed','disabled')),
    progress int default 0, chunk_count int default 0, char_count int default 0,
    parse_meta jsonb, parse_params jsonb, error text,
    enabled boolean default true,
    created_at timestamptz default now(), updated_at timestamptz default now()
);
CREATE INDEX idx_kb_document_collection ON kb_document(collection_id, status);

-- 知識塊
CREATE TABLE kb_chunk (
    id bigserial primary key,
    doc_id bigint references kb_document(id),
    ordinal int not null, text text not null, prefix text, char_count int,
    page_no int, heading_path text, sheet text, row_from int, row_to int,
    embedding real[], embedding_model text, embedding_dim int, embedding_status text,
    edited boolean default false, enabled boolean default true,
    created_at timestamptz default now()
);
CREATE INDEX idx_kb_chunk_doc ON kb_chunk(doc_id, ordinal);

-- 入庫工作佇列
CREATE TABLE kb_job (
    id bigserial primary key,
    doc_id bigint references kb_document(id),
    stage text not null, status text not null, attempts int default 0,
    last_error text, created_at timestamptz default now()
);

-- 參數覆寫（collection_id IS NULL = 全域）
CREATE TABLE kb_param (
    id bigserial primary key,
    collection_id bigint references kb_collection(id),
    key text not null, value jsonb not null,
    updated_at timestamptz default now(),
    UNIQUE NULLS NOT DISTINCT (collection_id, key)
);

-- 圖片資產
CREATE TABLE kb_asset (
    id bigserial primary key,
    collection_id bigint references kb_collection(id),
    doc_id bigint references kb_document(id),
    page_no int, rel_path text, filename text, mime text, size_bytes bigint,
    width int, height int, sha256 text,
    caption text, caption_source text check (caption_source in ('doc','manual','ai')),
    caption_status text, bbox jsonb,
    embedding real[], embedding_status text,
    enabled boolean default true, filtered_reason text,
    created_at timestamptz default now()
);

-- 圖片 ↔ 知識塊
CREATE TABLE kb_asset_chunk (
    asset_id bigint references kb_asset(id),
    chunk_id bigint references kb_chunk(id),
    PRIMARY KEY(asset_id, chunk_id)
);
```

### 5.6 FAQ、業務表格、AI 紀錄

```sql
-- FAQ（歸屬商家）
CREATE TABLE autoreply_faq (
    id bigserial primary key,
    business_id bigint not null references crm_business(id),
    question text not null, answer text not null, keywords text,
    enabled boolean default true, hit_count int default 0, sort_order int default 0,
    created_at timestamptz default now(), updated_at timestamptz default now()
);
CREATE INDEX idx_faq_business ON autoreply_faq(business_id, enabled);

-- 業務表格
CREATE TABLE autoreply_business_table (
    id bigserial primary key,
    business_id bigint not null references crm_business(id),
    name text not null, description text, columns jsonb,
    enabled boolean default true, sort_order int default 0,
    created_at timestamptz default now()
);
CREATE INDEX idx_table_business ON autoreply_business_table(business_id, enabled);

CREATE TABLE autoreply_business_row (
    id bigserial primary key,
    table_id bigint references autoreply_business_table(id),
    data jsonb not null, enabled boolean default true, sort_order int default 0
);

-- AI 自動回復紀錄（命中 + 未命中同一張表）
CREATE TABLE autoreply_log (
    id bigserial primary key,
    business_id bigint not null,
    platform text, business_platform_account_id bigint, end_user_sender_id text,
    message_id text,
    tier text not null check (tier in ('faq','table','rag','miss')),
    outbound_task_id bigint references crm_outbound_task(id),  -- 未命中為 NULL
    question text, answer text,
    score real, detail text, matched_rows jsonb,
    rag_threshold real, rag_candidates int, rag_queries jsonb,
    rewrote boolean, rewritten_query text, history_used boolean,
    send_status text,                             -- sent / not_sent / failed / pending
    elapsed_ms int, llm_calls int, llm_seconds real,
    created_at timestamptz default now()
);
CREATE INDEX idx_autoreply_log_business ON autoreply_log(business_id, created_at);
CREATE INDEX idx_autoreply_log_tier ON autoreply_log(tier, created_at);
CREATE INDEX idx_autoreply_log_task ON autoreply_log(outbound_task_id);
```

### 5.7 基準啟用集與貼文配置

```sql
-- 商家基準啟用集（三組獨立）
CREATE TABLE business_kb_binding (
    id bigserial primary key,
    business_id bigint references crm_business(id),
    kb_collection_id bigint references kb_collection(id),
    enabled boolean default true,
    UNIQUE(business_id, kb_collection_id)
);

CREATE TABLE business_table_binding (
    id bigserial primary key,
    business_id bigint references crm_business(id),
    business_table_id bigint references autoreply_business_table(id),
    enabled boolean default true,
    UNIQUE(business_id, business_table_id)
);

CREATE TABLE business_faq_binding (
    id bigserial primary key,
    business_id bigint references crm_business(id),
    faq_id bigint references autoreply_faq(id),
    enabled boolean default true,
    UNIQUE(business_id, faq_id)
);

-- 貼文層級 AI 配置
CREATE TABLE post_ai_config (
    id bigserial primary key,
    platform text not null,                       -- ig / fb
    post_id text not null,
    business_platform_account_id bigint references crm_business_platform_account(id),
    enabled boolean not null default true,        -- 該貼文 AI 總開關
    configured boolean not null default false,    -- 是否人工配置過
    kb_enabled boolean default true,              -- 三層各自的開關
    table_enabled boolean default true,
    faq_enabled boolean default true,
    updated_by bigint references crm_staff(id),
    updated_at timestamptz default now(),
    UNIQUE(platform, post_id)
);

CREATE TABLE post_kb_binding (
    post_config_id bigint references post_ai_config(id),
    kb_collection_id bigint references kb_collection(id),
    enabled boolean default true,
    PRIMARY KEY(post_config_id, kb_collection_id)
);

CREATE TABLE post_table_binding (
    post_config_id bigint references post_ai_config(id),
    business_table_id bigint references autoreply_business_table(id),
    enabled boolean default true,
    PRIMARY KEY(post_config_id, business_table_id)
);

CREATE TABLE post_faq_binding (
    post_config_id bigint references post_ai_config(id),
    faq_id bigint references autoreply_faq(id),
    enabled boolean default true,
    PRIMARY KEY(post_config_id, faq_id)
);
```

---

## 6. Connector DB 調整

> 原有 16 張表完全保留，不修改原有字段語義。**新增以下欄位與表**：

### 6.1 訊息表新增欄位

```sql
-- forward_log / igfb_dm / igfb_comment / igfb_post 各加：
ALTER TABLE forward_log
    ADD COLUMN business_id bigint NOT NULL,
    ADD COLUMN business_platform_account_id bigint NOT NULL;
-- 其他表同理
CREATE INDEX idx_forward_log_business ON forward_log(business_id, platform, created_at);
```

> ⚠️ 加 `NOT NULL` 的前提是「未註冊賬號的訊息直接丟棄」。若未來政策改變（允許 NULL），需 migration。

### 6.2 新增發送嘗試表

```sql
CREATE TABLE connector_outbound_attempt (
    id bigserial primary key,
    crm_task_id bigint not null,
    sub_seq int not null,
    platform text not null,
    target_id text not null,
    status text not null check (status in ('sent','failed')),
    platform_msg_id text,
    error text,
    attempted_at timestamptz default now(),
    UNIQUE(crm_task_id, sub_seq)
);
CREATE INDEX idx_attempt_task ON connector_outbound_attempt(crm_task_id);
```

### 6.3 新增回調重試佇列

```sql
CREATE TABLE connector_callback_queue (
    id bigserial primary key,
    crm_task_id bigint not null,
    result jsonb not null,                        -- status / fail_reason / attempts
    retry_count int default 0,
    max_retry int not null default 5,
    next_retry_at timestamptz default now(),
    status text not null default 'pending' check (status in ('pending','done','failed')),
    created_at timestamptz default now(),
    UNIQUE(crm_task_id)
);
```

---

## 7. 跨庫與內網 API

### 7.1 postgres_fdw 底層配置

1. **CRM DB** 建立 Connector DB 渠道消息只讀外部表：`forward_log`、`igfb_dm`、`igfb_comment`、`autoreply_log` 等；**僅 SELECT 權限**。
2. **Connector DB** 建立 CRM DB 只讀外部表：`crm_business`、`crm_business_platform_account`、`business_ai_switch`；**僅 SELECT 權限**（用於 AI 開關讀取）。
3. **業務層禁止直接寫 fdw JOIN SQL**；所有跨庫查詢封裝內網 API。
4. **禁止 fdw 執行 INSERT / UPDATE / DELETE**。

### 7.2 CRM 暴露給 Connector

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/internal/api/outbound-task/claim?limit=N` | **原子認領**任務；CRM 內部執行 `UPDATE ... WHERE status='pending'`，回傳已認領清單 |
| POST | `/internal/api/outbound-task/update-status` | 回調更新任務狀態 |
| POST | `/internal/api/ai/inbound` | Connector 通知 CRM 跑 AI（非同步，立即返回） |
| GET | `/internal/api/ops/config` | 讀取運維參數（重試、告警閾值） |
| GET | `/internal/api/messages/query` | 封裝跨庫查詢消息 |

### 7.3 Connector 暴露給 CRM

| 方法 | 路徑 | 用途 |
|---|---|---|
| POST | `/internal/api/media/upload` | CRM 推送附件 / 知識庫圖片二進制 |
| GET | `/internal/api/media/fetch?media_key=xxx` | CRM 代理獲取客戶來訊媒體 |
| GET | `/internal/api/outbound-attempt?crm_task_id=123` | 查詢發送嘗試紀錄（任務詳情 / ops） |
| POST | `/internal/api/outbound-attempt/batch` | 批次查詢（ops 批量查看 failed） |

> 全部接口內網 token 認證，禁止公網暴露。未來拆分不同主機僅替換底層實現，業務代碼不變。

---

## 8. 權限模型

### 8.1 角色與旗標

| 角色 / 旗標 | 含義 |
|---|---|
| `admin` | 員工 CRUD、角色、商家 CRUD、平台賬號綁定、**AI 開關**。不管知識庫。 |
| `agent` | 客服坐席：看會話、回覆、配置啟用。 |
| `ops` | 運維：重試參數、失敗任務、系統監控。不看會話、不發訊息。 |
| `is_super_agent` | agent 旗標，跳過商家白名單 |
| `is_kb_editor` | agent 旗標，可建立/刪除知識庫 collection、編輯 FAQ/表格/知識庫內容 |

### 8.2 權限對照表

| 動作 | admin | agent（有商家權限） | agent + kb_editor | ops |
|---|---|---|---|---|
| 看會話、回覆 | ✅ | ✅ | ✅ | ❌（除非兼 agent） |
| 配置啟用（勾選知識庫/表格/FAQ、貼文特殊配置） | — | ✅ | ✅ | ❌ |
| 編輯 FAQ / 表格 / 知識庫內容 | ❌ | ❌ | ✅ | ❌ |
| 建立 / 刪除知識庫 collection | ❌ | ❌ | ✅ | ❌ |
| 員工 / 商家 / 平台賬號 / AI 開關 | ✅ | ❌ | ❌ | ❌ |
| 重試參數 / 失敗任務 | ❌ | ❌ | ❌ | ✅ |
| 看審計日誌 | ✅ | ❌ | ❌ | ✅ |

### 8.3 後端校驗鐵則

- **每次查詢、回覆都做後端權限校驗**，不能依賴前端隱藏 UI。
- 校驗流程：讀 Session → 查 `crm_staff_business_permission`（或 `is_super_agent` 跳過）→ 確認目標商家在權限範圍內。
- **ops 複合 agent 角色**：若 `roles` 同時含 `ops` 與 `agent`，則同時具備運維能力與會話訪問權限。

---

## 9. 開關層級與知識庫綁定

### 9.1 AI 開關兩層 AND

```
第一層：商家 × 平台 × 場景（business_ai_switch）
  微信（dm） / WhatsApp（dm）
  IG（dm / comment） / FB（dm / comment）

第二層：平台賬號（crm_business_platform_account.ai_auto_reply_enabled）

生效 = 第一層 AND 第二層
```

**IG/FB 留言**：第一層留言開關 = 關 → 該商家**所有貼文沒有特殊配置選項**。

### 9.2 知識庫綁定三層結構

```
商家層級（基準啟用集）
  business_kb_binding / business_table_binding / business_faq_binding
  - 首次上傳知識庫/表格/FAQ 時預設啟用
  - 人工可取消勾選
  - 三組獨立，各有一個「層開關」
  - 層開關開了不能一個都不勾（UI 擋）
  - 如果最終一個都沒勾 → 不跑（不是「沒勾=全部」）

貼文層級（覆寫集）
  post_ai_config（總開關 + configured 旗標）
  - 總開關關 → 該貼文不用 AI
  - 總開關開 → 可點「自動回復資料特殊配置」按鈕
  - 彈窗預設候選 = 私訊（商家基準）完全相同
  - 一旦更改 → 完全按自己的儲存（不再逐組 fallback）
  - 每層各一個開關，開了不能空勾

私訊（dm）
  - 用商家基準集
```

### 9.3 檢索時的過濾

```
私訊：
  WHERE business_id = ?
    AND collection_id IN (商家基準啟用集)

貼文留言：
  WHERE business_id = ?
    AND collection_id IN (該貼文啟用集)
```

FAQ 與表格同理。**每個商家一份獨立記憶體索引**（天然隔離）。

### 9.4 新知識庫首次上傳的預設行為

- 上傳即進「基準啟用集」（`business_*_binding` 自動新增，`enabled=true`）。
- 貼文彈窗的「候選」預設 = 基準集。

---

## 10. 核心業務流程

### 10.1 流程 1：客戶入站消息

```
終端消費者 → 第三方平台 → Connector webhook
1. Connector 校驗簽章，立刻回 HTTP 200；耗時邏輯丟背景任務。
2. 解析訊息，識別 business_platform_account_id → business_id。
   ├─ 未註冊 → 不寫 DB，進 crm_alert（同一賬號只顯示一條）
   └─ 已註冊 → 寫入 Connector DB，帶 business_id + business_platform_account_id
3. Connector 非同步呼叫 CRM：POST /internal/api/ai/inbound（立即返回）
4. CRM 寫入 crm_ai_inbound_queue（存訊息快照，含文字 + media_key 清單）
5. CRM 背景 worker（單一序列）消費佇列：
   - 檢查 AI 開關（第一層 AND 第二層）
   - 開啟 → 執行四層決策引擎 + RAG
     - 命中 → 同一事務：建 crm_outbound_task(source='ai') + 寫 autoreply_log
     - 未命中 → 寫 autoreply_log(tier='miss', send_status='not_sent')
   - 關閉 → 只寫 log，不跑 AI
6. Connector 認領任務 → 發送 → 回調 CRM
7. 會話紅點：該會話單元存在未處理 inbound → 所有有該商家權限的 agent 顯示紅點
```

### 10.2 流程 2：客服人工回覆

```
1. Agent 在 CRM 編輯回覆，選擇目標會話 + 對應平台賬號。
2. CRM 後端（同一事務）：
   - 校驗 Session + 權限
   - INSERT crm_outbound_task(status='pending', source='manual')
   - INSERT crm_audit_log
   - 推送 Redis Stream 事件（加速器）
   - 返回前端：提交成功，輪詢 task 狀態
3. Connector 雙保險消費：
   ① Redis Stream 事件 → 呼叫 claim API
   ② 定時輪詢 claim API（兜底）
4. CRM 的 claim API 原子認領：
   UPDATE crm_outbound_task SET status='processing', processing_at=now()
   WHERE id IN (SELECT ... WHERE status='pending' LIMIT N) RETURNING *
5. Connector 拿到已認領任務：
   - 查 connector_outbound_attempt 跳過已成功的 sub_seq
   - 呼叫渠道 *_send 發送；每發一條寫 attempt
   - 全部成功 → 回調 CRM status='sent'
   - 有失敗 → 區分臨時/永久錯誤
6. Connector 呼叫 CRM 回調 /update-status
7. CRM 更新任務狀態 + 補審計日誌
8. 前端輪詢到 sent → 刷新對話；僅本會話單元清紅點
```

### 10.3 流程 3：媒體

**客戶來訊媒體**：
```
Connector 下載保存本地磁碟 → 生成 media_key
CRM 渲染會話時，前端請求 CRM 接口
  → CRM 校驗權限 → 內網代理向 Connector 拿二進制 → 返回瀏覽器
```

**知識庫圖片**：
```
AI 命中帶出 media_key → crm_outbound_task.message_content.assets 含 media_key
Connector 發送前：
  - 查本地 media/kb_cache/ 是否已有此 sha256
  - 沒有 → CRM 透過 POST /internal/api/media/upload 推送二進制
  - 存檔 + 拼公開 URL（或上傳平台拿 media_id）
發送完成
Connector 定時任務：清理超過 7 天的 kb_cache
```

**CRM 刪圖不聯動**：Connector 快取照 7 天週期清。

---

## 11. 任務狀態機與冪等

### 11.1 狀態機

```
pending ──▶ processing ──▶ sent
                │
                ├──▶ failed（永久錯誤，不重試）
                └──▶ uncertain（可能已發出，需人工確認）
```

### 11.2 關鍵機制

| 機制 | 實現 |
|---|---|
| **原子認領** | CRM 的 claim API 執行 `UPDATE ... WHERE status='pending'`，回傳 `RETURNING *` |
| **子項冪等** | `connector_outbound_attempt` 的 `UNIQUE(crm_task_id, sub_seq)` |
| **回調重試** | Connector 的 `connector_callback_queue`，指數退避，**上限 5 次 / 約 5 分鐘**（1s→5s→30s→2min→5min） |
| **逾時清理** | CRM 定時任務：`processing` 超過 5 分鐘 → 依情況標記 `uncertain` |
| **錯誤區分** | 確定沒發出（連線前失敗、平台 4xx）→ 可自動重試；可能已發出（逾時、回調失敗）→ `uncertain` |
| **人工確認** | ops 頁面查 `connector_outbound_attempt` 作為證據，一鍵「標記已發送」或「重試」 |

### 11.3 公平性

Connector claim 時，**每個商家每次最多 K 筆**（`crm_ops_config` 可配，建議 10），避免爆量商家餓死小商家。

### 11.4 逾時清理的具體規則

| 情境 | 處理 |
|---|---|
| `processing` 超時 + attempt 表無紀錄 | 標記回 `pending`，重新排隊（確定沒發出） |
| `processing` 超時 + attempt 表有部分成功 | 標記 `uncertain`（可能已發出） |
| `processing` 超時 + attempt 表全部成功但沒回調 | 標記 `sent`（其實成功了） |

---

## 12. 媒體架構

### 12.1 兩類媒體歸屬

| | 客戶來訊媒體 | 知識庫圖片 |
|---|---|---|
| 元數據 | Connector DB 訊息表 | CRM DB `kb_asset` |
| 實體檔案（永久） | Connector 伺服器 | CRM 伺服器 |
| 客服查看 | CRM 代理 → Connector | CRM 自己就有 |
| AI 使用 | 不處理（留人工） | 生成回覆時帶 `media_key` |
| 發送 | — | CRM 臨時上傳給 Connector |

### 12.2 知識庫圖片的「發送快取」

| 項目 | 決定 |
|---|---|
| 存放位置 | Connector `media/kb_cache/` |
| 去重 | `sha256` |
| 清理週期 | **7 天** |
| CRM 刪圖 | 不聯動 |
| 已知行為 | 刪除後最快 7 天內快取仍可存取（文檔記載） |

---

## 13. 鑑權與登錄

### 13.1 Session 模型

- 登錄成功 → 生成隨機 `session_id` → 存 **CRM DB `crm_session` 表** → HttpOnly Cookie。
- 會話資訊（`staff_id`、`roles`、`is_super_agent`、`is_kb_editor`）**存快照**。
- CSRF token 存在 session 中，前端請求需帶。

### 13.2 踢下線

| 情境 | 動作 |
|---|---|
| 管理員踢人 | `DELETE FROM crm_session WHERE staff_id = ?` |
| 使用者登出 | `DELETE FROM crm_session WHERE session_id = ?` |
| 員工被禁用 | 同上（管理員踢人） |

### 13.3 角色變更即時生效

admin 改某人角色時：`UPDATE crm_session SET roles=?, is_super_agent=?, is_kb_editor=? WHERE staff_id=?`。

### 13.4 Session 清理

CRM 定時任務：`DELETE FROM crm_session WHERE expires_at < now()`。

### 13.5 建議值（可調）

| 參數 | 建議值 |
|---|---|
| Session TTL | 8 小時 |
| 登入失敗鎖定 | 5 次失敗鎖 15 分鐘 |
| 密碼策略 | 最少 8 位，含字母與數字 |

---

## 14. 前端設計原則

### 14.1 核心原則（專案主人明確要求）

| 原則 | 含義 |
|---|---|
| **設計風格沿用舊項目** | 單檔 HTML、無框架、無 build step；自帶 CSS 與 JS，服務每請求重讀 |
| **不要「是/否」彈窗** | 二選一就給明確按鈕或單選，**永遠不要** `confirm(...)` |
| **頁面要少字** | 解釋性文字收進 `infotip(text)` 的「i」圓標（純 CSS tooltip）。狀態用底色/pill 表達 |
| **非必要備註用「i」圓標** | **不在界面直接顯示** |
| **彈窗寫成靜態節點** | 新增彈窗不進輪詢重繪區，避免被弄丟 |
| **`renderSettingsTab()` 唯一入口** | `renderSettingsBody()` 唯一寫 `#spBody`；內容沒變不動 DOM |
| **`mdImages()` 唯一圖片渲染器** | 每處顯示回覆/內文都走它，不能只 `esc()` |

### 14.2 三種角色的首頁

| 角色 | 首頁 |
|---|---|
| admin | 管理員後台（商家、平台賬號、員工、權限、AI 開關） |
| agent | 客服工作台（會話列表、對話、回覆、知識庫、記錄） |
| ops | 運維監控頁（失敗任務、重試參數、告警清單） |

**一個賬號多角色**：頁面合併展示對應功能（分頁合併）。

### 14.3 知識庫子頁跳轉

`arState.kbView`：`list` → `docs` → `doc` / `new`；每頁有麵包屑；跳頁前 `kbForgetDoc()` 清狀態。

⚠️ 圖片庫沒有「文件頁」（`kind==='image'`）：`kbOpenCollection()` 用 `kbIsImageLib()` 擋掉，改開 `kbOpenAssets()`。

### 14.4 貼文特殊配置彈窗

- 每條貼文管理頁有「自動回復資料特殊配置」按鈕。
- 點擊 → 彈窗出現「該商家所有表格 + FAQ + 知識庫」。
- 三組獨立勾選，每組有一個開關。
- 開關開了不能空勾。
- **預設候選 = 私訊（商家基準）完全相同的勾選狀態**。
- 一旦更改 → 完全按自己的儲存。
- 該商家留言總開關 = 關 → **不顯示特殊配置按鈕**。

---

## 15. 運維與告警

### 15.1 告警清單

CRM 運維頁展示 `crm_alert` 清單，包含：

- **未綁定平台賬號**：同一賬號只顯示一條，持續更新「首次出現、最後收到、累計丟棄數」。
- **大量任務失敗**：達到閾值觸發。
- **webhook 異常**。
- **知識庫批量入庫失敗**。
- **Redis 連線異常**（靠輪詢兜底，但告警）。

### 15.2 郵件告警

達到 `crm_ops_config` 配置閾值 → 發送郵件（收件人可配）。**不提供通用 webhook 推送**。

### 15.3 `crm_ops_config` 建議值（可調）

| Key | 建議值 | 說明 |
|---|---|---|
| `outbound.max_retry` | 3 | 任務最大重試 |
| `outbound.backoff_seconds` | 1 | 退避基數 |
| `outbound.claim_batch` | 50 | 每次 claim 上限 |
| `outbound.per_business_limit` | 10 | 公平性限流 |
| `outbound.processing_timeout` | 300 | processing 逾時（秒） |
| `callback.max_retry` | 5 | 回調重試上限 |
| `alert.mass_failure_threshold` | 20 | 大量失敗閾值（筆/小時） |
| `alert.email_recipients` | [] | 告警收件人 |
| `media.kb_cache_ttl_days` | 7 | 知識庫圖片快取天數 |

### 15.4 審計日誌可見性

| 角色 | 可見範圍 |
|---|---|
| admin | 全部 |
| ops | 運維相關操作（重試、配置變更），**不看客戶對話審計** |
| agent | ❌ |

### 15.5 審計操作類型清單

**人員 / 權限類**
`staff_create` / `staff_update` / `staff_disable` / `staff_role_change` / `business_create` / `business_update` / `business_disable` / `staff_business_permission_grant` / `staff_business_permission_revoke` / `platform_account_bind` / `platform_account_unbind` / `ai_switch_change`

**知識庫類**
`kb_collection_create` / `kb_collection_update` / `kb_collection_delete` / `kb_document_upload` / `kb_document_delete` / `kb_document_reparse` / `kb_asset_delete` / `kb_asset_caption_edit` / `faq_create` / `faq_update` / `faq_delete` / `business_table_create` / `business_table_update` / `business_table_delete` / `post_ai_config_change` / `binding_change`

**客服操作類**
`reply_send` / `message_handle` / `conversation_read`

**認證類**
`login` / `logout` / `force_logout`

### 15.6 審計日誌保留

**永久保留**（第一版不做歸檔）。

---

## 16. 已知風險與第一版不做

### 16.1 已知風險（接受）

| 風險 | 說明 | 防護 |
|---|---|---|
| **未註冊賬號訊息永久丟失** | 平台不重送，系統不暫存 | 告警提醒管理員綁定；文檔記載 |
| **知識庫圖片刪除後最快 7 天內快取仍可存取** | Connector 快取不聯動 | 文檔記載；未來可加「手動清快取」 |
| **CRM 啟動載入 640MB embedding 模型** | 啟動慢、記憶體佔用 | 載入失敗時 CRM 照常啟動，AI 停用並告警 |
| **同機部署，PG 是單點** | 若 PG 掛，全部停 | 靠備份與監控 |
| **Redis 掛了通知延遲** | 靠輪詢兜底 | 輪詢間隔建議 2 秒 |
| **跨庫 fdw 查詢效能** | 高頻查詢可能慢 | 關鍵查詢封裝 API；未來可拆主機 |
| **`min_score` 建議 0.30~0.32** | 原系統調參結論 | 用真實客服問句重跑 `kb_search_check.py` |

### 16.2 第一版不做

| 項目 | 原因 |
|---|---|
| rerank | 介面已留，待需要 |
| OCR | 模型本身能讀圖中文字 |
| pgvector | `REAL[]` + 記憶體 cosine 對萬級塊足夠 |
| CLIP 圖片向量 | 目前圖片靠 caption 檢索 |
| 多租戶 | 目前是外包公司單一租戶 |
| 物理刪除 | 全部軟刪除 |
| 審計日誌歸檔 | 永久保留 |
| 多 Connector 實例 | 單機即可 |
| 跨渠道身份聚合 | 刻意不做 |
| 人工多段回覆 | 前端拆多筆 task |
| 通用 webhook 告警推送 | 只有郵件 |

### 16.3 第二版可選

- 拆分兩個服務到不同主機（底層替換 fdw 為 HTTP）。
- 知識閉環：未命中 → 一鍵轉 FAQ 草稿。
- rerank / OCR / pgvector 按需啟用。
- 本地 embedding 服務（脫離 fastembed 進程內模型）。
- 多 Connector 實例。

---

## 17. 迭代落地階段計劃

### 階段一：數據基礎層

1. 部署 `crm_db` 全部表；初始化 `crm_ops_config`。
2. 配置 `postgres_fdw` 外部表（只讀）。
3. 實現 CRM 登錄、RBAC、Session 管理。
4. 管理員後台：商家維護、平台賬號維護、員工維護、權限分配、AI 開關。

### 階段二：異步任務與事件機制

1. 部署 Redis（僅通知）；實現 `crm_outbound_task` 完整流程。
2. Connector claim API + 原子認領；Connector 消費、發送、回調；重試邏輯。
3. `connector_outbound_attempt` + `connector_callback_queue`。
4. 媒體上傳、代理下載內網接口。

### 階段三：AI 引擎與知識庫

1. AI 引擎從 Connector 搬到 CRM。
2. `crm_ai_inbound_queue` + 背景 worker（單一序列）。
3. 知識庫（kb_*）、FAQ、業務表格搬到 CRM DB。
4. 開關層級（`business_ai_switch` + 平台賬號開關）。
5. 基準啟用集 + 貼文覆寫集。
6. AI 回覆走 `crm_outbound_task` 流程。

### 階段四：客服工作台與運維

1. 客服工作台；會話列表；紅點邏輯；審計日誌查詢。
2. 貼文特殊配置彈窗。
3. ops 運維頁；重試參數配置；失敗任務清單；手動重跑。
4. 告警清單 + 郵件告警。
5. 全流程功能測試；異常場景測試（網路斷開、Redis 故障、回調失敗）。

### 階段五（未來可選）：拆分兩個服務到不同主機

底層替換 fdw 實現為 HTTP；上層業務代碼不變。

---

## 附錄：給 AI 使用者的提示

若你要把這份文件貼給對話式 AI 討論擴展策略，建議在開頭補一句：

> 「這是一個 Connector + CRM 分離的多渠道客服系統，AI 引擎與知識庫全部在 CRM，Connector 是純通道。請先讀完架構，重點看 §3 服務邊界、§8 權限、§9 開關層級、§10 業務流程、§11 任務狀態機。
> 我要討論的是 **[你的方向]**。請注意 §16 的已知限制與第一版不做清單，不要建議已經實作或與架構衝突的方案。」

**最容易被忽略、但也最關鍵的五個約束**：

1. **AI 引擎與知識庫全部在 CRM**；Connector 只做收、存、發、媒體。
2. **各平台會話完全隔離**，會話單元是四元組 `business_id + platform + business_platform_account_id + end_user_sender_id`。
3. **開關是兩層 AND**（商家×平台×場景 + 平台賬號）。
4. **任務認領由 CRM 執行**；Connector 被動拉取。
5. **UI 保持簡潔**：非必要備註一律收進 `infotip()` 的「i」圓標，不在界面直接顯示。
