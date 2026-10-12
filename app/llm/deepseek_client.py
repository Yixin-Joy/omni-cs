"""DeepSeek 客戶端（OpenAI 相容 /chat/completions 介面）。

模型預設 `deepseek-flash`（可用 `DEEPSEEK_MODEL` 覆寫）。只依賴 httpx，
與本專案其餘部分一致；所有呼叫都是 async，避免阻塞 webhook。

⚠️ **兩類呼叫要分開對待（2026-10-08 實測 48.93 秒後定案）**：

    必要呼叫（生成答案）   → 一般超時（DEEPSEEK_TIMEOUT），失敗可重試一次
    輔助呼叫（改寫問句）   → **短超時 + 不重試**（`AUX_TIMEOUT`，預設 5 秒）

    理由：改寫只是「錦上添花」，失敗就沿用原問句即可；但用 30 秒超時 + 自動重試，
    一次 ReadTimeout 就會吃掉 **60 秒**（實測就是這樣），把整則回覆拖垮。

另外閘道常有限流（實測公司 LiteLLM 回 `max_parallel_requests` 429）：
`_GATE` 信號量把同時的呼叫數壓在 `DEEPSEEK_MAX_CONCURRENCY` 之內，
429 則依 `Limit resets at` 退避後重試。
"""
import asyncio
import json
import logging
import re
import threading
import time

import httpx

from app.core import config

log = logging.getLogger("wechat-forwarder")


class DeepSeekError(Exception):
    """DeepSeek 呼叫失敗（網路、金鑰、額度、回應格式）。"""


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 表格答不出來時，模型必須只輸出這個標記（供程式判定走「未命中」）
NO_ANSWER_SENTINEL = "__NO_ANSWER__"

# 使用者名稱若被誤當 API Key 填入，這裡做最後一道辨識
_API_KEY_PREFIX = "sk-"

# ---------- 併發閘門 ----------
# 為什麼要自己做：閘道端有 `max_parallel_requests` 限流（實測 =2），
# 超了就回 429。與其讓每個呼叫各自撞牆，不如在本地先排隊。
_GATE: asyncio.Semaphore | None = None
_GATE_LIMIT = 0                  # ⚠️ 必須在 _gate() 之前宣告（它是模組全域，被函式讀寫）
_GATE_LOCK = threading.Lock()


def _gate() -> asyncio.Semaphore:
    """（惰性建立）全域併發信號量。

    ⚠️ asyncio.Semaphore 綁定事件迴圈，所以每個迴圈各建一個 —— 服務是單一
    事件迴圈，正常只會建一次；測試腳本反覆 asyncio.run() 也不會拿到舊迴圈的物件。
    """
    global _GATE, _GATE_LIMIT
    limit = max(1, int(config.DEEPSEEK_MAX_CONCURRENCY or 1))
    with _GATE_LOCK:
        if _GATE is None or _GATE_LIMIT != limit:
            _GATE = asyncio.Semaphore(limit)
            _GATE_LIMIT = limit
        return _GATE

# ---------- 呼叫觀察鉤子 ----------
# 給呼叫端（reply_engine）統計「這次請求呼叫了幾次模型、各花多久」用。
# 用 contextvar：自動回復是併發的，兩則訊息同時跑時不能互相蓋掉。
import contextvars as _contextvars

_CALL_HOOK: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "deepseek_call_hook", default=None)


def set_call_hook(fn):
    """註冊觀察者：每次呼叫後以 (ok, seconds, error) 回呼。回傳 token 供還原。"""
    return _CALL_HOOK.set(fn)


def reset_call_hook(token) -> None:
    try:
        _CALL_HOOK.reset(token)
    except (ValueError, LookupError):
        pass


def _notify(ok: bool, seconds: float, err: Exception | None = None) -> None:
    fn = _CALL_HOOK.get()
    if fn is None:
        return
    try:
        fn(ok, seconds, err)
    except Exception:            # 觀察者壞掉不該影響主流程
        pass


