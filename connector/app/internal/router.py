"""Connector 內網 API 端點 —— 供 CRM 呼叫（設計案 §4.2）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import account_map, history_sync, media
from app.channels import igfb_store
from app.core import config
from app.core import db
from app.internal.auth import require_internal

log = logging.getLogger("connector")

router = APIRouter(
    prefix="/internal/api",
    tags=["internal"],
    dependencies=[Depends(require_internal)],
)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


# ---------------------------------------------------------------- 帳號映射

class AccountMapIn(BaseModel):
    items: list[dict] = []


@router.post("/account-map/sync")
def account_map_sync(payload: AccountMapIn):
    """收 CRM 主動推送的映射（冪等 upsert）。"""
    items = payload.items or []
    n = account_map.upsert_many(items)
    account_map.mark_synced()
    return {"ok": True, "upserted": n}


# ---------------------------------------------------------------- 憑證體檢

class VerifyCredentialIn(BaseModel):
    platform: str = ""
    credential: dict = {}
    # ★ 由 CRM 傳入「**這個商戶**應該用哪個 Meta App」：
    #   帳號 credential → 商家 meta_app_id → 系統 META_APP_ID（CRM 已經解析好）。
    #   空字串 = 誰都沒設定 → 這裡不做 App 歸屬判斷（只回報事實，不判對錯）。
    expected_app_id: str = ""


@router.post("/verify-credential")
async def verify_credential(payload: VerifyCredentialIn):
    """保存帳號前**體檢憑證** —— 最重要的一項是「這把 token 是哪個 Meta App 簽的」。

    ★★ 為什麼一定要驗（2026-10-20 小jo「meta上顯示的是有pages_read_engagement權限啊」）：
      Meta 後台看到的是**某一個 App** 的權限，但 token 可能由**另一個 App** 簽發 ——
      兩者互不相干。實測：
        · 商家1 的 page token → `GET /app` 回 `1612033297309372`（agent）
          scopes 有 pages_read_engagement → `/comments` 200
        · 商家123 的 page token → `GET /app` 回 `1611577203333651`（console_eprotele）
          → 同一個 `/comments` 永遠 400 #100，**不管 agent 這個 App 勾了什麼**
      而且這種 token 連 webhook 都收不到（App 層的 callback 不是我們的網址）、
      `debug_token` 也會直接 400（`App_id in the input_token did not match`），
      所以只能靠 `/app` 這條路認出來。

    ★★ **多租戶**（小jo：「以後每個商戶都可能對應一個獨立的 meta app」）：
      **不寫死系統 App** —— 比對基準是 `expected_app_id`（CRM 按商戶解析後傳進來）。
      沒傳／空字串 → `is_our_app` 回 `None`（不知道），由 CRM 決定放過。
    ⚠️ 只做提示、不阻擋（CRM 決定要不要擋）。
    ⚠️ 不用 `debug_token`：跨 App 的 token 它一律 400，拿不到 scopes。
    """
    plat = (payload.platform or "").strip().lower()
    cred = payload.credential or {}
    expected = (payload.expected_app_id or "").strip()
    source = "tenant" if expected else ""
    if not expected:
        # ★ 商戶/帳號都沒設定 → 回退系統級（connector/.env 的 META_APP_ID）。
        #   這樣「大家都用同一個 App」的舊部署仍是嚴格比對；
        #   而一商戶一 App 的情況只要在商家設定裡填上，就以它為準。
        #   ⚠️ 這樣做是為了**不必在 CRM 再配一份** App ID（少一份手動對齊的耦合）。
        expected = str(config.META_APP_ID or "").strip()
        source = "system" if expected else ""
    out: dict = {"ok": True, "platform": plat,
                 "expected_app_id": expected,
                 "expected_source": source,
                 "token_app_id": None, "token_app_name": None,
                 "is_our_app": None, "is_page_token": None,
                 "scopes": None, "missing_scopes": None, "problems": []}
    if plat not in ("fb", "ig"):
        return out                              # 微信 / WhatsApp 不適用

    token = (cred.get("page_access_token") or cred.get("ig_access_token") or "").strip()
    if not token:
        out["problems"].append("沒有填 token")
        return out

    import httpx

    from app.channels import igfb

    base, ver = igfb.FB_BASE, igfb._ver()
    async with httpx.AsyncClient(timeout=15) as cl:
        # 1) 這把 token 是哪個 App 簽的？
        try:
            r = await cl.get(f"{base}/{ver}/app", params={"access_token": token})
            if r.status_code == 200:
                j = r.json() or {}
                out["token_app_id"] = str(j.get("id") or "") or None
                out["token_app_name"] = j.get("name")
                if expected:
                    out["is_our_app"] = (out["token_app_id"] == expected)
                    if not out["is_our_app"]:
                        out["problems"].append(
                            f"這把 token 是【{out['token_app_name'] or '未知'}】"
                            f"（App {out['token_app_id']}）簽發的，"
                            f"但這個商戶設定的 Meta App 是 {expected}"
                            f"（{out['token_app_id']} ≠ {expected}）——"
                            f"在別的 App 後台勾權限對這把 token 完全無效，"
                            f"webhook 也收不到（callback 不是我們的網址）。"
                            f"請用 App {expected} 重新授權該粉專再取 token")
                else:
                    # 沒設定任何 App → 只回報事實，不判對錯（多租戶下可能是正常的）
                    out["is_our_app"] = None
                    out["problems"].append(
                        f"（提示）這把 token 屬於 App {out['token_app_id']}"
                        f"【{out['token_app_name'] or '未知'}】；"
                        f"本商戶未設定 Meta App，因此沒有可比對的基準")
            else:
                out["problems"].append(f"查 token 歸屬失敗 HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            out["problems"].append(f"查 token 歸屬失敗：{type(e).__name__}: {e}")

        # 2) 是粉專令牌還是用戶令牌？（用戶令牌讀不到粉專貼文/留言）
        try:
            r = await cl.get(f"{base}/{ver}/me",
                             params={"access_token": token, "fields": "id,name"})
            if r.status_code == 200:
                j = r.json() or {}
                me_id = str(j.get("id") or "")
                page_id = str(cred.get("page_id") or
                              (cred.get("ig_user_id") if plat == "ig" else "") or "")
                out["is_page_token"] = bool(page_id) and me_id == page_id
                if not out["is_page_token"]:
                    out["problems"].append(
                        f"這把 token 的 /me 回的是「{j.get('name') or me_id}」"
                        f"（id={me_id}），不是粉專本身"
                        f"（page_id={page_id or '未填'}）——"
                        f"看起來是**用戶令牌**，讀不到粉專的貼文／留言／私訊")
        except Exception as e:  # noqa: BLE001
            out["problems"].append(f"查 token 型態失敗：{type(e).__name__}: {e}")

        # 3) 有貼文的話，實際打一次 /comments（權限只有在真打時才暴露）
        if plat == "fb" and cred.get("page_id"):
            try:
                rp = await cl.get(f"{base}/{ver}/{cred['page_id']}/feed",
                                  params={"access_token": token,
                                          "fields": "id", "limit": 1})
                if rp.status_code == 200:
                    items = (rp.json() or {}).get("data") or []
                    if items:
                        pid = str(items[0].get("id") or "")
                        rc = await cl.get(f"{base}/{ver}/{pid}/comments",
                                          params={"access_token": token, "limit": 1})
                        if rc.status_code != 200:
                            msg = ((rc.json() or {}).get("error") or {}).get("message", "")
                            if "pages_read_engagement" in msg or "pages_read_user_content" in msg:
                                out["problems"].append(
                                    "讀留言列表被拒（需 pages_read_engagement／"
                                    "pages_read_user_content）：" + msg[:120])
                            else:
                                out["problems"].append(f"讀留言失敗 HTTP {rc.status_code}: {msg[:120]}")
                elif rp.status_code != 200:
                    msg = ((rp.json() or {}).get("error") or {}).get("message", "")
                    out["problems"].append(f"讀貼文列表失敗 HTTP {rp.status_code}: {msg[:120]}")
            except Exception as e:  # noqa: BLE001
                out["problems"].append(f"探測留言權限失敗：{type(e).__name__}: {e}")

    return out


@router.get("/account-map")
def account_map_list():
    """本地快取現況（除錯用）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, platform_business_account_id, business_id, bp_account_id,"
                "       enabled, synced_at FROM connector_account_map"
                " ORDER BY platform, platform_business_account_id"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    last = account_map.last_sync_at()
    return {
        "ok": True,
        "last_sync_at": last.isoformat() if last else None,
        "items": [
            {"platform": r[0], "platform_business_account_id": r[1], "business_id": r[2],
             "bp_account_id": r[3], "enabled": bool(r[4]),
             "synced_at": r[5].isoformat() if r[5] else None}
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 媒體

@router.post("/media/upload")
async def media_upload(
    file: UploadFile = File(...),
    scope: str = Query(default="kb_cache"),
):
    """CRM 推二進制（知識庫圖片 / 客服附件）→ 存檔回 `media_key`。"""
    data = await file.read()
    if not data:
        return _err(400, "空檔案")
    try:
        res = media.save_bytes(scope, data, file.filename or "")
    except ValueError as e:
        return _err(400, str(e))
    return {"ok": True, **{k: v for k, v in res.items() if k != "path"}}


@router.get("/media/fetch")
def media_fetch(media_key: str = Query(...)):
    """CRM 代理取客戶來訊媒體 / 快取圖片。"""
    try:
        p = media.resolve(media_key)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, "找不到媒體")
    return FileResponse(str(p), filename=p.name)


# ---------------------------------------------------------------- 發送嘗試紀錄

def _attempts_of(task_id: int) -> list[dict]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_seq, status, platform, target_id, platform_msg_id, error, attempted_at"
                "  FROM connector_outbound_attempt WHERE crm_task_id = %s ORDER BY sub_seq",
                (task_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"sub_seq": r[0], "status": r[1], "platform": r[2], "target_id": r[3],
         "platform_msg_id": r[4], "error": r[5],
         "attempted_at": r[6].isoformat() if r[6] else None}
        for r in rows
    ]


