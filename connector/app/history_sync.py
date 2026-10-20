"""歷史消息補拉（「重新整理」按鈕用）—— 主動打平台 API 把漏掉的消息撈回來。

★ 為什麼需要：公網隧道（ngrok）中斷時 webhook 推不進來 → 消息沒入庫 →
  工作台整段空白。客服按一下「重新整理」，CRM 驅動這裡把最近的會話 / 訊息 /
  貼文 / 留言拉回來補齊。

★ 平台能力（**不是漏做，是平台真的沒有**）：
  - `fb` / `ig`  → Graph API 有歷史查詢端點（conversations / messages / feed / comments）→ 可補拉。
  - `whatsapp`   → Cloud API **沒有**查詢歷史對話的端點，只有 webhook 推送。
  - `wechat`     → 客服消息同樣只有推送，微信不會讓你回頭拉。
  這兩個平台一律回報 `supported=false` 並附原因，讓前端能明確告知客服。

★★ **增量拉取（不重複處理、不覆蓋既有狀態）**：水位從**資料庫現有的最後一則**算
  （`igfb_store.dm_watermarks` / `comment_watermarks`），不是另存一張水位表 ——
  冪等、自愈，不會有「水位存了但資料沒落」的不一致。
  - 會話級：`updated_time` 不比該會話水位新 → **整個跳過**（連 thread messages 都不打）。
  - 消息級：`created_at` 不比水位新 → 不處理（不重複入庫、也不重算狀態）。
  - 留言級：貼文已有留言的最後時間之後才處理；**只重算「這次真的新增留言」的討論串**。
  這樣「之前的、已經標了忽略／已處理的」刷新後**不會被翻回未處理**。

★ 紅點規則（小jo 指定）：新入庫的**入站**私訊，
  其後（同一會話、時間更晚）已有 ≥1 則我方回覆 → `is_handled=true`（不補紅點）；
  沒有任何回覆 → `is_handled=false`（補紅點）。
  只對「這次新插入」的列套用（`igfb_store.apply_reply_rule`），
  **不重算整個會話**，才不會把客服手動標的「已處理」翻回去。

★ 留言同理：只對**這次真的新增留言**的討論串重算（`roots=[...]`）——
  傳 `None` 會重算整篇貼文，把客服手動標的「已忽略」蓋掉（舊版就是這樣）。
  已存在的留言完全不觸發重算。新留言進來的討論串本來就該重新亮紅點。
"""
from __future__ import annotations

import asyncio
import logging
import time

from app import account_map
from app.channels import igfb, igfb_recv, igfb_store

log = logging.getLogger("connector")

# 每個帳號的預設上限 —— 目的是「快又有用」，不是全量鏡像。
# ★ 因為有水位（`igfb_store.dm_watermarks`）：**沒有新消息的會話直接跳過**，
#   不呼叫 thread messages。所以會話數放寬也不會變慢。
DEFAULT_MAX_CONVERSATIONS = 25
DEFAULT_MAX_MESSAGES = 50
DEFAULT_MAX_POSTS = 10
DEFAULT_MAX_COMMENTS = 50
DEFAULT_MAX_SECONDS = 60.0

SUPPORTED = ("fb", "ig")


# ---------------------------------------------------------------- 小工具

def _norm_attachments(m: dict) -> list[dict]:
    return igfb_recv._norm_message(m or {}).get("attachments") or []


def _msg_sec(m: dict):
    return igfb_recv.to_dt(m.get("created_time"))


