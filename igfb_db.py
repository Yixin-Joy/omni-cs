"""IG/FB 收件匣的 DB 讀寫 + 歷史同步。

表：igfb_dm（私訊）、igfb_comment（留言）、igfb_post（貼文快取）。
同步：從 Graph API 拉歷史 → 下載媒體 → upsert 到 DB（去重）。
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

import config
import igfb

log = logging.getLogger("wechat-forwarder")

# 排序用的下限時間（避免 None 比較錯誤）
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------- 底層 DB ----------

def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> None:
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _self_id(platform: str) -> str:
    return config.META_PAGE_ID if platform == "fb" else config.META_IG_USER_ID


def _to_iso(dt):
    return dt.isoformat().replace("+00:00", "Z") if dt else ""


# ---------- 私訊 ----------

async def _download_attachments(attachments: list[dict]) -> list[dict]:
    """把媒體 url 下載到 media/，改指向 /files/ 本地路徑（失敗保留原 url）。"""
    out = []
    for att in attachments or []:
        url = att.get("url", "")
        if not url or att.get("type") == "sticker":
            out.append(att)
            continue
        ext = igfb.media_ext(url)
        fname = f"igfb_{int(time.time() * 1000)}_{len(out)}.{ext}"
        save_path = Path(config.MEDIA_DIR) / fname
        try:
            Path(config.MEDIA_DIR).mkdir(exist_ok=True)
            await igfb.download_media(url, str(save_path))
            a2 = dict(att)
            a2["url"] = f"/files/{fname}"
            a2["fileSize"] = save_path.stat().st_size
            out.append(a2)
        except Exception as e:
            log.warning("IG/FB 媒體下載失敗 %s: %s", url, e)
            out.append(att)
    return out


async def ingest_dm(platform: str, conversation_key: str, thread_id: str | None, sender_id: str,
                    sender_name: str, direction: str, message_type: str, text: str,
                    attachments: list[dict], message_id: str, created_at) -> None:
    attachments = await _download_attachments(attachments)
    mid = message_id or f"out_{int(time.time() * 1000)}"
    # 我方回覆（outbound）視為已處理；客戶來訊（inbound）預設未處理
    is_handled = (direction == "outbound")
    await asyncio.to_thread(_execute, """
        INSERT INTO igfb_dm (platform, conversation_key, thread_id, sender_id, sender_name,
                             direction, message_type, text, attachments, message_id, created_at, is_handled)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        ON CONFLICT (platform, message_id) DO NOTHING
    """, (platform, conversation_key, thread_id, sender_id, sender_name, direction,
          message_type, text, json.dumps(attachments, ensure_ascii=False), mid, created_at, is_handled))


def _other_participant(participants: list[dict], self_id: str) -> dict:
    for p in participants or []:
        if str(p.get("id") or "") != self_id:
            return p
    return {}


def _sec_of(value) -> int | None:
    """把訊息時間轉成整數秒（無法解析回 None）。"""
    dt = igfb.to_dt(value)
    return int(dt.timestamp()) if dt else None


def _drop_comment_dm_noise(msgs: list[dict]) -> list[dict]:
    """濾掉 FB「留言轉私訊」造成的噪音訊息。

    使用者對貼文留言後，FB 會把留言原文複製進 Messenger 並附一則系統通知
    （判斷見 igfb.is_system_dm_notice）。這類內容會污染私訊列表 —— 真實對話的
    預覽被系統文字蓋掉、又憑空多出一則未讀。做法：
      1) 系統通知本身一律丟棄；
      2) 與系統通知同一秒的訊息也丟棄 —— 那是 FB 複製過來的留言原文
         （實測同一秒成對出現），真人在同一秒另外私訊的機率極低。
    """
    notice_secs: set[int] = set()
    for m in msgs:
        if igfb.is_system_dm_notice(igfb._norm_message(m)["text"]):
            sec = _sec_of(m.get("created_time"))
            if sec is not None:
                notice_secs.add(sec)

    kept: list[dict] = []
    for m in msgs:
        if igfb.is_system_dm_notice(igfb._norm_message(m)["text"]):
            continue
        sec = _sec_of(m.get("created_time"))
        if sec is not None and sec in notice_secs:
            continue
        kept.append(m)
    return kept


async def sync_dms(platform: str) -> int:
    """拉取私訊歷史（會話 + 訊息）→ upsert。回傳處理的會話數。"""
    if platform == "fb":
        convs = await igfb.fb_conversations()
        self_id = config.META_PAGE_ID
    else:
        convs = await igfb.ig_conversations()
        self_id = config.META_IG_USER_ID

    count = 0
    for conv in convs:
        thread_id = str(conv.get("id") or "")
        if not thread_id:
            continue
        other = _other_participant(conv.get("participants", {}).get("data", []), self_id)
        customer_id = str(other.get("id") or "")
        customer_name = other.get("name") or other.get("username") or ""

        msgs = await (igfb.fb_thread_messages(thread_id) if platform == "fb"
                      else igfb.ig_thread_messages(thread_id))
        msgs = _drop_comment_dm_noise(msgs)
        for m in msgs:
            fid, fname = igfb._from_parts(m.get("from") or {})
            norm = igfb._norm_message(m)
            direction = "outbound" if fid == self_id else "inbound"
            conv_key = customer_id or fid
            await ingest_dm(
                platform=platform,
                conversation_key=conv_key,
                thread_id=thread_id,
                sender_id=customer_id or fid,
                sender_name=customer_name or fname,
                direction=direction,
                message_type=norm["message_type"],
                text=norm["text"],
                attachments=norm["attachments"],
                message_id=str(m.get("id") or m.get("mid") or ""),
                created_at=igfb.to_dt(m.get("created_time")),
            )
        count += 1
    return count


def _execute_rowcount(sql: str, params: tuple = ()) -> int:
    """執行 UPDATE/DELETE 並回傳受影響列數。"""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


async def _bg_sync_dm_names(platform: str) -> None:
    """背景補私訊名稱（只補沒有名字的會話），失敗只記 log。"""
    try:
        await sync_dm_names(platform)
    except Exception as e:
        log.warning("補私訊名稱失敗 %s: %s", platform, e)


async def sync_dm_names(platform: str) -> int:
    """補齊私訊會話的顯示名稱（只查名稱、不抓訊息，成本低）。

    webhook 推來的私訊**不帶使用者名稱**，所以新會話一開始都是空的；
    由這裡回頭補上（FB 只能從 conversations 的 participants 取，IG 可逐一查）。
    """
    pairs: list[tuple[str, str]] = []
    if platform == "fb":
        convs = await igfb.fb_conversations()
        self_id = config.META_PAGE_ID
        for conv in convs:
            other = _other_participant((conv.get("participants") or {}).get("data", []), self_id)
            cid = str(other.get("id") or "")
            nm = other.get("name") or other.get("username") or ""
            if cid and nm:
                pairs.append((cid, nm))
    else:
        # IG：只針對資料庫裡還沒有名字的會話去查，避免無謂的 API 呼叫
        nameless = _query("""
            SELECT DISTINCT conversation_key FROM igfb_dm
            WHERE platform='ig' AND (sender_name IS NULL OR sender_name='')
        """)
        for r in nameless:
            nm = await igfb.ig_user_name(r["conversation_key"])
            if nm:
                pairs.append((r["conversation_key"], nm))

    updated = 0
    for cid, nm in pairs:
        updated += _execute_rowcount("""
            UPDATE igfb_dm SET sender_name=%s
            WHERE platform=%s AND conversation_key=%s AND (sender_name IS NULL OR sender_name='')
        """, (nm, platform, cid))
    return updated


def list_dm_conversations(platform: str) -> list[dict]:
    """以用戶為單位分組私訊，含未處理訊息數與自動回復未命中數。"""
    rows = _query("""
        SELECT conversation_key, sender_id, sender_name, direction, message_type, text, attachments,
               created_at, inserted_at, is_handled, auto_reply_state
        FROM igfb_dm WHERE platform = %s ORDER BY COALESCE(created_at, inserted_at) ASC
    """, (platform,))

    groups: dict[str, list[dict]] = {}
    for r in rows:
        r["_t"] = r["created_at"] or r["inserted_at"]
        groups.setdefault(r["conversation_key"], []).append(r)

    out = []
    for ckey, rs in groups.items():
        # 未讀 = inbound 且 is_handled=false 的訊息數
        unread = sum(1 for r in rs
                     if r["direction"] == "inbound" and not r.get("is_handled"))
        # 自動回復未命中且尚未處理 → 列表要標出來
        auto_miss = sum(1 for r in rs
                        if r["direction"] == "inbound" and not r.get("is_handled")
                        and r.get("auto_reply_state") == "miss")
        last = rs[-1]
        out.append({
            "conversation_key": ckey,
            "sender_id": last.get("sender_id") or ckey,
            "name": _latest_name(rs),
            "last_preview": _preview(last),
            "last_direction": last.get("direction"),
            "last_time": _to_iso(last.get("_t")),
            "unread_count": unread,
            "auto_miss_count": auto_miss,
        })
    out.sort(key=lambda c: c["last_time"] or "", reverse=True)
    return out


def _latest_name(rows: list[dict]) -> str:
    for r in reversed(rows):
        if r.get("sender_name"):
            return r["sender_name"]
    return "未知用戶"


def _preview(r: dict) -> str:
    atts = r.get("attachments") or []
    if atts:
        t = (atts[0].get("type") or "file")
        if r.get("text"):
            return r["text"]
        label = {"image": "[圖片]", "audio": "[語音]", "video": "[影片]", "sticker": "[貼圖]"}.get(t, "[檔案]")
        return label
    return r.get("text") or "[空訊息]"


def list_dm_messages(platform: str, conversation_key: str) -> list[dict]:
    rows = _query("""
        SELECT conversation_key, sender_id, sender_name, direction, message_type, text, attachments,
               created_at, inserted_at, is_handled, auto_reply_state, auto_reply_tier, auto_reply_detail,
               message_id
        FROM igfb_dm WHERE platform = %s AND conversation_key = %s
        ORDER BY COALESCE(created_at, inserted_at) ASC
    """, (platform, conversation_key))
    out = []
    for r in rows:
        out.append({
            # message_id / is_handled 給工作台「在聊天框右鍵某一則、要引擎回那一則」用
            "message_id": str(r.get("message_id") or ""),
            "is_handled": bool(r.get("is_handled")),
            "direction": r["direction"],
            "type": r["message_type"],
            "text": r["text"] or "",
            "attachments": r["attachments"] or [],
            "time": _to_iso(r["created_at"] or r["inserted_at"]),
            "auto_reply_state": r.get("auto_reply_state") or "",
            "auto_reply_tier": r.get("auto_reply_tier") or "",
            "auto_reply_detail": r.get("auto_reply_detail") or "",
        })
    return out


# ---------- 留言 ----------

def _flatten_fb_comments(post_id: str, comments: list[dict]) -> list[dict]:
    out = []
    for c in comments:
        cid = str(c.get("id") or "")
        frm = c.get("from") or {}
        out.append(_c(post_id, cid, None, frm, c.get("message"), c.get("like_count"), c.get("created_time")))
        for r in (c.get("comments") or {}).get("data", []) or []:
            rf = r.get("from") or {}
            out.append(_c(post_id, str(r.get("id") or ""), cid, rf, r.get("message"),
                          r.get("like_count"), r.get("created_time")))
    return out


def _flatten_ig_comments(post_id: str, comments: list[dict]) -> list[dict]:
    out = []
    for c in comments:
        cid = str(c.get("id") or "")
        frm = c.get("from") or {}
        uname = frm.get("username") or c.get("username") or ""
        out.append(_c(post_id, cid, None, {"id": frm.get("id", ""), "name": uname},
                      c.get("text"), c.get("like_count"), c.get("timestamp")))
        for r in (c.get("replies") or {}).get("data", []) or []:
            rf = r.get("from") or {}
            runame = rf.get("username") or r.get("username") or ""
            out.append(_c(post_id, str(r.get("id") or ""), cid, {"id": rf.get("id", ""), "name": runame},
                          r.get("text"), r.get("like_count"), r.get("timestamp")))
    return out


def _c(post_id, comment_id, parent_id, frm, text, like_count, created_time):
    name = (frm or {}).get("name") or (frm or {}).get("username") or ""
    return {
        "post_id": post_id,
        "comment_id": comment_id,
        "parent_id": parent_id,
        "from_id": str((frm or {}).get("id") or ""),
        "from_name": name or "用戶",
        "text": text or "",
        "like_count": int(like_count or 0),
        "created_at": igfb.to_dt(created_time),
    }


def _ensure_post(platform: str, post_id: str) -> None:
    """留言所屬貼文若還沒同步進來，先補一筆佔位，確保該留言在工作台看得到。

    佔位名稱會在真正同步到貼文資料後被覆蓋。
    """
    if not post_id:
        return
    _execute("""
        INSERT INTO igfb_post (platform, post_id, name) VALUES (%s, %s, %s)
        ON CONFLICT (platform, post_id) DO NOTHING
    """, (platform, post_id, "（貼文資訊待同步）"))


def ingest_comments(platform: str, items: list[dict]) -> None:
    """批次 upsert 留言（歷史同步用）。"""
    for it in items:
        _ensure_post(platform, it["post_id"])
        _execute("""
            INSERT INTO igfb_comment (platform, post_id, comment_id, parent_id, from_id, from_name,
                                      text, like_count, replied, created_at, last_activity_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s)
            ON CONFLICT (platform, comment_id) DO UPDATE
              SET text=EXCLUDED.text, like_count=EXCLUDED.like_count, from_name=EXCLUDED.from_name,
                  from_id=COALESCE(NULLIF(EXCLUDED.from_id, ''), igfb_comment.from_id),
                  parent_id=EXCLUDED.parent_id,
                  created_at=COALESCE(igfb_comment.created_at, EXCLUDED.created_at)
        """, (platform, it["post_id"], it["comment_id"], it["parent_id"], it["from_id"],
              it["from_name"], it["text"], it["like_count"], it["created_at"], it["created_at"]))
        _touch_thread_root(platform, it["comment_id"], it["from_id"], it["created_at"])


def ingest_comment_event(platform: str, post_id: str, comment_id: str, parent_id: str | None,
                         from_id: str, from_name: str, text: str, like_count: int, created_at) -> None:
    """webhook 即時留言 → upsert。"""
    _ensure_post(platform, post_id)
    _execute("""
        INSERT INTO igfb_comment (platform, post_id, comment_id, parent_id, from_id, from_name,
                                  text, like_count, replied, created_at, last_activity_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s)
        ON CONFLICT (platform, comment_id) DO UPDATE
          SET text=EXCLUDED.text, like_count=EXCLUDED.like_count, from_name=EXCLUDED.from_name,
              created_at=COALESCE(igfb_comment.created_at, EXCLUDED.created_at)
    """, (platform, post_id, comment_id, parent_id, from_id, from_name, text, like_count,
          created_at, created_at))
    _touch_thread_root(platform, comment_id, from_id, created_at)


def _thread_root(platform: str, comment_id: str) -> str:
    """往上追溯，取得該留言所屬討論串的頂層留言 id。"""
    cid = comment_id
    for _ in range(10):
        rows = _query("SELECT parent_id FROM igfb_comment WHERE platform=%s AND comment_id=%s",
                      (platform, cid))
        if not rows or not rows[0].get("parent_id"):
            break
        cid = rows[0]["parent_id"]
    return cid


def _touch_thread_root(platform: str, comment_id: str, from_id: str, created_at) -> None:
    """依最新的一則留言，更新討論串頂層的處理狀態。

    客戶留言 → 標為「未處理」（重新亮起）；我方留言 → 標為「已回」。
    只在「此留言比已記錄的活動時間新」時才更新，因此同步重跑舊資料
    不會蓋掉較新的狀態，也不會抹掉使用者手動標的「已忽略」。
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    root_id = _thread_root(platform, comment_id)
    rows = _query("SELECT last_activity_at FROM igfb_comment WHERE platform=%s AND comment_id=%s",
                  (platform, root_id))
    if not rows:
        return
    prev = rows[0]["last_activity_at"]
    if prev is not None and created_at <= prev:
        return
    is_self = bool(from_id) and from_id == _self_id(platform)
    _execute("""
        UPDATE igfb_comment SET last_activity_at=%s, replied=%s, is_handled=%s
        WHERE platform=%s AND comment_id=%s
    """, (created_at, is_self, is_self, platform, root_id))


