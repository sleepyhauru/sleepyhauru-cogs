import types
import unittest
from datetime import datetime, timedelta, timezone

from tests.support import load_module


discord = load_module("discord")
discord.AuditLogAction = types.SimpleNamespace(
    ban="ban",
    unban="unban",
    kick="kick",
    member_update="member_update",
)
modlog_module = load_module("modlog.modlog")


class AsyncIterator:
    def __init__(self, values):
        self.values = list(values)

    def __aiter__(self):
        self._iter = iter(self.values)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class ModLogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def cog_disabled_in_guild(cog, guild):
            return False

        self.guilds = {}

        def get_guild(guild_id):
            return self.guilds.get(guild_id)

        self.bot = types.SimpleNamespace(cog_disabled_in_guild=cog_disabled_in_guild, get_guild=get_guild)
        self.cog = modlog_module.ModLog(self.bot)

    async def test_modlog_here_sets_channel_and_enables_logging(self):
        sent = []
        guild = types.SimpleNamespace(id=1)
        channel = types.SimpleNamespace(id=99)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, channel=channel, send=send)

        await self.cog.modlog_here(ctx)
        await self.cog.modlog_show(ctx)

        conf = self.cog.config.guild(guild)
        self.assertTrue(await conf.enabled())
        self.assertEqual(await conf.channel_id(), 99)
        self.assertEqual(sent[0], "ModLog channel set to <#99> and enabled.")
        self.assertIn("Channel: <#99>", sent[1])

    async def test_on_member_ban_sends_embed_with_audit_info(self):
        sent_embeds = []
        moderator = types.SimpleNamespace(id=50, name="Mod")
        target = types.SimpleNamespace(id=10, name="User")
        entry = types.SimpleNamespace(
            target=target,
            user=moderator,
            reason="Rule 1",
            created_at=datetime.now(timezone.utc),
        )

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=2,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, send=send),
            audit_logs=lambda **kwargs: AsyncIterator([entry]),
        )
        self.guilds[guild.id] = guild
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(123)

        await self.cog.on_member_ban(guild, target)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.title, "Member Banned")
        self.assertEqual(embed.description, "User (10)")
        self.assertEqual(embed.fields[0].name, "Moderator")
        self.assertEqual(embed.fields[0].value, "Mod (50)")
        self.assertEqual(embed.fields[1].value, "Rule 1")

    async def test_on_member_remove_ignores_regular_leaves(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=3,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, send=send),
            audit_logs=lambda **kwargs: AsyncIterator([]),
        )
        self.guilds[guild.id] = guild
        member = types.SimpleNamespace(id=20, name="User", guild=guild)
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(456)

        await self.cog.on_member_remove(member)

        self.assertEqual(sent_embeds, [])

    async def test_on_member_update_logs_timeout_changes(self):
        sent_embeds = []
        moderator = types.SimpleNamespace(id=55, name="Mod")
        target = types.SimpleNamespace(id=44, display_name="Target")
        entry = types.SimpleNamespace(
            target=target,
            user=moderator,
            reason="Cooling off",
            created_at=datetime.now(timezone.utc),
        )

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=4,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, send=send),
            audit_logs=lambda **kwargs: AsyncIterator([entry]),
        )
        self.guilds[guild.id] = guild
        before = types.SimpleNamespace(id=44, guild=guild, timed_out_until=None)
        after = types.SimpleNamespace(
            id=44,
            guild=guild,
            display_name="Target",
            timed_out_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(789)

        await self.cog.on_member_update(before, after)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.title, "Member Timed Out")
        self.assertEqual(embed.fields[0].value, "Mod (55)")
        self.assertEqual(embed.fields[1].value, "Cooling off")
        self.assertEqual(embed.fields[2].name, "Until")

    async def test_find_audit_entry_skips_stale_entries(self):
        target = types.SimpleNamespace(id=70, name="User")
        stale_entry = types.SimpleNamespace(
            target=target,
            user=types.SimpleNamespace(id=88, name="Mod"),
            reason="Old",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        guild = types.SimpleNamespace(
            id=5,
            get_channel=lambda channel_id: None,
            audit_logs=lambda **kwargs: AsyncIterator([stale_entry]),
        )
        self.guilds[guild.id] = guild
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(999)

        result = await self.cog._find_audit_entry(guild, "ban", 70)

        self.assertIsNone(result)

    async def test_on_message_delete_logs_content_and_attachments(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=6,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, send=send),
        )
        self.guilds[guild.id] = guild
        channel = types.SimpleNamespace(id=321, name="mods", mention="#mods")
        author = types.SimpleNamespace(id=90, name="User", bot=False)
        message = types.SimpleNamespace(
            id=555,
            guild=guild,
            channel=channel,
            author=author,
            content="deleted content",
            attachments=[object(), object()],
            embeds=[],
            stickers=[],
            jump_url="https://discord.test/jump",
        )
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(321)

        await self.cog.on_message_delete(message)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.title, "Message Deleted")
        self.assertEqual(embed.fields[0].name, "Author")
        self.assertEqual(embed.fields[1].name, "Content")
        self.assertEqual(embed.fields[1].value, "deleted content")
        self.assertEqual(embed.fields[2].name, "Attachments")
        self.assertEqual(embed.fields[2].value, "2")
        self.assertEqual(embed.footer, "Message ID: 555")

    async def test_on_message_delete_logs_embed_only_messages(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=66,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, send=send),
        )
        self.guilds[guild.id] = guild
        channel = types.SimpleNamespace(id=322, name="mods", mention="#mods")
        author = types.SimpleNamespace(id=92, name="User", bot=False)
        message = types.SimpleNamespace(
            id=556,
            guild=guild,
            channel=channel,
            author=author,
            content="",
            attachments=[],
            embeds=[object()],
            stickers=[],
            jump_url=None,
        )
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(322)

        await self.cog.on_message_delete(message)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.title, "Message Deleted")
        self.assertEqual(embed.fields[1].name, "Content")
        self.assertEqual(embed.fields[1].value, "None")
        self.assertEqual(embed.fields[2].name, "Embeds")
        self.assertEqual(embed.fields[2].value, "1")

    async def test_on_message_edit_logs_before_and_after(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=7,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, send=send),
        )
        self.guilds[guild.id] = guild
        channel = types.SimpleNamespace(id=654, name="mods", mention="#mods")
        author = types.SimpleNamespace(id=91, name="User", bot=False)
        before = types.SimpleNamespace(
            guild=guild,
            channel=channel,
            author=author,
            content="old content",
        )
        after = types.SimpleNamespace(
            guild=guild,
            channel=channel,
            author=author,
            content="new content",
            jump_url="https://discord.test/jump2",
        )
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(654)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.title, "Message Edited")
        self.assertEqual(embed.fields[1].name, "Before")
        self.assertEqual(embed.fields[1].value, "old content")
        self.assertEqual(embed.fields[2].name, "After")
        self.assertEqual(embed.fields[2].value, "new content")

    async def test_on_raw_message_delete_uses_cached_snapshot_when_message_was_seen(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=71,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, mention="#mods", send=send),
        )
        self.guilds[guild.id] = guild
        channel = types.SimpleNamespace(id=777, name="mods", mention="#mods")
        author = types.SimpleNamespace(id=93, name="User", display_name="User", bot=False)
        message = types.SimpleNamespace(
            id=9001,
            guild=guild,
            channel=channel,
            author=author,
            content="snapshot content",
            attachments=[object()],
            embeds=[],
            stickers=[],
            jump_url="https://discord.test/jump3",
        )
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(777)

        await self.cog.on_message(message)
        payload = types.SimpleNamespace(
            guild_id=71,
            channel_id=777,
            message_id=9001,
            cached_message=None,
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.fields[0].value, "User (93)")
        self.assertEqual(embed.fields[1].value, "snapshot content")
        self.assertEqual(embed.fields[2].name, "Attachments")
        self.assertEqual(embed.fields[2].value, "1")
        self.assertEqual(embed.fields[3].name, "Jump")
        self.assertEqual(embed.fields[3].value, "https://discord.test/jump3")
        self.assertEqual(embed.footer, "Message ID: 9001")

    async def test_on_message_edit_updates_snapshot_used_by_raw_delete(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=72,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, mention="#mods", send=send),
        )
        self.guilds[guild.id] = guild
        channel = types.SimpleNamespace(id=778, name="mods", mention="#mods")
        author = types.SimpleNamespace(id=94, name="User", display_name="User", bot=False)
        before_seen = types.SimpleNamespace(
            id=9002,
            guild=guild,
            channel=channel,
            author=author,
            content="first version",
            attachments=[],
            embeds=[],
            stickers=[],
            jump_url=None,
        )
        before_edit = types.SimpleNamespace(
            guild=guild,
            channel=channel,
            author=author,
            content="first version",
        )
        after_edit = types.SimpleNamespace(
            id=9002,
            guild=guild,
            channel=channel,
            author=author,
            content="second version",
            attachments=[],
            embeds=[],
            stickers=[],
            jump_url=None,
        )
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(778)

        await self.cog.on_message(before_seen)
        await self.cog.on_message_edit(before_edit, after_edit)
        sent_embeds.clear()
        payload = types.SimpleNamespace(
            guild_id=72,
            channel_id=778,
            message_id=9002,
            cached_message=None,
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.fields[1].value, "second version")

    async def test_on_raw_message_delete_logs_uncached_message_deletes(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=8,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, mention="#mods", send=send),
        )
        self.guilds[guild.id] = guild
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(777)

        payload = types.SimpleNamespace(
            guild_id=8,
            channel_id=777,
            message_id=123456,
            cached_message=None,
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertEqual(len(sent_embeds), 1)
        embed = sent_embeds[0]
        self.assertEqual(embed.title, "Message Deleted")
        self.assertEqual(embed.fields[0].value, "Unknown")
        self.assertEqual(embed.fields[1].value, modlog_module.UNKNOWN_MESSAGE_CONTENT)
        self.assertEqual(embed.footer, "Message ID: 123456")

    async def test_on_raw_message_delete_skips_cached_messages_to_avoid_duplicates(self):
        sent_embeds = []

        async def send(*, embed):
            sent_embeds.append(embed)

        guild = types.SimpleNamespace(
            id=9,
            get_channel=lambda channel_id: types.SimpleNamespace(id=channel_id, mention="#mods", send=send),
        )
        self.guilds[guild.id] = guild
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.config.guild(guild).channel_id.set(888)

        payload = types.SimpleNamespace(
            guild_id=9,
            channel_id=888,
            message_id=654321,
            cached_message=object(),
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertEqual(sent_embeds, [])


if __name__ == "__main__":
    unittest.main()
