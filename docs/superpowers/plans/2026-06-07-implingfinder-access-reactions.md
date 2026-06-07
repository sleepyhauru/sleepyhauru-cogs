# ImplingFinder Access Reactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add admin-configured reaction role assignment for existing ImplingFinder Discord access messages.

**Architecture:** Keep access-role state in per-guild Red Config under a new `access_reactions` mapping. Add `implingset access` admin commands to manage mappings, then handle Discord raw reaction add/remove events by matching guild, message ID, and normalized emoji before granting or removing the configured role.

**Tech Stack:** Python 3, Red-DiscordBot cog APIs, discord.py raw reaction payloads, repo-local `unittest` tests with `tests/support.py` stubs.

---

## File Structure

- Modify `implingfinder/implingfinder.py`: add `access_reactions` guild config, emoji normalization helpers, access admin commands, and raw reaction listeners.
- Modify `tests/support.py`: add a lightweight `discord.Role` stub, make `discord.PartialEmoji` stringify like discord.py, and allow nested command groups in the Red command stub.
- Modify `tests/test_implingfinder_import.py`: add focused command/config and reaction-role behavior tests.
- Modify `implingfinder/README.md`: document existing-message access setup, commands, and Discord permission requirements.
- Modify `implingfinder/info.json`: mention per-guild access reaction mappings in the data statement.

No changes are needed in `implingfinder/core.py` because this behavior depends on Discord guild state, not pure impling parsing or routing.

---

### Task 1: Access Command Configuration

**Files:**
- Modify: `tests/support.py`
- Modify: `tests/test_implingfinder_import.py`
- Modify: `implingfinder/implingfinder.py`

- [ ] **Step 1: Extend the test stubs for access command imports**

In `tests/support.py`, add a `Role` stub next to `Member` and `User`, add `PartialEmoji.__str__`, register `discord.Role`, and update `group()` so a decorated group can also define a nested subgroup.

```python
        class Member:
            pass

        class Role:
            pass

        class User:
            pass
```

```python
            def __str__(self):
                if self.id is None:
                    return self.name
                prefix = "a" if self.animated else ""
                return f"<{prefix}:{self.name}:{self.id}>"
```

```python
        discord.Member = Member
        discord.Role = Role
        discord.User = User
```

Replace the existing `group()` helper with this nested-group-capable version:

```python
        def group(*args, **kwargs):
            def wrap(func):
                def subcommand_decorator(*dargs, **dkwargs):
                    def subwrap(subfunc):
                        return subfunc

                    return subwrap

                def subgroup_decorator(*dargs, **dkwargs):
                    def subwrap(subfunc):
                        subfunc.command = subcommand_decorator
                        subfunc.group = subgroup_decorator
                        return subfunc

                    return subwrap

                func.command = subcommand_decorator
                func.group = subgroup_decorator
                return func

            return wrap
```

- [ ] **Step 2: Write failing tests for access config defaults and commands**

In `tests/test_implingfinder_import.py`, extend `test_cog_module_imports_and_registers_defaults_with_dependency_stubs`:

```python
        self.assertEqual(cog.config._guild_defaults["access_reactions"], {})
```

Add this test near the existing command tests:

```python
    async def test_access_commands_store_list_and_remove_mapping(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123)
        replies = []

        async def send(message):
            replies.append(message)

        ctx = types.SimpleNamespace(guild=guild, send=send)
        role = types.SimpleNamespace(id=987, mention="@Crystal", name="Crystal")

        await cog.implingset_access_add(ctx, 555, "🦋", role)
        await cog.implingset_access_list(ctx)
        await cog.implingset_access_remove(ctx, 555, "🦋")

        self.assertEqual(await cog.config.guild(guild).access_reactions(), {})
        self.assertEqual(
            replies,
            [
                "Reaction `🦋` on message `555` will manage @Crystal.",
                "Access reactions:\n- message `555` `🦋` -> <@&987>",
                "Removed access reaction `🦋` from message `555`.",
            ],
        )
```

