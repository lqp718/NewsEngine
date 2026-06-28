"""TickerSync — 从 SynapseEngine 同步 ticker 白名单。

职责:
1. 从 SynapseEngine GET /api/portfolio/tickers 拉取白名单
2. 支持定时刷新（默认每 6 小时）
3. 降级方案: SynapseEngine 不可达 → 本地缓存文件
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "TickerSync",
]

# SynapseEngine 响应样例:
# {
#   "tickers": [
#     {
#       "symbol": "00700",
#       "biz_code": "00700",
#       "name_zh": "腾讯控股",
#       "name_en": "Tencent",
#       "sector": "互联网平台",
#       "exchange": "HKEX",
#       "source": "portfolio"
#     }
#   ],
#   "total": 25,
#   "updated_at": "2026-06-09T08:00:00+08:00"
# }


class TickerSync:
    """从 SynapseEngine 同步 ticker 白名单。"""

    def __init__(
        self,
        synapse_url: str = "http://localhost:8000",
        cache_path: str = "data/ticker_cache.json",
        refresh_interval_sec: float = 21600.0,  # 6 小时
        timeout_sec: float = 10.0,
    ) -> None:
        self._synapse_url = synapse_url.rstrip("/")
        self._cache_path = cache_path
        self._refresh_interval_sec = refresh_interval_sec
        self._timeout_sec = timeout_sec

        self._whitelist: list[dict[str, str]] = []
        self._last_refresh: float = 0.0

    # ── 公开接口 ────────────────────────────────────────────────────

    async def refresh(self) -> list[dict[str, str]]:
        """拉取最新白名单，失败时降级为缓存。

        首次调用时尝试从 SynapseEngine 拉取。
        若不可达则从缓存文件加载，均不可用时返回空列表。
        """
        data = await self._fetch_from_synapse()

        if data is not None:
            whitelist = self._normalize_response(data)
            self._save_to_cache(whitelist)
            self._whitelist = whitelist
            self._last_refresh = time.time()
            logger.info(
                "TickerSync: SynapseEngine 拉取成功，共 %d 个 ticker",
                len(whitelist),
            )
            return whitelist

        # SynapseEngine 不可达，尝试降级
        cached = self._load_from_cache()
        if cached:
            self._whitelist = cached
            self._last_refresh = time.time()
            logger.warning(
                "TickerSync: SynapseEngine 不可达，使用本地缓存 (%s)，"
                "共 %d 个 ticker",
                self._cache_path,
                len(cached),
            )
            return cached

        self._whitelist = []
        self._last_refresh = time.time()
        logger.warning(
            "TickerSync: SynapseEngine 不可达且无缓存，返回空列表。"
            "适配器将不过滤（爬全量）"
        )
        return []

    def get_whitelist(self) -> list[dict[str, str]]:
        """获取当前白名单。"""
        return self._whitelist

    # ── 内部方法 ────────────────────────────────────────────────────

    async def _fetch_from_synapse(self) -> dict[str, Any] | None:
        """从 SynapseEngine GET /api/portfolio/tickers 获取白名单。

        返回解析后的 JSON dict，失败时返回 None。
        """
        url = f"{self._synapse_url}/api/portfolio/tickers"
        try:
            resp = requests.get(url, timeout=self._timeout_sec)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning(
                "TickerSync: 连接 SynapseEngine 失败 (%s → %s)",
                url,
                exc,
            )
            return None
        except json.JSONDecodeError as exc:
            logger.error(
                "TickerSync: SynapseEngine 返回非法 JSON: %s",
                exc,
            )
            return None

    def _normalize_response(
        self, data: dict[str, Any]
    ) -> list[dict[str, str]]:
        """将 SynapseEngine 响应标准化为适配器 ticker_whitelist 格式。"""
        result: list[dict[str, str]] = []
        for ticker in data.get("tickers", []):
            result.append(
                {
                    "symbol": str(ticker.get("symbol", "")),
                    "biz_code": str(ticker.get("biz_code", "")),
                    "name_zh": str(ticker.get("name_zh", "")),
                    "name_en": str(ticker.get("name_en", "")),
                    "sector": str(ticker.get("sector", "")),
                    "exchange": str(ticker.get("exchange", "")),
                }
            )
        return result

    def _save_to_cache(self, whitelist: list[dict[str, str]]) -> None:
        """将白名单写入缓存文件。"""
        cache = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "synapse",
            "whitelist": whitelist,
        }
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("TickerSync: 写入缓存文件失败: %s", exc)

    def _load_from_cache(self) -> list[dict[str, str]]:
        """从缓存文件加载白名单。

        缓存文件损坏时删除并返回空列表。
        """
        if not os.path.exists(self._cache_path):
            return []

        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            return cache.get("whitelist", [])
        except json.JSONDecodeError:
            logger.error(
                "TickerSync: 缓存文件损坏 (%s)，删除后返回空列表",
                self._cache_path,
            )
            try:
                os.remove(self._cache_path)
            except OSError:
                pass
            return []
        except OSError as exc:
            logger.error("TickerSync: 读取缓存文件失败: %s", exc)
            return []
