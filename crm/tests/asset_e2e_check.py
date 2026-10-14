r"""圖文鏈路的**真端到端**驗證（跨 CRM / Connector 兩個真進程模組，mock 發送）。

流程與正式一模一樣，只是最後一步不真的打 Meta：
  CRM：`autoreply._asset_items()` 讀 kb 圖 → 上傳給 Connector（HTTP）→ 拿 media_key
  → `enqueue_outbound_task()` 建任務
  Connector：`build_sub_items()` 拆子項 → `_send_one()`（mock）逐項送出
  → `record_outbound_message()` 落地（工作台要看得到圖，所以一定要有 url）

⚠️ 用一個測試用 sender，跑完**立刻刪掉**寫入的訊息列（不污染真實資料）。

跑法：
  PYTHONPATH='<根>/crm;<根>/connector;<根>/.venv/Lib/site-packages' PYTHONUTF8=1 \
    <python> crm/tests/asset_e2e_check.py
"""
import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "connector"))
sys.path.insert(0, str(_ROOT / "crm"))

FAKE_SENDER = "__CK__asset_e2e"
# ⚠️ task_id 每次都要唯一：`igfb_dm` 有 UNIQUE(platform, message_id)，
#    落地用的是 `out-<task_id>`；固定值會在第二次跑時直接撞唯一鍵。
FAKE_TASK_ID = 900000 + int(time.time()) % 100000

_pass = 0
_fail: list[str] = []


def _pf_sql(sql, params=()):
    """對 Connector 的 DB（platform_forwarder）執行一句 SQL；回受影響列數。"""
    import psycopg2 as _p
    conn = _p.connect("postgresql://postgres:postgres@localhost:5432/platform_forwarder")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def check(cond, label, extra=""):
    global _pass
    if cond:
        _pass += 1
        print(f"  PASS  {label}" + (f"   {extra}" if extra else ""))
    else:
        _fail.append(label)
        print(f"  FAIL  {label}" + (f"   {extra}" if extra else ""))


def crm_sql(sql, params=(), fetch=False):
    from app.core import db
    return db.query(sql, params) if fetch else db.execute(sql, params)


# ---------------- 前置：挑幾張真實 kb 圖 ----------------
# 開跑前先清掉上次的殘留（同 sender / 同 message_id）
_pf_sql("DELETE FROM igfb_dm WHERE conversation_key=%s OR message_id=%s",
        (FAKE_SENDER, f"out-{FAKE_TASK_ID}"))

print("\n[0] 挑 2 張真實的 kb 圖")
from app.rag import kb_db                                # noqa: E402  (CRM)
from app.rag import rag_config as rc                     # noqa: E402

rows = kb_db.query(
    "SELECT rel_path, filename, mime FROM kb_asset"
    " WHERE rel_path IS NOT NULL AND rel_path <> '' ORDER BY id LIMIT 2")
assets = [{"rel_path": r["rel_path"], "filename": r["filename"],
           "mime": r["mime"] or "image/png", "caption": "e2e 測試圖"} for r in rows]
for a in assets:
    print(f"       {a['rel_path']}  ({a['filename']})")
check(len(assets) == 2, "取到 2 張圖", str(len(assets)))

# ---------------- [1] CRM 上傳 → media_key ----------------
print("\n[1] CRM `_asset_items()`：讀檔並推給 Connector，回 media_key")
from app.autoreply import autoreply as careply           # noqa: E402

items = careply._asset_items(assets)
check(len(items) == 2, "兩張圖都變成任務資產（不是被靜默丟掉）", json.dumps(items, ensure_ascii=False)[:120])
check(all(i.get("media_key") for i in items), "每個資產都有 media_key",
      str([i.get("media_key") for i in items]))
check(all(i.get("rel_path") is None for i in items),
      "不再把 CRM 端的 rel_path 丟給 Connector（欄位名對不上會被丟掉）")

# ---------------- [2]+[3] Connector 側（子進程：同進程不能同時 import 兩邊的 app） ----------------
print("\n[2] Connector（子進程）：拆子項 → 逐一送出（mock）→ 落地")
import subprocess                                       # noqa: E402


