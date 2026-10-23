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
        # ★ `filename` / `mime` 一定要往下帶：
        #   CRM 上傳時只拿得到 `media_key`（`<sha16>.<ext>`），原始檔名只有呼叫端知道。
        #   少了它，工作台氣泡與 WhatsApp document 都只能顯示 sha 檔名，
        #   而且判斷「是不是圖片」只能靠副檔名 → PDF 被當成圖（氣泡變破圖）。
        items.append({"sub_seq": seq, "kind": "asset", "media_key": key,
                      "caption": (a.get("caption") or ""),
                      "filename": (a.get("filename") or "").strip(),
                      "mime": (a.get("mime") or "").strip()})
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
    # ★ 用**原始檔名**（呼叫端帶下來的）而不是 sha 檔名：
    #   WhatsApp 的 document 訊息會把 `filename` 直接顯示給客戶，
    #   顯示 `7a1102f7f0eec5f8.pdf` 會被當成損壞檔；IG／FB 則用它判 mime。
    name = item.get("filename") or os.path.basename(path)
    # ★ 平台能力差異先擋掉：IG／FB 的 attachment 只吃圖片、微信客服消息只發圖。
    #   以前不擋 → 送 PDF 時 `igfb._guess_mime()` 一律回 image/png，Meta 端才報錯，
    #   錯誤訊息跟真正原因（根本不支援文件）完全對不上。
    if platform in ("ig", "fb", "wechat") and not _is_image(name, item.get("mime") or ""):
        return base.SendResult(
            ok=False,
            error=f"{platform} 私訊不支援非圖片附件（{name}）；可改用 WhatsApp 或改放圖片")
    return await base.send_asset(platform, target, path, item.get("caption") or "",
                                 filename=name, credential=credential)


# ---------------------------------------------------------------- 出站訊息落地

def _mark_inbound_handled(cur, platform: str, target: str, scope: str) -> int:
    """把「早於現在」的客戶來訊標為已處理（紅點＝來訊且未處理）。

    ★ 為什麼在**這裡**（Connector 出站落地）而不是 CRM 建任務時：
      紅點語義是「這則客戶訊息有沒有被回應」。任務建了 ≠ 送出去了（可能重試、
      可能失敗），只有**回覆真的落地**時把它標掉才對。舊單體也是送成功後才標
      （`igfb_db.mark_handled` / `autoreply_db.mark_forward_log_handled`）。

    ⚠️ 用「早於這則出站訊息」而不是「把整個會話標掉」：回覆在途時客戶又發了
       新訊息，那則還沒被回答，紅點必須留著。
    ⚠️ 留言（scope='comment'）在上面的分支已把 `replied` 設 true，
       而留言的 pending 條件本來就看 `replied` —— 這裡不用再動。
    """
    if scope == "comment":
        return 0
    if platform in ("wechat", "whatsapp"):
        cur.execute(
            "UPDATE forward_log SET is_handled = true"
            " WHERE platform = %s AND sender_id = %s AND direction = 'inbound'"
            "   AND COALESCE(is_handled, false) = false"
            "   AND COALESCE(message_time, created_at) < now()",
            (platform, target))
        return cur.rowcount
    cur.execute(
        "UPDATE igfb_dm SET is_handled = true"
        " WHERE platform = %s AND (sender_id = %s OR conversation_key = %s)"
        "   AND direction = 'inbound'"
        "   AND COALESCE(is_handled, false) = false"
        "   AND COALESCE(created_at, inserted_at) < now()",
        (platform, target, target))
    return cur.rowcount


_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _is_image(name: str, mime: str = "") -> bool:
    if (mime or "").lower().startswith("image/"):
        return True
    return (name or "").split("?")[0].lower().endswith(_IMG_EXT)


