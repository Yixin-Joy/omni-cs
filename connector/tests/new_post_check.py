"""驗證：新貼文發佈**能被偵測到**（FB 靠 webhook、IG 靠週期同步）。

★ 為什麼要有這支（2026-10-20 小jo：「新貼文發佈檢測不到」）：
  實測證據（DB vs Graph，同一時刻）：

    Graph 上 fb 有一篇新貼文 `1297214806810643_122114927397487545`（robot3，今天 12:00），
    但 `igfb_post` 最新只到 10-06 → 工作台「貼文留言」看不到。

  兩個缺口：
    ① `igfb_recv.extract_events()` 只認 `feed` + `item == "comment"`，
       **自家發文（item=status/photo/…）被 `continue` 丟掉** → FB 新貼文永遠不推。
    ② IG **沒有**「發了新媒體」的 webhook（Meta 只推 comments/mentions/messages），
       而當時唯一的貼文寫入是 6 小時一次的「縮圖續命」迴圈
       → 最長 6 小時才看得到（體感就是「檢測不到」）。

  修法：
    · `_parse_fb_post()` + `extract_events()` 認 `feed` 的自家發文 → `type='fb_post'`；
    · webhook 收到後 `_bg_sync_post()` 再打一次 Graph 取完整欄位（webhook 沒有圖/連結）→ upsert；
    · `history_sync.sync_posts_once()` + `runner._post_sync_loop()` 每 `POST_SYNC_MINUTES`
      （預設 5）拉最近幾篇 → 補 IG，也當 FB 漏推的保險。

本自檢全部**打樁**（不呼叫 Graph、不寫 DB）：
  [A] webhook 事件解析（含 comment/reaction/remove 不誤判）
  [B] `_bg_sync_post()`：Graph 成功用完整欄位、失敗退回 webhook 欄位
  [C] `sync_posts_once()`：統計正確、單一帳號失敗不影響其他帳號
  [D] 週期任務與設定真的接上（runner/config）
  [E] DB：`known_post_ids()` 對真實資料工作（只讀）

跑法（repo 根）：
  PYTHONPATH='<根>\\connector;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> connector/tests/new_post_check.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "connector"))

from app import history_sync                       # noqa: E402
from app import webhook                            # noqa: E402
from app.channels import igfb                      # noqa: E402
from app.channels import igfb_recv                 # noqa: E402
from app.channels import igfb_store                # noqa: E402
from app.core import config                        # noqa: E402
from app.worker import runner                      # noqa: E402

_pass = 0
_fail: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    global _pass
    if ok:
        _pass += 1
        print(f"  PASS  {what}")
    else:
        _fail.append(what)
        print(f"  FAIL  {what}   {detail}")


def _body(item: str, verb: str = "add", **extra) -> dict:
    value = {"item": item, "verb": verb, "post_id": "PAGE_1", "created_time": 1760000000}
    value.update(extra)
    return {"object": "page", "entry": [{"id": "PAGE_1",
                                        "changes": [{"field": "feed", "value": value}]}]}


# ---------------------------------------------------------------- [A] 事件解析
def part_a() -> None:
    print("[A] webhook 事件解析")
    # 自家發文（純文字）
    evs = igfb_recv.extract_events(_body("status", message="新貼文上線"))
    check(len(evs) == 1 and evs[0]["type"] == "fb_post",
          "★ item=status → fb_post（舊版這裡是 0 筆）", str(evs)[:160])
    if evs:
        ev = evs[0]
        check(ev["post_id"] == "PAGE_1", "帶 post_id")
        check(ev["message"] == "新貼文上線", "帶 message（標題）")
        check(ev["platform"] == "fb", "platform=fb")
        check(ev["account_native_id"] == "PAGE_1", "帶 account_native_id（多租戶對齊鍵）")
        check(isinstance(ev["created_at"], datetime) and ev["created_at"].tzinfo is not None,
              "created_at 是 tz-aware datetime", str(ev.get("created_at")))

    for item in ("photo", "video", "share", "link", "api"):
        evs = igfb_recv.extract_events(_body(item))
        check(len(evs) == 1 and evs[0]["type"] == "fb_post",
              f"item={item} → fb_post", str(evs)[:120])

    # 留言不可以被吃掉
    cb = {"object": "page", "entry": [{"id": "PAGE_1", "changes": [{
        "field": "feed", "value": {"item": "comment", "verb": "add",
                                   "comment_id": "C_1", "post_id": "PAGE_1",
                                   "from": {"id": "U1", "name": "小明"},
                                   "message": "多少錢", "created_time": 1760000000}}]}]}
    evs = igfb_recv.extract_events(cb)
    check(len(evs) == 1 and evs[0]["type"] == "fb_comment",
          "回歸：留言仍是 fb_comment", str(evs)[:120])

    # 別人按讚（item=reaction）= 不是新貼文，也不該當留言
    evs = igfb_recv.extract_events(_body("reaction"))
    check(evs == [], "★ item=reaction → 忽略（不當新貼文，免被按讚洗）", str(evs)[:120])

    # 刪文 / 取消
    evs = igfb_recv.extract_events(_body("status", verb="remove"))
    check(evs == [], "verb=remove → 忽略", str(evs)[:120])

    # 缺 post_id
    bad = {"object": "page", "entry": [{"id": "PAGE_1", "changes": [{
        "field": "feed", "value": {"item": "status", "verb": "add"}}]}]}
    check(igfb_recv.extract_events(bad) == [], "缺 post_id → 忽略（不會寫出空貼文）")

    # 回歸：IG 留言
    ig = {"object": "instagram", "entry": [{"id": "IG_1", "changes": [{
        "field": "comments", "value": {"id": "C9", "text": "hi", "media": {"id": "M1"},
                                       "timestamp": 1760000000}}]}]}
    evs = igfb_recv.extract_events(ig)
    check(len(evs) == 1 and evs[0]["type"] == "ig_comment" and evs[0]["post_id"] == "M1",
          "回歸：IG 留言解析仍正常", str(evs)[:140])


# ---------------------------------------------------------------- [B] 落地
def part_b() -> None:
    print("\n[B] _bg_sync_post()：Graph 補齊 / 失敗退回")
    acc = {"business_id": 1, "bp_account_id": 28, "credential": {"page_id": "PAGE_1",
                                                                "page_access_token": "tok"}}
    ev = {"type": "fb_post", "platform": "fb", "post_id": "PAGE_1", "message": "只有文字",
          "created_at": datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)}

    saved: list[dict] = []
    orig_upsert, orig_get = igfb_store.upsert_post, igfb.get_post

    def fake_upsert(platform, account, post):
        saved.append({"platform": platform, "post": dict(post)})

    igfb_store.upsert_post = fake_upsert
    try:
        async def ok_get(platform, post_id, cred):
            return {"id": post_id, "message": "完整內容",
                    "created_time": "2026-10-20T04:00:00+0000",
                    "permalink_url": "https://fb/p/1", "full_picture": "https://cdn/x.jpg"}

        igfb.get_post = ok_get
        asyncio.run(webhook._bg_sync_post("fb", acc, ev))
        check(len(saved) == 1 and saved[0]["post"]["id"] == "PAGE_1",
              "★ Graph 讀到就用 Graph 的完整欄位", str(saved)[:160])
        check(saved and saved[0]["post"].get("permalink_url") == "https://fb/p/1",
              "★ permalink 有帶進 upsert（webhook payload 沒有這個）")
        check(saved and saved[0]["post"].get("full_picture") == "https://cdn/x.jpg",
              "★ 圖片有帶進 upsert（工作台才看得到縮圖）")
        check(saved and saved[0]["post"].get("message") == "完整內容", "用 Graph 的文案")
        check(saved and saved[0]["platform"] == "fb", "platform 正確")

        saved.clear()

        async def fail_get(platform, post_id, cred):
            return {}

        igfb.get_post = fail_get
        asyncio.run(webhook._bg_sync_post("fb", acc, ev))
        check(len(saved) == 1, "★ Graph 讀不到仍會入庫（不會靜靜消失）", str(saved)[:160])
        check(saved and saved[0]["post"]["id"] == "PAGE_1", "退回時帶 post_id")
        check(saved and saved[0]["post"].get("message") == "只有文字",
              "退回時用 webhook 的 message")
        check(saved and saved[0]["post"].get("created_time") is not None,
              "退回時保留 created_at（列表排序不會亂）")

        saved.clear()

        async def boom(platform, post_id, cred):
            raise RuntimeError("network down")

        igfb.get_post = boom
        asyncio.run(webhook._bg_sync_post("fb", acc, ev))
        check(len(saved) == 1, "Graph 丟例外也不影響入庫", str(saved)[:120])

        saved.clear()
        asyncio.run(webhook._bg_sync_post("fb", acc, {"type": "fb_post", "platform": "fb"}))
        check(saved == [], "沒有 post_id → 直接跳過（不寫假資料）")
    finally:
        igfb_store.upsert_post, igfb.get_post = orig_upsert, orig_get


# ---------------------------------------------------------------- [C] 週期同步
def part_c() -> None:
    print("\n[C] sync_posts_once()：統計與容錯")
    accs = [
        {"platform": "fb", "platform_business_account_id": "PAGE_1",
         "business_id": 1, "bp_account_id": 28, "credential": {}},
        {"platform": "ig", "platform_business_account_id": "IG_1",
         "business_id": 1, "bp_account_id": 29, "credential": {}},
    ]
    posts = {
        "PAGE_1": [{"id": "P1", "message": "a"}, {"id": "P2", "message": "b"}],
        "IG_1": [{"id": "M1", "caption": "c"}],
    }
    saved: list[str] = []
    orig = (history_sync._igfb_accounts, history_sync.fetch_account_posts,
            igfb_store.upsert_post, igfb_store.known_post_ids)

    async def fake_fetch(acc, limit):
        return posts[str(acc["platform_business_account_id"])]

    def fake_upsert(platform, account, post):
        saved.append(str(post.get("id")))

    def fake_known(platform, ids):
        return {"P2"}                    # P2 已經在庫

    history_sync._igfb_accounts = lambda: accs
    history_sync.fetch_account_posts = fake_fetch
    igfb_store.upsert_post = fake_upsert
    igfb_store.known_post_ids = fake_known
    try:
        stats = asyncio.run(history_sync.sync_posts_once(8))
        check(stats["accounts"] == 2, "兩個帳號都跑了", str(stats))
        check(stats["posts"] == 3, "共處理 3 篇", str(stats))
        check(stats["new"] == 2, "★ 真的新增 2 篇（P1 / M1，P2 已知不重算）", str(stats))
        check(set(saved) == {"P1", "P2", "M1"}, "每篇都 upsert（已知的也會刷新）", str(saved))
        check(stats["errors"] == [], "沒有錯誤", str(stats["errors"])[:120])

        # 單一帳號失敗不影響其他
        async def half_fetch(acc, limit):
            if acc["platform_business_account_id"] == "PAGE_1":
                raise RuntimeError("token expired")
            return posts["IG_1"]

        history_sync.fetch_account_posts = half_fetch
        saved.clear()
        stats = asyncio.run(history_sync.sync_posts_once(8))
        check(len(stats["errors"]) == 1 and "token expired" in stats["errors"][0],
              "★ 一個帳號失敗 → errors 有紀錄", str(stats["errors"])[:160])
        check(stats["accounts"] == 1 and saved == ["M1"],
              "★ 失敗的帳號跳過，另一個照樣同步", str(stats))

        # 空貼文 / 缺 id 不炸
        async def noid_fetch(acc, limit):
            return [{"message": "no id"}, {"id": "", "message": "empty"}]

        history_sync.fetch_account_posts = noid_fetch
        saved.clear()
        stats = asyncio.run(history_sync.sync_posts_once(8))
        check(saved == [] and stats["posts"] == 0, "沒有 id 的貼文跳過（不炸）", str(stats))
    finally:
        (history_sync._igfb_accounts, history_sync.fetch_account_posts,
         igfb_store.upsert_post, igfb_store.known_post_ids) = orig


# ---------------------------------------------------------------- [C2] 探測/告警
def part_c2() -> None:
    print("\n[C2] 0 篇時分辨「沒貼文」與「沒權限」 + 上報告警")
    acc = {"platform": "fb", "platform_business_account_id": "PAGE_9",
           "business_id": 127, "bp_account_id": 194,
           "credential": {"page_id": "PAGE_9", "page_access_token": "tok"}}

    calls: list[tuple[str, dict]] = []
    orig_get = igfb._get_json

    try:
        async def deny(url, token, params=None, timeout=20):
            calls.append((url, dict(params or {})))
            return 400, {"error": {"message": "(#283) Requires pages_read_engagement"
                                              " permission to manage the object", "code": 283}}

        igfb._get_json = deny
        history_sync._PERM_PROBE_AT.clear()
        history_sync._PERM_PROBE_LAST.clear()
        why = asyncio.run(history_sync.probe_posts_readable(acc))
        check("pages_read_engagement" in why and "283" in why,
              "★ 探測直接講出缺 pages_read_engagement（不是含混的「失敗」）", why[:120])
        check(calls and "published_posts" in calls[0][0],
              "用 /published_posts 探（對權限最敏感，缺權限會明確 400）", str(calls[:1])[:120])

        n = len(calls)
        why2 = asyncio.run(history_sync.probe_posts_readable(acc))
        check(len(calls) == n, "★ 一小時內只探一次（沒權限的帳號不會每 5 分鐘白打 Meta）",
              f"{n} → {len(calls)}")
        check(why2 == why, "快取回同一句話（不重打）")

        # 200 但其實有資料 → 也要說出來（分頁/篩選異常）
        async def has_data(url, token, params=None, timeout=20):
            calls.append((url, dict(params or {})))
            return 200, {"data": [{"id": "P1"}]}

        igfb._get_json = has_data
        history_sync._PERM_PROBE_AT.clear()
        history_sync._PERM_PROBE_LAST.clear()
        why3 = asyncio.run(history_sync.probe_posts_readable(acc))
        check("有資料" in why3, "API 回 0 篇但 /published_posts 有資料 → 指出異常", why3[:80])

        # 正常（真的沒貼文）→ 不該亂報
        async def empty(url, token, params=None, timeout=20):
            return 200, {"data": []}

        igfb._get_json = empty
        history_sync._PERM_PROBE_AT.clear()
        history_sync._PERM_PROBE_LAST.clear()
        check(asyncio.run(history_sync.probe_posts_readable(acc)) == "",
              "★ 真的沒貼文 → 回空字串（不對正常帳號報警）")
    finally:
        igfb._get_json = orig_get
        history_sync._PERM_PROBE_AT.clear()
        history_sync._PERM_PROBE_LAST.clear()

    # sync_posts_once 遇到 0 篇 → errors 有紀錄 + 觸發告警
    alerts: list[dict] = []
    orig = (history_sync._igfb_accounts, history_sync.fetch_account_posts,
            history_sync.probe_posts_readable, history_sync._alert_posts_unreadable,
            igfb_store.known_post_ids, igfb_store.upsert_post)

    async def fake_fetch(a, limit):
        return []

    async def fake_probe(a):
        return "讀不到貼文列表：(#283) Requires pages_read_engagement"

    async def fake_alert(a, why):
        alerts.append({"native": a.get("platform_business_account_id"), "why": why})

    history_sync._igfb_accounts = lambda: [dict(acc)]
    history_sync.fetch_account_posts = fake_fetch
    history_sync.probe_posts_readable = fake_probe
    history_sync._alert_posts_unreadable = fake_alert
    igfb_store.known_post_ids = lambda p, ids: set()
    igfb_store.upsert_post = lambda *a, **k: None
    try:
        stats = asyncio.run(history_sync.sync_posts_once(8))
        check(len(stats["errors"]) == 1 and "pages_read_engagement" in stats["errors"][0],
              "★ 0 篇且沒權限 → 進 errors（呼叫端/日誌看得到）", str(stats["errors"])[:140])
        check(len(alerts) == 1 and alerts[0]["native"] == "PAGE_9",
              "★ 同時上報告警（ops 台 crm_alert 可見）", str(alerts)[:140])
        check(stats["new"] == 0 and stats["posts"] == 0, "統計不會因為沒貼文而爆", str(stats))
    finally:
        (history_sync._igfb_accounts, history_sync.fetch_account_posts,
         history_sync.probe_posts_readable, history_sync._alert_posts_unreadable,
         igfb_store.known_post_ids, igfb_store.upsert_post) = orig

    # 告警失敗不可以影響同步
    orig_report = None
    try:
        from app.worker import client as _c
        orig_report = _c.report_alert

        async def boom(*a, **k):
            raise RuntimeError("crm down")

        _c.report_alert = boom
        asyncio.run(history_sync._alert_posts_unreadable(acc, "why"))
        check(True, "告警上報失敗不會往外丟（best-effort）")
    except Exception as e:  # noqa: BLE001
        check(False, "告警上報失敗不會往外丟（best-effort）", f"{type(e).__name__}: {e}")
    finally:
        if orig_report is not None:
            from app.worker import client as _c
            _c.report_alert = orig_report


# ---------------------------------------------------------------- [D] 接線
def part_d() -> None:
    print("\n[D] 週期任務與設定接線")
    src = (_ROOT / "connector" / "app" / "worker" / "runner.py").read_text(encoding="utf-8")
    check("_post_sync_loop" in src, "runner 有 _post_sync_loop")
    check("sync_posts_once" in src, "loop 呼叫 history_sync.sync_posts_once")
    check("asyncio.create_task(_post_sync_loop())" in src, "★ start() 有把它掛上去")
    check("async def _post_media_loop" in src and "create_task(_post_media_loop())" in src,
          "回歸：縮圖續命 loop 還在")

    check(getattr(config, "POST_SYNC_MINUTES", None) is not None, "config 有 POST_SYNC_MINUTES")
    check(float(config.POST_SYNC_MINUTES) > 0,
          f"★ 預設有開（{config.POST_SYNC_MINUTES} 分鐘）"
          "——設 0 會讓 IG 新貼文只能手動補拉")
    check(float(config.POST_SYNC_MINUTES) * 60 <= 600,
          "週期夠短（≤10 分鐘，才算「檢測得到」）", str(config.POST_SYNC_MINUTES))
    check(int(getattr(config, "POST_SYNC_LIMIT", 0)) >= 3, "POST_SYNC_LIMIT ≥ 3")

    wsrc = (_ROOT / "connector" / "app" / "webhook.py").read_text(encoding="utf-8")
    check('elif t == "fb_post"' in wsrc, "webhook 有處理 fb_post 事件")
    check("_bg_sync_post" in wsrc, "webhook 用背景任務補齊貼文（不卡回應）")
    check("from app.channels import igfb" in wsrc, "webhook 有 import igfb（get_post 用）")

    # 事件解析的關鍵字串
    rsrc = (_ROOT / "connector" / "app" / "channels" / "igfb_recv.py").read_text(encoding="utf-8")
    check("FB_POST_ITEMS" in rsrc and '"status"' in rsrc, "有自家發文的 item 白名單")
    check('"reaction"' not in rsrc.split("FB_POST_ITEMS")[1].split(")")[0],
          "reaction 不在白名單（按讚不當新貼文）")


# ---------------------------------------------------------------- [E] DB
def part_e() -> None:
    print("\n[E] known_post_ids() 對真實資料")
    from app.core import db
    try:
        conn = db.connect()
    except Exception as e:  # noqa: BLE001
        check(True, "（跳過：連不上 DB）" + str(e)[:60])
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT platform, post_id FROM igfb_post ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        check(True, "（跳過：igfb_post 是空的）")
        return
    plat, pid = row[0], str(row[1])
    got = igfb_store.known_post_ids(plat, [pid, "definitely-not-exist-xyz"])
    check(pid in got, f"★ 認得已在庫的貼文 {pid}", str(got))
    check("definitely-not-exist-xyz" not in got, "不存在的 id 不會回傳")
    check(igfb_store.known_post_ids(plat, []) == set(), "空清單 → 空集合（不打 DB）")


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_c2()
    part_d()
    part_e()
    print(f"\n{'=' * 62}\n  新貼文偵測：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
