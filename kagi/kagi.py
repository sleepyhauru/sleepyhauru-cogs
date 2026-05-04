import codecs
import json
import random
import re
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, app_commands, commands


class Kagi(commands.Cog):
    """Kagi Translate integrations for LinkedIn and Gen Z styles."""

    __author__ = "sleepyhauru"
    __version__ = "2.2.0"

    API_URL = "https://translate.kagi.com/api/translate"
    MAX_MESSAGE_LENGTH = 2000
    STYLE_RETURN_DIRECTIVE = "Return only the rewritten text."
    CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):\d{17,20}>")
    URL_ONLY_RE = re.compile(r"^(?:https?://\S+\s*)+$", re.IGNORECASE)
    LANGUAGE_ALIASES = {
        "auto": "auto",
        "detect": "auto",
        "automatic": "auto",
        "english": "en",
        "eng": "en",
        "en-us": "en",
        "en_us": "en",
        "spanish": "es",
        "espanol": "es",
        "español": "es",
        "french": "fr",
        "german": "de",
        "italian": "it",
        "portuguese": "pt",
        "brazilian portuguese": "pt_br",
        "brazilian-portuguese": "pt_br",
        "portuguese (brazil)": "pt_br",
        "japanese": "ja",
        "korean": "ko",
        "chinese": "zh",
        "traditional chinese": "zh_tw",
        "simplified chinese": "zh_cn",
        "russian": "ru",
        "ukrainian": "uk",
        "polish": "pl",
        "dutch": "nl",
        "swedish": "sv",
        "norwegian": "no",
        "danish": "da",
        "finnish": "fi",
        "turkish": "tr",
        "arabic": "ar",
        "hindi": "hi",
        "indonesian": "id",
        "vietnamese": "vi",
    }
    STYLE_CONFIGS = {
        "linkedin": {
            "target_lang": "linkedin",
            "missing_text_message": (
                "Provide text with `linkedin`, reply to a message before running it, "
                "or use the `LinkedIn Rewrite` message command."
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
            "missing_text_message": (
                "Provide text with `genz`, reply to a message before running it, "
                "or use the `Gen Z Rewrite` message command."
            ),
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
        self.translate_context_menu = app_commands.ContextMenu(
            name="Translate to English",
            callback=self.translate_message_app_command,
        )
        self.linkedin_context_menu = app_commands.ContextMenu(
            name="LinkedIn Rewrite",
            callback=self.linkedin_message_app_command,
        )
        self.genz_context_menu = app_commands.ContextMenu(
            name="Gen Z Rewrite",
            callback=self.genz_message_app_command,
        )
        self._register_context_menus()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs):
        return

    def cog_unload(self):
        self._unregister_context_menus()
        if self.session is not None and not getattr(self.session, "closed", False):
            loop = getattr(self.bot, "loop", None)
            if loop is not None:
                loop.create_task(self.session.close())

    def _register_context_menus(self) -> None:
        tree = getattr(self.bot, "tree", None)
        if tree is None:
            return
        for command in (
            self.translate_context_menu,
            self.linkedin_context_menu,
            self.genz_context_menu,
        ):
            tree.add_command(command)

    def _unregister_context_menus(self) -> None:
        tree = getattr(self.bot, "tree", None)
        if tree is None:
            return
        for command in (
            self.translate_context_menu,
            self.linkedin_context_menu,
            self.genz_context_menu,
        ):
            tree.remove_command(command.name, type=command.type)

    async def _get_auth(self) -> tuple[str, str]:
        kagi_session = await self.config.kagi_session()
        translate_session = await self.config.translate_session()
        return kagi_session.strip(), translate_session.strip()

    @staticmethod
    def _prefix(ctx: commands.Context) -> str:
        return getattr(ctx, "clean_prefix", "[p]")

    def _owner_setup_message(self, prefix: str) -> str:
        return (
            "Kagi auth is not configured. Use the owner setup commands first:\n"
            f"`{prefix}kagi setkagi <value>`\n"
            f"`{prefix}kagi settranslate <value>`"
        )

    async def _config_status_message(self, prefix: str) -> str:
        kagi_session, translate_session = await self._get_auth()
        model = await self.config.model()
        next_step = (
            f"Next: `{prefix}kagi test`"
            if kagi_session and translate_session
            else f"Next: configure in DMs with `{prefix}kagi setkagi <value>` and `{prefix}kagi settranslate <value>`."
        )
        return (
            "Kagi configuration\n"
            f"Configured `kagi_session`: `{'yes' if kagi_session else 'no'}`\n"
            f"Configured `translate_session`: `{'yes' if translate_session else 'no'}`\n"
            f"Model: `{model}`\n"
            f"{next_step}"
        )

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

    def _choose_style_prompt(self, mode_key: str) -> str:
        config = self.STYLE_CONFIGS[mode_key]
        return random.choice(config["rng_prompts"])

    def _build_style_context(self, prompt: str) -> str:
        return f"{prompt}\n{self.STYLE_RETURN_DIRECTIVE}"

    @classmethod
    def _normalize_custom_emoji_text(cls, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            return f":{name}:" if name else match.group(0)

        return cls.CUSTOM_EMOJI_RE.sub(replace, text)

    @classmethod
    def _is_url_only_text(cls, text: str) -> bool:
        return bool(text and cls.URL_ONLY_RE.fullmatch(text.strip()))

    @staticmethod
    def _strip_echoed_prompt(output: str, prompt: str) -> str:
        cleaned = output.strip()

        # Kagi occasionally echoes the instruction verbatim as its own paragraph.
        echoed_blocks = (
            f"Instruction: {prompt}",
            f"Instructions: {prompt}",
            prompt,
            Kagi.STYLE_RETURN_DIRECTIVE,
            "Text:",
        )
        for block in echoed_blocks:
            block_patterns = (
                f"\n\n{block}",
                f"{block}\n\n",
                f"\n{block}\n",
            )
            for pattern in block_patterns:
                while pattern in cleaned:
                    cleaned = cleaned.replace(pattern, "\n").strip()
            if cleaned == block:
                cleaned = ""

        if cleaned in echoed_blocks:
            cleaned = ""

        trailing_blocks = (
            f"Instruction: {prompt}",
            f"Instructions: {prompt}",
            prompt,
            Kagi.STYLE_RETURN_DIRECTIVE,
        )
        for block in trailing_blocks:
            if cleaned.endswith(block):
                raw_prefix = cleaned[: -len(block)].rstrip()
                if not raw_prefix or cleaned[len(raw_prefix) :].strip() == block:
                    cleaned = raw_prefix

        if cleaned:
            return cleaned

        if output.strip() in echoed_blocks:
            return ""

        return output.strip()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or getattr(self.session, "closed", False):
            timeout = aiohttp.ClientTimeout(total=60)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    def _build_payload(
        self,
        text: str,
        from_lang: str,
        to_lang: str,
        model: str,
        translate_session: str,
        context: str = "",
    ) -> dict:
        return {
            "text": text,
            "from": from_lang,
            "to": to_lang,
            "stream": True,
            "formality": "default",
            "speaker_gender": "unknown",
            "addressee_gender": "unknown",
            "language_complexity": "standard",
            "translation_style": "natural",
            "context": context,
            "model": model,
            "session_token": translate_session,
            "dictionary_language": "en",
            "use_definition_context": True,
            "enable_language_features": False,
        }

    @classmethod
    def _normalize_language_code(cls, language: str) -> str:
        normalized = language.strip().lower().replace("_", "-")
        return cls.LANGUAGE_ALIASES.get(normalized, normalized.replace("-", "_"))

    async def _collect_stream_text(self, resp) -> str:
        parts = []

        def handle_line(line: str) -> bool:
            line = line.strip()
            if not line or not line.startswith("data: "):
                return False

            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                return False

            if "delta" in event:
                parts.append(event["delta"])

            return event.get("done") is True

        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        buffer = ""
        done = False
        async for raw_line in resp.content:
            buffer += decoder.decode(raw_line)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if handle_line(line):
                    buffer = ""
                    done = True
                    break
            if done:
                break

        if not done:
            buffer += decoder.decode(b"", final=True)
            if buffer:
                handle_line(buffer)

        final_text = "".join(parts).strip()
        final_text = self._fix_mojibake(final_text)

        if not final_text:
            raise RuntimeError("No translated output was returned.")

        return final_text

    async def _translate(
        self, text: str, to_lang: str, context: str = "", from_lang: str = "auto"
    ) -> str:
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
        payload = self._build_payload(
            text,
            self._normalize_language_code(from_lang),
            self._normalize_language_code(to_lang),
            model,
            translate_session,
            context=context,
        )
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
        content = message.content.strip() if message.content and message.content.strip() else None

        embed_text_parts = []
        for embed in message.embeds or []:
            if embed.title:
                embed_text_parts.append(embed.title)
            if embed.description:
                embed_text_parts.append(embed.description)
            for field in embed.fields:
                if field.name:
                    embed_text_parts.append(field.name)
                if field.value:
                    embed_text_parts.append(field.value)

        embed_text = "\n".join(part for part in embed_text_parts if part and part.strip()).strip() or None

        if embed_text and (content is None or self._is_url_only_text(content)):
            return embed_text

        return content or embed_text

    async def _send_output(self, ctx: commands.Context, output: str):
        for start in range(0, len(output), self.MAX_MESSAGE_LENGTH):
            chunk = output[start : start + self.MAX_MESSAGE_LENGTH]
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    async def _send_interaction_message(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        kwargs = {
            "allowed_mentions": discord.AllowedMentions.none(),
            "ephemeral": ephemeral,
        }
        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", None)
        if response is not None and callable(is_done) and not is_done():
            await response.send_message(message, **kwargs)
            return

        followup = getattr(interaction, "followup", None)
        if followup is not None:
            await followup.send(message, **kwargs)

    async def _send_interaction_output(
        self,
        interaction: discord.Interaction,
        output: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        chunks = [
            output[start : start + self.MAX_MESSAGE_LENGTH]
            for start in range(0, len(output), self.MAX_MESSAGE_LENGTH)
        ]
        if not chunks:
            chunks = [""]

        for index, chunk in enumerate(chunks):
            await self._send_interaction_message(
                interaction,
                chunk,
                ephemeral=ephemeral if index == 0 else ephemeral,
            )

    async def _run_style_for_message(self, message: discord.Message, mode_key: str) -> str:
        target = self._extract_message_text(message)
        if not target:
            raise ValueError("That message doesn't have any text I can rewrite.")

        target = self._normalize_custom_emoji_text(target)
        if len(target) > 4000:
            raise ValueError("That message is too long to rewrite.")

        prompt = self._choose_style_prompt(mode_key)
        output = await self._translate(
            target,
            self.STYLE_CONFIGS[mode_key]["target_lang"],
            context=self._build_style_context(prompt),
        )
        return self._strip_echoed_prompt(output, prompt)

    async def _run_translate_for_message(
        self,
        message: discord.Message,
        *,
        to_lang: str,
        from_lang: str = "auto",
    ) -> str:
        target = self._extract_message_text(message)
        if not target:
            raise ValueError("That message doesn't have any text I can translate.")

        target = self._normalize_custom_emoji_text(target)
        if len(target) > 4000:
            raise ValueError("That message is too long to translate.")

        return await self._translate(target, to_lang, from_lang=from_lang)

    async def _message_context_translate(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        *,
        to_lang: str,
        from_lang: str = "auto",
    ) -> None:
        prefix = self._prefix(interaction)
        kagi_session, translate_session = await self._get_auth()
        if not kagi_session or not translate_session:
            await self._send_interaction_message(
                interaction,
                self._owner_setup_message(prefix),
                ephemeral=True,
            )
            return

        response = getattr(interaction, "response", None)
        if response is not None and hasattr(response, "defer"):
            await response.defer(thinking=True)

        try:
            output = await self._run_translate_for_message(
                message,
                to_lang=to_lang,
                from_lang=from_lang,
            )
        except Exception as error:
            await self._send_interaction_message(interaction, f"Error: {error}", ephemeral=True)
            return

        await self._send_interaction_output(interaction, output)

    async def _message_context_style(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        *,
        mode_key: str,
    ) -> None:
        prefix = self._prefix(interaction)
        kagi_session, translate_session = await self._get_auth()
        if not kagi_session or not translate_session:
            await self._send_interaction_message(
                interaction,
                self._owner_setup_message(prefix),
                ephemeral=True,
            )
            return

        response = getattr(interaction, "response", None)
        if response is not None and hasattr(response, "defer"):
            await response.defer(thinking=True)

        try:
            output = await self._run_style_for_message(message, mode_key)
        except Exception as error:
            await self._send_interaction_message(interaction, f"Error: {error}", ephemeral=True)
            return

        await self._send_interaction_output(interaction, output)

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
            await ctx.send(self._owner_setup_message(self._prefix(ctx)))
            return

        target = await self._get_text(ctx, text)
        if not target:
            await ctx.send(config["missing_text_message"])
            return

        target = self._normalize_custom_emoji_text(target)

        if len(target) > 4000:
            await ctx.send("That message is too long to translate.")
            return

        prompt = self._choose_style_prompt(mode_key)
        style_context = self._build_style_context(prompt)
        async with ctx.typing():
            try:
                output = await self._translate(target, config["target_lang"], context=style_context)
            except Exception as e:
                await ctx.send(f"Error: {e}")
                return

        output = self._strip_echoed_prompt(output, prompt)
        await self._send_output(ctx, output)

    async def _run_translate_command(
        self,
        ctx: commands.Context,
        text: Optional[str],
        *,
        to_lang: str = "en",
        from_lang: str = "auto",
        missing_text_message: str,
    ):
        kagi_session, translate_session = await self._get_auth()

        if not kagi_session or not translate_session:
            await ctx.send(self._owner_setup_message(self._prefix(ctx)))
            return

        target = await self._get_text(ctx, text)
        if not target:
            await ctx.send(missing_text_message)
            return

        target = self._normalize_custom_emoji_text(target)

        if len(target) > 4000:
            await ctx.send("That message is too long to translate.")
            return

        async with ctx.typing():
            try:
                output = await self._translate(target, to_lang, from_lang=from_lang)
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
        await ctx.send(await self._config_status_message(self._prefix(ctx)))

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
        msg = await self._config_status_message(self._prefix(ctx))
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
            await ctx.send(
                f"Kagi auth is incomplete. Use `{self._prefix(ctx)}kagi show` to inspect current config state."
            )
            return

        async with ctx.typing():
            try:
                result = await self._run_config_test()
            except Exception as error:
                await ctx.send(f"Auth check failed: {error}")
                return

        await self._send_owner_dm(ctx, result)

    @commands.hybrid_command(name="linkedin")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def linkedin(self, ctx: commands.Context, *, text: Optional[str] = None):
        """
        Rewrite text into LinkedIn Speak.

        Pass text directly, reply to a message before running it,
        or use the `LinkedIn Rewrite` message command.
        """
        await self._run_style_command(
            ctx=ctx,
            text=text,
            mode_key="linkedin",
        )

    @commands.hybrid_command(name="genz")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def genz(self, ctx: commands.Context, *, text: Optional[str] = None):
        """
        Rewrite text into Gen Z style.

        Pass text directly, reply to a message before running it,
        or use the `Gen Z Rewrite` message command.
        """
        await self._run_style_command(
            ctx=ctx,
            text=text,
            mode_key="genz",
        )

    @commands.hybrid_command(name="translate", aliases=["tr"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def translate(self, ctx: commands.Context, *, text: Optional[str] = None):
        """
        Translate text with automatic language detection into English.

        Pass text directly, reply to a message before running it,
        or use the `Translate to English` message command.
        """
        await self._run_translate_command(
            ctx,
            text,
            to_lang="en",
            from_lang="auto",
            missing_text_message=(
                "Provide text with `translate`, reply to a message before running it, "
                "or use the `Translate to English` message command."
            ),
        )

    @commands.hybrid_command(name="translateinto", aliases=["trto"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def translate_into(
        self, ctx: commands.Context, target_language: str, *, text: Optional[str] = None
    ):
        """
        Translate text with automatic language detection into a chosen language.

        Pass text directly or reply to a message before running it.
        """
        await self._run_translate_command(
            ctx,
            text,
            to_lang=target_language,
            from_lang="auto",
            missing_text_message=(
                "Provide text with `translateinto <language>` or reply to a message before running it."
            ),
        )

    @app_commands.guild_only()
    async def translate_message_app_command(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ):
        await self._message_context_translate(interaction, message, to_lang="en")

    @app_commands.guild_only()
    async def linkedin_message_app_command(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ):
        await self._message_context_style(interaction, message, mode_key="linkedin")

    @app_commands.guild_only()
    async def genz_message_app_command(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ):
        await self._message_context_style(interaction, message, mode_key="genz")
