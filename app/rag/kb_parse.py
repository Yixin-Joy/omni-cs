"""知識庫文件解析：把各種格式轉成「帶結構 metadata 的文字區塊」+「圖片資產」。

支援：pdf / docx / xlsx / md / txt / csv / html

輸出的 `Block` 保留來源座標（頁碼 / 標題階層 / 工作表 / 列範圍），
讓後面分塊、溯源、客服「命中詳情」都能顯示「這段話是從哪來的」。
設計依據：RAG_DESIGN.md §6。

★ PDF 走**行級解析**（pdfplumber，帶 y 座標），不是 `extract_text()`：
  - 頁碼 / 頁眉 / 頁腳會被辨識並丟棄（見 `_hf_lines`）
  - **跨頁只是物理隔斷，不產生段落分隔**：上一行沒以句末標點收尾、且本行沒有
    段落起始特徵時，兩行直接接起來（中文不插空格、英文插一個空格）。
    只有真正的語義段落結束才輸出 `\n\n`。
    ⚠️ 舊版用 `extract_text()` → 每頁自己成段、跨頁句子被硬切（見 RAG_DESIGN.md §6.2）。

★ 圖片（`ParsedAsset`）會一併抽出來交給呼叫端落地存檔；
  若圖片有**題注**（「圖 3-2 xxx」），直接拿它當預設描述，不必再花 AI 讀圖。

⚠️ 掃描型 PDF（沒有文字層）**不靜默通過**：解析出 0 字元時直接拋
`ParseError("no_text_layer")`，由呼叫端把文件標成 failed 並給客服人話提示。
"""
from __future__ import annotations

import csv
import hashlib
import html as _html
import io
import logging
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

log = logging.getLogger("wechat-forwarder")

# 可接受的副檔名 / mime
DOC_TYPES = ("pdf", "docx", "xlsx", "md", "txt", "csv", "html")

_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",          # 舊格式會解析失敗 → 由 parser 給人話原因
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xlsx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".log": "txt",
    ".csv": "csv",
    ".tsv": "csv",
    ".html": "html",
    ".htm": "html",
}


class ParseError(Exception):
    """解析失敗。`reason` 是穩定的錯誤碼，UI 據此顯示人話文案。"""

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        self.message = message or reason
        super().__init__(self.message)


@dataclass
class Block:
    """解析出來的一個結構化文字區塊（還不是最終的知識塊）。"""

    text: str
    page_no: int | None = None
    heading_path: str = ""
    sheet: str | None = None
    row_from: int | None = None
    row_to: int | None = None
    kind: str = "text"          # text | table_row | code | sheet_meta | heading
    y0: float | None = None     # ★ PDF：本塊第一行的上緣（拿來把圖片配到最近的塊）
    y1: float | None = None     # ★ PDF：本塊最後一行的下緣

    def clone_text(self, text: str) -> "Block":
        return Block(text, self.page_no, self.heading_path, self.sheet,
                     self.row_from, self.row_to, self.kind, self.y0, self.y1)


@dataclass
class ParsedAsset:
    """解析出來的一張圖片（還沒落地存檔）。

    ⚠️ `data` 是原始位元組，會一直留在記憶體直到呼叫端（kb_ingest）寫檔 ——
    所以文件很大時記得別同時解析太多份。
    """

    data: bytes
    ext: str = "png"                 # jpg / png / gif / webp …
    page_no: int | None = None
    bbox: dict | None = None         # PDF 座標（x0/top/x1/bottom），溯源用
    caption: str = ""                # ★ 題注（文件裡本來就有的圖說）
    name: str = ""                   # 原始資源名（Image25.jpg / image1.png）
    width: int = 0
    height: int = 0
    index: int = 0                   # 文件內序號（0 起）
    filtered_reason: str = ""        # 非空 = 被啟發式判為裝飾圖（仍會保留記錄）
    # ★ 綁定錨點：這張圖「屬於」第幾個 Block（由解析階段判定）。
    #   入庫時據此對應到涵蓋該 Block 的知識塊 —— 這是「圖必須綁到分塊」的依據。
    #   （DOCX 沒有頁碼，只能靠這個；PDF 有座標，可以配到最近的那一塊。）
    anchor_block: int | None = None

    @property
    def mime(self) -> str:
        e = (self.ext or "png").lower().lstrip(".")
        return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
                "webp": "image/webp", "bmp": "image/bmp"}.get(e, f"image/{e}")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data or b"").hexdigest()

    @property
    def public_name(self) -> str:
        """公開目錄裡的檔名（sha 前 16 碼 + 副檔名）—— 天然去重、無中文檔名問題。"""
        return f"{self.sha256[:16]}.{(self.ext or 'png').lstrip('.')}"

    @property
    def url(self) -> str:
        """對外網址（`/media/xxx.png`）。"""
        from app.rag import rag_config
        return rag_config.media_url(self.public_name)

    @property
    def markdown(self) -> str:
        """插進 chunk 文字的 markdown 圖片標記。

        ★ 命名用**題注**（「圖 3-2 請求流程」）；沒有題注就 `![](url)` —— 不亂標名字。
        alt 裡的 `]`、換行要先清掉，否則會把標記本身弄壞。
        """
        alt = re.sub(r"[\r\n]+", " ", (self.caption or "").strip())
        alt = alt.replace("]", "】").replace("[", "【")
        return f"![{alt}]({self.url})"


@dataclass
class ParsedDoc:
    blocks: list[Block] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    assets: list[ParsedAsset] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks)