@router.get("/outbound-attempt")
def outbound_attempt(crm_task_id: int = Query(...)):
    """任務詳情 / ops 查看；CRM 逾時清理也靠這支判斷「到底發出去了沒」。"""
    return {"ok": True, "items": _attempts_of(int(crm_task_id))}


class BatchIn(BaseModel):
    task_ids: list[int] = []


@router.post("/outbound-attempt/batch")
def outbound_attempt_batch(payload: BatchIn):
    ids = [int(i) for i in (payload.task_ids or [])][:500]
    return {"ok": True, "items": {str(i): _attempts_of(i) for i in ids}}


# ---------------------------------------------------------------- 訊息查詢

def _msg_text(raw, payload) -> str:
    """統一取出來訊／回覆的純文字（forward_log 的 JSON 或 igfb_dm 的 text）。"""
    import json as _json

    def _pick(o):
        if isinstance(o, dict):
            v = o.get("messageContent")
            if v in (None, ""):
                v = o.get("text")
            return v if isinstance(v, str) else ""
        if isinstance(o, str):
            s = o.strip()
            if s[:1] in ("{", "["):
                try:
                    return _pick(_json.loads(s))
                except Exception:
                    return s
            return s
        return ""

    return (_pick(raw) or _pick(payload) or "").strip()


