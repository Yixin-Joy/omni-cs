"""IG/FB 收件匣工作台接口（掛在 /console/igfb 之下）。

只在本機工作台查看 / 回覆，不轉發目標系統 9033。

端點：
  GET  /console/igfb/dm/conversations         私訊會話列表（以用戶為單位，含未回覆數）
  GET  /console/igfb/dm/messages              某用戶的私訊串
  POST /console/igfb/dm/reply                 私訊回覆（文字 / 圖片 / 文件，multipart）
  GET  /console/igfb/posts                    貼文列表（分頁 10 篇，含新留言數）
  GET  /console/igfb/post/{platform}/{id}     貼文詳情 + 留言（含子留言、已回標記）
  POST /console/igfb/comment/reply            回覆留言
  POST /console/igfb/sync                     手動拉取歷史（私訊 / 貼文留言）
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

import config
import igfb
import igfb_db

log = logging.getLogger("wechat-forwarder")

router = APIRouter(prefix="/console/igfb", tags=["igfb"])

PLATFORMS = ("ig", "fb")

# 同步冷卻（秒）：避免前端頻繁觸發拉取打爆 Graph API 額度。
# 依 scope 分開：輕量的（貼文清單 / 單篇留言 / 私訊名稱）放寬，工作台才能每 10 秒刷新；
# 重的（全量留言）維持 15 秒，避免貼文一多就撞到平台額度。
_SYNC_COOLDOWN = {
    "feed": 5,       # 只拉貼文清單（新貼文沒有 webhook，靠這條）
    "comments": 5,   # 只拉某一篇貼文的留言（前端每 10 秒對當前打開那篇跑）
    "auto": 5,       # 貼文清單 + 私訊名稱（舊前端的相容值）
    "names": 30,     # 只補私訊使用者名稱
    "dm": 15,
    "posts": 15,     # 貼文 + 每篇留言（手動「同步最新資料」）
    "all": 15,
}
_SYNC_COOLDOWN_DEFAULT = 15
_sync_cooldown: dict[str, float] = {}


def _ok(data) -> Response:
    return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")


def _err(msg: str, status: int = 400) -> Response:
    return Response(content=json.dumps({"error": msg}, ensure_ascii=False),
                    media_type="application/json", status_code=status)


def _check_platform(platform: str) -> Response | None:
    platform = (platform or "").strip().lower()
    if platform not in PLATFORMS:
        return _err("platform 必須是 ig 或 fb")
    return None


def _meta_hint(resp_body: str) -> str:
    """把常見的 Meta 錯誤碼翻成可讀提示，附在錯誤訊息裡。"""
    try:
        err = json.loads(resp_body or "{}").get("error") or {}
    except Exception:
        return ""
    code = err.get("code")
    if code == 190:
        return "Meta 存取權杖已失效或過期，請重新產生 token 並更新 .env 後重啟服務"
    if code == 10:
        return "此 App 缺少執行該動作的權限（例如 IG 留言回覆需要 instagram_manage_comments）"
    if code in (4, 17, 32):
        return "已達 Meta API 用量上限，請稍後再試"
    return ""


def _meta_error_response(result: dict) -> Response:
    body = result.get("body") or ""
    return Response(
        content=json.dumps({"ok": False, "status": result.get("status"), "body": body[:500],
                            "hint": _meta_hint(body)}, ensure_ascii=False),
        media_type="application/json", status_code=502,
    )


# ---------- 私訊 ----------

@router.get("/dm/conversations")
async def dm_conversations(platform: str = "ig"):
    if err := _check_platform(platform):
        return err
    return _ok({"platform": platform, "conversations": igfb_db.list_dm_conversations(platform)})


@router.get("/dm/messages")
async def dm_messages(platform: str = "ig", conversation_key: str = ""):
    if err := _check_platform(platform):
        return err
    if not conversation_key:
        return _err("missing conversation_key")
    rows = igfb_db.list_dm_messages(platform, conversation_key)
    name = ""
    sender_id = conversation_key
    convs = igfb_db.list_dm_conversations(platform)
    for c in convs:
        if c["conversation_key"] == conversation_key:
            name = c["name"]
            sender_id = c["sender_id"]
            break
    return _ok({"platform": platform, "conversation_key": conversation_key,
                "sender_id": sender_id, "name": name, "messages": rows})


@router.post("/dm/read")
async def dm_mark_read(platform: str = "ig", conversation_key: str = ""):
    """把某個私訊會話標為已處理（此次不回復），消除未讀紅點。"""
    if err := _check_platform(platform):
        return err
    if not conversation_key:
        return _err("missing conversation_key")
    igfb_db.mark_handled(platform, conversation_key)
    return _ok({"ok": True})


def _sanitize_filename(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._") or "file"
    return name


@router.post("/dm/reply")
async def dm_reply(
    platform: str = Form(...),
    to: str = Form(...),
    text: str = Form(""),
    auto_tier: str = Form(""),
    file: UploadFile | None = File(None),
):
    platform = platform.strip().lower()
    if platform not in PLATFORMS:
        return _err("platform 必須是 ig 或 fb")
    if not to:
        return _err("missing 'to'")
    auto_tier = (auto_tier or "").strip().lower()

    text = (text or "").strip()
    saved_name = ""
    filename = ""
    mime = ""
    if file is not None:
        filename = _sanitize_filename(file.filename or "file")
        mime = (file.content_type or "").lower().split(";")[0].strip()
        if not mime:
            suffix = Path(filename).suffix.lower()
            if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                mime = "image/" + suffix.lstrip(".")
            else:
                mime = "application/octet-stream"
        saved_name = f"igfb_reply_{int(time.time())}_{filename}"
        Path(config.MEDIA_DIR).mkdir(exist_ok=True)
        with open(str(Path(config.MEDIA_DIR) / saved_name), "wb") as f:
            f.write(await file.read())

    if saved_name:
        if not config.PUBLIC_BASE_URL:
            return _err("發送文件需要設定 PUBLIC_BASE_URL（Meta 要能下載該檔案）", 500)
        file_url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/files/{saved_name}"
    else:
        file_url = ""

    result = None
    try:
        if saved_name:
            result = await igfb.send_dm(platform, to, "file", text, file_url, mime)
        else:
            result = await igfb.send_dm(platform, to, "text", text, "", "")
    except Exception as e:
        log.error("IG/FB 私訊回覆失敗 %s: %s", platform, e)
        await _log_outbound_dm(platform, to, text, saved_name, filename, mime, "failed", str(e))
        return _err(str(e), 500)

    ok = result.get("status") == 200
    if ok:
        await _log_outbound_dm(platform, to, text, saved_name, filename, mime, "success", result.get("body"))
        # 這則是「由自動回復引擎產生、客服確認送出」的話，先標記再清未讀
        # （mark_manual_reply 需要那則還在 is_handled=false 狀態才找得到）
        if auto_tier:
            try:
                import autoreply
                autoreply.mark_manual_reply(platform, "dm", auto_tier, conversation_key=to)
            except Exception as e:
                log.warning("標記人工確認的引擎回覆失敗: %s", e)
        # 回覆成功後，把該會話的 inbound 全部標為已處理（消除紅點）
        igfb_db.mark_handled(platform, to)
        return _ok({"ok": True, "status": result["status"], "body": (result["body"] or "")[:500]})
    await _log_outbound_dm(platform, to, text, saved_name, filename, mime, "failed", result.get("body"))
    return _meta_error_response(result)


async def _log_outbound_dm(platform: str, to: str, text: str, saved_name: str,
                           filename: str, mime: str, status: str, resp_body: str) -> None:
    attachments = []
    if saved_name:
        url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/files/{saved_name}" if config.PUBLIC_BASE_URL else f"/files/{saved_name}"
        attachments = [{"type": "image" if mime.startswith("image/") else "file",
                        "url": url, "filename": filename, "mimeType": mime}]
    message_id = ""
    try:
        j = json.loads(resp_body or "{}")
        message_id = str(j.get("message_id") or j.get("id") or "")
    except Exception:
        message_id = ""
    await igfb_db.ingest_dm(
        platform=platform,
        conversation_key=to,
        thread_id=None,
        sender_id=to,
        sender_name="",
        direction="outbound",
        message_type="file" if saved_name else "text",
        text=text,
        attachments=attachments,
        message_id=message_id or f"out_{int(time.time() * 1000)}",
        created_at=None,
    )


# ---------- 貼文 / 留言 ----------

@router.get("/posts")
async def posts(platform: str = "ig", offset: int = 0, limit: int = 10):
    if err := _check_platform(platform):
        return err
    limit = max(1, min(limit, 50))
    return _ok({"platform": platform, **igfb_db.list_posts(platform, limit, offset)})


@router.get("/post/{platform}/{post_id}")
async def post_detail(platform: str, post_id: str):
    if err := _check_platform(platform):
        return err
    post = igfb_db.get_post(platform, post_id)
    if post is None:
        return _err("找不到貼文", 404)
    post["comments"] = igfb_db.list_comments(platform, post_id)
    return _ok(post)


@router.post("/comment/reply")
async def comment_reply(request: Request):
    body = await request.json()
    platform = (body.get("platform") or "").strip().lower()
    comment_id = body.get("comment_id", "")
    text = (body.get("text") or "").strip()
    if platform not in PLATFORMS:
        return _err("platform 必須是 ig 或 fb")
    if not comment_id:
        return _err("missing comment_id")
    if not text:
        return _err("missing text")
    auto_tier = (body.get("auto_tier") or "").strip().lower()

    result = None
    try:
        result = await igfb.reply_comment(platform, comment_id, text)
    except Exception as e:
        log.error("IG/FB 留言回覆失敗 %s: %s", platform, e)
        return _err(str(e), 500)

    ok = result.get("status") == 200
    if ok:
        # 把回覆本身落地，讓留言串立即顯示我方回覆（API 只回新留言 id）
        new_id = ""
        try:
            new_id = str(json.loads(result.get("body") or "{}").get("id") or "")
        except Exception:
            new_id = ""
        igfb_db.add_own_reply(platform, comment_id, new_id, text)
        # 「由自動回復引擎產生、客服確認送出」→ 標成人工確認的自動回復
        if auto_tier:
            try:
                import autoreply
                autoreply.mark_manual_reply(platform, "comment", auto_tier, comment_id=comment_id)
            except Exception as e:
                log.warning("標記人工確認的引擎回覆失敗: %s", e)
        return _ok({"ok": True, "status": result["status"], "body": (result["body"] or "")[:500]})
    return _meta_error_response(result)


@router.post("/comment/handle")
async def comment_handle(request: Request):
    """把某則留言標為已處理（不回覆，直接忽略），消除「N 新」標記。"""
    body = await request.json()
    platform = (body.get("platform") or "").strip().lower()
    comment_id = body.get("comment_id", "")
    if platform not in PLATFORMS:
        return _err("platform 必須是 ig 或 fb")
    if not comment_id:
        return _err("missing comment_id")
    igfb_db.mark_comment_handled(platform, comment_id)
    return _ok({"ok": True})


@router.get("/comment/pending")
async def comment_pending():
    """各平台待處理留言數（給導覽紅點用）。"""
    return _ok(igfb_db.pending_comment_counts())


@router.post("/refresh-ig-token")
async def refresh_ig_token():
    """手動續期 IG 登入權杖（平時由服務啟動/每日自動處理）。"""
    import token_refresh
    res = await token_refresh.refresh_ig_token()
    return _ok(res) if res.get("ok") else _err(str(res.get("error")), 502)


# ---------- 同步 ----------

@router.post("/sync")
async def sync(platform: str = "ig", scope: str = "all", post_id: str = ""):
    if err := _check_platform(platform):
        return err
    scope = (scope or "all").lower()
    key = f"{platform}:{scope}"
    now = time.time()
    cooldown = _SYNC_COOLDOWN.get(scope, _SYNC_COOLDOWN_DEFAULT)
    if now - _sync_cooldown.get(key, 0) < cooldown:
        return _ok({"ok": True, "skipped": True,
                    "reason": f"冷卻中（{cooldown} 秒內已同步過）"})
    _sync_cooldown[key] = now

    result = {"ok": True, "scope": scope}
    try:
        if scope in ("dm", "all"):
            result["dm_conversations"] = await igfb_db.sync_dms(platform)
        if scope == "auto":
            # 工作台定時刷新用：貼文清單 + 私訊名稱（不做全量留言/訊息同步，成本低）
            result["posts"] = await igfb_db.sync_post_list(platform)
            result["dm_names"] = await igfb_db.sync_dm_names(platform)
        if scope == "feed":
            # 只拉貼文清單（新貼文不會有 webhook，靠這條補進來）
            result["posts"] = await igfb_db.sync_post_list(platform)
        if scope == "comments":
            # 只拉某一篇貼文的留言（工作台每 10 秒對當前打開那篇跑）
            if not post_id:
                return _err("scope=comments 需要帶 post_id")
            result["post_id"] = post_id
            result["comments"] = await igfb_db.sync_post_comments(platform, post_id)
        if scope == "names":
            result["dm_names"] = await igfb_db.sync_dm_names(platform)
        if scope in ("posts", "all"):
            result["posts"] = await igfb_db.sync_posts_comments(platform)
    except Exception as e:
        log.error("同步失敗 %s: %s", platform, e)
        return _err(str(e), 500)
    return _ok(result)
