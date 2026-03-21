import aiohttp
import discord
import io
import re
from typing import Optional, Tuple, List, Dict, Any

from redbot.core import commands


EMOJI_SLOTS = "This server doesn't have any more space for emojis!"
EMOJI_FAIL = "Failed to upload emoji"
INVALID_LINK = "Please provide a valid 7TV emote link."
FETCH_FAIL = "Couldn't fetch the 7TV emote asset."


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


async def _fetch_7tv_meta(session: aiohttp.ClientSession, emote_id: str) -> Tuple[Optional[str], Optional[bool]]:
    """Return (name, animated) if available via 7TV API; tolerate failures.

    Tries v3 API first, then v2 as fallback. Returns (None, None) on failure.
    """
    # v3
    try:
        async with session.get(f"https://7tv.io/v3/emotes/{emote_id}", headers=HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                name = data.get("name")
                animated = data.get("animated")
                return name, animated
    except aiohttp.ClientError:
        pass

    # v2 fallback
    try:
        async with session.get(f"https://api.7tv.app/v2/emotes/{emote_id}", headers=HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                name = data.get("name")
                animated = bool(data.get("animated", False))
                return name, animated
    except aiohttp.ClientError:
        pass

    return None, None


async def _fetch_7tv_asset_via_meta(session: aiohttp.ClientSession, emote_id: str) -> Tuple[Optional[bytes], Optional[bool], Optional[str]]:
    """Use 7TV v3 metadata to choose a concrete asset URL and download it.

    Returns (bytes, is_animated, ext) or (None, None, None) if unavailable.
    """
    try:
        async with session.get(f"https://7tv.io/v3/emotes/{emote_id}", headers=HEADERS) as resp:
            if resp.status != 200:
                return None, None, None
            data: Dict[str, Any] = await resp.json()
    except aiohttp.ClientError:
        return None, None, None

    host = data.get("host") or {}
    base_url = host.get("url") or ""
    files = host.get("files") or []
    if not isinstance(files, list) or not base_url:
        return None, None, None

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

    for f in candidates:
        fmt = str(f.get("format", "")).upper()
        name = str(f.get("name", ""))
        size = int(f.get("size", 0))
        if fmt not in ("GIF", "PNG", "WEBP"):
            continue
        if size <= 0 or size > 256 * 1024:
            continue
        url = f"{base_url}/{name}"
        try:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    continue
                data_bytes = await resp.read()
        except aiohttp.ClientError:
            continue
        if len(data_bytes) > 256 * 1024:
            continue
        return data_bytes, (fmt == "GIF"), fmt.lower()

    return None, None, None


async def _fetch_7tv_bytes(session: aiohttp.ClientSession, emote_id: str) -> Tuple[Optional[bytes], Optional[bool], Optional[str]]:
    """Try to download a suitable emote asset from the 7TV CDN within Discord's limits.

    Returns (bytes, is_animated, ext) on success; otherwise (None, None, None).
    Tries meta-informed URLs first, then generic path fallback.
    Preference order: GIF 4x->1x, then PNG 4x->1x, then WEBP 4x->1x (as last resort).
    """
    # Use meta-informed selection first
    data, is_anim, ext = await _fetch_7tv_asset_via_meta(session, emote_id)
    if data:
        return data, is_anim, ext

    exts = ["gif", "png", "webp"]
    sizes = ["4x", "3x", "2x", "1x"]
    for ext in exts:
        for size in sizes:
            url = f"https://cdn.7tv.app/emote/{emote_id}/{size}.{ext}"
            try:
                async with session.get(url, headers=HEADERS) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
                    # Discord custom emoji limit is 256 KiB
                    if len(data) > 256 * 1024:
                        continue
                    return data, (ext == "gif"), ext
            except aiohttp.ClientError:
                continue
    return None, None, None


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

    @commands.guild_only()
    @commands.bot_has_permissions(manage_emojis=True)
    @commands.command(name="7tv", aliases=["seventv"])  # usage: [p]7tv <link> [name]
    async def seven_tv(self, ctx: commands.Context, link: str, *, name: Optional[str] = None):
        """Upload an emoji from a 7TV emote link.

        Example: [p]7tv https://7tv.app/emotes/<id> optional_name
        """
        assert ctx.guild

        emote_id = _extract_7tv_id(link)
        if not emote_id:
            return await ctx.send(INVALID_LINK)

        await ctx.typing()

        async with aiohttp.ClientSession() as session:
            # Determine default name and animation hint from API if possible
            api_name, api_animated = await _fetch_7tv_meta(session, emote_id)

            # Choose asset from CDN under 256 KiB
            data, is_animated, ext = await _fetch_7tv_bytes(session, emote_id)
            if not data:
                return await ctx.send(FETCH_FAIL)

            # If we only found WEBP, try converting to a suitable format
            if ext == "webp":
                # Prefer GIF for animated, PNG for static
                if is_animated:
                    converted = await self._webp_to_gif_under_limit(data)
                    if converted:
                        data = converted
                        ext = "gif"
                        is_animated = True
                else:
                    converted = await self._webp_to_png_under_limit(data)
                    if converted:
                        data = converted
                        ext = "png"
                        is_animated = False

            # Check slot availability by type (animated if gif)
            animated_flag = bool(is_animated or (ext == "gif"))
            if _available_emoji_slots(ctx.guild, animated_flag) <= 0:
                return await ctx.send(EMOJI_SLOTS)

            # Finalize name
            final_name = _sanitize_name(name) or _sanitize_name(api_name) or "seventv_emoji"

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
