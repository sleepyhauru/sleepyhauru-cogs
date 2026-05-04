import asyncio
import types
import unittest

from tests.support import load_module


discord = load_module("discord")
embedfix_module = load_module("embedfix.embedfix")


class EmbedFixTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def cog_disabled_in_guild(cog, guild):
            return False

        self.bot = types.SimpleNamespace(cog_disabled_in_guild=cog_disabled_in_guild)
        self.cog = embedfix_module.EmbedFix(self.bot)

    async def _enable(self, guild_id):
        await self.cog.config.guild(types.SimpleNamespace(id=guild_id)).enabled.set(True)

    def _interaction(self, user_id=1):
        edits = []
        messages = []

        class FakeResponse:
            async def edit_message(self, **kwargs):
                edits.append(kwargs)

            async def send_message(self, message, ephemeral=False):
                messages.append((message, ephemeral))

        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=user_id),
            message=types.SimpleNamespace(),
            response=FakeResponse(),
        )
        return interaction, edits, messages

    def test_rewrite_url_replaces_supported_hosts(self):
        rules = self.cog._default_rules()

        self.assertEqual(
            self.cog._rewrite_url("https://x.com/user/status/123?s=20#frag", rules),
            "https://fxtwitter.com/user/status/123?s=20#frag",
        )
        self.assertEqual(
            self.cog._rewrite_url("https://www.instagram.com/reel/abc/", rules),
            "https://fxstagram.com/reel/abc/",
        )
        self.assertIsNone(self.cog._rewrite_url("https://example.com/path", rules))

    async def test_get_rules_migrates_legacy_instagram_default_target(self):
        guild = types.SimpleNamespace(id=30)
        rules = self.cog._default_rules()
        instagram_rule = next(rule for rule in rules if rule["name"] == "instagram")
        instagram_rule["target_host"] = "ddinstagram.com"
        await self.cog.config.guild(guild).rules.set(rules)

        migrated = await self.cog._get_rules(guild)

        instagram_rule = next(rule for rule in migrated if rule["name"] == "instagram")
        self.assertEqual(instagram_rule["target_host"], "fxstagram.com")

    async def test_get_rules_migrates_vxinstagram_default_target(self):
        guild = types.SimpleNamespace(id=34)
        rules = self.cog._default_rules()
        instagram_rule = next(rule for rule in rules if rule["name"] == "instagram")
        instagram_rule["target_host"] = "vxinstagram.com"
        await self.cog.config.guild(guild).rules.set(rules)

        migrated = await self.cog._get_rules(guild)

        instagram_rule = next(rule for rule in migrated if rule["name"] == "instagram")
        self.assertEqual(instagram_rule["target_host"], "fxstagram.com")

    async def test_get_rules_migrates_toinstagram_default_target(self):
        guild = types.SimpleNamespace(id=37)
        rules = self.cog._default_rules()
        instagram_rule = next(rule for rule in rules if rule["name"] == "instagram")
        instagram_rule["target_host"] = "toinstagram.com"
        await self.cog.config.guild(guild).rules.set(rules)

        migrated = await self.cog._get_rules(guild)

        instagram_rule = next(rule for rule in migrated if rule["name"] == "instagram")
        self.assertEqual(instagram_rule["target_host"], "fxstagram.com")

    async def test_get_rules_migrates_d_toinstagram_default_target(self):
        guild = types.SimpleNamespace(id=41)
        rules = self.cog._default_rules()
        instagram_rule = next(rule for rule in rules if rule["name"] == "instagram")
        instagram_rule["target_host"] = "d.toinstagram.com"
        await self.cog.config.guild(guild).rules.set(rules)

        migrated = await self.cog._get_rules(guild)

        instagram_rule = next(rule for rule in migrated if rule["name"] == "instagram")
        self.assertEqual(instagram_rule["target_host"], "fxstagram.com")

    async def test_get_rules_preserves_custom_instagram_target(self):
        guild = types.SimpleNamespace(id=35)
        rules = self.cog._default_rules()
        instagram_rule = next(rule for rule in rules if rule["name"] == "instagram")
        instagram_rule["target_host"] = "kkinstagram.com"
        await self.cog.config.guild(guild).rules.set(rules)

        migrated = await self.cog._get_rules(guild)

        instagram_rule = next(rule for rule in migrated if rule["name"] == "instagram")
        self.assertEqual(instagram_rule["target_host"], "kkinstagram.com")

    def test_extract_urls_trims_punctuation_and_skips_suppressed_links(self):
        urls = self.cog._extract_urls(
            "Look https://x.com/a/status/1. and <https://x.com/a/status/2>"
        )

        self.assertEqual(urls, ["https://x.com/a/status/1"])

    def test_fixed_urls_deduplicates_and_respects_limit(self):
        rules = self.cog._default_rules()
        fixed_urls = self.cog._fixed_urls_for_message(
            "https://x.com/a/status/1 https://x.com/a/status/1 https://reddit.com/r/test/1",
            rules,
            1,
        )

        self.assertEqual(fixed_urls, ["https://fxtwitter.com/a/status/1"])

    async def test_listener_starts_disabled_by_default(self):
        sent = []
        edits = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def edit(**kwargs):
            edits.append(kwargs)

        guild = types.SimpleNamespace(id=10)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://x.com/user/status/123",
            channel=types.SimpleNamespace(send=send),
            edit=edit,
        )

        await self.cog.on_message_without_command(message)

        self.assertEqual(sent, [])
        self.assertEqual(edits, [])

    async def test_listener_reposts_fixed_link_and_suppresses_original_embed(self):
        sent = []
        edits = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def edit(**kwargs):
            edits.append(kwargs)

        guild = types.SimpleNamespace(id=1)
        await self._enable(guild.id)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="broken embed: https://x.com/user/status/123?s=20",
            channel=types.SimpleNamespace(send=send),
            edit=edit,
        )

        await self.cog.on_message_without_command(message)

        self.assertEqual(
            sent,
            [
                (
                    "[Tweet](<https://x.com/user/status/123?s=20>) • "
                    "[@user](<https://x.com/user>) • "
                    "[FxTwitter](https://fxtwitter.com/user/status/123?s=20)",
                    "none",
                )
            ],
        )
        self.assertEqual(edits, [{"suppress": True}])
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.detection_count(), 1)
        self.assertEqual(await conf.repost_count(), 1)
        self.assertEqual(await conf.suppressed_count(), 1)

    async def test_listener_retries_suppression_after_delayed_embeds(self):
        sent = []
        edits = []
        sleeps = []
        fetched = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def edit(**kwargs):
            edits.append(("original", kwargs))

        async def fetched_edit(**kwargs):
            edits.append(("fetched", kwargs))

        async def sleep(delay):
            sleeps.append(delay)

        async def fetch_message(message_id):
            fetched.append(message_id)
            return types.SimpleNamespace(edit=fetched_edit)

        guild = types.SimpleNamespace(id=32)
        await self._enable(guild.id)
        self.cog._sleep = sleep
        message = types.SimpleNamespace(
            id=3200,
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://twitter.com/user/status/123",
            channel=types.SimpleNamespace(send=send, fetch_message=fetch_message),
            edit=edit,
        )

        await self.cog.on_message_without_command(message)
        tasks = list(self.cog.suppress_retry_tasks)
        await asyncio.gather(*tasks)

        self.assertEqual(
            sent,
            [
                (
                    "[Tweet](<https://twitter.com/user/status/123>) • "
                    "[@user](<https://twitter.com/user>) • "
                    "[FxTwitter](https://fxtwitter.com/user/status/123)",
                    "none",
                )
            ],
        )
        self.assertEqual(
            edits,
            [
                ("fetched", {"suppress": True}),
                ("fetched", {"suppress": True}),
                ("fetched", {"suppress": True}),
                ("fetched", {"suppress": True}),
            ],
        )
        self.assertEqual(fetched, [3200, 3200, 3200, 3200])
        self.assertEqual(sleeps, list(embedfix_module.SUPPRESS_RETRY_DELAYS))
        self.assertEqual(self.cog.suppress_retry_tasks, set())
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.suppressed_count(), 1)
        self.assertEqual(await conf.suppress_error_count(), 0)

    async def test_listener_retries_when_initial_fresh_suppression_fails(self):
        sent = []
        edits = []
        sleeps = []
        fetched = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def sleep(delay):
            sleeps.append(delay)

        async def fetched_edit(**kwargs):
            edits.append(kwargs)
            if len(edits) == 1:
                raise discord.HTTPException()

        async def fetch_message(message_id):
            fetched.append(message_id)
            return types.SimpleNamespace(edit=fetched_edit)

        guild = types.SimpleNamespace(id=43)
        await self._enable(guild.id)
        self.cog._sleep = sleep
        message = types.SimpleNamespace(
            id=4300,
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://twitter.com/user/status/123",
            channel=types.SimpleNamespace(send=send, fetch_message=fetch_message),
        )

        await self.cog.on_message_without_command(message)
        tasks = list(self.cog.suppress_retry_tasks)
        await asyncio.gather(*tasks)

        self.assertEqual(
            sent,
            [
                (
                    "[Tweet](<https://twitter.com/user/status/123>) • "
                    "[@user](<https://twitter.com/user>) • "
                    "[FxTwitter](https://fxtwitter.com/user/status/123)",
                    "none",
                )
            ],
        )
        self.assertEqual(edits, [{"suppress": True}] * 4)
        self.assertEqual(fetched, [4300, 4300, 4300, 4300])
        self.assertEqual(sleeps, list(embedfix_module.SUPPRESS_RETRY_DELAYS))
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.suppressed_count(), 1)
        self.assertEqual(await conf.suppress_error_count(), 1)

    async def test_listener_reposts_instagram_as_fixtweetbot_provider_link(self):
        sent = []
        edits = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def edit(**kwargs):
            edits.append(kwargs)

        guild = types.SimpleNamespace(id=38)
        await self._enable(guild.id)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://instagram.com/reel/abc/",
            channel=types.SimpleNamespace(send=send),
            edit=edit,
        )

        await self.cog.on_message_without_command(message)

        self.assertEqual(
            sent,
            [
                (
                    "[Instagram](<https://instagram.com/reel/abc/>) • "
                    "[InstaFix](https://fxstagram.com/reel/abc/)",
                    "none",
                )
            ],
        )
        self.assertEqual(edits, [{"suppress": True}])
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.repost_count(), 1)

    async def test_listener_respects_suppression_toggle_with_provider_link(self):
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        guild = types.SimpleNamespace(id=39)
        await self._enable(guild.id)
        await self.cog.config.guild(guild).suppress_original.set(False)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://instagram.com/reel/abc/",
            channel=types.SimpleNamespace(send=send),
        )

        await self.cog.on_message_without_command(message)

        self.assertEqual(
            sent,
            [
                (
                    "[Instagram](<https://instagram.com/reel/abc/>) • "
                    "[InstaFix](https://fxstagram.com/reel/abc/)",
                    "none",
                )
            ],
        )
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.repost_count(), 1)

    async def test_send_fixed_links_replies_with_fixtweetbot_markdown(self):
        sent = []
        replies = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def reply(message, mention_author=False, allowed_mentions=None):
            replies.append((message, mention_author, allowed_mentions))

        fixed_links = [
            {
                "original_url": "https://instagram.com/grimkujow/reel/abc/",
                "fixed_url": "https://fxstagram.com/grimkujow/reel/abc/",
                "rule_name": embedfix_module.INSTAGRAM_RULE_NAME,
                "label": "Instagram",
                "fixer_name": "InstaFix",
            }
        ]
        original_message = types.SimpleNamespace(reply=reply)

        await self.cog._send_fixed_links(
            types.SimpleNamespace(send=send),
            types.SimpleNamespace(id=40),
            fixed_links,
            original_message=original_message,
        )

        self.assertEqual(sent, [])
        self.assertEqual(
            replies,
            [
                (
                    "[Instagram](<https://instagram.com/grimkujow/reel/abc/>) • "
                    "[@grimkujow](<https://instagram.com/grimkujow>) • "
                    "[InstaFix](https://fxstagram.com/grimkujow/reel/abc/)",
                    False,
                    "none",
                )
            ],
        )

    async def test_send_fixed_links_falls_back_to_channel_send_when_reply_fails(self):
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def reply(message, mention_author=False, allowed_mentions=None):
            raise discord.Forbidden()

        fixed_links = [
            {
                "original_url": "https://x.com/user/status/123",
                "fixed_url": "https://fxtwitter.com/user/status/123",
                "rule_name": "x",
                "label": "Tweet",
                "fixer_name": "FxTwitter",
            }
        ]
        original_message = types.SimpleNamespace(reply=reply)

        await self.cog._send_fixed_links(
            types.SimpleNamespace(send=send),
            types.SimpleNamespace(id=44),
            fixed_links,
            original_message=original_message,
        )

        self.assertEqual(
            sent,
            [
                (
                    "[Tweet](<https://x.com/user/status/123>) • "
                    "[@user](<https://x.com/user>) • "
                    "[FxTwitter](https://fxtwitter.com/user/status/123)",
                    "none",
                )
            ],
        )

    async def test_delayed_suppression_retry_failures_do_not_increment_errors(self):
        guild = types.SimpleNamespace(id=33)
        calls = 0

        async def edit(**kwargs):
            nonlocal calls
            calls += 1
            raise discord.NotFound()

        message = types.SimpleNamespace(edit=edit)

        await self.cog._suppress_message_embeds(
            message,
            guild,
            warn_on_failure=False,
            count_success=False,
            count_error=False,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(await self.cog.config.guild(guild).suppress_error_count(), 0)

    async def test_suppress_message_uses_edit_suppress_keyword(self):
        guild = types.SimpleNamespace(id=36)
        edit_calls = []
        suppress_calls = []

        async def suppress_embeds(value):
            suppress_calls.append(value)

        async def edit(**kwargs):
            edit_calls.append(kwargs)

        message = types.SimpleNamespace(suppress_embeds=suppress_embeds, edit=edit)

        result = await self.cog._suppress_message_embeds(
            message,
            guild,
            warn_on_failure=False,
            count_success=True,
            count_error=True,
        )

        self.assertTrue(result)
        self.assertEqual(edit_calls, [{"suppress": True}])
        self.assertEqual(suppress_calls, [])
        self.assertEqual(await self.cog.config.guild(guild).suppressed_count(), 1)

    async def test_suppress_message_falls_back_to_suppress_embeds_method(self):
        guild = types.SimpleNamespace(id=42)
        calls = []

        async def suppress_embeds(value):
            calls.append(value)

        message = types.SimpleNamespace(suppress_embeds=suppress_embeds)

        result = await self.cog._suppress_message_embeds(
            message,
            guild,
            warn_on_failure=False,
            count_success=True,
            count_error=True,
        )

        self.assertTrue(result)
        self.assertEqual(calls, [True])
        self.assertEqual(await self.cog.config.guild(guild).suppressed_count(), 1)

    async def test_listener_still_suppresses_when_send_fails(self):
        edits = []

        async def send(message, allowed_mentions=None):
            raise discord.HTTPException()

        async def edit(**kwargs):
            edits.append(kwargs)

        guild = types.SimpleNamespace(id=2)
        await self._enable(guild.id)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://instagram.com/reel/abc/",
            channel=types.SimpleNamespace(send=send),
            edit=edit,
        )

        await self.cog.on_message_without_command(message)

        self.assertEqual(edits, [{"suppress": True}])
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.repost_count(), 0)
        self.assertEqual(await conf.send_error_count(), 1)
        self.assertEqual(await conf.suppressed_count(), 1)

    async def test_listener_counts_suppress_failures(self):
        sent = []
        scheduled = []

        async def send(message, allowed_mentions=None):
            sent.append((message, allowed_mentions))

        async def edit(**kwargs):
            raise discord.Forbidden()

        guild = types.SimpleNamespace(id=3)
        await self._enable(guild.id)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://reddit.com/r/test/comments/123/title/",
            channel=types.SimpleNamespace(send=send),
            edit=edit,
        )
        self.cog._schedule_suppress_retries = lambda message, guild: scheduled.append(message)

        await self.cog.on_message_without_command(message)

        self.assertEqual(
            sent[0],
            (
                "[Reddit](<https://reddit.com/r/test/comments/123/title/>) • "
                "[@test](<https://reddit.com/r/test>) • "
                "[vxreddit](https://vxreddit.com/r/test/comments/123/title/)",
                "none",
            ),
        )
        self.assertIn("Manage Messages", sent[1][0])
        self.assertEqual(sent[1][1], "none")
        conf = self.cog.config.guild(guild)
        self.assertEqual(await conf.repost_count(), 1)
        self.assertEqual(await conf.suppressed_count(), 0)
        self.assertEqual(await conf.suppress_error_count(), 1)
        self.assertEqual(scheduled, [])

    async def test_suppress_failure_notice_is_throttled_per_channel(self):
        sent = []

        async def send(message, allowed_mentions=None):
            sent.append(message)

        async def edit(**kwargs):
            raise discord.Forbidden()

        guild = types.SimpleNamespace(id=31)
        await self._enable(guild.id)
        channel = types.SimpleNamespace(id=3100, send=send)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False),
            guild=guild,
            content="https://x.com/user/status/123",
            channel=channel,
            edit=edit,
        )
        original_now = self.cog._now
        values = iter([100.0, 101.0])
        self.cog._now = lambda: next(values)
        try:
            await self.cog.on_message_without_command(message)
            await self.cog.on_message_without_command(message)
        finally:
            self.cog._now = original_now

        self.assertEqual(
            sent[0],
            "[Tweet](<https://x.com/user/status/123>) • "
            "[@user](<https://x.com/user>) • "
            "[FxTwitter](https://fxtwitter.com/user/status/123)",
        )
        self.assertIn("Manage Messages", sent[1])
        self.assertEqual(
            sent[2],
            "[Tweet](<https://x.com/user/status/123>) • "
            "[@user](<https://x.com/user>) • "
            "[FxTwitter](https://fxtwitter.com/user/status/123)",
        )
        self.assertEqual(len(sent), 3)

    async def test_embedfix_group_sends_embed_panel_with_dropdowns(self):
        sent = []
        guild = types.SimpleNamespace(id=20)

        async def send(**kwargs):
            sent.append(kwargs)
            return types.SimpleNamespace()

        ctx = types.SimpleNamespace(
            guild=guild,
            author=types.SimpleNamespace(id=200),
            clean_prefix="!",
            send=send,
        )

        await self.cog.embedfixset(ctx)

        self.assertEqual(sent[0]["embed"].title, "EmbedFix Settings")
        self.assertIsInstance(sent[0]["view"], embedfix_module.EmbedFixSettingsView)
        self.assertEqual(len(sent[0]["view"].children), 2)
        self.assertEqual(sent[0]["view"].children[0].kwargs["placeholder"], "EmbedFix settings...")
        self.assertEqual(sent[0]["view"].children[1].kwargs["placeholder"], "Show rule details...")

    async def test_panel_select_rejects_other_users(self):
        guild = types.SimpleNamespace(id=21)
        view = embedfix_module.EmbedFixSettingsView(
            self.cog,
            author_id=1,
            guild=guild,
            prefix="!",
            rules=self.cog._default_rules(),
        )
        select = view.children[0]
        select.values = ["toggle_enabled"]
        interaction, edits, messages = self._interaction(user_id=2)

        await select.callback(interaction)

        self.assertEqual(edits, [])
        self.assertEqual(messages, [("You can't use this menu.", True)])
        self.assertFalse(await self.cog.config.guild(guild).enabled())

    async def test_panel_select_toggles_enabled_and_suppression(self):
        guild = types.SimpleNamespace(id=22)
        view = embedfix_module.EmbedFixSettingsView(
            self.cog,
            author_id=1,
            guild=guild,
            prefix="!",
            rules=self.cog._default_rules(),
        )
        select = view.children[0]
        interaction, edits, messages = self._interaction(user_id=1)

        select.values = ["toggle_enabled"]
        await select.callback(interaction)
        select.values = ["toggle_suppression"]
        await select.callback(interaction)

        conf = self.cog.config.guild(guild)
        self.assertTrue(await conf.enabled())
        self.assertFalse(await conf.suppress_original())
        self.assertEqual(messages, [])
        self.assertEqual(edits[-1]["embed"].title, "EmbedFix Settings")

    async def test_rule_select_shows_rule_detail_embed(self):
        guild = types.SimpleNamespace(id=23)
        view = embedfix_module.EmbedFixSettingsView(
            self.cog,
            author_id=1,
            guild=guild,
            prefix="!",
            rules=self.cog._default_rules(),
        )
        select = view.children[1]
        select.values = ["x"]
        interaction, edits, messages = self._interaction(user_id=1)

        await select.callback(interaction)

        self.assertEqual(messages, [])
        self.assertEqual(edits[0]["embed"].title, "EmbedFix Rule: x")
        self.assertEqual(edits[0]["view"], view)

    async def test_panel_select_resets_rules_and_rebuilds_view(self):
        guild = types.SimpleNamespace(id=24)

        async def send(message):
            return None

        await self.cog.embedfixset_addrule(
            types.SimpleNamespace(guild=guild, send=send),
            "custom",
            "fixed.example",
            "source.example",
        )
        rules = await self.cog.config.guild(guild).rules()
        view = embedfix_module.EmbedFixSettingsView(
            self.cog,
            author_id=1,
            guild=guild,
            prefix="!",
            rules=rules,
        )
        select = view.children[0]
        select.values = ["reset_rules"]
        interaction, edits, messages = self._interaction(user_id=1)

        await select.callback(interaction)

        rules = await self.cog.config.guild(guild).rules()
        self.assertFalse(any(rule["name"] == "custom" for rule in rules))
        self.assertEqual(messages, [])
        self.assertEqual(edits[0]["embed"].title, "EmbedFix Rules")
        self.assertIsInstance(edits[0]["view"], embedfix_module.EmbedFixSettingsView)

    async def test_embedfixset_updates_config_and_rules(self):
        sent = []
        guild = types.SimpleNamespace(id=4)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, send=send)

        await self.cog.embedfixset_enable(ctx)
        await self.cog.embedfixset_suppress(ctx, False)
        await self.cog.embedfixset_maxlinks(ctx, 99)
        await self.cog.embedfixset_addrule(ctx, "threads", "fixthreads.net", "threads.net")
        await self.cog.embedfixset_disablerule(ctx, "threads")
        await self.cog.embedfixset_rules(ctx)
        await self.cog.embedfixset_show(ctx)
        await self.cog.embedfixset_stats(ctx)

        conf = self.cog.config.guild(guild)
        rules = await conf.rules()
        threads_rule = next(rule for rule in rules if rule["name"] == "threads")
        self.assertTrue(await conf.enabled())
        self.assertFalse(await conf.suppress_original())
        self.assertEqual(await conf.max_links(), embedfix_module.MAX_LINKS_LIMIT)
        self.assertFalse(threads_rule["enabled"])
        self.assertIn("EmbedFix enabled.", sent)
        self.assertIn("Original embed suppression set to `False`.", sent)
        self.assertIn("Max fixed links per message set to `10`.", sent)
        self.assertIn("Rule `threads` saved: `threads.net` -> `fixthreads.net`.", sent)
        self.assertIn("Rule `threads` disabled.", sent)
        self.assertIn("`threads` [off]: `threads.net` -> `fixthreads.net`", sent[-3])
        self.assertIn("Enabled: `True`", sent[-2])
        self.assertIn("Messages detected: `0`", sent[-1])

    async def test_embedfixset_remove_and_reset_rules(self):
        sent = []
        guild = types.SimpleNamespace(id=5)

        async def send(message):
            sent.append(message)

        ctx = types.SimpleNamespace(guild=guild, send=send)

        await self.cog.embedfixset_addrule(ctx, "custom", "fixed.example", "source.example")
        await self.cog.embedfixset_removerule(ctx, "custom")
        await self.cog.embedfixset_resetrules(ctx)

        rules = await self.cog.config.guild(guild).rules()
        self.assertFalse(any(rule["name"] == "custom" for rule in rules))
        self.assertEqual(len(rules), len(embedfix_module.DEFAULT_RULES))
        self.assertEqual(sent[-2], "Rule `custom` removed.")
        self.assertEqual(sent[-1], "EmbedFix rules reset to defaults.")
