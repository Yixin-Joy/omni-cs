# Multi-CS 部署 Runbook（connector 8010 + crm 8020）

> **目標機器（已確認）**
> - 主機：`43.103.3.152`（阿里雲 ECS，內網 `172.17.31.154`）
> - 系統：Ubuntu 22.04.5 LTS / 核心 5.15 / 時區 CST
> - 登入：`joyli`（家目錄 `/home/joyli`，`media1` 與 `media2` 已建好）
> - 磁碟：`/` 39.01GB，已用 10.1%（約 35GB 可用）
> - Python：系統內建 **3.10**（本專案實測相容，見 §2 說明）
> - 記憶體：28% 已用
>
> **域名（已存在，實測 2026-10-22）**
> - `media1.plexusai.app` → `43.103.3.152` → **connector**（8010）
> - `media2.plexusai.app` → `43.103.3.152` → **crm**（8020）
> - Let's Encrypt 憑證涵蓋這兩域，有效期 **2026-10-22 → 2027-01-20**
> - 目前是 nginx 佔位頁（`<h1>media1 home</h1>`），要改成反向代理
>
> **媒體分配**：`media1` → connector、`media2` → crm（域名、目錄、服務三者一致）
>
> 這份文件只做「搬遷 + 上線」，**不改任何業務程式碼**。
> ★ 這台機器沒有 GPU 需求：fastembed 走 onnxruntime **CPU**、DeepSeek 是 API 呼叫，
> 專案裡沒有一行 CUDA 程式碼。

---

## 執行總覽（照這個順序做）

| # | 做什麼 | 在哪台 | 章節 | 關鍵產出 |
|---|---|---|---|---|
| ✅ | 驗 Meta 網路連通性 | 伺服器 | §0.1 | **已完成**：`graph.facebook.com` 回 400（通） |
| ✅ | 盤點 nginx / 憑證 / 目錄 | 伺服器 | §0.2 | **已完成**：4 個 site、certbot 已 enabled |
| ✅ | **取得 sudo 權限** | — | §0.0 | **已取得** |
| → | **拿到 sudo 之後的完整流程** | 兩邊 | **§0.6** | **← 從這裡開始照做** |
| 1 | 系統更新 + 重啟（需與主管協調時段） | 伺服器 | §0.3 | 能連回來 |
| 2 | 安全組確認 80/443 開放 | 控制台 | §0.4 | 443 可達 |
| 3 | 推程式碼上去 | Windows | §3.1 | `~/multi-cs/{connector,crm,scripts}` |
| 4 | 跑 `server_bootstrap.sh`（裝環境+依賴） | 伺服器 | §2.2 | `[ OK ]` 一連串、兩個 import 成功 |
| 5 | 裝 PG 18 + 建庫 + 還原 + 設時區 | 伺服器 | §3.2 | `pg_restore` 零錯誤 |
| 6 | 推媒體 + 合併 + 修 `source_path` | 兩邊 | §3.3 §3.4 | `check_media_setup.py` 25/25 |
| 7 | 推模型 + 改兩個 `.env` | 兩邊 | §3.5 §4 | 三個 `True`（路徑自洽） |
| 8 | systemd + 改 nginx（只動 media1/media2） | 伺服器 | §5 §6 | 兩個 `/health` 都回 ok |
| 9 | 跑驗證清單 → 切 webhook | 兩邊 | §8 §7 | `/webhook` 驗證回 challenge |

**時間估算**（不含排錯）：1~2 小時。其中第 4 步裝依賴最久（~10 分鐘），
第 5 步裝 PG 18 + 還原其次。

### 已完成的實測結論（2026-10-22）

> ✅ **網路已驗通**：`graph.facebook.com` 回 **400** —— 那是 Meta 在拒我們的
> 空請求，代表連得到。不需要換節點、不需要代理。（`000` 才是網路不通。）
>
> ✅ **域名與 HTTPS 已就緒**：`media1.plexusai.app` / `media2.plexusai.app` 都指向這台，
> Let's Encrypt 憑證（SAN 涵蓋兩域）有效期到 2027-01-20。**不用買域名、不用跑 certbot**。
>
> ✅ **憑證續期正常**：`certbot.timer` 是 `enabled`，下次執行 2026-10-22 14:40。
>
> ✅ **目錄已備妥**：`~/media1` `~/media2` 存在（`joyli:www-data`），各有一個 `index.html`。
> 在 `/dev/vda3` 上（不是獨立磁碟），不用 `mkfs`/`fstab`。
>
> ✅ **與 GPU 無關**：`no NVIDIA driver` —— 本專案零 CUDA 依賴，走 CPU onnxruntime。
>
> ⛔ **`joyli` 不在 sudoers** → 全部需要權限的操作都做不了，見 §0.0。
>
> ⚠️ **這台有別人的活躍服務** `voice.plexusai.app`（Voice Agent Demo）→ nginx 只能改 media1/media2。

---

## 0. 開工前：五個檢查（順序不能換）

### 0.0 ⛔ 先確認有沒有 sudo —— 沒有就什麼都做不了

實測（2026-10-22）：**`joyli` 不在 sudoers**，這台機器上做不了任何需要權限的操作。

```
$ id
uid=1001(joyli) gid=1001(joyli) groups=1001(joyli),33(www-data)
$ sudo -v
Sorry, user joyli may not run sudo on iZj6c574eqg8q8p65okq6oZ.
joyli is not in the sudoers file. This incident will be reported.
```

**帳號盤點（實測）**：

| 項目 | 結果 | 判讀 |
|---|---|---|
| `joyli` | uid 1001，組只有 `joyli` + `www-data` | 只有 nginx 讀檔權，沒有管理權 |
| `getent group sudo` | `sudo:x:27:` ← **成員列表是空的** | 沒人靠 sudo 組取得權限 |
| 可登入用戶 | `root`(0)、**`admin`(1000)**、`joyli`(1001) | **`admin` 是 uid 1000 = 機器初始用戶** |
| `sudo -n -l` | `sudo: a password is required` | 非「已授權特定命令」，是真的沒權 |

> ★ `admin` 是 uid 1000 —— Ubuntu 安裝時建立的第一個用戶，**大概率持有 sudo**。
> 本機上 `joyli` 大概是後來為這個專案建的。**找 `admin` 要權限**（或請主管轉達）。
> 若 `admin` 也進不去，只能請 root（雲廠商控制台的 VNC / 重置密碼）。

**這不是技術問題，是權限問題，繞不過去。** 以下全部需要 sudo：

| 操作 | 為什麼必須 |
|---|---|
| `apt install` | 裝 python3-venv（其餘依賴有預編譯 wheel，不需編譯工具） |
| 裝 PostgreSQL 18 | 建庫、建角色 |
| 改 `/etc/nginx/sites-available/*` | 把佔位頁換成反向代理 |
| `systemctl` | 建立並啟動兩個服務 |
| `apt upgrade` + `reboot` | 清掉 `System restart required` |

**好消息**：`/etc/nginx/sites-available/*` 是 `644 root:root`，所以**不用 sudo 就能讀**
（`cat` 得到）。現有配置已經讀出來了 → 見 §6.2，改法已經是「照抄 + 換一段」的確定動作。

**申請權限時直接給這份清單**（比「給我 sudo」有效得多）：

> 部署 Multi-CS（connector 8010 + crm 8020）到 media1/media2 需要以下權限：
>
> 1. `apt install python3-venv postgresql-18`（其餘 Python 依賴有預編譯 wheel）
>    （PG 18 因為本機 dump 是 18 格式，Ubuntu 預設的 14 讀不了）
> 2. `systemctl` 建立兩個服務 `multics-connector` / `multics-crm`，enable 開機自啟
> 3. 改 `/etc/nginx/sites-available/media1.plexusai.app` 與 `media2.plexusai.app`
>    → 靜態佔位頁改成反向代理到 `127.0.0.1:8010` / `8020`
>    → **不會動 `default` 與 `voice.plexusai.app`**（後者是別人的 Voice Agent Demo）
> 4. PostgreSQL 建角色 `joyli` + 兩個庫 `platform_forwarder` / `crm_db`
>
> 可以只授權這幾項，不必給完整 root。
> 另外 `apt upgrade` + `reboot` 會影響 `voice` 服務，請指定可重啟的時段。

**替代路徑**（按優先序）：

1. **請 `admin` 用他的帳號執行** —— 你把命令準備好，他貼上跑（最省事，不用改權限）
2. **給 `joyli` 開 sudo 白名單**（`/etc/sudoers.d/joyli-multics`），只授權上面那幾條命令：
   ```
   joyli ALL=(ALL) NOPASSWD: /usr/bin/apt, /usr/bin/systemctl, /usr/bin/nginx, /usr/bin/tee
   ```
   ⚠️ 這寫法偏寬（`tee` 可寫任意檔），實務上多半直接加進 sudo 組
3. **Docker** —— `joyli` 不在 `docker` 組，走不了（除非請 admin 加）

**沒有 sudo 時仍可先做的事**（不浪費時間，見 §0.5）：

- 本機 dump 兩個資料庫
- 推程式碼與模型到 `~/multi-cs`（家目錄可寫）
- 建 venv + 裝依賴（`python3-venv` 若已裝則不需 sudo；否則要）
- 準備好 nginx 設定檔放在家目錄，等權限下來直接 `sudo cp`

### 0.1 ✅ 網路連通性（已完成）

實測結果：`https://graph.facebook.com/v26.0/` → **HTTP 400**。
這是 Meta 對「沒帶 access token 的空請求」的正常回應，代表**網路通、DNS 正常、TLS 正常**。
不需要換節點，也不需要代理。

若之後要複查，指令仍是這三條（回 4xx = 通，回 `000` = 不通）：

```bash
curl -s -o /dev/null -m 12 -w 'graph.facebook.com -> %{http_code}\n' https://graph.facebook.com/v26.0/
curl -s -o /dev/null -m 12 -w 'api.deepseek.com   -> %{http_code}\n' https://api.deepseek.com
curl -s -o /dev/null -m 12 -w 'aliyun pypi        -> %{http_code}\n' https://mirrors.aliyun.com/pypi/simple/
```

### 0.2 ⚠️ 這是共用機器 —— 動 nginx 前先看懂現況

實測 `sites-enabled/` 有 **4 個** site（不只 media1/media2）：

| 檔案 | 狀態 | 能不能碰 |
|---|---|---|
| `default` | nginx 預設 | ⛔ 不要動 |
| `media1.plexusai.app` → `~/media1` | 21 bytes 佔位頁 `<h1>media1 home</h1>` | ✅ 要改成反向代理 |
| `media2.plexusai.app` → `~/media2` | 21 bytes 佔位頁 | ✅ 要改成反向代理 |
| **`voice.plexusai.app`** | **活躍服務**（`Voice Agent Demo`，21,610 bytes） | ⛔⛔ **絕對不能碰，別人在用** |

> ★★ **動 `/etc/nginx/` 的鐵律**：只改 `media1.plexusai.app` 與 `media2.plexusai.app` 這兩個檔，
> **不新增、不刪除、不重寫** `default` 與 `voice.plexusai.app`。
> 改完 `nginx -t` 通過才 reload。若失敗，先還原備份再查。

盤點命令（**只讀，不需要 sudo 就能跑的部分**）：

