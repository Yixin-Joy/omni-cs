"""階段四自檢 —— 工作台會話列表（Connector 內網 API `/internal/api/conversations`）。

只碰 Connector 一個服務，故用 `TestClient` 掛**最小 app**（不進 lifespan、不起 worker），
不必真的起兩個服務。

覆蓋：
  [1] 內網 token 認證
  [2] whatsapp 私訊：聚合、pending（來訊未處理）、最後一則純文字
  [3] ig 私訊：sender_name 帶出、conversation_key 退路
  [4] fb 留言：scope=comment、key=comment_id、post_id 帶出
  [5] 參數校驗（不支援平台 / 平台不支援 scope / limit 夾邊界）
  [6] 逐列容錯（壞 payload 純量不拖垮整份清單）
  [7] before 分頁（嚴格向前，不含邊界那筆）

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> connector/tests/phase4_conversations_check.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONN_DIR = ROOT / "connector"
sys.path.insert(0, str(CONN_DIR))

import psycopg2  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import config  # noqa: E402
from app.internal.router import router as internal_router  # noqa: E402

MARK = "zz_p4_conv"
ACC = f"{MARK}_acc"
WA_A = f"{MARK}_wa_a"
WA_B = f"{MARK}_wa_b"
IG_A = f"{MARK}_ig_a"
FB_C1 = f"{MARK}_fb_c1"
FB_C2 = f"{MARK}_fb_c2"

failures: list[str] = []
passes = 0


def check(cond, msg):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def db():
    return psycopg2.connect(config.DATABASE_URL)


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 準備 / 清理

def cleanup():
    """冪等清掉本腳本留下的列（只按本腳本的哨兵值刪）。"""
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM forward_log WHERE platform_business_account_id = %s", (ACC,))
            cur.execute("DELETE FROM igfb_dm WHERE platform_business_account_id = %s", (ACC,))
            cur.execute("DELETE FROM igfb_comment WHERE platform_business_account_id = %s", (ACC,))
        conn.commit()
    finally:
        conn.close()


def seed():
    """造一批可控資料：兩個 wa 會話、一個 ig 會話、兩則 fb 留言。"""
    t0 = now()
    conn = db()
    try:
        with conn.cursor() as cur:
            # --- whatsapp：A 有一筆「未處理來訊」，B 全部已處理 ---
            cur.execute(
                "INSERT INTO forward_log(platform, sender_id, direction, message_type,"
                " raw_content, message_time, is_handled, platform_business_account_id)"
                " VALUES ('whatsapp', %s, 'inbound', 'text', %s, %s, false, %s)",
                (WA_A, "我想問退貨", t0 - timedelta(minutes=5), ACC))
            cur.execute(
                "INSERT INTO forward_log(platform, sender_id, direction, message_type,"
                " raw_content, message_time, is_handled, platform_business_account_id)"
                " VALUES ('whatsapp', %s, 'outbound', 'text', %s, %s, false, %s)",
                (WA_A, "好的，稍等", t0 - timedelta(minutes=1), ACC))
            cur.execute(
                "INSERT INTO forward_log(platform, sender_id, direction, message_type,"
                " raw_content, message_time, is_handled, platform_business_account_id)"
                " VALUES ('whatsapp', %s, 'inbound', 'text', %s, %s, true, %s)",
                (WA_B, "已處理過的舊訊", t0 - timedelta(minutes=10), ACC))

            # --- ig dm：sender_name 帶出；最後一則為 forwarded_payload 純量（容錯） ---
            cur.execute(
                "INSERT INTO igfb_dm(platform, conversation_key, sender_id, sender_name,"
                " direction, message_type, text, created_at, is_handled,"
                " platform_business_account_id)"
                " VALUES ('ig', %s, %s, 'Joy Li', 'inbound', 'text', %s, %s, false, %s)",
                (IG_A, IG_A, "IG 私訊一", t0 - timedelta(minutes=3), ACC))

            # --- fb 留言：兩則，一則未處理 ---
            cur.execute(
                "INSERT INTO igfb_comment(platform, post_id, comment_id, from_id, from_name,"
                " text, created_at, is_handled, platform_business_account_id)"
                " VALUES ('fb', 'post_1', %s, 'u1', '小明', %s, %s, false, %s)",
                (FB_C1, "這款有貨嗎", t0 - timedelta(minutes=4), ACC))
            cur.execute(
                "INSERT INTO igfb_comment(platform, post_id, comment_id, from_id, from_name,"
                " text, created_at, is_handled, platform_business_account_id)"
                " VALUES ('fb', 'post_2', %s, 'u2', '小華', %s, %s, true, %s)",
                (FB_C2, "已回覆過的留言", t0 - timedelta(minutes=8), ACC))
        conn.commit()
    finally:
        conn.close()


def client() -> TestClient:
    app = FastAPI()
    app.include_router(internal_router)
    return TestClient(app)


def hdr() -> dict:
    return {"X-Internal-Token": config.INTERNAL_API_TOKEN or ""}


# ---------------------------------------------------------------- 主流程

def main() -> int:
    cleanup()
    seed()
    c = client()

    print("\n[1] 內網 token 認證")
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC})
    check(r.status_code == 401, f"未帶 token → 401（實得 {r.status_code}）")
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC},
              headers={"X-Internal-Token": "wrong"})
    check(r.status_code == 401, f"錯 token → 401（實得 {r.status_code}）")

    print("\n[2] whatsapp 私訊聚合")
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC},
              headers=hdr())
    check(r.status_code == 200, f"200（實得 {r.status_code}）")
    items = r.json().get("items") or []
    check(len(items) == 2, f"2 個會話（實得 {len(items)}）")
    by_key = {it["key"]: it for it in items}
    a, b = by_key.get(WA_A), by_key.get(WA_B)
    check(a is not None and b is not None, "兩個 sender 都在清單內")
    if a:
        check(a["pending"] == 1, f"A pending=1（實得 {a['pending']}）")
        check(a["total"] == 2, f"A total=2（實得 {a['total']}）")
        check(a["text"] == "好的，稍等", f"A 取最後一則（實得 {a['text']!r}）")
        check(a["direction"] == "outbound", f"A direction=outbound（實得 {a['direction']}）")
    if b:
        check(b["pending"] == 0, f"B pending=0（已處理，實得 {b['pending']}）")
    check(items[0]["key"] == WA_A, "清單按最後訊息時間倒序（A 在前）")

    print("\n[3] ig 私訊（sender_name / conversation_key 退路）")
    r = c.get("/internal/api/conversations",
              params={"platform": "ig", "platform_business_account_id": ACC}, headers=hdr())
    its = (r.json().get("items") or [])
    check(len(its) == 1 and its[0]["key"] == IG_A, "ig 會話 key = sender_id")
    if its:
        check(its[0]["sender_name"] == "Joy Li", f"帶出 sender_name（實得 {its[0]['sender_name']!r}）")
        check(its[0]["text"] == "IG 私訊一", "取正規化後的純文字")
        check(its[0]["pending"] == 1, "未處理來訊 pending=1")

    print("\n[4] fb 留言（scope=comment）")
    r = c.get("/internal/api/conversations",
              params={"platform": "fb", "platform_business_account_id": ACC, "scope": "comment"},
              headers=hdr())
    its = (r.json().get("items") or [])
    check(len(its) == 2, f"2 則留言各自成會話（實得 {len(its)}）")
    bk = {it["key"]: it for it in its}
    c1 = bk.get(FB_C1)
    check(c1 is not None and c1["pending"] == 1, "未回覆留言 pending=1")
    check(c1 is not None and c1["post_id"] == "post_1", f"帶出 post_id（實得 {c1 and c1['post_id']}）")
    check(c1 is not None and c1["sender_name"] == "小明", "帶出留言者名稱")
    check(bk.get(FB_C2, {}).get("pending") == 0, "已回覆留言 pending=0")

    print("\n[4b] 未處理計數 /pending-count（二級平台 tab 紅點）")
    r = c.get("/internal/api/pending-count",
              params={"platform": "whatsapp", "platform_business_account_id": ACC, "scope": "dm"},
              headers=hdr())
    check(r.status_code == 200 and r.json().get("count") == 1,
          f"whatsapp dm 未處理=1（實得 {r.json().get('count') if r.status_code == 200 else r.status_code}）")
    r = c.get("/internal/api/pending-count",
              params={"platform": "ig", "platform_business_account_id": ACC, "scope": "dm"},
              headers=hdr())
    check(r.json().get("count") == 1, f"ig dm 未處理=1（實得 {r.json().get('count')}）")
    r = c.get("/internal/api/pending-count",
              params={"platform": "fb", "platform_business_account_id": ACC, "scope": "comment"},
              headers=hdr())
    check(r.json().get("count") == 1, f"fb comment 未處理=1（實得 {r.json().get('count')}）")
    r = c.get("/internal/api/pending-count",
              params={"platform": "fb", "platform_business_account_id": ACC, "scope": "dm"}, headers=hdr())
    check(r.json().get("count") == 0, "fb 無私訊 → 0")
    r = c.get("/internal/api/pending-count",
              params={"platform": "wechat", "platform_business_account_id": ACC, "scope": "comment"},
              headers=hdr())
    check(r.status_code == 400, "wechat 不支援 comment → 400")
    r = c.get("/internal/api/pending-count",
              params={"platform": "line", "platform_business_account_id": ACC, "scope": "dm"},
              headers=hdr())
    check(r.status_code == 400, "不支援平台 → 400")
    r = c.get("/internal/api/pending-count",
              params={"platform": "whatsapp", "platform_business_account_id": ACC, "scope": "bogus"},
              headers=hdr())
    check(r.status_code == 400, "不支援 scope → 400")

    print("\n[5] 參數校驗")
    r = c.get("/internal/api/conversations",
              params={"platform": "line", "platform_business_account_id": ACC}, headers=hdr())
    check(r.status_code == 400, f"不支援平台 → 400（實得 {r.status_code}）")
    r = c.get("/internal/api/conversations",
              params={"platform": "wechat", "platform_business_account_id": ACC, "scope": "comment"},
              headers=hdr())
    check(r.status_code == 400, f"wechat 不支援 comment → 400（實得 {r.status_code}）")
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC, "limit": 0},
              headers=hdr())
    check(r.status_code == 200, "limit=0 被夾成 1（不報錯）")
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC, "limit": 99999},
              headers=hdr())
    check(r.status_code == 200, "limit 超上限被夾住（不報錯）")

    print("\n[6] 逐列容錯（壞 payload 純量）")
    conn = db()
    try:
        with conn.cursor() as cur:
            # forwarded_payload 為 jsonb 純量 null、raw_content 空 → 不應炸，text 為空字串
            cur.execute(
                "INSERT INTO forward_log(platform, sender_id, direction, message_type,"
                " raw_content, forwarded_payload, message_time, is_handled,"
                " platform_business_account_id)"
                " VALUES ('whatsapp', %s, 'inbound', 'text', '', NULL, %s, false, %s)",
                (f"{MARK}_bad", now(), ACC))
        conn.commit()
    finally:
        conn.close()
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC}, headers=hdr())
    check(r.status_code == 200, f"含怪列仍 200（實得 {r.status_code}）")
    bad = next((it for it in (r.json().get("items") or []) if it["key"] == f"{MARK}_bad"), None)
    check(bad is not None and bad["text"] == "", "怪列 text 退化為空字串")

    print("\n[7] before 分頁")
    r = c.get("/internal/api/conversations",
              params={"platform": "whatsapp", "platform_business_account_id": ACC}, headers=hdr())
    page1 = r.json().get("items") or []
    top_key, top_ts = page1[0]["key"], page1[0]["last_ts"]
    r2 = c.get("/internal/api/conversations",
               params={"platform": "whatsapp", "platform_business_account_id": ACC,
                       "before": top_ts}, headers=hdr())
    keys2 = [it["key"] for it in (r2.json().get("items") or [])]
    check(top_key not in keys2, f"before 嚴格向前，不含邊界那筆（{top_key}）")
    check(len(keys2) == len(page1) - 1, f"第二頁少一筆（{len(page1)} → {len(keys2)}）")

    cleanup()
    print("\n" + "=" * 68)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    if failures:
        for f in failures:
            print("   -", f)
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