Add this custom-emoji normalization test near the access command test:

```python
    async def test_access_command_normalizes_custom_emoji_mapping(self):
        module = load_module("implingfinder.implingfinder")
        cog = module.ImplingFinder(bot=types.SimpleNamespace(user=None))
        guild = types.SimpleNamespace(id=123)

        async def send(_message):
            return None

        ctx = types.SimpleNamespace(guild=guild, send=send)
        role = types.SimpleNamespace(id=988, mention="@Dragon", name="Dragon")

        await cog.implingset_access_add(ctx, "556", "<:dragon:123456789012345678>", role)

        self.assertEqual(
            await cog.config.guild(guild).access_reactions(),
            {"556": {"<:dragon:123456789012345678>": "988"}},
        )
```

- [ ] **Step 3: Run the focused tests and verify they fail for missing behavior**

Run:

```bash
python3 -m unittest \
  tests.test_implingfinder_import.CogImportTest.test_cog_module_imports_and_registers_defaults_with_dependency_stubs \
  tests.test_implingfinder_import.CogImportTest.test_access_commands_store_list_and_remove_mapping \
  tests.test_implingfinder_import.CogImportTest.test_access_command_normalizes_custom_emoji_mapping
```

Expected: FAIL because `access_reactions` is not registered and `implingset_access_add` does not exist yet.

- [ ] **Step 4: Add config default, emoji helpers, and access commands**

In `implingfinder/implingfinder.py`, add `re` to the imports:

```python
import re
```

Add constants near the other module constants:

```python
ACCESS_ROLE_REASON = "ImplingFinder access reaction"
CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_]+):(\d+)>$")
```

Add the new guild config default in `__init__`:

```python
            access_reactions={},
```

Add these helper methods after `_normalize_puro_channel`:

```python
    def _access_emoji_key(self, emoji: Any) -> str:
        emoji_id = getattr(emoji, "id", None)
        if emoji_id is not None:
            prefix = "a" if bool(getattr(emoji, "animated", False)) else ""
            name = str(getattr(emoji, "name", "")).strip()
            if not name:
                return ""
            return f"<{prefix}:{name}:{int(emoji_id)}>"

        value = str(getattr(emoji, "name", emoji)).strip()
        match = CUSTOM_EMOJI_RE.match(value)
        if match is None:
            return value
        prefix = "a" if match.group(1) else ""
        return f"<{prefix}:{match.group(2)}:{match.group(3)}>"

    def _normalize_access_reactions(self, mappings: Mapping[str, Any]) -> dict[str, dict[str, str]]:
        normalized: dict[str, dict[str, str]] = {}
        for message_id, emoji_roles in dict(mappings or {}).items():
            try:
                message_key = str(int(message_id))
            except (TypeError, ValueError):
                continue
            clean_emoji_roles: dict[str, str] = {}
            for emoji, role_id in dict(emoji_roles or {}).items():
                emoji_key = self._access_emoji_key(emoji)
                if not emoji_key:
                    continue
                try:
                    clean_emoji_roles[emoji_key] = str(int(role_id))
                except (TypeError, ValueError):
                    continue
            if clean_emoji_roles:
                normalized[message_key] = clean_emoji_roles
        return normalized

    def _role_mention(self, role_id: str) -> str:
        return f"<@&{int(role_id)}>"
```

Add this command group after `implingset_removechannel` and before `implingset_list`:

