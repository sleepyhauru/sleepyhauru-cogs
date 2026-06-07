from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .core import (
    DEFAULT_ENDPOINT,
    DEFAULT_ID_ENDPOINT,
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    IMPLING_ORDER,
    IMPLINGS,
    MapLabel,
    MIN_POLL_INTERVAL_SECONDS,
    build_map_url,
    build_id_endpoint,
    collapse_duplicate_sightings,
    explv_tiles_for_crop,
    filter_stale_spawns,
    npc_ids_for_types,
    parse_backend_payload,
    parse_impling_types,
    resolve_location_name,
    routed_channel_ids,
    sanitize_endpoint_url,
    select_unseen_spawns,
    sighting_key_from_legacy_dedupe_key,
)
from .core import ImplingSpawn
from .dashboard import DASHBOARD_HOST, DASHBOARD_PORT, DashboardServer
from .metrics import MetricEvent, MetricsStore


log = logging.getLogger("red.implingfinder")

REQUEST_TIMEOUT_SECONDS = 12
MAX_BACKOFF_SECONDS = 300
MANUAL_RECENT_MAX_COUNT = 25
CONFIG_IDENTIFIER = 0x1A9D1F1644
COG_DIR = Path(__file__).resolve().parent
AREAS_PATH = COG_DIR / "data" / "areas.json"
IMPLING_ASSET_DIR = COG_DIR / "assets"
MAP_IMAGE_SIZE = 512
MAP_TILE_ZOOM = 10
MAP_CROP_SIZE = 512
IMPLING_ICON_SIZE = 72
MAP_RENDER_SEND_TIMEOUT_SECONDS = 2.0
FEED_CLEANUP_HISTORY_LIMIT = 100
FEED_CLEANUP_INTERVAL_SECONDS = 30.0
DESPAWN_DELETE_DELAY_SECONDS = 30.0
DESPAWN_NOTICE_RETENTION_SECONDS = DESPAWN_DELETE_DELAY_SECONDS
DESPAWN_DEFAULT_IMAGE_URL = "attachment://impling-map.png"
POLL_SUPERVISOR_INTERVAL_SECONDS = MIN_POLL_INTERVAL_SECONDS
SCREENSHOT_WORKER_COUNT = 4
SCREENSHOT_QUEUE_SIZE = 128
MAINTENANCE_WORKER_COUNT = 1
MAINTENANCE_QUEUE_SIZE = 256
ACCESS_ROLE_REASON = "ImplingFinder access reaction"
CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_]+):(\d+)>$")


def _display_impling_name(name: str) -> str:
    if name.endswith(" impling"):
        return f"{name[:-len(' impling')]} Impling"
    return name


class BackendError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class MapTileError(Exception):
    pass


@dataclass(frozen=True)
class ScreenshotEditJob:
    guild: discord.Guild
    channel: Any
    message: discord.Message
    spawn: ImplingSpawn
    fetch_ms: Optional[float]
    fetch_completed_at: Optional[datetime]
    process_ms: Optional[float]


@dataclass(frozen=True)
class MaintenanceJob:
    guild: discord.Guild
    channels: dict[str, list[int]]
    current_sighting_keys: set[str]
    current_sighting_aliases: dict[str, str]
    current_spawns_by_key: dict[str, ImplingSpawn]


