import types
import unittest

from tests.support import load_module


nofuckyou_module = load_module("nofuckyou.nofuckyou")


class NoFuckYouTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def cog_disabled_in_guild(cog, guild):
            return False

        self.bot = types.SimpleNamespace(cog_disabled_in_guild=cog_disabled_in_guild)
        self.cog = nofuckyou_module.NoFuckYou(self.bot)

    async def _enable(self, guild_id):
        await self.cog.config.guild(types.SimpleNamespace(id=guild_id)).enabled.set(True)

    def test_contains_trigger_matches_common_variants(self):
        self.assertTrue(self.cog._contains_trigger("fuck you"))
        self.assertTrue(self.cog._contains_trigger("FUK you"))
        self.assertFalse(self.cog._contains_trigger("thanks, friend"))

    async def test_listener_starts_disabled_by_default(self):
        sent = []
        guild = types.SimpleNamespace(id=10)

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            clean_content="fuck you",
            channel=types.SimpleNamespace(id=10, send=send),
        )

        original_random = nofuckyou_module.random.random
        nofuckyou_module.random.random = lambda: 0.0
        try:
            await self.cog.on_message_without_command(message)
        finally:
            nofuckyou_module.random.random = original_random

        self.assertEqual(sent, [])

    async def test_listener_sends_default_reply(self):
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        guild = types.SimpleNamespace(id=1)
        await self._enable(guild.id)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            clean_content="fuck you",
            channel=types.SimpleNamespace(id=101, send=send),
        )

        original_random = nofuckyou_module.random.random
        values = iter([0.0, 0.9])
        nofuckyou_module.random.random = lambda: next(values)
        try:
            await self.cog.on_message_without_command(message)
        finally:
            nofuckyou_module.random.random = original_random

        self.assertEqual(sent, [("No fuck you", "none")])
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.trigger_count(), 1)
        self.assertEqual(await conf.reply_count(), 1)
        self.assertEqual(await conf.thirsty_count(), 0)

    async def test_listener_sends_thirsty_reply(self):
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        guild = types.SimpleNamespace(id=2)
        await self._enable(guild.id)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            clean_content="fu you",
            channel=types.SimpleNamespace(id=202, send=send),
        )

        original_random = nofuckyou_module.random.random
        values = iter([0.0, 0.0])
        nofuckyou_module.random.random = lambda: next(values)
        try:
            await self.cog.on_message_without_command(message)
        finally:
            nofuckyou_module.random.random = original_random

        self.assertEqual(sent, [("Please fuck me :pleading_face:", "none")])
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.thirsty_count(), 1)

    async def test_listener_respects_guild_disable(self):
        await self.cog.config.guild(types.SimpleNamespace(id=3)).enabled.set(False)
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=types.SimpleNamespace(id=3),
            clean_content="fuck you",
            channel=types.SimpleNamespace(id=303, send=send),
        )

        original_random = nofuckyou_module.random.random
        nofuckyou_module.random.random = lambda: 0.0
        try:
            await self.cog.on_message_without_command(message)
        finally:
            nofuckyou_module.random.random = original_random

        self.assertEqual(sent, [])

    async def test_listener_respects_channel_cooldown(self):
        guild = types.SimpleNamespace(id=5)
        await self._enable(guild.id)
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        channel = types.SimpleNamespace(id=505, send=send)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            clean_content="fuck you",
            channel=channel,
        )

        original_random = nofuckyou_module.random.random
        nofuckyou_module.random.random = lambda: 0.0
        original_now = self.cog._now
        values = iter([100.0, 105.0, 111.0, 111.0])
        self.cog._now = lambda: next(values)
        try:
            await self.cog.on_message_without_command(message)
            await self.cog.on_message_without_command(message)
            await self.cog.on_message_without_command(message)
        finally:
            nofuckyou_module.random.random = original_random
            self.cog._now = original_now

        self.assertEqual(len(sent), 2)
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.trigger_count(), 3)
        self.assertEqual(await conf.reply_count(), 2)

    async def test_listener_handles_send_exception(self):
        class FakeHTTPException(load_module("discord").HTTPException):
            pass

        guild = types.SimpleNamespace(id=6)
        await self._enable(guild.id)

        async def send(message, allowed_mentions=None):
            raise FakeHTTPException()

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            clean_content="fuck you",
            channel=types.SimpleNamespace(id=606, send=send),
        )

        original_random = nofuckyou_module.random.random
        nofuckyou_module.random.random = lambda: 0.0
        try:
            await self.cog.on_message_without_command(message)
        finally:
            nofuckyou_module.random.random = original_random

        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.reply_count(), 0)
        self.assertEqual(await conf.send_error_count(), 1)

    async def test_nofuckyouset_updates_config(self):
        sent = []
        guild = types.SimpleNamespace(id=4)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, send=send)

        await self.cog.nofuckyouset_disable(ctx)
        await self.cog.nofuckyouset_chance(ctx, 2.0)
        await self.cog.nofuckyouset_cooldown(ctx, 12)
        await self.cog.nofuckyouset_thirsty(ctx, -1.0)
        await self.cog.nofuckyouset_show(ctx)
        await self.cog.nofuckyouset_stats(ctx)

        conf = self.cog.config.guild(guild)
        self.assertFalse(await conf.enabled())
        self.assertEqual(await conf.response_chance(), 1.0)
        self.assertEqual(await conf.cooldown_seconds(), 12)
        self.assertEqual(await conf.thirsty_chance(), 0.0)
        self.assertEqual(sent[0], "No Fuck You disabled.")
        self.assertEqual(sent[1], "Response chance set to `1.00`.")
        self.assertEqual(sent[2], "Cooldown set to `12s`.")
        self.assertEqual(sent[3], "Thirsty chance set to `0.00`.")
        self.assertIn("Enabled: `False`", sent[4])
        self.assertIn("Triggers seen: `0`", sent[5])


if __name__ == "__main__":
    unittest.main()
