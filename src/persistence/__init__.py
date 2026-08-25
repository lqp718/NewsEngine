"""NewsEngine JSON 持久化层（landing zone）。

解耦「抓取（Stage A: capture）」与「入库（Stage B: ingest）」：
- models.py        — EpisodeEnvelope / EpisodeStatus / CaptureRunRecord
- landing_store.py — LandingStore（SQLite 状态 + JSONL 快照 IO）
- ingest_worker.py — IngestWorker（Stage B 异步 drain 循环）

设计文档: docs/architecture/json-persistence-layer.md
"""

from src.persistence.ingest_worker import IngestWorker
from src.persistence.landing_store import ClaimedEpisode, LandingStore
from src.persistence.models import CaptureRunRecord, EpisodeEnvelope, EpisodeStatus

__all__ = [
    "LandingStore",
    "IngestWorker",
    "ClaimedEpisode",
    "EpisodeEnvelope",
    "EpisodeStatus",
    "CaptureRunRecord",
]