_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _files_of(raw, payload, attachments) -> list[dict]:
    """把訊息裡的附件正規化成工作台渲染用的 `files`（沿用舊單體的欄位約定）。

    - wechat / whatsapp：從 `forwarded_payload.uploadedFiles` 取（`raw_content` 備援）
    - ig / fb：從 `igfb_dm.attachments` 取
    """
    import json as _json

    def _load(v):
        if isinstance(v, (list, dict)):
            return v
        if isinstance(v, str) and v.strip()[:1] in ("{", "["):
            try:
                return _json.loads(v)
            except Exception:
                return None
        return None

    items: list = []
    a = _load(attachments)
    if isinstance(a, list) and a:
        for x in a:
            if not isinstance(x, dict):
                continue
            url = x.get("url") or ((x.get("payload") or {}).get("url") if isinstance(x.get("payload"), dict) else "") or ""
            typ = x.get("type") or "file"
            items.append({
                "url": url,
                "filename": url.split("/")[-1].split("?")[0] or typ or "檔案",
                "fileSize": int(x.get("fileSize") or 0),
                "isImage": typ == "image" or url.lower().endswith(_IMG_EXT),
                "sticker": typ == "sticker",
                "sticker_id": str(x.get("sticker_id") or ""),
            })
        return items

    for src in (payload, raw):
        d = _load(src)
        if isinstance(d, dict) and isinstance(d.get("uploadedFiles"), list):
            for x in d["uploadedFiles"]:
                if not isinstance(x, dict):
                    continue
                url = x.get("url") or ""
                items.append({
                    "url": url,
                    "filename": x.get("filename") or (url.split("/")[-1].split("?")[0] or "檔案"),
                    "fileSize": int(x.get("fileSize") or 0),
                    "isImage": bool(x.get("isImage")) or url.lower().endswith(_IMG_EXT),
                    "sticker": bool(x.get("sticker")),
                    "sticker_id": str(x.get("sticker_id") or ""),
                })
            return items
    return []


@router.get("/messages/query")
def messages_query(
    platform: str = Query(...),
    platform_business_account_id: str = Query(...),
    end_user_sender_id: str = Query(...),
    scope: str = Query(default="dm"),
    before: str | None = Query(default=None),
    limit: int = Query(default=50),
):
    """封裝跨庫查詢訊息（CRM 工作台渲染會話 / 引擎取對話上下文，設計案 §4.2）。

    回傳每筆都帶 `text`（已從 raw_content / forwarded_payload 正規化出的純文字），
    CRM 端不必再自己解析各家格式。
    """
    n = max(1, min(int(limit or 50), 200))
    # ★ 排序鍵一律用 COALESCE(訊息時間, 入庫時間)。
    #   forward_log 的**我方回覆**（舊單體寫入）`message_time` 是 NULL，
    #   而 Postgres 的 `ORDER BY x DESC` 預設是 **NULLS FIRST** ——
    #   直接排會讓所有回覆跑到最前面，反轉後變成「客戶訊息一串、我方回覆一串」。
    #   與舊單體一致：舊端點就是用 `ORDER BY created_at ASC`。
    if platform in ("wechat", "whatsapp"):
        _ts = "COALESCE(message_time, created_at)"
        sql = (
            "SELECT id, platform, direction, message_id, sender_id, message_type,"
            f"       raw_content, forwarded_payload, {_ts} AS ts,"
            "       COALESCE(is_handled, false), auto_reply_state, auto_reply_tier,"
            "       auto_reply_detail, NULL::jsonb AS attachments,"
            "       NULLIF(forwarded_payload->'webClientSender'->>'name', '') AS sender_name"
            "  FROM forward_log"
            " WHERE platform = %s AND platform_business_account_id = %s AND sender_id = %s"
            f"   AND (%s IS NULL OR {_ts} < %s::timestamptz)"
            f" ORDER BY {_ts} DESC NULLS LAST LIMIT %s"
        )
        args = (platform, platform_business_account_id, end_user_sender_id, before, before, n)
    elif platform in ("ig", "fb"):
        _ts = "COALESCE(created_at, inserted_at)"
        sql = (
            "SELECT id, platform, direction, message_id, sender_id, message_type,"
            f"       COALESCE(text, '') AS raw_content, NULL AS forwarded_payload, {_ts} AS ts,"
            "       COALESCE(is_handled, false), auto_reply_state, auto_reply_tier,"
            "       auto_reply_detail, attachments, COALESCE(sender_name, '')"
            "  FROM igfb_dm"
            " WHERE platform = %s AND platform_business_account_id = %s"
            "   AND (sender_id = %s OR conversation_key = %s)"
            f"   AND (%s IS NULL OR {_ts} < %s::timestamptz)"
            f" ORDER BY {_ts} DESC NULLS LAST LIMIT %s"
        )
        args = (platform, platform_business_account_id, end_user_sender_id,
                end_user_sender_id, before, before, n)
    else:
        return _err(400, f"不支援的平台: {platform}")

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "ok": True,
        "items": [
            {"id": r[0], "platform": r[1], "direction": r[2], "message_id": r[3],
             "sender_id": r[4], "message_type": r[5], "content": r[6],
             "text": _msg_text(r[6], r[7]),
             "ts": r[8].isoformat() if r[8] else None,
             "is_handled": bool(r[9]),
             "auto_reply_state": r[10] or "",
             "auto_reply_tier": r[11] or "",
             "auto_reply_detail": r[12] or "",
             "files": _files_of(r[6], r[7], r[13]),
             "sender_name": r[14] or ""}
            for r in rows
        ],
    }