```bash
ls -la /etc/nginx/sites-enabled/        # 有哪些 site
ls -l  /etc/nginx/sites-available/      # 檔案權限
cat /etc/nginx/sites-available/media1.plexusai.app   # 若 644 就直接讀得到
ss -lnt | grep -E ':(80|443|8010|8020|5432)\b'       # 埠佔用
systemctl is-enabled certbot.timer                    # 續期（已確認 enabled）
```

**判讀重點**：

| 看到的 | 意思 |
|---|---|
| `sites-enabled/` 有 `media1` `media2` 兩個檔 | 直接改這兩個檔的 `root`/`proxy_pass`，**不要新增** |
| `certbot.timer` 是 `enabled` | ✅ 續期沒問題（**實測已確認**：下次 2026-10-22 14:40） |
| `pg_lsclusters` 無輸出 / `psql` 不在 PATH | PG 還沒裝，要裝 18（§3.2） |
| 8010 / 8020 已被佔 | 要改埠（三處：`.env`、systemd、nginx） |

`server_inspect.sh` 這個腳本可以整套跑（需 sudo），但**若沒有 sudo 就用上面的手動命令**：
`scp` 要在你自己的 Windows 上執行，不是在伺服器上（見下方警告）。

> ⚠️ **`scp` 一定要在你自己的 Windows 上跑，不是伺服器上。**
> 在伺服器上跑 `scp scripts/...` 會報 `No such file or directory` ——
> 那是在把伺服器傳給自己，而伺服器上沒有 `scripts/` 這個目錄。

**方法 A：直接貼（最快，不用傳檔）**

SSH 在伺服器上，貼這整段：

```bash
sudo -v
echo "===== A. nginx sites-enabled ====="
ls -la /etc/nginx/sites-enabled/
echo
echo "===== B. server_name / listen / root / proxy_pass ====="
sudo grep -rnE '^\s*(server_name|listen|root|alias|proxy_pass|ssl_certificate)' \
  /etc/nginx/sites-enabled/ /etc/nginx/sites-available/ 2>/dev/null
echo
echo "===== C. 設定檔全文 ====="
for f in /etc/nginx/sites-enabled/*; do echo "--- $f ---"; sudo cat "$f"; done
echo
echo "===== D. 監聽埠 ====="
sudo ss -lntp | grep -E ':(80|443|8010|8020|5432)\b'
echo
echo "===== E. certbot 續期 ====="
systemctl is-enabled certbot.timer 2>/dev/null || echo "certbot.timer NOT enabled"
systemctl list-timers 'certbot*' --no-pager 2>/dev/null | head -3
sudo certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry'
echo
echo "===== F. PostgreSQL ====="
pg_lsclusters 2>/dev/null || echo "pg_lsclusters 無輸出"
command -v psql >/dev/null && psql --version || echo "psql 不在 PATH"
echo
echo "===== G. 目錄 ====="
ls -ld ~/media1 ~/media2 2>/dev/null
findmnt -T ~/media1 2>/dev/null | tail -1
findmnt -T ~/media2 2>/dev/null | tail -1
echo "media1: $(ls -A ~/media1 2>/dev/null | wc -l) 項"; ls -A ~/media1 2>/dev/null | head
echo "media2: $(ls -A ~/media2 2>/dev/null | wc -l) 項"; ls -A ~/media2 2>/dev/null | head
echo
echo "===== H. GPU ====="
nvidia-smi 2>/dev/null || echo "no NVIDIA driver / no nvidia-smi"
```

**方法 B：傳腳本上去跑**

在**你自己的 Windows** 上（不是伺服器），且必須在專案根目錄：

```powershell
cd C:\Users\its-admin\Desktop\Multi-CS
scp scripts\server_inspect.sh joyli@43.103.3.152:~/
```

然後回到伺服器執行：

```bash
bash ~/server_inspect.sh 2>&1 | tee ~/inspect.txt
```

腳本會回報：nginx 現有 server 區塊（`root` / `proxy_pass` / `server_name`）、
正在監聽的埠、certbot 續期 timer 是否啟用、PG 叢集版本、家目錄現況。
（判讀重點見上一節的表格。）

### 0.3 系統更新與重啟

登入時它已經提示 `*** System restart required ***`，先清掉（**需要 sudo**）：

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
sudo reboot
# 等 30 秒再連回來
```

> ⚠️ 這是**共用機器**，重啟會影響 `voice.plexusai.app` 的服務。
> 動手前先跟主管確認可以重啟，或挑離峰時段。

### 0.4 阿里雲安全組要放行 80 / 443

這是**控制台操作**，在伺服器上做不了。阿里雲控制台 → ECS → 安全組 → 配置規則 →
入方向加兩條：

| 端口 | 授權對象 | 用途 |
|---|---|---|
| 80/tcp | 0.0.0.0/0 | Let's Encrypt 驗證 + HTTP 跳轉 |
| 443/tcp | 0.0.0.0/0 | webhook / 工作台 |
| 8010、8020 | **不要開** | 只綁 127.0.0.1，走 nginx 轉發 |

沒開 80/443，後面證書簽不下來，webhook 也驗不過。

> 實測：`http://43.103.3.152:8010/health` 從外部回 `000`（連不上）→ 說明安全組**沒有**
> 放行 8010/8020，這是**正確的**。但 80/443 是通的（`curl https://media1...` 成功），
> 所以安全組已經開好了，這步可以跳過。

### 0.5 ⏳ 進度看板：哪些做完了、哪些卡在 sudo

#### 已完成（不需要再動）

| 項目 | 證據 |
|---|---|
| 網路連通 Meta | `graph.facebook.com` → 400 |
| 域名 + HTTPS | `media1` / `media2` 都指向本機，憑證受信任 |
| 憑證續期 | `certbot.timer` enabled（下次 14:40） |
| 目錄 | `~/media1` `~/media2`（`joyli:www-data`） |
| nginx 模板 | 已寫好並 scp 到 `~/nginx/`（3 個檔） |
| 模型 614MB | 已 scp 到 `~/multi-cs/models/fastembed/`（`du` 顯示 1.2G 是硬連結展開，正常） |
| 資料庫 dump | `pf.dump` 487K / `crm.dump` 536K（`-Fc` 壓縮後，非原始 12MB） |
| 核對 SQL | `scripts/verify_restore.sql`（兩庫通用） |

#### 待做，**不需要 sudo**

| # | 做什麼 | 前置 |
|---|---|---|
| A | 傳程式碼 `connector` `crm` `scripts` | — |
| B | 傳媒體（`media/` → `media1`、`crm/media/` → `media2`） | — |
| C | 傳 dump 兩個檔 | — |
| D | 建 venv + 裝依賴（~10 分鐘，最耗時） | `python3-venv` 已裝（見下） |
| E | 合併 `kb/` 與 `kb_assets/` 進 media2 | B 完成 |
| F | 寫兩個 `.env` | 先定好 DB 密碼 |

#### 待做，**需要 sudo**

| # | 做什麼 |
|---|---|
| 1 | `apt install python3-venv`（若缺） |
| 2 | 裝 PostgreSQL 18（PGDG 源） |
| 3 | 建角色 `joyli` + 兩個庫 |
| 4 | `pg_restore` 兩個 dump |
| 5 | 修 `source_path`（§3.4） |
| 6 | 建兩個 systemd 服務 |
| 7 | 改兩個 nginx 設定 + reload |

> ★ 第 5 項其實**不需要 sudo**（`joyli` 是 DB owner），但它必須等第 4 項還原完成。

#### 現在就能做的（可貼）

**Windows** —— 傳程式碼、媒體、dump（四個 `scp`，約 10MB）：

```powershell
cd C:\Users\its-admin\Desktop\Multi-CS

# 先建遠端目錄（★ 多個目錄之間一定要空格，不可連寫成 ~/a~/b）
ssh joyli@43.103.3.152 "mkdir -p /home/joyli/multi-cs /home/joyli/media1 /home/joyli/media2"

# 程式碼
scp -r connector crm scripts joyli@43.103.3.152:/home/joyli/multi-cs/

# 資料庫
scp pf.dump crm.dump joyli@43.103.3.152:/home/joyli/multi-cs/

# 媒體：根 media/ 整包先上去（含 kb/ kb_assets/，之後再清理）
scp -r media/* joyli@43.103.3.152:/home/joyli/media1/

# 媒體：crm 的那份
scp -r crm/media/* joyli@43.103.3.152:/home/joyli/media2/
```

**伺服器** —— 清快取、測 venv、合併媒體：

```bash
# 清掉 Windows 帶過來的 .pyc（Linux 用不到，還佔空間）
find ~/multi-cs -name __pycache__ -type d -prune -exec rm -rf {} +

# 測 python3-venv 能不能用（決定 D 要不要等 sudo）
python3 -m venv /tmp/_vtest && /tmp/_vtest/bin/pip -V && rm -rf /tmp/_vtest \
  && echo "=== venv 可用，D 可以現在做 ===" \
  || echo "=== venv 不可用 → 需要 sudo apt install python3-venv ==="

# 合併 kb（★ 不要加 --delete，兩邊內容不重疊）
rsync -a ~/media1/kb/        ~/media2/kb/
rsync -a ~/media1/kb_assets/ ~/media2/kb_assets/
ls ~/media2/kb | wc -l          # 期望 121（19 + 102）
ls ~/media2/kb_assets | wc -l   # 期望 9

# 可選：清掉 media1 裡的 crm 殘留（已有完整副本在 media2）
rm -rf ~/media1/kb ~/media1/kb_assets
```

**若 venv 可用**，順手把最耗時的依賴裝完：

```bash
cd ~/multi-cs
python3 -m venv venv
venv/bin/pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
# 驗證：兩個 app 都能 import（不會串錯，因為 cwd 不同）
( cd connector && PYTHONPATH=. ../venv/bin/python -c "from app.core import config; print('connector OK', config.PORT)" )
( cd crm       && PYTHONPATH=. ../venv/bin/python -c "from app.core import config; print('crm OK', config.PORT)" )
```

> ★ **`build-essential` / `libpq-dev` 大概率不是必需的。**
> 理由：`psycopg2-binary` 自帶 libpq（不需要 `libpq-dev`）；
> `onnxruntime` / `pillow` / `lxml` 在 Linux x86_64 + Python 3.10 都有 manylinux wheel；
> `jieba` 是純 Python 的 sdist（解壓即可，不需編譯）。
> **若 `pip install` 真的回退到源碼編譯**（log 出現 `Building wheel for xxx` 並失敗），
> 再請 sudo 補 `build-essential python3-dev` 即可。
> 換取的是：**只有 `python3-venv` 這一個 apt 包擋在前面**，審批更容易。

這樣等權限下來，剩下的只有「apt → PG → 還原 → 修 source_path → systemd → nginx」，
約 30 分鐘收尾。

---

## 0.6 ★ 拿到 sudo 之後：按這個順序做（4 個階段）

所有容易出錯的步驟都已包成腳本，**你只需要貼命令 + 看 `[ OK ]`**。
每個腳本都是**冪等**的（重跑不會壞）、**自帶檢查**（失敗會說原因）。

### 階段 1｜Windows 端：把缺的檔案補齊（不用 sudo）

