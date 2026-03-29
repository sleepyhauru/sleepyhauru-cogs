from datetime import datetime, timezone
from typing import Optional

import discord
from redbot.core import Config, commands


DEFAULT_AUDIT_WINDOW_SECONDS = 15


class ModLog(commands.Cog):
    """Log core moderator actions to a configured channel."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=742904511, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channel_id=None,
            audit_window_seconds=DEFAULT_AUDIT_WINDOW_SECONDS,
        )

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _safe_name(self, entity) -> str:
        if entity is None:
            return "Unknown"
        return getattr(entity, "display_name", None) or getattr(entity, "name", None) or str(entity)

    def _entity_label(self, entity) -> str:
        if entity is None:
            return "Unknown"
        entity_id = getattr(entity, "id", None)
        if entity_id is None:
            return self._safe_name(entity)
        return f"{self._safe_name(entity)} ({entity_id})"

    def _format_dt(self, value) -> str:
        if value is None:
            return "Unknown"
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _truncate(self, value: Optional[str], limit: int = 1000) -> str:
        if not value:
            return "None"
        compact = " ".join(str(value).split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    async def _is_disabled_in_guild(self, guild) -> bool:
        result = self.bot.cog_disabled_in_guild(self, guild)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    async def _get_log_channel(self, guild):
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return None

        channel_id = await conf.channel_id()
        if not channel_id:
            return None

        getter = getattr(guild, "get_channel", None)
        if getter is None:
            return None
        return getter(channel_id)

    def _audit_action(self, name: str):
        audit_log_action = getattr(discord, "AuditLogAction", None)
        if audit_log_action is None:
            return None
        return getattr(audit_log_action, name, None)

    async def _find_audit_entry(self, guild, action_name: str, target_id: int):
        action = self._audit_action(action_name)
        if action is None or not hasattr(guild, "audit_logs"):
            return None

        try:
            entries = guild.audit_logs(limit=5, action=action)
            async for entry in entries:
                entry_target = getattr(entry, "target", None)
                entry_target_id = getattr(entry_target, "id", entry_target)
                if entry_target_id != target_id:
                    continue

                created_at = getattr(entry, "created_at", None)
                if created_at is not None:
                    if getattr(created_at, "tzinfo", None) is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    max_age = await self.config.guild(guild).audit_window_seconds()
                    age = (self._utcnow() - created_at).total_seconds()
                    if age > max_age:
                        continue

                return entry
        except (discord.Forbidden, discord.HTTPException, TypeError, AttributeError):
            return None

        return None

    def _base_embed(self, title: str, *, color: int) -> discord.Embed:
        return discord.Embed(title=title, color=color, timestamp=self._utcnow())

    async def _send_embed(self, guild, embed: discord.Embed):
        channel = await self._get_log_channel(guild)
        if channel is None:
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            return

    async def _send_mod_action(
        self,
        guild,
        *,
        title: str,
        color: int,
        target,
        moderator=None,
        reason: Optional[str] = None,
        extra_fields=None,
    ):
        if await self._is_disabled_in_guild(guild):
            return

        embed = self._base_embed(title, color=color)
        embed.description = self._entity_label(target)
        embed.add_field(name="Moderator", value=self._entity_label(moderator))
        embed.add_field(name="Reason", value=reason or "No reason provided")

        for name, value in extra_fields or []:
            embed.add_field(name=name, value=value)

        await self._send_embed(guild, embed)

    async def _send_message_event(self, guild, *, title: str, color: int, message, before=None, after=None):
        if await self._is_disabled_in_guild(guild):
            return

        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        attachments = getattr(message, "attachments", []) or []

        embed = self._base_embed(title, color=color)
        embed.description = f"Channel: {getattr(channel, 'mention', '#' + getattr(channel, 'name', 'unknown'))}"
        embed.add_field(name="Author", value=self._entity_label(author))

        if before is not None:
            embed.add_field(name="Before", value=self._truncate(before),)
        if after is not None:
            embed.add_field(name="After", value=self._truncate(after),)
        if before is None and after is None:
            embed.add_field(name="Content", value=self._truncate(getattr(message, "content", None)))

        if attachments:
            embed.add_field(name="Attachments", value=str(len(attachments)))

        jump_url = getattr(message, "jump_url", None)
        if jump_url:
            embed.add_field(name="Jump", value=jump_url)

        await self._send_embed(guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        entry = await self._find_audit_entry(guild, "ban", getattr(user, "id", 0))
        await self._send_mod_action(
            guild,
            title="Member Banned",
            color=0xD9534F,
            target=user,
            moderator=getattr(entry, "user", None),
            reason=getattr(entry, "reason", None),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        entry = await self._find_audit_entry(guild, "unban", getattr(user, "id", 0))
        await self._send_mod_action(
            guild,
            title="Member Unbanned",
            color=0x5CB85C,
            target=user,
            moderator=getattr(entry, "user", None),
            reason=getattr(entry, "reason", None),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        guild = member.guild
        entry = await self._find_audit_entry(guild, "kick", getattr(member, "id", 0))
        if entry is None:
            return

        await self._send_mod_action(
            guild,
            title="Member Kicked",
            color=0xF0AD4E,
            target=member,
            moderator=getattr(entry, "user", None),
            reason=getattr(entry, "reason", None),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        before_timeout = getattr(before, "timed_out_until", None)
        after_timeout = getattr(after, "timed_out_until", None)
        if before_timeout == after_timeout:
            return

        guild = after.guild
        entry = await self._find_audit_entry(guild, "member_update", getattr(after, "id", 0))

        if before_timeout is None and after_timeout is not None:
            title = "Member Timed Out"
            extra_fields = [("Until", self._format_dt(after_timeout))]
        elif before_timeout is not None and after_timeout is None:
            title = "Member Timeout Removed"
            extra_fields = [("Previous Timeout", self._format_dt(before_timeout))]
        else:
            title = "Member Timeout Updated"
            extra_fields = [
                ("Previous Timeout", self._format_dt(before_timeout)),
                ("New Timeout", self._format_dt(after_timeout)),
            ]

        await self._send_mod_action(
            guild,
            title=title,
            color=0x5BC0DE,
            target=after,
            moderator=getattr(entry, "user", None),
            reason=getattr(entry, "reason", None),
            extra_fields=extra_fields,
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        guild = getattr(message, "guild", None)
        if guild is None:
            return

        author = getattr(message, "author", None)
        if getattr(author, "bot", False):
            return

        if not getattr(message, "content", None) and not getattr(message, "attachments", None):
            return

        await self._send_message_event(
            guild,
            title="Message Deleted",
            color=0x6C757D,
            message=message,
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        guild = getattr(after, "guild", None)
        if guild is None:
            return

        author = getattr(after, "author", None)
        if getattr(author, "bot", False):
            return

        before_content = getattr(before, "content", None) or ""
        after_content = getattr(after, "content", None) or ""
        if before_content == after_content:
            return

        await self._send_message_event(
            guild,
            title="Message Edited",
            color=0x9370DB,
            message=after,
            before=before_content,
            after=after_content,
        )

    @commands.group(name="modlog", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def modlog(self, ctx: commands.Context):
        """Configure the moderation log channel."""
        await self.modlog_show(ctx)

    @modlog.command(name="show")
    async def modlog_show(self, ctx: commands.Context):
        """Show current mod-log settings."""
        assert ctx.guild
        conf = self.config.guild(ctx.guild)
        channel_id = await conf.channel_id()
        channel_label = f"<#{channel_id}>" if channel_id else "Not set"
        message = (
            "ModLog settings\n"
            f"Enabled: `{await conf.enabled()}`\n"
            f"Channel: {channel_label}\n"
            f"Audit window: `{await conf.audit_window_seconds()}s`"
        )
        await ctx.send(message)

    @modlog.command(name="here")
    async def modlog_here(self, ctx: commands.Context):
        """Set the current channel as the mod-log channel and enable logging."""
        assert ctx.guild
        await self.config.guild(ctx.guild).channel_id.set(ctx.channel.id)
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send(f"ModLog channel set to <#{ctx.channel.id}> and enabled.")

    @modlog.command(name="enable")
    async def modlog_enable(self, ctx: commands.Context):
        """Enable mod logging for this guild."""
        assert ctx.guild
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("ModLog enabled.")

    @modlog.command(name="disable")
    async def modlog_disable(self, ctx: commands.Context):
        """Disable mod logging for this guild."""
        assert ctx.guild
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("ModLog disabled.")

    @modlog.command(name="test")
    async def modlog_test(self, ctx: commands.Context):
        """Send a test entry to the configured mod-log channel."""
        assert ctx.guild
        await self._send_mod_action(
            ctx.guild,
            title="ModLog Test",
            color=0x5865F2,
            target=ctx.author,
            moderator=ctx.author,
            reason="Manual test entry",
        )
        await ctx.send("Sent a test mod-log entry if the channel is configured.")
