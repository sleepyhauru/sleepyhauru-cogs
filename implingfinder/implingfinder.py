from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import time
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
    build_id_endpoint,
    build_map_url,
    collapse_duplicate_sightings,
    explv_tiles_for_crop,
    filter_stale_spawns,
    impling_icon_center,
    matching_channel_ids,
    npc_ids_for_types,
    parse_backend_payload,
    parse_impling_types,
    resolve_location_name,
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
MAP_LABELS_PATH = COG_DIR / "data" / "map_labels.json"
IMPLING_ASSET_DIR = COG_DIR / "assets"
MAP_IMAGE_SIZE = 512
MAP_TILE_ZOOM = 10
MAP_CROP_SIZE = 256
IMPLING_ICON_SIZE = 72
MAP_RENDER_SEND_TIMEOUT_SECONDS = 2.0


class BackendError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class MapTileError(Exception):
    pass


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
        )
        self.config.register_global(seen={}, active_messages={})

        self.session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._last_poll: dict[int, float] = {}
        self._failure_counts: dict[int, int] = {}
        self._backoff_until: dict[int, float] = {}
        self._pillow_warning_logged = False
        self._map_labels = self._load_map_labels()
        self.metrics_store: Optional[MetricsStore] = None
        self.dashboard: Optional[DashboardServer] = None
        self._started_at = time.time()

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
        self._poll_task = asyncio.create_task(self._poll_loop(), name="implingfinder-poller")

    def cog_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        self._create_task(self._close_resources())

    def _create_task(self, coro):
        loop = getattr(self.bot, "loop", None)
        if loop is not None and not loop.is_closed():
            return loop.create_task(coro)
        return asyncio.create_task(coro)

    async def _close_resources(self) -> None:
        if self._poll_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
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
                await self._poll_enabled_guilds()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Impling Finder polling loop hit an unexpected error")
            await asyncio.sleep(MIN_POLL_INTERVAL_SECONDS)

    async def _poll_enabled_guilds(self) -> None:
        now_monotonic = time.monotonic()
        for guild in list(getattr(self.bot, "guilds", [])):
            try:
                await self._poll_guild(guild, now_monotonic)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Impling Finder failed while polling guild %s", getattr(guild, "id", "?"))

    async def _poll_guild(self, guild: discord.Guild, now_monotonic: float) -> None:
        if await self._cog_disabled_in_guild(guild):
            return

        settings = await self.config.guild(guild).all()
        if not settings.get("enabled"):
            return

        channels = self._normalize_channels(settings.get("channels", {}))
        if not channels:
            return

        interval = max(
            MIN_POLL_INTERVAL_SECONDS,
            int(settings.get("poll_interval", DEFAULT_POLL_INTERVAL_SECONDS)),
        )
        if now_monotonic - self._last_poll.get(guild.id, 0) < interval:
            return

        if now_monotonic < self._backoff_until.get(guild.id, 0):
            return

        self._last_poll[guild.id] = now_monotonic
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
            return
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
            return

        fetch_ms = (time.monotonic() - fetch_started) * 1000
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

    async def _process_polled_spawns(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
        channels: dict[str, list[int]],
        spawns: list[ImplingSpawn],
        *,
        fetch_ms: Optional[float] = None,
        process_started: Optional[float] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        max_age = max(0, int(settings.get("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)))
        current_sighting_keys = {spawn.sighting_key for spawn in spawns}
        current_sighting_aliases: dict[str, str] = {}
        for spawn in spawns:
            current_sighting_aliases.setdefault(spawn.dedupe_key, spawn.sighting_key)
            current_sighting_aliases.setdefault(spawn.legacy_area_key, spawn.sighting_key)
        await self._delete_missing_active_messages(
            guild,
            current_sighting_keys,
            current_sighting_aliases,
        )
        await self._clear_missing_seen_sightings(
            guild,
            current_sighting_keys,
            current_sighting_aliases,
        )

        fresh_spawns = filter_stale_spawns(spawns, now, max_age)
        unique_spawns = collapse_duplicate_sightings(fresh_spawns)
        duplicate_count = max(0, len(fresh_spawns) - len(unique_spawns))
        routed_spawns = [
            spawn for spawn in reversed(unique_spawns) if matching_channel_ids(channels, spawn)
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
        if not routed_spawns:
            return

        guild_key = guild_id
        async with self.config.seen() as seen:
            current_seen = list(seen.get(guild_key, []))
            to_announce, updated_seen = select_unseen_spawns(
                routed_spawns,
                current_seen,
                announce_existing=bool(settings.get("announce_existing", False)),
            )
            seen[guild_key] = updated_seen

        for spawn in to_announce:
            for channel_id in matching_channel_ids(channels, spawn):
                channel = guild.get_channel(channel_id)
                if channel is None:
                    log.warning(
                        "Impling Finder channel %s no longer exists in guild %s",
                        channel_id,
                        guild.id,
                    )
                    continue
                send_kwargs = {"screenshots": bool(settings.get("screenshots", False))}
                if fetch_ms is not None:
                    send_kwargs["fetch_ms"] = fetch_ms
                if process_started is not None:
                    send_kwargs["process_ms"] = (time.monotonic() - process_started) * 1000
                message = await self._send_spawn_to_channel(guild, channel, spawn, **send_kwargs)
                await self._record_active_message(guild, spawn, channel_id, message)

    async def _delete_missing_active_messages(
        self,
        guild: discord.Guild,
        current_sighting_keys: set[str],
        current_sighting_aliases: Mapping[str, str],
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
                if migrated_key is not None and migrated_key != spawn_key:
                    if isinstance(channel_messages, dict):
                        migrated_messages = guild_messages.get(migrated_key)
                        if not isinstance(migrated_messages, dict):
                            migrated_messages = {}
                            guild_messages[migrated_key] = migrated_messages
                        migrated_messages.update(channel_messages)
                    guild_messages.pop(spawn_key, None)
                    continue

                if spawn_key in current_sighting_keys:
                    continue

                if not isinstance(channel_messages, dict):
                    guild_messages.pop(spawn_key, None)
                    continue

                for channel_id, message_id in list(channel_messages.items()):
                    if await self._delete_tracked_message(guild, channel_id, message_id):
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

    async def _delete_tracked_message(
        self,
        guild: discord.Guild,
        channel_id: Any,
        message_id: Any,
    ) -> bool:
        delete_started = time.monotonic()
        try:
            clean_channel_id = int(channel_id)
            clean_message_id = int(message_id)
        except (TypeError, ValueError):
            return True

        channel = guild.get_channel(clean_channel_id)
        if channel is None:
            log.warning(
                "Impling Finder cannot delete despawned message %s; channel %s is missing",
                clean_message_id,
                clean_channel_id,
            )
            return True

        try:
            if hasattr(channel, "get_partial_message"):
                message = channel.get_partial_message(clean_message_id)
            else:
                message = await channel.fetch_message(clean_message_id)
            await message.delete()
            self._record_metric(
                MetricEvent(
                    kind="despawn",
                    guild_id=str(guild.id),
                    guild_name=str(getattr(guild, "name", guild.id)),
                    channel_id=str(clean_channel_id),
                    channel_name=str(getattr(channel, "name", "")) or None,
                    duration_ms=(time.monotonic() - delete_started) * 1000,
                )
            )
            return True
        except discord.NotFound:
            return True
        except discord.Forbidden:
            log.warning(
                "Impling Finder lacks permission to delete despawned message %s in channel %s",
                clean_message_id,
                clean_channel_id,
            )
            return False
        except discord.HTTPException:
            log.exception(
                "Impling Finder failed to delete despawned message %s in channel %s",
                clean_message_id,
                clean_channel_id,
            )
            return False

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

            spawn_messages[clean_channel_id] = clean_message_id

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

    def _load_map_labels(self) -> list[MapLabel]:
        try:
            raw_labels = json.loads(MAP_LABELS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Impling Finder could not load bundled Explv map labels")
            return []

        labels: list[MapLabel] = []
        for item in raw_labels:
            try:
                labels.append(
                    MapLabel(
                        name=str(item["name"]),
                        xcoord=int(item["x"]),
                        ycoord=int(item["y"]),
                        plane=int(item["plane"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return labels

    def _location_for_spawn(self, spawn: ImplingSpawn) -> str:
        return resolve_location_name(spawn, self._map_labels)

    def _embed_for_spawn(self, spawn: ImplingSpawn, *, now: Optional[datetime] = None) -> discord.Embed:
        type_key = spawn.type_key or "dragon"
        info = IMPLINGS.get(type_key)
        color = info.color if info is not None else 0x5865F2
        map_url = build_map_url(spawn)
        location = self._location_for_spawn(spawn)

        embed = discord.Embed(
            title=f"{spawn.impling_name} spotted",
            url=map_url,
            color=color,
            timestamp=spawn.discovered.astimezone(timezone.utc),
        )
        embed.add_field(name="World", value=str(spawn.world), inline=True)
        embed.add_field(name="Location", value=location, inline=True)
        embed.add_field(
            name="Discovered",
            value=f"<t:{spawn.discovered_epoch}:F>\n<t:{spawn.discovered_epoch}:R>",
            inline=True,
        )
        embed.add_field(name="Map", value=f"[Open Explv map]({map_url})", inline=False)
        return embed

    def _content_for_spawn(self, spawn: ImplingSpawn) -> str:
        return (
            f"{spawn.impling_name} spotted on world {spawn.world} at "
            f"{self._location_for_spawn(spawn)}."
        )

    async def _send_spawn_to_channel(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        *,
        screenshots: bool,
        fetch_ms: Optional[float] = None,
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
                process_ms=process_ms,
                error_category="send_messages",
            )
            return None

        can_embed = permissions is None or permissions.embed_links
        can_attach = permissions is None or permissions.attach_files
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
            file = None
            if screenshots:
                if can_attach:
                    file, render_ms = await self._make_screenshot_file_for_send(spawn)
                    if file is not None:
                        embed.set_image(url=f"attachment://{file.filename}")
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
            if file is not None:
                kwargs["file"] = file

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
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
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
                process_ms=process_ms,
                render_ms=render_ms,
                send_ms=send_ms,
                error_category="discord_http",
            )
            return None

    def _record_post_metric(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        *,
        outcome: str,
        total_started: float,
        fetch_ms: Optional[float],
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

        center_x, center_y = impling_icon_center(spawn, canvas_size=MAP_CROP_SIZE)
        if MAP_CROP_SIZE != MAP_IMAGE_SIZE:
            center_x = round(center_x * (MAP_IMAGE_SIZE / MAP_CROP_SIZE))
            center_y = round(center_y * (MAP_IMAGE_SIZE / MAP_CROP_SIZE))
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
        lines = [
            "Impling Finder settings",
            f"Enabled: `{bool(settings.get('enabled'))}`",
            f"Poll interval: `{settings.get('poll_interval', DEFAULT_POLL_INTERVAL_SECONDS)}s`",
            f"Max age: `{settings.get('max_age_seconds', DEFAULT_MAX_AGE_SECONDS)}s`",
            f"Map screenshots: `{bool(settings.get('screenshots'))}`",
            f"Endpoint: `{settings.get('endpoint', DEFAULT_ENDPOINT)}`",
        ]
        if not channels:
            lines.append("Channels: `none`")
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
