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

    async def test_assert_safe_remote_url_rejects_private_ip_literal(self):
        with self.assertRaises(deepfry_module.ImageFindError) as cm:
            await self.cog._assert_safe_remote_url("http://127.0.0.1/image.png")

        self.assertEqual(str(cm.exception), "That image URL is not allowed.")

    async def test_assert_safe_remote_url_rejects_hostnames_resolving_to_private_ips(self):
        async def fake_resolve(hostname, port):
            self.assertEqual((hostname, port), ("example.com", 443))
            return {"10.0.0.5"}

        self.cog._resolve_hostname_addresses = fake_resolve

        with self.assertRaises(deepfry_module.ImageFindError) as cm:
            await self.cog._assert_safe_remote_url("https://example.com/image.png")

        self.assertEqual(str(cm.exception), "That image URL is not allowed.")

    async def test_assert_safe_remote_url_allows_public_hosts(self):
        async def fake_resolve(hostname, port):
            self.assertEqual((hostname, port), ("example.com", 443))
            return {"93.184.216.34"}

        self.cog._resolve_hostname_addresses = fake_resolve

        await self.cog._assert_safe_remote_url("https://example.com/image.png")

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

    def test_source_filesize_limit_uses_processing_budget(self):
        self.assertEqual(self.cog._source_filesize_limit(10), deepfry_module.MAX_SOURCE_SIZE)
        self.assertEqual(self.cog._source_filesize_limit(50_000_000), 50_000_000)

    def test_open_image_from_bytes_downscales_large_static_images(self):
        class FakeImage:
            def __init__(self):
                self.size = (8000, 4000)
                self.is_animated = False
                self.n_frames = 1
                self.info = {}

            def convert(self, mode):
                return self

            def resize(self, size, resample):
                self.size = size
                return self

        fake = FakeImage()
        original_open = deepfry_module.Image.open
        deepfry_module.Image.open = lambda data: fake
        try:
            img, isgif, duration = self.cog._open_image_from_bytes(b"data")
        finally:
            deepfry_module.Image.open = original_open

        self.assertFalse(isgif)
        self.assertIsNone(duration)
        self.assertLess(img.size[0], 8000)
        self.assertLessEqual(max(img.size), deepfry_module.MAX_DIMENSION)
        self.assertLessEqual(img.size[0] * img.size[1], deepfry_module.MAX_PIXELS)

    def test_open_image_from_bytes_allows_large_animated_images(self):
        class FakeImage:
            def __init__(self):
                self.size = (8000, 4000)
                self.is_animated = True
                self.n_frames = 20
                self.info = {"duration": 80}

        fake = FakeImage()
        original_open = deepfry_module.Image.open
        deepfry_module.Image.open = lambda data: fake
        try:
            img, isgif, duration = self.cog._open_image_from_bytes(b"data")
        finally:
            deepfry_module.Image.open = original_open

        self.assertTrue(isgif)
        self.assertEqual(duration, 80)
        self.assertEqual(img.size, (8000, 4000))

    async def test_get_image_uses_source_limit_for_oversized_attachment_inputs(self):
        class GuildConfig:
            async def allowAllTypes(self):
                return False

            async def debug(self):
                return False

        limits = []

        async def resolve_target(ctx, link, allow_all_types):
            return ("attachment", types.SimpleNamespace(size=15_000_000), "attachment")

        async def read_attachment_bytes(attachment, filesize_limit):
            limits.append(filesize_limit)
            return b"data"

        self.cog.config.guild = lambda guild: GuildConfig()
        self.cog._resolve_target = resolve_target
        self.cog._read_attachment_bytes = read_attachment_bytes
        self.cog._open_image_from_bytes = lambda data: (types.SimpleNamespace(size=(100, 100)), False, None)

        ctx = types.SimpleNamespace(guild=types.SimpleNamespace(filesize_limit=10))
        await self.cog._get_image(ctx, None)

        self.assertEqual(limits, [deepfry_module.MAX_SOURCE_SIZE])

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
        self.cog._fry = lambda img, filesize_limit: "fried"

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

    async def test_on_message_without_command_allows_large_attachment_with_processing_budget(self):
        class GuildConfig:
            async def allowAllTypes(self):
                return False

            async def fryChance(self):
                return 1

            async def nukeChance(self):
                return 0

        used_attachments = []

        async def send(*, file):
            return None

        def permissions_for(member):
            return types.SimpleNamespace(attach_files=True)

        async def cog_disabled_in_guild(cog, guild):
            return False

        async def read_attachment_bytes(attachment, filesize_limit):
            used_attachments.append((attachment.url, filesize_limit))
            return b"data"

        async def immediate_wait_for(task, timeout):
            return task

        self.bot.cog_disabled_in_guild = cog_disabled_in_guild
        self.cog.config.guild = lambda guild: GuildConfig()
        self.cog._read_attachment_bytes = read_attachment_bytes
        self.cog._open_image_from_bytes = lambda data: ("img", False, None)
        self.cog._fry = lambda img, filesize_limit: "fried"

        original_wait_for = deepfry_module.asyncio.wait_for
        deepfry_module.asyncio.wait_for = immediate_wait_for
        try:
            msg = types.SimpleNamespace(
                author=types.SimpleNamespace(bot=False),
                attachments=[
                    types.SimpleNamespace(
                        url="https://example.com/large.png",
                        size=deepfry_module.MAX_SIZE + 1,
                    )
                ],
                guild=types.SimpleNamespace(filesize_limit=deepfry_module.MAX_SIZE, me=object()),
                channel=types.SimpleNamespace(permissions_for=permissions_for, send=send),
            )

            await self.cog.on_message_without_command(msg)
        finally:
            deepfry_module.asyncio.wait_for = original_wait_for

        self.assertEqual(
            used_attachments,
            [("https://example.com/large.png", deepfry_module.MAX_SOURCE_SIZE)],
        )

    async def test_on_message_without_command_uses_gif_processing_limit(self):
        class GuildConfig:
            async def allowAllTypes(self):
                return False

            async def fryChance(self):
                return 1

            async def nukeChance(self):
                return 0

        sent = []
        used_limits = []

        async def send(*, file):
            sent.append(file)

        def permissions_for(member):
            return types.SimpleNamespace(attach_files=True)

        async def cog_disabled_in_guild(cog, guild):
            return False

        async def read_attachment_bytes(attachment, filesize_limit):
            return b"data"

        async def immediate_wait_for(task, timeout):
            return task

        self.bot.cog_disabled_in_guild = cog_disabled_in_guild
        self.cog.config.guild = lambda guild: GuildConfig()
        self.cog._read_attachment_bytes = read_attachment_bytes
        self.cog._open_image_from_bytes = lambda data: ("img", True, 120)
        self.cog._videofry = lambda img, duration, filesize_limit: used_limits.append(
            (duration, filesize_limit)
        ) or "fried-gif"

        original_wait_for = deepfry_module.asyncio.wait_for
        deepfry_module.asyncio.wait_for = immediate_wait_for
        try:
            msg = types.SimpleNamespace(
                author=types.SimpleNamespace(bot=False),
                attachments=[
                    types.SimpleNamespace(
                        url="https://example.com/large.gif",
                        size=deepfry_module.MAX_SIZE + 1,
                    )
                ],
                guild=types.SimpleNamespace(filesize_limit=deepfry_module.MAX_SIZE, me=object()),
                channel=types.SimpleNamespace(permissions_for=permissions_for, send=send),
            )

            await self.cog.on_message_without_command(msg)
        finally:
            deepfry_module.asyncio.wait_for = original_wait_for

        self.assertEqual(used_limits, [(120, deepfry_module.MAX_SIZE)])
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
