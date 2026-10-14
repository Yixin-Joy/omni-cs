"""IG/FB 來訊落地（收訊側）—— 寫入 `igfb_dm` / `igfb_comment`（+ `igfb_post` 佔位）。

★★ 為什麼不寫 `forward_log`：
  工作台讀 IG/FB 的資料來源是 **`igfb_dm`（私訊）/ `igfb_comment`（留言）/ `igfb_post`（貼文）**
  —— 見 `app/internal/router.py` 的 `_conv_dm_igfb_sql`、`_conv_comment_sql`、`/posts`，
  以及 CRM 的 `merchant-stats`。舊單體的 webhook 也是寫這兩張表
  （`app/channels/igfb_db.handle_webhook_events`）。
  ⚠️ 本模組建立前，webhook 誤用 `_land_local`（寫 `forward_log`）→ Meta 送達的 IG/FB
  訊息全部落到工作台讀不到的表中 → 症狀是「WhatsApp 正常，IG/FB 一則都收不到」。

對齊鍵：`platform_business_account_id`（FB = page id；IG = ig user id），
與 `account_map` / `forward_log` 一致。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.channels.igfb_recv import to_dt
from app.core import config
from app.core import db

log = logging.getLogger("connector")


# ---------------------------------------------------------------- 工具

def self_id(platform: str, account: dict | None) -> str:
    """我方在該平台的 id。

    多租戶：直接用帳號原生 id（FB = page id、IG = ig user id，兩者都是 `entry.id`）；
    查不到帳號時退回 config（單商家過渡期）。
    """
    if account and account.get("platform_business_account_id"):
        return str(account["platform_business_account_id"])
    return config.META_PAGE_ID if platform == "fb" else config.META_IG_USER_ID


def _media_ext(url: str) -> str:
    path = (url or "").split("?")[0]
    suffix = Path(path).suffix.lstrip(".")
    return suffix if suffix and len(suffix) <= 5 else "bin"


async def download_attachments(attachments: list[dict]) -> list[dict]:
    """把媒體 url 下載到 media/，改指向本機 `/files/<name>`（失敗保留原 url）。

    FB/IG 的媒體是簽名 CDN url，不需 auth（與舊單體 `igfb_db._download_attachments` 相同）。
    """
    out: list[dict] = []
    for i, att in enumerate(attachments or []):
        url = (att or {}).get("url") or ""
        if not url or att.get("type") == "sticker":
            out.append(att)
            continue
        fname = f"igfb_{int(time.time() * 1000)}_{i}.{_media_ext(url)}"
        save_path = Path(config.MEDIA_DIR) / fname
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                save_path.write_bytes(resp.content)
            a2 = dict(att)
            a2["url"] = f"/files/{fname}"
            a2["fileSize"] = save_path.stat().st_size
            out.append(a2)
        except Exception as e:  # noqa: BLE001 —— 下載失敗不影響訊息本身
            log.warning("IG/FB 媒體下載失敗 %s: %s", url, e)
            out.append(att)
    return out


# ---------------------------------------------------------------- 落地

def _account_cols(account: dict | None) -> tuple:
    acc = account or {}
    return (acc.get("platform_business_account_id"), acc.get("business_id"),
            acc.get("bp_account_id"))


def land_dm(platform: str, ev: dict, account: dict | None,
            attachments: list[dict] | None = None, *, direction: str = "inbound",
            conversation_key: str | None = None, sender_name: str | None = None,
            thread_id: str | None = None, created_at=None) -> int | None:
    """一則 IG/FB 私訊 → `igfb_dm`（冪等：`(platform, message_id)` 唯一）。

    ★ 回傳**新插入的列 id**；若該訊息已存在（衝突）回 `None`。
      歷史補拉靠這個判定「哪些是新消息」，才能只對新消息套用回覆規則
      （`apply_reply_rule`）而不覆蓋人工標記。
    """
    acc_native, biz_id, bp_id = _account_cols(account)
    mid = str(ev.get("message_id") or "") or f"in_{int(time.time() * 1000)}"
    ckey = conversation_key if conversation_key is not None else (ev.get("sender_id") or "")
    nm = sender_name if sender_name is not None else (ev.get("sender_name") or "")
    ts = created_at if created_at is not None else ev.get("created_at")
    is_handled = (direction != "inbound")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO igfb_dm (
                    platform, conversation_key, thread_id, sender_id, sender_name,
                    direction, message_type, text, attachments, message_id, created_at,
                    is_handled, platform_business_account_id, business_id, bp_account_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, message_id) DO NOTHING
                RETURNING id
                """,
                (platform, ckey, thread_id, ev.get("sender_id") or ckey, nm,
                 direction, ev.get("message_type") or "text", ev.get("text") or "",
                 json.dumps(attachments or [], ensure_ascii=False),
                 mid, ts, is_handled, acc_native, biz_id, bp_id),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row else None
    except Exception as e:  # noqa: BLE001 —— 落地失敗只記 log，不影響 webhook 回應
        log.warning("寫入 igfb_dm 失敗: %s", e)
        return None
    finally:
        conn.close()


