"""Pipeline — unified fetch → normalize → dedup → write → health contract.

Provides a reusable pipeline abstraction that wraps an adapter's
fetch/normalize/dedup stages, feeds episodes through EpisodeWriter,
and tracks per-adapter health metadata.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.adapters.models import NormalizedEpisode
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Data types ─────────────────────────────────────────────────────────


@dataclass
class SourceHealth:
    """Per-adapter health metadata."""

    source_type: str
    last_run_time: datetime | None = None
    last_success_time: datetime | None = None
    consecutive_errors: int = 0
    total_episodes: int = 0
    last_error: str | None = None
    """Last error message (None when healthy)."""


@dataclass
class PipelineResult:
    """Result of a single pipeline run."""

    source_type: str
    success: bool = False
    episode_count: int = 0
    elapsed_seconds: float = 0.0
    error: Exception | None = None
    health: SourceHealth | None = None
    episodes: list = field(default_factory=list)
    """Populated with NormalizedEpisode list only in dry-run mode. Empty in normal mode."""
    fetch_count: int = 0
    """Raw fetch count before relevance filtering. Populated only in dry-run mode."""
    filtered_count: int = 0
    """Count removed by relevance filtering + dedup. Populated only in dry-run mode."""


# ── Global health registry ─────────────────────────────────────────────

# Maps source_type -> SourceHealth, shared across all pipeline runs.
# The scheduler reads this for health endpoint reporting.
_HEALTH_REGISTRY: dict[str, SourceHealth] = {}


def get_health_registry() -> dict[str, SourceHealth]:
    """Return the shared health registry (read-only reference)."""
    return _HEALTH_REGISTRY


# ── Pipeline functions ─────────────────────────────────────────────────


async def run_pipeline(
    adapter: Any,
    writer: Any,
    tickers: list[dict[str, str]] | None = None,
    dry_run: bool = False,
) -> PipelineResult:
    """Run a full pipeline cycle for one adapter.

    Stages:
        1. adapter.run(tickers=tickers) → fetch → normalize → dedup
        2. EpisodeWriter.write_batch(episodes) if any episodes (skipped in dry-run)
        3. Update health metadata (skipped in dry-run)

    Args:
        adapter: Data source adapter instance.
        writer: EpisodeWriter instance (None in dry-run mode).
        tickers: Optional ticker whitelist.
        dry_run: When True, skip write_batch and health tracking, return episodes.

    Returns:
        PipelineResult with outcome, health (None in dry-run), and episodes (dry-run).
    """
    source_type = _resolve_source_type(adapter)
    start = time.monotonic()
    result = PipelineResult(source_type=source_type)

    # ── In dry-run mode, skip health tracking ─────────────────────
    health = None
    if not dry_run:
        if source_type not in _HEALTH_REGISTRY:
            _HEALTH_REGISTRY[source_type] = SourceHealth(source_type=source_type)
        health = _HEALTH_REGISTRY[source_type]

    try:
        # Stage 1: adapter run (fetch → normalize → dedup)
        kwargs: dict[str, Any] = {}
        if tickers is not None:
            kwargs["tickers"] = tickers
        episodes = await adapter.run(**kwargs)

        # Stage 2: write to graphiti (skipped in dry-run mode)
        episode_count = len(episodes)

        if dry_run:
            # Dry-run: populate episodes directly, skip write_batch
            result.episodes = episodes
            result.episode_count = episode_count
            # Read pre-filter count from adapter (set during fetch before relevance filtering)
            pre_filter = getattr(adapter, '_pre_filter_count', 0)
            result.fetch_count = pre_filter if pre_filter else episode_count
            result.filtered_count = max(0, pre_filter - episode_count) if pre_filter else 0
            logger.info(
                "pipeline [%s] dry-run: %d episodes (pre_filter=%d, filtered=%d)",
                source_type,
                episode_count,
                pre_filter,
                result.filtered_count,
            )
        elif episodes:
            write_result = await writer.write_batch(episodes)
            episode_count = write_result.ok
            logger.info(
                "pipeline [%s]: wrote %d / %d episodes "
                "(skipped=%d, errors=%d)",
                source_type,
                write_result.ok,
                len(episodes),
                write_result.skipped,
                write_result.error,
            )
        else:
            logger.debug("pipeline [%s]: no new episodes", source_type)

        # ── Update health: success path (skipped in dry-run) ───────
        if not dry_run and health is not None:
            health.last_run_time = datetime.now(timezone.utc)
            health.last_success_time = health.last_run_time
            health.consecutive_errors = 0
            health.total_episodes += episode_count

        result.success = True
        result.episode_count = episode_count
        result.error = None

    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error(
            "pipeline [%s] failed after %.1fs: %s",
            source_type,
            elapsed,
            exc,
            exc_info=True,
        )

        # ── Update health: error path (skipped in dry-run) ─────────
        if not dry_run and health is not None:
            health.last_run_time = datetime.now(timezone.utc)
            health.consecutive_errors += 1
            health.last_error = str(exc)

            if health.consecutive_errors >= 6:
                logger.critical(
                    "pipeline [%s]: %d consecutive failures (>= 1.5 hours). "
                    "Source is degraded.",
                    source_type,
                    health.consecutive_errors,
                )

        result.success = False
        result.episode_count = 0
        result.error = exc

    finally:
        result.elapsed_seconds = time.monotonic() - start
        if not dry_run:
            result.health = health

    return result


async def run_pipeline_single(
    adapter: Any,
    writer: Any,
    tickers: list[dict[str, str]] | None = None,
) -> PipelineResult:
    """Run pipeline in single-episode write mode.

    Unlike ``run_pipeline`` which uses ``write_batch()``, this mode
    calls ``write_one()`` for each episode so that individual failures
    are caught without aborting the rest of the batch.
    """
    source_type = _resolve_source_type(adapter)
    start = time.monotonic()
    result = PipelineResult(source_type=source_type)

    if source_type not in _HEALTH_REGISTRY:
        _HEALTH_REGISTRY[source_type] = SourceHealth(source_type=source_type)
    health = _HEALTH_REGISTRY[source_type]

    try:
        kwargs: dict[str, Any] = {}
        if tickers is not None:
            kwargs["tickers"] = tickers
        episodes = await adapter.run(**kwargs)

        episode_count = 0
        for ep in episodes:
            try:
                wr = await writer.write_one(ep)
                if wr.status == "ok":
                    episode_count += 1
            except Exception as exc:
                logger.warning(
                    "pipeline_single [%s]: write_one failed for '%s': %s",
                    source_type,
                    ep.name,
                    exc,
                )

        health.last_run_time = datetime.now(timezone.utc)
        health.last_success_time = health.last_run_time
        health.consecutive_errors = 0
        health.total_episodes += episode_count

        result.success = True
        result.episode_count = episode_count

    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error(
            "pipeline_single [%s] failed after %.1fs: %s",
            source_type,
            elapsed,
            exc,
            exc_info=True,
        )
        health.last_run_time = datetime.now(timezone.utc)
        health.consecutive_errors += 1
        health.last_error = str(exc)
        result.success = False
        result.error = exc

    finally:
        result.elapsed_seconds = time.monotonic() - start
        result.health = health

    return result


# ── Helpers ─────────────────────────────────────────────────────────────


def _resolve_source_type(adapter: Any) -> str:
    """Extract the source type string from an adapter instance.

    Tries adapter.SOURCE_TYPE constant first, falls back to class name.
    """
    source_type = getattr(adapter, "SOURCE_TYPE", None)
    if source_type:
        return source_type

    # Derive from class name
    name = type(adapter).__name__.lower()
    if "gdelt" in name:
        return "gdelt_csv"
    if "rss" in name:
        return "rss"
    if "akshare" in name:
        return "akshare"
    return name


__all__ = [
    "PipelineResult",
    "SourceHealth",
    "run_pipeline",
    "run_pipeline_single",
    "get_health_registry",
]
