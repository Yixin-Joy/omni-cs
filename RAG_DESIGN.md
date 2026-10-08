# 第三層：RAG 知識庫匹配 — 設計與框架（v2）

> 狀態：**設計稿，尚未實作**。
> 決策鏈：`FAQ 規則` → `業務表格` → **`RAG 知識庫`** → `降級人工`
> v2 更新：併入第二輪四條需求（參數可調／記錄溯源／FAQ 檢索／命中測試子頁籤／知識庫配置向導），
> 並以實測數據確定模型選型。
> v2.1（2026-10-08）：UI 定案 —— 設定頁子頁籤「知識庫」排在業務表格之後；**參數配置移出上傳向導**，
> 改成知識庫概覽頁的「🔧 參數配置」按鈕（檢索參數按知識庫存 `kb_param`、改完立即生效）。

---

## 0. 本輪結論

| 你的需求 | 落地方式 | 關鍵點 |
|---|---|---|
| **1. 塊長／top-k／門檻可調** | `.env` 當預設值，**UI 調的值按知識庫存 `kb_param`**，執行時 DB 優先 | 不能只靠 `.env` —— 它只在服務啟動時讀一次，改完要重啟 |
| **1. 記錄可點開看命中情況** | 候選清單寫 `autoreply_log.matched_rows`；新增詳情端點 | **列表輕、詳情重**：列表查詢不加這個欄位，避免肥大 |
| **2. FAQ 頁可依內文檢索定位** | `GET /console/autoreply/faq?q=` 對 question/answer/keywords 做 ILIKE | ⚠️ 必須參數化，不能寫字面 `LIKE '%x%'`（見 §10） |
| **3. 命中測試獨立子頁籤** | 新增 `sp-tab="test"`，與既有頁籤平行、**排在「知識庫」之後**；可勾選三層 | ⚠️ 測試**只影響該次呼叫，不改 DB 開關**；也不寫記錄 |
| **4. 知識庫配置向導** | 上傳 → **分塊預覽與微調** → embedding → 確認入庫（四步，draft 態） | **參數配置不在向導裡**；微調直接寫 `kb_chunk`，最後只改 status → 不會丟資料 |
| **4. 參數配置獨立成按鈕** | 知識庫**概覽頁**每個庫一個「🔧 參數配置」按鈕，隨時可調 | 檢索參數改完 **5 秒生效、不用重啟、不用重建**；分塊參數標「需重建」（見 §5.2） |
| **4. rerank 模型** | **第一版不裝**，但介面先留好（`NoopReranker`） | 成本預告：CPU 上 rerank 每次估 +1~3 秒（見 §7） |
| **模型選型** | **改建議 `jina-embeddings-v2-base-zh`（640MB）** | 理由見 §12，**與你原本問的 bge-small 差別不小** |
| **決策鏈層順序** | `RAG` 固定排在**業務表格之後**（FAQ → 表格 → RAG → 人工） | 已定案（2026-10-08），不做可切換開關 |
| **設定頁子頁籤順序** | 「知識庫」**排在業務表格後面**：自動回復／FAQ 問答對／業務表格／**知識庫**／命中測試／紀錄 | 已定案（2026-10-08）；「紀錄」維持在最後（見 §11.1） |

---

## 1. 現況查證（實測結果，非推測）

### 1.1 環境

| 項目 | 實測結果 | 影響 |
|---|---|---|
| 公司 LiteLLM proxy `172.17.7.90:4000` | 只有 `company-code` → 上游 `custom_openai/bonsai-2-27b`（`:8080`） | 只有 chat |
| `POST /v1/embeddings` | **400** `Unmapped LLM provider for this endpoint`；換 5 個模型名 → 全 **403** | **沒有任何可用的 embedding 端點** |
| 直連上游 `:8080` | **401 Invalid API Key** | 拿不到 |
| PostgreSQL | **18.6**，`postgres` 是 superuser，UTF8 | 可 `CREATE EXTENSION` |
| `pg_available_extensions` | 只有 `pg_trgm`、`unaccent`，**沒有 `vector`** | 不用 pgvector，改 `REAL[]` + 記憶體 cosine |
| Python | **3.14.7** | |
| cp314 wheel | `numpy 2.5.3`／`pypdf 6.19`／`python-docx 1.2`／`lxml 6.1.3`／`fastembed 0.9.0`／`onnxruntime 1.30` 全部可裝 | 不被版本卡住 |
| 硬體 | **12 緒 CPU、RAM 16GB（可用 6.3GB）、C 槽剩 356GB** | 640MB 模型完全吃得下 |
| 模型下載 | `hf-mirror.com` **實測可用**（已抓到 `config.json`） | 模型能下來 |
| 本機服務 | `8001` 主服務在跑；`11434`(Ollama)／`8002` 皆無服務 | 需自架 embedding |

