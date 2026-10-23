# Multi-CS 部署 —— 命令清單（供主管審閱）

> **目的**：這份文件列出部署 Multi-CS（客服系統）到公司伺服器時，**所有會修改系統的指令**，
> 以及每一條改了什麼、能不能退回。純唯讀的檢查指令列在最後一節，不改任何東西。
>
> **部署位置**：`43.103.3.152`（阿里雲 ECS / Ubuntu 22.04.5）
> **執行身分**：`joyli`（目前無 sudo，需授權）
> **對外域名**：`media1.plexusai.app` → connector(8010)、`media2.plexusai.app` → crm(8020)
> **完整部署文檔**：`docs/DEPLOY.md`
>
> **一句話總結**：只做三件事 —— 裝 PostgreSQL、建兩個服務、把兩個佔位頁改成反向代理。
> **不改** `default`、**不改** `voice.plexusai.app`（別人的活躍服務）、**不重啟**既有服務。
>
> ⚠️ **唯一會讓線上服務短暫受影響的動作**：階段 6 的 `systemctl reload nginx`。
> 這是平滑重載（不中斷既有連線），且失敗會自動還原。
> 不會重啟整台機器，不需要 `reboot`。

---

## 一、總覽

| 階段 | 做什麼 | 需 sudo | 修改的系統位置 | 對線上影響 | 可回退 |
|---|---|---|---|---|---|
| 1 | 安裝系統套件 | ✅ | `/usr`, `/var/lib/dpkg` | 無 | 可（`apt remove`） |
| 2 | 安裝 PostgreSQL 18 + 建兩個庫 | ✅ | `/var/lib/postgresql` | 無 | 可（`dropdb`） |
| 3 | 建立媒體子目錄 | ❌ | `~/media1`, `~/media2` | 無 | 是 |
| 4 | 建立 Python 虛擬環境 | ❌ | `~/multi-cs/venv` | 無 | 是 |
| 5 | 建立兩個 systemd 服務 | ✅ | `/etc/systemd/system/` | 無（新服務，只綁 127.0.0.1） | 是（`disable`） |
| 6 | 修改兩個 nginx 設定 | ✅ | `/etc/nginx/sites-available/` | **reload 一次（平滑）** | 是（自動備份） |
| 7 | 驗收 | ❌ | 無 | 無 | — |

**需要 sudo 的是階段 1、2、5、6**；階段 3、4、7 全部在家目錄內完成。

### 兩種執行方式

**方式 A：授權 `joyli` 執行**（建議）

只需把 `joyli` 加入 sudo 群組，或授權上述命令。之後所有操作可自行完成。

**方式 B：主管／admin 代跑**

所有需要的檔案都在 `~/`（家目錄，不需要權限即可寫入），由 `joyli` 準備好，
再由 admin 用自己帳號執行。建議按這個順序：

```bash
# 1) 先看要改的 nginx 設定（純文字檔，可先 full read）
cat ~/nginx/media1.plexusai.app.conf
cat ~/nginx/media2.plexusai.app.conf

# 2) 看套用腳本（含備份與自動回滾邏輯）
cat ~/nginx/apply_nginx.sh

# 3) 確認沒問題才執行（腳本自己會做 nginx -t，失敗自動還原）
sudo bash ~/nginx/apply_nginx.sh
```

系統套件與 PostgreSQL 的安裝，則按本文件階段 1、2 的指令逐條執行即可。

---

## 二、階段 1：安裝系統套件

```bash
# 1-1 更新套件索引
sudo apt-get update

# 1-2 安裝 Python 虛擬環境支援（唯一必要的 apt 包）
sudo apt-get install -y python3-venv

# 1-3 加入 PostgreSQL 官方套件庫（阿里雲鏡像），用於安裝 18 版
curl -fsSL https://mirrors.aliyun.com/postgresql/repos/apt/ACCC4CF8.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/pgdg.gpg

sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null <<'EOF'
deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://mirrors.aliyun.com/postgresql/repos/apt jammy-pgdg main
EOF

# 1-4 安裝 PostgreSQL 18
sudo apt-get update
sudo apt-get install -y postgresql-18
```

| 命令 | 做什麼 | 影響 |
|---|---|---|
| 1-1 | 刷新 apt 套件索引 | 只更新 `/var/lib/apt/lists` |
| 1-2 | 裝 Python venv 支援 | 新增 1 個套件；**不動任何既有服務** |
| 1-3 | 新增一個 apt 來源與 GPG 金鑰 | 只新增 2 個檔案 |
| 1-4 | 安裝 PostgreSQL 18 | 新增服務，監聽 `127.0.0.1:5432` |

