"""歷史補拉自檢 —— 「重新整理」按鈕背後的同步邏輯（`connector/app/history_sync.py`）。

★ 全程**離線**：把 `app.channels.igfb` 的 Graph 唯讀函式打成假資料，
  不發任何網路請求。驗的是「落地 + 紅點規則 + 冪等 + 容錯」。

覆蓋：
  [1] 不支援的平台（whatsapp / wechat）→ supported=false + 原因，且不寫任何表
  [2] 缺憑證 → supported=false，不炸
  [3] fb 私訊補拉：訊息落地 igfb_dm、方向正確、對齊鍵正確
  [4] ★ 紅點規則：新入站私訊「之後有我方回覆」→ is_handled=true（不補紅點）；
      「之後沒有任何回覆」→ is_handled=false（補紅點）
  [5] 冪等：再跑一次 dm_new=0（不會重複入庫）
  [6] FB「留言轉私訊」系統通知被濾掉
  [7] 貼文 + 留言：igfb_post 落地；討論串「最後一則是我方」→ replied=true，否則 false
  [8] 批次 sync_many：totals 彙總；單帳號失敗不拖垮整批

跑法（從 connector 目錄）：
    PYTHONPATH='<connector>;<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> tests/history_sync_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

CONN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONN_DIR))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from app import account_map, history_sync  # noqa: E402
from app.channels import igfb  # noqa: E402
from app.core import config  # noqa: E402

MARK = "zz_hs"
FB_NATIVE = f"{MARK}_fb_page"
BIZ = 990901
BP = 880901
SENDER_A = f"{MARK}_u1"
SENDER_B = f"{MARK}_u2"

failures: list[str] = []
passes = 0


def check(cond, msg, extra=""):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


def db(sql: str, args=None, fetch=False):
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


def cleanup() -> None:
    for t in ("igfb_dm", "igfb_comment", "igfb_post"):
        db(f"DELETE FROM {t} WHERE platform_business_account_id LIKE %s", (MARK + "%",))
    db("DELETE FROM connector_account_map WHERE platform_business_account_id LIKE %s", (MARK + "%",))


# ---------------------------------------------------------------- 假 Graph 資料

def _fb_convs(**_kw):
    async def run(page_id, token, limit=25):
        return [
            {"id": f"{MARK}_t1", "participants": {"data": [
                {"id": FB_NATIVE, "name": "粉專"}, {"id": SENDER_A, "name": "小明"}]}},
            {"id": f"{MARK}_t2", "participants": {"data": [
                {"id": FB_NATIVE, "name": "粉專"}, {"id": SENDER_B, "name": "小華"}]}},
        ]
    return run


_MSGS = {
    # ★ Graph 慣例：新 → 舊
    f"{MARK}_t1": [
        {"id": f"{MARK}_m3", "from": {"id": SENDER_A}, "message": "還在嗎",
         "created_time": "2026-10-14T10:10:00+0000"},
        {"id": f"{MARK}_m2", "from": {"id": FB_NATIVE}, "message": "好的，稍等",
         "created_time": "2026-10-14T10:05:00+0000"},
        {"id": f"{MARK}_m1", "from": {"id": SENDER_A}, "message": "我想問退貨",
         "created_time": "2026-10-14T10:00:00+0000"},
    ],
    f"{MARK}_t2": [
        {"id": f"{MARK}_m5", "from": {"id": SENDER_B}, "message": "在嗎",
         "created_time": "2026-10-14T09:02:00+0000"},
        {"id": f"{MARK}_noise", "from": {"id": SENDER_B},
         "message": "创建这个聊天是因为用户评论了你的帖子",
         "created_time": "2026-10-14T09:01:00+0000"},
        {"id": f"{MARK}_m4", "from": {"id": SENDER_B}, "message": "你好",
         "created_time": "2026-10-14T09:00:00+0000"},
    ],
}

_COMMENTS = {
    f"{MARK}_p1": [
        {"id": f"{MARK}_c1", "message": "這款有貨嗎", "from": {"id": SENDER_A, "name": "小明"},
         "created_time": "2026-10-14T08:00:00+0000"},
        {"id": f"{MARK}_c2", "message": "多少錢", "from": {"id": SENDER_B, "name": "小華"},
         "created_time": "2026-10-14T08:05:00+0000",
         "comments": {"data": [
             {"id": f"{MARK}_c3", "message": "稍後私訊您", "from": {"id": FB_NATIVE, "name": "粉專"},
              "created_time": "2026-10-14T08:06:00+0000"}]}},
    ],
}


def _latest_time(thread_id: str) -> str:
    """該會話最新一則的時間（模擬 Graph 回的 updated_time）。"""
    return max(((m.get("created_time") or "") for m in (_MSGS.get(thread_id) or [])),
               default="") or None


def _patch_graph() -> None:
    async def convs(page_id, token, limit=25):
        # ⚠️ 一定要帶 updated_time —— 會話級增量預篩靠它（沒有它就得每次都拉 thread）
        return [
            {"id": f"{MARK}_t1", "updated_time": _latest_time(f"{MARK}_t1"),
             "participants": {"data": [
                 {"id": FB_NATIVE}, {"id": SENDER_A, "name": "小明"}]}},
            {"id": f"{MARK}_t2", "updated_time": _latest_time(f"{MARK}_t2"),
             "participants": {"data": [
                 {"id": FB_NATIVE}, {"id": SENDER_B, "name": "小華"}]}},
        ]

    async def msgs(thread_id, token, limit=30):
        return list(_MSGS.get(thread_id, []))

    async def posts(page_id, token, limit=10):
        return [{"id": f"{MARK}_p1", "message": "新品上市", "created_time": "2026-10-14T07:00:00+0000",
                 "permalink_url": "https://example.com/p1", "full_picture": "https://example.com/i.jpg",
                 "comments_count": 2, "like_count": 3}]

    async def comments(post_id, token, limit=50):
        return list(_COMMENTS.get(post_id, []))

    igfb.fb_conversations = convs
    igfb.fb_thread_messages = msgs
    igfb.fb_posts = posts
    igfb.fb_comments = comments


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print(f"DB={config.DATABASE_URL}\n")
    cleanup()
    account_map.upsert_many([
        {"platform": "fb", "platform_business_account_id": FB_NATIVE,
         "business_id": BIZ, "bp_account_id": BP, "enabled": True,
         "credential": {"page_id": FB_NATIVE, "page_access_token": "tok"}},
        {"platform": "ig", "platform_business_account_id": f"{MARK}_ig",
         "business_id": BIZ, "bp_account_id": BP, "enabled": True,
         "credential": {"ig_user_id": f"{MARK}_ig", "ig_access_token": "IGAA_x"}},
        {"platform": "whatsapp", "platform_business_account_id": f"{MARK}_wa",
         "business_id": BIZ, "bp_account_id": BP, "enabled": True,
         "credential": {"access_token": "x", "phone_number_id": f"{MARK}_wa"}},
        {"platform": "wechat", "platform_business_account_id": f"{MARK}_wx",
         "business_id": BIZ, "bp_account_id": BP, "enabled": True,
         "credential": {"appid": f"{MARK}_wx"}},
    ])
    _patch_graph()

    print("\n[1] 不支援的平台（whatsapp / wechat）")
    wa = asyncio.run(history_sync.sync_account("whatsapp", f"{MARK}_wa"))
    check(wa["supported"] is False, "whatsapp supported=false")
    check("Cloud API" in (wa.get("reason") or ""), "whatsapp 有說明原因", wa.get("reason", "")[:40])
    wx = asyncio.run(history_sync.sync_account("wechat", f"{MARK}_wx"))
    check(wx["supported"] is False and "微信" in (wx.get("reason") or ""), "wechat supported=false + 原因")
    check(len(db("SELECT 1 FROM igfb_dm WHERE platform_business_account_id=%s", (f"{MARK}_wa",), fetch=True)) == 0,
          "不支援的平台沒有寫任何訊息")

    print("\n[2] 缺憑證")
    account_map.upsert_many([{"platform": "fb", "platform_business_account_id": f"{MARK}_nocred",
                              "business_id": BIZ, "bp_account_id": BP, "enabled": True}])
    nc = asyncio.run(history_sync.sync_account("fb", f"{MARK}_nocred"))
    check(nc["supported"] is False and "憑證" in (nc.get("reason") or ""), "缺憑證 → supported=false + 原因")

    print("\n[3] fb 私訊補拉")
    res = asyncio.run(history_sync.sync_account("fb", FB_NATIVE))
    check(res["supported"] is True, "supported=true")
    check(res["conversations"] == 2, f"拉到 2 個會話（實得 {res['conversations']}）")
    check(res["dm_new"] == 4, f"新增入站私訊 4 則（實得 {res['dm_new']}）")
    check(res["dm_out"] == 1, f"新增我方回覆 1 則（實得 {res['dm_out']}）")
    rows = db("SELECT message_id, direction, sender_name, business_id, bp_account_id,"
              "       platform_business_account_id, is_handled"
              "  FROM igfb_dm WHERE platform_business_account_id=%s", (FB_NATIVE,), fetch=True)
    by_mid = {r["message_id"]: r for r in rows}
    check(len(rows) == 5, f"igfb_dm 共 5 列（實得 {len(rows)}）")
    check(by_mid[f"{MARK}_m1"]["direction"] == "inbound", "m1 是 inbound")
    check(by_mid[f"{MARK}_m2"]["direction"] == "outbound", "m2 是 outbound")
    check(by_mid[f"{MARK}_m1"]["business_id"] == BIZ, "落地帶 business_id")
    check(by_mid[f"{MARK}_m1"]["bp_account_id"] == BP, "落地帶 bp_account_id")
    check(by_mid[f"{MARK}_m1"]["platform_business_account_id"] == FB_NATIVE, "落地帶平台原生 ID")
    check(by_mid[f"{MARK}_m1"]["sender_name"] == "小明", "帶出對話者名稱（而非訊息發送者）")

    print("\n[4] ★ 紅點規則：後面有沒有我方回覆")
    check(by_mid[f"{MARK}_m1"]["is_handled"] is True,
          "m1（之後有我方回覆 10:05）→ handled，不補紅點",
          str(by_mid[f"{MARK}_m1"]["is_handled"]))
    check(by_mid[f"{MARK}_m3"]["is_handled"] is False,
          "m3（之後沒有任何回覆）→ 未處理，補紅點",
          str(by_mid[f"{MARK}_m3"]["is_handled"]))
    check(by_mid[f"{MARK}_m4"]["is_handled"] is False, "m4（整段無回覆）→ 補紅點")
    check(by_mid[f"{MARK}_m5"]["is_handled"] is False, "m5（整段無回覆）→ 補紅點")
    check(by_mid[f"{MARK}_m2"]["is_handled"] is True, "我方回覆本身標 handled")
    check(res["dm_handled"] == 1 and res["dm_pending"] == 3,
          f"統計 handled=1 / pending=3（實得 {res['dm_handled']}/{res['dm_pending']}）")

    print("\n[5] ★ 增量：只從「最後一則之後」開始拉")
    res2 = asyncio.run(history_sync.sync_account("fb", FB_NATIVE))
    check(res2["dm_new"] == 0 and res2["dm_out"] == 0, "第二次 dm_new=0 / dm_out=0")
    check(len(db("SELECT 1 FROM igfb_dm WHERE platform_business_account_id=%s",
                 (FB_NATIVE,), fetch=True)) == 5, "igfb_dm 仍是 5 列")
    check(res2["skipped_conversations"] == 2,
          f"兩個會話都不比水位新 → 整個跳過（實得 {res2['skipped_conversations']}）")
    # 會話級已攔下 → 連 thread messages 都不用打，訊息層自然是 0（更省）
    check(res2["skipped_messages"] == 0,
          f"會話級就攔住了，訊息層不需再篩（實得 {res2['skipped_messages']}）")

    print("\n[5b] ★ 已標記的狀態不得被翻回")
    # 模擬客服把 m1（本來就會被算成 handled）與 m3（補了紅點）都標成已處理
    db("UPDATE igfb_dm SET is_handled = true WHERE platform_business_account_id=%s",
       (FB_NATIVE,))
    # 再跑一次（無新消息）→ 不得有任何變動
    res3 = asyncio.run(history_sync.sync_account("fb", FB_NATIVE))
    check(res3["dm_new"] == 0 and res3["dm_handled"] == 0, "沒有新消息 → 不重算")
    still = db("SELECT count(*) AS n FROM igfb_dm"
               " WHERE platform_business_account_id=%s AND is_handled = false",
               (FB_NATIVE,), fetch=True)[0]["n"]
    check(still == 0, f"★ 手動標的已處理全部保留（剩下的未處理 = {still}）")

    # 只追加一則「新」入站消息 → 只影響它自己，不影響其他列
    _MSGS[f"{MARK}_t1"] = [
        {"id": f"{MARK}_m6", "from": {"id": SENDER_A}, "message": "還有嗎",
         "created_time": "2026-10-14T10:20:00+0000"},
    ] + _MSGS[f"{MARK}_t1"]
    res4 = asyncio.run(history_sync.sync_account("fb", FB_NATIVE))
    check(res4["dm_new"] == 1, f"只新增 1 則新消息（實得 {res4['dm_new']}）")
    check(res4["skipped_conversations"] == 1,
          f"t2 沒新消息 → 跳過（實得 {res4['skipped_conversations']}）")
    # t1 因為有新消息必須拉，但拉回來的舊消息（m1/m2/m3）被消息級水位擋掉
    check(res4["skipped_messages"] == 3,
          f"t1 的舊消息被消息級水位擋掉 3 則（實得 {res4['skipped_messages']}）")
    check(res4["dm_pending"] == 1 and res4["dm_handled"] == 0,
          f"新消息後面沒有回覆 → 補紅點（實得 handled={res4['dm_handled']} pending={res4['dm_pending']}）")
    kept = db("SELECT is_handled FROM igfb_dm WHERE platform_business_account_id=%s"
              " AND message_id = %s", (FB_NATIVE, f"{MARK}_m3"), fetch=True)
    check(kept and kept[0]["is_handled"] is True,
          "★ 之前標的已處理（m3）沒有被新一輪翻回未處理")
    check(len(db("SELECT 1 FROM igfb_dm WHERE platform_business_account_id=%s",
                 (FB_NATIVE,), fetch=True)) == 6, "總共 6 列")

    print("\n[6] FB「留言轉私訊」系統通知被濾掉")
    check(f"{MARK}_noise" not in by_mid, "系統通知沒有入庫")

    print("\n[7] 貼文 + 留言")
    posts = db("SELECT post_id, name, media_url, permalink, business_id"
               "  FROM igfb_post WHERE platform_business_account_id=%s", (FB_NATIVE,), fetch=True)
    check(len(posts) == 1 and posts[0]["post_id"] == f"{MARK}_p1", "貼文落地")
    check(posts[0]["name"] == "新品上市" and posts[0]["media_url"] == "https://example.com/i.jpg",
          "貼文標題/媒體帶出")
    check(posts[0]["business_id"] == BIZ, "貼文帶 business_id")
    cs = db("SELECT comment_id, parent_id, replied, is_handled, business_id"
            "  FROM igfb_comment WHERE platform_business_account_id=%s", (FB_NATIVE,), fetch=True)
    by_cid = {r["comment_id"]: r for r in cs}
    check(len(cs) == 3, f"留言 3 則（實得 {len(cs)}）")
    check(by_cid[f"{MARK}_c3"]["parent_id"] == f"{MARK}_c2", "我方回覆是 c2 的子留言")
    check(by_cid[f"{MARK}_c1"]["replied"] is False, "c1 討論串最後一則是客戶 → 未回（亮紅點）")
    check(by_cid[f"{MARK}_c2"]["replied"] is True, "c2 討論串最後一則是我方 → 已回（不亮紅點）")
    check(by_cid[f"{MARK}_c2"]["is_handled"] is True, "c2 is_handled=true")
    check(by_cid[f"{MARK}_c1"]["business_id"] == BIZ, "留言帶 business_id")
    check(res["posts"] == 1, f"統計 posts=1（實得 {res['posts']}）")

    print("\n[7b] ★ 留言：已忽略不得被翻回 / 只重算有新留言的討論串")
    # 模擬客服把 c1 討論串標成「已忽略」（replied=false + is_handled=true）
    db("UPDATE igfb_comment SET replied=false, is_handled=true"
       " WHERE platform_business_account_id=%s AND comment_id=%s", (FB_NATIVE, f"{MARK}_c1"))
    res_r = asyncio.run(history_sync.sync_account("fb", FB_NATIVE))
    check(res_r["comments_new"] == 0, "沒有新留言 → 不新增")
    check(res_r["threads_recomputed"] == 0, "★ 沒重算任何討論串（不會翻掉人工標記）")
    kept_c1 = db("SELECT replied, is_handled FROM igfb_comment WHERE comment_id=%s",
                 (f"{MARK}_c1",), fetch=True)[0]
    check(kept_c1["is_handled"] is True and kept_c1["replied"] is False,
          "★ c1 的「已忽略」原封不動")

    # 追加一則**新**留言到 c1 討論串（客戶發的）→ 只有 c1 被重算，c2 不動。
    # ⚠️ fb_comments 回的是「頂層 + 巢狀 replies」，c4 是 c1 的子留言 → 要掛進 c1 的 comments
    _COMMENTS[f"{MARK}_p1"] = [
        {**c,
         "comments": {"data": (c.get("comments", {}).get("data") or []) + (
             [{"id": f"{MARK}_c4", "message": "那我明天去",
               "from": {"id": SENDER_A, "name": "小明"},
               "created_time": "2026-10-14T08:30:00+0000"}]
             if c.get("id") == f"{MARK}_c1" else [])}}
        for c in _COMMENTS[f"{MARK}_p1"] if not c.get("parent_id")
    ]
    res_n = asyncio.run(history_sync.sync_account("fb", FB_NATIVE))
    check(res_n["comments_new"] == 1, f"只新增 1 則新留言（實得 {res_n['comments_new']}）")
    check(res_n["skipped_comments"] == 3, f"舊的 3 則被水位跳過（實得 {res_n['skipped_comments']}）")
    check(res_n["threads_recomputed"] == 1,
          f"★ 只重算 c1 一個討論串（實得 {res_n['threads_recomputed']}）")
    c1n = db("SELECT replied, is_handled FROM igfb_comment WHERE comment_id=%s",
             (f"{MARK}_c1",), fetch=True)[0]
    check(c1n["is_handled"] is False, "★ 有新客戶留言 → c1 重新亮紅點（預期）")
    c2n = db("SELECT replied, is_handled FROM igfb_comment WHERE comment_id=%s",
             (f"{MARK}_c2",), fetch=True)[0]
    check(c2n["replied"] is True and c2n["is_handled"] is True,
          "★ c2 討論串完全沒被碰（仍為已回）")

    print("\n[8] 批次 sync_many：彙總 + 容錯")
    # 讓 ig 的 conversations 爆錯 → 只影響 ig 自己
    async def boom(token, limit=25):
        raise RuntimeError("ig 掛了")
    igfb.ig_conversations = boom
    batch = asyncio.run(history_sync.sync_many([
        {"platform": "fb", "platform_business_account_id": FB_NATIVE},
        {"platform": "ig", "platform_business_account_id": f"{MARK}_ig"},
        {"platform": "whatsapp", "platform_business_account_id": f"{MARK}_wa"},
        {"platform": "fb", "platform_business_account_id": ""},        # 無效項 → 略過
    ]))
    t = batch["totals"]
    check(batch["ok"] is True, "批次回 ok")
    check(t["accounts"] == 3, f"有效帳號 3 個（實得 {t['accounts']}）")
    check(t["accounts_unsupported"] == 1, f"不支援 1 個（whatsapp）（實得 {t['accounts_unsupported']}）")
    check(t["accounts_failed"] == 1, f"失敗 1 個（ig）（實得 {t['accounts_failed']}）")
    check(any("ig 掛了" in e for e in t["errors"]), "ig 的錯誤有回報", str(t["errors"])[:80])
    check(t["dm_new"] == 0, "第二次批次沒有新訊息（冪等）")

    print("\n[9] 內網端點 /internal/api/sync-history（認證 + 契約）")
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    from app.internal.router import router as internal_router  # noqa: E402

    _patch_graph()   # 還原假資料（[8] 曾把 ig_conversations 改成爆錯）
    mini = FastAPI()
    mini.include_router(internal_router)
    tc = TestClient(mini)
    hdr = {"X-Internal-Token": config.INTERNAL_API_TOKEN or ""}

    r = tc.post("/internal/api/sync-history", json={"items": []})
    check(r.status_code == 401, f"未帶內網 token → 401（實得 {r.status_code}）")
    r = tc.post("/internal/api/sync-history", json={"items": []}, headers={"X-Internal-Token": "x"})
    check(r.status_code == 401, f"錯 token → 401（實得 {r.status_code}）")

    r = tc.post("/internal/api/sync-history", headers=hdr, json={
        "items": [{"platform": "whatsapp", "platform_business_account_id": f"{MARK}_wa"},
                  {"platform": "fb", "platform_business_account_id": FB_NATIVE}]})
    check(r.status_code == 200, f"帶 token → 200（實得 {r.status_code}）")
    body = r.json()
    check(body.get("ok") is True, "回 ok")
    its = {i["platform_business_account_id"]: i for i in body["items"]}
    check(its[f"{MARK}_wa"]["supported"] is False, "端點回報 whatsapp 不支援")
    check(its[FB_NATIVE]["supported"] is True, "端點回報 fb 支援")
    check(body["totals"]["accounts"] == 2, f"totals.accounts=2（實得 {body['totals']['accounts']}）")

    r = tc.post("/internal/api/sync-history", headers=hdr, json={
        "items": [{"platform": "fb", "platform_business_account_id": FB_NATIVE}],
        "max_conversations": 0, "max_messages": 0, "max_posts": 0})
    check(r.status_code == 200, "上限傳 0 被夾住（不報錯）")

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
