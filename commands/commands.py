import discord
from redbot.core import commands
from typing import List, Set


class Commands(commands.Cog):
    """Embedded command list for selected cogs."""

    TARGET_COGS = [
        "EmojiSteal",
        "Deepfry",
        "AddImage",
        "Birthday",
    ]

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
        text = " ".join(text.split())

        if len(text) > 90:
            text = text[:87] + "..."

        return text

    def _format_command_line(self, prefix: str, command: commands.Command) -> str:
        usage = self._command_usage(prefix, command)
        desc = self._command_description(command)

        if desc:
            return f"**`{usage}`** — {desc}"

        return f"**`{usage}`**"

    def _build_cog_section(self, prefix: str, cog_name: str) -> str:
        lines: List[str] = []
        seen: Set[str] = set()

        root_commands = self._visible_root_commands_for_cog(cog_name)

        for root in root_commands:
            if root.qualified_name in seen:
                continue

            seen.add(root.qualified_name)
            lines.append(self._format_command_line(prefix, root))

            for sub in self._walk_visible_subcommands(root):
                if sub.qualified_name in seen:
                    continue

                seen.add(sub.qualified_name)
                lines.append(self._format_command_line(prefix, sub))

        text = "\n".join(lines)

        if not text:
            return "No commands detected."

        if len(text) > 1024:
            text = text[:1000] + "\n..."

        return text

    def build_embed(self, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="Bot Commands",
            description=f"Use `{prefix}help <command>` for detailed help.",
            color=discord.Color.blurple(),
        )

        for cog_name in self.TARGET_COGS:
            section = self._build_cog_section(prefix, cog_name)
            embed.add_field(
                name=cog_name,
                value=section,
                inline=False,
            )

        embed.set_footer(text="Command list")
        return embed

    @commands.command(name="commands", aliases=["cmds", "helpmenu", "clanhelp"])
    async def commands_menu(self, ctx: commands.Context):
        """Show the command list."""
        embed = self.build_embed(ctx.clean_prefix)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Commands(bot))