```powershell
cd C:\Users\its-admin\Desktop\Multi-CS

# 建好遠端目錄（★ 多個目錄之間要有空格）
ssh joyli@43.103.3.152 "mkdir -p /home/joyli/multi-cs /home/joyli/media1 /home/joyli/media2"

# 程式碼（含 scripts/，後面所有腳本都在裡面）
scp -r connector crm scripts joyli@43.103.3.152:/home/joyli/multi-cs/

# 資料庫 dump
scp pf.dump crm.dump joyli@43.103.3.152:/home/joyli/multi-cs/

# .env（含所有真實渠道憑證，等下用腳本改路徑）
scp connector\.env joyli@43.103.3.152:/home/joyli/multi-cs/connector/.env
scp crm\.env       joyli@43.103.3.152:/home/joyli/multi-cs/crm/.env

# 媒體：根 media/ → media1（含 kb/ kb_assets/，之後再清）
scp -r media/* joyli@43.103.3.152:/home/joyli/media1/

# 媒體：crm 的那份 → media2
scp -r crm/media/* joyli@43.103.3.152:/home/joyli/media2/
```

> 模型（614MB）之前已傳過，若還沒傳：
> `scp -r models\fastembed joyli@43.103.3.152:/home/joyli/multi-cs/models/`

### 階段 2｜伺服器：不需要 sudo 的準備（含 10 分鐘裝依賴）

```bash
cd ~/multi-cs

# ① 清掉 Windows 帶來的 .pyc（Linux 用不到）
find . -name __pycache__ -type d -prune -exec rm -rf {} +

# ② 合併知識庫（★ 不要加 --delete：兩個 kb/ 內容不重疊）
rsync -a ~/media1/kb/        ~/media2/kb/
rsync -a ~/media1/kb_assets/ ~/media2/kb_assets/
echo "kb 子目錄: $(ls ~/media2/kb | wc -l)  期望 121"
echo "kb_assets:  $(ls ~/media2/kb_assets | wc -l)  期望 9"

# ③ 建 media1 的快取目錄（connector 要寫）
mkdir -p ~/media1/kb_cache ~/media1/post_cache

# ④ 寫 .env（把 <PASSWORD> 換成你要的密碼，兩邊會自動一致）
python3 scripts/patch_env.py \
  --db-password '<PASSWORD>' \
  --internal-token "$(openssl rand -hex 32)" \
  --public-base https://media1.plexusai.app

# ⑤ 建 venv + 裝依賴（最久，約 10 分鐘，可先去泡咖啡）
python3 -m venv venv
venv/bin/pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# ⑥ 驗證兩個 app 都能 import（★ 必須在各自目錄跑，否則會串錯 package）
( cd connector && PYTHONPATH=. ../venv/bin/python -c "from app.core import config as c; print('connector OK port', c.PORT, '| media', c.MEDIA_DIR)" )
( cd crm       && PYTHONPATH=. ../venv/bin/python -c "from app.core import config as c; print('crm OK port', c.PORT, '| media', c.MEDIA_DIR)" )
```

> ⚠️ ⑤ 若報 `ensurepip is not available` → 缺 `python3-venv`，先 `sudo apt install python3-venv` 再重跑。

### 階段 3｜伺服器：sudo 主流程（3 條命令）

```bash
# ① 裝 PG 18 → 建角色/兩庫 → 還原 → 設時區 → 修 source_path → 核對
sudo bash ~/multi-cs/scripts/server_apply.sh

# ② 建並啟動兩個服務（寫 systemd unit、enable 開機自啟、健康檢查）
sudo bash ~/multi-cs/scripts/install_services.sh

# ③ 改 nginx（備份 + nginx -t + 失敗自動回滾 + 驗證 voice 未受影響）
sudo bash ~/nginx/apply_nginx.sh
```

三個腳本各自會印出檢查結果。**判斷成功的標準**：

| 腳本 | 成功的樣子 |
|---|---|
| `server_apply.sh` | 兩個庫 `還原成功`、時區 `Asia/Taipei`、`source_path 修復完成（剩 0 筆）`、`全部 source_path 都能落到實體檔` |
| `install_services.sh` | 兩個服務 `active`、兩個 `/health` 有 JSON 回傳、`『Connector worker 啟動』出現 1 次` |
| `apply_nginx.sh` | `nginx -t` 通過、`media1 /` 回 404 或 502、`/internal/` 回 403、`voice /` 仍 200 |

### 階段 4｜驗證與切換

```bash
# 對外可達性（從你電腦或伺服器都可）
curl -s https://media1.plexusai.app/health
curl -s https://media2.plexusai.app/health
curl -s -o /dev/null -w 'internal 封鎖: %{http_code}\n' https://media1.plexusai.app/internal/api/x   # 403
curl -s -o /dev/null -w 'voice 未受影響: %{http_code}\n' https://voice.plexusai.app/                 # 200

# 知識庫路徑自洽（三個 True）
venv/bin/python scripts/check_media_setup.py --root ~/multi-cs
```

全部通過後，**才**去改 Meta 後台的 Callback URL（§7）：
`https://media1.plexusai.app/webhook`

> ⚠️ 切換前先確認本機的 `start_connector.bat` / `start_crm.bat` 是**停的**，
> 否則兩套會搶同一個 webhook，訊息會亂序或重複。

### 出問題怎麼查

| 症狀 | 先看這裡 |
|---|---|
| 服務起不來 | `tail -50 ~/multi-cs/logs/connector.log`（或 crm.log） |
| `ModuleNotFoundError` | venv 依賴沒裝齊 → 重跑階段 2 的 ⑤ |
| 連不上資料庫 | `.env` 的 `DATABASE_URL` 密碼與 PG 角色不符 → 重跑 `patch_env.py` + `server_apply.sh` |
| `/health` 502 | 服務沒起，或 nginx 指到錯的埠 |
| 知識庫帶圖沒圖 | `check_media_setup.py` 的 `kb_cache` / `kb_assets` 檢查 |
| 全部要重來 | `sudo systemctl disable --now multics-connector multics-crm`，nginx 用 `.bak.*` 還原 |

---

## 0b. 先看結論：必踩的 17 個坑

17 條沒處理就是上線當天出事：

| # | 坑 | 後果 | 正確做法 |
|---|---|---|---|
| 1 | **`--workers` 必須是 1** | 6 條週期循環（貼文同步、留言補拉、圖續命、帳號映射、快取清理、告警巡檢）在 `lifespan` 裡啟動，守衛是**模組級全域變數**、只在單進程內有效。N 個 worker → 每條跑 N 份：Meta API 調用 ×N、DeepSeek 並發 ×N（`_GATE` 是進程內信號量）、模型內存 ×N（640MB/份）、5s TTL 參數緩存各一份。★ 兩條主隊列有 `SKIP LOCKED`，**不會**重複發送（見 §5 說明） | `uvicorn --workers 1`，擴容靠升級機器 |
| 2 | **兩個 package 都叫 `app`** | 從錯的 cwd 起服務會 import 到另一邊（或舊單體 `app/`） | 每個服務自己的 `WorkingDirectory` + `PYTHONPATH`，見 §5 |
| 3 | **`KB_CACHE_DIR` 必須在 `MEDIA_DIR` 底下** | 指到別的根 → 知識庫圖片快取寫得進、`resolve()` 找不到，圖片靜默丟失 | 見 §3.3 / §4 |
| 4 | **`RAG_MODEL_DIR` 是絕對路徑** | 本機寫死 `C:/Users/...`，伺服器上載不到模型 → 首次啟動自動下載 640MB（或直接失敗） | 搬 `models/fastembed` 並改成伺服器絕對路徑 |
| 5 | **`/internal/api/*` 只靠 token，沒有來源 IP 限制** | `INTERNAL_BIND_HOST` 在程式裡**只定義、沒使用**。若 8010 直接對公網 → 內網 API 全裸露 | nginx 明確 `location /internal/ { return 403; }` |
| 6 | **`META_VERIFY_SIGNATURE=false`** | 公網上任何人都能偽造 Meta webhook 打進來 | 上線改 `true`（secret 已填） |
| 7 | **`INTERNAL_API_TOKEN=CHANGE-ME-INTERNAL-TOKEN`** | 兩個服務都用預設 token，等於沒有認證 | 兩邊同時換 `openssl rand -hex 32` |
| 8 | **每日上限用 `date_trunc('day', now())`** | PG 時區是 UTC 時，「今天」在台灣時間早上 8 點才歸零 | PG `timezone = 'Asia/Taipei'` |
| 9 | ★★**`kb_document.source_path` 存的是 Windows 絕對路徑** | 實測 **22/22 筆**都是 `C:\Users\its-admin\Desktop\Multi-CS\media\kb\...`。搬到 Linux 後「重新解析 / 重新分塊」直接找不到檔 | 搬完跑修復 SQL（§3.4），不能靠改 .env 解決 |
| 10 | ★★**media 要拆成兩個根，不是複製兩份** | 根 `media/` 裡混著 connector 的東西（客戶媒體、`kb_cache`、`post_cache`）和 crm 的東西（`kb/`、`kb_assets/`）。整包丟給 media1 → 知識庫檔案留在錯的根，DB 卻指向 media2 → 404 | 見 §3.3 的拆分手順 |
| 11 | **`media/kb` 與 `crm/media/kb` 完全不重疊** | 實測 19 vs 102 個子目錄、**交集 0**：舊單體與 CRM 時期各寫各的，只搬一邊就掉一半文件 | `rsync` 合併，不要 `--delete` |
| 12 | **上傳上限 50MB（`RAG_MAX_UPLOAD_MB`）** | nginx 預設 1MB → PDF 上傳 413 | `client_max_body_size 64m` |
| 13 | **Meta / 微信 webhook 必須 HTTPS 且憑證有效** | 自簽憑證 Meta 直接拒收 | certbot（Let's Encrypt） |
| 14 | ★★**本機 PG 18 → 伺服器預設 PG 14** | `pg_restore: unsupported version (1.16) in file header`，整個還原失敗 | 伺服器裝 PG 18（PGDG 源），見 §3.2 |
| 15 | ★★**這台在阿里雲、時區 CST，很可能在大陸** | `graph.facebook.com` 不通 → webhook 收不到、訊息發不出，**渠道整體失效** | **開工前先測**（§0.1），不通就換節點或走代理 |
| 16 | **Meta webhook 一定要 HTTPS + 有效憑證** | 只有 IP 簽不到憑證 → 驗證必失敗 | ✅ **已解決**：`media1`/`media2` 域名 + LE 憑證都現成，見 §6.1 |
| 17 | ★★**憑證 2027-01-20 到期** | 沒設自動續期 → 到期當天 webhook 全掛，症狀是「昨天還好好的」 | 確認 `certbot.timer` enabled + `certbot renew --dry-run` |

---

## 1. 目標架構

```
公網 ──443──> nginx ─┬─ media1.plexusai.app  ──> 127.0.0.1:8010  (connector)
                     │     /wechat /webhook /whatsapp/webhook /files/
                     │     /internal/** → 403
                     └─ media2.plexusai.app    ──> 127.0.0.1:8020  (crm)
                           /  /login /admin /agent /ops /console/** /media/** /files/**
                           /internal/** → 403

伺服器內部：crm → http://127.0.0.1:8010/internal/api/*
           connector → http://127.0.0.1:8020/internal/api/*
           └─ 同一個 PostgreSQL 實例的兩個 database
```

**為什麼分兩個子域**：connector 要對公網開 webhook，crm 要對公網開工作台。分開之後
connector 那一側可以用白名單只放行 4 條路徑（連根路徑都 404），攻擊面最小。
（也可以單域用路徑分流的，見 §6 附註，但白名單會變複雜。）