```python
    @implingset.group(name="access", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access(self, ctx: commands.Context) -> None:
        """Configure reaction role access for existing impling access messages."""
        await self.implingset_access_list(ctx)

    @implingset_access.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access_add(
        self,
        ctx: commands.Context,
        message_id: int,
        emoji: str,
        role: discord.Role,
    ) -> None:
        """Map a reaction on an existing access message to a role."""
        message_key = str(int(message_id))
        emoji_key = self._access_emoji_key(emoji)
        if not emoji_key:
            await ctx.send("Provide a valid emoji.")
            return

        async with self.config.guild(ctx.guild).access_reactions() as access_reactions:
            access_reactions.setdefault(message_key, {})[emoji_key] = str(int(role.id))

        role_display = getattr(role, "mention", self._role_mention(str(role.id)))
        await ctx.send(f"Reaction `{emoji_key}` on message `{message_key}` will manage {role_display}.")

    @implingset_access.command(name="remove")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access_remove(
        self,
        ctx: commands.Context,
        message_id: int,
        emoji: str,
    ) -> None:
        """Remove one access reaction mapping."""
        message_key = str(int(message_id))
        emoji_key = self._access_emoji_key(emoji)

        async with self.config.guild(ctx.guild).access_reactions() as access_reactions:
            message_mappings = access_reactions.get(message_key)
            if not isinstance(message_mappings, dict) or emoji_key not in message_mappings:
                await ctx.send(f"No access reaction `{emoji_key}` is configured for message `{message_key}`.")
                return
            message_mappings.pop(emoji_key, None)
            if not message_mappings:
                access_reactions.pop(message_key, None)

        await ctx.send(f"Removed access reaction `{emoji_key}` from message `{message_key}`.")

    @implingset_access.command(name="list")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def implingset_access_list(self, ctx: commands.Context) -> None:
        """List configured access reaction mappings."""
        raw_access = await self.config.guild(ctx.guild).access_reactions()
        access_reactions = self._normalize_access_reactions(raw_access)
        if not access_reactions:
            await ctx.send("No access reactions configured.")
            return

        lines = ["Access reactions:"]
        for message_id in sorted(access_reactions, key=int):
            for emoji_key, role_id in sorted(access_reactions[message_id].items()):
                lines.append(
                    f"- message `{message_id}` `{emoji_key}` -> {self._role_mention(role_id)}"
                )
        await ctx.send("\n".join(lines))
```

- [ ] **Step 5: Run the focused tests and verify they pass**

Run:

```bash
python3 -m unittest \
  tests.test_implingfinder_import.CogImportTest.test_cog_module_imports_and_registers_defaults_with_dependency_stubs \
  tests.test_implingfinder_import.CogImportTest.test_access_commands_store_list_and_remove_mapping \
  tests.test_implingfinder_import.CogImportTest.test_access_command_normalizes_custom_emoji_mapping
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add tests/support.py tests/test_implingfinder_import.py implingfinder/implingfinder.py
git commit -m "feat: add impling access reaction commands"
```

---

### Task 2: Raw Reaction Role Handling

**Files:**
- Modify: `tests/test_implingfinder_import.py`
- Modify: `implingfinder/implingfinder.py`

- [ ] **Step 1: Write failing tests for reaction add, reaction remove, and ignored events**

Add these tests near the access command tests in `tests/test_implingfinder_import.py`:

```python
    async def test_access_reaction_add_grants_configured_role(self):
        module = load_module("implingfinder.implingfinder")
        calls = []

        class Member:
            id = 42
            bot = False

            async def add_roles(self, role, *, reason=None):
                calls.append(("add", role.id, reason))

            async def remove_roles(self, role, *, reason=None):
                calls.append(("remove", role.id, reason))

        role = types.SimpleNamespace(id=987)
        member = Member()
        guild = types.SimpleNamespace(
            id=123,
            get_role=lambda role_id: role if role_id == 987 else None,
            get_member=lambda user_id: member if user_id == 42 else None,
        )
        bot = types.SimpleNamespace(
            user=types.SimpleNamespace(id=999),
            get_guild=lambda guild_id: guild if guild_id == 123 else None,
        )
        cog = module.ImplingFinder(bot=bot)
        await cog.config.guild(guild).access_reactions.set({"555": {"🦋": "987"}})
        payload = types.SimpleNamespace(
            guild_id=123,
            message_id=555,
            user_id=42,
            emoji="🦋",
            member=member,
        )

        await cog.on_raw_reaction_add(payload)

        self.assertEqual(calls, [("add", 987, "ImplingFinder access reaction")])
```

