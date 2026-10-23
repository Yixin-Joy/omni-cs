#!/usr/bin/env bash
# ============================================================
#  Multi-CS 伺服器端準備（Ubuntu 22.04 / Python 3.10）
#
#  這支只做「裝環境 + 建目錄 + 裝依賴」，不動資料、不起服務。
#  可重複執行（已完成的部分會跳過）。
#
#  用法（用 joyli 身分跑，需要 sudo 時會提示密碼）：
#      bash server_bootstrap.sh
#
#  刻意不做的事：
#    * 不建資料庫、不還原 dump  → 你可能有既有資料，交給 runbook 手動做
#    * 不寫 systemd / nginx     → 那些要對齊你的網域與路徑
#    * 不動 media1 / media2 內容 → 只 chown/chmod
#
#  訊息一律用英文：伺服器 locale 可能是 POSIX，中文會變亂碼。
# ============================================================
set -uo pipefail

HOME_DIR="${HOME:-/home/joyli}"
CODE_DIR="${HOME_DIR}/multi-cs"
M1="${HOME_DIR}/media1"
M2="${HOME_DIR}/media2"
PY=python3

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'
ok()   { printf "${GRN}[ OK ]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[WARN]${NC} %s\n" "$*"; }
bad()  { printf "${RED}[FAIL]${NC} %s\n" "$*"; }
hdr()  { printf "\n${YEL}==== %s ====%s\n" "$*" "$NC"; }

# ------------------------------------------------------------
hdr "0. Environment"
echo "user      : $(whoami)"
echo "home      : ${HOME_DIR}"
echo "python    : $(${PY} -V 2>&1)"
echo "disk /    : $(df -h / | awk 'NR==2{print $4" free of "$2}')"

if [ "$(id -u)" -eq 0 ]; then
  bad "Do NOT run this as root. Run as joyli; it will sudo when needed."
  exit 1
fi

# ------------------------------------------------------------
hdr "1. Network gate (Meta / DeepSeek reachability)"
# 這是最重要的一步：主機在中國大陸時，Meta Graph API 可能完全連不通，
# 那 webhook 收不到、訊息也發不出去 —— 等於整個專案不能用。
# 先測，再決定要不要繼續裝。
probe() {
  local name="$1" url="$2"
  local code
  code=$(curl -s -o /dev/null -m 12 -w '%{http_code}' "$url" 2>/dev/null || echo "000")
  if [ "$code" = "000" ]; then
    bad  "${name} unreachable (timeout/DNS) -> ${url}"
    return 1
  fi
  ok   "${name} reachable (HTTP ${code}) -> ${url}"
  return 0
}
NET_OK=1
probe "graph.facebook.com" "https://graph.facebook.com/v26.0/" || NET_OK=0
probe "api.deepseek.com"   "https://api.deepseek.com"        || NET_OK=0
probe "pypi mirror"        "https://mirrors.aliyun.com/pypi/simple/" || NET_OK=0

if [ "$NET_OK" -eq 0 ]; then
  cat <<'EOF'

  ------------------------------------------------------------
  STOP AND READ.
  At least one required endpoint is unreachable from this host.

  * graph.facebook.com unreachable:
      Meta webhook verification AND every outbound send will fail.
      The whole IG/FB/WhatsApp channel is dead. Options:
        a) move to a Hong Kong / overseas host  (recommended)
        b) route Graph API through an HTTP proxy (HTTPS_PROXY)
  * api.deepseek.com unreachable:
      AI replies will not work; only manual customer service remains.

  You can still finish the install, but the platform will not work
  until this is fixed.
  ------------------------------------------------------------
EOF
fi

# ------------------------------------------------------------
hdr "2. System packages"
# NOTE: postgresql is intentionally NOT installed here.
# Ubuntu 22.04 ships PG 14, but the dump produced on the Windows dev box is from
# PG 18 -> pg_restore 14 cannot read it ("unsupported version in file header").
# Install PG 18 from the PGDG repo instead; see docs/DEPLOY.md section 3.2.
#
# NOTE: build-essential / libpq-dev are NOT installed on purpose.
#   * every Python dependency has a manylinux wheel (onnxruntime, pillow, lxml)
#   * psycopg2-binary bundles libpq, so libpq-dev is unnecessary
# If pip ever falls back to compiling from source, add them then.
if ! dpkg -s python3-venv >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
  ok "python3-venv installed"
