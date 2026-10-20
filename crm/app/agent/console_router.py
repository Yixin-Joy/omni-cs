"""客服工作台「配置與檢視」端點 —— 啟用集、開關、FAQ／表格／知識庫清單、紀錄、命中測試。

薄薄一層：資料邏輯都在已 business 化的 `autoreply_db` / `kb_db` / `reply_engine` 裡。
（舊單體帶過來的 `autoreply_console.py` / `kb_console.py` 仍是舊 API、尚未接線，
見 `docs/MIGRATION_PLAN.md` 階段四待辦。）

- 讀取：`require_agent`；變更：`require_csrf` + `require_agent`。
- 每個端點都先做 `rbac.can_access_business`（不靠前端隱藏 UI）。
- 編輯 FAQ／表格／知識庫內容需 `is_kb_editor`（設計案 §8.2）。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app import audit
from app.agent import repo
from app.auth import deps, rbac
from app.autoreply import autoreply_db
from app.core import config
from app.internal import connector_client
from app.llm import reply_engine
from app.rag import kb_db
from app.utils import xlsx_io

log = logging.getLogger("crm")

router = APIRouter(prefix="/console/agent", tags=["agent-console"])

PLATFORMS = tuple(config.CHANNEL_LABELS.keys())
BINDING_KINDS = ("faq", "table", "kb")
LAYER_LABEL = {"faq": "FAQ", "table": "業務表格", "kb": "知識庫", "rag": "知識庫"}
# 只有這兩個平台有「貼文留言」場景（§9.3）
COMMENT_PLATFORMS = tuple(config.AUTOREPLY_COMMENT_CHANNELS)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


def _biz_guard(sess: dict, business_id: int):
    if not rbac.can_access_business(sess, business_id):
        return _err(403, "沒有該商家的權限")


def _kb_edit_guard(sess: dict):
    if not rbac.can_edit_kb_content(sess):
        return _err(403, "需要知識庫編輯權限（agent + is_kb_editor）")


# ================================================================ 概覽

def _effective(business_id: int, platform: str, scope: str, bp_account_id: int) -> bool:
    """兩層 AND 的實際結果（設計案 §9.1）。"""
    if not autoreply_db.effective_scope_mode(int(business_id), platform, scope):
        return False
    return autoreply_db.platform_account_enabled(int(bp_account_id))


@router.get("/overview")
def overview(business_id: int = Query(...), sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    bid = int(business_id)

    scopes = []
    for plat, scope in config.AUTOREPLY_SCOPES:
        scopes.append({"platform": plat, "scope": scope,
                       "enabled": autoreply_db.effective_scope_mode(bid, plat, scope)})

    accounts = []
    for a in repo.list_accounts(bid):
        accounts.append({**a,
                         "ai_enabled": autoreply_db.platform_account_enabled(int(a["id"]))})

    counts = repo.business_summary(bid)
    counts["row"] = len(autoreply_db.list_rows(enabled_only=False))
    bindings = {k: autoreply_db.list_binding_ids(bid, k) for k in BINDING_KINDS}

    return {
        "ok": True,
        "business_id": bid,
        "scopes": scopes,
        "layers": autoreply_db.all_layer_switches(bid),
        "accounts": accounts,
        "counts": counts,
        "bindings": bindings,
        "can_edit_kb": rbac.can_edit_kb_content(sess),
    }


# ================================================================ 開關 / 綁定

class SwitchIn(BaseModel):
    business_id: int
    platform: str = ""
    scope: str = ""
    enabled: bool = False


@router.post("/scope")
def set_scope(payload: SwitchIn, sess: dict = Depends(deps.require_csrf)):
    """第一層：商家 × 平台 × 場景 的 AI 總開關。"""
    if not rbac.can_configure_binding(sess):
        return _err(403, "需要客服坐席權限")
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    if payload.platform not in PLATFORMS or (payload.platform, payload.scope) not in config.AUTOREPLY_SCOPES:
        return _err(400, "平台或場景不合法")
    autoreply_db.set_scope_mode(int(payload.business_id), payload.platform, payload.scope,
                                bool(payload.enabled))
    reply_engine.invalidate_cache()
    audit.log_standalone(sess, "ai_switch_change", business_id=int(payload.business_id),
                         platform=payload.platform,
                         payload={"scope": payload.scope, "enabled": bool(payload.enabled),
                                  "layer": "scope"})
    return {"ok": True}


class AccountAIIn(BaseModel):
    business_id: int
    bp_account_id: int
    enabled: bool = False


@router.post("/account-ai")
def set_account_ai(payload: AccountAIIn, sess: dict = Depends(deps.require_csrf)):
    """第二層：平台帳號層的 AI 開關。"""
    if not rbac.can_configure_binding(sess):
        return _err(403, "需要客服坐席權限")
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    ids = {int(a["id"]) for a in repo.list_accounts(int(payload.business_id))}
    if int(payload.bp_account_id) not in ids:
        return _err(400, "平台帳號不屬於該商家")
    autoreply_db.set_platform_account_ai(int(payload.bp_account_id), bool(payload.enabled))
    audit.log_standalone(sess, "ai_switch_change", business_id=int(payload.business_id),
                         bp_account_id=int(payload.bp_account_id),
                         payload={"enabled": bool(payload.enabled), "layer": "account"})
    return {"ok": True}


class LayerIn(BaseModel):
    business_id: int
    layer: str
    enabled: bool = False


@router.post("/layer")
def set_layer(payload: LayerIn, sess: dict = Depends(deps.require_csrf)):
    """商家的層開關（FAQ / 表格 / 知識庫）。"""
    if not rbac.can_configure_binding(sess):
        return _err(403, "需要客服坐席權限")
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    if payload.layer not in reply_engine.ALL_LAYERS:
        return _err(400, f"不支援的層: {payload.layer}")
    reply_engine.set_layer_enabled(int(payload.business_id), payload.layer, bool(payload.enabled))
    audit.log_standalone(sess, "ai_switch_change", business_id=int(payload.business_id),
                         payload={"layer": payload.layer, "enabled": bool(payload.enabled)})
    return {"ok": True}


class BindingIn(BaseModel):
    business_id: int
    kind: str
    item_id: int
    enabled: bool = False


@router.post("/binding")
def set_binding(payload: BindingIn, sess: dict = Depends(deps.require_csrf)):
    """基準啟用集的勾選（★ 空集＝該層不跑）。"""
    if not rbac.can_configure_binding(sess):
        return _err(403, "需要客服坐席權限")
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    if payload.kind not in BINDING_KINDS:
        return _err(400, f"不支援的類別: {payload.kind}")
    autoreply_db.set_binding(int(payload.business_id), payload.kind, int(payload.item_id),
                             bool(payload.enabled))
    reply_engine.invalidate_cache()
    audit.log_standalone(sess, "binding_change", business_id=int(payload.business_id),
                         payload={"kind": payload.kind, "item_id": int(payload.item_id),
                                  "enabled": bool(payload.enabled)})
    return {"ok": True}


# ================================================================ 貼文特殊配置（§9.3）

def _candidates(business_id: int) -> dict:
    """該商家三層的**全部**可勾項目（含未啟用的；未啟用一樣能勾，只是跑起來也不會有料）。"""
    bid = int(business_id)
    faqs = [{"id": int(f["id"]), "title": f.get("question") or f"FAQ#{f['id']}",
             "enabled": bool(f.get("enabled", True))}
            for f in autoreply_db.list_faqs(bid, enabled_only=False)]
    tables = [{"id": int(t["id"]), "title": t.get("name") or f"表格#{t['id']}",
               "enabled": bool(t.get("enabled", True))}
              for t in autoreply_db.list_tables(bid, enabled_only=False)]
    colls = []
    for c in kb_db.list_collections(business_id=bid, enabled_only=False):
        colls.append({"id": int(c["id"]), "title": c.get("name") or f"知識庫#{c['id']}",
                      "kind": c.get("kind") or "doc",
                      "enabled": bool(c.get("enabled", True))})
    return {"faq": faqs, "table": tables, "kb": colls}


@router.get("/post-config")
def get_post_config(business_id: int = Query(...), platform: str = Query(...),
                    post_id: str = Query(...), bp_account_id: int = Query(default=0),
                    sess: dict = Depends(deps.require_agent)):
    """一則貼文的特殊配置彈窗資料 —— 候選、預設勾選、三組開關。

    ★ **沒配置過（configured=false）＝與私訊完全相同**：勾選預設就是私訊基準集
      （`base`），彈窗打開直接看到的就是「這則貼文現在實際會用什麼」。
    """
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    plat = (platform or "").strip()
    pid = (post_id or "").strip()
    if plat not in COMMENT_PLATFORMS:
        return _err(400, f"{plat or '(空)'} 沒有貼文留言場景")
    if not pid:
        return _err(400, "缺少 post_id")

    bid = int(business_id)
    base = autoreply_db.business_scope_set(bid)
    cfg = repo.get_post_config(plat, pid)
    exists = cfg is not None
    if cfg and cfg.get("configured"):
        picked = {k: repo.post_binding_ids(int(cfg["id"]), k) for k in BINDING_KINDS}
        layers = {"faq": bool(cfg["faq_enabled"]), "table": bool(cfg["table_enabled"]),
                  "rag": bool(cfg["kb_enabled"])}
    else:
        picked = {k: list(base.get(k) or []) for k in BINDING_KINDS}
        layers = dict(base.get("layers") or {})
        layers.setdefault("rag", True)

    return {
        "ok": True,
        "platform": plat, "post_id": pid, "bp_account_id": int(bp_account_id or 0),
        "exists": exists,
        "configured": bool(cfg and cfg.get("configured")),
        "enabled": bool(cfg["enabled"]) if cfg else True,
        "updated_at": (cfg or {}).get("updated_at"),
        # 該商家該平台的「留言」總開關（關了就整場不跑 AI → 前端不顯示入口）
        "comment_switch_on": autoreply_db.effective_scope_mode(bid, plat, "comment"),
        "layers": layers,
        "base": {k: list(base.get(k) or []) for k in BINDING_KINDS},
        "picked": picked,
        "candidates": _candidates(bid),
        "can_configure": rbac.can_configure_binding(sess),
    }


class PostConfigIn(BaseModel):
    business_id: int
    platform: str
    bp_account_id: int = 0
    post_id: str = ""
    enabled: bool = True
    layers: dict | None = None
    bindings: dict | None = None
    remove: bool = False


def _norm_id_list(v) -> list[int]:
    if not isinstance(v, list):
        return []
    out: list[int] = []
    for x in v:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in out:
            out.append(i)
    return out


@router.post("/post-config")
def save_post_config(payload: PostConfigIn, sess: dict = Depends(deps.require_csrf)):
    """儲存／移除一則貼文的特殊配置。

    ★ 鐵則：**開關開著就不能空勾** —— 「這層要跑，但一項都沒勾」等於用一個
      使用者以為已經停用的空集合去回答，比不回覆更糟（§9.2）。
    """
    if not rbac.can_configure_binding(sess):
        return _err(403, "需要客服坐席權限")
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    plat = (payload.platform or "").strip()
    pid = (payload.post_id or "").strip()
    if plat not in COMMENT_PLATFORMS:
        return _err(400, f"{plat or '(空)'} 沒有貼文留言場景")
    if not pid:
        return _err(400, "缺少 post_id")
    bid = int(payload.business_id)

    if payload.remove:
        ok = repo.delete_post_config(plat, pid)
        reply_engine.invalidate_cache()
        audit.log_standalone(sess, "post_config_change", business_id=bid, platform=plat,
                             payload={"post_id": pid, "removed": True, "removed_ok": ok})
        return {"ok": True, "removed": ok}

    cand = _candidates(bid)
    allowed = {k: {c["id"] for c in cand[k]} for k in BINDING_KINDS}
    bindings = {k: _norm_id_list((payload.bindings or {}).get(k)) for k in BINDING_KINDS}
    for k in BINDING_KINDS:
        unknown = [i for i in bindings[k] if i not in allowed[k]]
        if unknown:
            return _err(400, f"有項目不屬於該商家（{k}: {unknown}）")

    raw_layers = payload.layers if isinstance(payload.layers, dict) else {}
    layers = {"faq": bool(raw_layers.get("faq", True)),
              "table": bool(raw_layers.get("table", True)),
              "rag": bool(raw_layers.get("kb", raw_layers.get("rag", True)))}

    if payload.enabled:
        empty = [LAYER_LABEL[k] for k in ("faq", "table")
                 if layers[k] and not bindings[k]]
        if layers["rag"] and not bindings["kb"]:
            empty.append(LAYER_LABEL["rag"])
        if empty:
            return _err(400, "、".join(empty) + " 的開關是開的，至少要勾一個項目（或把開關關掉）")

    res = repo.save_post_config(
        sess, business_id=bid, platform=plat, bp_account_id=int(payload.bp_account_id or 0),
        post_id=pid, enabled=bool(payload.enabled), layers=layers, bindings=bindings)
    reply_engine.invalidate_cache()
    audit.log_standalone(sess, "post_config_change", business_id=bid, platform=plat,
                         bp_account_id=int(payload.bp_account_id or 0),
                         payload={"post_id": pid, "enabled": bool(payload.enabled),
                                  "layers": layers,
                                  "bindings": {k: len(v) for k, v in bindings.items()}})
    return {"ok": True, **res}


# ================================================================ 清單

@router.get("/faqs")
def faqs(business_id: int = Query(...), q: str = Query(default=""),
         sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    items = autoreply_db.list_faqs(int(business_id), q=q)
    # ★ 同時回 `faqs` / `total` 兩個別名 —— 工作台前端是從舊版 console 整包移植的，
    #   舊端點就是這個形狀；給別名省掉一整批「這裡讀 items、那裡讀 faqs」的分支。
    return {"ok": True, "items": items, "faqs": items, "total": len(items)}


@router.get("/tables")
def tables(business_id: int = Query(...), rows: int = Query(default=1),
           sess: dict = Depends(deps.require_agent)):
    """列出該商家的業務表格。

    ★ 每一張都附上 `rows`（資料列）與 `row_count` —— 舊 console 就是這個形狀
      （工作台前端整包移植過來，展開資料列時直接讀 `t.rows`，不再另外打一次 API）。
      `rows=0` 可省掉資料列（只要清單時用）。
    """
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    items = autoreply_db.list_tables(int(business_id))
    by_table: dict[int, list[dict]] = {}
    if rows:
        for tid in [int(t["id"]) for t in items]:
            by_table[tid] = autoreply_db.list_rows(tid)
    for t in items:
        t["rows"] = by_table.get(int(t["id"]), []) if rows else []
        t["row_count"] = len(by_table.get(int(t["id"]), []))
    return {"ok": True, "items": items, "tables": items, "total": len(items)}


@router.get("/collections")
def collections(business_id: int = Query(...), sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    return {"ok": True, "items": kb_db.list_collections(business_id=int(business_id))}


# ================================================================ FAQ 編輯（塊C）

def _refresh_engine() -> None:
    """改完資料要讓引擎快取失效，否則要等 TTL 才生效。"""
    try:
        reply_engine.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass


class FaqIn(BaseModel):
    business_id: int
    question: str = ""
    answer: str = ""
    keywords: str = ""
    enabled: bool = True


@router.post("/faq")
def faq_create(payload: FaqIn, sess: dict = Depends(deps.require_csrf)):
    """新增 FAQ（該商家）。"""
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    q = (payload.question or "").strip()
    a = (payload.answer or "").strip()
    if not q or not a:
        return _err(400, "問題與答案都不可為空")
    row = autoreply_db.create_faq(int(payload.business_id), q, a,
                                  (payload.keywords or "").strip(), bool(payload.enabled))
    _refresh_engine()
    audit.log_standalone(sess, "faq_create", business_id=int(payload.business_id),
                         payload={"faq_id": row.get("id") if isinstance(row, dict) else None})
    return {"ok": True, "faq": row}


class FaqUpdateIn(BaseModel):
    business_id: int
    id: int
    question: str | None = None
    answer: str | None = None
    keywords: str | None = None
    enabled: bool | None = None


@router.post("/faq-update")
def faq_update(payload: FaqUpdateIn, sess: dict = Depends(deps.require_csrf)):
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    fields = {k: getattr(payload, k) for k in ("question", "answer", "keywords", "enabled")
              if getattr(payload, k) is not None}
    if not fields:
        return _err(400, "沒有可更新欄位")
    if not autoreply_db.update_faq(int(payload.id), fields):
        return _err(404, "找不到該 FAQ")
    # ★ 設定頁已無「啟用集」區塊 → FAQ 子頁的「啟用」是唯一入口。
    #   實際生效 = business_faq_binding.enabled AND autoreply_faq.enabled，
    #   只改一邊會讓另一邊變成看不見的障礙（子頁顯示啟用卻不生效）→ 一起改。
    if "enabled" in fields:
        autoreply_db.set_binding(int(payload.business_id), "faq", int(payload.id),
                                 bool(fields["enabled"]))
    _refresh_engine()
    return {"ok": True}


class FaqDeleteIn(BaseModel):
    business_id: int
    ids: list[int] = []


@router.post("/faq-delete")
def faq_delete(payload: FaqDeleteIn, sess: dict = Depends(deps.require_csrf)):
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    ids = [int(i) for i in (payload.ids or [])]
    if not ids:
        return _err(400, "缺少 ids")
    # 只刪屬於該商家的（避免跨商家刪除）
    owned = {int(f["id"]) for f in autoreply_db.list_faqs(int(payload.business_id))}
    n = 0
    for i in ids:
        if i in owned and autoreply_db.delete_faq(i):
            n += 1
    _refresh_engine()
    return {"ok": True, "deleted": n}


# ================================================================ 業務表格編輯（塊C）

@router.get("/table")
def table_detail(business_id: int = Query(...), table_id: int = Query(...),
                 sess: dict = Depends(deps.require_agent)):
    """單張業務表格 + 其資料列。"""
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    t = next((x for x in autoreply_db.list_tables(int(business_id)) if int(x["id"]) == int(table_id)), None)
    if not t:
        return _err(404, "找不到該表")
    t["rows"] = autoreply_db.list_rows(int(table_id))
    return {"ok": True, "table": t}


class TableIn(BaseModel):
    business_id: int
    name: str = ""
    description: str = ""
    columns: list[str] = []
    enabled: bool = True


@router.post("/table-create")
def table_create(payload: TableIn, sess: dict = Depends(deps.require_csrf)):
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    name = (payload.name or "").strip()
    if not name:
        return _err(400, "請填表名")
    cols = [str(c).strip() for c in (payload.columns or []) if str(c).strip()]
    t = autoreply_db.create_table(int(payload.business_id), name,
                                  (payload.description or "").strip(), cols, bool(payload.enabled))
    _refresh_engine()
    return {"ok": True, "table": t}


class TableUpdateIn(BaseModel):
    business_id: int
    id: int
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    columns: list[str] | None = None


@router.post("/table-update")
def table_update(payload: TableUpdateIn, sess: dict = Depends(deps.require_csrf)):
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    owned = {int(x["id"]) for x in autoreply_db.list_tables(int(payload.business_id))}
    if int(payload.id) not in owned:
        return _err(404, "找不到該表")
    fields = {k: getattr(payload, k) for k in ("name", "description", "enabled", "columns")
              if getattr(payload, k) is not None}
    if "columns" in fields:
        fields["columns"] = [str(c).strip() for c in (fields["columns"] or []) if str(c).strip()]
    if not fields or not autoreply_db.update_table(int(payload.id), fields):
        return _err(404, "沒有可更新欄位或找不到該表")
    # ★ 同 FAQ：表格子頁的「啟用」是唯一入口，連動基準集以免出現隱形障礙
    if "enabled" in fields:
        autoreply_db.set_binding(int(payload.business_id), "table", int(payload.id),
                                 bool(fields["enabled"]))
    _refresh_engine()
    return {"ok": True}


class TableDeleteIn(BaseModel):
    business_id: int
    id: int


@router.post("/table-delete")
def table_delete(payload: TableDeleteIn, sess: dict = Depends(deps.require_csrf)):
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    owned = {int(x["id"]) for x in autoreply_db.list_tables(int(payload.business_id))}
    if int(payload.id) not in owned:
        return _err(404, "找不到該表")
    autoreply_db.delete_table(int(payload.id))
    _refresh_engine()
    return {"ok": True}


class TableRowIn(BaseModel):
    business_id: int
    table_id: int
    action: str = "create"          # create | update | delete
    id: int | None = None
    data: dict | None = None
    enabled: bool | None = None


@router.post("/table-row")
def table_row(payload: TableRowIn, sess: dict = Depends(deps.require_csrf)):
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    owned = {int(x["id"]) for x in autoreply_db.list_tables(int(payload.business_id))}
    if int(payload.table_id) not in owned:
        return _err(404, "找不到該表")
    action = (payload.action or "create").lower()
    if action == "create":
        data = {k: str(v) for k, v in (payload.data or {}).items()}
        if not any(str(v).strip() for v in data.values()):
            return _err(400, "資料列不可全空")
        row = autoreply_db.create_row(int(payload.table_id), data)
        _refresh_engine()
        return {"ok": True, "row": row}
    if action == "update":
        if not payload.id:
            return _err(400, "缺少 id")
        data = None if payload.data is None else {k: str(v) for k, v in payload.data.items()}
        if not autoreply_db.update_row(int(payload.id), data, payload.enabled):
            return _err(404, "找不到該列")
        _refresh_engine()
        return {"ok": True}
    if action == "delete":
        if not payload.id:
            return _err(400, "缺少 id")
        if not autoreply_db.delete_row(int(payload.id)):
            return _err(404, "找不到該列")
        _refresh_engine()
        return {"ok": True}
    return _err(400, "action 必須是 create / update / delete")


# ================================================================ FAQ／表格 匯入匯出（Excel / CSV）

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _attachment(data: bytes, filename: str, mime: str) -> Response:
    """下載附件（檔名用 RFC 5987 編碼 —— 中文檔名才不會變亂碼）。"""
    from urllib.parse import quote
    return Response(
        content=data, media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _json_faqs(text: str) -> list[dict]:
    """貼上的 JSON → FAQ items（支援中英欄名）。"""
    import json as _json
    data = _json.loads(text)
    if isinstance(data, dict):
        data = data.get("faqs") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError("JSON 需為陣列")
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or it.get("問題") or it.get("问题") or "").strip()
        a = str(it.get("answer") or it.get("答案") or "").strip()
        if not q or not a:
            continue
        out.append({"question": q, "answer": a,
                    "keywords": str(it.get("keywords") or it.get("關鍵詞") or "").strip(),
                    "enabled": xlsx_io.parse_bool(str(it.get("enabled", "")), default=True)})
    if not out:
        raise ValueError("沒有解析到任何有效的問答對")
    return out


def _json_table(text: str) -> tuple[list[str], list[dict]]:
    """貼上的 JSON → (欄位, 資料列)。"""
    import json as _json
    data = _json.loads(text)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("data") or []
    if not isinstance(data, list) or not data:
        raise ValueError("JSON 需為非空陣列")
    columns: list[str] = []
    for it in data:
        if isinstance(it, dict):
            for k in it:
                if str(k) not in columns:
                    columns.append(str(k))
    rows = [{str(k): str(v) for k, v in it.items()} for it in data if isinstance(it, dict)]
    if not columns or not rows:
        raise ValueError("沒有解析到任何資料列")
    return columns, rows


@router.post("/faq-import")
async def faq_import(business_id: int = Form(...), mode: str = Form("replace"),
                     file: UploadFile | None = File(None), text: str = Form(""),
                     sess: dict = Depends(deps.require_csrf)):
    """匯入 FAQ（Excel / CSV / 貼上 JSON）。mode = replace（先清空該商家）或 append。"""
    bad = _biz_guard(sess, business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    mode = (mode or "replace").lower()
    if mode not in ("replace", "append"):
        return _err(400, "mode 必須是 replace 或 append")
    try:
        if file is not None:
            raw = await file.read()
            items = xlsx_io.parse_faq_rows(xlsx_io.read_rows(file.filename or "", raw))
        elif text.strip():
            items = _json_faqs(text)
        else:
            return _err(400, "請選擇檔案或提供 JSON 內容")
    except xlsx_io.SpreadsheetError as e:
        return _err(400, str(e))
    except Exception as e:  # noqa: BLE001
        return _err(400, f"解析失敗：{e}")

    result = autoreply_db.replace_faqs(int(business_id), items, mode=mode)
    _refresh_engine()
    audit.log_standalone(sess, "faq_import", business_id=int(business_id),
                         payload={"mode": mode, **{k: v for k, v in result.items()}})
    return {"ok": True, **result,
            "total": len(autoreply_db.list_faqs(int(business_id)))}


@router.get("/faq-export")
def faq_export(business_id: int = Query(...), sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    faqs = autoreply_db.list_faqs(int(business_id))
    rows = [[f["question"], f["answer"], f.get("keywords") or "",
             "是" if f["enabled"] else "否"] for f in faqs]
    data = xlsx_io.to_xlsx_bytes(xlsx_io.FAQ_HEADERS, rows, "FAQ")
    return _attachment(data, "faq.xlsx", _XLSX_MIME)


def _template(headers: list[str], rows: list[list[str]], title: str, fmt: str) -> Response:
    if (fmt or "xlsx").lower() == "csv":
        return _attachment(xlsx_io.to_csv_bytes(headers, rows),
                           f"{title}_template.csv", "text/csv; charset=utf-8")
    return _attachment(xlsx_io.to_xlsx_bytes(headers, rows, title),
                       f"{title}_template.xlsx", _XLSX_MIME)


@router.get("/faq-template")
def faq_template(fmt: str = Query(default="xlsx"), sess: dict = Depends(deps.require_agent)):
    return _template(xlsx_io.FAQ_HEADERS, xlsx_io.FAQ_TEMPLATE_ROWS, "FAQ", fmt)


@router.post("/table-import")
async def table_import(business_id: int = Form(...), table_id: int = Form(0),
                       name: str = Form(""), description: str = Form(""),
                       mode: str = Form("replace"),
                       file: UploadFile | None = File(None), text: str = Form(""),
                       sess: dict = Depends(deps.require_csrf)):
    """匯入業務表格。`table_id=0` → 依第一列欄位名新建一張表；否則覆寫／附加。"""
    bad = _biz_guard(sess, business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    mode = (mode or "replace").lower()
    if mode not in ("replace", "append"):
        return _err(400, "mode 必須是 replace 或 append")
    try:
        if file is not None:
            raw = await file.read()
            columns, rows = xlsx_io.parse_grid(xlsx_io.read_rows(file.filename or "", raw))
        elif text.strip():
            columns, rows = _json_table(text)
        else:
            return _err(400, "請選擇檔案或提供 JSON 內容")
    except xlsx_io.SpreadsheetError as e:
        return _err(400, str(e))
    except Exception as e:  # noqa: BLE001
        return _err(400, f"解析失敗：{e}")

    mine = autoreply_db.list_tables(int(business_id))
    if table_id:
        t = next((x for x in mine if int(x["id"]) == int(table_id)), None)
        if t is None:
            return _err(404, "找不到該表")
        if columns and columns != [str(c) for c in (t.get("columns") or [])]:
            autoreply_db.update_table(int(t["id"]), {"columns": columns})
        n = (autoreply_db.replace_rows(int(t["id"]), rows) if mode == "replace"
             else autoreply_db.append_rows(int(t["id"]), rows))
        _refresh_engine()
        return {"ok": True, "table_id": int(t["id"]), "written": n,
                "columns": columns, "mode": mode}

    t = autoreply_db.create_table(int(business_id), name.strip() or "業務表格",
                                  (description or "").strip(), columns)
    n = autoreply_db.replace_rows(int(t["id"]), rows)
    _refresh_engine()
    return {"ok": True, "table_id": int(t["id"]), "written": n,
            "columns": columns, "table": t}


@router.get("/table-export")
def table_export(business_id: int = Query(...), table_id: int = Query(default=0),
                 sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    mine = autoreply_db.list_tables(int(business_id))
    if table_id:
        tables = [t for t in mine if int(t["id"]) == int(table_id)]
    else:
        tables = mine
    if not tables:
        return _err(404, "找不到該表")
    t = tables[0]
    columns = [str(c) for c in (t.get("columns") or [])]
    rows = autoreply_db.list_rows(int(t["id"]))
    if not columns and rows:
        for r in rows:
            for k in (r.get("data") or {}):
                if k not in columns:
                    columns.append(k)
    body = [[str((r.get("data") or {}).get(c, "")) for c in columns] for r in rows]
    data = xlsx_io.to_xlsx_bytes(columns, body, t["name"] or "業務表格")
    return _attachment(data, f"{t['name'] or 'business_table'}.xlsx", _XLSX_MIME)


@router.get("/table-template")
def table_template(fmt: str = Query(default="xlsx"), sess: dict = Depends(deps.require_agent)):
    rows = xlsx_io.TABLE_TEMPLATE_ROWS
    return _template(rows[0], rows[1:], "業務表格", fmt)


class TableRowsBulkDeleteIn(BaseModel):
    business_id: int
    ids: list[int] = []


@router.post("/table-row-bulk-delete")
def table_row_bulk_delete(payload: TableRowsBulkDeleteIn,
                          sess: dict = Depends(deps.require_csrf)):
    """批次刪資料列（只刪屬於該商家的表底下的列）。"""
    bad = _biz_guard(sess, payload.business_id) or _kb_edit_guard(sess)
    if bad:
        return bad
    ids = [int(i) for i in (payload.ids or [])]
    if not ids:
        return _err(400, "缺少 ids")
    owned_tables = {int(x["id"]) for x in autoreply_db.list_tables(int(payload.business_id))}
    owned_rows = {int(r["id"]) for tid in owned_tables
                  for r in autoreply_db.list_rows(tid)}
    safe = [i for i in ids if i in owned_rows]
    n = autoreply_db.delete_rows_bulk(safe)
    _refresh_engine()
    return {"ok": True, "deleted": n}


@router.get("/stats")
def stats(business_id: int = Query(...), sess: dict = Depends(deps.require_agent)):
    """命中情況統計（紀錄頁的「命中情況統計」子頁）。"""
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    biz = int(business_id)
    summary = autoreply_db.log_summary(biz)
    miss = autoreply_db.miss_counts() or {}
    scopes = []
    for channel, scope in config.AUTOREPLY_SCOPES:
        key = autoreply_db.scope_key(channel, scope)
        s = summary.get(key, {"faq": 0, "table": 0, "rag": 0, "miss": 0, "error": 0,
                              "rewritten": 0, "total": 0})
        scopes.append({
            "channel": channel, "scope": scope, "label": config.scope_label(channel, scope),
            "enabled": autoreply_db.effective_scope_mode(biz, channel, scope),
            "pending_miss": miss.get(key, 0),
            **s,
        })
    return {"ok": True, "scopes": scopes,
            "miss_total": sum(miss.values()),
            "rewritten_total": sum(int(s.get("rewritten") or 0) for s in summary.values())}


# ================================================================ 紀錄

@router.get("/logs")
def logs(business_id: int = Query(...), platform: str = Query(default=""),
         scope: str = Query(default=""), tier: str = Query(default=""),
         limit: int = Query(default=100), offset: int = Query(default=0),
         sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    return {"ok": True, **autoreply_db.list_logs(int(business_id), platform=platform,
                                                 scope=scope, tier=tier,
                                                 limit=int(limit), offset=int(offset))}


@router.get("/log")
def log_detail(business_id: int = Query(...), id: int = Query(...),
               sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    row = autoreply_db.get_log(int(id))
    if not row or int(row.get("business_id") or 0) != int(business_id):
        return _err(404, "找不到紀錄")
    # ★ `item` 之外再攤平一次（`matched_rows` / `detail` 等）—— 舊 console 的
    #   `/logs/<id>` 是直接回那筆紀錄本身，前端讀 `d.matched_rows`。
    return {"ok": True, "item": row, **row}


# ================================================================ 命中測試

class TestIn(BaseModel):
    business_id: int
    platform: str = "whatsapp"
    scope: str = "dm"
    text: str = ""
    layers: list[str] | None = None
    post_config_id: int = 0
    # ★ 與舊 console 的 `/test/engine` 對齊（命中測試頁要能模擬真實情境）：
    history: list[dict] | None = None      # 自備對話（[{direction,text},…]）
    with_history: bool = True              # 沒自備時，自動撈最近對話
    query_rewrite: bool | None = None      # 只覆寫這次；不動 DB 參數
    sender_id: str = ""
    comment_id: str = ""


@router.post("/test")
async def test_engine(payload: TestIn, sess: dict = Depends(deps.require_agent)):
    """命中測試（設計案 §14）：**只跑指定層、不寫任何紀錄、不動開關**。"""
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    text = (payload.text or "").strip()
    if not text:
        return _err(400, "請輸入要測試的問句")
    if payload.platform not in PLATFORMS:
        return _err(400, "平台不合法")
    if (payload.platform, payload.scope) not in config.AUTOREPLY_SCOPES:
        return _err(400, "場景不合法")
    layers = payload.layers or None
    if layers:
        bad_layers = [x for x in layers if x not in reply_engine.ALL_LAYERS]
        if bad_layers:
            return _err(400, f"不支援的層: {'、'.join(bad_layers)}")
        layers = [x for x in reply_engine.ALL_LAYERS if x in layers]

    # ★ 沒自備對話就自動撈最近對話（等同真實流程）——舊版就是這個行為，
    #   少了它「帶對話改寫問句」這一塊在測試頁永遠試不出來。
    history = payload.history
    if history is None and payload.with_history:
        try:
            history = await asyncio.to_thread(
                autoreply_db.get_conversation_history, payload.platform, payload.scope,
                sender_id=payload.sender_id, comment_id=payload.comment_id,
                message_id="", current_text=text,
                limit=config.TABLE_REWRITE_HISTORY_TURNS)
        except Exception as e:  # noqa: BLE001 —— 取不到對話不該讓測試失敗
            log.warning("命中測試取對話失敗（改用無對話）: %s", e)
            history = []
    history = history or []

    ss = await asyncio.to_thread(
        autoreply_db.resolve_scope_set, int(payload.business_id),
        scope=payload.scope, post_config_id=int(payload.post_config_id))
    ans = await reply_engine.answer(
        text, business_id=int(payload.business_id), channel=payload.platform,
        scope=payload.scope, history=history, layers=layers,
        query_rewrite=payload.query_rewrite, scope_set=ss)
    # ★ 回傳形狀與舊版 `/test/engine` 一致（前端是整包移植過來的）：
    #   as_dict() 已含 tier/text/score/detail/reason/matched_rows/rag_* /rewrote/…，
    #   再加 would_send / history / layers / query_rewrite 與相容別名。
    out = {"ok": True, **ans.as_dict(),
           "would_send": bool(ans.text),
           "history": history, "layers": layers, "query_rewrite": payload.query_rewrite,
           # 相容別名（舊前端讀這幾個名字）
           "answer": ans.text, "matched": ans.matched_rows,
           "scope_set": {"faq": len(ss.get("faq") or []), "table": len(ss.get("table") or []),
                         "kb": len(ss.get("kb") or []), "source": ss.get("source"),
                         "layers": ss.get("layers")}}
    return out


# ================================================================ 由引擎產生草稿（右鍵）

class DraftIn(BaseModel):
    business_id: int
    platform: str
    scope: str = "dm"
    text: str = ""              # 被右鍵那一則的原文（前端一定有）
    message_id: str = ""        # 私訊：錨定「就回這一則」
    comment_id: str = ""        # 留言
    conversation_key: str = ""  # 私訊：會話鍵（＝end_user_sender_id）
    post_config_id: int = 0     # 貼文留言：帶入該貼文的特殊配置


@router.post("/draft")
async def draft_reply(payload: DraftIn, sess: dict = Depends(deps.require_agent)):
    """★ 工作台右鍵「由自動回復引擎給出回覆」——**只產生草稿、不發送**。

    與自動回復正式流程的差別（與舊單體一致）：
      * **不檢查自動回復開關**（關著也要能用），**不套冷卻與每日上限**（人工判斷優先）
      * 命中時**不寫任何標記或日誌**；等客服真的按送出，才由回覆端點標成
        「人工確認的引擎回覆」（`autoreply.mark_manual_reply`）
      * 未命中時寫一筆 `manual_no_match` 紀錄 —— 紀錄頁才看得出「這一則人工試過、答不出來」

    走同一套四層引擎（FAQ → 業務表格 → 知識庫 → 人工），所以在命中測試頁看到的結果
    與這裡一致。**回傳只給前端填進回覆框，不會自動送出。**
    """
    bad = _biz_guard(sess, payload.business_id)
    if bad:
        return bad
    plat = (payload.platform or "").strip()
    if plat not in PLATFORMS:
        return _err(400, f"不支援的平台: {plat}")
    scope = (payload.scope or "dm").strip()
    if (plat, scope) not in config.AUTOREPLY_SCOPES:
        return _err(400, f"不支援的場景: {plat}/{scope}")

    question = (payload.text or "").strip()
    key = (payload.conversation_key or "").strip()

    # 私訊：拿對話上下文（以被右鍵那一則為界，往前取最近幾則）
    history: list[dict] = []
    if scope == "dm" and key:
        turns = int(config.TABLE_REWRITE_HISTORY_TURNS or 0)
        if turns > 0:
            raw = await asyncio.to_thread(
                connector_client.fetch_history, plat, key,
                platform_account_id=0, limit=turns + 20, scope="dm")
            # ⚠️ Connector 回 DESC（新→舊）→ 轉正序，才能當「這一則之前」的上下文
            msgs = list(reversed(raw or []))
            if payload.message_id:
                cut = next((i for i, m in enumerate(msgs)
                            if str(m.get("message_id") or "") == payload.message_id), None)
                if cut is not None:
                    msgs = msgs[:cut]
            msgs = msgs[-turns:] if turns else []
            history = [{"direction": m.get("direction"), "text": m.get("text") or ""}
                       for m in msgs if (m.get("text") or "").strip()]
            if not question and payload.message_id:
                # 前端沒給原文 → 從歷史裡把那一則撈出來當問句
                for m in reversed(msgs):
                    if str(m.get("message_id") or "") == payload.message_id:
                        question = (m.get("text") or "").strip()
                        break
    if not question and scope == "comment" and payload.comment_id:
        # ★ 第二道保險：留言場景不像私訊有 history 可查原文，
        #   前端一旦漏給 `text`（2026-10-20 就是這樣）就直接 400
        #   「找不到要回覆的內容」→ 使用者看到的是「右鍵生成回覆沒有任何內容」。
        #   這裡按 comment_id 反查資料庫把原文補回來。
        question = (await asyncio.to_thread(
            connector_client.fetch_comment_text, plat, payload.comment_id) or "").strip()
    if not question:
        return _err(400, "找不到要回覆的內容（請重新整理後再試）")

    ss = await asyncio.to_thread(
        autoreply_db.resolve_scope_set, int(payload.business_id),
        scope=scope, post_config_id=int(payload.post_config_id))
    ans = await reply_engine.answer(
        question, business_id=int(payload.business_id), channel=plat, scope=scope,
        history=history, scope_set=ss)

    if not ans.tier or ans.tier == reply_engine.TIER_MISS:
        # 未命中 → 留一筆紀錄（命中的那半由「送出」時的 mark_manual_reply 記）
        try:
            from app.autoreply import autoreply
            await asyncio.to_thread(
                autoreply.log_manual_miss, int(payload.business_id), plat, scope,
                text=question, detail=(ans.detail or ans.reason or ""),
                message_id=payload.message_id, comment_id=payload.comment_id,
                conversation_key=key, matched_rows=ans.matched_rows)
        except Exception as e:  # noqa: BLE001 —— 記錄失敗不該擋住草稿
            log.warning("寫入人工未命中紀錄失敗: %s", e)

    return {"ok": True, "question": question, "would_send": bool(ans.text),
            "tier": ans.tier, "answer": ans.text, "detail": ans.detail, "reason": ans.reason,
            "matched": ans.matched_rows, "assets": ans.assets,
            "rewrote": ans.rewrote, "rewritten_query": ans.rewritten_query,
            "history_used": len(history),
            "scope_set": {"faq": len(ss.get("faq") or []), "table": len(ss.get("table") or []),
                          "kb": len(ss.get("kb") or []), "source": ss.get("source"),
                          "layers": ss.get("layers")}}
