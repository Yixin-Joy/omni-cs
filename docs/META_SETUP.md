# Meta 後台設定 Checklist（IG/FB 私訊 + 貼文留言）

> 這份是讓本專案的 IG/FB 私訊查看/回覆、貼文留言查看/回覆功能「即時推送」跑起來，
> 你需要在 Meta 後台（developers.facebook.com）照做的步驟。
>
> 前提：你已經有這個 App，且已把粉專（page）和 IG 專業帳號連結到 App。

---

## 一、webhook 驗證（一次性）

1. 進 App 首頁 → **左側選單 → 「WhatsApp / Messaging / Instagram」任一個產品的「設定」→ Webhooks**。
   或直接：**App → 設定 → 進階 → Webhooks**（不同產品共用同一個 webhook 端點）。
2. 「回呼網址（Callback URL）」填你的對外位址 + `/webhook`，例如：
   ```
   https://你的-ngrok-或-natapp-位址/webhook
   ```
   （本專案 `main.py` 的 `/webhook` 同時接 WhatsApp / IG / FB，共用同一支。）
3. 「驗證權杖（Verify Token）」填你 `.env` 裡的 `META_VERIFY_TOKEN` 值（自行設定的任意字串，兩邊要一致）。
4. 按「驗證並儲存」。Meta 會打一次 GET `/webhook`，我們回傳 challenge，就會顯示成功。

---

## 二、訂閱 webhook 欄位

在 webhook 設定的「**Webhook 欄位（Webhook Fields）**」頁，把你需要的訂閱打開：

| 對象 | 訂閱欄位 | 對應功能 |
|---|---|---|
| **Page（粉專）** | `messages` | FB 私訊即時推送 |
| **Page（粉專）** | `feed` | FB 貼文留言即時推送 |
| **Instagram（IG 登入路徑那顆帳號）** | `messages` | IG 私訊即時推送 |
| **Instagram（IG 登入路徑那顆帳號）** | `comments` | IG 貼文留言即時推送 |

> 注意：IG 的 webhook 是「**Instagram 登入路徑**」下的那顆帳號
> （就是 `META_IG_ACCESS_TOKEN` 這支 IGAA token 對應的帳號），不是粉專連結的 IG business 帳號。

---

## 三、把粉專 / IG 帳號「訂」到 App

訂閱欄位後，還要把實際的粉專 / IG 帳號授權給這個 App，才會收到事件：

1. **粉專**：App → 使用案例 → Messenger → 設定 → 把粉專加進來。
   或到粉專設定 → 進階 → 確認 App 在「已連結的 App」清單。
2. **IG 帳號**：App → 使用案例 → Instagram → 「以 Instagram 登入設定」→ Configure Webhooks，
   勾選該 IG 帳號並**開啟它的 Webhook Subscription 開關**。

---

## 四、權限（Permission / Scopes）

你的 page token 目前已有（實測確認）：

```
pages_show_list, pages_messaging, instagram_basic, whatsapp_business_management,
instagram_manage_messages, pages_read_engagement, pages_manage_metadata,
pages_read_user_content, pages_manage_engagement, whatsapp_business_messaging,
pages_utility_messaging, public_profile
```

### 已開通、可用 ✅
- **FB 私訊**：`pages_messaging`（讀 + 回）
- **FB 貼文留言**：`pages_read_engagement` + `pages_manage_engagement`（讀 + 回）
- **IG 私訊**：`instagram_manage_messages`（讀 + 回）
- **IG 貼文 / 留言讀取**：`instagram_basic`（讀）

### 還缺、需要你去開通 ❌
- **IG 留言「回覆」**：需要 **`instagram_manage_comments`**（進階權限，要送 App Review 審核）。
  - 位置：App → **App Review → Permissions and Features** → 找 `instagram_manage_comments` → Request Advanced Access。
  - 有些第三方文件會寫成 `instagram_business_manage_comments`，是舊名/筆誤，後台就搜 `instagram_manage_comments`。
  - **這條權限只能走「Facebook 登入路徑」**（`graph.facebook.com` + page token），
    跟你 IG 私訊走的「Instagram 登入路徑」（`graph.instagram.com` + IGAA token）是兩回事。
    也就是說：IG 留言回覆用的 token 是 **page token**（`META_PAGE_ACCESS_TOKEN`），不是 IGAA 那顆。
  - 送審時 Meta 會要你附 demo 影片（展示回 IG 留言的流程）+ 說明使用情境，且 IG 帳號需是 **Business / Creator 專業帳號**並連結到粉專。
  - 審核通過後：**重新授權 page token**，讓它帶上 `instagram_manage_comments` scope。
  - 開通前：IG 貼文留言能看、能讀，但「回覆」送出會被 Meta 擋（回 403 / 權限不足，工作台會回 502）。

