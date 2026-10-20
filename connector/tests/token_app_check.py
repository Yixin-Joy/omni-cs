"""驗證：保存 FB/IG 帳號時要能認出「**token 屬於別的 App**」並擋下。

★ 為什麼要有這支（2026-10-20 小jo「現在還是不行嗎，meta上顯示的是有pages_read_engagement權限啊」）：

  Meta 後台顯示的是**某一個 App** 的權限，但使用者填進來的 token 可能由**另一個 App** 簽發，
  兩者互不相干。實測（同一台機器、同一次呼叫）：

    bid=1   商家1  token → GET /app 回 1612033297309372（agent）
             GET /{post}/comments → 200
    bid=127 商家123 token → GET /app 回 1611577203333651（console_eprotele）
             GET /{post}/comments → 400 #100（權限不足）
             GET /{page}/subscribed_apps → 403 #200（webhook 一個都訂不上）

  這種 token 最惡毒的地方是「看起來很正常」：
    · `/me` 回粉專名（id == page_id）→ 會誤判成「粉專令牌沒問題」
    · `/{page}/feed` → 200
    · `/{post}?fields=comments.summary(...)` → 200（還回得出 total_count=N）
  只有 `GET /app` 認得出來。而 `debug_token` **不行** —— 跨 App 的 token 它一律
  400「App_id in the input_token did not match the Viewing App」。

★★ 但**一商戶一 App 是合法的**（小jo：「以後每個商戶都可能對應一個獨立的 meta app」）：
  所以比對基準不能寫死系統 App，而是**按商戶解析**：
    帳號 `credential.meta_app_id` → 商家 `crm_business.meta_app_id` → 系統（Connector 的 .env）
  → 同時，webhook 驗簽也必須用**該商戶自己的 App Secret**，否則一商戶一 App 後
    所有那個商戶的 webhook 都會 403（而且看起來像「Meta 沒推」）。

這支自檢釘住的四件事：
  [A] 接線（Connector 端點 / CRM 兩條保存路徑 / 商家 App 欄位 / 驗簽多候選）
  [B] connector 子進程實跑 `verify_credential`：本 App / 別的 App / 用戶令牌 / 讀留言被拒
  [C] crm 子進程實跑 `_reject_wrong_app_token`：擋 / 放過 / 連不上不擋
  [D] connector 子進程實跑 `_candidate_app_secrets` + `_verify_igfb_signature`：
      **商戶 secret 能驗過、亂簽要拒、沒 header 要拒、舊行為相容**

⚠️ `crm` 與 `connector` 的頂層 package **都叫 `app`** → 同一個進程不能同時 import。
   所以 [B][D]（connector）與 [C]（crm）各自跑在**獨立子進程**，PYTHONPATH 只放一邊。

跑法（repo 根）：
  PYTHONPATH='<根>\\connector;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> connector/tests/token_app_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

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


CONN_R = (_ROOT / "connector" / "app" / "internal" / "router.py").read_text(encoding="utf-8")
CRM_R = (_ROOT / "crm" / "app" / "admin" / "router.py").read_text(encoding="utf-8")
CRM_C = (_ROOT / "crm" / "app" / "internal" / "connector_client.py").read_text(encoding="utf-8")


def _py() -> str:
    return sys.executable


def _run_isolated(code: str, side: str) -> dict:
    """在只帶 `side`（connector / crm）PYTHONPATH 的子進程裡跑 code，回傳它印出的 RESULT。"""
    root = str(_ROOT)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join([str(_ROOT / side), str(_ROOT / ".venv" / "Lib" / "site-packages")])
    # ★ 剝掉另一邊 .env 會注入的同名鍵（load_dotenv 不覆蓋 → 會污染子進程）
    other = "crm" if side == "connector" else "connector"
    env.pop("CRM_CONFIG", None)
    for k in ("META_APP_ID", "META_APP_SECRET", "INTERNAL_API_TOKEN", "DATABASE_URL",
              "PUBLIC_BASE_URL", "CONNECTOR_INTERNAL_BASE"):
        env.pop(k, None)
    env["_OTHER_SIDE"] = other

    tmp = _ROOT / ".tmp"
    tmp.mkdir(exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".py", dir=str(tmp))
    os.close(fd)
    f = Path(path)
    f.write_text(code, encoding="utf-8")
    try:
        r = subprocess.run([_py(), str(f)], capture_output=True, text=True, env=env,
                           cwd=str(_ROOT / side))
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout or "")[-1200:]}
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[7:])
        return {"error": "沒有 RESULT：" + (r.stdout or "")[-600:] + (r.stderr or "")[-600:]}
    finally:
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------- [A] 接線
def part_a() -> None:
    print("[A] 接線（靜態檢查原始碼）")

    check('"/verify-credential"' in CONN_R, "★ Connector 有 /verify-credential 端點")
    check('f"{base}/{ver}/app"' in CONN_R, "★ 用 GET /app 查 token 歸屬（跨 App 也有效）")
    body = CONN_R[CONN_R.index("async def verify_credential"):]
    body = body[:body.index("@router.get(\"/account-map\")")] if '@router.get("/account-map")' in body else body
    # 只查「實際呼叫」而不是註解 —— docstring 裡會提到 debug_token 說明為何不用它
    check("/debug_token" not in body and "verify_credential" in body,
          "★ 體檢不依賴 debug_token（跨 App 一律 400，不可靠）",
          "body 內出現 /debug_token 呼叫")
    check("is_our_app" in body, "回 is_our_app")
    check("is_page_token" in body, "回 is_page_token（用戶令牌要認得出來）")
    check('"problems"' in body, "回 problems（給人看的原因清單）")
    check("pages_read_engagement" in body, "★ 會實際打一次 /comments 驗留言權限")
    check("已達" not in body, "（無殘留）")

    check("def verify_credential" in CRM_C, "★ CRM connector_client 有 verify_credential()")
    check("timeout=20.0" in CRM_C, "體檢給足超時（要多打幾次 Graph）")

    check("def _reject_wrong_app_token" in CRM_R, "★ CRM 有 _reject_wrong_app_token()")
    rj = CRM_R[CRM_R.index("def _reject_wrong_app_token"):][:2600]
    check('res.get("is_our_app") is False' in rj, "★ 只有明確 is_our_app=False 才擋")
    check("if not res:" in rj and 'return ""' in rj,
          "★ best-effort：Connector 連不上（None）要放過")
    check("token_app_name" in rj and "expected_app_id" in rj, "錯誤訊息要講清楚兩個 App")

    ca = CRM_R[CRM_R.index("def create_account"):]
    ca = ca[:ca.index("@router.post(\"/accounts/update\")")] if \
        '@router.post("/accounts/update")' in ca else ca[:2200]
    check("_reject_wrong_app_token(platform, cred, payload.business_id)" in ca,
          "★ create_account 會擋，並把 business_id 傳進去（多租戶要按商戶解析 App）")

    ua = CRM_R[CRM_R.index("def update_account"):][:2400]
    check('_reject_wrong_app_token(row["platform"], cred, row["business_id"])' in ua,
          "★ update_account 也會擋，同樣帶 business_id")
    check(ua.index("repo.ro()") < ua.index("repo.tx()"),
          "★ 體檢在事務外（不然會持鎖 20 秒等 Graph）")

    # ---------------- 多租戶（一商戶一 App）----------------
    print("  --- 多租戶：一商戶一 App ---")
    check("def _resolve_meta_app" in CRM_R, "★ CRM 有 _resolve_meta_app()（按商戶解析 App）")
    rm = CRM_R[CRM_R.index("def _resolve_meta_app"):][:2400]
    check('cred.get("meta_app_id")' in rm, "★ 帳號層 priority 1")
    check("meta_app_id, meta_app_secret FROM crm_business" in rm,
          "★ 商家層 priority 2（crm_business 的欄位）")
    check("config, \"META_APP_ID\"" in rm or '"META_APP_ID"' in rm,
          "★ 系統層 priority 3（最後回退）")

    SCHEMA = (_ROOT / "crm" / "app" / "core" / "schema.py").read_text(encoding="utf-8")
    check("meta_app_id" in SCHEMA and "meta_app_secret" in SCHEMA,
          "★ crm_business 有 meta_app_id / meta_app_secret 欄位")
    check("ADD COLUMN IF NOT EXISTS meta_app_id" in SCHEMA,
          "★ 走 MIGRATIONS 補欄（既有庫也能升級）")

    IREPO = (_ROOT / "crm" / "app" / "internal" / "repo.py").read_text(encoding="utf-8")
    am = IREPO[IREPO.index("def account_map"):][:3200]
    check("b.meta_app_id AS biz_meta_app_id" in am,
          "★★ 帳號映射 JOIN 商家拿 App（推送與拉取兩條路都靠這支）")
    check('cred.get(key) or ""' in am or 'if not (cred.get(key)' in am,
          "★ 帳號層優先、缺了才用商家的")

    WH = (_ROOT / "connector" / "app" / "webhook.py").read_text(encoding="utf-8")
    check("def _candidate_app_secrets" in WH,
          "★★ webhook 會按 payload 的 entry.id 找「該商戶的 secret」")
    check("meta_app_secret" in WH, "候選 secret 來自帳號映射的 credential")
    check("_verify_igfb_signature(raw, request.headers, body)" in WH,
          "★ 驗簽帶上 body（不然挑不到商戶 secret）")
    vf = WH[WH.index("def _verify_igfb_signature"):][:2200]
    check("for desc, secret in cands" in vf,
          "★ 逐一試候選 secret（多租戶 + 系統級都相容）")


# ---------------------------------------------------------------- [B] connector 側實跑
_B_SRC = r'''
import asyncio, json, sys
from app.core import config
from app.internal.router import VerifyCredentialIn, verify_credential
import httpx

REAL = httpx.AsyncClient
OURS = str(getattr(config, "META_APP_ID", "") or "")

class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
    def json(self):
        return self._p

def make(case):
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            if url.endswith("/app"):
                return FakeResp(200, case.get("app") or {})
            if url.endswith("/me"):
                return FakeResp(200, case.get("me") or {})
            if "/feed" in url:
                return FakeResp(*case.get("feed", (200, {"data": [{"id": "P1"}]})))
            if url.endswith("/comments"):
                return FakeResp(*case.get("comments", (200, {"data": []})))
            return FakeResp(404, {})
    return FakeClient

def run(case):
    httpx.AsyncClient = make(case)
    try:
        return asyncio.run(verify_credential(VerifyCredentialIn(
            platform=case.get("platform", "fb"),
            credential=case.get("cred", {"page_id": "PAGE_1", "page_access_token": "tok"}))))
    finally:
        httpx.AsyncClient = REAL

OTHER = "1611577203333651"
out = {}

# 1) 本系統 App + 粉專令牌 + 留言讀得到
out["ok_case"] = run({"app": {"id": OURS, "name": "agent"},
                      "me": {"id": "PAGE_1", "name": "粉專"},
                      "comments": (200, {"data": []})})
# 2) 別的 App（本次真正根因）
out["other_app"] = run({"app": {"id": OTHER, "name": "console_eprotele"},
                        "me": {"id": "PAGE_1", "name": "粉專"},
                        "comments": (400, {"error": {"message":
                            "This endpoint requires the 'pages_read_engagement' permission"}})})
# 3) 用戶令牌
out["user_token"] = run({"app": {"id": OURS, "name": "agent"},
                         "me": {"id": "122121539151481457", "name": "Joy Li"},
                         "comments": (400, {"error": {"message": "Unsupported"}})})
# 4) App 對但缺 pages_read_engagement
out["no_perm"] = run({"app": {"id": OURS, "name": "agent"},
                      "me": {"id": "PAGE_1", "name": "粉專"},
                      "comments": (400, {"error": {"message":
                          "requires the 'pages_read_engagement' permission"}})})
# 5) 沒填 token
out["no_token"] = run({"cred": {"page_id": "PAGE_1"}})
# 6) 微信不適用
out["wechat"] = run({"platform": "wechat", "cred": {"appid": "x"}})
out["OURS"] = OURS
print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def part_b() -> None:
    print("\n[B] connector 子進程實跑 verify_credential（打樁 httpx）")

    r = _run_isolated(_B_SRC, "connector")
    if "error" in r:
        check(False, "B 段子進程執行成功", str(r["error"])[:600])
        return
    ours = r.get("OURS") or ""

    ok_case = r["ok_case"]
    check(ok_case["is_our_app"] is True, "★ 本系統 App → is_our_app=True",
          str(ok_case.get("token_app_id")))
    check(ok_case["is_page_token"] is True, "★ /me id == page_id → 粉專令牌")
    check(ok_case["problems"] == [], "★ 沒問題時 problems 為空", str(ok_case["problems"]))

    other = r["other_app"]
    check(other["is_our_app"] is False, "★ 別的 App → is_our_app=False",
          str(other.get("token_app_id")))
    check(other["token_app_name"] == "console_eprotele", "帶出對方 App 的名字")
    joined = " ".join(other["problems"])
    check("console_eprotele" in joined and "1611577203333651" in joined and ours in joined,
          "★ 原因裡同時講出「對方 App」與「本系統 App」", joined[:170])
    check("完全無效" in joined, "★ 明說在別的 App 勾權限無效", joined[:170])
    check("webhook" in joined, "★ 也提到 webhook 收不到", joined[:200])

    ut = r["user_token"]
    check(ut["is_page_token"] is False, "★ /me 回人名 → is_page_token=False")
    check(any("用戶令牌" in p for p in ut["problems"]),
          "★ 明說是「用戶令牌」讀不到貼文/留言", str(ut["problems"])[:200])

    np_ = r["no_perm"]
    check(any("pages_read_engagement" in p for p in np_["problems"]),
          "★ App 對但權限不足 → 也講清楚", str(np_["problems"])[:220])

    nt = r["no_token"]
    check(any("沒有填 token" in p for p in nt["problems"]),
          "★ 沒 token → 直接說（不用等網路）", str(nt["problems"]))

    wx = r["wechat"]
    check(wx["problems"] == [] and wx["token_app_id"] is None,
          "★ 微信不適用 → 直接放過（不誤報）", str(wx))


# ---------------------------------------------------------------- [C] crm 側實跑
_C_SRC = r'''
import json
from app.admin import router as adm
from app.internal import connector_client as cc

orig = cc.verify_credential
out = {}
try:
    cc.verify_credential = lambda p, c, **k: {
        "ok": True, "is_our_app": False,
        "token_app_id": "1611577203333651", "token_app_name": "console_eprotele",
        "expected_app_id": "1612033297309372", "problems": []}
    out["deny"] = adm._reject_wrong_app_token("fb", {"page_id": "P", "page_access_token": "t"})

    cc.verify_credential = lambda p, c, **k: {
        "ok": True, "is_our_app": True, "token_app_id": "1612033297309372",
        "expected_app_id": "1612033297309372", "problems": []}
    out["allow"] = adm._reject_wrong_app_token("fb", {"page_access_token": "t"})

    cc.verify_credential = lambda p, c, **k: None
    out["bright"] = adm._reject_wrong_app_token("fb", {"page_access_token": "t"})

    calls = {"n": 0}
    def spy(p, c, **k):
        calls["n"] += 1
        return {"is_our_app": False}
    cc.verify_credential = spy
    out["wechat"] = adm._reject_wrong_app_token("wechat", {"appid": "x"})
    out["no_cred"] = adm._reject_wrong_app_token("fb", None)
    out["calls"] = calls["n"]
finally:
    cc.verify_credential = orig
print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def part_c() -> None:
    print("\n[C] crm 子進程實跑 _reject_wrong_app_token")

    r = _run_isolated(_C_SRC, "crm")
    if "error" in r:
        check(False, "C 段子進程執行成功", str(r["error"])[:600])
        return

    deny = r["deny"]
    check(bool(deny), "★ is_our_app=False → 回錯誤字串（會擋）")
    check("console_eprotele" in deny and "1612033297309372" in deny,
          "★ 訊息帶出「對方 App」與「本系統 App」", deny[:130])
    check("重新授權" in deny, "★ 給出可執行的下一步", deny[:170])

    check(r["allow"] == "", "★ 本系統 App → 放過")
    check(r["bright"] == "",
          "★★ Connector 連不上（None）→ 放過，不擋保存")
    check(r["wechat"] == "" and r["calls"] == 0,
          "★ 微信不體檢（不打無謂的網路），且完全沒呼叫", f'calls={r["calls"]}')
    check(r["no_cred"] == "",
          "★ 沒帶憑證（只改名字/開關）→ 不擋")


# ---------------------------------------------------------------- [D] webhook 多租戶驗簽
_D_SRC = r'''
import hashlib, hmac, json, sys
from app import webhook
from app.core import config, db

BIZ_SEC = "b" * 32
SYS_SEC = "s" * 32
PAGE = "MT_SIG_PAGE"

# 臨時：一個「屬於某商戶、有自己 secret」的帳號映射（測完刪）
c = db.connect()
with c.cursor() as cur:
    cur.execute("INSERT INTO connector_account_map"
                " (platform, platform_business_account_id, business_id, bp_account_id,"
                "  enabled, credential, synced_at)"
                " VALUES ('fb', %s, 203, 9999, true, %s::jsonb, now())"
                " ON CONFLICT (platform, platform_business_account_id) DO UPDATE SET"
                "  credential = EXCLUDED.credential",
                (PAGE, json.dumps({"page_id": PAGE, "page_access_token": "tok",
                                   "meta_app_secret": BIZ_SEC})))
c.commit(); c.close()

raw = json.dumps({"object": "page", "entry": [{"id": PAGE, "changes": []}]}).encode()
body = json.loads(raw)
out = {}

def sig(secret):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

class H(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

orig_flag = config.META_VERIFY_SIGNATURE
orig_sys = config.META_APP_SECRET
try:
    config.META_VERIFY_SIGNATURE = True
    config.META_APP_SECRET = SYS_SEC

    out["cands"] = [d for d, s in webhook._candidate_app_secrets(body)]
    out["has_biz_sec"] = any(s == BIZ_SEC for _d, s in webhook._candidate_app_secrets(body))

    def try_sig(s):
        try:
            webhook._verify_igfb_signature(raw, H({"x-hub-signature-256": s}), body)
            return True
        except Exception as e:
            return str(e)

    out["biz_ok"] = try_sig(sig(BIZ_SEC)) is True
    out["sys_ok"] = try_sig(sig(SYS_SEC)) is True
    out["bad_rej"] = try_sig(sig("wrong" * 8))
    out["no_hdr_rej"] = try_sig("")
    # 舊行為：不傳 body → 只用系統 secret
    try:
        webhook._verify_igfb_signature(raw, H({"x-hub-signature-256": sig(SYS_SEC)}))
        out["legacy_ok"] = True
    except Exception as e:
        out["legacy_ok"] = str(e)
    # 關閉驗簽 → 一律放過
    config.META_VERIFY_SIGNATURE = False
    try:
        webhook._verify_igfb_signature(raw, H({}), body)
        out["off_ok"] = True
    except Exception as e:
        out["off_ok"] = str(e)
finally:
    config.META_VERIFY_SIGNATURE = orig_flag
    config.META_APP_SECRET = orig_sys
    c = db.connect()
    with c.cursor() as cur:
        cur.execute("DELETE FROM connector_account_map"
                    " WHERE platform_business_account_id=%s", (PAGE,))
    c.commit(); c.close()

print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def part_d() -> None:
    print("\n[D] connector 子進程實跑 webhook 多租戶驗簽")

    r = _run_isolated(_D_SRC, "connector")
    if "error" in r:
        check(False, "D 段子進程執行成功", str(r["error"])[:700])
        return

    check(r["has_biz_sec"], "★ 候選 secret 含「該商戶自己的」",
          str(r.get("cands")))
    check("system" in (r.get("cands") or []), "候選 secret 也含系統級（混合部署相容）",
          str(r.get("cands")))
    check(r["biz_ok"] is True,
          "★★ 用**商戶自己的 App Secret** 簽 → 驗證通過（一商戶一 App 的關鍵）")
    check(r["sys_ok"] is True, "系統 secret 簽也能過（舊部署不受影響）")
    check(r["bad_rej"] is not True, "★ 亂簽要拒", str(r["bad_rej"]))
    check(isinstance(r["bad_rej"], str) and "已試" in r["bad_rej"],
          "拒絕訊息列出試過哪些 secret（好排錯）", str(r["bad_rej"])[:120])
    check(r["no_hdr_rej"] is not True, "★ 沒帶簽章 header 要拒", str(r["no_hdr_rej"]))
    check(r["legacy_ok"] is True,
          "★ 相容：不傳 body（舊呼叫）→ 仍用系統 secret 驗")
    check(r["off_ok"] is True, "關閉驗簽（META_VERIFY_SIGNATURE=false）→ 一律放過")


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_d()
    print(f"\n{'=' * 62}\n  Token 歸屬體檢（多租戶）：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