else
  ok "python3-venv already present (skip)"
fi
# nginx / certbot are expected to be pre-installed on this host; report only.
for p in nginx certbot rsync curl; do
  if command -v "$p" >/dev/null 2>&1; then ok "$p present ($(command -v $p))"; else warn "$p MISSING -- ask admin for: apt install $p"; fi
done
${PY} -V
if ! command -v psql >/dev/null 2>&1; then
  warn "psql not found yet -- install PostgreSQL 18 (docs/DEPLOY.md 3.2), do NOT use the PG 14 default"
else
  ok "psql: $(psql --version 2>/dev/null)"
fi

# ------------------------------------------------------------
hdr "3. Directories"
mkdir -p "${CODE_DIR}" "${CODE_DIR}/models" "${M1}" "${M2}"
for d in "${CODE_DIR}" "${M1}" "${M2}"; do
  ok "dir ${d} ($(stat -c '%U:%G %a' "$d" 2>/dev/null || echo '?'))"
done
# 服務以 joyli 身分跑，media 也屬於 joyli → 不需要改 owner，只確認可寫
for d in "${M1}" "${M2}"; do
  if [ -w "$d" ]; then ok "writable ${d}"; else bad "NOT writable ${d} (run: sudo chown -R $(whoami) $d)"; fi
done

# ------------------------------------------------------------
hdr "4. Python venv"
if [ ! -x "${CODE_DIR}/venv/bin/python" ]; then
  ${PY} -m venv "${CODE_DIR}/venv"
  ok "venv created -> ${CODE_DIR}/venv"
else
  ok "venv exists (reuse)"
fi
"${CODE_DIR}/venv/bin/pip" install -q -U pip setuptools wheel \
  -i https://mirrors.aliyun.com/pypi/simple/
ok "pip $(  "${CODE_DIR}/venv/bin/pip" -V | awk '{print $2}')"

hdr "5. Dependencies (slow: onnxruntime ~200MB, be patient)"
REQ="${CODE_DIR}/crm/requirements.txt"
if [ -f "$REQ" ]; then
  "${CODE_DIR}/venv/bin/pip" install \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r "$REQ" || { bad "pip install failed"; exit 1; }
  ok "dependencies installed"
else
  warn "missing ${REQ} -- copy the crm/ directory first, then re-run this script"
fi

# ------------------------------------------------------------
hdr "6. Sanity: import the app (no DB needed)"
if [ -d "${CODE_DIR}/crm/app" ]; then
  ( cd "${CODE_DIR}/crm" && PYTHONPATH="${CODE_DIR}/crm" \
    "${CODE_DIR}/venv/bin/python" -c \
    "from app.core import config; print('crm config OK, PORT =', config.PORT)" ) \
    && ok "crm imports" || bad "crm import failed"
  ( cd "${CODE_DIR}/connector" && PYTHONPATH="${CODE_DIR}/connector" \
    "${CODE_DIR}/venv/bin/python" -c \
    "from app.core import config; print('connector config OK, PORT =', config.PORT)" ) \
    && ok "connector imports" || bad "connector import failed"
else
  warn "crm/app not found yet -- copy code first"
fi

# ------------------------------------------------------------
hdr "7. onnxruntime check"
"${CODE_DIR}/venv/bin/python" -c \
  "import onnxruntime as o; print('onnxruntime', o.__version__, '| providers:', o.get_available_providers())" \
  2>/dev/null && ok "onnxruntime usable (CPU)" || bad "onnxruntime import failed"

# ------------------------------------------------------------
hdr "Next steps"
cat <<EOF
  1) copy media/data from your PC (see runbook section 3)
  2) restore both databases
  3) fill connector/.env and crm/.env
  4) create systemd units, then nginx
  5) run: python ${CODE_DIR}/scripts/check_media_setup.py --root ${CODE_DIR}
EOF
echo
