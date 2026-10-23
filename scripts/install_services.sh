#!/usr/bin/env bash
# ============================================================
#  建立並啟動兩個 systemd 服務（connector 8010 / crm 8020）
#
#  ★ 需要 sudo：
#        sudo bash ~/multi-cs/scripts/install_services.sh
#
#  前置：
#    ① 程式碼在 ~/multi-cs/{connector,crm}
#    ② venv 已建好、依賴已裝（~/multi-cs/venv/bin/uvicorn 存在）
#    ③ 兩個 .env 已寫好
#    ④ 資料庫已還原（服務啟動時會跑 DDL，但沒有庫會起不來）
#
#  設計重點：
#    * User=joyli，log 寫 ~/multi-cs/logs/（**不能放 /var/log**：屬 root，
#      append: 打不開 → systemd 報 status=209/STDOUT，錯誤訊息極難懂）
#    * --workers 1（服務內含 6 條背景迴圈，多 worker 會重複跑）
#    * --host 127.0.0.1（不對外，外部走 nginx）
# ============================================================
set -uo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'
ok()   { printf "${GRN}[ OK ]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[WARN]${NC} %s\n" "$*"; }
bad()  { printf "${RED}[FAIL]${NC} %s\n" "$*"; }
hdr()  { printf "\n\033[0;36m==== %s ====\033[0m\n" "$*"; }

# ★ sudo 之下 $HOME=/root、$SUDO_USER 可能是 admin（代跑情境）。
#   程式碼與 venv 都在 joyli 家目錄 → 一律以擁有者為準，可用環境變數覆蓋。
RUN_USER="${MULTICS_USER:-joyli}"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  bad "使用者 $RUN_USER 不存在（可用 MULTICS_USER=xxx 覆蓋）"; exit 1
fi
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
ROOT="${MULTICS_ROOT:-$RUN_HOME/multi-cs}"
VENV="$ROOT/venv"

hdr "0. 前置檢查"
[ "$(id -u)" -eq 0 ] || { bad "需要 root：sudo bash $0"; exit 1; }
[ -x "$VENV/bin/uvicorn" ] || { bad "$VENV/bin/uvicorn 不存在 —— 先建 venv 並裝依賴"; exit 1; }
ok "uvicorn: $VENV/bin/uvicorn"
for d in connector crm; do
  [ -f "$ROOT/$d/main.py" ] || { bad "缺 $ROOT/$d/main.py"; exit 1; }
  [ -f "$ROOT/$d/.env" ]    || { bad "缺 $ROOT/$d/.env"; exit 1; }
  ok "$d 就緒"
done

# 埠有沒有被佔
for p in 8010 8020; do
  if ss -lnt 2>/dev/null | grep -q ":$p "; then
    warn "埠 $p 已有程式在聽 —— 若不是本服務，先停掉它"
  else
    ok "埠 $p 空閒"
  fi
done

hdr "1. log 目錄"
mkdir -p "$ROOT/logs"
touch "$ROOT/logs/connector.log" "$ROOT/logs/crm.log"
chown -R "$RUN_USER":"$RUN_USER" "$ROOT/logs"
ok "$ROOT/logs（owner=$RUN_USER）"

hdr "2. 寫入 unit 檔"
write_unit() {
  local name="$1" dir="$2" port="$3" desc="$4"
  cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=${desc}
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${ROOT}/${dir}
# .env 由 app/core/config.py 自己載入（用檔案自身位置找），這裡不重複宣告
Environment=PYTHONPATH=${ROOT}/${dir}
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
# ★ workers 必須是 1：服務內含背景迴圈，多 worker 會讓每條迴圈重複執行
ExecStart=${VENV}/bin/uvicorn main:app --host 127.0.0.1 --port ${port} --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
StandardOutput=append:${ROOT}/logs/${dir}.log
StandardError=append:${ROOT}/logs/${dir}.log

[Install]
WantedBy=multi-user.target
EOF
  ok "/etc/systemd/system/${name}.service"
}
write_unit multics-connector connector 8010 "Multi-CS Connector (8010)"
write_unit multics-crm       crm       8020 "Multi-CS CRM (8020)"

hdr "3. 啟動"
systemctl daemon-reload
systemctl enable --now multics-connector multics-crm >/dev/null 2>&1
sleep 4

hdr "4. 狀態"
for s in multics-connector multics-crm; do
  st=$(systemctl is-active "$s")
  if [ "$st" = "active" ]; then ok "$s → active"; else bad "$s → $st"; fi
done

hdr "5. 健康檢查"
for p in 8010 8020; do
  body=$(curl -s -m 5 "http://127.0.0.1:$p/health" 2>/dev/null || echo "")
  if [ -n "$body" ]; then ok "127.0.0.1:$p → $body"; else bad "127.0.0.1:$p 沒有回應"; fi
done

hdr "6. 關鍵檢查：每個服務只該啟動一次 worker"
for pair in "multics-connector:Connector worker 啟動" "multics-crm:AI 佇列消費者啟動"; do
  svc="${pair%%:*}"; pat="${pair##*:}"
  n=$(grep -c "$pat" "$ROOT/logs/${svc#multics-}.log" 2>/dev/null || echo 0)
  if [ "$n" -eq 1 ]; then ok "$svc：『$pat』出現 1 次（正確）"
  elif [ "$n" -eq 0 ]; then warn "$svc：還沒看到『$pat』（可能還在啟動中，稍等再看 log）"
  else bad "$svc：『$pat』出現 $n 次 —— workers 設錯了！"; fi
done

hdr "7. 失敗時的排查"
cat <<EOF
  看 log：
    tail -50 $ROOT/logs/connector.log
    tail -50 $ROOT/logs/crm.log
    journalctl -u multics-connector -n 50 --no-pager
    journalctl -u multics-crm       -n 50 --no-pager

  常見原因：
    * ModuleNotFoundError  → venv 依賴沒裝齊（重跑 pip install）
    * 連不上資料庫         → .env 的 DATABASE_URL 或密碼不對
    * status=209/STDOUT    → log 目錄權限（本腳本已避開，若出現請檢查 owner）
    * 模型載入失敗         → RAG_MODEL_DIR 指到不存在的路徑

  停用（要重來時）：
    sudo systemctl disable --now multics-connector multics-crm
EOF
echo
