from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from .client import OllamaClient, OllamaClientError, normalize_base_url
from .formatters import split_discord_messages, truncate_response
from .history import build_ollama_messages, make_user_content, trim_history
from .personality import (
    DEFAULT_ANALYSIS_MESSAGE_LIMIT,
    MAX_ANALYSIS_MESSAGES,
    MIN_ANALYSIS_MESSAGES,
    PersonalityProfileError,
    build_personality_analysis_messages,
    clean_message_sample,
    format_personality_display,
    format_personality_prompt_block,
    personality_history_scan_limit,
    parse_personality_profile,
    unique_personality_name,
    validate_personality_name,
)


DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful local assistant in a private Discord server. "
    "Keep replies concise, friendly, and safe for Discord. "
    "User messages may be prefixed with Discord display names. "
    "Do not create mass mentions such as @everyone or @here."
)
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_HISTORY_LIMIT = 12
DEFAULT_CONTEXT_CHAR_BUDGET = 12000
DEFAULT_FOLLOWUP_SECONDS = 300
DEFAULT_MAX_RESPONSE_CHARS = 6000
CUSTOM_CHANNEL_HISTORY = "CHANNEL_HISTORY"
NO_MENTIONS = discord.AllowedMentions.none()


class OllamaChat(commands.Cog):
    """Chat with a local Ollama server from owner-whitelisted channels."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=847239512034681,
            force_registration=True,
        )
        self.config.register_guild(
            base_url=DEFAULT_BASE_URL,
            model=DEFAULT_MODEL,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            temperature=0.7,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            history_limit=DEFAULT_HISTORY_LIMIT,
            context_char_budget=DEFAULT_CONTEXT_CHAR_BUDGET,
            followup_window_seconds=DEFAULT_FOLLOWUP_SECONDS,
            trigger_mode="mention",
            max_response_chars=DEFAULT_MAX_RESPONSE_CHARS,
            whitelisted_channels=[],
            history_channels=[],
            personalities={},
            active_personality=None,
        )
        self.config.init_custom(CUSTOM_CHANNEL_HISTORY, 2)
        self.config.register_custom(CUSTOM_CHANNEL_HISTORY, history=[], last_followup=0.0)
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    @commands.command(name="ai")
    async def ai_command(self, ctx: commands.Context, *, prompt: str = "") -> None:
        """Ask the configured local Ollama model."""
        await self._handle_prompt(ctx, prompt)

    @commands.group(name="ollama", aliases=["ollamachat"], invoke_without_command=True)
    async def ollama_group(self, ctx: commands.Context) -> None:
        """Chat with or inspect the local Ollama connection."""
        if ctx.invoked_subcommand is None:
            if not await self._ensure_guild(ctx):
                return
            await ctx.send_help()

    @ollama_group.command(name="ask")
    async def ollama_ask(self, ctx: commands.Context, *, prompt: str = "") -> None:
        """Ask the configured local Ollama model."""
        await self._handle_prompt(ctx, prompt)

    @ollama_group.command(name="status")
    @commands.is_owner()
    async def ollama_status(self, ctx: commands.Context) -> None:
        """Show current settings and test the Ollama connection."""
        if not await self._ensure_guild(ctx):
            return

        settings = await self.config.guild(ctx.guild).all()
        client = OllamaClient(
            settings["base_url"],
            timeout_seconds=float(settings["timeout_seconds"]),
        )
        connection = "unreachable"
        try:
            models = await client.list_models()
        except OllamaClientError as exc:
            connection_detail = str(exc)
        else:
            connection = "reachable"
            connection_detail = (
                f"{len(models)} model(s) available"
                if models
                else "reachable, but no models were reported"
            )

        channels = await self._format_whitelisted_channels(ctx.guild)
        mode = settings["trigger_mode"]
        followup_minutes = int(settings["followup_window_seconds"]) / 60
        personalities = settings.get("personalities") or {}
        active_personality = settings.get("active_personality") or "none"
        message = (
            "**OllamaChat status**\n"
            f"Connection: {connection} ({connection_detail})\n"
            f"Base URL: `{settings['base_url']}`\n"
            f"Model: `{settings['model']}`\n"
            f"Trigger mode: `{mode}` (commands always require a whitelisted channel)\n"
            f"Follow-up window: `{followup_minutes:g}` minute(s)\n"
            f"Temperature: `{settings['temperature']}`\n"
            f"History limit: `{settings['history_limit']}` turn(s)\n"
            f"Context budget: `{settings['context_char_budget']}` characters\n"
            f"Max response: `{settings['max_response_chars']}` characters\n"
            f"Personalities: `{len(personalities)}` stored, active `{active_personality}`\n"
            f"Whitelisted channels: {channels}"
        )
        await self._send_text(ctx.channel, message)

    @ollama_group.command(name="models")
    @commands.is_owner()
    async def ollama_models(self, ctx: commands.Context) -> None:
        """List models reported by the configured Ollama server."""
        if not await self._ensure_guild(ctx):
            return

        settings = await self.config.guild(ctx.guild).all()
        client = OllamaClient(
            settings["base_url"],
            timeout_seconds=float(settings["timeout_seconds"]),
        )
        async with ctx.typing():
            try:
                models = await client.list_models()
            except OllamaClientError as exc:
                await self._send_text(ctx.channel, f"Could not list Ollama models: {exc}")
                return

        if not models:
            await self._send_text(ctx.channel, "Ollama is reachable, but it did not report any models.")
            return

        await self._send_text(ctx.channel, "**Available Ollama models**\n" + "\n".join(f"- `{name}`" for name in models))

    @ollama_group.group(name="personality", aliases=["persona"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def personality_group(self, ctx: commands.Context) -> None:
        """Learn and manage OllamaChat personality profiles."""
        if ctx.invoked_subcommand is None:
            if not await self._ensure_guild(ctx):
                return
            await ctx.send_help()

    @personality_group.command(name="learn")
    @commands.has_permissions(manage_guild=True)
    async def personality_learn(
        self,
        ctx: commands.Context,
        target: discord.Member,
        message_limit: int = DEFAULT_ANALYSIS_MESSAGE_LIMIT,
    ) -> None:
        """Analyze a member's whitelisted-channel messages and save a profile."""
        if not await self._ensure_guild(ctx):
            return
        if target.bot:
            await ctx.send("I cannot learn a useful personality from bot messages.", allowed_mentions=NO_MENTIONS)
            return
        if message_limit < MIN_ANALYSIS_MESSAGES or message_limit > MAX_ANALYSIS_MESSAGES:
            await ctx.send(
                f"Message limit should be between `{MIN_ANALYSIS_MESSAGES}` and `{MAX_ANALYSIS_MESSAGES}`.",
                allowed_mentions=NO_MENTIONS,
            )
            return

        whitelisted_channels = await self.config.guild(ctx.guild).whitelisted_channels()
        if not whitelisted_channels:
            await ctx.send(
                "No channels are whitelisted yet. Add one with `[p]ollamaset channel add` before learning a profile.",
                allowed_mentions=NO_MENTIONS,
            )
            return

        async with ctx.typing():
            samples = await self.collect_user_messages(ctx.guild, target, message_limit)
            if len(samples) < MIN_ANALYSIS_MESSAGES:
                await ctx.send(
                    f"I found `{len(samples)}` usable message(s) from {target.mention}; "
                    f"I need at least `{MIN_ANALYSIS_MESSAGES}` from whitelisted channels.",
                    allowed_mentions=NO_MENTIONS,
                )
                return

            try:
                generated = await self.generate_personality_profile(ctx.guild, samples)
            except OllamaClientError as exc:
                await ctx.send(f"Ollama could not generate a personality profile: {exc}", allowed_mentions=NO_MENTIONS)
                return

        guild_conf = self.config.guild(ctx.guild)
        personalities = await guild_conf.personalities()
        if not isinstance(personalities, dict):
            personalities = {}

        profile_name = unique_personality_name(target.name, personalities)
        profile = {
            **generated,
            "name": profile_name,
            "source_user_id": target.id,
            "source_username": str(target),
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "message_count": len(samples),
        }
        personalities[profile_name] = profile
        await guild_conf.personalities.set(personalities)

        await ctx.send(
            f"Saved personality `{profile_name}` from `{len(samples)}` message(s). "
            f"Activate it with `[p]ollamachat personality set {profile_name}`.",
            allowed_mentions=NO_MENTIONS,
        )

    @personality_group.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def personality_list(self, ctx: commands.Context) -> None:
        """List stored personality profiles."""
        if not await self._ensure_guild(ctx):
            return

        settings = await self.config.guild(ctx.guild).all()
        personalities = settings.get("personalities") or {}
        active = settings.get("active_personality")
        if not personalities:
            await ctx.send("No OllamaChat personalities are saved yet.", allowed_mentions=NO_MENTIONS)
            return

        lines = ["**OllamaChat personalities**"]
        for name in sorted(personalities):
            marker = " (active)" if name == active else ""
            profile = personalities.get(name) or {}
            count = profile.get("message_count", 0) if isinstance(profile, dict) else 0
            lines.append(f"- `{name}`{marker} - `{count}` message(s)")
        await self._send_text(ctx.channel, "\n".join(lines))

    @personality_group.command(name="show")
    @commands.has_permissions(manage_guild=True)
    async def personality_show(self, ctx: commands.Context, name: str) -> None:
        """Show a stored personality profile."""
        if not await self._ensure_guild(ctx):
            return

        try:
            profile_name = validate_personality_name(name)
        except PersonalityProfileError as exc:
            await ctx.send(str(exc), allowed_mentions=NO_MENTIONS)
            return

        personalities = await self.config.guild(ctx.guild).personalities()
        profile = personalities.get(profile_name) if isinstance(personalities, dict) else None
        if not isinstance(profile, dict):
            await ctx.send(f"I do not have a personality named `{profile_name}`.", allowed_mentions=NO_MENTIONS)
            return

        try:
            display = format_personality_display(profile_name, profile)
        except PersonalityProfileError as exc:
            await ctx.send(f"That personality is malformed: {exc}", allowed_mentions=NO_MENTIONS)
            return
        await self._send_text(ctx.channel, display)

    @personality_group.command(name="set", aliases=["activate"])
    @commands.has_permissions(manage_guild=True)
    async def personality_set(self, ctx: commands.Context, name: str) -> None:
        """Activate a stored personality profile."""
        if not await self._ensure_guild(ctx):
            return

        try:
            profile_name = validate_personality_name(name)
        except PersonalityProfileError as exc:
            await ctx.send(str(exc), allowed_mentions=NO_MENTIONS)
            return

        personalities = await self.config.guild(ctx.guild).personalities()
        if not isinstance(personalities, dict) or profile_name not in personalities:
            await ctx.send(f"I do not have a personality named `{profile_name}`.", allowed_mentions=NO_MENTIONS)
            return

        await self.config.guild(ctx.guild).active_personality.set(profile_name)
        await ctx.send(f"Active OllamaChat personality set to `{profile_name}`.", allowed_mentions=NO_MENTIONS)

    @personality_group.command(name="clear")
    @commands.has_permissions(manage_guild=True)
    async def personality_clear(self, ctx: commands.Context) -> None:
        """Clear the active personality profile."""
        if not await self._ensure_guild(ctx):
            return
        await self.config.guild(ctx.guild).active_personality.set(None)
        await ctx.send("Active OllamaChat personality cleared.", allowed_mentions=NO_MENTIONS)

    @personality_group.command(name="delete", aliases=["remove"])
    @commands.has_permissions(manage_guild=True)
    async def personality_delete(self, ctx: commands.Context, name: str) -> None:
        """Delete a stored personality profile."""
        if not await self._ensure_guild(ctx):
            return

        try:
            profile_name = validate_personality_name(name)
        except PersonalityProfileError as exc:
            await ctx.send(str(exc), allowed_mentions=NO_MENTIONS)
            return

        guild_conf = self.config.guild(ctx.guild)
        personalities = await guild_conf.personalities()
        if not isinstance(personalities, dict) or profile_name not in personalities:
            await ctx.send(f"I do not have a personality named `{profile_name}`.", allowed_mentions=NO_MENTIONS)
            return

        del personalities[profile_name]
        await guild_conf.personalities.set(personalities)
        if await guild_conf.active_personality() == profile_name:
            await guild_conf.active_personality.set(None)

        await ctx.send(f"Deleted personality `{profile_name}`.", allowed_mentions=NO_MENTIONS)

    @commands.group(name="ollamaset", invoke_without_command=True)
    @commands.is_owner()
    async def ollamaset_group(self, ctx: commands.Context) -> None:
        """Configure OllamaChat."""
        if ctx.invoked_subcommand is None:
            if not await self._ensure_guild(ctx):
                return
            await ctx.send_help()

    @ollamaset_group.command(name="url")
    async def set_url(self, ctx: commands.Context, *, url: str = "") -> None:
        """Set the Ollama base URL."""
        if not await self._ensure_guild(ctx):
            return
        try:
            normalized = normalize_base_url(url)
        except ValueError as exc:
            await ctx.send(f"I could not use that URL: {exc}", allowed_mentions=NO_MENTIONS)
            return

        await self.config.guild(ctx.guild).base_url.set(normalized)
        await ctx.send(f"Ollama base URL set to `{normalized}`.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="model")
    async def set_model(self, ctx: commands.Context, *, model: str = "") -> None:
        """Set the Ollama model name."""
        if not await self._ensure_guild(ctx):
            return
        model = model.strip()
        if not model:
            await ctx.send("Tell me which model to use, such as `qwen3:8b`.", allowed_mentions=NO_MENTIONS)
            return
        await self.config.guild(ctx.guild).model.set(model)
        await ctx.send(f"Ollama model set to `{model}`.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="prompt")
    async def set_prompt(self, ctx: commands.Context, *, prompt: str = "") -> None:
        """Set or reset the system prompt."""
        if not await self._ensure_guild(ctx):
            return
        cleaned = prompt.strip()
        if not cleaned or cleaned.lower() in {"default", "reset", "clear"}:
            await self.config.guild(ctx.guild).system_prompt.set(DEFAULT_SYSTEM_PROMPT)
            await ctx.send("System prompt reset to the default Discord-safe prompt.", allowed_mentions=NO_MENTIONS)
            return

        await self.config.guild(ctx.guild).system_prompt.set(cleaned)
        await ctx.send("System prompt updated.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="temperature")
    async def set_temperature(self, ctx: commands.Context, temperature: float) -> None:
        """Set model temperature between 0 and 2."""
        if not await self._ensure_guild(ctx):
            return
        if temperature < 0 or temperature > 2:
            await ctx.send("Temperature should be between `0` and `2`.", allowed_mentions=NO_MENTIONS)
            return
        await self.config.guild(ctx.guild).temperature.set(temperature)
        await ctx.send(f"Temperature set to `{temperature:g}`.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="history")
    async def set_history_limit(self, ctx: commands.Context, turns: int) -> None:
        """Set how many recent user/assistant turns are kept per channel."""
        if not await self._ensure_guild(ctx):
            return
        if turns < 0 or turns > 40:
            await ctx.send("History limit should be between `0` and `40` turns.", allowed_mentions=NO_MENTIONS)
            return
        await self.config.guild(ctx.guild).history_limit.set(turns)
        await ctx.send(f"History limit set to `{turns}` turn(s).", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="budget")
    async def set_context_budget(self, ctx: commands.Context, characters: int) -> None:
        """Set the approximate character budget for stored context."""
        if not await self._ensure_guild(ctx):
            return
        if characters < 1000 or characters > 60000:
            await ctx.send("Context budget should be between `1000` and `60000` characters.", allowed_mentions=NO_MENTIONS)
            return
        await self.config.guild(ctx.guild).context_char_budget.set(characters)
        await ctx.send(f"Context budget set to `{characters}` characters.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="followup")
    async def set_followup(self, ctx: commands.Context, minutes: float) -> None:
        """Set the unmentioned follow-up window in minutes."""
        if not await self._ensure_guild(ctx):
            return
        if minutes < 0 or minutes > 120:
            await ctx.send("Follow-up window should be between `0` and `120` minutes.", allowed_mentions=NO_MENTIONS)
            return
        seconds = int(minutes * 60)
        await self.config.guild(ctx.guild).followup_window_seconds.set(seconds)
        await ctx.send(f"Follow-up window set to `{minutes:g}` minute(s).", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="mode")
    async def set_trigger_mode(self, ctx: commands.Context, mode: str = "") -> None:
        """Set listener mode to command or mention."""
        if not await self._ensure_guild(ctx):
            return
        normalized = mode.strip().lower()
        if normalized not in {"command", "mention"}:
            await ctx.send("Mode must be `command` or `mention`.", allowed_mentions=NO_MENTIONS)
            return
        await self.config.guild(ctx.guild).trigger_mode.set(normalized)
        if normalized == "command":
            detail = "Mention listener disabled; `[p]ai` and `[p]ollama ask` still work in whitelisted channels."
        else:
            detail = "Mention listener enabled for whitelisted channels."
        await ctx.send(f"Trigger mode set to `{normalized}`. {detail}", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.command(name="maxchars")
    async def set_max_response_chars(self, ctx: commands.Context, characters: int) -> None:
        """Set the maximum stored/sent response length."""
        if not await self._ensure_guild(ctx):
            return
        if characters < 500 or characters > 12000:
            await ctx.send("Max response length should be between `500` and `12000` characters.", allowed_mentions=NO_MENTIONS)
            return
        await self.config.guild(ctx.guild).max_response_chars.set(characters)
        await ctx.send(f"Max response length set to `{characters}` characters.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.group(name="channel", aliases=["whitelist"], invoke_without_command=True)
    async def channel_group(self, ctx: commands.Context) -> None:
        """Manage channels where chat is allowed."""
        if ctx.invoked_subcommand is None:
            if not await self._ensure_guild(ctx):
                return
            await ctx.send_help()

    @channel_group.command(name="add", aliases=["enable"])
    async def channel_add(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Whitelist a channel. Defaults to the current channel."""
        if not await self._ensure_guild(ctx):
            return
        target = await self._resolve_channel(ctx, channel)
        if target is None:
            return

        guild_conf = self.config.guild(ctx.guild)
        channels = await guild_conf.whitelisted_channels()
        if target.id in channels:
            await ctx.send(f"{target.mention} is already whitelisted.", allowed_mentions=NO_MENTIONS)
            return

        channels.append(target.id)
        await guild_conf.whitelisted_channels.set(channels)
        await ctx.send(f"{target.mention} is now whitelisted for OllamaChat.", allowed_mentions=NO_MENTIONS)

    @channel_group.command(name="remove", aliases=["disable"])
    async def channel_remove(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Remove a channel from the whitelist. Defaults to the current channel."""
        if not await self._ensure_guild(ctx):
            return
        target = await self._resolve_channel(ctx, channel)
        if target is None:
            return

        guild_conf = self.config.guild(ctx.guild)
        channels = await guild_conf.whitelisted_channels()
        if target.id not in channels:
            await ctx.send(f"{target.mention} is not currently whitelisted.", allowed_mentions=NO_MENTIONS)
            return

        channels = [channel_id for channel_id in channels if channel_id != target.id]
        await guild_conf.whitelisted_channels.set(channels)
        await ctx.send(f"{target.mention} was removed from the OllamaChat whitelist.", allowed_mentions=NO_MENTIONS)

    @channel_group.command(name="list")
    async def channel_list(self, ctx: commands.Context) -> None:
        """List whitelisted channels."""
        if not await self._ensure_guild(ctx):
            return
        channels = await self._format_whitelisted_channels(ctx.guild)
        await self._send_text(ctx.channel, f"Whitelisted channels: {channels}")

    @channel_group.command(name="clear")
    async def channel_clear(self, ctx: commands.Context) -> None:
        """Clear the channel whitelist."""
        if not await self._ensure_guild(ctx):
            return
        await self.config.guild(ctx.guild).whitelisted_channels.set([])
        await ctx.send("OllamaChat channel whitelist cleared.", allowed_mentions=NO_MENTIONS)

    @ollamaset_group.group(name="forget", aliases=["reset"], invoke_without_command=True)
    async def forget_group(self, ctx: commands.Context) -> None:
        """Clear stored recent conversation context."""
        if ctx.invoked_subcommand is None:
            if not await self._ensure_guild(ctx):
                return
            await ctx.send_help()

    @forget_group.command(name="channel")
    async def forget_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Clear recent context for one channel. Defaults to the current channel."""
        if not await self._ensure_guild(ctx):
            return
        target = await self._resolve_channel(ctx, channel)
        if target is None:
            return
        await self._clear_channel_history(ctx.guild.id, target.id)
        await ctx.send(f"Recent OllamaChat context cleared for {target.mention}.", allowed_mentions=NO_MENTIONS)

    @forget_group.command(name="guild")
    async def forget_guild(self, ctx: commands.Context) -> None:
        """Clear recent context for all tracked channels in this guild."""
        if not await self._ensure_guild(ctx):
            return
        guild_conf = self.config.guild(ctx.guild)
        channel_ids = await guild_conf.history_channels()
        for channel_id in channel_ids:
            await self._clear_channel_history(ctx.guild.id, int(channel_id))
        await guild_conf.history_channels.set([])
        await ctx.send("Recent OllamaChat context cleared for this guild.", allowed_mentions=NO_MENTIONS)

    @forget_group.command(name="user")
    async def forget_user(
        self,
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
    ) -> None:
        """Explain v1 user data behavior."""
        if not await self._ensure_guild(ctx):
            return
        target = user or ctx.author
        await ctx.send(
            f"OllamaChat v1 does not keep separate user memory for {target.mention}. "
            "It only stores bounded recent channel context; use `forget channel` or `forget guild` to clear it.",
            allowed_mentions=NO_MENTIONS,
        )

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        settings = await self.config.guild(message.guild).all()
        if settings["trigger_mode"] != "mention":
            return
        if message.channel.id not in settings["whitelisted_channels"]:
            return

        mentioned = self._mentions_bot(message)
        if mentioned:
            prompt = self._strip_bot_mentions(message.content).strip()
        else:
            if not await self._has_active_followup(message.guild.id, message.channel.id, settings):
                return
            prompt = message.content.strip()

        if not prompt:
            if mentioned:
                await message.channel.send(
                    "Mention me with a question or prompt and I will ask Ollama.",
                    allowed_mentions=NO_MENTIONS,
                )
            return

        await self._run_chat(
            guild=message.guild,
            channel=message.channel,
            author=message.author,
            prompt=prompt,
        )

    async def _handle_prompt(self, ctx: commands.Context, prompt: str) -> None:
        if not await self._ensure_guild(ctx):
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("OllamaChat v1 only works in server text channels.", allowed_mentions=NO_MENTIONS)
            return

        prompt = prompt.strip()
        if not prompt:
            await ctx.send("Give me something to ask Ollama, like `[p]ai explain Docker cache dirs`.", allowed_mentions=NO_MENTIONS)
            return

        if not await self._is_channel_whitelisted(ctx.guild, ctx.channel.id):
            await ctx.send(
                "This channel is not whitelisted for OllamaChat yet. "
                "The bot owner can run `[p]ollamaset channel add` here.",
                allowed_mentions=NO_MENTIONS,
            )
            return

        await self._run_chat(
            guild=ctx.guild,
            channel=ctx.channel,
            author=ctx.author,
            prompt=prompt,
        )

    async def _run_chat(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel,
        author: discord.abc.User,
        prompt: str,
    ) -> None:
        lock = self._get_lock(guild.id, channel.id)
        if lock.locked():
            await channel.send(
                "I am already working on an Ollama reply in this channel. Try again when it lands.",
                allowed_mentions=NO_MENTIONS,
            )
            return

        async with lock:
            settings = await self.config.guild(guild).all()
            system_prompt = self._build_system_prompt(settings)
            channel_conf = self.config.custom(CUSTOM_CHANNEL_HISTORY, str(guild.id), str(channel.id))
            user_content = make_user_content(author.display_name, prompt)
            history_budget = max(
                0,
                int(settings["context_char_budget"])
                - len(system_prompt)
                - len(user_content),
            )
            history = trim_history(
                await channel_conf.history(),
                max_turns=int(settings["history_limit"]),
                char_budget=history_budget,
            )
            messages = build_ollama_messages(
                system_prompt=system_prompt,
                history=history,
                user_content=user_content,
            )
            client = OllamaClient(
                settings["base_url"],
                timeout_seconds=float(settings["timeout_seconds"]),
            )

            async with channel.typing():
                try:
                    response = await client.chat(
                        model=settings["model"],
                        messages=messages,
                        temperature=float(settings["temperature"]),
                    )
                except OllamaClientError as exc:
                    await channel.send(f"Ollama could not answer: {exc}", allowed_mentions=NO_MENTIONS)
                    return

            response = truncate_response(response, int(settings["max_response_chars"]))
            for chunk in split_discord_messages(response):
                await channel.send(chunk, allowed_mentions=NO_MENTIONS)

            updated_history = trim_history(
                [
                    *history,
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": response},
                ],
                max_turns=int(settings["history_limit"]),
                char_budget=int(settings["context_char_budget"]),
            )
            await channel_conf.history.set(updated_history)
            await channel_conf.last_followup.set(time.time())
            await self._track_history_channel(guild, channel.id)

    async def collect_user_messages(
        self,
        guild: discord.Guild,
        target: discord.Member,
        limit: int,
    ) -> list[str]:
        """Collect clean recent messages from whitelisted channels for one member."""
        channel_ids = await self.config.guild(guild).whitelisted_channels()
        scan_limit = personality_history_scan_limit(limit)
        collected: list[tuple[float, str]] = []
        seen: set[str] = set()

        for channel_id in channel_ids:
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                async for message in channel.history(limit=scan_limit):
                    if message.author.id != target.id or message.author.bot:
                        continue
                    if await self._is_command_message(message):
                        continue

                    cleaned = clean_message_sample(message.content)
                    if cleaned is None:
                        continue

                    spam_key = cleaned.casefold()
                    if spam_key in seen:
                        continue

                    seen.add(spam_key)
                    collected.append((message.created_at.timestamp(), cleaned))
            except (discord.Forbidden, discord.HTTPException):
                continue

        selected = sorted(collected, key=lambda item: item[0], reverse=True)[:limit]
        selected.sort(key=lambda item: item[0])
        return [content for _, content in selected]

    async def generate_personality_profile(
        self,
        guild: discord.Guild,
        messages: list[str],
    ) -> dict:
        """Ask Ollama to turn message samples into a safe structured profile."""
        settings = await self.config.guild(guild).all()
        client = OllamaClient(
            settings["base_url"],
            timeout_seconds=float(settings["timeout_seconds"]),
        )
        analysis_messages = build_personality_analysis_messages(messages)
        last_error: Optional[Exception] = None

        for attempt in range(2):
            response = await client.chat(
                model=settings["model"],
                messages=analysis_messages,
                temperature=0.2,
            )
            try:
                return parse_personality_profile(response)
            except PersonalityProfileError as exc:
                last_error = exc
                if attempt == 0:
                    analysis_messages.extend(
                        [
                            {"role": "assistant", "content": response[:4000]},
                            {
                                "role": "user",
                                "content": "Return ONLY valid JSON using the required structure. Do not include markdown.",
                            },
                        ]
                    )

        detail = f": {last_error}" if last_error else ""
        raise OllamaClientError(f"Ollama returned an invalid personality profile{detail}")

    async def build_system_prompt(self, guild: discord.Guild) -> str:
        """Build the current chat system prompt, including any active personality."""
        settings = await self.config.guild(guild).all()
        return self._build_system_prompt(settings)

    def _build_system_prompt(self, settings: dict) -> str:
        base_prompt = str(settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT).strip()
        personalities = settings.get("personalities") or {}
        active = settings.get("active_personality")
        if not isinstance(personalities, dict) or not isinstance(active, str):
            return base_prompt

        profile = personalities.get(active)
        if not isinstance(profile, dict):
            return base_prompt

        try:
            personality_block = format_personality_prompt_block(profile)
        except PersonalityProfileError:
            return base_prompt

        return f"{base_prompt}\n\n{personality_block}"

    async def _ensure_guild(self, ctx: commands.Context) -> bool:
        if ctx.guild is not None:
            return True
        await ctx.send(
            "OllamaChat v1 has DMs disabled. Use a whitelisted channel in your server.",
            allowed_mentions=NO_MENTIONS,
        )
        return False

    async def _resolve_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel],
    ) -> Optional[discord.TextChannel]:
        if channel is not None:
            return channel
        if isinstance(ctx.channel, discord.TextChannel):
            return ctx.channel
        await ctx.send("Please specify a server text channel.", allowed_mentions=NO_MENTIONS)
        return None

    async def _is_channel_whitelisted(self, guild: discord.Guild, channel_id: int) -> bool:
        channels = await self.config.guild(guild).whitelisted_channels()
        return channel_id in channels

    async def _has_active_followup(
        self,
        guild_id: int,
        channel_id: int,
        settings: dict,
    ) -> bool:
        window = int(settings["followup_window_seconds"])
        if window <= 0:
            return False
        last_followup = await self.config.custom(CUSTOM_CHANNEL_HISTORY, str(guild_id), str(channel_id)).last_followup()
        return bool(last_followup and time.time() - float(last_followup) <= window)

    async def _format_whitelisted_channels(self, guild: discord.Guild) -> str:
        channel_ids = await self.config.guild(guild).whitelisted_channels()
        if not channel_ids:
            return "No channels are whitelisted yet."

        mentions: list[str] = []
        missing: list[str] = []
        for channel_id in channel_ids:
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                missing.append(f"`{channel_id}`")
            else:
                mentions.append(channel.mention)

        parts = mentions + missing
        return ", ".join(parts) if parts else "No channels are whitelisted yet."

    async def _send_text(self, destination: discord.abc.Messageable, text: str) -> None:
        for chunk in split_discord_messages(text):
            await destination.send(chunk, allowed_mentions=NO_MENTIONS)

    async def _track_history_channel(self, guild: discord.Guild, channel_id: int) -> None:
        guild_conf = self.config.guild(guild)
        channel_ids = await guild_conf.history_channels()
        if channel_id not in channel_ids:
            channel_ids.append(channel_id)
            await guild_conf.history_channels.set(channel_ids)

    async def _clear_channel_history(self, guild_id: int, channel_id: int) -> None:
        channel_conf = self.config.custom(CUSTOM_CHANNEL_HISTORY, str(guild_id), str(channel_id))
        await channel_conf.history.set([])
        await channel_conf.last_followup.set(0.0)

    def _get_lock(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (guild_id, channel_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _mentions_bot(self, message: discord.Message) -> bool:
        return bool(self.bot.user and self.bot.user.id in message.raw_mentions)

    def _strip_bot_mentions(self, content: str) -> str:
        if not self.bot.user:
            return content
        pattern = rf"<@!?{re.escape(str(self.bot.user.id))}>"
        return re.sub(pattern, "", content)

    async def _is_command_message(self, message: discord.Message) -> bool:
        try:
            context = await self.bot.get_context(message)
        except Exception:
            return False
        return bool(getattr(context, "valid", False))