```python
    async def test_access_reaction_remove_fetches_member_and_removes_role(self):
        module = load_module("implingfinder.implingfinder")
        calls = []

        class Member:
            id = 42
            bot = False

            async def add_roles(self, role, *, reason=None):
                calls.append(("add", role.id, reason))

            async def remove_roles(self, role, *, reason=None):
                calls.append(("remove", role.id, reason))

        role = types.SimpleNamespace(id=988)
        member = Member()

        async def fetch_member(user_id):
            return member if user_id == 42 else None

        guild = types.SimpleNamespace(
            id=123,
            get_role=lambda role_id: role if role_id == 988 else None,
            get_member=lambda _user_id: None,
            fetch_member=fetch_member,
        )
        bot = types.SimpleNamespace(
            user=types.SimpleNamespace(id=999),
            get_guild=lambda guild_id: guild if guild_id == 123 else None,
        )
        cog = module.ImplingFinder(bot=bot)
        await cog.config.guild(guild).access_reactions.set(
            {"556": {"<:dragon:123456789012345678>": "988"}}
        )
        payload = types.SimpleNamespace(
            guild_id=123,
            message_id=556,
            user_id=42,
            emoji=types.SimpleNamespace(id=123456789012345678, name="dragon", animated=False),
            member=None,
        )

        await cog.on_raw_reaction_remove(payload)

        self.assertEqual(calls, [("remove", 988, "ImplingFinder access reaction")])
```

```python
    async def test_access_reaction_ignores_unconfigured_and_unusable_events(self):
        module = load_module("implingfinder.implingfinder")
        calls = []

        class Member:
            id = 42
            bot = False

            async def add_roles(self, role, *, reason=None):
                calls.append(("add", role.id, reason))

            async def remove_roles(self, role, *, reason=None):
                calls.append(("remove", role.id, reason))

        role = types.SimpleNamespace(id=987)
        member = Member()
        bot_member = types.SimpleNamespace(id=43, bot=True, add_roles=self._async_return(None))
        guild = types.SimpleNamespace(
            id=123,
            get_role=lambda role_id: role if role_id == 987 else None,
            get_member=lambda user_id: {42: member, 43: bot_member}.get(user_id),
        )
        bot = types.SimpleNamespace(
            user=types.SimpleNamespace(id=999),
            get_guild=lambda guild_id: guild if guild_id == 123 else None,
        )
        cog = module.ImplingFinder(bot=bot)
        await cog.config.guild(guild).access_reactions.set({"555": {"🦋": "987"}})

        await cog.on_raw_reaction_add(
            types.SimpleNamespace(guild_id=123, message_id=999, user_id=42, emoji="🦋", member=member)
        )
        await cog.on_raw_reaction_add(
            types.SimpleNamespace(guild_id=123, message_id=555, user_id=42, emoji="❌", member=member)
        )
        await cog.on_raw_reaction_add(
            types.SimpleNamespace(guild_id=123, message_id=555, user_id=43, emoji="🦋", member=bot_member)
        )
        await cog.config.guild(guild).access_reactions.set({"555": {"🦋": "123456"}})
        await cog.on_raw_reaction_add(
            types.SimpleNamespace(guild_id=123, message_id=555, user_id=42, emoji="🦋", member=member)
        )

        self.assertEqual(calls, [])
```

- [ ] **Step 2: Run the reaction tests and verify they fail for missing listeners**

Run:

```bash
python3 -m unittest \
  tests.test_implingfinder_import.CogImportTest.test_access_reaction_add_grants_configured_role \
  tests.test_implingfinder_import.CogImportTest.test_access_reaction_remove_fetches_member_and_removes_role \
  tests.test_implingfinder_import.CogImportTest.test_access_reaction_ignores_unconfigured_and_unusable_events
```

Expected: FAIL because `on_raw_reaction_add` and `on_raw_reaction_remove` do not exist yet.

- [ ] **Step 3: Add listener and member resolution implementation**

