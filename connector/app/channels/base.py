"""發送結果型別與分派。

★ 錯誤語義（決定 CRM 端要不要重試，見設計案 §11）：
- `temp_error=True`  → 確定**沒發出去**（連線失敗、平台 5xx/429）→ 可安全重試
- `uncertain=True`   → **可能已發出**（送出後逾時、回應無法解析）→ 不可重試，交人工
- 兩者皆 False       → 永久錯誤（平台 4xx、參數錯）→ 不重試
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass

from app.core import config

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@dataclass
class SendResult:
    ok: bool
    platform_msg_id: str | None = None
    error: str | None = None
    temp_error: bool = False
    uncertain: bool = False

    def as_dict(self) -> dict:
        return {"ok": self.ok, "platform_msg_id": self.platform_msg_id,
                "error": self.error, "temp_error": self.temp_error,
                "uncertain": self.uncertain}


def is_mock() -> bool:
    """★ 每次呼叫時讀（讓自檢能臨時改成 mock，不必重啟）。"""
    return (getattr(config, "OUTBOUND_SEND_MODE", "live") or "live").lower() == "mock"


def is_image(filename: str) -> bool:
    import os
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXTS:
        return True
    mime, _ = mimetypes.guess_type(filename or "")
    return bool(mime and mime.startswith("image/"))


def _module(platform: str):
    if platform == "wechat":
        from app.channels import wechat as m
        return m
    if platform == "whatsapp":
        from app.channels import whatsapp as m
        return m
    return None


async def send_text(platform: str, target_id: str, text: str) -> SendResult:
    if is_mock():
        return SendResult(ok=True, platform_msg_id=f"mock-t-{abs(hash((platform, target_id, text))) % 10**10}")
    m = _module(platform)
    if m is None:
        return SendResult(ok=False, error=f"平台 {platform} 的發送尚未搬入 Connector（階段三）")
    return await m.send_text(target_id, text)


async def send_asset(platform: str, target_id: str, media_path: str,
                     caption: str = "", filename: str = "") -> SendResult:
    if is_mock():
        return SendResult(ok=True, platform_msg_id=f"mock-a-{abs(hash((platform, target_id, media_path))) % 10**10}")
    m = _module(platform)
    if m is None:
        return SendResult(ok=False, error=f"平台 {platform} 的發送尚未搬入 Connector（階段三）")
    return await m.send_asset(target_id, media_path, caption, filename)