目錄規劃（`media1` / `media2` 由公司分配，這裡假設掛在 `/data` 下）：

```
/home/joyli/media1/                 # ← connector 的 MEDIA_DIR
├── kb_cache/                 #   知識庫圖片快取（KB_CACHE_DIR，必須在這裡面）
├── post_cache/               #   貼文縮圖本地化（Meta CDN 24h 過期）
├── igfb_*.bin                #   客戶來訊的語音/圖片
└── reply_*                   #   帶附件的歷史訊息檔

/home/joyli/media2/                 # ← crm 的 MEDIA_DIR
├── kb/                       #   上傳的來源文件（RAG_KB_DIR，不對外）
└── kb_assets/                #   抽出的公開圖片（RAG_MEDIA_DIR，掛 /media）

/home/joyli/multi-cs/
├── connector/                # 程式碼（含 .env）
├── crm/                      # 程式碼（含 .env、ui/）
├── models/fastembed/         # 614MB embedding 模型
└── venv/
```

> ★ **`media1` / `media2` 是目錄不是磁碟時也照這樣用**。若它們其實是兩塊獨立磁碟，
> 先掛載（§2.1）再建子目錄，其餘不變。

---

## 2. 伺服器準備

### 2.1 media1 / media2：三個層次要分清

這三個東西名字一樣，很容易搞混 —— 先釐清：

| 層次 | 是什麼 | 實測結果 |
|---|---|---|
| **域名** | `media1.plexusai.app` / `media2.plexusai.app` | ✅ 已存在，指向這台，HTTPS 可用 |
| **家目錄** | `~/media1` / `~/media2` | 已存在（推測是 nginx 佔位頁的 `root`） |
| **`.env` 設定** | `MEDIA_DIR` 等 | 待填，指向上面那兩個目錄 |

**推測的完整設計**（跑完 §0.2 的盤點腳本就能確認）：

```
媒體檔案落在 ~/media1/…           對外網址 https://media1.plexusai.app/files/…
   ↑ connector 讀寫 MEDIA_DIR        ↑ Meta 從這裡抓圖（PUBLIC_BASE_URL）

媒體檔案落在 ~/media2/kb_assets/…  對外網址 https://media2.plexusai.app/media/…
   ↑ crm 讀寫 RAG_MEDIA_DIR          ↑ 知識庫圖片
```

要驗證目錄形態（是普通目錄還是獨立磁碟）：

```bash
ls -ld ~/media1 ~/media2
findmnt -T ~/media1 ; findmnt -T ~/media2
df -h ~/media1 ~/media2
```

| `findmnt` 輸出 | 意思 | 還要做什麼 |
|---|---|---|
| 來源是 `/dev/sdb1` 之類 | 獨立磁碟，**已掛載** | 只要確認可寫 |
| 顯示 `/` 或 `overlay` | 根分割區上的**普通目錄** | 容量跟根分割區共用（`/` 39G，夠） |
| `findmnt` 報 not mounted | 目錄不存在 | 先 `mkdir` |

> 從登入 banner 看 `Usage of /: 10.1% of 39.01GB` 且只有一個分割區，
> **大概率是普通目錄**。不用 `mkfs`、不用寫 `/etc/fstab`。

如果是裸磁碟才需要掛載（**會清空該磁碟，先確認盤符沒認錯**）：

```bash
lsblk -f                                    # 確認哪塊盤是空的、沒有資料
sudo mkfs.ext4 /dev/sdb1
sudo mkdir -p /home/joyli/media1
sudo blkid /dev/sdb1                        # 取 UUID
echo 'UUID=<上面那串> /home/joyli/media1 ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a && findmnt -T /home/joyli/media1
```

### 2.2 系統套件 + 目錄 + venv（一條命令做完）

> ⚠️ **執行順序**：腳本要讀 `crm/requirements.txt`，所以**先做 §3.1 把程式碼推上去**，
> 再回來跑這個。不然它會警告找不到 requirements 然後跳過裝依賴。

上面 2.1 確認完 `media1`/`media2` 是普通目錄（你的情況：家目錄下兩個空目錄）之後，
直接跑我寫好的腳本，它把 §2.2 / §3.6 全部做掉，而且**可重複執行**：

```powershell
# ① 在「你自己的 Windows」上跑（★ 不是伺服器上），且必須在專案根目錄
cd C:\Users\its-admin\Desktop\Multi-CS
scp scripts\server_bootstrap.sh joyli@43.103.3.152:~/
```

```bash
# ② 回到伺服器上跑（不要 sudo，腳本需要時自己會提權）
bash ~/server_bootstrap.sh
```

> ⚠️ 若 `scp` 報 `No such file or directory`，只有兩種原因：
> ① 你在伺服器上跑（要在自己電腦跑）；② 不在專案根目錄（先 `cd`）。
> 替代方案：直接在伺服器上 `nano ~/server_bootstrap.sh` 貼內容，效果一樣。

腳本做這些事，每一步都有 `[ OK ]` / `[FAIL]` 回報：

1. 印環境（python 版本、磁碟、可用空間）
2. **重跑一次 §0.1 的網路測試**（Meta / DeepSeek / PyPI）
3. `apt install`：python3-venv（其餘依賴有預編譯 wheel；nginx/certbot 已裝）
   （★ 刻意**不裝** postgresql —— apt 預設給 14，而本機 dump 是 18 的格式）
4. 建 `/home/joyli/multi-cs`、`models`、確認 `media1`/`media2` 可寫
5. 建 venv + 用**阿里雲 PyPI 鏡像**裝依賴（約 200MB，幾分鐘）
6. 分別 import crm 與 connector 的 config（驗證「兩個 `app` package」沒串錯）
7. 驗 onnxruntime 可用

手動做也不是不行，就是下面這幾行（腳本做的事一模一樣）：

```bash
sudo apt-get update
# 只裝 venv 支援；nginx / certbot 這台已經有了（$ nginx -v 可確認）
# 刻意不裝 build-essential / libpq-dev / postgresql：
#   · 依賴全是預編譯 wheel，不需要編譯工具
#   · psycopg2-binary 自帶 libpq，不需要 libpq-dev
#   · postgresql 由 apt 安裝會給 14，而本機 dump 是 18 格式（見 §3.2）
sudo apt-get install -y python3-venv

mkdir -p ~/multi-cs/models

python3 -m venv ~/multi-cs/venv
~/multi-cs/venv/bin/pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
# ⚠️ 這行要在「程式碼已經 scp 上去」之後才能跑（§3.1）
~/multi-cs/venv/bin/pip install -r ~/multi-cs/crm/requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/
```

> ★ **Python 用系統內建的 3.10 就好，不要升 3.12。**
> 當初擔心的是 3.11+ 語法，但我掃過整個專案：**沒有任何 3.11/3.12 專屬語法**
> （沒有 `TaskGroup`、`except*`、`tomllib`、`typing.Self`），92 個檔案用了
> `from __future__ import annotations`，`str | None` 在 3.10 也合法。
> 用 3.10 可以省掉 deadsnakes PPA —— 那個從大陸網路連常常卡住，是純粹的風險。
> ⚠️ 本機是 3.14，伺服器是 3.10，**不要反過來**（3.14 上 `psycopg2-binary`、
> `onnxruntime` 輪子不齊，會逼你本地編譯）。

### 2.3 先確認埠沒被佔（共用機器必查）

```bash
ss -lntp | grep -E ':8010|:8020'    # 有沒有被別的服務佔
free -h                              # 記憶體（模型 640MB + uvicorn，1.5GB 起）
df -h /                              # 磁碟
```

- 埠衝突 → 改 `.env` 的 `PORT`，同時改 systemd 的 `--port` 和 nginx 的 `proxy_pass`，**三處要一致**
- 若同機還跑別的 CPU 密集任務，`onnxruntime` 預設會吃多核 → 在 systemd 加
  `Environment=OMP_NUM_THREADS=4` 限制

---

## 3. 搬程式碼與資料

### 3.1 程式碼（從 Windows 推上去）

專案不是 git repo 的話（`.git` 有，但可能沒 remote），**直接 scp 資料夾最快**。

> ⚠️ **這一節的所有 `scp` 都在你自己的 Windows PowerShell 上跑**，
> 不是在 SSH 的伺服器視窗裡。在伺服器上跑 `scp -r connector ...` 會報
> `No such file or directory`，因為伺服器上沒有你的 `connector/` 目錄。

先建遠端目錄（這條可以在伺服器上跑，也可以跳過 —— scp 到不存在的目錄會失敗）：

```bash
ssh joyli@43.103.3.152 "mkdir -p ~/multi-cs"
```

```powershell
# Windows PowerShell，專案根目錄
cd C:\Users\its-admin\Desktop\Multi-CS

# 只推兩包（舊單體 app/ 與 8001 不必部署，CRM 已自帶 /console/kb/*）
scp -r connector joyli@43.103.3.152:~/multi-cs/
scp -r crm       joyli@43.103.3.152:~/multi-cs/
scp -r scripts   joyli@43.103.3.152:~/multi-cs/
```

要重推（改過程式碼）用 `scp -r` 覆蓋即可。若要排除快取，在伺服器上清一次：

```bash
find ~/multi-cs -name __pycache__ -type d -prune -exec rm -rf {} +
```

然後把 `.env` 也推上去（`.gitignore` 有排除，scp 不會漏但容易被忘）：

```powershell
scp connector\.env joyli@43.103.3.152:~/multi-cs/connector/.env
scp crm\.env       joyli@43.103.3.152:~/multi-cs/crm/.env
```

### 3.2 資料庫（兩個）

**時序很重要**：先停本機服務，再 dump，避免搬過去之後又漏最後幾筆。

```powershell
# ① 本機：先關掉兩個服務的黑窗（start_connector.bat / start_crm.bat）
#    確認 8010 / 8020 沒有在 LISTEN 了：
netstat -ano | findstr "127.0.0.1:8010 127.0.0.1:8020"

# ② 本機：dump（pg_dump 不在 PATH 就用完整路徑，見下面註）
$pg = "C:\Program Files\PostgreSQL\18\bin"
& "$pg\pg_dump.exe" -h localhost -U LEE -Fc -d platform_forwarder -f pf.dump
& "$pg\pg_dump.exe" -h localhost -U LEE -Fc -d crm_db             -f crm.dump
# 各 12~14MB，直接推上去
scp pf.dump crm.dump joyli@43.103.3.152:~/multi-cs/
```

> Windows 上 `pg_dump` 通常不在 PATH。要先加：
> `$env:Path += ";C:\Program Files\PostgreSQL\18\bin"`，或用上面的完整路徑。

