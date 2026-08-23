"""Sanctions Adapter — OFAC SDN + OpenSanctions sanctions data.

Phase 1 (add-phase1-macro-adapters): no-API-key sanctions ingestion.

Data sources (both merged under a single ``source_type="sanctions"``,
design.md ADR-3):
- Primary: OpenSanctions search API (https://api.opensanctions.org).
  Note: OpenSanctions now requires a (paid) API key; without one the
  request degrades gracefully and the adapter falls back to OFAC.
- Fallback: OFAC SDN consolidated list CSV (Treasury official file,
  no authentication required).

The adapter always works without a key: OpenSanctions is attempted
first, and any failure (auth error / network error / empty result)
falls back to the OFAC SDN CSV.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient; never raises — returns [] + warning on
  total source failure
- normalize(): one NormalizedEpisode per sanctioned entity
- severity: default ``high`` (design.md ADR-5: sanction events are
  strong signals by themselves)
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.core.config import get_settings
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

logger = get_logger(__name__)

# ── Module-level constants ─────────────────────────────────────────────

_OPEN_SANCTIONS_SEARCH_URL = "https://api.opensanctions.org/search/default"
# OFAC SDN CSV moved to sanctionslistservice.ofac.treas.gov (2026-08).
# The old treasury.gov URL 302-redirects here; use the canonical endpoint.
_OFAC_SDN_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/sdn.csv"
# OFAC ADD.CSV (address table) — used to extract country info for SDN entries.
_OFAC_ADD_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/add.csv"

# OFAC SDN CSV is sorted by entry number ascending; higher numbers are
# more recently added entries. Cap the per-cycle records to bound the
# volume of LLM entity extraction (the standing list is ~17k entries).
_OFAC_MAX_RECORDS = 200

# How many trailing (most recent) OFAC entries to scan before capping.
# The window must be wide enough to cover recent designations across
# ALL major programs: OFAC's newest batches (entries 58xxx) are Cuba/
# Iran, while the newest Russia (entry 56996) and Ukraine (entry 52727)
# designations sit BELOW them. A narrow tail (500) would exclude every
# Russia/Ukraine entry, so use 3000 rows to bring them into the scan.
_OFAC_TAIL_SCAN = 3000


# ── Module-level helper functions ──────────────────────────────────────


def _map_ofac_type(raw_type: str) -> str:
    """Map an OFAC SDN type string to the sanctions target_type.

    OFAC types: 'individual', 'entity', 'vessel', 'aircraft', or '-0-'
    (missing → treat as entity/legalEntity).
    """
    t = (raw_type or "").strip().lower()
    if t == "individual":
        return "person"
    if t in ("vessel", "aircraft"):
        return t
    # '-0-' or 'entity' → legal entity
    return "legalEntity"


def _map_sanctions_severity(target_type: str) -> Severity:
    """Map a sanctioned target type to an episode severity.

    Sanctions are strong signals by default (design.md ADR-5); a
    targeted individual (person) is treated as the strongest case.
    """
    if target_type == "person":
        return "high"
    return "high"


def _build_sanctions_body(
    entity_name: str,
    target_type: str,
    country: str,
    sanction_program: str,
    listing_date: str | None,
    source: str,
) -> str:
    """Build a structured Markdown episode body for one sanction entry."""
    from src.adapters.sanctions_codebook import translate_program

    lines = [f"## Sanctions: {entity_name}", ""]
    lines.append(f"- Target type: {target_type}")
    if country:
        lines.append(f"- Country: {country}")
    if sanction_program:
        programs = _split_programs(sanction_program)
        if len(programs) > 1:
            translated_parts = []
            for prog in programs:
                desc = translate_program(prog)
                if desc != prog:
                    translated_parts.append(f"{prog} — {desc}")
                else:
                    translated_parts.append(prog)
            lines.append(f"- Sanction program: {'; '.join(translated_parts)}")
        elif len(programs) == 1:
            prog = programs[0]
            program_desc = translate_program(prog)
            if program_desc != prog:
                lines.append(f"- Sanction program: {prog} — {program_desc}")
            else:
                lines.append(f"- Sanction program: {prog}")
    if listing_date:
        lines.append(f"- Listing date: {listing_date}")
    lines.append(f"- Source: {source}")
    return "\n".join(lines)


def _parse_iso_date(date_str: str | None) -> datetime | None:
    """Parse an ISO date string (YYYY-MM-DD or ISO datetime) to UTC."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    except ValueError:
        logger.debug("Sanctions: unparseable date: %s", date_str)
        return None


