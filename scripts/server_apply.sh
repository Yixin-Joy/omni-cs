#!/usr/bin/env bash
# ============================================================
#  伺服器端sudo 主流程：裝 PostgreSQL 18 → 建角色/兩庫 → 還原 → 修 source_path
#
#  ★ 需要 sudo：
#        sudo bash ~/multi-cs/scripts/server_apply.sh
#
#  前置（不需要 sudo，先做好）：
#    ① 程式碼已在 ~/multi-cs（connector/ crm/ scripts/）
#    ② 兩個 dump 已在 ~/multi-cs（pf.dump crm.dump）
#    ③ 兩個 .env 已寫好（scripts/patch_env.py 跑過）
#
#  冪等性：
#    * 已裝 PG 18 → 跳過
#    * 角色已存在 → 只更新密碼
#    * 資料庫已存在 → 跳過
#    * 資料庫已有表 → **不還原**（除非 --force），避免蓋掉資料
#    * source_path 修復可重複執行（WHERE 條件會自然跳過已修的）
#
#  選項：
#    --force      即使資料庫已有表也強制還原（會先 drop 再建，危險）
#    --skip-pg    跳過 PG 安裝（已有 18 時想快一點）
# ============================================================
set -uo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'
ok()   { printf "${GRN}[ OK ]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[WARN]${NC} %s\n" "$*"; }
bad()  { printf "${RED}[FAIL]${NC} %s\n" "$*"; }
hdr()  { printf "\n\033[0;36m==== %s ====\033[0m\n" "$*"; }

# ★ sudo 之下 $HOME=/root、$SUDO_USER 可能是 admin（代跑情境）。
#   程式碼與 dump 都在 joyli 家目錄 → 一律以擁有者為準，可用環境變數覆蓋。
RUN_USER="${MULTICS_USER:-joyli}"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  bad "使用者 $RUN_USER 不存在（可用 MULTICS_USER=xxx 覆蓋）"; exit 1
fi
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
ROOT="${MULTICS_ROOT:-$RUN_HOME/multi-cs}"

PG_MIN=18
FORCE=0
SKIP_PG=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --skip-pg) SKIP_PG=1 ;;
    *) echo "未知參數: $a"; exit 2 ;;
  esac
done

# ------------------------------------------------------------ 0. 前置
# ★ 版本標記：跑起來第一行會印出它，用來確認「伺服器上是不是最新版」
SCRIPT_VERSION="2026-10-22.3-exclude-ext"

hdr "0. 前置檢查"
echo "       腳本版本：$SCRIPT_VERSION"
echo "       腳本路徑：$(readlink -f "$0")"
[ "$(id -u)" -eq 0 ] || { bad "需要 root：sudo bash $0"; exit 1; }
[ -d "$ROOT" ] || { bad "$ROOT 不存在"; exit 1; }

# 自檢：確認「剔除 EXTENSION」的邏輯真的在這份檔案裡。
# 舊版腳本沒有這段 → 直接擋下來，避免又跑到一半才失敗。
if ! grep -q "EXTENSION" "$(readlink -f "$0")"; then
  bad "這是舊版腳本（沒有 EXTENSION 剔除邏輯）"
  bad "請從 Windows 重新 scp："
  bad "  scp scripts\\server_apply.sh joyli@43.103.3.152:/home/joyli/multi-cs/scripts/"
  exit 1
fi
ok "腳本含 EXTENSION 剔除邏輯（版本 $SCRIPT_VERSION）"

for f in pf.dump crm.dump; do
  [ -f "$ROOT/$f" ] || { bad "缺 $ROOT/$f（先在 Windows 端 scp 上來）"; exit 1; }
  # 檔頭必須是 PGDMP
  head -c5 "$ROOT/$f" | grep -q PGDMP || { bad "$f 不是 pg_dump 格式（檔頭不是 PGDMP）"; exit 1; }
  ok "$f 存在且格式正確 ($(du -h "$ROOT/$f" | cut -f1))"
