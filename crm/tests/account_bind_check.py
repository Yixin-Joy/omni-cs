"""自檢 —— 平台帳號綁定：原生 ID 由憑證推導 + 憑證必填。

起因（小jo）：
  ① 「既然原生 ID 和每個平台的 AppID / phone_number_id / page id / ig user id 重合，
     新增綁定的時候就不用再填一遍了吧，只是在賬號查看頁面統一顯示成原生 id 一欄。」
  ② 「這些憑證并不是選填，想連接就必須填。」

所以兩條規則：
  ★ `platform_business_account_id` **不再由使用者填** —— 它就是某一個憑證欄位
    （見 `config.NATIVE_ID_FIELD`），伺服器端從憑證推導。
  ★ 憑證**必填** —— 缺任何一項直接 400（見 `config.CREDENTIAL_REQUIRED`），
    不再允許「先建空殼之後再補」：空殼帳號會讓 webhook 取不到憑證而靜默失敗。

覆蓋：
  [1] 設定一致性：NATIVE_ID_FIELD / CREDENTIAL_REQUIRED 都指向真實存在的憑證欄位
  [2] 四個平台各自：只給憑證（不給原生 ID）→ 原生 ID 正確推導
  [3] 憑證缺項 → 400，且**一列都不寫進 DB**
  [4] 舊前端（仍帶 platform_business_account_id）向後相容
  [5] 前端契約閘門：沒有原生 ID 輸入框、有「＝原生 ID」標記、不送該欄位

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/account_bind_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ck  # noqa: E402
from _ck import check, sql  # noqa: E402

_ck.ROOT and sys.path.insert(0, str(_ck.ROOT))

from app.admin import router as AR  # noqa: E402
from app.admin import repo  # noqa: E402
from app.core import config  # noqa: E402

MARK = "acctbind"
BIZ = f"{_ck.MARK}{MARK}"

ADMIN = {"active_role": "admin", "roles": ["admin"], "staff_id": 1}

# 每個平台的「完整憑證」（原生 ID 用欄位刻意填成可辨識的值）
FULL_CRED = {
    "wechat": {"appid": "wx_NATIVE_APPID", "appsecret": "S1", "encoding_aes_key": "K1"},
    "whatsapp": {"phone_number_id": "111222333", "access_token": "T2"},
    "fb": {"page_id": "PAGE_999", "page_access_token": "T3"},
    "ig": {"ig_user_id": "IG_777", "ig_access_token": "T4", "page_access_token": "T5"},
}
EXPECT_NATIVE = {"wechat": "wx_NATIVE_APPID", "whatsapp": "111222333",
                 "fb": "PAGE_999", "ig": "IG_777"}


def _err_of(res) -> str:
    return json.dumps(res.body.decode("utf-8") if hasattr(res, "body") else res,
                      ensure_ascii=False) if not isinstance(res, dict) else str(res)


def _status(res) -> int:
    return getattr(res, "status_code", 200)


def main() -> int:
    old = sql("SELECT id FROM crm_business WHERE business_name=%s", (BIZ,))
    if old:
        _ck.purge_business(old[0]["id"])
    bid = _ck.ensure_business(BIZ)
    print(f"自檢商家 {BIZ} (id={bid})\n")

    # ------------------------------------------------------------ [1]
    print("[1] 設定一致性")
    for plat in ("wechat", "whatsapp", "fb", "ig"):
        keys = {k for k, _, _ in config.CREDENTIAL_FIELDS.get(plat, [])}
        nk = config.NATIVE_ID_FIELD.get(plat)
        check(nk in keys, f"{plat}：原生 ID 欄位「{nk}」是真實憑證欄位")
        req = set(config.CREDENTIAL_REQUIRED.get(plat, ()))
        check(req and req <= keys, f"{plat}：必填集合 ⊆ 憑證欄位（{sorted(req)}）")
        check(nk in req, f"{plat}：原生 ID 欄位本身也在必填集合內")
    check(set(config.NATIVE_ID_FIELD) == set(config.CREDENTIAL_REQUIRED),
          "兩個映射的平台集合一致")

    # ------------------------------------------------------------ [2]
    print("\n[2] 只給憑證 → 原生 ID 自動推導（四平台）")
    made: dict[str, int] = {}
    for plat, cred in FULL_CRED.items():
        res = AR.create_account(AR.AccountIn(business_id=bid, platform=plat,
                                             display_name="測試", credential=cred),
                                sess=ADMIN)
        if _status(res) != 200:
            check(False, f"{plat}：建立成功", _err_of(res))
            continue
        aid = res["id"]
        made[plat] = aid
        row = sql("SELECT platform_business_account_id AS n FROM"
                  " crm_business_platform_account WHERE id=%s", (aid,))[0]
        check(row["n"] == EXPECT_NATIVE[plat],
              f"★ {plat}：原生 ID 由憑證推導 = {EXPECT_NATIVE[plat]}（實得 {row['n']}）")

    # ------------------------------------------------------------ [3]
    print("\n[3] 憑證必填：缺項 → 400 且不寫 DB")
    for plat, cred in FULL_CRED.items():
        for drop in config.CREDENTIAL_REQUIRED[plat]:
            partial = {k: v for k, v in cred.items() if k != drop}
            res = AR.create_account(
                AR.AccountIn(business_id=bid, platform=plat,
                             credential=partial, display_name="應被擋"),
                sess=ADMIN)
            ok = _status(res) == 400
            check(ok, f"{plat}：缺「{drop}」→ 400（實得 {_status(res)}）")
            if ok:
                body = res.body.decode("utf-8")
                check(drop in body or "缺少必要憑證" in body,
                      f"     錯誤訊息指得出缺哪一項", body[:80])
    # 完全不給憑證
    res = AR.create_account(AR.AccountIn(business_id=bid, platform="fb"), sess=ADMIN)
    check(_status(res) == 400, f"完全沒憑證 → 400（實得 {_status(res)}）")
    n = sql("SELECT count(*) AS n FROM crm_business_platform_account WHERE business_id=%s",
            (bid,))[0]["n"]
    check(n == 4, f"★ 被擋下的都沒有寫進 DB（應為 4 筆，實得 {n}）")

    # ------------------------------------------------------------ [4]
    print("\n[4] 向後相容：舊前端仍帶 platform_business_account_id")
    res = AR.create_account(
        AR.AccountIn(business_id=bid, platform="fb",
                     platform_business_account_id="LEGACY_PAGE_ID",
                     credential={"page_id": "SHOULD_NOT_WIN", "page_access_token": "T"},
                     display_name="舊前端"),
        sess=ADMIN)
    if _status(res) == 200:
        row = sql("SELECT platform_business_account_id AS n FROM"
                  " crm_business_platform_account WHERE id=%s", (res["id"],))[0]
        check(row["n"] == "LEGACY_PAGE_ID",
              f"顯式傳來的原生 ID 優先（實得 {row['n']}）")
    else:
        check(False, "舊前端仍可建立", _err_of(res))

    # ------------------------------------------------------------ [5]
    print("\n[5] 前端契約（原始碼閘門）")
    html = (Path(_ck.ROOT) / "ui" / "admin.html").read_text(encoding="utf-8")
    check("id=\"acctNative\"" not in html,
          "★ 新增表單已無「平台原生帳號 ID」輸入框（不再讓使用者填第二次）")
    check("acctNativeNote" in html, "有原生 ID 來源說明節點")
    check("NATIVE_ID_FIELD" in html, "前端有 NATIVE_ID_FIELD 映射（與後端對齊）")
    check("CREDENTIAL_REQUIRED" in html, "前端有 CREDENTIAL_REQUIRED（提交前先擋）")
    check("＝原生 ID" in html, "憑證欄位標出「＝原生 ID」")
    # ★ 精確檢查「新增綁定」實際送出的 payload：不得含 platform_business_account_id 這個 key
    #   （只看 POST 物件字面值，不看註解或表格渲染那幾處）
    seg = html.split("api('/api/admin/accounts', 'POST', {")[1].split("})")[0]
    check("platform_business_account_id" not in seg,
          "★ 新增時不送 platform_business_account_id（交給後端推導）", seg.strip()[:100])
    check("credential:" in seg, "新增時有送憑證（原生 ID 就從這裡推導）")
    check("'必填'" in html, "憑證欄位 placeholder 顯示「必填」而非「選填」")
    check("'選填'" not in html.split("function renderCredFields")[1].split("function collectCred")[0],
          "★ renderCredFields 內已無「選填」字樣")

    # 收尾
    _ck.purge_business(bid)
    left = sql("SELECT count(*) AS n FROM crm_business WHERE business_name=%s", (BIZ,))[0]["n"]
    check(left == 0, "自檢商家已清乾淨")
    return _ck.report("平台帳號綁定（原生 ID 推導 + 憑證必填）自檢")


if __name__ == "__main__":
    sys.exit(main())