def _split_programs(raw: str) -> list[str]:
    """Split an OFAC program field that may contain multiple programs.

    OFAC SDN CSV uses ``'] ['`` as separator (e.g. ``'SDGT] [IFSR'``).
    Returns a list of cleaned program codes.
    """
    if not raw:
        return []
    parts = raw.split("] [")
    result: list[str] = []
    for p in parts:
        cleaned = p.strip().strip("[]").strip()
        if cleaned and cleaned != "-0-":
            result.append(cleaned)
    return result


def _parse_add_csv(text: str) -> dict[str, str]:
    """Parse OFAC ADD.CSV and build ent_num → country mapping.

    ADD.CSV format (no header, 12 columns):
    - Column 0: ent_num (links to SDN.CSV ent_num)
    - Column 4: country
    Skip rows with missing/placeholder country (``'-0-'`` or empty).
    First non-empty country wins for each ent_num.
    """
    mapping: dict[str, str] = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 5:
            continue
        ent_num = row[0].strip()
        country = row[4].strip()
        if not ent_num or not country or country == "-0-":
            continue
        if ent_num not in mapping:  # First non-empty country wins
            mapping[ent_num] = country
    return mapping


# ── Adapter ────────────────────────────────────────────────────────────


class SanctionsAdapter(BaseAdapter):
    """OFAC SDN / OpenSanctions sanctions adapter.

    Merges OFAC SDN + OpenSanctions into a single ``source_type``.
    OpenSanctions is the primary (graceful when keyed), OFAC SDN CSV is
    the no-key fallback. Never raises; degrades to ``[]`` + warning when
    all sources are unavailable.
    """

    SOURCE_TYPE = "sanctions"

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch recent sanctions entries.

        If an OpenSanctions API key is configured, attempts that first;
        otherwise (or on failure) falls back to the OFAC SDN CSV.
        Records carry: ``entity_name`` / ``target_type`` / ``country`` /
        ``sanction_program`` / ``listing_date`` / ``source_url`` /
        ``source``.
        """
        settings = get_settings()
        timeout = settings.open_sanctions_timeout_sec

        records: list[dict] = []
        has_os_key = bool(settings.open_sanctions_api_key)

        if has_os_key:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    records = await self._fetch_opensanctions(client, settings)
                    if records:
                        logger.info("Sanctions: %d entries from OpenSanctions", len(records))
                    else:
                        logger.info("Sanctions: OpenSanctions empty, falling back to OFAC SDN")
                        records = await self._fetch_ofac_sdn(settings)
            except Exception as exc:
                logger.warning(
                    "Sanctions: OpenSanctions fetch failed (%s), falling back to OFAC SDN",
                    exc,
                )
                try:
                    records = await self._fetch_ofac_sdn(settings)
                except Exception as exc2:
                    logger.warning("Sanctions: OFAC SDN fetch also failed: %s", exc2)
                    records = []
        else:
            # No OpenSanctions API key — go directly to OFAC SDN
            try:
                records = await self._fetch_ofac_sdn(settings)
            except Exception as exc:
                logger.warning("Sanctions: OFAC SDN fetch failed: %s", exc)
                records = []

        self._pre_filter_count = len(records)
        return records

    async def _fetch_opensanctions(
        self, client: httpx.AsyncClient, settings: Any
    ) -> list[dict]:
        """Fetch recent sanctions from OpenSanctions search API.

        Requires ``open_sanctions_api_key`` to be configured. Returns an
        empty list (no exception) when the API returns no results.
        """
        headers: dict[str, str] = {}
        api_key = settings.open_sanctions_api_key
        if api_key:
            headers["Authorization"] = f"ApiKey {api_key}"
        params = {
            "q": "",
            "schema": "Sanction",
            "limit": str(_OFAC_MAX_RECORDS),
        }
        resp = await client.get(
            _OPEN_SANCTIONS_SEARCH_URL, params=params, headers=headers
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results") or []
        records: list[dict] = []
        for item in results:
            entity_name = item.get("caption") or ""
            schema = (item.get("schema") or "").lower()
            target_type = "person" if schema == "person" else "legalEntity"
            props = item.get("properties") or {}
            countries = props.get("countries") or []
            programs = props.get("programs") or props.get("topics") or []
            first_seen = props.get("first_seen") or props.get("start_date") or []
            listing_date = first_seen[0] if first_seen else None

            # Date window: only keep entries listed within the recency window
            listing_dt = _parse_iso_date(listing_date)
            if listing_dt is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(
                    days=settings.news_max_age_days
                )
                if listing_dt < cutoff:
                    continue

            entity_id = item.get("id") or ""
            records.append(
                {
                    "entity_name": entity_name,
                    "target_type": target_type,
                    "country": countries[0] if countries else "",
                    "sanction_program": programs[0] if programs else "",
                    "listing_date": listing_date,
                    "source_url": (
                        f"https://www.opensanctions.org/entities/{entity_id}/"
                        if entity_id
                        else None
                    ),
                    "source": "opensanctions",
                }
            )
        return records

    async def _fetch_ofac_sdn(self, settings: Any) -> list[dict]:
        """Fetch the OFAC SDN consolidated CSV and parse a recent, diverse sample.

        The CSV is sorted by entry number ascending; higher numbers are
        more recently added. We scan the trailing ``_OFAC_TAIL_SCAN``
        rows (wide enough to include the most recent Russia/Ukraine
        designations that sit below the newest Cuba/Iran batch) and
        select up to ``_OFAC_MAX_RECORDS`` with a two-pass strategy:

        1. Newest entry of every distinct sanction program in the
           window — guarantees program diversity (e.g. Russia/Ukraine
           coverage instead of a single old-list block), then
        2. Fill the remaining slots newest-first — preserves recency
           (largest entry numbers).

        OFAC entries carry no listing date (standing list), so they are
        not date-window filtered; ``listing_date`` stays ``None`` and
        cross-cycle dedup prevents re-ingestion.
        """
        # follow_redirects=True: the old treasury.gov URL 302s to the
        # new sanctionslistservice endpoint; be defensive.
        async with httpx.AsyncClient(
            timeout=settings.open_sanctions_timeout_sec, follow_redirects=True
        ) as client:
            resp = await client.get(_OFAC_SDN_CSV_URL)
            resp.raise_for_status()
            text = resp.text

            # Fetch ADD.CSV for country info (fail-open)
            country_map: dict[str, str] = {}
            try:
                add_resp = await client.get(_OFAC_ADD_CSV_URL)
                add_resp.raise_for_status()
                country_map = _parse_add_csv(add_resp.text)
                if country_map:
                    logger.info(
                        "Sanctions: loaded %d country mappings from ADD.CSV",
                        len(country_map),
                    )
            except Exception as exc:
                logger.warning(
                    "Sanctions: ADD.CSV fetch failed (%s), country info unavailable",
                    exc,
                )

        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader if row and row[0].strip().isdigit()]
        recent_rows = rows[-_OFAC_TAIL_SCAN:] if len(rows) > _OFAC_TAIL_SCAN else rows

        # Pre-parse the window (skip rows without a name), newest first.
        # Each entry: (ent_num, program, record).
        parsed: list[tuple[str, str, dict]] = []
        for row in reversed(recent_rows):
            ent_num = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            if not name:
                continue
            raw_type = row[2].strip() if len(row) > 2 else ""
            program = row[3].strip() if len(row) > 3 else ""
            if program == "-0-":
                program = ""
            parsed.append(
                (
                    ent_num,
                    program,
                    {
                        "entity_name": name,
                        "target_type": _map_ofac_type(raw_type),
                        "country": country_map.get(ent_num, ""),
                        "sanction_program": program,
                        "listing_date": None,
                        "source_url": (
                            f"https://sanctionssearch.ofac.treas.gov/Details.aspx?id={ent_num}"
                        ),
                        "source": "ofac",
                    },
                )
            )

        records: list[dict] = []
        seen_programs: set[str] = set()
        seen_entry_numbers: set[str] = set()

        # Pass 1 — newest entry of every distinct program (diversity).
        for ent_num, program, record in parsed:
            if len(records) >= _OFAC_MAX_RECORDS:
                break
            if program in seen_programs:
                continue
            seen_programs.add(program)
            seen_entry_numbers.add(ent_num)
            records.append(record)

        # Pass 2 — fill remaining slots newest-first (recency).
        for ent_num, program, record in parsed:
            if len(records) >= _OFAC_MAX_RECORDS:
                break
            if ent_num in seen_entry_numbers:
                continue
            seen_entry_numbers.add(ent_num)
            records.append(record)

        return records

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert one sanctions entry to a NormalizedEpisode.

        Returns ``None`` only for entries with an explicit listing date
        outside the recency window (OFAC entries have no listing date and
        always pass through).
        """
        entity_name = str(record.get("entity_name", "")).strip()
        if not entity_name:
            return None
        target_type = str(record.get("target_type", "legalEntity"))
        country = str(record.get("country", "") or "")
        sanction_program = str(record.get("sanction_program", "") or "")
        listing_date = record.get("listing_date")
        source_url = record.get("source_url")
        source = str(record.get("source", "ofac"))

        # Date window cutoff (only when a listing date is available)
        listing_dt = _parse_iso_date(listing_date)
        if listing_dt is not None:
            settings = get_settings()
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=settings.news_max_age_days
            )
            if listing_dt < cutoff:
                logger.debug(
                    "Sanctions: %s listed before window — skipping", entity_name
                )
                return None
        valid_at = listing_dt or now_hkt().astimezone(timezone.utc)

        severity = _map_sanctions_severity(target_type)
        episode_body = _build_sanctions_body(
            entity_name,
            target_type,
            country,
            sanction_program,
            listing_date,
            source,
        )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        ep_name = NormalizedEpisode.make_name(
            source_type="sanctions",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id="SDN",
        )

        # Entity extraction: person/organization for the target, country
        # entity when the target country is known.
        entities: list[EntityItem] = []
        if target_type == "person":
            entities.append(EntityItem(type="person", name=entity_name))
        else:
            entities.append(EntityItem(type="organization", name=entity_name))
        if country:
            entities.append(EntityItem(type="country", name=country))

        return NormalizedEpisode(
            episode_body=episode_body,
            name=ep_name,
            source_description="OFAC SDN / OpenSanctions Sanctions List",
            source_type="sanctions",
            source_url=source_url,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=["sanctions", "ofac", "sdn", target_type],
            entities=entities,
            metadata={
                "_structured": True,
                "content_scope": "MACRO",
                "target_type": target_type,
                "sanction_program": sanction_program,
                "source": source,
            },
        )


__all__ = [
    "SanctionsAdapter",
    "_map_ofac_type",
    "_map_sanctions_severity",
    "_build_sanctions_body",
    "_split_programs",
    "_parse_add_csv",
    "_OFAC_SDN_CSV_URL",
    "_OFAC_ADD_CSV_URL",
    "_OPEN_SANCTIONS_SEARCH_URL",
    "_OFAC_MAX_RECORDS",
]
