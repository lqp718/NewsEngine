"""Unit tests for CLSAdapter normalisation logic (P0-1: dynamic content_scope).

All tests use synthetic data — no HTTP requests.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from src.adapters.cls_adapter import CLSAdapter
from src.adapters.models import NormalizedEpisode


def _make_record(**overrides: Any) -> dict[str, Any]:
    """Build a synthetic CLS telegraph record with a fresh timestamp."""
    record: dict[str, Any] = {
        "id": 1234567890,
        "title": "财联社快讯",
        "brief": "财联社快讯简报",
        "content": "财联社电报正文内容，长度超过标题以便进入 body 拼接分支。",
        "shareurl": "https://www.cls.cn/detail/1234567890",
        "ctime": int(time.time()),
        "level": "B",
        "stock_list": [],
        "subjects": [{"subject_name": "A股"}],
    }
    record.update(overrides)
    return record


def _stock_entry(
    name: str = "兆易创新", stock_id: str = "sh603986", **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "StockID": stock_id,
        "RiseRange": 3.3,
        "last": 100.0,
    }
    entry.update(extra)
    return entry


class TestClsDynamicContentScope:
    """P0-1: content_scope must follow the API-annotated stock_list."""

    @pytest.mark.asyncio
    async def test_non_empty_stock_list_yields_symbol_scope(self):
        adapter = CLSAdapter()
        record = _make_record(
            title="兆易创新发布三季度业绩预告",
            stock_list=[_stock_entry()],
        )
        episode = await adapter.normalize(record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.metadata["content_scope"] == "SYMBOL"
        # cls_stock_count must still record the annotated stock number
        assert episode.metadata["cls_stock_count"] == 1
        # Entities extracted from stock_list stay intact
        assert len(episode.entities) == 1
        assert episode.entities[0].ticker == "SH603986"

    @pytest.mark.asyncio
    async def test_empty_stock_list_yields_macro_scope(self):
        adapter = CLSAdapter()
        record = _make_record(title="央行开展逆回购操作", stock_list=[])
        episode = await adapter.normalize(record)

        assert episode.metadata["content_scope"] == "MACRO"
        assert episode.metadata["cls_stock_count"] == 0
        assert episode.entities == []

    @pytest.mark.asyncio
    async def test_missing_stock_list_key_yields_macro_scope(self):
        """Key absent entirely → same as empty list."""
        adapter = CLSAdapter()
        record = _make_record()
        record.pop("stock_list")
        episode = await adapter.normalize(record)

        assert episode.metadata["content_scope"] == "MACRO"
        assert episode.metadata["cls_stock_count"] == 0

    @pytest.mark.asyncio
    async def test_null_stock_list_yields_macro_scope(self):
        """Explicit JSON null must not crash normalize (defensive `or []`)."""
        adapter = CLSAdapter()
        record = _make_record(stock_list=None)
        episode = await adapter.normalize(record)

        assert episode.metadata["content_scope"] == "MACRO"
        assert episode.metadata["cls_stock_count"] == 0
        assert episode.entities == []

    @pytest.mark.asyncio
    async def test_multi_stock_list_counts_all(self):
        adapter = CLSAdapter()
        record = _make_record(
            title="白酒板块集体拉升",
            stock_list=[
                _stock_entry(name="贵州茅台", stock_id="sh600519"),
                _stock_entry(name="五粮液", stock_id="sz000858"),
            ],
        )
        episode = await adapter.normalize(record)

        assert episode.metadata["content_scope"] == "SYMBOL"
        assert episode.metadata["cls_stock_count"] == 2
        assert len(episode.entities) == 2


class TestClsNormalizeUnchangedBehavior:
    """P0-1 must not regress existing V6.1.1 metadata/dedup behavior."""

    @pytest.mark.asyncio
    async def test_core_metadata_fields_preserved(self):
        adapter = CLSAdapter()
        record = _make_record(
            id=987654,
            level="A",
            subjects=[{"subject_name": "科创板"}, {"subject_name": ""}],
            stock_list=[_stock_entry(is_stib=True)],
        )
        episode = await adapter.normalize(record)

        assert episode.source_type == "cls_telegraph"
        assert episode.metadata["adapter"] == "cls"
        assert episode.metadata["content_fetched"] is True
        assert episode.metadata["cls_article_id"] == 987654
        assert episode.metadata["cls_level"] == "A"
        # Empty subject names are filtered out
        assert episode.metadata["cls_subjects"] == ["科创板"]
        assert episode.name.startswith("cls_telegraph-")
        assert len(episode.content_hash) == 64

    @pytest.mark.asyncio
    async def test_content_hash_is_body_based(self):
        """NormalizedEpisode.model_post_init recomputes content_hash from the
        body — the adapter-level article_id hash is overridden by the model
        (existing behavior, kept under test so regressions are visible)."""
        adapter = CLSAdapter()
        ep1 = await adapter.normalize(_make_record(id=555, title="标题一"))
        ep2 = await adapter.normalize(_make_record(id=555, title="标题一"))
        ep3 = await adapter.normalize(_make_record(id=555, title="标题二"))
        assert ep1.content_hash == ep1.compute_hash()
        assert ep1.content_hash == ep2.content_hash  # same body → same hash
        assert ep1.content_hash != ep3.content_hash  # different body → different

    @pytest.mark.asyncio
    async def test_stale_record_dropped(self):
        """Records older than news_max_age_days cutoff → normalize returns None."""
        adapter = CLSAdapter()
        record = _make_record(ctime=int(time.time()) - 400 * 24 * 3600)
        episode = await adapter.normalize(record)
        assert episode is None