done
for f in connector/.env crm/.env; do
  [ -f "$ROOT/$f" ] || { bad "缺 $ROOT/$f（先跑 scripts/patch_env.py）"; exit 1; }
  ok "$f 存在"
done

# 從 .env 讀 DB 連線資訊（用 python3 解析，避免 shell 處理特殊字元出錯）
# ★ 密碼在 .env 裡是 **URL 編碼後** 的形式（patch_env.py 用 urllib quote 寫入），
#   這裡要 unquote 回原始密碼才能拿去 ALTER ROLE。
read -r DB_USER DB_PASS DB_NAMES < <(python3 - "$ROOT" <<'PY'
import sys, pathlib
from urllib.parse import unquote

root = pathlib.Path(sys.argv[1])
urls = []
for p in (root / "connector/.env", root / "crm/.env"):
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            urls.append(line.split("=", 1)[1].strip().strip('"').strip("'"))
            break
if len(urls) < 2:
    print("", "", ""); sys.exit(1)

def split_url(u: str):
    body = u.split("://", 1)[1]
    creds, hostpart = body.rsplit("@", 1)      # 取最後一個 @ → 密碼裡有 @ 也不怕
    user, _, pw = creds.partition(":")         # 第一個 : → 密碼裡有 : 也不怕
    db = hostpart.split("/", 1)[1] if "/" in hostpart else ""
    return user, unquote(pw), db

user, pw, db1 = split_url(urls[0])
_, _, db2 = split_url(urls[1])
print(user, pw, f"{db1},{db2}")
PY
)
[ -n "${DB_USER:-}" ] || { bad "無法從 .env 解析 DATABASE_URL（格式應為 postgresql://user:pass@host:port/db）"; exit 1; }
ok "DB 使用者 = $DB_USER，資料庫 = ${DB_NAMES:-?}（密碼已讀取，不顯示）"

# ------------------------------------------------------------ 1. PostgreSQL 18
hdr "1. PostgreSQL 18"
cur_ver=""
if command -v psql >/dev/null 2>&1; then
  cur_ver=$(psql -V 2>/dev/null | grep -oE '[0-9]+' | head -1)
fi

if [ "$SKIP_PG" -eq 1 ]; then
  warn "--skip-pg：跳過安裝（目前 psql=${cur_ver:-none}）"
elif [ -n "$cur_ver" ] && [ "$cur_ver" -ge "$PG_MIN" ]; then
  ok "已安裝 PostgreSQL $cur_ver，跳過"
else
  warn "目前 psql=${cur_ver:-none}，需要 $PG_MIN —— 開始安裝"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg >/dev/null 2>&1 || true
  curl -fsSL https://mirrors.aliyun.com/postgresql/repos/apt/ACCC4CF8.asc \
    | gpg --dearmor --yes -o /usr/share/keyrings/pgdg.gpg
  echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://mirrors.aliyun.com/postgresql/repos/apt jammy-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  if ! apt-get install -y -qq postgresql-18; then
    bad "postgresql-18 安裝失敗"; exit 1
  fi
  ok "postgresql-18 已安裝"
fi

# 若舊版叢集佔著 5432，先停掉
if command -v pg_lsclusters >/dev/null 2>&1; then
  while read -r ver name port status _; do
    [ -z "${ver:-}" ] && continue
    if [ "$ver" != "18" ] && [ "$status" = "online" ]; then
      warn "舊叢集 $ver/$name 在 port $port 上跑著 → 停用它（PG18 要用 5432）"
      pg_ctlcluster "$ver" "$name" stop || true
    fi
  done < <(pg_lsclusters -h 2>/dev/null | awk '{print $1, $2, $3, $4}')
fi

systemctl enable --now postgresql >/dev/null 2>&1 || true
sleep 2
pg_lsclusters 2>/dev/null | sed 's/^/       /'
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  bad "PostgreSQL 127.0.0.1:5432 沒有回應"
  systemctl status postgresql --no-pager 2>&1 | head -15
  exit 1
fi
ok "PostgreSQL 就緒（$(psql -V))"

