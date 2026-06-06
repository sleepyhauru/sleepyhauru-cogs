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

    async def test_cog_load_starts_metrics_and_dashboard_and_cleanup_stops_them(self):
        module = load_module("implingfinder.implingfinder")
        calls = []

        class FakeMetricsStore:
            def __init__(self, path):
                calls.append(("metrics_init", path.name))

            async def start(self):
                calls.append(("metrics_start",))

            async def stop(self):
                calls.append(("metrics_stop",))

            def health(self):
                return {"status": "ok"}

        class FakeDashboardServer:
            def __init__(self, store, *, health_provider, host, port):
                calls.append(("dashboard_init", host, port, store.__class__.__name__))

            async def start(self):
                calls.append(("dashboard_start",))

            async def stop(self):
                calls.append(("dashboard_stop",))

        module.MetricsStore = FakeMetricsStore
        module.DashboardServer = FakeDashboardServer
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        cog._poll_loop = lambda: self._never_started()

        await cog.cog_load()
        cog._poll_task.cancel()
        await cog._close_resources()

        self.assertIn(("metrics_init", "metrics.sqlite3"), calls)
        self.assertIn(("metrics_start",), calls)
        self.assertIn(("dashboard_init", "0.0.0.0", 8765, "FakeMetricsStore"), calls)
        self.assertIn(("dashboard_start",), calls)
        self.assertLess(calls.index(("dashboard_stop",)), calls.index(("metrics_stop",)))

    async def _never_started(self):
        await __import__("asyncio").sleep(3600)

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

    async def test_successful_spawn_post_records_pipeline_timings(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", me=None)
        channel = types.SimpleNamespace(
            id=456,
            name="dragon-imps",
            send=self._async_return(types.SimpleNamespace(id=999)),
        )
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=False,
        )
        cog._location_for_spawn = lambda _spawn: "Crafting Guild"

        await cog._send_spawn_to_channel(
            guild,
            channel,
            spawn,
            screenshots=False,
            fetch_ms=80,
            process_ms=20,
        )

        event = recorded[-1]
        self.assertEqual(event.kind, "post")
        self.assertEqual(event.outcome, "ok")
        self.assertEqual(event.guild_name, "Impling Hunters")
        self.assertEqual(event.channel_name, "dragon-imps")
        self.assertEqual(event.impling_type, "dragon")
        self.assertEqual(event.location, "Crafting Guild")
        self.assertEqual(event.fetch_ms, 80)
        self.assertEqual(event.process_ms, 20)
        self.assertIsNotNone(event.send_ms)
        self.assertIsNotNone(event.end_to_end_ms)

    async def test_metrics_failure_does_not_block_spawn_post(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        cog.metrics_store = types.SimpleNamespace(
            record=lambda _event: (_ for _ in ()).throw(RuntimeError("metrics unavailable"))
        )
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", me=None)
        message = types.SimpleNamespace(id=999)
        channel = types.SimpleNamespace(id=456, name="dragon-imps", send=self._async_return(message))
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=False,
        )

        with self.assertLogs("red.implingfinder", level="ERROR"):
            sent = await cog._send_spawn_to_channel(guild, channel, spawn, screenshots=False)

        self.assertIs(sent, message)

    async def test_poll_records_backend_failure_without_processing(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters")
        settings = {
            "enabled": True,
            "channels": {"456": [1644]},
            "poll_interval": 30,
            "endpoint": module.DEFAULT_ENDPOINT,
        }
        cog.config.guild = lambda _guild: types.SimpleNamespace(all=self._async_return(settings))
        cog._cog_disabled_in_guild = self._async_return(False)

        async def fail_fetch(_endpoint):
            raise module.BackendError("Backend returned HTTP 503.", status=503)

        cog._fetch_spawns = fail_fetch

        with self.assertLogs("red.implingfinder", level="WARNING"):
            await cog._poll_guild(guild, 1_000)

        event = recorded[-1]
        self.assertEqual(event.kind, "fetch")
        self.assertEqual(event.outcome, "error")
        self.assertEqual(event.guild_name, "Impling Hunters")
        self.assertEqual(event.error_category, "http_503")
        self.assertIsNotNone(event.fetch_ms)

    def _async_return(self, value):
        async def result(*args, **kwargs):
            return value

        return result

    async def test_embed_uses_human_location_and_omits_internal_spawn_fields(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=3210,
            ycoord=3420,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        cog._location_for_spawn = lambda _spawn: "Varrock"

        embed = cog._embed_for_spawn(spawn)

        self.assertEqual(
            [field.name for field in embed.fields],
            ["World", "Location", "Discovered", "Map"],
        )
        self.assertEqual(embed.fields[1].value, "Varrock")
        self.assertIsNone(embed.footer)
        content = cog._content_for_spawn(spawn)
        self.assertIn("Varrock", content)
        self.assertNotIn("3210", content)
        self.assertNotIn("plane", content.lower())

    async def test_process_deletes_tracked_message_when_spawn_disappears(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
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
        self.assertEqual(recorded[-1].kind, "despawn")
        self.assertEqual(recorded[-1].outcome, "ok")

    async def test_map_render_records_download_and_image_timings(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )

        class FakeImage:
            width = 64
            height = 72

            def convert(self, _mode):
                return self

            def resize(self, _size, _resampling):
                self.width = 512
                self.height = 512
                return self

            def thumbnail(self, size, _resampling):
                self.width, self.height = size

            def alpha_composite(self, _icon, _position):
                return None

            def save(self, output, format):
                output.write(b"png")

        fake_image_module = types.SimpleNamespace(
            Resampling=types.SimpleNamespace(NEAREST=1, LANCZOS=2),
            open=lambda _source: FakeImage(),
        )
        cog._load_pillow = lambda: (fake_image_module, None, None)
        cog._fetch_map_tile = self._async_return(b"tile")

        result = await cog._make_map_file(spawn)

        self.assertEqual(result.filename, "impling-map.png")
        event = recorded[-1]
        self.assertEqual(event.kind, "render")
        self.assertEqual(event.impling_type, "dragon")
        self.assertEqual(event.outcome, "ok")
        self.assertIsNotNone(event.fetch_ms)
        self.assertIsNotNone(event.render_ms)

    async def test_process_migrates_legacy_active_message_key_for_current_sighting(self):
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
        deleted = []

        class PartialMessage:
            async def delete(self):
                deleted.append(222)

        class Channel:
            def get_partial_message(self, _message_id):
                return PartialMessage()

        guild = types.SimpleNamespace(id=123, get_channel=lambda channel_id: Channel())
        cog.config._global_store["active_messages"] = {
            "123": {spawn.dedupe_key: {"111": 222}}
        }

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [spawn],
        )

        self.assertEqual(deleted, [])
        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {spawn.sighting_key: {"111": 222}}},
        )

    async def test_process_migrates_deployed_coarse_area_key_for_current_sighting(self):
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
        cog.config._global_store["active_messages"] = {
            "123": {spawn.legacy_area_key: {"111": 222}}
        }

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [spawn],
        )

        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {spawn.sighting_key: {"111": 222}}},
        )

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
            {"123": {spawn.sighting_key: {"111": 333}}},
        )

    async def test_process_posts_one_message_for_duplicate_moving_rows(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        older = module.ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1282,
            ycoord=3155,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        newer = module.ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1289,
            ycoord=3158,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        guild = types.SimpleNamespace(id=123, get_channel=lambda channel_id: object())
        sent = []
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))

        async def send_spawn(_guild, _channel, received_spawn, *, screenshots):
            sent.append(received_spawn)
            return types.SimpleNamespace(id=444 + len(sent))

        cog._send_spawn_to_channel = send_spawn

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": True, "screenshots": False},
            {"111": [1642]},
            [newer, older],
        )

        self.assertEqual(sent, [newer])
        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {newer.sighting_key: {"111": 445}}},
        )
        self.assertEqual(
            [(event.kind, event.count_value) for event in recorded if event.kind in {"duplicate", "routed"}],
            [("duplicate", 1), ("routed", 1)],
        )


if __name__ == "__main__":
    unittest.main()
