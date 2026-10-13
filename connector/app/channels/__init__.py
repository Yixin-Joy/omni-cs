"""通道發送 —— Connector 專責（設計案 §3.1.6）。

可插拔：`send_text` / `send_asset` 依 platform 分派到對應模組；
`OUTBOUND_SEND_MODE=mock` 時完全不碰網路（自檢/演示用）。
"""
from app.channels import base  # noqa: F401

__all__ = ["base"]
