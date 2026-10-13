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

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.agent import repo
from app.auth import deps, rbac
from app.autoreply import autoreply_db
from app.core import config
from app.llm import reply_engine
from app.rag import kb_db

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
    return {"ok": True, "items": items}


@router.get("/tables")
def tables(business_id: int = Query(...), sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    return {"ok": True, "items": autoreply_db.list_tables(int(business_id))}


@router.get("/collections")
def collections(business_id: int = Query(...), sess: dict = Depends(deps.require_agent)):
    bad = _biz_guard(sess, business_id)
    if bad:
        return bad
    return {"ok": True, "items": kb_db.list_collections(business_id=int(business_id))}


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
    return {"ok": True, "item": row}


# ================================================================ 命中測試

class TestIn(BaseModel):
    business_id: int
    platform: str = "whatsapp"
    scope: str = "dm"
    text: str = ""
    layers: list[str] | None = None
    post_config_id: int = 0


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

    ss = await asyncio.to_thread(
        autoreply_db.resolve_scope_set, int(payload.business_id),
        scope=payload.scope, post_config_id=int(payload.post_config_id))
    ans = await reply_engine.answer(
        text, business_id=int(payload.business_id), channel=payload.platform,
        scope=payload.scope, layers=layers, scope_set=ss)
    return {"ok": True, "tier": ans.tier, "answer": ans.text, "score": ans.score,
            "detail": ans.detail, "reason": ans.reason,
            "matched": ans.matched_rows, "assets": ans.assets,
            "rewrote": ans.rewrote, "rewritten_query": ans.rewritten_query,
            "elapsed_ms": ans.elapsed_ms, "llm_calls": ans.llm_calls,
            "scope_set": {"faq": len(ss.get("faq") or []), "table": len(ss.get("table") or []),
                          "kb": len(ss.get("kb") or []), "source": ss.get("source"),
                          "layers": ss.get("layers")}}
