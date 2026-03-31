import asyncio
import shutil
import types
import unittest
import uuid
from pathlib import Path

from tests.support import load_module


addimage_module = load_module("addimage.addimage")


class AddImageHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_root = Path(__file__).resolve().parent / "_tmp_addimage" / uuid.uuid4().hex
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self.original_cog_data_path = addimage_module.cog_data_path
        addimage_module.cog_data_path = lambda cog: self.tmp_root

        bot = types.SimpleNamespace(
            get_command=lambda name: None,
            user=types.SimpleNamespace(display_name="Bot", display_avatar="avatar"),
        )
        self.cog = addimage_module.AddImage(bot)
        self.guild = types.SimpleNamespace(id=123, name="Guild")
        self.data_dir = self.tmp_root / str(self.guild.id)

    def tearDown(self):
        addimage_module.cog_data_path = self.original_cog_data_path
        shutil.rmtree(self.tmp_root, ignore_errors=True)

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

    async def test_validate_attachment_rejects_non_media_and_oversized_files(self):
        async def max_file_size():
            return 4 * 1024 * 1024

        self.cog.config.max_file_size = max_file_size

        bad = types.SimpleNamespace(filename="notes.txt", size=100)
        too_large = types.SimpleNamespace(filename="image.png", size=5 * 1024 * 1024)
        ok = types.SimpleNamespace(filename="image.png", size=100)
        video = types.SimpleNamespace(filename="clip.mp4", size=100)

        self.assertEqual(
            await self.cog.validate_attachment(bad),
            "That attachment is not a supported image or video type.",
        )
        self.assertEqual(
            await self.cog.validate_attachment(too_large),
            "That file is too large. Max allowed size is 4 MB.",
        )
        self.assertIsNone(await self.cog.validate_attachment(ok))
        self.assertIsNone(await self.cog.validate_attachment(video))

    def test_safe_storage_extension_normalizes_filename_suffixes(self):
        self.assertEqual(self.cog._safe_storage_extension("image.png"), ".png")
        self.assertEqual(self.cog._safe_storage_extension("../../weird/../photo.jpeg"), ".jpeg")
        self.assertEqual(self.cog._safe_storage_extension("avatar.jpe"), ".jpg")

    def test_generate_storage_filename_keeps_only_safe_suffix(self):
        generated = self.cog._generate_storage_filename("../../weird/../photo.jpeg")

        self.assertTrue(generated.endswith(".jpeg"))
        self.assertNotIn("/", generated)
        self.assertNotIn("\\", generated)
        self.assertNotIn("..", generated)

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
        self.assertEqual(sent, ["Media adding cancelled."])

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
        self.assertEqual(sent, ["Media adding timed out."])

    async def test_wait_for_image_ignores_non_exact_exit_messages(self):
        sent = []

        async def send(message):
            sent.append(message)

        first = types.SimpleNamespace(author="user", attachments=[], content="please exit this")
        second = types.SimpleNamespace(
            author="user",
            attachments=[types.SimpleNamespace(filename="image.png", size=100)],
            content="here",
        )

        async def wait_for(event, check, timeout):
            self.assertFalse(check(first))
            self.assertTrue(check(second))
            return second

        self.cog.bot.wait_for = wait_for
        ctx = types.SimpleNamespace(author="user", send=send)

        result = await self.cog.wait_for_image(ctx)

        self.assertIs(result, second)
        self.assertEqual(sent, [])

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

    async def test_listimages_uses_passed_guild_object(self):
        captured_pages = []

        async def fake_menu(ctx, pages, controls):
            captured_pages.extend(pages)

        self.cog.bot.get_guild = lambda guild_id: (_ for _ in ()).throw(
            AssertionError("get_guild should not be called with a converted guild")
        )
        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 2, "author": 1, "file_loc": "x.png"}]
        )

        original_menu = addimage_module.menu
        addimage_module.menu = fake_menu
        try:
            ctx = types.SimpleNamespace(
                message=types.SimpleNamespace(guild=None, created_at=None),
                send=lambda *args, **kwargs: None,
            )
            await self.cog.listimages(ctx, "guild", self.guild)
        finally:
            addimage_module.menu = original_menu

        self.assertEqual(len(captured_pages), 1)
        self.assertEqual(captured_pages[0].fields[0].name, "guildimg")

    async def test_clear_images_handles_missing_guild_folder(self):
        ticked = []

        async def tick():
            ticked.append(True)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 0, "author": 1, "file_loc": "x.png"}]
        )
        shutil.rmtree(self.data_dir, ignore_errors=True)
        ctx = types.SimpleNamespace(guild=self.guild, tick=tick)

        await self.cog.clear_images(ctx)

        self.assertEqual(await self.cog.config.guild(self.guild).images(), [])
        self.assertEqual(ticked, [True])

    async def test_clean_deleted_images_handles_missing_guild_folder(self):
        ticked = []

        async def tick():
            ticked.append(True)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 0, "author": 1, "file_loc": "x.png"}]
        )
        shutil.rmtree(self.data_dir, ignore_errors=True)
        ctx = types.SimpleNamespace(guild=self.guild, tick=tick)

        await self.cog.clean_deleted_images(ctx)

        self.assertEqual(await self.cog.config.guild(self.guild).images(), [])
        self.assertEqual(ticked, [True])

    async def test_add_image_guild_does_not_confirm_before_validation(self):
        sent = []

        async def send(message):
            sent.append(message)

        self.cog.validate_attachment = lambda attachment: asyncio.sleep(0, result="bad file")
        ctx = types.SimpleNamespace(
            guild=self.guild,
            message=types.SimpleNamespace(
                guild=self.guild,
                attachments=[types.SimpleNamespace(filename="image.png", size=100)],
            ),
            send=send,
        )

        await self.cog.add_image_guild(ctx, "sample")

        self.assertEqual(sent, ["bad file"])

    async def test_save_image_location_ignores_original_attachment_filename(self):
        saved_paths = []

        async def fake_save(path):
            saved_paths.append(Path(path))

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(id=5),
            attachments=[types.SimpleNamespace(filename="../../escape.png", save=fake_save)],
        )

        await self.cog.save_image_location(message, "sample", self.guild)

        images = await self.cog.config.guild(self.guild).images()
        self.assertEqual(len(images), 1)
        stored_name = images[0]["file_loc"]
        self.assertNotIn("/", stored_name)
        self.assertNotIn("\\", stored_name)
        self.assertNotIn("..", stored_name)
        self.assertTrue(stored_name.endswith(".png"))
        self.assertEqual(saved_paths[0], self.data_dir / stored_name)

    async def test_save_image_location_keeps_video_extension(self):
        saved_paths = []

        async def fake_save(path):
            saved_paths.append(Path(path))

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(id=5),
            attachments=[types.SimpleNamespace(filename="clip.mp4", save=fake_save)],
        )

        await self.cog.save_image_location(message, "sample", self.guild)

        images = await self.cog.config.guild(self.guild).images()
        self.assertEqual(len(images), 1)
        stored_name = images[0]["file_loc"]
        self.assertTrue(stored_name.endswith(".mp4"))
        self.assertEqual(saved_paths[0], self.data_dir / stored_name)

    async def test_copy_image_location_copies_file_and_resets_count(self):
        source_guild = types.SimpleNamespace(id=222, name="Source")
        destination_guild = types.SimpleNamespace(id=333, name="Destination")
        source_dir = self.tmp_root / str(source_guild.id)
        destination_dir = self.tmp_root / str(destination_guild.id)
        shutil.rmtree(source_dir, ignore_errors=True)
        shutil.rmtree(destination_dir, ignore_errors=True)
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "origin.png").write_bytes(b"image-bytes")

        image = {"command_name": "old", "count": 9, "file_loc": "origin.png", "author": 42}

        await self.cog.copy_image_location(image, source_guild, destination_guild, "newname")

        images = await self.cog.config.guild(destination_guild).images()
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["command_name"], "newname")
        self.assertEqual(images[0]["count"], 0)
        self.assertEqual(images[0]["author"], 42)
        copied_path = destination_dir / images[0]["file_loc"]
        self.assertTrue(copied_path.is_file())
        self.assertEqual(copied_path.read_bytes(), b"image-bytes")

    async def test_copy_image_guild_copies_from_source_guild(self):
        sent = []

        async def send(message):
            sent.append(message)

        source_guild = types.SimpleNamespace(id=222, name="Source")
        destination_guild = types.SimpleNamespace(id=333, name="Destination")
        await self.cog.config.guild(source_guild).images.set(
            [{"command_name": "cat", "count": 4, "file_loc": "origin.png", "author": 7}]
        )

        called = []

        async def fake_copy(image, source, destination, new_name):
            called.append((image, source, destination, new_name))

        self.cog.copy_image_location = fake_copy
        ctx = types.SimpleNamespace(guild=destination_guild, send=send)

        await self.cog.copy_image_guild(ctx, source_guild, "cat")

        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][1], source_guild)
        self.assertEqual(called[0][2], destination_guild)
        self.assertEqual(called[0][3], "cat")
        self.assertEqual(sent, ["Copied `cat` from `Source` to `Destination`."])

    async def test_copy_image_guild_reports_missing_source_file(self):
        sent = []

        async def send(message):
            sent.append(message)

        source_guild = types.SimpleNamespace(id=222, name="Source")
        destination_guild = types.SimpleNamespace(id=333, name="Destination")
        await self.cog.config.guild(source_guild).images.set(
            [{"command_name": "cat", "count": 4, "file_loc": "origin.png", "author": 7}]
        )

        async def fake_copy(image, source, destination, new_name):
            raise FileNotFoundError("origin.png")

        self.cog.copy_image_location = fake_copy
        ctx = types.SimpleNamespace(guild=destination_guild, send=send)

        await self.cog.copy_image_guild(ctx, source_guild, "cat")

        self.assertEqual(
            sent,
            [
                "The source file for `cat` is missing from `Source`. "
                "Run `addimage clean_deleted_images` there first."
            ],
        )


if __name__ == "__main__":
    unittest.main()