### 1.2 程式現況（決定接點）

| 位置 | 現況 | 對設計的影響 |
|---|---|---|
| `reply_engine.answer()` | 三層：`TIER_FAQ` → `TIER_TABLE` → `TIER_MISS`；命中即 `return` | 新層插在 `TIER_MISS` 之前即可；`Answer.hit` / `.label` 要認得新 tier |
| `reply_engine._cache` | 5 秒 TTL 快取（FAQ／表格／開關） | **參數從 DB 讀也能套用同一套快取 → 改完 5 秒內生效、不用重啟** |
| `config._get()` | **import 時就求值** | 這是「UI 可調」的關鍵限制，見 §4 |
| `autoreply_setting` | `scope_key` → enabled 的開關表 | 參數覆寫可另開表或複用它 |
| `autoreply_log` | 已有 `matched_rows JSONB` / `score` / `tier` / `detail` 等欄位 | **RAG 溯源不用新增表** |
| `list_logs()` | 只 SELECT 固定欄位，**不含 `matched_rows`** | 詳情要另外開端點（列表保持輕） |
| `console_ui.html` | `sp-tabs` 四個頁籤：自動回復／FAQ 問答對／業務表格／紀錄 | 擴成六個；「知識庫」**插在業務表格後**、再接「命中測試」；「紀錄」仍在最後（見 §11.1） |
| 「試跑」 | 現在塞在「自動回復」頁的卡片裡（`testEngine()` → `POST /console/autoreply/test/engine`） | 搬出來變成獨立頁籤 |
| `autoreply_console._check_target` | 既有渠道/場景校驗 | 測試端點複用 |

---

## 2. 決策鏈

```
客戶來訊
  ├─ 第一層 FAQ 規則匹配 ────────── 命中 → 直接回（不呼叫模型）
  ├─ 第二層 業務表格 ────────────── 命中 → 模型依表生成
  ├─ 第三層 RAG 知識庫 ──────────── 命中 → 模型依命中文件生成   ← 新增
  └─ 第四層 降級人工
```

**層順序已定案（2026-10-08）**：`RAG` **固定排在業務表格之後**，不做成可切換 ——
少一個會被設錯的開關，行為也更好預期。

> ⚠️ 別跟另一件事搞混：這裡講的是**決策鏈**（比對的先後）。
> 工作台設定面板的**子頁籤排列**（知識庫要排在哪一格）是另一件事，見 §11.1。

---

## 3. 資料模型（新增 5 張表）

```sql
kb_collection (            -- 知識庫（分組），可整組啟停
    id BIGSERIAL PK, name TEXT, description TEXT,
    enabled BOOL DEFAULT true, sort_order INT DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now()
);

kb_document (
    id BIGSERIAL PK,
    collection_id BIGINT REFERENCES kb_collection(id) ON DELETE SET NULL,
    title TEXT, filename TEXT, mime TEXT, size_bytes BIGINT, sha256 TEXT,
    source_path TEXT,                       -- media/kb/<id>/<原始檔>
    doc_type VARCHAR(10),                   -- pdf|docx|xlsx|md|txt|csv|html
    version INT DEFAULT 1, superseded_by BIGINT NULL,
    status VARCHAR(12) NOT NULL,            -- draft|parsing|chunking|embedding|ready|failed|disabled
    progress SMALLINT DEFAULT 0,
    chunk_count INT DEFAULT 0, char_count INT DEFAULT 0,
    parse_meta JSONB,                       -- 頁數／工作表／標題階層
    parse_params JSONB,                     -- ★ 這次分塊用的參數快照（之後重跑才可重現）
    error TEXT, enabled BOOL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
);

kb_chunk (
    id BIGSERIAL PK,
    doc_id BIGINT NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    ordinal INT NOT NULL,
    text TEXT NOT NULL,                     -- 已含 prefix（客服可編輯）
    prefix TEXT,                            -- 「產品手冊 › 第3章 藍牙耳機 › 價格」
    char_count INT,
    page_no INT, heading_path TEXT, sheet TEXT, row_from INT, row_to INT,
    embedding REAL[],
    embedding_model TEXT, embedding_dim INT,
    embedding_status VARCHAR(8) DEFAULT 'pending',   -- pending|done|failed
    edited BOOL DEFAULT false,              -- ★ 客服手改過（重跑時要問要不要覆蓋）
    enabled BOOL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON kb_chunk(doc_id, ordinal);

kb_job (                       -- 入庫工作佇列（重啟可續跑）
    id BIGSERIAL PK, doc_id BIGINT REFERENCES kb_document(id) ON DELETE CASCADE,
    stage VARCHAR(12), status VARCHAR(10),      -- queued|running|done|failed
    attempts INT DEFAULT 0, last_error TEXT,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);

kb_param (                      -- 參數覆寫（★ 按知識庫存：概覽頁「參數配置」寫這裡）
    collection_id BIGINT REFERENCES kb_collection(id) ON DELETE CASCADE,
    key TEXT, value TEXT, updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (collection_id, key)
);
```

