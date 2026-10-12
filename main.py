"""相容入口：真正的 FastAPI app 在 `app/main.py`。

保留這個檔案，讓舊的啟動指令 `uvicorn main:app` 依然可用；
新指令是 `uvicorn app.main:app`。
"""
from app.main import app  # noqa: F401

__all__ = ["app"]