def mark_replied(platform: str, comment_id: str) -> None:
    """工作台回覆留言後，把所屬討論串頂層標為已回。"""
    root_id = _thread_root(platform, comment_id)
    _execute("""
        UPDATE igfb_comment SET replied=true, is_handled=true, last_activity_at=now()
        WHERE platform=%s AND comment_id=%s
    """, (platform, root_id))


def add_own_reply(platform: str, parent_comment_id: str, new_comment_id: str, text: str) -> None:
    """把工作台發出的回覆寫進 igfb_comment，讓留言串即時顯示我方回覆。

    發送 API 只回傳新留言 id，不會自動出現在同步結果裡，故在此主動落地。
    """
    rows = _query("SELECT post_id FROM igfb_comment WHERE platform=%s AND comment_id=%s",
                  (platform, parent_comment_id))
    post_id = rows[0]["post_id"] if rows else ""
    now = datetime.now(timezone.utc)
    cid = new_comment_id or f"own_{int(time.time() * 1000)}"
    _execute("""
        INSERT INTO igfb_comment (platform, post_id, comment_id, parent_id, from_id, from_name,
                                  text, like_count, replied, is_handled, created_at, last_activity_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,0,false,false,%s,%s)
        ON CONFLICT (platform, comment_id) DO UPDATE SET text=EXCLUDED.text
    """, (platform, post_id, cid, parent_comment_id, _self_id(platform), "客服", text, now, now))
    mark_replied(platform, parent_comment_id)


