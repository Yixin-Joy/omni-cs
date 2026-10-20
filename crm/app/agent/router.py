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
        text=text, assets=assets, extend_history=(payload.scope == "dm"),
        scope=("comment" if payload.scope == "comment" else "dm"))
    return {"ok": True, **res}


# ---------------------------------------------------------------- 貼文 / 留言

@router.get("/posts")
def posts(business_id: int = Query(...), platform: str = Query(...),
          bp_account_id: int = Query(default=0), limit: int = Query(default=10),
          offset: int = Query(default=0), sess: dict = Depends(deps.require_agent)):
    """貼文列表（以貼文為單位；走 Connector）。`bp_account_id=0` = 該平台全部帳號合併。"""
    bad = _guard(sess, business_id)
    if bad:
        return bad
    plat = _clean_platform(platform)
    if plat not in ("ig", "fb"):
        return _err(400, "貼文只有 ig / fb")
    accounts_ = repo.list_accounts(int(business_id), plat)
    if bp_account_id:
        accounts_ = [a for a in accounts_ if int(a["id"]) == int(bp_account_id)]
    if not accounts_:
        return {"ok": True, "items": [], "has_more": False,
                "reason": "該商家在此平台沒有可用帳號"}

    merged: list[dict] = []
    has_more = False
    unreachable = False
    for a in accounts_:
        got = connector_client.fetch_posts(plat, a["id"], limit=int(limit), offset=int(offset))
        if got is None:
            unreachable = True
            continue
        for it in got.get("items", []):
            it["bp_account_id"] = a["id"]
            it["account_name"] = a.get("display_name") or a.get("platform_business_account_id")
        merged.extend(got.get("items", []))
        has_more = has_more or bool(got.get("has_more"))
    merged.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    if unreachable and not merged:
        return _err(502, "連不上 Connector，暫時取不到貼文")
    return {"ok": True, "items": merged, "has_more": has_more}


@router.get("/post")
def post(business_id: int = Query(...), platform: str = Query(...),
         bp_account_id: int = Query(...), post_id: str = Query(...),
         sess: dict = Depends(deps.require_agent)):
    """單篇貼文詳情 + 留言樹（走 Connector）。"""
    bad = _guard(sess, business_id)
    if bad:
        return bad
    plat = _clean_platform(platform)
    if not plat:
        return _err(400, f"不支援的平台: {platform}")
    acc_ids = {int(a["id"]) for a in repo.list_accounts(int(business_id), plat)}
    if int(bp_account_id) not in acc_ids:
        return _err(400, "平台帳號不屬於該商家")
    data = connector_client.fetch_post(plat, int(bp_account_id), post_id)
    if not data:
        return _err(502, "連不上 Connector 或找不到貼文")
    data.pop("ok", None)
    data["bp_account_id"] = int(bp_account_id)
    return {"ok": True, **data}


@router.post("/comment/reply")
def comment_reply(payload: "CommentReplyIn", sess: dict = Depends(deps.require_csrf)):
    """回覆貼文留言 → 建任務（scope=comment）→ Connector 認領送出。"""
    if not rbac.can_view_conversation(sess):
        return _err(403, "需要客服坐席權限")
    bad = _guard(sess, payload.business_id)
    if bad:
        return bad
    plat = _clean_platform(payload.platform)
    if plat not in ("ig", "fb"):
        return _err(400, "留言回覆只有 ig / fb")
    cid = (payload.comment_id or "").strip()
    if not cid:
        return _err(400, "缺少 comment_id")
    text = (payload.text or "").strip()
    if not text:
        return _err(400, "回覆內容不能為空")
    acc_ids = {int(a["id"]) for a in repo.list_accounts(int(payload.business_id), plat)}
    if int(payload.bp_account_id) not in acc_ids:
        return _err(400, "平台帳號不屬於該商家")
    res = repo.create_manual_reply(
        sess, business_id=int(payload.business_id), platform=plat,
        bp_account_id=int(payload.bp_account_id), sender_id=cid,
        text=text, assets=[], extend_history=False, scope="comment")
    return {"ok": True, **res}


