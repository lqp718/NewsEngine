"""YAML front matter parser.

Strips ``---`` fenced YAML front matter blocks from text and returns
the pure body text together with the parsed metadata dictionary.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Matches  ---\n<yaml>\n---  at the very start of the text.
_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def strip_yaml_front_matter(text: str) -> tuple[str, dict[str, Any]]:
    """Strip YAML front matter (``---`` … ``---`` block) from *text*.

    Returns:
        A ``(pure_text, metadata_dict)`` tuple.
        - *pure_text*: the text after the closing ``---`` fence (stripped of
          leading blank lines).
        - *metadata_dict*: parsed YAML key/value pairs.  Empty dict when no
          front matter is present or parsing fails.

    If no front matter is found the original *text* is returned unchanged
    together with an empty dict (backward-compatible).
    """
    if not text:
        return text, {}

    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return text, {}

    yaml_block = match.group(1)
    pure_text = text[match.end():]

    # Try to parse with PyYAML; fall back to empty dict on failure.
    try:
        import yaml  # type: ignore[import-untyped]

        metadata: dict[str, Any] = yaml.safe_load(yaml_block) or {}
        if not isinstance(metadata, dict):
            logger.debug("YAML front matter parsed as non-dict (%s); ignoring", type(metadata))
            metadata = {}
    except Exception as exc:  # pragma: no cover – defensive
        logger.debug("Failed to parse YAML front matter: %s", exc)
        metadata = {}

    return pure_text, metadata
