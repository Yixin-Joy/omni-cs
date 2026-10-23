#!/usr/bin/env bash
# ============================================================
#  更新伺服器上的程式碼（在 Windows 的 Git Bash 執行）
#
#  用法（專案根目錄）：
#      bash scripts/deploy_update.sh              # 只同步程式碼（不重啟）
#      bash scripts/deploy_update.sh --restart    # 同步 + 重啟兩個服務 + 健康檢查
#      bash scripts/deploy_update.sh --dry-run    # 只打包，不傳不重啟
#
#  ★ 這支腳本做的事與不做事：
#      做：同步 connector / crm / scripts / docs 的檔案；備份；清 __pycache__；
#          殘留舊檔比對；requirements.txt 變更偵測
#      不做：pip install（只提示）、nginx 設定、systemd unit、資料庫 schema、媒體檔案
#      → 若這次改了 requirements.txt，跑完要自己裝依賴（腳本會提醒）
#
#  ★ 為什麼不用 scp -r：
#      scp 只會「覆蓋同名檔」，**不會刪除伺服器上已被刪掉的檔案**。
#      若你本地刪了某個 .py（或改了模組結構），伺服器上會留著舊檔，
#      Python import 時可能命中殘留 → 出現「明明刪了卻還在生效」的怪現象。
#      這支腳本會比對檔案清單並列出「伺服器上有、新包裡沒有」的檔案，讓你確認。
#
#  ★ 絕對不會碰的東西：
#      .env（含所有渠道憑證）、media/、models/、venv/、logs/
#
#  流程：
#      1) 打包 connector crm scripts docs（排除 .env / __pycache__ / *.pyc）
#      2) scp 上傳
#      3) 伺服器端：備份現有程式碼 → 解壓覆蓋 → 比對刪除清單 → 清 __pycache__
#      4) 重啟服務（可選）
# ============================================================
set -uo pipefail

HOST="${MULTICS_HOST:-joyli@43.103.3.152}"
REMOTE_DIR="${MULTICS_DIR:-/home/joyli/multi-cs}"
STAMP=$(date +%Y%m%d_%H%M%S)
PKG="multics_update_${STAMP}.tar.gz"
DRY=0
RESTART=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --restart) RESTART=1 ;;
    -h|--help)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知參數: $a（可用：--dry-run / --restart）"; exit 2 ;;
  esac
done

G='\033[0;32m'; Y='\033[0;33m'; R='\033[0;31m'; C='\033[0;36m'; N='\033[0m'
ok()   { printf "${G}[ OK ]${N} %s\n" "$*"; }
warn() { printf "${Y}[WARN]${N} %s\n" "$*"; }
bad()  { printf "${R}[FAIL]${N} %s\n" "$*"; }
hdr()  { printf "\n${C}==== %s ====${N}\n" "$*"; }

# ------------------------------------------------------------ 0. 前置
hdr "0. 前置檢查"
# ★ 自動切到專案根目錄：不管從哪裡呼叫（家目錄、C:\、雙擊…）都能正確找到
#   connector/ 與 crm/。原本要求「必須在專案根目錄執行」，實務上很容易踩
#   （2026-10-23 小jo 在 C:\Users\its-admin 執行 → No such file or directory）。
_SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
_PROJ=$(cd "$(dirname "$_SELF")/.." 2>/dev/null && pwd)
if [ -n "$_PROJ" ] && [ -d "$_PROJ/connector/app" ] && [ -d "$_PROJ/crm/app" ]; then
  cd "$_PROJ" || exit 1
  ok "專案目錄：$PWD"
else
  bad "找不到專案根目錄（connector/app 或 crm/app）"
  bad "  腳本位置：$_SELF"
  bad "  推測根目錄：${_PROJ:-（無法判定）}"
  exit 1
fi
[ -d connector/app ] || { bad "找不到 connector/app"; exit 1; }
[ -d crm/app ]      || { bad "找不到 crm/app"; exit 1; }

for f in connector/.env crm/.env; do
  [ -f "$f" ] || warn "本機沒有 $f（沒關係，只是不會更新它）"
done

