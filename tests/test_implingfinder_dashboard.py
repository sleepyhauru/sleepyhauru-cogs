import json
import types
import unittest

from tests.support import load_module


class FakeMetricsStore:
    async def summary(self, *, hours, guild_id=None, now=None):
        return {
            "totals": {"fetches": 10, "posts": 2, "errors": 1, "despawns": 1},
            "latency_ms": {
                "fetch": {"average": 42.0, "maximum": 90.0},
                "age_at_fetch": {"average": 20_000.0, "maximum": 30_000.0},
                "end_to_end": {"average": 20_300.0, "maximum": 30_600.0},
            },
        }

    async def hourly(self, *, hours, guild_id=None, now=None):
        return [{"hour": "2026-06-05T18:00:00Z", "kind": "fetch", "count": 10}]

    async def recent_events(self, *, limit, guild_id=None):
        return [{"kind": "post", "guild_name": "Impling Hunters", "world": 489}]

    async def servers(self):
        return [{"id": "123", "name": "Impling Hunters"}]

    def health(self):
        return {
            "status": "ok",
            "queue_depth": 0,
            "dropped_events": 0,
            "database_bytes": 4096,
        }


class ImplingFinderDashboardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_module("implingfinder.dashboard")
        self.dashboard = self.module.DashboardServer(
            FakeMetricsStore(),
            health_provider=lambda: {"bot_uptime_seconds": 120, "active_backoffs": 0},
        )

    def test_application_registers_get_only_routes(self):
        app = self.dashboard.create_app()

        self.assertEqual(
            [(route.method, route.path) for route in app.router.routes],
            [
                ("GET", "/"),
                ("GET", "/api/summary"),
                ("GET", "/api/hourly"),
                ("GET", "/api/events"),
                ("GET", "/healthz"),
            ],
        )

    async def test_index_is_read_only_operational_dashboard(self):
        response = await self.dashboard.handle_index(types.SimpleNamespace(query={}))

        self.assertEqual(response.status, 200)
        self.assertIn("ImplingFinder Performance", response.text)
        self.assertIn("Pipeline latency", response.text)
        self.assertIn("Age at fetch", response.text)
        self.assertIn("Bot after fetch", response.text)
        self.assertIn("Recent events", response.text)
        self.assertNotIn("<form", response.text.lower())
        self.assertEqual(response.headers["Content-Security-Policy"].split(";")[0], "default-src 'self'")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_json_routes_apply_filters_and_limits(self):
        request = types.SimpleNamespace(query={"hours": "48", "guild_id": "123", "limit": "9999"})

        summary = json.loads((await self.dashboard.handle_summary(request)).text)
        hourly = json.loads((await self.dashboard.handle_hourly(request)).text)
        events = json.loads((await self.dashboard.handle_events(request)).text)
        health = json.loads((await self.dashboard.handle_health(request)).text)

        self.assertEqual(summary["hours"], 48)
        self.assertEqual(summary["guild_id"], "123")
        self.assertEqual(hourly["series"][0]["kind"], "fetch")
        self.assertEqual(events["limit"], 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["bot_uptime_seconds"], 120)


if __name__ == "__main__":
    unittest.main()
