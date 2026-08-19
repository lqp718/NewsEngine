"""Sanctions Codebook Translator — OFAC program codes to human-readable descriptions.

Translates OFAC SDN program codes (e.g. ``"SDGT"``, ``"UKR"``) to
human-readable descriptions using the JSON codebook in
``data/codebooks/ofac_program_codes.json``.

Data flow::

    translate_program("SDGT")   →  "Specially Designated Global Terrorist"
    translate_program("UKR")    →  "Ukraine-Related Sanctions"
    translate_program("IRAN")   →  "Iran Sanctions"

Design: mirrors ``gdelt_codebook.py`` — lazy loading, thread-safe, fail-open.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from threading import Lock

__all__ = ["translate_program"]

# ── module-level state ───────────────────────────────────────────────

_lock = Lock()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CODEBOOK_PATH = _PROJECT_ROOT / "data" / "codebooks" / "ofac_program_codes.json"


# ── internal helper ──────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_codebook() -> dict[str, str]:
    """Load the OFAC program codes JSON (lazy, thread-safe)."""
    with _lock:
        if not _CODEBOOK_PATH.exists():
            return {}
        try:
            with open(_CODEBOOK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}


# ── public API ───────────────────────────────────────────────────────

def translate_program(code: str) -> str:
    """Translate an OFAC program code to a human-readable description.

    Args:
        code: OFAC program code (e.g. ``"SDGT"``, ``"UKR"``).

    Returns:
        Human-readable description, or the original code if unknown.
        For compound codes (e.g. ``"IRAN-EO13902"``), falls back to
        prefix matching: if the prefix before ``-`` is in the codebook,
        returns ``"{prefix_translation} ({full_code})"``.

    Examples::

        >>> translate_program("SDGT")
        'Specially Designated Global Terrorist'
        >>> translate_program("UKR")
        'Ukraine-Related Sanctions'
        >>> translate_program("IRAN-EO13902")
        'Iran Sanctions (IRAN-EO13902)'
        >>> translate_program("UNKNOWN_CODE")
        'UNKNOWN_CODE'
    """
    if not code:
        return code
    codebook = _load_codebook()
    # 1. Exact match
    if code in codebook:
        return codebook[code]
    # 2. Prefix fallback for compound codes (e.g. "IRAN-EO13902" → "IRAN")
    if "-" in code:
        prefix = code.split("-", 1)[0]
        if prefix in codebook:
            return f"{codebook[prefix]} ({code})"
    # 3. No match — return original code
    return code
