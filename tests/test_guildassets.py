import tempfile
import types
import unittest
from hashlib import sha256
from pathlib import Path

from tests.support import load_module


guildassets_module = load_module("guildassets.guildassets")


class GuildAssetsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        guildassets_module.cog_data_path = lambda cog: Path(self.tmp.name)
        self.cog = guildassets_module.GuildAssets(types.SimpleNamespace())

    def tearDown(self):
        self.tmp.cleanup()

    def test_name_sanitizers(self):
        self.assertEqual(self.cog._slugify_name("party time!!.png", "fallback"), "party_time_.png")
        self.assertEqual(self.cog._sanitize_emoji_name("!!", "fallback"), "fallback")
        self.assertEqual(self.cog._sanitize_sticker_name("   ", "fallback"), "fallback")

    def test_latest_export_dir_chooses_newest_timestamp(self):
        root = self.cog._guild_export_root(123)
        (root / "20250101T000000Z").mkdir(parents=True)
        (root / "20260101T000000Z").mkdir(parents=True)

        latest = self.cog._latest_export_dir(123)

        self.assertEqual(latest.name, "20260101T000000Z")

    def test_list_and_get_export_dirs(self):
        root = self.cog._guild_export_root(123)
        older = root / "20250101T000000Z"
        newer = root / "20260101T000000Z"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)

        export_dirs = self.cog._list_export_dirs(123)

        self.assertEqual([path.name for path in export_dirs], ["20250101T000000Z", "20260101T000000Z"])
        self.assertEqual(self.cog._get_export_dir(123, "20250101T000000Z"), older)
        self.assertEqual(self.cog._get_export_dir(123, "missing"), None)

    def test_get_export_dir_rejects_path_traversal_timestamps(self):
        root = self.cog._guild_export_root(123)
        (root / "20250101T000000Z").mkdir(parents=True)

        self.assertIsNone(self.cog._get_export_dir(123, "../20250101T000000Z"))
        self.assertIsNone(self.cog._get_export_dir(123, "20250101T000000Z/.."))

    async def test_guildassets_group_shows_export_summary(self):
        root = self.cog._guild_export_root(321)
        (root / "20260101T000000Z").mkdir(parents=True)
        (self.cog._guild_export_root(999) / "20260202T000000Z").mkdir(parents=True)
        sent = []
        guild = types.SimpleNamespace(id=321)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, clean_prefix="!", send=send)

        await self.cog.guildassets(ctx)

        self.assertIn("Saved exports for this guild: `1`", sent[0])
        self.assertIn("Tracked source guilds: `2`", sent[0])
        self.assertIn("Next: run `!guildassets export`", sent[0])

    async def test_export_guild_assets_writes_manifest_and_files(self):
        class FakeEmoji:
            def __init__(self, name, animated, url):
                self.name = name
                self.animated = animated
                self.url = url

        class FakeSticker:
            def __init__(self, name, url, description=None, emoji=None):
                self.name = name
                self.url = url
                self.description = description
                self.emoji = emoji

        payloads = {
            "https://cdn.test/wave.png": b"wave-bytes",
            "https://cdn.test/dance.gif": b"dance-bytes",
            "https://cdn.test/sticker.png": b"sticker-bytes",
        }

        async def fake_read_url(session, url):
            return payloads[url]

        self.cog._read_url = fake_read_url

        guild = types.SimpleNamespace(
            id=321,
            name="Source Guild",
            emojis=[
                FakeEmoji("wave", False, "https://cdn.test/wave.png"),
                FakeEmoji("dance", True, "https://cdn.test/dance.gif"),
            ],
            stickers=[
                FakeSticker("hello sticker", "https://cdn.test/sticker.png", description="desc", emoji="🙂"),
            ],
        )

        export_dir, counts = await self.cog._export_guild_assets(guild)

        self.assertEqual(counts, {"emojis": 2, "stickers": 1})
        manifest = self.cog._load_manifest(export_dir)
        self.assertEqual(manifest["guild_id"], 321)
        self.assertEqual(len(manifest["emojis"]), 2)
        self.assertEqual(len(manifest["stickers"]), 1)
        self.assertEqual(manifest["emojis"][0]["sha256"], sha256(b"wave-bytes").hexdigest())
        self.assertEqual(manifest["stickers"][0]["sha256"], sha256(b"sticker-bytes").hexdigest())
        self.assertTrue((export_dir / manifest["emojis"][0]["filename"]).exists())
        self.assertTrue((export_dir / manifest["stickers"][0]["filename"]).exists())

    async def test_import_guild_assets_creates_items_and_reports_slot_skips(self):
        export_dir = self.cog._guild_export_root(555) / "20260330T000000Z"
        (export_dir / "emojis").mkdir(parents=True)
        (export_dir / "stickers").mkdir(parents=True)
        (export_dir / "emojis" / "001_wave.png").write_bytes(b"emoji-bytes")
        (export_dir / "emojis" / "002_dance.gif").write_bytes(b"emoji2-bytes")
        (export_dir / "stickers" / "001_hi.png").write_bytes(b"sticker-bytes")
        (export_dir / "manifest.json").write_text(
            f"""{{
  "guild_id": 555,
  "emojis": [
    {{"name": "wave", "animated": false, "filename": "emojis/001_wave.png", "sha256": "{sha256(b"emoji-bytes").hexdigest()}"}},
    {{"name": "dance", "animated": true, "filename": "emojis/002_dance.gif", "sha256": "{sha256(b"emoji2-bytes").hexdigest()}"}}
  ],
  "stickers": [
    {{"name": "hi", "description": "desc", "emoji": "🙂", "filename": "stickers/001_hi.png", "sha256": "{sha256(b"sticker-bytes").hexdigest()}"}}
  ]
}}""",
            encoding="utf-8",
        )

        added_emojis = []
        added_stickers = []

        async def create_custom_emoji(**kwargs):
            added_emojis.append(kwargs["name"])
            guild.emojis.append(types.SimpleNamespace(animated=kwargs["image"].startswith(b"emoji2")))

        async def create_sticker(**kwargs):
            added_stickers.append(kwargs["name"])
            guild.stickers.append(types.SimpleNamespace(name=kwargs["name"]))

        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(animated=False)],
            emoji_limit=1,
            stickers=[],
            sticker_limit=1,
            create_custom_emoji=create_custom_emoji,
            create_sticker=create_sticker,
        )

        results = await self.cog._import_guild_assets(guild, export_dir)

        self.assertEqual(results["added_emojis"], ["dance"])
        self.assertEqual(results["skipped_emojis"], ["wave (no static slots)"])
        self.assertEqual(results["added_stickers"], ["hi"])
        self.assertEqual(results["skipped_stickers"], [])
        self.assertEqual(added_stickers, ["hi"])

    async def test_plan_guild_assets_import_reports_adds_and_skips_without_mutating(self):
        export_dir = self.cog._guild_export_root(556) / "20260330T000000Z"
        (export_dir / "emojis").mkdir(parents=True)
        (export_dir / "stickers").mkdir(parents=True)
        (export_dir / "emojis" / "001_wave.png").write_bytes(b"emoji-bytes")
        (export_dir / "emojis" / "002_dance.gif").write_bytes(b"emoji2-bytes")
        (export_dir / "stickers" / "001_hi.png").write_bytes(b"sticker-bytes")
        (export_dir / "manifest.json").write_text(
            f"""{{
  "guild_id": 556,
  "emojis": [
    {{"name": "wave", "animated": false, "filename": "emojis/001_wave.png", "sha256": "{sha256(b"emoji-bytes").hexdigest()}"}},
    {{"name": "dance", "animated": true, "filename": "emojis/002_dance.gif", "sha256": "{sha256(b"emoji2-bytes").hexdigest()}"}}
  ],
  "stickers": [
    {{"name": "hi", "description": "desc", "emoji": "🙂", "filename": "stickers/001_hi.png", "sha256": "{sha256(b"sticker-bytes").hexdigest()}"}}
  ]
}}""",
            encoding="utf-8",
        )

        async def create_custom_emoji(**kwargs):
            raise AssertionError("preview should not create emojis")

        async def create_sticker(**kwargs):
            raise AssertionError("preview should not create stickers")

        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(animated=False)],
            emoji_limit=1,
            stickers=[],
            sticker_limit=1,
            create_custom_emoji=create_custom_emoji,
            create_sticker=create_sticker,
        )

        plan = await self.cog._plan_guild_assets_import(guild, export_dir)

        self.assertEqual(plan["source_guild_id"], 556)
        self.assertEqual(plan["added_emojis"], ["dance"])
        self.assertEqual(plan["skipped_emojis"], ["wave (no static slots)"])
        self.assertEqual(plan["added_stickers"], ["hi"])
        self.assertEqual(plan["skipped_stickers"], [])
        self.assertEqual([item["name"] for item in plan["emoji_payloads"]], ["dance"])
        self.assertEqual([item["name"] for item in plan["sticker_payloads"]], ["hi"])

    async def test_import_guild_assets_skips_existing_name_and_hash_matches(self):
        export_dir = self.cog._guild_export_root(777) / "20260330T000000Z"
        (export_dir / "emojis").mkdir(parents=True)
        (export_dir / "stickers").mkdir(parents=True)
        (export_dir / "emojis" / "001_wave.png").write_bytes(b"same-emoji")
        (export_dir / "stickers" / "001_hi.png").write_bytes(b"same-sticker")
        (export_dir / "manifest.json").write_text(
            """{
  "guild_id": 777,
  "emojis": [
    {"name": "wave", "animated": false, "filename": "emojis/001_wave.png"}
  ],
  "stickers": [
    {"name": "hi", "description": "desc", "emoji": "🙂", "filename": "stickers/001_hi.png"}
  ]
}""",
            encoding="utf-8",
        )

        async def create_custom_emoji(**kwargs):
            raise AssertionError("duplicate emoji should have been skipped")

        async def create_sticker(**kwargs):
            raise AssertionError("duplicate sticker should have been skipped")

        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(name="wave", animated=False, url="https://cdn.test/existing-wave.png")],
            emoji_limit=5,
            stickers=[types.SimpleNamespace(name="hi", url="https://cdn.test/existing-hi.png")],
            sticker_limit=5,
            create_custom_emoji=create_custom_emoji,
            create_sticker=create_sticker,
        )

        async def fake_read_url(session, url):
            payloads = {
                "https://cdn.test/existing-wave.png": b"same-emoji",
                "https://cdn.test/existing-hi.png": b"same-sticker",
            }
            return payloads[url]

        self.cog._read_url = fake_read_url

        results = await self.cog._import_guild_assets(guild, export_dir)

        self.assertEqual(results["added_emojis"], [])
        self.assertEqual(results["added_stickers"], [])
        self.assertEqual(results["skipped_emojis"], ["wave (already exists)"])
        self.assertEqual(results["skipped_stickers"], ["hi (already exists)"])

    async def test_import_guild_assets_skips_existing_animated_name_when_cdn_bytes_differ(self):
        export_dir = self.cog._guild_export_root(778) / "20260330T000000Z"
        (export_dir / "emojis").mkdir(parents=True)
        (export_dir / "stickers").mkdir(parents=True)
        (export_dir / "emojis" / "001_dance.gif").write_bytes(b"exported-gif-bytes")
        (export_dir / "manifest.json").write_text(
            """{
  "guild_id": 778,
  "emojis": [
    {"name": "dance", "animated": true, "filename": "emojis/001_dance.gif"}
  ],
  "stickers": []
}""",
            encoding="utf-8",
        )

        async def create_custom_emoji(**kwargs):
            raise AssertionError("duplicate animated emoji should have been skipped")

        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(name="dance", animated=True, url="https://cdn.test/existing-dance.gif")],
            emoji_limit=5,
            stickers=[],
            sticker_limit=5,
            create_custom_emoji=create_custom_emoji,
            create_sticker=None,
        )

        async def fake_read_url(session, url):
            if url == "https://cdn.test/existing-dance.gif":
                raise load_module("aiohttp").ClientError("gif unavailable")
            if url == "https://cdn.test/existing-dance.webp?animated=true":
                return b"existing-webp-bytes"
            raise AssertionError(f"unexpected url: {url}")

        self.cog._read_url = fake_read_url

        results = await self.cog._import_guild_assets(guild, export_dir)

        self.assertEqual(results["added_emojis"], [])
        self.assertEqual(results["skipped_emojis"], ["dance (already exists)"])
        self.assertEqual(results["added_stickers"], [])
        self.assertEqual(results["skipped_stickers"], [])

    async def test_import_guild_assets_retries_and_paces_emoji_uploads(self):
        export_dir = self.cog._guild_export_root(888) / "20260330T000000Z"
        (export_dir / "emojis").mkdir(parents=True)
        (export_dir / "stickers").mkdir(parents=True)
        (export_dir / "emojis" / "001_wave.png").write_bytes(b"emoji-bytes")
        (export_dir / "manifest.json").write_text(
            f"""{{
  "guild_id": 888,
  "emojis": [
    {{"name": "wave", "animated": false, "filename": "emojis/001_wave.png", "sha256": "{sha256(b"emoji-bytes").hexdigest()}"}}
  ],
  "stickers": []
}}""",
            encoding="utf-8",
        )

        sleep_calls = []
        original_sleep = guildassets_module.asyncio.sleep

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        guildassets_module.asyncio.sleep = fake_sleep

        attempts = {"count": 0}

        async def create_custom_emoji(**kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise load_module("discord").HTTPException("rate limited")

        guild = types.SimpleNamespace(
            emojis=[],
            emoji_limit=5,
            stickers=[],
            sticker_limit=5,
            create_custom_emoji=create_custom_emoji,
            create_sticker=None,
        )

        try:
            results = await self.cog._import_guild_assets(guild, export_dir)
        finally:
            guildassets_module.asyncio.sleep = original_sleep

        self.assertEqual(results["added_emojis"], ["wave"])
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(sleep_calls, [guildassets_module.EMOJI_UPLOAD_RETRY_BASE, guildassets_module.EMOJI_UPLOAD_DELAY])

    async def test_guildassets_preview_summarizes_planned_import(self):
        export_dir = self.cog._guild_export_root(999) / "20260330T000000Z"
        export_dir.mkdir(parents=True)
        sent = []
        guild = types.SimpleNamespace(name="Target Guild")

        async def send(message):
            sent.append(message)

        class Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        ctx = types.SimpleNamespace(
            guild=guild,
            clean_prefix="!",
            send=send,
            typing=lambda: Typing(),
        )

        async def fake_plan(target_guild, preview_dir):
            self.assertIs(target_guild, guild)
            self.assertEqual(preview_dir, export_dir)
            return {
                "source_guild_id": 999,
                "added_emojis": ["dance"],
                "skipped_emojis": ["wave (already exists)"],
                "added_stickers": ["hi"],
                "skipped_stickers": ["bye (no sticker slots)"],
                "emoji_payloads": [],
                "sticker_payloads": [],
            }

        self.cog._plan_guild_assets_import = fake_plan

        await self.cog.guildassets_preview(ctx, 999, "20260330T000000Z")

        self.assertEqual(len(sent), 1)
        self.assertIn("Preview import from `999` into `Target Guild` using `20260330T000000Z`.", sent[0])
        self.assertIn("Would add emojis: 1", sent[0])
        self.assertIn("Emoji plan: dance", sent[0])
        self.assertIn("Skipped emojis: wave (already exists)", sent[0])
        self.assertIn("Run `!guildassets import 999 20260330T000000Z` to apply this import.", sent[0])