class MarkIn(BaseModel):
    platform: str
    message_ids: list[str] | None = None
    message_id: str | None = None
    comment_id: str | None = None
    conversation_id: str | None = None
    # ★ 工作台「清紅點」用：整個會話（私訊＝某個 sender 的全部訊息）
    end_user_sender_id: str | None = None
    state: str | None = None
    tier: str | None = None
    detail: str | None = None
    handled: bool | None = None


@router.post("/messages/mark")
def messages_mark(payload: MarkIn):
    """CRM 標記訊息的自動回復處置（`auto_reply_state` / tier / detail / `is_handled`）。

    表歸屬：wechat / whatsapp → `forward_log`；ig / fb → `igfb_dm`（有 comment_id 則 `igfb_comment`）。
    """
    sets, args = [], []
    if payload.state is not None:
        sets.append("auto_reply_state=%s"); args.append(payload.state)
    if payload.tier is not None:
        sets.append("auto_reply_tier=%s"); args.append(payload.tier)
    if payload.detail is not None:
        sets.append("auto_reply_detail=%s"); args.append(payload.detail)
    if payload.handled is not None:
        sets.append("is_handled=%s"); args.append(bool(payload.handled))
    if not sets:
        return {"ok": True, "updated": 0}

    p = payload.platform
    if p not in ("wechat", "whatsapp", "ig", "fb"):
        return _err(400, f"不支援的平台: {p}")

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if p in ("wechat", "whatsapp"):
                table = "forward_log"
                where, wargs = ["platform=%s"], [p]
                if payload.message_ids:
                    where.append("message_id = ANY(%s)"); wargs.append(list(payload.message_ids))
                if payload.message_id:
                    where.append("message_id=%s"); wargs.append(payload.message_id)
                if payload.conversation_id:
                    where.append("conversation_id=%s"); wargs.append(payload.conversation_id)
                if payload.end_user_sender_id:
                    where.append("sender_id=%s"); wargs.append(payload.end_user_sender_id)
            else:
                if payload.comment_id:
                    table = "igfb_comment"
                    where, wargs = ["platform=%s", "comment_id=%s"], [p, payload.comment_id]
                else:
                    table = "igfb_dm"
                    where, wargs = ["platform=%s"], [p]
                    if payload.message_ids:
                        where.append("message_id = ANY(%s)"); wargs.append(list(payload.message_ids))
                    if payload.message_id:
                        where.append("message_id=%s"); wargs.append(payload.message_id)
                    if payload.end_user_sender_id:
                        # ig/fb 的會話鍵是 sender_id，退回 conversation_key（與 messages/query 一致）
                        where.append("(sender_id=%s OR conversation_key=%s)")
                        wargs += [payload.end_user_sender_id, payload.end_user_sender_id]
            if len(where) == 1:
                return _err(400, "缺少 message_id / message_ids / comment_id / conversation_id /"
                                 " end_user_sender_id")
            cur.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE " + " AND ".join(where),
                        tuple(args + wargs))
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "updated": n}


# ---------------------------------------------------------------- 會話列表（工作台）

_CONV_LIMIT_MAX = 200


def _conv_dm_forward_sql(before: str | None) -> str:
    """wechat / whatsapp 私訊：`forward_log` 依 sender_id 聚合。"""
    # ★ 排序鍵用 COALESCE(message_time, created_at)：舊資料的我方回覆
    #   `message_time` 是 NULL（舊單體沒寫），只按 message_time 排會 NULLS FIRST
    #   → 回覆全被擠到最後（工作台看起來「客戶訊息一串、我方回覆一串」）。
    _ts = "COALESCE(message_time, created_at)"
    return (
        "SELECT sender_id AS ckey,"
        f"       max({_ts}) AS last_ts,"
        "       count(*) FILTER (WHERE direction = 'inbound'"
        "                          AND COALESCE(is_handled, false) = false) AS pending,"
        "       count(*) AS total,"
        f"       (array_agg(direction ORDER BY {_ts} DESC NULLS LAST))[1] AS last_dir,"
        f"       (array_agg(message_type ORDER BY {_ts} DESC NULLS LAST))[1] AS last_type,"
        f"       (array_agg(raw_content ORDER BY {_ts} DESC NULLS LAST))[1] AS last_raw,"
        f"       (array_agg(forwarded_payload ORDER BY {_ts} DESC NULLS LAST))[1] AS last_payload,"
        # ★ 顯示名字：收訊時存在 `forwarded_payload.webClientSender.name`
        #   （WhatsApp webhook 的 contacts[].profile.name；舊單體也是寫在這）。
        #   ⚠️ 用 `FILTER (WHERE ... <> '')` 而不是 NULLIF —— **array_agg 不會忽略 NULL**
        #   （跟 string_agg 不一樣）：我方的 outbound payload 沒有這個欄位，
        #   排序後 [1] 就會拿到那個 NULL，名字永遠是空的。
        f"       (array_agg(forwarded_payload->'webClientSender'->>'name'"
        f"                  ORDER BY {_ts} DESC NULLS LAST)"
        "        FILTER (WHERE COALESCE(forwarded_payload->'webClientSender'->>'name', '') <> ''"
        "        ))[1] AS last_name,"
        "       NULL::text AS post_id"
        "  FROM forward_log"
        " WHERE platform = %s AND platform_business_account_id = %s"
        "   AND sender_id IS NOT NULL AND sender_id <> ''"
        f"   AND (%s IS NULL OR {_ts} < %s::timestamptz)"
        " GROUP BY sender_id ORDER BY last_ts DESC NULLS LAST LIMIT %s"
    )


