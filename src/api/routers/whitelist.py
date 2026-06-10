"""Whitelist POST endpoint — /api/tickers/whitelist

Receives ticker whitelist pushes from SynapseEngine and atomically writes
them to the local cache file data/ticker_whitelist.json.

The IngestionScheduler reads this cache file via get_ticker_whitelist()
at the start of each ingestion cycle, enabling a push-based ticker update
model without requiring a restart.

File format (written atomically via temp + rename):
{
  "tickers": [
    {"ticker": "0700.HK", "sector": "Tech", "biz_code": "00700", "name": "", "exchange": ""}
  ],
  "updated_at": "2026-06-10T04:25:00+08:00",
  "source": "synapse_push"
}
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import get_settings
from src.core.config import PROJECT_ROOT, Settings
from src.utils.time_utils import now_hkt
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/tickers",
    tags=["Tickers"],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TickerItem(BaseModel):
    """Single ticker entry pushed by SynapseEngine."""

    ticker: str = Field(
        ...,
        description="Stock ticker symbol, e.g. 0700.HK",
    )
    sector: str = Field(
        ...,
        description="Sector name, e.g. 互联网平台",
    )
    biz_code: str = Field(
        ...,
        description="Business code / local listing code, e.g. 00700",
    )
    name: str = Field(
        default="",
        description="Optional display name",
    )
    exchange: str = Field(
        default="",
        description="Optional exchange name",
    )


class WhitelistPushRequest(BaseModel):
    """Request body for POST /api/tickers/whitelist."""

    tickers: list[TickerItem] = Field(
        ...,
        description="List of tickers to set as the active whitelist",
    )


class WhitelistPushResponse(BaseModel):
    """Response body for POST /api/tickers/whitelist."""

    status: str = Field(
        ...,
        description="Operation status: ok",
    )
    count: int = Field(
        ...,
        description="Number of tickers written",
    )
    timestamp: str = Field(
        ...,
        description="ISO 8601 timestamp in HKT (when the write occurred)",
    )


# ---------------------------------------------------------------------------
# Helper — atomic file write
# ---------------------------------------------------------------------------


def _atomic_write_json(target_path: str, data: object) -> None:
    """Write JSON data atomically to *target_path* (temp + rename).

    Ensures concurrent readers never see a partially written file.
    Raises OSError on I/O failures.
    """
    # Ensure parent directory exists
    parent = os.path.dirname(target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Write to temp file on the same filesystem, then rename atomically
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        prefix="ticker_whitelist_",
        dir=parent or ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.flush()
            os.fsync(fd)  # ensure data hits disk
        os.replace(tmp_path, target_path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# POST /api/tickers/whitelist
# ---------------------------------------------------------------------------


@router.post("/whitelist", response_model=WhitelistPushResponse)
async def push_whitelist(
    body: WhitelistPushRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> WhitelistPushResponse:
    """接受 SynapseEngine 推送的 ticker 白名单，原子写入本地缓存文件。

    - 200: 写入成功，scheduler 将在下一周期使用新数据
    - 422: 请求体验证失败（缺少 tickers 字段或格式错误）
    - 500: 文件写入失败（磁盘满、权限错误等）

    SynapseEngine 应在以下场景调用此端点：
    1. 用户添加/删除观察股票时
    2. 用户切换 watchlist 时
    3. 系统启动后恢复 watchlist 时
    """
    logger.info(
        "POST /api/tickers/whitelist — %d tickers received",
        len(body.tickers),
    )

    # Resolve cache file path relative to project root
    cache_path = str(PROJECT_ROOT / settings.ticker_whitelist_file)

    now = now_hkt()
    payload = {
        "tickers": [t.model_dump() for t in body.tickers],
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "synapse_push",
    }

    try:
        _atomic_write_json(cache_path, payload)
    except OSError as exc:
        logger.error(
            "Failed to write ticker whitelist to %s: %s",
            cache_path,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "File write failed",
                "detail": f"Could not write whitelist cache: {exc}",
            },
        )

    timestamp_str = now.isoformat(timespec="seconds")
    logger.info(
        "Whitelist written: %d tickers -> %s",
        len(body.tickers),
        cache_path,
    )
    return WhitelistPushResponse(
        status="ok",
        count=len(body.tickers),
        timestamp=timestamp_str,
    )
