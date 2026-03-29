import asyncio
import mimetypes
import os
import random
import string
from pathlib import Path
from typing import Literal, Optional, cast

import discord
from red_commons.logging import getLogger
from redbot.core import Config, checks, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

log = getLogger("red.sleepyhauru-cogs.addimage")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DEFAULT_MAX_FILE_SIZE = 8 * 1024 * 1024


class AddImage(commands.Cog):
    """
    Add images the bot can upload
    """

    __author__ = ["sleepyhauru"]
    __version__ = "1.3.5"

    def __init__(self, bot):
        self.bot = bot
        temp_folder = cog_data_path(self) / "global"
        temp_folder.mkdir(exist_ok=True, parents=True)
        default_global = {"images": [], "max_file_size": DEFAULT_MAX_FILE_SIZE}
        default_guild = {"images": [], "ignore_global": False}
        self.config = Config.get_conf(self, 16446735546)
        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """
        Thanks Sinbad!
        """
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        """
        Method for finding users data inside the cog and deleting it.
        """
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            remaining_images = []
            for image in data["images"]:
                if image["author"] == user_id:
                    try:
                        os.remove(cog_data_path(self) / str(guild_id) / image["file_loc"])
                    except Exception:
                        log.error(
                            "Error deleting image %s",
                            image["file_loc"],
                            exc_info=True,
                        )
                else:
                    remaining_images.append(image)
            await self.config.guild_from_id(guild_id).images.set(remaining_images)

    async def initialize(self) -> None:
        guilds = await self.config.all_guilds()
        for guild_id, data in guilds.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            for image in data["images"]:
                image["file_loc"] = image["file_loc"].split("/")[-1]
            await self.config.guild(guild).set(data)

        async with self.config.images() as images:
            for image in images:
                image["file_loc"] = image["file_loc"].split("/")[-1]

    async def first_word(self, msg: str) -> str:
        return msg.split(" ")[0].lower()

    async def get_prefix(self, message: discord.Message) -> str:
        """
        From Redbot Alias Cog
        Tries to determine what prefix is used in a message object.
            Looks to identify from longest prefix to smallest.
            Will raise ValueError if no prefix is found.
        :param message: Message object
        :return:
        """
        try:
            guild = message.guild
        except AttributeError:
            guild = None
        content = message.content
        try:
            prefixes = await self.bot.get_valid_prefixes(guild)
        except AttributeError:
            # Red 3.1 support
            prefix_list = await self.bot.command_prefix(self.bot, message)
            prefixes = sorted(prefix_list, key=lambda pfx: len(pfx), reverse=True)
        for p in prefixes:
            if content.startswith(p):
                return p
        raise ValueError("No prefix found.")

    async def part_of_existing_command(self, alias: str) -> bool:
        """Command or alias"""
        command = self.bot.get_command(alias)
        return command is not None

    async def make_guild_folder(self, directory: Path) -> None:
        if not directory.is_dir():
            directory.mkdir(exist_ok=True, parents=True)

    async def get_directory(self, guild: Optional[discord.Guild] = None) -> Path:
        if guild is None:
            return cog_data_path(self) / "global"
        return cog_data_path(self) / str(guild.id)

    async def get_image(self, alias: str, guild: Optional[discord.Guild] = None) -> dict:
        if guild is None:
            for image in await self.config.images():
                if image["command_name"].lower() == alias.lower():
                    return image
        else:
            for image in await self.config.guild(guild).images():
                if image["command_name"].lower() == alias.lower():
                    return image
        return {}

    async def get_image_path(self, image: dict, guild: Optional[discord.Guild] = None) -> Path:
        return await self.get_directory(guild) / image["file_loc"]

    async def validate_attachment(self, attachment: discord.Attachment) -> Optional[str]:
        suffix = Path(attachment.filename).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            guessed_type, _ = mimetypes.guess_type(attachment.filename)
            if not guessed_type or not guessed_type.startswith("image/"):
                return "That attachment is not a supported image type."

        max_file_size = await self.config.max_file_size()
        if attachment.size > max_file_size:
            return f"That file is too large. Max allowed size is {max_file_size // (1024 * 1024)} MB."

        return None

    def _safe_storage_extension(self, filename: str) -> str:
        ext_aliases = {
            ".jpe": ".jpg",
        }
        suffix = Path(filename).suffix.lower()
        suffix = ext_aliases.get(suffix, suffix)
        if suffix and suffix[1:].isalnum() and len(suffix) <= 10:
            return suffix

        guessed_type, _ = mimetypes.guess_type(filename)
        if not guessed_type or not guessed_type.startswith("image/"):
            return ""

        guessed_ext = (mimetypes.guess_extension(guessed_type) or "").lower()
        guessed_ext = ext_aliases.get(guessed_ext, guessed_ext)
        if guessed_ext and guessed_ext[1:].isalnum() and len(guessed_ext) <= 10:
            return guessed_ext

        return ""

    async def local_perms(self, message: discord.Message) -> bool:
        """Check the user is/isn't locally whitelisted/blacklisted.
        https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/release/3.0.0/redbot/core/global_checks.py
        """
        if await self.bot.is_owner(message.author):
            return True
        elif message.guild is None:
            return True
        author = cast(discord.Member, message.author)
        try:
            return await self.bot.allowed_by_whitelist_blacklist(
                message.author,
                who_id=message.author.id,
                guild=message.guild,
                role_ids=[r.id for r in author.roles],
            )
        except AttributeError:
            guild_settings = self.bot.db.guild(message.guild)
            local_blacklist = await guild_settings.blacklist()
            local_whitelist = await guild_settings.whitelist()

            _ids = [r.id for r in author.roles if not r.is_default()]
            _ids.append(message.author.id)
            if local_whitelist:
                return any(i in local_whitelist for i in _ids)

            return not any(i in local_blacklist for i in _ids)

    async def global_perms(self, message: discord.Message) -> bool:
        """Check the user is/isn't globally whitelisted/blacklisted.
        https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/release/3.0.0/redbot/core/global_checks.py
        """
        if await self.bot.is_owner(message.author):
            return True
        try:
            return await self.bot.allowed_by_whitelist_blacklist(message.author)
        except AttributeError:
            whitelist = await self.bot.db.whitelist()
            if whitelist:
                return message.author.id in whitelist

            return message.author.id not in await self.bot.db.blacklist()

    async def check_ignored_channel(self, message: discord.Message) -> bool:
        """
        https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/release/3.0.0/redbot/cogs/mod/mod.py#L1273
        """
        ctx = await self.bot.get_context(message)
        return await self.bot.ignored_channel_or_guild(ctx)

    @commands.Cog.listener()
    async def on_message(self, message):
        if len(message.content) < 2 or message.guild is None:
            return

        msg = message.content
        guild = message.guild
        channel = message.channel
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        try:
            prefix = await self.get_prefix(message)
        except ValueError:
            return
        if message.author.bot:
            return
        alias = await self.first_word(msg[len(prefix) :])
        if not await self.local_perms(message):
            return
        if not await self.global_perms(message):
            return
        if not await self.check_ignored_channel(message):
            return
        ignore_global = await self.config.guild(guild).ignore_global()
        if not channel.permissions_for(channel.guild.me).attach_files:
            return

        guild_images = await self.config.guild(guild).images()
        if alias in [x["command_name"] for x in guild_images]:
            await channel.typing()
            image = await self.get_image(alias, guild)
            async with self.config.guild(guild).images() as stored_guild_images:
                stored_guild_images.remove(image)
                image["count"] += 1
                stored_guild_images.append(image)
            path = str(cog_data_path(self)) + f"/{guild.id}/" + image["file_loc"]
            file = discord.File(path)
            try:
                await channel.send(files=[file])
            except discord.errors.Forbidden:
                log.error("Error sending image")
            return

        if alias in [x["command_name"] for x in await self.config.images()] and not ignore_global:
            await channel.typing()
            image = await self.get_image(alias)
            async with self.config.images() as list_images:
                list_images.remove(image)
                image["count"] += 1
                list_images.append(image)
            path = str(cog_data_path(self)) + "/global/" + image["file_loc"]
            file = discord.File(path)
            try:
                await channel.send(files=[file])
            except discord.errors.Forbidden:
                log.error("Error sending image")

    async def check_command_exists(self, command: str, guild: discord.Guild) -> bool:
        command = command.lower()
        if command in [x["command_name"] for x in await self.config.guild(guild).images()]:
            return True
        elif await self.part_of_existing_command(command):
            return True
        elif command in [x["command_name"] for x in await self.config.images()]:
            return True
        else:
            return False

    @commands.group()
    @commands.guild_only()
    async def addimage(self, ctx: commands.Context) -> None:
        """
        Add an image for the bot to directly upload
        """
        pass

    @checks.is_owner()
    @addimage.command(name="setmaxsize")
    async def set_max_file_size(self, ctx: commands.Context, size_mb: int) -> None:
        """
        Set the maximum allowed upload size in MB.
        """
        if size_mb < 1:
            await ctx.send("Size must be at least 1 MB.")
            return
        await self.config.max_file_size.set(size_mb * 1024 * 1024)
        await ctx.send(f"Max image upload size set to {size_mb} MB.")

    @checks.is_owner()
    @addimage.command()
    async def deleteallbyuser(self, ctx: commands.Context, user_id: int):
        """
        Delete all triggers created by a specified user ID.
        """
        await self.red_delete_data_for_user(requester="owner", user_id=user_id)
        await ctx.tick()

    @addimage.command(name="ignoreglobal")
    @checks.mod_or_permissions(manage_channels=True)
    async def ignore_global_commands(self, ctx: commands.Context) -> None:
        """
        Toggle usage of bot owner set global images on this server
        """
        ignore_global = await self.config.guild(ctx.guild).ignore_global()
        await self.config.guild(ctx.guild).ignore_global.set(not ignore_global)
        if ignore_global:
            await ctx.send("Bot owner global images enabled.")
        else:
            await ctx.send("Ignoring bot owner global images.")

    @addimage.command(name="list")
    @commands.bot_has_permissions(embed_links=True)
    async def listimages(
        self, ctx: commands.Context, image_loc="guild", server_id: discord.Guild = None
    ) -> None:
        """
        List images added to bot
        """
        if image_loc in ["global"]:
            image_list = await self.config.images()
        elif image_loc in ["guild", "server"]:
            if server_id is None:
                guild = ctx.message.guild
            else:
                guild = server_id
            image_list = await self.config.guild(guild).images()

        if image_list == []:
            await ctx.send("I do not have any images saved!")
            return
        post_list = [image_list[i : i + 25] for i in range(0, len(image_list), 25)]
        images = []
        for post in post_list:
            em = discord.Embed(timestamp=ctx.message.created_at)
            for image in post:
                info = (
                    "__Author__: "
                    + "<@{}>\n".format(image["author"])
                    + "__Count__: "
                    + "**{}**".format(image["count"])
                )
                em.add_field(name=image["command_name"], value=info)
            em.set_author(name=self.bot.user.display_name, icon_url=self.bot.user.display_avatar)
            em.set_footer(
                text="Page " + "{}/{}".format(post_list.index(post) + 1, len(post_list))
            )
            images.append(em)
        await menu(ctx, images, DEFAULT_CONTROLS)

    @addimage.command(name="show")
    async def show_image(
        self, ctx: commands.Context, name: str, image_loc: str = "guild", server_id: discord.Guild = None
    ) -> None:
        """
        Show a saved image and its metadata.
        """
        name = name.lower()
        guild = ctx.guild if server_id is None else self.bot.get_guild(server_id.id)

        if image_loc == "global":
            image = await self.get_image(name)
            source_guild = None
        else:
            image = await self.get_image(name, guild)
            source_guild = guild

        if not image:
            await ctx.send(f"`{name}` was not found.")
            return

        path = await self.get_image_path(image, source_guild)
        embed = discord.Embed(title=image["command_name"], timestamp=ctx.message.created_at)
        embed.add_field(name="Author", value=f"<@{image['author']}>")
        embed.add_field(name="Uses", value=str(image["count"]))
        embed.add_field(name="Scope", value="global" if source_guild is None else f"guild: {source_guild.name}")
        await ctx.send(embed=embed, file=discord.File(path))

    async def get_saved_filenames(self, guild: Optional[discord.Guild] = None) -> set[str]:
        directory = await self.get_directory(guild)
        if not directory.exists():
            return set()
        return {file.name for file in directory.iterdir() if file.is_file()}

    @addimage.command()
    @checks.is_owner()
    async def clear_global(self, ctx: commands.Context) -> None:
        """
        Clears the full set of images stored globally
        """
        await self.config.images.set([])
        directory = cog_data_path(self) / "global"
        for file in os.listdir(str(directory)):
            try:
                os.remove(str(directory / file))
            except Exception:
                log.error("Error deleting image {image}".format(image=file), exc_info=True)

    @addimage.command()
    @checks.mod_or_permissions(manage_channels=True)
    async def clear_images(self, ctx: commands.Context) -> None:
        """
        Clear all the images stored for the current server
        """
        await self.config.guild(ctx.guild).images.set([])
        directory = await self.get_directory(ctx.guild)
        for file in await self.get_saved_filenames(ctx.guild):
            try:
                os.remove(directory / file)
            except Exception:
                log.error("Error deleting image {image}".format(image=file), exc_info=True)
        await ctx.tick()

    @addimage.command()
    @checks.mod_or_permissions(manage_channels=True)
    async def clean_deleted_images(self, ctx: commands.Context) -> None:
        """
        Cleanup deleted images that are not supposed to be saved anymore
        """
        images = await self.config.guild(ctx.guild).images()
        saved = await self.get_saved_filenames(ctx.guild)
        cleaned_images = []
        for image in images:
            if image["file_loc"] in saved:
                cleaned_images.append(image)
        await self.config.guild(ctx.guild).images.set(cleaned_images)
        await ctx.tick()

    @addimage.command(name="delete", aliases=["remove", "rem", "del"])
    @checks.mod_or_permissions(manage_channels=True)
    async def remimage(self, ctx: commands.Context, name: str) -> None:
        """
        Remove a selected images

        `name` the command name used to post the image
        """
        guild = ctx.message.guild
        channel = ctx.message.channel
        name = name.lower()
        if name not in [x["command_name"] for x in await self.config.guild(guild).images()]:
            await ctx.send(name + " is not an image for this guild!")
            return

        await channel.typing()
        all_imgs = await self.config.guild(guild).images()
        image = await self.get_image(name, guild)
        all_imgs.remove(image)
        try:
            os.remove(cog_data_path(self) / str(guild.id) / image["file_loc"])
        except Exception:
            log.error("Error deleting image %s", image["file_loc"], exc_info=True)
            pass
        await self.config.guild(guild).images.set(all_imgs)
        await ctx.send(name + " has been deleted from this guild!")

    @checks.is_owner()
    @addimage.command(name="deleteglobal", aliases=["dg", "delglobal"])
    async def rem_image_global(self, ctx: commands.Context, name: str) -> None:
        """
        Remove a selected images

        `name` the command name used to post the image
        """
        channel = ctx.message.channel
        name = name.lower()
        if name not in [x["command_name"] for x in await self.config.images()]:
            await ctx.send(name + " is not a global image!")
            return

        await channel.typing()
        all_imgs = await self.config.images()
        image = await self.get_image(name)
        all_imgs.remove(image)
        try:
            os.remove(cog_data_path(self) / "global" / image["file_loc"])
        except Exception:
            log.error("Error deleting image %s", image["file_loc"], exc_info=True)
            pass
        await self.config.images.set(all_imgs)
        await ctx.send(name + " has been deleted globally!")

    @addimage.command(name="rename")
    @checks.mod_or_permissions(manage_channels=True)
    async def rename_image(self, ctx: commands.Context, old_name: str, new_name: str) -> None:
        """
        Rename a guild image trigger.
        """
        guild = ctx.guild
        assert guild is not None
        old_name = old_name.lower()
        new_name = new_name.lower()

        if old_name == new_name:
            await ctx.send("The new name must be different.")
            return

        if await self.check_command_exists(new_name, guild):
            await ctx.send(f"`{new_name}` is already in use.")
            return

        images = await self.config.guild(guild).images()
        image = await self.get_image(old_name, guild)
        if not image:
            await ctx.send(f"`{old_name}` is not an image for this guild.")
            return

        images.remove(image)
        image["command_name"] = new_name
        images.append(image)
        await self.config.guild(guild).images.set(images)
        await ctx.send(f"Renamed `{old_name}` to `{new_name}`.")

    @checks.is_owner()
    @addimage.command(name="renameglobal")
    async def rename_global_image(self, ctx: commands.Context, old_name: str, new_name: str) -> None:
        """
        Rename a global image trigger.
        """
        guild = ctx.guild
        old_name = old_name.lower()
        new_name = new_name.lower()

        if old_name == new_name:
            await ctx.send("The new name must be different.")
            return

        if guild is not None and await self.check_command_exists(new_name, guild):
            await ctx.send(f"`{new_name}` is already in use.")
            return

        images = await self.config.images()
        image = await self.get_image(old_name)
        if not image:
            await ctx.send(f"`{old_name}` is not a global image.")
            return

        images.remove(image)
        image["command_name"] = new_name
        images.append(image)
        await self.config.images.set(images)
        await ctx.send(f"Renamed global image `{old_name}` to `{new_name}`.")

    async def save_image_location(
        self, msg: discord.Message, name: str, guild: Optional[discord.Guild] = None
    ) -> None:
        seed = "".join(random.sample(string.ascii_uppercase + string.digits, k=5))
        suffix = self._safe_storage_extension(msg.attachments[0].filename)
        filename = f"{seed}{suffix}"
        if guild is not None:
            directory = await self.get_directory(guild)
            cur_images = await self.config.guild(guild).images()
        else:
            directory = await self.get_directory()
            cur_images = await self.config.images()
        await self.make_guild_folder(directory)
        name = name.lower()

        file_path = directory / filename

        new_entry = {
            "command_name": name,
            "count": 0,
            "file_loc": filename,
            "author": msg.author.id,
        }

        cur_images.append(new_entry)
        await msg.attachments[0].save(file_path)
        if guild is not None:
            await self.config.guild(guild).images.set(cur_images)
        else:
            await self.config.images.set(cur_images)

    async def wait_for_image(self, ctx: commands.Context) -> Optional[discord.Message]:
        msg = None
        while msg is None:

            def check(m: discord.Message):
                return m.author == ctx.author and (m.attachments or m.content.lower().strip() == "exit")

            try:
                msg = await self.bot.wait_for("message", check=check, timeout=60)
            except asyncio.TimeoutError:
                await ctx.send("Image adding timed out.")
                break
            if msg.content.lower().strip() == "exit":
                await ctx.send("Image adding cancelled.")
                break
        return msg

    @addimage.command(name="add")
    @checks.mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(attach_files=True)
    async def add_image_guild(self, ctx: commands.Context, name: str) -> None:
        """
        Add an image to direct upload on this server

        `name` the command name used to post the image
        """
        guild = ctx.message.guild
        name = name.lower()
        if await self.check_command_exists(name, guild):
            msg = name + " is already in the list, try another!"
            return await ctx.send(msg)
        if ctx.message.attachments == []:
            msg = "Upload an image for me to use! Type `exit` to cancel."
            await ctx.send(msg)
            file_msg = await self.wait_for_image(ctx)
            if not file_msg or not file_msg.attachments:
                return
            error = await self.validate_attachment(file_msg.attachments[0])
            if error:
                await ctx.send(error)
                return
            await self.save_image_location(file_msg, name, guild)
        else:
            error = await self.validate_attachment(ctx.message.attachments[0])
            if error:
                await ctx.send(error)
                return
            await self.save_image_location(ctx.message, name, guild)
        await ctx.send(name + " has been added to my files!")

    @checks.is_owner()
    @addimage.command(name="addglobal")
    async def add_image_global(self, ctx: commands.Context, name: str) -> None:
        """
        Add an image to direct upload globally

        `name` the command name used to post the image
        """
        guild = ctx.message.guild
        name = name.lower()
        msg = ctx.message
        if await self.check_command_exists(name, guild):
            msg = name + " is already in the list, try another!"
            return await ctx.send(msg)
        if ctx.message.attachments == []:
            msg = "Upload an image for me to use! Type `exit` to cancel."
            await ctx.send(msg)
            file_msg = await self.wait_for_image(ctx)
            if not file_msg or not file_msg.attachments:
                return
            error = await self.validate_attachment(file_msg.attachments[0])
            if error:
                await ctx.send(error)
                return
            await self.save_image_location(file_msg, name)
        else:
            error = await self.validate_attachment(ctx.message.attachments[0])
            if error:
                await ctx.send(error)
                return
            await self.save_image_location(ctx.message, name)
        await ctx.send(name + " has been added to my files!")
