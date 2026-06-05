from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import aiohttp
import discord
from redbot.core import Config, commands

from .core import (
    DEFAULT_ENDPOINT,
    DEFAULT_ID_ENDPOINT,
    DEFAULT_MAP_ZOOM,
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    IMPLING_ORDER,
    IMPLINGS,
    MIN_POLL_INTERVAL_SECONDS,
    build_id_endpoint,
    build_map_url,
    explv_tiles_for_crop,
    filter_stale_spawns,
    format_age,
    matching_channel_ids,
    npc_ids_for_types,
    parse_backend_payload,
    parse_impling_types,
    sanitize_endpoint_url,
    select_unseen_spawns,
)
from .core import ImplingSpawn


log = logging.getLogger("red.implingfinder")

REQUEST_TIMEOUT_SECONDS = 12
MAX_BACKOFF_SECONDS = 300
MANUAL_RECENT_MAX_COUNT = 25
CONFIG_IDENTIFIER = 0x1A9D1F1644
MAP_IMAGE_WIDTH = 760
MAP_IMAGE_HEIGHT = 420


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

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
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
        if self.session is not None and not self.session.closed:
            await self.session.close()

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

        try:
            endpoint = sanitize_endpoint_url(settings.get("endpoint") or DEFAULT_ENDPOINT)
            spawns = await self._fetch_spawns(endpoint)
        except BackendError as exc:
            self._record_backend_failure(guild.id, exc)
            return
        except ValueError as exc:
            log.warning("Invalid Impling Finder endpoint for guild %s: %s", guild.id, exc)
            return

        self._record_backend_success(guild.id)
        await self._process_polled_spawns(guild, settings, channels, spawns)

    async def _process_polled_spawns(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
        channels: dict[str, list[int]],
        spawns: list[ImplingSpawn],
    ) -> None:
        now = datetime.now(timezone.utc)
        max_age = max(0, int(settings.get("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)))
        current_spawn_keys = {spawn.dedupe_key for spawn in spawns}
        await self._delete_missing_active_messages(guild, current_spawn_keys)

        fresh_spawns = filter_stale_spawns(spawns, now, max_age)
        routed_spawns = [
            spawn for spawn in reversed(fresh_spawns) if matching_channel_ids(channels, spawn)
        ]
        if not routed_spawns:
            return

        guild_key = str(guild.id)
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
                message = await self._send_spawn_to_channel(
                    guild,
                    channel,
                    spawn,
                    screenshots=bool(settings.get("screenshots", False)),
                )
                await self._record_active_message(guild, spawn, channel_id, message)

    async def _delete_missing_active_messages(
        self,
        guild: discord.Guild,
        current_spawn_keys: set[str],
    ) -> None:
        guild_key = str(guild.id)
        async with self.config.active_messages() as active_messages:
            guild_messages = active_messages.get(guild_key)
            if not isinstance(guild_messages, dict):
                if guild_key in active_messages:
                    active_messages.pop(guild_key, None)
                return

            for spawn_key, channel_messages in list(guild_messages.items()):
                if spawn_key in current_spawn_keys:
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

    async def _delete_tracked_message(
        self,
        guild: discord.Guild,
        channel_id: Any,
        message_id: Any,
    ) -> bool:
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

            spawn_messages = guild_messages.get(spawn.dedupe_key)
            if not isinstance(spawn_messages, dict):
                spawn_messages = {}
                guild_messages[spawn.dedupe_key] = spawn_messages

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

    def _embed_for_spawn(self, spawn: ImplingSpawn, *, now: Optional[datetime] = None) -> discord.Embed:
        now = now or datetime.now(timezone.utc)
        age_seconds = (now - spawn.discovered.astimezone(timezone.utc)).total_seconds()
        type_key = spawn.type_key or "dragon"
        info = IMPLINGS.get(type_key)
        color = info.color if info is not None else 0x5865F2
        map_url = build_map_url(spawn)

        embed = discord.Embed(
            title=f"{spawn.impling_name} spotted",
            url=map_url,
            color=color,
            timestamp=spawn.discovered.astimezone(timezone.utc),
        )
        embed.add_field(name="World", value=str(spawn.world), inline=True)
        embed.add_field(name="Coordinates", value=f"{spawn.xcoord}, {spawn.ycoord}", inline=True)
        embed.add_field(name="Plane", value=str(spawn.plane), inline=True)
        embed.add_field(name="Age", value=format_age(age_seconds), inline=True)
        embed.add_field(
            name="Discovered",
            value=f"<t:{spawn.discovered_epoch}:F>\n<t:{spawn.discovered_epoch}:R>",
            inline=True,
        )
        embed.add_field(name="NPC ID", value=str(spawn.npcid), inline=True)
        embed.add_field(name="Map", value=f"[Open Explv map]({map_url})", inline=False)
        embed.set_footer(text="Read-only data from the Impling Finder ORDS backend")
        return embed

    def _content_for_spawn(self, spawn: ImplingSpawn) -> str:
        return (
            f"{spawn.impling_name} spotted on world {spawn.world} at "
            f"{spawn.xcoord}, {spawn.ycoord}, plane {spawn.plane}. "
            f"Map: {build_map_url(spawn)}"
        )

    async def _send_spawn_to_channel(
        self,
        guild: discord.Guild,
        channel,
        spawn: ImplingSpawn,
        *,
        screenshots: bool,
    ) -> Optional[discord.Message]:
        permissions = self._bot_permissions(guild, channel)
        if permissions is not None and not permissions.send_messages:
            log.warning(
                "Impling Finder cannot send messages in channel %s for guild %s",
                getattr(channel, "id", "?"),
                guild.id,
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
            return await channel.send(
                self._content_for_spawn(spawn),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        embed = self._embed_for_spawn(spawn)
        file = None
        if screenshots:
            if can_attach:
                file = await self._make_screenshot_file(spawn)
                if file is not None:
                    embed.set_image(url=f"attachment://{file.filename}")
            else:
                log.warning(
                    "Impling Finder lacks Attach Files in channel %s for guild %s; skipping card",
                    getattr(channel, "id", "?"),
                    guild.id,
                )

        try:
            if file is not None:
                return await channel.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning(
                "Impling Finder was denied while sending to channel %s in guild %s",
                getattr(channel, "id", "?"),
                guild.id,
            )
            return None
        except discord.HTTPException:
            log.exception(
                "Impling Finder failed to send spawn to channel %s in guild %s",
                getattr(channel, "id", "?"),
                guild.id,
            )
            return None

    def _bot_permissions(self, guild: discord.Guild, channel):
        member = getattr(guild, "me", None)
        if member is None and getattr(self.bot, "user", None) is not None:
            member = guild.get_member(self.bot.user.id)
        if member is None or not hasattr(channel, "permissions_for"):
            return None
        return channel.permissions_for(member)

    async def _make_screenshot_file(self, spawn: ImplingSpawn) -> Optional[discord.File]:
        map_file = await self._make_map_file(spawn)
        if map_file is not None:
            return map_file
        return self._make_coordinate_card_file(spawn)

    async def _make_map_file(self, spawn: ImplingSpawn) -> Optional[discord.File]:
        pillow = self._load_pillow()
        if pillow is None:
            return None
        Image, ImageDraw, _ImageFont = pillow

        tiles = explv_tiles_for_crop(
            spawn,
            width=MAP_IMAGE_WIDTH,
            height=MAP_IMAGE_HEIGHT,
            zoom=DEFAULT_MAP_ZOOM,
        )
        try:
            tile_payloads = await asyncio.gather(
                *(self._fetch_map_tile(tile.url) for tile in tiles)
            )
            image = Image.new("RGB", (MAP_IMAGE_WIDTH, MAP_IMAGE_HEIGHT), (18, 20, 24))
            for tile, payload in zip(tiles, tile_payloads):
                tile_image = Image.open(io.BytesIO(payload)).convert("RGB")
                image.paste(tile_image, (tile.paste_x, tile.paste_y))
        except (MapTileError, OSError, ValueError) as exc:
            log.warning("Impling Finder could not build Explv map crop: %s", exc)
            return None

        self._draw_map_overlay(image, spawn, ImageDraw)

        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
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

    def _draw_map_overlay(self, image, spawn: ImplingSpawn, ImageDraw) -> None:
        draw = ImageDraw.Draw(image, "RGBA")
        width, height = image.size
        center_x = width // 2
        center_y = height // 2
        type_key = spawn.type_key or "dragon"
        color = IMPLINGS.get(type_key, IMPLINGS["dragon"]).color
        accent = ((color >> 16) & 255, (color >> 8) & 255, color & 255)

        draw.rectangle((0, 0, width, 74), fill=(12, 14, 17, 218))
        draw.rectangle((0, 72, width, 76), fill=(*accent, 235))

        title_font = self._load_font(31, bold=True)
        body_font = self._load_font(21)
        draw.text((18, 11), spawn.impling_name, fill=(248, 249, 252, 255), font=title_font)
        draw.text(
            (20, 47),
            f"World {spawn.world} | {spawn.xcoord}, {spawn.ycoord}, plane {spawn.plane}",
            fill=(220, 225, 232, 255),
            font=body_font,
        )

        draw.ellipse(
            (center_x - 14, center_y - 14, center_x + 14, center_y + 14),
            outline=(255, 255, 255, 245),
            width=4,
        )
        draw.ellipse(
            (center_x - 8, center_y - 8, center_x + 8, center_y + 8),
            fill=(*accent, 235),
            outline=(15, 18, 22, 240),
            width=2,
        )
        marker_line = (255, 255, 255, 230)
        draw.line((center_x - 30, center_y, center_x - 17, center_y), fill=marker_line, width=3)
        draw.line((center_x + 17, center_y, center_x + 30, center_y), fill=marker_line, width=3)
        draw.line((center_x, center_y - 30, center_x, center_y - 17), fill=marker_line, width=3)
        draw.line((center_x, center_y + 17, center_x, center_y + 30), fill=marker_line, width=3)

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
        image = Image.new("RGB", (760, 300), (28, 31, 35))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 760, 300), fill=(28, 31, 35))
        draw.rectangle((0, 0, 18, 300), fill=accent)
        draw.rectangle((40, 40, 720, 260), outline=(68, 74, 82), width=2)

        title_font = self._load_font(46, bold=True)
        label_font = self._load_font(24, bold=True)
        body_font = self._load_font(30)
        small_font = self._load_font(22)
        discovered = spawn.discovered.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        draw.text((56, 54), spawn.impling_name, fill=(245, 246, 250), font=title_font)
        draw.text((58, 134), "World", fill=accent, font=label_font)
        draw.text((58, 166), str(spawn.world), fill=(245, 246, 250), font=body_font)
        draw.text((210, 134), "Coords", fill=accent, font=label_font)
        draw.text((210, 166), f"{spawn.xcoord}, {spawn.ycoord}", fill=(245, 246, 250), font=body_font)
        draw.text((430, 134), "Plane", fill=accent, font=label_font)
        draw.text((430, 166), str(spawn.plane), fill=(245, 246, 250), font=body_font)
        draw.text((58, 224), discovered, fill=(205, 210, 218), font=small_font)

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
        """Set the polling interval in seconds. Minimum: 10 seconds."""
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
                file = await self._make_screenshot_file(spawn)
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