# 429 的「額度幾點重置」→ 退避秒數（抓不到就用預設）
# ⚠️ 只寫 `resets?_at` 會漏掉閘道實際用的「Limit resets at: …」（有空格），
#    退避時間就永遠走預設值。這裡放寬成「reset 之後 24 字內的時間戳」，容錯優先。
_RESET_RE = re.compile(
    r"reset\w{0,3}[^\d]{0,12}(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.I)


def _retry_after(err: Exception, default: float) -> float:
    """從錯誤訊息推算該等多久再試（只認 429 的 reset 時間，其他用固定退避）。"""
    m = _RESET_RE.search(str(err))
    if m:
        try:
            from datetime import datetime, timezone
            ts = m.group(1).replace("T", " ")
            when = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            wait = (when - datetime.now(timezone.utc)).total_seconds() + 0.5
            return max(0.5, min(wait, 15.0))     # 最多等 15 秒，別把請求吊死
        except Exception:
            pass
    return default


def api_key() -> str:
    return (config.DEEPSEEK_API_KEY or "").strip()


def configured() -> bool:
    return bool(api_key())


def masked_key() -> str:
    k = api_key()
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return f"{k[:4]}****{k[-4:]}"


# ---------- 提示詞（可在工作台編輯；DB 覆寫優先） ----------

def _db_prompt(name: str, fallback: str) -> str:
    """取提示詞的執行時值：DB（`kb_param` 全域）有覆寫就用，否則用 `fallback`。

    ★ 2026-10-08 起提示詞是**參數**（知識庫 → 參數配置 → C. 提示詞 可直接編輯），
    所以不能在 import 期就把字串定死 —— 否則改了頁面不會生效。
    取值走 `kb_search.prompt_of()`（共用參數快取，5 秒 TTL）。

    ⚠️ 用 lazy import 是刻意的：`kb_search` 會拉進 psycopg2 / numpy，
    「LLM 客戶端」不該在 import 期就扛這些依賴（單獨跑測試腳本也不會踩到 DB）。
    DB 讀不到就安靜退回 fallback —— 提示詞拿不到不該讓整個知識庫層掛掉。
    """
    try:
        from app.rag import kb_search
        return kb_search.prompt_of(name) or fallback
    except Exception as e:
        log.debug("讀取提示詞覆寫失敗（用內建預設）: %s", e)
        return fallback


def _fill(template: str, **values) -> str:
    """把 `{key}` 逐個換掉，**不用 `str.format`**。

    與 `rag_config` 同一套慣例：使用者自訂的提示詞裡只要出現一個孤立的 `{`
    （例如寫「用 {} 包起來」），`format()` 就會直接 KeyError/ValueError
    把整層打掛。逐字 replace 最笨但不會炸，多餘的 `{...}` 原樣留著。
    """
    out = template or ""
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


class DeepSeekClient:
    def __init__(
        self,
        key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        self.api_key = (key if key is not None else api_key()).strip()
        self.base_url = (base_url or config.DEEPSEEK_BASE_URL or DEFAULT_BASE_URL).rstrip("/")
        self.model = (model or config.DEEPSEEK_MODEL or DEFAULT_MODEL).strip()
        self.timeout = float(timeout or config.DEEPSEEK_TIMEOUT or 30)

    async def complete(self, messages: list, temperature: float = 0.0, *,
                       timeout: float | None = None, retries: int | None = None) -> str:
        """送出一輪對話，回傳模型輸出文字。

        `timeout` / `retries` 可**單次覆寫**（改寫類呼叫走短超時、不重試 —— 見模組說明）。

        網路類錯誤（連線中斷 / 逾時）預設自動重試一次：這類錯誤多半是暫時的。
        ⚠️ 但重試會讓最壞耗時變成兩倍（30 秒超時 → 60 秒），所以呼叫端若覺得
        「失敗了也無所謂」，就該傳 `retries=0`。
        """
        max_tries = 1 if retries is None else max(0, int(retries)) + 1
        last_error: Exception | None = None
        t0 = time.perf_counter()
        for attempt in range(max_tries):
            try:
                out = await self._complete_once(messages, temperature, timeout=timeout)
                _notify(True, time.perf_counter() - t0)
                return out
            except DeepSeekError as e:
                last_error = e
                if attempt + 1 < max_tries and _is_transient(e):
                    wait = 0.8 if not _is_rate_limited(e) else _retry_after(e, 2.0)
                    log.warning("DeepSeek 呼叫暫時失敗，%.1fs 後重試：%s", wait, str(e)[:120])
                    await asyncio.sleep(wait)
                    continue
                _notify(False, time.perf_counter() - t0, e)
                raise
        raise last_error  # pragma: no cover

    async def _complete_once(self, messages: list, temperature: float = 0.0,
                             timeout: float | None = None) -> str:
        if not self.api_key:
            raise DeepSeekError("尚未配置 DeepSeek API Key（請在 .env 填 DEEPSEEK_API_KEY）")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }

        # 本地先排隊，別讓併發量超過閘道限流（否則每個呼叫都撞 429）
        try:
            async with _gate():
                async with httpx.AsyncClient(timeout=float(timeout or self.timeout)) as client:
                    resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            # httpx 的某些例外 str() 是空的，補上型別名方便排查
            raise DeepSeekError(f"連線失敗：{type(e).__name__}{f'（{e}）' if str(e) else ''}") from e

        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message") or resp.text[:200]
            except Exception:
                detail = resp.text[:200]
            raise DeepSeekError(f"HTTP {resp.status_code}：{detail}")

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise DeepSeekError(f"回應格式異常：{e}") from e

    # ---------- 具體用途 ----------

    async def ping(self) -> str:
        """連線測試。"""
        return await self.complete(
            [{"role": "user", "content": "請只回覆「OK」兩個字。"}], temperature=0.0
        )

    @property
    def aux_timeout(self) -> float:
        """輔助呼叫（改寫）的超時：短，且**不重試**。

        改寫失敗沿用原問句就好，沒有必要為它等 30 秒甚至 60 秒。
        """
        return float(config.DEEPSEEK_AUX_TIMEOUT or 5)

    async def rewrite_queries(self, question: str, history_text: str = "",
                              count: int = 2, faq_questions: list | None = None,
                              post_context: str = "") -> list[str]:
        """**一次改寫、三層共用**：依上下文改寫出 `count` 條查詢句。

        這是整個自動回復流程裡**唯一**的改寫呼叫（2026-10-08 定案）——
        產出的查詢會同時用於：FAQ 二次匹配、業務表格生成、知識庫檢索。
        舊做法是每層各自改寫（各有一條提示詞），實測一次回覆因此吃掉 8 次模型呼叫。

        重點是**同音錯別字**、**上下文補齊**、以及**留一條客戶口語版**。
        提示詞是**參數**（`rewrite_prompt`），可在「知識庫 → 參數配置 → C. 提示詞」
        直接編輯並立刻生效 —— 這裡只是取它的執行時值。

        `faq_questions` 可選：現有 FAQ 問句清單，注進提示詞當「用語風格參考」，
        讓改寫結果更容易被 FAQ 規則匹配到（會截斷，避免 prompt 膨脹）。

        只送**單一 user message** —— 有些 OpenAI 相容閘道（實測 LiteLLM）不接受連續
        多則 system，這樣寫最保險。

        回傳去重後、最多 `count` 條的查詢句；模型沒給出可用的東西就回空 list
        （呼叫端據此沿用原問句，不要當成錯誤）。
        """
        from app.rag import rag_config

        hint = ""
        faqs = [str(q).strip() for q in (faq_questions or []) if str(q or "").strip()]
        if faqs:
            limit = max(0, int(rag_config.RAG_REWRITE_FAQ_HINT_MAX or 0))
            shown = faqs[:limit] if limit else faqs
            more = f"（另有 {len(faqs) - len(shown)} 條未列出）" if len(faqs) > len(shown) else ""
            hint = _fill(rag_config.RAG_REWRITE_FAQ_HINT,
                         faq_list="\n".join(f"- {q}" for q in shown) + more)

        template = _db_prompt("rewrite_prompt", rag_config.RAG_REWRITE_PROMPT)
        prompt = _fill(template,
                       n=str(max(1, int(count or 1))),
                       faq_hint=hint,
                       history=(history_text or "").strip() or "（沒有更早的對話紀錄）",
                       question=question)
        if post_context:
            prompt = f"{prompt}\n\n貼文內容（僅供理解語境）：\n{post_context}"
        out = await self.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            timeout=self.aux_timeout, retries=0,
        )
        return clean_rewrite_list(out, limit=count, exclude=[question])

    async def rewrite_rag_queries(self, question: str, history_text: str = "",
                                  count: int = 2, post_context: str = "") -> list[str]:
        """`rewrite_queries()` 的相容包裝（`/console/kb/search` 調參端點在用）。"""
        return await self.rewrite_queries(question, history_text, count=count,
                                          post_context=post_context)

    async def answer_with_table(self, question: str, table_context: str,
                                flavor: str = "dm", post_context: str = "") -> str:
        """把業務表格當上下文回答；答不出來時回傳含 NO_ANSWER_SENTINEL 的字串。

        ⚠️ 多段 system 指示一律**合併成單一 system message**，不可拆成連續多則。
        部分 OpenAI 相容閘道的 prompt 模板（實測 LiteLLM proxy）要求 system
        只能是最前面那一則，連續多個 system 會直接回 HTTP 500
        （`Jinja Exception: System message must be at the beginning`）。
        合併後語意完全相同（都是 system 層級指示），DeepSeek 官方也照樣接受。
        """
        parts = [config.DEEPSEEK_SYSTEM_PROMPT]
        if flavor == "comment":
            parts.append(config.DEEPSEEK_COMMENT_PROMPT)
        if post_context:
            parts.append(post_context)
        parts.append(config.DEEPSEEK_TABLE_PROMPT.format(
            context=table_context, sentinel=NO_ANSWER_SENTINEL
        ))
        messages = [
            {"role": "system", "content": "\n\n".join(parts)},
            {"role": "user", "content": question},
        ]
        return (await self.complete(messages, temperature=0.2)).strip()

    async def answer_with_rag(self, question: str, rag_context: str,
                              flavor: str = "dm", post_context: str = "") -> str:
        """把知識庫檢索到的段落當上下文回答；答不出來時回含 NO_ANSWER_SENTINEL 的字串。

        ★ 主體指示（`rag_answer_prompt`）是**可以在工作台編輯的參數**
        （知識庫 → 參數配置 → C. 提示詞），所以每次呼叫都從 DB 取現行值，
        不在 import 期定死；取不到才退回 `rag_config.RAG_PROMPT`。

        與 `answer_with_table` 同一套規矩：**多段指示一律合併成單一 system message**
        （部分 OpenAI 相容閘道的模板只接受最前面那一則 system，連續多則會回 HTTP 500）。
        """
        from app.rag import rag_config

        parts = [config.DEEPSEEK_SYSTEM_PROMPT]
        if flavor == "comment":
            parts.append(config.DEEPSEEK_COMMENT_PROMPT)
        if post_context:
            parts.append(post_context)
        main = _db_prompt("rag_answer_prompt", rag_config.RAG_PROMPT)
        parts.append(_fill(main, context=rag_context, sentinel=NO_ANSWER_SENTINEL))
        messages = [
            {"role": "system", "content": "\n\n".join(parts)},
            {"role": "user", "content": question},
        ]
        return (await self.complete(messages, temperature=0.2)).strip()