class ImplingFinder(commands.Cog):
    """Post recent rare OSRS impling sightings from the Impling Finder backend."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(
            enabled=False,
            poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
            max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
            channels={},
            endpoint=DEFAULT_ENDPOINT,
            id_endpoint=DEFAULT_ID_ENDPOINT,
            screenshots=False,
            announce_existing=False,
            puro_enabled=False,
            puro_channel=None,
            access_reactions={},
        )
        self.config.register_global(seen={}, active_messages={})

        self.session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_runner_tasks: dict[int, asyncio.Task] = {}
        self._failure_counts: dict[int, int] = {}
        self._backoff_until: dict[int, float] = {}
        self._pillow_warning_logged = False
        self._location_areas = self._load_location_areas()
        self.metrics_store: Optional[MetricsStore] = None
        self.dashboard: Optional[DashboardServer] = None
        self._started_at = time.time()
        self._startup_cleaned_guilds: set[int] = set()
        self._screenshot_queue: asyncio.Queue[ScreenshotEditJob] = asyncio.Queue(
            maxsize=SCREENSHOT_QUEUE_SIZE
        )
        self._screenshot_worker_tasks: set[asyncio.Task] = set()
        self._despawn_delete_tasks: set[asyncio.Task] = set()
        self._maintenance_queue: asyncio.Queue[MaintenanceJob] = asyncio.Queue(
            maxsize=MAINTENANCE_QUEUE_SIZE
        )
        self._maintenance_worker_tasks: set[asyncio.Task] = set()
        self._last_feed_cleanup: dict[int, float] = {}
        self._despawn_notice_until_by_channel: dict[str, dict[int, float]] = {}

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        )
        try:
            self.metrics_store = MetricsStore(cog_data_path(self) / "metrics.sqlite3")
            await self.metrics_store.start()
        except Exception:
            self.metrics_store = None
            log.exception("Impling Finder metrics store failed to start")

        if self.metrics_store is not None:
            try:
                self.dashboard = DashboardServer(
                    self.metrics_store,
                    health_provider=self._dashboard_health,
                    host=DASHBOARD_HOST,
                    port=DASHBOARD_PORT,
                )
                await self.dashboard.start()
            except Exception:
                self.dashboard = None
                log.exception(
                    "Impling Finder dashboard failed to start on %s:%s",
                    DASHBOARD_HOST,
                    DASHBOARD_PORT,
                )
        self._start_screenshot_workers()
        self._start_maintenance_workers()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="implingfinder-poller")

    def cog_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        for task in list(self._poll_runner_tasks.values()):
            task.cancel()
        for task in list(self._screenshot_worker_tasks):
            task.cancel()
        for task in list(self._despawn_delete_tasks):
            task.cancel()
        for task in list(self._maintenance_worker_tasks):
            task.cancel()
        self._create_task(self._close_resources())

    def _create_task(self, coro):
        loop = getattr(self.bot, "loop", None)
        if loop is not None and not loop.is_closed():
            return loop.create_task(coro)
        return asyncio.create_task(coro)

    def _create_despawn_delete_task(self, coro) -> asyncio.Task:
        task = self._create_task(coro)
        self._despawn_delete_tasks.add(task)
        task.add_done_callback(lambda completed: self._despawn_delete_tasks.discard(completed))
        return task

    def _start_screenshot_workers(self, *, count: int = SCREENSHOT_WORKER_COUNT) -> None:
        active_count = sum(1 for task in self._screenshot_worker_tasks if not task.done())
        for index in range(max(0, int(count) - active_count)):
            task = self._create_task(self._screenshot_worker_loop())
            self._screenshot_worker_tasks.add(task)
            task.add_done_callback(lambda completed: self._screenshot_worker_tasks.discard(completed))

    def _start_maintenance_workers(self, *, count: int = MAINTENANCE_WORKER_COUNT) -> None:
        active_count = sum(1 for task in self._maintenance_worker_tasks if not task.done())
        for index in range(max(0, int(count) - active_count)):
            task = self._create_task(self._maintenance_worker_loop())
            self._maintenance_worker_tasks.add(task)
            task.add_done_callback(lambda completed: self._maintenance_worker_tasks.discard(completed))

    async def _screenshot_worker_loop(self) -> None:
        while True:
            job = await self._screenshot_queue.get()
            try:
                await self._edit_spawn_message_with_screenshot(
                    job.guild,
                    job.channel,
                    job.message,
                    job.spawn,
                    fetch_ms=job.fetch_ms,
                    fetch_completed_at=job.fetch_completed_at,
                    process_ms=job.process_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Impling Finder screenshot worker failed for message %s in channel %s",
                    getattr(job.message, "id", "?"),
                    getattr(job.channel, "id", "?"),
                )
            finally:
                self._screenshot_queue.task_done()

    async def _maintenance_worker_loop(self) -> None:
        while True:
            job = await self._maintenance_queue.get()
            try:
                await self._run_post_poll_maintenance(
                    job.guild,
                    job.channels,
                    job.current_sighting_keys,
                    job.current_sighting_aliases,
                    job.current_spawns_by_key,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Impling Finder maintenance worker failed for guild %s",
                    getattr(job.guild, "id", "?"),
                )
            finally:
                self._maintenance_queue.task_done()

    async def _close_resources(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        for task in list(self._poll_runner_tasks.values()):
            task.cancel()
        for task in list(self._screenshot_worker_tasks):
            task.cancel()
        for task in list(self._despawn_delete_tasks):
            task.cancel()
        for task in list(self._maintenance_worker_tasks):
            task.cancel()
        if self._poll_runner_tasks:
            await asyncio.gather(*self._poll_runner_tasks.values(), return_exceptions=True)
            self._poll_runner_tasks.clear()
        if self._screenshot_worker_tasks:
            await asyncio.gather(*self._screenshot_worker_tasks, return_exceptions=True)
        if self._despawn_delete_tasks:
            await asyncio.gather(*self._despawn_delete_tasks, return_exceptions=True)
        if self._maintenance_worker_tasks:
            await asyncio.gather(*self._maintenance_worker_tasks, return_exceptions=True)
        if self.dashboard is not None:
            try:
                await self.dashboard.stop()
            except Exception:
                log.exception("Impling Finder dashboard failed to stop cleanly")
            self.dashboard = None
        if self.metrics_store is not None:
            try:
                await self.metrics_store.stop()
            except Exception:
                log.exception("Impling Finder metrics store failed to stop cleanly")
            self.metrics_store = None
        if self.session is not None and not self.session.closed:
            await self.session.close()

    def _record_metric(self, event: MetricEvent) -> None:
        if self.metrics_store is None:
            return
        try:
            self.metrics_store.record(event)
        except Exception:
            log.exception("Impling Finder could not enqueue a metric event")

    def _dashboard_health(self) -> dict[str, Any]:
        now = time.monotonic()
        bot_uptime = getattr(self.bot, "uptime", None)
        if isinstance(bot_uptime, datetime):
            if bot_uptime.tzinfo is None:
                bot_uptime = bot_uptime.replace(tzinfo=timezone.utc)
            uptime_seconds = (
                datetime.now(timezone.utc) - bot_uptime.astimezone(timezone.utc)
            ).total_seconds()
        else:
            uptime_seconds = time.time() - self._started_at
        return {
            "bot_uptime_seconds": max(0, round(uptime_seconds)),
            "active_backoffs": sum(1 for deadline in self._backoff_until.values() if deadline > now),
            "enabled_guilds": len(getattr(self.bot, "guilds", [])),
            "poll_runners": len(self._poll_runner_tasks),
            "screenshot_queue_depth": self._screenshot_queue.qsize(),
            "maintenance_queue_depth": self._maintenance_queue.qsize(),
        }

    async def _wait_until_ready(self) -> None:
        waiter = getattr(self.bot, "wait_until_red_ready", None)
        if waiter is None:
            waiter = getattr(self.bot, "wait_until_ready", None)
        if waiter is not None:
            await waiter()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            )
        return self.session

    async def _poll_loop(self) -> None:
        await self._wait_until_ready()
        while True:
            try:
                await self._sync_poll_runners()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Impling Finder poll supervisor hit an unexpected error")
            await asyncio.sleep(POLL_SUPERVISOR_INTERVAL_SECONDS)

    async def _sync_poll_runners(self) -> None:
        active_guild_ids: set[int] = set()
        for guild in list(getattr(self.bot, "guilds", [])):
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                continue
            should_run = False
            if not await self._cog_disabled_in_guild(guild):
                settings = await self.config.guild(guild).all()
                should_run = bool(settings.get("enabled")) and bool(
                    self._normalize_channels(settings.get("channels", {}))
                )
            active_guild_ids.add(guild_id)
            task = self._poll_runner_tasks.get(guild_id)
            if should_run:
                if task is None or task.done():
                    task = self._create_task(self._run_guild_poll_loop(guild))
                    self._poll_runner_tasks[guild_id] = task
                    task.add_done_callback(
                        lambda completed, done_guild_id=guild_id: (
                            self._poll_runner_tasks.pop(done_guild_id, None)
                            if self._poll_runner_tasks.get(done_guild_id) is completed
                            else None
                        )
                    )
            elif task is not None:
                task.cancel()
                self._poll_runner_tasks.pop(guild_id, None)

        for guild_id, task in list(self._poll_runner_tasks.items()):
            if guild_id not in active_guild_ids:
                task.cancel()
                self._poll_runner_tasks.pop(guild_id, None)

    async def _run_guild_poll_loop(self, guild: discord.Guild) -> None:
        next_poll_at = time.monotonic()
        while True:
            poll_started = time.monotonic()
            interval = MIN_POLL_INTERVAL_SECONDS
            try:
                configured_interval = await self._poll_guild_safely(guild, poll_started)
                if configured_interval is not None:
                    interval = max(MIN_POLL_INTERVAL_SECONDS, configured_interval)
            except asyncio.CancelledError:
                raise
            next_poll_at = max(next_poll_at + interval, poll_started + interval)
            now = time.monotonic()
            if next_poll_at <= now:
                next_poll_at = now
                continue
            await asyncio.sleep(next_poll_at - now)

    async def _poll_enabled_guilds(self) -> None:
        now_monotonic = time.monotonic()
        await asyncio.gather(
            *(
                self._poll_guild_safely(guild, now_monotonic)
                for guild in list(getattr(self.bot, "guilds", []))
            )
        )

    async def _poll_guild_safely(
        self,
        guild: discord.Guild,
        now_monotonic: float,
    ) -> Optional[float]:
        try:
            return await self._poll_guild(guild, now_monotonic)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Impling Finder failed while polling guild %s", getattr(guild, "id", "?"))
            return MIN_POLL_INTERVAL_SECONDS

    async def _poll_guild(
        self,
        guild: discord.Guild,
        now_monotonic: float,
    ) -> Optional[float]:
        if await self._cog_disabled_in_guild(guild):
            return None

        settings = await self.config.guild(guild).all()
        if not settings.get("enabled"):
            return None

        channels = self._normalize_channels(settings.get("channels", {}))
        feed_channels = self._feed_channels_for_settings(settings, channels)
        if not feed_channels:
            return None

        await self._clean_feed_channels_on_startup(guild, feed_channels)

        interval = max(
            MIN_POLL_INTERVAL_SECONDS,
            int(settings.get("poll_interval", DEFAULT_POLL_INTERVAL_SECONDS)),
        )
        if now_monotonic < self._backoff_until.get(guild.id, 0):
            return max(MIN_POLL_INTERVAL_SECONDS, self._backoff_until[guild.id] - now_monotonic)

        poll_started = time.monotonic()
        fetch_started = time.monotonic()

        try:
            endpoint = sanitize_endpoint_url(settings.get("endpoint") or DEFAULT_ENDPOINT)
            spawns = await self._fetch_spawns(endpoint)
        except BackendError as exc:
            fetch_ms = (time.monotonic() - fetch_started) * 1000
            self._record_metric(
                MetricEvent(
                    kind="fetch",
                    outcome="error",
                    guild_id=str(guild.id),
                    guild_name=str(getattr(guild, "name", guild.id)),
                    duration_ms=fetch_ms,
                    fetch_ms=fetch_ms,
                    error_category=f"http_{exc.status}" if exc.status else "backend_error",
                )
            )
            self._record_backend_failure(guild.id, exc)
            return max(
                MIN_POLL_INTERVAL_SECONDS,
                self._backoff_until.get(guild.id, now_monotonic + interval) - now_monotonic,
            )
        except ValueError as exc:
            fetch_ms = (time.monotonic() - fetch_started) * 1000
            self._record_metric(
                MetricEvent(
                    kind="fetch",
                    outcome="error",
                    guild_id=str(guild.id),
                    guild_name=str(getattr(guild, "name", guild.id)),
                    duration_ms=fetch_ms,
                    fetch_ms=fetch_ms,
                    error_category="invalid_endpoint",
                )
            )
            log.warning("Invalid Impling Finder endpoint for guild %s: %s", guild.id, exc)
            return interval

        fetch_ms = (time.monotonic() - fetch_started) * 1000
        fetch_completed_at = datetime.now(timezone.utc)
        self._record_metric(
            MetricEvent(
                kind="fetch",
                guild_id=str(guild.id),
                guild_name=str(getattr(guild, "name", guild.id)),
                duration_ms=fetch_ms,
                fetch_ms=fetch_ms,
                items_count=len(spawns),
            )
        )
        self._record_backend_success(guild.id)
        process_started = time.monotonic()
        await self._process_polled_spawns(
            guild,
            settings,
            channels,
            spawns,
            fetch_ms=fetch_ms,
            fetch_completed_at=fetch_completed_at,
            process_started=process_started,
        )
        process_ms = (time.monotonic() - process_started) * 1000
        self._record_metric(
            MetricEvent(
                kind="poll",
                guild_id=str(guild.id),
                guild_name=str(getattr(guild, "name", guild.id)),
                duration_ms=(time.monotonic() - poll_started) * 1000,
                fetch_ms=fetch_ms,
                process_ms=process_ms,
                items_count=len(spawns),
            )
        )
        return interval

    async def _process_polled_spawns(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
        channels: dict[str, list[int]],
        spawns: list[ImplingSpawn],
        *,
        fetch_ms: Optional[float] = None,
        fetch_completed_at: Optional[datetime] = None,
        process_started: Optional[float] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        max_age = max(0, int(settings.get("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)))
        fresh_spawns = filter_stale_spawns(spawns, now, max_age)
        current_spawns_by_key: dict[str, ImplingSpawn] = {}
        for spawn in sorted(fresh_spawns, key=lambda item: item.discovered, reverse=True):
            current_spawns_by_key.setdefault(spawn.sighting_key, spawn)
        current_sighting_keys = set(current_spawns_by_key)
        current_sighting_aliases: dict[str, str] = {}
        for spawn in fresh_spawns:
            current_sighting_aliases.setdefault(spawn.dedupe_key, spawn.sighting_key)
            current_sighting_aliases.setdefault(spawn.legacy_area_key, spawn.sighting_key)
        await self._clear_missing_seen_sightings(
            guild,
            current_sighting_keys,
            current_sighting_aliases,
        )

        unique_spawns = collapse_duplicate_sightings(fresh_spawns)
        duplicate_count = max(0, len(fresh_spawns) - len(unique_spawns))
        routed_spawns = [
            spawn
            for spawn in reversed(unique_spawns)
            if self._routed_channel_ids(settings, channels, spawn)
        ]
        guild_id = str(guild.id)
        guild_name = str(getattr(guild, "name", guild.id))
        if duplicate_count:
            self._record_metric(
                MetricEvent(
                    kind="duplicate",
                    guild_id=guild_id,
                    guild_name=guild_name,
                    count_value=duplicate_count,
                )
            )
        if routed_spawns:
            self._record_metric(
                MetricEvent(
                    kind="routed",
                    guild_id=guild_id,
                    guild_name=guild_name,
                    count_value=len(routed_spawns),
                )
            )

        if routed_spawns:
            guild_key = guild_id
            async with self.config.seen() as seen:
                current_seen = list(seen.get(guild_key, []))
                to_announce, updated_seen = select_unseen_spawns(
                    routed_spawns,
                    current_seen,
                    announce_existing=bool(settings.get("announce_existing", False)),
                )
                seen[guild_key] = updated_seen

            send_tasks = []
            for spawn in to_announce:
                for channel_id in self._routed_channel_ids(settings, channels, spawn):
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        log.warning(
                            "Impling Finder channel %s no longer exists in guild %s",
                            channel_id,
                            guild.id,
                        )
                        continue
                    send_tasks.append(
                        self._send_spawn_job(
                            guild,
                            channel,
                            spawn,
                            channel_id,
                            screenshots=bool(settings.get("screenshots", False)),
                            fetch_ms=fetch_ms,
                            fetch_completed_at=fetch_completed_at,
                            process_started=process_started,
                        )
                    )
            if send_tasks:
                send_results = await asyncio.gather(*send_tasks)
                for spawn, channel_id, message in send_results:
                    await self._record_active_message(guild, spawn, channel_id, message)

        self._schedule_post_poll_maintenance(
            guild,
            self._feed_channels_for_settings(settings, channels),
            current_sighting_keys,
            current_sighting_aliases,
            current_spawns_by_key,
        )

    async def _send_spawn_job(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        channel_id: int,
        *,
        screenshots: bool,
        fetch_ms: Optional[float],
        fetch_completed_at: Optional[datetime],
        process_started: Optional[float],
    ) -> tuple[ImplingSpawn, int, Optional[discord.Message]]:
        send_kwargs: dict[str, Any] = {"screenshots": screenshots}
        if fetch_ms is not None:
            send_kwargs["fetch_ms"] = fetch_ms
        if fetch_completed_at is not None:
            send_kwargs["fetch_completed_at"] = fetch_completed_at
        if process_started is not None:
            send_kwargs["process_ms"] = (time.monotonic() - process_started) * 1000
        message = await self._send_spawn_to_channel(
            guild,
            channel,
            spawn,
            **send_kwargs,
        )
        return spawn, channel_id, message

    def _schedule_post_poll_maintenance(
        self,
        guild: discord.Guild,
        channels: dict[str, list[int]],
        current_sighting_keys: set[str],
        current_sighting_aliases: Mapping[str, str],
        current_spawns_by_key: Mapping[str, ImplingSpawn],
    ) -> None:
        try:
            self._maintenance_queue.put_nowait(
                MaintenanceJob(
                    guild=guild,
                    channels=dict(channels),
                    current_sighting_keys=set(current_sighting_keys),
                    current_sighting_aliases=dict(current_sighting_aliases),
                    current_spawns_by_key=dict(current_spawns_by_key),
                )
            )
        except asyncio.QueueFull:
            log.warning(
                "Impling Finder maintenance queue is full; skipped maintenance for guild %s",
                getattr(guild, "id", "?"),
            )

    def _enqueue_screenshot_edit(
        self,
        guild: discord.Guild,
        channel,
        message: discord.Message,
        spawn: ImplingSpawn,
        *,
        fetch_ms: Optional[float],
        fetch_completed_at: Optional[datetime],
        process_ms: Optional[float],
    ) -> None:
        try:
            self._screenshot_queue.put_nowait(
                ScreenshotEditJob(
                    guild=guild,
                    channel=channel,
                    message=message,
                    spawn=spawn,
                    fetch_ms=fetch_ms,
                    fetch_completed_at=fetch_completed_at,
                    process_ms=process_ms,
                )
            )
        except asyncio.QueueFull:
            log.warning(
                "Impling Finder screenshot queue is full; skipped map edit for message %s",
                getattr(message, "id", "?"),
            )
            self._record_attachment_metric(
                guild,
                channel,
                spawn,
                outcome="error",
                total_started=time.monotonic(),
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=None,
                send_ms=None,
                error_category="queue_full",
            )

    async def _run_post_poll_maintenance(
        self,
        guild: discord.Guild,
        channels: dict[str, list[int]],
        current_sighting_keys: set[str],
        current_sighting_aliases: Mapping[str, str],
        current_spawns_by_key: Mapping[str, ImplingSpawn],
    ) -> None:
        try:
            await self._delete_missing_active_messages(
                guild,
                current_sighting_keys,
                current_sighting_aliases,
                current_spawns_by_key,
            )
            await self._clean_feed_channels_if_due(guild, channels)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Impling Finder failed during post-poll maintenance for guild %s",
                getattr(guild, "id", "?"),
            )

    async def _clean_feed_channels_if_due(
        self,
        guild: discord.Guild,
        channels: Mapping[str, list[int]],
    ) -> None:
        now = time.monotonic()
        if now - self._last_feed_cleanup.get(guild.id, 0) < FEED_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_feed_cleanup[guild.id] = now
        await self._clean_feed_channels(guild, channels)

    async def _delete_missing_active_messages(
        self,
        guild: discord.Guild,
        current_sighting_keys: set[str],
        current_sighting_aliases: Mapping[str, str],
        current_spawns_by_key: Mapping[str, ImplingSpawn],
    ) -> None:
        guild_key = str(guild.id)
        async with self.config.active_messages() as active_messages:
            guild_messages = active_messages.get(guild_key)
            if not isinstance(guild_messages, dict):
                if guild_key in active_messages:
                    active_messages.pop(guild_key, None)
                return

            for spawn_key, channel_messages in list(guild_messages.items()):
                migrated_key = self._current_sighting_key(
                    spawn_key,
                    current_sighting_keys,
                    current_sighting_aliases,
                )
                if migrated_key is not None:
                    if isinstance(channel_messages, dict):
                        target_messages = channel_messages
                        if migrated_key != spawn_key:
                            migrated_messages = guild_messages.get(migrated_key)
                            if not isinstance(migrated_messages, dict):
                                migrated_messages = {}
                                guild_messages[migrated_key] = migrated_messages
                            migrated_messages.update(channel_messages)
                            guild_messages.pop(spawn_key, None)
                            target_messages = migrated_messages
                        self._hydrate_active_message_records(
                            target_messages,
                            current_spawns_by_key.get(migrated_key),
                        )
                    else:
                        guild_messages.pop(spawn_key, None)
                    continue

                if not isinstance(channel_messages, dict):
                    guild_messages.pop(spawn_key, None)
                    continue

                for channel_id, message_record in list(channel_messages.items()):
                    if await self._mark_tracked_message_despawned(
                        guild,
                        channel_id,
                        message_record,
                        spawn_key,
                    ):
                        channel_messages.pop(channel_id, None)

                if not channel_messages:
                    guild_messages.pop(spawn_key, None)

            if not guild_messages:
                active_messages.pop(guild_key, None)

    async def _clear_missing_seen_sightings(
        self,
        guild: discord.Guild,
        current_sighting_keys: set[str],
        current_sighting_aliases: Mapping[str, str],
    ) -> None:
        guild_key = str(guild.id)
        async with self.config.seen() as seen:
            current_seen = list(seen.get(guild_key, []))
            normalized_seen: list[str] = []
            normalized_set: set[str] = set()
            for key in current_seen:
                current_key = self._current_sighting_key(
                    str(key),
                    current_sighting_keys,
                    current_sighting_aliases,
                )
                if current_key is None or current_key in normalized_set:
                    continue
                normalized_seen.append(current_key)
                normalized_set.add(current_key)
            seen[guild_key] = normalized_seen

    def _current_sighting_key(
        self,
        key: str,
        current_sighting_keys: set[str],
        current_sighting_aliases: Mapping[str, str],
    ) -> str | None:
        if key in current_sighting_keys:
            return key
        if key in current_sighting_aliases:
            return current_sighting_aliases[key]
        legacy_key = sighting_key_from_legacy_dedupe_key(key)
        if legacy_key in current_sighting_keys:
            return legacy_key
        return None

    def _hydrate_active_message_records(
        self,
        channel_messages: dict[Any, Any],
        spawn: Optional[ImplingSpawn],
    ) -> None:
        if spawn is None:
            return
        for channel_id, message_record in list(channel_messages.items()):
            message_id = self._tracked_message_id(message_record)
            if message_id is None:
                channel_messages.pop(channel_id, None)
                continue
            channel_messages[channel_id] = self._active_message_record(message_id, spawn)

    def _tracked_message_id(self, message_record: Any) -> Optional[int]:
        raw_message_id = (
            message_record.get("message_id")
            if isinstance(message_record, Mapping)
            else message_record
        )
        try:
            return int(raw_message_id)
        except (TypeError, ValueError):
            return None

    def _active_message_record(self, message_id: int, spawn: ImplingSpawn) -> dict[str, int]:
        return {
            "message_id": int(message_id),
            "npcid": int(spawn.npcid),
            "world": int(spawn.world),
            "xcoord": int(spawn.xcoord),
            "ycoord": int(spawn.ycoord),
            "plane": int(spawn.plane),
            "discovered_epoch": int(spawn.discovered_epoch),
        }

    def _spawn_from_active_message_record(
        self,
        spawn_key: str,
        message_record: Any,
    ) -> Optional[ImplingSpawn]:
        if isinstance(message_record, Mapping):
            try:
                return ImplingSpawn(
                    npcid=int(message_record["npcid"]),
                    world=int(message_record["world"]),
                    xcoord=int(message_record["xcoord"]),
                    ycoord=int(message_record["ycoord"]),
                    plane=int(message_record["plane"]),
                    discovered=datetime.fromtimestamp(
                        int(message_record["discovered_epoch"]),
                        timezone.utc,
                    ),
                )
            except (KeyError, TypeError, ValueError, OSError):
                pass

        parts = str(spawn_key).split(":")
        if len(parts) != 6:
            return None
        try:
            npcid, world, xcoord, ycoord, plane, discovered_epoch = map(int, parts)
        except ValueError:
            return None
        return ImplingSpawn(
            npcid=npcid,
            world=world,
            xcoord=xcoord,
            ycoord=ycoord,
            plane=plane,
            discovered=datetime.fromtimestamp(discovered_epoch, timezone.utc),
        )

    def _impling_name_from_spawn_key(self, spawn_key: str) -> str:
        parts = str(spawn_key).split(":")
        try:
            npcid = int(parts[0])
        except (IndexError, TypeError, ValueError):
            return "Impling"
        for info in IMPLINGS.values():
            if info.npcid == npcid:
                return _display_impling_name(info.name)
        return f"NPC {npcid}"

    async def _mark_tracked_message_despawned(
        self,
        guild: discord.Guild,
        channel_id: Any,
        message_record: Any,
        spawn_key: str,
    ) -> bool:
        despawn_started = time.monotonic()
        try:
            clean_channel_id = int(channel_id)
            clean_message_id = self._tracked_message_id(message_record)
        except (TypeError, ValueError):
            return True
        if clean_message_id is None:
            return True

        channel = guild.get_channel(clean_channel_id)
        if channel is None:
            log.warning(
                "Impling Finder cannot mark despawned message %s; channel %s is missing",
                clean_message_id,
                clean_channel_id,
            )
            self._record_despawn_metric(
                guild,
                None,
                None,
                outcome="skipped",
                started=despawn_started,
                error_category="channel_missing",
            )
            return True

        spawn = self._spawn_from_active_message_record(spawn_key, message_record)
        try:
            if hasattr(channel, "get_partial_message"):
                message = channel.get_partial_message(clean_message_id)
            else:
                message = await channel.fetch_message(clean_message_id)
            kwargs: dict[str, Any] = {
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            permissions = self._bot_permissions(guild, channel)
            can_embed = permissions is None or getattr(permissions, "embed_links", False)
            if spawn is not None and can_embed:
                kwargs.update(
                    content=None,
                    embed=self._embed_for_spawn(
                        spawn,
                        status="despawned",
                        image_url=self._existing_embed_image_url(message),
                    ),
                )
            else:
                kwargs.update(
                    content=self._content_for_despawn(spawn, spawn_key),
                    embed=None,
                )
            await message.edit(**kwargs)
            self._remember_despawn_notice(clean_channel_id, clean_message_id)
            self._create_despawn_delete_task(
                self._delete_despawn_notice_after(guild, clean_channel_id, clean_message_id)
            )
            self._record_despawn_metric(
                guild,
                channel,
                spawn,
                outcome="ok",
                started=despawn_started,
            )
            return True
        except discord.NotFound:
            self._record_despawn_metric(
                guild,
                channel,
                spawn,
                outcome="skipped",
                started=despawn_started,
                error_category="message_not_found",
            )
            return True
        except discord.Forbidden:
            log.warning(
                "Impling Finder lacks permission to mark despawned message %s in channel %s",
                clean_message_id,
                clean_channel_id,
            )
            self._record_despawn_metric(
                guild,
                channel,
                spawn,
                outcome="permission_denied",
                started=despawn_started,
                error_category="discord_forbidden",
            )
            return False
        except discord.HTTPException:
            log.exception(
                "Impling Finder failed to mark despawned message %s in channel %s",
                clean_message_id,
                clean_channel_id,
            )
            self._record_despawn_metric(
                guild,
                channel,
                spawn,
                outcome="error",
                started=despawn_started,
                error_category="discord_http",
            )
            return False

    async def _delete_despawn_notice_after(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
    ) -> None:
        try:
            await asyncio.sleep(DESPAWN_DELETE_DELAY_SECONDS)
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                return
            if hasattr(channel, "get_partial_message"):
                message = channel.get_partial_message(int(message_id))
            else:
                message = await channel.fetch_message(int(message_id))
            await message.delete()
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            return
        except discord.Forbidden:
            log.warning(
                "Impling Finder lacks permission to delete despawn notice %s in channel %s",
                message_id,
                channel_id,
            )
        except discord.HTTPException:
            log.exception(
                "Impling Finder failed to delete despawn notice %s in channel %s",
                message_id,
                channel_id,
            )
        finally:
            self._forget_despawn_notice(channel_id, message_id)

    def _record_despawn_metric(
        self,
        guild: discord.Guild,
        channel,
        spawn: Optional[ImplingSpawn],
        *,
        outcome: str,
        started: float,
        error_category: Optional[str] = None,
    ) -> None:
        self._record_metric(
            MetricEvent(
                kind="despawn",
                outcome=outcome,
                guild_id=str(guild.id),
                guild_name=str(getattr(guild, "name", guild.id)),
                channel_id=str(getattr(channel, "id", "")) if channel is not None else None,
                channel_name=(
                    str(getattr(channel, "name", "")) or None
                    if channel is not None
                    else None
                ),
                impling_type=spawn.type_key if spawn is not None else None,
                world=spawn.world if spawn is not None else None,
                location=self._location_for_spawn(spawn) if spawn is not None else None,
                duration_ms=(time.monotonic() - started) * 1000,
                error_category=error_category,
            )
        )

    def _remember_despawn_notice(self, channel_id: int, message_id: int) -> None:
        channel_key = str(int(channel_id))
        self._despawn_notice_until_by_channel.setdefault(channel_key, {})[
            int(message_id)
        ] = time.monotonic() + DESPAWN_NOTICE_RETENTION_SECONDS

    def _forget_despawn_notice(self, channel_id: Any, message_id: Any) -> None:
        try:
            channel_key = str(int(channel_id))
            clean_message_id = int(message_id)
        except (TypeError, ValueError):
            return
        message_expirations = self._despawn_notice_until_by_channel.get(channel_key)
        if message_expirations is None:
            return
        message_expirations.pop(clean_message_id, None)
        if not message_expirations:
            self._despawn_notice_until_by_channel.pop(channel_key, None)

    def _despawn_notice_ids_by_channel(self) -> dict[str, set[int]]:
        now = time.monotonic()
        active_notice_ids: dict[str, set[int]] = {}
        for channel_id, message_expirations in list(self._despawn_notice_until_by_channel.items()):
            for message_id, expires_at in list(message_expirations.items()):
                if expires_at <= now:
                    message_expirations.pop(message_id, None)
                    continue
                active_notice_ids.setdefault(channel_id, set()).add(message_id)
            if not message_expirations:
                self._despawn_notice_until_by_channel.pop(channel_id, None)
        return active_notice_ids

    def _is_despawn_notice(self, channel_id: Any, message_id: Any) -> bool:
        try:
            channel_key = str(int(channel_id))
            clean_message_id = int(message_id)
        except (TypeError, ValueError):
            return False
        return clean_message_id in self._despawn_notice_ids_by_channel().get(channel_key, set())

    def _existing_embed_image_url(self, message: Any) -> str:
        for embed in getattr(message, "embeds", []) or []:
            image = getattr(embed, "image", None)
            if isinstance(image, str) and image:
                return image
            image_url = getattr(image, "url", None)
            if image_url:
                return str(image_url)
        return DESPAWN_DEFAULT_IMAGE_URL

    async def _record_active_message(
        self,
        guild: discord.Guild,
        spawn: ImplingSpawn,
        channel_id: Any,
        message,
    ) -> None:
        message_id = getattr(message, "id", None)
        if message_id is None:
            return

        try:
            clean_channel_id = str(int(channel_id))
            clean_message_id = int(message_id)
        except (TypeError, ValueError):
            return

        guild_key = str(guild.id)
        async with self.config.active_messages() as active_messages:
            guild_messages = active_messages.get(guild_key)
            if not isinstance(guild_messages, dict):
                guild_messages = {}
                active_messages[guild_key] = guild_messages

            spawn_messages = guild_messages.get(spawn.sighting_key)
            if not isinstance(spawn_messages, dict):
                spawn_messages = {}
                guild_messages[spawn.sighting_key] = spawn_messages

            spawn_messages[clean_channel_id] = self._active_message_record(
                clean_message_id,
                spawn,
            )

    async def _clean_feed_channels(
        self,
        guild: discord.Guild,
        channels: Mapping[str, list[int]],
    ) -> None:
        channel_ids: set[int] = set()
        for channel_id in channels:
            try:
                channel_ids.add(int(channel_id))
            except (TypeError, ValueError):
                continue
        if not channel_ids:
            return

        active_message_ids = await self._active_message_ids_by_channel(guild.id)
        for channel_id, notice_ids in self._despawn_notice_ids_by_channel().items():
            active_message_ids.setdefault(channel_id, set()).update(notice_ids)
        for channel_id in sorted(channel_ids):
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            await self._clean_feed_channel(
                guild,
                channel,
                active_message_ids.get(str(channel_id), set()),
            )

    async def _active_message_ids_by_channel(self, guild_id: int) -> dict[str, set[int]]:
        active_messages = await self.config.active_messages()
        guild_messages = active_messages.get(str(guild_id))
        active_by_channel: dict[str, set[int]] = {}
        if not isinstance(guild_messages, dict):
            return active_by_channel

        for channel_messages in guild_messages.values():
            if not isinstance(channel_messages, dict):
                continue
            for channel_id, message_record in channel_messages.items():
                message_id = self._tracked_message_id(message_record)
                if message_id is None:
                    continue
                try:
                    clean_channel_id = str(int(channel_id))
                except (TypeError, ValueError):
                    continue
                active_by_channel.setdefault(clean_channel_id, set()).add(message_id)
        return active_by_channel

    async def _clean_feed_channel(
        self,
        guild: discord.Guild,
        channel,
        active_message_ids: set[int],
    ) -> None:
        permissions = self._bot_permissions(guild, channel)
        if permissions is not None:
            if not getattr(permissions, "manage_messages", False):
                log.warning(
                    "Impling Finder cannot clean channel %s in guild %s without Manage Messages",
                    getattr(channel, "id", "?"),
                    guild.id,
                )
                self._record_feed_cleanup_metric(
                    guild,
                    channel,
                    outcome="permission_denied",
                    error_category="manage_messages",
                )
                return
            if not getattr(permissions, "read_message_history", False):
                log.warning(
                    "Impling Finder cannot clean channel %s in guild %s without Read Message History",
                    getattr(channel, "id", "?"),
                    guild.id,
                )
                self._record_feed_cleanup_metric(
                    guild,
                    channel,
                    outcome="permission_denied",
                    error_category="read_message_history",
                )
                return

        if not hasattr(channel, "history"):
            return

        cleanup_started = time.monotonic()
        deleted_count = 0
        try:
            async for message in channel.history(limit=FEED_CLEANUP_HISTORY_LIMIT):
                try:
                    message_id = int(getattr(message, "id"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if message_id in active_message_ids or getattr(message, "pinned", False):
                    continue

                try:
                    await message.delete()
                    deleted_count += 1
                except discord.NotFound:
                    continue
                except discord.Forbidden:
                    log.warning(
                        "Impling Finder was denied while cleaning message %s in channel %s",
                        message_id,
                        getattr(channel, "id", "?"),
                    )
                    self._record_feed_cleanup_metric(
                        guild,
                        channel,
                        outcome="permission_denied",
                        started=cleanup_started,
                        count_value=max(1, deleted_count),
                        error_category="discord_forbidden",
                    )
                    return
                except discord.HTTPException:
                    log.exception(
                        "Impling Finder failed to clean message %s in channel %s",
                        message_id,
                        getattr(channel, "id", "?"),
                    )
                    self._record_feed_cleanup_metric(
                        guild,
                        channel,
                        outcome="error",
                        started=cleanup_started,
                        count_value=max(1, deleted_count),
                        error_category="discord_http",
                    )
                    continue
        except discord.Forbidden:
            log.warning(
                "Impling Finder was denied while reading history in channel %s",
                getattr(channel, "id", "?"),
            )
            self._record_feed_cleanup_metric(
                guild,
                channel,
                outcome="permission_denied",
                started=cleanup_started,
                count_value=max(1, deleted_count),
                error_category="history_forbidden",
            )
        except discord.HTTPException:
            log.exception(
                "Impling Finder failed to read history while cleaning channel %s",
                getattr(channel, "id", "?"),
            )
            self._record_feed_cleanup_metric(
                guild,
                channel,
                outcome="error",
                started=cleanup_started,
                count_value=max(1, deleted_count),
                error_category="history_http",
            )
        finally:
            if deleted_count:
                self._record_feed_cleanup_metric(
                    guild,
                    channel,
                    outcome="ok",
                    started=cleanup_started,
                    count_value=deleted_count,
                )

    def _record_feed_cleanup_metric(
        self,
        guild: discord.Guild,
        channel,
        *,
        outcome: str,
        started: Optional[float] = None,
        count_value: int = 1,
        error_category: Optional[str] = None,
    ) -> None:
        self._record_metric(
            MetricEvent(
                kind="cleanup",
                outcome=outcome,
                guild_id=str(guild.id),
                guild_name=str(getattr(guild, "name", guild.id)),
                channel_id=str(getattr(channel, "id", "")) or None,
                channel_name=str(getattr(channel, "name", "")) or None,
                duration_ms=(time.monotonic() - started) * 1000 if started is not None else None,
                count_value=max(1, int(count_value)),
                error_category=error_category,
            )
        )

    async def _cog_disabled_in_guild(self, guild: discord.Guild) -> bool:
        checker = getattr(self.bot, "cog_disabled_in_guild", None)
        if checker is None:
            return False
        result = checker(self, guild)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    def _normalize_channels(self, channels: Mapping[str, Any]) -> dict[str, list[int]]:
        normalized: dict[str, list[int]] = {}
        for channel_id, npc_ids in dict(channels or {}).items():
            try:
                clean_channel_id = str(int(channel_id))
            except (TypeError, ValueError):
                continue
            clean_npc_ids = []
            for npcid in list(npc_ids or []):
                try:
                    clean_npc_ids.append(int(npcid))
                except (TypeError, ValueError):
                    continue
            if clean_npc_ids:
                normalized[clean_channel_id] = clean_npc_ids
        return normalized

    def _normalize_puro_channel(self, settings: Mapping[str, Any]) -> Optional[str]:
        try:
            return str(int(settings.get("puro_channel")))
        except (TypeError, ValueError):
            return None

    def _access_emoji_key(self, emoji: Any) -> str:
        emoji_id = getattr(emoji, "id", None)
        if emoji_id is not None:
            prefix = "a" if bool(getattr(emoji, "animated", False)) else ""
            name = str(getattr(emoji, "name", "")).strip()
            if not name:
                return ""
            return f"<{prefix}:{name}:{int(emoji_id)}>"

        value = str(getattr(emoji, "name", emoji)).strip()
        match = CUSTOM_EMOJI_RE.match(value)
        if match is None:
            return value
        prefix = "a" if match.group(1) else ""
        return f"<{prefix}:{match.group(2)}:{match.group(3)}>"

    def _normalize_access_reactions(self, mappings: Mapping[str, Any]) -> dict[str, dict[str, str]]:
        normalized: dict[str, dict[str, str]] = {}
        for message_id, emoji_roles in dict(mappings or {}).items():
            try:
                message_key = str(int(message_id))
            except (TypeError, ValueError):
                continue
            clean_emoji_roles: dict[str, str] = {}
            for emoji, role_id in dict(emoji_roles or {}).items():
                emoji_key = self._access_emoji_key(emoji)
                if not emoji_key:
                    continue
                try:
                    clean_emoji_roles[emoji_key] = str(int(role_id))
                except (TypeError, ValueError):
                    continue
            if clean_emoji_roles:
                normalized[message_key] = clean_emoji_roles
        return normalized

    def _role_mention(self, role_id: str) -> str:
        return f"<@&{int(role_id)}>"

    def _feed_channels_for_settings(
        self,
        settings: Mapping[str, Any],
        channels: Mapping[str, list[int]],
    ) -> dict[str, list[int]]:
        feed_channels = dict(channels)
        if bool(settings.get("puro_enabled", False)):
            puro_channel = self._normalize_puro_channel(settings)
            if puro_channel is not None:
                feed_channels.setdefault(puro_channel, [])
        return feed_channels

    def _routed_channel_ids(
        self,
        settings: Mapping[str, Any],
        channels: Mapping[str, list[int]],
        spawn: ImplingSpawn,
    ) -> list[int]:
        return routed_channel_ids(
            channels,
            spawn,
            puro_enabled=bool(settings.get("puro_enabled", False)),
            puro_channel_id=self._normalize_puro_channel(settings),
        )

    async def _fetch_spawns(self, endpoint: str) -> list[ImplingSpawn]:
        session = await self._get_session()
        try:
            async with session.get(endpoint, headers={"Accept": "application/json"}) as response:
                if response.status == 429:
                    raise BackendError("Backend rate limited the request.", status=response.status)
                if response.status < 200 or response.status >= 300:
                    raise BackendError(
                        f"Backend returned HTTP {response.status}.",
                        status=response.status,
                    )
                payload = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise BackendError(f"Backend request failed: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise BackendError("Backend request timed out.") from exc

        if not isinstance(payload, Mapping):
            raise BackendError("Backend returned JSON that was not an object.")

        try:
            return parse_backend_payload(payload)
        except ValueError as exc:
            raise BackendError(f"Backend payload was invalid: {exc}") from exc

    def _record_backend_failure(self, guild_id: int, exc: BackendError) -> None:
        failures = min(self._failure_counts.get(guild_id, 0) + 1, 6)
        self._failure_counts[guild_id] = failures
        delay = min(MAX_BACKOFF_SECONDS, MIN_POLL_INTERVAL_SECONDS * (2 ** (failures - 1)))
        if exc.status == 429:
            delay = max(delay, 60)
        self._backoff_until[guild_id] = time.monotonic() + delay
        log.warning(
            "Impling Finder backend failure for guild %s: %s; backing off for %ss",
            guild_id,
            exc,
            delay,
        )

    def _record_backend_success(self, guild_id: int) -> None:
        self._failure_counts.pop(guild_id, None)
        self._backoff_until.pop(guild_id, None)

    async def _clean_feed_channels_on_startup(
        self,
        guild: discord.Guild,
        channels: Mapping[str, list[int]],
    ) -> None:
        if guild.id in self._startup_cleaned_guilds:
            return
        self._startup_cleaned_guilds.add(guild.id)
        try:
            await self._clean_feed_channels(guild, channels)
        except Exception:
            log.exception(
                "Impling Finder failed during startup feed cleanup for guild %s",
                guild.id,
            )

    def _load_location_areas(self) -> list[MapLabel]:
        try:
            raw_data = json.loads(AREAS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Impling Finder could not load bundled mapped area labels")
            return []

        raw_labels = raw_data.get("locations", []) if isinstance(raw_data, Mapping) else raw_data
        areas: list[MapLabel] = []
        for item in raw_labels:
            try:
                coords = item["coords"]
                xcoord, ycoord, plane = coords[:3]
                areas.append(
                    MapLabel(
                        name=str(item["name"]),
                        xcoord=int(xcoord),
                        ycoord=int(ycoord),
                        plane=int(plane),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return areas

    def _location_for_spawn(self, spawn: ImplingSpawn) -> str:
        return resolve_location_name(spawn, self._location_areas)

    def _display_name_for_spawn(self, spawn: ImplingSpawn) -> str:
        return _display_impling_name(spawn.impling_name)

    def _coordinate_link_for_spawn(self, spawn: ImplingSpawn) -> str:
        return f"[{spawn.xcoord}, {spawn.ycoord}]({build_map_url(spawn, zoom=7)})"

    def _embed_for_spawn(
        self,
        spawn: ImplingSpawn,
        *,
        status: str = "spawned",
        image_url: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> discord.Embed:
        type_key = spawn.type_key or "dragon"
        info = IMPLINGS.get(type_key)
        color = info.color if info is not None else 0x5865F2
        if status == "despawned":
            color = 0x747F8D
        location = self._location_for_spawn(spawn)

        embed = discord.Embed(
            title=f"{self._display_name_for_spawn(spawn)} {status}",
            url=build_map_url(spawn, zoom=7),
            color=color,
        )
        embed.add_field(name="World", value=str(spawn.world), inline=True)
        embed.add_field(name="Location", value=location, inline=True)
        embed.add_field(
            name="Discovered",
            value=f"<t:{spawn.discovered_epoch}:R>",
            inline=True,
        )
        if image_url:
            embed.set_image(url=image_url)
        return embed

    def _content_for_spawn(self, spawn: ImplingSpawn) -> str:
        return (
            f"{self._display_name_for_spawn(spawn)} spawned on world {spawn.world} at "
            f"{self._location_for_spawn(spawn)} ({self._coordinate_link_for_spawn(spawn)})."
        )

    def _content_for_despawn(
        self,
        spawn: Optional[ImplingSpawn],
        spawn_key: str,
    ) -> str:
        if spawn is None:
            return f"{self._impling_name_from_spawn_key(spawn_key)} despawned"
        return (
            f"{self._display_name_for_spawn(spawn)} despawned on world {spawn.world} at "
            f"{self._location_for_spawn(spawn)} ({self._coordinate_link_for_spawn(spawn)})."
        )

    async def _send_spawn_to_channel(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        *,
        screenshots: bool,
        fetch_ms: Optional[float] = None,
        fetch_completed_at: Optional[datetime] = None,
        process_ms: Optional[float] = None,
    ) -> Optional[discord.Message]:
        total_started = time.monotonic()
        render_ms: Optional[float] = None
        send_ms: Optional[float] = None
        permissions = self._bot_permissions(guild, channel)
        if permissions is not None and not permissions.send_messages:
            log.warning(
                "Impling Finder cannot send messages in channel %s for guild %s",
                getattr(channel, "id", "?"),
                guild.id,
            )
            self._record_post_metric(
                guild,
                channel,
                spawn,
                outcome="permission_denied",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                error_category="send_messages",
            )
            return None

        can_embed = permissions is None or permissions.embed_links
        can_attach = permissions is None or permissions.attach_files
        should_edit_screenshot = False
        if not can_embed:
            log.warning(
                "Impling Finder lacks Embed Links in channel %s for guild %s; sending plain text",
                getattr(channel, "id", "?"),
                guild.id,
            )
            args = (self._content_for_spawn(spawn),)
            kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
        else:
            embed = self._embed_for_spawn(spawn)
            if screenshots:
                if can_attach:
                    should_edit_screenshot = True
                else:
                    log.warning(
                        "Impling Finder lacks Attach Files in channel %s for guild %s; skipping card",
                        getattr(channel, "id", "?"),
                        guild.id,
                    )
            args = ()
            kwargs = {
                "embed": embed,
                "allowed_mentions": discord.AllowedMentions.none(),
            }

        send_started = time.monotonic()
        try:
            message = await channel.send(*args, **kwargs)
            send_ms = (time.monotonic() - send_started) * 1000
            self._record_post_metric(
                guild,
                channel,
                spawn,
                outcome="ok",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
            )
            if should_edit_screenshot:
                self._enqueue_screenshot_edit(
                    guild,
                    channel,
                    message,
                    spawn,
                    fetch_ms=fetch_ms,
                    fetch_completed_at=fetch_completed_at,
                    process_ms=process_ms,
                )
            return message
        except discord.Forbidden:
            send_ms = (time.monotonic() - send_started) * 1000
            log.warning(
                "Impling Finder was denied while sending to channel %s in guild %s",
                getattr(channel, "id", "?"),
                guild.id,
            )
            self._record_post_metric(
                guild,
                channel,
                spawn,
                outcome="permission_denied",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
                error_category="discord_forbidden",
            )
            return None
        except discord.HTTPException:
            send_ms = (time.monotonic() - send_started) * 1000
            log.exception(
                "Impling Finder failed to send spawn to channel %s in guild %s",
                getattr(channel, "id", "?"),
                guild.id,
            )
            self._record_post_metric(
                guild,
                channel,
                spawn,
                outcome="error",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
                error_category="discord_http",
            )
            return None

    async def _edit_spawn_message_with_screenshot(
        self,
        guild: discord.Guild,
        channel,
        message: discord.Message,
        spawn: ImplingSpawn,
        *,
        fetch_ms: Optional[float] = None,
        fetch_completed_at: Optional[datetime] = None,
        process_ms: Optional[float] = None,
    ) -> None:
        total_started = time.monotonic()
        render_ms: Optional[float] = None
        edit_ms: Optional[float] = None
        try:
            if self._is_despawn_notice(
                getattr(channel, "id", None),
                getattr(message, "id", None),
            ):
                self._record_attachment_metric(
                    guild,
                    channel,
                    spawn,
                    outcome="skipped",
                    total_started=total_started,
                    fetch_ms=fetch_ms,
                    fetch_completed_at=fetch_completed_at,
                    process_ms=process_ms,
                    render_ms=render_ms,
                    send_ms=edit_ms,
                    error_category="message_despawned",
                )
                return

            render_started = time.monotonic()
            file = await self._make_screenshot_file(spawn)
            render_ms = (time.monotonic() - render_started) * 1000
            if file is None:
                self._record_attachment_metric(
                    guild,
                    channel,
                    spawn,
                    outcome="error",
                    total_started=total_started,
                    fetch_ms=fetch_ms,
                    fetch_completed_at=fetch_completed_at,
                    process_ms=process_ms,
                    render_ms=render_ms,
                    send_ms=edit_ms,
                    error_category="render_unavailable",
                )
                return

            if self._is_despawn_notice(
                getattr(channel, "id", None),
                getattr(message, "id", None),
            ):
                self._record_attachment_metric(
                    guild,
                    channel,
                    spawn,
                    outcome="skipped",
                    total_started=total_started,
                    fetch_ms=fetch_ms,
                    fetch_completed_at=fetch_completed_at,
                    process_ms=process_ms,
                    render_ms=render_ms,
                    send_ms=edit_ms,
                    error_category="message_despawned",
                )
                return

            embed = self._embed_for_spawn(spawn)
            embed.set_image(url=f"attachment://{file.filename}")
            edit_started = time.monotonic()
            await message.edit(
                embed=embed,
                attachments=[file],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            edit_ms = (time.monotonic() - edit_started) * 1000
            self._record_attachment_metric(
                guild,
                channel,
                spawn,
                outcome="ok",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=edit_ms,
            )
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            log.info(
                "Impling Finder skipped map edit because message %s was gone in channel %s",
                getattr(message, "id", "?"),
                getattr(channel, "id", "?"),
            )
            self._record_attachment_metric(
                guild,
                channel,
                spawn,
                outcome="skipped",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=edit_ms,
                error_category="message_not_found",
            )
        except discord.Forbidden:
            log.warning(
                "Impling Finder was denied while editing map onto message %s in channel %s",
                getattr(message, "id", "?"),
                getattr(channel, "id", "?"),
            )
            self._record_attachment_metric(
                guild,
                channel,
                spawn,
                outcome="permission_denied",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=edit_ms,
                error_category="discord_forbidden",
            )
        except discord.HTTPException:
            log.exception(
                "Impling Finder failed to edit map onto message %s in channel %s",
                getattr(message, "id", "?"),
                getattr(channel, "id", "?"),
            )
            self._record_attachment_metric(
                guild,
                channel,
                spawn,
                outcome="error",
                total_started=total_started,
                fetch_ms=fetch_ms,
                fetch_completed_at=fetch_completed_at,
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=edit_ms,
                error_category="discord_http",
            )

    def _record_post_metric(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        *,
        outcome: str,
        total_started: float,
        fetch_ms: Optional[float],
        fetch_completed_at: Optional[datetime],
        process_ms: Optional[float],
        render_ms: Optional[float] = None,
        send_ms: Optional[float] = None,
        error_category: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self._record_metric(
            MetricEvent(
                kind="post",
                outcome=outcome,
                guild_id=str(guild.id),
                guild_name=str(getattr(guild, "name", guild.id)),
                channel_id=str(getattr(channel, "id", "")) or None,
                channel_name=str(getattr(channel, "name", "")) or None,
                impling_type=spawn.type_key,
                world=spawn.world,
                location=self._location_for_spawn(spawn),
                duration_ms=(time.monotonic() - total_started) * 1000,
                fetch_ms=fetch_ms,
                age_at_fetch_ms=self._age_at_fetch_ms(spawn, fetch_completed_at),
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
                end_to_end_ms=max(
                    0.0,
                    (now - spawn.discovered.astimezone(timezone.utc)).total_seconds() * 1000,
                ),
                error_category=error_category,
            )
        )

    def _record_attachment_metric(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        *,
        outcome: str,
        total_started: float,
        fetch_ms: Optional[float],
        fetch_completed_at: Optional[datetime],
        process_ms: Optional[float],
        render_ms: Optional[float],
        send_ms: Optional[float],
        error_category: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self._record_metric(
            MetricEvent(
                kind="attachment",
                outcome=outcome,
                guild_id=str(guild.id),
                guild_name=str(getattr(guild, "name", guild.id)),
                channel_id=str(getattr(channel, "id", "")) or None,
                channel_name=str(getattr(channel, "name", "")) or None,
                impling_type=spawn.type_key,
                world=spawn.world,
                location=self._location_for_spawn(spawn),
                duration_ms=(time.monotonic() - total_started) * 1000,
                fetch_ms=fetch_ms,
                age_at_fetch_ms=self._age_at_fetch_ms(spawn, fetch_completed_at),
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
                end_to_end_ms=max(
                    0.0,
                    (now - spawn.discovered.astimezone(timezone.utc)).total_seconds() * 1000,
                ),
                error_category=error_category,
            )
        )

    def _age_at_fetch_ms(
        self,
        spawn: ImplingSpawn,
        fetch_completed_at: Optional[datetime],
    ) -> Optional[float]:
        if fetch_completed_at is None:
            return None
        if fetch_completed_at.tzinfo is None:
            fetch_completed_at = fetch_completed_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (
                fetch_completed_at.astimezone(timezone.utc)
                - spawn.discovered.astimezone(timezone.utc)
            ).total_seconds()
            * 1000,
        )

    def _bot_permissions(self, guild: discord.Guild, channel):
        member = getattr(guild, "me", None)
        if member is None and getattr(self.bot, "user", None) is not None:
            member = guild.get_member(self.bot.user.id)
        if member is None or not hasattr(channel, "permissions_for"):
            return None
        return channel.permissions_for(member)

    async def _make_screenshot_file_for_send(
        self,
        spawn: ImplingSpawn,
    ) -> tuple[Optional[discord.File], Optional[float]]:
        render_started = time.monotonic()
        try:
            file = await asyncio.wait_for(
                self._make_screenshot_file(spawn),
                timeout=MAP_RENDER_SEND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            render_ms = (time.monotonic() - render_started) * 1000
            log.warning(
                "Impling Finder map render timed out after %.2fs; sending without attachment",
                MAP_RENDER_SEND_TIMEOUT_SECONDS,
            )
            self._record_render_timeout_metric(spawn, render_ms)
            return None, render_ms
        return file, (time.monotonic() - render_started) * 1000

    def _record_render_timeout_metric(self, spawn: ImplingSpawn, render_ms: float) -> None:
        self._record_metric(
            MetricEvent(
                kind="render",
                outcome="timeout",
                impling_type=spawn.type_key,
                world=spawn.world,
                location=self._location_for_spawn(spawn),
                duration_ms=render_ms,
                render_ms=render_ms,
                error_category="timeout",
            )
        )

    async def _make_screenshot_file(self, spawn: ImplingSpawn) -> Optional[discord.File]:
        map_file = await self._make_map_file(spawn)
        if map_file is not None:
            return map_file
        return self._make_coordinate_card_file(spawn)

    async def _make_map_file(self, spawn: ImplingSpawn) -> Optional[discord.File]:
        total_started = time.monotonic()
        fetch_ms: Optional[float] = None
        pillow = self._load_pillow()
        if pillow is None:
            return None
        Image, _ImageDraw, _ImageFont = pillow

        try:
            fetch_started = time.monotonic()
            tiles = explv_tiles_for_crop(
                spawn,
                width=MAP_CROP_SIZE,
                height=MAP_CROP_SIZE,
                zoom=MAP_TILE_ZOOM,
            )
            tile_payloads = await asyncio.gather(
                *(self._fetch_map_tile(tile.url) for tile in tiles)
            )
            fetch_ms = (time.monotonic() - fetch_started) * 1000
            image = Image.new("RGBA", (MAP_CROP_SIZE, MAP_CROP_SIZE), (0, 0, 0, 0))
            for tile, tile_payload in zip(tiles, tile_payloads):
                tile_image = Image.open(io.BytesIO(tile_payload)).convert("RGBA")
                image.alpha_composite(tile_image, (tile.paste_x, tile.paste_y))

            if MAP_CROP_SIZE != MAP_IMAGE_SIZE:
                image = image.resize(
                    (MAP_IMAGE_SIZE, MAP_IMAGE_SIZE),
                    Image.Resampling.NEAREST,
                )
            icon = Image.open(IMPLING_ASSET_DIR / f"{spawn.type_key}.png").convert("RGBA")
            icon.thumbnail(
                (IMPLING_ICON_SIZE, IMPLING_ICON_SIZE),
                Image.Resampling.LANCZOS,
            )
        except (MapTileError, OSError, ValueError) as exc:
            log.warning("Impling Finder could not build Explv map crop: %s", exc)
            self._record_metric(
                MetricEvent(
                    kind="render",
                    outcome="error",
                    impling_type=spawn.type_key,
                    world=spawn.world,
                    location=self._location_for_spawn(spawn),
                    duration_ms=(time.monotonic() - total_started) * 1000,
                    fetch_ms=fetch_ms,
                    error_category=type(exc).__name__,
                )
            )
            return None

        center_x = MAP_IMAGE_SIZE // 2
        center_y = MAP_IMAGE_SIZE // 2
        icon_x = max(0, min(MAP_IMAGE_SIZE - icon.width, center_x - icon.width // 2))
        icon_y = max(0, min(MAP_IMAGE_SIZE - icon.height, center_y - icon.height // 2))
        image.alpha_composite(icon, (icon_x, icon_y))

        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        total_ms = (time.monotonic() - total_started) * 1000
        self._record_metric(
            MetricEvent(
                kind="render",
                impling_type=spawn.type_key,
                world=spawn.world,
                location=self._location_for_spawn(spawn),
                duration_ms=total_ms,
                fetch_ms=fetch_ms,
                render_ms=max(0.0, total_ms - (fetch_ms or 0.0)),
            )
        )
        return discord.File(output, filename="impling-map.png")

    async def _fetch_map_tile(self, url: str) -> bytes:
        session = await self._get_session()
        try:
            async with session.get(url, headers={"Accept": "image/png"}) as response:
                if response.status < 200 or response.status >= 300:
                    raise MapTileError(f"map tile returned HTTP {response.status}: {url}")
                return await response.read()
        except aiohttp.ClientError as exc:
            raise MapTileError(f"map tile request failed: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise MapTileError("map tile request timed out") from exc

    def _load_pillow(self):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            if not self._pillow_warning_logged:
                log.warning("Pillow is not installed; Impling Finder screenshots are disabled")
                self._pillow_warning_logged = True
            return None
        return Image, ImageDraw, ImageFont

    def _load_font(self, size: int, *, bold: bool = False):
        pillow = self._load_pillow()
        if pillow is None:
            return None
        _Image, _ImageDraw, ImageFont = pillow
        paths = (
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
        for path in paths:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _make_card_file(self, spawn: ImplingSpawn) -> Optional[discord.File]:
        return self._make_coordinate_card_file(spawn)

    def _make_coordinate_card_file(self, spawn: ImplingSpawn) -> Optional[discord.File]:
        pillow = self._load_pillow()
        if pillow is None:
            return None
        Image, ImageDraw, _ImageFont = pillow

        type_key = spawn.type_key or "dragon"
        color = IMPLINGS.get(type_key, IMPLINGS["dragon"]).color
        accent = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
        image = Image.new("RGB", (760, 260), (28, 31, 35))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 760, 260), fill=(28, 31, 35))
        draw.rectangle((0, 0, 18, 260), fill=accent)
        draw.rectangle((40, 40, 720, 220), outline=(68, 74, 82), width=2)

        title_font = self._load_font(46, bold=True)
        label_font = self._load_font(24, bold=True)
        body_font = self._load_font(24)
        location = self._location_for_spawn(spawn)

        draw.text((56, 54), spawn.impling_name, fill=(245, 246, 250), font=title_font)
        draw.text((58, 134), "World", fill=accent, font=label_font)
        draw.text((58, 166), str(spawn.world), fill=(245, 246, 250), font=body_font)
        draw.text((210, 134), "Location", fill=accent, font=label_font)
        draw.text((210, 166), location, fill=(245, 246, 250), font=body_font)

        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return discord.File(output, filename="impling-card.png")

    async def _recent_spawns_for_types(
        self,
        settings: Mapping[str, Any],
        type_keys: list[str],
    ) -> list[ImplingSpawn]:
        if len(type_keys) == 1:
            id_endpoint = build_id_endpoint(settings.get("id_endpoint") or DEFAULT_ID_ENDPOINT)
            endpoint = f"{id_endpoint}{IMPLINGS[type_keys[0]].npcid}"
        else:
            endpoint = sanitize_endpoint_url(settings.get("endpoint") or DEFAULT_ENDPOINT)

        spawns = await self._fetch_spawns(endpoint)
        wanted_ids = set(npc_ids_for_types(type_keys))
        max_age = max(0, int(settings.get("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)))
        fresh = filter_stale_spawns(spawns, datetime.now(timezone.utc), max_age)
        return [spawn for spawn in fresh if spawn.npcid in wanted_ids]

    def _parse_bool(self, value: str) -> bool:
        normalized = value.strip().lower()
        if normalized in {"true", "t", "yes", "y", "on", "1", "enable", "enabled"}:
            return True
        if normalized in {"false", "f", "no", "n", "off", "0", "disable", "disabled"}:
            return False
        raise ValueError("Expected true or false.")

    async def _settings_message(self, guild: discord.Guild, prefix: str) -> str:
        settings = await self.config.guild(guild).all()
        channels = self._normalize_channels(settings.get("channels", {}))
        puro_channel = self._normalize_puro_channel(settings)
        puro_channel_display = f"<#{puro_channel}>" if puro_channel else "none"
        lines = [
            "Impling Finder settings",
            f"Enabled: `{bool(settings.get('enabled'))}`",
            f"Poll interval: `{settings.get('poll_interval', DEFAULT_POLL_INTERVAL_SECONDS)}s`",
            f"Max age: `{settings.get('max_age_seconds', DEFAULT_MAX_AGE_SECONDS)}s`",
            f"Map screenshots: `{bool(settings.get('screenshots'))}`",
            f"Puro-Puro: `{bool(settings.get('puro_enabled', False))}`",
            f"Puro-Puro channel: `{puro_channel_display}`",
            f"Endpoint: `{settings.get('endpoint', DEFAULT_ENDPOINT)}`",
        ]
        if not channels:
            lines.append("Channels: `none`")
            if not self._feed_channels_for_settings(settings, channels):
                lines.append(f"Next: run `{prefix}implingset addchannel #channel dragon lucky`.")
            return "\n".join(lines)

        lines.append("Channels:")
        for channel_id, npc_ids in channels.items():
            names = [
                IMPLINGS[type_key].key
                for type_key in IMPLING_ORDER
                if IMPLINGS[type_key].npcid in npc_ids
            ]
            lines.append(f"- <#{channel_id}>: `{', '.join(names)}`")
        return "\n".join(lines)

    @commands.group(name="implingset", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset(self, ctx: commands.Context) -> None:
        """Configure automatic Impling Finder spawn posts."""
        await ctx.send(await self._settings_message(ctx.guild, ctx.clean_prefix))

    @implingset.command(name="enable")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_enable(self, ctx: commands.Context) -> None:
        """Enable automatic impling spawn polling for this server."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("Impling Finder polling is enabled.")

    @implingset.command(name="disable")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_disable(self, ctx: commands.Context) -> None:
        """Disable automatic impling spawn polling for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("Impling Finder polling is disabled.")

    @implingset.command(name="interval")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set the polling interval in seconds. Minimum: 5 seconds."""
        if seconds < MIN_POLL_INTERVAL_SECONDS:
            await ctx.send(f"Polling interval must be at least {MIN_POLL_INTERVAL_SECONDS} seconds.")
            return
        await self.config.guild(ctx.guild).poll_interval.set(seconds)
        await ctx.send(f"Polling interval set to `{seconds}s`.")

    @implingset.command(name="maxage")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_maxage(self, ctx: commands.Context, seconds: int) -> None:
        """Set the maximum spawn age in seconds before sightings are ignored."""
        if seconds < 0:
            await ctx.send("Max age must be zero or greater.")
            return
        await self.config.guild(ctx.guild).max_age_seconds.set(seconds)
        await ctx.send(f"Max spawn age set to `{seconds}s`.")

    @implingset.command(name="addchannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_addchannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        *types: str,
    ) -> None:
        """Route one or more impling types to a channel."""
        try:
            type_keys = parse_impling_types(types)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        npc_ids = npc_ids_for_types(type_keys)
        async with self.config.guild(ctx.guild).channels() as channels:
            channels[str(channel.id)] = npc_ids
        names = ", ".join(type_keys)
        await ctx.send(f"{channel.mention} will receive `{names}` impling posts.")

    @implingset.command(name="puro")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_puro(self, ctx: commands.Context, value: str) -> None:
        """Enable or disable Puro-Puro impling posts."""
        try:
            enabled = self._parse_bool(value)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await self.config.guild(ctx.guild).puro_enabled.set(enabled)
        await ctx.send(f"Puro-Puro impling posts are now `{enabled}`.")

    @implingset.command(name="purochannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_purochannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        """Set the dedicated Puro-Puro impling feed channel."""
        await self.config.guild(ctx.guild).puro_channel.set(str(channel.id))
        await ctx.send(f"{channel.mention} will receive Puro-Puro impling posts.")

    @implingset.command(name="removechannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_removechannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        """Remove an impling feed channel."""
        async with self.config.guild(ctx.guild).channels() as channels:
            removed = channels.pop(str(channel.id), None)
        if removed is None:
            await ctx.send(f"{channel.mention} was not configured.")
            return
        await ctx.send(f"Removed Impling Finder routing for {channel.mention}.")

    @implingset.group(name="access", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access(self, ctx: commands.Context) -> None:
        """Configure reaction role access for existing impling access messages."""
        await self.implingset_access_list(ctx)

    @implingset_access.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access_add(
        self,
        ctx: commands.Context,
        message_id: int,
        emoji: str,
        role: discord.Role,
    ) -> None:
        """Map a reaction on an existing access message to a role."""
        message_key = str(int(message_id))
        emoji_key = self._access_emoji_key(emoji)
        if not emoji_key:
            await ctx.send("Provide a valid emoji.")
            return

        async with self.config.guild(ctx.guild).access_reactions() as access_reactions:
            access_reactions.setdefault(message_key, {})[emoji_key] = str(int(role.id))

        role_display = getattr(role, "mention", self._role_mention(str(role.id)))
        await ctx.send(f"Reaction `{emoji_key}` on message `{message_key}` will manage {role_display}.")

    @implingset_access.command(name="remove")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access_remove(
        self,
        ctx: commands.Context,
        message_id: int,
        emoji: str,
    ) -> None:
        """Remove one access reaction mapping."""
        message_key = str(int(message_id))
        emoji_key = self._access_emoji_key(emoji)

        async with self.config.guild(ctx.guild).access_reactions() as access_reactions:
            message_mappings = access_reactions.get(message_key)
            if not isinstance(message_mappings, dict) or emoji_key not in message_mappings:
                await ctx.send(f"No access reaction `{emoji_key}` is configured for message `{message_key}`.")
                return
            message_mappings.pop(emoji_key, None)
            if not message_mappings:
                access_reactions.pop(message_key, None)

        await ctx.send(f"Removed access reaction `{emoji_key}` from message `{message_key}`.")

    @implingset_access.command(name="list")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access_list(self, ctx: commands.Context) -> None:
        """List configured access reaction mappings."""
        raw_access = await self.config.guild(ctx.guild).access_reactions()
        access_reactions = self._normalize_access_reactions(raw_access)
        if not access_reactions:
            await ctx.send("No access reactions configured.")
            return

        lines = ["Access reactions:"]
        for message_id in sorted(access_reactions, key=int):
            for emoji_key, role_id in sorted(access_reactions[message_id].items()):
                lines.append(
                    f"- message `{message_id}` `{emoji_key}` -> {self._role_mention(role_id)}"
                )
        await ctx.send("\n".join(lines))

    @implingset.command(name="list")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_list(self, ctx: commands.Context) -> None:
        """Show Impling Finder settings for this server."""
        await ctx.send(await self._settings_message(ctx.guild, ctx.clean_prefix))

    @implingset.command(name="screenshots")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_screenshots(self, ctx: commands.Context, value: str) -> None:
        """Enable or disable generated map screenshot attachments."""
        try:
            enabled = self._parse_bool(value)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await self.config.guild(ctx.guild).screenshots.set(enabled)
        await ctx.send(f"Generated map screenshots are now `{enabled}`.")

    @implingset.command(name="endpoint")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_endpoint(self, ctx: commands.Context, url: str) -> None:
        """Override the read-only Impling Finder ORDS endpoint."""
        try:
            endpoint = sanitize_endpoint_url(url)
            id_endpoint = build_id_endpoint(endpoint)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await self.config.guild(ctx.guild).endpoint.set(endpoint)
        await self.config.guild(ctx.guild).id_endpoint.set(id_endpoint)
        await ctx.send("Impling Finder endpoint updated.")

    @implingset.command(name="resetendpoint")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_resetendpoint(self, ctx: commands.Context) -> None:
        """Reset the backend endpoint to the RuneLite plugin's default ORDS endpoint."""
        await self.config.guild(ctx.guild).endpoint.set(DEFAULT_ENDPOINT)
        await self.config.guild(ctx.guild).id_endpoint.set(DEFAULT_ID_ENDPOINT)
        await ctx.send("Impling Finder endpoint reset to the default backend.")

    @implingset.command(name="clearseen")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_clearseen(self, ctx: commands.Context) -> None:
        """Clear this server's dedupe cache."""
        async with self.config.seen() as seen:
            seen[str(ctx.guild.id)] = []
        await ctx.send("Cleared this server's Impling Finder seen cache.")

    @commands.command(name="implingrecent")
    @commands.guild_only()
    async def implingrecent(
        self,
        ctx: commands.Context,
        impling_type: str = "all",
        count: int = 10,
    ) -> None:
        """Fetch recent impling sightings on demand without changing feed settings."""
        try:
            type_keys = parse_impling_types([impling_type])
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        if count < 1 or count > MANUAL_RECENT_MAX_COUNT:
            await ctx.send(f"Count must be between 1 and {MANUAL_RECENT_MAX_COUNT}.")
            return

        settings = await self.config.guild(ctx.guild).all()
        async with ctx.typing():
            try:
                spawns = await self._recent_spawns_for_types(settings, type_keys)
            except (BackendError, ValueError) as exc:
                log.warning("Manual Impling Finder request failed in guild %s: %s", ctx.guild.id, exc)
                await ctx.send(f"Could not fetch impling data: {exc}")
                return

        spawns = spawns[:count]
        if not spawns:
            await ctx.send("No recent matching impling spawns found.")
            return

        channel = ctx.channel
        permissions = self._bot_permissions(ctx.guild, channel)
        screenshots = bool(settings.get("screenshots", False))
        for spawn in spawns:
            if permissions is not None and not permissions.embed_links:
                await ctx.send(
                    self._content_for_spawn(spawn),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                continue

            embed = self._embed_for_spawn(spawn)
            file = None
            if screenshots and (permissions is None or permissions.attach_files):
                file, _render_ms = await self._make_screenshot_file_for_send(spawn)
                if file is not None:
                    embed.set_image(url=f"attachment://{file.filename}")
            if file is not None:
                await ctx.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
