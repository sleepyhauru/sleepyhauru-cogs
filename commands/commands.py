import discord
from redbot.core import commands


class CustomCommandsHelp(commands.Cog):
    """Custom embedded command list for selected cogs."""

    def __init__(self, bot):
        self.bot = bot

    def build_embed(self, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="Bot Commands",
            description=(
                f"Use `{prefix}help <command>` for more detail.\n\n"
                "**Permission Markers**\n"
                "🔓 Everyone\n"
                "🛡️ Mod / Manage Channels\n"
                "👑 Admin / Manage Server\n"
                "🏠 Guild Owner\n"
                "⚙️ Bot Owner\n"
                "😀 Manage Emojis\n"
                "🤖 Bot perms required"
            ),
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="EmojiSteal",
            value=(
                f"🔓 `{prefix}steal` / `{prefix}emojisteal` — Get emoji/sticker URLs from a replied message\n"
                f"😀🤖 `{prefix}steal upload [names...]` — Upload replied emojis/stickers to this server\n"
                f"🔓 `{prefix}getemoji <emoji|emoji_id>` — Get emoji image URL\n"
                f"😀🤖 `{prefix}uploadsticker [name]` — Upload an attached sticker file\n"
                "🔓 Context menus: `Steal Emotes`\n"
                "😀🤖 Context menus: `Steal+Upload Emotes`"
            ),
            inline=False,
        )

        embed.add_field(
            name="Deepfry",
            value=(
                f"🤖 `{prefix}deepfry [member|image_url]` — Deepfry an image\n"
                f"🤖 `{prefix}df [member|image_url]` — Alias for deepfry\n"
                f"🤖 `{prefix}nuke [member|image_url]` — More destructive image effect\n"
                f"🏠 `{prefix}deepfryset` — View deepfry config\n"
                f"🏠 `{prefix}deepfryset frychance <value>` — Set auto-deepfry chance\n"
                f"🏠 `{prefix}deepfryset nukechance <value>` — Set auto-nuke chance\n"
                f"🏠 `{prefix}deepfryset allowalltypes <true|false>` — Allow unverified file types\n"
                f"🏠 `{prefix}deepfryset replyonly <true|false>` — Require reply/direct input only\n"
                f"🏠 `{prefix}deepfryset debug <true|false>` — Toggle debug mode"
            ),
            inline=False,
        )

        embed.add_field(
            name="AddImage",
            value=(
                f"🛡️🤖 `{prefix}addimage add <name>` — Add a server image trigger\n"
                f"🔓🤖 `{prefix}addimage list [guild|global]` — List saved image triggers\n"
                f"🛡️ `{prefix}addimage ignoreglobal` — Toggle global image triggers in this server\n"
                f"🛡️ `{prefix}addimage clear_images` — Remove all server image triggers\n"
                f"🛡️ `{prefix}addimage clean_deleted_images` — Clean missing/deleted image files\n"
                f"🛡️ `{prefix}addimage delete <name>` — Delete a server image trigger\n"
                f"⚙️ `{prefix}addimage deleteglobal <name>` — Delete a global image trigger\n"
                f"⚙️ `{prefix}addimage clear_global` — Clear all global image triggers\n"
                f"⚙️ `{prefix}addimage deleteallbyuser <user_id>` — Delete all triggers by user ID\n"
                f"Aliases for delete: `remove`, `rem`, `del`\n"
                f"Aliases for deleteglobal: `dg`, `delglobal`"
            ),
            inline=False,
        )

        embed.add_field(
            name="Birthday",
            value=(
                f"🔓 `{prefix}birthday set <date>` / `{prefix}bday set <date>` — Set your birthday\n"
                f"🔓 `{prefix}birthday remove` — Remove your birthday\n"
                f"🔓 `{prefix}birthday upcoming [days]` — Show upcoming birthdays\n"
                f"👑 `{prefix}bdset` — Birthday admin settings\n"
                f"👑🤖 `{prefix}bdset interactive` — Run setup flow\n"
                f"👑 `{prefix}bdset settings` — View current birthday settings\n"
                f"👑 `{prefix}bdset time <time>` — Set birthday announcement time\n"
                f"👑 `{prefix}bdset msgwithoutyear <message>` — Set no-year birthday message\n"
                f"👑 `{prefix}bdset msgwithyear <message>` — Set with-year birthday message\n"
                f"👑 `{prefix}bdset forceset <user> <date>` — Force-set a user birthday\n"
                f"👑 `{prefix}bdset forceremove <user>` — Force-remove a user birthday\n"
                f"⚙️ `{prefix}birthdaydebug upcoming` — Hidden owner debug command"
            ),
            inline=False,
        )

        embed.set_footer(text="Custom command menu")
        return embed

    @commands.command(name="commands", aliases=["cmds", "helpmenu", "clanhelp"])
    async def commands_menu(self, ctx: commands.Context):
        """Show a custom embedded command list."""
        embed = self.build_embed(ctx.clean_prefix)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(CustomCommandsHelp(bot))