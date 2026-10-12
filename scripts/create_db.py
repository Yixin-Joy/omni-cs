"""建資料庫 platform_forwarder 與所有資料表（含 AI 自動回復相關表）。

可重複執行，不會清掉既有資料。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 專案根

import psycopg2

HOST = "localhost"
PORT = 5432
USER = "postgres"
PASSWORD = "postgres"
ADMIN_DB = "postgres"  # 用現有庫連上去，執行 CREATE DATABASE
NEW_DB = "platform_forwarder"


def connect(dbname):
    return psycopg2.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=dbname)


lines = []

# 1) 建庫（若不存在）
conn = connect(ADMIN_DB)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (NEW_DB,))
if cur.fetchone():
    lines.append(f"database {NEW_DB} already exists")
else:
    cur.execute(f'CREATE DATABASE "{NEW_DB}"')
    lines.append(f"created database {NEW_DB}")
cur.close()
conn.close()

# 2) 建表 + 索引
conn = connect(NEW_DB)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS forward_log (
    id                 BIGSERIAL PRIMARY KEY,
    platform           VARCHAR(20) NOT NULL,
    message_id         VARCHAR(128),
    sender_id          VARCHAR(128),
    conversation_id    VARCHAR(64),
    message_type       VARCHAR(20),
    raw_content        TEXT,
    forwarded_payload  JSONB,
    forward_status     VARCHAR(20) NOT NULL DEFAULT 'success',
    target_http_status INTEGER,
    target_response    TEXT,
    error_message      TEXT,
    message_time       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    direction          VARCHAR(20) NOT NULL DEFAULT 'inbound',
    is_handled         BOOLEAN NOT NULL DEFAULT false
);
""")
cur.execute("CREATE INDEX IF NOT EXISTS idx_forward_log_platform ON forward_log(platform);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_forward_log_created ON forward_log(created_at);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_forward_log_sender ON forward_log(sender_id);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_forward_log_direction ON forward_log(direction);")
conn.commit()
lines.append("table forward_log ready")

# ---------- IG/FB 收件匣（私訊 + 貼文留言） ----------
cur.execute("""
CREATE TABLE IF NOT EXISTS igfb_dm (
    id               BIGSERIAL PRIMARY KEY,
    platform         VARCHAR(10) NOT NULL,
    conversation_key TEXT NOT NULL,
    thread_id        TEXT,
    sender_id        TEXT,
    sender_name      VARCHAR(255),
    direction        VARCHAR(10) NOT NULL,
    message_type     VARCHAR(20),
    text             TEXT,
    attachments      JSONB,
    message_id       TEXT,
    created_at       TIMESTAMPTZ,
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_handled       BOOLEAN NOT NULL DEFAULT false
);
""")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_igfb_dm_msg ON igfb_dm(platform, message_id);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_igfb_dm_conv ON igfb_dm(platform, conversation_key, created_at);")

cur.execute("""
CREATE TABLE IF NOT EXISTS igfb_comment (
    id          BIGSERIAL PRIMARY KEY,
    platform    VARCHAR(10) NOT NULL,
    post_id     TEXT NOT NULL,
    comment_id  TEXT NOT NULL,
    parent_id   TEXT,
    from_id     TEXT,
    from_name   VARCHAR(255),
    text        TEXT,
    like_count  INTEGER DEFAULT 0,
    replied     BOOLEAN NOT NULL DEFAULT false,
    is_handled  BOOLEAN NOT NULL DEFAULT false,
    last_activity_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_igfb_comment_id ON igfb_comment(platform, comment_id);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_igfb_comment_post ON igfb_comment(platform, post_id, created_at);")

cur.execute("""
CREATE TABLE IF NOT EXISTS igfb_post (
    id             BIGSERIAL PRIMARY KEY,
    platform       VARCHAR(10) NOT NULL,
    post_id        TEXT NOT NULL,
    name           TEXT,
    media_type     VARCHAR(20),
    media_url      TEXT,
    permalink      TEXT,
    like_count     INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    created_at     TIMESTAMPTZ,
    inserted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
""")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_igfb_post_id ON igfb_post(platform, post_id);")
conn.commit()
lines.append("tables igfb_dm / igfb_comment / igfb_post ready")

# 4) AI 自動回復相關表（定義集中在 autoreply_db，避免兩份 DDL 走鐘）
cur.close()
conn.close()

from app.autoreply import autoreply_db  # noqa: E402  (放在建表之後，import 時不連線)

autoreply_db.init_schema()
lines.append("tables autoreply_faq / autoreply_business_table / autoreply_business_row "
             "/ autoreply_setting / autoreply_log ready（含 forward_log、igfb_dm、"
             "igfb_comment 的自動回復欄位）")

# 5) 驗證表結構
conn = connect(NEW_DB)
cur = conn.cursor()
cur.execute("""
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'forward_log'
ORDER BY ordinal_position;
""")
for name, dtype, nullable in cur.fetchall():
    lines.append(f"  {name:20s} {dtype:20s} nullable={nullable}")
cur.close()
conn.close()

lines.append("DONE")
print("\n".join(lines))
