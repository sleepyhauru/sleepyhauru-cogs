import asyncio
from datetime import datetime, timezone
import io
import types
import unittest

from tests.support import load_module


class CogImportTest(unittest.IsolatedAsyncioTestCase):
    def test_cog_module_imports_and_registers_defaults_with_dependency_stubs(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=object())

        self.assertEqual(module.MIN_POLL_INTERVAL_SECONDS, 5)
        self.assertEqual(module.DEFAULT_POLL_INTERVAL_SECONDS, 5)
        self.assertEqual(cog.config._guild_defaults["poll_interval"], 5)
        self.assertEqual(cog.config._guild_defaults["max_age_seconds"], 900)
        self.assertFalse(cog.config._guild_defaults["puro_enabled"])
        self.assertIsNone(cog.config._guild_defaults["puro_channel"])
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

    async def test_guild_poll_runner_uses_fixed_start_cadence_after_slow_poll(self):
        module = load_module("implingfinder.implingfinder")
        original_interval = module.MIN_POLL_INTERVAL_SECONDS
        module.MIN_POLL_INTERVAL_SECONDS = 0.05
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123)
        starts = []

        async def poll_guild(_guild, _now_monotonic):
            starts.append(asyncio.get_running_loop().time())
            if len(starts) == 1:
                await asyncio.sleep(0.07)
                return 0.05
            raise asyncio.CancelledError()

        cog._poll_guild_safely = poll_guild

        try:
            with self.assertRaises(asyncio.CancelledError):
                await cog._run_guild_poll_loop(guild)
        finally:
            module.MIN_POLL_INTERVAL_SECONDS = original_interval

        self.assertEqual(len(starts), 2)
        self.assertLess(starts[1] - starts[0], 0.105)

    async def test_guild_poll_runner_does_not_schedule_under_interval_after_late_wake(self):
        module = load_module("implingfinder.implingfinder")
        original_interval = module.MIN_POLL_INTERVAL_SECONDS
        original_monotonic = module.time.monotonic
        original_sleep = module.asyncio.sleep
        module.MIN_POLL_INTERVAL_SECONDS = 0.05
        clock = {"now": 0.0}
        sleep_extras = [0.004, 0.0]
        starts = []
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123)

        def monotonic():
            return clock["now"]

        async def sleep(delay):
            extra = sleep_extras.pop(0) if sleep_extras else 0.0
            clock["now"] += max(0.0, delay) + extra

        async def poll_guild(_guild, _now_monotonic):
            starts.append(clock["now"])
            if len(starts) == 3:
                raise asyncio.CancelledError()
            return 0.05

        cog._poll_guild_safely = poll_guild

        try:
            module.time.monotonic = monotonic
            module.asyncio.sleep = sleep
            with self.assertRaises(asyncio.CancelledError):
                await cog._run_guild_poll_loop(guild)
        finally:
            module.MIN_POLL_INTERVAL_SECONDS = original_interval
            module.time.monotonic = original_monotonic
            module.asyncio.sleep = original_sleep

        self.assertEqual(len(starts), 3)
        self.assertGreaterEqual(starts[2] - starts[1], 0.05)

    async def test_sync_poll_runners_starts_and_cancels_per_guild_tasks(self):
        module = load_module("implingfinder.implingfinder")
        guild = types.SimpleNamespace(id=123, name="Impling Hunters")
        settings = {
            "enabled": True,
            "channels": {"456": [1644]},
            "poll_interval": 5,
            "endpoint": module.DEFAULT_ENDPOINT,
        }
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None, guilds=[guild]))
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_guild_poll_loop(received_guild):
            self.assertIs(received_guild, guild)
            started.set()
            await release.wait()

        cog.config.guild = lambda _guild: types.SimpleNamespace(all=self._async_return(settings))
        cog._cog_disabled_in_guild = self._async_return(False)
        cog._run_guild_poll_loop = run_guild_poll_loop

        try:
            await cog._sync_poll_runners()
            await asyncio.wait_for(started.wait(), timeout=0.05)
            self.assertIn(guild.id, cog._poll_runner_tasks)

            settings["enabled"] = False
            await cog._sync_poll_runners()
            self.assertNotIn(guild.id, cog._poll_runner_tasks)
        finally:
            release.set()
            for task in list(getattr(cog, "_poll_runner_tasks", {}).values()):
                task.cancel()
            if getattr(cog, "_poll_runner_tasks", None):
                await asyncio.gather(*cog._poll_runner_tasks.values(), return_exceptions=True)

    async def test_spawn_post_queues_screenshot_edit_job(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        cog.metrics_store = types.SimpleNamespace(record=lambda _event: None)
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", me=None)

        class Message:
            id = 999

        class Channel:
            id = 456
            name = "rare-imps"

            async def send(self, *args, **kwargs):
                return Message()

        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=True,
        )

        message = await cog._send_spawn_to_channel(guild, Channel(), spawn, screenshots=True)

        self.assertEqual(message.id, 999)
        self.assertEqual(cog._screenshot_queue.qsize(), 1)

    async def test_process_queues_post_poll_maintenance_job(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda _channel_id: None)

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [],
        )

        self.assertEqual(cog._maintenance_queue.qsize(), 1)

    async def test_spawn_posts_immediately_then_edits_generated_map_attachment(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", me=None)
        sent_messages = []
        edited_messages = []

        class Message:
            id = 999

            async def edit(self, **kwargs):
                edited_messages.append(kwargs)
                return self

        class Channel:
            id = 456
            name = "rare-imps"

            async def send(self, *args, **kwargs):
                sent_messages.append((args, kwargs))
                return Message()

        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        cog._location_for_spawn = lambda _spawn: "Crafting Guild"
        calls = []
        render_started = asyncio.Event()
        finish_render = asyncio.Event()

        async def make_screenshot_file(received_spawn):
            calls.append(received_spawn)
            render_started.set()
            await finish_render.wait()
            return module.discord.File(io.BytesIO(b"png"), filename="impling-map.png")

        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=True,
        )
        cog._make_screenshot_file = make_screenshot_file
        cog._start_screenshot_workers(count=1)

        try:
            sent_message = await asyncio.wait_for(
                cog._send_spawn_to_channel(guild, Channel(), spawn, screenshots=True),
                timeout=0.05,
            )

            self.assertEqual(sent_message.id, 999)
            self.assertEqual(len(sent_messages), 1)
            _, kwargs = sent_messages[0]
            self.assertNotIn("file", kwargs)
            self.assertIsNone(kwargs["embed"].image)
            self.assertEqual(recorded[-1].kind, "post")
            self.assertIsNone(recorded[-1].render_ms)

            await asyncio.wait_for(render_started.wait(), timeout=0.05)
            self.assertEqual(calls, [spawn])
            finish_render.set()
            await asyncio.wait_for(cog._screenshot_queue.join(), timeout=0.25)
        finally:
            await self._cancel_screenshot_workers(cog)

        self.assertEqual(len(edited_messages), 1)
        edit_kwargs = edited_messages[0]
        self.assertEqual(edit_kwargs["attachments"][0].filename, "impling-map.png")
        self.assertEqual(edit_kwargs["embed"].image, "attachment://impling-map.png")
        attachment_event = recorded[-1]
        self.assertEqual(attachment_event.kind, "attachment")
        self.assertEqual(attachment_event.outcome, "ok")
        self.assertEqual(attachment_event.guild_name, "Impling Hunters")
        self.assertEqual(attachment_event.channel_name, "rare-imps")
        self.assertEqual(attachment_event.impling_type, "dragon")
        self.assertEqual(attachment_event.world, 489)
        self.assertEqual(attachment_event.location, "Crafting Guild")
        self.assertIsNotNone(attachment_event.render_ms)
        self.assertIsNotNone(attachment_event.send_ms)
        self.assertIsNotNone(attachment_event.end_to_end_ms)

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
        discovered = datetime.fromtimestamp(1_715_000_000, timezone.utc)
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=discovered,
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
            fetch_completed_at=datetime.fromtimestamp(1_715_000_003, timezone.utc),
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
        self.assertEqual(event.age_at_fetch_ms, 3_000)
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
        cleaned = []
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

        async def clean_feed_channels(received_guild, channels):
            cleaned.append((received_guild, channels))

        cog._clean_feed_channels = clean_feed_channels

        with self.assertLogs("red.implingfinder", level="WARNING"):
            await cog._poll_guild(guild, 1_000)

        self.assertEqual(cleaned, [(guild, {"456": [1644]})])
        event = recorded[-1]
        self.assertEqual(event.kind, "fetch")
        self.assertEqual(event.outcome, "error")
        self.assertEqual(event.guild_name, "Impling Hunters")
        self.assertEqual(event.error_category, "http_503")
        self.assertIsNotNone(event.fetch_ms)

    async def test_startup_feed_cleanup_runs_only_once_per_cog_instance(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        cleaned = []
        processed = []
        guild = types.SimpleNamespace(id=123, name="Impling Hunters")
        settings = {
            "enabled": True,
            "channels": {"456": [1644]},
            "poll_interval": 5,
            "endpoint": module.DEFAULT_ENDPOINT,
        }
        cog.config.guild = lambda _guild: types.SimpleNamespace(all=self._async_return(settings))
        cog._cog_disabled_in_guild = self._async_return(False)
        cog._fetch_spawns = self._async_return([])

        async def clean_feed_channels(received_guild, channels):
            cleaned.append((received_guild, channels))

        async def process_polled_spawns(*args, **kwargs):
            processed.append(args)

        cog._clean_feed_channels = clean_feed_channels
        cog._process_polled_spawns = process_polled_spawns

        await cog._poll_guild(guild, 1_000)
        await cog._poll_guild(guild, 1_006)

        self.assertEqual(cleaned, [(guild, {"456": [1644]})])
        self.assertEqual(len(processed), 2)

    def _async_return(self, value):
        async def result(*args, **kwargs):
            return value

        return result

    def _active_message_record(self, spawn, message_id):
        return {
            "message_id": message_id,
            "npcid": spawn.npcid,
            "world": spawn.world,
            "xcoord": spawn.xcoord,
            "ycoord": spawn.ycoord,
            "plane": spawn.plane,
            "discovered_epoch": spawn.discovered_epoch,
        }

    async def _cancel_post_poll_tasks(self, cog):
        tasks = list(getattr(cog, "_maintenance_worker_tasks", set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_post_poll_tasks(self, cog):
        queue = getattr(cog, "_maintenance_queue", None)
        self.assertIsNotNone(queue)
        cog._start_maintenance_workers(count=1)
        await asyncio.wait_for(queue.join(), timeout=0.25)
        await self._cancel_post_poll_tasks(cog)

    async def _cancel_screenshot_workers(self, cog):
        tasks = list(getattr(cog, "_screenshot_worker_tasks", set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_interval_command_accepts_five_second_minimum(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123)
        replies = []

        async def send(message):
            replies.append(message)

        ctx = types.SimpleNamespace(guild=guild, send=send)

        await cog.implingset_interval(ctx, 5)

        self.assertEqual(await cog.config.guild(guild).poll_interval(), 5)
        self.assertEqual(replies, ["Polling interval set to `5s`."])

    async def test_puro_commands_set_toggle_and_channel(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123)
        replies = []

        async def send(message):
            replies.append(message)

        ctx = types.SimpleNamespace(guild=guild, send=send)
        channel = types.SimpleNamespace(id=222, mention="#puro-puro")

        await cog.implingset_puro(ctx, "on")
        await cog.implingset_purochannel(ctx, channel)

        self.assertTrue(await cog.config.guild(guild).puro_enabled())
        self.assertEqual(await cog.config.guild(guild).puro_channel(), "222")
        self.assertEqual(
            replies,
            [
                "Puro-Puro impling posts are now `True`.",
                "#puro-puro will receive Puro-Puro impling posts.",
            ],
        )

    async def test_embed_uses_spawned_title_as_map_link_without_coordinate_field(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        spawn = module.ImplingSpawn(
            npcid=8741,
            world=489,
            xcoord=3210,
            ycoord=3420,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        cog._location_for_spawn = lambda _spawn: "Varrock"

        embed = cog._embed_for_spawn(spawn)

        self.assertEqual(embed.title, "Crystal Impling spawned")
        self.assertEqual(
            embed.url,
            "https://explv.github.io/?centreX=3210&centreY=3420&centreZ=0&zoom=7",
        )
        self.assertEqual(
            [field.name for field in embed.fields],
            ["World", "Location", "Discovered"],
        )
        self.assertEqual(embed.fields[1].value, "Varrock")
        self.assertEqual(embed.fields[2].value, "<t:1715000000:R>")
        self.assertNotIn("<t:1715000000:F>", embed.fields[2].value)
        self.assertIsNone(embed.timestamp)
        self.assertIsNone(embed.footer)
        content = cog._content_for_spawn(spawn)
        self.assertIn("Varrock", content)
        self.assertIn("https://explv.github.io/?centreX=3210&centreY=3420&centreZ=0&zoom=7", content)
        self.assertNotIn("plane", content.lower())

    async def test_process_marks_tracked_message_despawned_when_spawn_disappears(self):
        module = load_module("implingfinder.implingfinder")
        module.DESPAWN_DELETE_DELAY_SECONDS = 0
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        old_spawn = module.ImplingSpawn(
            npcid=8741,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        edited = []
        deleted = []

        class PartialMessage:
            id = 222

            async def edit(self, **kwargs):
                edited.append(kwargs)

            async def delete(self):
                deleted.append(222)

        class Channel:
            id = 111
            name = "crystal-imps"

            def get_partial_message(self, message_id):
                self.message_id = message_id
                return PartialMessage()

        channel = Channel()
        guild = types.SimpleNamespace(id=123, get_channel=lambda channel_id: channel)
        cog.config._global_store["active_messages"] = {
            "123": {
                old_spawn.sighting_key: {
                    "111": {
                        "message_id": 222,
                        "npcid": old_spawn.npcid,
                        "world": old_spawn.world,
                        "xcoord": old_spawn.xcoord,
                        "ycoord": old_spawn.ycoord,
                        "plane": old_spawn.plane,
                        "discovered_epoch": old_spawn.discovered_epoch,
                    }
                }
            }
        }

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [],
        )
        await self._wait_post_poll_tasks(cog)
        if cog._despawn_delete_tasks:
            await asyncio.wait_for(
                asyncio.gather(*list(cog._despawn_delete_tasks)),
                timeout=0.05,
            )

        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0]["embed"].title, "Crystal Impling despawned")
        self.assertEqual(
            edited[0]["embed"].url,
            "https://explv.github.io/?centreX=2914&centreY=3323&centreZ=0&zoom=7",
        )
        self.assertEqual(edited[0]["embed"].image, "attachment://impling-map.png")
        self.assertNotIn("Coordinates", [field.name for field in edited[0]["embed"].fields])
        self.assertNotIn("attachments", edited[0])
        self.assertEqual(deleted, [222])
        self.assertEqual(channel.message_id, 222)
        self.assertEqual(cog.config._global_store["active_messages"], {})
        self.assertEqual(recorded[-1].kind, "despawn")
        self.assertEqual(recorded[-1].outcome, "ok")

    async def test_process_despawns_when_only_stale_backend_row_remains(self):
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
        edited = []

        class PartialMessage:
            async def edit(self, **kwargs):
                edited.append(kwargs)

        class Channel:
            id = 111

            def get_partial_message(self, _message_id):
                return PartialMessage()

        guild = types.SimpleNamespace(id=123, get_channel=lambda channel_id: Channel())
        cog.config._global_store["active_messages"] = {
            "123": {
                old_spawn.sighting_key: {
                    "111": {
                        "message_id": 222,
                        "npcid": old_spawn.npcid,
                        "world": old_spawn.world,
                        "xcoord": old_spawn.xcoord,
                        "ycoord": old_spawn.ycoord,
                        "plane": old_spawn.plane,
                        "discovered_epoch": old_spawn.discovered_epoch,
                    }
                }
            }
        }

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 1, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [old_spawn],
        )
        await self._wait_post_poll_tasks(cog)

        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0]["embed"].title, "Dragon Impling despawned")
        self.assertEqual(cog.config._global_store["active_messages"], {})

    async def test_process_posts_before_background_despawn_and_cleanup(self):
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
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda channel_id: object())
        sent = []
        maintenance_started = asyncio.Event()
        release_maintenance = asyncio.Event()

        async def send_spawn(_guild, _channel, received_spawn, *, screenshots, **_kwargs):
            sent.append(received_spawn)
            return types.SimpleNamespace(id=333)

        async def blocked_maintenance(*_args, **_kwargs):
            maintenance_started.set()
            await release_maintenance.wait()

        cog._send_spawn_to_channel = send_spawn
        cog._delete_missing_active_messages = blocked_maintenance
        cog._clean_feed_channels = blocked_maintenance
        cog._start_maintenance_workers(count=1)

        try:
            await asyncio.wait_for(
                cog._process_polled_spawns(
                    guild,
                    {"max_age_seconds": 900, "announce_existing": True, "screenshots": False},
                    {"111": [1644]},
                    [spawn],
                ),
                timeout=0.05,
            )
            await asyncio.sleep(0)
            self.assertEqual(sent, [spawn])
            self.assertTrue(maintenance_started.is_set())
        finally:
            release_maintenance.set()
            await self._cancel_post_poll_tasks(cog)

    async def test_process_sends_matching_channels_in_parallel(self):
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
        channels = {111: object(), 222: object()}
        guild = types.SimpleNamespace(
            id=123,
            name="Impling Hunters",
            get_channel=lambda channel_id: channels.get(channel_id),
        )
        started = 0
        max_in_flight = 0
        in_flight = 0
        release_sends = asyncio.Event()

        async def send_spawn(_guild, _channel, _spawn, *, screenshots, **_kwargs):
            nonlocal started, in_flight, max_in_flight
            started += 1
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if started == 2:
                release_sends.set()
            await release_sends.wait()
            in_flight -= 1
            return types.SimpleNamespace(id=400 + started)

        cog._send_spawn_to_channel = send_spawn
        cog._delete_missing_active_messages = self._async_return(None)
        cog._clean_feed_channels = self._async_return(None)

        await asyncio.wait_for(
            cog._process_polled_spawns(
                guild,
                {"max_age_seconds": 900, "announce_existing": True, "screenshots": False},
                {"111": [1644], "222": [1644]},
                [spawn],
            ),
            timeout=0.05,
        )

        self.assertEqual(started, 2)
        self.assertEqual(max_in_flight, 2)

    async def test_process_routes_puro_spawn_only_to_puro_channel(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2590,
            ycoord=4310,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        channels = {
            111: types.SimpleNamespace(id=111, name="dragon-imps"),
            222: types.SimpleNamespace(id=222, name="puro-puro"),
        }
        guild = types.SimpleNamespace(
            id=123,
            name="Impling Hunters",
            get_channel=lambda channel_id: channels.get(channel_id),
        )
        sent = []

        async def send_spawn(_guild, channel, received_spawn, *, screenshots, **_kwargs):
            sent.append((channel.id, received_spawn, screenshots))
            return types.SimpleNamespace(id=900 + channel.id)

        cog._send_spawn_to_channel = send_spawn

        await cog._process_polled_spawns(
            guild,
            {
                "max_age_seconds": 900,
                "announce_existing": True,
                "screenshots": False,
                "puro_enabled": True,
                "puro_channel": "222",
            },
            {"111": [1644]},
            [spawn],
        )

        self.assertEqual(sent, [(222, spawn, False)])
        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {spawn.sighting_key: {"222": self._active_message_record(spawn, 1122)}}},
        )

    async def test_process_skips_puro_spawn_when_puro_is_disabled(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2590,
            ycoord=4310,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda _channel_id: object())
        sent = []

        async def send_spawn(_guild, _channel, received_spawn, *, screenshots, **_kwargs):
            sent.append(received_spawn)
            return types.SimpleNamespace(id=333)

        cog._send_spawn_to_channel = send_spawn

        await cog._process_polled_spawns(
            guild,
            {
                "max_age_seconds": 900,
                "announce_existing": True,
                "screenshots": False,
                "puro_enabled": False,
                "puro_channel": "222",
            },
            {"111": [1644]},
            [spawn],
        )

        self.assertEqual(sent, [])
        self.assertEqual(cog.config._global_store["active_messages"], {})

    async def test_process_cleans_feed_channel_when_no_live_spawns(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        deleted = []

        class Message:
            def __init__(self, message_id, *, pinned=False):
                self.id = message_id
                self.pinned = pinned

            async def delete(self):
                deleted.append(self.id)

        class Channel:
            id = 111
            name = "rare-imps"

            async def history(self, *, limit):
                self.history_limit = limit
                for message in [Message(10), Message(11, pinned=True)]:
                    yield message

        channel = Channel()
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda channel_id: channel)
        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            manage_messages=True,
            read_message_history=True,
        )

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [],
        )
        await self._wait_post_poll_tasks(cog)

        self.assertEqual(deleted, [10])
        self.assertEqual(channel.history_limit, module.FEED_CLEANUP_HISTORY_LIMIT)

    async def test_process_includes_enabled_puro_channel_in_feed_cleanup(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda _channel_id: None)

        await cog._process_polled_spawns(
            guild,
            {
                "max_age_seconds": 900,
                "announce_existing": False,
                "screenshots": False,
                "puro_enabled": True,
                "puro_channel": "222",
            },
            {"111": [1644]},
            [],
        )

        job = cog._maintenance_queue.get_nowait()
        self.assertEqual(job.channels, {"111": [1644], "222": []})
        cog._maintenance_queue.task_done()

    async def test_feed_cleanup_keeps_recent_despawn_notice(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        deleted = []

        class Message:
            def __init__(self, message_id):
                self.id = message_id
                self.pinned = False

            async def delete(self):
                deleted.append(self.id)

        class Channel:
            id = 111
            name = "rare-imps"

            async def history(self, *, limit):
                self.history_limit = limit
                for message in [Message(222), Message(10)]:
                    yield message

        channel = Channel()
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda channel_id: channel)
        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            manage_messages=True,
            read_message_history=True,
        )
        cog._remember_despawn_notice(channel.id, 222)

        await cog._clean_feed_channels(guild, {"111": [1644]})

        self.assertEqual(deleted, [10])

    async def test_process_cleans_feed_channel_but_keeps_active_and_pinned_messages(self):
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

        class Message:
            def __init__(self, message_id, *, pinned=False):
                self.id = message_id
                self.pinned = pinned

            async def delete(self):
                deleted.append(self.id)

        class Channel:
            id = 111
            name = "rare-imps"

            async def history(self, *, limit):
                self.history_limit = limit
                for message in [
                    Message(222),
                    Message(333),
                    Message(444, pinned=True),
                ]:
                    yield message

        channel = Channel()
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", get_channel=lambda channel_id: channel)
        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            manage_messages=True,
            read_message_history=True,
        )
        cog.config._global_store["active_messages"] = {
            "123": {spawn.sighting_key: {"111": 222}}
        }

        await cog._process_polled_spawns(
            guild,
            {"max_age_seconds": 900, "announce_existing": False, "screenshots": False},
            {"111": [1644]},
            [spawn],
        )
        await self._wait_post_poll_tasks(cog)

        self.assertEqual(deleted, [333])
        self.assertEqual(channel.history_limit, module.FEED_CLEANUP_HISTORY_LIMIT)

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
        crop_calls = []
        fetch_urls = []
        new_calls = []
        resize_calls = []
        composite_calls = []

        class FakeImage:
            def __init__(self, width=64, height=72):
                self.width = width
                self.height = height

            def convert(self, _mode):
                return self

            def resize(self, _size, _resampling):
                resize_calls.append(_size)
                self.width = 512
                self.height = 512
                return self

            def thumbnail(self, size, _resampling):
                self.width, self.height = size

            def alpha_composite(self, _icon, _position):
                composite_calls.append(_position)
                return None

            def save(self, output, format):
                output.write(b"png")

        def fake_new(mode, size, color):
            new_calls.append((mode, size, color))
            return FakeImage(*size)

        fake_image_module = types.SimpleNamespace(
            Resampling=types.SimpleNamespace(NEAREST=1, LANCZOS=2),
            open=lambda _source: FakeImage(),
            new=fake_new,
        )

        def fake_tiles_for_crop(received_spawn, *, width, height, zoom=None):
            crop_calls.append((received_spawn, width, height, zoom))
            return [
                types.SimpleNamespace(url="tile-a", paste_x=0, paste_y=0),
                types.SimpleNamespace(url="tile-b", paste_x=128, paste_y=0),
                types.SimpleNamespace(url="tile-c", paste_x=0, paste_y=128),
                types.SimpleNamespace(url="tile-d", paste_x=128, paste_y=128),
            ]

        async def fetch_map_tile(url):
            fetch_urls.append(url)
            return b"tile"

        module.explv_tiles_for_crop = fake_tiles_for_crop
        cog._load_pillow = lambda: (fake_image_module, None, None)
        cog._fetch_map_tile = fetch_map_tile

        result = await cog._make_map_file(spawn)

        self.assertEqual(result.filename, "impling-map.png")
        self.assertEqual(crop_calls, [(spawn, 512, 512, 10)])
        self.assertEqual(fetch_urls, ["tile-a", "tile-b", "tile-c", "tile-d"])
        self.assertEqual(new_calls, [("RGBA", (512, 512), (0, 0, 0, 0))])
        self.assertEqual(resize_calls, [])
        self.assertEqual(composite_calls[-1], (220, 220))
        event = recorded[-1]
        self.assertEqual(event.kind, "render")
        self.assertEqual(event.impling_type, "dragon")
        self.assertEqual(event.outcome, "ok")
        self.assertIsNotNone(event.fetch_ms)
        self.assertIsNotNone(event.render_ms)

    async def test_slow_screenshot_render_does_not_block_spawn_post(self):
        module = load_module("implingfinder.implingfinder")
        module.MAP_RENDER_SEND_TIMEOUT_SECONDS = 0.01
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters", me=None)
        sent_messages = []
        message = types.SimpleNamespace(id=999)

        class Channel:
            id = 456
            name = "rare-imps"

            async def send(self, *args, **kwargs):
                sent_messages.append((args, kwargs))
                return message

        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )

        async def slow_screenshot_file(_spawn):
            await asyncio.sleep(3600)

        cog._bot_permissions = lambda _guild, _channel: types.SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=True,
        )
        cog._make_screenshot_file = slow_screenshot_file

        sent_message = await asyncio.wait_for(
            cog._send_spawn_to_channel(guild, Channel(), spawn, screenshots=True),
            timeout=0.05,
        )

        self.assertIs(sent_message, message)
        self.assertEqual(len(sent_messages), 1)
        _args, kwargs = sent_messages[0]
        self.assertNotIn("file", kwargs)
        self.assertIsNone(kwargs["embed"].image)
        self.assertEqual(cog._screenshot_queue.qsize(), 1)
        post_event = recorded[-1]
        self.assertEqual(post_event.kind, "post")
        self.assertEqual(post_event.outcome, "ok")
        self.assertIsNone(post_event.render_ms)

    async def test_screenshot_edit_skips_message_already_marked_despawned(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        recorded = []
        edited = []
        cog.metrics_store = types.SimpleNamespace(record=lambda event: recorded.append(event))
        guild = types.SimpleNamespace(id=123, name="Impling Hunters")

        class Message:
            id = 999

            async def edit(self, **kwargs):
                edited.append(kwargs)

        channel = types.SimpleNamespace(id=456, name="rare-imps")
        spawn = module.ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.now(timezone.utc),
        )

        async def make_screenshot_file(_spawn):
            return module.discord.File(io.BytesIO(b"png"), filename="impling-map.png")

        cog._make_screenshot_file = make_screenshot_file
        cog._remember_despawn_notice(channel.id, Message.id)

        await cog._edit_spawn_message_with_screenshot(
            guild,
            channel,
            Message(),
            spawn,
            fetch_ms=None,
            process_ms=None,
        )

        self.assertEqual(edited, [])
        self.assertEqual(recorded[-1].kind, "attachment")
        self.assertEqual(recorded[-1].outcome, "skipped")
        self.assertEqual(recorded[-1].error_category, "message_despawned")

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
        await self._wait_post_poll_tasks(cog)

        self.assertEqual(deleted, [])
        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {spawn.sighting_key: {"111": self._active_message_record(spawn, 222)}}},
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
        await self._wait_post_poll_tasks(cog)

        self.assertEqual(
            cog.config._global_store["active_messages"],
            {"123": {spawn.sighting_key: {"111": self._active_message_record(spawn, 222)}}},
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
            {"123": {spawn.sighting_key: {"111": self._active_message_record(spawn, 333)}}},
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
            {"123": {newer.sighting_key: {"111": self._active_message_record(newer, 445)}}},
        )
        self.assertEqual(
            [(event.kind, event.count_value) for event in recorded if event.kind in {"duplicate", "routed"}],
            [("duplicate", 1), ("routed", 1)],
        )


if __name__ == "__main__":
    unittest.main()