```bash
# ③ 伺服器（★ 先裝 PG 18，見下面「版本陷阱」——不要用 apt 預設的 14）
#    裝完確認版本是 18：
psql --version && pg_restore --version

# ④ 建角色與兩個庫
#    ★ 密碼自己定一個強的，等下要填進兩個 .env 的 DATABASE_URL
sudo -u postgres psql -c "CREATE ROLE joyli LOGIN PASSWORD '<新密碼>';"
sudo -u postgres createdb -O joyli platform_forwarder
sudo -u postgres createdb -O joyli crm_db

# ⑤ 還原
#    ★ crm.dump 有一個 superuser-only 的 postgres_fdw（開發機殘留），必須先剔除，
#      否則整個還原會因 permission denied 回滾（表一張都不會建）。
pg_restore -l ~/multi-cs/crm.dump \
  | grep -vE "^[0-9]+; [0-9]+ [0-9]+ EXTENSION - |COMMENT - EXTENSION " \
  > /tmp/crm.toc
grep -c EXTENSION /tmp/crm.toc        # 期望 0

pg_restore -h 127.0.0.1 -U joyli -d platform_forwarder --no-owner --no-privileges ~/multi-cs/pf.dump
pg_restore -h 127.0.0.1 -U joyli -d crm_db --no-owner --no-privileges \
           --single-transaction -L /tmp/crm.toc ~/multi-cs/crm.dump

# ⑥ 時區（坑 8）
sudo -u postgres psql -c "ALTER DATABASE platform_forwarder SET timezone='Asia/Taipei';"
sudo -u postgres psql -c "ALTER DATABASE crm_db             SET timezone='Asia/Taipei';"

# ⑦ 驗證還原成功（數字要對得上本機）
psql -h 127.0.0.1 -U joyli -d platform_forwarder -c "select count(*) from forward_log;"   # 108
psql -h 127.0.0.1 -U joyli -d crm_db -c "select count(*) from crm_business;"              # 2
psql -h 127.0.0.1 -U joyli -d crm_db -c "select count(*) from kb_chunk;"                  # 102
```

> 兩個庫都**不需要任何 extension**（embedding 存 `real[]`，cosine 在 Python 裡算；沒有 pgvector 依賴）。
>
> ⚠️ **但 `crm.dump` 裡有一個 `postgres_fdw`**（開發機殘留，見下方）。它是 superuser-only 的，
> `joyli` 還原時會敗在 `permission denied to create extension "postgres_fdw"`，
> 而且 `--single-transaction` 會讓**整個還原回滾**（表一張都不會建出來）。
> `scripts/server_apply.sh` 已自動把它從 TOC 剔除，手動還原時請照著做。
> `pg_restore` 的 `--no-owner` 是必要的：dump 裡的 owner 是本機的 `LEE`，伺服器上不存在會全部報錯。

#### ⚠️ 版本陷阱：本機 PG 18 → 伺服器預設 PG 14，dump 讀不進去

這是實打實會擋住的：本機是 **PostgreSQL 18.6**，而 Ubuntu 22.04 的 `apt install postgresql`
只會給你 **14**。`pg_restore` 讀到 18 格式的 archive 會直接報：

```
pg_restore: error: unsupported version (1.16) in file header
```

**解法：伺服器上也裝 PG 18**（用官方 PGDG 套件庫，跟你本機同版本，最省事）：

```bash
# ① 加 PGDG 源（阿里雲鏡像，大陸網路比官方快）
curl -fsSL https://mirrors.aliyun.com/postgresql/repos/apt/ACCC4CF8.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/pgdg.gpg
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://mirrors.aliyun.com/postgresql/repos/apt jammy-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list

# ② 裝 18（會跟 22.04 預載的 14 並存，但 18 會接管 5432）
sudo apt-get update
sudo apt-get install -y postgresql-18
psql --version          # 期望 psql (PostgreSQL) 18.x

# ③ 若 14 已經在跑、佔住 5432，先停掉它
sudo systemctl stop postgresql@14-main 2>/dev/null
sudo systemctl disable postgresql@14-main 2>/dev/null
sudo systemctl status postgresql --no-pager | head -5
```

驗證版本對得上（兩個數字要一致）：

```bash
psql --version                                  # 伺服器端工具版本
psql -h 127.0.0.1 -U joyli -d postgres -c "select version();"
```

> 不想裝 PG 18 的替代方案：本機改 dump 成**純 SQL**，容忍版本噪音。
> `pg_dump --no-owner --no-privileges -Fp -f pf.sql`，還原用 `psql -f pf.sql`。
> 缺點：PG18 的 dump 會帶 `\restrict` 之類的新 psql 指令，在 14 上會刷一堆錯誤
> （大多不影響後續語句，但很容易誤判為失敗）。**除非有理由，建議直接裝 PG 18。**

如果是**全新庫**（不搬歷史資料）：`platform_forwarder` 用 `scripts/create_db.py` 建表（⚠️ 裡面 `HOST/USER/PASSWORD/ADMIN_DB` 是寫死的，要先改），`crm_db` 用 `scripts/init_crm_db.py --admin-account admin --admin-password '<密碼>'`。

### 3.3 媒體（media1 / media2）—— 這節最容易做錯

**先搞懂現況**（實測過，不是推測）：

本機根 `media/` 是**混著的**，裡面同時有 connector 和 crm 兩邊的東西：

```
media/                          crm/media/
├── 4016343e...png   ← connector  ├── kb/          ← crm（102 個子目錄）
├── igfb_*.bin       ← connector  └── kb_assets/   ← crm（9 個圖片）
├── reply_*          ← connector
├── kb/              ← crm（19 個子目錄）★舊單體寫的
├── kb_assets/       ← crm（9 個圖片）★與 crm/media/kb_assets 完全同名同大小
├── kb_cache/        ← connector（知識庫圖片快取）
└── post_cache/      ← connector（貼文縮圖）
```

兩個關鍵事實：

| 實測項 | 資料 |
|---|---|
| `media/kb` vs `crm/media/kb` | **19 vs 102 個子目錄，交集 0** → 完全不重疊，**兩邊都要** |
| `media/kb_assets` vs `crm/media/kb_assets` | 9 個檔案**完全相同** → 留一邊即可 |
| `kb_asset.rel_path` | 7 筆全是 `kb_assets/<sha16>.png`，**相對於 MEDIA_DIR** → 檔案必須落在 `<crm MEDIA_DIR>/kb_assets/` |
| `kb_document.source_path` | 22 筆全是 `C:\Users\...\media\kb\...` 絕對路徑 → **搬完要改** |

**手順**（在本機執行，`<server>` 換成機器位址）：

```bash
# ① 整個根 media/ 先上去（含 kb/ kb_assets/，暫時冗餘沒關係）
rsync -av media/     joyli@43.103.3.152:/home/joyli/media1/

# ② crm 的那份也上去（102 個子目錄 + 圖片）
rsync -av crm/media/ joyli@43.103.3.152:/home/joyli/media2/

# ③ 把根 media 裡屬於 crm 的兩個目錄，合併進 media2
#    ⚠️ 一定不要加 --delete：兩邊 kb/ 內容不重疊，--delete 會洗掉 102 個目錄
rsync -av media/kb/        joyli@43.103.3.152:/home/joyli/media2/kb/
rsync -av media/kb_assets/ joyli@43.103.3.152:/home/joyli/media2/kb_assets/

# ④ 確認合併結果（在伺服器上）
ls /home/joyli/media2/kb | wc -l          # 期望 121（19 + 102）
ls /home/joyli/media2/kb_assets | wc -l   # 期望 9
```

**如果想把冗餘清掉**（可選，建議先確認服務跑起來再說）：

```bash
# media1 只留 connector 的東西，kb/ 與 kb_assets/ 已經在 media2 有完整副本
rm -rf /home/joyli/media1/kb /home/joyli/media1/kb_assets
```

**權限**（很容易漏 → 服務啟動後「寫不進媒體」）：

```bash
sudo chown -R joyli:joyli /home/joyli/media1 /home/joyli/media2
sudo chmod -R u+rwX /home/joyli/media1 /home/joyli/media2
```

> ★ 若要保留原本的目錄結構（`/home/joyli/multi-cs/media`、`/home/joyli/multi-cs/media-crm`）也可以，
> 只要 §4 的 `.env` 對得上、且 `kb_cache` 在 connector 的根底下、`kb_assets` 在 crm 的根底下就行。
> **用 media1/media2 的差別只是路徑字串**，程式不看名字。

### 3.3.1 用腳本驗（做完上面所有步驟之後）

`scripts/check_media_setup.py` 會把上面所有「靜默失效」的條件逐條驗一遍
（路徑自洽、`rel_path` 是否落到實體檔、`source_path` 是否還有 Windows 路徑…）。
**它不 import 任何 app 模組**（兩個 package 都叫 `app`，同進程必衝突），只解析 `.env` + 查 DB。

```bash
cd ~/multi-cs
# 直接用 venv 的 python，它天生就找得到自己的 site-packages，不用設 PYTHONPATH
venv/bin/python scripts/check_media_setup.py --root ~/multi-cs
```

⚠️ 這步要**在 §3.4 修完 `source_path` 之前先跑一次**（確認問題存在），
修完後**再跑一次**（確認全過）。兩次的差異就是修復有沒有生效的證據。

本機（搬遷前）的預期輸出是 **23/25**，兩個 FAIL 正是要修的：

```
[FAIL] kb/ 子目錄數 ≥ 121（19 舊 + 102 CRM）  →  實際 102      ← §3.3 的合併還沒做
[FAIL] kb_document.source_path 沒有殘留 Windows 絕對路徑
       →  22 筆仍是 C:\ 開頭（跑 §3.4 的修復 SQL）
```

伺服器上應該 **25/25 全 PASS**。沒到的話逐條修，別急著起服務 ——
這些問題在服務啟動時**一個都不會報錯**，只會在客戶問到帶圖的知識庫問題時靜默失效。

### 3.4 修 `kb_document.source_path`（Windows 絕對路徑）

`source_path` 在運行時真的會被讀：`kb_ingest.parse_cached()` 用它去解析文件
（重新分塊預覽、重新入庫），`delete_document()` 用它清源檔案夾。
搬到 Linux 後那 22 筆 `C:\Users\...` 全部無效 → 想重新解析任何一份舊文件都會失敗。

#### ⚠️ 注意：22 筆分佈在**兩個不同前綴**（實測）

| 來源前綴 | 筆數 | 搬到哪 |
|---|---|---|
| `C:\Users\its-admin\Desktop\Multi-CS\media\kb\...` | **18** | `/home/joyli/media2/kb/...` |
| `C:\Users\its-admin\Desktop\Multi-CS\crm\media\kb\...` | **4** | `/home/joyli/media2/kb/...` |

兩者都要映射到 `media2/kb`（§3.3 已把兩個根的 `kb/` 合併進去）。
**只替換 `Multi-CS/media` 會漏掉那 4 筆**（它們是 `Multi-CS/crm/media`，前綴不匹配）。
所以要**兩條規則都寫**。

**先看**（在伺服器上，還原完 DB 之後）：

```bash
psql -h 127.0.0.1 -U joyli -d crm_db -c \
  "select count(*) filter (where source_path like 'C:%') as win_abs, count(*) from kb_document;"
# 期望：win_abs = 22（你的資料量可能不同）
```

**修復**：

```bash
# ① dry-run：一次套兩條規則（★ 順序無妨，但兩條都要有）
psql -h 127.0.0.1 -U joyli -d crm_db -c "
select replace(source_path,'\\','/') as before,
       regexp_replace(
         regexp_replace(replace(source_path,'\\','/'),
           '^C:/Users/its-admin/Desktop/Multi-CS/crm/media', '/home/joyli/media2'),
         '^C:/Users/its-admin/Desktop/Multi-CS/media',       '/home/joyli/media2') as after
  from kb_document where source_path like 'C:%' limit 8;"

# ② 確認 after 全部是 /home/joyli/media2/... 才寫入
psql -h 127.0.0.1 -U joyli -d crm_db -c "
update kb_document
   set source_path =
       regexp_replace(
         regexp_replace(replace(source_path,'\\','/'),
           '^C:/Users/its-admin/Desktop/Multi-CS/crm/media', '/home/joyli/media2'),
         '^C:/Users/its-admin/Desktop/Multi-CS/media',       '/home/joyli/media2')
 where source_path like 'C:%';"

# ③ 驗證：不該再有 C: 開頭
psql -h 127.0.0.1 -U joyli -d crm_db -c \
  "select count(*) from kb_document where source_path like 'C:%';"   # 期望 0
```

