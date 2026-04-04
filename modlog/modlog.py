from collections import OrderedDict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import discord
from redbot.core import Config, commands


DEFAULT_AUDIT_WINDOW_SECONDS = 15
MAX_CACHED_MESSAGE_SNAPSHOTS = 5000
UNKNOWN_MESSAGE_CONTENT = "Unavailable (message was not cached at deletion time)"


class ModLog(commands.Cog):
    """Log core moderator actions to a configured channel."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=742904511, force_registration=True)
        self._message_cache = OrderedDict()
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

    @staticmethod
    def _prefix(ctx: commands.Context) -> str:
        return getattr(ctx, "clean_prefix", "[p]")

    def _normalize_name(self, value: Optional[str]) -> str:
        return self._truncate(value or "None", limit=256)

    def _role_sort_key(self, role):
        return getattr(role, "position", 0), getattr(role, "id", 0)

    def _role_map(self, member) -> dict:
        roles = getattr(member, "roles", None) or []
        result = {}
        for role in roles:
            role_id = getattr(role, "id", None)
            if role_id is None:
                continue
            result[role_id] = role
        return result

    def _role_label(self, role) -> str:
        mention = getattr(role, "mention", None)
        if mention:
            return mention
        return self._entity_label(role)

    def _format_role_list(self, roles) -> str:
        if not roles:
            return "None"
        ordered_roles = sorted(roles, key=self._role_sort_key, reverse=True)
        return ", ".join(self._role_label(role) for role in ordered_roles)

    def _message_has_visible_state(self, message) -> bool:
        return any(
            [
                bool(getattr(message, "content", None)),
                bool(getattr(message, "attachments", None)),
                bool(getattr(message, "embeds", None)),
                bool(getattr(message, "stickers", None)),
            ]
        )

    def _snapshot_count_list(self, items) -> list:
        count = len(items or [])
        return [None] * count

    def _store_message_snapshot(self, message):
        message_id = getattr(message, "id", None)
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        if message_id is None or guild is None or channel is None:
            return
        if getattr(author, "bot", False):
            return
        if not self._message_has_visible_state(message):
            return

        snapshot = SimpleNamespace(
            id=message_id,
            guild=guild,
            channel=SimpleNamespace(
                id=getattr(channel, "id", None),
                mention=getattr(channel, "mention", None) or f"<#{getattr(channel, 'id', 'unknown')}>",
                name=getattr(channel, "name", "unknown"),
            ),
            author=SimpleNamespace(
                id=getattr(author, "id", None),
                name=getattr(author, "name", None),
                display_name=getattr(author, "display_name", None),
            ),
            content=getattr(message, "content", None),
            attachments=self._snapshot_count_list(getattr(message, "attachments", None)),
            embeds=self._snapshot_count_list(getattr(message, "embeds", None)),
            stickers=self._snapshot_count_list(getattr(message, "stickers", None)),
            jump_url=getattr(message, "jump_url", None),
        )

        self._message_cache.pop(message_id, None)
        self._message_cache[message_id] = snapshot
        while len(self._message_cache) > MAX_CACHED_MESSAGE_SNAPSHOTS:
            self._message_cache.popitem(last=False)

    def _pop_message_snapshot(self, message_id: int):
        return self._message_cache.pop(message_id, None)

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

    async def _settings_message(self, guild, prefix: str) -> str:
        conf = self.config.guild(guild)
        channel_id = await conf.channel_id()
        enabled = await conf.enabled()
        audit_window = await conf.audit_window_seconds()
        channel_label = f"<#{channel_id}>" if channel_id else "Not set"

        if not channel_id:
            next_step = f"Next: run `{prefix}modlog here` in the channel you want to use."
        elif not enabled:
            next_step = f"Next: run `{prefix}modlog enable` to start logging there."
        else:
            next_step = f"Next: run `{prefix}modlog test` to verify the log channel."

        return (
            "ModLog settings\n"
            f"Enabled: `{enabled}`\n"
            f"Channel: {channel_label}\n"
            f"Audit window: `{audit_window}s`\n"
            f"{next_step}"
        )

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

    async def _send_member_event(self, guild, *, title: str, color: int, member, extra_fields=None):
        if await self._is_disabled_in_guild(guild):
            return

        embed = self._base_embed(title, color=color)
        embed.description = self._entity_label(member)

        for name, value in extra_fields or []:
            embed.add_field(name=name, value=value)

        await self._send_embed(guild, embed)

    async def _send_message_event(self, guild, *, title: str, color: int, message, before=None, after=None):
        if await self._is_disabled_in_guild(guild):
            return

        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        attachments = getattr(message, "attachments", []) or []
        embeds = getattr(message, "embeds", []) or []
        stickers = getattr(message, "stickers", []) or []

        embed = self._base_embed(title, color=color)
        channel_label = getattr(channel, "mention", None)
        if not channel_label:
            channel_id = getattr(channel, "id", None)
            if channel_id is not None:
                channel_label = f"<#{channel_id}>"
            else:
                channel_label = "#" + getattr(channel, "name", "unknown")

        embed.description = f"Channel: {channel_label}"
        embed.add_field(name="Author", value=self._entity_label(author))

        if before is not None:
            embed.add_field(name="Before", value=self._truncate(before),)
        if after is not None:
            embed.add_field(name="After", value=self._truncate(after),)
        if before is None and after is None:
            embed.add_field(name="Content", value=self._truncate(getattr(message, "content", None)))

        if attachments:
            embed.add_field(name="Attachments", value=str(len(attachments)))
        if embeds:
            embed.add_field(name="Embeds", value=str(len(embeds)))
        if stickers:
            embed.add_field(name="Stickers", value=str(len(stickers)))

        jump_url = getattr(message, "jump_url", None)
        if jump_url:
            embed.add_field(name="Jump", value=jump_url)

        message_id = getattr(message, "id", None)
        if message_id is not None:
            embed.set_footer(text=f"Message ID: {message_id}")

        await self._send_embed(guild, embed)

    async def _send_bulk_message_delete_event(self, guild, *, channel, count: int, message_ids=None):
        if await self._is_disabled_in_guild(guild):
            return

        embed = self._base_embed("Messages Bulk Deleted", color=0x6C757D)
        channel_label = getattr(channel, "mention", None)
        if not channel_label:
            channel_id = getattr(channel, "id", None)
            channel_label = f"<#{channel_id}>" if channel_id is not None else "#unknown"

        embed.description = f"Channel: {channel_label}"
        embed.add_field(name="Count", value=str(count))

        if message_ids:
            sample_ids = sorted(str(message_id) for message_id in list(message_ids)[:5])
            embed.add_field(name="Sample Message IDs", value=", ".join(sample_ids))

        await self._send_embed(guild, embed)

    async def _send_raw_delete_event(self, guild, channel_id: int, message_id: int):
        if await self._is_disabled_in_guild(guild):
            return

        snapshot = self._pop_message_snapshot(message_id)
        if snapshot is not None:
            await self._send_message_event(
                guild,
                title="Message Deleted",
                color=0x6C757D,
                message=snapshot,
            )
            return

        channel = getattr(guild, "get_channel", lambda _channel_id: None)(channel_id)
        channel_label = getattr(channel, "mention", None) or f"<#{channel_id}>"

        embed = self._base_embed("Message Deleted", color=0x6C757D)
        embed.description = f"Channel: {channel_label}"
        embed.add_field(name="Author", value="Unknown")
        embed.add_field(name="Content", value=UNKNOWN_MESSAGE_CONTENT)
        embed.set_footer(text=f"Message ID: {message_id}")

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
        if entry is not None:
            await self._send_mod_action(
                guild,
                title="Member Kicked",
                color=0xF0AD4E,
                target=member,
                moderator=getattr(entry, "user", None),
                reason=getattr(entry, "reason", None),
            )
            return

        extra_fields = []
        joined_at = getattr(member, "joined_at", None)
        if joined_at is not None:
            extra_fields.append(("Joined Server", self._format_dt(joined_at)))

        await self._send_member_event(
            guild,
            title="Member Left",
            color=0x6C757D,
            member=member,
            extra_fields=extra_fields,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member):
        extra_fields = []
        created_at = getattr(member, "created_at", None)
        if created_at is not None:
            extra_fields.append(("Account Created", self._format_dt(created_at)))

        await self._send_member_event(
            member.guild,
            title="Member Joined",
            color=0x5CB85C,
            member=member,
            extra_fields=extra_fields,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        guild = after.guild
        before_timeout = getattr(before, "timed_out_until", None)
        after_timeout = getattr(after, "timed_out_until", None)
        if before_timeout != after_timeout:
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

        before_nick = getattr(before, "nick", None)
        after_nick = getattr(after, "nick", None)
        if before_nick != after_nick:
            entry = await self._find_audit_entry(guild, "member_update", getattr(after, "id", 0))
            await self._send_mod_action(
                guild,
                title="Member Nickname Changed",
                color=0x5BC0DE,
                target=after,
                moderator=getattr(entry, "user", None),
                reason=getattr(entry, "reason", None),
                extra_fields=[
                    ("Before", self._normalize_name(before_nick)),
                    ("After", self._normalize_name(after_nick)),
                ],
            )

        before_roles = self._role_map(before)
        after_roles = self._role_map(after)
        added_roles = [after_roles[role_id] for role_id in after_roles.keys() - before_roles.keys()]
        removed_roles = [before_roles[role_id] for role_id in before_roles.keys() - after_roles.keys()]
        if added_roles or removed_roles:
            entry = await self._find_audit_entry(guild, "member_role_update", getattr(after, "id", 0))
            extra_fields = []
            if added_roles:
                extra_fields.append(("Added Roles", self._format_role_list(added_roles)))
            if removed_roles:
                extra_fields.append(("Removed Roles", self._format_role_list(removed_roles)))

            await self._send_mod_action(
                guild,
                title="Member Roles Updated",
                color=0x5BC0DE,
                target=after,
                moderator=getattr(entry, "user", None),
                reason=getattr(entry, "reason", None),
                extra_fields=extra_fields,
            )

    @commands.Cog.listener()
    async def on_message(self, message):
        if getattr(message, "guild", None) is None:
            return
        self._store_message_snapshot(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        guild = getattr(message, "guild", None)
        if guild is None:
            return

        message_id = getattr(message, "id", None)
        if message_id is not None:
            self._pop_message_snapshot(message_id)

        author = getattr(message, "author", None)
        if getattr(author, "bot", False):
            return

        if not self._message_has_visible_state(message):
            return

        await self._send_message_event(
            guild,
            title="Message Deleted",
            color=0x6C757D,
            message=message,
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if getattr(payload, "cached_message", None) is not None:
            return

        guild_id = getattr(payload, "guild_id", None)
        channel_id = getattr(payload, "channel_id", None)
        message_id = getattr(payload, "message_id", None)
        if guild_id is None or channel_id is None or message_id is None:
            return

        guild = getattr(self.bot, "get_guild", lambda _guild_id: None)(guild_id)
        if guild is None:
            return

        await self._send_raw_delete_event(guild, channel_id, message_id)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        messages = list(messages or [])
        if not messages:
            return

        first_message = messages[0]
        guild = getattr(first_message, "guild", None)
        channel = getattr(first_message, "channel", None)
        if guild is None or channel is None:
            return

        for message in messages:
            message_id = getattr(message, "id", None)
            if message_id is not None:
                self._pop_message_snapshot(message_id)

        await self._send_bulk_message_delete_event(guild, channel=channel, count=len(messages))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        cached_messages = getattr(payload, "cached_messages", None)
        if cached_messages:
            return

        guild_id = getattr(payload, "guild_id", None)
        channel_id = getattr(payload, "channel_id", None)
        message_ids = getattr(payload, "message_ids", None)
        if guild_id is None or channel_id is None or not message_ids:
            return

        guild = getattr(self.bot, "get_guild", lambda _guild_id: None)(guild_id)
        if guild is None:
            return

        channel = getattr(guild, "get_channel", lambda _channel_id: None)(channel_id)
        for message_id in message_ids:
            self._pop_message_snapshot(message_id)

        await self._send_bulk_message_delete_event(
            guild,
            channel=channel,
            count=len(message_ids),
            message_ids=message_ids,
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        guild = getattr(after, "guild", None)
        if guild is None:
            return

        author = getattr(after, "author", None)
        if getattr(author, "bot", False):
            return

        self._store_message_snapshot(after)

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
        assert ctx.guild
        await ctx.send(await self._settings_message(ctx.guild, self._prefix(ctx)))

    @modlog.command(name="show")
    async def modlog_show(self, ctx: commands.Context):
        """Show current mod-log settings."""
        assert ctx.guild
        await ctx.send(await self._settings_message(ctx.guild, self._prefix(ctx)))

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

    @modlog.command(name="auditwindow", aliases=["window"])
    async def modlog_audit_window(self, ctx: commands.Context, seconds: int):
        """Set how long to search audit logs for matching actions."""
        assert ctx.guild
        seconds = max(0, seconds)
        await self.config.guild(ctx.guild).audit_window_seconds.set(seconds)
        await ctx.send(f"ModLog audit window set to `{seconds}s`.")

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
