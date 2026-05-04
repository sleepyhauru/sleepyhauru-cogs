import types
import unittest
from datetime import datetime, timedelta, timezone

from tests.support import load_module


voicelog_module = load_module("voicelog.voicelog")


class VoiceLogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = voicelog_module.VoiceLog(
            bot=types.SimpleNamespace(cog_disabled_in_guild=self._not_disabled)
        )
        self.cog.allowed_guild_ids = {123}

    async def _not_disabled(self, cog, guild_arg):
        return False

    def _make_member(self, guild, member_id=1):
        return types.SimpleNamespace(
            id=member_id,
            guild=guild,
            color="blue",
            mention="@user",
            display_avatar=types.SimpleNamespace(url="https://example.com/avatar.png"),
        )

    def _make_channel(self, guild, mention, sent, *, channel_id=None, can_send=True, fails=False):
        perms = types.SimpleNamespace(send_messages=can_send, embed_links=can_send)

        async def send(*, embed):
            if fails:
                raise voicelog_module.discord.HTTPException("boom")
            sent.append((mention, embed))

        return types.SimpleNamespace(
            id=channel_id,
            mention=mention,
            guild=guild,
            permissions_for=lambda me: perms,
            send=send,
        )

    async def test_cog_load_restores_enabled_guilds(self):
        guild = types.SimpleNamespace(id=555)
        await self.cog.config.guild(guild).enabled.set(True)
        await self.cog.cog_load()
        self.assertIn(555, self.cog.allowed_guild_ids)

    async def test_on_voice_state_update_sends_join_embed_to_allowed_channel(self):
        sent = []
        guild = types.SimpleNamespace(id=123, me=object())
        channel = self._make_channel(guild, "#general", sent)
        member = self._make_member(guild)
        before = types.SimpleNamespace(channel=None)
        after = types.SimpleNamespace(channel=channel)

        await self.cog.on_voice_state_update(member, before, after)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].description, "@user has joined #general")
        self.assertEqual(sent[0][1].author["name"], "Connected")

    async def test_on_voice_state_update_skips_when_guild_not_enabled(self):
        sent = []
        guild = types.SimpleNamespace(id=999, me=object())
        channel = self._make_channel(guild, "#general", sent)
        member = self._make_member(guild)
        before = types.SimpleNamespace(channel=None)
        after = types.SimpleNamespace(channel=channel)

        await self.cog.on_voice_state_update(member, before, after)

        self.assertEqual(sent, [])

    async def test_on_voice_state_update_dedupes_same_channel_target(self):
        sent = []
        guild = types.SimpleNamespace(id=123, me=object())
        shared_channel = self._make_channel(guild, "#voice-chat", sent, channel_id=77)
        member = self._make_member(guild)

        await self.cog.on_voice_state_update(
            member,
            types.SimpleNamespace(channel=shared_channel),
            types.SimpleNamespace(channel=shared_channel),
        )
        self.assertEqual(sent, [])

        target_before = types.SimpleNamespace(channel=shared_channel)
        target_after = types.SimpleNamespace(channel=shared_channel)
        self.cog.last_move_at[(guild.id, member.id)] = self.cog._utcnow() - timedelta(seconds=20)
        target_after.channel = shared_channel
        target_before.channel = types.SimpleNamespace(**shared_channel.__dict__)
        target_before.channel.id = 77
        target_before.channel.voice_name = "Voice A"
        target_after.channel.voice_name = "Voice B"

        await self.cog.on_voice_state_update(member, target_before, target_after)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].author["name"], "Moved")

    async def test_on_voice_state_update_handles_leave_and_move(self):
        sent = []
        guild = types.SimpleNamespace(id=123, me=object())
        before_channel = self._make_channel(guild, "#one", sent, channel_id=1)
        after_channel = self._make_channel(guild, "#two", sent, channel_id=2)
        member = self._make_member(guild)

        joined_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        leave_at = joined_at + timedelta(minutes=2, seconds=5)
        self.cog.session_starts[(guild.id, member.id)] = joined_at

        self.cog._utcnow = lambda: leave_at
        await self.cog.on_voice_state_update(
            member,
            types.SimpleNamespace(channel=before_channel),
            types.SimpleNamespace(channel=None),
        )

        self.cog._utcnow = lambda: leave_at + timedelta(seconds=20)
        await self.cog.on_voice_state_update(
            member,
            types.SimpleNamespace(channel=before_channel),
            types.SimpleNamespace(channel=after_channel),
        )

        descriptions = [embed.description for _, embed in sent]
        self.assertIn("@user has left #one", descriptions)
        self.assertIn("@user has moved from #one to #two", descriptions)
        leave_embed = next(embed for _, embed in sent if embed.author["name"] == "Disconnected")
        self.assertEqual(leave_embed.footer, "Session length: 2m 5s")

    async def test_on_voice_state_update_ignores_disabled_event_types(self):
        sent = []
        guild = types.SimpleNamespace(id=123, me=object())
        channel = self._make_channel(guild, "#general", sent)
        member = self._make_member(guild)
        await self.cog.config.guild(guild).log_joins.set(False)

        await self.cog.on_voice_state_update(
            member,
            types.SimpleNamespace(channel=None),
            types.SimpleNamespace(channel=channel),
        )

        self.assertEqual(sent, [])
        self.assertIn((guild.id, member.id), self.cog.session_starts)

    async def test_on_voice_state_update_applies_move_cooldown(self):
        sent = []
        guild = types.SimpleNamespace(id=123, me=object())
        before_channel = self._make_channel(guild, "#one", sent, channel_id=1)
        after_channel = self._make_channel(guild, "#two", sent, channel_id=2)
        member = self._make_member(guild)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.cog._utcnow = lambda: now
        self.cog.last_move_at[(guild.id, member.id)] = now - timedelta(seconds=2)

        await self.cog.on_voice_state_update(
            member,
            types.SimpleNamespace(channel=before_channel),
            types.SimpleNamespace(channel=after_channel),
        )

        self.assertEqual(sent, [])

    async def test_on_voice_state_update_continues_when_one_channel_send_fails(self):
        sent = []
        guild = types.SimpleNamespace(id=123, me=object())
        broken = self._make_channel(guild, "#broken", sent, channel_id=1, fails=True)
        healthy = self._make_channel(guild, "#healthy", sent, channel_id=2)
        member = self._make_member(guild)
        self.cog.last_move_at[(guild.id, member.id)] = self.cog._utcnow() - timedelta(seconds=20)

        await self.cog.on_voice_state_update(
            member,
            types.SimpleNamespace(channel=broken),
            types.SimpleNamespace(channel=healthy),
        )

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "#healthy")

    async def test_enable_disable_and_show_update_settings(self):
        ticks = []
        sent = []
        guild = types.SimpleNamespace(id=321)

        async def tick(message):
            ticks.append(message)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, tick=tick, send=send)

        await self.cog.voicelog_enable(ctx)
        self.assertIn(321, self.cog.allowed_guild_ids)
        await self.cog.voicelog_joins(ctx, False)
        await self.cog.voicelog_leaves(ctx, False)
        await self.cog.voicelog_moves(ctx, False)
        await self.cog.voicelog_cooldown(ctx, 4)
        await self.cog.voicelog_show(ctx)
        await self.cog.voicelog_disable(ctx)

        self.assertFalse(await self.cog.config.guild(guild).log_joins())
        self.assertFalse(await self.cog.config.guild(guild).log_leaves())
        self.assertFalse(await self.cog.config.guild(guild).log_moves())
        self.assertEqual(await self.cog.config.guild(guild).move_cooldown_seconds(), 4)
        self.assertIn("Enabled: `True`", sent[0])
        self.assertIn("Move cooldown: `4s`", sent[0])
        self.assertNotIn(321, self.cog.allowed_guild_ids)
        self.assertEqual(
            ticks,
            [
                "Voice Log enabled",
                "Voice Log join events disabled",
                "Voice Log leave events disabled",
                "Voice Log move events disabled",
                "Voice Log move cooldown set to 4s",
                "Voice Log disabled",
            ],
        )

    async def test_voicelog_group_shows_status_and_next_step(self):
        sent = []
        guild = types.SimpleNamespace(id=654)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, clean_prefix="!", send=send)

        await self.cog.voicelog(ctx)

        self.assertIn("Voice Log settings", sent[0])
        self.assertIn("Next: run `!voicelog enable`.", sent[0])


if __name__ == "__main__":
    unittest.main()
