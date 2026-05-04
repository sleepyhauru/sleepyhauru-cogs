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

    @staticmethod
    def _author(user_id=1, *, manage_channels=False):
        return types.SimpleNamespace(
            id=user_id,
            guild_permissions=types.SimpleNamespace(manage_channels=manage_channels),
        )

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

        ctx = types.SimpleNamespace(
            guild=self.guild,
            author=self._author(manage_channels=True),
            send=send,
        )

        await self.cog.ignore_global_commands(ctx)
        await self.cog.ignore_global_commands(ctx)

        self.assertEqual(
            sent,
            ["Ignoring bot owner global images.", "Bot owner global images enabled."],
        )

    async def test_allowlist_management_updates_user_ids(self):
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=self.guild, send=send)

        await self.cog.allow_user(ctx, 42)
        await self.cog.allow_user(ctx, 42)
        await self.cog.show_allowlist(ctx)
        await self.cog.deny_user(ctx, 42)
        await self.cog.deny_user(ctx, 42)

        self.assertEqual(sent[0], "Added `42` to the AddImage allowlist.")
        self.assertEqual(sent[1], "`42` is already on the AddImage allowlist.")
        self.assertIn("<@42> (`42`)", sent[2])
        self.assertEqual(sent[3], "Removed `42` from the AddImage allowlist.")
        self.assertEqual(sent[4], "`42` is not on the AddImage allowlist.")

    async def test_allowlisted_user_can_bypass_manage_channels(self):
        sent = []

        async def send(message):
            sent.append(message)

        author = self._author(user_id=99, manage_channels=False)
        ctx = types.SimpleNamespace(guild=self.guild, author=author, send=send)
        await self.cog.config.guild(self.guild).manage_channels_allowlist.set([99])

        await self.cog.ignore_global_commands(ctx)

        self.assertEqual(sent, ["Ignoring bot owner global images."])

    async def test_non_allowlisted_user_without_manage_channels_is_denied(self):
        sent = []

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(
            guild=self.guild,
            author=self._author(user_id=55, manage_channels=False),
            send=send,
        )

        await self.cog.ignore_global_commands(ctx)

        self.assertEqual(sent, [addimage_module.ALLOWLIST_DENIED_MESSAGE])

    async def test_rename_image_updates_matching_entry(self):
        sent = []

        async def send(message):
            sent.append(message)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "oldname", "count": 0, "file_loc": "x.png", "author": 1}]
        )
        ctx = types.SimpleNamespace(
            guild=self.guild,
            author=self._author(manage_channels=True),
            send=send,
        )

        await self.cog.rename_image(ctx, "oldname", "newname")

        images = await self.cog.config.guild(self.guild).images()
        self.assertEqual(images[0]["command_name"], "newname")
        self.assertEqual(sent, ["Renamed `oldname` to `newname`."])

    async def test_listimages_uses_passed_guild_object(self):
        sent = []

        async def fake_send(*, embed=None, file=None):
            sent.append((embed, file))

        async def fake_preview(images, guild, page_number):
            self.assertEqual(page_number, 1)
            self.assertIs(guild, self.guild)
            self.assertEqual(images[0]["command_name"], "guildimg")
            return types.SimpleNamespace(filename="addimage-list-page-1.png")

        self.cog.bot.get_guild = lambda guild_id: (_ for _ in ()).throw(
            AssertionError("get_guild should not be called with a converted guild")
        )
        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 2, "author": 1, "file_loc": "x.png"}]
        )

        original_preview = self.cog._build_list_preview_file
        self.cog._build_list_preview_file = fake_preview
        try:
            ctx = types.SimpleNamespace(
                message=types.SimpleNamespace(guild=None, created_at=None),
                send=fake_send,
            )
            await self.cog.listimages(ctx, "guild", self.guild)
        finally:
            self.cog._build_list_preview_file = original_preview

        self.assertEqual(len(sent), 1)
        embed, file = sent[0]
        self.assertEqual(embed.fields[0].name, "guildimg")
        self.assertEqual(embed.image, "attachment://addimage-list-page-1.png")
        self.assertEqual(file.filename, "addimage-list-page-1.png")

    async def test_clear_images_handles_missing_guild_folder(self):
        ticked = []

        async def tick():
            ticked.append(True)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 0, "author": 1, "file_loc": "x.png"}]
        )
        shutil.rmtree(self.data_dir, ignore_errors=True)
        ctx = types.SimpleNamespace(
            guild=self.guild,
            author=self._author(manage_channels=True),
            tick=tick,
            send=lambda *args, **kwargs: None,
        )

        await self.cog.clear_images(ctx)

        self.assertEqual(await self.cog.config.guild(self.guild).images(), [])
        self.assertEqual(ticked, [True])

    async def test_addimage_group_shows_summary(self):
        sent = []

        async def send(message):
            sent.append(message)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 1, "author": 1, "file_loc": "x.png"}]
        )
        await self.cog.config.images.set(
            [{"command_name": "globalimg", "count": 2, "author": 1, "file_loc": "g.png"}]
        )

        ctx = types.SimpleNamespace(guild=self.guild, clean_prefix="!", send=send)

        await self.cog.addimage(ctx)

        self.assertIn("Guild media saved: `1`", sent[0])
        self.assertIn("Global media available: `1`", sent[0])
        self.assertIn("Next: run `!addimage add <name>`", sent[0])

    async def test_clean_deleted_images_handles_missing_guild_folder(self):
        ticked = []

        async def tick():
            ticked.append(True)

        await self.cog.config.guild(self.guild).images.set(
            [{"command_name": "guildimg", "count": 0, "author": 1, "file_loc": "x.png"}]
        )
        shutil.rmtree(self.data_dir, ignore_errors=True)
        ctx = types.SimpleNamespace(
            guild=self.guild,
            author=self._author(manage_channels=True),
            tick=tick,
            send=lambda *args, **kwargs: None,
        )

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
            author=self._author(manage_channels=True),
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
        source_guild.get_member = lambda user_id: self._author(user_id=user_id, manage_channels=True)
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
        source_guild.get_member = lambda user_id: self._author(user_id=user_id, manage_channels=True)
        await self.cog.config.guild(source_guild).images.set(
            [{"command_name": "cat", "count": 4, "file_loc": "origin.png", "author": 7}]
        )

        called = []

        async def fake_copy(image, source, destination, new_name):
            called.append((image, source, destination, new_name))

        self.cog.copy_image_location = fake_copy
        ctx = types.SimpleNamespace(
            guild=destination_guild,
            author=self._author(manage_channels=True),
            send=send,
        )

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
        source_guild.get_member = lambda user_id: self._author(user_id=user_id, manage_channels=True)
        await self.cog.config.guild(source_guild).images.set(
            [{"command_name": "cat", "count": 4, "file_loc": "origin.png", "author": 7}]
        )

        async def fake_copy(image, source, destination, new_name):
            raise FileNotFoundError("origin.png")

        self.cog.copy_image_location = fake_copy
        ctx = types.SimpleNamespace(
            guild=destination_guild,
            author=self._author(manage_channels=True),
            send=send,
        )

        await self.cog.copy_image_guild(ctx, source_guild, "cat")

        self.assertEqual(
            sent,
            [
                "The source file for `cat` is missing from `Source`. "
                "Run `addimage clean_deleted_images` there first."
            ],
        )

    async def test_copy_image_guild_requires_source_guild_permission(self):
        sent = []

        async def send(message):
            sent.append(message)

        source_guild = types.SimpleNamespace(id=222, name="Source")
        destination_guild = types.SimpleNamespace(id=333, name="Destination")
        source_guild.get_member = lambda user_id: self._author(user_id=user_id, manage_channels=False)
        ctx = types.SimpleNamespace(
            guild=destination_guild,
            author=self._author(user_id=7, manage_channels=True),
            send=send,
        )

        await self.cog.copy_image_guild(ctx, source_guild, "cat")

        self.assertEqual(
            sent,
            ["You need Manage Channels in the source server to copy its saved media."],
        )

    async def test_copy_permission_uses_current_author_when_member_cache_misses(self):
        self.guild.get_member = lambda user_id: None
        ctx = types.SimpleNamespace(
            guild=self.guild,
            author=self._author(user_id=7, manage_channels=True),
        )

        self.assertTrue(await self.cog._can_copy_from_source_guild(ctx, self.guild))


if __name__ == "__main__":
    unittest.main()
