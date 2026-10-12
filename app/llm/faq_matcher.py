"""FAQ 匹配邏輯：繁簡正規化 + jieba 分詞 + 字符 bigram + 字符相似度，多信號加權打分。

與小 jo 提供的 `FAQ test/faq_matcher.py` 打分邏輯完全一致，另加上：
  * `preprocess` / `score_pair`：讓呼叫端能把 FAQ 清單預先算好，避免每則訊息重複分詞。
  * `match_many`：一次對多個關鍵詞（一個 FAQ 可設多個問法）取最高分。
"""
import difflib
import re

import jieba
from opencc import OpenCC

# 全域繁→簡轉換（jieba 簡體詞典效果最好，統一正規化到簡體再比對）
_CC = OpenCC("t2s")

# 常見停用詞（簡體，去除後不影響語義判斷）
_STOPWORDS = set(
    "的 了 吗 呢 啊 呀 哦 吧 嘛 请 问 请问 一下 怎么 如何 什么 多少 是否 可以 能 会 要 想 有 是 我 你 他 她 它 我们 你们 他们 帮 个 这 那 一个 谢谢 麻烦 在 和 与 或 及 都 很 你好 想知道 请问一下 我想 想问".split()
)

# 單字功能字（用於把「我要」「你們」這類 jieba 黏連詞裡的功能字清掉，只留實義）
_STOP_CHARS = set(
    "的 我 你 他 她 它 们 了 么 吗 呢 啊 呀 哦 吧 嘛 是 在 有 和 与 或 及 都 很 就 还 也 再 才 又 给 把 被 从 向 对 个 这 那 请 问 要 想 会 能 可 以 一 下 不 您 咱".split()
)

# 標點以外的合法字元：中文 / 字母 / 數字
_NORM_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

# 匹配門檻：score 達此值才視為命中
THRESHOLD = 0.45


def normalize(text: str) -> str:
    """繁→簡 + 小寫 + 去標點空白，統一格式。"""
    if not text:
        return ""
    text = _CC.convert(text)
    return _NORM_RE.sub("", text.lower().strip())


def tokenize(text: str) -> list:
    """jieba 分詞，先過濾整詞停用詞，再剔除黏連詞裡的功能字。"""
    words = []
    for w in jieba.lcut(normalize(text)):
        w = w.strip()
        if not w or w in _STOPWORDS:
            continue
        cleaned = "".join(ch for ch in w if ch not in _STOP_CHARS)
        if cleaned:
            words.append(cleaned)
    return words


def _bigrams(s: str) -> set:
    """連續字符 2-gram（捕捉「付款」「退货」這類核心詞重合）。"""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard_set(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _char_similarity(na: str, nb: str) -> float:
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def preprocess(text: str) -> tuple[str, set]:
    """預處理：回傳 (normalized, token_set)，供 score_pair 重用。"""
    return normalize(text), set(tokenize(text))


def score_pair(nu: str, user_tokens: set, nf: str, faq_tokens: set) -> float:
    """雙方皆已預處理時的打分；分數與 score() 完全一致。"""
    if not nu or not nf:
        return 0.0

    # 完全一致
    if nu == nf:
        return 1.0

    # 包含關係（至少兩個字才算，避免單字誤命中）
    shorter, longer = min(len(nu), len(nf)), max(len(nu), len(nf))
    if shorter >= 2 and (nf in nu or nu in nf):
        return 0.80 + 0.20 * (shorter / longer)

    # 三信號加權：詞重合 + 字符 bigram 重合 + 字符序列相似
    w = _jaccard_set(user_tokens, faq_tokens)
    b = _jaccard_set(_bigrams(nu), _bigrams(nf))
    c = _char_similarity(nu, nf)
    return 0.30 * w + 0.30 * b + 0.40 * c


def score(user_q: str, faq_q: str) -> float:
    """回傳 0~1 的匹配分數（對外介面不變）。"""
    nu, ut = preprocess(user_q)
    nf, ft = preprocess(faq_q)
    return score_pair(nu, ut, nf, ft)


def match(user_q: str, faqs: list, threshold: float = THRESHOLD):
    """在 faqs（每個元素含 question/answer）裡找最佳匹配。

    回傳 (best_faq_or_None, best_score)。
    """
    best, best_score = None, 0.0
    for f in faqs:
        q = f.get("question", "")
        s = score(user_q, q)
        if s > best_score:
            best_score = s
            best = f
    if best_score >= threshold:
        return best, best_score
    return None, best_score


# ---------- 供回覆引擎用的高效能版本 ----------

def prepare(questions: list) -> list:
    """把一組問法預先算成 [(原問法, (normalized, tokens)), ...]。"""
    out = []
    for q in questions:
        q = (q or "").strip()
        if q:
            out.append((q, preprocess(q)))
    return out


def match_prepared(user_q: str, prepared: list, threshold: float = THRESHOLD,
                   best_of: int = 1) -> tuple[float, list]:
    """對已預處理的問法清單打分。

    回傳 (最高分, 達門檻的 [(分數, 問法)] 由高到低，最多 best_of 筆)。
    呼叫端可據 best_of 判斷「是否有並列高分」以決定要不要問 AI。
    """
    nu, ut = preprocess(user_q)
    hits: list[tuple[float, str]] = []
    best = 0.0
    for raw, (nf, ft) in prepared:
        s = score_pair(nu, ut, nf, ft)
        if s > best:
            best = s
        if s >= threshold:
            hits.append((s, raw))
    hits.sort(key=lambda x: x[0], reverse=True)
    return best, hits[:best_of] if best_of > 0 else hits
