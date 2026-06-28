"""GDELT Codebook Translator — lazy-loaded human-readable descriptions.

Translates machine-readable CAMEO event codes, actor codes, and GKG
theme codes to human-readable descriptions using JSON codebooks in
``data/codebooks/``.

Data flow::

    translate_cameo("057")  →  "Sign formal agreement"
    translate_actor("USA")  →  "United States"
    translate_theme("TAX_FNCACT")  →  "Taxonomy - Fncact"

Design decisions (see design.md for full rationale):

1.  **Lazy loading** — codebook JSONs are parsed on first call, not at
    import time.  This avoids loading 4.4 MB of theme data when GDELT
    is not in use (e.g. RSS-only pipelines).
2.  **``@lru_cache(maxsize=1)``** — one cached dict per distinct
    filename argument.  ``_load_codebook("theme_codes.json")`` and
    ``_load_codebook("cameo_event_codes.json")`` are independent cache
    entries.  ``maxsize=1`` keeps memory bounded.
3.  **``threading.Lock``** — guards the JSON load path so concurrent
    first-time calls read the file exactly once.
4.  **Fail-open** — unknown codes return the original string unchanged.

Usage::

    from src.adapters.gdelt_codebook import translate_cameo, translate_actor, translate_theme
    print(translate_theme("TAX_FNCACT"))  # "Taxonomy - Fncact"
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from threading import Lock

__all__ = ["translate_cameo", "translate_actor", "translate_theme"]

# ── module-level state ───────────────────────────────────────────────

_lock = Lock()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CODEBOOK_DIR = _PROJECT_ROOT / "data" / "codebooks"


# ── internal helper ──────────────────────────────────────────────────


@lru_cache(maxsize=3)
def _load_codebook(filename: str) -> dict[str, str]:
    """Load a single codebook JSON from disk (lazy, cached).

    The ``filename`` argument (e.g. ``"theme_codes.json"``) is the cache
    key.  A separate ``dict`` is cached for each distinct name so that
    calling ``translate_theme`` and ``translate_cameo`` in the same
    process loads and caches both independently.

    Thread safety: the JSON file read path is guarded by ``_lock`` so
    that concurrent first-time calls produce exactly one disk read.
    """
    filepath = _CODEBOOK_DIR / filename
    with _lock:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)


# ── public API ───────────────────────────────────────────────────────


def translate_cameo(code: str) -> str:
    """Translate a CAMEO event code to human-readable description.

    Args:
        code: CAMEO root/child/leaf code (e.g. ``"01"``, ``"0211"``).

    Returns:
        Human-readable description (e.g. ``"MAKE PUBLIC STATEMENT"``),
        or ``code`` unchanged if not found (fail-open).
    """
    codebook = _load_codebook("cameo_event_codes.json")
    return codebook.get(code, code)


def translate_actor(code: str) -> str:
    """Translate a GDELT Actor code to human-readable name.

    Args:
        code: three-letter actor code (e.g. ``"CHN"``, ``"USA"``).

    Returns:
        Country / organisation / group name (e.g. ``"China"``),
        or ``code`` unchanged if not found (fail-open).
    """
    codebook = _load_codebook("actor_codes.json")
    return codebook.get(code, code)


def translate_theme(code: str) -> str:
    """Translate a GKG Theme code to human-readable description.

    Args:
        code: GKG theme code (e.g. ``"TAX_FNCACT"``, ``"KILL"``).

    Returns:
        Human-readable theme description (e.g. ``"Taxonomy - Fncact"``),
        or ``code`` unchanged if not found (fail-open).
    """
    codebook = _load_codebook("theme_codes.json")
    return codebook.get(code, code)