**參數覆寫**：建議另開 `kb_param` 表 —— 因為「參數配置」是**按知識庫**調的（見 §4／§5.2），
**PK 是 `(collection_id, key)`**，語義乾淨（**待你確認**，替代方案是複用 `autoreply_setting`，
但那邊的 `scope_key` 是「渠道／場景」語義，塞知識庫 id 會歪掉）。

**不新增查詢日誌表**：RAG 的命中寫進 `autoreply_log`（`tier='rag'`、`score=top1`、`matched_rows=候選清單`）。

---

## 4. 需求 1 前半：參數可調（含 UI）

### 4.0 放哪裡（已定案 2026-10-08）

**與文檔解析無關的參數，不放在上傳向導裡。** 向導只管「這份文件怎麼切」；
檢索類參數一律移到**知識庫概覽頁的「參數配置」按鈕**，隨時可調。

| 參數組 | 放在哪 | 改了之後的後果 |
|---|---|---|
| **檢索參數**（top-k／門檻／單檔上限／上下文上限／rerank） | **知識庫概覽頁 →「參數配置」彈窗** | **5 秒生效，不用重啟、不用重建** |
| **分塊參數**（塊長／重疊／分塊策略／前綴） | 向導「分塊預覽」那一步就地調（立即重切＋預覽）；概覽頁彈窗內唯讀顯示 + 標「需重建」 | 要**重新切塊 ＋ 重跑 embedding** |

> 好處：上傳時客服只需要判斷「切得好不好」，不用面對 top-k 這種他不該懂的旋鈕。
> 參數**按知識庫（`kb_collection`）隔離** —— 改 A 庫不影響 B 庫，所以存 `kb_param`（見 §3）。

### 4.1 為什麼不能只改 `.env`

`config.py` 的 `_get()` 在 **import 時就求值**，所以 `.env` 的值在服務啟動那一刻就固定了。要做到
「UI 改了立刻生效、不用重啟」，必須是：

```
執行時取值 = DB 有設定 ? DB 值 : .env 預設值
             ↑ 走 reply_engine._cache（5 秒 TTL）
```

- `.env` → 提供**預設值**（與你放提示詞的習慣一致，也讓新環境開箱可用）
- DB → 提供**覆寫值**，UI 寫這裡
- 快取 5 秒 → 改完下一則訊息就生效，**不必重啟服務**

### 4.2 可調參數與範圍（`.env` key → 預設）

**A. 檢索參數** —— 知識庫概覽頁「參數配置」按鈕（改完**立即生效**）

| UI 標籤 | `.env` key | 預設 | 允許範圍 | 說明 |
|---|---|---|---|---|
| 召回數 top-k | `RAG_TOP_K` | 6 | 1–30 | 送進模型的塊數 |
| 入選門檻 | `RAG_MIN_SCORE` | 0.55 | 0.0–0.95 | 低於 → 直接降級人工，不呼叫模型 |
| 單檔最多入選 | `RAG_MAX_PER_DOC` | 3 | 1–10 | 防單一文件洗版 |
| 上下文上限 | `RAG_MAX_CONTEXT_CHARS` | 6000 | 1000–20000 | 拼給模型的總字數 |
| rerank 開關 | `RAG_RERANK_ENABLED` | false | — | 先不裝 |
| rerank 候選數 | `RAG_RERANK_TOP_N` | 20 | 5–100 | 介面先留 |

**B. 分塊參數** —— 向導「分塊預覽」步驟（就地調、立即重切預覽）；
概覽頁彈窗內**唯讀顯示**並標「改了要重建」

| UI 標籤 | `.env` key | 預設 | 允許範圍 | 說明 |
|---|---|---|---|---|
| 分塊策略 | `RAG_CHUNK_STRATEGY` | auto | auto／段落／標題／固定窗／表格列 | 依 `doc_type` 自動挑（見 §6） |
| 目標塊長 | `RAG_CHUNK_SIZE` | 500 | 100–1200 | 分塊目標字數 |
| 塊長下限 | `RAG_CHUNK_MIN` | 200 | 50–800 | 小於此值併入前一塊 |
| 塊長上限 | `RAG_CHUNK_MAX` | 600 | 200–1200 | 超過就切 |
| 塊重疊 | `RAG_CHUNK_OVERLAP` | 80 | 0–300 | 約 15% |
| 每塊前綴 | `RAG_CHUNK_PREFIX` | 開 | — | `{文件} › {章節}`，與內文一起 embed |