> **為什麼只裝 `python3-venv`**：其餘依賴（fastapi、onnxruntime、pillow 等）
> 都有 Linux 預編譯 wheel，`pip` 直接下載解壓即可，不需要編譯工具鏈。
> 若真的遇到需要編譯的包，再補 `build-essential python3-dev`，但目前評估不需要。
>
> **為什麼要 PG 18**：本機（開發機）是 PostgreSQL 18.6，備份檔是新版格式，
> Ubuntu 22.04 預設的 14 版無法讀取（會報 `unsupported version in file header`）。
> 不是為了用新功能，純粹是版本相容。
>
> **不會做的事**：不移除系統既有的套件、不升級其他套件、不改 `apt` 預設來源。

---

## 三、階段 2：建立資料庫

```bash
# 2-1 建立資料庫角色（密碼另行設定，不寫在文件裡）
sudo -u postgres psql -c "CREATE ROLE joyli LOGIN PASSWORD '<自訂密碼>';"

# 2-2 建立兩個資料庫
sudo -u postgres createdb -O joyli platform_forwarder
sudo -u postgres createdb -O joyli crm_db

# 2-3 還原備份（從開發機匯出的資料）
pg_restore -h 127.0.0.1 -U joyli -d platform_forwarder --no-owner --no-privileges ~/multi-cs/pf.dump
pg_restore -h 127.0.0.1 -U joyli -d crm_db             --no-owner --no-privileges ~/multi-cs/crm.dump

# 2-4 設定時區（讓「每日上限」以台灣時間歸零，而非 UTC 08:00）
sudo -u postgres psql -c "ALTER DATABASE platform_forwarder SET timezone='Asia/Taipei';"
sudo -u postgres psql -c "ALTER DATABASE crm_db             SET timezone='Asia/Taipei';"
```

| 命令 | 做什麼 | 影響 |
|---|---|---|
| 2-1 | 建立 `joyli` 資料庫帳號 | 新增一個 PG 角色 |
| 2-2 | 建立兩個空資料庫 | 新增兩個 DB，owner 是 `joyli` |
| 2-3 | 匯入既有資料 | 只寫入這兩個新 DB |
| 2-4 | 設定這兩個 DB 的時區 | 只影響這兩個 DB |

> **不會做的事**：不建立 `postgres` 超級使用者以外的角色、不改 `pg_hba.conf`、
> 不動其他既有資料庫。兩個新庫名稱是 `platform_forwarder` 與 `crm_db`（目前都不存在）。

---

## 四、階段 3：媒體子目錄（不需要 sudo）

```bash
# 3-1 確認目錄存在（目前已存在於家目錄）
ls -ld /home/joyli/media1 /home/joyli/media2

# 3-2 建立子目錄（joyli 是 owner，有寫入權，不需 sudo）
mkdir -p /home/joyli/media1/kb_cache /home/joyli/media1/post_cache
mkdir -p /home/joyli/media2/kb /home/joyli/media2/kb_assets
```

| 命令 | 做什麼 | 影響 |
|---|---|---|
| 3-1 | 查看目錄屬性（唯讀） | 無 |
| 3-2 | 在既有目錄下建 4 個子目錄 | 只在 `media1`、`media2` 內新增空目錄 |

> **不需要 chown**：這兩個目錄目前 owner 已是 `joyli:www-data`，`joyli` 有寫入權。
> 這也是為什麼這階段不需 sudo。
>
> **不會做的事**：不刪除 `media1`、`media2` 內任何既有檔案
> （目前各有一個 `index.html` 佔位頁，改 nginx 後就不再被使用）。

---

## 五、階段 4：應用程式依賴（不需要 sudo）

```bash
# 全部在家目錄內，不涉及系統目錄
cd ~/multi-cs
python3 -m venv venv
venv/bin/pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
venv/bin/pip install -r crm/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

| 命令 | 影響 |
|---|---|
| 全部 | 只寫入 `~/multi-cs/venv/`，不影響系統 Python |

> 這階段**完全不需要 sudo**，列出來是為了讓流程完整。

---

## 六、階段 5：建立兩個 systemd 服務

### 5.0 先建 log 目錄（不需 sudo）

```bash
mkdir -p ~/multi-cs/logs
touch ~/multi-cs/logs/connector.log ~/multi-cs/logs/crm.log
```

> 服務以 `joyli` 身分執行，log 寫在自家目錄。若放 `/var/log`（root 所有），
> systemd 會啟動失敗並報 `status=209/STDOUT`，這個錯誤訊息很難懂，所以刻意避開。

### 5.1 寫入服務定義檔

```bash
# 5-1 connector 服務（對接 Meta / 微信，處理收發訊息）
sudo tee /etc/systemd/system/multics-connector.service > /dev/null <<'EOF'
[Unit]
Description=Multi-CS Connector (8010)
After=network.target postgresql.service

