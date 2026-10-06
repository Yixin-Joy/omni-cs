# WhatsApp 回覆接口說明

## 概述

貴司系統收到 WhatsApp 用戶消息後，若要回覆用戶，請向以下接口發送報文。中轉服務器會將報文轉換為 WhatsApp 格式，發送到對應用戶的聊天窗口。

## 發送地址

- **方法**：HTTP POST
- **地址**：`https://slam-shivering-revival.ngrok-free.dev/whatsapp/reply`
- **Content-Type**：`application/json`

> 註：此為測試環境地址，正式上線會改為固定域名，屆時另行通知。

## 報文格式

### 文字回覆

```json
{
  "to": "85269527578",
  "messageType": "text",
  "messageContent": "您好，已收到您的來訊"
}
```

### 文件 / 圖片回覆

```json
{
  "to": "85269527578",
  "messageType": "file",
  "uploadedFiles": [
    {
      "url": "https://your-server.com/files/reply.pdf",
      "filename": "回覆.pdf",
      "mimeType": "application/pdf"
    }
  ]
}
```

## 字段說明

| 字段 | 類型 | 必填 | 說明 |
|---|---|---|---|
| `to` | string | 是 | 回覆對象的用戶 ID。此值 = 之前我方推送給貴司的報文中 `webClientSender.webClientUUID` 的值（即 WhatsApp 用戶的 wa_id）。 |
| `messageType` | string | 是 | 消息類型，`text` 或 `file`。 |
| `messageContent` | string | 否 | 文字內容，`messageType=text` 時必填。 |
| `uploadedFiles` | array | 否 | 文件列表，`messageType=file` 時必填，目前取第一個元素。 |
| `uploadedFiles[].url` | string | 是 | 文件的可下載地址（需公網可訪問，中轉服務器會先下載再發送）。 |
| `uploadedFiles[].filename` | string | 否 | 文件名。 |
| `uploadedFiles[].mimeType` | string | 否 | 文件 MIME 類型，`image/*` 以圖片發送，其餘以文件發送。 |

## 支援的文件類型

WhatsApp 對媒體類型有白名單限制，建議貴司回覆文件時使用下列 `mimeType`：

| 類別 | 可用 `mimeType` |
|---|---|
| 文件 | `application/pdf`、`application/msword`、`application/vnd.openxmlformats-officedocument.wordprocessingml.document`（docx）、`application/vnd.ms-excel`、`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`（xlsx）、`application/vnd.ms-powerpoint`、`application/vnd.openxmlformats-officedocument.presentationml.presentation`（pptx）、`text/plain` |
| 圖片 | `image/jpeg`、`image/png` |
| 影片 | `video/mp4`、`video/3gpp` |
| 音訊 | `audio/aac`、`audio/mp4`、`audio/mpeg`、`audio/amr`、`audio/ogg`、`audio/opus` |

> 特別說明：**CSV（`text/csv`）不在 WhatsApp 白名單內**。中轉服務器已做自動轉換
> （`text/csv`、`text/xml` 等純文字類會以 `text/plain` 送出），貴司照原樣填寫即可，不需特別處理。
> 若為白名單外且無法安全轉換的類型（例如 `.exe`），回覆會失敗並返回錯誤訊息。

## 響應

成功：

```json
{"ok": true, "status": 200}
```

失敗（HTTP 4xx / 5xx）：

```json
{"ok": false, "error": "錯誤描述"}
```

## 完整示例

用戶 `85269527578` 之前發來訊息，我方推送時 `webClientSender.webClientUUID` 為 `85269527578`。貴司回覆時：

```bash
curl -X POST https://slam-shivering-revival.ngrok-free.dev/whatsapp/reply \
  -H "Content-Type: application/json" \
  -d '{"to": "85269527578", "messageType": "text", "messageContent": "您好，已收到您的文件"}'
```

## 注意事項

- `to` 必須精確等於我方推送報文中的 `webClientSender.webClientUUID`，否則無法找到對應用戶。
- 文件回覆的 `url` 需為公網可訪問地址；中轉服務器收到後會即時下載，建議該 url 保留一定時間。
- 此接口目前僅支援 WhatsApp 渠道。