**不給開關的**：層順序已定案固定 —— **RAG 一定在業務表格之後**。

**邊界要夾**：UI 輸入超出範圍就夾到邊界並提示，避免客服填 `top-k=999` 把模型打爆。

⚠️ **參數範圍要跟模型掛鉤**：`bge-small-zh-v1.5` 上限 **512 token**（實測 `max_position_embeddings=512`），
中文約 1 字 ≈ 1 token → 塊長上限必須壓到 ~400，否則會被**無聲截斷**。
`jina-embeddings-v2-base-zh` 是 **8192 token**，塊長可放心到 1200。
→ 「塊長上限」滑桿的最大值會依最終選型給定，並在 UI 顯示「目前模型上限 N token」。

---

## 5. 需求 4：知識庫配置向導

**四步**（參數配置已從向導移除，見 §5.2），**全部在 draft 態**，最後一步才上線：

```
① 上傳 ──▶ ② 分塊預覽與微調 ──▶ ③ 開始 embedding ──▶ ④ 確認入庫
              ↑ 分塊參數在這裡就地調（改了立刻重切、立刻看效果）
```

| 步 | 內容 | 細節 |
|---|---|---|
| ① 上傳 | 拖曳多檔（pdf/docx/xlsx/md/txt/csv/html），選／新建**目標知識庫**，立即存檔 + 解析 | 每檔顯示頁數／字數／工作表；解析失敗給人話原因 |
| ② 分塊預覽與微調 | 逐塊預覽，可**編輯／拆分／合併／刪除／停用**，顯示字數與來源 | 塊長／重疊／策略就在這頁調（見 §4.2 B）；標 `edited` |
| ③ Embedding | 顯示模型、塊數、進度；可中斷、可只重跑失敗塊 | 寫 `embedding_status` |
| ④ 確認入庫 | 摘要（N 檔／M 塊／用哪個模型）→ 上線 | 只把 `status` 改成 `ready` |

**技術要點：draft 直接寫 `kb_chunk`。** 微調即時落庫（不是存在瀏覽器記憶體），
所以「改到一半關掉、明天再回來」不會丟。第 ④ 步只是一個 status 切換，失敗不會有半成品上線。
**重跑 embedding 時**：`edited=true` 的塊要問「保留手改內容還是用新切法覆蓋」。

**「目標知識庫」為什麼留在向導裡**：它是**歸屬**不是**參數** —— 決定這批文件進哪個庫、
之後吃哪一組參數。所以第 ① 步就選，後面的分塊參數預設值也從該庫的 `kb_param` 帶出。

### 5.1 與 MaxKB 的差異

| MaxKB 的做法 | 你的描述 | 我的建議 |
|---|---|---|
| 先建知識庫 → 再上傳文件 | 上傳 → … → 入庫 | **在上傳那一步就選／新建目標知識庫**（保留，理由見上） |
| 分段預覽可編輯 | 分塊後每塊可微調 | 一致；我們再加「停用單塊」 |
| 建庫時就要填一輪參數 | 參數**不進向導** | 照你的：**移出向導**，改在概覽頁按鈕隨時調 |
| 「命中考試」在知識庫內 | 你要獨立成一個子頁籤 | 照你說的做（見 §11） |

### 5.2 知識庫概覽頁與「參數配置」按鈕（已定案 2026-10-08）

知識庫子頁籤（見 §11.1）的主畫面是**概覽頁**：一張知識庫清單。

| 欄位 | 內容 |
|---|---|
| 知識庫名稱 | ＋描述、啟停開關（`kb_collection.enabled`） |
| 文件數 / 塊數 / 最後更新 | 由 `kb_document` 聚合 |
| 狀態摘要 | `N 份待處理 / M 份失敗`（可點進去） |
| **操作** | **🔧 參數配置** ｜ 檢視文件 ｜ 重新向量化 ｜ 刪除 |

點「🔧 參數配置」→ 彈窗，**兩個分頁**：

```
┌─ 參數配置：產品手冊 ────────────────────────┐
│  [檢索參數]  [分塊參數]                      │
│                                              │
│  召回數 top-k          [ 6 ]  (1–30)         │
│  入選門檻              [ 0.55 ]               │
│  單檔最多入選          [ 3 ]                  │
│  上下文上限(字)        [ 6000 ]               │
│  啟用 rerank           [ ] 需要 570MB 模型    │
│  rerank 候選數         [ 20 ]                 │
│                                              │
│  ℹ️ 儲存後 5 秒內生效，不用重啟、不用重建      │
│                       [取消]  [儲存]          │
└──────────────────────────────────────────────┘
```