def apply_reply_rule(platform: str, ids: list[int]) -> tuple[int, int]:
    """★ 補拉後的回覆規則（小jo 指定）：

    新入庫的**入站**私訊，若它之後（同一會話、時間更晚）已有至少一則我方回覆
    → `is_handled = true`（**不補紅點**）；
    之後沒有任何回覆 → `is_handled = false`（**補紅點**）。

    ⚠️ 只動這次新插入的列（`ids`），**不重算整個會話** —— 否則會把客服
       手動標的「已處理」又翻回未處理。

    回 `(已回覆數, 待處理數)`。⚠️ 不能用 `rowcount` —— UPDATE 的 rowcount 是
    「符合條件的列數」，不分值有沒有變，所以會全部算成 handled。
    """
    if not ids:
        return (0, 0)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE igfb_dm d
                   SET is_handled = EXISTS (
                         SELECT 1 FROM igfb_dm o
                          WHERE o.platform = d.platform
                            AND COALESCE(NULLIF(o.sender_id, ''), o.conversation_key)
                                = COALESCE(NULLIF(d.sender_id, ''), d.conversation_key)
                            AND o.direction IN ('out', 'outbound')
                            AND COALESCE(o.created_at, o.inserted_at)
                                > COALESCE(d.created_at, d.inserted_at))
                 WHERE d.id = ANY(%s)
                   AND d.direction IN ('in', 'inbound')
                RETURNING is_handled
                """,
                (list(ids),),
            )
            rows = cur.fetchall()
        conn.commit()
        handled = sum(1 for r in rows if r[0])
        return (handled, len(rows) - handled)
    except Exception as e:  # noqa: BLE001
        log.warning("套用回覆規則失敗: %s", e)
        return (0, 0)
    finally:
        conn.close()


def _thread_root(cur, platform: str, comment_id: str) -> str:
    """往上追溯討論串的頂層留言 id。"""
    cid = comment_id
    for _ in range(10):
        cur.execute(
            "SELECT parent_id FROM igfb_comment WHERE platform=%s AND comment_id=%s",
            (platform, cid))
        row = cur.fetchone()
        if not row or not row[0]:
            break
        cid = row[0]
    return cid


def _touch_thread_root(cur, platform: str, me: str, comment_id: str,
                       from_id: str, created_at) -> None:
    """依最新一則留言更新討論串頂層的處理狀態（客戶留言 → 亮紅點；我方 → 已回）。

    只在「比已記錄的活動時間新」時更新 → 重跑舊資料不會蓋掉較新狀態、
    也不會抹掉使用者手動標的「已忽略」。（與舊單體 `igfb_db._touch_thread_root` 相同）
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    root_id = _thread_root(cur, platform, comment_id)
    cur.execute(
        "SELECT last_activity_at FROM igfb_comment WHERE platform=%s AND comment_id=%s",
        (platform, root_id))
    row = cur.fetchone()
    if not row:
        return
    prev = row[0]
    if prev is not None and created_at <= prev:
        return
    is_self = bool(from_id) and str(from_id) == str(me)
    cur.execute(
        "UPDATE igfb_comment SET last_activity_at=%s, replied=%s, is_handled=%s"
        " WHERE platform=%s AND comment_id=%s",
        (created_at, is_self, is_self, platform, root_id))