def detect_type(filename: str = "", mime: str = "") -> str:
    """依副檔名判斷 doc_type；副檔名不認得才退回 mime。認不出回空字串。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _EXT_MAP:
        return _EXT_MAP[ext]
    m = (mime or "").lower()
    if "pdf" in m:
        return "pdf"
    if "word" in m or "officedocument.wordprocessing" in m:
        return "docx"
    if "sheet" in m or "excel" in m:
        return "xlsx"
    if "html" in m:
        return "html"
    if "csv" in m:
        return "csv"
    if m.startswith("text/"):
        return "txt"
    return ""


def parse(path: str, doc_type: str = "", filename: str = "") -> ParsedDoc:
    """解析檔案。失敗一律拋 ParseError（reason 為穩定錯誤碼）。"""
    name = filename or os.path.basename(path)
    dt = (doc_type or detect_type(name, "")).lower()
    if dt not in DOC_TYPES:
        raise ParseError("unsupported_format", f"不支援的檔案格式：{os.path.splitext(name)[1] or name}")

    if not os.path.exists(path):
        raise ParseError("file_missing", f"檔案不存在：{path}")

    try:
        if dt == "pdf":
            doc = _parse_pdf(path)
        elif dt == "docx":
            doc = _parse_docx(path)
        elif dt == "xlsx":
            doc = _parse_xlsx(path)
        elif dt == "md":
            doc = _parse_markdown(_read_text(path))
        elif dt == "csv":
            doc = _parse_csv(_read_text(path))
        elif dt == "html":
            doc = _parse_html(_read_text(path))
        else:
            doc = _parse_plain(_read_text(path))
    except ParseError:
        raise
    except Exception as e:  # 套件內部錯誤 → 轉成人話
        raise ParseError("parse_failed", f"解析失敗（{type(e).__name__}: {e}）") from e

    doc.meta["doc_type"] = dt
    doc.meta["filename"] = name
    doc.meta["block_count"] = len(doc.blocks)
    doc.meta["char_count"] = doc.char_count
    # 圖片統計（各 parser 會填更細的，這裡是共通的三個）
    doc.meta["asset_count"] = len(doc.assets)
    doc.meta["asset_filtered"] = len([a for a in doc.assets if a.filtered_reason])
    doc.meta["asset_with_caption"] = len([a for a in doc.assets if a.caption])

    if doc.char_count <= 0 or not any(b.text.strip() for b in doc.blocks):
        # 最常見的原因：掃描件（只有圖沒有文字層）
        extra = f"（偵測到 {len(doc.assets)} 張圖，但沒有文字層）" if doc.assets else ""
        raise ParseError("no_text_layer",
                         f"讀不到文字，這可能是掃描件（圖片型 PDF）{extra}")
    return doc


# ---------- 讀檔 ----------

def _read_text(path: str) -> str:
    """讀文字檔，容忍 BOM / 非 UTF-8（依序試 utf-8-sig → utf-8 → gbk → latin-1）。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------- 通用文字清理 ----------

_MULTI_BLANK = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = _MULTI_BLANK.sub("\n\n", t)
    return t.strip()


def _split_paragraphs(text: str) -> list[str]:
    """依空行切段；沒有空行的話，退化成依換行切。"""
    t = _clean(text)
    if not t:
        return []
    parts = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in t.split("\n") if p.strip()]
    return parts


# ---------- PDF：行級解析（不用 extract_text） ----------
#
# 為什麼不用 `page.extract_text()`：它只回一大坨文字，沒有座標，於是
#   ① 頁碼／頁眉／頁腳混進內文（直接污染檢索）
#   ② **每一頁自己成一段** → 跨頁的句子被硬切成兩段（實測見 RAG_DESIGN.md §6.2）
# 改用 pdfplumber 拿**行級物件**（text + top/bottom/x0/x1）後，這三件事都能處理。

# 句末標點（出現這些 → 這句說完了，不該跟下一行接）
_SENT_END = "。！？!?；;…"
# 允許跟在句末標點之後的收尾符號（「。」」這種）
_SENT_CLOSE = "」』）)】》\"'’"

# 段落起始特徵（行首比對）—— 命中就代表「這是新段落」，不接上一行
_PARA_START_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百千零〇\d]+[章節节條条款項项]"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|[（(][一二三四五六七八九十\d]+[）)]"
    r"|\d+(?:\.\d+)*[、.．\s]"
    r"|[•·※*]\s*"
    r"|附[錄录件表]"
    r"|抄\s*送"
    r")"
)

# ★ 編號式章節標題：「1. 产品概述」「3.2 完整安装步骤」「1.1.2 xxx」
#   ⚠️ 編號與標題文字之間**必須有空白** —— 這樣才不會把「11.0及以上」（版本號）
#      誤認成「11」+「.」+「0及以上」的章節標題（實測踩過）。
#   ⚠️ 但「1.1产品简介」（無空白）也會被排除 —— 這是刻意取捨：漏認一個標題
#      只是少一層路徑，誤認一個正文行則會切碎整份文件。
_NUM_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.、．]?\s+(\S.*)$")
# 標題裡不該出現的句中標點（有這些 → 是正文句子，不是標題）
_NUM_HEADING_BAD = "，。；：,;:！？、"
_NUM_HEADING_MAX = 24       # 標題字數上限（「1. 关闭电脑防火墙及杀毒软件拦截…」是列表項）
_HEAD_TAIL_MAX = 40         # 「標題自己換行」的續行長度上限（再長就是正文了）
# 第X章／第X節（這一條不受長度以外的限制）
_CN_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千零〇\d]+[章節节]\s*[\s\S]{0,20}$")

# 頁碼樣式。⚠️ 必須**同時**落在頁邊帶才算（否則表格裡的「1」會被誤殺）
_PAGENO_PATTERNS = (
    re.compile(r"^[\s\-–—_]*\d{1,4}[\s\-–—_]*$"),
    re.compile(r"^[\s\-–—_]*[ivxlcdmIVXLCDM]{1,7}[\s\-–—_]*$"),
    re.compile(r"^第\s*\d+\s*[页頁]\s*(?:[/／]\s*共?\s*\d+\s*[页頁])?$"),
    re.compile(r"^\d+\s*[/／]\s*\d+$"),
    re.compile(r"^(?:page|p\.)\s*\d+", re.I),
)

# 頁邊帶比例：上方 9% 內 = 頁眉帶；下方 14% 內 = 頁腳帶
# （實測教育部 PDF 的頁碼「— 2 —」在 762/842 = 90.5% 處）
_HF_TOP_RATIO = 0.09
_HF_BOTTOM_RATIO = 0.86
_HF_MIN_PAGES = 3          # 同一段文字在邊界帶出現 ≥ 3 頁 → 視為頁眉頁腳

# 題注樣式（圖表說明）—— 中英都要，對接手冊常混用
_CAPTION_RE = re.compile(
    r"^\s*(?:图|圖|表|figure|fig\.?|table|exhibit|chart|image|图片|圖片)"
    r"\s*[\d一二三四五六七八九十]+(?:\s*[-–—.．]\s*\d+)*"
    r"\s*[:：.、]?\s*(.+)$",
    re.I,
)

# 裝飾圖判斷門檻（寧可留下也不要誤刪 —— 保留下來只是多一張圖，誤刪是資料損失）
_MIN_PIXELS = (60, 40)          # 像素邊長下限
_EXTREME_RATIO = 8.0            # 長寬比 ≥ 8:1 → 分隔線那類
_MIN_AREA_RATIO = 0.012         # 顯示面積佔頁面 < 1.2% → 裝飾


