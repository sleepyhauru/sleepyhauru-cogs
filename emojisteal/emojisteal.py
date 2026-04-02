import io
import re
import zipfile
import aiohttp
import discord
from typing import Optional, Union, List, Sequence
from itertools import zip_longest
from redbot.core import Config, commands, app_commands

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
STICKER_KB = 512
STICKER_DIM = 320
STICKER_TIME = 5
MAX_STICKER_ARCHIVE_RATIO = 20
MAX_STICKER_ARCHIVE_FILES = 25

MISSING_EMOJIS = "Can't find emojis or stickers in that message."
MISSING_REFERENCE = "Reply to a message with this command to steal an emoji."
MESSAGE_FAIL = "I couldn't grab that message, sorry."
UPLOADED_BY = "Uploaded by"
STICKER_DESC = "Stolen sticker"
STICKER_EMOJI = "😶"
STICKER_FAIL = "❌ Failed to upload sticker"
STICKER_SUCCESS = "✅ Uploaded sticker"
STICKER_SLOTS = "⚠ This server doesn't have any more space for stickers!"
EMOJI_FAIL = "❌ Failed to upload"
EMOJI_SLOTS = "⚠ This server doesn't have any more space for emojis!"
INVALID_EMOJI = "Invalid emoji or emoji ID."
UPLOAD_NOT_ALLOWED = "You need Manage Emojis and Stickers or be on the steal upload allowlist."
STICKER_TOO_BIG = f"Stickers may only be up to {STICKER_KB} KB and {STICKER_DIM}x{STICKER_DIM} pixels and last up to {STICKER_TIME} seconds."
STICKER_ATTACHMENT = """\
>>> For a non-moving sticker, simply use this command and attach a PNG image.
For a moving sticker, Discord limitations make it very annoying. Follow these steps:
1. Scale down and optimize your video/gif in <https://ezgif.com>
2. Convert it to APNG in that same website.
3. Download it and put it inside a zip file.
4. Use this command and attach that zip file.
\n**Important:** """ + STICKER_TOO_BIG


async def fetch_emoji_image(session: aiohttp.ClientSession, emoji: discord.PartialEmoji) -> bytes:
    """
    Fetch emoji bytes from Discord CDN.

    Some animated emojis now 415 on the legacy .gif path. If that happens,
    retry using animated WebP.
    """
    url = str(emoji.url)

    async with session.get(url) as resp:
        # Discord CDN may 415 on .gif for animated emojis; retry as animated webp
        if resp.status == 415 and getattr(emoji, "animated", False):
            webp_url = url.replace(".gif", ".webp")

            # ensure animated=true is present
            if "animated=" not in webp_url:
                sep = "&" if "?" in webp_url else "?"
                webp_url = f"{webp_url}{sep}animated=true"

            async with session.get(webp_url) as resp2:
                resp2.raise_for_status()
                return await resp2.read()

        resp.raise_for_status()
        return await resp.read()


