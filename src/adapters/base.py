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
        - dedup(episodes)  — remove duplicates by content_hash / source_url
        - run(**kwargs)    — full pipeline: fetch → normalize → dedup
    """

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        self.dedup_cache: set[str] = dedup_cache or set()
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
        (self.dedup_cache) are skipped. Cross-cycle cache is updated
        at the end so future *cycles* also skip these hashes.
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

        # Update the cross-cycle cache so future *runs* also skip these
        self.dedup_cache.update(seen_hashes)
        return result

    async def run(self, **kwargs: Any) -> list[NormalizedEpisode]:
        """Full pipeline: fetch → normalize → dedup."""
        import asyncio
        records = await self.fetch(**kwargs)
        episodes = await asyncio.gather(*[self.normalize(r) for r in records])
        # Filter out None results (e.g., when normalize skips due to date cutoff)
        valid_episodes = [ep for ep in episodes if ep is not None]
        return self.dedup(valid_episodes)