def clean_rewrite(text: str) -> str:
    """清理模型輸出的引號、常見前綴、多餘空白與換行。"""
    if not text:
        return ""
    t = text.strip()

    # 先剝掉外層成對引號（含「」『』 這類左右不同的）
    pairs = [("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")]
    changed = True
    while changed and len(t) >= 2:
        changed = False
        for left, right in pairs:
            if t.startswith(left) and t.endswith(right):
                t = t[1:-1].strip()
                changed = True
                break

    # 再剝掉常見前綴
    for prefix in ("改寫後：", "改寫後:", "改寫：", "改写后：", "改写：",
                   "標準問句：", "标准问句：", "改寫後的問句：", "改写后的问句："):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break

    # 只取第一行，避免模型多餘輸出
    return t.splitlines()[0].strip() if t else ""


def clean_rewrite_list(text: str, limit: int = 2, exclude: list[str] | None = None) -> list[str]:
    """把模型輸出的「多條改寫查詢」解析成 list。

    模型被要求只輸出 N 行，但實務上會多送編號（`1.` / `1、` / `-`）、引號、JSON 陣列、
    甚至一句「好的，以下是改寫結果：」——這裡全部容忍，只挑得出問句。

    規則：
      * 先試 JSON 陣列（`["a","b"]`），失敗才逐行拆
      * 逐行剝掉編號／項目符號／引號（重用 `clean_rewrite`）
      * 丟掉空行、超過 60 字的行（那是說明不是問句）、含「改寫」「查詢句」之類的說明行
      * 去掉與 `exclude`（原問句）重複的、以及自身重複的（比對時忽略空白）
    """
    out: list[str] = []
    if not text:
        return out

    t = (text or "").strip()
    if t.startswith("```"):                      # 去掉 ```json 圍欄
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]

    lines: list[str] = []
    start, end = t.find("["), t.rfind("]")
    if 0 <= start < end:
        try:
            data = json.loads(t[start:end + 1])
            if isinstance(data, list):
                lines = [str(x) for x in data if isinstance(x, (str, int, float))]
        except Exception:
            lines = []
    if not lines:
        lines = t.splitlines()

    seen: set[str] = set()
    for raw in lines:
        line = (raw or "").strip()
        if not line or _looks_like_instruction(line):
            continue
        line = _strip_list_marker(line)
        line = clean_rewrite(line)
        key = "".join(line.split())
        if not line or len(line) > 60 or key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= max(1, limit):
            break

    if exclude:
        skip = {"".join(str(x or "").split()) for x in exclude}
        out = [x for x in out if "".join(x.split()) not in skip]
    return out


