# 第三層：RAG 知識庫匹配 — 設計與框架

> 狀態：**設計稿，尚未實作**。本文件只做決策與框架，不含程式碼改動。  
> 決策鏈：`FAQ 規則` → `業務表格` → **`RAG 知識庫`** → `降級人工`

---

## 0. 結論先講

1. **可以加，且不用改動既有兩層的邏輯** —— 引擎是「前三層命中即 return，全落空才降級」，新增一層就是把 `TIER_RAG` 插在 `TIER_MISS` 之前。
2. **今天沒有可用的 embedding 端點**（實測，見 §1）。所以第一件事不是寫程式，是拍板 embedding 走哪裡（§13 決策 1）。
3. **建議檢索用「向量 + 字面」混合**，不要純向量。理由：客戶問句短、口語、帶指代（「那這個多少錢？」），單靠向量在中文短句上召回不穩；而專案裡現成的 `faq_matcher`（jieba 分詞 + OpenCC 繁簡 + 相似度打分）正好補這塊。
4. **生成層直接複用第二層的 sentinel 模式**（餵 context → 只依 context 回答 → 答不出輸出 `__NO_ANSWER__`）。架構、提示詞寫法、日誌欄位全部沿用，實作量最小。
5. 對客服端只暴露兩個動作：**上傳**與**看狀態**；再加一個**測試檢索**當校準工具。所有參數（chunk 大小、overlap、top-k、門檻）都有預設值並收在「進階」裡。

---

## 1. 現況查證（實測結果，非推測）

| 項目                                        | 實測結果                                                                                                                                                              | 對設計的影響                                   |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| 公司 LiteLLM proxy `172.17.7.90:4000`       | 只掛一個 model group `company-code` → 上游 `custom_openai/bonsai-2-27b`（`172.17.7.90:8080`）                                                                             |                                          |
| `POST /v1/embeddings`（model=company-code） | **HTTP 400** `Unmapped LLM provider for this endpoint`                                                                                                            | proxy 有這條路由，但沒有 embedding 模型             |
| `POST /v1/embeddings`（其他模型名 ×5）           | **HTTP 403** 全部 not available for this API key                                                                                                                    | 這把 key 拿不到任何 embedding                   |
| `172.17.7.90:8080`（上游本體）                  | **HTTP 401 Invalid API Key**                                                                                                                                      | 沒有另一把 key，無法直連                           |
| PostgreSQL                                | **18.6**，`LEE` 是 **superuser**，`server_encoding=UTF8`                                                                                                             | 可以 `CREATE EXTENSION`                    |
| `pg_available_extensions`                 | 只有 `pg_trgm`、`unaccent`；**沒有 `vector`**                                                                                                                           | Windows 編 pgvector 需 MSVC 工具鏈 → 不列為第一版方案 |
| 已安裝                                       | `jieba 0.42.1`、`opencc-python-reimplemented`、`openpyxl 3.1.5`、`python-multipart`                                                                                  | 分詞／繁簡／Excel 現成                           |
| Python                                    | **3.14.7**                                                                                                                                                        | 見下                                       |
| cp314 wheel 可用性                           | `numpy 2.5.3` / `pypdf 6.19.0` / `python-docx 1.2.0` / `lxml 6.1.3` / `fastembed 0.8.1` / `onnxruntime 1.30.0` / `sentence-transformers 6.1.0` 全部 `Would install` | **不會被 Python 版本卡住**                      |
| 本機服務                                      | `8001` 主服務在跑；`11434`（Ollama）/`8002` 都沒有服務                                                                                                                         | 沒有現成的本地 embedding 服務                     |

---

## 2. 決策鏈

```
客戶來訊
  ├─ 第一層 FAQ 規則匹配 ─────────── 命中 → 直接回（不呼叫模型）
  ├─ 第二層 業務表格 ─────────────── 命中 → 模型依表生成
  ├─ 第三層 RAG 知識庫 ───────────── 命中 → 模型依命中文件生成   ← 新增
  └─ 第四層 降級人工
```

**與第二層的關鍵差異**：第二層是「把整張表灌進 prompt」（成本高、易被無關列干擾）；第三層是「先精準檢索 top-k 塊再生成」（成本低、可溯源）。

**順序**：預設照你說的放在表格層之後。若日後發現表格層的 prompt 成本太高，可把 RAG 提到表格層之前 —— 引擎只是 if 順序，改一行。

---

## 3. 資料模型（新增 4 張表 + 1 個 job 表）

