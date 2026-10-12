"""Excel / CSV 解析與範本產生。

只用標準庫 csv + openpyxl，不引入 pandas。
支援 .xlsx / .xlsm 與 .csv（UTF-8 優先，失敗時依次嘗試 utf-8-sig → big5 → cp950 → gbk）。
"""
import csv
import io
import logging

log = logging.getLogger("wechat-forwarder")

_ENCODINGS = ("utf-8-sig", "utf-8", "big5", "cp950", "gbk", "latin-1")

# CSV 匯入大小上限（位元組），避免誤傳大檔
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


class SpreadsheetError(Exception):
    """檔案無法解析（格式、編碼、內容為空）。"""


# ---------- 讀取 ----------

def read_rows(filename: str, raw: bytes) -> list[list[str]]:
    """把上傳的檔案讀成二維字串表（含標題列）。"""
    if not raw:
        raise SpreadsheetError("檔案是空的")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise SpreadsheetError(f"檔案過大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）")

    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(raw)
    if name.endswith(".xls"):
        raise SpreadsheetError("不支援舊版 .xls，請另存為 .xlsx 或 .csv 後再上傳")
    return _read_csv(raw)


def _read_xlsx(raw: bytes) -> list[list[str]]:
    import openpyxl  # 延遲匯入：只有真的上傳 Excel 才需要

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        raise SpreadsheetError(f"Excel 檔無法開啟：{e}") from e

    try:
        ws = wb[wb.sheetnames[0]]
        out: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            out.append([_cell(v) for v in row])
        return _trim(out)
    finally:
        wb.close()


def _read_csv(raw: bytes) -> list[list[str]]:
    text = None
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise SpreadsheetError("CSV 編碼無法辨識，請另存為 UTF-8")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[_cell(v) for v in row] for row in csv.reader(io.StringIO(text), dialect)]
    return _trim(rows)


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """去掉全空列與列尾空欄，避免 Excel 的幽靈列。"""
    out = []
    for r in rows:
        while r and not r[-1]:
            r.pop()
        if any(c for c in r):
            out.append(r)
    return out


# ---------- 標題正規化 ----------

def norm_header(h: str) -> str:
    """標題正規化：去 BOM、去空白、全形括號轉半形、轉小寫。"""
    s = (h or "").replace("\ufeff", "").strip().lower()
    for a, b in (("（", "("), ("）", ")"), ("　", ""), (" ", ""), ("_", ""), ("-", "")):
        s = s.replace(a, b)
    return s


def header_map(headers: list[str]) -> dict[str, int]:
    """回傳 {正規化標題: 欄索引}。"""
    return {norm_header(h): i for i, h in enumerate(headers)}


def pick(headers: list[str], aliases: tuple[str, ...]) -> int:
    """在多個候選標題名中找出第一個命中的欄索引，找不到回 -1。"""
    m = header_map(headers)
    for a in aliases:
        idx = m.get(norm_header(a))
        if idx is not None:
            return idx
    return -1


# ---------- FAQ ----------

FAQ_HEADERS = ["問題", "答案", "關鍵詞(選填,逗號分隔)", "啟用(是/否)"]

_FAQ_Q = ("問題", "问题", "question", "q", "問", "問法", "關鍵詞", "关键词", "keywords")
_FAQ_A = ("答案", "answer", "a", "回答", "回覆", "回复", "内容", "內容")
_FAQ_K = ("關鍵詞(選填,逗號分隔)", "關鍵詞", "关键词", "keywords", "keyword", "同義詞", "同义词", "別名")
_FAQ_E = ("啟用(是/否)", "啟用", "启用", "enabled", "enable", "狀態", "状态", "是否啟用")


