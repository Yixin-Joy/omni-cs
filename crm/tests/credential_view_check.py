"""自檢 —— 平台憑證的「回顯 + 合併更新」語義。

起因（真實症狀）：Epro_test 各帳號轉發一切正常（憑證明明有值），
但後台點「憑證」進編輯模式，**每個欄位都是空的** → 管理員以為憑證沒填。
兩個問題：
  ① 前端一律傳 `null` 當既有值 → 不回顯。修：非敏感欄位明文回顯、
     敏感欄位用 `••••••••` 佔位 + 「已填」標記。
  ② `update_account` 的 credential 是**整塊覆蓋**（`credential = %s::jsonb`）。
     因為 ① 讓欄位看起來是空的，使用者「只想改 appsecret」就會把 appid 與 aes_key
     一起清成 NULL —— **靜默的憑證遺失**，且收發會偷偷退回 .env。

覆蓋：
  [1] credential_view()：非敏感 → 明文；敏感 → 只回「已填」；空值處理
  [2] list_accounts()：**絕不**把敏感值帶出後端（掃整個回傳 JSON）
  [3] update_account()：**逐欄合併**（只覆蓋有提供的 key，其餘保留）
  [4] update_account(credential=None)：完全不動
  [5] 端到端：只提交一個欄位 → 其他欄位仍在（＝UI「留空＝不改」真的成立）
  [6] 前端契約：mask 佔位符不會被當成新憑證提交（原始碼閘門）

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/credential_view_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ck  # noqa: E402
from _ck import check, sql  # noqa: E402

_ck.ROOT and sys.path.insert(0, str(_ck.ROOT))

from app.admin import repo  # noqa: E402
from app.core import config  # noqa: E402

MARK = "zz_credview"
BIZ = f"{_ck.MARK}{MARK}"

WECHAT_CRED = {"appid": "wx_TEST_APPID", "appsecret": "SECRET_MUST_NOT_LEAK",
               "encoding_aes_key": "AESKEY_MUST_NOT_LEAK"}


def main() -> int:
    # 清掉上次殘留
    old = sql("SELECT id FROM crm_business WHERE business_name=%s", (BIZ,))
    if old:
        _ck.purge_business(old[0]["id"])
    bid = _ck.ensure_business(BIZ)

    print(f"自檢商家 {BIZ} (id={bid})\n")

    # ------------------------------------------------------------ [1]
    print("[1] credential_view()：只明文非敏感欄位")
    filled, plain = repo.credential_view("wechat", WECHAT_CRED)
    check(filled == ["appid", "appsecret", "encoding_aes_key"],
          f"三個欄位都算「已填」（實得 {filled}）")
    check(plain == {"appid": "wx_TEST_APPID"},
          f"★ 只有非敏感欄位進明文（實得 {plain}）")
    check("appsecret" not in plain and "encoding_aes_key" not in plain,
          "★ 敏感欄位不進明文")

    filled, plain = repo.credential_view("wechat", None)
    check(filled == [] and plain == {}, "None → 空")
    filled, plain = repo.credential_view("wechat", {})
    check(filled == [] and plain == {}, "空 dict → 空")
    filled, plain = repo.credential_view("wechat", {"appid": "", "appsecret": "  "})
    check(filled == [] and plain == {}, "空字串／空白不算已填")
    filled, plain = repo.credential_view("wechat", ["not", "a", "dict"])
    check(filled == [] and plain == {}, "非 dict（JSONB 純量）不炸")

    filled, plain = repo.credential_view("fb", {"page_id": "P1", "page_access_token": "T1"})
    check(plain == {"page_id": "P1"}, f"fb：只明文 page_id（實得 {plain}）")
    filled, plain = repo.credential_view("ig", {"ig_user_id": "I1", "ig_access_token": "A",
                                                "page_access_token": "B"})
    check(plain == {"ig_user_id": "I1"}, f"ig：只明文 ig_user_id（實得 {plain}）")
    filled, plain = repo.credential_view("no_such_platform", {"x": "y"})
    check(filled == [] and plain == {}, "未知平台 → 空（不炸）")

    # ------------------------------------------------------------ [2]
    print("\n[2] list_accounts()：敏感值絕不出後端")
    with repo.tx() as (_, cur):
        aid = repo.create_account(cur, bid, "wechat", "wx_test_native", "測試號", WECHAT_CRED)
    items = repo.list_accounts(bid)
    item = [a for a in items if a["id"] == aid][0]
    blob = json.dumps(items, ensure_ascii=False, default=str)
    check("credential" not in item or not isinstance(item.get("credential"), (dict, str)),
          "回傳不含 credential 原文欄位")
    check("SECRET_MUST_NOT_LEAK" not in blob, "★ appsecret 的值不在回傳 JSON 裡")
    check("AESKEY_MUST_NOT_LEAK" not in blob, "★ encoding_aes_key 的值不在回傳 JSON 裡")
    check(item["credential_plain"].get("appid") == "wx_TEST_APPID",
          "非敏感 appid 有明文回顯（前端才看得到）")
    check(item["credential_filled"] == ["appid", "appsecret", "encoding_aes_key"],
          "credential_filled 標出全部已填欄位（前端據此顯示 ••••••••）")
    check(item["has_credential"] is True, "has_credential=True")

    # ------------------------------------------------------------ [3]
    print("\n[3] update_account()：逐欄合併")
    with repo.tx() as (_, cur):
        repo.update_account(cur, aid, "測試號", True, True, {"appsecret": "NEW_SECRET"})
    row = sql("SELECT credential FROM crm_business_platform_account WHERE id=%s", (aid,))[0]
    cred = row["credential"]
    check(cred.get("appsecret") == "NEW_SECRET", "appsecret 被更新")
    check(cred.get("appid") == "wx_TEST_APPID",
          f"★ appid **保留**（沒有被清掉）— 實得 {cred.get('appid')!r}")
    check(cred.get("encoding_aes_key") == "AESKEY_MUST_NOT_LEAK",
          f"★ encoding_aes_key **保留** — 實得 {cred.get('encoding_aes_key')!r}")

    # ------------------------------------------------------------ [4]
    print("\n[4] credential=None → 完全不動")
    with repo.tx() as (_, cur):
        repo.update_account(cur, aid, "改名了", True, True, None)
    row = sql("SELECT display_name, credential FROM crm_business_platform_account WHERE id=%s",
              (aid,))[0]
    check(row["credential"] == cred, "憑證原封不動")
    check(row["display_name"] == "改名了", "其他欄位正常更新")

    # ------------------------------------------------------------ [5]
    print("\n[5] 端到端：只提交一個欄位（模擬 UI「留空＝不改」）")
    from app.admin.router import _clean_credential
    # 使用者在編輯框只打了新的 appsecret，其他欄位留著佔位符 → 前端只提交這一個
    payload = _clean_credential("wechat", {"appsecret": "THIRD_SECRET"})
    check(payload == {"appsecret": "THIRD_SECRET"}, f"清理後只剩提供的欄位（實得 {payload}）")
    with repo.tx() as (_, cur):
        repo.update_account(cur, aid, "改名了", True, True, payload)
    cred2 = sql("SELECT credential FROM crm_business_platform_account WHERE id=%s", (aid,))[0]["credential"]
    check(cred2 == {"appid": "wx_TEST_APPID", "appsecret": "THIRD_SECRET",
                    "encoding_aes_key": "AESKEY_MUST_NOT_LEAK"},
          f"★ 三個欄位都齊（appid/aes_key 沒被清）— 實得 {cred2}")
    # 完全沒提供憑證
    check(_clean_credential("wechat", None) is None, "沒提供憑證 → None（後端不動）")
    check(_clean_credential("wechat", {}) is None, "空 dict → None")
    check(_clean_credential("wechat", {"bogus_field": "x"}) is None,
          "非法欄位被過濾 → None（不會寫進 DB）")

    # ------------------------------------------------------------ [6]
    print("\n[6] 前端契約（原始碼閘門）")
    js = (Path(_ck.ROOT).parent / "crm" / "ui" / "admin.html").read_text(encoding="utf-8")
    check("CRED_MASK" in js, "前端有遮罩常數 CRED_MASK")
    check("data-masked" in js, "佔位 input 帶 data-masked 標記")
    check("inp.dataset.masked === '1' && v === CRED_MASK" in js,
          "★ collectCred() 排除未修改的佔位符（否則用一串圓點覆蓋真憑證）")
    check("credential_plain" in js and "credential_filled" in js,
          "前端使用後端回的 credential_plain / credential_filled")
    check("cred-ok" in js, "已填欄位有「已填」標記")

    # 收尾
    _ck.purge_business(bid)
    ok = sql("SELECT count(*) AS n FROM crm_business WHERE business_name=%s", (BIZ,))[0]["n"]
    check(ok == 0, "自檢商家已清乾淨")

    return _ck.report("平台憑證回顯與合併更新自檢")


if __name__ == "__main__":
    sys.exit(main())
