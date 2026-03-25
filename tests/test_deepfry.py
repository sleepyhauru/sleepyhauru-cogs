import types
import unittest

from tests.support import load_module


deepfry_module = load_module("deepfry.deepfry")


class DeepfryHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.loop = types.SimpleNamespace(run_in_executor=lambda executor, func: func())
        self.bot = types.SimpleNamespace(loop=self.loop, cog_disabled_in_guild=lambda cog, guild: False)
        self.cog = deepfry_module.Deepfry(self.bot)

    def test_valid_path_type_respects_allow_all_types(self):
        self.assertTrue(self.cog._valid_path_type("image.PNG"))
        self.assertTrue(self.cog._valid_path_type("clip.gif"))
        self.assertFalse(self.cog._valid_path_type("document.txt"))
        self.assertTrue(self.cog._valid_path_type("document.txt", allow_all_types=True))

    def test_get_message_image_url_prefers_attachment_then_embed_then_thumbnail(self):
        message_with_attachment = types.SimpleNamespace(
            attachments=[types.SimpleNamespace(url="https://example.com/image.png")],
            embeds=[],
        )
        self.assertEqual(
            self.cog._get_message_image_url(message_with_attachment),
            "https://example.com/image.png",
        )

        embed_image = types.SimpleNamespace(image=types.SimpleNamespace(url="https://example.com/embed.png"), thumbnail=None)
        message_with_embed = types.SimpleNamespace(attachments=[], embeds=[embed_image])
        self.assertEqual(
            self.cog._get_message_image_url(message_with_embed),
            "https://example.com/embed.png",
        )

        embed_thumb = types.SimpleNamespace(image=None, thumbnail=types.SimpleNamespace(url="https://example.com/thumb.png"))
        message_with_thumb = types.SimpleNamespace(attachments=[], embeds=[embed_thumb])
        self.assertEqual(
            self.cog._get_message_image_url(message_with_thumb),
            "https://example.com/thumb.png",
        )

    def test_get_message_image_url_skips_invalid_attachments_for_valid_ones(self):
        message = types.SimpleNamespace(
            attachments=[
                types.SimpleNamespace(url="https://example.com/readme.txt"),
                types.SimpleNamespace(url="https://example.com/image.png"),
            ],
            embeds=[],
        )

        self.assertEqual(
            self.cog._get_message_image_url(message),
            "https://example.com/image.png",
        )

    async def test_resolve_target_uses_reply_attachment_before_history(self):
        class ReplyOnlyValue:
            async def __call__(self):
                return False

        self.cog.config.guild = lambda guild: types.SimpleNamespace(replyOnly=ReplyOnlyValue())
        ref_attachment = types.SimpleNamespace(url="https://example.com/reply.png")
        ref_message = types.SimpleNamespace(id=222, attachments=[ref_attachment], embeds=[])

        async def fake_ref(ctx):
            return ref_message

        self.cog._get_referenced_message = fake_ref
        channel = types.SimpleNamespace(history=lambda **kwargs: None)
        ctx = types.SimpleNamespace(
            guild=object(),
            message=types.SimpleNamespace(attachments=[]),
            channel=channel,
        )

        result = await self.cog._resolve_target(ctx, None, allow_all_types=False)

        self.assertEqual(result, ("attachment", ref_attachment, "reply attachment from message 222"))

    async def test_resolve_target_skips_invalid_invoking_attachment(self):
        class ReplyOnlyValue:
            async def __call__(self):
                return False

        self.cog.config.guild = lambda guild: types.SimpleNamespace(replyOnly=ReplyOnlyValue())
        valid_attachment = types.SimpleNamespace(url="https://example.com/image.png")
        ctx = types.SimpleNamespace(
            guild=object(),
            message=types.SimpleNamespace(
                attachments=[
                    types.SimpleNamespace(url="https://example.com/readme.txt"),
                    valid_attachment,
                ]
            ),
            channel=types.SimpleNamespace(history=lambda **kwargs: None),
        )

        result = await self.cog._resolve_target(ctx, None, allow_all_types=False)

        self.assertEqual(result, ("attachment", valid_attachment, "invoking message attachment"))

    async def test_resolve_target_reply_only_blocks_history_search(self):
        class ReplyOnlyValue:
            async def __call__(self):
                return True

        self.cog.config.guild = lambda guild: types.SimpleNamespace(replyOnly=ReplyOnlyValue())

        async def fake_ref(ctx):
            return None

        self.cog._get_referenced_message = fake_ref
        ctx = types.SimpleNamespace(
            guild=object(),
            message=types.SimpleNamespace(attachments=[]),
            channel=types.SimpleNamespace(history=lambda **kwargs: None),
        )

        with self.assertRaises(deepfry_module.ImageFindError) as cm:
            await self.cog._resolve_target(ctx, None, allow_all_types=False)

        self.assertEqual(
            str(cm.exception),
            "Reply-only mode is enabled. Reply to a message with an image or provide a direct link.",
        )

    async def test_get_referenced_message_returns_none_on_http_error(self):
        async def fetch_message(message_id):
            raise load_module("discord").HTTPException()

        ctx = types.SimpleNamespace(
            message=types.SimpleNamespace(reference=types.SimpleNamespace(message_id=5)),
            channel=types.SimpleNamespace(fetch_message=fetch_message),
        )

        result = await self.cog._get_referenced_message(ctx)

        self.assertIsNone(result)

    async def test_read_attachment_bytes_rejects_oversized_attachment(self):
        attachment = types.SimpleNamespace(size=11)

        with self.assertRaises(deepfry_module.ImageFindError) as cm:
            await self.cog._read_attachment_bytes(attachment, filesize_limit=10)

        self.assertEqual(str(cm.exception), "That image is too large.")

    async def test_deepfryset_shows_config_without_help(self):
        class GuildConfig:
            async def all(self):
                return {
                    "allowAllTypes": False,
                    "replyOnly": True,
                    "debug": False,
                    "fryChance": 2,
                    "nukeChance": 3,
                }

        sent = []
        help_called = []

        async def send(message):
            sent.append(message)

        async def send_help():
            help_called.append(True)

        self.cog.config.guild = lambda guild: GuildConfig()
        ctx = types.SimpleNamespace(guild=object(), send=send, send_help=send_help)

        await self.cog.deepfryset(ctx)

        self.assertEqual(help_called, [])
        self.assertIn("Reply only mode: True", sent[0])

    async def test_on_message_without_command_uses_first_valid_attachment(self):
        class GuildConfig:
            async def allowAllTypes(self):
                return False

            async def fryChance(self):
                return 1

            async def nukeChance(self):
                return 0

        sent = []
        used_attachments = []

        async def send(*, file):
            sent.append(file)

        def permissions_for(member):
            return types.SimpleNamespace(attach_files=True)

        async def cog_disabled_in_guild(cog, guild):
            return False

        async def read_attachment_bytes(attachment, filesize_limit):
            used_attachments.append(attachment.url)
            return b"data"

        async def immediate_wait_for(task, timeout):
            return task

        self.bot.cog_disabled_in_guild = cog_disabled_in_guild
        self.cog.config.guild = lambda guild: GuildConfig()
        self.cog._read_attachment_bytes = read_attachment_bytes
        self.cog._open_image_from_bytes = lambda data: ("img", False, None)
        self.cog._fry = lambda img: "fried"

        original_wait_for = deepfry_module.asyncio.wait_for
        deepfry_module.asyncio.wait_for = immediate_wait_for
        try:
            msg = types.SimpleNamespace(
                author=types.SimpleNamespace(bot=False),
                attachments=[
                    types.SimpleNamespace(url="https://example.com/readme.txt", size=10),
                    types.SimpleNamespace(url="https://example.com/image.png", size=10),
                ],
                guild=types.SimpleNamespace(filesize_limit=100, me=object()),
                channel=types.SimpleNamespace(permissions_for=permissions_for, send=send),
            )

            await self.cog.on_message_without_command(msg)
        finally:
            deepfry_module.asyncio.wait_for = original_wait_for

        self.assertEqual(used_attachments, ["https://example.com/image.png"])
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
