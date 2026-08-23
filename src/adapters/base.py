"""BaseAdapter — abstract base class for all data-source adapters.

Defines the three-stage pipeline contract: fetch → normalize → dedup.
Every concrete adapter (GDELT, RSS, AkShare, Treasury) inherits from this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.adapters.models import NormalizedEpisode
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class BaseAdapter(ABC):
    """Abstract base class for all data-source adapters.

    Subclasses must implement:
        - fetch(**kwargs) -> list[dict]   — retrieve raw records
        - normalize(record: dict) -> NormalizedEpisode  — convert one record

    Subclasses inherit:
        - dedup(episodes)  — remove duplicates by content_hash / source_url (filter-only)
        - mark_written(episodes) — register successfully-written episodes in dedup_cache
        - run(**kwargs)    — full pipeline: fetch → normalize → dedup

    dedup_cache 契约 (P0-1):
        dedup() 只做过滤，不登记；只有写入成功（或 writer 判定已处理）
        的 episode 才会通过 mark_written() 登记进 dedup_cache。
        这样写入失败的 episode 不会被标记为已处理，下个 cycle 会重试。
    """

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        # NOTE: `is not None` (not truthiness) — an EMPTY shared cache is legit:
        # the scheduler creates its shared dedup_cache as set() and passes it to
        # every adapter; `or set()` would silently replace it with a private set
        # and break cross-adapter / cross-cycle dedup sharing.
        self.dedup_cache: set[str] = dedup_cache if dedup_cache is not None else set()
        self._pre_filter_count: int = 0
        """Record count before relevance filtering. Set by fetch(); read by pipeline for dry-run stats."""

    # ── abstract methods ──────────────────────────────────────────────

    @abstractmethod
    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch raw records from the data source.

        Returns a list of plain dicts, each representing one raw record.
        """

    @abstractmethod
    async def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single raw record into a NormalizedEpisode."""

    # ── concrete methods ─────────────────────────────────────────────

    def dedup(
        self, episodes: list[NormalizedEpisode]
    ) -> list[NormalizedEpisode]:
        """Remove duplicate episodes by content_hash and source_url.

        Duplicates within this batch AND against the cross-cycle cache
        (self.dedup_cache) are skipped.

        注意 (P0-1): 本方法只负责过滤，不会把 hash 登记进 dedup_cache。
        登记必须由调用方在写入成功后通过 mark_written() 完成，否则写入
        失败的 episode 会被误标记为已处理，下个 cycle 静默丢失。
        """
        seen_hashes: set[str] = set()
        seen_urls: set[str] = set()
        result: list[NormalizedEpisode] = []

        for ep in episodes:
            # Skip if hash already seen in current batch or previous cycles
            if ep.content_hash in seen_hashes or ep.content_hash in self.dedup_cache:
                logger.debug(
                    "Skipping duplicate (hash): %s…", ep.content_hash[:12]
                )
                continue

            # Skip if same source_url already seen
            if ep.source_url and ep.source_url in seen_urls:
                logger.debug(
                    "Skipping duplicate (url): %s", ep.source_url
                )
                continue

            seen_hashes.add(ep.content_hash)
            if ep.source_url:
                seen_urls.add(ep.source_url)
            result.append(ep)

        return result

    def mark_written(self, episodes: list[NormalizedEpisode]) -> None:
        """Register successfully-processed episodes into dedup_cache.

        P0-1 时序修复: 必须在写入成功之后调用（例如 pipeline 在
        write_batch 完成后只对 status ok / skipped_duplicate 的 episode
        调用本方法）。写入失败的 episode 不入缓存，下个 cycle 会重试。
        未写入（dry-run）也不入缓存。
        """
        for ep in episodes:
            if ep is None or not ep.content_hash:
                continue
            if ep.content_hash not in self.dedup_cache:
                self.dedup_cache.add(ep.content_hash)
                logger.debug(
                    "mark_written: registered %s… in dedup_cache",
                    ep.content_hash[:12],
                )

    async def run(self, **kwargs: Any) -> list[NormalizedEpisode]:
        """Full pipeline: fetch → normalize → dedup."""
        import asyncio
        records = await self.fetch(**kwargs)
        episodes = await asyncio.gather(*[self.normalize(r) for r in records])
        # Filter out None results (e.g., when normalize skips due to date cutoff)
        valid_episodes = [ep for ep in episodes if ep is not None]
        return self.dedup(valid_episodes)
