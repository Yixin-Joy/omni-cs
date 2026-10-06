"""媒體檔下載：微信 media_id / WhatsApp Graph API → 本機 media/ 目錄。

訊息本身不再對外轉發（原 TARGET_WEBHOOK_URL 已移除），
下載回來的檔案由客服工作台的 /files/ 靜態路徑取用。
"""
import httpx


async def download_media(appid: str, secret: str, media_id: str, save_path: str) -> None:
    """用 media_id 換 access_token 後下載媒體檔，存到 save_path。

    注意：個人（未認證）號對 media 下載接口的權限需實測，失敗會拋出例外。
    """
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": appid, "secret": secret},
        )
        token_json = token_resp.json()
        access_token = token_json.get("access_token")
        if not access_token:
            raise RuntimeError(f"取得 access_token 失敗: {token_json}")

        media_resp = await client.get(
            "https://api.weixin.qq.com/cgi-bin/media/get",
            params={"access_token": access_token, "media_id": media_id},
        )
        media_resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(media_resp.content)


async def download_whatsapp_media(
    access_token: str, media_id: str, save_path: str, graph_api_version: str = "v22.0"
) -> None:
    """透過 Graph API 拿 WhatsApp 媒體的 url 並下載，存到 save_path。"""
    async with httpx.AsyncClient(timeout=20) as client:
        meta_resp = await client.get(
            f"https://graph.facebook.com/{graph_api_version}/{media_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        meta_resp.raise_for_status()
        url = meta_resp.json().get("url")
        if not url:
            raise RuntimeError(f"取得 WhatsApp 媒體 url 失敗: {meta_resp.json()}")
        file_resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        file_resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(file_resp.content)