In `implingfinder/implingfinder.py`, add these methods after `_cog_disabled_in_guild` and before `_normalize_channels`:

```python
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        await self._handle_access_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload) -> None:
        await self._handle_access_reaction(payload, add=False)

    async def _handle_access_reaction(self, payload, *, add: bool) -> None:
        guild_id = getattr(payload, "guild_id", None)
        message_id = getattr(payload, "message_id", None)
        user_id = getattr(payload, "user_id", None)
        if guild_id is None or message_id is None or user_id is None:
            return

        bot_user = getattr(self.bot, "user", None)
        if getattr(bot_user, "id", None) == user_id:
            return

        access_reactions = self._normalize_access_reactions(
            await self.config.guild_from_id(int(guild_id)).access_reactions()
        )
        message_mappings = access_reactions.get(str(int(message_id)))
        if not message_mappings:
            return

        emoji_key = self._access_emoji_key(getattr(payload, "emoji", ""))
        role_id = message_mappings.get(emoji_key)
        if role_id is None:
            return

        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(int(guild_id)) if get_guild is not None else None
        if guild is None:
            log.warning(
                "Impling Finder access reaction matched guild %s but the guild was unavailable",
                guild_id,
            )
            return

        member = await self._member_for_access_payload(guild, payload)
        if member is None or bool(getattr(member, "bot", False)):
            return

        role = self._role_for_access_reaction(guild, role_id)
        if role is None:
            log.warning(
                "Impling Finder access reaction role %s was unavailable in guild %s",
                role_id,
                guild_id,
            )
            return

        try:
            if add:
                await member.add_roles(role, reason=ACCESS_ROLE_REASON)
            else:
                await member.remove_roles(role, reason=ACCESS_ROLE_REASON)
        except (discord.Forbidden, discord.HTTPException, discord.DiscordException):
            action = "grant" if add else "remove"
            log.warning(
                "Impling Finder failed to %s access role %s for user %s in guild %s",
                action,
                role_id,
                user_id,
                guild_id,
                exc_info=True,
            )

    async def _member_for_access_payload(self, guild, payload):
        member = getattr(payload, "member", None)
        if member is not None:
            return member

        user_id = int(getattr(payload, "user_id"))
        get_member = getattr(guild, "get_member", None)
        if get_member is not None:
            member = get_member(user_id)
            if member is not None:
                return member

        fetch_member = getattr(guild, "fetch_member", None)
        if fetch_member is None:
            return None
        try:
            return await fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, discord.DiscordException):
            log.warning(
                "Impling Finder access reaction member %s was unavailable in guild %s",
                user_id,
                getattr(guild, "id", "?"),
                exc_info=True,
            )
            return None

    def _role_for_access_reaction(self, guild, role_id: str):
        get_role = getattr(guild, "get_role", None)
        if get_role is None:
            return None
        try:
            return get_role(int(role_id))
        except (TypeError, ValueError):
            return None
```

- [ ] **Step 4: Run reaction tests and verify they pass**

Run:

```bash
python3 -m unittest \
  tests.test_implingfinder_import.CogImportTest.test_access_reaction_add_grants_configured_role \
  tests.test_implingfinder_import.CogImportTest.test_access_reaction_remove_fetches_member_and_removes_role \
  tests.test_implingfinder_import.CogImportTest.test_access_reaction_ignores_unconfigured_and_unusable_events
```

Expected: PASS.

- [ ] **Step 5: Run all ImplingFinder import tests**

Run:

```bash
python3 -m unittest tests.test_implingfinder_import
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add tests/test_implingfinder_import.py implingfinder/implingfinder.py
git commit -m "feat: manage impling access roles from reactions"
```

---

### Task 3: README, Metadata, and Verification

**Files:**
- Modify: `implingfinder/README.md`
- Modify: `implingfinder/info.json`

- [ ] **Step 1: Update README command list and setup docs**

In `implingfinder/README.md`, add this section after the Puro-Puro setup block and before `## Commands`:

