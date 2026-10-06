"""批次補做：把「開啟自動回復之前就累積的未處理訊息」跑一輪自動回復。

使用情境
--------
客服原本關著自動回復，累積了一批沒回覆的客戶訊息／留言。按下開啟開關時，
工作台會先問「要不要一併自動回復這些未處理訊息」（先給數量），確定後：
  * 如果某個客戶有多則未回覆 → 工作台逐一彈窗問「逐條回 / 只回最後一則」
  * 再把計畫丟給這裡，在背景逐則處理，工作台輪詢進度顯示「已處理 x/y」

與 webhook 即時流程的差別（都是 autoreply.handle_inbound 的參數）
--------------------------------------------------------------
  * reprocess=True      這些訊息多半已被「認領」過（去重表有紀錄），要跳過重複檢查
  * ignore_cooldown=True 人工剛開啟開關，不該被「30 秒冷卻」擋掉（每日上限仍保留）
  * 未命中的仍照原流程標成「自動回復未命中，待處理」，降級人工
"""
import asyncio
import logging
import time
import uuid

import autoreply
import autoreply_db
import config

log = logging.getLogger("wechat-forwarder")

# 進行中的批次：放記憶體就好（服務重啟＝中止，工作台會看到 job 不存在）
_jobs: dict[str, dict] = {}
_JOB_TTL = 3600.0


def _supported(channel: str, scope: str) -> bool:
    return (channel, scope) in config.AUTOREPLY_SCOPES


# ---------- 未處理清單 ----------

def _dm_rows(channel: str) -> list[dict]:
    """IG/FB 私訊：每個會話一列，帶「最新一則未處理訊息」與該會話未處理則數。"""
    return autoreply_db.query("""
        SELECT DISTINCT ON (conversation_key)
               conversation_key,
               message_id,
               coalesce(nullif(sender_name, ''), '未知用戶') AS name,
               coalesce(text, '') AS text,
               coalesce(created_at, inserted_at) AS last_t,
               count(*) OVER (PARTITION BY conversation_key) AS n
        FROM igfb_dm
        WHERE platform=%s AND direction='inbound' AND is_handled=false
        ORDER BY conversation_key, coalesce(created_at, inserted_at) DESC
    """, (channel,))


def _wa_rows(channel: str) -> list[dict]:
    """WhatsApp：訊息在 forward_log，文字放在 forwarded_payload（微信不支援，不會走到這）。"""
    return autoreply_db.query("""
        SELECT DISTINCT ON (sender_id)
               sender_id AS conversation_key,
               message_id,
               coalesce(nullif(forwarded_payload->'webClientSender'->>'name', ''), sender_id) AS name,
               coalesce(forwarded_payload->>'messageContent', '') AS text,
               created_at AS last_t,
               count(*) OVER (PARTITION BY sender_id) AS n
        FROM forward_log
        WHERE platform=%s AND direction='inbound' AND is_handled=false
        ORDER BY sender_id, created_at DESC
    """, (channel,))


def _comment_threads(channel: str) -> list[dict]:
    """IG/FB 留言：每個「未回覆的頂層討論串」一列，取該串中最新一則客戶留言當回覆目標。

    狀態（replied / is_handled）掛在頂層留言上，代表整串「最後一句是不是我們說的」；
    所以要回的是串裡最新那句客戶的話，不是頂層那第一句。
    """
    import igfb
    return autoreply_db.query("""
        WITH RECURSIVE thread AS (
            SELECT platform, comment_id AS root_id, comment_id, post_id,
                   coalesce(from_id, '') AS from_id,
                   coalesce(from_name, '') AS from_name,
                   coalesce(text, '') AS text,
                   coalesce(created_at, inserted_at) AS ts
            FROM igfb_comment
            WHERE platform=%s AND parent_id IS NULL
              AND replied=false AND is_handled=false
            UNION ALL
            SELECT c.platform, t.root_id, c.comment_id, c.post_id,
                   coalesce(c.from_id, ''), coalesce(c.from_name, ''),
                   coalesce(c.text, ''), coalesce(c.created_at, c.inserted_at)
            FROM igfb_comment c
            JOIN thread t ON c.parent_id = t.comment_id AND c.platform = t.platform
        )
        SELECT DISTINCT ON (root_id)
               root_id, comment_id, post_id, from_name, text, ts
        FROM thread
        WHERE from_id <> %s
        ORDER BY root_id, ts DESC
    """, (channel, igfb.self_id(channel)))


