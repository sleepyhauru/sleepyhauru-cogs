import discord
from discord.ui import Select, View
from redbot.core import commands
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

    TARGET_COGS = [
        "EmojiSteal",
        "Deepfry",
        "AddImage",
        "Birthday",
    ]

    INCLUDED_COMMANDS = {
        "EmojiSteal": {
            "getemoji",
            "steal",
            "steal upload",
            "uploadsticker",
        },
        "Deepfry": {
            "deepfry",
            "nuke",
        },
        "AddImage": {
            "addimage add",
            "addimage delete",
            "addimage list",
        },
        "Birthday": {
            "birthday",
            "birthday set",
            "birthday remove",
            "birthday upcoming",
        },
    }

    def __init__(self, bot):
        self.bot = bot

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

    def _is_included(self, cog_name: str, command: commands.Command) -> bool:
        allowed = self.INCLUDED_COMMANDS.get(cog_name, set())
        return command.qualified_name in allowed

    def _build_cog_lines(self, prefix: str, cog_name: str) -> List[str]:
        lines: List[str] = []
        seen: Set[str] = set()

        root_commands = self._visible_root_commands_for_cog(cog_name)

        for root in root_commands:
            all_commands = [root] + self._walk_visible_subcommands(root)

            for cmd in all_commands:
                if cmd.qualified_name in seen:
                    continue
                if not self._is_included(cog_name, cmd):
                    continue

                seen.add(cmd.qualified_name)
                lines.append(self._format_command_line(prefix, cmd))

        return lines

    def _available_cogs(self, prefix: str) -> List[str]:
        return [
            cog_name for cog_name in self.TARGET_COGS
            if self._build_cog_lines(prefix, cog_name)
        ]

    def build_home_embed(self, prefix: str) -> discord.Embed:
        lines = []

        for cog_name in self._available_cogs(prefix):
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
        available_cogs = self._available_cogs(prefix)
        embed = self.build_home_embed(prefix)

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