def drop_comment_dm_noise(msgs: list[dict]) -> list[dict]:
    """濾掉 FB「留言轉私訊」造成的噪音（與舊單體 `igfb_db._drop_comment_dm_noise` 同規則）。

    使用者對貼文留言後，FB 會把留言原文複製進 Messenger 並附一則系統通知；
    系統通知本身一律丟棄，與它同一秒的訊息也丟棄（實測同秒成對出現）。
    """
    notice_secs: set[int] = set()
    for m in msgs:
        if igfb_recv.is_system_dm_notice(igfb_recv._norm_message(m or {})["text"]):
            dt = _msg_sec(m)
            if dt is not None:
                notice_secs.add(int(dt.timestamp()))

    kept: list[dict] = []
    for m in msgs:
        if igfb_recv.is_system_dm_notice(igfb_recv._norm_message(m or {})["text"]):
            continue
        dt = _msg_sec(m)
        if dt is not None and int(dt.timestamp()) in notice_secs:
            continue
        kept.append(m)
    return kept


def _flatten_fb_comments(post_id: str, comments: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in comments or []:
        cid = str(c.get("id") or "")
        frm = c.get("from") or {}
        out.append(_c(post_id, cid, None, frm, c.get("message"),
                      c.get("like_count"), c.get("created_time")))
        for r in ((c.get("comments") or {}).get("data") or []):
            rf = r.get("from") or {}
            out.append(_c(post_id, str(r.get("id") or ""), cid, rf, r.get("message"),
                          r.get("like_count"), r.get("created_time")))
    return out


def _flatten_ig_comments(post_id: str, comments: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in comments or []:
        cid = str(c.get("id") or "")
        frm = c.get("from") or {}
        uname = frm.get("username") or c.get("username") or ""
        out.append(_c(post_id, cid, None, {"id": frm.get("id", ""), "name": uname},
                      c.get("text"), c.get("like_count"), c.get("timestamp")))
        for r in ((c.get("replies") or {}).get("data") or []):
            rf = r.get("from") or {}
            runame = rf.get("username") or r.get("username") or ""
            out.append(_c(post_id, str(r.get("id") or ""), cid, {"id": rf.get("id", ""), "name": runame},
                          r.get("text"), r.get("like_count"), r.get("timestamp")))
    return out


def _c(post_id, comment_id, parent_id, frm, text, like_count, created_time) -> dict:
    name = (frm or {}).get("name") or (frm or {}).get("username") or ""
    return {
        "post_id": post_id,
        "comment_id": comment_id,
        "parent_id": parent_id,
        "from_id": str((frm or {}).get("id") or ""),
        "from_name": name or "用戶",
        "text": text or "",
        "like_count": int(like_count or 0),
        "created_at": igfb_recv.to_dt(created_time),
    }


# ---------------------------------------------------------------- 單一帳號

async def _sync_dm(platform: str, native_id: str, acc: dict, self_id: str,
                   max_conversations: int, max_messages: int, deadline: float) -> dict:
    """拉私訊會話 + 訊息 → `igfb_dm`。回統計。"""
    cred = acc.get("credential") or {}
    errors: list[str] = []
    if platform == "fb":
        page_id, token = igfb._fb_creds(cred)
        if not page_id or not token:
            return {"conversations": 0, "dm_new": 0, "dm_out": 0, "dm_handled": 0,
                    "dm_pending": 0, "skipped_conversations": 0, "skipped_messages": 0,
                    "errors": ["FB 憑證缺少 page_id / page_access_token，無法補拉私訊"]}
        convs = await igfb.fb_conversations(page_id or native_id, token, max_conversations)
    else:
        _ig_id, token = igfb._ig_creds(cred)
        if not token:
            return {"conversations": 0, "dm_new": 0, "dm_out": 0, "dm_handled": 0,
                    "dm_pending": 0, "skipped_conversations": 0, "skipped_messages": 0,
                    "errors": ["IG 憑證缺少 ig_access_token，無法補拉私訊"]}
        convs = await igfb.ig_conversations(token, max_conversations)

    # ★ 增量水位：每個會話「我們收到的最後一則」的時間。
    #   比它舊的消息一律不碰 —— 既省 API，也保證**已標記的狀態不被重算覆蓋**。
    wm = await asyncio.to_thread(igfb_store.dm_watermarks, platform, native_id)

    new_ids: list[int] = []
    n_threads = n_new = n_out = 0
    skipped_convs = skipped_msgs = 0

    for conv in convs:
        if time.monotonic() > deadline:
            errors.append("已達單帳號時間上限，部分會話未拉取")
            break
        thread_id = str(conv.get("id") or "")
        if not thread_id:
            continue
        parts = (conv.get("participants") or {}).get("data") or []
        other = next((p for p in parts if str(p.get("id") or "") != str(self_id)), {})
        customer_id = str(other.get("id") or "")
        customer_name = other.get("name") or other.get("username") or ""

        w = wm.get(customer_id) or None
        # 會話級預篩：這會話的最後活動時間不比水位新 → 整個跳過（省一次 thread messages）
        upd = igfb_recv.to_dt(conv.get("updated_time"))
        if w is not None and upd is not None and upd <= w:
            skipped_convs += 1
            continue

        try:
            if platform == "fb":
                page_id, token = igfb._fb_creds(cred)
                msgs = await igfb.fb_thread_messages(thread_id, token, max_messages)
            else:
                _ig_id, token = igfb._ig_creds(cred)
                msgs = await igfb.ig_thread_messages(thread_id, token, max_messages)
        except Exception as e:  # noqa: BLE001
            errors.append(f"會話 {thread_id} 拉取失敗: {e}")
            continue

        msgs = drop_comment_dm_noise(msgs)
        # ★ 一律按時間正序處理：Graph 回的是新→舊
        msgs.sort(key=lambda m: m.get("created_time") or "")
        n_threads += 1

        for m in msgs:
            ts = igfb_recv.to_dt(m.get("created_time"))
            # ★ 消息級水位：只處理「最後一則之後」的新消息（不重複處理、不覆蓋既有狀態）
            if w is not None and ts is not None and ts <= w:
                skipped_msgs += 1
                continue

            from_id, from_name = igfb_recv._from_parts(m.get("from") or {})
            norm = igfb_recv._norm_message(m or {})
            is_self = bool(self_id) and str(from_id) == str(self_id)
            direction = "outbound" if is_self else "inbound"
            mid = str(m.get("id") or m.get("mid") or "")
            if not mid:
                continue

            atts = norm["attachments"]
            if atts and direction == "inbound":
                try:
                    atts = await igfb_store.download_attachments(atts)
                except Exception as e:  # noqa: BLE001
                    log.warning("歷史附件下載失敗 %s: %s", mid, e)

            new_id = await asyncio.to_thread(
                igfb_store.land_dm, platform,
                {"message_id": mid, "message_type": norm["message_type"], "text": norm["text"]},
                acc,
                atts,
                direction=direction,
                conversation_key=customer_id or from_id,
                sender_name=customer_name or from_name,
                thread_id=thread_id,
                created_at=ts,
            )
            if new_id is None:
                continue
            if direction == "inbound":
                new_ids.append(new_id)
                n_new += 1
            else:
                n_out += 1

    handled, pending = (0, 0)
    if new_ids:
        handled, pending = await asyncio.to_thread(igfb_store.apply_reply_rule, platform, new_ids)
    return {"conversations": n_threads, "dm_new": n_new, "dm_out": n_out,
            "dm_handled": handled, "dm_pending": pending,
            "skipped_conversations": skipped_convs, "skipped_messages": skipped_msgs,
            "errors": errors}


async def _sync_posts(platform: str, native_id: str, acc: dict, self_id: str,
                      max_posts: int, max_comments: int, deadline: float) -> dict:
    """拉貼文 + 留言 → `igfb_post` / `igfb_comment`，並重算討論串的已回／未回。"""
    cred = acc.get("credential") or {}
    errors: list[str] = []
    if platform == "fb":
        page_id, page_token = igfb._fb_creds(cred)
        if not page_id or not page_token:
            return {"posts": 0, "comments_new": 0, "threads_recomputed": 0,
                    "skipped_comments": 0,
                    "errors": ["FB 憑證缺少 page_id / page_access_token，無法補拉貼文"]}
        posts = await igfb.fb_posts(page_id or native_id, page_token, max_posts)
    else:
        ig_id, _ig_token = igfb._ig_creds(cred)
        # ⚠️ IG 貼文/留言走 **FB 登入路徑**（graph.facebook.com + 粉專 token + IG business id）
        page_token = cred.get("page_access_token") or ""
        if not page_token:
            return {"posts": 0, "comments_new": 0, "threads_recomputed": 0,
                    "skipped_comments": 0,
                    "errors": ["IG 憑證缺少 page_access_token（粉專 token），無法補拉貼文留言"]}
        posts = await igfb.ig_posts(ig_id or native_id, page_token, max_posts)

    # ★ 留言增量水位：每篇貼文「已有留言的最後時間」
    cwm = await asyncio.to_thread(
        igfb_store.comment_watermarks, platform, [str(p.get("id") or "") for p in posts])

    n_posts = n_comments = n_threads = 0
    skipped_comments = 0
    for p in posts:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        await asyncio.to_thread(igfb_store.upsert_post, platform, acc, p)
        n_posts += 1
        if time.monotonic() > deadline:
            errors.append("已達單帳號時間上限，部分貼文留言未拉取")
            break
        try:
            if platform == "fb":
                _page, page_token = igfb._fb_creds(cred)
                raw = await igfb.fb_comments(pid, page_token, max_comments)
                items = _flatten_fb_comments(pid, raw)
            else:
                raw = await igfb.ig_comments(pid, cred.get("page_access_token") or "", max_comments)
                items = _flatten_ig_comments(pid, raw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"貼文 {pid} 留言拉取失敗: {e}")
            continue

        w = cwm.get(pid)
        # ★★ 只重算「這次真的**新增**留言」的討論串。
        #    - 傳 None 會重算整篇貼文 → 把客服手動標的「已忽略」翻回未處理（舊版就是這樣）
        #    - 已存在的留言不觸發重算 → 狀態原封不動
        new_roots: set[str] = set()
        for it in items:
            if not it.get("comment_id"):
                continue
            cts = it.get("created_at")
            if w is not None and cts is not None and cts <= w:
                skipped_comments += 1
                continue
            res = await asyncio.to_thread(
                igfb_store.land_comment, platform, it, acc, touch=False)
            if res and res.get("inserted"):
                n_comments += 1
                if res.get("root_id"):
                    new_roots.add(res["root_id"])
        if new_roots:
            n_threads += await asyncio.to_thread(
                igfb_store.recompute_comment_threads, platform, pid, self_id,
                sorted(new_roots))

    return {"posts": n_posts, "comments_new": n_comments, "threads_recomputed": n_threads,
            "skipped_comments": skipped_comments, "errors": errors}


async def sync_account(platform: str, native_id: str, account: dict | None = None,
                       *, max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
                       max_messages: int = DEFAULT_MAX_MESSAGES,
                       max_posts: int = DEFAULT_MAX_POSTS,
                       max_comments: int = DEFAULT_MAX_COMMENTS,
                       max_seconds: float = DEFAULT_MAX_SECONDS) -> dict:
    """補拉單一帳號的歷史。回一份可讀的統計（前端直接顯示）。"""
    acc = account or account_map.lookup(platform, native_id) or {}
    cred = acc.get("credential") or {}
    out = {
        "platform": platform,
        "platform_business_account_id": native_id,
        "supported": platform in SUPPORTED,
        # supported = 「這個平台有沒有歷史 API」；failed = 「這次跑有沒有炸」
        "failed": False,
        "conversations": 0, "dm_new": 0, "dm_out": 0, "dm_handled": 0, "dm_pending": 0,
        "skipped_conversations": 0, "skipped_messages": 0,
        "posts": 0, "comments_new": 0, "threads_recomputed": 0, "skipped_comments": 0,
        "errors": [],
    }
    if platform not in SUPPORTED:
        out["reason"] = {
            "whatsapp": "WhatsApp Cloud API 沒有查詢歷史對話的端點，只能靠 webhook 推送",
            "wechat": "微信客服消息只有推送，沒有可回溯拉取的歷史端點",
        }.get(platform, "此平台不支援歷史補拉")
        return out
    if not cred:
        out["supported"] = False
        out["reason"] = "Connector 沒有這個帳號的憑證（等 CRM 同步帳號映射）"
        out["errors"].append("缺少 credential")
        return out

    self_id = igfb_store.self_id(platform, acc) or native_id
    deadline = time.monotonic() + max(5.0, float(max_seconds))
    try:
        dm = await _sync_dm(platform, native_id, acc, self_id,
                            max_conversations, max_messages, deadline)
        out.update({k: dm[k] for k in ("conversations", "dm_new", "dm_out",
                                       "dm_handled", "dm_pending",
                                       "skipped_conversations", "skipped_messages")})
        out["errors"].extend(dm["errors"])
    except Exception as e:  # noqa: BLE001 —— 單帳號失敗不拖垮整批
        out["errors"].append(f"私訊補拉失敗: {type(e).__name__}: {e}")
        out["failed"] = True
    try:
        ps = await _sync_posts(platform, native_id, acc, self_id,
                               max_posts, max_comments, deadline)
        out["posts"] = ps["posts"]
        out["comments_new"] = ps["comments_new"]
        out["threads_recomputed"] = ps["threads_recomputed"]
        out["skipped_comments"] = ps["skipped_comments"]
        out["errors"].extend(ps["errors"])
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"貼文留言補拉失敗: {type(e).__name__}: {e}")
        out["failed"] = True
    return out


# ---------------------------------------------------------------- 批次

async def sync_many(items: list[dict], **kw) -> dict:
    """逐帳號補拉。`items` = `[{platform, platform_business_account_id}]`。

    ⚠️ 逐帳號容錯：任一個失敗只讓它自己帶 errors，不影響其他帳號。
    """
    results: list[dict] = []
    for it in items or []:
        plat = (it.get("platform") or "").strip()
        native = str(it.get("platform_business_account_id") or "").strip()
        if not plat or not native:
            continue
        try:
            results.append(await sync_account(plat, native, **kw))
        except Exception as e:  # noqa: BLE001
            log.warning("歷史補拉失敗 %s/%s: %s", plat, native, e)
            results.append({"platform": plat, "platform_business_account_id": native,
                            "supported": False, "reason": f"{type(e).__name__}: {e}",
                            "errors": [str(e)]})

    totals = {
        "accounts": len(results),
        "accounts_synced": sum(1 for r in results if r.get("supported")),
        "accounts_unsupported": sum(1 for r in results if not r.get("supported")),
        "accounts_failed": sum(1 for r in results if r.get("failed")),
        "dm_new": sum(int(r.get("dm_new") or 0) for r in results),
        "dm_out": sum(int(r.get("dm_out") or 0) for r in results),
        "dm_handled": sum(int(r.get("dm_handled") or 0) for r in results),
        "dm_pending": sum(int(r.get("dm_pending") or 0) for r in results),
        "skipped_messages": sum(int(r.get("skipped_messages") or 0) for r in results),
        "posts": sum(int(r.get("posts") or 0) for r in results),
        "comments_new": sum(int(r.get("comments_new") or 0) for r in results),
        "skipped_comments": sum(int(r.get("skipped_comments") or 0) for r in results),
        "errors": [e for r in results for e in (r.get("errors") or [])],
    }
    return {"ok": True, "items": results, "totals": totals}


# ======================================================================
# 貼文縮圖續命（週期性）
#
# ★ 為什麼需要（2026-10-20 小jo 回報「facebook 的貼文圖片不見了」）：
#   `igfb_post.media_url` 存的是 Meta CDN **簽名網址**（`oe=` 是到期時間），
#   有效期約 24 小時，過期後一律 403 —— 實測 8 篇貼文的簽名全在 10-18 18:00 UTC
#   到期，FB 與 IG 的圖同時全掛。
#
#   `igfb_store.upsert_post()` 現在會在寫入時把圖抓下來存 `media/post_cache/`
#   （sha 去重、永久），`media_url` 改存 `/files/post_cache/...`。
#   但**貼文只在「重新整理」時同步** → 沒人按就一直沒人續命。
#   所以加這個週期任務：每 `POST_MEDIA_REFRESH_HOURS` 小時（預設 6）拉一次最近貼文，
#   把「還沒有本地快取」的圖補下來。已有快取的貼文**不會重下**（`upsert_post` 內判斷）。
#
#   ⚠️ 成本很低：每個帳號一次 Graph 呼叫；圖片只在首次（或快取檔遺失時）才下載。
# ======================================================================

_POST_REFRESH_LIMIT = 25


def _igfb_accounts() -> list[dict]:
    """帳號映射裡啟用中的 IG/FB 帳號（含憑證）。"""
    from app.core import db

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, platform_business_account_id, business_id,"
                "       bp_account_id, credential"
                "  FROM connector_account_map"
                " WHERE enabled AND platform IN ('fb', 'ig')"
                " ORDER BY platform, platform_business_account_id")
            cols = ("platform", "platform_business_account_id", "business_id",
                    "bp_account_id", "credential")
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _cred_of(acc: dict) -> dict:
    """account map 的 credential（jsonb 有時回字串）。"""
    cred = acc.get("credential") or {}
    if isinstance(cred, str):
        import json as _json
        try:
            cred = _json.loads(cred)
        except Exception:  # noqa: BLE001
            cred = {}
    return cred or {}


