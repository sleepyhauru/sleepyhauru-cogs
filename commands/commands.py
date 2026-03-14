import discord
from redbot.core import commands
from typing import Dict, List, Set


class Commands(commands.Cog):
    """Embedded command list for selected cogs."""

    TARGET_COGS = [
        "EmojiSteal",
        "Deepfry",
        "AddImage",
        "Birthday",
    ]

    PERMISSION_MARKERS: Dict[str, str] = {
        "steal": "🔓",
        "steal upload": "😀🤖",
        "getemoji": "🔓",
        "uploadsticker": "😀🤖",

        "deepfry": "🤖",
        "df": "🤖",
        "nuke": "🤖",
        "deepfryset": "🏠",
        "deepfryset frychance": "🏠",
        "deepfryset nukechance": "🏠",

        "addimage": "🛡️",
        "addimage add": "🛡️🤖",
        "addimage list": "🔓🤖",
        "addimage delete": "🛡️",
        "addimage clear_images": "🛡️",
        "addimage clean_deleted_images": "🛡️",
        "addimage deleteglobal": "⚙️",
        "addimage clear_global": "⚙️",
        "addimage deleteallbyuser": "⚙️",

        "birthday": "🔓",
        "birthday set": "🔓",
        "birthday remove": "🔓",
        "birthday upcoming": "🔓",
        "bday": "🔓",

        "bdset": "👑",
        "bdset interactive": "👑🤖",
        "bdset settings": "👑",
        "bdset time": "👑",
    }

    def __init__(self, bot):
        self.bot = bot

    def _visible_root_commands_for_cog(self, cog_name: str) -> List[commands.Command]:
        cmds = []
        for cmd in self.bot.commands.values():
            if cmd.cog_name != cog_name:
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

    def _command_signature(self, prefix: str, command: commands.Command) -> str:
        full = f"{prefix}{command.qualified_name}"
        if command.signature:
            return f"`{full} {command.signature}`"
        return f"`{full}`"

    def _command_help_text(self, command: commands.Command) -> str:
        text = command.short_doc or command.help or "No description provided."
        text = " ".join(text.split())
        if len(text) > 110:
            text = text[:107].rstrip() + "..."
        return text

    def _permission_marker(self, command: commands.Command) -> str:
        qn = command.qualified_name.lower()
        if qn in self.PERMISSION_MARKERS:
            return self.PERMISSION_MARKERS[qn]

        root = command.root_parent.name.lower() if command.root_parent else command.name.lower()
        return self.PERMISSION_MARKERS.get(root, "🔓")

    def _format_command_line(self, prefix: str, command: commands.Command) -> str:
        marker = self._permission_marker(command)
        usage = self._command_signature(prefix, command)
        desc = self._command_help_text(command)
        return f"{marker} {usage} — {desc}"

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

        if len(text) > 1024:
            text = text[:1000] + "\n..."

        return text or "No commands detected."

    def build_embed(self, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="Bot Commands",
            description=(
                f"Use `{prefix}help <command>` for more detail.\n\n"
                "**Permission Markers**\n"
                "🔓 Everyone\n"
                "🛡️ Mod\n"
                "👑 Admin\n"
                "🏠 Guild Owner\n"
                "⚙️ Bot Owner\n"
                "😀 Manage Emojis\n"
                "🤖 Bot permissions required"
            ),
            color=discord.Color.blurple(),
        )

        for cog_name in self.TARGET_COGS:
            section = self._build_cog_section(prefix, cog_name)
            embed.add_field(name=cog_name, value=section, inline=False)

        embed.set_footer(text="Auto-detected commands")
        return embed

    @commands.command(name="commands", aliases=["cmds", "clanhelp", "helpmenu"])
    async def commands_menu(self, ctx: commands.Context):
        """Show the embedded command list."""
        embed = self.build_embed(ctx.clean_prefix)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Commands(bot))