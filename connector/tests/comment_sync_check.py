"""驗證：貼文留言的「週期補拉」—— webhook 訂不上的帳號也要收得到留言。

★ 為什麼要有這支（2026-10-20 小jo「商戶123，收不到最新的貼文留言」）：

  實測商家123（page 1267436386458261）這把 page token 的邊界非常誤導：

    GET /{page}/feed                             → 200（貼文讀得到）
    GET /{post}                                  → 200
    GET /{post}?fields=comments.summary(...)      → 200，total_count = 1（數量讀得到）
    GET /{post}/comments                          → 400 #100
        "This endpoint requires the 'pages_read_engagement' permission"
    GET /{page}/subscribed_apps                   → 403 #200
        "Requires pages_manage_metadata"

  → 也就是：**有一個 webhook 都沒訂上（連私訊都收不到）+ 留言列表讀不到**。
    而舊程式在這兩件事上都是「靜默」的：
      ① `fb_comments()` 在 status != 200 時只 log.warning 然後回 `[]`
         → 上層看起來跟「這篇沒人留言」完全一樣，永遠不會告警；
      ② 唯一的補拉是工作台那顆**手動**「重新整理」(`/sync-history`)，
         沒人按就一直是空的。

  修法（這支自檢要釘住的）：
    1. `fb_comments/ig_comments(strict=True)` → 失敗拋 `GraphError`（帶原始訊息）
    2. `sync_comments_once()` 週期對「每帳號最近 N 篇貼文」補拉留言（增量水位）
    3. `probe_comments_readable()` 講清楚「有留言但讀不到」是哪個權限
    4. 上報 `crm_alert(meta_comments_unreadable)`，同帳號 1h 只報一次

本自檢：
  [A] 接線（strict / loop / config / 告警）
  [B] 打樁實跑 sync_comments_once（**不打 Graph**）：新增、增量跳過、權限錯、沒貼文
  [C] 打樁實跑 probe_comments_readable：權限錯 + TTL 快取
  [D] 回歸：strict=False 仍然「失敗回 []」（sync-history 等舊呼叫端不受影響）

跑法（repo 根）：
  PYTHONPATH='<根>\\connector;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> connector/tests/comment_sync_check.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "connector"))

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


SRC_HS = (_ROOT / "connector" / "app" / "history_sync.py").read_text(encoding="utf-8")
SRC_IG = (_ROOT / "connector" / "app" / "channels" / "igfb.py").read_text(encoding="utf-8")
SRC_RUN = (_ROOT / "connector" / "app" / "worker" / "runner.py").read_text(encoding="utf-8")
SRC_CFG = (_ROOT / "connector" / "app" / "core" / "config.py").read_text(encoding="utf-8")
SRC_ST = (_ROOT / "connector" / "app" / "channels" / "igfb_store.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- [A] 接線
def part_a() -> None:
    print("[A] 接線")

    check("class GraphError" in SRC_IG, "★ igfb 有 GraphError（能帶出原始錯誤）")
    check("strict: bool = False" in SRC_IG, "fb_comments/ig_comments 收 strict 參數")
    check(SRC_IG.count("if strict:") >= 2, "★ 兩個 comments 函式都會在 strict 時拋錯")

    check("async def sync_comments_once" in SRC_HS, "★ 有 sync_comments_once()")
    check("async def probe_comments_readable" in SRC_HS, "★ 有 probe_comments_readable()")
    check("async def _alert_comments_unreadable" in SRC_HS, "有 _alert_comments_unreadable()")
    check("meta_comments_unreadable" in SRC_HS, "★ 告警類型 meta_comments_unreadable")
    check("strict=True" in SRC_HS, "★ 補拉與探測都用 strict=True（不然錯誤被吞）")
    check("comment_watermarks" in SRC_HS, "用增量水位（不重複 land 舊留言）")
    check("recompute_comment_threads" in SRC_HS and "sorted(new_roots)" in SRC_HS,
          "★ 只重算新增留言的討論串（不翻回已忽略）")

    check("def recent_post_ids" in SRC_ST, "★ igfb_store.recent_post_ids()（從 DB 取最近貼文）")
    rp = SRC_ST[SRC_ST.index("def recent_post_ids"):][:1200]
    check("bp_account_id = %s" in rp, " recent_post_ids 按帳號隔離")
    check("ORDER BY created_at DESC" in rp, "按發佈時間新到舊")

    check("async def _comment_sync_loop" in SRC_RUN, "★ runner 有 _comment_sync_loop()")
    check("asyncio.create_task(_comment_sync_loop())" in SRC_RUN, "★ loop 有註冊")
    check("COMMENT_SYNC_MINUTES" in SRC_RUN, "受 COMMENT_SYNC_MINUTES 控制")
    check("COMMENT_SYNC_MINUTES" in SRC_CFG, "★ config 有 COMMENT_SYNC_MINUTES")
    check('_get("COMMENT_SYNC_MINUTES", "5")' in SRC_CFG, "預設 5 分鐘")
    check("COMMENT_SYNC_POSTS" in SRC_CFG, "config 有 COMMENT_SYNC_POSTS（每帳號幾篇）")
    check("COMMENT_SYNC_FIRST_DELAY_SECONDS" in SRC_CFG, "有首次延遲（避免開機就打）")


# ---------------------------------------------------------------- [B] 打樁跑 sync_comments_once
def _run_sync_once(case: dict) -> dict:
    """在打樁環境下跑 `sync_comments_once`，回傳 stats + 呼叫紀錄。"""
    from app import history_sync as hs
    from app.channels import igfb as _ig, igfb_store as _st

    books: list = []
    landed: list = []
    threads: list = []

    orig = (hs._igfb_accounts, hs._cred_of, _ig._fb_creds, _ig._ig_creds,
            _ig.fb_comments, _ig.ig_comments, _st.recent_post_ids,
            _st.comment_watermarks, _st.land_comment, _st.recompute_comment_threads,
            hs._alert_comments_unreadable)
    hs._PERM_PROBE_AT.clear()
    hs._PERM_PROBE_LAST.clear()

    acc = {"platform": case.get("platform", "fb"),
           "platform_business_account_id": case.get("native", "PAGE_1"),
           "business_id": 127, "bp_account_id": 194,
           "credential": {"page_id": "PAGE_1", "page_access_token": "tok"}}
    hs._igfb_accounts = lambda: ([] if case.get("no_account") else [acc])
    hs._cred_of = lambda a: a.get("credential") or {}
    _ig._fb_creds = lambda c: (c.get("page_id") or "", c.get("page_access_token") or "")
    _ig._ig_creds = lambda c: ("IG_1", "")

    async def fake_fb_comments(post_id, token, limit=50, strict=False):
        books.append(("fb_comments", post_id, strict))
        r = case.get("graph")
        if isinstance(r, Exception):
            if strict:
                raise r
            return []
        return r if r is not None else []

    async def fake_ig_comments(media_id, token, limit=50, strict=False):
        books.append(("ig_comments", media_id, strict))
        r = case.get("graph")
        if isinstance(r, Exception):
            if strict:
                raise r
            return []
        return r if r is not None else []

    _ig.fb_comments = fake_fb_comments
    _ig.ig_comments = fake_ig_comments
    _st.recent_post_ids = lambda plat, bp, limit=3: case.get("targets", [])
    _st.comment_watermarks = lambda plat, ids: case.get("watermarks", {})

    def fake_land(platform, it, account, touch=False):
        landed.append(it.get("comment_id"))
        return case.get("land_result", {"inserted": True, "root_id": it.get("comment_id")})

    _st.land_comment = fake_land
    _st.recompute_comment_threads = lambda *a, **k: threads.append(a) or 0

    async def fake_alert(a, d):
        threads.append(("alert", d))
        return None

    hs._alert_comments_unreadable = fake_alert
    try:
        stats = asyncio.run(hs.sync_comments_once(case.get("per_account", 3)))
    finally:
        (hs._igfb_accounts, hs._cred_of, _ig._fb_creds, _ig._ig_creds,
         _ig.fb_comments, _ig.ig_comments, _st.recent_post_ids,
         _st.comment_watermarks, _st.land_comment, _st.recompute_comment_threads,
         hs._alert_comments_unreadable) = orig
    return {"stats": stats, "books": books, "landed": landed, "threads": threads}


def _cmt(cid: str, at: datetime, text: str = "hi") -> dict:
    return {"comment_id": cid, "text": text, "created_at": at, "from_id": "u1",
            "from_name": "客人", "post_id": "P1"}


def _iso(dt: datetime) -> str:
    """datetime → Graph 風格的 created_time（`+0000`，無冒號）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


