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

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=948271650431, force_registration=True)

        self.config.register_global(
            kagi_session="",
            translate_session="",
            model="standard",
        )

        self.last_outputs = {}

    def format_help_for_context(self, ctx: commands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs):
        return

    async def _get_auth(self) -> tuple[str, str]:
        kagi_session = await self.config.kagi_session()
        translate_session = await self.config.translate_session()
        return kagi_session.strip(), translate_session.strip()

    @staticmethod
    def _fix_mojibake(text: str) -> str:
        try:
            return text.encode("latin1").decode("utf-8")
        except Exception:
            return text

    def _apply_rng_style(self, text: str, mode: str) -> str:
        linkedin_styles = [
            "Rewrite this in corporate LinkedIn tone.",
            "Make this extremely over-the-top LinkedIn influencer cringe.",
            "Rewrite this as a humblebrag LinkedIn post.",
            "Rewrite this like a startup founder giving motivational insight.",
        ]

        genz_styles = [
            "Rewrite this in casual Gen Z style.",
            "Rewrite this in exaggerated Gen Z slang.",
            "Rewrite this like someone extremely online.",
            "Rewrite this in dry, deadpan Gen Z humor.",
        ]

        if mode == "linkedin":
            style = random.choice(linkedin_styles)
        else:
            style = random.choice(genz_styles)

        return f"{text}\n\n{style}"

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

        payload = {
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

        timeout = aiohttp.ClientTimeout(total=60)
        parts = []

        async with aiohttp.ClientSession(timeout=timeout, cookies=cookies) as session:
            async with session.post(self.API_URL, headers=headers, json=payload) as resp:
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

    async def _get_text(self, ctx: commands.Context, text: Optional[str]) -> Optional[str]:
        if text and text.strip():
            return text.strip()

        ref = getattr(ctx.message, "reference", None)
        if not ref:
            return None

        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            if resolved.content and resolved.content.strip():
                return resolved.content.strip()

            if resolved.embeds:
                embed_text_parts = []
                for embed in resolved.embeds:
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
                if combined:
                    return combined

            return None

        if ref.message_id:
            try:
                replied_message = await ctx.channel.fetch_message(ref.message_id)
                if replied_message.content and replied_message.content.strip():
                    return replied_message.content.strip()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        return None

    async def _run_style_command(
        self,
        ctx: commands.Context,
        text: Optional[str],
        mode_key: str,
        target_lang: str,
        missing_text_message: str,
    ):
        kagi_session, translate_session = await self._get_auth()

        if not kagi_session or not translate_session:
            await ctx.send(
                "Kagi auth is not configured. Use the owner setup commands first:\n"
                "`!linkedinset setkagi <value>`\n"
                "`!linkedinset settranslate <value>`"
            )
            return

        target = await self._get_text(ctx, text)
        if not target:
            await ctx.send(missing_text_message)
            return

        if len(target) > 4000:
            await ctx.send("That message is too long to translate.")
            return

        styled_input = self._apply_rng_style(target, mode_key)

        async with ctx.typing():
            try:
                output = await self._translate(styled_input, target_lang)

                history_key = (ctx.author.id, mode_key)
                last = self.last_outputs.get(history_key)

                if last == output:
                    styled_input = self._apply_rng_style(f"{target}\n\nMake this feel noticeably different.", mode_key)
                    output = await self._translate(styled_input, target_lang)

                self.last_outputs[history_key] = output

            except Exception as e:
                await ctx.send(f"Error: {e}")
                return

        await ctx.send(output, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="linkedinset", invoke_without_command=True)
    @commands.is_owner()
    async def linkedinset(self, ctx: commands.Context):
        """Configure the Kagi cog."""
        await ctx.send_help()

    @linkedinset.command(name="setkagi")
    @commands.is_owner()
    async def set_kagi_session(self, ctx: commands.Context, *, value: str):
        """Set the kagi_session cookie value."""
        await self.config.kagi_session.set(value.strip())
        await ctx.send("Saved `kagi_session`.")

    @linkedinset.command(name="settranslate")
    @commands.is_owner()
    async def set_translate_session(self, ctx: commands.Context, *, value: str):
        """Set the translate_session value."""
        await self.config.translate_session.set(value.strip())
        await ctx.send("Saved `translate_session`.")

    @linkedinset.command(name="setmodel")
    @commands.is_owner()
    async def set_model(self, ctx: commands.Context, *, model: str):
        """Set the Kagi model value, usually 'standard'."""
        await self.config.model.set(model.strip())
        await ctx.send(f"Saved model: `{model.strip()}`")

    @linkedinset.command(name="show")
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
        await ctx.send(msg)

    @linkedinset.command(name="clear")
    @commands.is_owner()
    async def clear_config(self, ctx: commands.Context):
        """Clear stored auth values."""
        await self.config.kagi_session.set("")
        await self.config.translate_session.set("")
        await ctx.send("Cleared stored Kagi auth values.")

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
            target_lang="linkedin",
            missing_text_message="Provide text after `!linkedin` or reply to a message with `!linkedin`.",
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
            target_lang="gen_z",
            missing_text_message="Provide text after `!genz` or reply to a message with `!genz`.",
        )