"""憑證連通性自檢 —— 用 **DB 裡的平台憑證**（經帳號映射快取）打各平台唯讀 API。

目的：確認「憑證已正確匯入 DB 且 Connector 取得到、且真的能用」，
而不只是檢查欄位存在。**全程唯讀**（不發任何訊息、不改任何狀態）。

覆蓋：
  [1] 四個平台帳號的 credential 都從 account_map 快取取得到、欄位齊全
  [2] 微信：用 appid/appsecret 換 access_token（唯讀）
  [3] WhatsApp：讀 phone_number_id 的基本資訊（唯讀）
  [4] Facebook：讀 page_id 的基本資訊（唯讀）
  [5] Instagram：讀 /me（唯讀）
  [6] 憑證不是走 .env fallback（快取真的有值）

跑法（從 connector 目錄）：
    PYTHONPATH='<connector>;<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> tests/credentials_check.py [--offline]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import account_map  # noqa: E402
from app.core import config  # noqa: E402

fails: list[str] = []
oks = [0]


def check(cond, msg, extra=""):
    if cond:
        oks[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(msg)
    return bool(cond)


def get_json(url: str, timeout: int = 15) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"_error": str(e)}


# 這四個是 Epro_test 平台上線帳號（原生 ID = 對齊鍵）
ACCOUNTS = [
    ("wechat", config.WECHAT_APPID),
    ("whatsapp", config.WHATSAPP_PHONE_NUMBER_ID),
    ("fb", config.META_PAGE_ID),
    ("ig", config.META_IG_USER_ID),
]

EXPECT_KEYS = {
    "wechat": {"appid", "appsecret", "encoding_aes_key"},
    "whatsapp": {"access_token", "phone_number_id"},
    "fb": {"page_id", "page_access_token"},
    "ig": {"ig_user_id", "ig_access_token"},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只驗憑證存在，不打外部 API")
    args = ap.parse_args()

    print("=== [1] 帳號映射快取的憑證 ===")
    creds = {}
    for platform, native in ACCOUNTS:
        if not native:
            check(False, f"{platform}: config 有原生 ID", "config 缺值")
            continue
        acc = account_map.lookup(platform, native)
        if not acc:
            check(False, f"{platform}: 快取可反查", f"native={native} 不在 connector_account_map")
            continue
        cred = acc.get("credential") or {}
        creds[platform] = cred
        got = set(cred.keys())
        want = EXPECT_KEYS[platform]
        check(bool(cred), f"{platform}: 快取有 credential（非 .env fallback）")
        check(want <= got, f"{platform}: 憑證欄位齊全", f"缺={sorted(want - got)}" if want - got else f"{len(got)} 欄")

    if args.offline:
        print(f"\n結果（offline）：{'PASS' if not fails else 'FAIL'}  （PASS {oks[0]} / FAIL {len(fails)}）")
        for f in fails:
            print("   FAIL:", f)
        return 0 if not fails else 1

    gv_fb = config.META_GRAPH_VERSION or "v21.0"
    gv_wa = config.GRAPH_API_VERSION or "v22.0"

    print("\n=== [2] 微信：換 access_token（唯讀）===")
    c = creds.get("wechat", {})
    if c.get("appid") and c.get("appsecret"):
        q = urllib.parse.urlencode({"grant_type": "client_credential",
                                    "appid": c["appid"], "secret": c["appsecret"]})
        st, body = get_json("https://api.weixin.qq.com/cgi-bin/token?" + q)
        ok = isinstance(body, dict) and bool(body.get("access_token"))
        check(ok, "微信 appid/appsecret 可換 access_token",
              "OK" if ok else f"errcode={body.get('errcode')} errmsg={body.get('errmsg')}")
    else:
        check(False, "微信憑證齊全", "缺 appid/appsecret")

    print("\n=== [3] WhatsApp：讀號碼資訊（唯讀）===")
    c = creds.get("whatsapp", {})
    if c.get("access_token") and c.get("phone_number_id"):
        url = (f"https://graph.facebook.com/{gv_wa}/{c['phone_number_id']}"
               f"?fields=display_phone_number,verified_name&access_token={c['access_token']}")
        st, body = get_json(url)
        ok = st == 200 and "id" in body
        check(ok, "WhatsApp access_token 可讀 phone_number_id",
              f"verified_name={body.get('verified_name')}" if ok else f"HTTP {st} {str(body)[:120]}")
    else:
        check(False, "WhatsApp 憑證齊全", "缺 access_token/phone_number_id")

    print("\n=== [4] Facebook：讀粉專資訊（唯讀）===")
    c = creds.get("fb", {})
    if c.get("page_access_token") and c.get("page_id"):
        url = (f"https://graph.facebook.com/{gv_fb}/{c['page_id']}"
               f"?fields=name,id&access_token={c['page_access_token']}")
        st, body = get_json(url)
        ok = st == 200 and "id" in body
        check(ok, "FB page_access_token 可讀 page_id",
              f"name={body.get('name')}" if ok else f"HTTP {st} {str(body)[:120]}")
    else:
        check(False, "FB 憑證齊全", "缺 page_access_token/page_id")

    print("\n=== [5] Instagram：讀 /me（唯讀）===")
    c = creds.get("ig", {})
    if c.get("ig_access_token"):
        host = config.ig_graph_host()
        url = f"{host}/{gv_fb}/me?fields=id,username&access_token={c['ig_access_token']}"
        st, body = get_json(url)
        ok = st == 200 and "id" in body
        check(ok, f"IG token 可讀 /me（{host}）",
              f"username={body.get('username')}" if ok else f"HTTP {st} {str(body)[:120]}")
    else:
        check(False, "IG 憑證齊全", "缺 ig_access_token")

    print(f"\n結果：{'PASS' if not fails else 'FAIL'}  （PASS {oks[0]} / FAIL {len(fails)}）")
    for f in fails:
        print("   FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