def land_comment(platform: str, ev: dict, account: dict | None) -> None:
    """一則 IG/FB 留言 → `igfb_comment`（並確保 `igfb_post` 有佔位）。"""
    acc_native, biz_id, bp_id = _account_cols(account)
    me = self_id(platform, account)
    post_id = ev.get("post_id") or ""
    created_at = ev.get("created_at")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if post_id:
                # 貼文佔位（名字會在真正同步到貼文資料後被覆蓋）
                cur.execute(
                    """
                    INSERT INTO igfb_post (platform, post_id, name,
                                           platform_business_account_id, business_id, bp_account_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (platform, post_id) DO UPDATE
                      SET platform_business_account_id =
                            COALESCE(EXCLUDED.platform_business_account_id,
                                     igfb_post.platform_business_account_id),
                          business_id = COALESCE(EXCLUDED.business_id, igfb_post.business_id),
                          bp_account_id = COALESCE(EXCLUDED.bp_account_id, igfb_post.bp_account_id)
                    """,
                    (platform, post_id, "（貼文資訊待同步）", acc_native, biz_id, bp_id),
                )
            cur.execute(
                """
                INSERT INTO igfb_comment (
                    platform, post_id, comment_id, parent_id, from_id, from_name,
                    text, like_count, replied, created_at, last_activity_at,
                    platform_business_account_id, business_id, bp_account_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, comment_id) DO UPDATE
                  SET text = EXCLUDED.text, like_count = EXCLUDED.like_count,
                      from_name = EXCLUDED.from_name,
                      from_id = COALESCE(NULLIF(EXCLUDED.from_id, ''), igfb_comment.from_id),
                      parent_id = EXCLUDED.parent_id,
                      created_at = COALESCE(igfb_comment.created_at, EXCLUDED.created_at),
                      platform_business_account_id =
                            COALESCE(EXCLUDED.platform_business_account_id,
                                     igfb_comment.platform_business_account_id),
                      business_id = COALESCE(EXCLUDED.business_id, igfb_comment.business_id),
                      bp_account_id = COALESCE(EXCLUDED.bp_account_id, igfb_comment.bp_account_id)
                """,
                (platform, post_id, ev.get("comment_id") or "", ev.get("parent_id"),
                 ev.get("from_id") or "", ev.get("from_name") or "", ev.get("text") or "",
                 int(ev.get("like_count") or 0), created_at, created_at,
                 acc_native, biz_id, bp_id),
            )
            _touch_thread_root(cur, platform, me, ev.get("comment_id") or "",
                               ev.get("from_id") or "", created_at)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("寫入 igfb_comment 失敗: %s", e)
    finally:
        conn.close()


# ---------------------------------------------------------------- 貼文（歷史補拉）

