import asyncio
import types
import unittest

from tests.support import load_module


addimage_module = load_module("addimage.addimage")


class AddImageHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot = types.SimpleNamespace(get_command=lambda name: None)
        self.cog = addimage_module.AddImage(bot)
        self.guild = types.SimpleNamespace(id=123, name="Guild")

    async def test_first_word_lowercases_first_token(self):
        self.assertEqual(await self.cog.first_word("Hello There Friend"), "hello")

    async def test_get_prefix_uses_sorted_fallback_prefixes(self):
        async def command_prefix(bot, message):
            return ["!", "!!"]

        self.cog.bot.command_prefix = command_prefix
        message = types.SimpleNamespace(guild=self.guild, content="!!ping")

        prefix = await self.cog.get_prefix(message)

        self.assertEqual(prefix, "!!")

    async def test_get_prefix_raises_when_no_prefix_matches(self):
        async def get_valid_prefixes(guild):
            return ["!"]

        self.cog.bot.get_valid_prefixes = get_valid_prefixes
        message = types.SimpleNamespace(guild=self.guild, content="hello")

        with self.assertRaises(ValueError):
            await self.cog.get_prefix(message)

    async def test_validate_attachment_rejects_non_images_and_oversized_files(self):
        async def max_file_size():
            return 4 * 1024 * 1024

        self.cog.config.max_file_size = max_file_size

        bad = types.SimpleNamespace(filename="notes.txt", size=100)
        too_large = types.SimpleNamespace(filename="image.png", size=5 * 1024 * 1024)
        ok = types.SimpleNamespace(filename="image.png", size=100)

        self.assertEqual(
            await self.cog.validate_attachment(bad),
            "That attachment is not a supported image type.",
        )
        self.assertEqual(
            await self.cog.validate_attachment(too_large),
            "That file is too large. Max allowed size is 4 MB.",
        )
        self.assertIsNone(await self.cog.validate_attachment(ok))

    async def test_wait_for_image_returns_exit_message_and_notifies_user(self):
        sent = []

        async def send(message):
            sent.append(message)

        exit_message = types.SimpleNamespace(
            author="user",
            attachments=[],
            content="exit",
        )

        async def wait_for(event, check, timeout):
            self.assertTrue(check(exit_message))
            return exit_message

        self.cog.bot.wait_for = wait_for
        ctx = types.SimpleNamespace(author="user", send=send)

        result = await self.cog.wait_for_image(ctx)

        self.assertEqual(result, exit_message)
        self.assertEqual(sent, ["Image adding cancelled."])

    async def test_wait_for_image_reports_timeout(self):
        sent = []

        async def send(message):
            sent.append(message)

        async def wait_for(event, check, timeout):
            raise asyncio.TimeoutError

        self.cog.bot.wait_for = wait_for
        ctx = types.SimpleNamespace(author="user", send=send)

        result = await self.cog.wait_for_image(ctx)

        self.assertIsNone(result)
        self.assertEqual(sent, ["Image adding timed out."])

    async def test_check_command_exists_checks_guild_global_and_bot_commands(self):
        await self.cog.config.guild(self.guild).images.set([{"command_name": "guildimg"}])
        await self.cog.config.images.set([{"command_name": "globalimg"}])
        self.cog.bot.get_command = lambda name: object() if name == "realcmd" else None

        self.assertTrue(await self.cog.check_command_exists("guildimg", self.guild))
        self.assertTrue(await self.cog.check_command_exists("globalimg", self.guild))
        self.assertTrue(await self.cog.check_command_exists("realcmd", self.guild))
        self.assertFalse(await self.cog.check_command_exists("missing", self.guild))

    async def test_ignore_global_commands_toggles_config_and_reports_state(self):
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=self.guild, send=send)

        await self.cog.ignore_global_commands(ctx)
        await self.cog.ignore_global_commands(ctx)

        self.assertEqual(
            sent,
            ["Ignoring bot owner global images.", "Bot owner global images enabled."],
        )

    async def test_rename_image_updates_matching_entry(self):
        sent = []

        async def send(message):
            sent.append(message)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "oldname", "count": 0, "file_loc": "x.png", "author": 1}]
        )
        ctx = types.SimpleNamespace(guild=self.guild, send=send)

        await self.cog.rename_image(ctx, "oldname", "newname")

        images = await self.cog.config.guild(self.guild).images()
        self.assertEqual(images[0]["command_name"], "newname")
        self.assertEqual(sent, ["Renamed `oldname` to `newname`."])


if __name__ == "__main__":
    unittest.main()