def part_b() -> None:
    print("\n[B] 打樁實跑 sync_comments_once（不打 Graph）")

    now = datetime.now(timezone.utc)
    past = now - timedelta(hours=3)          # 留言時間（3 小時前）
    past2 = now - timedelta(hours=2, minutes=50)
    wm_mid = now - timedelta(hours=1)        # 水位（1 小時前）→ 兩則都在水位之前

    # 1) 有 2 則新留言 → 全落地 + 重算討論串
    r = _run_sync_once({
        "targets": [{"post_id": "P1", "comments_count": 2}],
        "graph": [{"id": "c1", "message": "a", "created_time": _iso(past),
                   "from": {"id": "u1", "name": "客人"}},
                  {"id": "c2", "message": "b", "created_time": _iso(past2),
                   "from": {"id": "u2", "name": "客人2"}}],
        "watermarks": {},
    })
    s = r["stats"]
    check(s["comments_new"] == 2, "★ 2 則新留言都落地", str(s))
    check(sorted(r["landed"]) == ["c1", "c2"], "真的呼叫了 land_comment", str(r["landed"]))
    # recompute_comment_threads(platform, post_id, self_id, roots) → pid 在 index 1
    check(any(isinstance(t, tuple) and len(t) > 1 and t[1] == "P1" for t in r["threads"]),
          "★ 有新留言 → 重算該貼文討論串", str(r["threads"])[:120])
    check(r["books"] and all(b[2] is True for b in r["books"]),
          "★ 補拉用 strict=True（錯誤不會被吞）", str(r["books"]))

    # 2) 全部在增量水位內 → 不重複落地（不洗掉「已忽略」）
    r = _run_sync_once({
        "targets": [{"post_id": "P1", "comments_count": 2}],
        "graph": [{"id": "c1", "message": "a", "created_time": _iso(past)},
                  {"id": "c2", "message": "b", "created_time": _iso(past2)}],
        "watermarks": {"P1": wm_mid},
    })
    s = r["stats"]
    check(s["comments_new"] == 0 and s["skipped"] == 2,
          "★ 水位內的留言全部跳過（不重複寫）", str(s))
    check(r["landed"] == [], "沒有呼叫 land_comment", str(r["landed"]))
    check(not any(isinstance(t, tuple) and len(t) > 1 and t[1] == "P1"
                  for t in r["threads"]),
          "★ 沒有新留言 → 不重算討論串（不把已忽略翻回未處理）")

    # 2b) 一則在水位內、一則在水位後 → 只落地後者（增量真的有效）
    r = _run_sync_once({
        "targets": [{"post_id": "P1", "comments_count": 2}],
        "graph": [{"id": "old", "message": "a", "created_time": _iso(past)},
                  {"id": "new", "message": "b", "created_time": _iso(now + timedelta(minutes=1))}],
        "watermarks": {"P1": wm_mid},
    })
    s = r["stats"]
    check(r["landed"] == ["new"] and s["comments_new"] == 1 and s["skipped"] == 1,
          "★ 只有水位後的留言會落地", f"{s} landed={r['landed']}")

    # 3) 權限不足（400 pages_read_engagement）→ 進 unreadable，不是 errors，且不炸
    denied = RuntimeError(
        "fb_comments 400: (#100) Object does not exist ... "
        "This endpoint requires the 'pages_read_engagement' permission")
    r = _run_sync_once({
        "targets": [{"post_id": "P1", "comments_count": 1}],
        "graph": denied,
    })
    s = r["stats"]
    check(s["unreadable"], "★ 權限不足 → 記進 unreadable", str(s))
    check("pages_read_engagement" in (s["unreadable"][0] if s["unreadable"] else ""),
          "原因裡帶出缺少的權限名", str(s["unreadable"])[:120])
    check(not s["errors"], "★ 權限問題不混進一般 errors（才看得出是「永遠拉不到」）", str(s))
    check(any(isinstance(t, tuple) and t and t[0] == "alert" for t in r["threads"]),
          "★ 會上報告警", str(r["threads"])[:120])

    # 4) 一般錯誤（非權限）→ 進 errors，不當成權限問題
    r = _run_sync_once({
        "targets": [{"post_id": "P1", "comments_count": 1}],
        "graph": RuntimeError("fb_comments 500: Internal Server Error"),
    })
    s = r["stats"]
    check(s["errors"] and not s["unreadable"],
          "★ 一般錯誤 → errors（不要誤判成權限）", str(s))

    # 5) 帳號還沒有貼文 → 不打 Graph、不報錯
    r = _run_sync_once({"targets": [], "graph": []})
    s = r["stats"]
    check(s["accounts"] == 0 and not r["books"] and not s["errors"],
          "★ 沒貼文的帳號 → 直接跳過", str(s))

    # 6) IG 走 ig_comments
    r = _run_sync_once({
        "platform": "ig", "native": "IG_1",
        "targets": [{"post_id": "M1", "comments_count": 1}],
        "graph": [{"id": "ic1", "text": "x", "timestamp": "2026-10-20T05:00:00+0000",
                   "from": {"id": "u1", "username": "k"}}],
    })
    check(r["books"] and r["books"][0][0] == "ig_comments",
          "★ IG 用 ig_comments", str(r["books"]))
    check(r["stats"]["comments_new"] == 1, "IG 留言也落地", str(r["stats"]))

    # 7) 一次只拉最近 N 篇（per_account 有傳下去）
    r = _run_sync_once({
        "targets": [{"post_id": "P1", "comments_count": 0},
                    {"post_id": "P2", "comments_count": 0}],
        "graph": [],
        "per_account": 2,
    })
    check(len(r["books"]) == 2, "★ 每帳號對 N 篇貼文各拉一次", str(r["books"]))