> ★ 兩個 `regexp_replace` 是**嵌套**的：先處理 `crm/media`，再處理 `media`。
> 順序其實不影響（`Multi-CS/crm/media` 的第二段是 `crm` 不是 `media`，
> 第一條規則的 pattern 咬不到它），但嵌套寫法一次過，避免漏掉。

然後逐筆確認檔案在（路徑對不對，一條 shell 就能驗）：

```bash
psql -h 127.0.0.1 -U joyli -d crm_db -t -A -c \
  "select source_path from kb_document where source_path is not null" \
| while read -r p; do [ -f "$p" ] || echo "MISSING: $p"; done
# 沒有輸出 = 全部對得上
```

### 3.5 模型（614MB，Windows 推上去）

模型不走 git，也不建議在伺服器重新下載（640MB + hf-mirror 速度不穩）。直接推：

```powershell
# Windows PowerShell，專案根目錄
scp -r models\fastembed joyli@43.103.3.152:~/multi-cs/models/
```

推完在伺服器上確認（模型是否真的到位，「重新解析」才不會臨時去下載）：

```bash
du -sh ~/multi-cs/models/fastembed          # 期望 ~614M
ls ~/multi-cs/models/fastembed             # 應該有 blobs/ snapshots refs/ 之類
env RAG_MODEL_DIR=~/multi-cs/models/fastembed \
  PYTHONPATH=~/multi-cs/crm ~/multi-cs/venv/bin/python -c \
  "from app.rag import rag_config; print('model_dir =', rag_config.RAG_MODEL_DIR)"
```

> ★ 路徑要是**絕對路徑**。`~/` 在 shell 裡會展開，但 systemd 的 `Environment=` 不會展開 `~`
> —— 所以 `.env` 裡一律寫 `/home/joyli/multi-cs/models/fastembed`。

### 3.6 venv

**這步 §2.2 的腳本已經做了**，想手動重做或修補時用：

```bash
cd ~/multi-cs
python3 -m venv venv
venv/bin/pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
# 兩邊依賴有交集但 CRM 多很多；直接裝 CRM 那份，connector 需要的都在裡面
venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

---

## 4. 改 `.env`（逐項對照）

`config.py` 是**用檔案自身位置**去找 `.env`（`connector/.env`、`crm/.env`），不吃 cwd，所以確認檔案位置正確就好。

> ★ **只有 4 個鍵跟 media1/media2 有關**，其餘照抄。記住一條規則就夠：
> **`KB_CACHE_DIR` 必須在 connector 的 `MEDIA_DIR` 底下；`RAG_MEDIA_DIR` 與 `RAG_KB_DIR`
> 必須在 crm 的 `MEDIA_DIR` 底下。**

### connector/.env → 指向 `media1`

| 鍵 | 本機現值 | 伺服器要改成 | 說明 |
|---|---|---|---|
| `DATABASE_URL` | `...postgres:postgres@localhost:5432/platform_forwarder` | `postgresql://joyli:<密碼>@127.0.0.1:5432/platform_forwarder` | 新角色 |
| **`MEDIA_DIR`** | `../media` | **`/home/joyli/media1`** | 客戶來訊媒體 + kb_cache + post_cache |
| **`KB_CACHE_DIR`** | `../media/kb_cache` | **`/home/joyli/media1/kb_cache`** | **必須在 MEDIA_DIR 底下**（坑 3） |
| `CRM_INTERNAL_BASE` | `http://127.0.0.1:8020` | 不變 | 同機 |
| `INTERNAL_API_TOKEN` | `CHANGE-ME-INTERNAL-TOKEN` | `openssl rand -hex 32`（與 crm 一致） | 坑 7 |
| `PUBLIC_BASE_URL` | `https://slam-...ngrok-free.dev` | `https://media1.plexusai.app` | **只有它能被 Meta 抓圖**，`/files/` 要在這個域 |
| `META_VERIFY_SIGNATURE` | `false` | `true` | 坑 6 |
| 其餘渠道憑證 | — | 原樣搬 | 若之後改多商戶 App，憑證在 DB 不在這裡 |

### crm/.env → 指向 `media2`

| 鍵 | 本機現值 | 伺服器要改成 | 說明 |
|---|---|---|---|
| `DATABASE_URL` | `...postgres:postgres@localhost:5432/crm_db` | `postgresql://joyli:<密碼>@127.0.0.1:5432/crm_db` | |
| `CONNECTOR_INTERNAL_BASE` | `http://127.0.0.1:8010` | 不變 | |
| `INTERNAL_API_TOKEN` | `CHANGE-ME-INTERNAL-TOKEN` | 與 connector 同一個值 | |
| `RAG_MODEL_DIR` | `C:/Users/its-admin/Desktop/Multi-CS/models/fastembed` | `/home/joyli/multi-cs/models/fastembed` | 坑 4 |
| **`MEDIA_DIR`** | `media`（相對，會受 cwd 影響） | **`/home/joyli/media2`** | 絕對路徑 |
| **`RAG_MEDIA_DIR`** | `media/kb_assets` | **`/home/joyli/media2/kb_assets`** | 公開圖片目錄（掛 `/media`） |
| **`RAG_KB_DIR`** | 未設（預設 `media/kb`） | **`/home/joyli/media2/kb`** | 來源文件，**不對外** |
| `PUBLIC_BASE_URL` | 空 | 留空 | 目前程式沒用到（只定義在 config.py） |
| `CRM_ADMIN_PASSWORD` | 本機自檢用 | 可留可刪 | UI 重設密碼後就過期，不是系統依賴 |

改完可以用這條**自檢四個路徑是否自洽**（在伺服器上）：

```bash
env PYTHONPATH=/home/joyli/multi-cs/connector /home/joyli/multi-cs/venv/bin/python -c "
from app.core import config as c
print('MEDIA_DIR    =', c.MEDIA_DIR)
print('KB_CACHE_DIR =', c.KB_CACHE_DIR)
print('cache 在 media 底下:', c.KB_CACHE_DIR.startswith(c.MEDIA_DIR))"

env PYTHONPATH=/home/joyli/multi-cs/crm /home/joyli/multi-cs/venv/bin/python -c "
from app.core import config as c
from app.rag import rag_config as r
print('MEDIA_DIR   =', c.MEDIA_DIR)
print('RAG_KB_DIR  =', r.RAG_KB_DIR, '| 在 media 底下:', r.RAG_KB_DIR.startswith(c.MEDIA_DIR))
print('RAG_MEDIA   =', r.RAG_MEDIA_DIR, '| 在 media 底下:', r.RAG_MEDIA_DIR.startswith(c.MEDIA_DIR))"
```

三個 `True` 才算對。任何一個 `False` → 就是坑 3 那類「寫得進、讀不到」。

> `PUBLIC_BASE_URL` 的職責分工（實測 grep 過全部呼叫點）：
> **connector 的**用來拼 `<base>/files/<media_key>` 給 Meta 抓圖 → 必須公網可達；
> **crm 的**目前沒有任何程式在讀，`/media/xxx.png` 一律回相對路徑。
>
> ★ 所以 `connector/.env` 的 `PUBLIC_BASE_URL` 填 `https://media1.plexusai.app`
> 之後，Meta 會來抓 `https://media1.plexusai.app/files/<media_key>` —— nginx 的
> `location /files/` 必須轉到 8010（§6）。這條鏈路沒接好，**知識庫帶圖回覆會靜默沒圖**。

---

## 5. systemd（`--workers 1`）

`/etc/systemd/system/multics-connector.service`

```ini
[Unit]
Description=Multi-CS Connector (8010)
After=network.target postgresql.service

[Service]
Type=simple
User=joyli
Group=joyli
WorkingDirectory=/home/joyli/multi-cs/connector
# .env 由 app/core/config.py 自己載入，這裡不重複用 EnvironmentFile
Environment=PYTHONPATH=/home/joyli/multi-cs/connector
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/joyli/multi-cs/venv/bin/uvicorn main:app \
    --host 127.0.0.1 --port 8010 --workers 1 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
StandardOutput=append:/home/joyli/multi-cs/logs/connector.log
StandardError=append:/home/joyli/multi-cs/logs/connector.log

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/multics-crm.service` 只改四處：
`Description`、`WorkingDirectory=/home/joyli/multi-cs/crm`、
`Environment=PYTHONPATH=/home/joyli/multi-cs/crm`、`--port 8020`、log 檔名改成 `crm.log`。

```bash
# ★ 服務以 joyli 身分跑，所以 log 放在自己家目錄（別放 /var/log，會寫不進去）
mkdir -p ~/multi-cs/logs
touch ~/multi-cs/logs/connector.log ~/multi-cs/logs/crm.log

sudo systemctl daemon-reload
sudo systemctl enable --now multics-connector multics-crm
sudo systemctl status multics-connector --no-pager
```

> ⚠️ **log 千萬別放 `/var/log`**：服務是 `User=joyli`，`/var/log` 屬 root，
> `append:` 打不開 → systemd 啟動失敗，錯誤訊息還是 `status=209/STDOUT`，很難懂。

看 log 最快的方式（兩條服務都在裡面）：

```bash
tail -f ~/multi-cs/logs/connector.log
tail -f ~/multi-cs/logs/crm.log
# 或走 systemd
journalctl -u multics-connector -n 50 --no-pager
```

> **不要加 `--reload`**（吃資源且會重複起 worker）。
> **不要用 gunicorn 多 worker**（坑 1）。

### 為什麼不是「多 worker 會重複回覆」（這句常見，但不準確）

實測過：`crm_outbound_task` 與 `crm_ai_inbound_queue` 的認領都走
`FOR UPDATE SKIP LOCKED`（`crm/app/internal/repo.py` 的 `_CLAIM_SQL` 與
`claim_ai_inbound`），同一筆只會被一個進程拿到；再加 `connector_outbound_attempt`
的 `UNIQUE(crm_task_id, sub_seq)` 子項冪等、超時回收閾值（出站 300s / AI 600s）
遠大於單次處理時間，不會誤判回收。

所以拒絕多 worker 的真正理由是**重複幹活與資源放大**，不是資料錯亂：

| 每個 worker 各跑一份 | 後果 |
|---|---|
| `_post_sync_loop`（5 分鐘 × 8 篇） | `/feed`、`/media` 調用 ×N → Meta rate limit |
| `_comment_sync_loop`（5 分鐘 × 3 篇） | `/{post}/comments` 調用 ×N |
| `_post_media_loop`（6 小時） | 縮圖續命重複下載 |
| `_account_loop`（60 秒） | 反向打 CRM 內網 API ×N |
| `_cache_loop`（1 小時） | 多進程同時刪同一目錄 → 刷 `FileNotFoundError` 日誌 |
| `_alert_loop`（5 分鐘） | 有 `dedupe_key` 保護，不會重複寄信（唯一無害的） |
| `deepseek_client._GATE` | `Semaphore(2)` 是**進程內** → 3 worker = 實際併發 6 |
| `fastembed` 模型 | 每個進程獨立載入 → 3 worker ≈ 1.9GB 常駐 |