```sql
-- 知識庫分組（例：產品手冊 / 售後政策 / 內部 SOP），可整組啟停
kb_collection (
    id BIGSERIAL PK, name TEXT, description TEXT,
    enabled BOOL DEFAULT true, sort_order INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 一份文件 = 一列；重傳同名檔 → version +1，舊版 superseded_by 指向新版
kb_document (
    id BIGSERIAL PK,
    collection_id BIGINT REFERENCES kb_collection(id) ON DELETE SET NULL,
    title TEXT, filename TEXT, mime TEXT, size_bytes BIGINT,
    sha256 TEXT,                      -- 去重：內容相同直接複用既有 chunks
    source_path TEXT,                 -- media/kb/<id>/<原始檔>
    doc_type VARCHAR(10),             -- pdf | docx | xlsx | md | txt | csv | html
    version INT DEFAULT 1, superseded_by BIGINT NULL,
    status VARCHAR(12) NOT NULL,      -- pending|parsing|chunking|embedding|ready|failed|disabled
    progress SMALLINT DEFAULT 0,      -- 0~100，給 UI 顯示「向量化 62%」
    chunk_count INT DEFAULT 0, char_count INT DEFAULT 0,
    parse_meta JSONB,                 -- 頁數 / 工作表 / 標題階層統計
    error TEXT,                       -- 失敗原因（原文，UI 再翻成人話）
    enabled BOOL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
);

-- 知識塊：RAG 的最小單位
kb_chunk (
    id BIGSERIAL PK,
    doc_id BIGINT NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    ordinal INT NOT NULL,             -- 在文件中的序號
    text TEXT NOT NULL,               -- 原始塊內容（已含 prefix）
    prefix TEXT,                      -- 「產品手冊 › 第3章 藍牙耳機 › 價格」
    char_count INT,
    page_no INT,                      -- PDF 頁碼（來源追溯）
    heading_path TEXT,                -- Word/MD 標題階層
    sheet TEXT, row_from INT, row_to INT,   -- Excel/CSV 座標
    embedding REAL[],                 -- 無 pgvector 時的存法
    embedding_model TEXT, embedding_dim INT,
    embedding_status VARCHAR(8) DEFAULT 'pending',  -- pending|done|failed
    enabled BOOL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON kb_chunk(doc_id, ordinal);
CREATE INDEX ON kb_chunk(doc_id) WHERE enabled;      -- 檢索只掃 enabled

-- 入庫工作佇列：重啟後可續跑，客服可看「排隊中」
kb_job (
    id BIGSERIAL PK, doc_id BIGINT REFERENCES kb_document(id) ON DELETE CASCADE,
    stage VARCHAR(12), status VARCHAR(10),  -- queued|running|done|failed
    attempts INT DEFAULT 0, last_error TEXT,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
```

**日誌複用既有 `autoreply_log`**，不新增查詢日誌表：`tier='rag'`、`score=top1 分數`、`matched_rows` 存命中的 `{chunk_id, doc_id, score, page/heading}`。這樣工作台的「自動回復記錄」頁面不用改就能顯示溯源。

**向量儲存**：`REAL[]` + 啟動時載入記憶體做 cosine（沿用 `reply_engine._cache` 的 TTL 快取模式）。  
量級估算：1 萬塊 × 1024 維 ≈ 40 MB，暴力內積 ~10M 次運算 → **單次檢索 < 20ms**，完全不需要 pgvector。等真的上到 10 萬塊再考慮裝。

---

## 4. 入庫管線

`上傳 → 解析 → 分塊 → 向量化 → 入庫`

- **非同步**：上傳端點只做「存檔 + 建 document/ job 兩列 + 回 job_id」，實際處理丟 `asyncio.Queue` 的背景 worker（單一 worker、序列處理，避免打爆 embedding 服務）。
- **狀態寫 DB**：前端 2 秒輪詢 `/console/kb/documents`，不開 WebSocket（與現有工作台一致，零新依賴）。
- **可續跑**：worker 啟動時撿 `status IN ('pending','parsing','chunking','embedding')` 的列重跑；`attempts >= 3` 才標 `failed`。
- **可重試**：失敗的文件在 UI 上有「重試」按鈕 → 重置 status 並重新入列。
- **去重**：`sha256` 相同 → 不重複解析，直接複製既有 chunks（同一份文件被兩個客服上傳是常態）。
- **版本**：同 `title` + 不同 `sha256` → `version + 1`，舊版 `enabled=false`（保留可回溯，不刪資料）。

