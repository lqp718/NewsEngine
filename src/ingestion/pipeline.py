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


@dataclass
class PipelineResult:
    """Result of a single pipeline run."""

    source_type: str
    success: bool = False
    episode_count: int = 0
    elapsed_seconds: float = 0.0
    error: Exception | None = None
    health: SourceHealth | None = None


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
) -> PipelineResult:
    """Run a full pipeline cycle for one adapter.

    Stages:
        1. adapter.run(tickers=tickers) → fetch → normalize → dedup
        2. EpisodeWriter.write_batch(episodes) if any episodes
        3. Update health metadata

    Returns:
        PipelineResult with outcome and health status.
    """
    source_type = _resolve_source_type(adapter)
    start = time.monotonic()
    result = PipelineResult(source_type=source_type)

    # ── Ensure health entry exists ──────────────────────────────────
    if source_type not in _HEALTH_REGISTRY:
        _HEALTH_REGISTRY[source_type] = SourceHealth(source_type=source_type)
    health = _HEALTH_REGISTRY[source_type]

    try:
        # Stage 1: adapter run (fetch → normalize → dedup)
        kwargs: dict[str, Any] = {}
        if tickers is not None:
            kwargs["tickers"] = tickers
        episodes = await adapter.run(**kwargs)

        # Stage 2: write to graphiti
        episode_count = 0
        if episodes:
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

        # ── Update health: success path ────────────────────────────
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

        # ── Update health: error path ──────────────────────────────
        health.last_run_time = datetime.now(timezone.utc)
        health.consecutive_errors += 1

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
