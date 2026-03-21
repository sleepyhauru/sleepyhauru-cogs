import discord
from discord.ui import Select, View
from redbot.core import Config, commands
from typing import List, Optional, Set


class CommandsMenuSelect(Select):
    def __init__(self, cog_names: List[str]):
        options = [
            discord.SelectOption(label=cog_name, value=cog_name)
            for cog_name in cog_names
        ]
        super().__init__(
            placeholder="Select a cog...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if view is None or not isinstance(view, CommandsMenuView):
            return

        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                "You can't use this menu.",
                ephemeral=True,
            )
            return

        cog_name = self.values[0]
        embed = view.cog.build_cog_embed(view.prefix, cog_name)
        await interaction.response.edit_message(embed=embed, view=view)


class CommandsMenuView(View):
    def __init__(self, cog, author_id: int, prefix: str, cog_names: List[str]):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.prefix = prefix
        self.message: Optional[discord.Message] = None
        self.add_item(CommandsMenuSelect(cog_names))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Commands(commands.Cog):
    """Embedded command list for selected cogs."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=183040001, force_registration=True)
        self.config.register_global(
            allowlist=[],
            excluded_cogs=["Alias", "Audio", "Commands", "Core", "Dev", "Downloader", "Help"],
        )

    def _visible_root_commands_for_cog(self, cog_name: str) -> List[commands.Command]:
        cmds = []
        for cmd in self.bot.commands:
            if getattr(cmd, "cog_name", None) != cog_name:
                continue
            if cmd.hidden:
                continue
            if cmd.parent is not None:
                continue
            cmds.append(cmd)
        return sorted(cmds, key=lambda c: c.name.lower())

    def _walk_visible_subcommands(self, command: commands.Command) -> List[commands.Command]:
        found = []
        if isinstance(command, commands.Group):
            for sub in sorted(command.commands, key=lambda c: c.name.lower()):
                if sub.hidden:
                    continue
                found.append(sub)
                if isinstance(sub, commands.Group):
                    found.extend(self._walk_visible_subcommands(sub))
        return found

    def _command_usage(self, prefix: str, command: commands.Command) -> str:
        base = f"{prefix}{command.qualified_name}"
        if command.signature:
            return f"{base} {command.signature}"
        return base

    def _command_description(self, command: commands.Command) -> str:
        text = command.short_doc or command.help or ""
        return " ".join(text.split())

    def _format_command_line(self, prefix: str, command: commands.Command) -> str:
        usage = self._command_usage(prefix, command)
        desc = self._command_description(command)
        if desc:
            return f"**`{usage}`** — {desc}"
        return f"**`{usage}`**"

    def _build_cog_lines(self, prefix: str, cog_name: str) -> List[str]:
        lines: List[str] = []
        seen: Set[str] = set()

        root_commands = self._visible_root_commands_for_cog(cog_name)

        for root in root_commands:
            all_commands = [root] + self._walk_visible_subcommands(root)

            for cmd in all_commands:
                if cmd.qualified_name in seen:
                    continue

                seen.add(cmd.qualified_name)
                lines.append(self._format_command_line(prefix, cmd))

        return lines

    async def _available_cogs(self, prefix: str) -> List[str]:
        allowlist = await self.config.allowlist()
        excluded = set(await self.config.excluded_cogs())

        if allowlist:
            names = [cog_name for cog_name in allowlist if self._build_cog_lines(prefix, cog_name)]
            return names

        names = []
        for cog_name in sorted({getattr(cmd, "cog_name", None) for cmd in self.bot.commands if getattr(cmd, "cog_name", None)}):
            if cog_name in excluded:
                continue
            if self._build_cog_lines(prefix, cog_name):
                names.append(cog_name)
        return names

    def build_home_embed(self, prefix: str, cog_names: List[str]) -> discord.Embed:
        lines = []

        for cog_name in cog_names:
            command_count = len(self._build_cog_lines(prefix, cog_name))
            lines.append(f"**{cog_name}** — {command_count} command{'s' if command_count != 1 else ''}")

        description = (
            f"Use `{prefix}help <command>` for detailed help.\n\n"
            f"Select a category from the dropdown below.\n\n"
            f"{chr(10).join(lines) if lines else 'No command categories available.'}"
        )

        embed = discord.Embed(
            title="Bot Commands",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Choose a cog from the dropdown menu.")
        return embed

    def build_cog_embed(self, prefix: str, cog_name: str) -> discord.Embed:
        lines = self._build_cog_lines(prefix, cog_name)

        if not lines:
            description = "No commands detected."
        else:
            description = "\n".join(lines)

        if len(description) > 4096:
            trimmed_lines = []
            total_len = 0
            for line in lines:
                add_len = len(line) + 1
                if total_len + add_len > 4000:
                    trimmed_lines.append("...")
                    break
                trimmed_lines.append(line)
                total_len += add_len
            description = "\n".join(trimmed_lines)

        embed = discord.Embed(
            title=cog_name,
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Use {prefix}help <command> for detailed help.")
        return embed

    @commands.command(name="commands", aliases=["cmds", "helpmenu", "clanhelp"])
    async def commands_menu(self, ctx: commands.Context):
        """Show the command list."""
        prefix = ctx.clean_prefix
        available_cogs = await self._available_cogs(prefix)
        embed = self.build_home_embed(prefix, available_cogs)

        if not available_cogs:
            await ctx.send(embed=embed)
            return

        view = CommandsMenuView(
            cog=self,
            author_id=ctx.author.id,
            prefix=prefix,
            cog_names=available_cogs,
        )
        view.message = await ctx.send(embed=embed, view=view)

    @commands.group(name="commandsset", invoke_without_command=True)
    @commands.is_owner()
    async def commandsset(self, ctx: commands.Context):
        """Configure which cogs appear in the commands menu."""
        await ctx.send_help()

    @commandsset.command(name="show")
    @commands.is_owner()
    async def commandsset_show(self, ctx: commands.Context):
        """Show current commands menu configuration."""
        allowlist = await self.config.allowlist()
        excluded = await self.config.excluded_cogs()

        lines = [
            f"Allowlist mode: {'enabled' if allowlist else 'auto-discover'}",
            f"Allowed cogs: {', '.join(allowlist) if allowlist else 'all discovered cogs'}",
            f"Excluded in auto-discover: {', '.join(excluded) if excluded else 'none'}",
        ]
        await ctx.send("```py\n" + "\n".join(lines) + "\n```")

    @commandsset.command(name="allow")
    @commands.is_owner()
    async def commandsset_allow(self, ctx: commands.Context, *, cog_name: str):
        """Add a cog to the explicit allowlist."""
        allowlist = await self.config.allowlist()
        if cog_name in allowlist:
            await ctx.send(f"`{cog_name}` is already in the allowlist.")
            return
        allowlist.append(cog_name)
        await self.config.allowlist.set(allowlist)
        await ctx.send(f"Added `{cog_name}` to the allowlist.")

    @commandsset.command(name="deny")
    @commands.is_owner()
    async def commandsset_deny(self, ctx: commands.Context, *, cog_name: str):
        """Exclude a cog from auto-discovery mode."""
        excluded = await self.config.excluded_cogs()
        if cog_name in excluded:
            await ctx.send(f"`{cog_name}` is already excluded.")
            return
        excluded.append(cog_name)
        await self.config.excluded_cogs.set(excluded)
        await ctx.send(f"Excluded `{cog_name}` from auto-discovery.")

    @commandsset.command(name="remove")
    @commands.is_owner()
    async def commandsset_remove(self, ctx: commands.Context, *, cog_name: str):
        """Remove a cog from the allowlist and exclusion list."""
        allowlist = await self.config.allowlist()
        excluded = await self.config.excluded_cogs()
        changed = False

        if cog_name in allowlist:
            allowlist.remove(cog_name)
            await self.config.allowlist.set(allowlist)
            changed = True

        if cog_name in excluded:
            excluded.remove(cog_name)
            await self.config.excluded_cogs.set(excluded)
            changed = True

        if not changed:
            await ctx.send(f"`{cog_name}` was not configured.")
            return
        await ctx.send(f"Removed `{cog_name}` from commands menu overrides.")

    @commandsset.command(name="reset")
    @commands.is_owner()
    async def commandsset_reset(self, ctx: commands.Context):
        """Reset commands menu config back to auto-discovery defaults."""
        await self.config.allowlist.set([])
        await self.config.excluded_cogs.set(["Alias", "Audio", "Commands", "Core", "Dev", "Downloader", "Help"])
        await ctx.send("Reset commands menu config to auto-discovery defaults.")
