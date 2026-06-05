from datetime import datetime, timezone
import io
import types
import unittest

from tests.support import load_module


class CogImportTest(unittest.IsolatedAsyncioTestCase):
    def test_cog_module_imports_and_registers_defaults_with_dependency_stubs(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=object())

        self.assertEqual(cog.config._guild_defaults["poll_interval"], 30)
        self.assertEqual(cog.config._guild_defaults["max_age_seconds"], 900)
        self.assertEqual(cog.config._global_store["seen"], {})
        self.assertEqual(cog.config._global_store["active_messages"], {})

    async def test_spawn_posts_use_generated_map_attachment_filename(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123, me=None)
        sent_messages = []
        message = types.SimpleNamespace(id=999)

        class Channel:
            async def send(self, *args, **kwargs):
                sent_messages.append((args, kwargs))
                return message

        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        calls = []

        async def make_screenshot_file(received_spawn):
            calls.append(received_spawn)
            return module.discord.File(io.BytesIO(b"png"), filename="impling-map.png")

        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=True,
        )
        cog._make_screenshot_file = make_screenshot_file

        sent_message = await cog._send_spawn_to_channel(guild, Channel(), spawn, screenshots=True)

        self.assertEqual(calls, [spawn])
        self.assertEqual(len(sent_messages), 1)
        _, kwargs = sent_messages[0]
        self.assertEqual(kwargs["file"].filename, "impling-map.png")
        self.assertEqual(kwargs["embed"].image, "attachment://impling-map.png")
        self.assertIs(sent_message, message)

    async def test_process_deletes_tracked_message_when_spawn_disappears(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        old_spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        deleted = []

        class PartialMessage:
            async def delete(self):
                deleted.append(222)

        class Channel:
            id = 111

            def get_partial_message(self, message_id):
                self.message_id = message_id
                return PartialMessage()

        channel = Channel()
        guild = types.SimpleNamespace(id=123, get_channel=lambda channel_id: channel)
        cog.config._global_store["active_messages"] = {
            "123": {old_spawn.dedupe_key: {"111": 222}}
        }

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [],
        )

        self.assertEqual(deleted, [222])
        self.assertEqual(channel.message_id, 222)
        self.assertEqual(cog.config._global_store["active_messages"], {})

    async def test_process_records_sent_message_for_active_spawn(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        guild = types.SimpleNamespace(id=123, get_channel=lambda channel_id: object())
        sent = []

        async def send_spawn(_guild, _channel, received_spawn, *, screenshots):
            sent.append((received_spawn, screenshots))
            return types.SimpleNamespace(id=333)

        cog._send_spawn_to_channel = send_spawn

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": True, "screenshots": True},
            {"111": [1644]},
            [spawn],
        )

        self.assertEqual(sent, [(spawn, True)])
        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {spawn.dedupe_key: {"111": 333}}},
        )


if __name__ == "__main__":
    unittest.main()
