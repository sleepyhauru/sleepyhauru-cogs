import aiohttp
import discord
import io
import re
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any

from redbot.core import commands


DISCORD_EMOJI_SIZE_LIMIT = 256 * 1024
ASSET_EXTENSIONS = ("gif", "png", "webp")
ASSET_SIZES = ("4x", "3x", "2x", "1x")
SEVENTV_V3_EMOTE_URL = "https://7tv.io/v3/emotes/{emote_id}"
SEVENTV_V2_EMOTE_URL = "https://api.7tv.app/v2/emotes/{emote_id}"
SEVENTV_CDN_URL = "https://cdn.7tv.app/emote/{emote_id}/{asset_name}"

EMOJI_SLOTS = "This server doesn't have any more space for emojis!"
EMOJI_FAIL = "Failed to upload emoji"
INVALID_LINK = "Please provide a valid 7TV emote link."
FETCH_FAIL = "Couldn't fetch the 7TV emote asset."
FETCH_FAIL_TOO_LARGE = "The 7TV emote exists, but no asset fit within Discord's 256 KiB emoji limit."
FETCH_FAIL_WEBP = "The 7TV emote was only available as WEBP, and conversion to a Discord-safe format failed."
INFO_FAIL = "Couldn't load 7TV emote details."
UPLOAD_NOT_ALLOWED = "You need Manage Emojis and Stickers to upload 7TV emotes."


ID_PATTERNS = (
    # 7TV v3 emote IDs can be 26+ chars; allow a generous range
    re.compile(r"https?://(?:www\.)?7tv\.app/(?:emote|emotes)/([A-Za-z0-9]{24,36})", re.IGNORECASE),
    re.compile(r"https?://cdn\.7tv\.app/emote/([A-Za-z0-9]{24,36})/", re.IGNORECASE),
)


def _sanitize_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    cleaned = "".join(re.findall(r"\w+", name))
    if len(cleaned) < 2:
        return None
    return cleaned[:32]


def _available_emoji_slots(guild: discord.Guild, animated: bool) -> int:
    current = len([e for e in guild.emojis if e.animated == animated])
    return guild.emoji_limit - current


HEADERS = {"User-Agent": "Mozilla/5.0 Red-DiscordBot-SevenTV/1.0"}


@dataclass
class AssetResult:
    data: Optional[bytes]
    is_animated: Optional[bool]
    ext: Optional[str]
    reason: Optional[str] = None


def _extract_meta_fields(data: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[bool]]:
    if not isinstance(data, dict):
        return None, None
    return data.get("name"), data.get("animated")


async def _read_asset_response(resp, limit: int = DISCORD_EMOJI_SIZE_LIMIT) -> Optional[bytes]:
    content_length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
    if content_length:
        try:
            if int(content_length) > limit:
                return None
        except ValueError:
            return None

    content = getattr(resp, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if not callable(iter_chunked):
        data = await resp.read()
        return data if len(data) <= limit else None

    chunks = []
    total = 0
    async for chunk in iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_7tv_v3_emote(session: aiohttp.ClientSession, emote_id: str) -> Optional[Dict[str, Any]]:
    try:
        async with session.get(SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id), headers=HEADERS) as resp:
            if resp.status == 200:
                return await resp.json()
    except aiohttp.ClientError:
        pass
    return None