[Service]
Type=simple
User=joyli
Group=joyli
WorkingDirectory=/home/joyli/multi-cs/connector
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
EOF

# 5-2 crm 服務（AI 引擎 / 知識庫 / 工作台，對外 8020）
sudo tee /etc/systemd/system/multics-crm.service > /dev/null <<'EOF'
[Unit]
Description=Multi-CS CRM (8020)
After=network.target postgresql.service

[Service]
Type=simple
User=joyli
Group=joyli
WorkingDirectory=/home/joyli/multi-cs/crm
Environment=PYTHONPATH=/home/joyli/multi-cs/crm
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/joyli/multi-cs/venv/bin/uvicorn main:app \
    --host 127.0.0.1 --port 8020 --workers 1 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
StandardOutput=append:/home/joyli/multi-cs/logs/crm.log
StandardError=append:/home/joyli/multi-cs/logs/crm.log

[Install]
WantedBy=multi-user.target
EOF

# 5-3 載入並啟動
sudo systemctl daemon-reload
sudo systemctl enable --now multics-connector multics-crm

# 5-4 檢查狀態
sudo systemctl status multics-connector --no-pager
sudo systemctl status multics-crm --no-pager
```

| 命令 | 做什麼 | 影響 |
|---|---|---|
| 5-1 / 5-2 | 新增兩個服務定義檔 | 只新增 2 個檔案，不改既有服務 |
| 5-3 | 啟用並立即啟動 | 兩個新服務開始執行，開機自啟 |
| 5-4 | 查狀態（唯讀） | 無 |

**關鍵設計：兩個服務都只綁 `127.0.0.1`**

```
--host 127.0.0.1
```

→ 只能從本機存取，**不直接對外**。外部流量一律經過 nginx。
→ 安全組不需要開 8010 / 8020。

> **`--workers 1` 是刻意的**：服務內含 6 條背景迴圈（貼文同步、留言補拉、告警巡檢等），
> 多 worker 會讓每條迴圈重複執行、對外 API 呼叫與記憶體用量都變成 N 倍。
>
> **不會做的事**：不改任何既有 service、不 `systemctl daemon-reexec`、不動 `postgresql` 服務。

---

## 七、階段 6：nginx（唯一會影響既有網頁服務的步驟）

### 6.1 修改範圍

| 檔案 | 動作 |
|---|---|
| `/etc/nginx/sites-available/media1.plexusai.app` | 改（靜態頁 → 反向代理 8010） |
| `/etc/nginx/sites-available/media2.plexusai.app` | 改（靜態頁 → 反向代理 8020） |
| `/etc/nginx/sites-available/default` | **不動** |
| `/etc/nginx/sites-available/voice.plexusai.app` | **不動** |

現況（已實測）：

- `media1` / `media2` 目前是**靜態佔位頁**，各自回傳 `<h1>mediaN home</h1>`（21 bytes）
- `voice.plexusai.app` 是**活躍服務**（Voice Agent Demo，21,610 bytes），有人在用

### 6.2 具體命令

```bash
# 6-1 備份（帶時間戳，不覆蓋舊備份）
sudo cp -a /etc/nginx/sites-available/media1.plexusai.app \
            /etc/nginx/sites-available/media1.plexusai.app.bak.20261022_120000
sudo cp -a /etc/nginx/sites-available/media2.plexusai.app \
            /etc/nginx/sites-available/media2.plexusai.app.bak.20261022_120000

# 6-2 寫入新設定
sudo cp ~/nginx/media1.plexusai.app.conf /etc/nginx/sites-available/media1.plexusai.app
sudo cp ~/nginx/media2.plexusai.app.conf /etc/nginx/sites-available/media2.plexusai.app

# 6-3 語法檢查（不改執行狀態；失敗就不會繼續）
sudo nginx -t

# 6-4 平滑重載（既有連線不斷）
sudo systemctl reload nginx
```

> 上面四步已包成一個腳本 `~/nginx/apply_nginx.sh`，可先審閱內容再執行：
> `sudo bash ~/nginx/apply_nginx.sh`

### 6.3 改動的內容

**media1.plexusai.app → connector (8010)**

```diff
     ssl_certificate /etc/letsencrypt/live/media1.plexusai.app/fullchain.pem; # managed by Certbot
     ssl_certificate_key /etc/letsencrypt/live/media1.plexusai.app/privkey.pem; # managed by Certbot
     include /etc/letsencrypt/options-ssl-nginx.conf;
     ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
