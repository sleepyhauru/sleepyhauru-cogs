from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import discord
from redbot.core import Config, commands


MOVE_COOLDOWN_SECONDS = 10


class VoiceLog(commands.Cog):
    """Logs users joining and leaving a VC, inside the VC chat itself."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.allowed_guild_ids: Set[int] = set()
        self.session_starts: Dict[int, datetime] = {}
        self.last_move_at: Dict[int, datetime] = {}
        self.config = Config.get_conf(self, identifier=7669636567)
        self.config.register_guild(
            enabled=False,
            log_joins=True,
            log_leaves=True,
            log_moves=True,
            move_cooldown_seconds=MOVE_COOLDOWN_SECONDS,
        )

    async def cog_load(self):
        all_config = await self.config.all_guilds()
        self.allowed_guild_ids = {
            guild_id for guild_id, conf in all_config.items() if conf.get("enabled", False)
        }

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    async def _get_guild_settings(self, guild: discord.Guild) -> dict:
        conf = self.config.guild(guild)
        return {
            "enabled": await conf.enabled(),
            "log_joins": await conf.log_joins(),
            "log_leaves": await conf.log_leaves(),
            "log_moves": await conf.log_moves(),
            "move_cooldown_seconds": await conf.move_cooldown_seconds(),
        }

    def _format_duration(self, started_at: Optional[datetime], ended_at: datetime) -> Optional[str]:
        if started_at is None:
            return None
        elapsed = ended_at - started_at
        if elapsed <= timedelta(0):
            return None

        total_seconds = int(elapsed.total_seconds())
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)

        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    def _should_log_event(self, before_channel, after_channel, settings: dict, now: datetime, member_id: int) -> bool:
        if before_channel is None and after_channel is not None:
            return settings["log_joins"]
        if before_channel is not None and after_channel is None:
            return settings["log_leaves"]
        if before_channel is not None and after_channel is not None:
            if not settings["log_moves"]:
                return False
            last_move_at = self.last_move_at.get(member_id)
            if last_move_at is not None:
                cooldown = timedelta(seconds=settings["move_cooldown_seconds"])
                if now - last_move_at < cooldown:
                    return False
            return True
        return False

    def _build_voice_embed(
        self,
        member: discord.Member,
        before_channel,
        after_channel,
        now: datetime,
    ) -> discord.Embed:
        embed = discord.Embed(color=member.color, timestamp=now)
        if before_channel is None:
            embed.set_author(name="Connected", icon_url=member.display_avatar.url)
            embed.description = f"{member.mention} has joined {after_channel.mention}"
            return embed

        if after_channel is None:
            embed.set_author(name="Disconnected", icon_url=member.display_avatar.url)
            embed.description = f"{member.mention} has left {before_channel.mention}"
            duration = self._format_duration(self.session_starts.pop(member.id, None), now)
            if duration:
                embed.set_footer(text=f"Session length: {duration}")
            return embed

        embed.set_author(name="Moved", icon_url=member.display_avatar.url)
        embed.description = (
            f"{member.mention} has moved from {before_channel.mention} to {after_channel.mention}"
        )
        return embed

    def _get_target_channels(self, before_channel, after_channel) -> List:
        targets = []
        seen_ids = set()
        for channel in (before_channel, after_channel):
            if channel is None:
                continue
            channel_id = getattr(channel, "id", id(channel))
            if channel_id in seen_ids:
                continue
            seen_ids.add(channel_id)
            perms = channel.permissions_for(channel.guild.me)
            if not perms.send_messages or not perms.embed_links:
                continue
            targets.append(channel)
        return targets

    async def _send_to_channels(self, channels: List, embed: discord.Embed):
        for channel in channels:
            try:
                await channel.send(embed=embed)
            except discord.DiscordException:
                continue

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        if guild.id not in self.allowed_guild_ids:
            return
        if before.channel == after.channel:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return

        settings = await self._get_guild_settings(guild)
        now = self._utcnow()
        if not self._should_log_event(before.channel, after.channel, settings, now, member.id):
            if before.channel is None and after.channel is not None:
                self.session_starts[member.id] = now
            return

        if before.channel is None and after.channel is not None:
            self.session_starts[member.id] = now
        elif before.channel is not None and after.channel is not None:
            self.last_move_at[member.id] = now
        elif after.channel is None:
            self.last_move_at.pop(member.id, None)

        embed = self._build_voice_embed(member, before.channel, after.channel, now)
        targets = self._get_target_channels(before.channel, after.channel)
        await self._send_to_channels(targets, embed)

    @commands.group(invoke_without_command=True)  # type: ignore
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def voicelog(self, ctx: commands.Context):
        """Voice Log configuration"""
        await ctx.send_help()

    @voicelog.command(name="enable")
    async def voicelog_enable(self, ctx: commands.Context):
        """Enable voice log for the whole guild."""
        assert ctx.guild
        self.allowed_guild_ids.add(ctx.guild.id)
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.tick(message="Voice Log enabled")

    @voicelog.command(name="disable")
    async def voicelog_disable(self, ctx: commands.Context):
        """Disable voice log for the whole guild."""
        assert ctx.guild
        self.allowed_guild_ids.discard(ctx.guild.id)
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.tick(message="Voice Log disabled")

    @voicelog.command(name="show")
    async def voicelog_show(self, ctx: commands.Context):
        """Show current Voice Log settings for this guild."""
        assert ctx.guild
        settings = await self._get_guild_settings(ctx.guild)
        message = (
            f"Voice Log settings\n"
            f"Enabled: `{settings['enabled']}`\n"
            f"Join logs: `{settings['log_joins']}`\n"
            f"Leave logs: `{settings['log_leaves']}`\n"
            f"Move logs: `{settings['log_moves']}`\n"
            f"Move cooldown: `{settings['move_cooldown_seconds']}s`"
        )
        await ctx.send(message)

    @voicelog.command(name="joins")
    async def voicelog_joins(self, ctx: commands.Context, enabled: bool):
        """Enable or disable join logs."""
        assert ctx.guild
        await self.config.guild(ctx.guild).log_joins.set(enabled)
        await ctx.tick(message=f"Voice Log join events {'enabled' if enabled else 'disabled'}")

    @voicelog.command(name="leaves")
    async def voicelog_leaves(self, ctx: commands.Context, enabled: bool):
        """Enable or disable leave logs."""
        assert ctx.guild
        await self.config.guild(ctx.guild).log_leaves.set(enabled)
        await ctx.tick(message=f"Voice Log leave events {'enabled' if enabled else 'disabled'}")

    @voicelog.command(name="moves")
    async def voicelog_moves(self, ctx: commands.Context, enabled: bool):
        """Enable or disable move logs."""
        assert ctx.guild
        await self.config.guild(ctx.guild).log_moves.set(enabled)
        await ctx.tick(message=f"Voice Log move events {'enabled' if enabled else 'disabled'}")

    @voicelog.command(name="cooldown")
    async def voicelog_cooldown(self, ctx: commands.Context, seconds: int):
        """Set the move log cooldown in seconds."""
        assert ctx.guild
        seconds = max(0, seconds)
        await self.config.guild(ctx.guild).move_cooldown_seconds.set(seconds)
        await ctx.tick(message=f"Voice Log move cooldown set to {seconds}s")