async def fetch_account_posts(acc: dict, limit: int) -> list[dict]:
    """拉某個帳號最近 `limit` 篇貼文（已依平台走對的端點）。失敗丟例外。"""
    plat = acc["platform"]
    native = str(acc.get("platform_business_account_id") or "")
    cred = _cred_of(acc)
    if plat == "fb":
        page_id, token = igfb._fb_creds(cred)
        if not page_id or not token:
            raise RuntimeError("憑證缺少 page_id/page_access_token")
        return await igfb.fb_posts(page_id or native, token, limit)
    ig_id, _ig_tok = igfb._ig_creds(cred)
    token = cred.get("page_access_token") or ""
    if not token:
        raise RuntimeError("憑證缺少 page_access_token")
    return await igfb.ig_posts(ig_id or native, token, limit)


# 貼文可讀性探測的快取（同一帳號一小時內只探一次；見 probe_posts_readable）
_PERM_PROBE_AT: dict[str, float] = {}
_PERM_PROBE_LAST: dict[str, str] = {}
_PERM_PROBE_TTL = 3600.0


async def _alert_posts_unreadable(acc: dict, why: str) -> None:
    """把「讀不到貼文」上報到 CRM 的 `crm_alert`（ops 台可見）。

    `dedupe_key` 用「平台:原生ID」→ 同一個帳號反覆失敗只累加次數，不會洗版。
    """
    try:
        from app.worker import client as crm_client
        plat = str(acc.get("platform") or "")
        native = str(acc.get("platform_business_account_id") or "")
        await crm_client.report_alert(
            "meta_posts_unreadable", "warning",
            f"{plat.upper()} 帳號 {native} 讀不到貼文列表",
            detail={"platform": plat, "native_id": native,
                    "business_id": acc.get("business_id"),
                    "bp_account_id": acc.get("bp_account_id"),
                    "reason": why},
            dedupe_key=f"meta_posts_unreadable:{plat}:{native}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("貼文異常告警上報失敗: %s", e)


async def probe_posts_readable(acc: dict) -> str:
    """貼文拉回 0 篇時，判斷是「真的沒貼文」還是「**沒權限，被靜默過濾成空**」。

    回空字串＝沒問題（大概真的沒貼文）；回字串＝可直接給人看的失敗原因。

    ★ 為什麼要這支（2026-10-20 小jo「商家123也有一條新帖文，爲什麽不顯示」）：
      該粉專的 page token **沒有 `pages_read_engagement`**：
        · `GET /{page}/feed`        → **200 + `{"data": []}`**（不是報錯！）
        · `GET /{page}/published_posts` → **400 #283 Requires pages_read_engagement**
        · `GET /{page}?fields=posts{...}` → 欄位被**靜默丟掉**
      於是「拉不到貼文」在系統裡長得跟「真的沒貼文」一模一樣，只能靠人去猜。
      這裡用一條**只讀**的探針把差異講出來，並上報告警（ops 台可見）。

    ⚠️ 同一帳號一小時內只探一次（`_PERM_PROBE_TTL`）：沒權限的帳號會一直是 0 篇，
       每 5 分鐘探一次只是白打 Meta。
    """
    import time as _time

    plat = str(acc.get("platform") or "")
    native = str(acc.get("platform_business_account_id") or "")
    key = f"{plat}:{native}"
    now = _time.monotonic()
    last = _PERM_PROBE_AT.get(key)
    if last is not None and now - last < _PERM_PROBE_TTL:
        return _PERM_PROBE_LAST.get(key, "")
    _PERM_PROBE_AT[key] = now

    cred = _cred_of(acc)
    msg = ""
    try:
        if plat == "fb":
            page_id, token = igfb._fb_creds(cred)
            # published_posts 對權限最敏感：缺 pages_read_engagement 會明確 400 #283
            status, data = await igfb._get_json(
                f"{igfb.FB_BASE}/{igfb._ver()}/{page_id or native}/published_posts", token,
                {"fields": "id,created_time", "limit": 1})
            if status != 200:
                err = ((data or {}).get("error") or {})
                msg = (f"讀不到貼文列表：{err.get('message') or f'HTTP {status}'}"
                       f"（code={err.get('code')}）。請在 Meta 後台重新授權該粉專，"
                       f"勾選 pages_read_engagement（讀貼文／留言必需）後更新 token")
            else:
                got = len((data or {}).get("data") or [])
                if got:
                    msg = ("API 回 0 篇但 /published_posts 其實有資料 —— "
                           "可能是分頁或篩選異常，請回報")
        else:
            ig_id, _t = igfb._ig_creds(cred)
            token = cred.get("page_access_token") or ""
            status, data = await igfb._get_json(
                f"{igfb.FB_BASE}/{igfb._ver()}/{ig_id or native}", token,
                {"fields": "id,media_count,username"})
            if status != 200:
                err = ((data or {}).get("error") or {})
                msg = f"讀不到 IG 帳號：{err.get('message') or f'HTTP {status}'}"
            else:
                n = int((data or {}).get("media_count") or 0)
                if n > 0:
                    msg = (f"API 回 0 篇但這個 IG 帳號 media_count={n} —— "
                           f"token 權限不足或 media 端點被拒，請重新授權")
    except Exception as e:  # noqa: BLE001
        msg = f"貼文可讀性探測失敗：{type(e).__name__}: {e}"

    _PERM_PROBE_LAST[key] = msg
    return msg


async def sync_posts_once(limit: int = 8) -> dict:
    """**只同步貼文**（不含留言）—— 週期跑，讓新貼文自己冒出來。

    ★ 為什麼需要（2026-10-20 小jo「新貼文發佈檢測不到」）：
      · FB 可以用 `feed` webhook 即時收到自家發文（已補在 `igfb_recv` / `webhook`）；
      · **IG 沒有「發了新媒體」的 webhook 事件**（Meta 只推留言／提及／私訊），
        所以 IG 新貼文只能靠週期拉；
      · 這支同時是 FB 的保險：webhook 漏推、或服務當時沒開，都會被補回來。

    ⚠️ 成本：每帳號一次 `/feed`（或 `/media`）呼叫，`limit` 很小、不拉留言。
       `upsert_post()` 對已有本地快取的圖不會重下。`POST_SYNC_MINUTES=0` 可關閉。
    """
    stats = {"accounts": 0, "posts": 0, "new": 0, "errors": []}
    for acc in _igfb_accounts():
        plat = acc["platform"]
        native = str(acc.get("platform_business_account_id") or "")
        try:
            posts = await fetch_account_posts(acc, limit)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"{plat}/{native}: {type(e).__name__}: {e}")
            log.warning("貼文同步失敗 %s/%s: %s", plat, native, e)
            continue
        ids = [str(p.get("id") or "") for p in posts if p.get("id")]
        known = await asyncio.to_thread(igfb_store.known_post_ids, plat, ids)
        for p in posts:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            await asyncio.to_thread(igfb_store.upsert_post, plat, acc, p)
            if pid not in known:
                stats["new"] += 1
                log.info("新貼文（同步發現）%s/%s", plat, pid)
        stats["accounts"] += 1
        stats["posts"] += len(ids)

        # ★ 0 篇時問一句「是沒貼文，還是沒權限」——不然工作台上永遠是空清單，
        #   使用者只會覺得「新貼文檢測不到」。
        if not ids:
            why = await probe_posts_readable(acc)
            if why:
                tag = f"{plat}/{native}"
                stats["errors"].append(f"{tag}: {why}")
                log.warning("貼文讀取異常 %s: %s", tag, why)
                await _alert_posts_unreadable(acc, why)
    if stats["new"]:
        log.info("貼文同步：%s", {k: v for k, v in stats.items() if k != "errors"})
    return stats


