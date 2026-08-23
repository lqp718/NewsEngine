"""Entity canonical name normalization.

This module provides utilities to normalize entity names to a canonical form,
reducing duplication in the knowledge graph (e.g., "Bougainville Copper" vs
"Bougainville Copper Ltd." should map to the same canonical entity).

Two-stage normalization:
1. Lookup table loaded from data/canonical_entities.yaml (alias → canonical name)
2. Suffix stripping for corporate suffixes (Ltd, Inc, Corp, etc.)
3. Whitespace normalization

Usage:
    from src.utils.entity_canonical import canonical_name
    
    normalized = canonical_name("Tencent Holdings Ltd.", entity_type="stock")
    # Returns: "腾讯控股"
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Optional


# Corporate suffixes to strip (case-insensitive, trailing dots handled)
CORPORATE_SUFFIXES = {
    # English suffixes
    'ltd', 'ltd.', 'limited', 'inc', 'inc.', 'corp', 'corp.',
    'co', 'co.', 'plc', 'llc', 'lp', 'sa', 'ag', 'se',
    'holdings', 'holding', 'group',
    # Chinese suffixes
    '控股', '有限公司', '股份有限公司', '集团', '公司',
}


def _load_alias_map() -> dict[str, str]:
    """Load entity alias mapping from data/canonical_entities.yaml.

    The YAML format is ``canonical_name: [alias1, alias2, ...]``.
    This function builds a reverse mapping of ``alias → canonical_name``
    (all lowercased) for fast lookup.  The canonical name itself is also
    included as a key so that lookups are idempotent.
    """
    yaml_path = Path(__file__).resolve().parents[2] / "data" / "canonical_entities.yaml"
    alias_map: dict[str, str] = {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        warnings.warn(
            "PyYAML is not installed – entity canonical mapping is empty. "
            "Install with: pip install pyyaml",
            stacklevel=2,
        )
        return alias_map

    try:
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        warnings.warn(
            f"Entity mapping file not found: {yaml_path} – using empty mapping.",
            stacklevel=2,
        )
        return alias_map
    except Exception as exc:
        warnings.warn(
            f"Failed to load entity mapping from {yaml_path}: {exc}",
            stacklevel=2,
        )
        return alias_map

    if not isinstance(data, dict):
        warnings.warn(
            f"Entity mapping file {yaml_path} has unexpected format – using empty mapping.",
            stacklevel=2,
        )
        return alias_map

    for canonical, aliases in data.items():
        if not isinstance(aliases, list):
            continue
        # Map each alias (lowercased) → canonical name
        for alias in aliases:
            alias_map[str(alias).strip().lower()] = str(canonical)
        # Also map the canonical name itself (lowercased) → canonical name
        alias_map[str(canonical).strip().lower()] = str(canonical)

    return alias_map


# Module-level alias map (loaded once at import time)
ALIAS_MAP: dict[str, str] = _load_alias_map()


def canonical_name(name: str, entity_type: Optional[str] = None) -> str:
    """Normalize entity name to canonical form.
    
    Applies three-stage normalization:
    1. Lookup in ALIAS_MAP for known high-frequency entities (exact match)
    2. Strip corporate suffixes iteratively, checking map after each removal
    3. Normalize whitespace
    
    Args:
        name: Raw entity name (e.g., "Tencent Holdings Ltd.")
        entity_type: Optional entity type hint (stock/organization/country/etc.)
                    Currently unused but reserved for future type-specific rules.
    
    Returns:
        Canonical entity name (e.g., "腾讯控股")
    
    Examples:
        >>> canonical_name("Tencent Holdings Ltd.")
        '腾讯控股'
        >>> canonical_name("Bougainville Copper Ltd.")
        'Bougainville Copper'
        >>> canonical_name("  Apple   Inc  ")
        '苹果'
    """
    if not name:
        return name
    
    # Stage 0: Strip and lowercase for lookup
    cleaned = name.strip()
    lower = cleaned.lower()
    
    # Stage 1: Exact lookup in ALIAS_MAP (highest priority)
    if lower in ALIAS_MAP:
        return ALIAS_MAP[lower]
    
    # Stage 2: Strip corporate suffixes iteratively, checking map after each removal
    tokens = cleaned.split()
    while tokens:
        # Check if current token is a suffix
        last_token_lower = tokens[-1].lower().rstrip('.')
        if last_token_lower in CORPORATE_SUFFIXES:
            tokens.pop()
            # After removing suffix, check if remaining tokens match the map
            if tokens:
                remaining = ' '.join(tokens)
                remaining_lower = remaining.lower()
                if remaining_lower in ALIAS_MAP:
                    return ALIAS_MAP[remaining_lower]
        else:
            # No more suffixes to strip
            break
    
    # Stage 3: Normalize whitespace
    if tokens:
        cleaned = ' '.join(tokens)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned


def canonical_name_batch(names: list[str], entity_type: Optional[str] = None) -> list[str]:
    """Normalize a batch of entity names.
    
    Convenience function for processing multiple names at once.
    
    Args:
        names: List of raw entity names
        entity_type: Optional entity type hint
    
    Returns:
        List of canonical names (same order as input)
    """
    return [canonical_name(n, entity_type) for n in names]


def is_canonical(name: str, entity_type: Optional[str] = None) -> bool:
    """Check if a name is already in canonical form.
    
    Args:
        name: Entity name to check
        entity_type: Optional entity type hint
    
    Returns:
        True if name equals its canonical form, False otherwise
    """
    return canonical_name(name, entity_type) == name


__all__ = [
    'canonical_name',
    'canonical_name_batch',
    'is_canonical',
    'CORPORATE_SUFFIXES',
    'ALIAS_MAP',
]
