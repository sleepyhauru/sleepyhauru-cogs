import types
import unittest

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
                f"https://7tv.io/v3/emotes/{emote_id}",
                f"https://api.7tv.app/v2/emotes/{emote_id}",
            ],
        )

    async def test_fetch_7tv_asset_via_meta_prefers_valid_gif_candidate(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        meta_url = f"https://7tv.io/v3/emotes/{emote_id}"
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

        self.assertEqual(result, (b"gif-bytes", True, "gif"))
        self.assertEqual(
            session.calls,
            [meta_url, "https://cdn.7tv.app/emote/host/2x.gif"],
        )

    async def test_fetch_7tv_bytes_falls_back_to_cdn_when_meta_selection_fails(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        meta_url = f"https://7tv.io/v3/emotes/{emote_id}"
        session = FakeSession(
            {
                meta_url: FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/4x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/3x.gif": FakeResponse(404),
                f"https://cdn.7tv.app/emote/{emote_id}/2x.gif": FakeResponse(200, body=b"gif-fallback"),
            }
        )

        result = await seventv._fetch_7tv_bytes(session, emote_id)

        self.assertEqual(result, (b"gif-fallback", True, "gif"))
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
                f"https://7tv.io/v3/emotes/{emote_id}": FakeResponse(500),
                f"https://api.7tv.app/v2/emotes/{emote_id}": FakeResponse(500),
            }
        )

        result = await seventv._fetch_7tv_meta(session, emote_id)

        self.assertEqual(result, (None, None))

    async def test_fetch_7tv_asset_via_meta_returns_none_for_invalid_meta(self):
        emote_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        session = FakeSession(
            {
                f"https://7tv.io/v3/emotes/{emote_id}": FakeResponse(
                    200,
                    json_data={"host": {"url": "", "files": "bad"}},
                )
            }
        )

        result = await seventv._fetch_7tv_asset_via_meta(session, emote_id)

        self.assertEqual(result, (None, None, None))


class SevenTVCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_seven_tv_rejects_invalid_link(self):
        cog = seventv.SevenTV(bot=object())
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(send=send, guild=object())

        await cog.seven_tv(ctx, "https://example.com/not-7tv")

        self.assertEqual(sent, [seventv.INVALID_LINK])


if __name__ == "__main__":
    unittest.main()