def mark_comment_handled(platform: str, comment_id: str) -> None:
    """把某則留言（所屬討論串的頂層）標為已處理（不回覆，視為忽略）。"""
    root_id = _thread_root(platform, comment_id)
    _execute("UPDATE igfb_comment SET is_handled=true WHERE platform=%s AND comment_id=%s",
             (platform, root_id))


def mark_handled(platform: str, conversation_key: str) -> None:
    """把某會話的所有 inbound 訊息標為已處理，消除未讀紅點。"""
    _execute("""
        UPDATE igfb_dm SET is_handled = true
        WHERE platform = %s AND conversation_key = %s AND direction = 'inbound'
    """, (platform, conversation_key))


def list_comments(platform: str, post_id: str) -> list[dict]:
    """某貼文的留言，組成巢狀樹。

    頂層留言：後發表的在前；子留言：先發表的在前（依對話順序）。
    顯示層級最多 3 級 —— 第 1 級為頂層留言，第 2 級為其回覆，
    第 3 級把該回覆之下的所有後代攤平（不再往下縮排）。
    """
    rows = _query("""
        SELECT comment_id, parent_id, from_id, from_name, text, like_count, replied, is_handled,
               created_at, last_activity_at, auto_reply_state, auto_reply_tier, auto_reply_detail
        FROM igfb_comment WHERE platform=%s AND post_id=%s
    """, (platform, post_id))

    ids = {r["comment_id"] for r in rows}
    children: dict[str, list[dict]] = {}
    for r in rows:
        children.setdefault(r["parent_id"] or "", []).append(r)

    def _ct(r: dict):
        return r.get("created_at") or r.get("last_activity_at") or _EPOCH

    def descendants(node: dict) -> list[dict]:
        out = []
        for k in children.get(node["comment_id"], []):
            out.append(k)
            out.extend(descendants(k))
        return out

    def build(node: dict, level: int) -> dict:
        item = _comment_json(node)
        if level == 1:
            # 第 1 級：頂層留言 → 子留言各自成第 2 級（子留言依對話順序，舊→新）
            kids = sorted(children.get(node["comment_id"], []), key=_ct)
            item["replies"] = [build(k, 2) for k in kids]
        elif level == 2:
            # 第 2 級：把這條之下的所有後代攤平為第 3 級（不再往下縮排）
            item["replies"] = [_comment_json(k) for k in sorted(descendants(node), key=_ct)]
        else:
            item["replies"] = []
        return item

    # 頂層＝無父留言；父留言不在本貼文的孤兒也當頂層，避免留言消失
    # 頂層留言：後發表的在前
    tops = [r for r in rows if not r["parent_id"] or r["parent_id"] not in ids]
    return [build(t, 1) for t in sorted(tops, key=_ct, reverse=True)]


