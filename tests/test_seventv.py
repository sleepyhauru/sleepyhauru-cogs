import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import load_module


seventv = load_module("seventv.seventv")


class FakeResponse:
    def __init__(self, status, *, json_data=None, body=b""):
        self.status = status
        self._json_data = json_data
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_data

    async def read(self):
        return self._body


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append(url)
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value


class SevenTVHelpersTest(unittest.TestCase):
    def test_extract_7tv_id_supports_long_v3_ids(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        self.assertEqual(
            seventv._extract_7tv_id(f"https://7tv.app/emotes/{emote_id}"),
            emote_id,
        )
        self.assertEqual(
            seventv._extract_7tv_id(f"https://cdn.7tv.app/emote/{emote_id}/4x.gif"),
            emote_id,
        )

    def test_sanitize_name_strips_invalid_chars_and_truncates(self):
        raw_name = "!!party time!!__" + ("x" * 40)
        sanitized = seventv._sanitize_name(raw_name)
        self.assertEqual(sanitized, "partytime__" + ("x" * 21))
        self.assertIsNone(seventv._sanitize_name("!"))

    def test_available_emoji_slots_counts_by_animation_type(self):
        guild = types.SimpleNamespace(
            emojis=[
                types.SimpleNamespace(animated=False),
                types.SimpleNamespace(animated=False),
                types.SimpleNamespace(animated=True),
            ],
            emoji_limit=5,
        )

        self.assertEqual(seventv._available_emoji_slots(guild, animated=False), 3)
        self.assertEqual(seventv._available_emoji_slots(guild, animated=True), 4)


class SevenTVFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_7tv_meta_falls_back_to_v2(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        session = FakeSession(
            {
                f"https://7tv.io/v3/emotes/{emote_id}": FakeResponse(404),
                f"https://api.7tv.app/v2/emotes/{emote_id}": FakeResponse(
                    200,
                    json_data={"name": "Wave", "animated": 1},
                ),
            }
        )

        result = await seventv._fetch_7tv_meta(session, emote_id)

        self.assertEqual(result, ("Wave", True))
        self.assertEqual(
            session.calls,
            [
                seventv.SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id),
                seventv.SEVENTV_V2_EMOTE_URL.format(emote_id=emote_id),
            ],
        )

    async def test_fetch_7tv_asset_via_meta_prefers_valid_gif_candidate(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        meta_url = seventv.SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id)
        session = FakeSession(
            {
                meta_url: FakeResponse(
                    200,
                    json_data={
                        "host": {
                            "url": "//cdn.7tv.app/emote/host",
                            "files": [
                                {"name": "4x.gif", "format": "GIF", "size": 300000},
                                {"name": "2x.gif", "format": "GIF", "size": 1024},
                                {"name": "4x.png", "format": "PNG", "size": 512},
                            ],
                        }
                    },
                ),
                "https://cdn.7tv.app/emote/host/2x.gif": FakeResponse(200, body=b"gif-bytes"),
            }
        )

        result = await seventv._fetch_7tv_asset_via_meta(session, emote_id)

        self.assertEqual(result.data, b"gif-bytes")
        self.assertTrue(result.is_animated)
        self.assertEqual(result.ext, "gif")
        self.assertEqual(
            session.calls,
            [meta_url, "https://cdn.7tv.app/emote/host/2x.gif"],
        )

    async def test_fetch_7tv_bytes_falls_back_to_cdn_when_meta_selection_fails(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        meta_url = seventv.SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id)
        session = FakeSession(
            {
                meta_url: FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/4x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/3x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/2x.gif": FakeResponse(200, body=b"gif-fallback"),
            }
        )

        result = await seventv._fetch_7tv_bytes(session, emote_id)

        self.assertEqual(result.data, b"gif-fallback")
        self.assertTrue(result.is_animated)
        self.assertEqual(result.ext, "gif")
        self.assertEqual(
            session.calls[:4],
            [
                meta_url,
                f"https://cdn.7tv.app/emote/{emote_id}/4x.gif",
                f"https://cdn.7tv.app/emote/{emote_id}/3x.gif",
                f"https://cdn.7tv.app/emote/{emote_id}/2x.gif",
            ],
        )

    async def test_fetch_7tv_meta_returns_none_when_all_sources_fail(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        session = FakeSession(
            {
                seventv.SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id): FakeResponse(500),
                seventv.SEVENTV_V2_EMOTE_URL.format(emote_id=emote_id): FakeResponse(500),
            }
        )

        result = await seventv._fetch_7tv_meta(session, emote_id)

        self.assertEqual(result, (None, None))

    async def test_fetch_7tv_asset_via_meta_returns_none_for_invalid_meta(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        session = FakeSession(
            {
                seventv.SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id): FakeResponse(
                    200,
                    json_data={"host": {"url": "", "files": "bad"}},
                )
            }
        )

        result = await seventv._fetch_7tv_asset_via_meta(session, emote_id)

        self.assertEqual(result.reason, "unavailable")

    async def test_fetch_7tv_bytes_reports_too_large_when_only_oversized_assets_exist(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        meta_url = seventv.SEVENTV_V3_EMOTE_URL.format(emote_id=emote_id)
        session = FakeSession(
            {
                meta_url: FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/4x.gif": FakeResponse(
                    200, body=b"x" * (seventv.DISCORD_EMOJI_SIZE_LIMIT + 1)
                ),
                f"https://cdn.7tv.app/emote/{emote_id}/3x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/2x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/1x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/4x.png": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/3x.png": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/2x.png": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/1x.png": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/4x.webp": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/3x.webp": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/2x.webp": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/1x.webp": FakeResponse(404),
            }
        )

        result = await seventv._fetch_7tv_bytes(session, emote_id)

        self.assertIsNone(result.data)
        self.assertEqual(result.reason, "too_large")


class SevenTVCommandTest(unittest.IsolatedAsyncioTestCase):
    def _make_ctx(self, guild=None):
        sent = []
        reactions = []

        async def send(message):
            sent.append(message)

        async def add_reaction(emoji):
            reactions.append(emoji)

        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        ctx = types.SimpleNamespace(
            send=send,
            guild=guild,
            message=types.SimpleNamespace(add_reaction=add_reaction),
            typing=lambda: TypingContext(),
        )
        return ctx, sent, reactions

    async def test_seven_tv_rejects_invalid_link(self):
        cog = seventv.SevenTV(bot=object())
        ctx, sent, _ = self._make_ctx(guild=object())

        await cog.seven_tv(ctx, "https://example.com/not-7tv")

        self.assertEqual(sent, [seventv.INVALID_LINK])

    async def test_seven_tv_uploads_and_suffixes_duplicate_name(self):
        emoji = types.SimpleNamespace(name="Wave_2")
        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(name="Wave", animated=False)],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=emoji),
        )
        ctx, sent, reactions = self._make_ctx(guild=guild)
        cog = seventv.SevenTV(bot=object())
        fake_session = object()
        result = seventv.AssetResult(b"png-bytes", False, "png")

        with (
            patch.object(cog, "_get_session", AsyncMock(return_value=fake_session)),
            patch.object(seventv, "_fetch_7tv_v3_emote", AsyncMock(return_value={"name": "Wave", "animated": False})),
            patch.object(seventv, "_fetch_7tv_meta", AsyncMock(return_value=("Wave", False))),
            patch.object(seventv, "_fetch_7tv_bytes", AsyncMock(return_value=result)),
        ):
            await cog.seven_tv(ctx, "https://7tv.app/emotes/01ARZ3NDEKTSV4RRFFQ69G5FAV")

        guild.create_custom_emoji.assert_awaited_once_with(name="Wave_2", image=b"png-bytes")
        self.assertEqual(sent, ["Uploaded: namespace(name='Wave_2') `Wave_2`"])
        self.assertEqual(reactions, [emoji])

    async def test_seven_tv_reports_slot_exhaustion(self):
        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(name="Wave", animated=False)],
            emoji_limit=1,
        )
        ctx, sent, _ = self._make_ctx(guild=guild)
        cog = seventv.SevenTV(bot=object())
        result = seventv.AssetResult(b"png-bytes", False, "png")

        with (
            patch.object(cog, "_get_session", AsyncMock(return_value=object())),
            patch.object(seventv, "_fetch_7tv_v3_emote", AsyncMock(return_value={"name": "Wave", "animated": False})),
            patch.object(seventv, "_fetch_7tv_meta", AsyncMock(return_value=("Wave", False))),
            patch.object(seventv, "_fetch_7tv_bytes", AsyncMock(return_value=result)),
        ):
            await cog.seven_tv(ctx, "https://7tv.app/emotes/01ARZ3NDEKTSV4RRFFQ69G5FAV")

        self.assertEqual(sent, [seventv.EMOJI_SLOTS])

    async def test_seven_tv_reports_failed_webp_conversion(self):
        guild = types.SimpleNamespace(emojis=[], emoji_limit=10)
        ctx, sent, _ = self._make_ctx(guild=guild)
        cog = seventv.SevenTV(bot=object())
        result = seventv.AssetResult(b"webp-bytes", None, "webp")

        with (
            patch.object(cog, "_get_session", AsyncMock(return_value=object())),
            patch.object(seventv, "_fetch_7tv_v3_emote", AsyncMock(return_value={"name": "Wave", "animated": False})),
            patch.object(seventv, "_fetch_7tv_meta", AsyncMock(return_value=("Wave", False))),
            patch.object(seventv, "_fetch_7tv_bytes", AsyncMock(return_value=result)),
            patch.object(cog, "_webp_to_png_under_limit", AsyncMock(return_value=None)),
        ):
            await cog.seven_tv(ctx, "https://7tv.app/emotes/01ARZ3NDEKTSV4RRFFQ69G5FAV")

        self.assertEqual(sent, [seventv.FETCH_FAIL_WEBP])

    async def test_seven_tv_info_reports_metadata(self):
        cog = seventv.SevenTV(bot=object())
        fake_session = object()
        ctx, sent, _ = self._make_ctx(guild=object())
        result = seventv.AssetResult(b"gif-bytes", True, "gif")

        with (
            patch.object(cog, "_get_session", AsyncMock(return_value=fake_session)),
            patch.object(seventv, "_fetch_7tv_v3_emote", AsyncMock(return_value={"name": "Wave", "animated": True})),
            patch.object(seventv, "_fetch_7tv_meta", AsyncMock(return_value=("Wave", True))),
            patch.object(seventv, "_fetch_7tv_bytes", AsyncMock(return_value=result)),
        ):
            await cog.seven_tv_info(ctx, "https://7tv.app/emotes/01ARZ3NDEKTSV4RRFFQ69G5FAV")

        self.assertIn("Name: `Wave`", sent[0])
        self.assertIn("Animated: `True`", sent[0])
        self.assertIn("Best asset: `GIF asset within limit`", sent[0])


if __name__ == "__main__":
    unittest.main()
