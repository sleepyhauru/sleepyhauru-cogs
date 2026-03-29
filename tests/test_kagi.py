import types
import unittest
from unittest.mock import patch

from tests.support import load_module


kagi_module = load_module("kagi.kagi")
discord = load_module("discord")


class KagiHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = kagi_module.Kagi(bot=object())

    def test_fix_mojibake_converts_latin1_garble(self):
        self.assertEqual(self.cog._fix_mojibake("cafÃ©"), "café")
        self.assertEqual(self.cog._fix_mojibake("already fine"), "already fine")

    def test_build_styled_input_uses_mode_prompt(self):
        result = self.cog._build_styled_input("hello", "rng prompt")

        self.assertEqual(result, "hello\n\nrng prompt")

    def test_choose_style_prompt_uses_mode_prompt(self):
        with patch.object(kagi_module.random, "choice", return_value="rng prompt"):
            result = self.cog._choose_style_prompt("linkedin")

        self.assertEqual(result, "rng prompt")

    def test_strip_echoed_prompt_removes_trailing_internal_prompt(self):
        self.assertEqual(
            self.cog._strip_echoed_prompt("😭\n\nrng prompt", "rng prompt"),
            "😭",
        )
        self.assertEqual(
            self.cog._strip_echoed_prompt("styled output", "rng prompt"),
            "styled output",
        )

    def test_contains_only_custom_emojis_detects_discord_markup(self):
        self.assertTrue(
            self.cog._contains_only_custom_emojis("<a:PU_PepeInteresting:531807279280816129>")
        )
        self.assertTrue(
            self.cog._contains_only_custom_emojis(
                "<:wave:123456789012345678> <a:dance:987654321098765432>"
            )
        )
        self.assertFalse(self.cog._contains_only_custom_emojis("hello <:wave:123456789012345678>"))
        self.assertFalse(self.cog._contains_only_custom_emojis("😭"))

    async def test_get_auth_trims_values(self):
        await self.cog.config.kagi_session.set("  a  ")
        await self.cog.config.translate_session.set("  b  ")

        result = await self.cog._get_auth()

        self.assertEqual(result, ("a", "b"))

    async def test_get_text_prefers_explicit_text(self):
        ctx = types.SimpleNamespace(message=types.SimpleNamespace(reference=None))

        result = await self.cog._get_text(ctx, "  hello world  ")

        self.assertEqual(result, "hello world")

    async def test_get_text_collects_referenced_embed_text(self):
        embed = discord.Embed(title="Title", description="Body")
        embed.add_field(name="Field", value="Value")
        resolved = discord.Message(content="", embeds=[embed])
        reference = types.SimpleNamespace(resolved=resolved, message_id=None)
        ctx = types.SimpleNamespace(message=types.SimpleNamespace(reference=reference))

        result = await self.cog._get_text(ctx, None)

        self.assertEqual(result, "Title\nBody\nField\nValue")

    async def test_get_text_fetches_embed_text_when_reference_is_unresolved(self):
        embed = discord.Embed(title="Fetched", description="Embed")
        embed.add_field(name="Field", value="Value")
        fetched = discord.Message(content="", embeds=[embed])
        reference = types.SimpleNamespace(resolved=None, message_id=123)
        channel = types.SimpleNamespace(fetch_message=self._async_return(fetched))
        ctx = types.SimpleNamespace(
            channel=channel,
            message=types.SimpleNamespace(reference=reference),
        )

        result = await self.cog._get_text(ctx, None)

        self.assertEqual(result, "Fetched\nEmbed\nField\nValue")

    async def test_require_dm_config_warns_in_guild_context(self):
        sent = []

        async def send(message, **kwargs):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=object(), send=send)

        result = await self.cog._require_dm_config(ctx)

        self.assertFalse(result)
        self.assertEqual(sent, [self.cog.config_dm_notice])

    async def test_run_style_command_reports_missing_auth(self):
        sent = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: TypingContext(),
            author=types.SimpleNamespace(id=42),
        )

        await self.cog._run_style_command(
            ctx=ctx,
            text="hello",
            mode_key="linkedin",
        )

        self.assertIn("Kagi auth is not configured.", sent[0][0])

    async def test_run_style_command_uses_rng_prompt_and_sends_output(self):
        sent = []
        translate_calls = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def fake_translate(text, to_lang):
            translate_calls.append((text, to_lang))
            return "styled output"

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._translate = fake_translate
        self.cog._get_auth = fake_get_auth

        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: TypingContext(),
            author=types.SimpleNamespace(id=42),
        )

        with patch.object(kagi_module.random, "choice", return_value="rng prompt"):
            await self.cog._run_style_command(ctx=ctx, text="hello there", mode_key="linkedin")

        self.assertEqual(
            translate_calls,
            [("hello there\n\nrng prompt", "linkedin")],
        )
        self.assertEqual(sent[0][0], "styled output")
        self.assertEqual(sent[0][1]["allowed_mentions"], "none")

    async def test_run_style_command_strips_echoed_prompt_from_emoji_output(self):
        sent = []
        translate_calls = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def fake_translate(text, to_lang):
            translate_calls.append((text, to_lang))
            return "😭\n\nrng prompt"

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._translate = fake_translate
        self.cog._get_auth = fake_get_auth

        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: TypingContext(),
            author=types.SimpleNamespace(id=42),
        )

        with patch.object(kagi_module.random, "choice", return_value="rng prompt"):
            await self.cog._run_style_command(ctx=ctx, text="😭", mode_key="genz")

        self.assertEqual(
            translate_calls,
            [("😭\n\nrng prompt", "gen_z")],
        )
        self.assertEqual(sent[0][0], "😭")
        self.assertEqual(sent[0][1]["allowed_mentions"], "none")

    async def test_run_style_command_bypasses_translate_for_custom_emoji_only_text(self):
        sent = []
        translate_calls = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        async def fake_translate(text, to_lang):
            translate_calls.append((text, to_lang))
            return "should not be used"

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._translate = fake_translate
        self.cog._get_auth = fake_get_auth

        ctx = types.SimpleNamespace(
            send=send,
            author=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reference=None),
        )

        await self.cog._run_style_command(
            ctx=ctx,
            text="<a:PU_PepeInteresting:531807279280816129>",
            mode_key="genz",
        )

        self.assertEqual(translate_calls, [])
        self.assertEqual(sent[0][0], "<a:PU_PepeInteresting:531807279280816129>")
        self.assertEqual(sent[0][1]["allowed_mentions"], "none")

    async def test_send_output_chunks_long_messages(self):
        sent = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        ctx = types.SimpleNamespace(send=send)
        output = "a" * (self.cog.MAX_MESSAGE_LENGTH + 25)

        await self.cog._send_output(ctx, output)

        self.assertEqual(len(sent), 2)
        self.assertEqual(len(sent[0][0]), self.cog.MAX_MESSAGE_LENGTH)
        self.assertEqual(len(sent[1][0]), 25)
        self.assertEqual(sent[0][1]["allowed_mentions"], "none")

    async def test_run_style_command_handles_missing_text_and_translate_error(self):
        sent = []

        async def send(message, **kwargs):
            sent.append(message)

        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._get_auth = fake_get_auth
        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: TypingContext(),
            author=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(reference=None),
        )
        await self.cog._run_style_command(ctx, None, "linkedin")
        self.assertEqual(sent[-1], self.cog.STYLE_CONFIGS["linkedin"]["missing_text_message"])

        async def fake_translate(text, to_lang):
            raise RuntimeError("boom")

        self.cog._translate = fake_translate
        with patch.object(kagi_module.random, "choice", return_value="rng prompt"):
            await self.cog._run_style_command(ctx, "hello", "linkedin")
        self.assertEqual(sent[-1], "Error: boom")

    async def test_run_config_test_and_config_commands(self):
        sent = []
        dms = []

        async def send(message, **kwargs):
            sent.append(message)

        async def author_send(message):
            dms.append(message)

        ctx = types.SimpleNamespace(send=send, author=types.SimpleNamespace(send=author_send))

        async def fake_translate(text, to_lang):
            return "line1\nline2"

        self.cog._translate = fake_translate

        preview = await self.cog._run_config_test()
        self.assertIn("Auth check passed.", preview)

        await self.cog.set_model(ctx, model=" turbo ")
        await self.cog.config.kagi_session.set("token")
        await self.cog.config.translate_session.set("")
        await self.cog.show_config(ctx)
        await self.cog.clear_config(ctx, target="translate")
        await self.cog.clear_config(ctx, target="weird")

        self.assertEqual(sent[0], "Saved model: `turbo`")
        self.assertIn("- `kagi_session`: set", dms[0])
        self.assertIn("- `translate_session`: missing", dms[0])
        self.assertEqual(sent[1], "Cleared `translate_session`.")
        self.assertEqual(sent[2], "Use `all`, `kagi`, or `translate`.")

    async def test_linkedin_and_genz_commands_delegate_to_shared_runner(self):
        calls = []

        async def fake_run_style_command(ctx, text, mode_key):
            calls.append((ctx, text, mode_key))

        self.cog._run_style_command = fake_run_style_command
        ctx = types.SimpleNamespace()

        await self.cog.linkedin(ctx, text="post this")
        await self.cog.genz(ctx, text="say this")

        self.assertEqual(
            calls,
            [
                (ctx, "post this", "linkedin"),
                (ctx, "say this", "genz"),
            ],
        )

    @staticmethod
    def _async_return(value):
        async def inner(*args, **kwargs):
            return value

        return inner


if __name__ == "__main__":
    unittest.main()