def pending_comment_counts() -> dict:
    """各平台「待處理」的討論串數（頂層留言未回、未忽略，且非我方自己發的）。"""
    rows = _query("""
        SELECT platform, from_id, count(*) AS n FROM igfb_comment
        WHERE parent_id IS NULL AND replied=false AND is_handled=false
        GROUP BY platform, from_id
    """)
    out = {"ig": 0, "fb": 0}
    for r in rows:
        p = r["platform"]
        if p not in out:
            continue
        fid = r["from_id"] or ""
        if fid and fid == _self_id(p):
            continue  # 我方自己發的頂層留言不算待處理
        out[p] += r["n"]
    return out


def _comment_json(r: dict) -> dict:
    return {
        "comment_id": r["comment_id"],
        "parent_id": r["parent_id"],
        "from_id": r["from_id"],
        "from_name": r["from_name"],
        "text": r["text"] or "",
        "like_count": r["like_count"] or 0,
        "replied": bool(r["replied"]),
        "is_handled": bool(r.get("is_handled")),
        "time": _to_iso(r["created_at"]),
        "auto_reply_state": r.get("auto_reply_state") or "",
        "auto_reply_tier": r.get("auto_reply_tier") or "",
        "auto_reply_detail": r.get("auto_reply_detail") or "",
    }