def _conv_dm_igfb_sql() -> str:
    """ig / fb 私訊：`igfb_dm` 依 sender_id（退回 conversation_key）聚合。"""
    _ts = "COALESCE(created_at, inserted_at)"
    return (
        "SELECT COALESCE(NULLIF(sender_id, ''), conversation_key) AS ckey,"
        f"       max({_ts}) AS last_ts,"
        "       count(*) FILTER (WHERE direction = 'inbound'"
        "                          AND COALESCE(is_handled, false) = false) AS pending,"
        "       count(*) AS total,"
        f"       (array_agg(direction ORDER BY {_ts} DESC NULLS LAST))[1] AS last_dir,"
        f"       (array_agg(message_type ORDER BY {_ts} DESC NULLS LAST))[1] AS last_type,"
        f"       (array_agg(text ORDER BY {_ts} DESC NULLS LAST))[1] AS last_raw,"
        "       NULL::jsonb AS last_payload,"
        # ★★ 顯示名字：取**最近一則「有名字」的**，不能直接拿最後一則。
        #   我方的 outbound（AI 回覆 / 客服回覆）`sender_name` 是空的，
        #   而最後一則往往就是我們的回覆 → 名字會被蓋成空白
        #   （症狀：一被回覆，會話列表上的用戶名就消失）。同一段的
        #   forward_log SQL 早就加了這個 FILTER，IG/FB 這兩支漏了。
        #   ⚠️ `array_agg` 不像 `string_agg` 會忽略 NULL —— 不能靠 COALESCE 蒙過去。
        f"       (array_agg(COALESCE(sender_name, '') ORDER BY {_ts} DESC NULLS LAST)"
        "        FILTER (WHERE COALESCE(sender_name, '') <> ''))[1] AS last_name,"
        "       NULL::text AS post_id"
        "  FROM igfb_dm"
        " WHERE platform = %s AND platform_business_account_id = %s"
        "   AND COALESCE(NULLIF(sender_id, ''), conversation_key) IS NOT NULL"
        f"   AND (%s IS NULL OR {_ts} < %s::timestamptz)"
        " GROUP BY 1 ORDER BY last_ts DESC NULLS LAST LIMIT %s"
    )


def _conv_comment_sql() -> str:
    """ig / fb 留言：`igfb_comment` 每則留言一條會話（key = comment_id）。

    ★ `pending`（未處理）＝**頂層留言 && 未回 && 未忽略 && 非我方自己發的**。
    不能只寫 `is_handled = false` —— 我方回覆與他人接續的**子留言**也常常是
    `is_handled = false`，會被算成待處理（實測：35 筆子留言全被誤算，
    導致明明沒有待處理、紅點卻顯示 35）。
    條件與 `igfb_db.pending_comment_counts()` 一致。
    """
    _ts = "COALESCE(created_at, inserted_at)"
    return (
        "SELECT comment_id AS ckey,"
        f"       max({_ts}) AS last_ts,"
        "       count(*) FILTER (WHERE parent_id IS NULL"
        "                          AND COALESCE(replied, false) = false"
        "                          AND COALESCE(is_handled, false) = false"
        "                          AND (from_id IS NULL OR from_id = ''"
        "                               OR from_id <> %s)) AS pending,"
        "       count(*) AS total,"
        "       'inbound'::text AS last_dir,"
        "       'comment'::text AS last_type,"
        f"       (array_agg(text ORDER BY {_ts} DESC NULLS LAST))[1] AS last_raw,"
        "       NULL::jsonb AS last_payload,"
        # 同一道理由：只取有名字的那幾列（我方回覆的留言 `from_name` 是 '客服'，
        # 但保險起見仍過濾空值）。留言的作者名另外靠
        # `igfb_store.fill_comment_names_from_dm()` 用私訊名回填。
        f"       (array_agg(COALESCE(from_name, '') ORDER BY {_ts} DESC NULLS LAST)"
        "        FILTER (WHERE COALESCE(from_name, '') <> ''))[1] AS last_name,"
        f"       (array_agg(post_id ORDER BY {_ts} DESC NULLS LAST))[1] AS post_id"
        "  FROM igfb_comment"
        " WHERE platform = %s AND platform_business_account_id = %s"
        "   AND comment_id IS NOT NULL AND comment_id <> ''"
        f"   AND (%s IS NULL OR {_ts} < %s::timestamptz)"
        " GROUP BY comment_id ORDER BY last_ts DESC NULLS LAST LIMIT %s"
    )