async def _fetch_7tv_meta(
    session: aiohttp.ClientSession, emote_id: str, v3_data: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[str], Optional[bool]]:
    """Return (name, animated) if available via 7TV API; tolerate failures.

    Tries v3 API first, then v2 as fallback. Returns (None, None) on failure.
    """
    if v3_data is None:
        v3_data = await _fetch_7tv_v3_emote(session, emote_id)
    name, animated = _extract_meta_fields(v3_data)
    if name is not None or animated is not None:
        return name, animated

    # v2 fallback
    try:
        async with session.get(SEVENTV_V2_EMOTE_URL.format(emote_id=emote_id), headers=HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                name = data.get("name")
                animated = bool(data.get("animated", False))
                return name, animated
    except aiohttp.ClientError:
        pass

    return None, None


async def _fetch_7tv_asset_via_meta(
    session: aiohttp.ClientSession,
    emote_id: str,
    v3_data: Optional[Dict[str, Any]] = None,
) -> AssetResult:
    """Use 7TV v3 metadata to choose a concrete asset URL and download it.

    Returns an AssetResult describing the best candidate under Discord's size limit.
    """
    data = v3_data or await _fetch_7tv_v3_emote(session, emote_id)
    if not data:
        return AssetResult(None, None, None, reason="unavailable")

    host = data.get("host") or {}
    base_url = host.get("url") or ""
    files = host.get("files") or []
    if not isinstance(files, list) or not base_url:
        return AssetResult(None, None, None, reason="unavailable")

    # Ensure absolute URL
    if base_url.startswith("//"):
        base_url = "https:" + base_url

    # Preference order by format and size name suffix
    def size_rank(name: str) -> int:
        if name.startswith("4x"):
            return 0
        if name.startswith("3x"):
            return 1
        if name.startswith("2x"):
            return 2
        return 3

    def file_key(f: Dict[str, Any]) -> tuple:
        fmt = str(f.get("format", "")).upper()
        name = str(f.get("name", ""))
        # format priority: GIF, PNG, WEBP
        fmt_rank = {"GIF": 0, "PNG": 1, "WEBP": 2}.get(fmt, 99)
        return (fmt_rank, size_rank(name))

    # Sort by preferred format and larger size first
    candidates = sorted(
        [f for f in files if isinstance(f, dict) and f.get("name")],
        key=file_key,
    )
    saw_oversized = False

    for f in candidates:
        fmt = str(f.get("format", "")).upper()
        name = str(f.get("name", ""))
        size = int(f.get("size", 0))
        if fmt not in ("GIF", "PNG", "WEBP"):
            continue
        if size <= 0:
            continue
        if size > DISCORD_EMOJI_SIZE_LIMIT:
            saw_oversized = True
            continue
        url = f"{base_url}/{name}"
        try:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    continue
                data_bytes = await _read_asset_response(resp)
        except aiohttp.ClientError:
            continue
        if data_bytes is None:
            saw_oversized = True
            continue
        return AssetResult(data_bytes, (fmt == "GIF"), fmt.lower())

    reason = "too_large" if saw_oversized else "unavailable"
    return AssetResult(None, None, None, reason=reason)


async def _fetch_7tv_bytes(
    session: aiohttp.ClientSession,
    emote_id: str,
    v3_data: Optional[Dict[str, Any]] = None,
) -> AssetResult:
    """Try to download a suitable emote asset from the 7TV CDN within Discord's limits.

    Returns an AssetResult describing success or the failure reason.
    Tries meta-informed URLs first, then generic path fallback.
    Preference order: GIF 4x->1x, then PNG 4x->1x, then WEBP 4x->1x (as last resort).
    """
    # Use meta-informed selection first
    result = await _fetch_7tv_asset_via_meta(session, emote_id, v3_data=v3_data)
    if result.data:
        return result

    saw_oversized = result.reason == "too_large"
    for ext in ASSET_EXTENSIONS:
        for size in ASSET_SIZES:
            url = SEVENTV_CDN_URL.format(emote_id=emote_id, asset_name=f"{size}.{ext}")
            try:
                async with session.get(url, headers=HEADERS) as resp:
                    if resp.status != 200:
                        continue
                    data = await _read_asset_response(resp)
                    if data is None:
                        saw_oversized = True
                        continue
                    return AssetResult(data, (ext == "gif"), ext)
            except aiohttp.ClientError:
                continue

    reason = "too_large" if saw_oversized else "unavailable"
    return AssetResult(None, None, None, reason=reason)


def _extract_7tv_id(url: str) -> Optional[str]:
    for pat in ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


class SevenTV(commands.Cog):
    """Upload a guild emoji from a 7TV link."""

    def __init__(self, bot):
        self.bot = bot
        self.session = None

    def cog_unload(self):
        if self.session is not None and not getattr(self.session, "closed", False):
            self.bot.loop.create_task(self.session.close())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or getattr(self.session, "closed", False):
            self.session = aiohttp.ClientSession()
        return self.session

    async def _can_upload_emojis(self, ctx: commands.Context) -> bool:
        author = getattr(ctx, "author", None)
        if author is None:
            return False

        is_owner = getattr(self.bot, "is_owner", None)
        if is_owner is not None:
            owner_result = is_owner(author)
            if hasattr(owner_result, "__await__"):
                owner_result = await owner_result
            if owner_result:
                return True

        permissions = getattr(author, "guild_permissions", None)
        return bool(
            getattr(permissions, "manage_emojis", False)
            or getattr(permissions, "manage_emojis_and_stickers", False)
        )

    def _resolve_emoji_name(self, guild: discord.Guild, requested_name: Optional[str], api_name: Optional[str]) -> str:
        base_name = _sanitize_name(requested_name) or _sanitize_name(api_name) or "seventv_emoji"
        existing_names = {emoji.name for emoji in guild.emojis}
        if base_name not in existing_names:
            return base_name

        for index in range(2, 100):
            suffix = f"_{index}"
            candidate = f"{base_name[: 32 - len(suffix)]}{suffix}"
            if candidate not in existing_names:
                return candidate
        return base_name[:29] + "_99"

    async def _normalize_asset(
        self,
        result: AssetResult,
        api_animated: Optional[bool],
    ) -> Tuple[Optional[bytes], Optional[bool], Optional[str], Optional[str]]:
        if not result.data:
            if result.reason == "too_large":
                return None, None, None, FETCH_FAIL_TOO_LARGE
            return None, None, None, FETCH_FAIL

        data = result.data
        is_animated = result.is_animated
        ext = result.ext
        animated_hint = bool(is_animated if is_animated is not None else api_animated)

        if ext == "webp":
            if animated_hint:
                converted = await self._webp_to_gif_under_limit(data)
                if not converted:
                    return None, None, None, FETCH_FAIL_WEBP
                return converted, True, "gif", None

            converted = await self._webp_to_png_under_limit(data)
            if not converted:
                return None, None, None, FETCH_FAIL_WEBP
            return converted, False, "png", None

        return data, animated_hint if ext == "gif" else bool(is_animated), ext, None

    async def _send_info(self, ctx: commands.Context, emote_id: str):
        session = await self._get_session()
        v3_data = await _fetch_7tv_v3_emote(session, emote_id)
        name, animated = await _fetch_7tv_meta(session, emote_id, v3_data=v3_data)
        result = await _fetch_7tv_bytes(session, emote_id, v3_data=v3_data)

        if name is None and animated is None and not result.data and result.reason == "unavailable":
            await ctx.send(INFO_FAIL)
            return

        asset_summary = f"{result.ext.upper()} asset within limit" if result.ext else result.reason or "unknown"
        message = (
            f"7TV emote info for `{emote_id}`\n"
            f"Name: `{_sanitize_name(name) or name or 'unknown'}`\n"
            f"Animated: `{animated if animated is not None else 'unknown'}`\n"
            f"Best asset: `{asset_summary}`"
        )
        await ctx.send(message)

    @commands.guild_only()
    @commands.bot_has_permissions(manage_emojis=True)
    @commands.command(name="7tv", aliases=["seventv"])  # usage: [p]7tv <link> [name]
    async def seven_tv(self, ctx: commands.Context, link: str, *, name: Optional[str] = None):
        """Upload an emoji from a 7TV emote link.

        Example: [p]7tv https://7tv.app/emotes/<id> optional_name
        """
        assert ctx.guild
        if not await self._can_upload_emojis(ctx):
            return await ctx.send(UPLOAD_NOT_ALLOWED)

        emote_id = _extract_7tv_id(link)
        if not emote_id:
            return await ctx.send(INVALID_LINK)

        async with ctx.typing():
            session = await self._get_session()
            v3_data = await _fetch_7tv_v3_emote(session, emote_id)
            api_name, api_animated = await _fetch_7tv_meta(session, emote_id, v3_data=v3_data)
            result = await _fetch_7tv_bytes(session, emote_id, v3_data=v3_data)
            data, is_animated, ext, error_message = await self._normalize_asset(result, api_animated)
            if error_message:
                return await ctx.send(error_message)

            animated_flag = bool(is_animated or (ext == "gif"))
            if _available_emoji_slots(ctx.guild, animated_flag) <= 0:
                return await ctx.send(EMOJI_SLOTS)

            final_name = self._resolve_emoji_name(ctx.guild, name, api_name)

            try:
                added = await ctx.guild.create_custom_emoji(name=final_name, image=data)
            except discord.DiscordException as error:
                # Common issues: invalid image/format, size, rate limiting
                return await ctx.send(f"{EMOJI_FAIL}, {type(error).__name__}: {error}")

        try:
            await ctx.message.add_reaction(added)
        except discord.DiscordException:
            pass

        return await ctx.send(f"Uploaded: {str(added)} `{added.name}`")

    @commands.guild_only()
    @commands.command(name="7tvinfo")
    async def seven_tv_info(self, ctx: commands.Context, link: str):
        """Show 7TV emote details without uploading it."""
        emote_id = _extract_7tv_id(link)
        if not emote_id:
            return await ctx.send(INVALID_LINK)

        async with ctx.typing():
            await self._send_info(ctx, emote_id)

    async def _webp_to_gif_under_limit(self, webp_bytes: bytes, limit: int = 256 * 1024) -> Optional[bytes]:
        """Convert animated WEBP bytes to a GIF under the size limit if possible."""
        try:
            from PIL import Image
        except Exception:
            return None

        try:
            img = Image.open(io.BytesIO(webp_bytes))
        except Exception:
            return None

        # If not actually animated, bail
        if not getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) <= 1:
            return None

        # Collect frames and durations
        frames: List[Image.Image] = []  # type: ignore[name-defined]
        durations: List[int] = []
        try:
            n = getattr(img, "n_frames", 1)
        except Exception:
            n = 1
        for i in range(n):
            try:
                img.seek(i)
            except EOFError:
                break
            frame = img.convert("RGBA")
            frames.append(frame)
            durations.append(int(img.info.get("duration", 80)))

        if not frames:
            return None

        # Try combinations of downscale and frame skipping
        max_sides = [128, 112, 96, 80, 64]
        steps = [1, 2, 3]

        for step in steps:
            # sample frames by 'step' and sum durations
            sampled_frames = frames[::step]
            sampled_durations = [sum(durations[i:i+step]) for i in range(0, len(durations), step)]
            for max_side in max_sides:
                resized = []
                for fr in sampled_frames:
                    w, h = fr.size
                    scale = min(1.0, float(max_side) / float(max(w, h)))
                    if scale < 1.0:
                        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                        rf = fr.resize(new_size, resample=Image.LANCZOS)
                    else:
                        rf = fr
                    # Palette quantize to reduce size
                    rq = rf.convert("P", palette=Image.ADAPTIVE, colors=128)
                    resized.append(rq)

                buf = io.BytesIO()
                try:
                    resized[0].save(
                        buf,
                        format="GIF",
                        save_all=True,
                        append_images=resized[1:],
                        duration=sampled_durations[: len(resized)],
                        loop=0,
                        optimize=True,
                        disposal=2,
                    )
                except Exception:
                    continue

                data = buf.getvalue()
                if len(data) <= limit:
                    return data

        return None

    async def _webp_to_png_under_limit(self, webp_bytes: bytes, limit: int = 256 * 1024) -> Optional[bytes]:
        """Convert static WEBP bytes to a PNG under the size limit by downscaling if needed."""
        try:
            from PIL import Image
        except Exception:
            return None

        try:
            img = Image.open(io.BytesIO(webp_bytes))
        except Exception:
            return None

        # Animated check; if animated, don't handle here
        if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
            return None

        max_sides = [512, 256, 192, 160, 128, 96, 80, 64]
        for max_side in max_sides:
            w, h = img.size
            scale = min(1.0, float(max_side) / float(max(w, h)))
            if scale < 1.0:
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                im2 = img.resize(new_size, resample=Image.LANCZOS)
            else:
                im2 = img

            buf = io.BytesIO()
            try:
                im2.save(buf, format="PNG", optimize=True)
            except Exception:
                continue
            data = buf.getvalue()
            if len(data) <= limit:
                return data

        return None
