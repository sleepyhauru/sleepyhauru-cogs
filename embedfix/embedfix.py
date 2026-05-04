import inspect
import re
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import discord
from discord.ui import Select, View
from redbot.core import Config, commands


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", re.IGNORECASE)
RULE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:"
MAX_LINKS_LIMIT = 10
SUPPRESS_NOTICE_COOLDOWN_SECONDS = 600
LEGACY_INSTAGRAM_TARGET_HOSTS = {"ddinstagram.com"}

DEFAULT_RULES = (
    {
        "name": "x",
        "enabled": True,
        "source_hosts": ["x.com", "twitter.com"],
        "target_host": "fixupx.com",
    },
    {
        "name": "instagram",
        "enabled": True,
        "source_hosts": ["instagram.com"],
        "target_host": "vxinstagram.com",
    },
    {
        "name": "tiktok",
        "enabled": True,
        "source_hosts": ["tiktok.com"],
        "target_host": "vxtiktok.com",
    },
    {
        "name": "reddit",
        "enabled": True,
        "source_hosts": ["reddit.com", "redd.it"],
        "target_host": "rxddit.com",
    },
    {
        "name": "bluesky",
        "enabled": True,
        "source_hosts": ["bsky.app"],
        "target_host": "fxbsky.app",
    },
)


class EmbedFixPanelSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Overview", value="overview"),
            discord.SelectOption(label="Rules", value="rules"),
            discord.SelectOption(label="Stats", value="stats"),
            discord.SelectOption(label="Toggle enabled", value="toggle_enabled"),
            discord.SelectOption(label="Toggle suppression", value="toggle_suppression"),
            discord.SelectOption(label="Reset rules", value="reset_rules"),
        ]
        super().__init__(
            placeholder="EmbedFix settings...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if view is None or not isinstance(view, EmbedFixSettingsView):
            return

        if not view.user_can_interact(interaction):
            await interaction.response.send_message(
                "You can't use this menu.",
                ephemeral=True,
            )
            return

        action = self.values[0]
        if action == "overview":
            embed = await view.cog.build_settings_embed(view.guild, view.prefix)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if action == "rules":
            embed = await view.cog.build_rules_embed(view.guild, view.prefix)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if action == "stats":
            embed = await view.cog.build_stats_embed(view.guild)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if action == "toggle_enabled":
            conf = view.cog.config.guild(view.guild)
            enabled = not await conf.enabled()
            await conf.enabled.set(enabled)
            embed = await view.cog.build_settings_embed(view.guild, view.prefix)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if action == "toggle_suppression":
            conf = view.cog.config.guild(view.guild)
            suppress_original = not await conf.suppress_original()
            await conf.suppress_original.set(suppress_original)
            embed = await view.cog.build_settings_embed(view.guild, view.prefix)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if action == "reset_rules":
            await view.cog.config.guild(view.guild).rules.set(view.cog._default_rules())
            rules = await view.cog._get_rules(view.guild)
            new_view = EmbedFixSettingsView(
                view.cog,
                view.author_id,
                view.guild,
                view.prefix,
                rules,
            )
            new_view.message = getattr(interaction, "message", view.message)
            embed = await view.cog.build_rules_embed(view.guild, view.prefix)
            await interaction.response.edit_message(embed=embed, view=new_view)


class EmbedFixRuleSelect(Select):
    def __init__(self, rules: list[dict]):
        options = [
            discord.SelectOption(
                label=rule.get("name", "unnamed")[:100],
                value=rule.get("name", ""),
            )
            for rule in rules[:25]
            if rule.get("name")
        ]
        super().__init__(
            placeholder="Show rule details...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if view is None or not isinstance(view, EmbedFixSettingsView):
            return

        if not view.user_can_interact(interaction):
            await interaction.response.send_message(
                "You can't use this menu.",
                ephemeral=True,
            )
            return

        rule_name = self.values[0]
        rules = await view.cog._get_rules(view.guild)
        rule = next((rule for rule in rules if rule.get("name") == rule_name), None)
        if rule is None:
            embed = await view.cog.build_rules_embed(view.guild, view.prefix)
        else:
            embed = view.cog.build_rule_detail_embed(rule, view.prefix)
        await interaction.response.edit_message(embed=embed, view=view)


class EmbedFixSettingsView(View):
    def __init__(self, cog, author_id: Optional[int], guild, prefix: str, rules: list[dict]):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.guild = guild
        self.prefix = prefix
        self.message: Optional[discord.Message] = None
        self.add_item(EmbedFixPanelSelect())
        if any(rule.get("name") for rule in rules):
            self.add_item(EmbedFixRuleSelect(rules))

    def user_can_interact(self, interaction: discord.Interaction) -> bool:
        if self.author_id is None:
            return True
        return getattr(getattr(interaction, "user", None), "id", None) == self.author_id

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class EmbedFix(commands.Cog):
    """Repost social URLs through embed-friendly fixer domains."""

    __author__ = "sleepyhauru"
    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot
        self.last_suppress_notice_at = {}
        self.config = Config.get_conf(self, identifier=713902406551, force_registration=True)
        self.config.register_guild(
            enabled=False,
            suppress_original=True,
            max_links=3,
            rules=self._default_rules(),
            detection_count=0,
            repost_count=0,
            suppressed_count=0,
            send_error_count=0,
            suppress_error_count=0,
        )

    def format_help_for_context(self, ctx: commands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, **kwargs):
        return

    @staticmethod
    def _prefix(ctx: commands.Context) -> str:
        return getattr(ctx, "clean_prefix", "[p]")

    @staticmethod
    def _message_text(message: discord.Message) -> str:
        return getattr(message, "content", "") or ""

    def _now(self) -> float:
        return time.monotonic()

    def _channel_id(self, channel) -> int:
        return getattr(channel, "id", id(channel))

    @staticmethod
    def _default_rules() -> list[dict]:
        return [
            {
                "name": rule["name"],
                "enabled": rule["enabled"],
                "source_hosts": list(rule["source_hosts"]),
                "target_host": rule["target_host"],
            }
            for rule in DEFAULT_RULES
        ]

    @staticmethod
    def _migrate_rules(rules: list[dict]) -> tuple[list[dict], bool]:
        migrated_rules = []
        changed = False

        for rule in rules:
            if not isinstance(rule, dict):
                migrated_rules.append(rule)
                continue

            migrated_rule = dict(rule)
            source_hosts = migrated_rule.get("source_hosts")
            target_host = migrated_rule.get("target_host")
            if (
                migrated_rule.get("name") == "instagram"
                and source_hosts == ["instagram.com"]
                and target_host in LEGACY_INSTAGRAM_TARGET_HOSTS
            ):
                migrated_rule["target_host"] = "vxinstagram.com"
                changed = True
            migrated_rules.append(migrated_rule)

        return migrated_rules, changed

    @staticmethod
    def _normalize_rule_name(name: str) -> str:
        name = name.strip().lower()
        if not RULE_NAME_RE.fullmatch(name):
            raise ValueError("rule names must be 1-32 characters: letters, numbers, `_`, or `-`")
        return name

    @staticmethod
    def _normalize_host(value: str) -> str:
        candidate = value.strip().lower()
        if not candidate:
            raise ValueError("host cannot be empty")

        if "://" not in candidate:
            candidate = f"https://{candidate}"

        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").strip(".").lower()
        if not host or not HOST_RE.fullmatch(host) or "." not in host:
            raise ValueError(f"`{value}` is not a valid host")
        return host

    @classmethod
    def _build_rule(cls, name: str, target_host: str, source_hosts: tuple[str, ...]) -> dict:
        if not source_hosts:
            raise ValueError("at least one source host is required")

        normalized_sources = []
        seen = set()
        for source_host in source_hosts:
            source = cls._normalize_host(source_host)
            if source not in seen:
                normalized_sources.append(source)
                seen.add(source)

        return {
            "name": cls._normalize_rule_name(name),
            "enabled": True,
            "source_hosts": normalized_sources,
            "target_host": cls._normalize_host(target_host),
        }

    @staticmethod
    def _host_matches(host: str, source_host: str) -> bool:
        return host == source_host or host.endswith(f".{source_host}")

    @classmethod
    def _trim_url(cls, url: str) -> str:
        while url and url[-1] in TRAILING_PUNCTUATION:
            url = url[:-1]
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        while url.endswith("]") and url.count("]") > url.count("["):
            url = url[:-1]
        return url

    @classmethod
    def _extract_urls(cls, text: str) -> list[str]:
        urls = []
        for match in URL_RE.finditer(text):
            start, end = match.span()
            if start > 0 and text[start - 1] == "<" and end < len(text) and text[end] == ">":
                continue
            url = cls._trim_url(match.group(0))
            if url:
                urls.append(url)
        return urls

    @classmethod
    def _rewrite_url(cls, url: str, rules: list[dict]) -> Optional[str]:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None

        if parsed.scheme.lower() not in {"http", "https"}:
            return None

        host = (parsed.hostname or "").strip(".").lower()
        if not host:
            return None

        for rule in rules:
            if not rule.get("enabled", True):
                continue

            source_hosts = rule.get("source_hosts", [])
            target_host = rule.get("target_host")
            if not isinstance(source_hosts, list) or not isinstance(target_host, str):
                continue

            if not any(
                isinstance(source_host, str) and cls._host_matches(host, source_host)
                for source_host in source_hosts
            ):
                continue

            target_host = target_host.strip().lower()
            if host == target_host or host.endswith(f".{target_host}"):
                return None

            return urlunsplit(("https", target_host, parsed.path, parsed.query, parsed.fragment))

        return None

    @classmethod
    def _fixed_urls_for_message(cls, text: str, rules: list[dict], max_links: int) -> list[str]:
        fixed_urls = []
        seen = set()

        for url in cls._extract_urls(text):
            fixed_url = cls._rewrite_url(url, rules)
            if not fixed_url or fixed_url in seen:
                continue

            fixed_urls.append(fixed_url)
            seen.add(fixed_url)
            if len(fixed_urls) >= max_links:
                break

        return fixed_urls

    async def _is_disabled_in_guild(self, guild) -> bool:
        result = self.bot.cog_disabled_in_guild(self, guild)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _increment_guild_counter(self, guild, key: str):
        conf_value = getattr(self.config.guild(guild), key)
        current = await conf_value()
        await conf_value.set(current + 1)

    async def _get_rules(self, guild) -> list[dict]:
        rules = await self.config.guild(guild).rules()
        if not isinstance(rules, list):
            rules = self._default_rules()
            await self.config.guild(guild).rules.set(rules)
            return rules

        rules, changed = self._migrate_rules(rules)
        if changed:
            await self.config.guild(guild).rules.set(rules)
        return rules

    def _should_send_suppress_notice(self, channel) -> bool:
        channel_id = self._channel_id(channel)
        last_notice_at = self.last_suppress_notice_at.get(channel_id)
        now = self._now()
        if (
            last_notice_at is not None
            and now - last_notice_at < SUPPRESS_NOTICE_COOLDOWN_SECONDS
        ):
            return False

        self.last_suppress_notice_at[channel_id] = now
        return True

    async def _send_suppress_failure_notice(self, message: discord.Message):
        channel = getattr(message, "channel", None)
        if channel is None or not self._should_send_suppress_notice(channel):
            return

        try:
            await channel.send(
                "EmbedFix reposted the fixed link, but Discord would not let me "
                "suppress the original embed. Give the bot `Manage Messages` in "
                "this channel, then try again.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            return

    def _rules_message(self, rules: list[dict]) -> str:
        lines = ["EmbedFix rules"]
        for rule in rules:
            name = rule.get("name", "unnamed")
            enabled = "on" if rule.get("enabled", True) else "off"
            target_host = rule.get("target_host", "")
            source_hosts = rule.get("source_hosts", [])
            if not isinstance(source_hosts, list):
                source_hosts = []
            sources = ", ".join(f"`{source_host}`" for source_host in source_hosts) or "`none`"
            lines.append(f"`{name}` [{enabled}]: {sources} -> `{target_host}`")

        message = "\n".join(lines)
        if len(message) <= 2000:
            return message
        return message[:1990] + "\n(truncated)"

    async def build_settings_embed(self, guild, prefix: str) -> discord.Embed:
        conf = self.config.guild(guild)
        enabled = await conf.enabled()
        suppress_original = await conf.suppress_original()
        max_links = await conf.max_links()
        rules = await self._get_rules(guild)
        enabled_rules = sum(1 for rule in rules if rule.get("enabled", True))
        next_step = (
            f"Use `{prefix}embedfix enable` or choose `Toggle enabled` below."
            if not enabled
            else (
                f"Use `{prefix}embedfix addrule <name> <target_host> "
                "<source_hosts...>` to add domains."
            )
        )
        embed = discord.Embed(
            title="EmbedFix Settings",
            description=(
                "Automatically reposts supported social links through embed-friendly "
                "domains, then suppresses previews on the original message."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Enabled", value=f"`{enabled}`")
        embed.add_field(name="Original embed suppression", value=f"`{suppress_original}`")
        embed.add_field(name="Max fixed links per message", value=f"`{max_links}`")
        embed.add_field(name="Rules", value=f"`{enabled_rules}/{len(rules)}` enabled")
        embed.add_field(name="Next step", value=next_step)
        embed.set_footer(text="Use the dropdowns below to view details or toggle common settings.")
        return embed

    async def build_rules_embed(self, guild, prefix: str) -> discord.Embed:
        rules = await self._get_rules(guild)
        lines = []
        for rule in rules:
            name = rule.get("name", "unnamed")
            state = "on" if rule.get("enabled", True) else "off"
            target_host = rule.get("target_host", "")
            source_hosts = rule.get("source_hosts", [])
            if not isinstance(source_hosts, list):
                source_hosts = []
            sources = ", ".join(source_hosts) or "none"
            lines.append(f"**{name}** [{state}]\n`{sources}` -> `{target_host}`")

        description = "\n\n".join(lines) if lines else "No rules configured."
        if len(description) > 4096:
            description = description[:4000] + "\n..."

        embed = discord.Embed(
            title="EmbedFix Rules",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"Add or replace rules with {prefix}embedfix addrule <name> "
                "<target_host> <source_hosts...>."
            )
        )
        return embed

    def build_rule_detail_embed(self, rule: dict, prefix: str) -> discord.Embed:
        name = rule.get("name", "unnamed")
        source_hosts = rule.get("source_hosts", [])
        if not isinstance(source_hosts, list):
            source_hosts = []
        sources = "\n".join(f"`{source_host}`" for source_host in source_hosts) or "`none`"
        enabled = rule.get("enabled", True)
        target_host = rule.get("target_host", "")
        embed = discord.Embed(
            title=f"EmbedFix Rule: {name}",
            description="Selected rule details.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Enabled", value=f"`{enabled}`")
        embed.add_field(name="Source hosts", value=sources)
        embed.add_field(name="Target host", value=f"`{target_host}`")
        embed.add_field(
            name="Commands",
            value=(
                f"`{prefix}embedfix enablerule {name}`\n"
                f"`{prefix}embedfix disablerule {name}`\n"
                f"`{prefix}embedfix removerule {name}`"
            ),
        )
        embed.set_footer(text="Use the rules dropdown to inspect another rule.")
        return embed

    async def build_stats_embed(self, guild) -> discord.Embed:
        conf = self.config.guild(guild)
        embed = discord.Embed(
            title="EmbedFix Stats",
            description="Aggregate activity for this guild.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Messages detected", value=f"`{await conf.detection_count()}`")
        embed.add_field(name="Reposts sent", value=f"`{await conf.repost_count()}`")
        embed.add_field(
            name="Original embeds suppressed",
            value=f"`{await conf.suppressed_count()}`",
        )
        embed.add_field(name="Send errors", value=f"`{await conf.send_error_count()}`")
        embed.add_field(name="Suppress errors", value=f"`{await conf.suppress_error_count()}`")
        embed.set_footer(text="Suppress errors usually mean the bot lacks Manage Messages.")
        return embed

    async def _settings_message(self, guild, prefix: str) -> str:
        conf = self.config.guild(guild)
        enabled = await conf.enabled()
        suppress_original = await conf.suppress_original()
        max_links = await conf.max_links()
        rules = await self._get_rules(guild)
        next_step = (
            f"Next: run `{prefix}embedfix enable`."
            if not enabled
            else (
                f"Next: review `{prefix}embedfix rules` or add a rule with "
                f"`{prefix}embedfix addrule <name> <target_host> <source_hosts...>`."
            )
        )
        return (
            "EmbedFix settings\n"
            f"Enabled: `{enabled}`\n"
            f"Suppress original embeds: `{suppress_original}`\n"
            f"Max fixed links per message: `{max_links}`\n"
            f"Rules configured: `{len(rules)}`\n"
            f"{next_step}"
        )

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if getattr(getattr(message, "author", None), "bot", False):
            return

        guild = getattr(message, "guild", None)
        if guild is None:
            return

        if await self._is_disabled_in_guild(guild):
            return

        conf = self.config.guild(guild)
        if not await conf.enabled():
            return

        max_links = max(1, min(MAX_LINKS_LIMIT, int(await conf.max_links())))
        fixed_urls = self._fixed_urls_for_message(
            self._message_text(message),
            await self._get_rules(guild),
            max_links,
        )
        if not fixed_urls:
            return

        await self._increment_guild_counter(guild, "detection_count")

        try:
            await message.channel.send(
                "\n".join(fixed_urls),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await self._increment_guild_counter(guild, "send_error_count")
            return

        await self._increment_guild_counter(guild, "repost_count")

        if not await conf.suppress_original():
            return

        try:
            await message.edit(suppress=True)
        except discord.NotFound:
            await self._increment_guild_counter(guild, "suppress_error_count")
            return
        except (discord.Forbidden, discord.HTTPException):
            await self._increment_guild_counter(guild, "suppress_error_count")
            await self._send_suppress_failure_notice(message)
            return

        await self._increment_guild_counter(guild, "suppressed_count")

    @commands.group(name="embedfix", aliases=["embedfixset"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def embedfixset(self, ctx: commands.Context):
        """Open the EmbedFix settings panel for this guild."""
        assert ctx.guild
        prefix = self._prefix(ctx)
        rules = await self._get_rules(ctx.guild)
        author_id = getattr(getattr(ctx, "author", None), "id", None)
        view = EmbedFixSettingsView(self, author_id, ctx.guild, prefix, rules)
        view.message = await ctx.send(
            embed=await self.build_settings_embed(ctx.guild, prefix),
            view=view,
        )

    @embedfixset.command(name="show")
    async def embedfixset_show(self, ctx: commands.Context):
        """Show current EmbedFix settings as text."""
        assert ctx.guild
        await ctx.send(await self._settings_message(ctx.guild, self._prefix(ctx)))

    @embedfixset.command(name="enable")
    async def embedfixset_enable(self, ctx: commands.Context):
        """Enable automatic embed fixing in this guild."""
        assert ctx.guild
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("EmbedFix enabled.")

    @embedfixset.command(name="disable")
    async def embedfixset_disable(self, ctx: commands.Context):
        """Disable automatic embed fixing in this guild."""
        assert ctx.guild
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("EmbedFix disabled.")

    @embedfixset.command(name="suppress")
    async def embedfixset_suppress(self, ctx: commands.Context, enabled: bool):
        """Set whether original message embeds should be suppressed after reposting."""
        assert ctx.guild
        await self.config.guild(ctx.guild).suppress_original.set(enabled)
        await ctx.send(f"Original embed suppression set to `{enabled}`.")

    @embedfixset.command(name="maxlinks")
    async def embedfixset_maxlinks(self, ctx: commands.Context, amount: int):
        """Set the maximum fixed links reposted per message."""
        assert ctx.guild
        amount = max(1, min(MAX_LINKS_LIMIT, amount))
        await self.config.guild(ctx.guild).max_links.set(amount)
        await ctx.send(f"Max fixed links per message set to `{amount}`.")

    @embedfixset.command(name="rules")
    async def embedfixset_rules(self, ctx: commands.Context):
        """Show configured rewrite rules."""
        assert ctx.guild
        await ctx.send(self._rules_message(await self._get_rules(ctx.guild)))

    @embedfixset.command(name="addrule")
    async def embedfixset_addrule(
        self,
        ctx: commands.Context,
        name: str,
        target_host: str,
        *source_hosts: str,
    ):
        """Add or replace a rewrite rule."""
        assert ctx.guild
        try:
            new_rule = self._build_rule(name, target_host, source_hosts)
        except ValueError as exc:
            await ctx.send(f"Invalid rule: {exc}")
            return

        rules = await self._get_rules(ctx.guild)
        rules = [rule for rule in rules if rule.get("name") != new_rule["name"]]
        rules.append(new_rule)
        await self.config.guild(ctx.guild).rules.set(rules)
        sources = ", ".join(f"`{source}`" for source in new_rule["source_hosts"])
        await ctx.send(
            f"Rule `{new_rule['name']}` saved: {sources} -> `{new_rule['target_host']}`."
        )

    @embedfixset.command(name="removerule")
    async def embedfixset_removerule(self, ctx: commands.Context, name: str):
        """Remove a rewrite rule."""
        assert ctx.guild
        name = name.strip().lower()
        rules = await self._get_rules(ctx.guild)
        new_rules = [rule for rule in rules if rule.get("name") != name]
        if len(new_rules) == len(rules):
            await ctx.send(f"No rule named `{name}` exists.")
            return

        await self.config.guild(ctx.guild).rules.set(new_rules)
        await ctx.send(f"Rule `{name}` removed.")

    async def _set_rule_enabled(self, ctx: commands.Context, name: str, enabled: bool):
        assert ctx.guild
        name = name.strip().lower()
        rules = await self._get_rules(ctx.guild)
        changed = False
        for rule in rules:
            if rule.get("name") == name:
                rule["enabled"] = enabled
                changed = True
                break

        if not changed:
            await ctx.send(f"No rule named `{name}` exists.")
            return

        await self.config.guild(ctx.guild).rules.set(rules)
        status = "enabled" if enabled else "disabled"
        await ctx.send(f"Rule `{name}` {status}.")

    @embedfixset.command(name="enablerule")
    async def embedfixset_enablerule(self, ctx: commands.Context, name: str):
        """Enable a rewrite rule."""
        await self._set_rule_enabled(ctx, name, True)

    @embedfixset.command(name="disablerule")
    async def embedfixset_disablerule(self, ctx: commands.Context, name: str):
        """Disable a rewrite rule."""
        await self._set_rule_enabled(ctx, name, False)

    @embedfixset.command(name="resetrules")
    async def embedfixset_resetrules(self, ctx: commands.Context):
        """Reset rewrite rules to the built-in defaults."""
        assert ctx.guild
        await self.config.guild(ctx.guild).rules.set(self._default_rules())
        await ctx.send("EmbedFix rules reset to defaults.")

    @embedfixset.command(name="stats")
    async def embedfixset_stats(self, ctx: commands.Context):
        """Show EmbedFix response stats for this guild."""
        assert ctx.guild
        conf = self.config.guild(ctx.guild)
        message = (
            "EmbedFix stats\n"
            f"Messages detected: `{await conf.detection_count()}`\n"
            f"Reposts sent: `{await conf.repost_count()}`\n"
            f"Original embeds suppressed: `{await conf.suppressed_count()}`\n"
            f"Send errors: `{await conf.send_error_count()}`\n"
            f"Suppress errors: `{await conf.suppress_error_count()}`"
        )
        await ctx.send(message)
