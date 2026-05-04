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

    def test_commands_menu_view_splits_large_cog_lists_into_multiple_selects(self):
        cog_names = [f"Cog{i:02d}" for i in range(30)]

        view = commands_module.CommandsMenuView(
            cog=self.cog,
            author_id=1,
            prefix="!",
            cog_names=cog_names,
        )

        self.assertEqual(len(view.children), 2)
        self.assertEqual(len(view.children[0].kwargs["options"]), 25)
        self.assertEqual(len(view.children[1].kwargs["options"]), 5)
        self.assertEqual(view.children[0].kwargs["placeholder"], "Select a cog... (1/2)")
        self.assertEqual(view.children[1].kwargs["placeholder"], "Select a cog... (2/2)")

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
        await self.cog.commandsset_deny(ctx, cog_name="misc")
        excluded_before_reset = list(await self.cog.config.excluded_cogs())
        await self.cog.commandsset_remove(ctx, cog_name="MISC")
        await self.cog.commandsset_reset(ctx)

        allowlist = await self.cog.config.allowlist()
        excluded = await self.cog.config.excluded_cogs()
        self.assertEqual(allowlist, [])
        self.assertIn("Misc", excluded_before_reset)
        self.assertNotIn("Misc", excluded)
        self.assertEqual(sent[0], "Added `Misc` to the allowlist.")
        self.assertEqual(
            sent[1],
            "Excluded `Misc` from auto-discovery and removed it from the allowlist.",
        )
        self.assertEqual(sent[2], "Removed `Misc` from commands menu overrides.")
        self.assertEqual(sent[3], "Reset commands menu config to auto-discovery defaults.")

    async def test_commandsset_allow_removes_matching_exclusion_case_insensitively(self):
        sent = []

        async def send(message):
            sent.append(message)

        await self.cog.config.excluded_cogs.set(["Alias", "misc"])
        ctx = types.SimpleNamespace(send=send)

        await self.cog.commandsset_allow(ctx, cog_name="Misc")

        self.assertEqual(await self.cog.config.allowlist(), ["Misc"])
        self.assertEqual(await self.cog.config.excluded_cogs(), ["Alias"])
        self.assertEqual(sent, ["Added `Misc` to the allowlist and removed it from exclusions."])

    async def test_contextual_command_lines_respect_can_run(self):
        visible = FakeCommand("visible", cog_name="Utility", short_doc="Shown")
        blocked = FakeCommand("blocked", cog_name="Utility", short_doc="Hidden")

        async def blocked_can_run(ctx):
            return False

        blocked.can_run = blocked_can_run
        cog = commands_module.Commands(types.SimpleNamespace(commands=[visible, blocked]))
        ctx = types.SimpleNamespace()

        lines = await cog._build_cog_lines_for_context("!", "Utility", ctx)
        available = await cog._available_cogs("!", ctx)

        self.assertEqual(lines, ["**`!visible`** — Shown"])
        self.assertEqual(available, ["Utility"])


if __name__ == "__main__":
    unittest.main()
