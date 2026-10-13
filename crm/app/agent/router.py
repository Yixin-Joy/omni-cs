"""客服工作台端點（設計案 §10.2 / §8）—— 會話列表、對話、人工回覆。

- 讀取：`require_agent`（ops 單獨不行；複合 agent 可以）。
- 變更：`require_csrf` + `require_agent`。
- **每次查詢／回覆都做商家權限校驗**（`rbac.can_access_business`），不靠前端隱藏。
- 跨庫一律走 Connector 內網 API，CRM 不碰 fdw。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.agent import repo
from app.auth import deps, rbac
from app.internal import connector_client

router = APIRouter(prefix="/console/agent", tags=["agent"])

PLATFORMS = ("wechat", "whatsapp", "ig", "fb")
SCOPES = ("dm", "comment")


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


def _guard(sess: dict, business_id: int):
    """回 None 表示通過；否則回錯誤回應。"""
    if not rbac.can_access_business(sess, business_id):
        return _err(403, "沒有該商家的權限")


def _clean_platform(p: str) -> str:
    return p if p in PLATFORMS else ""


# ---------------------------------------------------------------- 商家 / 帳號

@router.get("/businesses")
def businesses(sess: dict = Depends(deps.require_agent)):
    """該登入者可操作的商家（super agent 全部；其餘只列被授權的）。"""
    items = repo.list_businesses()
    if not rbac.is_super_agent(sess):
        allowed = set(rbac.allowed_business_ids(sess))
        items = [b for b in items if b["id"] in allowed]
    for b in items:
        b["summary"] = repo.business_summary(b["id"])
    return {"ok": True, "items": items,
            "is_super_agent": rbac.is_super_agent(sess),
            "is_kb_editor": rbac.is_kb_editor(sess)}


@router.get("/accounts")
def accounts(business_id: int = Query(...), platform: str = Query(default=""),
             sess: dict = Depends(deps.require_agent)):
    bad = _guard(sess, business_id)
    if bad:
        return bad
    return {"ok": True, "items": repo.list_accounts(int(business_id), _clean_platform(platform))}


# ---------------------------------------------------------------- 會話

@router.get("/conversations")
def conversations(business_id: int = Query(...), platform: str = Query(...),
                  bp_account_id: int = Query(default=0), scope: str = Query(default="dm"),
                  limit: int = Query(default=50), before: str = Query(default=""),
                  sess: dict = Depends(deps.require_agent)):
    """會話列表（走 Connector）。`bp_account_id=0` = 該平台全部帳號合併。"""
    bad = _guard(sess, business_id)
    if bad:
        return bad
    plat = _clean_platform(platform)
    if not plat:
        return _err(400, f"不支援的平台: {platform}")
    if scope not in SCOPES:
        return _err(400, f"不支援的 scope: {scope}")

    accounts_ = repo.list_accounts(int(business_id), plat)
    if bp_account_id:
        accounts_ = [a for a in accounts_ if int(a["id"]) == int(bp_account_id)]
    if not accounts_:
        return {"ok": True, "items": [], "reason": "該商家在此平台沒有可用帳號"}

    merged: list[dict] = []
    unreachable = False
    for a in accounts_:
        got = connector_client.fetch_conversations(
            plat, a["id"], scope=scope, limit=int(limit), before=before)
        if got is None:
            unreachable = True
            continue
        for it in got:
            it["bp_account_id"] = a["id"]
            it["account_name"] = a.get("display_name") or a.get("platform_business_account_id")
        merged.extend(got)
    merged.sort(key=lambda x: x.get("last_ts") or "", reverse=True)
    merged = merged[: max(1, min(int(limit), 200))]
    if unreachable and not merged:
        return _err(502, "連不上 Connector，暫時取不到會話")
    return {"ok": True, "items": merged, "ai_enabled": repo.ai_switch_row(
        int(business_id), plat, "comment" if scope == "comment" else "dm")}


@router.get("/conversation")
def conversation(business_id: int = Query(...), platform: str = Query(...),
                 key: str = Query(...), bp_account_id: int = Query(...),
                 scope: str = Query(default="dm"), limit: int = Query(default=50),
                 before: str = Query(default=""),
                 sess: dict = Depends(deps.require_agent)):
    """單一會話的訊息（時間正序）+ 該會話的 AI 留痕。"""
    bad = _guard(sess, business_id)
    if bad:
        return bad
    plat = _clean_platform(platform)
    if not plat:
        return _err(400, f"不支援的平台: {platform}")

    msgs = connector_client.fetch_history(
        plat, key, platform_account_id=int(bp_account_id), limit=int(limit),
        before=before, scope=scope)
    if msgs is None:
        return _err(502, "連不上 Connector，暫時取不到訊息")
    msgs = list(reversed(msgs))          # Connector 回 DESC，這裡轉正序好渲染
    logs = repo.conversation_logs(int(business_id), plat, key, limit=20)
    return {"ok": True, "items": msgs, "logs": logs}


# ---------------------------------------------------------------- 人工回覆

class ReplyIn(BaseModel):
    business_id: int
    platform: str
    bp_account_id: int
    sender_id: str
    scope: str = "dm"
    text: str = ""
    assets: list[dict] | None = None


@router.post("/reply")
def reply(payload: ReplyIn, sess: dict = Depends(deps.require_csrf)):
    """★ 人工回覆：建任務（`source='manual'`）→ Connector 認領送出（設計案 §10.2）。"""
    if not rbac.can_view_conversation(sess):
        return _err(403, "需要客服坐席權限")
    bad = _guard(sess, payload.business_id)
    if bad:
        return bad
    plat = _clean_platform(payload.platform)
    if not plat:
        return _err(400, f"不支援的平台: {payload.platform}")
    sender = (payload.sender_id or "").strip()
    if not sender:
        return _err(400, "缺少 sender_id")
    text = (payload.text or "").strip()
    assets = [a for a in (payload.assets or []) if isinstance(a, dict) and a.get("media_key")]
    if not text and not assets:
        return _err(400, "回覆內容不能為空")

    # 帳號必須屬於該商家，避免用別的商家的帳號發訊
    acc_ids = {int(a["id"]) for a in repo.list_accounts(int(payload.business_id), plat)}
    if int(payload.bp_account_id) not in acc_ids:
        return _err(400, "平台帳號不屬於該商家")

    res = repo.create_manual_reply(
        sess, business_id=int(payload.business_id), platform=plat,
        bp_account_id=int(payload.bp_account_id), sender_id=sender,
        text=text, assets=assets, extend_history=(payload.scope == "dm"))
    return {"ok": True, **res}


class ReadIn(BaseModel):
    business_id: int
    platform: str
    bp_account_id: int
    key: str
    scope: str = "dm"


@router.post("/read")
def mark_read(payload: ReadIn, sess: dict = Depends(deps.require_csrf)):
    """清紅點：把該會話的來訊標記為已處理（走 Connector `messages/mark`）。"""
    if not rbac.can_view_conversation(sess):
        return _err(403, "需要客服坐席權限")
    bad = _guard(sess, payload.business_id)
    if bad:
        return bad
    plat = _clean_platform(payload.platform)
    if not plat:
        return _err(400, f"不支援的平台: {payload.platform}")

    if payload.scope == "comment":
        n = connector_client.mark_message(plat, comment_id=payload.key, handled=True)
    else:
        n = connector_client.mark_message(plat, end_user_sender_id=payload.key, handled=True)
    audit.log_standalone(sess, "conversation_read", business_id=int(payload.business_id),
                         platform=plat, bp_account_id=int(payload.bp_account_id),
                         end_user_sender_id=payload.key,
                         payload={"scope": payload.scope, "updated": n})
    return {"ok": True, "updated": n}


@router.post("/upload")
async def upload(file: UploadFile = File(...), scope: str = Form(default="outbound"),
                 sess: dict = Depends(deps.require_csrf)):
    """人工回覆的附件：CRM 轉推給 Connector 存檔，回 `media_key`。"""
    if not rbac.can_view_conversation(sess):
        return _err(403, "需要客服坐席權限")
    data = await file.read()
    if not data:
        return _err(400, "空檔案")
    res = connector_client.upload_media(data, file.filename or "upload.bin", scope=scope)
    if not res:
        return _err(502, "推送附件到 Connector 失敗")
    return {"ok": True, **{k: v for k, v in res.items() if k != "path"}}