def _connector_driver(payload: dict) -> dict:
    """在 connector/ 底下用子進程跑單次動作（沿用 phase2 自檢的既有模式）。

    ⚠️ 兩個必須做對的地方（都會讓子進程靜默用錯設定）：
      ① PYTHONPATH **只放 connector 自己**（＋ .venv 依賴），**絕不帶 crm** ——
         兩個 package 都叫 `app`，crm 排前面就會 import 到 CRM 的 config，
         連到 `crm_db`，死在「relation igfb_dm does not exist」。
      ② 把 **connector 自己的設定鍵從繼承環境裡清掉** —— `crm/.env` 也有
         `MEDIA_DIR=media`，母進程載入 CRM 設定後會把它塞進 `os.environ`，
         子進程 `load_dotenv` 預設**不覆蓋**已存在的變數 → connector 會用
         `connector/media`（預設）而不是 monorepo 根的 `media/`，
         於是 `media_key` 解析不到檔案、附圖送不出去。
    """
    py = sys.executable
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        str(_ROOT / "connector"),
        str(_ROOT / ".venv" / "Lib" / "site-packages"),
    ])
    # ⚠️ 母進程（CRM）載入 `crm/.env` 時會把**它自己的設定塞進 `os.environ`**，
    #    子進程繼承後，`load_dotenv` 預設**不覆蓋**既有變數 → Connector 會拿到
    #    CRM 的值。實際踩到兩個：`DATABASE_URL`（→ 連到 crm_db，`igfb_dm` 不存在，
    #    落地靜默失敗）與 `MEDIA_DIR`（→ 圖的根目錄不對，附圖送不出去）。
    #    所以：**把 crm/.env 定義過的鍵一律剥掉**，讓 connector/.env 自己說了算。
    for line in (_ROOT / "crm" / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env.pop(line.split("=", 1)[0].strip(), None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([py, str(_ROOT / "connector" / "tests" / "_connector_driver.py"),
                        "send-assets-mock"],
                       cwd=str(_ROOT / "connector"), env=env,
                       input=json.dumps(payload, ensure_ascii=False),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    for line in (r.stdout or "").splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise RuntimeError(f"driver 沒有回 RESULT：out={r.stdout[-400:]} err={r.stderr[-600:]}")


task = {
    "task_id": FAKE_TASK_ID, "platform": "fb", "end_user_sender_id": FAKE_SENDER,
    "platform_business_account_id": "28", "business_id": 1, "bp_account_id": 28,
}
mc = {"text": "安裝步驟如下（附圖說明）", "assets": items}
# 種子檔：讓 Connector 端自己把來源圖存進它的 kb_cache（不依賴執行中服務的設定）
seeds = [rc.local_path_of_rel(a["rel_path"]) for a in assets]
res = _connector_driver({"task": task, "content": mc, "seed_files": seeds})

kinds = [i["kind"] for i in res.get("items") or []]
check(kinds == ["text", "asset", "asset"],
      "拆成 1 文字 + 2 圖（原本只會送出文字那一條）", str(kinds))
for s_ in res.get("sends") or []:
    check(s_["ok"], f"{s_['kind']} 送出成功（mock）", s_.get("error") or "")

print("\n[3] 出站落地要帶可渲染 url（工作台渲染圖片的來源）")
import psycopg2                                         # noqa: E402

_pf = "postgresql://postgres:postgres@localhost:5432/platform_forwarder"
atts = res.get("db", {}).get("attachments")
if isinstance(atts, str):
    atts = json.loads(atts or "[]")
atts = atts or []
check(res.get("db", {}).get("message_type") in ("mixed", "image"),
      "message_type 標成帶圖", str(res.get("db", {}).get("message_type")))
check(len(atts) == 2, "attachments 有 2 張圖", str(len(atts)))
check(all(a.get("url") for a in atts), "每張圖都有 url（工作台才渲染得出來）",
      str([a.get("url") for a in atts])[:150])
check(all("/files/" in (a.get("url") or "") for a in atts),
      "url 指向 /files/（CRM 會代理到 Connector）")
files = res.get("files") or []
check(len(files) == 2 and all(f.get("isImage") for f in files),
      "工作台 `_files_of()` 解析出 2 張圖（isImage=True）",
      json.dumps(files, ensure_ascii=False)[:150])

print("\n[4] Connector 的媒體目錄配置（kb_cache 必須在 MEDIA_DIR 底下）")
check(res.get("kb_cache_dir", "").startswith(res.get("media_dir", "\x00")),
      "KB_CACHE_DIR 在 MEDIA_DIR 底下",
      f"media={res.get('media_dir')} kb_cache={res.get('kb_cache_dir')}")

# ---------------- 清理 ----------------
print("\n[5] 清理測試資料")
_c = psycopg2.connect(_pf)
try:
    with _c.cursor() as cur:
        cur.execute("DELETE FROM igfb_dm WHERE platform='fb' AND"
                    " (conversation_key=%s OR message_id=%s)",
                    (FAKE_SENDER, f"out-{FAKE_TASK_ID}"))
        gone = cur.rowcount
    _c.commit()
    with _c.cursor() as cur:
        cur.execute("SELECT count(*) FROM igfb_dm WHERE conversation_key=%s OR message_id=%s",
                    (FAKE_SENDER, f"out-{FAKE_TASK_ID}"))
        left = int(cur.fetchone()[0] or 0)
finally:
    _c.close()
check(left == 0, "測試訊息列已清空", f"刪了 {gone} 列")

print("\n" + "=" * 60)
print(f"  圖文端到端：PASS {_pass} / FAIL {len(_fail)}")
if _fail:
    print("  失敗：" + "、".join(_fail))
raise SystemExit(1 if _fail else 0)
