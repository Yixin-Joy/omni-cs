#!/usr/bin/env bash
# ============================================================
#  套用 nginx 反向代理設定（media1 → connector 8010 / media2 → crm 8020）
#
#  ★ 需要 sudo。若 joyli 沒有 sudo，請 admin 用他的帳號跑：
#        sudo bash apply_nginx.sh
#
#  安全設計：
#    ① 先備份原檔（帶時間戳，不會覆蓋舊備份）
#    ② 寫入後 `nginx -t`；**失敗自動回滾並退出**，nginx 不會被 reload
#    ③ 只有 -t 通過才 reload（reload 是平滑的，不斷線）
#    ④ 只碰 media1 / media2 兩個檔案
#       ⛔ 絕不動 default 與 voice.plexusai.app（後者是別人在用的活躍服務）
#
#  用法（把三個 .conf 跟這支腳本放同一個目錄）：
#        sudo bash apply_nginx.sh
#
#  反悔（手動回滾）：
#        sudo ls /etc/nginx/sites-available/*.bak.*     # 找備份
#        sudo cp <備份> <原檔> && sudo nginx -t && sudo systemctl reload nginx
# ============================================================
set -uo pipefail

NGX_AVAIL=/etc/nginx/sites-available
NGX_ENAB=/etc/nginx/sites-enabled
STAMP=$(date +%F_%H%M%S)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BACKUPS=()

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'
ok()   { printf "${GRN}[ OK ]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[WARN]${NC} %s\n" "$*"; }
bad()  { printf "${RED}[FAIL]${NC} %s\n" "$*"; }
hdr()  { printf "\n==== %s ====\n" "$*"; }

# ------------------------------------------------------------ 前置檢查
hdr "0. 前置檢查"
if [ "$(id -u)" -ne 0 ]; then
  bad "需要 root。請用: sudo bash $0"
  exit 1
fi
command -v nginx >/dev/null || { bad "nginx 未安裝"; exit 1; }

for f in media1.plexusai.app.conf media2.plexusai.app.conf; do
  if [ ! -f "${HERE}/${f}" ]; then
    bad "找不到 ${HERE}/${f}（要和這支腳本放同一目錄）"
    exit 1
  fi
done
ok "nginx $(nginx -v 2>&1 | awk -F/ '{print $2}')，來源檔案齊全"

# 這兩個絕對不能碰 —— 只做存在性提示，不寫入
for guard in default voice.plexusai.app; do
  if [ -e "${NGX_ENAB}/${guard}" ]; then
    warn "偵測到 ${guard}：不動它（本次只改 media1 / media2）"
  fi
done

# ------------------------------------------------------------ 備份
hdr "1. 備份原檔"
for d in media1 media2; do
  src="${NGX_AVAIL}/${d}.plexusai.app"
  if [ -f "$src" ]; then
    cp -a "$src" "${src}.bak.${STAMP}"
    BACKUPS+=("${src}|${src}.bak.${STAMP}")
    ok "已備份 ${src} → ${src}.bak.${STAMP}"
  else
    warn "${src} 不存在（會新建）"
  fi
done
# 記錄目前 nginx -t 的狀態（若本來就壞的，後面失敗不能怪我們）
if nginx -t >/tmp/_ngx_pre.txt 2>&1; then
  ok "套用前 nginx -t 通過（基準正常）"
else
  bad "套用前 nginx -t 就不通過 —— 中止，不要在有問題的基準上改設定"
  echo "---- 現有錯誤 ----"
  cat /tmp/_ngx_pre.txt
  echo "------------------"
  echo "先修好它（或問 admin 誰改壞的），再跑這支腳本。"
  exit 1
fi

# ------------------------------------------------------------ 寫入
hdr "2. 寫入新設定"
for d in media1 media2; do
  dst="${NGX_AVAIL}/${d}.plexusai.app"
  cp "${HERE}/${d}.plexusai.app.conf" "$dst"
  chown root:root "$dst"; chmod 644 "$dst"
  ok "已寫入 ${dst} ($(stat -c%s "$dst") bytes)"
  # sites-enabled 的 symlink 應該已存在；不存在才補
  if [ ! -e "${NGX_ENAB}/${d}.plexusai.app" ]; then
    ln -s "$dst" "${NGX_ENAB}/${d}.plexusai.app"
    ok "已建立 symlink ${NGX_ENAB}/${d}.plexusai.app"
  else
    ok "symlink 已存在（沿用）"
  fi
done

# ------------------------------------------------------------ 驗證
hdr "3. nginx -t"
if nginx -t 2>&1 | tee /tmp/_ngx_post.txt; then
  ok "語法檢查通過"
else
  bad "語法檢查失敗 —— 自動回滾"
  for pair in "${BACKUPS[@]:-}"; do
    [ -z "$pair" ] && continue
    orig="${pair%%|*}"; bak="${pair##*|}"
    cp -a "$bak" "$orig" && warn "已還原 ${orig}"
  done
  echo
  bad "nginx 未 reload，線上服務不受影響。錯誤詳情："
  cat /tmp/_ngx_post.txt
  exit 1
fi

# ------------------------------------------------------------ reload
hdr "4. reload nginx（平滑，不斷線）"
if systemctl reload nginx; then
  ok "nginx 已 reload"
else
  bad "reload 失敗，嘗試 rollback"
  for pair in "${BACKUPS[@]:-}"; do
    [ -z "$pair" ] && continue
    orig="${pair%%|*}"; bak="${pair##*|}"
    cp -a "$bak" "$orig"
  done
  systemctl reload nginx || systemctl restart nginx
  exit 1
fi

# ------------------------------------------------------------ 驗證結果
hdr "5. 結果驗證"
sleep 1
for h in media1 media2; do
  # / 應該不再是「mediaN home」靜態頁；服務沒起時 502，起了之後 media1=404 / media2=200
  code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://${h}.plexusai.app/" 2>/dev/null || echo "000")
  printf "  https://%s/            -> %s\n" "$h" "$code"
  code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://${h}.plexusai.app/internal/api/x" 2>/dev/null || echo "000")
  printf "  https://%s/internal/api/x -> %s  (期望 403)\n" "$h" "$code"
done
# voice 必須完全沒變
vcode=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://voice.plexusai.app/" 2>/dev/null || echo "000")
vsize=$(curl -s -m 8 "https://voice.plexusai.app/" 2>/dev/null | wc -c)
printf "  https://voice.plexusai.app/ -> %s (%s bytes)  (期望 200 / 21610 bytes)\n" "$vcode" "$vsize"

cat <<EOF

判讀：
  * 501/502/504 = 設定生效了，但後端服務（8010/8020）還沒起 → 正常，繼續部署
  * media1 / 回 404 = 正確（這個域只服務 webhook 與 /files）
  * media2 / 回 502 = 正確（crm 還沒起）；起了之後應該是 200 或 302
  * /internal/ 回 403 = 正確（內網 API 已封死）
  * voice 若 200 且 21610 bytes = 別人的服務沒被影響

回滾指令（若要反悔）：
$(for pair in "${BACKUPS[@]:-}"; do [ -z "$pair" ] && continue; orig="${pair%%|*}"; bak="${pair##*|}"; echo "  cp -a $bak $orig"; done)
  nginx -t && systemctl reload nginx
EOF
