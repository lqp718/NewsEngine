"""Persistence layer models — EpisodeEnvelope / EpisodeStatus / CaptureRunRecord.

JSON 持久化层（landing zone）的数据契约。设计文档:
docs/architecture/json-persistence-layer.md

- ``EpisodeEnvelope``: JSONL 中每一行的自包含快照（v / captured_at / cycle_id / episode）。
  ``episode`` 是完整 ``NormalizedEpisode.model_dump(mode='json')``，反序列化后可直接
  喂给 ``EpisodeWriter.write_one``，不依赖任何外部状态。
- ``EpisodeStatus``: SQLite 状态机（pending → processing → done/skipped/failed/dead）。
- ``CaptureRunRecord``: capture_runs 表的一行（本 cycle 该源抓取统计）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.adapters.models import NormalizedEpisode


class EpisodeStatus(str, Enum):
    """Episode 状态机（landed_episodes.status）。"""

    PENDING = "pending"
    """已落盘，等待 IngestWorker 认领。"""
    PROCESSING = "processing"
    """已被认领，正在 write_one（lease 超时后由 recover_leases 复位）。"""
    DONE = "done"
    """write_one 返回 ok，已入库。"""
    SKIPPED = "skipped"
    """write_one 返回 skipped_duplicate，视同完成。"""
    FAILED = "failed"
    """write_one 最终失败，attempts < max，等待退避后重试。"""
    DEAD = "dead"
    """attempts >= max，需人工介入（--retry-dead）。"""


class EpisodeEnvelope(BaseModel):
    """JSONL 单行 envelope（自包含 episode 快照）。

    设计 §3.2：envelope 必须包含入库所需的完整 episode，不裁剪任何字段
    （entities / keywords / severity / metadata 全部保留），保证清库重建、
    调整 entity_types 重放时零信息损失。
    """

    v: int = Field(
        default=1,
        description="Envelope 版本号。未来 schema 演进时按版本解析/迁移。",
    )
    captured_at: str = Field(
        ...,
        description="抓取时间戳（UTC ISO 8601，毫秒精度，Z 后缀），审计用。",
    )
    cycle_id: str = Field(
        ...,
        description="所属 cycle 标识，格式 YYYYMMDDTHHMMSSZ（UTC），与文件名 stem 一致。",
    )
    episode: NormalizedEpisode = Field(
        ..., description="完整 NormalizedEpisode 快照（model_dump(mode='json')）。"
    )


class CaptureRunRecord(BaseModel):
    """capture_runs 表中一行：本 cycle 该源的一次 capture 统计。"""

    cycle_id: str = Field(..., description="cycle 标识 YYYYMMDDTHHMMSSZ（UTC）。")
    source_type: str = Field(..., description="源类型（adapter SOURCE_TYPE）。")
    captured_at: str = Field(..., description="抓取时间（UTC ISO 8601）。")
    batch_file: str = Field(..., description="JSONL 相对路径（相对 landing 根目录）。")
    total: int = Field(..., description="本 cycle 该源尝试落盘的 episode 总数。")
    new_rows: int = Field(..., description="实际 INSERT 的行数（新 episode）。")
    dup_rows: int = Field(
        ..., description="INSERT OR IGNORE 跳过的行数（跨 cycle 去重命中）。"
    )


__all__ = [
    "EpisodeStatus",
    "EpisodeEnvelope",
    "CaptureRunRecord",
    "NormalizedEpisode",
]