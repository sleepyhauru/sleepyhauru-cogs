from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Optional


log = logging.getLogger("red.implingfinder.metrics")

EVENT_RETENTION_DAYS = 7
AGGREGATE_RETENTION_DAYS = 30
DEFAULT_QUEUE_SIZE = 2048
MAX_BATCH_SIZE = 100
MAINTENANCE_INTERVAL_SECONDS = 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 5
LATENCY_FIELDS = ("duration_ms", "fetch_ms", "process_ms", "render_ms", "send_ms", "end_to_end_ms")
LATENCY_KINDS = {
    "fetch_ms": "fetch",
    "process_ms": "poll",
    "render_ms": "render",
    "send_ms": "post",
    "end_to_end_ms": "post",
}


@dataclass(frozen=True)
class MetricEvent:
    kind: str
    outcome: str = "ok"
    occurred_at: float = field(default_factory=time.time)
    guild_id: Optional[str] = None
    guild_name: Optional[str] = None
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    impling_type: Optional[str] = None
    world: Optional[int] = None
    location: Optional[str] = None
    duration_ms: Optional[float] = None
    fetch_ms: Optional[float] = None
    process_ms: Optional[float] = None
    render_ms: Optional[float] = None
    send_ms: Optional[float] = None
    end_to_end_ms: Optional[float] = None
    items_count: Optional[int] = None
    count_value: int = 1
    error_category: Optional[str] = None


