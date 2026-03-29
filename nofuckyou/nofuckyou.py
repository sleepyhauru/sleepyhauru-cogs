import inspect
import random
import re
import time

import discord
from redbot.core import Config, commands


DEFAULT_RESPONSE_CHANCE = 0.5
DEFAULT_THIRSTY_CHANCE = 0.1
DEFAULT_COOLDOWN_SECONDS = 10
TRIGGER_RE = re.compile(r"\bf(?:u|uc|uck|uk|k|ck)\s+you\b", re.IGNORECASE)


class NoFuckYou(commands.Cog):
    """Reply to "fuck you" with "No fuck you"."""

    def __init__(self, bot):
        self.bot = bot
        self.last_response_at = {}
        self.config = Config.get_conf(self, identifier=661027401, force_registration=True)
        self.config.register_guild(
            enabled=False,
            response_chance=DEFAULT_RESPONSE_CHANCE,
            thirsty_chance=DEFAULT_THIRSTY_CHANCE,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
            trigger_count=0,
            reply_count=0,
            thirsty_count=0,
            send_error_count=0,
        )

    def _message_text(self, message: discord.Message) -> str:
        return getattr(message, "clean_content", None) or getattr(message, "content", "") or ""

    def _contains_trigger(self, text: str) -> bool:
        return bool(TRIGGER_RE.search(text))

    async def _is_disabled_in_guild(self, guild) -> bool:
        result = self.bot.cog_disabled_in_guild(self, guild)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def _now(self) -> float:
        return time.monotonic()

    def _channel_id(self, channel) -> int:
        return getattr(channel, "id", id(channel))

    def _on_cooldown(self, channel_id: int, cooldown_seconds: int) -> bool:
        last_response_at = self.last_response_at.get(channel_id)
        if last_response_at is None:
            return False
        return self._now() - last_response_at < cooldown_seconds

    def _mark_response(self, channel_id: int):
        self.last_response_at[channel_id] = self._now()

    async def _increment_guild_counter(self, guild, key: str):
        conf_value = getattr(self.config.guild(guild), key)
        current = await conf_value()
        await conf_value.set(current + 1)

    def _pick_response(self, thirsty_chance: float) -> str:
        if random.random() < thirsty_chance:
            return "Please fuck me :pleading_face:"
        return "No fuck you"

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if getattr(getattr(message, "author", None), "bot", False):
            return

        guild = getattr(message, "guild", None)
        if guild is None:
            return

        if not self._contains_trigger(self._message_text(message)):
            return

        if await self._is_disabled_in_guild(guild):
            return

        conf = self.config.guild(guild)
        if not await conf.enabled():
            return

        await self._increment_guild_counter(guild, "trigger_count")

        channel_id = self._channel_id(message.channel)
        if self._on_cooldown(channel_id, await conf.cooldown_seconds()):
            return

        if random.random() >= await conf.response_chance():
            return

        response = self._pick_response(await conf.thirsty_chance())
        try:
            await message.channel.send(response, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await self._increment_guild_counter(guild, "send_error_count")
            return

        self._mark_response(channel_id)
        await self._increment_guild_counter(guild, "reply_count")
        if response != "No fuck you":
            await self._increment_guild_counter(guild, "thirsty_count")

    @commands.group(name="nofuckyou", aliases=["nofuckyouset"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def nofuckyouset(self, ctx: commands.Context):
        """Configure No Fuck You for this guild."""
        await self.nofuckyouset_show(ctx)

    @nofuckyouset.command(name="show")
    async def nofuckyouset_show(self, ctx: commands.Context):
        """Show current No Fuck You settings."""
        assert ctx.guild
        conf = self.config.guild(ctx.guild)
        message = (
            "No Fuck You settings\n"
            f"Enabled: `{await conf.enabled()}`\n"
            f"Response chance: `{await conf.response_chance():.2f}`\n"
            f"Thirsty chance: `{await conf.thirsty_chance():.2f}`\n"
            f"Cooldown: `{await conf.cooldown_seconds()}s`"
        )
        await ctx.send(message)

    @nofuckyouset.command(name="enable")
    async def nofuckyouset_enable(self, ctx: commands.Context):
        """Enable No Fuck You in this guild."""
        assert ctx.guild
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("No Fuck You enabled.")

    @nofuckyouset.command(name="disable")
    async def nofuckyouset_disable(self, ctx: commands.Context):
        """Disable No Fuck You in this guild."""
        assert ctx.guild
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("No Fuck You disabled.")

    @nofuckyouset.command(name="chance")
    async def nofuckyouset_chance(self, ctx: commands.Context, chance: float):
        """Set the overall response chance from 0 to 1."""
        assert ctx.guild
        chance = max(0.0, min(1.0, chance))
        await self.config.guild(ctx.guild).response_chance.set(chance)
        await ctx.send(f"Response chance set to `{chance:.2f}`.")

    @nofuckyouset.command(name="cooldown")
    async def nofuckyouset_cooldown(self, ctx: commands.Context, seconds: int):
        """Set the per-channel response cooldown in seconds."""
        assert ctx.guild
        seconds = max(0, seconds)
        await self.config.guild(ctx.guild).cooldown_seconds.set(seconds)
        await ctx.send(f"Cooldown set to `{seconds}s`.")

    @nofuckyouset.command(name="thirsty")
    async def nofuckyouset_thirsty(self, ctx: commands.Context, chance: float):
        """Set the thirsty reply chance from 0 to 1."""
        assert ctx.guild
        chance = max(0.0, min(1.0, chance))
        await self.config.guild(ctx.guild).thirsty_chance.set(chance)
        await ctx.send(f"Thirsty chance set to `{chance:.2f}`.")

    @nofuckyouset.command(name="stats")
    async def nofuckyouset_stats(self, ctx: commands.Context):
        """Show No Fuck You response stats for this guild."""
        assert ctx.guild
        conf = self.config.guild(ctx.guild)
        message = (
            "No Fuck You stats\n"
            f"Triggers seen: `{await conf.trigger_count()}`\n"
            f"Replies sent: `{await conf.reply_count()}`\n"
            f"Thirsty replies: `{await conf.thirsty_count()}`\n"
            f"Send errors: `{await conf.send_error_count()}`"
        )
        await ctx.send(message)