# ---------------------------------------------------------------- [C] 探測 + TTL
def part_c() -> None:
    print("\n[C] 打樁實跑 probe_comments_readable + TTL")

    from app import history_sync as hs
    from app.channels import igfb as _ig, igfb_store as _st

    orig = (hs._cred_of, _ig._fb_creds, _ig.fb_comments, _st.recent_post_ids)
    hs._PERM_PROBE_AT.clear()
    hs._PERM_PROBE_LAST.clear()

    acc = {"platform": "fb", "platform_business_account_id": "PAGE_1",
           "business_id": 127, "bp_account_id": 194,
           "credential": {"page_id": "PAGE_1", "page_access_token": "tok"}}
    hs._cred_of = lambda a: a.get("credential") or {}
    _ig._fb_creds = lambda c: ("PAGE_1", "tok")
    _st.recent_post_ids = lambda plat, bp, limit=3: [{"post_id": "P1", "comments_count": 1}]

    calls = {"n": 0}

    async def denied(post_id, token, limit=50, strict=False):
        calls["n"] += 1
        if strict:
            raise RuntimeError("fb_comments 400: This endpoint requires the "
                               "'pages_read_engagement' permission")
        return []

    try:
        _ig.fb_comments = denied
        why = asyncio.run(hs.probe_comments_readable(acc))
        check("pages_read_engagement" in why,
              "★ 探測能講出缺 pages_read_engagement", why[:100])
        check("數量" in why, "★ 也說明「貼文與留言數量仍讀得到」這個陷阱", why[:120])

        # 第二次（TTL 內）→ 不再打 Graph
        before = calls["n"]
        why2 = asyncio.run(hs.probe_comments_readable(acc))
        check(calls["n"] == before, "★ 同帳號 1h 內只探一次（不重複打 Meta）",
              f"{before} → {calls['n']}")
        check(why2 == why, "快取回同樣的結果")

        # 讀得到 → 回 ""
        async def ok(post_id, token, limit=50, strict=False):
            calls["n"] += 1
            return []

        _ig.fb_comments = ok
        hs._PERM_PROBE_AT.clear()
        hs._PERM_PROBE_LAST.clear()
        why3 = asyncio.run(hs.probe_comments_readable(acc))
        check(why3 == "", "★ 讀得到 → 回空字串（不誤報）", why3)

        # 沒貼文 → 回 ""，且不打 Graph
        _st.recent_post_ids = lambda plat, bp, limit=3: []
        hs._PERM_PROBE_AT.clear()
        hs._PERM_PROBE_LAST.clear()
        before = calls["n"]
        why4 = asyncio.run(hs.probe_comments_readable(acc))
        check(why4 == "" and calls["n"] == before,
              "★ 沒貼文 → 不探、不誤報", f"{why4} calls={calls['n']}")

        # pages_manage_metadata（webhook 訂不上）也要講清楚
        async def no_meta(post_id, token, limit=50, strict=False):
            if strict:
                raise RuntimeError("(#200) Requires pages_manage_metadata permission")
            return []

        _st.recent_post_ids = lambda plat, bp, limit=3: [{"post_id": "P1", "comments_count": 0}]
        _ig.fb_comments = no_meta
        hs._PERM_PROBE_AT.clear()
        hs._PERM_PROBE_LAST.clear()
        why5 = asyncio.run(hs.probe_comments_readable(acc))
        check("pages_manage_metadata" in why5,
              "★ 也能認出 pages_manage_metadata（webhook 訂不上）", why5[:100])
    finally:
        (hs._cred_of, _ig._fb_creds, _ig.fb_comments, _st.recent_post_ids) = orig
        hs._PERM_PROBE_AT.clear()
        hs._PERM_PROBE_LAST.clear()


