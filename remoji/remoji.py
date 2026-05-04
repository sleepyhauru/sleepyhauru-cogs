from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union
from urllib.parse import urlparse

import aiohttp
import discord
from redbot.core import Config, app_commands, commands


DISCORD_EMOJI_SIZE_LIMIT = 256 * 1024
BATCH_PROGRESS_INTERVAL = 5
BATCH_UPLOAD_DELAY = 0.25
ALLOWED_IMAGE_HOSTS = {"cdn.discordapp.com", "media.discordapp.net", "i.imgur.com"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
EMOJI_RE = re.compile(r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{15,25})>")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
HYBRID_GROUP = getattr(commands, "hybrid_group", commands.group)

INVALID_NAME = (
    "That isn't a valid emoji name. Names must be 2-32 characters and may only "
    "contain letters, numbers, and underscores."
)
INVALID_URL = "That doesn't look like a valid image URL."
INVALID_DOMAIN = "That image isn't hosted on an allowed website. Try Discord CDN or Imgur."
INVALID_TYPE = "That URL did not return a supported image type."
IMAGE_TOO_LARGE = "That image is too large for a Discord emoji. The limit is 256 KiB."
DOWNLOAD_FAILED = "I couldn't download that image."
NO_EMOJIS = "I couldn't find any custom Discord emojis in that input."
EMOJI_SLOTS = "This server doesn't have any more space for that emoji type."
UPLOAD_FAILED = "Failed to upload emoji"
UPLOAD_NOT_ALLOWED = "You need Manage Emojis and Stickers or be on the Remoji upload allowlist."
SETTINGS_HINT = "Use `remojiset allowuser`, `remojiset denyuser`, or `remojiset showallowlist`."
EMOJI_SOURCE_HINT = "Provide custom emoji text or reply to a message containing custom emojis."


@dataclass(frozen=True)
class EmojiAsset:
    id: int
    name: str
    animated: bool = False

    @property
    def url(self) -> str:
        extension = "gif" if self.animated else "png"
        return f"https://cdn.discordapp.com/emojis/{self.id}.{extension}"


@dataclass(frozen=True)
class ImageDownload:
    data: Optional[bytes]
    error: Optional[str] = None
    content_type: Optional[str] = None


def extract_emojis(value: str) -> list[EmojiAsset]:
    emojis = []
    for match in EMOJI_RE.finditer(value):
        groups = match.groupdict()
        emojis.append(
            EmojiAsset(
                id=int(groups["id"]),
                name=groups["name"],
                animated=bool(groups["animated"]),
            )
        )
    return emojis


def sanitize_emoji_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = "".join(re.findall(r"\w+", value))[:32]
    if not NAME_RE.fullmatch(cleaned):
        return None
    return cleaned


def available_emoji_slots(guild: discord.Guild, animated: bool) -> int:
    current = len([emoji for emoji in guild.emojis if emoji.animated == animated])
    return guild.emoji_limit - current


def unique_emojis(emojis: list[EmojiAsset]) -> list[EmojiAsset]:
    return list(dict.fromkeys(emojis))


def resolve_emoji_name(
    guild: discord.Guild,
    requested_name: str,
    reserved_names: Optional[set[str]] = None,
) -> Optional[str]:
    base_name = sanitize_emoji_name(requested_name)
    if base_name is None:
        return None

    existing_names = {emoji.name.lower() for emoji in guild.emojis}
    if reserved_names:
        existing_names |= {name.lower() for name in reserved_names}
    if base_name.lower() not in existing_names:
        return base_name

    for index in range(2, 100):
        suffix = f"_{index}"
        candidate = f"{base_name[: 32 - len(suffix)]}{suffix}"
        if candidate.lower() not in existing_names:
            return candidate
    return f"{base_name[:29]}_99"


def image_download_is_animated(url: str, download: ImageDownload) -> bool:
    parsed = urlparse(url)
    query = parsed.query.lower()
    return (
        download.content_type == "image/gif"
        or parsed.path.lower().endswith(".gif")
        or "animated=true" in query
    )


class Remoji(commands.Cog):
    """Upload and copy custom Discord emojis."""

    __author__ = ["sleepyhauru"]
    __version__ = "1.1.0"

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=183040005, force_registration=True)
        self.config.register_guild(upload_allowlist=[])
        self.session: Optional[aiohttp.ClientSession] = None
        self.asset_context_menu = app_commands.ContextMenu(
            name="Remoji Asset URLs",
            callback=self.remoji_url_app_command,
        )
        self.copy_context_menu = app_commands.ContextMenu(
            name="Remoji Copy Emotes",
            callback=self.remoji_copy_app_command,
        )
        if hasattr(self.bot, "tree"):
            self.bot.tree.add_command(self.asset_context_menu)
            self.bot.tree.add_command(self.copy_context_menu)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    def cog_unload(self):
        if self.session is not None and not getattr(self.session, "closed", False):
            self.bot.loop.create_task(self.session.close())
        if hasattr(self.bot, "tree"):
            self.bot.tree.remove_command(self.asset_context_menu.name, type=self.asset_context_menu.type)
            self.bot.tree.remove_command(self.copy_context_menu.name, type=self.copy_context_menu.type)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or getattr(self.session, "closed", False):
            timeout = aiohttp.ClientTimeout(total=6)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def _download_image_url(self, url: str) -> ImageDownload:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ImageDownload(None, INVALID_URL)
        if parsed.hostname not in ALLOWED_IMAGE_HOSTS:
            return ImageDownload(None, INVALID_DOMAIN)

        session = await self._get_session()
        try:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status >= 400:
                    return ImageDownload(None, f"{DOWNLOAD_FAILED} HTTP {resp.status}.")

                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type and content_type not in ALLOWED_IMAGE_TYPES:
                    return ImageDownload(None, INVALID_TYPE)

                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > DISCORD_EMOJI_SIZE_LIMIT:
                    return ImageDownload(None, IMAGE_TOO_LARGE)

                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            return ImageDownload(None, f"{DOWNLOAD_FAILED} {type(error).__name__}: {error}")

        if len(data) > DISCORD_EMOJI_SIZE_LIMIT:
            return ImageDownload(None, IMAGE_TOO_LARGE)
        return ImageDownload(data, content_type=content_type or None)

    async def _download_emoji(self, emoji: EmojiAsset) -> ImageDownload:
        result = await self._download_image_url(emoji.url)
        if result.data or not emoji.animated:
            return result

        webp_url = f"https://cdn.discordapp.com/emojis/{emoji.id}.webp?animated=true"
        return await self._download_image_url(webp_url)

    async def _get_referenced_message(self, ctx: commands.Context) -> Optional[discord.Message]:
        message = getattr(ctx, "message", None)
        reference = getattr(message, "reference", None)
        if reference is None:
            return None

        resolved = getattr(reference, "resolved", None)
        if resolved is not None and getattr(resolved, "content", None) is not None:
            return resolved

        message_id = getattr(reference, "message_id", None)
        if not message_id:
            return None

        channel = getattr(ctx, "channel", None) or getattr(message, "channel", None)
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None

        try:
            return await fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _resolve_source_text(self, ctx: commands.Context, provided: Optional[str]) -> Optional[str]:
        if provided and provided.strip():
            return provided

        message = await self._get_referenced_message(ctx)
        if message is None:
            return None
        return getattr(message, "content", None)

    async def _create_emoji(
        self,
        guild: discord.Guild,
        image: bytes,
        name: str,
        *,
        reason: Optional[str] = None,
    ) -> discord.Emoji:
        try:
            return await guild.create_custom_emoji(name=name, image=image, reason=reason)
        except TypeError:
            return await guild.create_custom_emoji(name=name, image=image)

    async def _is_upload_allowed(self, guild: discord.Guild, user) -> bool:
        permissions = getattr(user, "guild_permissions", None)
        if getattr(permissions, "manage_emojis", False):
            return True
        if getattr(permissions, "manage_emojis_and_stickers", False):
            return True

        allowlist = await self.config.guild(guild).upload_allowlist()
        return getattr(user, "id", None) in allowlist

    async def _ensure_upload_allowed(
        self,
        destination,
        guild: discord.Guild,
        user,
        *,
        ephemeral: bool = False,
    ) -> bool:
        if await self._is_upload_allowed(guild, user):
            return True

        if ephemeral and hasattr(destination, "send_message"):
            await destination.send_message(UPLOAD_NOT_ALLOWED, ephemeral=True)
        else:
            await destination.send(UPLOAD_NOT_ALLOWED)
        return False

    async def _upload_asset(
        self,
        guild: discord.Guild,
        user,
        image: bytes,
        name: str,
        *,
        animated: bool,
        check_slots: bool = True,
    ) -> tuple[Optional[discord.Emoji], Optional[str]]:
        if check_slots and available_emoji_slots(guild, animated) <= 0:
            return None, EMOJI_SLOTS

        try:
            reason = f"Uploaded by {user} ({getattr(user, 'id', 'unknown')}) using remoji"
            return await self._create_emoji(guild, image, name, reason=reason), None
        except discord.DiscordException as error:
            return None, f"{UPLOAD_FAILED} `{name}`, {type(error).__name__}: {error}"

    async def _copy_one_emoji(
        self,
        guild: discord.Guild,
        user,
        source: EmojiAsset,
        requested_name: Optional[str] = None,
        *,
        remaining: Optional[dict[bool, int]] = None,
        reserved_names: Optional[set[str]] = None,
    ) -> tuple[Optional[discord.Emoji], Optional[str], Optional[str]]:
        final_name = resolve_emoji_name(guild, requested_name or source.name, reserved_names)
        if final_name is None:
            return None, INVALID_NAME, None

        if remaining is not None:
            if remaining[source.animated] <= 0:
                return None, EMOJI_SLOTS, final_name
            check_slots = False
        else:
            check_slots = True

        result = await self._download_emoji(source)
        if not result.data:
            return None, result.error or DOWNLOAD_FAILED, final_name

        created, error = await self._upload_asset(
            guild,
            user,
            result.data,
            final_name,
            animated=source.animated,
            check_slots=check_slots,
        )
        if created is not None and remaining is not None:
            remaining[source.animated] -= 1
            if reserved_names is not None:
                reserved_names.add(final_name.lower())
        return created, error, final_name

    async def _copy_many_emojis(
        self,
        guild: discord.Guild,
        user,
        sources: list[EmojiAsset],
        *,
        progress: Optional[Callable[[int, int, int, int], Awaitable[None]]] = None,
    ) -> tuple[list[discord.Emoji], list[str]]:
        remaining = {
            False: available_emoji_slots(guild, False),
            True: available_emoji_slots(guild, True),
        }
        reserved_names = {emoji.name.lower() for emoji in guild.emojis}
        uploaded = []
        failed = []
        total = len(sources)

        for index, source in enumerate(sources, start=1):
            created, error, _final_name = await self._copy_one_emoji(
                guild,
                user,
                source,
                remaining=remaining,
                reserved_names=reserved_names,
            )
            if created is None:
                if error == EMOJI_SLOTS:
                    reason = "no slots"
                elif error in {INVALID_NAME, INVALID_TYPE, IMAGE_TOO_LARGE} or (error or "").startswith(DOWNLOAD_FAILED):
                    reason = "download failed"
                else:
                    reason = "upload failed"
                failed.append(f"{source.name}: {reason}")
            else:
                uploaded.append(created)
                if index < total:
                    await asyncio.sleep(BATCH_UPLOAD_DELAY)

            if progress is not None and index % BATCH_PROGRESS_INTERVAL == 0 and index < total:
                await progress(index, total, len(uploaded), len(failed))

        return uploaded, failed

    @HYBRID_GROUP(name="remoji", aliases=["emotes"], invoke_without_command=True)
    @commands.guild_only()
    async def remoji(self, ctx: commands.Context):
        """Upload and copy custom Discord emojis."""
        assert ctx.guild is not None
        static_slots = available_emoji_slots(ctx.guild, False)
        animated_slots = available_emoji_slots(ctx.guild, True)
        prefix = getattr(ctx, "clean_prefix", "[p]")
        await ctx.send(
            "Remoji\n"
            f"Static slots remaining: `{static_slots}`\n"
            f"Animated slots remaining: `{animated_slots}`\n"
            f"Use `{prefix}remoji upload <url> <name>`, `{prefix}remoji copy <emoji> [name]`, "
            f"or reply to a message with `{prefix}remoji copy`."
        )

    @remoji.command(name="upload")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_emojis=True)
    async def remoji_upload(self, ctx: commands.Context, url: str, name: str):
        """Upload an emoji from an allowed image URL."""
        assert ctx.guild is not None
        if not await self._ensure_upload_allowed(ctx, ctx.guild, ctx.author):
            return
        final_name = resolve_emoji_name(ctx.guild, name)
        if final_name is None:
            await ctx.send(INVALID_NAME)
            return

        async with ctx.typing():
            result = await self._download_image_url(url)
            if not result.data:
                await ctx.send(result.error or DOWNLOAD_FAILED)
                return

            animated = image_download_is_animated(url, result)
            emoji, error = await self._upload_asset(
                ctx.guild,
                ctx.author,
                result.data,
                final_name,
                animated=animated,
            )
            if error:
                await ctx.send(error)
                return

        if emoji is not None:
            await ctx.send(f"Uploaded `:{final_name}:` to this server: {emoji}")

    @remoji.command(name="copy")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_emojis=True)
    async def remoji_copy(self, ctx: commands.Context, emoji: Optional[str] = None, *, name: Optional[str] = None):
        """Copy one custom Discord emoji into this server."""
        assert ctx.guild is not None
        if not await self._ensure_upload_allowed(ctx, ctx.guild, ctx.author):
            return
        source_text = await self._resolve_source_text(ctx, emoji)
        if source_text is None:
            await ctx.send(EMOJI_SOURCE_HINT)
            return

        found = extract_emojis(source_text)
        if not found:
            await ctx.send(NO_EMOJIS)
            return

        source = found[0]
        async with ctx.typing():
            created, error, final_name = await self._copy_one_emoji(ctx.guild, ctx.author, source, name)

        if created is None:
            await ctx.send(error or UPLOAD_FAILED)
            return

        try:
            await ctx.message.add_reaction(created)
        except discord.DiscordException:
            pass
        await ctx.send(f"Copied `:{final_name}:` to this server: {created}")

    @remoji.command(name="copymany", aliases=["copyall", "multiple"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_emojis=True)
    async def remoji_copy_many(self, ctx: commands.Context, *, emojis: Optional[str] = None):
        """Copy multiple custom Discord emojis into this server."""
        assert ctx.guild is not None
        if not await self._ensure_upload_allowed(ctx, ctx.guild, ctx.author):
            return
        source_text = await self._resolve_source_text(ctx, emojis)
        if source_text is None:
            await ctx.send(EMOJI_SOURCE_HINT)
            return

        found = unique_emojis(extract_emojis(source_text))
        if not found:
            await ctx.send(NO_EMOJIS)
            return

        async def progress(processed: int, total: int, uploaded_count: int, failed_count: int):
            await ctx.send(
                f"Copying emojis... {processed}/{total} processed, "
                f"{uploaded_count} uploaded, {failed_count} failed."
            )

        async with ctx.typing():
            uploaded, failed = await self._copy_many_emojis(ctx.guild, ctx.author, found, progress=progress)

        lines = [f"Uploaded {len(uploaded)}/{len(found)} emojis."]
        if uploaded:
            lines.append(" ".join(str(emoji) for emoji in uploaded))
        if failed:
            lines.append("Failed: " + ", ".join(failed[:8]))
            if len(failed) > 8:
                lines.append(f"...and {len(failed) - 8} more.")
        await ctx.send("\n".join(lines))

    @remoji.command(name="url", aliases=["asset"])
    async def remoji_url(self, ctx: commands.Context, *, emoji: Optional[str] = None):
        """Show CDN URLs for custom Discord emojis."""
        source_text = await self._resolve_source_text(ctx, emoji)
        if source_text is None:
            await ctx.send(EMOJI_SOURCE_HINT)
            return

        found = unique_emojis(extract_emojis(source_text))
        if not found:
            await ctx.send(NO_EMOJIS)
            return
        await ctx.send("\n".join(item.url for item in found))

    @remoji.command(name="info")
    @commands.guild_only()
    async def remoji_info(self, ctx: commands.Context):
        """Show Remoji slot availability and source information."""
        assert ctx.guild is not None
        static_slots = available_emoji_slots(ctx.guild, False)
        animated_slots = available_emoji_slots(ctx.guild, True)
        await ctx.send(
            "Remoji manages custom emoji uploads and copies.\n"
            f"Static slots remaining: `{static_slots}`\n"
            f"Animated slots remaining: `{animated_slots}`"
        )

    @commands.group(name="remojiset", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def remojiset(self, ctx: commands.Context):
        """Manage Remoji settings for this server."""
        await ctx.send(SETTINGS_HINT)

    @remojiset.command(name="allowuser")
    async def remojiset_allowuser(self, ctx: commands.Context, user: Union[discord.Member, discord.User]):
        """Allow one user to use Remoji uploads without Manage Emojis."""
        assert ctx.guild is not None
        allowlist = await self.config.guild(ctx.guild).upload_allowlist()
        if user.id in allowlist:
            await ctx.send(f"{user.mention} is already on the Remoji upload allowlist.")
            return

        allowlist.append(user.id)
        await self.config.guild(ctx.guild).upload_allowlist.set(allowlist)
        await ctx.send(f"Added {user.mention} to the Remoji upload allowlist.")

    @remojiset.command(name="denyuser")
    async def remojiset_denyuser(self, ctx: commands.Context, user: Union[discord.Member, discord.User]):
        """Remove one user from the Remoji upload allowlist."""
        assert ctx.guild is not None
        allowlist = await self.config.guild(ctx.guild).upload_allowlist()
        if user.id not in allowlist:
            await ctx.send(f"{user.mention} is not on the Remoji upload allowlist.")
            return

        allowlist.remove(user.id)
        await self.config.guild(ctx.guild).upload_allowlist.set(allowlist)
        await ctx.send(f"Removed {user.mention} from the Remoji upload allowlist.")

    @remojiset.command(name="showallowlist")
    async def remojiset_showallowlist(self, ctx: commands.Context):
        """Show users allowed to bypass the Remoji upload permission check."""
        assert ctx.guild is not None
        allowlist = await self.config.guild(ctx.guild).upload_allowlist()
        if not allowlist:
            await ctx.send("The Remoji upload allowlist is empty.")
            return

        mentions = ", ".join(f"<@{user_id}>" for user_id in allowlist)
        await ctx.send(f"Remoji upload allowlist: {mentions}")

    async def remoji_url_app_command(self, ctx: discord.Interaction, message: discord.Message):
        found = unique_emojis(extract_emojis(message.content))
        if not found:
            return await ctx.response.send_message(NO_EMOJIS, ephemeral=True)
        await ctx.response.send_message("\n".join(item.url for item in found), ephemeral=True)

    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_emojis=True)
    async def remoji_copy_app_command(self, ctx: discord.Interaction, message: discord.Message):
        assert ctx.guild is not None
        if not await self._ensure_upload_allowed(ctx.response, ctx.guild, ctx.user, ephemeral=True):
            return

        found = unique_emojis(extract_emojis(message.content))
        if not found:
            return await ctx.response.send_message(NO_EMOJIS, ephemeral=True)

        await ctx.response.defer(thinking=True)
        uploaded, failed = await self._copy_many_emojis(ctx.guild, ctx.user, found)
        lines = [f"Uploaded {len(uploaded)}/{len(found)} emojis."]
        if uploaded:
            lines.append(" ".join(str(emoji) for emoji in uploaded))
        if failed:
            lines.append("Failed: " + ", ".join(failed[:8]))
            if len(failed) > 8:
                lines.append(f"...and {len(failed) - 8} more.")
        await ctx.edit_original_response(content="\n".join(lines))