---

## 5. 分塊策略（依檔案類型）

| 類型   | 解析器           | 切法                                          | 保留的來源 metadata              |
| ---- | ------------- | ------------------------------------------- | --------------------------- |
| PDF  | `pypdf`       | 逐頁抽文字 → 按段落合併至目標長度                          | `page_no`                   |
| DOCX | `python-docx` | 按 Heading 1/2/3 階層切；表格轉 markdown 表          | `heading_path`              |
| XLSX | `openpyxl`    | 每個 sheet 一組；以標題列為欄名，每列組成「欄名：值」句，每 10~20 列一塊 | `sheet / row_from / row_to` |
| MD   | 內建            | 按 `##` 標題切；程式碼區塊整塊不切                        | `heading_path`              |
| TXT  | 內建            | 按空行段落切，合併至目標長度                              | 段序                          |
| CSV  | 內建            | 同 XLSX 邏輯                                   | 列座標                         |

**統一參數（預設值，UI 不暴露）**

| 參數   | 預設                | 說明                         |
| ---- | ----------------- | -------------------------- |
| 目標塊長 | 500 中文字           | 太短丟上下文，太長稀釋向量              |
| 重疊   | 80 字（約 15%）       | 避免答案剛好被切在邊界                |
| 最小塊  | 60 字              | 過短併入前一塊                    |
| 前置前綴 | `{文件標題} › {章節路徑}` | **與內文一起 embed**，對短問句召回提升明顯 |

**掃描型 PDF（沒有文字層）**：解析出 0 字元時**不靜默通過**，直接 `failed` + `error='no_text_layer'`，UI 顯示「讀不到文字，可能是掃描件」。OCR 列為 Phase 5 選配，第一版不做。

---

## 6. 檢索策略

```
q → 改寫（帶對話歷史）→ 混合召回 → 去重/截斷 → 門檻判斷 → 生成
```

1. **改寫**：複用現成的 `deepseek_client.rewrite_table_query(question, history_text)` —— 它本來就是為「那這個多少錢？」這類指代問句設計的，直接拿來用，不必新寫提示詞。
2. **混合召回**：  
   `final = 0.65 × cosine(q_vec, chunk_vec) + 0.35 × lexical(q, chunk_text)`  
   `lexical` 用 `faq_matcher.preprocess/score_pair`（jieba + 繁簡正規化 + 字元/詞重疊）。
3. **去重**：同一份文件最多取 3 塊（防單一文件洗版）；`top_k = 6`；總字數上限 6000（沿用 `TABLE_CONTEXT_MAX_CHARS` 的思路）。
4. **門檻**：`top1_final >= RAG_MIN_SCORE`（初值 0.55，**上線後用真實問句校準**）。未過門檻 → **不呼叫模型**，直接降級人工（省錢、省時間，且避免模型硬掰）。
5. **掛掉時的自動降級**：embedding 服務不可用 → 自動退回「純字面檢索」（`pg_trgm` + jieba）。RAG 層不會因為模型掛掉就整層失效。

---

## 7. 生成策略

新增環境變數 `DEEPSEEK_RAG_PROMPT`（與其他提示詞一致，可從 `.env` 覆寫）：

```
以下是从知识库检索到的资料（每段前有编号与来源）：
─────
[1]（產品手冊 › 第3章 藍牙耳機 › 價格）A 款建議售價 1280 元…
[2]（售後政策 › 保固）自購買日起 12 個月內…
─────

請只根據上述資料回答，回答要具體並引用可對應的資訊。
若上述資料完全無法回答此問題，你唯一允許的輸出就是這串字元：{sentinel}
不可用「沒有」「查不到」等自然語言代替。
```

- `{sentinel}` 沿用 `__NO_ANSWER__`，`reply_engine._tidy()` 直接可用。
- **對客戶不顯示來源**（客戶不需要看到「第 3 章」）；來源寫進 `autoreply_log.matched_rows`，**客服側可查**。
- 貼文留言場景沿用 `DEEPSEEK_COMMENT_PROMPT` 的疊加規則（公開場合不報價等）。

---

## 8. 開關與降級

- `rag_enabled` 走既有 `autoreply_setting` 的 `scope_key` 機制 → **天生支援全域 / 按渠道 / 按渠道+場景**三級開關，工作台開關頁面不用改結構，只多一個「知識庫層」勾選。
- 新增 `kb_collection.enabled`、`kb_document.enabled`、`kb_chunk.enabled` 三層細度，客服可整組停用或停用單塊。
- 任一層停用 / 無資料 / 未過門檻 / 模型回 sentinel → `TIER_MISS`，行為與現在完全一致（降級人工 + 記 miss）。

