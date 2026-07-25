"""GDELT Events Pipeline Filter — three-stage structured event filtering.

Data flow::

    config JSON (data/gdelt_events_filter.json) → _load_config()
    events: list[EventRecord] + mentions_by_event: dict
        │
        ├─ Stage 1: CAMEO filter (string prefix/exact/contains match)
        ├─ Stage 2: Goldstein filter (|goldstein| >= min_abs_value)
        ├─ Stage 3: Mentions filter (len(mentions) >= min_count)
        │
        └─ URL resolution: mentions_first (confidence sort + dedup)
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from src.adapters.gdelt_events_parser import EventRecord
from src.adapters.gdelt_mentions_parser import MentionRecord
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "EventsPipelineFilter",
    "DEFAULT_EVENTS_FILTER_CONFIG",
]

DEFAULT_EVENTS_FILTER_CONFIG: dict[str, Any] = {
    "version": "1.0",
    "cameo_filter": {
        "mode": "prefix_match",
        "codes": ["14", "16", "17", "18", "19", "20"],
    },
    "goldstein": {"min_abs_value": 5.0},
    "mentions": {"min_count": 5},
    "url_resolution": {
        "strategy": "mentions_first",
        "max_urls_per_event": 3,
    },
}

_VALID_CAMEO_MODES = frozenset({"prefix_match", "exact", "contains"})


class EventsPipelineFilter:
    """Three-stage structured filter for GDELT Events CSV data.

    Applies CAMEO code, Goldstein intensity, and mentions-count filters
    in sequence. Each stage is independently testable and can be skipped
    when prerequisite data is unavailable.

    Configuration is loaded from a JSON file at each ``filter()`` call,
    enabling reload-per-tick (no process restart needed).

    Args:
        config_path: Path to ``gdelt_events_filter.json``. Default is
            ``"data/gdelt_events_filter.json"`` relative to project root or
            absolute.
    """

    def __init__(self, config_path: str = "data/gdelt_events_filter.json") -> None:
        self._config_path = config_path

    # ── public API ───────────────────────────────────────────────────

    def filter(
        self,
        events: list[EventRecord],
        mentions_by_event: dict[str, list[MentionRecord]],
    ) -> list[tuple[EventRecord, list[MentionRecord]]]:
        """Apply all three filter stages sequentially.

        The cheapest stage (CAMEO, string prefix) runs first to minimize
        work. A stage is skipped when its prerequisite data is unavailable
        (e.g. mentions_by_event empty → Mentions filter skipped).

        Args:
            events: Parsed EventRecord list from ``parse_events_file()``.
            mentions_by_event: Dict mapping event_id to list of MentionRecords.

        Returns:
            List of ``(EventRecord, list[MentionRecord])`` tuples for
            events that passed all three filter stages. Empty list if all
            events were filtered out.
        """
        config = self._load_config()

        # Stage 1: CAMEO filter (cheapest — string prefix)
        cameo_passed = self._cameo_filter(events, config)
        logger.debug(
            "CAMEO filter: %d → %d events passed",
            len(events),
            len(cameo_passed),
        )

        if not cameo_passed:
            return []

        # Stage 2: Goldstein filter (single float compare)
        goldstein_passed = self._goldstein_filter(cameo_passed, config)
        logger.debug(
            "Goldstein filter: %d → %d events passed",
            len(cameo_passed),
            len(goldstein_passed),
        )

        if not goldstein_passed:
            return []

        # Stage 3: Mentions filter (dict lookup; skipped if mentions unavailable)
        mentions_passed = self._mentions_filter(
            goldstein_passed, mentions_by_event, config
        )
        logger.debug(
            "Mentions filter: %d → %d events passed",
            len(goldstein_passed),
            len(mentions_passed),
        )

        # Build output tuples with attached mentions
        result: list[tuple[EventRecord, list[MentionRecord]]] = []
        for ev in mentions_passed:
            got_mentions = mentions_by_event.get(ev.event_id, [])
            result.append((ev, got_mentions))

        return result

    # ── config loading ───────────────────────────────────────────────

    def _load_config(self) -> dict[str, Any]:
        """Load and validate JSON config, with fallback to defaults.

        Fallback chain (per ADR-003):
            1. Try to load ``self._config_path`` → parse → validate fields
            2. If file missing: warn + use ``DEFAULT_EVENTS_FILTER_CONFIG``
            3. If parse error: error + use ``DEFAULT_EVENTS_FILTER_CONFIG``
            4. If field missing: warn per-field + default for that field
            5. If numeric threshold ≤ 0: warn + default for that field
            6. If unknown CAMEO mode: warn + fallback to ``"prefix_match"``

        Returns:
            Validated config dict (merged with defaults for missing fields).
        """
        config_path = self._config_path

        # Step 1: Try to load file
        if not os.path.exists(config_path):
            logger.warning(
                "Events filter config not found at %s — using hardcoded defaults",
                config_path,
            )
            return dict(DEFAULT_EVENTS_FILTER_CONFIG)

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw: dict[str, Any] = json.load(f)
        except json.JSONDecodeError as exc:
            logger.error(
                "Events filter config parse error at %s: %s — using defaults",
                config_path,
                exc,
            )
            return dict(DEFAULT_EVENTS_FILTER_CONFIG)
        except OSError as exc:
            logger.error(
                "Events filter config I/O error at %s: %s — using defaults",
                config_path,
                exc,
            )
            return dict(DEFAULT_EVENTS_FILTER_CONFIG)

        # Step 2: Deep-merge with defaults for missing fields
        config = copy.deepcopy(DEFAULT_EVENTS_FILTER_CONFIG)

        # Version field
        version = raw.get("version")
        if version:
            logger.debug("Events filter config version: %s", version)
        else:
            logger.warning("Events filter config missing 'version' field — using '%s'", config["version"])

        # CAMEO filter
        cameo_raw = raw.get("cameo_filter", {})
        if isinstance(cameo_raw, dict):
            # Mode validation
            mode = cameo_raw.get("mode")
            if mode is not None:
                if mode in _VALID_CAMEO_MODES:
                    config["cameo_filter"]["mode"] = mode
                else:
                    logger.warning(
                        "Unknown cameo_filter.mode='%s' — falling back to 'prefix_match'",
                        mode,
                    )
            else:
                logger.warning(
                    "Events filter config missing 'cameo_filter.mode' — using default '%s'",
                    config["cameo_filter"]["mode"],
                )

            # Codes
            codes = cameo_raw.get("codes")
            if codes is not None:
                if isinstance(codes, list):
                    config["cameo_filter"]["codes"] = [str(c) for c in codes]
                else:
                    logger.warning(
                        "Events filter config 'cameo_filter.codes' is not a list — using default"
                    )
        else:
            logger.warning(
                "Events filter config 'cameo_filter' is not a dict — using defaults"
            )

        # Goldstein
        gs_raw = raw.get("goldstein", {})
        if isinstance(gs_raw, dict):
            min_abs = gs_raw.get("min_abs_value")
            if min_abs is not None:
                try:
                    val = float(min_abs)
                    if val > 0:
                        config["goldstein"]["min_abs_value"] = val
                    else:
                        logger.warning(
                            "goldstein.min_abs_value=%s <= 0 — using default %s",
                            val,
                            config["goldstein"]["min_abs_value"],
                        )
                except (ValueError, TypeError):
                    logger.warning(
                        "goldstein.min_abs_value='%s' not a number — using default %s",
                        min_abs,
                        config["goldstein"]["min_abs_value"],
                    )
        else:
            logger.warning(
                "Events filter config 'goldstein' is not a dict — using defaults"
            )

        # Mentions
        ment_raw = raw.get("mentions", {})
        if isinstance(ment_raw, dict):
            min_count = ment_raw.get("min_count")
            if min_count is not None:
                try:
                    val = int(min_count)
                    if val > 0:
                        config["mentions"]["min_count"] = val
                    else:
                        logger.warning(
                            "mentions.min_count=%s <= 0 — using default %s",
                            val,
                            config["mentions"]["min_count"],
                        )
                except (ValueError, TypeError):
                    logger.warning(
                        "mentions.min_count='%s' not an int — using default %s",
                        min_count,
                        config["mentions"]["min_count"],
                    )
        else:
            logger.warning(
                "Events filter config 'mentions' is not a dict — using defaults"
            )

        # URL resolution
        url_raw = raw.get("url_resolution", {})
        if isinstance(url_raw, dict):
            strategy = url_raw.get("strategy")
            if strategy is not None:
                config["url_resolution"]["strategy"] = strategy
            max_urls = url_raw.get("max_urls_per_event")
            if max_urls is not None:
                try:
                    val = int(max_urls)
                    if val > 0:
                        config["url_resolution"]["max_urls_per_event"] = val
                except (ValueError, TypeError):
                    pass
        else:
            logger.warning(
                "Events filter config 'url_resolution' is not a dict — using defaults"
            )

        return config

    # ── filter stages ────────────────────────────────────────────────

    @staticmethod
    def _cameo_filter(
        events: list[EventRecord],
        config: dict[str, Any],
    ) -> list[EventRecord]:
        """Filter events by CAMEO EventRootCode matching.

        Supports three modes:
            - ``"prefix_match"``: Match if the first 2 chars of cameo_code
              are in the codes list.
            - ``"exact"``: Match if the entire cameo_code equals a code.
            - ``"contains"``: Match if a code is a substring of cameo_code.

        Events with empty/malformed cameo_code (< 2 chars) are always rejected.

        Args:
            events: List of EventRecord instances.
            config: Filter config dict (must have ``cameo_filter.mode``
                and ``cameo_filter.codes``).

        Returns:
            Filtered EventRecord list.
        """
        cameo_config: dict[str, Any] = config.get("cameo_filter", {})
        mode: str = cameo_config.get("mode", "prefix_match")
        codes: list[str] = cameo_config.get("codes", [])

        if not codes:
            logger.warning("CAMEO filter: empty codes list — all events rejected")
            return []

        matched: list[EventRecord] = []

        for ev in events:
            cameo = ev.cameo_code.strip() if ev.cameo_code else ""
            if not cameo or len(cameo) < 2:
                continue

            if mode == "prefix_match":
                root = cameo[:2]
                if root in codes:
                    matched.append(ev)
            elif mode == "exact":
                if cameo in codes:
                    matched.append(ev)
            elif mode == "contains":
                if any(c in cameo for c in codes):
                    matched.append(ev)
            # Unknown mode already handled in _load_config (falls back to prefix_match)

        return matched

    @staticmethod
    def _goldstein_filter(
        events: list[EventRecord],
        config: dict[str, Any],
    ) -> list[EventRecord]:
        """Filter events by minimum absolute Goldstein scale.

        Requires ``|EventRecord.goldstein_scale| >= config.goldstein.min_abs_value``.

        Events with ``goldstein_scale = None`` are always rejected.

        Args:
            events: List of EventRecord instances.
            config: Filter config dict (must have ``goldstein.min_abs_value``).

        Returns:
            Filtered EventRecord list.
        """
        min_abs: float = config.get("goldstein", {}).get("min_abs_value", 5.0)
        matched: list[EventRecord] = []

        for ev in events:
            if ev.goldstein_scale is None:
                continue
            if abs(ev.goldstein_scale) >= min_abs:
                matched.append(ev)

        return matched

    @staticmethod
    def _mentions_filter(
        events: list[EventRecord],
        mentions_by_event: dict[str, list[MentionRecord]],
        config: dict[str, Any],
    ) -> list[EventRecord]:
        """Filter events by minimum mention count.

        Requires ``len(mentions_by_event.get(event_id, [])) >= config.mentions.min_count``.

        If ``mentions_by_event`` is empty (e.g. Mentions CSV download failed),
        the filter is skipped entirely — all events pass.

        Args:
            events: List of EventRecord instances.
            mentions_by_event: Dict mapping event_id to MentionRecord list.
            config: Filter config dict (must have ``mentions.min_count``).

        Returns:
            Filtered EventRecord list.
        """
        # If mentions data is unavailable, skip this filter (all events pass)
        if not mentions_by_event:
            logger.debug("Mentions filter: no mention data available — skipping")
            return list(events)

        min_count: int = config.get("mentions", {}).get("min_count", 5)
        matched: list[EventRecord] = []

        for ev in events:
            mentions = mentions_by_event.get(ev.event_id, [])
            if len(mentions) >= min_count:
                matched.append(ev)

        return matched

    # ── URL resolution ───────────────────────────────────────────────

    @staticmethod
    def resolve_urls(
        event_record: EventRecord,
        mentions: list[MentionRecord],
        config: dict[str, Any],
    ) -> list[str]:
        """Resolve up to ``max_urls_per_event`` URLs from Mentions, sorted by confidence.

        Strategy (mentions_first — ADR-006):
            1. From all MentionRecords for the event, collect unique non-empty
               ``document_identifier`` values.
            2. Sort by ``mention_confidence`` descending.
            3. Take top ``max_urls_per_event`` entries.
            4. If result is empty after dedup, fall back to ``[EventRecord.source_url]``.
            5. If ``source_url`` is also empty, return ``[]``.

        Args:
            event_record: The EventRecord.
            mentions: List of MentionRecords for this event (may be empty).
            config: Filter config dict (must have ``url_resolution.max_urls_per_event``).

        Returns:
            Up to ``max_urls_per_event`` unique URL strings.
        """
        max_urls: int = (
            config.get("url_resolution", {}).get("max_urls_per_event", 3)
        )

        # Step 1: Collect unique non-empty document_identifiers
        seen: set[str] = set()
        scored: list[tuple[int, str]] = []  # (confidence, url)

        for m in mentions:
            url = (m.document_identifier or "").strip()
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            scored.append((m.mention_confidence, url))

        # Step 2: Sort by confidence descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Step 3: Take top max_urls
        result = [url for _, url in scored[:max_urls]]

        # Step 4: Fallback to source_url if no mentions
        if not result:
            src_url = (event_record.source_url or "").strip()
            if src_url:
                result = [src_url]

        return result
