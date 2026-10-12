"""Instagram 登入路徑權杖（IGAA）自動續期。

IG 的長效權杖有效期 60 天，可在到期前用 refresh_access_token 換一顆新的。
本模組在服務啟動時續期一次，之後每 24 小時續一次，並把新權杖寫回 .env，
因此正常運作下不會再遇到 IG 私訊突然失效。
"""
import asyncio
import logging
from pathlib import Path

import httpx

from app.core import config

log = logging.getLogger("wechat-forwarder")

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_REFRESH_INTERVAL = 24 * 3600  # 每天續一次（權杖效期 60 天，餘裕充足）


def persist_env(key: str, value: str) -> None:
    """把單一環境變數寫回 .env，保留其他行與註解。"""
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    out, found = [], False
    for ln in lines:
        if ln.startswith(key + "="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


async def refresh_ig_token() -> dict:
    """續期一次 IG 權杖；成功時同步更新記憶體與 .env（立即生效，不必重啟）。"""
    token = config.META_IG_ACCESS_TOKEN
    if not token:
        return {"ok": False, "error": "尚未設定 IG 權杖"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{config.ig_graph_host()}/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
            )
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    new_token = (data or {}).get("access_token")
    if r.status_code != 200 or not new_token:
        return {"ok": False, "status": r.status_code, "error": str(data)[:200]}

    config.META_IG_ACCESS_TOKEN = new_token
    persist_env("META_IG_ACCESS_TOKEN", new_token)
    days = round((data.get("expires_in") or 0) / 86400, 1)
    log.info("IG 權杖已續期，效期約 %s 天", days)
    return {"ok": True, "days": days}


async def ig_token_loop() -> None:
    """啟動時續期一次，之後每 24 小時續一次；失敗只記 log，不影響服務。"""
    while True:
        try:
            res = await refresh_ig_token()
            if not res.get("ok"):
                log.warning("IG 權杖續期失敗（24 小時後重試）：%s", res.get("error"))
        except Exception as e:
            log.warning("IG 權杖續期異常：%s", e)
        await asyncio.sleep(_REFRESH_INTERVAL)