# ---------- 貼文 ----------

def upsert_posts(platform: str, posts: list[dict]) -> None:
    for p in posts:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        name = p.get("message") or p.get("caption") or ""
        media_url = p.get("full_picture") or p.get("media_url") or ""
        # ⚠ 貼文列可能是先前由 _ensure_post() 建的「佔位列」（留言先到、貼文還沒同步），
        #   那種列只有 post_id 和佔位名稱，created_at / media_type / permalink 都是空的。
        #   所以 DO UPDATE 一定要把這幾個欄位補上，否則佔位列永遠不會有時間（工作台顯示不出時間、
        #   也少了媒體與原文連結）。空字串不可覆蓋既有好值，故用 NULLIF 保護。
        _execute("""
            INSERT INTO igfb_post (platform, post_id, name, media_type, media_url, permalink,
                                   like_count, comments_count, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (platform, post_id) DO UPDATE
              SET name=EXCLUDED.name,
                  media_type=COALESCE(NULLIF(EXCLUDED.media_type, ''), igfb_post.media_type),
                  media_url=COALESCE(NULLIF(EXCLUDED.media_url, ''), igfb_post.media_url),
                  permalink=COALESCE(NULLIF(EXCLUDED.permalink, ''), igfb_post.permalink),
                  like_count=EXCLUDED.like_count,
                  comments_count=EXCLUDED.comments_count,
                  created_at=COALESCE(EXCLUDED.created_at, igfb_post.created_at)
        """, (platform, pid, name,
              p.get("type") or p.get("media_type") or "",
              media_url,
              p.get("permalink_url") or p.get("permalink") or "",
              int(p.get("like_count") or 0),
              int(p.get("comments_count") or 0),
              igfb.to_dt(p.get("created_time") or p.get("timestamp"))))


