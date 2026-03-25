import importlib
import sys
import types
from pathlib import Path


def install_stubs():
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")

        class DiscordException(Exception):
            pass

        class HTTPException(DiscordException):
            pass

        class Forbidden(DiscordException):
            pass

        class NotFound(DiscordException):
            pass

        class Message:
            def __init__(self, content="", embeds=None):
                self.content = content
                self.embeds = embeds or []

        class Attachment:
            pass

        class Member:
            pass

        class VoiceState:
            pass

        class Embed:
            def __init__(self, title=None, description=None, color=None, timestamp=None):
                self.title = title
                self.description = description
                self.color = color
                self.timestamp = timestamp
                self.fields = []
                self.footer = None

            def add_field(self, *, name, value):
                self.fields.append(types.SimpleNamespace(name=name, value=value))

            def set_footer(self, *, text):
                self.footer = text

            def set_author(self, **kwargs):
                self.author = kwargs

        class SelectOption:
            def __init__(self, label, value):
                self.label = label
                self.value = value

        class File:
            def __init__(self, fp):
                self.fp = fp

        class Emoji:
            pass

        class PartialEmoji:
            def __init__(self, name, animated=False, id=None):
                self.name = name
                self.animated = animated
                self.id = id
                ext = "gif" if animated else "png"
                self.url = f"https://cdn.discordapp.com/emojis/{id or 0}.{ext}"

            @classmethod
            def from_str(cls, value):
                import re

                match = re.match(r"<(a?):(\w+):(\d{10,20})>", value)
                animated = bool(match.group(1))
                return cls(name=match.group(2), animated=animated, id=int(match.group(3)))

            def __hash__(self):
                return hash((self.name, self.animated, self.id))

            def __eq__(self, other):
                return (
                    isinstance(other, PartialEmoji)
                    and self.name == other.name
                    and self.animated == other.animated
                    and self.id == other.id
                )

        class StickerItem:
            pass

        class AllowedMentions:
            @staticmethod
            def none():
                return "none"

        class Color:
            @staticmethod
            def blurple():
                return "blurple"

        class Interaction:
            pass

        class Guild:
            def __init__(self, emojis=None, emoji_limit=50):
                self.emojis = emojis or []
                self.emoji_limit = emoji_limit

        discord.DiscordException = DiscordException
        discord.HTTPException = HTTPException
        discord.Forbidden = Forbidden
        discord.NotFound = NotFound
        discord.Message = Message
        discord.Attachment = Attachment
        discord.Member = Member
        discord.VoiceState = VoiceState
        discord.Embed = Embed
        discord.SelectOption = SelectOption
        discord.File = File
        discord.Emoji = Emoji
        discord.PartialEmoji = PartialEmoji
        discord.StickerItem = StickerItem
        discord.AllowedMentions = AllowedMentions
        discord.Color = Color
        discord.Interaction = Interaction
        discord.Guild = Guild
        discord.errors = types.SimpleNamespace(Forbidden=Forbidden)
        discord.version_info = types.SimpleNamespace(major=2)

        ui = types.ModuleType("discord.ui")

        class Select:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.values = []
                self.view = None

        class View:
            def __init__(self, timeout=None):
                self.timeout = timeout
                self.children = []

            def add_item(self, item):
                item.view = self
                self.children.append(item)

        ui.Select = Select
        ui.View = View
        discord.ui = ui

        sys.modules["discord"] = discord
        sys.modules["discord.ui"] = ui

    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")

        class ClientError(Exception):
            pass

        class ClientTimeout:
            def __init__(self, total=None):
                self.total = total

        class ClientSession:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        aiohttp.ClientError = ClientError
        aiohttp.ClientTimeout = ClientTimeout
        aiohttp.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp

    if "redbot.core" not in sys.modules:
        redbot = types.ModuleType("redbot")
        core = types.ModuleType("redbot.core")
        utils = types.ModuleType("redbot.core.utils")
        menus = types.ModuleType("redbot.core.utils.menus")
        checks = types.ModuleType("redbot.core.checks")
        app_commands = types.ModuleType("redbot.core.app_commands")
        data_manager = types.ModuleType("redbot.core.data_manager")

        class ConfigValue:
            def __init__(self, store, key):
                self.store = store
                self.key = key

            async def __call__(self):
                return self.store[self.key]

            async def set(self, value):
                self.store[self.key] = value

        class ConfigInstance:
            def __init__(self):
                self._global_store = {}
                self._guild_defaults = {}
                self._guild_store = {}

            def register_global(self, **kwargs):
                for key, value in kwargs.items():
                    if key not in self._global_store:
                        self._global_store[key] = value

            def register_guild(self, **kwargs):
                for key, value in kwargs.items():
                    if key not in self._guild_defaults:
                        self._guild_defaults[key] = value

            def guild(self, guild):
                guild_id = getattr(guild, "id", guild)
                if guild_id not in self._guild_store:
                    self._guild_store[guild_id] = dict(self._guild_defaults)
                return types.SimpleNamespace(
                    **{
                        key: ConfigValue(self._guild_store[guild_id], key)
                        for key in self._guild_store[guild_id]
                    }
                )

            def guild_from_id(self, guild_id):
                return self.guild(guild_id)

            async def all_guilds(self):
                return self._guild_store

            def __getattr__(self, item):
                if item in self._global_store:
                    return ConfigValue(self._global_store, item)
                raise AttributeError(item)

        class Config:
            @staticmethod
            def get_conf(*args, **kwargs):
                return ConfigInstance()

        class Cog:
            def format_help_for_context(self, ctx):
                return ""

            @staticmethod
            def listener(*args, **kwargs):
                def wrap(func):
                    return func

                return wrap

        class Command:
            pass

        class Group(Command):
            def __init__(self):
                self.commands = []

        class Context:
            pass

        class BucketType:
            user = "user"

        def _decorator(*args, **kwargs):
            def wrap(func):
                return func

            return wrap

        def command(*args, **kwargs):
            return _decorator(*args, **kwargs)

        def hybrid_command(*args, **kwargs):
            return _decorator(*args, **kwargs)

        def group(*args, **kwargs):
            def wrap(func):
                def subcommand_decorator(*dargs, **dkwargs):
                    def subwrap(subfunc):
                        return subfunc

                    return subwrap

                func.command = subcommand_decorator
                return func

            return wrap

        commands = types.ModuleType("redbot.core.commands")
        commands.Cog = Cog
        commands.Command = Command
        commands.Group = Group
        commands.Context = Context
        commands.BucketType = BucketType
        commands.command = command
        commands.hybrid_command = hybrid_command
        commands.group = group
        commands.is_owner = _decorator
        commands.cooldown = _decorator
        commands.guild_only = _decorator
        commands.bot_has_permissions = _decorator
        commands.has_permissions = _decorator

        core.Config = Config
        core.commands = commands
        core.checks = checks
        core.app_commands = app_commands
        utils.get_end_user_data_statement = lambda path: "This cog does not store user data."
        menus.DEFAULT_CONTROLS = {}

        async def menu(ctx, pages, controls):
            return None

        menus.menu = menu

        class ContextMenu:
            def __init__(self, name, callback):
                self.name = name
                self.callback = callback
                self.type = "context_menu"

        class _Checks:
            @staticmethod
            def has_permissions(**kwargs):
                return _decorator()

            @staticmethod
            def bot_has_permissions(**kwargs):
                return _decorator()

        app_commands.ContextMenu = ContextMenu
        app_commands.guild_only = _decorator
        app_commands.checks = _Checks

        checks.is_owner = _decorator
        checks.mod_or_permissions = _decorator
        checks.guildowner = _decorator

        data_manager.cog_data_path = lambda cog: Path("/tmp/codex-cog-data")
        redbot.core = core

        sys.modules["redbot"] = redbot
        sys.modules["redbot.core"] = core
        sys.modules["redbot.core.commands"] = commands
        sys.modules["redbot.core.utils"] = utils
        sys.modules["redbot.core.utils.menus"] = menus
        sys.modules["redbot.core.checks"] = checks
        sys.modules["redbot.core.app_commands"] = app_commands
        sys.modules["redbot.core.data_manager"] = data_manager

    if "red_commons.logging" not in sys.modules:
        red_commons = types.ModuleType("red_commons")
        logging = types.ModuleType("red_commons.logging")

        class Logger:
            def error(self, *args, **kwargs):
                return None

        logging.getLogger = lambda name: Logger()
        sys.modules["red_commons"] = red_commons
        sys.modules["red_commons.logging"] = logging

    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        image = types.ModuleType("PIL.Image")
        imageenhance = types.ModuleType("PIL.ImageEnhance")

        class UnidentifiedImageError(Exception):
            pass

        class ImageClass:
            pass

        class _Resampling:
            BILINEAR = 1

        image.Image = ImageClass
        image.Resampling = _Resampling
        image.BILINEAR = 1
        image.open = lambda data: None
        imageenhance.Sharpness = lambda img: None
        imageenhance.Contrast = lambda img: None
        imageenhance.Brightness = lambda img: None

        pil.Image = image
        pil.ImageEnhance = imageenhance
        pil.UnidentifiedImageError = UnidentifiedImageError

        sys.modules["PIL"] = pil
        sys.modules["PIL.Image"] = image
        sys.modules["PIL.ImageEnhance"] = imageenhance


def load_module(module_name):
    install_stubs()
    return importlib.import_module(module_name)
