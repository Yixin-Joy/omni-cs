"""出站任務 worker：claim → 逐子項發送 → 寫 attempt → 回調 CRM（設計案 §10.2 / §11）。

子項模型：一個任務拆成 `sub_seq`（0 = 文字，之後每個附件一個），
`connector_outbound_attempt` 的 `UNIQUE(crm_task_id, sub_seq)` 保證**子項冪等**直：
重跑同一任務時，已 `sent` 的子項不會再發一次。
"""
from __future__ import annotations

import json
import logging
import os

from app import media
from app import account_map
from app.channels import base
from app.core import db
from app.worker import callback, client

log = logging.getLogger("connector")


# ---------------------------------------------------------------- 子項拆解

def build_sub_items(message_content) -> list[dict]:
    mc = message_content or {}
    if not isinstance(mc, dict):
        return []
    items: list[dict] = []
    seq = 0
    text = str(mc.get("text") or "").strip()
    if text:
        items.append({"sub_seq": seq, "kind": "text", "text": text})
        seq += 1
    for a in (mc.get("assets") or []):
        if not isinstance(a, dict):
            continue
        key = (a.get("media_key") or a.get("path") or "").strip()
        if not key:
            continue
        items.append({"sub_seq": seq, "kind": "asset", "media_key": key,
                      "caption": (a.get("caption") or "")})
        seq += 1
    return items


# ---------------------------------------------------------------- attempt

def _sent_seqs(task_id: int) -> set[int]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_seq FROM connector_outbound_attempt"
                " WHERE crm_task_id = %s AND status = 'sent'",
                (int(task_id),),
            )
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _record_attempt(task_id: int, sub_seq: int, platform: str, target: str,
                    res: base.SendResult) -> None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connector_outbound_attempt"
                " (crm_task_id, sub_seq, platform, target_id, status, platform_msg_id, error,"
                "  attempted_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, now())"
                " ON CONFLICT (crm_task_id, sub_seq) DO UPDATE SET"
                "  status = EXCLUDED.status, platform_msg_id = EXCLUDED.platform_msg_id,"
                "  error = EXCLUDED.error, attempted_at = now()",
                (int(task_id), int(sub_seq), platform, target,
                 "sent" if res.ok else "failed", res.platform_msg_id, res.error),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 發送

async def _send_one(platform: str, target: str, item: dict,
                    credential: dict | None = None, scope: str = "dm") -> base.SendResult:
    # ★ 留言回覆（scope=comment）：target 是 comment_id，走 Graph API 的留言端點
    if scope == "comment":
        if item["kind"] != "text":
            return base.SendResult(ok=False, error="留言回覆不支援附件")
        return await base.reply_comment(platform, target, item["text"], credential)

    if item["kind"] == "text":
        return await base.send_text(platform, target, item["text"], credential)

    key = item.get("media_key") or ""
    try:
        path = str(media.resolve(key))
    except (ValueError, FileNotFoundError) as e:
        # 媒體不存在 = 永久錯誤（不會因為重試而變好）
        return base.SendResult(ok=False, error=f"媒體不存在或不合法（{key}）: {e}")
    return await base.send_asset(platform, target, path, item.get("caption") or "",
                                 filename=os.path.basename(path), credential=credential)


# ---------------------------------------------------------------- 出站訊息落地