**真的要提吞吐**：瓶頸在 AI 長任務時，正確做法不是加 worker，而是拆進程 ——
`WORKER_ENABLED=false` 起 N 個純 web 進程（它們只服務請求、不跑循環），
再單獨起 1 個跑循環的進程。⚠️ 這需要一個獨立入口腳本，目前**沒有**
（`worker_runner.start()` 只被 `main.py` 的 lifespan 呼叫），要做得先寫一支
`worker_main.py`。Docker / K8s 同理：`replicas` 必須是 1。

**驗證方式**：N 個 worker 會在日誌裡打 N 次「Connector worker 啟動」／
「AI 佇列消費者啟動」。看到 2 次以上就是設錯了。

---

## 6. nginx

### 6.1 網域與憑證：已經有了，不用買

實測確認（2026-10-22，從外部探測）：

| 項目 | 實際情況 |
|---|---|
| `media1.plexusai.app` | A 記錄 → `43.103.3.152`（就是這台） |
| `media2.plexusai.app` | 同一 IP |
| HTTPS | ✅ 可用，`curl` 不需 `-k`（憑證受信任） |
| 憑證 | Let's Encrypt，`CN=media1.plexusai.app` |
| SAN | `media1.plexusai.app`, `media2.plexusai.app` |
| 有效期 | 2026-10-22 → **2027-01-20** |
| 現有內容 | `<h1>media1 home</h1>` / `<h1>media2 home</h1>`（佔位頁，nginx 1.18.0） |
| 其他子域 | `hook` / `cs` / `crm` / `api` 等**全部不存在** |

**推論**：這兩個域名就是給這兩個服務用的，而且對應關係正好是
**media1 → connector、media2 → crm**（跟主管說的一致，也跟 `~/media1` `~/media2` 目錄名對上）。

所以拿掉「買域名」這一步，改成把現有的佔位設定換成反向代理：

```
media1.plexusai.app ──> 127.0.0.1:8010 (connector)
                        /webhook  /wechat  /whatsapp/webhook  /files/  /health
                        /internal/** → 403

media2.plexusai.app ──> 127.0.0.1:8020 (crm)
                        /  /login  /admin  /agent  /ops  /console/**  /media/**  /files/**
                        /internal/** → 403
```

這樣 `PUBLIC_BASE_URL=https://media1.plexusai.app` 正好能讓 Meta 抓到
`https://media1.plexusai.app/files/<media_key>`，知識庫圖片走
`https://media2.plexusai.app/media/<sha16>.png`。

### 6.2 ★ 現有配置實測（已讀到，不用猜）

`media1.plexusai.app` 的完整內容（735 bytes，`root:root 644`，所以 **不用 sudo 也讀得到**）：

```nginx
server {
    if ($host = media1.plexusai.app) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    listen 80;
    listen [::]:80;
    server_name media1.plexusai.app;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name media1.plexusai.app;
    ssl_certificate /etc/letsencrypt/live/media1.plexusai.app/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/media1.plexusai.app/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    location / {
        root /home/joyli/media1;
        index index.html;
        try_files $uri $uri/ =404;
    }
}
```

**三個關鍵事實**：

1. **它是靜態檔案服務**（`root /home/joyli/media1`），不是反向代理 → 要改的就是這一行所在的 `location`
2. **Certbot 管理的**（`# managed by Certbot`）→ 保留 `ssl_*` 那幾行不動，`certbot renew` 只改憑證路徑，不會破壞 `location`
3. **`~/media1` 的 owner 是 `joyli:www-data`** → 這是「nginx 直接讀檔」的設計。改成 proxy 後 nginx 不再讀檔，
   但也不需要收回權限（留著無害）

### 6.3 改法：只換 `location`，其餘不動

**差異只有這一段**（media1）：

```diff
-    location / {
-        root /home/joyli/media1;
-        index index.html;
-        try_files $uri $uri/ =404;
-    }
+    location /internal/ { return 403; }
+
+    location ~ ^/(wechat|webhook|whatsapp/webhook)(/|$) {
+        proxy_pass http://127.0.0.1:8010;
+        proxy_set_header Host              $host;
+        proxy_set_header X-Real-IP         $remote_addr;
+        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
+        proxy_set_header X-Forwarded-Proto $scheme;
+        proxy_read_timeout 60s;
+    }
+
+    location /files/ {
+        proxy_pass http://127.0.0.1:8010;
+        proxy_set_header Host $host;
+        expires 7d;
+    }
+
+    location = /health { proxy_pass http://127.0.0.1:8010; }
+
+    location / { return 404; }
```

**media2 同理**（也已實測：跟 media1 一樣是 `root /home/joyli/media2` 的靜態服務，
憑證同樣指向 `live/media1.plexusai.app/`），但它是整站工作台，所以 `location /`
直接 `proxy_pass http://127.0.0.1:8020`：

```diff
-    location / {
-        root /home/joyli/media2;
-        index index.html;
-        try_files $uri $uri/ =404;
-    }
+    location /internal/ { return 403; }
+
+    location / {
+        proxy_pass http://127.0.0.1:8020;
+        proxy_set_header Host              $host;
+        proxy_set_header X-Real-IP         $remote_addr;
+        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
+        proxy_set_header X-Forwarded-Proto $scheme;
+        proxy_read_timeout 120s;
+    }
+
+    location = /health { proxy_pass http://127.0.0.1:8020; access_log off; }
```

兩份完整檔案在專案裡，拿到 sudo 後直接整份取代（保留 Certbot 的註解行）：

- `scripts/nginx/media1.plexusai.app.conf`
- `scripts/nginx/media2.plexusai.app.conf`

> ★ **憑證路徑已實測確認**：`media2` 原檔就寫 `/etc/letsencrypt/live/media1.plexusai.app/`
> （同一張證、SAN 涵蓋兩域）→ **照抄，不要改成 media2 的路徑**（那個目錄不存在）。
>
> ★ 原配置是 `listen 443 ssl;`（沒有 `http2`）→ 模板也照原樣不加，避免行為改變。

### 6.4 套用手順

#### 方法 A：一鍵腳本（建議，自帶備份與回滾）

`scripts/nginx/apply_nginx.sh` 把下面所有步驟包起來，並且**失敗自動回滾**：

```bash
# ① 把四個檔案傳上去（在你自己的 Windows 上執行）
cd C:\Users\its-admin\Desktop\Multi-CS
scp scripts\nginx\* joyli@43.103.3.152:~/nginx/

# ② 用 sudo 跑（joyli 沒有 sudo 的話請 admin 代跑）
cd ~/nginx && sudo bash apply_nginx.sh
```

腳本會做：前置檢查 → 備份（帶時間戳）→ 寫入 → `nginx -t` → **失敗自動還原原檔並退出** →
通過才 `reload` → 驗證 media1/media2/voice 三個域的狀態。

★ 安全設計：**先檢查套用前的 `nginx -t` 是否通過**。若基準就壞的，直接中止，
不會讓「本來就有的問題」被誤認為是這次改動造成的。
★ 只碰 `media1` / `media2`，`default` 與 `voice.plexusai.app` 只做存在性提示、不寫入。

#### 方法 B：手動

```bash
# ① 備份（★ 一定先做，出錯才好還原）
sudo cp /etc/nginx/sites-available/media1.plexusai.app{,.bak.$(date +%F_%H%M)}
sudo cp /etc/nginx/sites-available/media2.plexusai.app{,.bak.$(date +%F_%H%M)}

# ② 寫入（檔案從 Windows scp 到 ~/nginx/）
sudo cp ~/nginx/media1.plexusai.app.conf /etc/nginx/sites-available/media1.plexusai.app
sudo cp ~/nginx/media2.plexusai.app.conf /etc/nginx/sites-available/media2.plexusai.app

# ③ 一定要先 -t 再 reload（-t 失敗時 nginx 不會動，是安全的）
sudo nginx -t

# ④ 通過才重載
sudo systemctl reload nginx

# ⑤ 驗證
curl -s -o /dev/null -w 'media1 /        -> %{http_code}\n' https://media1.plexusai.app/
#   服務沒起時 502；起了之後 404（這個域只服務 webhook 與 /files）
curl -s -o /dev/null -w 'media1 internal -> %{http_code}\n' https://media1.plexusai.app/internal/api/x
#   期望 403
curl -s -o /dev/null -w 'voice /         -> %{http_code}\n' https://voice.plexusai.app/
#   期望 200（別人的服務必須毫髮無傷）

# ⑥ 出事了怎麼還原
sudo cp /etc/nginx/sites-available/media1.plexusai.app.bak.* /etc/nginx/sites-available/media1.plexusai.app
sudo nginx -t && sudo systemctl reload nginx
```

> ⚠️ **`voice.plexusai.app` 絕對不要動**。實測它是活躍服務（`Voice Agent Demo`，21,610 bytes）。
> 只碰 `media1` 與 `media2` 兩個檔案，`default` 也不要動。
>
> ✅ **憑證路徑已確認**：兩個檔都指向 `/etc/letsencrypt/live/media1.plexusai.app/`
> （同一張證、SAN 涵蓋兩域）。**不要改成 `live/media2.plexusai.app/` —— 那個目錄不存在。**
>
> ⚠️ **不需要跑 `certbot`**。憑證已簽好、`certbot.timer` 已 enabled（實測下次 14:40）。

---

## 7. 切換平台的 webhook 地址

**必須先確認服務起得來再切**，切了之後本機就不要再跑（否則兩套搶同一個 webhook）。

### Meta（FB / IG / WhatsApp）
1. 開發者後台 → 你的 App → Webhooks → Callback URL 改成 `https://media1.plexusai.app/webhook`
   （或 `/whatsapp/webhook`，兩條路由等價，同一支 handler 依 `object` 分流）
2. Verify Token 填 `connector/.env` 的 `META_VERIFY_TOKEN`（現在是 `123...` 那串）→ 按驗證
3. 訂閱欄位確認仍有 `messages`、`messaging_postbacks`、`feed`（粉專頁面要含 `feed`）
4. 若是「每商戶自己的 App」，每個 App 都要各自改一次（驗簽是按商戶 App Secret 試的）

### 微信公眾號
後台 → 設置與開發 → 基本配置 → 伺服器配置：
- URL：`https://media1.plexusai.app/wechat`（或 `http://media1.plexusai.app/wechat`，微信支援 80）
- Token：`WECHAT_TOKEN`
- EncodingAESKey：`WECHAT_ENCODING_AES_KEY`（安全模式）

---

## 8. 驗證清單（依序跑）

