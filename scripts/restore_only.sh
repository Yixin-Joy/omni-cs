#!/usr/bin/env bash
# ============================================================
#  只重做「還原」這一步（server_apply.sh 的 3~5 段）
#
#  用途：server_apply.sh 前面的 PG 安裝、建角色/建庫都成功了，
#        只有 pg_restore 失敗時，不必整支重跑，用這支補即可。
#
#  ★ 需要 sudo：
#        sudo bash ~/multi-cs/scripts/restore_only.sh
#
#  它做的事：
#    ① 兩個 dump 從 TOC 剔除 EXTENSION（postgres_fdw 需 superuser，本專案不用）
#    ② 還原到 platform_forwarder / crm_db
#    ③ 設時區、修 source_path、跑核對
#
#  安全：目標庫必須是空的（0 張表）才還原；已有表就跳過並提示用 --force。
# ============================================================
set -uo pipefail

SCRIPT_VERSION="2026-10-22.3-restore-only"

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'
ok()   { printf "${GRN}[ OK ]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[WARN]${NC} %s\n" "$*"; }
bad()  { printf "${RED}[FAIL]${NC} %s\n" "$*"; }
hdr()  { printf "\n\033[0;36m==== %s ====\033[0m\n" "$*"; }

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

RUN_USER="${MULTICS_USER:-joyli}"
id "$RUN_USER" >/dev/null 2>&1 || { bad "使用者 $RUN_USER 不存在"; exit 1; }
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
ROOT="${MULTICS_ROOT:-$RUN_HOME/multi-cs}"

hdr "0. 前置"
echo "       腳本版本：$SCRIPT_VERSION"
[ "$(id -u)" -eq 0 ] || { bad "需要 root：sudo bash $0"; exit 1; }
ok "root"

# 讀 .env 的連線資訊
read -r DB_USER DB_PASS DB_NAMES < <(python3 - "$ROOT" <<'PY'
import sys, pathlib
from urllib.parse import unquote
root = pathlib.Path(sys.argv[1])
urls = []
for p in (root / "connector/.env", root / "crm/.env"):
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            urls.append(line.split("=", 1)[1].strip().strip('"').strip("'")); break
if len(urls) < 2:
    print("", "", ""); sys.exit(1)
def split_url(u):
    body = u.split("://", 1)[1]
    creds, hostpart = body.rsplit("@", 1)
    user, _, pw = creds.partition(":")
    db = hostpart.split("/", 1)[1] if "/" in hostpart else ""
    return user, unquote(pw), db
user, pw, db1 = split_url(urls[0])
_, _, db2 = split_url(urls[1])
print(user, pw, f"{db1},{db2}")
PY
)
[ -n "${DB_USER:-}" ] || { bad "無法解析 .env 的 DATABASE_URL"; exit 1; }
ok "DB 使用者=$DB_USER，資料庫=$DB_NAMES"

for f in pf.dump crm.dump; do
  [ -f "$ROOT/$f" ] || { bad "缺 $ROOT/$f"; exit 1; }
  head -c5 "$ROOT/$f" | grep -q PGDMP || { bad "$f 格式不對"; exit 1; }
  ok "$f ($(du -h "$ROOT/$f" | cut -f1))"
done