# ------------------------------------------------------------ 2. 角色與資料庫
hdr "2. 角色與資料庫"
if [ "$(runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'")" = "1" ]; then
  ok "角色 $DB_USER 已存在 → 更新密碼"
else
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE \"$DB_USER\" LOGIN"
  ok "角色 $DB_USER 已建立"
fi
# 用 format(%I, %L) 安全引用角色名與密碼 —— 密碼含引號/反斜線也不會壞
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -v pw="$DB_PASS" -v usr="$DB_USER" >/dev/null <<'SQL'
SELECT format('ALTER ROLE %I PASSWORD %L', :'usr', :'pw') \gexec
SQL
ok "密碼已設定（長度 ${#DB_PASS}）"

IFS=',' read -ra DBS <<< "$DB_NAMES"
for db in "${DBS[@]}"; do
  [ -z "$db" ] && continue
  if [ "$(runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'")" = "1" ]; then
    ok "資料庫 $db 已存在"
  else
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$db\" OWNER \"$DB_USER\""
    ok "資料庫 $db 已建立（owner=$DB_USER）"
  fi
done

# ------------------------------------------------------------ 3. 還原
hdr "3. 還原"
restore_one() {
  local dump="$1" db="$2"
  local ntab
  ntab=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -tAc \
        "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo "?")
  if [ "$ntab" = "?" ]; then
    bad "無法連線 $db（密碼或 pg_hba 有問題）"
    return 1
  fi
  if [ "$ntab" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
    warn "$db 已有 $ntab 張表 → 跳過還原（要覆蓋請加 --force）"
    return 0
  fi
  if [ "$FORCE" -eq 1 ] && [ "$ntab" -gt 0 ]; then
    warn "--force：先清空 $db 的 public schema"
    PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -q -c \
      "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" || return 1
  fi
  echo "       還原 $dump → $db ..."

  # ★ 排除 EXTENSION：開發機的 crm_db 裝了 postgres_fdw（舊遷移腳本
  #   scripts/migrate_ai_data.py 留下的），但
  #     · 本專案**不需要任何 extension**（embedding 存 real[]、cosine 在 Python 算）
  #     · pg_foreign_server / pg_foreign_table 都是 0，程式也明寫「不碰 fdw」
  #     · CREATE EXTENSION 需要 superuser，joyli 沒有 → 會讓整個還原失敗
  #   所以從 TOC 裡剔掉，不裝。
  local toc="/tmp/_restore_${db}.toc"
  pg_restore -l "$dump" | grep -viE "EXTENSION" > "$toc" || true

  # 硬性斷言：過濾後不該再有 EXTENSION 字樣（含 COMMENT - EXTENSION）
  local left
  left=$(grep -ci "extension" "$toc" 2>/dev/null || true)
  if [ "${left:-0}" -gt 0 ]; then
    bad "TOC 過濾失敗：仍有 $left 行含 EXTENSION —— 中止（請回報這段輸出）"
    grep -in "extension" "$toc" | head -5 | sed 's/^/       /'
    return 1
  fi
  local total skipped
  total=$(pg_restore -l "$dump" | wc -l)
  skipped=$(( total - $(wc -l < "$toc") ))
  if [ "$skipped" -gt 0 ]; then
    ok "TOC 已剔除 $skipped 行（EXTENSION 相關，需 superuser；本專案不使用）"
    pg_restore -l "$dump" | grep -iE "extension" | sed 's/^/       /'
  fi
  echo "       TOC: $toc（$(wc -l < "$toc") 行）"

  if PGPASSWORD="$DB_PASS" pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$db" \
        --no-owner --no-privileges --single-transaction -L "$toc" "$dump" 2>/tmp/_restore_err.txt; then
    ok "$db 還原成功"
  else
    # owner / extension 之類的警告不算失敗，只有真的沒建出表才算
    local after
    after=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -tAc \
            "select count(*) from information_schema.tables where table_schema='public'")
    if [ "${after:-0}" -gt 0 ]; then
      warn "$db 有警告但表已建出（$after 張），視為成功。前 5 行警告："
      head -5 /tmp/_restore_err.txt | sed 's/^/       /'
    else
      # ★ 最後手段：改用 postgres 超級使用者還原，但物件仍歸屬 joyli（--role）。
      #   只有當一般還原真的失敗時才走這條，避免沒事就動用到 superuser。
      warn "$db 以 $DB_USER 還原失敗，改用 postgres 重試（--role=$DB_USER 保持物件歸屬）"
      head -5 /tmp/_restore_err.txt | sed 's/^/       /'
      if runuser -u postgres -- pg_restore -d "$db" --role="$DB_USER" \
            --no-privileges --single-transaction -L "$toc" "$dump" 2>/tmp/_restore_err2.txt; then
        ok "$db 還原成功（經 postgres 代跑，物件 owner=$DB_USER）"
      else
        bad "$db 還原失敗（兩種方式都失敗）"
        head -20 /tmp/_restore_err2.txt | sed 's/^/       /'
        return 1
      fi
    fi
  fi
}