````markdown
## Private Channel Access

If your impling spawn channels are private, keep using your existing access
message and configure its reactions to manage the roles that unlock those
channels:

```text
[p]implingset access add <message_id> <emoji> @CrystalImplings
[p]implingset access add <message_id> <emoji> @DragonImplings
[p]implingset access add <message_id> <emoji> @LuckyImplings
```

When a user adds one of those reactions, the cog grants the configured role.
When they remove the reaction, the cog removes the role. Channel visibility
still comes from Discord role permissions, so configure each private channel to
allow the matching role.

The bot needs `Manage Roles`, and the bot's highest role must be above the
roles it manages. The cog does not backfill old reactions; users who already
reacted before setup need to remove and re-add the reaction, or have the role
assigned manually once.
````

Add these bullets to the `## Commands` list:

```markdown
- `[p]implingset access add <message_id> <emoji> <role>`
- `[p]implingset access remove <message_id> <emoji>`
- `[p]implingset access list`
```

- [ ] **Step 2: Update cog metadata data statement**

In `implingfinder/info.json`, update `end_user_data_statement` to mention access
reaction role mappings:

```json
"end_user_data_statement": "This cog stores per-guild feed settings, access reaction role mappings, recent impling spawn dedupe keys, and operational performance metrics including guild/channel names. Detailed metrics are retained for 7 days and hourly aggregates for 30 days. It does not store Discord user data.",
```

- [ ] **Step 3: Run README and metadata diff check**

Run:

```bash
git diff -- implingfinder/README.md
git diff -- implingfinder/info.json
```

Expected: The README diff only adds the private channel access section and access command bullets. The metadata diff only updates `end_user_data_statement`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_implingfinder_import
```

Expected: PASS.

- [ ] **Step 5: Run required verification**

Run:

```bash
python3 -m unittest discover tests
python3 -m compileall -q implingfinder
python3 -m json.tool implingfinder/info.json >/dev/null
python3 -m json.tool implingfinder/data/map_labels.json >/dev/null
git diff --check
git status --short --branch
```

Expected:

- `python3 -m unittest discover tests`: all tests pass.
- `python3 -m compileall -q implingfinder`: no output and exit code 0.
- JSON validation commands: no output and exit code 0.
- `git diff --check`: no output and exit code 0.
- `git status --short --branch`: only ImplingFinder access-reaction files are modified.

- [ ] **Step 6: Commit Task 3**

```bash
git add implingfinder/README.md implingfinder/info.json
git commit -m "docs: document impling access reactions"
```

---

### Task 4: Final Integration Check

**Files:**
- Inspect: `implingfinder/implingfinder.py`
- Inspect: `tests/test_implingfinder_import.py`
- Inspect: `implingfinder/README.md`
- Inspect: `implingfinder/info.json`

- [ ] **Step 1: Inspect final diff**

Run:

```bash
git show --stat --oneline HEAD~2..HEAD
git diff --stat origin/main..HEAD
git status --short --branch
```

Expected: The code and docs changes are limited to `implingfinder/implingfinder.py`, `tests/support.py`, `tests/test_implingfinder_import.py`, `implingfinder/README.md`, `implingfinder/info.json`, and the committed spec/plan docs.

- [ ] **Step 2: Run the full required gate one more time**

Run:

```bash
python3 -m unittest discover tests
python3 -m compileall -q implingfinder
python3 -m json.tool implingfinder/info.json >/dev/null
python3 -m json.tool implingfinder/data/map_labels.json >/dev/null
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 3: Prepare deployment notes**

Use these notes in the completion response:

```text
Configure each mapping against the existing access message:
[p]implingset access add <message_id> <emoji> @CrystalImplings
[p]implingset access add <message_id> <emoji> @DragonImplings
[p]implingset access add <message_id> <emoji> @LuckyImplings

The bot needs Manage Roles, and its top role must be above those access roles.
Users who already reacted before setup need to remove and re-add the reaction
or have the role assigned manually once.
```
