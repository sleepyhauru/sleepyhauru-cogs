import types
import unittest

from tests.support import load_module


kagi_module = load_module("kagi.kagi")
discord = load_module("discord")


class KagiHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = kagi_module.Kagi(bot=object())

    def test_fix_mojibake_converts_latin1_garble(self):
        self.assertEqual(self.cog._fix_mojibake("cafÃ©"), "café")
        self.assertEqual(self.cog._fix_mojibake("already fine"), "already fine")

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
            target_lang="linkedin",
            missing_text_message="missing",
        )

        self.assertIn("Kagi auth is not configured.", sent[0][0])

    async def test_run_style_command_retries_duplicate_output_once(self):
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
            return "same output"

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._translate = fake_translate
        self.cog._get_auth = fake_get_auth
        self.cog.last_outputs[(42, "linkedin")] = "same output"

        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: TypingContext(),
            author=types.SimpleNamespace(id=42),
        )

        await self.cog._run_style_command(
            ctx=ctx,
            text="hello there",
            mode_key="linkedin",
            target_lang="linkedin",
            missing_text_message="missing",
        )

        self.assertEqual(
            translate_calls,
            [("hello there", "linkedin"), ("hello there", "linkedin")],
        )
        self.assertEqual(self.cog.last_outputs[(42, "linkedin")], "same output")
        self.assertEqual(sent[0][0], "same output")
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
        await self.cog._run_style_command(ctx, None, "linkedin", "linkedin", "missing text")
        self.assertEqual(sent[-1], "missing text")

        async def fake_translate(text, to_lang):
            raise RuntimeError("boom")

        self.cog._translate = fake_translate
        await self.cog._run_style_command(ctx, "hello", "linkedin", "linkedin", "missing text")
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


if __name__ == "__main__":
    unittest.main()