async def _fetch_posts(platform: str, max_posts: int = 50) -> list[dict]:
    """分頁拉取貼文清單。"""
    posts: list[dict] = []
    after = ""
    while len(posts) < max_posts:
        limit = min(25, max_posts - len(posts))
        page = await (igfb.fb_posts(limit=limit, after=after) if platform == "fb"
                      else igfb.ig_posts(limit=limit, after=after))
        data = page.get("data", [])
        posts.extend(data)
        after = page.get("after", "")
        if not data or not after:
            break
    return posts


async def sync_post_list(platform: str, max_posts: int = 50) -> int:
    """只同步貼文清單（不抓留言），成本低，供定時刷新用。

    新貼文不會產生 webhook，只能靠這條補進來，否則新貼文不會出現在工作台。
    """
    posts = await _fetch_posts(platform, max_posts)
    await asyncio.to_thread(upsert_posts, platform, posts)
    return len(posts)


async def sync_post_comments(platform: str, post_id: str) -> int:
    """只同步「某一篇貼文」的留言。

    工作台每 10 秒會對「當前打開的那篇」跑這條：只打 1~2 次 Graph API，
    成本遠低於 sync_posts_comments（全量），新留言才不用手動刷新。
    """
    pid = str(post_id or "").strip()
    if not pid:
        return 0
    if platform == "fb":
        comments = await igfb.fb_comments(pid)
        items = _flatten_fb_comments(pid, comments)
    else:
        comments = await igfb.ig_comments(pid)
        items = _flatten_ig_comments(pid, comments)
    await asyncio.to_thread(ingest_comments, platform, items)
    return len(items)