- **檢索參數分頁**：改完即存 `kb_param`，走 5 秒快取 → **立即生效**。
- **分塊參數分頁**：顯示目前值，**唯讀或編輯後標紅警示**：
  「這些參數影響既有 N 份文件的切法，儲存後需**重新切塊與向量化**」＋ 一顆「儲存並重建」。
  沒按重建就只存參數（新上傳的文件才套用），**不會偷偷動既有資料**。
- 按鈕就在概覽頁，**不用進向導**、不用碰文件，任何時候可調。

---

## 6. 分塊策略（依類型）

| 類型 | 解析器 | 切法 | 保留 metadata |
|---|---|---|---|
| PDF | `pypdf` | 逐頁抽文字 → 按段落合併至目標長度 | `page_no` |
| DOCX | `python-docx` | 按 Heading 1/2/3 階層切；表格轉 markdown | `heading_path` |
| XLSX | `openpyxl` | 每 sheet 一組；標題列當欄名，每列組成「欄名：值」句，每 10–20 列一塊 | `sheet / row_from / row_to` |
| MD | 內建 | 按 `##` 切；程式碼區塊整塊不切 | `heading_path` |
| TXT | 內建 | 按空行段落合併 | — |
| CSV | 內建 | 同 XLSX | 列座標 |

**每塊前置前綴**：`{文件標題} › {章節路徑}`，**與內文一起 embed**（對短問句召回提升明顯）。

**掃描型 PDF**（無文字層）→ 解析 0 字元時**不靜默通過**，標 `failed` + `error='no_text_layer'`，
UI 顯示「讀不到文字，可能是掃描件」。OCR 列為後續選配。

---

## 7. 檢索（含 rerank 介面預留）

```
q → 改寫（帶對話歷史）→ 混合召回 → 去重/截斷 → [rerank] → 門檻判斷 → 生成
```

1. **改寫**：直接複用 `deepseek_client.rewrite_table_query(question, history_text)` —— 它本來就是為「那這個多少錢？」這類指代問句寫的。
2. **混合召回**：`final = 0.65 × cosine + 0.35 × lexical`，`lexical` 用 `faq_matcher.preprocess/score_pair`（jieba + 繁簡正規化）。
3. **去重**：同文件最多 3 塊；`top_k=6`；總字數 ≤ `RAG_MAX_CONTEXT_CHARS`。
4. **門檻**：`top1 >= RAG_MIN_SCORE`（初值 0.55，**上線後用真實問句校準**）；未過 → **不呼叫模型**，直接降級人工。
5. **rerank 介面（先留好、預設不啟用）**：
   ```python
   class Reranker(Protocol):
       def rerank(self, query: str, chunks: list, top_n: int) -> list: ...
   class NoopReranker:      # 預設實作
       def rerank(self, query, chunks, top_n): return chunks[:top_n]
   # 之後要裝：FastEmbedCrossEncoder("BAAI/bge-reranker-v2-m3-int8")
   ```
6. **掛掉時自動降級**：embedding 服務不可用 → 退回純字面檢索（`pg_trgm` + jieba），RAG 層不會整層失效。

### ⚠️ 成本預告（你同意第一版不裝的理由）
CPU 上 cross-encoder 要對每個 (query, chunk) 跑一次完整前向。以 top-20 × 500 字估：
**單次檢索 +1~3 秒**。多個客戶同時問會排隊。若哪天要裝，建議只 rerank top-20 且設總逾時（超時就跳過）。

---

## 8. 生成

新增 `DEEPSEEK_RAG_PROMPT`（可從 `.env` 覆寫，與其他提示詞一致）：

```
以下是知识库检索到的资料（每段前有编号与来源）：
─────
[1]（產品手冊 › 第3章 藍牙耳機 › 價格）A 款建議售價 1280 元…
─────

請只根據上述資料回答，回答要具體並引用可對應的資訊。
若上述資料完全無法回答此問題，你唯一允許的輸出就是這串字元：{sentinel}
```

- `{sentinel}` 沿用 `__NO_ANSWER__`，`reply_engine._tidy()` 直接可用。
- 貼文留言場景沿用 `DEEPSEEK_COMMENT_PROMPT`（公開場合不報價等）。
- **對客戶不顯示來源**；來源寫進記錄，客服側可查。

---

## 9. 需求 1 後半：記錄與命中溯源

