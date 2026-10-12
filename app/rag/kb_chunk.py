"""知識庫分塊：把解析出來的區塊切成「知識塊」（送進 embedding 的最終單位）。

設計依據：RAG_DESIGN.md §6。

策略（`chunk_strategy`）：
  auto       依 doc_type 自動挑（預設）
  paragraph  PDF / TXT：逐頁把段落合併到目標長度
  heading    MD / DOCX / HTML：以標題階層為界，同一節內併到目標長度
  table_row  XLSX / CSV：同一工作表的列句併成塊（約 10~20 列）
  fixed      不看結構，純按字數切（附重疊）

每個塊都會加上前綴 `{文件標題} › {章節路徑}`，與內文一起 embed ——
對「一句短問句」的召回提升明顯（設計稿 §6 註）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.kb_parse import Block

# 策略 → 適用的 doc_type
_STRATEGY_BY_TYPE = {
    "pdf": "paragraph",
    "txt": "paragraph",
    "md": "heading",
    "docx": "heading",
    "html": "heading",
    "xlsx": "table_row",
    "csv": "table_row",
}

# 句末（中英標點）→ 用來切「超過上限的單一長段」
_SENT_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]|[^。！？!?；;\n]+$")

_MAX_PREFIX = 120


@dataclass
class Chunk:
    text: str                       # 最終文字（含前綴）—— 這才是拿去 embed / 顯示的
    body: str                       # 不含前綴的內文
    prefix: str = ""
    char_count: int = 0
    page_no: int | None = None
    heading_path: str = ""
    sheet: str | None = None
    row_from: int | None = None
    row_to: int | None = None
    # ★ 這塊涵蓋了輸入 blocks 的哪個區間（`chunk_blocks(blocks)` 的索引）。
    #   用途：圖片要綁到分塊 —— 解析階段記下「圖屬於第幾個 Block」（ParsedAsset.anchor_block），
    #   這裡再用區間把圖對應到真正落地的知識塊。
    block_from: int | None = None
    block_to: int | None = None

    def as_dict(self) -> dict:
        return {"text": self.text, "body": self.body, "prefix": self.prefix,
                "char_count": self.char_count, "page_no": self.page_no,
                "heading_path": self.heading_path, "sheet": self.sheet,
                "row_from": self.row_from, "row_to": self.row_to}


def resolve_strategy(doc_type: str, strategy: str = "auto") -> str:
    s = (strategy or "auto").lower()
    if s in ("paragraph", "heading", "table_row", "fixed"):
        return s
    return _STRATEGY_BY_TYPE.get((doc_type or "").lower(), "paragraph")


def _make_prefix(title: str, heading_path: str) -> str:
    """組合前綴：`{文件標題} › {章節路徑}`。

    章節路徑本身常常已含標題（Word 的 H1 通常就是文件名），這種情況只留章節路徑，
    避免「產品手冊 › 產品手冊 › 第3章」這種重複。
    """
    t = (title or "").strip()
    h = (heading_path or "").strip()
    if h and t and (h == t or h.startswith(t)):
        p = h
    elif t and h:
        p = f"{t} › {h}"
    else:
        p = t or h
    return p[:_MAX_PREFIX]


def _split_sentences(text: str) -> list[str]:
    out = [s.strip() for s in _SENT_RE.findall(text)]
    return [s for s in out if s]


def _split_long(text: str, hi: int, overlap: int) -> list[str]:
    """把一個超長段落切成若干 ≤ hi 的片段（重疊 overlap 字）。

    重疊同樣不得讓片段超過 hi —— 否則 `chunk_max` 就形同虛設
    （單句貼近 hi 時，接上尾端重疊會直接爆表）。
    """
    pieces: list[str] = []
    buf = ""
    for sent in _split_sentences(text) or [text]:
        while len(sent) > hi:                       # 單句就超長 → 硬切
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sent[:hi])
            sent = sent[max(1, hi - overlap):]
        if buf and len(buf) + len(sent) > hi:
            pieces.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + sent) if len(tail) + len(sent) <= hi else sent
        else:
            buf += sent
    if buf.strip():
        pieces.append(buf)
    return [p.strip() for p in pieces if p.strip()]


def _tail(text: str, n: int) -> str:
    """取尾端 n 字當重疊（盡量從句首起，讀起來才自然）。"""
    if n <= 0 or not text:
        return ""
    tail = text[-n:]
    for mark in ("。", "！", "？", "；", "\n"):
        idx = tail.find(mark)
        if 0 <= idx < len(tail) - 1:
            tail = tail[idx + 1:]
            break
    return tail.strip()


def _group_key(b: Block, strategy: str):
    if strategy == "table_row":
        return (b.sheet or "", b.heading_path or "")
    if strategy == "heading":
        return (b.heading_path or "", b.page_no)
    if strategy == "paragraph":
        return (b.page_no,)
    return ("__all__",)


def _pack(blocks: list[Block], strategy: str, target: int, lo: int, hi: int,
          overlap: int) -> list[tuple[str, Block, Block]]:
    """回傳 [(合併後的內文, 首塊, 尾塊)]。

    首塊／尾塊用來保留來源座標（頁碼 / 列範圍），合併後仍能溯源。

    ⚠️ **重疊（overlap）只能在「開新塊」時把上一塊的尾端接進來**，不可以在累積途中
    拿尾端去取代緩衝區 —— 那種寫法會把還沒收成塊的內容直接丟掉，是「重疊」變成
    「截斷」的經典錯誤（實測 600 字只剩 281 字，兩個段落整段消失）。
    這裡用 `carry` 單獨記住「上一塊的尾端」，緩衝區本身只累加、不刪減。

    ⚠️ 另外要記住「這一塊開頭實際放了多少重疊」（`carry_used`）—— 因為尾端的小碎塊
    可能被併回**同一塊**（見下方「太小的塊併進前一塊」）；那時若不把它開頭的重疊
    拿掉，就會出現「一塊自己重複自己尾巴」的內容（實測 JSON 區塊尾巴整段重複）。
    """
    # (內文, 首塊, 尾塊, 開頭放進去的重疊字)
    out: list[tuple[str, Block, Block, str]] = []
    group_key = None
    buf = ""
    first: Block | None = None
    last: Block | None = None
    carry = ""          # 上一塊的尾端（下一塊開頭要重疊的那段）
    carry_used = ""     # 這一塊開頭實際放進去的重疊

    def flush(keep_overlap: bool = True):
        nonlocal buf, first, last, carry, carry_used
        if buf.strip() and first is not None and last is not None:
            out.append((buf.strip(), first, last, carry_used))
            carry = _tail(buf, overlap) if overlap else ""
        else:
            carry = ""
        buf, first, last, carry_used = "", None, None, ""
        if not keep_overlap:
            carry = ""

    for b in blocks:
        text = (b.text or "").strip()
        if not text:
            continue
        key = _group_key(b, strategy)
        if group_key is not None and key != group_key:
            # 跨群組（換頁 / 換節 / 換工作表）不帶重疊，免得把上一頁的內容混進來
            flush(keep_overlap=False)
        group_key = key

        if len(text) > hi:                          # 單一塊就超長 → 先切
            flush(keep_overlap=False)
            for piece in _split_long(text, hi, overlap):
                out.append((piece, b, b, ""))
            continue

        if buf and len(buf) + 1 + len(text) > hi:
            flush()
        if not buf:
            # 開新塊：先接上一塊的尾端當重疊（放不下就放棄重疊，內容優先）
            head = carry if (carry and len(carry) + 1 + len(text) <= hi) else ""
            buf = (head + "\n" + text) if head else text
            carry_used, carry = head, ""
        else:
            buf += "\n" + text
        if first is None:
            first = b
        last = b
        if len(buf) >= target:
            flush()

    flush()

    # 太小的塊併進前一塊（同群組才併，避免跨節、跨頁、跨工作表）
    merged: list[tuple[str, Block, Block]] = []
    for text, f, l, ov_head in out:
        if (merged and len(text) < lo
                and _group_key(f, strategy) == _group_key(merged[-1][1], strategy)):
            add = text
            if ov_head and text.startswith(ov_head):
                # 這段開頭本來就是前一塊的尾端（重疊），併回去時要拿掉，否則會自己重複自己
                add = text[len(ov_head):].lstrip("\n")
            if len(merged[-1][0]) + 1 + len(add) <= hi:
                pt, pf, _pl = merged[-1]
                merged[-1] = (pt + "\n" + add, pf, l)
                continue
        merged.append((text, f, l))
    return merged


def _pack_fixed(blocks: list[Block], target: int, hi: int, overlap: int) -> list[tuple[str, Block, Block]]:
    """不看結構：全部接起來後按字數切。來源座標取「該片段起點所屬的塊」。"""
    whole = "\n".join(b.text.strip() for b in blocks if (b.text or "").strip())
    if not whole:
        return []
    # 先建立「字元 offset → Block」索引，切完才知道片段來自哪
    spans: list[tuple[int, int, Block]] = []
    pos = 0
    for b in blocks:
        t = (b.text or "").strip()
        if not t:
            continue
        spans.append((pos, pos + len(t), b))
        pos += len(t) + 1

    def locate(offset: int) -> Block:
        for s, e, b in spans:
            if s <= offset < e:
                return b
        return spans[-1][2] if spans else blocks[0]

    out = []
    step = max(1, target - overlap)
    for start in range(0, len(whole), step):
        piece = whole[start:start + hi].strip()
        if piece:
            out.append((piece, locate(start), locate(min(len(whole) - 1, start + len(piece) - 1))))
        if start + hi >= len(whole):
            break
    return out


def chunk_blocks(blocks: list[Block], *, doc_type: str = "", title: str = "",
                 params: dict | None = None) -> list[Chunk]:
    """把解析區塊切成知識塊。params 見 rag_config.PARAM_SPECS（chunk_* 系列）。"""
    p = params or {}
    strategy = resolve_strategy(doc_type, p.get("chunk_strategy", "auto"))
    target = int(p.get("chunk_size", 500) or 500)
    lo = int(p.get("chunk_min", 200) or 200)
    hi = int(p.get("chunk_max", 600) or 600)
    overlap = int(p.get("chunk_overlap", 80) or 0)
    use_prefix = bool(p.get("chunk_prefix", True))

    # 夾邊界：min ≤ target ≤ max，且重疊不得吃掉整個塊
    hi = max(50, hi)
    target = max(20, min(target, hi))
    lo = max(10, min(lo, target))
    overlap = max(0, min(overlap, max(0, hi // 2)))

    n_blocks = [b for b in blocks if (b.text or "").strip()]
    if not n_blocks:
        return []

    if strategy == "fixed":
        packed = _pack_fixed(n_blocks, target, hi, overlap)
    else:
        packed = _pack(n_blocks, strategy, target, lo, hi, overlap)

    chunks: list[Chunk] = []
    # `_pack` 只回 (文, 首塊, 尾塊)，這裡用物件 identity 反查索引，
    # 就知道每塊涵蓋了輸入 blocks 的哪個區間（圖片綁定要靠它）。
    idx_of = {id(b): i for i, b in enumerate(n_blocks)}
    for text, first, last in packed:
        prefix = _make_prefix(title, first.heading_path) if use_prefix else ""
        body = text
        full = f"{prefix}\n{body}" if prefix else body
        row_from = first.row_from
        row_to = last.row_to if last.row_to is not None else last.row_from
        i0 = idx_of.get(id(first))
        i1 = idx_of.get(id(last))
        if i0 is not None and i1 is not None:
            i0, i1 = min(i0, i1), max(i0, i1)
        chunks.append(Chunk(
            text=full, body=body, prefix=prefix, char_count=len(full),
            page_no=first.page_no,
            heading_path=first.heading_path or "",
            sheet=first.sheet,
            row_from=row_from, row_to=row_to,
            block_from=i0, block_to=i1,
        ))
    return chunks


def preview(blocks: list[Block], *, doc_type: str = "", title: str = "",
            params: dict | None = None, limit: int = 0) -> dict:
    """給「分塊預覽」用的摘要（字數分佈 + 前 N 塊）。"""
    chunks = chunk_blocks(blocks, doc_type=doc_type, title=title, params=params)
    sizes = [c.char_count for c in chunks] or [0]
    return {
        "count": len(chunks),
        "total_chars": sum(sizes),
        "min_chars": min(sizes),
        "max_chars": max(sizes),
        "avg_chars": round(sum(sizes) / len(sizes), 1),
        "strategy": resolve_strategy(doc_type, (params or {}).get("chunk_strategy", "auto")),
        "chunks": [c.as_dict() for c in chunks[:limit]] if limit else [],
    }
