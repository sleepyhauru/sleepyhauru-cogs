import asyncio
import functools
import ipaddress
import math
import socket
from io import BytesIO
from random import randint
from typing import Optional, Tuple, Union
from urllib.parse import urlparse

import aiohttp
import discord
from PIL import Image, ImageEnhance, UnidentifiedImageError
from redbot.core import Config, checks, commands


MAX_SIZE = 8 * 1000 * 1000
MAX_SOURCE_SIZE = 32 * 1000 * 1000
MAX_DIMENSION = 3840
MAX_PIXELS = 12_000_000
MAX_FRAMES = 200
HISTORY_LOOKBACK = 5

try:
    RESAMPLE_BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE_BILINEAR = Image.BILINEAR


class ImageFindError(Exception):
    """Generic error for image lookup/validation."""
    pass


class Deepfry(commands.Cog):
    """Deepfries memes."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7345167900)
        self.config.register_guild(
            fryChance=0,
            nukeChance=0,
            allowAllTypes=False,
            replyOnly=False,
            debug=False,
        )
        self.imagetypes = [".png", ".jpg", ".jpeg"]
        self.videotypes = [".gif", ".webp"]
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

    def cog_unload(self):
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())

    async def _debug(self, ctx, text: str):
        if ctx.guild and await self.config.guild(ctx.guild).debug():
            await ctx.send(f"`[deepfry debug] {text}`")

    def _valid_path_type(self, path: str, allow_all_types: bool = False):
        path = path.lower()
        return (
            any(path.endswith(x) for x in self.imagetypes)
            or any(path.endswith(x) for x in self.videotypes)
            or allow_all_types
        )

    def _get_valid_attachment(
        self, message: discord.Message, allow_all_types: bool = False
    ) -> Optional[discord.Attachment]:
        for attachment in message.attachments:
            path = urlparse(attachment.url).path
            if self._valid_path_type(path, allow_all_types):
                return attachment
        return None

    async def _get_referenced_message(self, ctx) -> Optional[discord.Message]:
        if not ctx.message.reference or not ctx.message.reference.message_id:
            return None
        try:
            return await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.HTTPException:
            return None

    def _get_message_image_url(
        self,
        message: discord.Message,
        allow_all_types: bool = False,
        allow_thumbnail: bool = True,
    ) -> Optional[str]:
        attachment = self._get_valid_attachment(message, allow_all_types)
        if attachment is not None:
            return attachment.url

        for embed in message.embeds:
            if embed.image and embed.image.url:
                return embed.image.url
            if allow_thumbnail and embed.thumbnail and embed.thumbnail.url:
                return embed.thumbnail.url

        return None

    @staticmethod
    def _is_private_network_address(value: str) -> bool:
        ip = ipaddress.ip_address(value)
        return any(
            [
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            ]
        )

    async def _resolve_hostname_addresses(self, hostname: str, port: int) -> set[str]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return {info[4][0] for info in infos if info and len(info) >= 5 and info[4]}

    async def _assert_safe_remote_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ImageFindError("Only http and https image URLs are allowed.")
        if parsed.username or parsed.password:
            raise ImageFindError("That image URL is not allowed.")

        hostname = parsed.hostname
        if not hostname:
            raise ImageFindError("That image URL is invalid.")
        if hostname.lower() == "localhost":
            raise ImageFindError("That image URL is not allowed.")

        try:
            if self._is_private_network_address(hostname):
                raise ImageFindError("That image URL is not allowed.")
            return
        except ValueError:
            pass

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = await self._resolve_hostname_addresses(hostname, port)
        except OSError:
            raise ImageFindError("That image URL could not be resolved.")

        if not addresses:
            raise ImageFindError("That image URL could not be resolved.")
        if any(self._is_private_network_address(address) for address in addresses):
            raise ImageFindError("That image URL is not allowed.")

    async def _read_url_bytes(self, url: str, filesize_limit: int) -> bytes:
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

        await self._assert_safe_remote_url(url)

        try:
            async with self.session.get(url) as response:
                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > filesize_limit:
                    raise ImageFindError("That image is too large.")

                data = await response.read()
                if len(data) > filesize_limit:
                    raise ImageFindError("That image is too large.")

                return data
        except ImageFindError:
            raise
        except aiohttp.ClientError:
            raise ImageFindError(
                "An image could not be downloaded. Make sure you provide a direct link."
            )

    @staticmethod
    def _source_filesize_limit(output_limit: int) -> int:
        return max(output_limit, MAX_SOURCE_SIZE)

    @staticmethod
    def _constrained_dimensions(width: int, height: int) -> Tuple[int, int]:
        scale = 1.0
        largest_side = max(width, height)
        if largest_side > MAX_DIMENSION:
            scale = min(scale, MAX_DIMENSION / largest_side)

        pixels = width * height
        if pixels > MAX_PIXELS:
            scale = min(scale, math.sqrt(MAX_PIXELS / pixels))

        if scale >= 1.0:
            return width, height

        new_width = max(1, int(width * scale))
        new_height = max(1, int(height * scale))
        while max(new_width, new_height) > MAX_DIMENSION or (new_width * new_height) > MAX_PIXELS:
            if new_width >= new_height and new_width > 1:
                new_width -= 1
            elif new_height > 1:
                new_height -= 1
            else:
                break
        return new_width, new_height

    @classmethod
    def _resize_within_limits(cls, img: Image.Image) -> Image.Image:
        width, height = img.size
        new_size = cls._constrained_dimensions(width, height)
        if new_size == (width, height):
            return img
        return img.resize(new_size, RESAMPLE_BILINEAR)

    @staticmethod
    def _buffer_size(temp: BytesIO) -> int:
        return temp.getbuffer().nbytes

    @classmethod
    def _encode_static_result(cls, img: Image.Image, filename: str, filesize_limit: int) -> BytesIO:
        img = cls._resize_within_limits(img.convert("RGB"))
        width, height = img.size
        best = None
        best_size = None
        scales = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]
        qualities = [95, 85, 75, 60, 45, 30, 20, 10, 5]

        for scale in scales:
            if scale == 1.0:
                candidate = img
            else:
                candidate = img.resize(
                    (
                        max(1, int(width * scale)),
                        max(1, int(height * scale)),
                    ),
                    RESAMPLE_BILINEAR,
                )

            for quality in qualities:
                temp = BytesIO()
                temp.name = filename
                try:
                    candidate.save(temp, format="JPEG", quality=quality, optimize=True)
                except OSError:
                    temp = BytesIO()
                    temp.name = filename
                    candidate.save(temp, format="JPEG", quality=quality)

                size = cls._buffer_size(temp)
                if best is None or size < best_size:
                    temp.seek(0)
                    best = temp
                    best_size = size

                if size <= filesize_limit:
                    temp.seek(0)
                    return temp

        if best is not None:
            best.seek(0)
            return best

        temp = BytesIO()
        temp.name = filename
        img.save(temp, format="JPEG", quality=5)
        temp.seek(0)
        return temp

    @staticmethod
    def _stepped_duration(duration, step: int):
        if duration is None:
            return None
        if isinstance(duration, int):
            return duration * step
        if isinstance(duration, (list, tuple)):
            stepped = []
            index = 0
            while index < len(duration):
                stepped.append(sum(duration[index:index + step]))
                index += step
            return stepped
        return duration

    @staticmethod
    def _quantize_frame(frame: Image.Image, colors: int) -> Image.Image:
        if colors >= 256:
            return frame
        quantize = getattr(frame, "quantize", None)
        if callable(quantize):
            try:
                return quantize(colors=colors)
            except TypeError:
                return quantize(colors)
            except Exception:
                return frame
        return frame

    @classmethod
    def _encode_animated_result(
        cls,
        frames: list[Image.Image],
        filename: str,
        filesize_limit: int,
        duration,
    ) -> BytesIO:
        if not frames:
            raise ImageFindError("Failed to process that animation.")

        best = None
        best_size = None
        scales = [1.0, 0.85, 0.7, 0.55, 0.4, 0.3]
        steps = [1, 2, 4]
        colors_list = [256, 128, 64]

        for step in steps:
            stepped_frames = frames[::step]
            stepped_duration = cls._stepped_duration(duration, step)

            for scale in scales:
                scaled_frames = []
                for frame in stepped_frames:
                    candidate = frame
                    if scale != 1.0:
                        width, height = candidate.size
                        candidate = candidate.resize(
                            (
                                max(1, int(width * scale)),
                                max(1, int(height * scale)),
                            ),
                            RESAMPLE_BILINEAR,
                        )
                    scaled_frames.append(candidate)

                for colors in colors_list:
                    encoded_frames = [cls._quantize_frame(frame, colors) for frame in scaled_frames]
                    temp = BytesIO()
                    temp.name = filename
                    save_kwargs = {
                        "format": "GIF",
                        "save_all": True,
                        "append_images": encoded_frames[1:],
                        "loop": 0,
                        "optimize": True,
                    }
                    if stepped_duration is not None:
                        save_kwargs["duration"] = stepped_duration
                    encoded_frames[0].save(temp, **save_kwargs)
                    size = cls._buffer_size(temp)

                    if best is None or size < best_size:
                        temp.seek(0)
                        best = temp
                        best_size = size

                    if size <= filesize_limit:
                        temp.seek(0)
                        return temp

        if best is not None:
            best.seek(0)
            return best

        raise ImageFindError("Failed to process that animation.")

    def _open_image_from_bytes(
        self, data: bytes
    ) -> Tuple[Image.Image, bool, Optional[int]]:
        try:
            img = Image.open(BytesIO(data))
        except (UnidentifiedImageError, OSError):
            raise ImageFindError("The downloaded file was not a valid image.")

        width, height = img.size
        is_animated = bool(
            getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1
        )

        if is_animated:
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > MAX_FRAMES:
                raise ImageFindError("That animation has too many frames.")
            duration = img.info.get("duration")
        else:
            duration = None
            img = self._resize_within_limits(img.convert("RGB"))

        return img, is_animated, duration

    async def _read_attachment_bytes(self, attachment: discord.Attachment, filesize_limit: int) -> bytes:
        if attachment.size > filesize_limit:
            raise ImageFindError("That image is too large.")

        temp = BytesIO()
        try:
            await attachment.save(temp)
        except discord.HTTPException:
            raise ImageFindError("I couldn't download that attachment.")
        return temp.getvalue()

    async def _resolve_target(
        self, ctx, link: Union[discord.Member, str, None], allow_all_types: bool
    ):
        reply_only = await self.config.guild(ctx.guild).replyOnly() if ctx.guild else False

        if isinstance(link, discord.Member):
            return ("member", link, None)

        if isinstance(link, str) and link:
            return ("url", link, "explicit link")

        invoking_attachment = self._get_valid_attachment(ctx.message, allow_all_types)
        if invoking_attachment is not None:
            return ("attachment", invoking_attachment, "invoking message attachment")

        ref_msg = await self._get_referenced_message(ctx)
        if ref_msg:
            ref_attachment = self._get_valid_attachment(ref_msg, allow_all_types)
            if ref_attachment is not None:
                return ("attachment", ref_attachment, f"reply attachment from message {ref_msg.id}")

            ref_url = self._get_message_image_url(ref_msg, allow_all_types, allow_thumbnail=True)
            if ref_url:
                return ("url", ref_url, f"reply embed image from message {ref_msg.id}")

            raise ImageFindError("No image was found in the replied-to message.")

        if reply_only:
            raise ImageFindError("Reply-only mode is enabled. Reply to a message with an image or provide a direct link.")

        async for msg in ctx.channel.history(limit=HISTORY_LOOKBACK, before=ctx.message):
            history_attachment = self._get_valid_attachment(msg, allow_all_types)
            if history_attachment is not None:
                return ("attachment", history_attachment, f"history attachment from message {msg.id}")

            hist_url = self._get_message_image_url(msg, allow_all_types, allow_thumbnail=True)
            if hist_url:
                return ("url", hist_url, f"history embed image from message {msg.id}")

        raise ImageFindError("Please provide an attachment, a direct link, or reply to a message with an image.")

    async def _get_image(self, ctx, link: Union[discord.Member, str, None]):
        """Helper function to find and validate an image."""
        if ctx.guild:
            allow_all_types = await self.config.guild(ctx.guild).allowAllTypes()
            output_limit = ctx.guild.filesize_limit
        else:
            allow_all_types = False
            output_limit = MAX_SIZE
        source_limit = self._source_filesize_limit(output_limit)

        source_type, source_value, source_note = await self._resolve_target(ctx, link, allow_all_types)
        await self._debug(ctx, f"Selected source: {source_note or source_type}")

        if source_type == "member":
            member = source_value
            if discord.version_info.major == 1:
                avatar = member.avatar_url_as(static_format="png")
            else:
                avatar = member.display_avatar.with_static_format("png")

            try:
                data = await avatar.read()
            except discord.HTTPException:
                raise ImageFindError("I couldn't read that member avatar.")

            img, isgif, duration = self._open_image_from_bytes(data)
            await self._debug(ctx, f"Avatar resolved: animated={isgif}, size={img.size}")
            return img, isgif, duration

        if source_type == "attachment":
            attachment = source_value
            data = await self._read_attachment_bytes(attachment, source_limit)
            img, isgif, duration = self._open_image_from_bytes(data)
            await self._debug(ctx, f"Attachment resolved: animated={isgif}, size={img.size}")
            return img, isgif, duration

        if source_type == "url":
            url = source_value
            path = urlparse(url).path
            if not self._valid_path_type(path, allow_all_types):
                await self._debug(ctx, "URL path had no trusted extension; attempting content sniff anyway.")
            data = await self._read_url_bytes(url, source_limit)
            img, isgif, duration = self._open_image_from_bytes(data)
            await self._debug(ctx, f"URL resolved: animated={isgif}, size={img.size}")
            return img, isgif, duration

        raise ImageFindError("Failed to resolve an image source.")

    @classmethod
    def _fry(cls, img, filesize_limit):
        e = ImageEnhance.Sharpness(img)
        img = e.enhance(100)
        e = ImageEnhance.Contrast(img)
        img = e.enhance(100)
        e = ImageEnhance.Brightness(img)
        img = e.enhance(0.27)
        r, g, b = img.split()
        e = ImageEnhance.Brightness(r)
        r = e.enhance(4)
        e = ImageEnhance.Brightness(g)
        g = e.enhance(1.75)
        e = ImageEnhance.Brightness(b)
        b = e.enhance(0.6)
        img = Image.merge("RGB", (r, g, b))
        e = ImageEnhance.Brightness(img)
        img = e.enhance(1.5)
        return cls._encode_static_result(img, "deepfried.jpg", filesize_limit)

    @classmethod
    def _videofry(cls, img, duration, filesize_limit):
        imgs = []
        frame = 0
        while img:
            if frame >= MAX_FRAMES:
                break
            i = cls._resize_within_limits(img.copy().convert("RGB"))
            e = ImageEnhance.Sharpness(i)
            i = e.enhance(100)
            e = ImageEnhance.Contrast(i)
            i = e.enhance(100)
            e = ImageEnhance.Brightness(i)
            i = e.enhance(0.27)
            r, g, b = i.split()
            e = ImageEnhance.Brightness(r)
            r = e.enhance(4)
            e = ImageEnhance.Brightness(g)
            g = e.enhance(1.75)
            e = ImageEnhance.Brightness(b)
            b = e.enhance(0.6)
            i = Image.merge("RGB", (r, g, b))
            e = ImageEnhance.Brightness(i)
            i = e.enhance(1.5)
            imgs.append(i)
            frame += 1
            try:
                img.seek(frame)
            except EOFError:
                break

        return cls._encode_animated_result(imgs, "deepfried.gif", filesize_limit, duration)

    @classmethod
    def _nuke(cls, img, filesize_limit):
        w, h = img.size
        dx = ((w + 200) // 200) * 2
        dy = ((h + 200) // 200) * 2
        img = img.resize(((w + 1) // dx, (h + 1) // dy))
        e = ImageEnhance.Sharpness(img)
        img = e.enhance(100)
        e = ImageEnhance.Contrast(img)
        img = e.enhance(100)
        e = ImageEnhance.Brightness(img)
        img = e.enhance(0.27)
        r, g, b = img.split()
        e = ImageEnhance.Brightness(r)
        r = e.enhance(4)
        e = ImageEnhance.Brightness(g)
        g = e.enhance(1.75)
        e = ImageEnhance.Brightness(b)
        b = e.enhance(0.6)
        img = Image.merge("RGB", (r, g, b))
        e = ImageEnhance.Brightness(img)
        img = e.enhance(1.5)
        e = ImageEnhance.Sharpness(img)
        img = e.enhance(100)
        img = img.resize((w, h), RESAMPLE_BILINEAR)
        return cls._encode_static_result(img, "nuke.jpg", filesize_limit)

    @classmethod
    def _videonuke(cls, img, duration, filesize_limit):
        imgs = []
        frame = 0
        while img:
            if frame >= MAX_FRAMES:
                break
            i = cls._resize_within_limits(img.copy().convert("RGB"))
            w, h = i.size
            dx = ((w + 200) // 200) * 2
            dy = ((h + 200) // 200) * 2
            i = i.resize(((w + 1) // dx, (h + 1) // dy))
            e = ImageEnhance.Sharpness(i)
            i = e.enhance(100)
            e = ImageEnhance.Contrast(i)
            i = e.enhance(100)
            e = ImageEnhance.Brightness(i)
            i = e.enhance(0.27)
            r, g, b = i.split()
            e = ImageEnhance.Brightness(r)
            r = e.enhance(4)
            e = ImageEnhance.Brightness(g)
            g = e.enhance(1.75)
            e = ImageEnhance.Brightness(b)
            b = e.enhance(0.6)
            i = Image.merge("RGB", (r, g, b))
            e = ImageEnhance.Brightness(i)
            i = e.enhance(1.5)
            e = ImageEnhance.Sharpness(i)
            i = e.enhance(100)
            i = i.resize((w, h), RESAMPLE_BILINEAR)
            imgs.append(i)
            frame += 1
            try:
                img.seek(frame)
            except EOFError:
                break

        return cls._encode_animated_result(imgs, "nuke.gif", filesize_limit, duration)

    @commands.command(aliases=["df"])
    @commands.bot_has_permissions(attach_files=True)
    async def deepfry(self, ctx, link: Union[discord.Member, str] = None):
        """
        Deepfries images.

        The optional parameter "link" can be either a member or a direct link to an image.
        """
        async with ctx.typing():
            try:
                img, isgif, duration = await self._get_image(ctx, link)
            except ImageFindError as e:
                return await ctx.send(str(e))

            if isgif:
                task = functools.partial(
                    self._videofry,
                    img,
                    duration,
                    ctx.guild.filesize_limit if ctx.guild else MAX_SIZE,
                )
            else:
                task = functools.partial(self._fry, img, ctx.guild.filesize_limit if ctx.guild else MAX_SIZE)

            task = self.bot.loop.run_in_executor(None, task)
            try:
                image = await asyncio.wait_for(task, timeout=60)
            except asyncio.TimeoutError:
                return await ctx.send("The image took too long to process.")

            try:
                await ctx.send(file=discord.File(image))
            except discord.HTTPException:
                return await ctx.send("That image is too large.")

    @commands.command()
    @commands.bot_has_permissions(attach_files=True)
    async def nuke(self, ctx, link: Union[discord.Member, str] = None):
        """
        Demolishes images.

        The optional parameter "link" can be either a member or a direct link to an image.
        """
        async with ctx.typing():
            try:
                img, isgif, duration = await self._get_image(ctx, link)
            except ImageFindError as e:
                return await ctx.send(str(e))

            if isgif:
                task = functools.partial(
                    self._videonuke,
                    img,
                    duration,
                    ctx.guild.filesize_limit if ctx.guild else MAX_SIZE,
                )
            else:
                task = functools.partial(self._nuke, img, ctx.guild.filesize_limit if ctx.guild else MAX_SIZE)

            task = self.bot.loop.run_in_executor(None, task)
            try:
                image = await asyncio.wait_for(task, timeout=60)
            except asyncio.TimeoutError:
                return await ctx.send("The image took too long to process.")

            try:
                await ctx.send(file=discord.File(image))
            except discord.HTTPException:
                return await ctx.send("That image is too large.")

    @commands.guild_only()
    @checks.guildowner()
    @commands.group(invoke_without_command=True)
    async def deepfryset(self, ctx):
        """Config options for deepfry."""
        cfg = await self.config.guild(ctx.guild).all()
        msg = (
            "Allow all filetypes: {allowAllTypes}\n"
            "Reply only mode: {replyOnly}\n"
            "Debug mode: {debug}\n"
            "Deepfry chance: {fryChance}\n"
            "Nuke chance: {nukeChance}"
        ).format_map(cfg)
        await ctx.send(f"```py\n{msg}\n```")

    @deepfryset.command()
    async def frychance(self, ctx, value: int = None):
        """
        Change the rate images are automatically deepfried.

        Images will have a 1/<value> chance to be deepfried.
        Higher values cause less often fries.
        Set to 0 to disable.
        """
        if value is None:
            v = await self.config.guild(ctx.guild).fryChance()
            if v == 0:
                await ctx.send("Autofrying is currently disabled.")
            elif v == 1:
                await ctx.send("All images are being fried.")
            else:
                await ctx.send(f"1 out of every {v} images are being fried.")
            return

        if value < 0:
            return await ctx.send("Value cannot be less than 0.")

        await self.config.guild(ctx.guild).fryChance.set(value)
        if value == 0:
            await ctx.send("Autofrying is now disabled.")
        elif value == 1:
            await ctx.send("All images will be fried.")
        else:
            await ctx.send(f"1 out of every {value} images will be fried.")

    @deepfryset.command()
    async def nukechance(self, ctx, value: int = None):
        """
        Change the rate images are automatically nuked.

        Images will have a 1/<value> chance to be nuked.
        Higher values cause less often nukes.
        Set to 0 to disable.
        """
        if value is None:
            v = await self.config.guild(ctx.guild).nukeChance()
            if v == 0:
                await ctx.send("Autonuking is currently disabled.")
            elif v == 1:
                await ctx.send("All images are being nuked.")
            else:
                await ctx.send(f"1 out of every {v} images are being nuked.")
            return

        if value < 0:
            return await ctx.send("Value cannot be less than 0.")

        await self.config.guild(ctx.guild).nukeChance.set(value)
        if value == 0:
            await ctx.send("Autonuking is now disabled.")
        elif value == 1:
            await ctx.send("All images will be nuked.")
        else:
            await ctx.send(f"1 out of every {value} images will be nuked.")

    @deepfryset.command()
    async def allowalltypes(self, ctx, value: bool = None):
        """
        Allow filetypes that have not been verified to be valid.

        Can cause errors if enabled, use at your own risk.
        Defaults to False.
        """
        if value is None:
            v = await self.config.guild(ctx.guild).allowAllTypes()
            if v:
                await ctx.send("You are currently able to use unverified types.")
            else:
                await ctx.send("You are currently not able to use unverified types.")
            return

        await self.config.guild(ctx.guild).allowAllTypes.set(value)
        if value:
            await ctx.send(
                "You will now be able to use unverified types.\n"
                "This mode can cause errors. Use at your own risk."
            )
        else:
            await ctx.send("You will no longer be able to use unverified types.")

    @deepfryset.command()
    async def replyonly(self, ctx, value: bool = None):
        """Require reply/direct input instead of searching recent history."""
        if value is None:
            current = await self.config.guild(ctx.guild).replyOnly()
            return await ctx.send(f"Reply-only mode is currently `{current}`.")

        await self.config.guild(ctx.guild).replyOnly.set(value)
        await ctx.send(f"Reply-only mode set to `{value}`.")

    @deepfryset.command()
    async def debug(self, ctx, value: bool = None):
        """Enable or disable debug output for command image resolution."""
        if value is None:
            current = await self.config.guild(ctx.guild).debug()
            return await ctx.send(f"Debug mode is currently `{current}`.")

        await self.config.guild(ctx.guild).debug.set(value)
        await ctx.send(f"Debug mode set to `{value}`.")

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete."""
        return

    @commands.Cog.listener()
    async def on_message_without_command(self, msg):
        """Passively deepfries random attached images."""
        if msg.author.bot:
            return
        if not msg.attachments:
            return
        if msg.guild is None:
            return
        if await self.bot.cog_disabled_in_guild(self, msg.guild):
            return
        if not msg.channel.permissions_for(msg.guild.me).attach_files:
            return

        allow_all_types = await self.config.guild(msg.guild).allowAllTypes()
        attachment = self._get_valid_attachment(msg, allow_all_types)
        if attachment is None:
            return
        if attachment.size > self._source_filesize_limit(msg.guild.filesize_limit):
            return

        try:
            data = await self._read_attachment_bytes(
                attachment,
                self._source_filesize_limit(msg.guild.filesize_limit),
            )
            img, isgif, duration = self._open_image_from_bytes(data)
        except ImageFindError:
            return

        vfry = await self.config.guild(msg.guild).fryChance()
        vnuke = await self.config.guild(msg.guild).nukeChance()

        if vnuke != 0 and randint(1, vnuke) == 1:
            if isgif:
                task = functools.partial(self._videonuke, img, duration, msg.guild.filesize_limit)
            else:
                task = functools.partial(self._nuke, img, msg.guild.filesize_limit)
            task = self.bot.loop.run_in_executor(None, task)
            try:
                image = await asyncio.wait_for(task, timeout=60)
                await msg.channel.send(file=discord.File(image))
            except (asyncio.TimeoutError, discord.HTTPException):
                pass
            return

        if vfry != 0 and randint(1, vfry) == 1:
            if isgif:
                task = functools.partial(self._videofry, img, duration, msg.guild.filesize_limit)
            else:
                task = functools.partial(self._fry, img, msg.guild.filesize_limit)
            task = self.bot.loop.run_in_executor(None, task)
            try:
                image = await asyncio.wait_for(task, timeout=60)
                await msg.channel.send(file=discord.File(image))
            except (asyncio.TimeoutError, discord.HTTPException):
                pass