def _ends_sentence(text: str) -> bool:
    """這一行是否以句末標點收尾（允許後面還有收尾引號／括號）。

    ⚠️ ASCII 句點要**另判**：`0.5`、`v1.2`、`192.168.1.1` 這種不是句末。
    規則：`.` 只有在「前一個字是英文字母」時才算句子結束（`warehouse.` ✅、`0.5` ❌）。
    """
    t = (text or "").rstrip()
    while t and t[-1] in _SENT_CLOSE:
        t = t[:-1].rstrip()
    if not t:
        return False
    if t[-1] in _SENT_END:
        return True
    return t[-1] == "." and len(t) >= 2 and t[-2].isalpha()


def _is_cjk(ch: str) -> bool:
    return bool(ch) and ("\u3400" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")


def _join_sep(prev: str, cur: str) -> str:
    """接續兩行時用什麼分隔：**中文不加**、英文加一個空格。

    ⚠️ 這裡刻意不用「一律加空格」——中文句子中間插一個空格會變成錯字級的問題。
    """
    a, b = (prev or "").rstrip(), (cur or "").lstrip()
    if not a or not b:
        return ""
    return "" if (_is_cjk(a[-1]) or _is_cjk(b[0])) else " "


def _img_ext(name: str, data: bytes) -> str:
    e = os.path.splitext(name or "")[1].lower().lstrip(".")
    if e in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
        return "jpg" if e == "jpeg" else e
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"


def _img_pixels(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def _in_margin_band(line: dict, page_h: float) -> str:
    """這一行落在頁眉 / 頁腳帶嗎？回 'top' / 'bottom' / ''。"""
    if not page_h:
        return ""
    if line.get("bottom", 0) <= page_h * _HF_TOP_RATIO:
        return "top"
    if line.get("top", 0) >= page_h * _HF_BOTTOM_RATIO:
        return "bottom"
    return ""


def _is_pageno(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 24:
        return False
    return any(p.match(t) for p in _PAGENO_PATTERNS)


def _looks_like_table(bbox: tuple, pg) -> bool:
    """這個 bbox 像不像真正的表格（濾掉誤判）。

    ⚠️ 「大到像整頁」的通常是版心框線被誤認成表格 —— 若照收，
    整頁正文都會被當成表格內容（那份文件就毀了）。
    """
    x0, top, x1, bottom = bbox
    if (bottom - top) < 20 or (x1 - x0) < 40:
        return False
    ph = float(getattr(pg, "height", 0) or 0)
    pw = float(getattr(pg, "width", 0) or 0)
    if ph and pw and ((bottom - top) * (x1 - x0)) > ph * pw * 0.90:
        return False
    return True


def _rows_to_markdown(rows: list) -> str:
    """表格的二維陣列 → markdown 表格（讓列／欄的關係保留下來）。

    ★ 為什麼要轉（2026-10-12，doc#319 實測）：PDF 的表格被 `extract_text_lines()`
    抽成一行行「視覺行」後會整團糊掉 ——
    `故障现象 报错代码 核心原因 解决方案` 後面接
    `软件安装完成后无 E001 安装路径包含中文 卸载软件，重新安法启动 或特殊符号 装至纯英文、无特`
    （「无法启动」被斷成「无」+「法启动」、「安装」被斷成兩半）。
    這種文字**檢索得到但讀不懂**，「E002 怎麼解」這種問題根本答不出來。
    轉成 markdown 後每列自成一列、每欄對齊，模型也看得懂。
    """
    def cell_text(c) -> str:
        """單元格文字：**儲存格內的換行是排版斷行，要接回同一句**。

        ⚠️ 中文之間不能補空格（用 `_join_sep`）—— 直接換成半角空格會變成
        「无 法启动」「重新安 装至纯英文」，明明是同一句話卻被拆開，
        檢索時「无法启动」就匹配不到了。
        """
        parts = [p.strip() for p in str(c or "").split("\n")]
        out = ""
        for p in parts:
            if not p:
                continue
            out = p if not out else (out + _join_sep(out, p) + p)
        return out.strip()

    clean: list[list[str]] = []
    for r in (rows or []):
        if not r:
            continue
        cells = [cell_text(c) for c in r]
        if any(cells):
            clean.append(cells)
    if len(clean) < 2:
        return ""
    width = max(len(r) for r in clean)
    if width < 2:                       # 只有一欄 → 不是表格（是誤判的框）
        return ""
    clean = [r + [""] * (width - len(r)) for r in clean]
    head, body = clean[0], clean[1:]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _page_tables(pg) -> list[tuple[tuple, str]]:
    """這一頁的表格：`[(bbox, markdown), …]`。

    pdfplumber 的 `find_tables()` 對「沒有框線、只有單元格底色」的表也抓得到
    （靠 rect 偵測），正好涵蓋這種排版。
    """
    out: list[tuple[tuple, str]] = []
    try:
        for t in (pg.find_tables() or []):
            bbox = tuple(float(v) for v in t.bbox)
            if not _looks_like_table(bbox, pg):
                continue
            try:
                md = _rows_to_markdown(t.extract())
            except Exception as e:
                log.debug("表格內容抽取失敗：%s", e)
                md = ""
            if md:
                out.append((bbox, md))
    except Exception as e:
        log.debug("表格偵測失敗（不影響解析）：%s", e)
    return out


def _in_bands(ln: dict, bands: list) -> bool:
    """這一行是否落在表格區域內（用行中線判斷）。"""
    if not bands:
        return False
    cy = (ln.get("top", 0) + ln.get("bottom", 0)) / 2.0
    return any(top - 1 <= cy <= bottom + 1 for (_x0, top, _x1, bottom) in bands)


def _hf_lines(pages: list[dict]) -> set[tuple[int, int]]:
    """找出所有頁眉／頁腳行的 `(page_no, line_index)`。

    兩個依據**並用**：
      ① 頁碼樣式 + 落在頁邊帶  —— 抓「— 2 —」「第 2 頁」這種
      ② 跨頁重複：同段文字在頁邊帶出現 ≥ `_HF_MIN_PAGES` 頁 —— 抓純文字頁眉
    只用①會漏掉純文字頁眉（公司名／文件名），只用②會漏掉只有兩頁的文件。
    """
    drop: set[tuple[int, int]] = set()
    counter: dict[str, set[int]] = {}

    for pg in pages:
        h = pg["h"]
        for li, ln in enumerate(pg["lines"]):
            if not _in_margin_band(ln, h):
                continue
            text = (ln.get("text") or "").strip()
            if not text:
                continue
            if _is_pageno(text):
                drop.add((pg["no"], li))
                continue
            key = re.sub(r"\s+", " ", text)[:80]
            counter.setdefault(key, set()).add(pg["no"])

    repeated = {k for k, ps in counter.items() if len(ps) >= _HF_MIN_PAGES}
    if repeated:
        for pg in pages:
            for li, ln in enumerate(pg["lines"]):
                if not _in_margin_band(ln, pg["h"]):
                    continue
                key = re.sub(r"\s+", " ", (ln.get("text") or "").strip())[:80]
                if key in repeated:
                    drop.add((pg["no"], li))
    return drop


def _median_gap(lines: list[dict]) -> float:
    """同一頁內相鄰行的中位行距（用來判斷「行距變大 = 段落間距」）。"""
    gaps = []
    for a, b in zip(lines, lines[1:]):
        g = b.get("top", 0) - a.get("bottom", 0)
        if 0 < g < 200:
            gaps.append(g)
    if not gaps:
        return 0.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def _body_left(lines: list[dict], page_w: float) -> float:
    """本文左邊界（取夠長的行，避開置中的標題）。"""
    xs = [ln.get("x0", 0) for ln in lines if len((ln.get("text") or "")) >= 8]
    if not xs:
        xs = [ln.get("x0", 0) for ln in lines]
    if not xs:
        return 0.0
    xs.sort()
    return xs[len(xs) // 2]


def _centered(ln: dict, page_w: float) -> bool:
    """左右留白都很大 → 看起來是置中排版。"""
    if not page_w:
        return False
    x0, x1 = ln.get("x0", 0), ln.get("x1", page_w)
    return x0 > page_w * 0.18 and (page_w - x1) > page_w * 0.18


def _looks_heading(ln: dict, page_w: float, prev: dict | None = None,
                   gap_med: float = 0.0, first_of_page: bool = False) -> bool:
    """這一行的排版看起來像不像章節標題。

    判準（依序，任一命中就定案）：
      ⓪ **表格區域內的行** → 永遠不是標題（表格欄位文字常置中或偏右，會被誤判）
      ① **題注**（「圖 3-2 請求流程」）→ 永遠不是標題 —— 它是圖的說明，
         由 `_match_caption()` 綁到圖上，不該進 heading_path
      ② **編號式章節標題**（「1. 产品概述」「3.2 完整安装步骤」）→ 是標題（左對齊也算）
      ③ 「第X章／第X節」→ 是標題
      ④ 其餘：**置中 + 短 + 不以句末標點收尾** → 標題

    ⚠️ ①② 為什麼一定要加（2026-10-12，實測 doc#319）：
      原先 ④ 是**唯一**判準（除了第X章特例）→ 這份文件的 4 個圖片題注
      （「圖 2 文件解压完成目录示意图」）與 7 個表格碎片（「径」「问」「密码登录」
      「PNG、JPG 析，不支持编辑」）全被判成標題，而 12 個真正的章節標題一個都沒認出。
      後果是 heading_path 被題注汙染 **51/74 個 block（69%）**，chunk 也跟著被切碎。
    """
    t = (ln.get("text") or "").strip()
    if not t or len(t) > 40 or _ends_sentence(t):
        return False
    if re.match(r"^[\d\s年月日./\-—:：()（）]+$", t):        # 日期／純數字行
        return False
    if ln.get("in_table"):                                   # ⓪ 表格欄位文字
        return False
    if _CAPTION_RE.match(t):                                 # ① 題注（圖/表說明）
        return False
    if _numbered_heading_level(t) or _CN_CHAPTER_RE.match(t):  # ② ③ 明確的章節標題
        return True
    # ⚠️ 續行不是標題：上一行沒把話說完、行距也正常 → 這行只是被排版斷開的續文
    #   （實測：「部内发送：…財務司、」被斷成下一行「政法司、职成司」而被誤判成標題）
    if prev is not None and not first_of_page:
        prev_t = (prev.get("text") or "").strip()
        gap = ln.get("top", 0) - prev.get("bottom", 0)
        if prev_t and not _ends_sentence(prev_t) and not (gap_med and gap > gap_med * 1.4):
            return False
    if len(t) < 6:                                           # ④ 太短的碎行不是標題
        return False
    return _centered(ln, page_w)


def _numbered_heading_level(text: str) -> int:
    """編號式章節標題的階層（「1. 产品概述」→1、「3.2 完整安装步骤」→2）；不是就 0。

    ★ 為什麼非有這條不可（2026-10-12，實測 doc#319）：
      編號標題**左對齊**，`_centered()` 抓不到 → 整份文件 12 個章節標題**一個都沒認出來**
      → `heading_path` 幾乎全是空的、`chunk_prefix` 只剩文件名。
      更糟的是它**不以句末標點結尾**，於是下一行被當成它的續行併進來，
      變成「3.3 首次登录初始化设置**软件首次打开需完成账号登录与基础配置：**」這種
      標題與正文黏成一行的方塊。認出標題後這兩件事一起解決
      （heading 分支會 flush → 下一行自然開新段）。
    """
    s = (text or "").strip()
    m = _NUM_HEADING_RE.match(s)
    if not m:
        return 0
    if len(s) > _NUM_HEADING_MAX:                 # 太長 → 是列表項／正文
        return 0
    if any(ch in s for ch in _NUM_HEADING_BAD):   # 含句中標點 → 是正文句子
        return 0
    if _ends_sentence(s):
        return 0
    # 「1」→1 層、「1.2」→2 層、「1.2.3」→3 層（再深就當 3，夠用）
    return min(m.group(1).count(".") + 1, 3)


def _heading_level_of(text: str) -> int:
    """章節標題的階層。"""
    t = (text or "").strip()
    if re.match(r"^第[一二三四五六七八九十百千零〇\d]+[章節节]", t):
        return 1
    return _numbered_heading_level(t)


def _should_join_across(prev: dict, cur: dict, prev_h: float, cur_h: float) -> bool:
    """上一頁最後一行 + 本頁第一行，是否屬於同一段（被分頁硬切）。

    ★ 這是「跨頁只是物理隔斷」的實作 —— 只有這裡回 True 才不產生段落分隔。
    """
    pt = (prev.get("text") or "").strip()
    ct = (cur.get("text") or "").strip()
    if not pt or not ct:
        return False
    if _ends_sentence(pt):                    # 上一頁已經把話說完
        return False
    if _PARA_START_RE.match(ct):              # 本行是「第X條 / 一、/ 1.」起頭
        return False
    if prev_h and prev.get("bottom", 0) < prev_h * 0.82:   # 上一行不在頁底 → 不是被切斷
        return False
    if cur_h and cur.get("top", 0) > cur_h * 0.22:         # 本行不在頁頂
        return False
    return True


def _is_new_para(ln: dict, prev: dict, gap_med: float, left_base: float) -> bool:
    """同一頁內，這一行是否開啟新段落。"""
    pt = (prev.get("text") or "").strip()
    ct = (ln.get("text") or "").strip()
    if not pt or not ct:
        return True
    if _PARA_START_RE.match(ct):                                  # 第X條 / 一、/ 1.
        return True
    # ★ 上一行是**題注**（「圖 2 文件解压完成目录示意图」）→ 這行一定是新段落。
    #   題注不以句末標點結尾，若只看那條會把下一句正文併上來，變成
    #   「圖 2 文件解压完成目录示意图步骤3：启动安装程序…」整句黏在一起。
    if _CAPTION_RE.match(pt):
        return True
    if _ends_sentence(pt):                                        # 上一行說完了
        return True
    if gap_med and (ln.get("top", 0) - prev.get("bottom", 0)) > gap_med * 1.5:
        return True                                               # 行距變大 = 段落間距
    if left_base and (ln.get("x0", 0) - left_base) > 18:           # 首行縮排（中文兩格）
        return True
    return False


def _lines_to_blocks(pages: list[dict], drop: set) -> tuple[list[Block], dict]:
    """行級物件 → Block（含跨頁接續與標題堆疊）。"""
    blocks: list[Block] = []
    buf = ""
    buf_page: int | None = None
    buf_y0: float | None = None
    buf_y1: float | None = None
    headings: list[str] = []
    heading_block: Block | None = None      # 最近一個標題塊（供「標題續行」併回去）
    prev: dict | None = None
    prev_h = 0.0
    stats = {"lines": 0, "dropped_hf": len(drop), "cross_page_joins": 0,
             "blocks": 0, "chars": 0}

    def flush():
        nonlocal buf, buf_page, buf_y0, buf_y1
        t = (buf or "").strip()
        if t:
            blocks.append(Block(t, page_no=buf_page, heading_path=" › ".join(headings),
                                y0=buf_y0, y1=buf_y1))
        buf, buf_page = "", None
        buf_y0 = buf_y1 = None

    for pg in pages:
        h, w = pg["h"], pg["w"]
        lines = [ln for li, ln in enumerate(pg["lines"]) if (pg["no"], li) not in drop]
        gap_med = _median_gap(lines)
        left_base = _body_left(lines, w)
        for li, ln in enumerate(lines):
            txt = (ln.get("text") or "").strip()
            if not txt:
                continue
            stats["lines"] += 1
            first_of_page = (li == 0)

            # ★ 表格：整塊自成一個 Block，**不參與**行合併／標題判定。
            #   它的文字已經是 markdown 表格（含換行），再被 `_join_sep` 併進來
            #   就會把表格攪爛。
            if ln.get("is_table"):
                flush()
                blocks.append(Block(txt, page_no=pg["no"],
                                    heading_path=" › ".join(headings),
                                    y0=ln.get("top"), y1=ln.get("bottom")))
                stats["tables"] = stats.get("tables", 0) + 1
                prev, prev_h = ln, h
                continue

            if _looks_heading(ln, w, prev, gap_med, first_of_page):
                flush()
                lvl = _heading_level_of(txt)
                headings = headings[:max(0, lvl - 1)] if lvl else headings[:0]
                headings.append(txt)
                heading_block = Block(txt, page_no=pg["no"],
                                      heading_path=" › ".join(headings), kind="heading",
                                      y0=ln.get("top"), y1=ln.get("bottom"))
                blocks.append(heading_block)
                prev, prev_h = ln, h
                continue

            # ★ 標題續行：標題自己也會換行（實測：「教育部关于印发《本科毕业论文（设计）」
            #   + 「抽检办法（试行）》的通知」）。這種續行要併回標題，否則會多出一個
            #   十幾個字的碎塊，章節路徑也只顯示半個標題。
            #   ⚠️ 必須排在「縮排判定」之前 —— 置中的續行 x0 一定比本文大，
            #   先跑縮排規則的話它會被當成新段落，就永遠併不回標題（踩過）。
            #   ⚠️ **續行必須短**（`_HEAD_TAIL_MAX`）：不然「1.1 产品简介」會把整段正文
            #   當成續行併進來，變成「1.1 产品简介本智能办公助手系统是面向…」這種
            #   標題＋正文黏成一坨的方塊（實測 doc#319 有 9 個這樣的塊）。
            prev_t = (prev.get("text") or "").strip() if prev else ""
            gap_now = (ln.get("top", 0) - prev.get("bottom", 0)) if prev else 0.0
            if (not buf and heading_block is not None and prev_t
                    and not _ends_sentence(prev_t)
                    and not _PARA_START_RE.match(txt)
                    and len(txt) <= _HEAD_TAIL_MAX
                    # ★ 續行不含句中標點 —— 這是最可靠的一條：標題續行是「半個標題」
                    #   （「抽检办法（试行）》的通知」），而正文續行一定有「，」「、」
                    #   （實測「本智能办公助手系统…，集成文件解析、智能检索」剛好 40 字，
                    #   只靠長度擋不住，仍會被併成「1.1 产品简介本智能…」）。
                    and not any(ch in txt for ch in _NUM_HEADING_BAD)
                    and not ln.get("in_table")        # 表格欄位不是標題的續行
                    and not (gap_med and gap_now > gap_med * 1.5)
                    and (_centered(ln, w) or abs(ln.get("x0", 0) - prev.get("x0", 0)) < w * 0.06)):
                heading_block.text += _join_sep(heading_block.text, txt) + txt
                if headings:
                    headings[-1] = heading_block.text
                prev, prev_h = ln, h
                continue

            if prev is None:
                start_new = True
            elif first_of_page:
                start_new = not _should_join_across(prev, ln, prev_h, h)
            else:
                start_new = _is_new_para(ln, prev, gap_med, left_base)

            if start_new:
                flush()
                buf, buf_page = txt, pg["no"]
                buf_y0, buf_y1 = ln.get("top"), ln.get("bottom")
            else:
                if first_of_page:
                    stats["cross_page_joins"] += 1
                if not buf:
                    # ⚠️ 緩衝區是空的（例如前面剛出現一個標題、或剛 flush 過）——
                    #    這行其實是「新段落的開頭」，頁碼／座標要在這裡補上，
                    #    否則這塊的 page_no 會是 None（圖片就綁不到它了）。
                    buf_page = pg["no"]
                    buf_y0 = ln.get("top")
                buf += _join_sep(buf, txt) + txt
                buf_y1 = ln.get("bottom")
            prev, prev_h = ln, h

    flush()
    stats["blocks"] = len(blocks)
    stats["chars"] = sum(len(b.text) for b in blocks)
    return blocks, stats


def _caption_candidates(lines: list[dict]) -> list[dict]:
    """這一頁裡看起來像題注的行。"""
    out = []
    for ln in lines:
        t = (ln.get("text") or "").strip()
        if not t:
            continue
        if _CAPTION_RE.match(t):
            out.append({"text": t, "top": ln.get("top", 0), "bottom": ln.get("bottom", 0)})
    return out


def _match_caption(cands: list[dict], bbox: dict | None, max_dist: float = 70.0) -> str:
    """圖片附近的題注（通常在下緣，少數在上緣）→ 取距離最近的那一條。"""
    if not cands:
        return ""
    if not bbox:
        return cands[0]["text"] if len(cands) == 1 else ""
    top, bottom = bbox.get("top", 0), bbox.get("bottom", 0)
    best, best_d = "", None
    for c in cands:
        d = (c["top"] - bottom) if c["top"] >= bottom else (top - c["bottom"])
        if d < 0:                     # 與圖重疊 → 大概是圖內文字，不是題注
            continue
        if d <= max_dist and (best_d is None or d < best_d):
            best, best_d = c["text"], d
    return best


def _decor_reason(a: ParsedAsset, page_w: float, page_h: float) -> str:
    """啟發式判斷「這是不是裝飾圖」。回空字串 = 看起來是內容圖。"""
    if a.width and a.height:
        if a.width < _MIN_PIXELS[0] or a.height < _MIN_PIXELS[1]:
            return f"太小（{a.width}×{a.height}px）"
        ratio = max(a.width / a.height, a.height / a.width)
        if ratio >= _EXTREME_RATIO:
            return f"長寬比極端（{ratio:.1f}:1）"
    if a.bbox and page_w and page_h:
        x0, x1 = a.bbox.get("x0", 0), a.bbox.get("x1", 0)
        tp, bt = a.bbox.get("top", 0), a.bbox.get("bottom", 0)
        dw, dh = abs(x1 - x0), abs(bt - tp)
        if (dw * dh) / (page_w * page_h) < _MIN_AREA_RATIO:
            return "面積過小"
        # 頁首的扁橫幅（紅頭文件那種「XX文件」抬頭）
        if (tp < page_h * 0.25 and dh < page_h * 0.12
                and dw > page_w * 0.5 and not a.caption):
            return "頁首扁橫幅（抬頭）"
    return ""


def _nearest_block(a: ParsedAsset, blocks: list[Block]) -> int | None:
    """這張圖最靠近哪一個 Block —— 「圖片必須綁到分塊」的依據。

    PDF 有座標 → 取同頁上「離圖最近」的那一塊（比塊的上緣與下緣，誰近算誰）；
    沒有座標（DOCX 之類）→ 退化成「同頁第一塊」。
    """
    if not blocks:
        return None
    if not a.bbox:
        for i, b in enumerate(blocks):
            if a.page_no is not None and b.page_no == a.page_no:
                return i
        return None
    itop, ibot = a.bbox.get("top", 0.0), a.bbox.get("bottom", 0.0)
    best: int | None = None
    best_d: float | None = None
    for i, b in enumerate(blocks):
        if a.page_no is not None and b.page_no != a.page_no:
            continue
        if b.y0 is None or b.y1 is None:
            continue
        # 圖在塊下方 → 比「圖上緣 vs 塊下緣」；圖在塊上方 → 比「圖下緣 vs 塊上緣」
        d = min(abs(b.y1 - itop), abs(ibot - b.y0))
        if best_d is None or d < best_d:
            best, best_d = i, d
    if best is None:
        for i, b in enumerate(blocks):
            if a.page_no is not None and b.page_no == a.page_no:
                return i
    return best


def _pdf_images(path: str, pages: list[dict]) -> list[ParsedAsset]:
    """抽 PDF 圖片：**座標用 pdfplumber、位元組用 pypdf**（兩者都已實測可用）。

    ⚠️ 兩邊的圖片清單是「同一頁內同序」的，所以按索引配對；
    數量對不上時就放棄座標（圖還是會留下，只是沒有 bbox 可溯源）。
    """
    assets: list[ParsedAsset] = []
    raw_by_page: dict[int, list[tuple[str, bytes]]] = {}
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        for i, page in enumerate(reader.pages, start=1):
            items: list[tuple[str, bytes]] = []
            try:
                for im in page.images:
                    items.append((getattr(im, "name", "") or "", im.data or b""))
            except Exception as e:
                log.debug("PDF 第 %d 頁抽圖失敗: %s", i, e)
            raw_by_page[i] = items
    except Exception as e:
        log.warning("PDF 圖片抽取失敗（只做文字）：%s", e)
        return assets

    idx = 0
    for pg in pages:
        items = raw_by_page.get(pg["no"], [])
        boxes = list(pg.get("images") or [])
        cands = _caption_candidates(pg["lines"])
        for j, (name, data) in enumerate(items):
            if not data:
                continue
            box = boxes[j] if j < len(boxes) else None
            bbox = None
            if box:
                bbox = {k: round(float(box.get(k) or 0), 1)
                        for k in ("x0", "top", "x1", "bottom")}
            w, h = _img_pixels(data)
            a = ParsedAsset(data=data, ext=_img_ext(name, data), page_no=pg["no"],
                            bbox=bbox, caption=_match_caption(cands, bbox),
                            name=name, width=w, height=h, index=idx)
            a.filtered_reason = _decor_reason(a, pg["w"], pg["h"])
            assets.append(a)
            idx += 1

    _mark_repeats(assets)
    return assets


def _mark_repeats(assets: list[ParsedAsset], min_count: int = 3) -> None:
    """同一張圖重複出現很多次 → 大概是 LOGO／浮水印（標記但仍保留記錄）。"""
    from collections import Counter
    counter = Counter(a.sha256 for a in assets)
    for a in assets:
        if not a.filtered_reason and counter[a.sha256] >= min_count:
            a.filtered_reason = f"重複出現 {counter[a.sha256]} 次（LOGO／浮水印？）"


def _docx_media_map(d) -> dict[str, tuple[str, bytes]]:
    """rId → (檔名, 位元組)：DOCX 的內嵌媒體（`word/media/*`）。"""
    out: dict[str, tuple[str, bytes]] = {}
    try:
        for rid, rel in d.part.rels.items():
            tp = getattr(rel, "target_part", None)
            ct = str(getattr(tp, "content_type", "") or "")
            if not ct.startswith("image/"):
                continue
            pn = str(getattr(tp, "partname", "") or "")
            out[str(rid)] = (pn.rsplit("/", 1)[-1], tp.blob)
    except Exception as e:
        log.debug("DOCX 讀內嵌媒體失敗: %s", e)
    return out


def _parse_pdf(path: str) -> ParsedDoc:
    """PDF → 帶座標的 Block + 圖片資產。"""
    import pdfplumber

    pages: list[dict] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, pg in enumerate(pdf.pages, start=1):
                lines: list[dict] = []
                tables = _page_tables(pg)     # 表格（bbox + markdown）
                bands = [b for b, _md in tables]
                try:
                    for ln in pg.extract_text_lines() or []:
                        t = _clean(ln.get("text") or "")
                        if not t:
                            continue
                        item = {"text": t, "top": float(ln.get("top") or 0),
                                "bottom": float(ln.get("bottom") or 0),
                                "x0": float(ln.get("x0") or 0),
                                "x1": float(ln.get("x1") or 0)}
                        item["in_table"] = _in_bands(item, bands)
                        lines.append(item)
                except Exception as e:
                    log.debug("PDF 第 %d 頁取行失敗: %s", i, e)
                # ★ 表格區域的那些「視覺行」整批換成一段 markdown 表格：
                #   原本的糊成一團（見 `_rows_to_markdown` 的說明），
                #   換掉之後表格列／欄的關係才保得住。
                if tables:
                    free = [ln for ln in lines if not ln.get("in_table")]
                    for bbox, md in tables:
                        free.append({"text": md, "top": bbox[1], "bottom": bbox[3],
                                     "x0": bbox[0], "x1": bbox[2],
                                     "in_table": True, "is_table": True})
                    free.sort(key=lambda x: (x.get("top", 0), x.get("x0", 0)))
                    lines = free
                pages.append({"no": i, "h": float(pg.height or 0), "w": float(pg.width or 0),
                              "lines": lines, "images": list(getattr(pg, "images", None) or [])})
    except Exception as e:
        # pdfplumber 掛掉（加密／損壞）→ 轉成人話
        msg = str(e)
        if "encrypt" in msg.lower() or "password" in msg.lower():
            raise ParseError("encrypted", "PDF 有密碼保護，請先解除密碼再上傳") from e
        raise ParseError("parse_failed", f"PDF 解析失敗（{type(e).__name__}: {e}）") from e

    drop = _hf_lines(pages)
    blocks, stats = _lines_to_blocks(pages, drop)
    assets = _pdf_images(path, pages)
    for a in assets:                       # 綁定錨點（圖片 → 最近的塊）
        a.anchor_block = _nearest_block(a, blocks)

    meta = {
        "pages": len(pages),
        "page_blocks": len(blocks),
        "dropped_header_footer": stats["dropped_hf"],
        "cross_page_joins": stats["cross_page_joins"],
        "assets": len(assets),
        "assets_filtered": len([a for a in assets if a.filtered_reason]),
        "assets_with_caption": len([a for a in assets if a.caption]),
    }
    try:
        from pypdf import PdfReader
        info = PdfReader(path).metadata or {}
        if info.get("/Title"):
            meta["pdf_title"] = str(info["/Title"])
    except Exception:
        pass
    return ParsedDoc(blocks, meta, assets)


# ---------- DOCX ----------

def _docx_paragraph(node, doc):
    from docx.text.paragraph import Paragraph
    return Paragraph(node, doc)


def _docx_table(node, doc):
    from docx.table import Table
    return Table(node, doc)


def _heading_level(style_name: str) -> int:
    """從樣式名判斷標題階層（相容中英文 Word：「Heading 1」/「標題 1」）。"""
    s = (style_name or "").strip().lower()
    if s in ("title", "标题", "標題"):
        return 0
    m = re.match(r"^(?:heading|標題|标题)\s*(\d+)$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(?:heading|標題|标题)(\d+)$", s)
    if m:
        return int(m.group(1))
    return 0


def _table_to_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = []
        for c in row.cells:
            cells.append(_clean(c.text).replace("\n", " "))
        if any(x for x in cells):
            rows.append(cells)
    return rows


def _parse_docx(path: str) -> ParsedDoc:
    import docx as _docx
    from docx.oxml.ns import qn

    d = _docx.Document(path)
    body = d.element.body
    blocks: list[Block] = []
    assets: list[ParsedAsset] = []
    headings: list[str] = []          # 目前的標題堆疊（依階層）
    table_count = 0
    rel_map = _docx_media_map(d)
    children = list(body.iterchildren())

    def _ptext(ch) -> str:
        if ch is None or ch.tag != qn("w:p"):
            return ""
        try:
            return _clean(_docx_paragraph(ch, d).text)
        except Exception:
            return ""

    def _collect_images(node, own_text: str, next_texts: list[str]) -> None:
        """收下 node 底下的圖片（含題注與錨點）。

        DOCX 沒有頁碼，所以錨點取「目前最後一個 Block」—— 圖片通常緊接在
        它的說明文字之後；題注優先取「同段落的文字」，其次才是後面幾段。
        """
        for blip in node.findall(".//" + qn("a:blip")):
            rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
            item = rel_map.get(str(rid)) if rid else None
            if not item:
                continue
            name, data = item
            w, h = _img_pixels(data)
            cap = own_text if _CAPTION_RE.match(own_text) else ""
            if not cap:
                for t2 in next_texts:
                    if t2:
                        if _CAPTION_RE.match(t2):
                            cap = t2
                        break
            a = ParsedAsset(data=data, ext=_img_ext(name, data), caption=cap,
                            name=name, width=w, height=h, index=len(assets),
                            anchor_block=(len(blocks) - 1 if blocks else 0))
            a.filtered_reason = _decor_reason(a, 0.0, 0.0)
            assets.append(a)

    for ci, child in enumerate(children):
        if child.tag == qn("w:p"):
            p = _docx_paragraph(child, d)
            txt = _clean(p.text)
            _collect_images(child, txt,
                            [_ptext(children[j]) for j in range(ci + 1, min(ci + 4, len(children)))])
            if not txt:
                continue
            lvl = _heading_level(getattr(p.style, "name", "") or "")
            if lvl:
                headings = headings[:max(0, lvl - 1)] if lvl > 0 else []
                headings.append(txt)
                blocks.append(Block(txt, heading_path=" › ".join(headings), kind="heading"))
            else:
                blocks.append(Block(txt, heading_path=" › ".join(headings)))
        elif child.tag == qn("w:tbl"):
            _collect_images(child, "", [])      # 表格裡也可能放圖
            tbl = _docx_table(child, d)
            rows = _table_to_rows(tbl)
            if not rows:
                continue
            table_count += 1
            header = rows[0]
            # 每列轉成「欄名：值；欄名：值」——讓 embedding 抓到語義
            for ri, r in enumerate(rows[1:], start=2):
                pairs = []
                for ci2, val in enumerate(r):
                    col = header[ci2] if ci2 < len(header) and header[ci2] else f"欄{ci2 + 1}"
                    if val:
                        pairs.append(f"{col}：{val}")
                if pairs:
                    blocks.append(Block("；".join(pairs),
                                        heading_path=" › ".join(headings),
                                        row_from=ri, row_to=ri, kind="table_row"))

    _mark_repeats(assets, min_count=5)         # DOCX 沒有頁碼，用「出現次數」
    return ParsedDoc(blocks, {"paragraphs": len([b for b in blocks if b.kind == "text"]),
                              "tables": table_count,
                              "headings": len([b for b in blocks if b.kind == "heading"])},
                     assets)


# ---------- XLSX ----------

def _rows_to_blocks(header: list[str], rows: list[list[str]], sheet: str,
                    heading_path: str = "") -> list[Block]:
    """每個資料列 → 「欄名：值」句；呼叫端再把連續列併成塊。"""
    out: list[Block] = []
    for ri, r in enumerate(rows, start=2):
        pairs = []
        for ci, val in enumerate(r):
            if not str(val).strip():
                continue
            col = header[ci] if ci < len(header) and header[ci] else f"欄{ci + 1}"
            pairs.append(f"{col}：{val}")
        if pairs:
            out.append(Block("；".join(pairs), sheet=sheet, row_from=ri, row_to=ri,
                             heading_path=heading_path, kind="table_row"))
    return out


def _parse_xlsx(path: str) -> ParsedDoc:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    blocks: list[Block] = []
    sheets_meta = []
    try:
        for ws in wb.worksheets:
            grid: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    grid.append(cells)
            if not grid:
                sheets_meta.append({"name": ws.title, "rows": 0})
                continue
            header = [h or f"欄{i + 1}" for i, h in enumerate(grid[0])]
            body = grid[1:]
            blocks += _rows_to_blocks(header, body, ws.title)
            sheets_meta.append({"name": ws.title, "rows": len(body), "cols": len(header)})
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return ParsedDoc(blocks, {"sheets": sheets_meta})


# ---------- CSV / TSV ----------

def _parse_csv(text: str) -> ParsedDoc:
    if not text.strip():
        return ParsedDoc([], {"rows": 0})
    sample = text[:4096]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    grid = [[("" if c is None else str(c).strip()) for c in row] for row in reader]
    grid = [r for r in grid if any(r)]
    if not grid:
        return ParsedDoc([], {"rows": 0})
    header = [h or f"欄{i + 1}" for i, h in enumerate(grid[0])]
    blocks = _rows_to_blocks(header, grid[1:], sheet="", heading_path="")
    return ParsedDoc(blocks, {"rows": len(grid) - 1, "cols": len(header), "delimiter": delim})


# ---------- Markdown ----------

_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")


def _parse_markdown(text: str) -> ParsedDoc:
    lines = _clean(text).split("\n")
    blocks: list[Block] = []
    headings: list[str] = []
    buf: list[str] = []
    in_code = False
    fence = ""

    def flush():
        if not buf:
            return
        body = "\n".join(buf).strip()
        buf.clear()
        if body:
            blocks.append(Block(body, heading_path=" › ".join(headings),
                                 kind="code" if in_code else "text"))

    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if not in_code:
                flush()
                in_code = True
                fence = stripped[:3]
            elif stripped.startswith(fence):
                in_code = False
                flush()
            else:
                buf.append(ln)
            continue
        if in_code:
            buf.append(ln)
            continue
        m = _MD_HEAD.match(stripped)
        if m and not in_code:
            flush()
            lvl = len(m.group(1))
            headings = headings[:lvl - 1]
            headings.append(m.group(2).strip())
            continue
        if not stripped:
            flush()          # 空行結束一段
            continue
        buf.append(ln)
    flush()
    return ParsedDoc(blocks, {"headings": len(headings), "code_blocks":
                              len([b for b in blocks if b.kind == "code"])})


# ---------- 純文字 ----------

def _parse_plain(text: str) -> ParsedDoc:
    return ParsedDoc([Block(p) for p in _split_paragraphs(text)], {})


# ---------- HTML ----------

class _HtmlText(HTMLParser):
    """極簡 HTML → 結構化文字：取標題階層 + 段落 + 表格列。"""

    _SKIP = {"script", "style", "noscript", "head", "meta", "link"}
    _BLOCK = {"p", "div", "section", "article", "li", "tr", "br", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.headings: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0
        self._heading_lvl = 0
        self._in_cell = False
        self._cells: list[str] = []
        self._cell_buf: list[str] = []
        self._row: list[str] = []

    def _flush(self, kind: str = "text"):
        txt = _clean(" ".join("".join(self._buf).split()))
        self._buf.clear()
        if txt:
            self.blocks.append(Block(txt, heading_path=" › ".join(self.headings), kind=kind))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self._heading_lvl = int(tag[1])
        elif tag == "td" or tag == "th":
            self._in_cell = True
            self._cell_buf = []
        elif tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            txt = _clean(" ".join("".join(self._buf).split()))
            self._buf.clear()
            if txt:
                lvl = self._heading_lvl or 1
                self.headings = self.headings[:lvl - 1]
                self.headings.append(txt)
        elif tag == "td" or tag == "th":
            self._row.append(_clean(" ".join("".join(self._cell_buf).split())))
            self._cell_buf = []
            self._in_cell = False
        elif tag == "tr":
            cells = [c for c in self._row]
            self._row = []
            if any(cells):
                pairs = [f"{cells[0]}：{c}" for c in cells[1:] if c] if len(cells) > 1 else [cells[0]]
                self.blocks.append(Block("；".join(pairs),
                                         heading_path=" › ".join(self.headings), kind="table_row"))
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_buf.append(data)
        else:
            self._buf.append(data)


def _parse_html(text: str) -> ParsedDoc:
    p = _HtmlText()
    p.feed(text)
    p._flush()
    return ParsedDoc(p.blocks, {"html_blocks": len(p.blocks)})


def describe_error(reason: str) -> str:
    """錯誤碼 → 給客服看的人話（RAG_DESIGN.md §13：失敗只有三種文案）。"""
    return {
        "no_text_layer": "讀不到文字，可能是掃描件（圖片型 PDF），目前不支援 OCR",
        "unsupported_format": "格式不支援（支援 PDF / Word / Excel / Markdown / TXT / CSV / HTML）",
        "encrypted": "PDF 有密碼保護，請先解除密碼再上傳",
        "file_missing": "檔案遺失，請重新上傳",
        "parse_failed": "解析失敗，請確認檔案沒有損壞後重試",
    }.get(reason, "解析失敗，請稍後重試或改用其他格式")
