"""內網 API 認證 —— 共用一把 `X-Internal-Token`（設計案 §4.3）。"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from app.core import config


def require_internal(request: Request) -> None:
    expect = (config.INTERNAL_API_TOKEN or "").strip()
    got = (request.headers.get("X-Internal-Token") or "").strip()
    if not expect or not got or not secrets.compare_digest(got, expect):
        raise HTTPException(status_code=401, detail="invalid internal token")
