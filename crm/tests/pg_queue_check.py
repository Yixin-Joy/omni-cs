"""實測：PostgreSQL 能不能取代 Redis 當「任務通知 + 可靠派發」？

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/pg_queue_check.py

測兩件事：
  A. 併發原子認領 —— 多個 worker 同時 claim，是否會重複派發？
     （用 FOR UPDATE SKIP LOCKED，等同 crm_outbound_task 的 claim API 核心）
  B. LISTEN/NOTIFY 能不能跨 database 送通知？
     （CRM 連 crm_db、Connector 連 platform_forwarder —— 同實例不同庫）
"""
import select
import sys
import threading
import time
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import config  # noqa: E402

CRM_DSN = config.DATABASE_URL
PROBE_TABLE = "_pg_queue_probe"
TOTAL_TASKS = 40
WORKERS = 4
BATCH = 5

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------- 測 A
def test_atomic_claim():
    print("\n[A] 併發原子認領（FOR UPDATE SKIP LOCKED）")
    conn = psycopg2.connect(CRM_DSN)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        cur.execute(
            f"CREATE TABLE {PROBE_TABLE} ("
            "  id bigserial primary key,"
            "  status text not null default 'pending',"
            "  processing_at timestamptz"
            ")"
        )
        cur.executemany(
            f"INSERT INTO {PROBE_TABLE} (status) VALUES ('pending')",
            [() for _ in range(TOTAL_TASKS)],
        )
    conn.close()

    claimed = []
    lock = threading.Lock()

    def worker():
        c = psycopg2.connect(CRM_DSN)
        c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        try:
            while True:
                with c.cursor() as cur:
                    cur.execute(
                        f"UPDATE {PROBE_TABLE} SET status='processing', processing_at=now() "
                        f"WHERE id IN ("
                        f"  SELECT id FROM {PROBE_TABLE} WHERE status='pending' "
                        f"  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT {BATCH}"
                        f") RETURNING id"
                    )
                    ids = [r[0] for r in cur.fetchall()]
                if not ids:
                    break
                with lock:
                    claimed.extend(ids)
                time.sleep(0.01)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0

    print(f"  送出 {TOTAL_TASKS} 筆，{WORKERS} 個 worker 併發認領，耗時 {dt:.2f}s")
    print(f"  實得 {len(claimed)} 筆，去重後 {len(set(claimed))} 筆")
    check(len(claimed) == TOTAL_TASKS, f"全部任務都被認領（{len(claimed)}/{TOTAL_TASKS}）")
    check(len(set(claimed)) == len(claimed), "沒有任何一筆被重複認領（無重派）")

    conn = psycopg2.connect(CRM_DSN)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {PROBE_TABLE} WHERE status='pending'")
        left = cur.fetchone()[0]
        check(left == 0, f"佇列已清空（剩 {left} 筆 pending）")
        cur.execute(f"DROP TABLE {PROBE_TABLE}")
    conn.close()


# ---------------------------------------------------------------- 測 B
def _listen_once(listen_dsn, notify_dsn, label):
    lc = psycopg2.connect(listen_dsn)
    lc.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    lcur = lc.cursor()
    lcur.execute("LISTEN probe_chan;")

    nc = psycopg2.connect(notify_dsn)
    nc.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    ncur = nc.cursor()
    ncur.execute("NOTIFY probe_chan, 'hello';")

    got = []
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if select.select([lc], [], [], 0.2) != ([], [], []):
            lc.poll()
            while lc.notifies:
                got.append(lc.notifies.pop(0).payload)
    lc.close()
    nc.close()
    print(f"  {label}：收到 {got}")
    return got


def test_listen_notify():
    print("\n[B] LISTEN/NOTIFY 跨 database")
    same = _listen_once(CRM_DSN, CRM_DSN, "同庫 crm_db → crm_db")
    check(same == ["hello"], "同庫能收到（機制本身可用）")

    # Connector 的庫
    fwd_dsn = _forwarder_dsn()
    if not fwd_dsn:
        print("  SKIP  找不到 platform_forwarder 連線字串，跳過跨庫測試")
        return
    cross = _listen_once(fwd_dsn, CRM_DSN, "跨庫 platform_forwarder ← crm_db")
    check(cross == [], "跨庫收不到（LISTEN 只在同一 database 生效）")


def _forwarder_dsn():
    env = Path(__file__).resolve().parents[1] / ".env"
    for p in (env, Path(__file__).resolve().parents[2] / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("DATABASE_URL=") and "platform_forwarder" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "postgresql://postgres:postgres@localhost:5432/platform_forwarder"


if __name__ == "__main__":
    print(f"CRM_DSN = {CRM_DSN}")
    test_atomic_claim()
    test_listen_notify()
    print("\n" + ("全部通過" if not failures else f"失敗 {len(failures)} 項：{failures}"))
    sys.exit(1 if failures else 0)
