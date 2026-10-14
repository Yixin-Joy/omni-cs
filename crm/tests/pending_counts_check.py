"""自檢 —— 工作台「各平台未處理數」端點（CRM `GET /console/agent/pending-counts`）。

★ 這支端點存在的理由：二級平台 tab（微信 / WhatsApp / IG / FB）的紅點必須
  **在還沒點進該平台前就顯示**，而 `/conversations` 一次只回一個平台。此端點
  逐平台帳號呼叫 Connector 的 `/pending-count` 再彙總。

直接呼叫 router 函式（不起服務），把 `repo.list_accounts` 與
`connector_client.fetch_pending_count` 打樁 → 只驗彙總/邊界邏輯。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/pending_counts_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
sys.path.insert(0, str(CRM_DIR))

from app.agent import repo, router  # noqa: E402
from app.internal import connector_client  # noqa: E402

failures: list[str] = []
passes = 0


def check(cond, msg):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


ACCOUNTS = [
    {"id": 11, "business_id": 1, "platform": "wechat", "platform_business_account_id": "wx1"},
    {"id": 12, "business_id": 1, "platform": "whatsapp", "platform_business_account_id": "wa1"},
    {"id": 13, "business_id": 1, "platform": "whatsapp", "platform_business_account_id": "wa2"},
    {"id": 14, "business_id": 1, "platform": "fb", "platform_business_account_id": "fb1"},
    {"id": 15, "business_id": 1, "platform": "ig", "platform_business_account_id": "ig1"},
]

DM_COUNTS = {11: 2, 12: 1, 13: 3, 14: 0, 15: 5}
COMMENT_COUNTS = {14: 4, 15: 6}

FAKE_SESS = {"active_role": "agent", "roles": ["agent"], "is_super_agent": True}


def run() -> int:
    orig_list = repo.list_accounts
    orig_fetch = connector_client.fetch_pending_count
    try:
        repo.list_accounts = lambda business_id, platform="": [
            dict(a) for a in ACCOUNTS if not platform or a["platform"] == platform]

        def fake_fetch(platform, account_id=0, *, scope="dm"):
            table = COMMENT_COUNTS if scope == "comment" else DM_COUNTS
            return table.get(int(account_id), 0)
        connector_client.fetch_pending_count = fake_fetch

        print("\n[1] scope=dm：逐平台彙總（同平台多帳號相加）")
        res = router.pending_counts(business_id=1, scope="dm", sess=FAKE_SESS)
        check(res.get("ok") is True, "回 ok")
        counts = res.get("counts") or {}
        check(counts.get("wechat") == 2, f"wechat=2（實得 {counts.get('wechat')}）")
        check(counts.get("whatsapp") == 4, f"whatsapp=1+3=4（實得 {counts.get('whatsapp')}）")
        check(counts.get("ig") == 5, f"ig=5（實得 {counts.get('ig')}）")
        check(counts.get("fb") == 0, f"fb=0（實得 {counts.get('fb')}）")
        check(res.get("total") == 11, f"total=2+4+0+5=11（實得 {res.get('total')}）")
        check(res.get("unreachable") is False, "unreachable=false")

        print("\n[2] scope=comment：只算 ig / fb")
        res = router.pending_counts(business_id=1, scope="comment", sess=FAKE_SESS)
        counts = res.get("counts") or {}
        check(set(counts.keys()) == {"fb", "ig"}, f"只有 ig/fb（實得 {sorted(counts.keys())}）")
        check(counts.get("fb") == 4 and counts.get("ig") == 6, "fb=4, ig=6")
        check(res.get("total") == 10, f"total=10（實得 {res.get('total')}）")

        print("\n[3] Connector 取不到 → unreachable（不可當成 0）")
        def flaky(platform, account_id=0, *, scope="dm"):
            return None if int(account_id) == 12 else DM_COUNTS.get(int(account_id), 0)
        connector_client.fetch_pending_count = flaky
        res = router.pending_counts(business_id=1, scope="dm", sess=FAKE_SESS)
        check(res.get("unreachable") is True, "unreachable=true")
        check((res.get("counts") or {}).get("whatsapp") == 3,
              f"取不到的那個帳號不計入（whatsapp=3，實得 {(res.get('counts') or {}).get('whatsapp')}）")

        print("\n[4] 權限與參數")
        no_sess = {"active_role": "agent", "roles": ["agent"], "staff_id": 999999}
        res = router.pending_counts(business_id=1, scope="dm", sess=no_sess)
        check(getattr(res, "status_code", 200) == 403, f"無權限商家 → 403（實得 {getattr(res, 'status_code', 200)}）")
        res = router.pending_counts(business_id=1, scope="bogus", sess=FAKE_SESS)
        check(getattr(res, "status_code", 200) == 400, f"不支援 scope → 400（實得 {getattr(res, 'status_code', 200)}）")
    finally:
        repo.list_accounts = orig_list
        connector_client.fetch_pending_count = orig_fetch

    print("\n" + "=" * 60)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    for f in failures:
        print("   -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(run())
