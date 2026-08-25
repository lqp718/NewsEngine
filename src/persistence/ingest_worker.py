"""IngestWorker — Stage B 异步 drain 循环（认领 → 读 JSONL → write_one → 状态更新）。

JSON 持久化层设计文档: docs/architecture/json-persistence-layer.md

职责:
1. 周期认领 pending 批（SQLite 原子 claim + lease）。
2. 读 JSONL 行 → 反序列化 EpisodeEnvelope → 原样调用 ``EpisodeWriter.write_one``
   （429 退避 / 熔断 / 全局信号量全部保留在 writer 内部）。
3. 按结果更新状态: ok → done / skipped_duplicate → skipped / error → failed（attempts++）。
4. 队列空时扫描 failed 行，按退避周期（min(6h, 10min*2^attempts)）移回 pending。
5. pending 高水位（默认 3000）告警。

线程模型: 单进程 asyncio 事件循环内运行；与 capture 任务共享同一 loop，
SQLite WAL + BEGIN IMMEDIATE 保证跨进程安全（--ingest-only 并发场景）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from src.persistence.landing_store import ClaimedEpisode, LandingStore

logger = logging.getLogger(__name__)

# 成功 / 去重跳过 / 失败（InfluxDB 风格计数仅用于日志汇总）
_OUTCOME_DONE = "done"
_OUTCOME_SKIPPED = "skipped"
_OUTCOME_FAILED = "failed"


class IngestWorker:
    """异步消费 landing 队列：claim → write_one → 状态更新。

    Args:
        store: LandingStore 实例。
        writer_resolver: ``Callable[[str], Any]`` — 按 source_type 返回
            EpisodeWriter（含 write_one）。返回 None 的行标记 failed。
        batch_size: 每轮认领行数（默认 20）。
        poll_interval_sec: 队列空时的轮询间隔（默认 30）。
        lease_sec: 认领 lease 超时（默认 900，须 > write_one 最坏耗时）。
        max_attempts: 失败重试上限，超限转 dead（默认 3）。
        pending_high_water: pending 高水位告警阈值（默认 3000）。
        stop_event: 停止信号（None 时内部创建）。
    """

    def __init__(
        self,
        store: LandingStore,
        writer_resolver: Callable[[str], Any] | None = None,
        *,
        batch_size: int = 20,
        poll_interval_sec: int = 30,
        lease_sec: int = 900,
        max_attempts: int = 3,
        pending_high_water: int = 3000,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._store = store
        self._writer_resolver = writer_resolver or (lambda source_type: None)
        self._batch_size = max(batch_size, 1)
        self._poll_interval_sec = max(poll_interval_sec, 0.1)
        self._lease_sec = max(lease_sec, 1)
        self._max_attempts = max(max_attempts, 1)
        self._pending_high_water = max(pending_high_water, 1)
        self._stop_event = stop_event if stop_event is not None else asyncio.Event()

        self._busy = False
        """当前是否正在处理一批（run_until_drained 空闲判断用）。"""

        self._last_high_water_warn: float = 0.0
        """高水位告警节流（最多每 10 分钟一次）。"""

        self._processed_total = 0
        self._ok_total = 0
        self._skipped_total = 0
        self._failed_total = 0

    # ── public ─────────────────────────────────────────────────────────

    @property
    def is_busy(self) -> bool:
        """是否正在消费一批（认领了但还没处理完）。"""
        return self._busy

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    @stop_event.setter
    def stop_event(self, event: asyncio.Event) -> None:
        self._stop_event = event

    @property
    def processed_total(self) -> int:
        return self._processed_total

    async def run(self) -> None:
        """常驻 drain 循环，直到 stop_event 置位。

        每轮: recover_leases → claim 一批 → 处理；队列空时 retry_failed +
        高水位检查 + 轮询休眠。workers 异常不会崩溃循环（记录错误继续）。
        """
        logger.info(
            "IngestWorker started (batch=%d, poll=%ds, lease=%ds, max_attempts=%d)",
            self._batch_size,
            self._poll_interval_sec,
            self._lease_sec,
            self._max_attempts,
        )
        try:
            while not self._stop_event.is_set():
                worked = await self._cycle()
                if not worked:
                    if self._stop_event.is_set():
                        break
                    await self._sleep(self._poll_interval_sec)
        except asyncio.CancelledError:
            logger.info("IngestWorker cancelled")
            raise
        logger.info(
            "IngestWorker stopped (processed=%d ok=%d skipped=%d failed=%d)",
            self._processed_total,
            self._ok_total,
            self._skipped_total,
            self._failed_total,
        )

    async def drain(self) -> None:
        """处理直到队列清空后返回（--ingest-only 非 watch 模式）。

        每轮与 run() 相同的循环体，但队列空（无 pending、无到期可重试的
        failed）时立即返回，不做轮询等待。failed 行仍在退避期内的保持
        failed 状态由后续 --ingest-only / 常驻模式继续处理。
        """
        logger.info(
            "IngestWorker drain started (batch=%d, lease=%ds, max_attempts=%d)",
            self._batch_size,
            self._lease_sec,
            self._max_attempts,
        )
        while not self._stop_event.is_set():
            worked = await self._cycle()
            if not worked:
                break
        logger.info(
            "IngestWorker drain complete (processed=%d ok=%d skipped=%d failed=%d)",
            self._processed_total,
            self._ok_total,
            self._skipped_total,
            self._failed_total,
        )

    # ── internals ─────────────────────────────────────────────────────

    async def _cycle(self) -> bool:
        """一轮循环。返回是否有工作可做（True 则外层不轮询等待）。"""
        store = self._store

        # 1) 周期 lease 恢复（崩溃恢复，断点续传）
        try:
            recovered = store.recover_leases()
            if recovered:
                logger.warning("IngestWorker: recovered %d expired lease(s)", recovered)
        except Exception as exc:
            logger.error("recover_leases failed: %s", exc, exc_info=True)

        # 2) 认领一批
        try:
            batch = store.claim_batch(self._batch_size, self._lease_sec)
        except Exception as exc:
            logger.error("claim_batch failed: %s", exc, exc_info=True)
            return False

        if batch:
            await self._process_batch(batch)
            return True

        # 3) 队列空 → failed 重试扫描（不阻塞 pending 正常消费）
        try:
            moved = store.retry_failed(self._max_attempts)
            if moved:
                logger.info("IngestWorker: %d failed episode(s) re-queued", moved)
                return True
        except Exception as exc:
            logger.error("retry_failed failed: %s", exc, exc_info=True)

        # 4) pending 高水位告警
        try:
            q = store.queue_summary(max_attempts=self._max_attempts)
            if q["pending"] >= self._pending_high_water:
                now = time.monotonic()
                if now - self._last_high_water_warn >= 600:
                    self._last_high_water_warn = now
                    logger.warning(
                        "IngestWorker: pending HIGH WATER %d >= %d — "
                        "capture 快于 ingest，队列持续堆积",
                        q["pending"],
                        self._pending_high_water,
                    )
        except Exception as exc:
            logger.debug("queue_summary failed: %s", exc)

        return False

    async def _process_batch(self, batch: list[ClaimedEpisode]) -> None:
        """处理一批（串行 write_one；单行失败不中断整批）。"""
        self._busy = True
        done = skipped = failed = 0
        try:
            for row in batch:
                if self._stop_event.is_set():
                    logger.info("IngestWorker: stop requested mid-batch (%d/%d done)", len(batch), batch.index(row))
                    break
                outcome = await self._process_one(row)
                if outcome == _OUTCOME_DONE:
                    done += 1
                elif outcome == _OUTCOME_SKIPPED:
                    skipped += 1
                else:
                    failed += 1
        finally:
            self._busy = False

        self._processed_total += len(batch)
        self._ok_total += done
        self._skipped_total += skipped
        self._failed_total += failed
        logger.info(
            "IngestWorker: batch %d processed (done=%d, skipped=%d, failed=%d)",
            len(batch),
            done,
            skipped,
            failed,
        )

    async def _process_one(self, row: ClaimedEpisode) -> str:
        """处理单行: 读 JSONL → write_one → 更新状态。

        任何失败都通过 store.fail()（attempts++）落库，绝不静默丢弃。
        返回 _OUTCOME_DONE / _OUTCOME_SKIPPED / _OUTCOME_FAILED。
        """
        store = self._store

        # 1) 读 JSONL 行（自包含快照）
        try:
            envelope = store.read_envelope(row.batch_file, row.line_no)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.fail(
                row.content_hash,
                f"read error ({row.batch_file}:{row.line_no}): {exc}",
                self._max_attempts,
            )
            return _OUTCOME_FAILED

        # 2) 解析 writer（按源类型；宏/个股管线使用不同 entity_types）
        writer = self._writer_resolver(row.source_type)
        if writer is None:
            store.fail(
                row.content_hash,
                f"no writer for source_type={row.source_type}",
                self._max_attempts,
            )
            return _OUTCOME_FAILED

        # 3) write_one（writer 内部保留 429 退避 / 熔断 / 全局信号量）
        try:
            wr = await writer.write_one(envelope.episode)
        except asyncio.CancelledError:
            raise  # 行保持 processing，lease 超时后自动复位
        except Exception as exc:
            store.fail(
                row.content_hash,
                f"{type(exc).__name__}: {exc}",
                self._max_attempts,
            )
            return _OUTCOME_FAILED

        # 4) 结果 → 状态机
        if wr.status == "ok":
            store.complete(row.content_hash)
            return _OUTCOME_DONE
        if wr.status == "skipped_duplicate":
            store.skip(row.content_hash)
            return _OUTCOME_SKIPPED
        store.fail(row.content_hash, wr.error or "write_one error", self._max_attempts)
        return _OUTCOME_FAILED

    async def _sleep(self, seconds: float) -> None:
        """可取消休眠（stop_event 置位时提前醒来）。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


__all__ = ["IngestWorker"]