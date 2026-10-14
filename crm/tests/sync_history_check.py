"""自檢 —— CRM `POST /console/agent/sync-history`（工作台「重新整理」補拉歷史）。

只驗 CRM 這一層的**編排**（不起服務、不打真 Connector）：
  彙整登入者可訪問商家的帳號 → 交給 Connector → 原樣帶回統計 → 寫審計。

覆蓋：
  [1] 超級坐席：所有商家的帳號都送去補拉
  [2] 一般客服：只送自己被授權的商家（不越權）
  [3] 無客服身分 → 403
  [4] Connector 連不上 → 502（不可假裝成功）
  [5] 託管／不支援平台的原因原樣帶回前端
  [6] 審計有寫（operate_type = sync_history）
  [7] 沒有任何帳號時不亂打 Connector

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/sync_history_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
sys.path.insert(0, str(CRM_DIR))

from app.agent import router  # noqa: E402
from app.internal import connector_client  # noqa: E402

failures: list[str] = []
passes = 0


def check(cond, msg, extra=""):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


BUSINESSES = [{"id": 1, "business_name": "甲商"}, {"id": 2, "business_name": "乙商"}]
ACCOUNTS = {
    1: [{"id": 11, "business_id": 1, "platform": "fb",
         "platform_business_account_id": "fb1", "display_name": "甲粉專"},
        {"id": 12, "business_id": 1, "platform": "whatsapp",
         "platform_business_account_id": "wa1", "display_name": "甲 WA"}],
    2: [{"id": 21, "business_id": 2, "platform": "ig",
         "platform_business_account_id": "ig1", "display_name": "乙 IG"}],
}

SUPER = {"active_role": "agent", "roles": ["admin", "agent"], "staff_id": 1, "is_super_agent": True}
AGENT = {"active_role": "agent", "roles": ["agent"], "staff_id": 2, "is_super_agent": False}
OPS = {"active_role": "ops", "roles": ["ops"], "staff_id": 3, "is_super_agent": False}


def run() -> int:
    calls: dict = {"items": None, "audit": []}
    originals = {
        "list_businesses": router.repo.list_businesses,
        "list_accounts": router.repo.list_accounts,
        "sync_history": router.connector_client.sync_history,
        "audit": router.audit.log_standalone,
        "can_view": router.rbac.can_view_conversation,
        "is_super": router.rbac.is_super_agent,
        "allowed": router.rbac.allowed_business_ids,
    }
    try:
        router.repo.list_businesses = lambda: [dict(b) for b in BUSINESSES]
        router.repo.list_accounts = lambda business_id, platform="": [
            dict(a) for a in ACCOUNTS.get(int(business_id), [])]
        router.audit.log_standalone = lambda sess, op, **kw: calls["audit"].append((op, kw))
        router.rbac.can_view_conversation = lambda sess: "agent" in (sess.get("roles") or [])
        router.rbac.is_super_agent = lambda sess: bool(sess.get("is_super_agent"))
        router.rbac.allowed_business_ids = lambda sess: [1]

        def fake_sync(items, timeout=300.0):
            calls["items"] = items
            return {"ok": True,
                    "items": [{"platform": it["platform"], "supported": it["platform"] != "whatsapp",
                               "reason": None if it["platform"] != "whatsapp" else "沒有歷史 API"}
                              for it in items],
                    "totals": {"accounts": len(items), "dm_new": 4, "dm_out": 2,
                               "dm_handled": 1, "dm_pending": 3, "posts": 1, "comments_new": 2,
                               "accounts_unsupported": 1, "accounts_failed": 0,
                               "errors": ["某帳號有警告"]}}
        router.connector_client.sync_history = fake_sync

        print("\n[1] 超級坐席：所有商家的帳號都送去補拉")
        res = router.sync_history(sess=SUPER)
        check(res.get("ok") is True, "回 ok")
        sent = {(i["platform"], i["platform_business_account_id"]) for i in (calls["items"] or [])}
        check(sent == {("fb", "fb1"), ("whatsapp", "wa1"), ("ig", "ig1")},
              f"送出全部 3 個帳號（實得 {sorted(sent)}）")
        check(res["totals"]["dm_new"] == 4, "totals 原樣帶回（dm_new=4）")
        check(res["totals"]["dm_pending"] == 3 and res["totals"]["dm_handled"] == 1,
              "補紅點 / 已回覆不補 兩個數字都在")
        check(res["totals"]["accounts_unsupported"] == 1, "不支援帳號數帶回")

        print("\n[2] 一般客服：只送被授權的商家")
        res2 = router.sync_history(sess=AGENT)
        sent2 = {(i["platform"], i["platform_business_account_id"]) for i in (calls["items"] or [])}
        check(sent2 == {("fb", "fb1"), ("whatsapp", "wa1")},
              f"只送商家的 1 的帳號（實得 {sorted(sent2)}）")
        check(("ig", "ig1") not in sent2, "★ 沒有越權送出乙商的帳號")
        check(res2.get("ok") is True, "一般客服也能補拉")

        print("\n[3] 無客服身分 → 403")
        res3 = router.sync_history(sess=OPS)
        check(getattr(res3, "status_code", 200) == 403, f"純運維 → 403（實得 {getattr(res3,'status_code',200)}）")

        print("\n[4] Connector 連不上 → 502")
        router.connector_client.sync_history = lambda items, timeout=300.0: None
        res4 = router.sync_history(sess=SUPER)
        check(getattr(res4, "status_code", 200) == 502,
              f"連不上 → 502（實得 {getattr(res4,'status_code',200)}）")
        router.connector_client.sync_history = fake_sync

        print("\n[5] 不支援平台的原因原樣帶回")
        router.connector_client.sync_history = fake_sync
        res5 = router.sync_history(sess=SUPER)
        wa = next((i for i in res5["items"] if i["platform"] == "whatsapp"), None)
        check(wa is not None and wa["supported"] is False and wa["reason"] == "沒有歷史 API",
              "whatsapp 的 reason 帶回前端")

        print("\n[6] 審計")
        check(len(calls["audit"]) >= 3, f"有寫審計（{len(calls['audit'])} 筆）")
        ops = [op for op, _kw in calls["audit"]]
        check(all(op == "sync_history" for op in ops), f"operate_type=sync_history（實得 {set(ops)}）")
        kw = calls["audit"][0][1]
        check(kw.get("payload", {}).get("accounts") == 3, "審計帶帳號數")
        check("totals" in (kw.get("payload") or {}), "審計帶 totals")

        print("\n[7] 沒有任何帳號 → 送空清單，且 client 不打網路")
        router.repo.list_accounts = lambda business_id, platform="": []
        seen = {"items": "unset"}

        def counting(items, timeout=300.0):
            seen["items"] = items
            return {"ok": True, "items": [], "totals": {}}
        router.connector_client.sync_history = counting
        res7 = router.sync_history(sess=SUPER)
        check(res7.get("ok") is True, "回 ok")
        check(seen["items"] == [], f"送空清單（實得 {seen['items']}）")

        # ★ client 端短路：空清單直接回 ok，不開連線（否則 CRM 會白等 5 秒）
        short = originals["sync_history"]([])
        check(short.get("ok") is True and short.get("items") == [],
              "connector_client.sync_history([]) 直接回空，不打網路")
    finally:
        router.repo.list_businesses = originals["list_businesses"]
        router.repo.list_accounts = originals["list_accounts"]
        router.connector_client.sync_history = originals["sync_history"]
        router.audit.log_standalone = originals["audit"]
        router.rbac.can_view_conversation = originals["can_view"]
        router.rbac.is_super_agent = originals["is_super"]
        router.rbac.allowed_business_ids = originals["allowed"]

    print("\n" + "=" * 64)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    if failures:
        for f in failures:
            print("   -", f)
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(run())
