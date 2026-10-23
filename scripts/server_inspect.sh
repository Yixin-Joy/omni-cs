#!/usr/bin/env bash
# ============================================================
#  伺服器現況盤點（只讀，不改任何東西）
#
#  為什麼要這支：media1.plexusai.app / media2.plexusai.app 已經配好了
#  （nginx 1.18 + Let's Encrypt 憑證 + 佔位頁），但**不知道是誰配的、
#  配成什麼樣**。直接照 runbook 覆蓋 nginx 可能撞掉別人正在用的設定。
#
#  用法（用 joyli 身分，需要 sudo 的項目會提示）：
#      bash ~/server_inspect.sh
#
#  輸出全部是「事實 + 判讀」，貼回去就能決定下一步。
# ============================================================
set -uo pipefail

hdr() { printf "\n\033[0;36m===== %s =====\033[0m\n" "$*"; }
sub() { printf "\n-- %s --\n" "$*"; }

hdr "0. 身分與系統"
whoami; id
lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY
uname -r
echo "uptime: $(uptime -p 2>/dev/null)"

hdr "1. 磁碟與記憶體"
df -h / | awk 'NR==1||NR==2'
echo
free -h
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv 2>/dev/null || echo "nvidia-smi 存在但執行失敗"
else
  echo "nvidia-smi: 不存在（這台沒有 NVIDIA 驅動/GPU）"
fi

hdr "2. 家目錄現況"
sub "~/media1 與 ~/media2"
ls -ld ~/media1 ~/media2 2>/dev/null
echo "media1 內容：$(ls -A ~/media1 2>/dev/null | wc -l) 項"
ls -A ~/media1 2>/dev/null | head -10
echo "media2 內容：$(ls -A ~/media2 2>/dev/null | wc -l) 項"
ls -A ~/media2 2>/dev/null | head -10
sub "是否為掛載點"
findmnt -T ~/media1 2>/dev/null || echo "findmnt 無輸出"
findmnt -T ~/media2 2>/dev/null || echo "findmnt 無輸出"
sub "~/multi-cs 是否已存在"
ls -la ~/multi-cs 2>/dev/null || echo "（不存在）"

hdr "3. 已安裝的關鍵套件"
for p in nginx certbot postgresql postgresql-14 postgresql-18 python3 python3-venv rsync; do
  v=$(dpkg-query -W -f='${Version}' "$p" 2>/dev/null)
  if [ -n "$v" ]; then printf "  %-16s %s\n" "$p" "$v"; else printf "  %-16s (not installed)\n" "$p"; fi
done
echo "python3: $(python3 -V 2>&1)"
command -v psql >/dev/null && echo "psql: $(psql --version)" || echo "psql: 不在 PATH"
command -v pg_restore >/dev/null && echo "pg_restore: $(pg_restore --version)" || echo "pg_restore: 不在 PATH"

hdr "4. nginx 現況（★ 最重要）"
sub "sites-enabled"
ls -la /etc/nginx/sites-enabled/ 2>/dev/null
sub "全部 server_name / listen / root / proxy_pass"
# 只抓關鍵指令，避免整份 config 太長
sudo grep -rnE '^\s*(server_name|listen|root|alias|proxy_pass|include)' \
     /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | grep -v '^\s*#'
sub "nginx -T 摘要"
sudo nginx -t 2>&1 | head -5
sub "正在監聽的埠"
sudo ss -lntp 2>/dev/null | grep -E ':(80|443|8010|8020|5432|3000|8000|8080)\b' || ss -lnt | grep -E ':(80|443|8010|8020|5432)\b'

hdr "5. TLS 憑證"
sub "certbot 已簽發的憑證"
sudo certbot certificates 2>/dev/null | head -30 || echo "certbot 未安裝或無憑證"
sub "續期 timer 是否啟用（★ 憑證 2027-01-20 到期，沒設自動續期就會斷）"
systemctl list-timers 'certbot*' --no-pager 2>/dev/null | head -5
systemctl is-enabled certbot.timer 2>/dev/null || echo "certbot.timer: not enabled"

hdr "6. PostgreSQL"
sub "叢集與版本"
pg_lsclusters 2>/dev/null || echo "pg_lsclusters 無輸出"
sub "服務狀態"
systemctl is-active postgresql 2>/dev/null || true
sub "現有資料庫（需要權限）"
sudo -u postgres psql -tAc "select datname, pg_size_pretty(pg_database_size(datname)) from pg_database where datistemplate=false order by 1" 2>/dev/null || echo "（查不到，可能 PG 沒跑）"
sub "時區設定"
sudo -u postgres psql -tAc "show timezone" 2>/dev/null || true

hdr "7. 已存在的服務（避免埠衝突）"
systemctl list-units --type=service --state=running --no-pager 2>/dev/null | grep -vE 'systemd|dbus|ssh|cron|rsyslog|polkit|udev|network|getty|snapd|packagekit|unattended|chrony|aliyun' | head -20

hdr "8. 結論提示"
cat <<'EOF'
  把上面輸出貼回去。判斷重點：
  * §4 若已有 media1/media2 兩個 server 區塊 → 只需要改其中的 root / proxy_pass，
    不要整份覆蓋（可能有別人的設定）
  * §5 若 certbot.timer 沒啟用 → 憑證 2027-01-20 到期後 webhook 會全掛
  * §6 若已是 PG 18 → 直接用；若只有 14 → 需要裝 18（見 docs/DEPLOY.md §3.2）
  * §7 若 8010 / 8020 已被佔用 → 要改埠
EOF
echo
