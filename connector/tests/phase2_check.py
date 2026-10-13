"""階段二自檢 —— 異步任務與事件（端到端，真的跑兩個服務）。

覆蓋（MIGRATION_PLAN §5 階段二驗收 + §4 契約 + §1.3 映射 + §11 狀態機）：
  [1] 內網 API 認證（X-Internal-Token）
  [2] claim 原子認領（併發不重派）+ 每商家公平限流
  [3] 一個輪詢週期內送出 → 回調 sent → 寫 attempt → 出站訊息落地
  [4] 兩份 worker 併發 → 同一筆不重複發送
  [5] 媒體上傳 / 取回 / 去重 / 路徑穿越防護
  [6] 圖文任務（文字 + 附件）+ 子項冪等（重跑不重發）
  [7] 帳號映射同步（推送 + 拉取兜底）
  [8] AI 入站入隊（冪等 + 未註冊帳號）
  [9] 逾時清理三情境（含 Connector 不可達的保守處理）
  [10] 回調重試佇列（失敗落隊 → 退避重試 → 成功）

★ 為什麼用「兩個真服務 + 子進程驅動」：connector 與 crm 的 package 都叫 `app`
  （刻意的，見 MIGRATION_PLAN §2），同一 Python 進程不可能同時 import 兩邊。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> connector/tests/phase2_check.py
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
CONN_DIR = ROOT / "connector"
CRM_DIR = ROOT / "crm"
PY = sys.executable

CRM_PORT = 8110
CONN_PORT = 8111
CRM_BASE = f"http://127.0.0.1:{CRM_PORT}"
CONN_BASE = f"http://127.0.0.1:{CONN_PORT}"

MARK = "zz_p2"
BIZ_A = f"{MARK}_A"
BIZ_B = f"{MARK}_B"
WX_NATIVE = f"{MARK}_wx"
UNREG_NATIVE = f"{MARK}_unregistered"

failures: list[str] = []
procs: list[tuple[subprocess.Popen, object]] = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


# ------------------------------------------------------------------ .env / DB

def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


CRM_ENV = read_env(CRM_DIR / ".env")
CONN_ENV = read_env(CONN_DIR / ".env")
CRM_DSN = CRM_ENV["DATABASE_URL"]
CONN_DSN = CONN_ENV["DATABASE_URL"]
TOKEN = CRM_ENV.get("INTERNAL_API_TOKEN") or "CHANGE-ME-INTERNAL-TOKEN"

CRM_H = {"X-Internal-Token": TOKEN}
CONN_H = {"X-Internal-Token": TOKEN}


def db(dsn: str, sql: str, args=None, fetch=False):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


# ------------------------------------------------------------------ 子進程

def base_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def run_driver(service_dir: Path, script: str, args: list[str],
               extra_env: dict | None = None, timeout: int = 120) -> dict:
    p = subprocess.run([PY, str(service_dir / "tests" / script)] + args,
                       cwd=str(service_dir), env=base_env(extra_env),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    res = None
    for line in (p.stdout or "").splitlines():
        if line.startswith("RESULT "):
            try:
                res = json.loads(line[len("RESULT "):])
            except Exception:
                res = None
    return {"code": p.returncode, "result": res, "stdout": p.stdout, "stderr": p.stderr}


def conn_tick(limit: int = 0, extra_env: dict | None = None) -> dict:
    env = {"CRM_INTERNAL_BASE": CRM_BASE, "OUTBOUND_SEND_MODE": "mock", "WORKER_ENABLED": "false"}
    env.update(extra_env or {})
    return run_driver(CONN_DIR, "_connector_driver.py", ["tick", str(limit)], env)


def crm_sweep(extra_env: dict | None = None) -> dict:
    env = {"CONNECTOR_INTERNAL_BASE": CONN_BASE, "WORKER_ENABLED": "false"}
    env.update(extra_env or {})
    return run_driver(CRM_DIR, "_crm_driver.py", ["sweep"], env)


def start_server(service_dir: Path, port: int, extra_env: dict) -> None:
    # 日誌寫到系統暫存目錄，不污染 repo
    logf = open(Path(tempfile.gettempdir()) / f"multi_cs_p2_{port}.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(service_dir), env=base_env(extra_env), stdout=logf, stderr=subprocess.STDOUT)
    procs.append((proc, logf))
    print(f"  服務日誌：{Path(tempfile.gettempdir()) / f'multi_cs_p2_{port}.log'}")


def wait_health(base: str, timeout: float = 40) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(base + "/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def stop_servers() -> None:
    for proc, logf in procs:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            logf.close()
        except Exception:
            pass
    procs.clear()


# ------------------------------------------------------------------ 資料準備

def cleanup() -> None:
    biz = [r["id"] for r in db(CRM_DSN, "SELECT id FROM crm_business WHERE business_name LIKE %s",
                               (MARK + "%",), fetch=True)]
    task = [r["id"] for r in db(CRM_DSN, "SELECT id FROM crm_outbound_task WHERE business_id = ANY(%s)",
                                (biz or [0],), fetch=True)]
    db(CONN_DSN, "DELETE FROM connector_outbound_attempt WHERE crm_task_id = ANY(%s)", (task or [0],))
    db(CONN_DSN, "DELETE FROM connector_callback_queue WHERE crm_task_id = ANY(%s)", (task or [0],))
    db(CONN_DSN, "DELETE FROM forward_log WHERE message_id LIKE 'out-%%' AND platform_business_account_id = %s",
       (WX_NATIVE,))
    db(CONN_DSN, "DELETE FROM igfb_dm WHERE message_id LIKE 'out-%%' AND platform_business_account_id = %s",
       (WX_NATIVE,))
    db(CONN_DSN, "DELETE FROM connector_account_map WHERE platform_business_account_id LIKE %s", (MARK + "%",))
    db(CRM_DSN, "DELETE FROM crm_ai_inbound_queue WHERE business_id = ANY(%s)", (biz or [0],))
    db(CRM_DSN, "DELETE FROM crm_outbound_task WHERE business_id = ANY(%s)", (biz or [0],))
    db(CRM_DSN, "DELETE FROM crm_business_platform_account WHERE business_id = ANY(%s)", (biz or [0],))
    db(CRM_DSN, "DELETE FROM crm_business WHERE business_name LIKE %s", (MARK + "%",))


def make_task(biz: int, acc: int, sender: str, text: str, assets=None,
              status: str = "pending", processing_ago: float | None = None) -> int:
    mc = json.dumps({"text": text, "assets": assets or []}, ensure_ascii=False)
    if processing_ago is None:
        sql = ("INSERT INTO crm_outbound_task (business_id, source, platform,"
               " business_platform_account_id, end_user_sender_id, message_content, status)"
               " VALUES (%s,'manual','wechat',%s,%s,%s::jsonb,%s) RETURNING id")
        args = (biz, acc, sender, mc, status)
    else:
        sql = ("INSERT INTO crm_outbound_task (business_id, source, platform,"
               " business_platform_account_id, end_user_sender_id, message_content, status,"
               " processing_at)"
               " VALUES (%s,'manual','wechat',%s,%s,%s::jsonb,%s,"
               " now() - make_interval(secs => %s)) RETURNING id")
        args = (biz, acc, sender, mc, status, float(processing_ago))
    return db(CRM_DSN, sql, args, fetch=True)[0]["id"]


def task_status(token: str) -> str | None:
    """token 可以是 'id=123' 或 message 標記；這裡用 id 清單查。"""
    ids = [int(x) for x in token.split(",") if x.strip()]
    rows = db(CRM_DSN, "SELECT id, status FROM crm_outbound_task WHERE id = ANY(%s) ORDER BY id",
              (ids,), fetch=True)
    return {r["id"]: r["status"] for r in rows}


def reset_pending(ids: list[int]) -> None:
    db(CRM_DSN, "UPDATE crm_outbound_task SET status='pending', processing_at=NULL,"
                " finished_at=NULL, retry_count=0, next_retry_at=NULL WHERE id = ANY(%s)", (ids,))


def park_foreign() -> list[dict]:
    """把非本次自檢的 pending/processing 任務暫時挪開（**收尾會原狀還原**）。

    共用一台開發庫，別人的待處理任務不該被自檢的 claim 撈走。
    """
    rows = db(CRM_DSN,
              "SELECT t.id, t.status FROM crm_outbound_task t"
              "  JOIN crm_business b ON b.id = t.business_id"
              " WHERE t.status IN ('pending','processing')"
              "   AND b.business_name NOT LIKE %s",
              (MARK + "%",), fetch=True)
    if rows:
        db(CRM_DSN, "UPDATE crm_outbound_task SET status='failed',"
                    " fail_reason='zz_p2 自檢暫時挪開' WHERE id = ANY(%s)",
           ([r["id"] for r in rows],))
    return rows


def unpark(rows: list[dict]) -> None:
    for r in rows:
        db(CRM_DSN, "UPDATE crm_outbound_task SET status=%s,"
                    " fail_reason=NULL WHERE id=%s", (r["status"], r["id"]))


# ================================================================== 主流程

def main() -> int:
    print(f"CRM={CRM_DSN}\nCONNECTOR={CONN_DSN}\n")

    cleanup()
    start_server(CRM_DIR, CRM_PORT, {"WORKER_ENABLED": "false", "CONNECTOR_INTERNAL_BASE": CONN_BASE})
    start_server(CONN_DIR, CONN_PORT, {"WORKER_ENABLED": "false", "CRM_INTERNAL_BASE": CRM_BASE,
                                       "OUTBOUND_SEND_MODE": "mock"})
    ok_crm = wait_health(CRM_BASE)
    ok_conn = wait_health(CONN_BASE)
    print(f"服務啟動：crm={ok_crm} connector={ok_conn}")
    if not (ok_crm and ok_conn):
        print("!! 服務沒起來，中止。日誌見 " + str(Path(tempfile.gettempdir()) / "multi_cs_p2_*.log"))
        stop_servers()
        return 2

    biz_a = db(CRM_DSN, "INSERT INTO crm_business (business_name) VALUES (%s) RETURNING id",
               (BIZ_A,), fetch=True)[0]["id"]
    biz_b = db(CRM_DSN, "INSERT INTO crm_business (business_name) VALUES (%s) RETURNING id",
               (BIZ_B,), fetch=True)[0]["id"]
    acc = db(CRM_DSN, "INSERT INTO crm_business_platform_account"
                      " (business_id, platform, platform_business_account_id, display_name)"
                      " VALUES (%s,'wechat',%s,'p2 測試帳號') RETURNING id",
             (biz_a, WX_NATIVE), fetch=True)[0]["id"]
    parked = park_foreign()
    if parked:
        print(f"（已暫時挪開 {len(parked)} 筆非自檢的待處理任務，收尾會還原）")

    # ---------------------------------------------------------------- [1]
    print("\n[1] 內網 API 認證（X-Internal-Token）")
    r = httpx.get(CRM_BASE + "/internal/api/ping", timeout=5)
    check(r.status_code == 401, f"CRM 無 token → 401（實得 {r.status_code}）")
    r = httpx.get(CRM_BASE + "/internal/api/ops/config", timeout=5)
    check(r.status_code == 401, f"CRM 無 token 取運維參數 → 401（實得 {r.status_code}）")
    r = httpx.get(CRM_BASE + "/internal/api/ping", headers=CRM_H, timeout=5)
    check(r.status_code == 200, f"CRM 帶 token → 200（實得 {r.status_code}）")
    r = httpx.get(CONN_BASE + "/internal/api/ping", timeout=5)
    check(r.status_code == 401, f"Connector 無 token → 401（實得 {r.status_code}）")
    r = httpx.get(CONN_BASE + "/internal/api/ping", headers=CONN_H, timeout=5)
    check(r.status_code == 200, f"Connector 帶 token → 200（實得 {r.status_code}）")
    r = httpx.get(CRM_BASE + "/internal/api/ops/config", headers=CRM_H, timeout=5)
    cfg = (r.json() or {}).get("config") or {}
    check(r.status_code == 200 and cfg.get("outbound", {}).get("claim_batch") == 50,
          f"運維參數以巢狀回傳（outbound.claim_batch=50，實得 {cfg.get('outbound')}）")

    # ---------------------------------------------------------------- [2]
    print("\n[2] claim 原子認領 + 每商家公平限流")
    ids = ([make_task(biz_a, acc, f"u{i}", f"公平A{i}") for i in range(3)]
           + [make_task(biz_b, acc, f"v{i}", f"公平B{i}") for i in range(3)])
    r = httpx.get(CRM_BASE + "/internal/api/outbound-task/claim",
                  params={"limit": 10, "per_business_limit": 1}, headers=CRM_H, timeout=10)
    items = (r.json() or {}).get("items") or []
    ours = [it for it in items if it["business_id"] in (biz_a, biz_b)]
    per_biz = {}
    for it in ours:
        per_biz[it["business_id"]] = per_biz.get(it["business_id"], 0) + 1
    check(sorted(per_biz.values()) == [1, 1],
          f"per_business_limit=1 → 我們的兩個商家各只認領 1 筆（實得 {per_biz}）")
    check(all(it["platform_business_account_id"] == WX_NATIVE for it in ours),
          "認領結果帶平台原生 ID（對齊鍵）")
    claimed_ids = [it["task_id"] for it in items]
    reset_pending(claimed_ids)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(httpx.get, CRM_BASE + "/internal/api/outbound-task/claim",
                          params={"limit": 50, "per_business_limit": 10},
                          headers=CRM_H, timeout=15) for _ in range(4)]
        got = []
        for f in futs:
            got.extend([it["task_id"] for it in ((f.result().json() or {}).get("items") or [])])
    check(all(t in set(got) for t in ids), f"我們 6 筆在併發認領中被全部拿走（實得 {len(set(got) & set(ids))}/6）")
    check(len(set(got)) == len(got), f"沒有任何一筆被重複認領（去重後 {len(set(got))}／共 {len(got)}）")
    reset_pending(ids)
    db(CRM_DSN, "DELETE FROM crm_outbound_task WHERE id = ANY(%s)", (ids,))

    # ---------------------------------------------------------------- [3]
    print("\n[3] 一個輪詢週期內送出 → 回調 sent")
    t1 = make_task(biz_a, acc, "u1", "你好，請問營業時間？")
    t2 = make_task(biz_b, acc, "v1", "價格怎麼算？")
    tick = conn_tick()
    check(tick["result"] is not None, f"worker tick 可執行（stderr 前 200 字：{(tick['stderr'] or '')[:200]}）")
    st = task_status(f"{t1},{t2}")
    check(st.get(t1) == "sent" and st.get(t2) == "sent",
          f"一個輪詢週期內兩筆都送出（實得 {st}）")
    att = db(CONN_DSN, "SELECT crm_task_id, sub_seq, status FROM connector_outbound_attempt"
                       " WHERE crm_task_id = ANY(%s) ORDER BY crm_task_id, sub_seq",
             ([t1, t2],), fetch=True)
    check(len(att) == 2 and all(a["status"] == "sent" for a in att),
          f"每筆任務寫了一條 sent 的發送紀錄（實得 {att}）")
    msgs = db(CONN_DSN, "SELECT message_id, direction, message_type FROM forward_log"
                        " WHERE message_id LIKE 'out-%%' AND platform_business_account_id = %s",
              (WX_NATIVE,), fetch=True)
    check({m["message_id"] for m in msgs} >= {f"out-{t1}", f"out-{t2}"},
          f"出站訊息已落地訊息表（實得 {[m['message_id'] for m in msgs]}）")

    # ---------------------------------------------------------------- [4]
    print("\n[4] 兩份 worker 併發 → 不重複發送")
    ids4 = [make_task(biz_a, acc, f"w{i}", f"併發{i}") for i in range(4)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        rs = list(ex.map(lambda _: conn_tick(), range(2)))
    st4 = task_status(",".join(map(str, ids4)))
    check(all(v == "sent" for v in st4.values()), f"4 筆全部送出（實得 {st4}）")
    cnt = db(CONN_DSN, "SELECT crm_task_id, count(*) AS n FROM connector_outbound_attempt"
                       " WHERE crm_task_id = ANY(%s) GROUP BY crm_task_id", (ids4,), fetch=True)
    check(len(cnt) == 4 and all(c["n"] == 1 for c in cnt),
          f"每筆任務只留 1 條發送紀錄（無重複發送，實得 {cnt}）")

    # ---------------------------------------------------------------- [5]
    print("\n[5] 媒體上傳 / 取回 / 去重 / 路徑穿越防護")
    payload = b"\x89PNG\r\n\x1a\n" + b"p2-media-test" * 10
    r = httpx.post(CONN_BASE + "/internal/api/media/upload", params={"scope": "kb_cache"},
                   files={"file": ("p2.png", payload, "image/png")}, headers=CONN_H, timeout=15)
    up = r.json() if r.status_code == 200 else {}
    check(r.status_code == 200 and up.get("size") == len(payload),
          f"上傳成功（實得 {r.status_code} {up}）")
    mkey = up.get("media_key")
    r2 = httpx.get(CONN_BASE + "/internal/api/media/fetch", params={"media_key": mkey},
                   headers=CONN_H, timeout=10)
    check(r2.status_code == 200 and r2.content == payload, "取回的位元組與上傳一致")
    r3 = httpx.post(CONN_BASE + "/internal/api/media/upload", params={"scope": "kb_cache"},
                    files={"file": ("p2b.png", payload, "image/png")}, headers=CONN_H, timeout=15)
    check(r3.json().get("media_key") == mkey, "相同內容去重 → 同一 media_key")
    r4 = httpx.get(CONN_BASE + "/internal/api/media/fetch",
                   params={"media_key": "../../crm/.env"}, headers=CONN_H, timeout=10)
    check(r4.status_code == 400, f"路徑穿越 → 400（實得 {r4.status_code}）")
    r5 = httpx.post(CONN_BASE + "/internal/api/media/upload", params={"scope": "etc"},
                    files={"file": ("x.png", b"x", "image/png")}, headers=CONN_H, timeout=10)
    check(r5.status_code == 400, f"非法 scope → 400（實得 {r5.status_code}）")

    # ---------------------------------------------------------------- [6]
    print("\n[6] 圖文任務（文字 + 附件）+ 子項冪等")
    t6 = make_task(biz_a, acc, "u6", "這是圖文說明", assets=[{"media_key": mkey, "caption": "附圖"}])
    conn_tick()
    att6 = db(CONN_DSN, "SELECT sub_seq, status FROM connector_outbound_attempt"
                        " WHERE crm_task_id = %s ORDER BY sub_seq", (t6,), fetch=True)
    check(len(att6) == 2 and all(a["status"] == "sent" for a in att6),
          f"文字 + 附件各一個子項都送出（實得 {att6}）")
    check(task_status(str(t6)).get(t6) == "sent", "圖文任務為 sent")

    reset_pending([t6])          # 模擬 CRM 端把任務重排（例如逾時退回）
    conn_tick()
    att6b = db(CONN_DSN, "SELECT sub_seq, status FROM connector_outbound_attempt"
                         " WHERE crm_task_id = %s ORDER BY sub_seq", (t6,), fetch=True)
    check(att6b == att6, "重跑同一任務不會重發（子項冪等，紀錄不變）")
    check(task_status(str(t6)).get(t6) == "sent", "重跑後仍為 sent")

    # ---------------------------------------------------------------- [7]
    print("\n[7] 帳號映射同步（推送 + 拉取兜底）")
    r = httpx.post(CONN_BASE + "/internal/api/account-map/sync",
                   json={"items": [{"platform": "wechat",
                                    "platform_business_account_id": WX_NATIVE,
                                    "business_id": biz_a, "bp_account_id": acc,
                                    "enabled": True}]},
                   headers=CONN_H, timeout=10)
    check(r.status_code == 200 and r.json().get("upserted") == 1, "推送映射（冪等 upsert）成功")
    r = httpx.get(CONN_BASE + "/internal/api/account-map", headers=CONN_H, timeout=10)
    hit = [i for i in (r.json().get("items") or []) if i["platform_business_account_id"] == WX_NATIVE]
    check(len(hit) == 1 and hit[0]["business_id"] == biz_a,
          f"本地快取命中且 business_id 正確（實得 {hit}）")

    pull = run_driver(CONN_DIR, "_connector_driver.py", ["sync-map"],
                      {"CRM_INTERNAL_BASE": CRM_BASE, "WORKER_ENABLED": "false"})
    check((pull["result"] or {}).get("ok") is True,
          f"拉取兜底成功（實得 {pull['result'] or (pull['stderr'] or '')[:200]}）")
    r = httpx.get(CONN_BASE + "/internal/api/account-map", headers=CONN_H, timeout=10)
    check((r.json().get("items") or []) != [], "拉取後本地仍有映射")

    # ---------------------------------------------------------------- [8]
    print("\n[8] AI 入站入隊")
    body = {"message_id": f"{MARK}_m1", "platform": "wechat",
            "platform_business_account_id": WX_NATIVE, "end_user_sender_id": "u8",
            "scope": "dm", "content": {"text": "請問有現貨嗎", "assets": []}}
    r = httpx.post(CRM_BASE + "/internal/api/ai/inbound", json=body, headers=CRM_H, timeout=10)
    check(r.status_code == 200 and r.json().get("queued") is True,
          f"已註冊帳號 → 入隊（實得 {r.status_code} {r.text[:160]}）")
    rows = db(CRM_DSN, "SELECT business_id, status, content FROM crm_ai_inbound_queue"
                       " WHERE message_id = %s", (f"{MARK}_m1",), fetch=True)
    check(len(rows) == 1 and rows[0]["business_id"] == biz_a and rows[0]["status"] == "pending",
          f"佇列有一筆 pending 且商家正確（實得 {rows}）")
    check(isinstance(rows[0]["content"], dict) and rows[0]["content"].get("scope") == "dm",
          "訊息快照含 scope 且是 JSON 物件（不是純量）")
    r = httpx.post(CRM_BASE + "/internal/api/ai/inbound", json=body, headers=CRM_H, timeout=10)
    check(r.json().get("duplicate") is True, "同一 message_id 重送 → duplicate（冪等）")
    r = httpx.post(CRM_BASE + "/internal/api/ai/inbound",
                   json={**body, "message_id": f"{MARK}_m2",
                         "platform_business_account_id": UNREG_NATIVE},
                   headers=CRM_H, timeout=10)
    check(r.status_code == 200 and r.json().get("queued") is False
          and r.json().get("reason") == "unregistered_account",
          f"未註冊帳號 → 不入隊並回報原因（實得 {r.text[:160]}）")

    # ---------------------------------------------------------------- [9]
    print("\n[9] 逾時清理三情境")
    # 情境 1：無 attempt → 退回 pending
    c1 = make_task(biz_a, acc, "c1", "逾時沒發出去", status="processing", processing_ago=3600)
    # 情境 2：部分成功 → uncertain（預期 2 個子項，只成功 1 個）
    c2 = make_task(biz_a, acc, "c2", "逾時部分成功", assets=[{"media_key": mkey}],
                   status="processing", processing_ago=3600)
    db(CONN_DSN, "INSERT INTO connector_outbound_attempt"
                 " (crm_task_id, sub_seq, platform, target_id, status) VALUES (%s,0,'wechat','c2','sent')",
       (c2,))
    # 情境 3：全部成功但沒回調 → sent
    c3 = make_task(biz_a, acc, "c3", "逾時但其實成功", status="processing", processing_ago=3600)
    db(CONN_DSN, "INSERT INTO connector_outbound_attempt"
                 " (crm_task_id, sub_seq, platform, target_id, status) VALUES (%s,0,'wechat','c3','sent')",
       (c3,))

    sw = crm_sweep()
    st9 = task_status(f"{c1},{c2},{c3}")
    check(sw["result"] is not None, f"CRM sweep 可執行（stderr 前 200 字：{(sw['stderr'] or '')[:200]}）")
    check(st9.get(c1) == "pending", f"無發送紀錄 → 退回 pending（實得 {st9.get(c1)}）")
    check(st9.get(c2) == "uncertain", f"部分成功 → uncertain（實得 {st9.get(c2)}）")
    check(st9.get(c3) == "sent", f"全部成功但沒回調 → 補記 sent（實得 {st9.get(c3)}）")

    # 情境 4：Connector 不可達 → 保守 uncertain（不可重試，避免重複發送）
    c4 = make_task(biz_a, acc, "c4", "取不到發送紀錄", status="processing", processing_ago=3600)
    crm_sweep({"CONNECTOR_INTERNAL_BASE": "http://127.0.0.1:9"})
    check(task_status(str(c4)).get(c4) == "uncertain",
          f"取不到發送紀錄 → 保守標 uncertain（實得 {task_status(str(c4)).get(c4)}）")

    # ---------------------------------------------------------------- [10]
    print("\n[10] 回調重試佇列")
    t10 = make_task(biz_a, acc, "c10", "回調要重試")
    run_driver(CONN_DIR, "_connector_driver.py", ["enqueue-callback", str(t10), "sent"],
               {"WORKER_ENABLED": "false"})
    n = db(CONN_DSN, "SELECT count(*) AS n FROM connector_callback_queue WHERE crm_task_id = %s",
           (t10,), fetch=True)[0]["n"]
    check(n == 1, "回調已落重試佇列")
    f1 = run_driver(CONN_DIR, "_connector_driver.py", ["flush-callback"],
                    {"CRM_INTERNAL_BASE": "http://127.0.0.1:9", "WORKER_ENABLED": "false"})
    row = db(CONN_DSN, "SELECT retry_count, status FROM connector_callback_queue WHERE crm_task_id = %s",
             (t10,), fetch=True)[0]
    check((f1["result"] or {}).get("retry") == 1 and row["status"] == "pending" and row["retry_count"] == 1,
          f"CRM 不可達 → 保留 pending 並退避重試（實得 {f1['result']} / {row}）")
    time.sleep(1.4)          # 等退避窗口過去（第一階 1 秒）
    f2 = run_driver(CONN_DIR, "_connector_driver.py", ["flush-callback"],
                    {"CRM_INTERNAL_BASE": CRM_BASE, "WORKER_ENABLED": "false"})
    row = db(CONN_DSN, "SELECT retry_count, status FROM connector_callback_queue WHERE crm_task_id = %s",
             (t10,), fetch=True)[0]
    check((f2["result"] or {}).get("done") == 1 and row["status"] == "done",
          f"CRM 恢復 → 回調送出成功（實得 {f2['result']} / {row}）")
    check(task_status(str(t10)).get(t10) == "sent", "重試成功後任務狀態同步為 sent")

    # ---------------------------------------------------------------- 收尾
    stop_servers()
    unpark(parked)
    cleanup()
    print("\n" + ("全部通過" if not failures else f"失敗 {len(failures)} 項：{failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        stop_servers()
    sys.exit(code)
