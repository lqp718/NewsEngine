"""NormalizedEpisode — unified intermediate representation for all adapters.

Every adapter (GDELT, RSS, AkShare, Treasury) converts raw data into a
NormalizedEpisode, which serves as the common data contract between the
adapter layer and the graphiti ingestion layer.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal["gdelt_csv", "gdelt_events", "rss", "akshare", "treasury", "eastmoney"]
Severity = Literal["low", "medium", "high", "critical"]


class EntityItem(BaseModel):
    """A single pre-extracted entity attached to an episode."""

    type: str = Field(
        ...,
        description=(
            "Entity category. One of: stock, sector, country, policy, "
            "organization, location, person, theme"
        ),
    )
    name: str = Field(..., description="Entity display name.")
    ticker: str | None = Field(
        default=None,
        description="Stock ticker (e.g. '0700.HK'). Only present for stock entities.",
    )
    sector: str | None = Field(
        default=None,
        description=(
            "Stock sector (e.g. 'Tech', 'Financials'). "
            "Only present for stock entities."
        ),
    )
    exchange: str | None = Field(
        default=None,
        description=(
            "Stock exchange (e.g. 'HKEX', 'NYSE'). "
            "Only present for stock entities."
        ),
    )


def build_entity_suffix(entities: list[EntityItem]) -> str:
    """Format pre-extracted entities as a trailing block in episode_body.

    This suffix helps the LLM during graphiti entity extraction by providing
    already-identified entities as hints.
    """
    if not entities:
        return ""
    lines = ["\n[END OF CONTENT]\n", "PRE-EXTRACTED ENTITIES (for reference):"]
    for ent in entities:
        if ent.type == "stock" and ent.ticker:
            extra = ent.ticker
            if ent.sector or ent.exchange:
                details = []
                if ent.sector:
                    details.append(f"sector={ent.sector}")
                if ent.exchange:
                    details.append(f"exchange={ent.exchange}")
                extra += f" ({'; '.join(details)})"
            lines.append(f"- Stock: {ent.name} ({extra})")
        elif ent.type == "person":
            lines.append(f"- Person: {ent.name}")
        elif ent.type == "organization":
            lines.append(f"- Organization: {ent.name}")
        elif ent.type == "location":
            lines.append(f"- Location: {ent.name}")
        elif ent.type == "theme":
            lines.append(f"- Theme: {ent.name}")
        elif ent.type == "country":
            lines.append(f"- Country: {ent.name}")
        else:
            lines.append(f"- {ent.type.title()}: {ent.name}")
    return "\n".join(lines)


class NormalizedEpisode(BaseModel):
    """Unified intermediate representation for all adapter outputs.

    Every adapter fetch → normalize → dedup pipeline produces a list of
    NormalizedEpisode instances. These are later consumed by EpisodeWriter
    which converts them to graphiti-core EpisodicNode writes.
    """

    episode_body: str = Field(
        ...,
        description="Main text content — the LLM extracts entities/relations from this.",
    )
    name: str = Field(
        ...,
        description=(
            "Globally-unique episode name. "
            'Format: "{source_type}-{YYYYMMDD}-{group_id}-{hash[:12]}"'
        ),
    )
    source_description: str = Field(
        ..., description="Human-readable source description."
    )
    source_type: SourceType = Field(..., description="Data source enum.")
    source_url: str | None = Field(
        default=None,
        description="Original source URL for traceability and dedup.",
    )
    valid_at: datetime = Field(
        ..., description="Event time in UTC (ISO 8601)."
    )
    content_hash: str = Field(
        ...,
        description="SHA256 digest of episode_body for content-based dedup.",
    )
    entities: list[EntityItem] = Field(
        default_factory=list,
        description="Pre-extracted entities to help LLM extraction.",
    )
    is_plain_text: bool = Field(
        default=True,
        description="Whether the body contains plain text (always True for adapters).",
    )
    severity: Severity = Field(
        default="medium",
        description="Episode severity for prioritization.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Keywords extracted from content.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Adapter-specific extra metadata.",
    )

    def compute_hash(self) -> str:
        """Compute SHA256 digest of episode_body."""
        return hashlib.sha256(self.episode_body.encode("utf-8")).hexdigest()

    @classmethod
    def make_name(
        cls,
        source_type: SourceType,
        valid_at: datetime,
        content_hash: str,
        group_id: str = "",
    ) -> str:
        """Build a globally-unique episode name.

        Format: "{source_type}-{YYYYMMDD}-{group_id}-{hash[:12]}"
        """
        date_part = valid_at.strftime("%Y%m%d")
        hash_part = content_hash[:12]
        if group_id:
            return f"{source_type}-{date_part}-{group_id}-{hash_part}"
        return f"{source_type}-{date_part}-{hash_part}"

    def model_post_init(self, __context: Any) -> None:
        """Post-initialisation hook: ensure content_hash is consistent."""
        computed = self.compute_hash()
        if self.content_hash != computed:
            self.content_hash = computed
