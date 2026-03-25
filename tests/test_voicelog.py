import types
import unittest

from tests.support import load_module


voicelog_module = load_module("voicelog.voicelog")


class VoiceLogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = voicelog_module.VoiceLog(bot=types.SimpleNamespace())
        self.cog.allowedguilds = {123}

    async def test_on_voice_state_update_sends_join_embed_to_allowed_channel(self):
        sent = []

        async def send(*, embed):
            sent.append(embed)

        perms = types.SimpleNamespace(send_messages=True, embed_links=True)
        guild = types.SimpleNamespace(id=123, me=object())
        channel = types.SimpleNamespace(
            mention="#general",
            guild=guild,
            permissions_for=lambda me: perms,
            send=send,
        )

        async def cog_disabled_in_guild(cog, guild_arg):
            return False

        self.cog.bot.cog_disabled_in_guild = cog_disabled_in_guild

        member = types.SimpleNamespace(
            guild=guild,
            color="blue",
            mention="@user",
            display_avatar=types.SimpleNamespace(url="https://example.com/avatar.png"),
        )
        before = types.SimpleNamespace(channel=None)
        after = types.SimpleNamespace(channel=channel)

        await self.cog.on_voice_state_update(member, before, after)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].description, "@user has joined #general")
        self.assertEqual(sent[0].author["name"], "Connected")

    async def test_on_voice_state_update_skips_when_guild_not_enabled(self):
        sent = []

        async def send(*, embed):
            sent.append(embed)

        guild = types.SimpleNamespace(id=999, me=object())
        channel = types.SimpleNamespace(
            mention="#general",
            guild=guild,
            permissions_for=lambda me: types.SimpleNamespace(send_messages=True, embed_links=True),
            send=send,
        )
        member = types.SimpleNamespace(
            guild=guild,
            color="blue",
            mention="@user",
            display_avatar=types.SimpleNamespace(url="https://example.com/avatar.png"),
        )
        before = types.SimpleNamespace(channel=None)
        after = types.SimpleNamespace(channel=channel)

        await self.cog.on_voice_state_update(member, before, after)

        self.assertEqual(sent, [])

    async def test_on_voice_state_update_handles_leave_and_move(self):
        sent = []

        async def send(*, embed):
            sent.append(embed.description)

        perms = types.SimpleNamespace(send_messages=True, embed_links=True)
        guild = types.SimpleNamespace(id=123, me=object())
        before_channel = types.SimpleNamespace(
            mention="#one",
            guild=guild,
            permissions_for=lambda me: perms,
            send=send,
        )
        after_channel = types.SimpleNamespace(
            mention="#two",
            guild=guild,
            permissions_for=lambda me: perms,
            send=send,
        )

        async def cog_disabled_in_guild(cog, guild_arg):
            return False

        self.cog.bot.cog_disabled_in_guild = cog_disabled_in_guild
        member = types.SimpleNamespace(
            guild=guild,
            color="blue",
            mention="@user",
            display_avatar=types.SimpleNamespace(url="https://example.com/avatar.png"),
        )

        await self.cog.on_voice_state_update(member, types.SimpleNamespace(channel=before_channel), types.SimpleNamespace(channel=None))
        await self.cog.on_voice_state_update(member, types.SimpleNamespace(channel=before_channel), types.SimpleNamespace(channel=after_channel))

        self.assertIn("@user has left #one", sent)
        self.assertIn("@user has moved from #one to #two", sent)

    async def test_enable_and_disable_update_allowed_guilds(self):
        ticks = []
        guild = types.SimpleNamespace(id=321)

        async def tick(message):
            ticks.append(message)

        ctx = types.SimpleNamespace(guild=guild, tick=tick)

        await self.cog.voicelog_enable(ctx)
        self.assertIn(321, self.cog.allowedguilds)
        self.assertTrue(await self.cog.config.guild(guild).enabled())

        await self.cog.voicelog_disable(ctx)
        self.assertNotIn(321, self.cog.allowedguilds)
        self.assertFalse(await self.cog.config.guild(guild).enabled())
        self.assertEqual(ticks, ["Voice Log enabled", "Voice Log disabled"])


if __name__ == "__main__":
    unittest.main()