restore_one "$ROOT/pf.dump"  platform_forwarder || exit 1
restore_one "$ROOT/crm.dump" crm_db             || exit 1

# ------------------------------------------------------------ 4. 時區
hdr "4. 時區（每日上限用 date_trunc('day', now())，UTC 會讓「今天」在早上 8 點才歸零）"
for db in platform_forwarder crm_db; do
  runuser -u postgres -- psql -q -c "ALTER DATABASE \"$db\" SET timezone='Asia/Taipei'"
  tz=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$db" -tAc "show timezone")
  ok "$db → $tz"
done

# ------------------------------------------------------------ 5. 修 source_path
hdr "5. 修 kb_document.source_path（Windows 絕對路徑）"
before=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -tAc \
         "select count(*) from kb_document where source_path like 'C:%'" 2>/dev/null || echo 0)
echo "       修復前：$before 筆是 C:\\ 開頭"
if [ "${before:-0}" -gt 0 ]; then
  # 兩個前綴都要處理（實測 18 筆走 media/、4 筆走 crm/media/）
  PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
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
  [ "$after" = "0" ] && ok "修復完成（剩 $after 筆）" || bad "仍有 $after 筆未修復"
else
  ok "不需要修（已是 Linux 路徑）"
fi

# 檔案是否真的存在
miss=0
while IFS= read -r p; do
  [ -z "$p" ] && continue
  [ -f "$p" ] || { echo "       MISSING: $p"; miss=$((miss+1)); }
done < <(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db -tAc \
         "select source_path from kb_document where source_path is not null")
if [ "$miss" -eq 0 ]; then ok "全部 source_path 都能落到實體檔"; else bad "$miss 筆檔案不存在（檢查 §3.3 的媒體合併）"; fi

# ------------------------------------------------------------ 6. 驗收
hdr "6. 驗收"
if [ -f "$ROOT/scripts/verify_restore.sql" ]; then
  echo "---- platform_forwarder ----"
  PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d platform_forwarder \
     -f "$ROOT/scripts/verify_restore.sql" 2>&1 | grep -E "NOTICE|rows|db " | sed 's/^/  /' | head -30
  echo "---- crm_db ----"
  PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d crm_db \
     -f "$ROOT/scripts/verify_restore.sql" 2>&1 | grep -E "NOTICE|rows|db " | sed 's/^/  /' | head -40
else
  warn "找不到 scripts/verify_restore.sql，跳過詳細核對"
fi

hdr "完成"
cat <<EOF
  下一步：
    1) 核對上面的行數（本機基準：pf forward_log=108 igfb_dm=176；
       crm kb_document=22 kb_chunk=102 向量 70/102）
    2) 裝依賴（不需 sudo）：
         cd $ROOT && python3 -m venv venv
         venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
    3) 建服務：sudo bash $ROOT/scripts/install_services.sh
    4) 改 nginx：sudo bash ~/nginx/apply_nginx.sh
EOF
echo
