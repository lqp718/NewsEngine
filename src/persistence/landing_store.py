"""LandingStore — SQLite 状态 + JSONL 文件 IO（Stage A capture / Stage B claim）。

JSON 持久化层核心。设计文档: docs/architecture/json-persistence-layer.md

职责:
- ``capture_batch``: 把 normalize 后的 episode 写成 JSONL（原子 tmp+rename）并登记
  SQLite pending（INSERT OR IGNORE，content_hash 权威去重）。
- ``claim_batch``: 原子认领 pending 批（BEGIN IMMEDIATE + lease），进程间安全。
- ``complete/skip/fail``: 状态机转移（done / skipped / failed / dead）。
- ``recover_leases``: 启动恢复 — processing 超 lease 复位 pending。
- ``scan_orphan_files``: 启动恢复 — 已落文件未登记 → 幂等补登记。
- ``replay / retry_dead / rebuild_index / retention_sweep / stats``: 运营能力。

时间约定: 所有时间戳统一 UTC ISO 8601（毫秒，Z 后缀），字符串字典序即时间序。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.adapters.models import NormalizedEpisode
from src.persistence.models import CaptureRunRecord, EpisodeEnvelope, EpisodeStatus

logger = logging.getLogger(__name__)

# ── 时间格式 ────────────────────────────────────────────────────────────

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_ISO_FMT_NO_MS = "%Y-%m-%dT%H:%M:%SZ"


def _to_iso(dt: datetime) -> str:
    """datetime → UTC ISO 8601（毫秒，Z 后缀）。naive 视为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(s: str) -> datetime:
    """ISO 字符串 → aware UTC datetime。兼容带/不带毫秒。"""
    for fmt in (_ISO_FMT, _ISO_FMT_NO_MS, "%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _to_iso(_now_utc())


def _to_cycle_id(dt: datetime) -> str:
    """datetime → cycle_id ``YYYYMMDDTHHMMSSZ``（UTC，粒度到秒）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backoff_seconds(attempts: int) -> float:
    """行级重试退避: min(6h, 10min * 2^attempts)。"""
    return min(6 * 3600.0, 600.0 * (2 ** max(attempts, 0)))


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_bound(value: str | None, exclusive: bool = False) -> datetime | None:
    """解析 --since/--until 过滤器值。

    支持 "YYYY-MM-DD"（exclusive 时取次日零点）或完整 ISO 字符串。
    返回 aware UTC datetime，None 表示无过滤。
    """
    if not value:
        return None
    s = str(value).strip()
    if _DATE_ONLY_RE.fullmatch(s):
        dt = datetime.strptime(s, "%Y-%m-%d")
        if exclusive:
            dt = dt + timedelta(days=1)
        return dt.replace(tzinfo=timezone.utc)
    try:
        return _parse_iso(s)
    except ValueError as exc:
        raise ValueError(f"无法解析时间过滤值 {value!r}（支持 YYYY-MM-DD 或 ISO 8601）") from exc


# ── 认领行结构 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClaimedEpisode:
    """IngestWorker 从 SQLite 认领到的一行（读 JSONL 所需的最小信息）。"""

    content_hash: str
    name: str
    source_type: str
    batch_file: str
    line_no: int


# ── LandingStore ────────────────────────────────────────────────────────


class LandingStore:
    """JSON 持久化层（landing zone）: SQLite 状态索引 + JSONL 快照文件。

    线程模型: 单进程 asyncio 事件循环内同步调用（每次操作都很短）。跨进程
    场景由 SQLite WAL + BEGIN IMMEDIATE 原子认领保证安全。
    """

    def __init__(
        self,
        landing_dir: str | os.PathLike[str] = "data/landing",
        db_path: str | os.PathLike[str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        """初始化 landing 根目录 + SQLite 状态库。

        Args:
            landing_dir: landing 根目录（相对路径基于 cwd 解析）。
            db_path: state.db 路径，默认 ``{landing_dir}/state.db``。
            timeout: sqlite3 busy timeout（秒）。
        """
        self._landing_dir = Path(landing_dir)
        self._landing_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = Path(db_path) if db_path is not None else self._landing_dir / "state.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=timeout,
            isolation_level=None,  # autocommit; 事务用显式 BEGIN IMMEDIATE
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

        # 进程唯一认领者标识（日志区分 / 崩溃诊断）
        self._lease_owner: str = uuid.uuid4().hex
        logger.info(
            "LandingStore ready: dir=%s db=%s owner=%s",
            self._landing_dir,
            self._db_path,
            self._lease_owner,
        )

    # ── schema ────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        """建表（幂等）。设计 §4.3。"""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS landed_episodes (
                content_hash  TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                source_type   TEXT NOT NULL,
                batch_file    TEXT NOT NULL,
                line_no       INTEGER NOT NULL,
                valid_at      TEXT NOT NULL,
                captured_at   TEXT NOT NULL,
                cycle_id      TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','processing','done','skipped','failed','dead')),
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT,
                lease_owner   TEXT,
                lease_expires TEXT,
                updated_at    TEXT NOT NULL,
                ingested_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_status_captured ON landed_episodes(status, captured_at);
            CREATE INDEX IF NOT EXISTS idx_source_status  ON landed_episodes(source_type, status);
            CREATE INDEX IF NOT EXISTS idx_batch          ON landed_episodes(batch_file);

            CREATE TABLE IF NOT EXISTS capture_runs (
                cycle_id    TEXT NOT NULL,
                source_type TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                batch_file  TEXT NOT NULL,
                total       INTEGER NOT NULL,
                new_rows    INTEGER NOT NULL,
                dup_rows    INTEGER NOT NULL,
                PRIMARY KEY (cycle_id, source_type)
            );
            """
        )

    # ── Stage A: capture ──────────────────────────────────────────────

    def capture_batch(
        self,
        episodes: list[NormalizedEpisode],
        source_type: str,
        captured_at: datetime | None = None,
    ) -> CaptureRunRecord:
        """写 JSONL（原子 tmp+rename）+ 登记 SQLite pending。

        顺序理由（设计 §3.4）: 文件是「真相」，SQLite 是「索引」。先落文件、
        后写索引。若 rename 后崩溃（文件存在无索引），启动时「孤儿文件扫描」
        幂等补登记。

        Args:
            episodes: 已 dedup 的 NormalizedEpisode 列表。
            source_type: adapter 源类型（batch_file / capture_runs 用）。
            captured_at: 抓取时间（默认 now UTC）；派生 cycle_id 与日期分区。

        Returns:
            CaptureRunRecord: total / new_rows（实际 INSERT）/ dup_rows。
        """
        if not episodes:
            return CaptureRunRecord(
                cycle_id=_to_cycle_id(captured_at or _now_utc()),
                source_type=source_type,
                captured_at=_to_iso(captured_at or _now_utc()),
                batch_file="",
                total=0,
                new_rows=0,
                dup_rows=0,
            )
        captured_at = captured_at or _now_utc()
        cycle_id = _to_cycle_id(captured_at)
        captured_iso = _to_iso(captured_at)
        date_dir = captured_at.astimezone(timezone.utc).strftime("%Y-%m-%d")

        rel_path = f"{date_dir}/{source_type}-{cycle_id}.jsonl"
        abs_path = self._landing_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) 序列化所有行（envelope 自包含快照）
        lines: list[tuple[int, str]] = []
        for line_no, ep in enumerate(episodes):
            envelope = EpisodeEnvelope(
                v=1,
                captured_at=captured_iso,
                cycle_id=cycle_id,
                episode=ep,
            )
            lines.append((line_no, envelope.model_dump_json()))

        # 2) 原子写 JSONL: tmp → fsync → os.replace
        self._write_jsonl_atomic(abs_path, [line for _, line in lines])

        # 3) 事务登记 + capture_runs（文件是真相；SQLite 是索引）
        new_rows = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for line_no, _ in lines:
                ep = episodes[line_no]
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO landed_episodes
                        (content_hash, name, source_type, batch_file, line_no,
                         valid_at, captured_at, cycle_id, status, attempts,
                         updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (
                        ep.content_hash,
                        ep.name,
                        source_type,
                        rel_path,
                        line_no,
                        _to_iso(ep.valid_at),
                        captured_iso,
                        cycle_id,
                        captured_iso,
                    ),
                )
                new_rows += cur.rowcount
            self._conn.execute(
                """
                INSERT INTO capture_runs
                    (cycle_id, source_type, captured_at, batch_file, total, new_rows, dup_rows)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, source_type) DO UPDATE SET
                    captured_at=excluded.captured_at,
                    batch_file=excluded.batch_file,
                    total=excluded.total,
                    new_rows=excluded.new_rows,
                    dup_rows=excluded.dup_rows
                """,
                (
                    cycle_id,
                    source_type,
                    captured_iso,
                    rel_path,
                    len(episodes),
                    new_rows,
                    len(episodes) - new_rows,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        logger.info(
            "landing capture [%s]: %s — total=%d new=%d dup=%d",
            source_type,
            rel_path,
            len(episodes),
            new_rows,
            len(episodes) - new_rows,
        )
        return CaptureRunRecord(
            cycle_id=cycle_id,
            source_type=source_type,
            captured_at=captured_iso,
            batch_file=rel_path,
            total=len(episodes),
            new_rows=new_rows,
            dup_rows=len(episodes) - new_rows,
        )

    # ── Stage B: claim / status transitions ───────────────────────────

    def claim_batch(
        self,
        batch_size: int = 20,
        lease_sec: int = 900,
        lease_owner: str | None = None,
    ) -> list[ClaimedEpisode]:
        """原子认领 pending 批（BEGIN IMMEDIATE）：pending → processing + lease。

        设计 §4.6: 认领必须原子，避免 scheduler 常驻与手动 --ingest-only 并发
        时重复认领。SQLite 写事务串行化保证同一行只会被一个进程认领。

        Args:
            batch_size: 每轮认领行数。
            lease_sec: lease 超时（秒），须 > write_one 最坏耗时（429 退避下可达数分钟）。
            lease_owner: 认领者标识（默认本进程 uuid）。

        Returns:
            认领到的行列表（按 captured_at 保序）。
        """
        if batch_size <= 0:
            return []
        owner = lease_owner or self._lease_owner
        now = _now_utc()
        now_iso = _to_iso(now)
        expires_iso = _to_iso(now + timedelta(seconds=max(lease_sec, 1)))

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """
                SELECT content_hash, name, source_type, batch_file, line_no
                FROM landed_episodes
                WHERE status = 'pending'
                ORDER BY captured_at, content_hash
                LIMIT ?
                """,
                (batch_size,),
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    """
                    UPDATE landed_episodes
                    SET status='processing', lease_owner=?, lease_expires=?,
                        updated_at=?
                    WHERE content_hash=?
                    """,
                    (owner, expires_iso, now_iso, r[0]),
                )
            self._conn.commit()
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return [
            ClaimedEpisode(
                content_hash=r[0],
                name=r[1],
                source_type=r[2],
                batch_file=r[3],
                line_no=r[4],
            )
            for r in rows
        ]

    def complete(self, content_hash: str, ingested_at: datetime | None = None) -> None:
        """processing → done（write_one 返回 ok）。"""
        now = _to_iso(ingested_at or _now_utc())
        self._conn.execute(
            """
            UPDATE landed_episodes
            SET status='done', ingested_at=?, last_error=NULL,
                lease_owner=NULL, lease_expires=NULL, updated_at=?
            WHERE content_hash=?
            """,
            (now, now, content_hash),
        )

    def skip(self, content_hash: str, ingested_at: datetime | None = None) -> None:
        """processing → skipped（write_one 返回 skipped_duplicate），视同完成。"""
        now = _to_iso(ingested_at or _now_utc())
        self._conn.execute(
            """
            UPDATE landed_episodes
            SET status='skipped', ingested_at=?, last_error=NULL,
                lease_owner=NULL, lease_expires=NULL, updated_at=?
            WHERE content_hash=?
            """,
            (now, now, content_hash),
        )

    def fail(
        self,
        content_hash: str,
        error: str | None = None,
        max_attempts: int = 3,
    ) -> None:
        """processing → failed（attempts++）；attempts >= max → dead。

        设计 §4.4: write_one 内部重试保持（429 退避/熔断）；本方法只处理
        「write_one 最终仍失败」的情况。行级重试由 ``retry_failed`` 按退避
        周期移回 pending。
        """
        now = _now_iso()
        self._conn.execute(
            """
            UPDATE landed_episodes
            SET status='failed', attempts=attempts+1, last_error=?,
                lease_owner=NULL, lease_expires=NULL, updated_at=?
            WHERE content_hash=?
            """,
            (error, now, content_hash),
        )
        row = self._conn.execute(
            "SELECT attempts FROM landed_episodes WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if row is not None and row[0] >= max_attempts:
            self._conn.execute(
                "UPDATE landed_episodes SET status='dead', updated_at=? WHERE content_hash=?",
                (now, content_hash),
            )
            logger.warning(
                "episode %s… dead after %d attempts (max=%d) — 需人工介入 (--retry-dead)",
                content_hash[:12],
                row[0],
                max_attempts,
            )

    # ── recovery ──────────────────────────────────────────────────────

    def recover_leases(self, now: datetime | None = None) -> int:
        """processing 超 lease → 复位 pending（进程崩溃恢复）。

        设计 §4.4/§8.2: 崩溃后 processing 行在下个启动/周期扫描中复位，
        断点续传。lease_expires 为 NULL 的 processing 行一并复位（防御）。
        """
        now = now or _now_utc()
        now_iso = _to_iso(now)
        cur = self._conn.execute(
            """
            UPDATE landed_episodes
            SET status='pending', lease_owner=NULL, lease_expires=NULL, updated_at=?
            WHERE status='processing' AND (lease_expires IS NULL OR lease_expires < ?)
            """,
            (now_iso, now_iso),
        )
        if cur.rowcount:
            logger.warning("recover_leases: reset %d expired lease(s) to pending", cur.rowcount)
        return cur.rowcount

    def retry_failed(self, max_attempts: int = 3, now: datetime | None = None) -> int:
        """failed → pending（attempts < max 且退避已过）；attempts >= max → dead。

        设计 §4.5: backoff = min(6h, 10min * 2^attempts)。重试不阻塞 pending
        正常消费（由 IngestWorker 在队列空时扫描）。返回移回 pending 的行数。
        """
        now = now or _now_utc()
        now_iso = _to_iso(now)
        rows = self._conn.execute(
            "SELECT content_hash, attempts, updated_at FROM landed_episodes WHERE status='failed'"
        ).fetchall()
        moved = 0
        for content_hash, attempts, updated_at in rows:
            if attempts >= max_attempts:
                self._conn.execute(
                    "UPDATE landed_episodes SET status='dead', updated_at=? WHERE content_hash=?",
                    (now_iso, content_hash),
                )
                logger.warning(
                    "retry_failed: episode %s… dead after %d attempts (max=%d)",
                    content_hash[:12],
                    attempts,
                    max_attempts,
                )
                continue
            try:
                last = _parse_iso(updated_at)
            except ValueError:
                last = now
            if (now - last).total_seconds() >= _backoff_seconds(attempts):
                self._conn.execute(
                    """
                    UPDATE landed_episodes
                    SET status='pending', lease_owner=NULL, lease_expires=NULL, updated_at=?
                    WHERE content_hash=?
                    """,
                    (now_iso, content_hash),
                )
                moved += 1
        if moved:
            logger.info("retry_failed: %d episode(s) re-queued to pending", moved)
        return moved

    # ── file IO ───────────────────────────────────────────────────────

    def read_envelope(self, batch_file: str, line_no: int) -> EpisodeEnvelope:
        """读 JSONL 一行 → EpisodeEnvelope（自包含快照）。

        Raises:
            FileNotFoundError: batch 文件缺失（DB 已登记但文件被删）。
            json.JSONDecodeError / pydantic.ValidationError: 该行损坏。
        """
        path = self._landing_dir / batch_file
        if not path.exists():
            raise FileNotFoundError(f"batch file missing: {batch_file}")
        with open(path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if idx != line_no:
                    continue
                stripped = line.strip()
                if not stripped:
                    raise ValueError(f"empty line {line_no} in {batch_file}")
                data = json.loads(stripped)
                return EpisodeEnvelope.model_validate(data)
        raise IndexError(f"line {line_no} not found in {batch_file}")

    @staticmethod
    def _write_jsonl_atomic(abs_path: Path, lines: list[str]) -> None:
        """写入 JSONL 的原子操作: tmp → fsync → os.replace（设计 §3.4）。"""
        tmp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(line)
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_path)

    # ── orphan scan / rebuild ─────────────────────────────────────────

    def _scan_jsonl_files(self) -> list[Path]:
        """列出 landing 下所有 JSONL（字典序，即按时间排序）。"""
        return sorted(self._landing_dir.rglob("*.jsonl"))

    def _register_file(self, abs_path: Path) -> tuple[int, int, int]:
        """把一个 JSONL 文件的所有行 INSERT OR IGNORE 进索引 + 登记 capture_runs。

        幂等: content_hash PK 保证重复登记安全。损坏行跳过并告警（单行损坏
        不中断整批，设计 §8.3）。

        Returns:
            (total, new_rows, dup_rows)
        """
        rel = str(abs_path.relative_to(self._landing_dir))
        total = 0
        new_rows = 0
        cycle_id = ""
        captured_at = ""
        with open(abs_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f):
                stripped = line.strip()
                if not stripped:
                    continue
                total += 1
                try:
                    data = json.loads(stripped)
                    env = EpisodeEnvelope.model_validate(data)
                except Exception as exc:
                    logger.warning(
                        "corrupt line in %s:%d (skipped): %s", rel, line_no, exc
                    )
                    continue
                ep = env.episode
                if not cycle_id:
                    cycle_id = env.cycle_id
                    captured_at = env.captured_at
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO landed_episodes
                        (content_hash, name, source_type, batch_file, line_no,
                         valid_at, captured_at, cycle_id, status, attempts, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (
                        ep.content_hash,
                        ep.name,
                        env.episode.source_type,
                        rel,
                        line_no,
                        _to_iso(ep.valid_at),
                        env.captured_at,
                        env.cycle_id,
                        _now_iso(),
                    ),
                )
                new_rows += cur.rowcount

        if total:
            if not cycle_id:
                # 文件名兜底: {source_type}-{cycle_id}.jsonl
                stem = abs_path.stem
                if "-" in stem:
                    cycle_id = stem.rsplit("-", 1)[1]
                if not captured_at:
                    captured_at = _now_iso()
            self._conn.execute(
                """
                INSERT INTO capture_runs
                    (cycle_id, source_type, captured_at, batch_file, total, new_rows, dup_rows)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, source_type) DO UPDATE SET
                    captured_at=excluded.captured_at,
                    batch_file=excluded.batch_file,
                    total=excluded.total,
                    new_rows=excluded.new_rows,
                    dup_rows=excluded.dup_rows
                """,
                (cycle_id, env_or_source_type(abs_path, cycle_id), captured_at, rel, total, new_rows, total - new_rows),
            )
        return total, new_rows, total - new_rows

    def scan_orphan_files(self) -> int:
        """孤儿文件扫描: capture_runs 未记录的 JSONL → 幂等补登记。

        设计 §8.2/§8.3: 文件已写、DB 未登记（rename 后崩溃）时的启动恢复。
        返回新 INSERT 的行数（纯 dup 返回 0）。
        """
        existing = {
            r[0]
            for r in self._conn.execute("SELECT batch_file FROM capture_runs").fetchall()
        }
        orphans = [
            f for f in self._scan_jsonl_files() if str(f.relative_to(self._landing_dir)) not in existing
        ]
        inserted = 0
        for f in orphans:
            _, new_rows, _ = self._register_file(f)
            inserted += new_rows
            logger.info(
                "orphan file registered: %s (new=%d)",
                f.relative_to(self._landing_dir),
                new_rows,
            )
        if inserted:
            logger.info("scan_orphan_files: registered %d orphan row(s)", inserted)
        return inserted

    def rebuild_index(self) -> dict[str, int]:
        """重建索引: 扫描所有 JSONL 重建 SQLite（--rebuild-index，设计 §8.3）。

        注意: 重建后 done/skipped 状态丢失 → 已入库 episode 可能重写。
        缓解: 重建前优先尝试 SQLite 自愈（WAL 恢复）；graphiti 侧 name 唯一性
        提供幂等兜底。此操作记录告警。
        """
        files = self._scan_jsonl_files()
        total_lines = 0
        new_rows = 0
        dup_rows = 0
        for f in files:
            t, n, d = self._register_file(f)
            total_lines += t
            new_rows += n
            dup_rows += d
        logger.warning(
            "rebuild_index: %d file(s), %d line(s), %d new, %d dup — "
            "done/skipped 状态已丢失，已入库 episode 可能被重写（依赖 graphiti name 幂等）",
            len(files),
            total_lines,
            new_rows,
            dup_rows,
        )
        return {"files": len(files), "lines": total_lines, "new_rows": new_rows, "dup_rows": dup_rows}

    def cleanup_tmp_files(self) -> int:
        """删除残留 .tmp 文件（写入中途崩溃，设计 §8.3）。返回删除数。"""
        removed = 0
        for f in sorted(self._landing_dir.rglob("*.tmp")):
            try:
                f.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("cleanup_tmp_files: %s: %s", f, exc)
        if removed:
            logger.info("cleanup_tmp_files: removed %d tmp file(s)", removed)
        return removed

    # ── ops: replay / retry-dead / retention / stats ─────────────────

    def replay(
        self,
        since: str | None = None,
        until: str | None = None,
        source_type: str | None = None,
    ) -> int:
        """把 done/skipped 复位为 pending（--replay，设计 §6）。

        配合调整 entity_types 后重放：复位时清除 attempts/last_error/ingested_at，
        重放时 write_one 读当前 entity_types。
        """
        since_dt = _parse_bound(since)
        until_dt = _parse_bound(until, exclusive=True)
        where = ["status IN ('done','skipped')"]
        params: list[Any] = []
        if since_dt is not None:
            where.append("captured_at >= ?")
            params.append(_to_iso(since_dt))
        if until_dt is not None:
            where.append("captured_at < ?")
            params.append(_to_iso(until_dt))
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        params.insert(0, _now_iso())
        sql = (
            "UPDATE landed_episodes SET status='pending', attempts=0, last_error=NULL, "
            "lease_owner=NULL, lease_expires=NULL, ingested_at=NULL, updated_at=? "
            f"WHERE {' AND '.join(where)}"
        )
        cur = self._conn.execute(sql, params)
        logger.info(
            "replay: %d episode(s) reset to pending (since=%s until=%s source=%s)",
            cur.rowcount,
            since or "*",
            until or "*",
            source_type or "*",
        )
        return cur.rowcount

    def retry_dead(self) -> int:
        """dead → pending 且 attempts=0（--retry-dead，人工确认后恢复）。"""
        cur = self._conn.execute(
            """
            UPDATE landed_episodes
            SET status='pending', attempts=0, last_error=NULL,
                lease_owner=NULL, lease_expires=NULL, ingested_at=NULL, updated_at=?
            WHERE status='dead'
            """,
            (_now_iso(),),
        )
        logger.info("retry_dead: %d episode(s) reset to pending", cur.rowcount)
        return cur.rowcount

    def retention_sweep(self, retention_days: int = 14, now: datetime | None = None) -> int:
        """保留期清理（设计 §5.1）: 删除过期 done/skipped/dead 行 + 对应 JSONL。

        绝不自动删除 pending / processing / failed 的行或文件——未入库数据
        永不清理。返回删除的行数。
        """
        now = now or _now_utc()
        cutoff = now - timedelta(days=max(retention_days, 0))
        cutoff_iso = _to_iso(cutoff)
        cutoff_date = cutoff.strftime("%Y-%m-%d")

        cur = self._conn.execute(
            """
            DELETE FROM landed_episodes
            WHERE captured_at < ? AND status IN ('done','skipped','dead')
            """,
            (cutoff_iso,),
        )
        deleted = cur.rowcount

        # 清理过期日期目录下「无剩余行引用」的 JSONL（目录整体早于保留期）
        for date_dir in sorted(self._landing_dir.glob("????-??-??")):
            if not date_dir.is_dir():
                continue
            if date_dir.name >= cutoff_date:
                continue
            for f in sorted(date_dir.glob("*.jsonl")):
                rel = f"{date_dir.name}/{f.name}"
                referenced = self._conn.execute(
                    "SELECT 1 FROM landed_episodes WHERE batch_file=? LIMIT 1", (rel,)
                ).fetchone()
                if referenced is None:
                    try:
                        f.unlink()
                    except OSError as exc:
                        logger.warning("retention_sweep: unlink %s failed: %s", f, exc)
            try:
                date_dir.rmdir()  # 仅当目录已空
            except OSError:
                pass

        if deleted:
            logger.info(
                "retention_sweep: removed %d row(s) older than %d days",
                deleted,
                retention_days,
            )
        return deleted

    def queue_summary(self, max_attempts: int = 3, now: datetime | None = None) -> dict[str, int]:
        """队列水位（IngestWorker 空闲判断 / 高水位告警用）。"""
        now = now or _now_utc()
        counts = {"pending": 0, "processing": 0, "done": 0, "skipped": 0, "failed": 0, "dead": 0}
        for status, cnt in self._conn.execute(
            "SELECT status, COUNT(*) FROM landed_episodes GROUP BY status"
        ).fetchall():
            if status in counts:
                counts[status] = cnt
        # 当前退避已过、可立即重试的 failed 行
        failed_rows = self._conn.execute(
            "SELECT attempts, updated_at FROM landed_episodes WHERE status='failed'"
        ).fetchall()
        retryable = 0
        for attempts, updated_at in failed_rows:
            if attempts >= max_attempts:
                continue
            try:
                last = _parse_iso(updated_at)
            except ValueError:
                last = now
            if (now - last).total_seconds() >= _backoff_seconds(attempts):
                retryable += 1
        counts["retryable"] = retryable
        return counts

    def stats(self) -> dict[str, Any]:
        """审计统计（--stats，设计 §6）。按 status / source / 捕获日期分组。"""
        by_status = {
            status: cnt
            for status, cnt in self._conn.execute(
                "SELECT status, COUNT(*) FROM landed_episodes GROUP BY status"
            ).fetchall()
        }
        by_source = {
            source: cnt
            for source, cnt in self._conn.execute(
                "SELECT source_type, COUNT(*) FROM landed_episodes GROUP BY source_type"
            ).fetchall()
        }
        by_date = {
            day: cnt
            for day, cnt in self._conn.execute(
                """
                SELECT substr(captured_at, 1, 10) AS day, COUNT(*)
                FROM landed_episodes GROUP BY day ORDER BY day DESC LIMIT 30
                """
            ).fetchall()
        }
        runs = self._conn.execute("SELECT COUNT(*) FROM capture_runs").fetchone()[0]
        total = self._conn.execute("SELECT COUNT(*) FROM landed_episodes").fetchone()[0]
        jsonl_bytes = sum(f.stat().st_size for f in self._scan_jsonl_files())
        db_bytes = self._db_path.stat().st_size if self._db_path.exists() else 0
        return {
            "total": total,
            "by_status": by_status,
            "by_source": by_source,
            "by_date": by_date,
            "capture_runs": runs,
            "jsonl_bytes": jsonl_bytes,
            "db_bytes": db_bytes,
            "landing_dir": str(self._landing_dir),
        }

    # ── lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """关闭 SQLite 连接。"""
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self) -> None:  # pragma: no cover - defensive
        try:
            self.close()
        except Exception:
            pass


def env_or_source_type(abs_path: Path, cycle_id: str) -> str:
    """从 JSONL 文件名推导 source_type: ``{source_type}-{cycle_id}.jsonl``。"""
    stem = abs_path.stem
    if cycle_id and stem.endswith(cycle_id) and len(stem) > len(cycle_id) + 1:
        return stem[: -(len(cycle_id) + 1)]
    return stem.split("-")[0]


__all__ = ["LandingStore", "ClaimedEpisode", "EpisodeEnvelope", "CaptureRunRecord"]