def record_outbound_message(task: dict, items: list[dict]) -> None:
    """把送出的回覆也寫進訊息表（工作台才看得到回覆）。

    **best-effort**：失敗只記 log，絕不因此讓任務失敗。
    """
    platform = task.get("platform") or ""
    target = task.get("end_user_sender_id") or ""
    native = task.get("platform_business_account_id") or ""
    bid = task.get("business_id")
    bp = task.get("bp_account_id")
    mid = f"out-{task.get('task_id')}"
    text = " ".join(i["text"] for i in items if i["kind"] == "text")
    # ★ 附件要寫**可渲染的 url**：工作台（`_files_of`）讀的是 `url` / `type`，
    #   只寫 media_key 的話前端拿不到任何網址 → 我方帶圖的回覆在氣泡裡**只顯示文字**。
    #   路徑用 `<PUBLIC_BASE_URL>/files/<media_key>`（前端會把它正規化成相對 `/files/...`，
    #   走 CRM 的代理）；PUBLIC_BASE_URL 沒設時退回相對 `/files/<media_key>`。
    assets = []
    for i in items:
        if i["kind"] != "asset":
            continue
        key = i.get("media_key") or ""
        assets.append({
            "media_key": key,
            "url": media.public_url(key) or (f"/files/{key.lstrip('/')}" if key else ""),
            "type": "image",
            "filename": os.path.basename(key),
            "caption": i.get("caption") or "",
        })
    mtype = "text" if not assets else ("image" if len(assets) == 1 else "mixed")
    payload = json.dumps({"text": text, "assets": assets}, ensure_ascii=False)

    scope = (task.get("message_content") or {}).get("scope") or "dm"

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if scope == "comment" and platform in ("ig", "fb"):
                # ★ 留言回覆：寫進 igfb_comment（我方回覆），讓留言串即時顯示
                cur.execute(
                    "INSERT INTO igfb_comment"
                    " (platform, post_id, comment_id, parent_id, from_id, from_name, text,"
                    "  like_count, replied, is_handled, created_at, last_activity_at,"
                    "  platform_business_account_id, business_id, bp_account_id)"
                    " VALUES (%s,"
                    "   (SELECT post_id FROM igfb_comment WHERE platform = %s AND comment_id = %s),"
                    "   %s, %s, %s, '客服', %s, 0, false, true, now(), now(), %s, %s, %s)"
                    " ON CONFLICT (platform, comment_id) DO UPDATE SET text = EXCLUDED.text",
                    (platform, platform, target, mid, target, native, text, native, bid, bp),
                )
                # 被回覆的那則標為已回
                cur.execute(
                    "UPDATE igfb_comment SET replied = true"
                    " WHERE platform = %s AND comment_id = %s",
                    (platform, target),
                )
            elif platform in ("wechat", "whatsapp"):
                cur.execute(
                    "INSERT INTO forward_log"
                    " (platform, message_id, sender_id, conversation_id, message_type,"
                    "  raw_content, forwarded_payload, direction, message_time, is_handled,"
                    "  platform_business_account_id, business_id, bp_account_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'outbound', now(), true,"
                    "         %s, %s, %s)",
                    (platform, mid, target, target, mtype, payload, payload, native, bid, bp),
                )
            elif platform in ("ig", "fb"):
                cur.execute(
                    "INSERT INTO igfb_dm"
                    " (platform, conversation_key, thread_id, sender_id, direction,"
                    "  message_type, text, attachments, message_id, created_at, is_handled,"
                    "  platform_business_account_id, business_id, bp_account_id)"
                    " VALUES (%s, %s, %s, %s, 'out', %s, %s, %s::jsonb, %s, now(), true,"
                    "         %s, %s, %s)",
                    (platform, target, target, target, mtype, text,
                     json.dumps(assets, ensure_ascii=False), mid, native, bid, bp),
                )
            else:
                return
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        log.warning("寫出站訊息失敗 task=%s: %s", task.get("task_id"), e)
    finally:
        conn.close()


# ---------------------------------------------------------------- 回調

async def _report(task_id: int, status: str, reason: str | None = None,
                  temp_error: bool | None = None) -> None:
    try:
        await client.update_task_status(task_id, status, reason, temp_error)
    except Exception as e:  # noqa: BLE001
        log.warning("回調 CRM 失敗 task=%s (%s)，落重試佇列: %s", task_id, status, e)
        callback.enqueue(task_id, status, reason, temp_error)


# ---------------------------------------------------------------- 主流程

async def process_task(task: dict) -> str:
    task_id = task["task_id"]
    platform = task.get("platform") or ""
    target = task.get("end_user_sender_id") or ""
    native_id = task.get("platform_business_account_id") or ""

    mc = task.get("message_content") if isinstance(task.get("message_content"), dict) else {}
    scope = (mc or {}).get("scope") or "dm"   # dm（預設）| comment

    # ★ 多租戶：發送憑證從帳號映射快取讀（真相在 CRM），不再讀 config/.env
    cred = account_map.credential_of(platform, native_id) if native_id else {}

    items = build_sub_items(task.get("message_content"))
    if scope == "comment":
        # 留言回覆只發文字；附件子項丟棄，避免整任務失敗
        items = [i for i in items if i["kind"] == "text"]
    if not items:
        await _report(task_id, "failed", "任務沒有可發送的內容", False)
        return "failed"

    done = _sent_seqs(task_id)
    failure: base.SendResult | None = None

    for item in items:
        if item["sub_seq"] in done:
            continue                      # ★ 冪等：已成功的不重發
        res = await _send_one(platform, target, item, cred, scope)
        _record_attempt(task_id, item["sub_seq"], platform, target, res)
        if not res.ok:
            failure = res
            break

    if failure is not None:
        if failure.uncertain:
            await _report(task_id, "uncertain", failure.error, True)
            return "uncertain"
        if failure.temp_error:
            await _report(task_id, "failed", failure.error, True)
            return "failed_temp"
        await _report(task_id, "failed", failure.error, False)
        return "failed"

    record_outbound_message(task, items)
    await _report(task_id, "sent")
    return "sent"


async def tick(limit: int = 0) -> dict:
    """跑一輪：認領 → 逐筆處理。回傳統計（自檢用）。"""
    try:
        tasks = await client.claim_tasks(limit=limit)
    except Exception as e:  # noqa: BLE001
        return {"claimed": 0, "error": f"認領失敗: {e}"}

    stats = {"claimed": len(tasks), "sent": 0, "failed_temp": 0, "failed": 0, "uncertain": 0}
    for t in tasks:
        try:
            outcome = await process_task(t)
        except Exception as e:  # noqa: BLE001
            log.exception("處理任務 %s 異常", t.get("task_id"))
            await _report(t.get("task_id"), "uncertain", f"worker 異常: {e}", True)
            outcome = "uncertain"
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats
