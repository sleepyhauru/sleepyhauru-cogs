import asyncio
from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest


class ImplingFinderMetricsTest(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_queue_drops_without_waiting(self):
        from implingfinder.metrics import MetricEvent, MetricsStore

        with tempfile.TemporaryDirectory() as tmp:
            store = MetricsStore(Path(tmp) / "metrics.sqlite3", queue_size=1)

            self.assertTrue(store.record(MetricEvent(kind="fetch", duration_ms=12.5)))
            self.assertFalse(store.record(MetricEvent(kind="fetch", duration_ms=15.0)))
            self.assertEqual(store.dropped_events, 1)
            self.assertEqual(store.queue_depth, 1)

    async def test_events_update_queries_and_hourly_aggregates(self):
        from implingfinder.metrics import MetricEvent, MetricsStore

        now = datetime(2026, 6, 5, 18, 30, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            store = MetricsStore(Path(tmp) / "metrics.sqlite3")
            await store.start()
            self.addAsyncCleanup(store.stop)

            store.record(
                MetricEvent(
                    kind="post",
                    occurred_at=now,
                    guild_id="123",
                    guild_name="Impling Hunters",
                    channel_id="456",
                    channel_name="dragon-imps",
                    impling_type="dragon",
                    world=489,
                    location="Crafting Guild",
                    duration_ms=220,
                    fetch_ms=80,
                    process_ms=20,
                    render_ms=70,
                    send_ms=50,
                    end_to_end_ms=4_200,
                )
            )
            store.record(
                MetricEvent(
                    kind="attachment",
                    occurred_at=now,
                    guild_id="123",
                    guild_name="Impling Hunters",
                    channel_id="456",
                    channel_name="dragon-imps",
                    impling_type="dragon",
                    world=489,
                    location="Crafting Guild",
                    duration_ms=180,
                    render_ms=150,
                    send_ms=30,
                    end_to_end_ms=4_500,
                )
            )
            store.record(
                MetricEvent(
                    kind="fetch",
                    outcome="error",
                    occurred_at=now,
                    guild_id="123",
                    guild_name="Impling Hunters",
                    duration_ms=500,
                    fetch_ms=500,
                    error_category="timeout",
                )
            )
            await store.flush()

            summary = await store.summary(hours=24, guild_id="123", now=now + 60)
            events = await store.recent_events(limit=10, guild_id="123")
            hourly = await store.hourly(hours=24, guild_id="123", now=now + 60)
            servers = await store.servers()

            self.assertEqual(summary["totals"]["posts"], 1)
            self.assertEqual(summary["totals"]["attachments"], 1)
            self.assertEqual(summary["totals"]["errors"], 1)
            self.assertEqual(summary["latency_ms"]["fetch"]["average"], 500.0)
            self.assertEqual(summary["latency_ms"]["send"]["average"], 50.0)
            self.assertEqual(events[0]["error_category"], "timeout")
            self.assertEqual(events[1]["kind"], "attachment")
            self.assertEqual(events[1]["channel_name"], "dragon-imps")
            self.assertEqual(events[1]["location"], "Crafting Guild")
            self.assertEqual(len(hourly), 3)
            self.assertEqual(servers, [{"id": "123", "name": "Impling Hunters"}])

    async def test_retention_keeps_seven_day_events_and_thirty_day_aggregates(self):
        from implingfinder.metrics import MetricEvent, MetricsStore

        now = datetime(2026, 6, 5, 18, 0, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            store = MetricsStore(Path(tmp) / "metrics.sqlite3")
            await store.start()
            self.addAsyncCleanup(store.stop)

            store.record(MetricEvent(kind="fetch", occurred_at=now - 8 * 86400))
            store.record(MetricEvent(kind="fetch", occurred_at=now - 2 * 86400))
            await store.flush()
            await store.prune(now=now)

            events = await store.recent_events(limit=10)
            hourly = await store.hourly(hours=30 * 24, now=now)

            self.assertEqual(len(events), 1)
            self.assertEqual(len(hourly), 2)


if __name__ == "__main__":
    unittest.main()