# 說明句而非問句的樣態（模型很愛多寫這幾句）。
# ⚠️ 只比對**行首**，不要用「包含」——「保固說明」是正常問句，「查詢訂單」也是。
_INSTRUCTION_PREFIXES = ("以下是", "如下", "好的", "改寫", "改写", "查詢句", "查询句",
                         "輸出", "输出", "結果", "结果", "前綴", "前缀")


def _looks_like_instruction(line: str) -> bool:
    """這一行看起來是「模型的說明」而不是查詢句。"""
    s = line.lstrip()
    if s.endswith("：") or s.endswith(":"):
        return True
    return s.startswith(_INSTRUCTION_PREFIXES)


def _strip_list_marker(line: str) -> str:
    """剝掉行首的編號／項目符號：`1.` `1、` `1)` `-` `*` `①`。"""
    s = line.lstrip()
    for mark in ("-", "*", "•", "·"):
        if s.startswith(mark):
            return s[len(mark):].strip()
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] in "①②③④⑤⑥⑦⑧⑨" or s[i] in ".、)）]"):
        i += 1
    if 0 < i <= 3:
        return s[i:].strip()
    return s


def looks_answered(text: str) -> bool:
    """模型輸出是否為「有效答案」（非空、且沒有回 NO_ANSWER 標記）。"""
    t = (text or "").strip()
    if not t:
        return False
    return NO_ANSWER_SENTINEL not in t.upper().replace(" ", "")


# 值得重試的暫時性錯誤關鍵字（連線中斷 / 逾時 / 伺服器端錯誤）。
# HTTP 4xx 這種（金鑰錯、參數錯）重試沒意義，不重試。
_TRANSIENT_HINTS = ("連線失敗", "timeout", "timed out", "connection",
                    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")


def _is_transient(err: Exception) -> bool:
    """這個錯誤是否可能重試就成功。"""
    msg = str(err).lower()
    return any(h.lower() in msg for h in _TRANSIENT_HINTS)


def _is_rate_limited(err: Exception) -> bool:
    """是不是被限流（429）—— 這種要等久一點，退避時間看閘道給的 reset 時間。"""
    return "429" in str(err) or "rate limit" in str(err).lower()


def safe_json(text: str):
    """從模型輸出中撈出第一個 JSON 物件（容忍 ```json 圍欄與前後贅字）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1] if len(t.split("```")) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except Exception:
        return None
