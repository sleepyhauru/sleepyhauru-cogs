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
            text = text[:87].rstrip() + "..."
        return text

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
            if root.qualified_name in seen:
                continue

            seen.add(root.qualified_name)
            lines.append(self._format_command_line(prefix, root))

            for sub in self._walk_visible_subcommands(root):
                if sub.qualified_name in seen:
                    continue
                seen.add(sub.qualified_name)
                lines.append(self._format_command_line(prefix, sub))

        return lines

    def _build_cog_embed(self, prefix: str, cog_name: str, index: int, total: int) -> discord.Embed:
        lines = self._build_cog_lines(prefix, cog_name)

        if not lines:
            description = "No commands detected."
        else:
            description = "\n".join(lines)

        # Discord embed description limit is 4096
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
            title=f"{cog_name}",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Use {prefix}help <command> for detailed help • Page {index}/{total}")
        return embed

    def build_embeds(self, prefix: str) -> List[discord.Embed]:
        embeds = []
        total = len(self.TARGET_COGS)

        for index, cog_name in enumerate(self.TARGET_COGS, start=1):
            embeds.append(self._build_cog_embed(prefix, cog_name, index, total))

        return embeds

    @commands.command(name="commands", aliases=["cmds", "helpmenu", "clanhelp"])
    async def commands_menu(self, ctx: commands.Context):
        """Show the command list."""
        embeds = self.build_embeds(ctx.clean_prefix)

        for embed in embeds:
            await ctx.send(embed=embed)