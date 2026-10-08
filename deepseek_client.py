"""DeepSeek 客戶端（OpenAI 相容 /chat/completions 介面）。

模型預設 `deepseek-flash`（可用 `DEEPSEEK_MODEL` 覆寫）。只依賴 httpx，
與本專案其餘部分一致；所有呼叫都是 async，避免阻塞 webhook。
"""
import asyncio
import json
import logging

import httpx

import config

log = logging.getLogger("wechat-forwarder")


class DeepSeekError(Exception):
    """DeepSeek 呼叫失敗（網路、金鑰、額度、回應格式）。"""


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 表格答不出來時，模型必須只輸出這個標記（供程式判定走「未命中」）
NO_ANSWER_SENTINEL = "__NO_ANSWER__"

# 使用者名稱若被誤當 API Key 填入，這裡做最後一道辨識
_API_KEY_PREFIX = "sk-"


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

    async def complete(self, messages: list, temperature: float = 0.0) -> str:
        """送出一輪對話，回傳模型輸出文字。

        網路類錯誤（連線中斷 / 逾時）自動重試一次：這類錯誤多半是暫時的，
        重試一下就成功，可避免白白降級到人工。
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await self._complete_once(messages, temperature)
            except DeepSeekError as e:
                last_error = e
                if attempt == 0 and _is_transient(e):
                    log.warning("DeepSeek 呼叫暫時失敗，重試一次：%s", e)
                    await asyncio.sleep(0.8)
                    continue
                raise
        raise last_error  # pragma: no cover

    async def _complete_once(self, messages: list, temperature: float = 0.0) -> str:
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

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
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

    async def rewrite_query(self, question: str, faq_questions: list) -> str:
        """把口語化問題改寫成貼近 FAQ 風格的標準問句，只回傳問句本身。"""
        listed = "\n".join(f"- {q}" for q in faq_questions if q and str(q).strip())
        system = config.DEEPSEEK_REWRITE_PROMPT.replace("{faq_list}", listed)
        out = await self.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
        )
        return clean_rewrite(out)

    async def rewrite_table_query(self, question: str, history_text: str) -> str:
        """業務表格第一次查不到時：參考最近對話把問句補成可獨立理解的查詢。

        history_text 為「客戶/客服：內容」逐行的對話文字；沒有對話時傳空字串，
        此時退化成單純的單句改寫。
        """
        system = (config.DEEPSEEK_TABLE_REWRITE_PROMPT
                  .replace("{history}", history_text.strip() or "（沒有更早的對話紀錄）")
                  .replace("{question}", question))
        out = await self.complete(
            [{"role": "user", "content": system}],
            temperature=0.0,
        )
        return clean_rewrite(out)

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

        與 `answer_with_table` 同一套規矩：**多段指示一律合併成單一 system message**
        （部分 OpenAI 相容閘道的模板只接受最前面那一則 system，連續多則會回 HTTP 500）。
        """
        import rag_config

        parts = [config.DEEPSEEK_SYSTEM_PROMPT]
        if flavor == "comment":
            parts.append(config.DEEPSEEK_COMMENT_PROMPT)
        if post_context:
            parts.append(post_context)
        parts.append(rag_config.RAG_PROMPT.format(
            context=rag_context, sentinel=NO_ANSWER_SENTINEL
        ))
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