# ------------------------------------------------------------ 1. 打包
hdr "1. 打包程式碼"
# ★ 排除規則：
#   .env              憑證，絕不覆蓋伺服器上的（可能已改成生產值）
#   __pycache__/*.pyc Windows 產物，Linux 用不到
#   .venv/venv        不該存在於源碼，但保險起見排除
#   media/ models/    資料，不在 connector/crm 內，這裡也排除以防萬一
if tar -czf "$PKG" \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --exclude='*/.env' \
    --exclude='.env' \
    --exclude='*/venv' \
    --exclude='*/media' \
    --exclude='*/models' \
    --exclude='*/logs' \
    --exclude='*.log' \
    --exclude='*.dump' \
    connector crm scripts docs 2>/dev/null; then
  ok "打包完成：$PKG（$(du -h "$PKG" | cut -f1)）"
else
  bad "打包失敗"; exit 1
fi

# 統計內容
NFILES=$(tar -tzf "$PKG" | grep -vc '/$' || true)
ok "內含 $NFILES 個檔案"

# 自我檢查：確保 .env 沒被打進去（這是最容易出錯的地方）
if tar -tzf "$PKG" | grep -qE '(^|/)\.env$'; then
  bad "封包裡竟然有 .env！中止（不該發生）"
  tar -tzf "$PKG" | grep -E '(^|/)\.env$'
  rm -f "$PKG"; exit 1
fi
ok ".env 未被打包（憑證安全）"

# 產生檔案清單，供伺服器端比對「該刪的舊檔」
tar -tzf "$PKG" | grep -v '/$' | sort > "/tmp/${PKG}.filelist"
ok "檔案清單：/tmp/${PKG}.filelist"

if [ "$DRY" -eq 1 ]; then
  hdr "dry-run：到此為止"
  echo "  封包：$PKG"
  echo "  清單：/tmp/${PKG}.filelist"
  echo "  要真的部署，去掉 --dry-run 重跑"
  exit 0
fi

# ------------------------------------------------------------ 2. 上傳
hdr "2. 上傳到伺服器"
if scp -q "$PKG" "/tmp/${PKG}.filelist" "${HOST}:/tmp/"; then
  ok "已上傳到 ${HOST}:/tmp/"
else
  bad "上傳失敗（檢查網路 / 密碼 / 磁碟空間）"; exit 1
fi

# ------------------------------------------------------------ 3. 伺服器端套用
hdr "3. 伺服器端套用"
ssh -t "$HOST" bash -s -- "$PKG" "$REMOTE_DIR" "$STAMP" "$RESTART" <<'REMOTE_SCRIPT'
set -uo pipefail
PKG="$1"; DEST="$2"; STAMP="$3"; RESTART="${4:-0}"
G='\033[0;32m'; Y='\033[0;33m'; R='\033[0;31m'; N='\033[0m'
ok()   { printf "${G}[ OK ]${N} %s\n" "$*"; }
warn() { printf "${Y}[WARN]${N} %s\n" "$*"; }
bad()  { printf "${R}[FAIL]${N} %s\n" "$*"; }

cd "$DEST" || { bad "進不去 $DEST"; exit 1; }
echo "       工作目錄：$(pwd)"
echo "       使用者　：$(whoami)"

# 3.1 檢查磁碟（備份要空間）
AVAIL=$(df -Pk . | awk 'NR==2{print $4}')
if [ "$AVAIL" -lt 51200 ]; then
  warn "可用空間只有 $((AVAIL/1024))MB，備份可能不夠"
fi

# 3.2 備份現有程式碼（★ 排除 venv/media/models，否則會複製幾百 MB）
echo "       ---- 備份現有程式碼 ----"
BK="../code_backup_${STAMP}.tar.gz"
if tar -czf "$BK" \
    --exclude='*/__pycache__' --exclude='*.pyc' \
    --exclude='*/venv' --exclude='venv' \
    --exclude='*/media' --exclude='*/models' --exclude='*/logs' \
    connector crm scripts 2>/dev/null; then
  ok "備份 → $(cd .. && pwd)/code_backup_${STAMP}.tar.gz ($(du -h "$BK" | cut -f1))"