---

## 9. 客服端 UX（針對「有限的 RAG 知識」設計）

**兩個動作搞定：上傳、看狀態。**

| 元素         | 作用                                                       |
| ---------- | -------------------------------------------------------- |
| 拖曳上傳（多檔排隊） | 不用選格式、不用調參數，認得 pdf/docx/xlsx/md/txt/csv                  |
| 狀態用人話      | `待處理` / `解析中` / `分塊中` / `向量化 62%` / `完成` / `失敗（附人話原因）`   |
| 失敗只有三種文案   | ①讀不到文字（疑似掃描件）②格式不支援 ③服務暫時不可用，請重試                         |
| **切塊預覽**   | **最關鍵的一塊** —— 客服看得到「文件被切成什麼」，才不會覺得系統是黑箱；可停用單塊            |
| **測試檢索**   | 輸入一句客戶常問的話 → 顯示 top-5 命中塊 + 分數 + 「會不會被採用」→ 讓客服**自己校準**門檻 |
| 未命中待補清單    | 把降級人工的問題集中列出，一鍵「轉成 FAQ」→ 知識閉環、系統自我成長                     |
| 進階參數       | 收在摺疊區（塊長 / 重疊 / top-k / 門檻），預設值可直接上線                     |

**知識閉環**是這個設計的重點：客服不懂 RAG 沒關係，只要看「未命中清單」→ 補 FAQ 或補文件，知識庫就會隨時間變準。

---

## 10. 要改的接點清單（實作時才動）

