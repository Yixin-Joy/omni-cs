"""IG/FB 名稱補齊 + 回覆後清紅點的自檢。

對應 2026-10-14 小jo 回報的兩個問題：
  ① 「FB 私訊和留言無法識別用戶名」——兩個獨立成因：
     a. **聚合取錯列**：會話列表的名字取「最後一則」，而我方 outbound 的
        `sender_name` 是空的 → 一被回覆，名字就變空白（forward_log 那支
        早就有 FILTER，IG/FB 漏了）。
     b. **補名機制沒搬**：Meta 的 webhook 不帶使用者名稱，舊單體靠
        `sync_dm_names()` 背景補；搬進 Connector 時漏掉 → 新會話永遠沒名。
        留言更慘：Graph **不給**留言作者名（實測客戶的 `from{name}` 直接不回），
        只能用「作者 from_id ＝ 私訊 PSID」從私訊名字回填。
  ② 「新消息有 AI 回覆後仍然標紅點」——舊單體在回覆**送出成功後**呼叫
     `mark_handled` / `mark_forward_log_handled` 清紅點；搬過來時漏掉，
     於是 AI 明明回了，`is_handled` 還是 false → 紅點永遠不消。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> connector/tests/igfb_names_check.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "connector"))

import psycopg2  # noqa: E402

from app.channels import igfb, igfb_store  # noqa: E402
from app.core import config  # noqa: E402
from app.internal.router import _conv_dm_igfb_sql, _conv_comment_sql  # noqa: E402
from app.worker import outbound  # noqa: E402

MARK = "zz_names"
ACC = f"{MARK}_acc"          # platform_business_account_id
PSID = f"{MARK}_psid"        # 有名字的客戶（私訊 + 留言同一人）
PSID2 = f"{MARK}_psid2"      # 缺名的客戶（測補名）
FBC = f"{MARK}_cmt"          # 缺名留言

failures: list[str] = []
passes = 0


def check(cond, msg, extra=""):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


def db():
    return psycopg2.connect(config.DATABASE_URL)


def now():
    return datetime.now(timezone.utc)


def cleanup():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM igfb_dm WHERE platform_business_account_id = %s", (ACC,))
            cur.execute("DELETE FROM igfb_comment WHERE platform_business_account_id = %s", (ACC,))
        conn.commit()
    finally:
        conn.close()


def seed():
    """造：① 有名字的會話、② 缺名的會話、③ 缺名留言（作者＝①那位）。"""
    t0 = now()
    conn = db()
    try:
        with conn.cursor() as cur:
            # ① 客戶來訊有名字 → 我方 outbound 沒名字（最新一則）
            for direction, nm, mins in (("inbound", "Joy Li", 5), ("out", None, 4)):
                cur.execute(
                    "INSERT INTO igfb_dm(platform, conversation_key, sender_id, sender_name,"
                    " direction, message_type, text, created_at, is_handled,"
                    " platform_business_account_id)"
                    " VALUES ('fb', %s, %s, %s, %s, 'text', %s, %s, %s, %s)",
                    (PSID, PSID, nm, direction, f"{MARK} hi", t0 - timedelta(minutes=mins),
                     direction == "out", ACC))
            # ② 缺名的客戶來訊
            cur.execute(
                "INSERT INTO igfb_dm(platform, conversation_key, sender_id, sender_name,"
                " direction, message_type, text, created_at, is_handled,"
                " platform_business_account_id)"
                " VALUES ('fb', %s, %s, NULL, 'inbound', 'text', %s, %s, false, %s)",
                (PSID2, PSID2, f"{MARK} hi2", t0 - timedelta(minutes=3), ACC))
            # ③ 缺名留言（作者 = ① 那位，from_id ＝ 他的 PSID）
            cur.execute(
                "INSERT INTO igfb_comment(platform, post_id, comment_id, parent_id, from_id,"
                " from_name, text, replied, is_handled, created_at, last_activity_at,"
                " platform_business_account_id)"
                " VALUES ('fb', %s, %s, NULL, %s, '用戶', %s, false, false, %s, %s, %s)",
                (f"{MARK}_post", FBC, PSID, f"{MARK} 留言", t0 - timedelta(minutes=5), t0, ACC))
            # ④ 一則**已有真名**的留言（驗證不會被覆蓋）
            cur.execute(
                "INSERT INTO igfb_comment(platform, post_id, comment_id, parent_id, from_id,"
                " from_name, text, replied, is_handled, created_at, last_activity_at,"
                " platform_business_account_id)"
                " VALUES ('fb', %s, %s, NULL, %s, 'Real Name', %s, false, false, %s, %s, %s)",
                (f"{MARK}_post", FBC + "_keep", PSID, f"{MARK} 留言2",
                 t0 - timedelta(minutes=4), t0, ACC))
        conn.commit()
    finally:
        conn.close()


def conv_row(ckey: str):
    """用**真的**會話列表 SQL 取那一列（不是自己重寫一份 SQL）。"""
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(_conv_dm_igfb_sql(), ("fb", ACC, None, None, 50))
            for r in cur.fetchall():
                if r[0] == ckey:
                    return r
        return None
    finally:
        conn.close()


def comment_name(comment_id: str):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT from_name FROM igfb_comment WHERE comment_id = %s", (comment_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


print("\n[1] 會話名字取「最近一則有名稱的」（不能被我方回覆蓋成空白）")
cleanup()
seed()
row = conv_row(PSID)
check(row is not None, "種子會話存在")
check(row and (row[8] or "") == "Joy Li",
      "一被回覆，名字仍在（最後一則是我方 outbound、沒有 sender_name）",
      f"last_name={row[8]!r}" if row else "")

print("\n[2] 缺名會話 → 補名（模擬 Graph participants，不發真網路）")
_orig_fb_conv = igfb.fb_conversations


async def _fake_fb_conversations(page_id, token, limit=25):
    return [{"id": f"{MARK}_thread",
             "participants": {"data": [
                 {"id": page_id, "name": "我的粉專"},
                 {"id": PSID2, "name": "Toby Ng"}]}}]


igfb.fb_conversations = _fake_fb_conversations
try:
    got = asyncio.run(igfb_store.sync_dm_names("fb", {
        "platform_business_account_id": ACC,
        "credential": {"page_id": ACC, "page_access_token": "x"}}))
finally:
    igfb.fb_conversations = _orig_fb_conv
check(got.get("updated", 0) >= 1, "補名有更新", str(got))
row2 = conv_row(PSID2)
check(row2 and (row2[8] or "") == "Toby Ng", "缺名會話補上了名字",
      f"last_name={row2[8]!r}" if row2 else "")

print("\n[3] 留言作者名用私訊名回填（Graph 不給留言作者名）")
check(comment_name(FBC) == "Joy Li", "佔位『用戶』被私訊真名取代", f"{comment_name(FBC)!r}")
check(comment_name(FBC + "_keep") == "Real Name", "已有真名的留言**不被覆蓋**",
      f"{comment_name(FBC + '_keep')!r}")

print("\n[4] 只補「缺名」的 inbound（不要每次收訊都白打 Graph API）")
left = igfb_store.conversations_without_name("fb", ACC)
check(PSID not in left, "已有名字的會話不再列為缺名")
check(PSID2 not in left, "剛補好的也不再列為缺名")
check(len(left) == 0, "沒有缺名會話", str(left))

print("\n[5] 標題層：留言列表的 last_name 也不能被空值蓋掉")


def comment_row(comment_id: str):
    conn = db()
    try:
        with conn.cursor() as cur:
            # comment SQL 的參數：self(排除我方發的) → platform → native → before×2 → limit
            cur.execute(_conv_comment_sql(), (ACC, "fb", ACC, None, None, 50))
            for r in cur.fetchall():
                if r[0] == comment_id:
                    return r
        return None
    finally:
        conn.close()


fbc_row = comment_row(FBC)
check(fbc_row and (fbc_row[8] or "") == "Joy Li", "留言列表帶出作者名",
      f"last_name={fbc_row[8]!r}" if fbc_row else "")

print("\n[6] 回覆送出後 → 清紅點（舊單體 mark_handled，搬遷時漏掉）")
conn = db()
try:
    with conn.cursor() as cur:
        cur.execute("UPDATE igfb_dm SET is_handled = false"
                    " WHERE platform=%s AND platform_business_account_id=%s"
                    "   AND direction='inbound'", ("fb", ACC))
        # 造一則「回覆在途時客戶又發的」新訊息（未來時間 → 不該被標掉）
        cur.execute(
            "INSERT INTO igfb_dm(platform, conversation_key, sender_id, direction,"
            " message_type, text, created_at, is_handled, platform_business_account_id)"
            " VALUES ('fb', %s, %s, 'inbound', 'text', %s, %s, false, %s)",
            (PSID, PSID, f"{MARK} 新的", now() + timedelta(minutes=5), ACC))
        cur.execute("SELECT count(*) FROM igfb_dm WHERE platform=%s"
                    " AND platform_business_account_id=%s"
                    " AND direction='inbound' AND COALESCE(is_handled,false)=false",
                    ("fb", ACC))
        before = int(cur.fetchone()[0])
        n = outbound._mark_inbound_handled(cur, "fb", PSID, "dm")
        cur.execute("SELECT count(*) FROM igfb_dm WHERE platform=%s"
                    " AND platform_business_account_id=%s"
                    " AND direction='inbound' AND COALESCE(is_handled,false)=false",
                    ("fb", ACC))
        after = int(cur.fetchone()[0])
        # 「回覆在途時的新訊息」那一則
        cur.execute("SELECT COALESCE(is_handled,false) FROM igfb_dm WHERE platform=%s"
                    " AND text = %s", ("fb", f"{MARK} 新的"))
        later_handled = bool(cur.fetchone()[0])
    conn.commit()
finally:
    conn.close()
check(n >= 1, "有把舊的客戶來訊標為已處理", f"標了 {n} 筆")
check(before > after, "紅點數下降", f"{before} → {after}")
check(not later_handled, "★ 回覆在途時的**新訊息不被誤標**（紅點該留著）")

print("\n[7] 留言回覆走 replied，不靠 is_handled")
conn = db()
try:
    with conn.cursor() as cur:
        n2 = outbound._mark_inbound_handled(cur, "fb", FBC, "comment")
    conn.commit()
finally:
    conn.close()
check(n2 == 0, "scope=comment 不動 is_handled（留言 pending 看 replied）")

cleanup()
print("\n" + "=" * 60)
print(f"  IG/FB 名稱與紅點自檢：PASS {passes} / FAIL {len(failures)}")
if failures:
    print("  失敗：" + "、".join(failures))
raise SystemExit(1 if failures else 0)