```bash
# ① 兩個服務活著
curl -s https://media1.plexusai.app/health      # {"ok":true,"service":"connector"}
curl -s https://media2.plexusai.app/health        # {"ok":true,"service":"crm","pid":...,"started_at":...}

# ② 內網接口被封
curl -s -o /dev/null -w '%{http_code}\n' https://media1.plexusai.app/internal/api/conversations   # 403

# ③ Meta 驗證（用你的 META_VERIFY_TOKEN 取代）
curl -s "https://media1.plexusai.app/webhook?hub.mode=subscribe&hub.verify_token=<TOKEN>&hub.challenge=12345"
# 期望：純文字 12345（回 403 = token 不對）

# ④ 簽章機制真的開了（故意不帶簽章打一個 POST，期望 403 而不是 200）
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://media1.plexusai.app/webhook \
  -H 'Content-Type: application/json' -d '{"object":"page","entry":[]}'

# ⑤ 資料庫連得上、資料在
psql -h 127.0.0.1 -U joyli -d platform_forwarder -c "select platform, count(*) from igfb_dm group by 1;"
psql -h 127.0.0.1 -U joyli -d crm_db -c "select id, business_name from crm_business;"

# ⑥ 模型與檢索（CRM 起來後打一次知識庫「命中測試」頁最直觀）
psql -h 127.0.0.1 -U joyli -d crm_db -c \
  "select count(*) filter (where embedding is not null) as done, count(*) from kb_chunk;"

# ⑦ media1 / media2 檔案真的在，而且 DB 指得到
echo "--- media1（connector）---"
ls /home/joyli/media1/kb_cache | wc -l        # 知識庫圖片快取
ls /home/joyli/media1/post_cache | wc -l      # 貼文縮圖
echo "--- media2（crm）---"
ls /home/joyli/media2/kb | wc -l              # 期望 121（19 + 102）
ls /home/joyli/media2/kb_assets | wc -l       # 期望 9

# ⑧ kb_asset.rel_path 每一筆都能落到實際檔案（7 筆全中才算對）
psql -h 127.0.0.1 -U joyli -d crm_db -t -A -c "select rel_path from kb_asset" \
| while read -r r; do [ -f "/home/joyli/media2/$r" ] || echo "MISSING asset: /home/joyli/media2/$r"; done
# 沒有輸出 = 全部對得上

# ⑨ source_path 已修（不該有 C: 開頭），且源檔都在
psql -h 127.0.0.1 -U joyli -d crm_db -t -A -c \
  "select count(*) from kb_document where source_path like 'C:%';"   # 期望 0
psql -h 127.0.0.1 -U joyli -d crm_db -t -A -c \
  "select source_path from kb_document where source_path is not null" \
| while read -r p; do [ -f "$p" ] || echo "MISSING doc: $p"; done
```

跑得起來之後，用**真機**再過一輪（這些只有真流量能驗）：

- [ ] IG / FB 私訊一則 → 工作台 20 秒內出現、名字正常、AI 有回
- [ ] 貼文留言一則 → 留言區出現、能回覆（回覆成功且 `replied=true`）
- [ ] 管理員端 AI 開關改一下 → 客服端顯示同步（不能一邊開一邊關）
- [ ] 知識庫「命中測試」→ 用原本會命中的問句，分數跟本機接近（差距大 = 模型沒載對）
- [ ] 圖片：知識庫帶圖回覆 → Meta 端真的收到圖（`/files` 公網可達才會成功）
- [ ] 微信發一則訊息 → 入庫

> 現成的自檢腳本可以直接指到新環境跑（在 `/home/joyli/multi-cs` 下）：
> `python crm/tests/pages_check.py https://media2.plexusai.app`
> 其餘 `crm/tests/*_check.py` 多半寫死 `127.0.0.1` 與本機帳密，要改參數或改跑在伺服器上。

---

## 9. 回滾

1. Meta 後台把 Callback URL 改回 ngrok 那個（`https://slam-shivering-revival.ngrok-free.dev/webhook`）
2. 微信後台改回原 URL
3. 本機 `start_connector.bat` / `start_crm.bat` 起回來
4. 本機 DB 沒動過（dump 是唯讀），歷史資料不受影響

切換窗口內新伺服器收到的訊息不會自動回本機 —— 若在意這幾分鐘的資料，
切之前先停本機服務、切之後確認新機正常，再決定要不要把新機資料反向 dump 回來。

---

## 10. 上線後該做的事（不在部署範圍，但別忘）

- **備份**：`pg_dump` 兩庫 + `rsync /home/joyli/media1 /home/joyli/media2`，每天一次；
  `media1` 是客戶私訊與媒體，`media2` 是知識庫源文件與圖片 —— **兩個都不能丟**
- **日誌輪替**：`/etc/logrotate.d/multics`，別讓 `append:` 的 log 長到把磁碟吃滿
- **監控**：`/health` 兩個端點掛 uptime 監測；connector 掛了 = 所有渠道靜默不收訊
- **`META_VERIFY_SIGNATURE=true` 之後**，若某個商戶用自己的 App 而 webhook 進不來，
  先查 `_candidate_app_secrets()` 那條路徑 —— 症狀會很像「Meta 沒推」
- **搬完或改過 .env 之後，重跑一次核對**：
  `python /home/joyli/multi-cs/scripts/check_media_setup.py --root /home/joyli/multi-cs`（期望 25/25）

---

## 11. ★ 更新伺服器上的程式碼（日常迭代用）

### 11.1 為什麼不能直接用 `scp -r` 覆蓋

`scp -r` 只會覆蓋同名檔案，**不會刪除伺服器上已被刪掉的檔案**。實際會咬人的情境：

- 你把 `old_module.py` 刪了或改名 → 伺服器上還留著 → 某處 `import old_module` 意外成功
- 你把 `repo.py` 拆成 `repo/` 套件 → 舊的 `repo.py` 仍存在 → Python 可能優先載入舊檔
- 症狀通常是「**明明改好了卻沒生效**」，而且看 diff 完全看不出問題

所以用 `scripts/deploy_update.sh`，它會**比對檔案清單並列出殘留舊檔**。

### 11.2 一鍵更新

```powershell
# Windows，專案根目錄
cd C:\Users\its-admin\Desktop\Multi-CS

# ① 演練（只打包，不傳不重啟）—— 建議每次都先跑
bash scripts/deploy_update.sh --dry-run

# ② 同步程式碼（不重啟）
bash scripts/deploy_update.sh

# ③ 同步 + 重啟 + 健康檢查（一條搞定）
bash scripts/deploy_update.sh --restart
```

> ★ 若 `bash` 不在 PATH（Git for Windows 只把 `cmd\` 加進 PATH，不含 `bin\bash.exe`）：
> ```powershell
> & "C:\Program Files\Git\bin\bash.exe" scripts/deploy_update.sh --restart
> ```
> 或永久加進用戶 PATH：`$env:Path += ";C:\Program Files\Git\bin"`

腳本做的事：

| 步驟 | 內容 |
|---|---|
| 1 | 打包 `connector` `crm` `scripts` `docs`（**排除 `.env` / `__pycache__` / `*.pyc` / `media` / `models` / `venv` / `logs`**） |
| 2 | 打包後**自我檢查**：若封包裡出現 `.env` 立即中止（憑證不能上傳覆蓋） |
| 3 | scp 到 `/tmp/` |
| 4 | 伺服器端：**先備份現有程式碼** → 解壓覆蓋 → 比對殘留舊檔 → 清 `__pycache__` → `compileall` 語法檢查 |
| 5 | **`requirements.txt` 變更偵測**：變了就提示要先 `pip install`，並列出差異 |
| 6 | 列出「該刪但沒刪」的檔案清單（**不會自動刪**，要你確認） |
| 7 | 有 `--restart` 才重啟；**若 requirements 變了會拒絕重啟**（先裝依賴，否則必 `ModuleNotFoundError`） |

**它不做的事**（要自己處理）：

| 情況 | 要另外做 |
|---|---|
| 改了 `requirements.txt` | `venv/bin/pip install -r crm/requirements.txt` |
| 改了 nginx 設定 | `sudo bash ~/nginx/apply_nginx.sh` |
| 改了 systemd unit | `sudo bash ~/multi-cs/scripts/install_services.sh` |
| 改了資料庫 schema | 先 `pg_dump` 備份（§11.7） |
| 新增/修改媒體檔案 | 只傳對應檔案，別整包 `scp -r media` |

### 11.3 有殘留舊檔時怎麼處理

腳本會印出清單並給指令：

```
[WARN] 有 2 個檔案在伺服器上存在、但新版本已移除：
         crm/app/old_repo.py
         connector/app/legacy.py
[WARN] 確認清單沒問題後，執行下面這行刪除它們：
         cd /home/joyli/multi-cs && xargs -a /tmp/_to_delete.txt rm -f && echo 已刪除
```

**先看一眼清單**再執行。若清單裡有你不確定的檔案，先別刪，去問清楚它是不是真的該移除。

### 11.4 重啟讓新程式生效

更新檔案**不會**自動生效 —— uvicorn 沒開 `--reload`。

**方式 A：讓腳本代勞**（推薦）

```powershell
bash scripts/deploy_update.sh --restart
```

它會重啟、等 4 秒、檢查兩個服務 `active`、打兩個 `/health`、印最近 8 行 log。

**方式 B：手動**

```bash
# 伺服器上
sudo systemctl restart multics-connector multics-crm
systemctl is-active multics-connector multics-crm

# 確認活著
curl -s http://127.0.0.1:8010/health
curl -s http://127.0.0.1:8020/health

# 看有沒有啟動錯誤
tail -50 ~/multi-cs/logs/connector.log
tail -50 ~/multi-cs/logs/crm.log
```

> ⚠️ 只改 `.env` 也要重啟。但改**資料庫裡**的設定（AI 開關、知識庫參數）不用重啟，
> 程式有 5s TTL 快取會自己重讀。
>
> ⚠️ **改了 `requirements.txt` 卻沒 `pip install` 就重啟 → 必炸**（`ModuleNotFoundError`）。
> 腳本偵測到 requirements 變更時會**拒絕 `--restart`** 並告訴你該裝什麼。

### 11.5 回滾（程式碼出問題時）

每次更新都會留一份備份在上一層目錄：

```bash
cd ~ && ls -lh code_backup_*.tar.gz        # 找出要回滾的版本
tar -xzf code_backup_20261022_153000.tar.gz -C ~/multi-cs
sudo systemctl restart multics-connector multics-crm
```

備份**不含** `.env` / `media` / `models` / `venv`，所以回滾不會動到憑證和資料。

### 11.6 只改了某幾個檔案時

不想整包傳，只想覆蓋單一檔案（例如只改了 `crm/ui/console_ui.html`）：

```powershell
scp crm\ui\console_ui.html joyli@43.103.3.152:/home/joyli/multi-cs/crm/ui/
```

```bash
# 伺服器：UI 是每請求重讀，HTML/JS 不用重啟
# 但若改的是 .py，一定要重啟
sudo systemctl restart multics-crm
```

> ★ UI 檔案（`.html` / `.js` / `.css`）由 `main._ui()` 每請求重讀 → **不用重啟**。
> 但新增的 JS 必須加進 `main.py` 的 `UI_ASSETS`，否則 404 → 整頁空白。這條改的是 `.py`，要重啟。

### 11.7 更新資料庫結構時要另外處理

若這次更新包含 **schema 變更**（新增表／欄位／索引）：

1. 程式碼裡 `crm/app/core/schema.py` 的 DDL 會在服務啟動時自動補（新建表、加欄位），
   但**索引只能寫在 MIGRATIONS**
2. 更嚴謹的做法：先在 staging 庫跑一次、確認無誤再上生產
3. **更新前先備份資料庫**：

```bash
# 伺服器上
pg_dump -h 127.0.0.1 -U joyli -Fc -d crm_db             -f ~/backup_crm_$(date +%F_%H%M).dump
pg_dump -h 127.0.0.1 -U joyli -Fc -d platform_forwarder -f ~/backup_pf_$(date +%F_%H%M).dump
```