else
  bad "備份失敗，中止（不冒險覆蓋）"; exit 1
fi

# 3.3 記錄「更新前」的檔案清單
find connector crm scripts docs -type f \
  -not -path '*/__pycache__/*' -not -name '*.pyc' 2>/dev/null | LC_ALL=C sort -u > /tmp/_old_files.txt

# 3.3b 記錄「更新前」的 requirements，解壓後比對 → 判斷要不要重裝依賴
#   ★ 這是本腳本唯一不自動做的事。漏了它，新增的套件會在你重啟後才炸
#     （ModuleNotFoundError），而且錯誤訊息指向 import 行，看不出是依賴沒裝。
REQ_BEFORE=""
if [ -f crm/requirements.txt ]; then
  REQ_BEFORE=$(sha256sum crm/requirements.txt | awk '{print $1}')
fi

# 3.4 解壓覆蓋
echo "       ---- 解壓覆蓋 ----"
if tar -xzf "/tmp/$PKG" -C "$DEST"; then
  ok "已解壓到 $DEST"
else
  bad "解壓失敗"; exit 1
fi

# 3.5 比對：伺服器上有、但新包裡沒有的檔案（= 本地已刪除）
NEWLIST="/tmp/${PKG%.tar.gz}.filelist"
if [ -f "$NEWLIST" ]; then
  # ★ 兩邊都用 LC_ALL=C 排序：不同 locale（C vs zh_TW.UTF-8）排序結果不同，
  #   comm 會因此漏比對或誤報。這行不能省。
  LC_ALL=C sort -u "$NEWLIST" > /tmp/_new_files.txt
  LC_ALL=C sort -u /tmp/_old_files.txt > /tmp/_old_sorted.txt
  # old - new = 應該被刪除的檔案
  LC_ALL=C comm -23 /tmp/_old_sorted.txt /tmp/_new_files.txt > /tmp/_to_delete.txt 2>/dev/null || true
  NDEL=$(wc -l < /tmp/_to_delete.txt)
  if [ "$NDEL" -gt 0 ]; then
    echo
    warn "有 $NDEL 個檔案在伺服器上存在、但新版本已移除："
    head -30 /tmp/_to_delete.txt | sed 's/^/         /'
    [ "$NDEL" -gt 30 ] && echo "         ...（共 $NDEL 個）"
    echo
    warn "這些是殘留舊檔，可能造成「明明刪了卻還在生效」。"
    warn "確認清單沒問題後，執行下面這行刪除它們："
    echo "         cd $DEST && xargs -a /tmp/_to_delete.txt rm -f && echo 已刪除"
    echo
    echo "      清單已存到：/tmp/_to_delete.txt"
  else
    ok "沒有需要刪除的殘留檔案"
  fi
else
  warn "找不到檔案清單 $NEWLIST，跳過殘留比對"
fi

# 3.6 清 __pycache__（★ 一定要做：Windows 的 .pyc 在 Linux 上無效且可能誤導）
echo "       ---- 清理 __pycache__ ----"
N=$(find connector crm -name __pycache__ -type d 2>/dev/null | wc -l)
find connector crm -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find connector crm -name '*.pyc' -delete 2>/dev/null || true
ok "清掉 $N 個 __pycache__ 目錄"

# 3.7 驗證關鍵檔案還在（防止解壓意外覆蓋掉 .env）
for f in connector/.env crm/.env; do
  if [ -f "$f" ]; then ok "$f 仍存在（未被覆蓋）"; else bad "$f 不見了！從備份還原"; fi
done

# 3.7b requirements.txt 是否變了 → 決定要不要重裝依賴
REQ_CHANGED=0
if [ -f crm/requirements.txt ] && [ -n "${REQ_BEFORE:-}" ]; then
  REQ_AFTER=$(sha256sum crm/requirements.txt | awk '{print $1}')
  if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
    REQ_CHANGED=1
    echo
    warn "★★ requirements.txt 有變更 —— 依賴可能沒裝，重啟會 ModuleNotFoundError"
    echo "      建議先裝依賴再重啟："
    echo "        cd $DEST && venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/"
    # 順便列出差異（只支援能讀到舊版的情況）
    echo "      變更摘要（vs 備份）："
    tar -xzOf "$BK" crm/requirements.txt 2>/dev/null > /tmp/_req_old.txt || true
    if [ -s /tmp/_req_old.txt ]; then
      diff /tmp/_req_old.txt crm/requirements.txt 2>/dev/null | grep -E "^[<>]" | head -15 | sed 's/^/         /' || true
    fi
    rm -f /tmp/_req_old.txt
  else
    ok "requirements.txt 無變更（不需重裝依賴）"
  fi
