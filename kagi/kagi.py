import json
import random
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands


class Kagi(commands.Cog):
    """Kagi Translate integrations for LinkedIn and Gen Z styles."""

    __author__ = "sleepyhauru"
    __version__ = "2.1.0"

    API_URL = "https://translate.kagi.com/api/translate"
    MAX_MESSAGE_LENGTH = 2000
    STYLE_CONFIGS = {
        "linkedin": {
            "target_lang": "linkedin",
            "missing_text_message": (
                "Provide text after `!linkedin` or reply to a message with `!linkedin`."
            ),
            "rng_prompts": [
                "Rewrite this in corporate LinkedIn tone.",
                "Make this extremely over-the-top LinkedIn influencer cringe.",
                "Rewrite this as a humblebrag LinkedIn post.",
                "Rewrite this like a startup founder giving motivational insight.",
            ],
        },
        "genz": {
            "target_lang": "gen_z",
            "missing_text_message": "Provide text after `!genz` or reply to a message with `!genz`.",
            "rng_prompts": [
                "Rewrite this in casual Gen Z style.",
                "Rewrite this in exaggerated Gen Z slang.",
                "Rewrite this like someone extremely online.",
                "Rewrite this in dry, deadpan Gen Z humor.",
            ],
        },
    }

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=948271650431, force_registration=True)

        self.config.register_global(
            kagi_session="",
            translate_session="",
            model="standard",
        )

        self.config_dm_notice = (
            "For safety, send this command to me in DMs instead of a server channel."
        )
        self.session = None

    def format_help_for_context(self, ctx: commands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs):
        return

    def cog_unload(self):
        if self.session is not None and not getattr(self.session, "closed", False):
            self.bot.loop.create_task(self.session.close())

    async def _get_auth(self) -> tuple[str, str]:
        kagi_session = await self.config.kagi_session()
        translate_session = await self.config.translate_session()
        return kagi_session.strip(), translate_session.strip()

    async def _require_dm_config(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True
        await ctx.send(self.config_dm_notice)
        return False

    @staticmethod
    def _fix_mojibake(text: str) -> str:
        try:
            return text.encode("latin1").decode("utf-8")
        except Exception:
            return text

    def _build_styled_input(self, text: str, mode_key: str) -> str:
        config = self.STYLE_CONFIGS[mode_key]
        prompt = random.choice(config["rng_prompts"])
        return f"{text}\n\n{prompt}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or getattr(self.session, "closed", False):
            timeout = aiohttp.ClientTimeout(total=60)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    def _build_payload(self, text: str, to_lang: str, model: str, translate_session: str) -> dict:
        return {
            "text": text,
            "from": "en_us",
            "to": to_lang,
            "stream": True,
            "formality": "default",
            "speaker_gender": "unknown",
            "addressee_gender": "unknown",
            "language_complexity": "standard",
            "translation_style": "natural",
            "context": "",
            "model": model,
            "session_token": translate_session,
            "dictionary_language": "en",
            "use_definition_context": True,
            "enable_language_features": False,
        }

    async def _collect_stream_text(self, resp) -> str:
        parts = []

        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line or not line.startswith("data: "):
                continue

            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if "delta" in event:
                parts.append(event["delta"])

            if event.get("done") is True:
                break

        final_text = "".join(parts).strip()
        final_text = self._fix_mojibake(final_text)

        if not final_text:
            raise RuntimeError("No translated output was returned.")

        return final_text

    async def _translate(self, text: str, to_lang: str) -> str:
        kagi_session, translate_session = await self._get_auth()
        model = await self.config.model()

        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Origin": "https://translate.kagi.com",
            "Referer": "https://translate.kagi.com/",
            "User-Agent": "Mozilla/5.0",
            "X-Signal": "abortable",
        }

        cookies = {
            "kagi_session": kagi_session,
            "translate_session": translate_session,
        }
        payload = self._build_payload(text, to_lang, model, translate_session)
        session = await self._get_session()

        async with session.post(self.API_URL, headers=headers, cookies=cookies, json=payload) as resp:
            if resp.status == 401:
                raise RuntimeError("Authentication failed. Your Kagi session values may be expired.")
            if resp.status == 403:
                raise RuntimeError("Access denied by Kagi.")
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Kagi returned HTTP {resp.status}: {body[:300]}")

            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                body = await resp.text()
                raise RuntimeError(f"Unexpected response type: {content_type}\n{body[:300]}")

            return await self._collect_stream_text(resp)

    def _extract_message_text(self, message: discord.Message) -> Optional[str]:
        if message.content and message.content.strip():
            return message.content.strip()

        if not message.embeds:
            return None

        embed_text_parts = []
        for embed in message.embeds:
            if embed.title:
                embed_text_parts.append(embed.title)
            if embed.description:
                embed_text_parts.append(embed.description)
            for field in embed.fields:
                if field.name:
                    embed_text_parts.append(field.name)
                if field.value:
                    embed_text_parts.append(field.value)

        combined = "\n".join(part for part in embed_text_parts if part and part.strip()).strip()
        return combined or None

    async def _send_output(self, ctx: commands.Context, output: str):
        for start in range(0, len(output), self.MAX_MESSAGE_LENGTH):
            chunk = output[start : start + self.MAX_MESSAGE_LENGTH]
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    async def _get_text(self, ctx: commands.Context, text: Optional[str]) -> Optional[str]:
        if text and text.strip():
            return text.strip()

        ref = getattr(ctx.message, "reference", None)
        if not ref:
            return None

        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            return self._extract_message_text(resolved)

        if ref.message_id:
            try:
                replied_message = await ctx.channel.fetch_message(ref.message_id)
                return self._extract_message_text(replied_message)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        return None

    async def _run_style_command(self, ctx: commands.Context, text: Optional[str], mode_key: str):
        kagi_session, translate_session = await self._get_auth()
        config = self.STYLE_CONFIGS[mode_key]

        if not kagi_session or not translate_session:
            await ctx.send(
                "Kagi auth is not configured. Use the owner setup commands first:\n"
                "`!kagi setkagi <value>`\n"
                "`!kagi settranslate <value>`"
            )
            return

        target = await self._get_text(ctx, text)
        if not target:
            await ctx.send(config["missing_text_message"])
            return

        if len(target) > 4000:
            await ctx.send("That message is too long to translate.")
            return

        styled_input = self._build_styled_input(target, mode_key)
        async with ctx.typing():
            try:
                output = await self._translate(styled_input, config["target_lang"])
            except Exception as e:
                await ctx.send(f"Error: {e}")
                return

        await self._send_output(ctx, output)

    async def _send_owner_dm(self, ctx: commands.Context, message: str):
        try:
            await ctx.author.send(message)
        except discord.Forbidden:
            await ctx.send("I couldn't DM you. Please enable DMs and try again.")

    async def _run_config_test(self) -> str:
        output = await self._translate("Test message.", "linkedin")
        preview = output.replace("\n", " ")[:120]
        return f"Auth check passed.\nModel: `{await self.config.model()}`\nPreview: {preview}"

    @commands.group(name="kagi", invoke_without_command=True)
    @commands.is_owner()
    async def kagi(self, ctx: commands.Context):
        """Configure the Kagi cog."""
        await ctx.send_help()

    @kagi.command(name="setkagi")
    @commands.is_owner()
    async def set_kagi_session(self, ctx: commands.Context, *, value: str):
        """Set the kagi_session cookie value."""
        if not await self._require_dm_config(ctx):
            return
        await self.config.kagi_session.set(value.strip())
        await self._send_owner_dm(ctx, "Saved `kagi_session`.")

    @kagi.command(name="settranslate")
    @commands.is_owner()
    async def set_translate_session(self, ctx: commands.Context, *, value: str):
        """Set the translate_session value."""
        if not await self._require_dm_config(ctx):
            return
        await self.config.translate_session.set(value.strip())
        await self._send_owner_dm(ctx, "Saved `translate_session`.")

    @kagi.command(name="setmodel")
    @commands.is_owner()
    async def set_model(self, ctx: commands.Context, *, model: str):
        """Set the Kagi model value, usually 'standard'."""
        await self.config.model.set(model.strip())
        await ctx.send(f"Saved model: `{model.strip()}`")

    @kagi.command(name="show")
    @commands.is_owner()
    async def show_config(self, ctx: commands.Context):
        """Show whether the required auth values are configured."""
        kagi_session, translate_session = await self._get_auth()
        model = await self.config.model()

        msg = (
            f"**Configured:**\n"
            f"- `kagi_session`: {'set' if kagi_session else 'missing'}\n"
            f"- `translate_session`: {'set' if translate_session else 'missing'}\n"
            f"- `model`: `{model}`"
        )
        await self._send_owner_dm(ctx, msg)

    @kagi.command(name="clear")
    @commands.is_owner()
    async def clear_config(self, ctx: commands.Context, target: str = "all"):
        """Clear stored auth values for one token or all tokens."""
        target = target.lower().strip()

        if target in {"all", "both"}:
            await self.config.kagi_session.set("")
            await self.config.translate_session.set("")
            await ctx.send("Cleared stored Kagi auth values.")
            return

        if target in {"kagi", "kagi_session"}:
            await self.config.kagi_session.set("")
            await ctx.send("Cleared `kagi_session`.")
            return

        if target in {"translate", "translate_session"}:
            await self.config.translate_session.set("")
            await ctx.send("Cleared `translate_session`.")
            return

        await ctx.send("Use `all`, `kagi`, or `translate`.")

    @kagi.command(name="test")
    @commands.is_owner()
    async def test_config(self, ctx: commands.Context):
        """Validate the stored Kagi auth values."""
        kagi_session, translate_session = await self._get_auth()
        if not kagi_session or not translate_session:
            await ctx.send("Kagi auth is incomplete. Use `kagi show` to inspect current config state.")
            return

        async with ctx.typing():
            try:
                result = await self._run_config_test()
            except Exception as error:
                await ctx.send(f"Auth check failed: {error}")
                return

        await self._send_owner_dm(ctx, result)

    @commands.command(name="linkedin")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def linkedin(self, ctx: commands.Context, *, text: Optional[str] = None):
        """
        Rewrite text into LinkedIn Speak.

        Usage:
        - !linkedin some text here
        - reply to a message with !linkedin
        """
        await self._run_style_command(
            ctx=ctx,
            text=text,
            mode_key="linkedin",
        )

    @commands.command(name="genz")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def genz(self, ctx: commands.Context, *, text: Optional[str] = None):
        """
        Rewrite text into Gen Z style.

        Usage:
        - !genz some text here
        - reply to a message with !genz
        """
        await self._run_style_command(
            ctx=ctx,
            text=text,
            mode_key="genz",
        )