- **寫入**：命中時把候選清單寫進 `autoreply_log.matched_rows`：
  ```json
  [{"chunk_id":812,"doc_id":7,"doc":"產品手冊 2026.pdf","heading":"第3章 › 價格","page":18,
    "vec":0.71,"lex":0.42,"final":0.61,"picked":true,"text":"A 款建議售價 1280 元…"}]
  ```
- **讀取：列表輕、詳情重**
  - `GET /console/autoreply/logs` **不加** `matched_rows`（列表可能上百筆，加了會肥）
  - 新增 `GET /console/autoreply/logs/{id}` → 回完整記錄 + 命中明細
- **前端**：`logItemHtml()` 每筆加「詳情」→ 展開面板，顯示召回候選表
  （排名／向量分／字面分／最終分／來源／片段／是否入選）。
  **未命中的記錄也要能點開**（看它檢索到什麼、為什麼沒過門檻）—— 這才是調參最需要的。
- `logs` 的 tier 篩選加 `rag` 選項；`log_summary()` 的 bucket 加 `rag`。

---

## 10. 需求 2：FAQ 頁面依內文檢索

- `GET /console/autoreply/faq?q=` → 對 `question` / `answer` / `keywords` 三欄做 ILIKE。
- ⚠️ **實作坑（這個專案已踩過）**：`autoreply_db.query(sql, params)` 即使傳空 tuple，
  psycopg2 仍會做 `%` 解析 → SQL 裡若出現字面 `%` 會 `IndexError: tuple index out of range`。
  **必須參數化** `ILIKE %s` + `f"%{q}%"`，不要寫字面 `LIKE '%x%'`；SQL 內需要字面 `%` 時改用 `strpos()`。
- 中文子字串用 ILIKE 就夠，不需要分詞或 pg_trgm。
- 前端：搜尋框 + 「符合 N 筆」提示 + 命中詞高亮。
  **注意**：要沿用既有的 `spSnapshot` / `spRestore` 機制，別把客服正在編輯的欄位洗掉（既有痛點）。

---

## 11. 需求 3：命中測試獨立子頁籤

### 11.1 子頁籤順序（已定案 2026-10-08）

設定面板由四個頁籤擴成**六個**，「知識庫」**緊接在「業務表格」後面**：

```
自動回復 │ FAQ 問答對 │ 業務表格 │ 知識庫 │ 命中測試 │ 紀錄
                          └─ 新增 ─┘  └─ 新增 ─┘
```

- 「知識庫」插在「業務表格」後面 ← **你的指定**（與決策鏈層順序一致，好記）。
- 「紀錄」維持壓在最後，既有習慣不變。
- 「命中測試」擺在「知識庫」之後：它是**校準知識庫門檻／top-k／塊長**的工具，跟知識庫相鄰最好用。
  （位置是我的安排，想換跟我說。）
- 既有四個頁籤的**內部內容與行為完全不動**，只是多兩格。

### 11.2 命中測試頁內容

- 從「自動回復」頁移除試跑卡片（連同 `#arTestResult` 一起搬）。
- **UI**：
  - 三層勾選：`FAQ 規則` ／ `業務表格` ／ `知識庫`，加「全選」
  - 渠道 + 場景選擇（複用 `s.scopes`）、「帶入最近對話」開關
  - 輸入框 + 測試鈕
- **結果面板**：走哪一層、會不會送、回覆內容、各層分數；
  RAG 命中時展開**召回明細表**（同 §9）。
- **後端**：`POST /console/autoreply/test/engine` 加 `layers: ["faq","table","rag"]` 參數。
  - ⚠️ **只影響這次呼叫，不寫回 DB 開關** —— 否則「測個試就把線上開關改了」。
    做法：`reply_engine.answer(..., layers=None)`，`None` = 依 DB 開關（正式流程用）。
  - **不寫 `autoreply_log`**（避免污染統計與未命中紅點）。現行 `test/engine` 本來就不寫，維持。

---

## 12. 模型選型（實測數據）

### 12.1 先講結論：我改建議 `jina-embeddings-v2-base-zh`

你問「`bge-small-zh-v1.5` 效果如何、跟 jina 差別大不大」——
**差別不小，而且因為你決定第一版不裝 reranker，這個差距會被放大**（沒有第二道防線修正召回）。

| | `BAAI/bge-small-zh-v1.5` | `jinaai/jina-embeddings-v2-base-zh` |
|---|---|---|
| 參數量 | 24M | 161M |
| 向量維度 | 512 | 768 |
| **最大輸入長度** | **512 token** ⚠️ | **8192 token** |
| 語言 | 中文（英文偏弱） | 中英雙語 |
| 下載大小（fastembed 實測） | **95MB** | **640MB** |
| C-MTEB 中文檢索 | 自家 6 任務表 Retrieval **61.77**／Avg 57.82 | 官方自報 C-MTEB **63.79**（≈ bge-large-zh 檔次） |
| 常駐記憶體 | 約 +0.2GB | 約 +0.9GB |
| 入庫速度（12 核 CPU 估） | 約 0.03 秒/塊 | 約 0.15 秒/塊 |