fi

# 3.8 語法快檢（編譯所有 .py，抓低級語法錯誤）
echo "       ---- 語法檢查 ----"
if [ -x venv/bin/python ]; then
  if venv/bin/python -m compileall -q connector/app crm/app >/tmp/_compile.txt 2>&1; then
    ok "全部 .py 語法通過"
  else
    bad "有語法錯誤："; head -20 /tmp/_compile.txt | sed 's/^/         /'
  fi
else
  warn "venv 不存在，跳過語法檢查"
fi

rm -f "/tmp/$PKG" "$NEWLIST" /tmp/_old_files.txt /tmp/_new_files.txt 2>/dev/null || true

echo
ok "程式碼更新完成"
echo "      備份位置：$(cd .. && pwd)/code_backup_${STAMP}.tar.gz"

# 3.9 重啟（只有 --restart 才做）
#   ★ 刻意不做預設行為：重啟會中斷 webhook 收訊與工作台幾秒，
#     且若 requirements 變了會啟動失敗 → 要讓人自己決定時機。
if [ "$RESTART" -eq 1 ]; then
  if [ "${REQ_CHANGED:-0}" -eq 1 ]; then
    bad "requirements.txt 有變更但依賴未安裝 —— 拒絕重啟（先 pip install）"
    echo "        cd $DEST && venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/"
    echo "        sudo systemctl restart multics-connector multics-crm"
  else
    echo "       ---- 重啟服務 ----"
    if systemctl restart multics-connector multics-crm; then
      sleep 4
      for s in multics-connector multics-crm; do
        st=$(systemctl is-active "$s")
        if [ "$st" = "active" ]; then ok "$s → active"; else bad "$s → $st"; fi
      done
      for p in 8010 8020; do
        body=$(curl -s -m 5 "http://127.0.0.1:$p/health" 2>/dev/null || echo "")
        if [ -n "$body" ]; then ok "127.0.0.1:$p → $body"; else bad "127.0.0.1:$p 沒回應"; fi
      done
      echo "       ---- 最近 log ----"
      tail -8 logs/connector.log 2>/dev/null | sed 's/^/         cc| /' || true
      tail -8 logs/crm.log       2>/dev/null | sed 's/^/         crm| /' || true
    else
      bad "重啟失敗，看 journalctl -u multics-crm -n 30 --no-pager"
    fi
  fi
else
  echo
  echo "      下一步（重啟讓新程式生效）："
  echo "        sudo systemctl restart multics-connector multics-crm"
  echo "        （或下次直接加 --restart 讓腳本代勞）"
fi
REMOTE_SCRIPT

RC=$?
if [ $RC -ne 0 ]; then
  bad "伺服器端腳本回傳 $RC"
else
  ok "伺服器端套用完成"
fi

# ------------------------------------------------------------ 4. 清理本地
rm -f "$PKG" "/tmp/${PKG}.filelist" 2>/dev/null || true

hdr "完成"
cat <<EOF
  接下來（在伺服器上）：
    sudo systemctl restart multics-connector multics-crm
    systemctl is-active multics-connector multics-crm
    curl -s http://127.0.0.1:8010/health
    curl -s http://127.0.0.1:8020/health

  看 log 確認沒有啟動錯誤：
    tail -50 ~/multi-cs/logs/crm.log
    tail -50 ~/multi-cs/logs/connector.log

  要回滾程式碼：
    cd ~ && ls code_backup_*.tar.gz
    tar -xzf code_backup_<時間>.tar.gz -C ~/multi-cs
    sudo systemctl restart multics-connector multics-crm
EOF
echo