class EmojiSteal(commands.Cog):
    """Steals emojis and stickers sent by other people and optionally uploads them to your own server. Supports context menu commands."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=183040004, force_registration=True)
        self.config.register_guild(upload_allowlist=[])
        self.steal_context_menu = app_commands.ContextMenu(name='Steal Emotes', callback=self.steal_app_command)
        self.steal_upload_context_menu = app_commands.ContextMenu(name='Steal+Upload Emotes', callback=self.steal_upload_app_command)
        self.bot.tree.add_command(self.steal_context_menu)
        self.bot.tree.add_command(self.steal_upload_context_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.steal_context_menu.name, type=self.steal_context_menu.type)
        self.bot.tree.remove_command(self.steal_upload_context_menu.name, type=self.steal_upload_context_menu.type)

    @staticmethod
    def get_emojis(content: str) -> Optional[List[discord.PartialEmoji]]:
        results = re.findall(r"(<(a?):(\w+):(\d{10,20})>)", content)
        return [discord.PartialEmoji.from_str(result[0]) for result in results]
    
    @staticmethod
    def available_emoji_slots(guild: discord.Guild, animated: bool) -> int:
        current_emojis = len([em for em in guild.emojis if em.animated == animated])
        return guild.emoji_limit - current_emojis

    @staticmethod
    def _sanitize_names(names: Sequence[str]) -> List[Optional[str]]:
        cleaned = [''.join(re.findall(r"\w+", name)) for name in names]
        return [name if len(name) >= 2 else None for name in cleaned]

    @staticmethod
    def _join_names(names: Sequence[str]) -> str:
        return ", ".join(names)

    @staticmethod
    def _validate_sticker_archive_entry(info: zipfile.ZipInfo) -> bool:
        filename = info.filename
        path_parts = filename.replace("\\", "/").split("/")
        if info.is_dir():
            return False
        if filename.startswith(("/", "\\")):
            return False
        if any(part == ".." for part in path_parts):
            return False
        return True

    def _extract_sticker_png_from_zip(self, fp: io.BytesIO) -> Optional[io.BytesIO]:
        fp.seek(0)
        with zipfile.ZipFile(fp) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_STICKER_ARCHIVE_FILES:
                raise ValueError(STICKER_TOO_BIG)

            png_infos = []
            for info in infos:
                if not self._validate_sticker_archive_entry(info):
                    raise ValueError(STICKER_ATTACHMENT)
                if info.filename.lower().endswith(".png"):
                    png_infos.append(info)

            if len(png_infos) != 1:
                raise ValueError(STICKER_ATTACHMENT)

            info = png_infos[0]
            if info.file_size > STICKER_KB * 1024:
                raise ValueError(STICKER_TOO_BIG)
            if info.compress_size <= 0:
                raise ValueError(STICKER_ATTACHMENT)
            if info.file_size > info.compress_size * MAX_STICKER_ARCHIVE_RATIO:
                raise ValueError(STICKER_TOO_BIG)

            png_fp = io.BytesIO(archive.read(info))
            png_fp.seek(0)
            return png_fp

    async def _is_upload_allowed(self, guild: discord.Guild, user) -> bool:
        permissions = getattr(user, "guild_permissions", None)
        if getattr(permissions, "manage_emojis", False):
            return True

        allowlist = await self.config.guild(guild).upload_allowlist()
        return getattr(user, "id", None) in allowlist

    async def _ensure_upload_allowed(self, destination, guild: discord.Guild, user, *, ephemeral: bool = False) -> bool:
        if await self._is_upload_allowed(guild, user):
            return True

        if ephemeral:
            await destination.send_message(UPLOAD_NOT_ALLOWED, ephemeral=True)
        else:
            await destination.send(UPLOAD_NOT_ALLOWED)
        return False

    async def _upload_emojis(
        self,
        guild: discord.Guild,
        emojis: List[discord.PartialEmoji],
        custom_names: Optional[Sequence[Optional[str]]] = None,
    ) -> tuple[List[discord.Emoji], Optional[str]]:
        added_emojis = []
        custom_names = list(custom_names or [])

        async with aiohttp.ClientSession() as session:
            for emoji, name in zip_longest(emojis, custom_names):
                if not emoji:
                    break
                if not self.available_emoji_slots(guild, emoji.animated):
                    return added_emojis, EMOJI_SLOTS

                try:
                    image = await fetch_emoji_image(session, emoji)
                    added = await guild.create_custom_emoji(name=name or emoji.name, image=image)
                except (aiohttp.ClientError, discord.DiscordException) as error:
                    return added_emojis, f"{EMOJI_FAIL} {emoji.name}, {type(error).__name__}: {error}"

                added_emojis.append(added)

        return added_emojis, None

    async def _upload_stickers(
        self,
        guild: discord.Guild,
        stickers: List[discord.StickerItem],
        custom_names: Optional[Sequence[Optional[str]]] = None,
    ) -> tuple[List[str], Optional[str]]:
        uploaded = []
        custom_names = list(custom_names or [])

        for sticker, custom_name in zip_longest(stickers, custom_names):
            if not sticker:
                break
            if len(guild.stickers) >= guild.sticker_limit:
                return uploaded, STICKER_SLOTS

            fp = io.BytesIO()
            try:
                await sticker.save(fp)
                fp.seek(0)
                await guild.create_sticker(
                    name=custom_name or sticker.name,
                    description=STICKER_DESC,
                    emoji=STICKER_EMOJI,
                    file=discord.File(fp),
                )
            except discord.DiscordException as error:
                return uploaded, f"{STICKER_FAIL}, {type(error).__name__}: {error}"

            uploaded.append(custom_name or sticker.name)

        return uploaded, None

    async def steal_ctx(self, ctx: commands.Context) -> Optional[Union[List[discord.PartialEmoji], List[discord.StickerItem]]]:
        reference = ctx.message.reference
        if not reference or not reference.message_id:
            await ctx.send(MISSING_REFERENCE)
            return None
        try:
            message = await ctx.channel.fetch_message(reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await ctx.send(MESSAGE_FAIL)
            return None
        if message.stickers:
            return message.stickers
        if not (emojis := self.get_emojis(message.content)):
            await ctx.send(MISSING_EMOJIS)
            return None
        return emojis

    async def _send_steal_info(
        self,
        destination,
        guild: Optional[discord.Guild],
        items: Union[List[discord.PartialEmoji], List[discord.StickerItem]],
    ):
        if items and isinstance(items[0], discord.StickerItem):
            stickers = items
            lines = [f"Found {len(stickers)} sticker{'s' if len(stickers) != 1 else ''}."]
            lines.extend(f"- `{sticker.name}`" for sticker in stickers)
            if guild is not None:
                remaining = guild.sticker_limit - len(guild.stickers)
                lines.append(f"Sticker slots remaining: {remaining}")
            await destination.send("\n".join(lines))
            return

        emojis = list(dict.fromkeys(items))  # type: ignore[arg-type]
        static_count = sum(1 for emoji in emojis if not emoji.animated)
        animated_count = sum(1 for emoji in emojis if emoji.animated)
        lines = [f"Found {len(emojis)} custom emoji{'s' if len(emojis) != 1 else ''}."]
        lines.append(f"- Static: {static_count}")
        lines.append(f"- Animated: {animated_count}")
        if guild is not None:
            lines.append(f"- Static slots remaining: {self.available_emoji_slots(guild, False)}")
            lines.append(f"- Animated slots remaining: {self.available_emoji_slots(guild, True)}")
        await destination.send("\n".join(lines))


    @commands.group(name="steal", aliases=["emojisteal"], invoke_without_command=True)
    async def steal_command(self, ctx: commands.Context):
        """Steals the emojis and stickers of the message you reply to. Can also upload them with [p]steal upload."""
        if not (emojis := await self.steal_ctx(ctx)):
            return
        response = '\n'.join([emoji.url for emoji in emojis])
        await ctx.send(response)

    @steal_command.command(name="info")
    async def steal_info_command(self, ctx: commands.Context):
        """Show what would be stolen and current slot availability."""
        if not (emojis_or_stickers := await self.steal_ctx(ctx)):
            return
        await self._send_steal_info(ctx, ctx.guild, emojis_or_stickers)


    # context menu added in __init__
    async def steal_app_command(self, ctx: discord.Interaction, message: discord.Message):
        if message.stickers:
            emojis = message.stickers
        elif not (emojis := self.get_emojis(message.content)):
            return await ctx.response.send_message(MISSING_EMOJIS, ephemeral=True)

        response = '\n'.join([emoji.url for emoji in emojis])
        await ctx.response.send_message(content=response, ephemeral=True)


    @steal_command.command(name="upload")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_emojis=True)
    async def steal_upload_command(self, ctx: commands.Context, *names: str):
        """Steals emojis and stickers you reply to and uploads them to this server."""
        assert ctx.guild
        if not await self._ensure_upload_allowed(ctx, ctx.guild, ctx.author):
            return
        if not (emojis_or_stickers := await self.steal_ctx(ctx)):
            return
        final_names = self._sanitize_names(names)
        
        if isinstance(emojis_or_stickers[0], discord.StickerItem):
            uploaded, error = await self._upload_stickers(ctx.guild, emojis_or_stickers, final_names)
            if error and not uploaded:
                return await ctx.send(error)

            response = []
            if uploaded:
                response.append(f"{STICKER_SUCCESS}: {self._join_names(uploaded)}")
                response.append(f"Uploaded {len(uploaded)}/{len(emojis_or_stickers)} stickers.")
            if error:
                response.append(error)
            return await ctx.send("\n".join(response))
        
        emojis: List[discord.PartialEmoji] = list(dict.fromkeys(emojis_or_stickers))  # type: ignore
        added_emojis, error = await self._upload_emojis(ctx.guild, emojis, final_names)
        if error and not added_emojis:
            return await ctx.send(error)

        for added in added_emojis:
            try:
                await ctx.message.add_reaction(added)
            except discord.DiscordException:
                pass

        response = []
        if added_emojis:
            response.append(' '.join(str(e) for e in added_emojis))
            response.append(f"Uploaded {len(added_emojis)}/{len(emojis)} emojis.")
        if error:
            response.append(error)
        await ctx.send("\n".join(response))


    # context menu added in __init__
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_emojis=True)
    async def steal_upload_app_command(self, ctx: discord.Interaction, message: discord.Message):
        assert ctx.guild
        if not await self._ensure_upload_allowed(ctx.response, ctx.guild, ctx.user, ephemeral=True):
            return
        if message.stickers:
            emojis_or_stickers = message.stickers
        else:
            emojis_or_stickers = self.get_emojis(message.content)

        if not emojis_or_stickers:
            return await ctx.response.send_message(MISSING_EMOJIS, ephemeral=True)
        
        await ctx.response.defer(thinking=True)
        
        if isinstance(emojis_or_stickers[0], discord.StickerItem):
            uploaded, error = await self._upload_stickers(ctx.guild, emojis_or_stickers)
            if error and not uploaded:
                return await ctx.edit_original_response(content=error)

            response = []
            if uploaded:
                response.append(f"{STICKER_SUCCESS}: {self._join_names(uploaded)}")
                response.append(f"Uploaded {len(uploaded)}/{len(emojis_or_stickers)} stickers.")
            if error:
                response.append(error)
            return await ctx.edit_original_response(content="\n".join(response))

        emojis: List[discord.PartialEmoji] = list(dict.fromkeys(emojis_or_stickers))  # type: ignore
        added_emojis, error = await self._upload_emojis(ctx.guild, emojis)
        response = []
        if added_emojis:
            response.append(' '.join(str(e) for e in added_emojis))
            response.append(f"Uploaded {len(added_emojis)}/{len(emojis)} emojis.")
        if error:
            response.append(error)
        await ctx.edit_original_response(content="\n".join(response))


    @commands.group(name="stealset", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def stealset(self, ctx: commands.Context):
        """Manage EmojiSteal settings for this server."""
        await ctx.send("Use `stealset allowuser`, `stealset denyuser`, or `stealset showallowlist`.")

    @stealset.command(name="allowuser")
    async def stealset_allowuser(self, ctx: commands.Context, user: Union[discord.Member, discord.User]):
        """Allow one user to use steal upload without Manage Emojis."""
        assert ctx.guild
        allowlist = await self.config.guild(ctx.guild).upload_allowlist()
        if user.id in allowlist:
            return await ctx.send(f"{user.mention} is already on the steal upload allowlist.")

        allowlist.append(user.id)
        await self.config.guild(ctx.guild).upload_allowlist.set(allowlist)
        await ctx.send(f"Added {user.mention} to the steal upload allowlist.")

    @stealset.command(name="denyuser")
    async def stealset_denyuser(self, ctx: commands.Context, user: Union[discord.Member, discord.User]):
        """Remove one user from the steal upload allowlist."""
        assert ctx.guild
        allowlist = await self.config.guild(ctx.guild).upload_allowlist()
        if user.id not in allowlist:
            return await ctx.send(f"{user.mention} is not on the steal upload allowlist.")

        allowlist.remove(user.id)
        await self.config.guild(ctx.guild).upload_allowlist.set(allowlist)
        await ctx.send(f"Removed {user.mention} from the steal upload allowlist.")

    @stealset.command(name="showallowlist")
    async def stealset_showallowlist(self, ctx: commands.Context):
        """Show users allowed to bypass the steal upload permission check."""
        assert ctx.guild
        allowlist = await self.config.guild(ctx.guild).upload_allowlist()
        if not allowlist:
            return await ctx.send("The steal upload allowlist is empty.")

        mentions = ", ".join(f"<@{user_id}>" for user_id in allowlist)
        await ctx.send(f"Steal upload allowlist: {mentions}")


    @commands.command()
    async def getemoji(self, ctx: commands.Context, *, emoji: str):
        """Get the image link for custom emojis or an emoji ID."""
        emoji = emoji.strip()

        if emoji.isnumeric():
            emojis = [discord.PartialEmoji(name="e", animated=b, id=int(emoji)) for b in [False, True]]
        elif not (emojis := self.get_emojis(emoji)):
            await ctx.send(INVALID_EMOJI)
            return

        await ctx.send('\n'.join(emoji.url for emoji in emojis))


    @commands.bot_has_permissions(manage_emojis=True)
    @commands.guild_only()
    @commands.command()
    async def uploadsticker(self, ctx: commands.Context, *, name: str = None):
        """Uploads a sticker to the server, useful for mobile."""
        assert ctx.guild
        if not await self._ensure_upload_allowed(ctx, ctx.guild, ctx.author):
            return
        if len(ctx.guild.stickers) >= ctx.guild.sticker_limit:
            return await ctx.send(content=STICKER_SLOTS)

        if not ctx.message.attachments:
            return await ctx.send(STICKER_ATTACHMENT)

        attachment = ctx.message.attachments[0]
        filename = attachment.filename.lower()
        if not filename.endswith((".png", ".zip")):
            return await ctx.send(STICKER_ATTACHMENT)
        if attachment.size > STICKER_KB * 1024 or attachment.width and attachment.width > STICKER_DIM or attachment.height and attachment.height > STICKER_DIM:
            return await ctx.send(STICKER_TOO_BIG)

        await ctx.typing()
        name = name or attachment.filename.rsplit(".", 1)[0]
        fp = io.BytesIO()

        try:
            await attachment.save(fp)
            fp.seek(0)

            if filename.endswith(".zip"):
                fp = self._extract_sticker_png_from_zip(fp)
            sticker = await ctx.guild.create_sticker(
                name=name, description=f"{UPLOADED_BY} {ctx.author}", emoji=STICKER_EMOJI, file=discord.File(fp))

        except ValueError as error:
            return await ctx.send(str(error))
        except (discord.DiscordException, zipfile.BadZipFile) as error:
            if "exceed" in str(error):
                return await ctx.send(STICKER_TOO_BIG)
            return await ctx.send(f"{STICKER_FAIL}, {type(error).__name__}: {error}")

        return await ctx.send(f"{STICKER_SUCCESS}: {sticker.name}")