async def sync_posts_comments(platform: str, max_posts: int = 50) -> int:
    """拉取貼文（分頁，最多 max_posts 篇）+ 每篇留言 → upsert。"""
    posts = await _fetch_posts(platform, max_posts)
    await asyncio.to_thread(upsert_posts, platform, posts)

    for p in posts:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        try:
            if platform == "fb":
                comments = await igfb.fb_comments(pid)
                items = _flatten_fb_comments(pid, comments)
            else:
                comments = await igfb.ig_comments(pid)
                items = _flatten_ig_comments(pid, comments)
            await asyncio.to_thread(ingest_comments, platform, items)
        except Exception as e:
            log.warning("同步貼文留言失敗 post=%s: %s", pid, e)
    return len(posts)


def list_posts(platform: str, limit: int = 10, offset: int = 0) -> dict:
    self_id = _self_id(platform)
    rows = _query("""
        SELECT p.post_id, p.name, p.media_type, p.media_url, p.permalink, p.like_count,
               p.comments_count, p.created_at,
               (SELECT count(*) FROM igfb_comment c
                 WHERE c.platform=p.platform AND c.post_id=p.post_id
                   AND c.parent_id IS NULL AND c.replied=false AND c.is_handled=false
                   AND (c.from_id IS NULL OR c.from_id = '' OR c.from_id <> %s)) AS new_comments,
               (SELECT count(*) FROM igfb_comment c
                 WHERE c.platform=p.platform AND c.post_id=p.post_id AND c.parent_id IS NULL) AS top_comments
        FROM igfb_post p
        WHERE p.platform=%s
        ORDER BY p.created_at DESC NULLS LAST
        LIMIT %s OFFSET %s
    """, (self_id, platform, limit, offset))
    total = _query("SELECT count(*) AS n FROM igfb_post WHERE platform=%s", (platform,))
    items = []
    for r in rows:
        items.append({
            "post_id": r["post_id"],
            "name": r["name"] or "",
            "media_type": r["media_type"] or "",
            "media_url": r["media_url"] or "",
            "permalink": r["permalink"] or "",
            "like_count": r["like_count"] or 0,
            # 留言數以 DB 實算為準（API 的 comments_count 對 FB 拿不到、對 IG 含回覆，都不可靠）
            "comments_count": r["top_comments"] or 0,
            "created_at": _to_iso(r["created_at"]),
            "new_comments": r["new_comments"] or 0,
            "top_comments": r["top_comments"] or 0,
        })
    return {"posts": items, "has_more": offset + len(items) < (total[0]["n"] if total else 0)}