@router.get("/conversations")
def conversations(
    platform: str = Query(...),
    platform_business_account_id: str = Query(...),
    scope: str = Query(default="dm"),
    limit: int = Query(default=50),
    before: str | None = Query(default=None),
):
    """工作台會話列表（設計案 §4.2）—— CRM 不碰 fdw，一律走這裡。

    依 `end_user_sender_id` 聚合；回傳每筆**最後一則**訊息的純文字（`text`、
    已由 `_msg_text` 正規化）、方向、未處理筆數（`pending` = 來訊且未標記
    `is_handled`）與總筆數。前端紅點即 `pending > 0`。

    - `scope='dm'`：wechat / whatsapp → `forward_log`；ig / fb → `igfb_dm`。
    - `scope='comment'`：ig / fb → `igfb_comment`（每則留言一個會話）。

    ⚠️ 逐列容錯：任一列 `_msg_text` 解析失敗只讓該列 text 為空，不影響其他列。
    """
    n = max(1, min(int(limit or 50), _CONV_LIMIT_MAX))
    if platform in ("wechat", "whatsapp"):
        if scope != "dm":
            return _err(400, f"{platform} 不支援 scope={scope}")
        sql = _conv_dm_forward_sql(before)
        args = (platform, platform_business_account_id, before, before, n)
    elif platform in ("ig", "fb"):
        if scope == "comment":
            sql = _conv_comment_sql()
            # comment 的 pending 要把「我方自己發的」排除 → 多帶一次原生 ID 當 self
            args = (platform_business_account_id, platform, platform_business_account_id,
                    before, before, n)
        else:
            sql = _conv_dm_igfb_sql()
            args = (platform, platform_business_account_id, before, before, n)
    else:
        return _err(400, f"不支援的平台: {platform}")

    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 —— 查詢失敗回結構化錯誤，不 500
        return _err(500, f"會話查詢失敗: {type(e).__name__}: {e}")

    items = []
    for r in rows:
        try:
            items.append({
                "key": r[0],
                "sender_id": r[0],
                "sender_name": (r[8] or "") if len(r) > 8 else "",
                "last_ts": r[1].isoformat() if r[1] else None,
                "pending": int(r[2] or 0),
                "total": int(r[3] or 0),
                "direction": r[4] or "inbound",
                "message_type": r[5] or "text",
                "text": _msg_text(r[6], r[7]),
                "post_id": r[9] if len(r) > 9 else None,
                "scope": scope,
                "platform": platform,
                "platform_business_account_id": platform_business_account_id,
            })
        except Exception:  # noqa: BLE001 —— 單列壞掉不拖垮整份清單
            continue
    return {"ok": True, "items": items, "scope": scope, "platform": platform}


# ---------------------------------------------------------------- 貼文 / 留言（工作台）

_POST_LIMIT_MAX = 50


def _iso(v) -> str | None:
    return v.isoformat() if v else None


@router.get("/posts")
def posts(
    platform: str = Query(...),
    platform_business_account_id: str = Query(...),
    limit: int = Query(default=10),
    offset: int = Query(default=0),
):
    """工作台「貼文留言」列表（設計案 §4.2）——**以貼文為單位**（不是每則留言一條）。

    每筆帶：
    - `new_comments`：頂層、未回、未忽略、且**非我方自己發的**留言數（= 列表「N 新」紅標）。
    - `comments_count`：頂層留言數（以 DB 實算為準，Meta API 的 comments_count 不可靠）。

    ⚠️ 與 `conversations(scope=comment)` 的差異：那支是「每則留言一個會話」，
    這支是「每篇貼文一列 + 其留言統計」，工作台貼文列表用這支。
    """
    if platform not in ("ig", "fb"):
        return _err(400, f"貼文只支援 ig / fb（收到 {platform}）")
    n = max(1, min(int(limit or 10), _POST_LIMIT_MAX))
    off = max(0, int(offset or 0))
    self_id = platform_business_account_id  # fb=page_id / ig=ig_user_id，就是這欄

    sql = (
        "SELECT p.post_id, p.name, p.media_type, p.media_url, p.permalink, p.like_count,"
        "       p.comments_count, p.created_at,"
        "       (SELECT count(*) FROM igfb_comment c"
        "         WHERE c.platform = p.platform AND c.post_id = p.post_id"
        "           AND c.parent_id IS NULL AND COALESCE(c.replied, false) = false"
        "           AND COALESCE(c.is_handled, false) = false"
        "           AND (c.from_id IS NULL OR c.from_id = '' OR c.from_id <> %s)) AS new_comments,"
        "       (SELECT count(*) FROM igfb_comment c"
        "         WHERE c.platform = p.platform AND c.post_id = p.post_id"
        "           AND c.parent_id IS NULL) AS top_comments"
        "  FROM igfb_post p"
        " WHERE p.platform = %s AND p.platform_business_account_id = %s"
        " ORDER BY p.created_at DESC NULLS LAST"
        " LIMIT %s OFFSET %s"
    )
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (self_id, platform, platform_business_account_id, n, off))
            rows = cur.fetchall()
            cur.execute(
                "SELECT count(*) FROM igfb_post"
                " WHERE platform = %s AND platform_business_account_id = %s",
                (platform, platform_business_account_id),
            )
            total = int(cur.fetchone()[0])
    except Exception as e:  # noqa: BLE001 —— 查詢失敗回結構化錯誤，不 500
        return _err(500, f"貼文查詢失敗: {type(e).__name__}: {e}")
    finally:
        conn.close()

    items = [
        {
            "post_id": r[0],
            "name": r[1] or "",
            "media_type": r[2] or "",
            "media_url": r[3] or "",
            "permalink": r[4] or "",
            "like_count": int(r[5] or 0),
            "comments_count": int(r[9] or 0),   # 頂層實算，比 API 的可靠
            "created_at": _iso(r[7]),
            "new_comments": int(r[8] or 0),
            "top_comments": int(r[9] or 0),
        }
        for r in rows
    ]
    return {"ok": True, "platform": platform, "items": items,
            "has_more": (off + len(items)) < total}