> 兩邊分數來自不同聚合方式（BGE 自家 6 任務表 vs MTEB 排行榜），**不能逐位相比**，
> 但檔次差距明確：jina-zh 與 bge-**large**-zh 同級，bge-**small**-zh 低一檔。

**三個決定性理由**：

1. **512 token 是硬傷**。你要的塊長「可調、預設 500 字」——中文在 BERT 系 tokenizer 下約 1 字 ≈ 1 token，
   用 bge-small 的話**塊長一超過 500 就被無聲截斷**，等於後面調的參數有一半是假的。jina 的 8192 沒這問題。
2. **沒有 reranker 兜底**。第一版不裝 rerank，召回品質 100% 由 embedding 決定，這時候不能省。
3. **成本可接受**。640MB／12 核／16GB RAM 完全吃得下；而且**檢索延遲差別很小**
   （每次只編碼一句短問句），差的是「入庫時間」與「常駐記憶體 ~0.9GB」，兩者都可接受。

### 12.2 已核實的完整選單（fastembed 0.9.0）

**重要發現：fastembed 的 zh 系列只有 `bge-small-zh-v1.5` 一個**，`bge-base-zh` / `bge-large-zh` 都不支援
（要自己轉 ONNX）。所以中文選項實際上就這幾個：

| 模型 | dim | 下載大小 | 語言 | 上限 |
|---|---|---|---|---|
| `BAAI/bge-small-zh-v1.5` | 512 | 95MB | 中文 | 512 token |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 | 220MB | 50+ 語言 | 128 token（太小） |
| **`jinaai/jina-embeddings-v2-base-zh`** | 768 | **640MB** | 中英 | 8192 token |
| `intfloat/multilingual-e5-large` | 1024 | 2.24GB | 100+ 語言 | 512 token |
| `jinaai/jina-embeddings-v3` | 1024 | 2.29GB | 多語 | 8192 token |

**Reranker（未來要裝時，已核實 fastembed 的實際下載來源）**：

| 模型 | 實際下載 | 大小 | 語言 |
|---|---|---|---|
| `BAAI/bge-reranker-v2-m3-int8` | `onnx-community/bge-reranker-v2-m3-ONNX` / `onnx/model_int8.onnx` | **570MB** | 多語含中文 ← 最小可用 |
| `BAAI/bge-reranker-base` | 同 repo / `onnx/model.onnx` | 1.04GB | 中英（無量化版） |
| `jinaai/jina-reranker-v2-base-multilingual` | 同 repo / `onnx/model.onnx` | 1.11GB | 多語 |
| `Xenova/ms-marco-MiniLM-L-6-v2` | — | 80MB | 英文（不適用） |

**全部走 `fastembed` = 只有 `onnxruntime`，不需要 `torch`（省下 ~2.5GB 依賴）。**

---

## 13. 客服端 UX（低 RAG 知識導向）

| 元素 | 作用 |
|---|---|
| 拖曳上傳 → 向導**四步** | 不用選格式、不用懂檢索參數 |
| 狀態用人話 | 待處理／解析中／分塊中／向量化 62%／完成／失敗（附人話原因） |
| 失敗只有三種文案 | ①讀不到文字（疑似掃描件）②格式不支援 ③服務暫時不可用，請重試 |
| **切塊預覽與微調** | 客服看得到「切成什麼」才不會覺得是黑箱 |
| **命中測試子頁籤** | 自己校準門檻與 top-k |
| 未命中待補清單 | 一鍵「轉成 FAQ」→ 知識閉環 |
| **概覽頁「🔧 參數配置」** | 想調 top-k／門檻時**不用碰文件、不用進向導**；改完 5 秒生效，**不用重啟** |

---

## 14. 接點清單（實作時才動）