---

## 五、注意事項

1. **留言者姓名**：FB / IG 的貼文留言，Meta 對「陌生留言者」的公開姓名有隱私限制，
   即使有權限，`from.name` / `username` 也可能回空。本專案會 fallback 顯示「用戶」。
   這不是 bug，是平台限制；要拿到真實姓名需要 App 通過 Advanced Access 審核 + 用戶是粉專追蹤者等條件。
2. **IG 留言回覆**（`instagram_manage_comments`）開通前，工作台的 IG 貼文留言「回覆」按鈕
   仍會顯示，但送出會失敗並提示權限不足。
3. **歷史資料**：webhook 只推「設定之後」的新事件。設定完後，開工作台頁面會用 Graph API
   自動補拉歷史私訊 / 貼文 / 留言（`sync`），兩者合併顯示，不用另外處理。

---

## 六、權杖管理（最重要，權杖一失效全功能停擺）

### 症狀：怎麼判斷是權杖問題

權杖過期時，**所有 Meta 功能一起壞**，但看起來像不同的 bug：

| 症狀 | 真正原因 |
|---|---|
| 回覆留言 / 私訊回 **502** | 權杖過期（Meta 回 401，後端包成 502） |
| 按下「同步最新資料」卻拉不到新貼文 | 同上（API 失敗被吞成空資料） |
| 紅點有數字，但列表找不到對應貼文 | 同上（資料沒同步進來） |

**確認方法**：

```bash
curl -s "https://graph.facebook.com/v21.0/debug_token?input_token=<你的token>&access_token=<你的token>"
```
看 `is_valid` 是否為 `false`，或錯誤碼 `190 / error_subcode 463`（Session has expired）。

### 粉專權杖（`META_PAGE_ACCESS_TOKEN`）：要做成「永久」

**關鍵**：只有「用**長效 user token** 從 `/me/accounts` 取出來的」粉專權杖才**永不過期**
（`debug_token` 會顯示 `expires_at: 0`）。直接從 Graph API Explorer 複製的那顆是短效的（1～2 小時）。

取得步驟：

1. 到 [Graph API Explorer](https://developers.facebook.com/tools/explorer/)，選你的 App，
   勾選所需權限（`pages_show_list`、`pages_messaging`、`pages_read_engagement`、
   `pages_manage_engagement`、`pages_read_user_content`、`instagram_basic`、
   `instagram_manage_comments`、`instagram_manage_messages`、`business_management`），
   按 **Generate Access Token**（會跳出 Facebook 授權，需本人操作）。
2. 把這顆**短效** user token 換成**長效**（60 天）：
   ```bash
   curl -s "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=<APP_ID>&client_secret=<APP_SECRET>&fb_exchange_token=<短效USER_TOKEN>"
   ```
3. 用長效 user token 取粉專權杖（`data[].access_token` 即為**永久**粉專權杖）：
   ```bash
   curl -s "https://graph.facebook.com/v21.0/me/accounts?access_token=<長效USER_TOKEN>"
   ```
4. 填入 `.env`：
   - `META_USER_ACCESS_TOKEN` = 第 2 步的長效 user token
   - `META_PAGE_ACCESS_TOKEN` = 第 3 步取出的粉專權杖

### IG 登入權杖（`META_IG_ACCESS_TOKEN`）：已自動續期

IG 登入路徑的權杖效期 **60 天**，本專案**已內建自動續期**：服務啟動時續期一次，
之後每 24 小時再續一次，並自動寫回 `.env`。正常運作下不需人工處理。

也可手動觸發：`POST /console/igfb/refresh-ig-token`。