def _build_comment_tree(rows: list) -> list[dict]:
    """把扁平留言列組成巢狀樹（與舊單體 `igfb_db.list_comments` 同一套規則）。

    頂層：後發的在前；子留言：先發的在前；最多 3 級（第 3 級攤平所有後代）。
    """
    children: dict[str, list] = {}
    for r in rows:
        children.setdefault(r[1] or "", []).append(r)   # r[1] = parent_id

    def ct(r):
        return r[8] or r[9] or r[10]    # created_at / last_activity_at / inserted_at

    def _json(r) -> dict:
        return {
            "comment_id": r[0], "parent_id": r[1], "from_id": r[2], "from_name": r[3],
            "text": r[4] or "", "like_count": int(r[5] or 0),
            "replied": bool(r[6]), "is_handled": bool(r[7]),
            "time": _iso(r[8]),
            "auto_reply_state": r[11] or "",
            "auto_reply_tier": r[12] or "",
            "auto_reply_detail": r[13] or "",
        }

    def descendants(node) -> list:
        out = []
        for k in children.get(node[0], []):
            out.append(k)
            out.extend(descendants(k))
        return out

    def build(node, level: int) -> dict:
        item = _json(node)
        if level == 1:
            item["replies"] = [build(k, 2) for k in sorted(children.get(node[0], []), key=ct)]
        elif level == 2:
            item["replies"] = [_json(k) for k in sorted(descendants(node), key=ct)]
        else:
            item["replies"] = []
        return item

    ids = {r[0] for r in rows}
    tops = [r for r in rows if not r[1] or r[1] not in ids]
    return [build(t, 1) for t in sorted(tops, key=ct, reverse=True)]