def record_outbound_message(task: dict, items: list[dict]) -> None:
    """把送出的回覆也寫進訊息表（工作台才看得到回覆）。

    **best-effort**：失敗只記 log，絕不因此讓任務失敗。

    ★★ 落地格式必須讓工作台**渲染得出附件**（2026-10-22 修 P0）：
      工作台讀 `Connector._files_of()` ——
        · ig / fb    → `igfb_dm.attachments`（要 `url` / `type` / `filename`）
        · wechat / wa → `forward_log.forwarded_payload.uploadedFiles`（要 `url` / `filename`）
      舊寫法對 wechat / whatsapp 只寫 `{"text", "assets"}` —— `uploadedFiles` 根本不存在
      → `_files_of()` 回空陣列 → **客戶收到檔案、工作台氣泡只有文字**（就是這則回報）。
      ⚠️ 兩個 key 都要寫：`assets` 是本系統的原始紀錄，`uploadedFiles` 是工作台的渲染契約。
    """
    platform = task.get("platform") or ""
    target = task.get("end_user_sender_id") or ""
    native = task.get("platform_business_account_id") or ""
    bid = task.get("business_id")
    bp = task.get("bp_account_id")
    mid = f"out-{task.get('task_id')}"
    text = " ".join(i["text"] for i in items if i["kind"] == "text")

    # ★ 附件要寫**可渲染的 url**：工作台（`_files_of`）讀的是 `url` / `type`，
    #   只寫 media_key 的話前端拿不到任何網址 → 我方帶檔的回覆在氣泡裡**只顯示文字**。
    #   路徑用 `<PUBLIC_BASE_URL>/files/<media_key>`（前端會把它正規化成相對 `/files/...`，
    #   走 CRM 的代理）；PUBLIC_BASE_URL 沒設時退回相對 `/files/<media_key>`。
    assets = []
    for i in items:
        if i["kind"] != "asset":
            continue
        key = i.get("media_key") or ""
        name = i.get("filename") or os.path.basename(key)
        img = _is_image(name, i.get("mime") or "")
        size = 0
        try:
            size = int(os.path.getsize(str(media.resolve(key))))
        except Exception:  # noqa: BLE001 —— 量不到大小不影響送出，只是氣泡不顯示大小
            size = 0
        assets.append({
            "media_key": key,
            "url": media.public_url(key) or (f"/files/{key.lstrip('/')}" if key else ""),
            "type": "image" if img else "file",
            "mime": i.get("mime") or "",
            "filename": name,
            "fileSize": size,
            "caption": i.get("caption") or "",
        })

    if not assets:
        mtype = "text"
    elif len(assets) == 1:
        mtype = assets[0]["type"]          # image / file
    else:
        mtype = "mixed"

    # ig / fb 的附件格式（工作台直接讀 attachments）
    att_payload = json.dumps(assets, ensure_ascii=False)
    # wechat / whatsapp 的附件格式：`uploadedFiles` 是舊單體沿用下來的渲染契約
    uploaded = [{"url": a["url"], "filename": a["filename"], "fileSize": a["fileSize"],
                 "isImage": a["type"] == "image"} for a in assets]
    raw_obj = {"text": text, "assets": assets}
    fwd_obj = {"text": text, "assets": assets}
    if uploaded:
        fwd_obj["uploadedFiles"] = uploaded
    raw_payload = json.dumps(raw_obj, ensure_ascii=False)
    fwd_payload = json.dumps(fwd_obj, ensure_ascii=False)

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
                    " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'outbound', now(), true,"
                    "         %s, %s, %s)",
                    (platform, mid, target, target, mtype, raw_payload, fwd_payload,
                     native, bid, bp),
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
                     att_payload, mid, native, bid, bp),
                )
            else:
                return
            # ★★ 回覆真的送出去了 → 把這則之前的客戶來訊標為已處理（消除未讀紅點）。
            #    舊單體就是這樣做的（IG/FB：`igfb_db.mark_handled(channel, to)`；
            #    微信/WhatsApp：`autoreply_db.mark_forward_log_handled`），
            #    搬到 Connector 時**漏掉了** → 症狀：AI 明明回了，紅點還在。
            #    ⚠️ 條件用「早於這則出站訊息」的 inbound：回覆在途時客戶又發新訊息
            #       不會被誤標（那則還沒被回答，紅點該留著）。
            _mark_inbound_handled(cur, platform, target, scope)
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
    if scope == "comment":
        # ★ 留言回覆的 target **必須是 comment_id**（Graph 端點吃的是留言節點）。
        #   自動回復那條任務的 `end_user_sender_id` 存的是客戶 PSID（方便追溯），
        #   comment_id 另放在 message_content 裡 —— 所以要覆寫。
        #   ⚠️ 原本沒有這一段：scope 一旦是 comment 而 target 還是 PSID，
        #      就會 POST /{psid}/comments → Meta 400 `(#10) 不在允許期間`。
        #   （人工回覆那條把 sender_id 直接設成 comment_id，所以兩邊都兼容）
        comment_target = str(mc.get("comment_id") or "").strip()
        if comment_target:
            target = comment_target

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