@router.post("/comment/handle")
def comment_handle(payload: "CommentHandleIn", sess: dict = Depends(deps.require_csrf)):
    """把某則留言標為已處理（不回覆，直接忽略），消除「N 新」標記。"""
    if not rbac.can_view_conversation(sess):
        return _err(403, "需要客服坐席權限")
    bad = _guard(sess, payload.business_id)
    if bad:
        return bad
    plat = _clean_platform(payload.platform)
    if plat not in ("ig", "fb"):
        return _err(400, "留言只有 ig / fb")
    cid = (payload.comment_id or "").strip()
    if not cid:
        return _err(400, "缺少 comment_id")
    n = connector_client.mark_message(plat, comment_id=cid, handled=True)
    audit.log_standalone(sess, "comment_handle", business_id=int(payload.business_id),
                         platform=plat, bp_account_id=int(payload.bp_account_id),
                         payload={"comment_id": cid, "updated": n})
    return {"ok": True, "updated": n}


@router.get("/task-status")
def task_status(business_id: int = Query(...), ids: str = Query(default=""),
                sess: dict = Depends(deps.require_agent)):
    """查外送任務的狀態（前端「送出後即時反饋」用）。

    ★ 為什麼需要（2026-10-20 小jo：「點擊回覆後只有重置整個頁面才看得到內容」）：
      留言送出後前端原本只能 `setTimeout(600)` 重抓一次貼文 —— 那時任務幾乎都還在
      `pending`（Connector 每秒 claim 一次，還要打 Graph API），重抓當然看不到，
      之後也沒有任何補刷 → 只能手動 F5。有了這支，前端可以只針對「剛送出的那幾筆」
      短輪詢，看到 `sent` 就補刷貼文詳情（那時 Connector 已把回覆寫進留言串、
      `replied` 也翻成 true）。

    `ids` 逗號分隔（上限 20 筆，避免濫用）；只回**屬於該商家**的任務。
    """
    bad = _guard(sess, business_id)
    if bad:
        return bad
    want: list[int] = []
    for p in (ids or "").split(","):
        p = p.strip()
        if p.isdigit():
            want.append(int(p))
    if not want:
        return {"ok": True, "items": []}
    return {"ok": True, "items": repo.tasks_status(want[:20], int(business_id))}


@router.get("/merchant-stats")
def merchant_stats(sess: dict = Depends(deps.require_agent)):
    """工作台頂部商家列用：每個商家「未處理數 + 最近訊息時間」。

    跨平台帳號彙總（走 Connector 一次算完）。回 `{items: {business_id: {pending, last_ts}}}`。
    """
    items = repo.list_businesses()
    if not rbac.is_super_agent(sess):
        allowed = set(rbac.allowed_business_ids(sess))
        items = [b for b in items if b["id"] in allowed]

    all_accounts: list[dict] = []
    biz_acc: dict[int, list[dict]] = {}
    for b in items:
        accs = repo.list_accounts(b["id"], "")
        biz_acc[b["id"]] = accs
        for a in accs:
            all_accounts.append({"platform": a["platform"],
                                 "platform_business_account_id": a["platform_business_account_id"],
                                 "_biz": b["id"]})
    stats = connector_client.fetch_merchant_stats(
        [{"platform": a["platform"],
          "platform_business_account_id": a["platform_business_account_id"]}
         for a in all_accounts]) or []
    # 把回來的統計按 (platform, native) 對回商家
    by_key = {(s["platform"], s["platform_business_account_id"]): s for s in stats}
    out: dict[str, dict] = {}
    for b in items:
        pending = 0
        last_ts = None
        for a in biz_acc[b["id"]]:
            s = by_key.get((a["platform"], a["platform_business_account_id"]))
            if not s:
                continue
            pending += int(s.get("pending") or 0)
            ts = s.get("last_ts")
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts
        out[str(b["id"])] = {"pending": pending, "last_ts": last_ts}
    return {"ok": True, "items": out}


@router.get("/pending-counts")
def pending_counts(business_id: int = Query(...), scope: str = Query(default="dm"),
                   sess: dict = Depends(deps.require_agent)):
    """工作台側欄用：該商家在**每個平台**的未處理筆數（二級 tab 紅點 + 一級彙總）。

    ★ 為什麼要有這支：二級平台 tab（微信 / WhatsApp / IG / FB）的紅點必須
      **在還沒點進該平台前就顯示**，而 `/conversations` 一次只回一個平台 → 前端
      算不出其他平台的數字。這裡走 Connector 的 `/pending-count`（最輕量的 count）。

    回 `{counts: {wechat: n, ...}, total: N, unreachable: bool, accounts: [...]}`；
    `unreachable=true` 代表有帳號取不到（前端要區分「0 筆」與「連不上」）。
    ★ `accounts` = **逐帳號**明細（`{id, platform, name, count}`）—— 同一平台綁了多個
      帳號時（例如兩個 WhatsApp），工作台的帳號下拉需要各自的红點。`count` 為 `None`
      代表那個帳號取不到。
    """
    bad = _guard(sess, business_id)
    if bad:
        return bad
    if scope not in SCOPES:
        return _err(400, f"不支援的 scope: {scope}")

    accounts_ = repo.list_accounts(int(business_id), "")
    counts: dict[str, int] = {}
    per_acc: list[dict] = []
    unreachable = False
    total = 0
    for a in accounts_:
        plat = a["platform"]
        if scope == "comment" and plat not in ("ig", "fb"):
            continue
        name = a.get("display_name") or a.get("platform_business_account_id") or str(a["id"])
        n = connector_client.fetch_pending_count(plat, a["id"], scope=scope)
        if n is None:
            # 取不到就回 `count=None`：前端要能區分「真的 0 筆」與「連不上」，
            # 否則紅點會消失得像「已經處理完了」。
            unreachable = True
            per_acc.append({"id": a["id"], "platform": plat,
                            "name": name, "count": None})
            continue
        counts[plat] = counts.get(plat, 0) + int(n)
        total += int(n)
        per_acc.append({"id": a["id"], "platform": plat,
                        "name": name, "count": int(n)})
    return {"ok": True, "counts": counts, "total": total, "unreachable": unreachable,
            "accounts": per_acc}


@router.post("/sync-history")
def sync_history(sess: dict = Depends(deps.require_csrf)):
    """★ 工作台「重新整理」：驅動 Connector 主動補拉歷史，避免公網中斷漏訊息。

    對象＝**這次登入者可訪問的全部商家**（管理員/超級坐席→全部；一般客服→
    只列被授權的商家，不會越權）。逐帳號補拉並計數。

    Connector 只對 `fb` / `ig` 有歷史端點；`wechat` / `whatsapp` 會回
    `supported=false` 與原因（平台沒有可回溯的 API），這裡原樣帶回讓前端顯示。

    回：`{ok, totals, items}`；Connector 連不上 → 502。
    """
    if not rbac.can_view_conversation(sess):
        return _err(403, "需要客服坐席權限")

    items = repo.list_businesses()
    if not rbac.is_super_agent(sess):
        allowed = set(rbac.allowed_business_ids(sess))
        items = [b for b in items if b["id"] in allowed]

    accounts: list[dict] = []
    for b in items:
        for a in repo.list_accounts(b["id"], ""):
            accounts.append({"platform": a["platform"],
                             "platform_business_account_id": a["platform_business_account_id"]})

    res = connector_client.sync_history(accounts)
    if res is None:
        return _err(502, "連不上 Connector，無法補拉歷史")
    totals = res.get("totals") or {}
    audit.log_standalone(sess, "sync_history", payload={
        "accounts": len(accounts),
        "businesses": [b["id"] for b in items],
        "totals": totals,
    })
    return {"ok": True, "totals": totals, "items": res.get("items") or []}


class ReadIn(BaseModel):
    business_id: int
    platform: str
    bp_account_id: int
    key: str
    scope: str = "dm"
    # ★ 單條訊息定位（工作台右鍵「標記這一則已處理」用）。
    #   有值時**只**標這一則，不擴大到整個會話 —— 前端右鍵只出現在單條氣泡上。
    message_id: str = ""


class CommentReplyIn(BaseModel):
    business_id: int
    platform: str
    bp_account_id: int
    comment_id: str
    text: str = ""


class CommentHandleIn(BaseModel):
    business_id: int
    platform: str
    bp_account_id: int
    comment_id: str


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
    elif payload.message_id:
        # ★ 單條：只標這一則（右鍵在「未處理的單條訊息氣泡」上）。
        #   ⚠️ 不可在這裡 fallback 到整個會話 —— 那會把使用者只想標一則的意圖
        #   變成一次清掉整個會話的紅點。
        n = connector_client.mark_message(plat, message_id=payload.message_id, handled=True)
    else:
        n = connector_client.mark_message(plat, end_user_sender_id=payload.key, handled=True)
    audit.log_standalone(sess, "conversation_read", business_id=int(payload.business_id),
                         platform=plat, bp_account_id=int(payload.bp_account_id),
                         end_user_sender_id=payload.key,
                         payload={"scope": payload.scope, "updated": n,
                                  "message_id": payload.message_id})
    return {"ok": True, "updated": n, "message_id": payload.message_id}


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
