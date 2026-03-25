import types
import unittest
import zipfile
from io import BytesIO

from tests.support import load_module


emojisteal_module = load_module("emojisteal.emojisteal")


class EmojiStealHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = types.SimpleNamespace(add_command=lambda command: None, remove_command=lambda *args, **kwargs: None)
        self.cog = emojisteal_module.EmojiSteal(types.SimpleNamespace(tree=tree))

    def test_get_emojis_parses_static_and_animated_custom_emojis(self):
        content = "hello <:wave:123456789012345678> <a:dance:987654321098765432>"

        emojis = self.cog.get_emojis(content)

        self.assertEqual(len(emojis), 2)
        self.assertFalse(emojis[0].animated)
        self.assertTrue(emojis[1].animated)
        self.assertEqual(emojis[0].name, "wave")
        self.assertEqual(emojis[1].name, "dance")

    def test_sanitize_names_and_join_names(self):
        sanitized = self.cog._sanitize_names(["  hi!!  ", "x", "party_time!!"])

        self.assertEqual(sanitized, ["hi", None, "party_time"])
        self.assertEqual(self.cog._join_names(["one", "two"]), "one, two")

    def test_available_emoji_slots_counts_per_type(self):
        guild = types.SimpleNamespace(
            emojis=[types.SimpleNamespace(animated=False), types.SimpleNamespace(animated=True)],
            emoji_limit=5,
        )

        self.assertEqual(self.cog.available_emoji_slots(guild, False), 4)
        self.assertEqual(self.cog.available_emoji_slots(guild, True), 4)

    async def test_send_steal_info_reports_deduplicated_emoji_counts_and_slots(self):
        sent = []

        async def send(message):
            sent.append(message)

        emoji_static = load_module("discord").PartialEmoji(name="wave", animated=False, id=1)
        emoji_dup = load_module("discord").PartialEmoji(name="wave", animated=False, id=1)
        emoji_anim = load_module("discord").PartialEmoji(name="dance", animated=True, id=2)
        guild = types.SimpleNamespace(
            emojis=[
                types.SimpleNamespace(animated=False),
                types.SimpleNamespace(animated=True),
                types.SimpleNamespace(animated=True),
            ],
            emoji_limit=5,
        )

        await self.cog._send_steal_info(types.SimpleNamespace(send=send), guild, [emoji_static, emoji_dup, emoji_anim])

        self.assertEqual(
            sent[0],
            "Found 2 custom emojis.\n"
            "- Static: 1\n"
            "- Animated: 1\n"
            "- Static slots remaining: 4\n"
            "- Animated slots remaining: 3",
        )

    async def test_send_steal_info_reports_sticker_counts(self):
        sent = []

        async def send(message):
            sent.append(message)

        StickerItem = load_module("discord").StickerItem

        class FakeSticker(StickerItem):
            def __init__(self, name):
                self.name = name

        stickers = [FakeSticker("one"), FakeSticker("two")]
        guild = types.SimpleNamespace(sticker_limit=5, stickers=[1, 2])

        await self.cog._send_steal_info(types.SimpleNamespace(send=send), guild, stickers)

        self.assertEqual(
            sent[0],
            "Found 2 stickers.\n- `one`\n- `two`\nSticker slots remaining: 3",
        )

    async def test_steal_ctx_handles_missing_reference_and_missing_content(self):
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(
            message=types.SimpleNamespace(reference=None),
            send=send,
        )

        result = await self.cog.steal_ctx(ctx)
        self.assertIsNone(result)
        self.assertEqual(sent, [emojisteal_module.MISSING_REFERENCE])

        async def fetch_message(message_id):
            return types.SimpleNamespace(stickers=[], content="plain text")

        ctx = types.SimpleNamespace(
            message=types.SimpleNamespace(reference=types.SimpleNamespace(message_id=5)),
            channel=types.SimpleNamespace(fetch_message=fetch_message),
            send=send,
        )
        sent.clear()
        result = await self.cog.steal_ctx(ctx)
        self.assertIsNone(result)
        self.assertEqual(sent, [emojisteal_module.MISSING_EMOJIS])

    async def test_getemoji_handles_numeric_id_and_invalid_value(self):
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(send=send)

        await self.cog.getemoji(ctx, emoji="123456789012345678")
        self.assertEqual(len(sent[0].splitlines()), 2)

        sent.clear()
        await self.cog.getemoji(ctx, emoji="not an emoji")
        self.assertEqual(sent, [emojisteal_module.INVALID_EMOJI])

    async def test_upload_stickers_rewinds_file_before_upload(self):
        StickerItem = load_module("discord").StickerItem

        class FakeSticker(StickerItem):
            def __init__(self, name):
                self.name = name

            async def save(self, fp):
                fp.write(b"sticker-bytes")

        seen_positions = []

        async def create_sticker(**kwargs):
            seen_positions.append(kwargs["file"].fp.tell())
            return types.SimpleNamespace(name=kwargs["name"])

        guild = types.SimpleNamespace(stickers=[], sticker_limit=5, create_sticker=create_sticker)

        uploaded, error = await self.cog._upload_stickers(guild, [FakeSticker("wave")])

        self.assertEqual(uploaded, ["wave"])
        self.assertIsNone(error)
        self.assertEqual(seen_positions, [0])

    async def test_uploadsticker_accepts_uppercase_zip_and_png_inside(self):
        sent = []
        captured = {}
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("STICKER.PNG", b"png-data")
        zip_bytes = zip_buffer.getvalue()

        async def send(content=None):
            sent.append(content)

        async def typing():
            return None

        async def save(fp):
            fp.write(zip_bytes)

        async def create_sticker(**kwargs):
            captured["name"] = kwargs["name"]
            captured["position"] = kwargs["file"].fp.tell()
            return types.SimpleNamespace(name=kwargs["name"])

        attachment = types.SimpleNamespace(
            filename="fun.sticker.ZIP",
            size=len(zip_bytes),
            width=None,
            height=None,
            save=save,
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(stickers=[], sticker_limit=5, create_sticker=create_sticker),
            message=types.SimpleNamespace(attachments=[attachment]),
            author="tester",
            send=send,
            typing=typing,
        )

        await self.cog.uploadsticker(ctx)

        self.assertEqual(captured["name"], "fun.sticker")
        self.assertEqual(captured["position"], 0)
        self.assertEqual(sent, [f"{emojisteal_module.STICKER_SUCCESS}: fun.sticker"])

    async def test_uploadsticker_rejects_non_sticker_attachment_case_insensitively(self):
        sent = []

        async def send(content=None):
            sent.append(content)

        attachment = types.SimpleNamespace(
            filename="notes.TXT",
            size=10,
            width=None,
            height=None,
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(stickers=[], sticker_limit=5),
            message=types.SimpleNamespace(attachments=[attachment]),
            send=send,
        )

        await self.cog.uploadsticker(ctx)

        self.assertEqual(sent, [emojisteal_module.STICKER_ATTACHMENT])

    async def test_uploadsticker_rejects_zip_with_multiple_pngs(self):
        sent = []
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("one.png", b"a")
            zf.writestr("two.png", b"b")
        zip_bytes = zip_buffer.getvalue()

        async def send(content=None):
            sent.append(content)

        async def typing():
            return None

        async def save(fp):
            fp.write(zip_bytes)

        async def create_sticker(**kwargs):
            raise AssertionError("create_sticker should not be called for invalid zip")

        attachment = types.SimpleNamespace(
            filename="multi.zip",
            size=len(zip_bytes),
            width=None,
            height=None,
            save=save,
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(stickers=[], sticker_limit=5, create_sticker=create_sticker),
            message=types.SimpleNamespace(attachments=[attachment]),
            author="tester",
            send=send,
            typing=typing,
        )

        await self.cog.uploadsticker(ctx)

        self.assertEqual(sent, [emojisteal_module.STICKER_ATTACHMENT])

    async def test_uploadsticker_rejects_zip_with_suspicious_path(self):
        sent = []
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("../evil.png", b"a")
        zip_bytes = zip_buffer.getvalue()

        async def send(content=None):
            sent.append(content)

        async def typing():
            return None

        async def save(fp):
            fp.write(zip_bytes)

        async def create_sticker(**kwargs):
            raise AssertionError("create_sticker should not be called for invalid zip")

        attachment = types.SimpleNamespace(
            filename="bad.zip",
            size=len(zip_bytes),
            width=None,
            height=None,
            save=save,
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(stickers=[], sticker_limit=5, create_sticker=create_sticker),
            message=types.SimpleNamespace(attachments=[attachment]),
            author="tester",
            send=send,
            typing=typing,
        )

        await self.cog.uploadsticker(ctx)

        self.assertEqual(sent, [emojisteal_module.STICKER_ATTACHMENT])

    async def test_uploadsticker_rejects_zip_with_oversized_png_payload(self):
        sent = []
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("large.png", b"a" * (emojisteal_module.STICKER_KB * 1024 + 1))
        zip_bytes = zip_buffer.getvalue()

        async def send(content=None):
            sent.append(content)

        async def typing():
            return None

        async def save(fp):
            fp.write(zip_bytes)

        async def create_sticker(**kwargs):
            raise AssertionError("create_sticker should not be called for oversized zip payload")

        attachment = types.SimpleNamespace(
            filename="large.zip",
            size=len(zip_bytes),
            width=None,
            height=None,
            save=save,
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(stickers=[], sticker_limit=5, create_sticker=create_sticker),
            message=types.SimpleNamespace(attachments=[attachment]),
            author="tester",
            send=send,
            typing=typing,
        )

        await self.cog.uploadsticker(ctx)

        self.assertEqual(sent, [emojisteal_module.STICKER_TOO_BIG])


if __name__ == "__main__":
    unittest.main()
