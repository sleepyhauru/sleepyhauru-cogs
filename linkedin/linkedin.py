import json
from typing import Optional

import aiohttp
import discord
from redbot.core import commands, Config


class LinkedIn(commands.Cog):
    """Translate text into Kagi LinkedIn Speak."""

    __author__ = "sleepyhauru"
    __version__ = "1.2.0"

    API_URL = "https://translate.kagi.com/api/translate"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=948271650431, force_registration=True)

        self.config.register_global(
            kagi_session="",
            translate_session="",
            model="standard",
        )

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nVersion: {self.__version__}"

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

    async def _translate(self, text: str, from_lang: str = "en_us", to_lang: str = "linkedin") -> str:
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
            "from": from_lang,
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

    async def _get_target_text(self, ctx: commands.Context, text: Optional[str]) -> Optional[str]:
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

    @commands.group(name="linkedinset", invoke_without_command=True)
    @commands.is_owner()
    async def linkedinset(self, ctx: commands.Context):
        """Configure the LinkedIn cog."""
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
        kagi_session, translate_session = await self._get_auth()
        if not kagi_session or not translate_session:
            await ctx.send(
                "Kagi auth is not configured. Use the owner setup commands first:\n"
                "`!linkedinset setkagi <value>`\n"
                "`!linkedinset settranslate <value>`"
            )
            return

        target_text = await self._get_target_text(ctx, text)
        if not target_text:
            await ctx.send("Provide text after `!linkedin` or reply to a message with `!linkedin`.")
            return

        async with ctx.typing():
            try:
                output = await self._translate(target_text, from_lang="en_us", to_lang="linkedin")
            except Exception as e:
                await ctx.send(f"Translation failed: {e}")
                return

        await ctx.send(output)