-    location / {
-        root /home/joyli/media1;
-        index index.html;
-        try_files $uri $uri/ =404;
-    }
+    client_max_body_size 64m;
+
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

**media2.plexusai.app → crm (8020)** —— 同樣只換 `location`：

```diff
     ssl_certificate /etc/letsencrypt/live/media1.plexusai.app/fullchain.pem; # managed by Certbot
     ssl_certificate_key /etc/letsencrypt/live/media1.plexusai.app/privkey.pem; # managed by Certbot
     include /etc/letsencrypt/options-ssl-nginx.conf;
     ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
-    location / {
-        root /home/joyli/media2;
-        index index.html;
-        try_files $uri $uri/ =404;
-    }
+    client_max_body_size 64m;
+
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

**兩個檔都只換 `location` 區塊。`ssl_*`、`include`、`server_name`、`listen` 全部原樣保留。**

> 註：media2 原檔的 `ssl_certificate` 就指向 `live/media1.plexusai.app/`
> —— 兩個域共用同一張證（SAN 涵蓋兩域），照原樣不動。

### 6.4 為何要封 `location /internal/`

應用程式有一套內部 API（服務之間呼叫用），只靠一組 token 認證。
若不封，任何人拿到 token 就能從公網呼叫。封掉後只有本機能呼叫。

### 6.5 證書

- 兩個域共用同一張 Let's Encrypt 憑證（SAN 涵蓋兩域），有效期至 **2027-01-20**
- `certbot.timer` 已啟用（實測下次執行 2026-10-22 14:40）→ 到期前會自動續期
- **本次不需要執行 `certbot`**，不修改任何憑證設定

---

## 八、階段 7：驗收（全部唯讀）

```bash
# 服務健康檢查
curl -s http://127.0.0.1:8010/health
curl -s http://127.0.0.1:8020/health

# 對外可達性
curl -s -o /dev/null -w '%{http_code}\n' https://media1.plexusai.app/internal/api/x   # 期望 403
curl -s -o /dev/null -w '%{http_code}\n' https://media2.plexusai.app/                # 期望 200/302
curl -s -o /dev/null -w '%{http_code}\n' https://voice.plexusai.app/                 # 期望 200（未受影響）

# 資料庫
psql -h 127.0.0.1 -U joyli -d platform_forwarder -c "select count(*) from forward_log;"
psql -h 127.0.0.1 -U joyli -d crm_db -c "select count(*) from crm_business;"
```

---

## 九、如果出問題怎麼回退

| 出問題的地方 | 回退指令 |
|---|---|
| nginx 設定寫錯 | `sudo nginx -t` 會先擋下；若已 reload 出問題：<br>`sudo cp -a /etc/nginx/sites-available/media1.plexusai.app.bak.* /etc/nginx/sites-available/media1.plexusai.app`<br>`sudo nginx -t && sudo systemctl reload nginx` |
| 服務起不來 | `sudo systemctl stop multics-connector multics-crm`<br>`sudo systemctl disable multics-connector multics-crm` |
| 資料庫要重來 | `sudo -u postgres dropdb platform_forwarder && sudo -u postgres dropdb crm_db` |
| 全部撤掉 | 停用並刪除兩個 service、還原 nginx 備份、`dropdb` 兩個庫、`apt remove postgresql-18` |

**目前線上的服務（`voice.plexusai.app`）不會被任何一步影響** ——
它用的是另一個 `.conf`，本次不修改、不重啟 nginx 以外的任何服務。

---

## 十、附：完全唯讀的盤點命令（已執行過，不會改任何東西）

```bash
id; groups
getent group sudo
awk -F: '$7 ~ /(bash|sh)$/ {print $1, $3}' /etc/passwd
ls -la /etc/nginx/sites-enabled/
ls -l  /etc/nginx/sites-available/
cat /etc/nginx/sites-available/media1.plexusai.app
cat /etc/nginx/sites-available/media2.plexusai.app
ss -lnt
systemctl is-enabled certbot.timer
systemctl list-timers 'certbot*' --no-pager
ls -ld /home/joyli/media1 /home/joyli/media2
findmnt -T /home/joyli/media1
df -h /
nvidia-smi
pg_lsclusters
```

以上都只讀取資訊、不寫入任何檔案、不改任何服務狀態。