async def refresh_post_media_once(limit: int = _POST_REFRESH_LIMIT) -> dict:
    """拉最近貼文 → `upsert_post`（順便把缺的縮圖存到本地）。回統計。"""
    stats = {"accounts": 0, "posts": 0, "errors": []}
    for acc in _igfb_accounts():
        plat = acc["platform"]
        native = str(acc.get("platform_business_account_id") or "")
        cred = acc.get("credential") or {}
        if isinstance(cred, str):                       # jsonb 有時回字串
            import json as _json
            try:
                cred = _json.loads(cred)
            except Exception:  # noqa: BLE001
                cred = {}
        try:
            if plat == "fb":
                page_id, token = igfb._fb_creds(cred)
                if not page_id or not token:
                    stats["errors"].append(f"fb/{native}: 憑證缺少 page_id/token")
                    continue
                posts = await igfb.fb_posts(page_id or native, token, limit)
            else:
                ig_id, _t = igfb._ig_creds(cred)
                token = cred.get("page_access_token") or ""
                if not token:
                    stats["errors"].append(f"ig/{native}: 憑證缺少 page_access_token")
                    continue
                posts = await igfb.ig_posts(ig_id or native, token, limit)

            for p in posts:
                if str(p.get("id") or ""):
                    await asyncio.to_thread(igfb_store.upsert_post, plat, acc, p)
            stats["accounts"] += 1
            stats["posts"] += len(posts)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"{plat}/{native}: {type(e).__name__}: {e}")
            log.warning("貼文縮圖續命失敗 %s/%s: %s", plat, native, e)
    if stats["posts"]:
        log.info("貼文縮圖續命：%s", {k: v for k, v in stats.items() if k != "errors"})
    return stats
