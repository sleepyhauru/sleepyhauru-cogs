import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import load_module


remoji = load_module("remoji.remoji")


class FakeResponse:
    def __init__(self, status=200, *, headers=None, body=b""):
        self.status = status
        self.headers = headers or {"Content-Type": "image/png", "Content-Length": str(len(body))}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self):
        return self._body


class FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


class TypingContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RemojiHelpersTest(unittest.TestCase):
    def test_extract_emojis_parses_static_and_animated_custom_emojis(self):
        found = remoji.extract_emojis("hello <:wave:123456789012345678> <a:dance:987654321098765432>")

        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].name, "wave")
        self.assertFalse(found[0].animated)
        self.assertEqual(found[0].url, "https://cdn.discordapp.com/emojis/123456789012345678.png")
        self.assertEqual(found[1].name, "dance")
        self.assertTrue(found[1].animated)
        self.assertEqual(found[1].url, "https://cdn.discordapp.com/emojis/987654321098765432.gif")

    def test_sanitize_emoji_name_rejects_invalid_names(self):
        self.assertEqual(remoji.sanitize_emoji_name("party_time!!"), "party_time")
        self.assertEqual(remoji.sanitize_emoji_name("x" * 40), "x" * 32)
        self.assertIsNone(remoji.sanitize_emoji_name("!"))

    def test_available_emoji_slots_counts_static_and_animated_separately(self):
        guild = types.SimpleNamespace(
            emojis=[
                types.SimpleNamespace(animated=False),
                types.SimpleNamespace(animated=True),
                types.SimpleNamespace(animated=True),
            ],
            emoji_limit=5,
        )

        self.assertEqual(remoji.available_emoji_slots(guild, False), 4)
        self.assertEqual(remoji.available_emoji_slots(guild, True), 3)

    def test_unique_emojis_deduplicates_preserving_order(self):
        first = remoji.EmojiAsset(1, "one", False)
        second = remoji.EmojiAsset(2, "two", True)

        self.assertEqual(remoji.unique_emojis([first, second, first]), [first, second])

    def test_resolve_emoji_name_suffixes_duplicates(self):
        guild = types.SimpleNamespace(
            emojis=[
                types.SimpleNamespace(name="wave"),
                types.SimpleNamespace(name="wave_2"),
            ]
        )

        self.assertEqual(remoji.resolve_emoji_name(guild, "wave"), "wave_3")

    def test_image_download_is_animated_uses_content_type_and_query_hint(self):
        self.assertTrue(
            remoji.image_download_is_animated(
                "https://i.imgur.com/static.png",
                remoji.ImageDownload(b"gif", content_type="image/gif"),
            )
        )
        self.assertTrue(
            remoji.image_download_is_animated(
                "https://cdn.discordapp.com/emojis/123.webp?animated=true",
                remoji.ImageDownload(b"webp", content_type="image/webp"),
            )
        )
        self.assertFalse(
            remoji.image_download_is_animated(
                "https://i.imgur.com/static.png",
                remoji.ImageDownload(b"png", content_type="image/png"),
            )
        )


class RemojiDownloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_download_image_url_rejects_invalid_url_and_domain(self):
        cog = remoji.Remoji(bot=object())

        result = await cog._download_image_url("not a url")
        self.assertEqual(result.error, remoji.INVALID_URL)

        result = await cog._download_image_url("https://example.com/image.png")
        self.assertEqual(result.error, remoji.INVALID_DOMAIN)

    async def test_download_image_url_rejects_unsupported_content_type(self):
        cog = remoji.Remoji(bot=object())
        url = "https://cdn.discordapp.com/emojis/123.png"
        cog.session = FakeSession({url: FakeResponse(headers={"Content-Type": "text/plain"}, body=b"nope")})

        result = await cog._download_image_url(url)

        self.assertIsNone(result.data)
        self.assertEqual(result.error, remoji.INVALID_TYPE)

    async def test_download_image_url_rejects_oversized_body(self):
        cog = remoji.Remoji(bot=object())
        url = "https://cdn.discordapp.com/emojis/123.png"
        body = b"x" * (remoji.DISCORD_EMOJI_SIZE_LIMIT + 1)
        cog.session = FakeSession({url: FakeResponse(body=body)})

        result = await cog._download_image_url(url)

        self.assertIsNone(result.data)
        self.assertEqual(result.error, remoji.IMAGE_TOO_LARGE)

    async def test_download_animated_emoji_falls_back_to_webp(self):
        cog = remoji.Remoji(bot=object())
        gif_url = "https://cdn.discordapp.com/emojis/123456789012345678.gif"
        webp_url = "https://cdn.discordapp.com/emojis/123456789012345678.webp?animated=true"
        cog.session = FakeSession(
            {
                gif_url: FakeResponse(415, body=b""),
                webp_url: FakeResponse(body=b"webp-bytes", headers={"Content-Type": "image/webp"}),
            }
        )

        result = await cog._download_emoji(remoji.EmojiAsset(123456789012345678, "dance", True))

        self.assertEqual(result.data, b"webp-bytes")
        self.assertEqual([call[0] for call in cog.session.calls], [gif_url, webp_url])