restore_one() {
  local dump="$1" db="$2"
  local ntab
  ntab=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -tAc \
        "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo "?")
  [ "$ntab" = "?" ] && { bad "連不上 $db（檢查 DATABASE_URL 或 pg_hba）"; return 1; }

  if [ "$ntab" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
    warn "$db 已有 $ntab 張表 → 跳過（要覆蓋請加 --force）"
    return 0
  fi
  if [ "$ntab" -gt 0 ] && [ "$FORCE" -eq 1 ]; then
    warn "--force：清空 $db 的 public schema"
    PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -q -c \
      "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" || return 1
  fi

  # ---- TOC 過濾：剔除所有含 EXTENSION 的行（含 COMMENT - EXTENSION）----
  local toc="/tmp/_toc_${db}.txt"
  pg_restore -l "$dump" | grep -viE "EXTENSION" > "$toc" || true
  local total kept dropped
  total=$(pg_restore -l "$dump" | wc -l)
  kept=$(wc -l < "$toc")
  dropped=$(( total - kept ))
  if grep -qi "extension" "$toc"; then
    bad "TOC 過濾失敗（仍有 extension 行）"; grep -in extension "$toc" | head -5; return 1
  fi
  if [ "$dropped" -gt 0 ]; then
    ok "TOC 剔除 $dropped 行（EXTENSION 相關，需 superuser，本專案不使用）"
    pg_restore -l "$dump" | grep -iE "extension" | sed 's/^/       /'
  else
    ok "TOC 無 extension 項目（$kept 行）"
  fi

  echo "       還原 $db ..."
  if PGPASSWORD="$DB_PASS" pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$db" \
        --no-owner --no-privileges --single-transaction -L "$toc" "$dump" \
        2>/tmp/_re1.txt; then
    ok "$db 還原成功"
  else
    local after
    after=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -tAc \
            "select count(*) from information_schema.tables where table_schema='public'")
    if [ "${after:-0}" -gt 0 ]; then
      warn "$db 有警告但表已建出（$after 張），視為成功"
      head -3 /tmp/_re1.txt | sed 's/^/       /'
    else
      warn "$db 以 $DB_USER 失敗，改用 postgres 重試（--role=$DB_USER 保持 owner）"
      head -5 /tmp/_re1.txt | sed 's/^/       /'
      if runuser -u postgres -- pg_restore -d "$db" --role="$DB_USER" \
            --no-privileges --single-transaction -L "$toc" "$dump" 2>/tmp/_re2.txt; then
        ok "$db 還原成功（postgres 代跑，owner=$DB_USER）"
      else
        bad "$db 兩種方式都失敗"; head -20 /tmp/_re2.txt | sed 's/^/       /'; return 1
      fi
    fi
  fi
}

hdr "1. 還原"
restore_one "$ROOT/pf.dump"  platform_forwarder || exit 1
restore_one "$ROOT/crm.dump" crm_db             || exit 1

hdr "2. 時區"
for db in platform_forwarder crm_db; do
  runuser -u postgres -- psql -q -c "ALTER DATABASE \"$db\" SET timezone='Asia/Taipei'"
  ok "$db → $(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -tAc 'show timezone')"
done

hdr "3. 修 kb_document.source_path"
before=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -tAc \
         "select count(*) from kb_document where source_path like 'C:%'" 2>/dev/null || echo 0)
echo "       修復前 $before 筆"
if [ "${before:-0}" -gt 0 ]; then
  PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -q -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
UPDATE kb_document
   SET source_path =
       regexp_replace(
         regexp_replace(replace(source_path,'\','/'),
           '^C:/Users/its-admin/Desktop/Multi-CS/crm/media', '/home/joyli/media2'),
         '^C:/Users/its-admin/Desktop/Multi-CS/media',       '/home/joyli/media2')
 WHERE source_path LIKE 'C:%';
SQL
  after=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -tAc \
          "select count(*) from kb_document where source_path like 'C:%'")
  [ "$after" = "0" ] && ok "修復完成（剩 0 筆）" || bad "仍有 $after 筆"
else
  ok "不需要修"
fi

miss=0
while IFS= read -r p; do
  [ -z "$p" ] && continue
  [ -f "$p" ] || { echo "       MISSING: $p"; miss=$((miss+1)); }
done < <(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -tAc \
         "select source_path from kb_document where source_path is not null")
[ "$miss" -eq 0 ] && ok "全部 source_path 都落到實體檔" || warn "$miss 筆檔案不存在（檢查媒體是否傳齊）"

hdr "4. 核對（本機基準：forward_log=108  business=2  kb_chunk=102）"
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d platform_forwarder -t -A -F' | ' -c \
  "select 'forward_log',count(*) from forward_log union all select 'igfb_dm',count(*) from igfb_dm union all select 'igfb_comment',count(*) from igfb_comment union all select 'acct_map',count(*) from connector_account_map;" | sed 's/^/  pf : /'
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -t -A -F' | ' -c \
  "select 'crm_business',count(*) from crm_business union all select 'accounts',count(*) from crm_business_platform_account union all select 'kb_document',count(*) from kb_document union all select 'kb_chunk',count(*) from kb_chunk union all select 'kb_asset',count(*) from kb_asset union all select 'autoreply_log',count(*) from autoreply_log;" | sed 's/^/  crm: /'
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -t -A -c \
  "select count(*) from pg_extension where extname<>'plpgsql';" | xargs -I{} echo "  非內建 extension 數：{}（期望 0）"

hdr "完成"
echo "  下一步：sudo bash $ROOT/scripts/install_services.sh"
echo