| 檔案 | 改動 |
|---|---|
| `reply_engine.py` | `TIER_RAG`、`rag_layer_enabled()`、`_rag_answer()`（插在表格層後）、`answer(..., layers=None)`；`Answer.hit/.label` 認得 rag |
| `autoreply_db.py` | 5 張新表 DDL、kb CRUD、參數讀寫、`get_log(id)`、`list_logs` 加 tier=rag、`log_summary` 加 rag bucket、`list_faqs(q=)` |
| `autoreply_console.py` | `GET /faq?q=`、`GET /logs/{id}`、`test/engine` 加 `layers` |
| `config.py` | `RAG_*` 設定群 + `DEEPSEEK_RAG_PROMPT` |
| **`kb_ingest.py`（新）** | 解析 + 分塊 |
| **`kb_embed.py`（新）** | embedding provider（fastembed；介面留 openai-compatible / ollama） |
| **`kb_rank.py`（新）** | `Reranker` 介面 + `NoopReranker` |
| **`kb_search.py`（新）** | 混合檢索 + 門檻 + 降級 |
| **`kb_console.py`（新）** | 上傳／向導（四步）／概覽頁列表／**參數配置（讀寫 `kb_param`）**／詳情／重試／測試檢索端點 |
| `main.py` | 掛 router、startup 續跑未完成 job |
| `console_ui.html` | 子頁籤擴成六個，順序 自動回復／FAQ 問答對／業務表格／**知識庫**／**命中測試**／紀錄；知識庫頁＝概覽清單 +「🔧 參數配置」彈窗（兩分頁）＋ 向導四步；命中測試搬試跑；記錄加詳情展開；FAQ 加搜尋框 |
| `requirements.txt` | `pypdf`、`python-docx`、`numpy`、`fastembed`、`onnxruntime` |

**不動的**：`autoreply.py`（只認 `Answer`）、`console.py`、`igfb*.py`、送訊息鏈路。

---

## 15. 落地路線與驗收

| 階段 | 內容 | 驗收（可重複執行） |
|---|---|---|
| **P0 選型＋環境** | 裝套件、下模型、建表、冒煙測試 | 對「藍牙耳機」拿到維度正確的向量 |
| **P1 向導與入庫** | 上傳→分塊預覽與微調→embedding→入庫（四步） | 上傳 PDF，300 秒內 `ready`；塊內容人工看過合理 |
| **P1.5 參數配置按鈕** | 概覽頁「🔧 參數配置」彈窗（檢索參數立即生效；分塊參數標需重建） | 改 top-k 後**不重啟**，下一則訊息就用新值；分塊參數改完不重建則不動既有塊 |
| **P2 檢索與測試頁籤** | 檢索端點 + 「命中測試」子頁籤 | 5 條已知問題 top-1 命中率 ≥ 80% |
| **P3 接引擎與溯源** | 第三層 + 三級開關 + 記錄詳情 + FAQ 搜尋 | 端到端：`tier=rag`、訊息送出、記錄可點開看到召回明細 |
| **P4 校準** | 用真實 miss 樣本調門檻／top-k／塊長 | 誤判下降，不再出現「硬掰」回覆 |
| **P5（之後）** | rerank、OCR、pgvector | — |

---

## 16. 風險與成本

1. **rerank 延遲**：CPU 上 +1~3 秒/次（故第一版不裝，介面先留）。
2. **入庫時間**：jina-zh 約 0.15 秒/塊 → 1000 塊約 2.5 分鐘；可中斷、可只重跑失敗塊。
3. **常駐記憶體**：jina-zh 約 +0.9GB（機器 16GB、可用 6.3GB，安全）。
4. **掃描型 PDF**：第一版只偵測回報，不做 OCR。
5. **模型下載**：需外網（`hf-mirror` 實測可用）；建議預先下載到 `models/` 後設定離線。
6. **機密外流面**：全程本機，**文件內文不會離開這台機器**（這正是選本地模型的好處）。
7. **參數被玩壞**：UI 一律夾邊界；塊長上限依模型上限動態限制。
8. **知識庫與業務表格重疊**：照你說的「使用者設定放哪層就放哪層」——系統**不強制分流**，
   但會做**重複偵測提示**（同一份資料兩邊都出現時提醒，避免新舊答案打架）。

---

## 17. 待你確認

1. **Embedding 選型**（唯一 blocker）：我改建議 `jina-embeddings-v2-base-zh`（640MB）。若堅持體積優先就用
   `bge-small-zh-v1.5`（95MB），但塊長上限要壓到 400。
2. ~~上傳時先選知識庫？~~ **已定案**：是（§5 第 ① 步）—— 因為參數按知識庫隔離。
3. **參數覆寫存哪**：另開 `kb_param`（**PK 是 `(collection_id, key)`**，我傾向）還是複用 `autoreply_setting`？
   → 複用的話 `scope_key` 得同時承載「渠道／場景」與「知識庫 id」兩種語義，我認為會歪，建議另開。
4. **命中測試要不要留紀錄**：我建議不留（避免污染統計）。
5. **分塊參數在概覽頁彈窗要不要可編輯**：我建議**可編輯但預設唯讀**，要動就按「儲存並重建」
   （避免客服隨手改塊長、把既有索引改壞）。