def get_post(platform: str, post_id: str) -> dict | None:
    rows = _query("""
        SELECT post_id, name, media_type, media_url, permalink, like_count, comments_count, created_at
        FROM igfb_post WHERE platform=%s AND post_id=%s
    """, (platform, post_id))
    if not rows:
        return None
    r = rows[0]
    # 留言數以 DB 實算為準
    cnt = _query("SELECT count(*) AS n FROM igfb_comment WHERE platform=%s AND post_id=%s AND parent_id IS NULL",
                 (platform, post_id))
    r["comments_count"] = cnt[0]["n"] if cnt else 0
    r["created_at"] = _to_iso(r["created_at"])
    return r


# ---------- webhook 事件落地 ----------

async def handle_webhook_events(events: list[dict]) -> None:
    # 延後匯入：autoreply 會再回頭匯入本模組，避免載入順序問題
    import autoreply

    for ev in events:
        t = ev["type"]
        try:
            if t in ("fb_dm", "ig_dm"):
                await ingest_dm(
                    platform=ev["platform"],
                    conversation_key=ev["sender_id"],
                    thread_id=None,
                    sender_id=ev["sender_id"],
                    sender_name=ev["sender_name"],
                    direction="inbound",
                    message_type=ev["message_type"],
                    text=ev["text"],
                    attachments=ev["attachments"],
                    message_id=ev["message_id"],
                    created_at=ev["created_at"],
                )
                # 私訊 webhook 不帶使用者名稱：背景補一次，讓新會話馬上顯示名字
                if not ev.get("sender_name"):
                    asyncio.create_task(_bg_sync_dm_names(ev["platform"]))
                # AI 自動回復（背景執行，非文字訊息由 autoreply 自行略過）
                autoreply.spawn_inbound(
                    channel=ev["platform"], scope="dm", text=ev["text"],
                    to=ev["sender_id"], message_id=ev["message_id"],
                    conversation_key=ev["sender_id"], sender_name=ev.get("sender_name") or "",
                )
            elif t in ("fb_comment", "ig_comment"):
                await asyncio.to_thread(
                    ingest_comment_event,
                    ev["platform"], ev["post_id"], ev["comment_id"], ev["parent_id"],
                    ev["from_id"], ev["from_name"], ev["text"], ev["like_count"], ev["created_at"],
                )
                # 我方自己發的留言不觸發自動回復（避免回覆自己，形成循環）
                if not igfb.is_self(ev["platform"], ev.get("from_id")):
                    autoreply.spawn_inbound(
                        channel=ev["platform"], scope="comment", text=ev["text"],
                        comment_id=ev["comment_id"], post_id=ev["post_id"],
                        conversation_key=ev["comment_id"],
                        sender_name=ev.get("from_name") or "",
                    )
        except Exception as e:
            log.warning("處理 webhook 事件失敗 %s: %s", t, e)
