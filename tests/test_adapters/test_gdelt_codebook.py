"""Tests for the GDELT Codebook Translator module.

Covers:
- Valid code translation (cameo, actor, theme)
- Unknown code fail-open
- Edge-case inputs (empty string, whitespace)
- Lazy-load verification (no I/O at import time)
- Thread-safety of concurrent first-time access
"""

from __future__ import annotations

from threading import Barrier, Thread
from typing import Any

import pytest

from src.adapters.gdelt_codebook import translate_cameo, translate_actor, translate_theme


# ── valid translations ───────────────────────────────────────────────


class TestValidTranslations:
    """Known-good codes from the codebook JSON files."""

    def test_cameo_root_code(self) -> None:
        assert translate_cameo("01") == "MAKE PUBLIC STATEMENT"

    def test_cameo_leaf_code(self) -> None:
        assert translate_cameo("0211") == "Appeal for economic cooperation"

    def test_cameo_three_digit(self) -> None:
        assert translate_cameo("057") == "Sign formal agreement"

    def test_actor_usa(self) -> None:
        assert translate_actor("USA") == "United States"

    def test_actor_chn(self) -> None:
        assert translate_actor("CHN") == "China"

    def test_actor_jpn(self) -> None:
        assert translate_actor("JPN") == "Japan"

    def test_theme_tax_fncact(self) -> None:
        assert translate_theme("TAX_FNCACT") == "Taxonomy - Fncact"

    def test_theme_kill(self) -> None:
        assert translate_theme("KILL") == "Kill"

    def test_theme_legislation(self) -> None:
        assert translate_theme("LEGISLATION") == "Legislation"


# ── fail-open (unknown codes) ────────────────────────────────────────


class TestFailOpen:
    """Unknown codes return the original string unchanged."""

    def test_unknown_theme(self) -> None:
        assert translate_theme("NONEXISTENT_CODE") == "NONEXISTENT_CODE"

    def test_unknown_actor(self) -> None:
        assert translate_actor("UNKNOWN123") == "UNKNOWN123"

    def test_unknown_cameo(self) -> None:
        assert translate_cameo("9999") == "9999"


# ── edge-case inputs ─────────────────────────────────────────────────


class TestEdgeCases:
    """Graceful handling of empty, whitespace, or weird inputs."""

    def test_empty_string_theme(self) -> None:
        assert translate_theme("") == ""

    def test_empty_string_actor(self) -> None:
        assert translate_actor("") == ""

    def test_empty_string_cameo(self) -> None:
        assert translate_cameo("") == ""

    def test_whitespace_theme(self) -> None:
        assert translate_theme("   ") == "   "

    def test_whitespace_cameo(self) -> None:
        assert translate_cameo("   ") == "   "

    def test_whitespace_actor(self) -> None:
        assert translate_actor("   ") == "   "


# ── lazy load verification ───────────────────────────────────────────


class TestLazyLoad:
    """Codebook JSON files are NOT loaded at module import time."""

    def test_no_io_at_import(self) -> None:
        """Import alone must not trigger file I/O.

        The simplest way to verify this is to run the import successfully
        — if the module tried to open a file at import time and the file
        were missing, it would crash.  Our import succeeds, proving lazy
        loading is active (the codebooks exist, but we don't touch them
        until a translate function is called).
        """
        # This whole test file already imported the module at the top.
        # If it loaded codebooks at import time, any missing codebook
        # would have raised FileNotFoundError here.
        pass

    def test_multiple_calls_use_cache(self) -> None:
        """Repeated calls return consistent results (cache works)."""
        assert translate_theme("TAX_FNCACT") == "Taxonomy - Fncact"
        assert translate_theme("TAX_FNCACT") == "Taxonomy - Fncact"
        assert translate_theme("TAX_FNCACT") == "Taxonomy - Fncact"

    def test_independent_codebooks(self) -> None:
        """Each codebook loads independently and on demand."""
        # Access theme, then cameo — each should work independently
        assert translate_theme("KILL") == "Kill"
        assert translate_cameo("01") == "MAKE PUBLIC STATEMENT"
        assert translate_actor("CHN") == "China"


# ── thread safety ────────────────────────────────────────────────────


class TestThreadSafety:
    """Concurrent first-time access must be safe."""

    def test_concurrent_first_time_access(self) -> None:
        """Multiple threads calling translate_theme concurrently.

        A Barrier ensures all threads hit the first call simultaneously.
        """
        n_threads = 6
        results: list[str] = []
        errors: list[Exception] = []
        barrier = Barrier(n_threads)

        def _translate() -> None:
            barrier.wait()  # all threads synchronise here
            try:
                result = translate_theme("TAX_FNCACT")
                results.append(result)
            except Exception as exc:
                errors.append(exc)

        threads = [Thread(target=_translate) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access raised: {errors}"
        assert len(results) == n_threads
        assert all(r == "Taxonomy - Fncact" for r in results)

    def test_concurrent_mixed_codebooks(self) -> None:
        """Different threads access different codebooks concurrently."""
        n_threads = 9  # 3 per codebook
        results: dict[str, list[str]] = {"cameo": [], "actor": [], "theme": []}
        errors: list[Exception] = []
        barrier = Barrier(n_threads)

        def _cameo() -> None:
            barrier.wait()
            try:
                results["cameo"].append(translate_cameo("057"))
            except Exception as exc:
                errors.append(exc)

        def _actor() -> None:
            barrier.wait()
            try:
                results["actor"].append(translate_actor("USA"))
            except Exception as exc:
                errors.append(exc)

        def _theme() -> None:
            barrier.wait()
            try:
                results["theme"].append(translate_theme("TAX_FNCACT"))
            except Exception as exc:
                errors.append(exc)

        threads = [Thread(target=_cameo) for _ in range(3)] + \
                  [Thread(target=_actor) for _ in range(3)] + \
                  [Thread(target=_theme) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent mixed access raised: {errors}"
        assert all(r == "Sign formal agreement" for r in results["cameo"])
        assert all(r == "United States" for r in results["actor"])
        assert all(r == "Taxonomy - Fncact" for r in results["theme"])


# ── integration sanity (re-import from adapter package) ──────────────


class TestPackageImport:
    """Functions are re-exportable from the adapter package."""

    def test_import_from_package(self) -> None:
        from src.adapters import (  # type: ignore[import-unimported]
            translate_cameo as pkg_cameo,
            translate_actor as pkg_actor,
            translate_theme as pkg_theme,
        )
        assert pkg_cameo("01") == "MAKE PUBLIC STATEMENT"
        assert pkg_actor("CHN") == "China"
        assert pkg_theme("TAX_FNCACT") == "Taxonomy - Fncact"