class RemojiCommandTest(unittest.IsolatedAsyncioTestCase):
    def make_ctx(self, guild):
        sent = []
        reactions = []

        async def send(message):
            sent.append(message)

        async def add_reaction(emoji):
            reactions.append(emoji)

        ctx = types.SimpleNamespace(
            send=send,
            guild=guild,
            author=types.SimpleNamespace(
                id=42,
                guild_permissions=types.SimpleNamespace(manage_emojis=True),
            ),
            message=types.SimpleNamespace(add_reaction=add_reaction),
            typing=lambda: TypingContext(),
            clean_prefix="!",
        )
        return ctx, sent, reactions

    async def test_remoji_upload_creates_emoji_from_allowed_url(self):
        created = types.SimpleNamespace(name="wave")
        guild = types.SimpleNamespace(
            id=1,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        ctx, sent, _ = self.make_ctx(guild)
        cog = remoji.Remoji(bot=object())
        url = "https://i.imgur.com/wave.png"
        cog.session = FakeSession({url: FakeResponse(body=b"png-bytes")})

        await cog.remoji_upload(ctx, url, "wave")

        guild.create_custom_emoji.assert_awaited_once()
        kwargs = guild.create_custom_emoji.await_args.kwargs
        self.assertEqual(kwargs["name"], "wave")
        self.assertEqual(kwargs["image"], b"png-bytes")
        self.assertEqual(sent, ["Uploaded `:wave:` to this server: namespace(name='wave')"])

    async def test_remoji_upload_rejects_invalid_name_before_download(self):
        guild = types.SimpleNamespace(id=2, emojis=[], emoji_limit=10)
        ctx, sent, _ = self.make_ctx(guild)
        cog = remoji.Remoji(bot=object())

        await cog.remoji_upload(ctx, "https://i.imgur.com/wave.png", "!")

        self.assertEqual(sent, [remoji.INVALID_NAME])
        self.assertIsNone(cog.session)

    async def test_remoji_copy_downloads_source_emoji_and_reacts(self):
        created = types.SimpleNamespace(name="dance")
        guild = types.SimpleNamespace(
            id=3,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        ctx, sent, reactions = self.make_ctx(guild)
        cog = remoji.Remoji(bot=object())
        url = "https://cdn.discordapp.com/emojis/987654321098765432.gif"
        cog.session = FakeSession({url: FakeResponse(body=b"gif-bytes", headers={"Content-Type": "image/gif"})})

        await cog.remoji_copy(ctx, "<a:dance:987654321098765432>")

        guild.create_custom_emoji.assert_awaited_once()
        kwargs = guild.create_custom_emoji.await_args.kwargs
        self.assertEqual(kwargs["name"], "dance")
        self.assertEqual(kwargs["image"], b"gif-bytes")
        self.assertEqual(reactions, [created])
        self.assertEqual(sent, ["Copied `:dance:` to this server: namespace(name='dance')"])

    async def test_remoji_copy_uses_replied_message_when_no_emoji_argument(self):
        created = types.SimpleNamespace(name="reply")
        guild = types.SimpleNamespace(
            id=12,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        ctx, sent, _ = self.make_ctx(guild)

        async def fetch_message(message_id):
            self.assertEqual(message_id, 55)
            return types.SimpleNamespace(content="<:reply:333333333333333333>")

        ctx.message.reference = types.SimpleNamespace(message_id=55)
        ctx.channel = types.SimpleNamespace(fetch_message=fetch_message)
        cog = remoji.Remoji(bot=object())
        url = "https://cdn.discordapp.com/emojis/333333333333333333.png"
        cog.session = FakeSession({url: FakeResponse(body=b"png-bytes")})

        await cog.remoji_copy(ctx)

        guild.create_custom_emoji.assert_awaited_once()
        self.assertEqual(guild.create_custom_emoji.await_args.kwargs["name"], "reply")
        self.assertEqual(sent, ["Copied `:reply:` to this server: namespace(name='reply')"])

    async def test_remoji_copy_many_reports_success_and_failures(self):
        created = types.SimpleNamespace(name="one")
        guild = types.SimpleNamespace(
            id=4,
            emojis=[],
            emoji_limit=1,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        ctx, sent, _ = self.make_ctx(guild)
        cog = remoji.Remoji(bot=object())
        first_url = "https://cdn.discordapp.com/emojis/111111111111111111.png"
        cog.session = FakeSession({first_url: FakeResponse(body=b"png-bytes")})

        await cog.remoji_copy_many(
            ctx,
            emojis="<:one:111111111111111111> <:two:222222222222222222>",
        )

        guild.create_custom_emoji.assert_awaited_once()
        self.assertEqual(
            sent,
            [
                "Uploaded 1/2 emojis.\n"
                "namespace(name='one')\n"
                "Failed: two: no slots"
            ],
        )

    async def test_remoji_copy_many_uses_resolved_reply_for_hybrid_context_without_input(self):
        created = [types.SimpleNamespace(name="one"), types.SimpleNamespace(name="two")]
        guild = types.SimpleNamespace(
            id=13,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(side_effect=created),
        )
        ctx, sent, _ = self.make_ctx(guild)
        ctx.interaction = types.SimpleNamespace()
        ctx.message.reference = types.SimpleNamespace(
            resolved=types.SimpleNamespace(
                content="<:one:444444444444444444> <:two:555555555555555555>"
            )
        )
        cog = remoji.Remoji(bot=object())
        cog.session = FakeSession(
            {
                "https://cdn.discordapp.com/emojis/444444444444444444.png": FakeResponse(body=b"one"),
                "https://cdn.discordapp.com/emojis/555555555555555555.png": FakeResponse(body=b"two"),
            }
        )

        with patch.object(remoji.asyncio, "sleep", AsyncMock()):
            await cog.remoji_copy_many(ctx)

        self.assertEqual(guild.create_custom_emoji.await_count, 2)
        self.assertEqual(sent, ["Uploaded 2/2 emojis.\nnamespace(name='one') namespace(name='two')"])

    async def test_remoji_url_lists_unique_asset_urls(self):
        ctx, sent, _ = self.make_ctx(guild=types.SimpleNamespace(id=5, emojis=[], emoji_limit=1))
        cog = remoji.Remoji(bot=object())

        await cog.remoji_url(ctx, emoji="<:one:111111111111111111> <:one:111111111111111111>")

        self.assertEqual(sent, ["https://cdn.discordapp.com/emojis/111111111111111111.png"])

    async def test_remoji_url_uses_replied_message_when_no_emoji_text(self):
        ctx, sent, _ = self.make_ctx(guild=types.SimpleNamespace(id=14, emojis=[], emoji_limit=1))
        ctx.message.reference = types.SimpleNamespace(
            resolved=types.SimpleNamespace(content="<a:spin:666666666666666666>")
        )
        cog = remoji.Remoji(bot=object())

        await cog.remoji_url(ctx)

        self.assertEqual(sent, ["https://cdn.discordapp.com/emojis/666666666666666666.gif"])

    async def test_remoji_copy_without_input_or_reply_prompts_for_source(self):
        ctx, sent, _ = self.make_ctx(guild=types.SimpleNamespace(id=15, emojis=[], emoji_limit=1))
        cog = remoji.Remoji(bot=object())

        await cog.remoji_copy(ctx)

        self.assertEqual(sent, [remoji.EMOJI_SOURCE_HINT])

    async def test_remoji_upload_uses_content_type_for_animated_slot_and_suffixes_name(self):
        created = types.SimpleNamespace(name="wave_2")
        guild = types.SimpleNamespace(
            id=6,
            emojis=[types.SimpleNamespace(name="wave", animated=True)],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        ctx, sent, _ = self.make_ctx(guild)
        cog = remoji.Remoji(bot=object())
        url = "https://i.imgur.com/notgif.png"
        cog.session = FakeSession({url: FakeResponse(body=b"gif-bytes", headers={"Content-Type": "image/gif"})})

        await cog.remoji_upload(ctx, url, "wave")

        kwargs = guild.create_custom_emoji.await_args.kwargs
        self.assertEqual(kwargs["name"], "wave_2")
        self.assertEqual(sent, ["Uploaded `:wave_2:` to this server: namespace(name='wave_2')"])

    async def test_remoji_copy_rejects_user_without_permission_or_allowlist(self):
        guild = types.SimpleNamespace(id=7, emojis=[], emoji_limit=10)
        ctx, sent, _ = self.make_ctx(guild)
        ctx.author.guild_permissions = types.SimpleNamespace(manage_emojis=False)
        cog = remoji.Remoji(bot=object())

        await cog.remoji_copy(ctx, "<:wave:123456789012345678>")

        self.assertEqual(sent, [remoji.UPLOAD_NOT_ALLOWED])

    async def test_remoji_allowlist_allows_user_without_manage_emojis(self):
        created = types.SimpleNamespace(name="wave")
        guild = types.SimpleNamespace(
            id=8,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        ctx, sent, _ = self.make_ctx(guild)
        ctx.author.guild_permissions = types.SimpleNamespace(manage_emojis=False)
        cog = remoji.Remoji(bot=object())
        await cog.config.guild(guild).upload_allowlist.set([42])
        url = "https://cdn.discordapp.com/emojis/123456789012345678.png"
        cog.session = FakeSession({url: FakeResponse(body=b"png-bytes")})

        await cog.remoji_copy(ctx, "<:wave:123456789012345678>")

        guild.create_custom_emoji.assert_awaited_once()
        self.assertEqual(sent, ["Copied `:wave:` to this server: namespace(name='wave')"])

    async def test_remojiset_allowuser_and_denyuser_update_allowlist(self):
        guild = types.SimpleNamespace(id=9)
        ctx, sent, _ = self.make_ctx(guild)
        user = types.SimpleNamespace(id=99, mention="<@99>")
        cog = remoji.Remoji(bot=object())

        await cog.remojiset_allowuser(ctx, user)
        self.assertEqual(await cog.config.guild(guild).upload_allowlist(), [99])

        await cog.remojiset_denyuser(ctx, user)
        self.assertEqual(await cog.config.guild(guild).upload_allowlist(), [])
        self.assertEqual(
            sent,
            [
                "Added <@99> to the Remoji upload allowlist.",
                "Removed <@99> from the Remoji upload allowlist.",
            ],
        )

    async def test_remoji_copy_many_sends_progress_for_larger_batches(self):
        guild = types.SimpleNamespace(
            id=10,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(side_effect=[types.SimpleNamespace(name=f"e{i}") for i in range(6)]),
        )
        ctx, sent, _ = self.make_ctx(guild)
        cog = remoji.Remoji(bot=object())
        mapping = {}
        emoji_markup = []
        for index in range(6):
            emoji_id = 111111111111111111 + index
            mapping[f"https://cdn.discordapp.com/emojis/{emoji_id}.png"] = FakeResponse(body=b"png-bytes")
            emoji_markup.append(f"<:e{index}:{emoji_id}>")
        cog.session = FakeSession(mapping)

        with patch.object(remoji.asyncio, "sleep", AsyncMock()):
            await cog.remoji_copy_many(ctx, emojis=" ".join(emoji_markup))

        self.assertEqual(sent[0], "Copying emojis... 5/6 processed, 5 uploaded, 0 failed.")
        self.assertTrue(sent[-1].startswith("Uploaded 6/6 emojis."))

    async def test_remoji_info_does_not_include_upstream_inspiration_link(self):
        ctx, sent, _ = self.make_ctx(
            guild=types.SimpleNamespace(
                id=16,
                emojis=[types.SimpleNamespace(animated=False), types.SimpleNamespace(animated=True)],
                emoji_limit=10,
            )
        )
        cog = remoji.Remoji(bot=object())

        await cog.remoji_info(ctx)

        self.assertNotIn("Inspired", sent[0])
        self.assertNotIn("github.com/remoji-bot", sent[0])
        self.assertEqual(
            sent[0],
            "Remoji manages custom emoji uploads and copies.\n"
            "Static slots remaining: `9`\n"
            "Animated slots remaining: `9`",
        )


class FakeInteractionResponse:
    def __init__(self):
        self.messages = []
        self.deferred = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class RemojiInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_context_menus_register_and_unload(self):
        added = []
        removed = []
        tree = types.SimpleNamespace(
            add_command=lambda command: added.append(command.name),
            remove_command=lambda name, type=None: removed.append((name, type)),
        )
        bot = types.SimpleNamespace(tree=tree)
        cog = remoji.Remoji(bot=bot)

        cog.cog_unload()

        self.assertEqual(added, ["Remoji Asset URLs", "Remoji Copy Emotes"])
        self.assertEqual(removed, [("Remoji Asset URLs", "context_menu"), ("Remoji Copy Emotes", "context_menu")])

    async def test_remoji_url_app_command_sends_asset_urls_ephemerally(self):
        cog = remoji.Remoji(bot=object())
        response = FakeInteractionResponse()
        interaction = types.SimpleNamespace(response=response)
        message = types.SimpleNamespace(content="<:one:111111111111111111>")

        await cog.remoji_url_app_command(interaction, message)

        self.assertEqual(
            response.messages,
            [("https://cdn.discordapp.com/emojis/111111111111111111.png", {"ephemeral": True})],
        )

    async def test_remoji_copy_app_command_uploads_message_emojis(self):
        created = types.SimpleNamespace(name="one")
        guild = types.SimpleNamespace(
            id=11,
            emojis=[],
            emoji_limit=10,
            create_custom_emoji=AsyncMock(return_value=created),
        )
        response = FakeInteractionResponse()
        edited = []

        async def edit_original_response(content=None):
            edited.append(content)

        user = types.SimpleNamespace(id=50, guild_permissions=types.SimpleNamespace(manage_emojis=True))
        interaction = types.SimpleNamespace(
            guild=guild,
            user=user,
            response=response,
            edit_original_response=edit_original_response,
        )
        message = types.SimpleNamespace(content="<:one:111111111111111111>")
        cog = remoji.Remoji(bot=object())
        url = "https://cdn.discordapp.com/emojis/111111111111111111.png"
        cog.session = FakeSession({url: FakeResponse(body=b"png-bytes")})

        await cog.remoji_copy_app_command(interaction, message)

        guild.create_custom_emoji.assert_awaited_once()
        self.assertEqual(response.deferred, [{"thinking": True}])
        self.assertEqual(edited, ["Uploaded 1/1 emojis.\nnamespace(name='one')"])


if __name__ == "__main__":
    unittest.main()