def parse_faq_rows(rows: list[list[str]]) -> list[dict]:
    """把二維表轉成 FAQ 清單。第一個非空列視為標題列。"""
    if not rows:
        raise SpreadsheetError("檔案沒有內容")
    headers = rows[0]
    qi = pick(headers, _FAQ_Q)
    ai = pick(headers, _FAQ_A)
    if qi < 0 or ai < 0:
        # 沒有標題列時，容忍「第一欄問題、第二欄答案」的兩欄格式
        if len(headers) >= 2 and not any(norm_header(h) in
                                        [norm_header(x) for x in _FAQ_A] for h in headers):
            qi, ai = 0, 1
            body = rows
        else:
            raise SpreadsheetError("找不到「問題」與「答案」欄位，請參考範本檔的第一列標題")
    else:
        body = rows[1:]

    ki = pick(headers, _FAQ_K)
    ei = pick(headers, _FAQ_E)

    items, seen = [], set()
    for r in body:
        q = r[qi].strip() if qi < len(r) else ""
        a = r[ai].strip() if ai < len(r) else ""
        if not q or not a:
            continue
        # 問題+答案完全相同視為重複（同一份表內去重）
        key = (q, a)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "question": q,
            "answer": a,
            "keywords": (r[ki].strip() if 0 <= ki < len(r) else ""),
            "enabled": parse_bool(r[ei].strip() if 0 <= ei < len(r) else "", default=True),
        })
    if not items:
        raise SpreadsheetError("沒有解析到任何有效的問答對（問題與答案都不可為空）")
    return items


# ---------- 業務表格 ----------

def parse_grid(rows: list[list[str]]) -> tuple[list[str], list[dict]]:
    """把二維表轉成 (欄位清單, 資料列清單)。

    第一列為欄位名，其餘為資料。欄位名重複時自動加序號。
    """
    if not rows:
        raise SpreadsheetError("檔案沒有內容")
    headers = [h.strip() for h in rows[0]]
    if not any(headers):
        raise SpreadsheetError("第一列必須是欄位名稱")

    # 補齊空白欄名並去重
    columns: list[str] = []
    for i, h in enumerate(headers):
        name = h or f"欄位{i + 1}"
        base, n = name, 2
        while name in columns:
            name = f"{base}_{n}"
            n += 1
        columns.append(name)

    out = []
    for r in rows[1:]:
        data = {}
        for i, col in enumerate(columns):
            val = r[i].strip() if i < len(r) else ""
            if val:
                data[col] = val
        if data:
            out.append(data)
    if not out:
        raise SpreadsheetError("只有標題列，沒有資料列")
    return columns, out


def table_context(headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[dict]]:
    """舊介面相容：等價於 parse_grid。"""
    return parse_grid([headers] + rows)


# ---------- 布林 ----------

_TRUE = {"是", "y", "yes", "true", "1", "啟用", "启用", "on", "t"}
_FALSE = {"否", "n", "no", "false", "0", "停用", "禁用", "off", "f"}


def parse_bool(value: str, default: bool = True) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return default
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


# ---------- 產生範本 ----------

def to_csv_bytes(headers: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    # 加 UTF-8 BOM，Excel 直開不亂碼
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def to_xlsx_bytes(headers: list[str], rows: list[list[str]], sheet_title: str = "Sheet1") -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title[:31] or "Sheet1"

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1A7F5C")
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for r, row in enumerate(rows, start=2):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v)

    for c, h in enumerate(headers, start=1):
        width = max(10, min(60, max([len(str(h))] + [len(str(r[c - 1])) for r in rows if c - 1 < len(r)] or [10]) + 4))
        ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width = width
    ws.freeze_panes = "A2"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


FAQ_TEMPLATE_ROWS = [
    ["你們的營業時間是幾點？", "我們的營業時間是週一至週日 9:00 至 21:00，例假日照常營業。",
     "營業時間,幾點開門,開到幾點", "是"],
    ["怎麼退貨？", "請在訂單頁點擊「申請退貨」，7 天內可無理由退貨，退款將於 3-5 個工作天原路退回。",
     "退貨,退款,換貨", "是"],
    ["配送需要多久？", "一般下單後 24 小時內出貨，配送時間視地區約 2-5 個工作天。",
     "運費,多久到,物流", "是"],
]

TABLE_TEMPLATE_ROWS = [
    ["產品名稱", "價格", "庫存", "規格"],
    ["A 款藍牙耳機", "1280", "36", "白色 / 黑色"],
    ["B 款行動電源", "890", "12", "10000mAh"],
]