| 檔案                     | 改動                                                                                                                                       |                                          |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `reply_engine.py`      | 加 `TIER_RAG`、`rag_layer_enabled()`、`_rag_answer()`（插在表格層之後）；`Answer.hit` / `.label` 認得 rag                                               |                                          |
| `autoreply_db.py`      | 新增 5 張表 DDL、`kb_*` CRUD、層開關沿用 `setting` 機制                                                                                               |                                          |
| `config.py`            | `RAG_*` 設定群（見 §11）、`DEEPSEEK_RAG_PROMPT`                                                                                                 |                                          |
| **`kb_ingest.py`（新）**  | 解析 + 分塊（依 §5 分流）                                                                                                                         |                                          |
| **`kb_embed.py`（新）**   | embedding provider 抽象層（見 §13 決策 1）                                                                                                       |                                          |
| **`kb_search.py`（新）**  | 混合檢索 + 門檻 + 降級                                                                                                                           |                                          |
| **`kb_console.py`（新）** | 端點：`POST /console/kb/upload`、`GET /console/kb/documents`、`GET /console/kb/documents/{id}/chunks`、\`POST /console/kb/documents/{id}/retry | delete`、`POST /console/kb/search\`（測試檢索） |
| `main.py`              | 掛 `kb_console.router`；startup 續跑未完成的 job                                                                                                 |                                          |
| `console_ui.html`      | 新增「知識庫」分頁（掛在既有設定面板的 `sp-tabs` 旁）                                                                                                         |                                          |
| `requirements.txt`     | `pypdf`、`python-docx`、`numpy`、embedding 套件（見決策 1）                                                                                        |                                          |

**不動的**：`autoreply.py`（它只認 `Answer`，加層不必改它）、`console.py`、`igfb*.py`、送訊息鏈路。

新增設定群（`config.py`）：

```ini
RAG_ENABLED=true
RAG_TOP_K=6
RAG_MIN_SCORE=0.55
RAG_MAX_CONTEXT_CHARS=6000
RAG_CHUNK_SIZE=500
RAG_CHUNK_OVERLAP=80
RAG_MAX_CHUNKS_PER_DOC=3
EMBED_PROVIDER=fastembed        # fastembed | openai | ollama | lexical
EMBED_MODEL=BAAI/bge-small-zh-v1.5
EMBED_BASE_URL=
EMBED_API_KEY=
```

---

## 11. 落地路線與驗收標準

| 階段              | 內容                                          | 驗收（可重複執行）                                                                              |
| --------------- | ------------------------------------------- | -------------------------------------------------------------------------------------- |
| **Phase 0**     | 拍板 embedding 方案；裝套件；建表；`kb_embed.py` 打通     | 對「藍牙耳機」拿到一個 **維度 > 0 的真實向量**                                                           |
| **Phase 1**     | 上傳端點 + 解析 + 分塊 + job 佇列；UI 只做「上傳 / 列表 / 狀態」 | 上傳一份 PDF → 300 秒內 `status=ready` 且 `chunk_count > 0`；`kb_chunk` 內容人工看過合理               |
| **Phase 2**     | 檢索端點 + 「測試檢索」UI                             | 5 條已知問題，top-1 命中率 ≥ 80%                                                                |
| **Phase 3**     | 接進引擎第三層 + 三級開關 + 日誌溯源 + 未命中轉 FAQ            | 端到端：知識庫涵蓋的問題 → `tier=rag`、訊息送出、`autoreply_log` 有 chunk 溯源；沒涵蓋的 → `tier=miss` 且未命中清單 +1 |
| **Phase 4**     | 用真實 miss 樣本校準門檻 / top-k                     | 誤判率下降，且不再出現「硬掰」回覆                                                                      |
| **Phase 5（選配）** | 掃描件 OCR、rerank 模型、pgvector                  | —                                                                                      |

---

## 12. 風險清單

1. **掃描型 PDF 無文字層** → 偵測並誠實回報，不靜默失敗；OCR 延後。
2. **模型下載需外網** → 建議預先下載到本地 `models/` 並設 `HF_HUB_OFFLINE=1`；離線環境要先確認。
3. **門檻值需要校準** → 初值一定不準，「測試檢索」就是校準工具，不要期待一次到位。
4. **in-process 佇列** → 服務重啟會中斷入庫；靠 `kb_job` + startup 續跑解決。
5. **知識庫與業務表格內容重疊** → 同一份價格資料別兩邊都放，否則兩層都可能命中、答案不一致（見決策 2）。


1. **機密外流面**：文件存在本機 `media/kb/`；但**若 embedding 走公司 proxy，內文會離開本機**。純內部文件（成本、內部 SOP）要留意這點 —— 決策 1 請一併考慮。
2. **首次上線的期望管理**：RAG 是「有文件才答得出」的層，客服要先上傳對的文件；建議先挑 1~2 份高頻文件試點，而不是一次灌 20 份。

---

## 13. 需要你拍板的三件事

### 決策 1 — Embedding 跑在哪裡？（最關鍵，其他都等這個）

| 方案                                                                            | 成本             | 依賴                    | 適用                               |
| ----------------------------------------------------------------------------- | -------------- | --------------------- | -------------------------------- |
| **A. 請 IT 在 LiteLLM 加一組 embedding model**（如 `bge-m3`）→ 我們直接打 `/v1/embeddings` | 零本機算力、最省事      | 需 IT 配合開模型            | 若內部文件不介意離開本機 → **首選**            |
| **B. 本機 CPU 跑 `fastembed`（onnxruntime，無 torch）**，模型 `BAAI/bge-small-zh-v1.5`  | 約 300MB、CPU 可跑 | 首次需下載模型（可用 hf-mirror） | 兼顧「不外流」與「今天就能開工」→ **次選，我推薦先走 B** |
| **C. `sentence-transformers` + `bge-m3`**                                     | 效果最好           | torch ~2.5GB、CPU 慢    | 有 GPU 再說                         |
| **D. 純字面檢索（`pg_trgm` + jieba）**                                               | 零新依賴           | 語義召回弱                 | 作為 embedding 掛掉時的自動降級            |

程式上做成 provider 可切換（`EMBED_PROVIDER` 一行改），所以**現在選 B、以後換 A 不用改架構**。

### 決策 2 — 知識庫與業務表格怎麼分工？

價格／庫存這類**結構化數字**，放哪裡？

- 放**業務表格**：數字精準、模型直接讀表，但每次都要灌整張表進 prompt。
- 放**知識庫（上傳價格表 Excel）**：檢索便宜、可溯源到列，但數字可能被模型轉述出錯。

我的建議：**結構化數字留業務表格，敘述性內容（手冊、政策、SOP）進知識庫**。Excel 仍可上傳到知識庫（作為輔助問答材料），但兩邊不要放同一份資料。

### 決策 3 — 要不要「停用單個知識塊」這個功能？

- 要：多花一點 UI 工作量，但客服能自己修掉「切壞的塊 / 過期的段落」，不必重傳整份文件。
- 不要：少了知識庫的細粒度維護能力，只能整份文件停用。

我建議要 —— 這是最貼近「客服不想學 RAG」的一項設計。
