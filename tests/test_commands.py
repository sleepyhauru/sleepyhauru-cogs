import types
import unittest

from tests.support import load_module


commands_module = load_module("commands.commands")
red_commands = load_module("redbot.core.commands")


class FakeCommand:
    def __init__(
        self,
        name,
        *,
        cog_name,
        qualified_name=None,
        signature="",
        short_doc="",
        help_text="",
        hidden=False,
        parent=None,
    ):
        self.name = name
        self.cog_name = cog_name
        self.qualified_name = qualified_name or name
        self.signature = signature
        self.short_doc = short_doc
        self.help = help_text
        self.hidden = hidden
        self.parent = parent


class FakeGroup(red_commands.Group):
    def __init__(self, name, *, cog_name, signature="", short_doc="", hidden=False, parent=None):
        super().__init__()
        self.name = name
        self.cog_name = cog_name
        self.qualified_name = name if parent is None else f"{parent.qualified_name} {name}"
        self.signature = signature
        self.short_doc = short_doc
        self.help = short_doc
        self.hidden = hidden
        self.parent = parent


class CommandsCogTest(unittest.TestCase):
    def setUp(self):
        self.alpha = FakeCommand(
            "alpha",
            cog_name="Utility",
            signature="<value>",
            short_doc="Alpha root command",
        )
        self.tools = FakeGroup(
            "tools",
            cog_name="Utility",
            short_doc="Tool commands",
        )
        self.tools.commands = [
            FakeCommand(
                "hidden",
                cog_name="Utility",
                qualified_name="tools hidden",
                short_doc="Should not show",
                hidden=True,
                parent=self.tools,
            ),
            FakeCommand(
                "zeta",
                cog_name="Utility",
                qualified_name="tools zeta",
                short_doc="Zeta subcommand",
                parent=self.tools,
            ),
            FakeCommand(
                "beta",
                cog_name="Utility",
                qualified_name="tools beta",
                short_doc="Beta subcommand",
                parent=self.tools,
            ),
        ]
        bot = types.SimpleNamespace(commands=[self.alpha, self.tools])
        self.cog = commands_module.Commands(bot)

    def test_build_cog_lines_includes_sorted_visible_commands(self):
        lines = self.cog._build_cog_lines("!", "Utility")

        self.assertEqual(
            lines,
            [
                "**`!alpha <value>`** \u2014 Alpha root command",
                "**`!tools`** \u2014 Tool commands",
                "**`!tools beta`** \u2014 Beta subcommand",
                "**`!tools zeta`** \u2014 Zeta subcommand",
            ],
        )

    def test_build_cog_embed_trims_long_output(self):
        self.cog._build_cog_lines = lambda prefix, cog_name: ["x" * 1200] * 5

        embed = self.cog.build_cog_embed("!", "Utility")

        self.assertTrue(embed.description.endswith("..."))
        self.assertLessEqual(len(embed.description), 4004)
        self.assertEqual(embed.title, "Utility")

    def test_command_description_falls_back_to_help_text(self):
        cmd = FakeCommand("alpha", cog_name="Utility", help_text="  Detailed \n help  ")

        self.assertEqual(self.cog._command_description(cmd), "Detailed help")

    def test_build_home_embed_shows_counts_and_empty_state(self):
        embed = self.cog.build_home_embed("!", ["Utility"])
        self.assertIn("**Utility** — 4 commands", embed.description)

        empty = self.cog.build_home_embed("!", [])
        self.assertIn("No command categories available.", empty.description)


class CommandsCogAsyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        alpha = FakeCommand("alpha", cog_name="Utility", short_doc="Alpha")
        audio = FakeCommand("play", cog_name="Audio", short_doc="Play")
        misc = FakeCommand("misc", cog_name="Misc", short_doc="Misc")
        self.cog = commands_module.Commands(types.SimpleNamespace(commands=[alpha, audio, misc]))

    async def test_available_cogs_uses_allowlist_when_present(self):
        await self.cog.config.allowlist.set(["Misc", "Missing"])

        result = await self.cog._available_cogs("!")

        self.assertEqual(result, ["Misc"])

    async def test_available_cogs_auto_discovers_and_excludes_defaults(self):
        result = await self.cog._available_cogs("!")

        self.assertEqual(result, ["Misc", "Utility"])

    async def test_commandsset_handlers_update_config(self):
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(send=send)

        await self.cog.commandsset_allow(ctx, cog_name="Misc")
        await self.cog.commandsset_deny(ctx, cog_name="Games")
        excluded_before_reset = await self.cog.config.excluded_cogs()
        await self.cog.commandsset_remove(ctx, cog_name="Misc")
        await self.cog.commandsset_reset(ctx)

        allowlist = await self.cog.config.allowlist()
        excluded = await self.cog.config.excluded_cogs()
        self.assertEqual(allowlist, [])
        self.assertIn("Games", excluded_before_reset)
        self.assertNotIn("Games", excluded)
        self.assertEqual(sent[0], "Added `Misc` to the allowlist.")
        self.assertEqual(sent[1], "Excluded `Games` from auto-discovery.")
        self.assertEqual(sent[2], "Removed `Misc` from commands menu overrides.")
        self.assertEqual(sent[3], "Reset commands menu config to auto-discovery defaults.")


if __name__ == "__main__":
    unittest.main()