def upsert_post(platform: str, account: dict | None, post: dict) -> None:
    """貼文 upsert（歷史補拉用）。

    ⚠️ 貼文列可能是先前由 `_ensure_post()` 建的「佔位列」（留言先到、貼文還沒同步），
       那種列只有 post_id 與佔位名稱；所以 `DO UPDATE` 一定要把時間/媒體/連結補上，
       空字串**不可**覆蓋既有好值 → 用 `NULLIF` 保護。
    """
    pid = str(post.get("id") or "").strip()
    if not pid:
        return
    acc_native, biz_id, bp_id = _account_cols(account)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO igfb_post (platform, post_id, name, media_type, media_url, permalink,
                                       like_count, comments_count, created_at,
                                       platform_business_account_id, business_id, bp_account_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, post_id) DO UPDATE
                  SET name = EXCLUDED.name,
                      media_type = COALESCE(NULLIF(EXCLUDED.media_type, ''), igfb_post.media_type),
                      media_url = COALESCE(NULLIF(EXCLUDED.media_url, ''), igfb_post.media_url),
                      permalink = COALESCE(NULLIF(EXCLUDED.permalink, ''), igfb_post.permalink),
                      like_count = EXCLUDED.like_count,
                      comments_count = EXCLUDED.comments_count,
                      created_at = COALESCE(EXCLUDED.created_at, igfb_post.created_at),
                      platform_business_account_id =
                            COALESCE(EXCLUDED.platform_business_account_id,
                                     igfb_post.platform_business_account_id),
                      business_id = COALESCE(EXCLUDED.business_id, igfb_post.business_id),
                      bp_account_id = COALESCE(EXCLUDED.bp_account_id, igfb_post.bp_account_id)
                """,
                (platform, pid,
                 post.get("message") or post.get("caption") or "",
                 post.get("type") or post.get("media_type") or "",
                 post.get("full_picture") or post.get("media_url") or "",
                 post.get("permalink_url") or post.get("permalink") or "",
                 int(post.get("like_count") or 0),
                 int(post.get("comments_count") or 0),
                 to_dt(post.get("created_time") or post.get("timestamp")),
                 acc_native, biz_id, bp_id),
            )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("寫入 igfb_post 失敗: %s", e)
    finally:
        conn.close()


def recompute_comment_threads(platform: str, post_id: str, self_id: str) -> int:
    """重算該貼文所有討論串的「已回／未回」（歷史補拉後跑一次）。

    ★ 為什麼不靠 `_touch_thread_root` 逐則推進：歷史拉回來的順序是
      **新→舊**（Graph API 慣例），逐則推進會依錯的順序決定狀態。
      這裡直接算「討論串（頂層 + 所有子孫）中最後一則是誰發的」：
        我方（`from_id` == `self_id`）→ `replied/is_handled = true`（不亮紅點）
        客戶 → `false`（亮紅點）
    回傳更新的討論串數。
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, comment_id, parent_id, from_id,"
                "       COALESCE(created_at, inserted_at) AS ts"
                "  FROM igfb_comment WHERE platform = %s AND post_id = %s"
                " ORDER BY COALESCE(created_at, inserted_at), id",
                (platform, post_id))
            rows = cur.fetchall()
            if not rows:
                return 0
            parent = {r[1]: r[2] for r in rows}

            def root_of(cid: str) -> str:
                seen = 0
                cur_id = cid
                while seen < 10:
                    p = parent.get(cur_id)
                    if not p:
                        break
                    cur_id = p
                    seen += 1
                return cur_id

            latest: dict[str, tuple] = {}   # root -> (ts, id, from_id)
            for r in rows:
                cid, from_id = r[1], r[3]
                rid = root_of(cid)
                cand = (r[4], r[0], from_id or "")
                if rid not in latest or (cand[0], cand[1]) > (latest[rid][0], latest[rid][1]):
                    latest[rid] = cand

            n = 0
            for rid, (_ts, _id, from_id) in latest.items():
                is_self = bool(from_id) and bool(self_id) and str(from_id) == str(self_id)
                cur.execute(
                    "UPDATE igfb_comment SET replied = %s, is_handled = %s, last_activity_at = %s"
                    " WHERE platform = %s AND comment_id = %s",
                    (is_self, is_self, _ts, platform, rid))
                n += cur.rowcount or 0
        conn.commit()
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("重算留言討論串失敗 %s/%s: %s", platform, post_id, e)
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 計數（紅點）

def count_pending(platform: str, account_id: str, scope: str) -> int:
    """某帳號在該平台的「未處理」筆數（工作台紅點）。

    - `scope='dm'`：wechat / whatsapp 讀 `forward_log`；ig / fb 讀 `igfb_dm`。
      條件＝`direction='inbound' AND is_handled=false`（與會話列表一致）。
    - `scope='comment'`：ig / fb 讀 `igfb_comment`。條件＝**頂層 + 未回 + 未忽略 + 非我方**
      （與 `igfb_db.pending_comment_counts()` 及 `/posts` 的 `new_comments` 一致）。
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if scope == "comment":
                cur.execute(
                    "SELECT count(*) FROM igfb_comment"
                    " WHERE platform=%s AND platform_business_account_id=%s"
                    "   AND parent_id IS NULL"
                    "   AND COALESCE(replied, false) = false"
                    "   AND COALESCE(is_handled, false) = false"
                    "   AND (from_id IS NULL OR from_id = '' OR from_id <> %s)",
                    (platform, account_id, account_id))
            elif platform in ("wechat", "whatsapp"):
                cur.execute(
                    "SELECT count(*) FROM forward_log"
                    " WHERE platform=%s AND platform_business_account_id=%s"
                    "   AND direction='inbound' AND COALESCE(is_handled, false) = false",
                    (platform, account_id))
            else:
                cur.execute(
                    "SELECT count(*) FROM igfb_dm"
                    " WHERE platform=%s AND platform_business_account_id=%s"
                    "   AND direction='inbound' AND COALESCE(is_handled, false) = false",
                    (platform, account_id))
            return int(cur.fetchone()[0] or 0)
    except Exception as e:  # noqa: BLE001 —— 計數失敗回 0，不影響畫面
        log.warning("pending-count 失敗 %s/%s(%s): %s", platform, account_id, scope, e)
        return 0
    finally:
        conn.close()