def _rows_for(channel: str, scope: str) -> list[dict]:
    if scope == "comment":
        return _comment_threads(channel)
    if channel in ("ig", "fb"):
        return _dm_rows(channel)
    return _wa_rows(channel)


def pending_summary(channel: str, scope: str) -> dict:
    """某渠道＋場景的未處理訊息清單（工作台彈窗與計數用）。"""
    out = {"channel": channel, "scope": scope, "label": config.scope_label(channel, scope),
           "supported": _supported(channel, scope), "total": 0, "items": [], "multi": []}
    if not out["supported"]:
        return out
    try:
        rows = _rows_for(channel, scope)
    except Exception as e:
        log.warning("取未處理清單失敗 %s/%s: %s", channel, scope, e)
        return out

    items = []
    for r in rows:
        n = 1
        try:
            n = int(r.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        key = str(r.get("conversation_key") or r.get("root_id") or "")
        items.append({
            "key": key,
            "name": r.get("name") or r.get("from_name") or "未知用戶",
            "count": max(n, 1),
            "text": (r.get("text") or "")[:120],
            "message_id": str(r.get("message_id") or ""),
            "comment_id": str(r.get("comment_id") or "") if scope == "comment" else "",
            "post_id": str(r.get("post_id") or "") if scope == "comment" else "",
        })
    out["items"] = items
    out["total"] = sum(i["count"] for i in items)
    out["multi"] = [i for i in items if i["count"] > 1]
    return out


def all_pending() -> dict:
    """全部支援的渠道＋場景（全域開關的彈窗要列出每個渠道的數量）。"""
    scopes = [pending_summary(c, s) for c, s in config.AUTOREPLY_SCOPES]
    return {"scopes": scopes, "total": sum(s["total"] for s in scopes)}


# ---------- 建立要處理的訊息清單 ----------

def _collect(channel: str, scope: str, plan: dict) -> list[dict]:
    """把某渠道＋場景的未處理訊息攤平成「要逐則處理」的清單（舊→新）。

    plan：{會話key: 'all' | 'last'}，未指定時預設 'last'（只回最後一則）。
    """
    out = []
    if scope == "comment":
        for r in _comment_threads(channel):
            out.append({
                "channel": channel, "scope": scope,
                "key": str(r.get("root_id") or ""),
                "name": r.get("from_name") or "用戶",
                "text": r.get("text") or "",
                "comment_id": str(r.get("comment_id") or ""),
                "post_id": str(r.get("post_id") or ""),
                "message_id": "",
                "sender": "",
            })
        return out

    if channel in ("ig", "fb"):
        rows = autoreply_db.query("""
            SELECT conversation_key, message_id,
                   coalesce(nullif(sender_name, ''), '未知用戶') AS name,
                   coalesce(text, '') AS text
            FROM igfb_dm
            WHERE platform=%s AND direction='inbound' AND is_handled=false
            ORDER BY conversation_key, coalesce(created_at, inserted_at) ASC
        """, (channel,))
    else:
        rows = autoreply_db.query("""
            SELECT sender_id AS conversation_key, message_id,
                   coalesce(nullif(forwarded_payload->'webClientSender'->>'name', ''), sender_id) AS name,
                   coalesce(forwarded_payload->>'messageContent', '') AS text
            FROM forward_log
            WHERE platform=%s AND direction='inbound' AND is_handled=false
            ORDER BY sender_id, created_at ASC
        """, (channel,))

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(str(r.get("conversation_key") or ""), []).append(r)

    for key, msgs in grouped.items():
        mode = (plan or {}).get(key, "last")
        use = msgs if mode == "all" else msgs[-1:]
        for m in use:
            out.append({
                "channel": channel, "scope": scope, "key": key,
                "name": m.get("name") or key,
                "text": m.get("text") or "",
                "message_id": str(m.get("message_id") or ""),
                "comment_id": "", "post_id": "",
                "sender": key,
            })
    return out


# ---------- 背景批次 ----------

def start_job(targets: list[dict]) -> dict:
    """targets: [{channel, scope, plan:{key: 'all'|'last'}}] → 回 {job_id, total}。"""
    items: list[dict] = []
    for t in targets or []:
        channel = (t.get("channel") or "").strip().lower()
        scope = (t.get("scope") or "dm").strip().lower()
        if not _supported(channel, scope):
            log.warning("批次補做：略過不支援的組合 %s/%s", channel, scope)
            continue
        try:
            items.extend(_collect(channel, scope, t.get("plan") or {}))
        except Exception as e:
            log.warning("批次補做：收集 %s/%s 失敗: %s", channel, scope, e)

    job = {
        "job_id": uuid.uuid4().hex[:12],
        "total": len(items), "done": 0,
        "hit": 0, "miss": 0, "failed": 0, "skipped": 0,
        "current": "", "finished": False, "started_at": time.time(),
        "_items": items, "_last": time.time(),
    }
    _jobs[job["job_id"]] = job
    asyncio.create_task(_run_job(job))
    log.info("批次補做開始：job=%s 共 %d 則", job["job_id"], job["total"])
    return {"job_id": job["job_id"], "total": job["total"]}


async def _run_job(job: dict) -> None:
    try:
        for it in job["_items"]:
            job["current"] = "%s：%s" % (it.get("name") or "", (it.get("text") or "")[:24])
            job["_last"] = time.time()
            try:
                res = await autoreply.handle_inbound(
                    channel=it["channel"], scope=it["scope"], text=it.get("text") or "",
                    to=it.get("sender") or "", message_id=it.get("message_id") or "",
                    comment_id=it.get("comment_id") or "", post_id=it.get("post_id") or "",
                    conversation_key=it.get("key") or "", sender_name=it.get("name") or "",
                    reprocess=True, ignore_cooldown=True,
                )
                state = (res or {}).get("state") or ""
                if state == autoreply.STATE_HIT:
                    job["hit"] += 1
                elif state == autoreply.STATE_MISS:
                    job["miss"] += 1
                elif state == autoreply.STATE_FAILED:
                    job["failed"] += 1
                else:
                    job["skipped"] += 1
            except Exception as e:
                log.warning("批次補做單則失敗（%s，%s）: %s", it.get("channel"), it.get("key"), e)
                job["failed"] += 1
            job["done"] += 1
            job["_last"] = time.time()
            await asyncio.sleep(0)   # 每則之間讓出事件迴圈，webhook 不會被卡住
    finally:
        job["finished"] = True
        job["current"] = ""
        job["_last"] = time.time()
        log.info("批次補做結束：job=%s 命中 %d、未命中 %d、失敗 %d、略過 %d（共 %d）",
                 job["job_id"], job["hit"], job["miss"], job["failed"], job["skipped"], job["total"])
        _prune()


def _prune() -> None:
    """清掉一小時前結束的批次，避免記憶體一直長。"""
    now = time.time()
    for jid in [k for k, v in _jobs.items()
                if v.get("finished") and now - v.get("_last", now) > _JOB_TTL]:
        _jobs.pop(jid, None)


def get_job(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    return {k: v for k, v in job.items() if not k.startswith("_")}