@router.get("/post")
def post_detail(
    platform: str = Query(...),
    platform_business_account_id: str = Query(...),
    post_id: str = Query(...),
):
    """單篇貼文詳情 + 留言樹（工作台貼文詳情頁用）。找不到回 404。"""
    if platform not in ("ig", "fb"):
        return _err(400, f"貼文只支援 ig / fb（收到 {platform}）")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT post_id, name, media_type, media_url, permalink, like_count,"
                "       comments_count, created_at"
                "  FROM igfb_post"
                " WHERE platform = %s AND platform_business_account_id = %s AND post_id = %s",
                (platform, platform_business_account_id, post_id),
            )
            p = cur.fetchone()
            if not p:
                return _err(404, "找不到貼文")
            cur.execute(
                "SELECT comment_id, parent_id, from_id, from_name, text, like_count,"
                "       replied, is_handled, created_at, last_activity_at, inserted_at,"
                "       auto_reply_state, auto_reply_tier, auto_reply_detail"
                "  FROM igfb_comment"
                " WHERE platform = %s AND platform_business_account_id = %s AND post_id = %s",
                (platform, platform_business_account_id, post_id),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return _err(500, f"貼文詳情查詢失敗: {type(e).__name__}: {e}")
    finally:
        conn.close()

    comments = _build_comment_tree(rows)
    return {
        "ok": True,
        "post_id": p[0], "name": p[1] or "", "media_type": p[2] or "",
        "media_url": p[3] or "", "permalink": p[4] or "",
        "like_count": int(p[5] or 0), "comments_count": len(comments),
        "created_at": _iso(p[7]), "platform": platform,
        "comments": comments,
    }


class MerchantStatsIn(BaseModel):
    accounts: list[dict] = []   # [{platform, platform_business_account_id}, ...]


@router.post("/merchant-stats")
def merchant_stats(payload: MerchantStatsIn):
    """一次算出多個帳號的「未處理數 + 最近訊息時間」（工作台頂部商家列紅點/排序用）。

    `pending` = 來訊且未 `is_handled` 的筆數（私訊＋留言都算）；
    `last_ts` = 該帳號最後一則訊息的時間（排序：最近有訊息的商家排前面）。

    ⚠️ 逐帳號容錯：任一帳號查詢失敗只讓它回 0/None，不影響其他。
    """
    out = []
    conn = db.connect()
    try:
        for a in payload.accounts or []:
            plat = (a.get("platform") or "").strip()
            acc = (a.get("platform_business_account_id") or "").strip()
            item = {"platform": plat, "platform_business_account_id": acc,
                    "pending": 0, "last_ts": None}
            if plat and acc:
                try:
                    with conn.cursor() as cur:
                        if plat in ("wechat", "whatsapp"):
                            cur.execute(
                                "SELECT count(*) FILTER (WHERE direction = 'inbound'"
                                "                          AND COALESCE(is_handled, false) = false),"
                                "       max(COALESCE(message_time, created_at)) FROM forward_log"
                                " WHERE platform = %s AND platform_business_account_id = %s",
                                (plat, acc))
                        else:
                            # ★ 留言的「待處理」＝**頂層 + 未回 + 未忽略 + 非我方**
                            #   （只寫 is_handled=false 會把我方回覆的子留言也算進來）
                            cur.execute(
                                "SELECT (SELECT count(*) FROM igfb_dm"
                                "         WHERE platform = %s AND platform_business_account_id = %s"
                                "           AND direction = 'inbound'"
                                "           AND COALESCE(is_handled, false) = false)"
                                "     + (SELECT count(*) FROM igfb_comment"
                                "         WHERE platform = %s AND platform_business_account_id = %s"
                                "           AND parent_id IS NULL"
                                "           AND COALESCE(replied, false) = false"
                                "           AND COALESCE(is_handled, false) = false"
                                "           AND (from_id IS NULL OR from_id = ''"
                                "                OR from_id <> %s)),"
                                "       GREATEST("
                                "         (SELECT max(COALESCE(created_at, inserted_at)) FROM igfb_dm"
                                "           WHERE platform = %s AND platform_business_account_id = %s),"
                                "         (SELECT max(COALESCE(created_at, inserted_at)) FROM igfb_comment"
                                "           WHERE platform = %s AND platform_business_account_id = %s))",
                                (plat, acc, plat, acc, acc, plat, acc, plat, acc))
                        row = cur.fetchone()
                    item["pending"] = int(row[0] or 0)
                    item["last_ts"] = _iso(row[1])
                except Exception as e:  # noqa: BLE001 —— 單帳號失敗不拖垮整批
                    log.warning("merchant-stats %s/%s 失敗: %s", plat, acc, e)
            out.append(item)
    finally:
        conn.close()
    return {"ok": True, "items": out}


# ---------------------------------------------------------------- 歷史補拉

class SyncHistoryIn(BaseModel):
    items: list[dict] = []
    max_conversations: int = history_sync.DEFAULT_MAX_CONVERSATIONS
    max_messages: int = history_sync.DEFAULT_MAX_MESSAGES
    max_posts: int = history_sync.DEFAULT_MAX_POSTS
    max_comments: int = history_sync.DEFAULT_MAX_COMMENTS


@router.post("/sync-history")
async def sync_history(payload: SyncHistoryIn):
    """★ 主動補拉歷史（工作台「重新整理」按鈕）—— 公網中斷時的補救路徑。

    `items` = `[{platform, platform_business_account_id}]`（CRM 依登入者權限挑帳號）。
    憑證一律取自本機 `connector_account_map` 快取，呼叫端**不需要**傳 credential。

    只支援 fb / ig（Graph API 有歷史端點）；wechat / whatsapp 回
    `supported=false` 並附原因 —— 不是漏做，是平台沒有可回溯的 API。

    ⚠️ 逐帳號容錯，且每個帳號有時間上限，不會讓一個壞帳號拖垮整批。
    """
    return await history_sync.sync_many(
        payload.items,
        max_conversations=max(1, min(int(payload.max_conversations or 0), 50)),
        max_messages=max(1, min(int(payload.max_messages or 0), 100)),
        max_posts=max(1, min(int(payload.max_posts or 0), 30)),
        max_comments=max(1, min(int(payload.max_comments or 0), 200)),
    )


@router.get("/pending-count")
def pending_count(
    platform: str = Query(...),
    platform_business_account_id: str = Query(...),
    scope: str = Query(default="dm"),
):
    """某帳號在該平台的「未處理」筆數（工作台二級平台 tab 的紅點）。

    ★ 為什麼需要這支：二級 tab 的紅點必須**在不點進去該平台前就能顯示**，
      而 `/conversations` 只回某一平台的清單 → 前端無法得知其他平台的未處理數。
      這裡用最輕量的 count 查詢一次算完。
    - `scope='dm'`：wechat / whatsapp 讀 `forward_log`；ig / fb 讀 `igfb_dm`。
    - `scope='comment'`：ig / fb 讀 `igfb_comment`（頂層 + 未回 + 未忽略 + 非我方）。
    """
    if platform not in ("wechat", "whatsapp", "ig", "fb"):
        return _err(400, f"不支援的平台: {platform}")
    if scope not in ("dm", "comment"):
        return _err(400, f"不支援的 scope: {scope}")
    if scope == "comment" and platform not in ("ig", "fb"):
        return _err(400, "comment 只支援 ig / fb")
    n = igfb_store.count_pending(platform, platform_business_account_id, scope)
    return {"ok": True, "count": n, "platform": platform, "scope": scope}


@router.get("/comment-text")
def comment_text(
    platform: str = Query(...),
    comment_id: str = Query(...),
):
    """某則留言的原文（工作台右鍵「由自動回復引擎給出回覆」的兜底）。

    ★ 為什麼需要（2026-10-20 小jo：留言右鍵生成回覆「沒有任何內容出現」）：
      前端 `engineDraft` 忘了帶 `text` → 後端 `question` 為空 → 400
      「找不到要回覆的內容」。前端已補上傳原文，這支是**第二道保險**：
      只要知道 comment_id，就一定能拿回原文（例如從別處觸發、或 DOM 上取不到）。
    """
    if platform not in ("ig", "fb"):
        return _err(400, f"留言只支援 ig / fb（收到 {platform}）")
    if not (comment_id or "").strip():
        return _err(400, "缺少 comment_id")
    rows = igfb_store.comment_text(platform, comment_id.strip())
    if not rows:
        return _err(404, "找不到該留言")
    return {"ok": True, **rows}


@router.get("/ping")
def ping():
    return {"ok": True, "service": config.SERVICE_NAME}