class MetricsStore:
    def __init__(
        self,
        db_path: Path,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        event_retention_days: int = EVENT_RETENTION_DAYS,
        aggregate_retention_days: int = AGGREGATE_RETENTION_DAYS,
    ):
        self.db_path = Path(db_path)
        self.queue: asyncio.Queue[MetricEvent] = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self.event_retention_days = max(1, int(event_retention_days))
        self.aggregate_retention_days = max(self.event_retention_days, int(aggregate_retention_days))
        self.dropped_events = 0
        self.write_failures = 0
        self.last_write_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.event_loop_lag_ms = 0.0
        self.started_at = time.time()
        self._running = False
        self._writer_task: Optional[asyncio.Task] = None
        self._maintenance_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def queue_depth(self) -> int:
        return self.queue.qsize()

    def record(self, event: MetricEvent) -> bool:
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped_events += max(1, int(event.count_value))
            return False

    async def start(self) -> None:
        if self._running:
            return
        await asyncio.to_thread(self._initialize_sync)
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop(), name="implingfinder-metrics-writer")
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name="implingfinder-metrics-maintenance",
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="implingfinder-metrics-heartbeat",
        )

    async def stop(self) -> None:
        if not self._running:
            return
        await self.flush()
        self._running = False
        for task in (self._writer_task, self._maintenance_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        for task in (self._writer_task, self._maintenance_task, self._heartbeat_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._writer_task = None
        self._maintenance_task = None
        self._heartbeat_task = None

    async def flush(self) -> None:
        if not self._running and self.queue_depth:
            await asyncio.to_thread(self._write_batch_sync, self._drain_queue())
            return
        await self.queue.join()

    async def prune(self, *, now: Optional[float] = None) -> None:
        await asyncio.to_thread(self._prune_sync, now or time.time())

    async def summary(
        self,
        *,
        hours: int,
        guild_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._summary_sync,
            max(1, min(int(hours), self.aggregate_retention_days * 24)),
            guild_id,
            now or time.time(),
        )

    async def hourly(
        self,
        *,
        hours: int,
        guild_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._hourly_sync,
            max(1, min(int(hours), self.aggregate_retention_days * 24)),
            guild_id,
            now or time.time(),
        )

    async def recent_events(
        self,
        *,
        limit: int,
        guild_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._recent_events_sync,
            max(1, min(int(limit), 200)),
            guild_id,
        )

    async def servers(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._servers_sync)

    def health(self) -> dict[str, Any]:
        database_bytes = 0
        try:
            database_bytes = self.db_path.stat().st_size
            wal_path = Path(f"{self.db_path}-wal")
            if wal_path.exists():
                database_bytes += wal_path.stat().st_size
        except OSError:
            pass
        status = "ok"
        if self.last_error or self.write_failures or self.dropped_events:
            status = "degraded"
        if not self._running:
            status = "stopped"
        return {
            "status": status,
            "running": self._running,
            "uptime_seconds": max(0, round(time.time() - self.started_at)),
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue.maxsize,
            "dropped_events": self.dropped_events,
            "write_failures": self.write_failures,
            "last_write_at": _iso_timestamp(self.last_write_at),
            "last_error": self.last_error,
            "event_loop_lag_ms": round(self.event_loop_lag_ms, 2),
            "rss_bytes": _current_rss_bytes(),
            "database_bytes": database_bytes,
            "event_retention_days": self.event_retention_days,
            "aggregate_retention_days": self.aggregate_retention_days,
        }

    async def _writer_loop(self) -> None:
        while True:
            first = await self.queue.get()
            batch = [first]
            while len(batch) < MAX_BATCH_SIZE:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self._write_batch_sync, batch)
                self.last_write_at = time.time()
                self.last_error = None
            except Exception as exc:
                self.write_failures += len(batch)
                self.last_error = type(exc).__name__
                log.exception("Impling Finder metrics batch write failed")
            finally:
                for _event in batch:
                    self.queue.task_done()

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
            try:
                await self.prune()
            except Exception:
                log.exception("Impling Finder metrics retention cleanup failed")

    async def _heartbeat_loop(self) -> None:
        expected = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            now = time.monotonic()
            self.event_loop_lag_ms = max(0.0, (now - expected) * 1000)
            expected = now + HEARTBEAT_INTERVAL_SECONDS

    def _drain_queue(self) -> list[MetricEvent]:
        batch: list[MetricEvent] = []
        while True:
            try:
                batch.append(self.queue.get_nowait())
                self.queue.task_done()
            except asyncio.QueueEmpty:
                return batch

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.executescript(_SCHEMA)
                self._migrate_sync(connection)
        self._prune_sync(time.time())

    def _write_batch_sync(self, events: Iterable[MetricEvent]) -> None:
        rows = [event for event in events]
        if not rows:
            return
        with closing(self._connect()) as connection:
            with connection:
                for event in rows:
                    values = _event_values(event)
                    connection.execute(_INSERT_EVENT, values)
                    connection.execute(_UPSERT_HOURLY, _aggregate_values(event))

    def _prune_sync(self, now: float) -> None:
        event_cutoff = float(now) - self.event_retention_days * 86400
        aggregate_cutoff = int(float(now) - self.aggregate_retention_days * 86400)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM events WHERE occurred_at < ?", (event_cutoff,))
                connection.execute("DELETE FROM hourly_metrics WHERE hour_start < ?", (aggregate_cutoff,))

    def _summary_sync(self, hours: int, guild_id: Optional[str], now: float) -> dict[str, Any]:
        cutoff = int(now - hours * 3600)
        where, params = _aggregate_where(cutoff, guild_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM hourly_metrics WHERE {where}",
                params,
            ).fetchall()

        totals = {
            "fetches": 0,
            "polls": 0,
            "posts": 0,
            "errors": 0,
            "despawns": 0,
            "duplicates": 0,
            "routed": 0,
        }
        for row in rows:
            count = int(row["event_count"])
            if row["kind"] == "fetch":
                totals["fetches"] += count
            elif row["kind"] == "poll":
                totals["polls"] += count
            elif row["kind"] == "post" and row["outcome"] == "ok":
                totals["posts"] += count
            elif row["kind"] == "despawn" and row["outcome"] == "ok":
                totals["despawns"] += count
            elif row["kind"] == "duplicate":
                totals["duplicates"] += count
            elif row["kind"] == "routed":
                totals["routed"] += count
            if row["outcome"] != "ok":
                totals["errors"] += count

        latency = {}
        for field_name in LATENCY_FIELDS:
            display_name = field_name.removesuffix("_ms")
            expected_kind = LATENCY_KINDS.get(field_name)
            latency_rows = [
                row for row in rows if expected_kind is None or row["kind"] == expected_kind
            ]
            total_sum = sum(float(row[f"{field_name}_sum"]) for row in latency_rows)
            total_count = sum(int(row[f"{field_name}_count"]) for row in latency_rows)
            maxima = [
                float(row[f"{field_name}_max"])
                for row in latency_rows
                if row[f"{field_name}_max"] is not None
            ]
            latency[display_name] = {
                "average": round(total_sum / total_count, 2) if total_count else None,
                "maximum": round(max(maxima), 2) if maxima else None,
                "samples": total_count,
            }

        return {
            "generated_at": _iso_timestamp(now),
            "hours": hours,
            "guild_id": guild_id,
            "totals": totals,
            "latency_ms": latency,
        }

    def _hourly_sync(self, hours: int, guild_id: Optional[str], now: float) -> list[dict[str, Any]]:
        cutoff = int(now - hours * 3600)
        where, params = _aggregate_where(cutoff, guild_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM hourly_metrics WHERE {where} ORDER BY hour_start ASC, kind ASC",
                params,
            ).fetchall()
        return [_hourly_row(row) for row in rows]

    def _recent_events_sync(self, limit: int, guild_id: Optional[str]) -> list[dict[str, Any]]:
        where = "1 = 1"
        params: list[Any] = []
        if guild_id:
            where += " AND guild_id = ?"
            params.append(str(guild_id))
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM events WHERE {where} ORDER BY occurred_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_event_row(row) for row in rows]

    def _servers_sync(self) -> list[dict[str, str]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT guild_id, MAX(guild_name) AS guild_name
                FROM (
                    SELECT guild_id, guild_name FROM events WHERE guild_id != ''
                    UNION ALL
                    SELECT guild_id, guild_name FROM hourly_metrics WHERE guild_id != ''
                )
                GROUP BY guild_id
                ORDER BY LOWER(MAX(guild_name)), guild_id
                """
            ).fetchall()
        return [
            {"id": str(row["guild_id"]), "name": str(row["guild_name"] or row["guild_id"])}
            for row in rows
        ]

    def _migrate_sync(self, connection: sqlite3.Connection) -> None:
        event_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "items_count" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN items_count INTEGER")


def _event_values(event: MetricEvent) -> dict[str, Any]:
    values = asdict(event)
    for field_name in (
        "guild_id",
        "guild_name",
        "channel_id",
        "channel_name",
        "impling_type",
        "location",
        "error_category",
    ):
        values[field_name] = values[field_name] or ""
    values["count_value"] = max(1, int(values["count_value"]))
    return values


def _aggregate_values(event: MetricEvent) -> dict[str, Any]:
    values = _event_values(event)
    values["hour_start"] = int(event.occurred_at // 3600 * 3600)
    values["error_count"] = values["count_value"] if event.outcome != "ok" else 0
    for field_name in LATENCY_FIELDS:
        value = values[field_name]
        values[f"{field_name}_sum"] = float(value) if value is not None else 0.0
        values[f"{field_name}_count"] = 1 if value is not None else 0
        values[f"{field_name}_min"] = value
        values[f"{field_name}_max"] = value
    return values


def _aggregate_where(cutoff: int, guild_id: Optional[str]) -> tuple[str, list[Any]]:
    where = "hour_start >= ?"
    params: list[Any] = [cutoff]
    if guild_id:
        where += " AND guild_id = ?"
        params.append(str(guild_id))
    return where, params


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("id", None)
    result["occurred_at"] = _iso_timestamp(result["occurred_at"])
    for key, value in list(result.items()):
        if value == "":
            result[key] = None
    return result


def _hourly_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "hour": _iso_timestamp(row["hour_start"]),
        "kind": row["kind"],
        "outcome": row["outcome"],
        "guild_id": row["guild_id"] or None,
        "guild_name": row["guild_name"] or None,
        "impling_type": row["impling_type"] or None,
        "count": int(row["event_count"]),
        "errors": int(row["error_count"]),
        "latency_ms": {},
    }
    for field_name in LATENCY_FIELDS:
        count = int(row[f"{field_name}_count"])
        result["latency_ms"][field_name.removesuffix("_ms")] = {
            "average": round(float(row[f"{field_name}_sum"]) / count, 2) if count else None,
            "minimum": row[f"{field_name}_min"],
            "maximum": row[f"{field_name}_max"],
            "samples": count,
        }
    return result


def _iso_timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _current_rss_bytes() -> int:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        return resident_pages * page_size
    except (OSError, ValueError, IndexError):
        return 0


_EVENT_COLUMNS = (
    "occurred_at, kind, outcome, guild_id, guild_name, channel_id, channel_name, "
    "impling_type, world, location, duration_ms, fetch_ms, process_ms, render_ms, "
    "send_ms, end_to_end_ms, items_count, count_value, error_category"
)
_INSERT_EVENT = (
    f"INSERT INTO events ({_EVENT_COLUMNS}) VALUES ("
    ":occurred_at, :kind, :outcome, :guild_id, :guild_name, :channel_id, :channel_name, "
    ":impling_type, :world, :location, :duration_ms, :fetch_ms, :process_ms, :render_ms, "
    ":send_ms, :end_to_end_ms, :items_count, :count_value, :error_category)"
)

_HOURLY_LATENCY_COLUMNS = ", ".join(
    f"{field_name}_sum, {field_name}_count, {field_name}_min, {field_name}_max"
    for field_name in LATENCY_FIELDS
)
_HOURLY_LATENCY_VALUES = ", ".join(
    f":{field_name}_sum, :{field_name}_count, :{field_name}_min, :{field_name}_max"
    for field_name in LATENCY_FIELDS
)
_HOURLY_LATENCY_UPDATES = ", ".join(
    (
        f"{field_name}_sum = {field_name}_sum + excluded.{field_name}_sum, "
        f"{field_name}_count = {field_name}_count + excluded.{field_name}_count, "
        f"{field_name}_min = CASE "
        f"WHEN excluded.{field_name}_min IS NULL THEN {field_name}_min "
        f"WHEN {field_name}_min IS NULL THEN excluded.{field_name}_min "
        f"ELSE MIN({field_name}_min, excluded.{field_name}_min) END, "
        f"{field_name}_max = CASE "
        f"WHEN excluded.{field_name}_max IS NULL THEN {field_name}_max "
        f"WHEN {field_name}_max IS NULL THEN excluded.{field_name}_max "
        f"ELSE MAX({field_name}_max, excluded.{field_name}_max) END"
    )
    for field_name in LATENCY_FIELDS
)
_UPSERT_HOURLY = f"""
    INSERT INTO hourly_metrics (
        hour_start, kind, outcome, guild_id, guild_name, impling_type,
        event_count, error_count, {_HOURLY_LATENCY_COLUMNS}
    ) VALUES (
        :hour_start, :kind, :outcome, :guild_id, :guild_name, :impling_type,
        :count_value, :error_count, {_HOURLY_LATENCY_VALUES}
    )
    ON CONFLICT(hour_start, kind, outcome, guild_id, impling_type) DO UPDATE SET
        guild_name = CASE WHEN excluded.guild_name != '' THEN excluded.guild_name ELSE guild_name END,
        event_count = event_count + excluded.event_count,
        error_count = error_count + excluded.error_count,
        {_HOURLY_LATENCY_UPDATES}
"""

_LATENCY_SCHEMA = ",\n".join(
    (
        f"{field_name}_sum REAL NOT NULL DEFAULT 0, "
        f"{field_name}_count INTEGER NOT NULL DEFAULT 0, "
        f"{field_name}_min REAL, "
        f"{field_name}_max REAL"
    )
    for field_name in LATENCY_FIELDS
)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at REAL NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    guild_name TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    impling_type TEXT NOT NULL DEFAULT '',
    world INTEGER,
    location TEXT NOT NULL DEFAULT '',
    duration_ms REAL,
    fetch_ms REAL,
    process_ms REAL,
    render_ms REAL,
    send_ms REAL,
    end_to_end_ms REAL,
    items_count INTEGER,
    count_value INTEGER NOT NULL DEFAULT 1,
    error_category TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_time_idx ON events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_guild_time_idx ON events(guild_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_kind_time_idx ON events(kind, occurred_at DESC);

CREATE TABLE IF NOT EXISTS hourly_metrics (
    hour_start INTEGER NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    guild_name TEXT NOT NULL DEFAULT '',
    impling_type TEXT NOT NULL DEFAULT '',
    event_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    {_LATENCY_SCHEMA},
    PRIMARY KEY(hour_start, kind, outcome, guild_id, impling_type)
);
CREATE INDEX IF NOT EXISTS hourly_time_idx ON hourly_metrics(hour_start);
CREATE INDEX IF NOT EXISTS hourly_guild_time_idx ON hourly_metrics(guild_id, hour_start);
"""
