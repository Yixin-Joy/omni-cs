"""知識庫文件解析：把各種格式轉成「帶結構 metadata 的文字區塊」。

支援：pdf / docx / xlsx / md / txt / csv / html

輸出的 `Block` 保留來源座標（頁碼 / 標題階層 / 工作表 / 列範圍），
讓後面分塊、溯源、客服「命中詳情」都能顯示「這段話是從哪來的」。
設計依據：RAG_DESIGN.md §6。

⚠️ 掃描型 PDF（沒有文字層）**不靜默通過**：解析出 0 字元時直接拋
`ParseError("no_text_layer")`，由呼叫端把文件標成 failed 並給客服人話提示。
"""
from __future__ import annotations

import csv
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
    kind: str = "text"          # text | table_row | code | sheet_meta

    def clone_text(self, text: str) -> "Block":
        return Block(text, self.page_no, self.heading_path, self.sheet,
                     self.row_from, self.row_to, self.kind)


@dataclass
class ParsedDoc:
    blocks: list[Block] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

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

    if doc.char_count <= 0 or not any(b.text.strip() for b in doc.blocks):
        # 最常見的原因：掃描件（只有圖沒有文字層）
        raise ParseError("no_text_layer", "讀不到文字，這可能是掃描件（圖片型 PDF）")
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


# ---------- PDF ----------

def _parse_pdf(path: str) -> ParsedDoc:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")     # 空密碼（很多 PDF 只是設定權限密碼）
        except Exception:
            raise ParseError("encrypted", "PDF 有密碼保護，請先解除密碼再上傳")

    blocks: list[Block] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as e:
            log.debug("PDF 第 %d 頁抽字失敗: %s", i, e)
            continue
        for para in _split_paragraphs(raw):
            blocks.append(Block(para, page_no=i))

    meta = {"pages": len(reader.pages), "page_blocks": len(blocks)}
    try:
        info = reader.metadata or {}
        if info.get("/Title"):
            meta["pdf_title"] = str(info["/Title"])
    except Exception:
        pass
    return ParsedDoc(blocks, meta)


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

    d = _docx.Document(path)
    body = d.element.body
    blocks: list[Block] = []
    headings: list[str] = []          # 目前的標題堆疊（依階層）
    heading_levels: list[int] = []
    table_count = 0

    from docx.oxml.ns import qn

    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            p = _docx_paragraph(child, d)
            txt = _clean(p.text)
            if not txt:
                continue
            lvl = _heading_level(getattr(p.style, "name", "") or "")
            if lvl:
                headings = headings[:max(0, lvl - 1)] if lvl > 0 else []
                heading_levels = heading_levels[:max(0, lvl - 1)] if lvl > 0 else []
                headings.append(txt)
                heading_levels.append(lvl)
                blocks.append(Block(txt, heading_path=" › ".join(headings), kind="heading"))
            else:
                blocks.append(Block(txt, heading_path=" › ".join(headings)))
        elif child.tag == qn("w:tbl"):
            tbl = _docx_table(child, d)
            rows = _table_to_rows(tbl)
            if not rows:
                continue
            table_count += 1
            header = rows[0]
            # 每列轉成「欄名：值；欄名：值」——讓 embedding 抓到語義
            for ri, r in enumerate(rows[1:], start=2):
                pairs = []
                for ci, val in enumerate(r):
                    col = header[ci] if ci < len(header) and header[ci] else f"欄{ci + 1}"
                    if val:
                        pairs.append(f"{col}：{val}")
                if pairs:
                    blocks.append(Block("；".join(pairs),
                                        heading_path=" › ".join(headings),
                                        row_from=ri, row_to=ri, kind="table_row"))

    return ParsedDoc(blocks, {"paragraphs": len([b for b in blocks if b.kind == "text"]),
                              "tables": table_count,
                              "headings": len([b for b in blocks if b.kind == "heading"])})


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