# ---------------------------------------------------------------- [E] 欄位降級
def part_e() -> None:
    """★★ `from` 欄位被 Meta 拒時的**自動降級** —— 這是本次真正的根因。

    實測（商家123，頁面 1267436386458261，貼文 ..._122116181475485140）：
        fields=id,message        → 200  ✅ 內容讀得到
        fields=id,created_time   → 200  ✅
        fields=id,like_count     → 200  ✅
        fields=id,from           → 400  ❌ ← 只有這個欄位被拒
        fields=id,parent         → 200  ✅
    而 token 明明有 `pages_read_engagement` + `pages_read_user_content`
    （用商家 App 的 app token 跑 debug_token，granular_scopes 也確實授權到該粉專）。
    舊程式把 `from{name,id}` 寫死在 fields 裡 → **一個欄位拖垮整個請求**
    → 400 → 被吞成 `[]` → 看起來像「完全沒權限／沒人留言」。
    """
    print("\n[E] 欄位降級（`from` 被拒仍要讀到留言內容）")

    from app.channels import igfb

    orig = igfb._get_json
    igfb._NOFROM_WARNED.clear()

    calls: list = []

    def make(deny_from: bool):
        async def fake(url, token, params=None):
            params = params or {}
            fl = params.get("fields", "")
            calls.append(fl)
            if deny_from and "from{" in fl:
                # 模擬 Meta：帶 from 就整體 400
                return 400, {"error": {"code": 100, "message":
                    "Object does not exist ... This endpoint requires the "
                    "'pages_read_engagement' permission"}}
            return 200, {"data": [
                {"id": "c1", "message": "hi", "created_time": "2026-10-20T04:46:33+0000",
                 "like_count": 0},
                {"id": "c2", "message": "second", "created_time": "2026-10-20T05:00:00+0000"},
            ]}
        return fake

    try:
        # 1) 正常情況：一次就成功，不降級
        calls.clear()
        igfb._get_json = make(deny_from=False)
        out = asyncio.run(igfb.fb_comments("P1", "tok", 10, strict=True))
        check(len(out) == 2, "★ 正常時一次就拿到 2 則留言", str(out)[:100])
        check(len(calls) == 1, "★ 正常時只打一次（不做多餘降級）", f"calls={len(calls)}")

        # 2) from 被拒 → 降級，內容仍拿到
        calls.clear()
        igfb._NOFROM_WARNED.clear()
        igfb._get_json = make(deny_from=True)
        out = asyncio.run(igfb.fb_comments("P1", "tok", 10, strict=True))
        check(len(out) == 2, "★★ `from` 被拒 → 降級後仍讀到 2 則留言（不再是空清單）",
              str(out)[:140])
        check(out and out[0].get("message") == "hi", "★ 留言內容正確", str(out[:1])[:140])
        check(len(calls) == 2, "★ 先試完整欄位、失敗再試降級版（共 2 次）", f"calls={len(calls)}")
        check("from{" in calls[0] and "from{" not in calls[1],
              "★ 第二次真的去掉了 from", str([c[:28] for c in calls]))
        check(igfb._NOFROM_WARNED, "★ 降級有記錄（只記一次，不刷 log）")

        # 3) strict=True 也不會因為「from 被拒」就拋錯（那是可降級的）
        raised = False
        try:
            asyncio.run(igfb.fb_comments("P1", "tok", 10, strict=True))
        except igfb.GraphError:
            raised = True
        check(not raised, "★ from 被拒屬「可降級」，strict=True 不該拋錯")

        # 4) 真的無解（降級也失敗）→ strict 才拋
        async def always400(url, token, params=None):
            calls.append((params or {}).get("fields", ""))
            return 400, {"error": {"code": 100, "message": "nope"}}

        igfb._get_json = always400
        raised = False
        try:
            asyncio.run(igfb.fb_comments("P1", "tok", 10, strict=True))
        except igfb.GraphError:
            raised = True
        check(raised, "★ 降級後仍失敗 → strict=True 拋 GraphError")

        out = asyncio.run(igfb.fb_comments("P1", "tok", 10))
        check(out == [], "★ 非 strict 時仍回 []（舊呼叫端行為不變）")

        # 5) IG 也有同樣保護
        calls.clear()
        igfb._NOFROM_WARNED.clear()
        igfb._get_json = make(deny_from=True)
        out_ig = asyncio.run(igfb.ig_comments("M1", "tok", 10, strict=True))
        check(len(calls) == 2, "★ IG 也會降級重試", f"calls={len(calls)}")
        check(isinstance(out_ig, list), "IG 降級後回 list 不炸", str(out_ig)[:80])
    finally:
        igfb._get_json = orig
        igfb._NOFROM_WARNED.clear()


# ---------------------------------------------------------------- [D] 回歸
def part_d() -> None:
    print("\n[D] 回歸：strict=False 仍然「失敗回 []」")

    from app.channels import igfb as _ig

    orig = _ig._get_json

    async def boom(url, token, params=None):
        return 400, {"error": {"message": "This endpoint requires the 'pages_read_engagement'"}}

    try:
        _ig._get_json = boom
        out = asyncio.run(_ig.fb_comments("P1", "tok", 5))
        check(out == [], "★ fb_comments 預設失敗回 []（sync-history 等舊呼叫端不受影響）", str(out))

        raised = False
        try:
            asyncio.run(_ig.fb_comments("P1", "tok", 5, strict=True))
        except _ig.GraphError:
            raised = True
        check(raised, "★ strict=True 才拋 GraphError")

        out_ig = asyncio.run(_ig.ig_comments("M1", "tok", 5))
        check(out_ig == [], "ig_comments 預設也回 []")
    finally:
        _ig._get_json = orig


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_e()
    part_d()
    print(f"\n{'=' * 62}\n  貼文留言週期補拉：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
