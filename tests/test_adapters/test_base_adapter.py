"""Unit tests for BaseAdapter (dedup logic)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode


class _TestConcreteAdapter(BaseAdapter):
    """Minimal concrete adapter for testing BaseAdapter methods."""

    def __init__(self, dedup_cache=None):
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs):
        return []

    def normalize(self, record):
        return record  # type: ignore


def _make_ep(
    body: str,
    url: str | None = None,
    content_hash: str | None = None,
) -> NormalizedEpisode:
    now = datetime.now(timezone.utc)
    h = content_hash or "aaaa"
    return NormalizedEpisode(
        episode_body=body,
        name=f"test-{h[:12]}",
        source_description="test",
        source_type="gdelt_csv",
        source_url=url,
        valid_at=now,
        content_hash=h,
        severity="medium",
    )


class TestDedupLogic:
    """dedup() method tests."""

    def test_dedup_all_unique(self):
        """Different content_hashes → all retained."""
        adapter = _TestConcreteAdapter()
        eps = [
            _make_ep("body1", "http://a.com/1", "hash_1111"),
            _make_ep("body2", "http://a.com/2", "hash_2222"),
            _make_ep("body3", "http://a.com/3", "hash_3333"),
        ]
        result = adapter.dedup(eps)
        assert len(result) == 3

    def test_dedup_by_hash(self):
        """Same content_hash → only first retained."""
        adapter = _TestConcreteAdapter()
        eps = [
            _make_ep("same body", "http://a.com/1", "hash_1111"),
            _make_ep("same body", "http://a.com/2", "hash_1111"),
        ]
        result = adapter.dedup(eps)
        assert len(result) == 1
        assert result[0].source_url == "http://a.com/1"

    def test_dedup_by_url(self):
        """Same source_url → only first retained."""
        adapter = _TestConcreteAdapter()
        eps = [
            _make_ep("body1", "http://a.com/shared", "hash_1111"),
            _make_ep("body2", "http://a.com/shared", "hash_2222"),
        ]
        result = adapter.dedup(eps)
        assert len(result) == 1
        assert result[0].source_url == "http://a.com/shared"

    def test_dedup_mixed(self):
        """Hash + url mixed scenario."""
        adapter = _TestConcreteAdapter()
        # content_hash is recomputed by model_post_init, so use explicitly
        # different bodies to control dedup by URL only
        eps = [
            _make_ep("body1", "http://a.com/1"),
            _make_ep("different2", "http://a.com/2"),
            _make_ep("body3", "http://a.com/1"),  # dup url → skipped
            _make_ep("body4", None),
            _make_ep("body5", "http://a.com/5"),
        ]
        result = adapter.dedup(eps)
        # body1 kept, body2 kept, body3 skipped (url dup), body4 kept, body5 kept
        assert len(result) == 4

    def test_dedup_none_url(self):
        """None source_url → skipped for URL dedup."""
        adapter = _TestConcreteAdapter()
        eps = [
            _make_ep("body1", None, "hash_1111"),
            _make_ep("body2", None, "hash_2222"),
        ]
        result = adapter.dedup(eps)
        assert len(result) == 2

    def test_dedup_cross_cycle_cache(self):
        """Cross-cycle dedup_cache prevents re-adding duplicates."""
        # Use the actual computed SHA256 hash for "body1" in the cache
        import hashlib
        body1_hash = hashlib.sha256(b"duplicate_body").hexdigest()
        body2_hash = hashlib.sha256(b"unique_body").hexdigest()

        adapter = _TestConcreteAdapter(dedup_cache={body1_hash})
        eps = [
            _make_ep("duplicate_body", "http://a.com/1"),
            _make_ep("unique_body", "http://a.com/2"),
        ]
        result = adapter.dedup(eps)
        assert len(result) == 1
        assert result[0].content_hash == body2_hash


class TestContentHash:
    """Content hash computation consistency."""

    def test_compute_hash_consistent(self):
        """Same input → same hash."""
        ep1 = _make_ep("Hello World", None, "placeholder")
        ep1.content_hash = ep1.compute_hash()  # force recompute
        ep2 = _make_ep("Hello World", None, "placeholder")
        ep2.content_hash = ep2.compute_hash()
        assert ep1.content_hash == ep2.content_hash

    def test_compute_hash_different(self):
        """Different input → different hash."""
        ep1 = _make_ep("Hello", None, "h1")
        ep1.content_hash = ep1.compute_hash()
        ep2 = _make_ep("World", None, "h2")
        ep2.content_hash = ep2.compute_hash()
        assert ep1.content_hash != ep2.content_hash
