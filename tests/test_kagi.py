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

    def test_build_style_context_uses_mode_prompt(self):
        result = self.cog._build_style_context("rng prompt")

        self.assertEqual(
            result,
            "rng prompt\nReturn only the rewritten text.",
        )

    def test_normalize_language_code_supports_aliases(self):
        self.assertEqual(self.cog._normalize_language_code("english"), "en")
        self.assertEqual(self.cog._normalize_language_code("pt-br"), "pt_br")
        self.assertEqual(self.cog._normalize_language_code("detect"), "auto")

    def test_build_payload_uses_supplied_languages(self):
        payload = self.cog._build_payload(
            "hola",
            "auto",
            "en",
            "model-name",
            "translate-token",
        )

        self.assertEqual(payload["from"], "auto")
        self.assertEqual(payload["to"], "en")

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
        self.assertEqual(
            self.cog._strip_echoed_prompt(
                "styled output\nInstructions: rng prompt\nReturn only the rewritten text.\nmore text",
                "rng prompt",
            ),
            "styled output\nmore text",
        )
        self.assertEqual(
            self.cog._strip_echoed_prompt("rng prompt", "rng prompt"),
            "",
        )

    def test_normalize_custom_emoji_text_extracts_names(self):
        self.assertEqual(
            self.cog._normalize_custom_emoji_text("<a:PU_PepeInteresting:531807279280816129>"),
            ":PU_PepeInteresting:",
        )
        self.assertEqual(
            self.cog._normalize_custom_emoji_text(
                "hello <:wave:123456789012345678> <a:dance_party:987654321098765432>"
            ),
            "hello :wave: :dance_party:",
        )
        self.assertEqual(self.cog._normalize_custom_emoji_text("😭"), "😭")

    def test_extract_message_text_prefers_embed_over_url_only_content(self):
        embed = discord.Embed(description="tweet body here")
        message = discord.Message(
            content="https://x.com/example/status/1234567890",
            embeds=[embed],
        )

        self.assertEqual(self.cog._extract_message_text(message), "tweet body here")

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
        self.assertIn("[p]kagi setkagi <value>", sent[0][0])

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

        async def fake_translate(text, to_lang, context=""):
            translate_calls.append((text, to_lang, context))
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
            [
                (
                    "hello there",
                    "linkedin",
                    "rng prompt\nReturn only the rewritten text.",
                )
            ],
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

        async def fake_translate(text, to_lang, context=""):
            translate_calls.append((text, to_lang, context))
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
            [
                (
                    "😭",
                    "gen_z",
                    "rng prompt\nReturn only the rewritten text.",
                )
            ],
        )
        self.assertEqual(sent[0][0], "😭")
        self.assertEqual(sent[0][1]["allowed_mentions"], "none")

    async def test_run_style_command_normalizes_custom_emoji_before_translate(self):
        sent = []
        translate_calls = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        async def fake_translate(text, to_lang, context=""):
            translate_calls.append((text, to_lang, context))
            return "extra af pepe interesting"

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._translate = fake_translate
        self.cog._get_auth = fake_get_auth

        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: self._typing_context(),
            author=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reference=None),
        )

        with patch.object(kagi_module.random, "choice", return_value="rng prompt"):
            await self.cog._run_style_command(
                ctx=ctx,
                text="<a:PU_PepeInteresting:531807279280816129>",
                mode_key="genz",
            )

        self.assertEqual(
            translate_calls,
            [
                (
                    ":PU_PepeInteresting:",
                    "gen_z",
                    "rng prompt\nReturn only the rewritten text.",
                )
            ],
        )
        self.assertEqual(sent[0][0], "extra af pepe interesting")
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

        async def fake_translate(text, to_lang, context=""):
            raise RuntimeError("boom")

        self.cog._translate = fake_translate
        with patch.object(kagi_module.random, "choice", return_value="rng prompt"):
            await self.cog._run_style_command(ctx, "hello", "linkedin")
        self.assertEqual(sent[-1], "Error: boom")

    async def test_run_translate_command_defaults_to_auto_to_english(self):
        sent = []
        translate_calls = []

        async def send(message, **kwargs):
            sent.append((message, kwargs))

        async def fake_translate(text, to_lang, context="", from_lang="auto"):
            translate_calls.append((text, to_lang, context, from_lang))
            return "hello"

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        self.cog._translate = fake_translate
        self.cog._get_auth = fake_get_auth

        ctx = types.SimpleNamespace(
            send=send,
            typing=lambda: self._typing_context(),
            author=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reference=None),
        )

        await self.cog._run_translate_command(
            ctx,
            "hola",
            missing_text_message="missing",
        )

        self.assertEqual(translate_calls, [("hola", "en", "", "auto")])
        self.assertEqual(sent[0][0], "hello")
        self.assertEqual(sent[0][1]["allowed_mentions"], "none")

    async def test_translate_commands_delegate_to_shared_runner(self):
        calls = []

        async def fake_run_translate_command(
            ctx, text, *, to_lang="en", from_lang="auto", missing_text_message
        ):
            calls.append((ctx, text, to_lang, from_lang, missing_text_message))

        self.cog._run_translate_command = fake_run_translate_command
        ctx = types.SimpleNamespace()

        await self.cog.translate(ctx, text="bonjour")
        await self.cog.translate_into(ctx, target_language="spanish", text="hello")

        self.assertEqual(
            calls,
            [
                (
                    ctx,
                    "bonjour",
                    "en",
                    "auto",
                    "Provide text with `translate`, reply to a message before running it, "
                    "or use the `Translate to English` message command.",
                ),
                (
                    ctx,
                    "hello",
                    "spanish",
                    "auto",
                    "Provide text with `translateinto <language>` or reply to a message before running it.",
                ),
            ],
        )

    async def test_run_config_test_and_config_commands(self):
        sent = []
        dms = []

        async def send(message, **kwargs):
            sent.append(message)

        async def author_send(message):
            dms.append(message)

        ctx = types.SimpleNamespace(send=send, author=types.SimpleNamespace(send=author_send))

        async def fake_translate(text, to_lang, context=""):
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
        self.assertIn("Configured `kagi_session`: `yes`", dms[0])
        self.assertIn("Configured `translate_session`: `no`", dms[0])
        self.assertIn("Next: configure in DMs", dms[0])
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

    async def test_config_status_message_uses_prefix_and_next_step(self):
        await self.cog.config.kagi_session.set("token")
        await self.cog.config.translate_session.set("translate")
        await self.cog.config.model.set("standard")

        message = await self.cog._config_status_message("!")

        self.assertIn("Configured `kagi_session`: `yes`", message)
        self.assertIn("Configured `translate_session`: `yes`", message)
        self.assertIn("Next: `!kagi test`", message)

    async def test_kagi_group_sends_status_summary(self):
        sent = []
        ctx = types.SimpleNamespace(
            clean_prefix="?",
            send=self._async_collector(sent),
        )

        await self.cog.kagi(ctx)

        self.assertIn("Kagi configuration", sent[0])
        self.assertIn("Next: configure in DMs with `?kagi setkagi <value>`", sent[0])

    async def test_context_menu_translation_defers_and_sends_output(self):
        sent = []
        deferred = []

        async def fake_get_auth():
            return "kagi-cookie", "translate-cookie"

        async def fake_translate(text, to_lang, context="", from_lang="auto"):
            self.assertEqual(text, "hola")
            self.assertEqual(to_lang, "en")
            self.assertEqual(from_lang, "auto")
            return "hello"

        self.cog._get_auth = fake_get_auth
        self.cog._translate = fake_translate
        interaction = self._interaction(sent=sent, deferred=deferred)
        message = discord.Message(content="hola")

        await self.cog.translate_message_app_command(interaction, message)

        self.assertEqual(deferred, [True])
        self.assertEqual(
            sent,
            [("hello", {"allowed_mentions": "none", "ephemeral": False})],
        )

    async def test_context_menu_style_reports_missing_auth_ephemerally(self):
        sent = []
        interaction = self._interaction(sent=sent, deferred=[])
        message = discord.Message(content="post this")

        await self.cog.linkedin_message_app_command(interaction, message)

        self.assertIn("Kagi auth is not configured.", sent[0][0])
        self.assertTrue(sent[0][1]["ephemeral"])

    async def test_context_menu_registration_and_unload_use_tree(self):
        added = []
        removed = []

        class FakeTree:
            def add_command(self, command):
                added.append(command.name)

            def remove_command(self, name, type=None):
                removed.append((name, type))

        cog = kagi_module.Kagi(bot=types.SimpleNamespace(tree=FakeTree()))
        cog.cog_unload()

        self.assertEqual(
            added,
            ["Translate to English", "LinkedIn Rewrite", "Gen Z Rewrite"],
        )
        self.assertEqual(
            removed,
            [
                ("Translate to English", "context_menu"),
                ("LinkedIn Rewrite", "context_menu"),
                ("Gen Z Rewrite", "context_menu"),
            ],
        )

    @staticmethod
    def _async_return(value):
        async def inner(*args, **kwargs):
            return value

        return inner

    @staticmethod
    def _async_collector(target):
        async def inner(message, **kwargs):
            target.append(message)

        return inner

    @staticmethod
    def _typing_context():
        class TypingContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return TypingContext()

    @staticmethod
    def _interaction(*, sent, deferred):
        class Response:
            def __init__(self):
                self.done = False

            def is_done(self):
                return self.done

            async def send_message(self, message, **kwargs):
                self.done = True
                sent.append((message, kwargs))

            async def defer(self, **kwargs):
                self.done = True
                deferred.append(True)

        class Followup:
            async def send(self, message, **kwargs):
                sent.append((message, kwargs))

        return types.SimpleNamespace(
            response=Response(),
            followup=Followup(),
        )


if __name__ == "__main__":
    unittest.main()
