# ImplingFinder Access Reactions Design

## Goal

Add reaction-based role assignment for existing Discord access messages used by
private ImplingFinder spawn channels. Server admins should be able to map a
reaction on an already-posted message to a Discord role. When a user adds that
reaction, the cog grants the role; when the user removes that reaction, the cog
removes the role.

This keeps the current manually written access message and reaction set in
place. The cog only manages the role assignment side of that message.

## Non-Goals

- Do not create, edit, or delete the access message.
- Do not manage channel permission overwrites.
- Do not replace Discord's built-in role hierarchy or permission checks.
- Do not change spawn polling, feed routing, despawn handling, screenshots, or
  dashboard behavior.
- Do not store per-user access state. Discord role membership remains the source
  of truth.

## Admin Configuration

Add an `access` subgroup under the existing `implingset` admin command group:

```text
[p]implingset access add <message_id> <emoji> <role>
[p]implingset access remove <message_id> <emoji>
[p]implingset access list
```

The `<message_id>` is the existing access-panel message. The `<emoji>` is the
reaction users click. The `<role>` is the role that unlocks the matching private
spawn channel.

The `add` command will store or replace the mapping for that message and emoji.
The `remove` command will delete that single mapping and clean up the message
entry if no mappings remain. The `list` command will show configured mappings
for the current guild.

## Stored Data

Store mappings per guild in Red Config:

```text
access_reactions = {
  "<message_id>": {
    "<emoji_key>": "<role_id>"
  }
}
```

Emoji keys must be stable for Discord raw reaction events:

- Unicode emoji: the literal emoji string.
- Custom emoji: the raw payload string form when available, such as
  `<:name:id>` or `<a:name:id>`.

The config is separate from existing `channels`, `puro_channel`, `seen`, and
`active_messages` state.

## Runtime Behavior

Add listeners for raw reaction events:

- `on_raw_reaction_add`
- `on_raw_reaction_remove`

For each event, the cog will:

1. Ignore events without a guild ID or message ID.
2. Load the current guild's `access_reactions` config.
3. Normalize the payload emoji to the configured emoji key.
4. If the message ID and emoji key match, look up the guild, member, and role.
5. Ignore bot members, including the bot itself.
6. On reaction add, call `member.add_roles(role, reason=...)`.
7. On reaction remove, call `member.remove_roles(role, reason=...)`.

For reaction-add payloads, use `payload.member` when Discord provides it. For
reaction-remove payloads, resolve the member from the guild cache and fall back
to a guild member fetch if the API is available.

The listeners only act on configured message IDs. Reactions on spawn posts,
despawn notices, dashboard output, or unrelated server messages are ignored.

## Error Handling

The cog will log and return without raising for expected Discord-side failures:

- Guild not found.
- Member not found or unavailable.
- Role not found.
- Missing permissions, role hierarchy failure, or Discord API errors while
  adding/removing a role.

The setup documentation will state that the bot needs `Manage Roles` and that
the bot's top role must be above the managed access roles.

## Testing

Use repo-local `unittest` tests with the existing Discord and Red stubs. Add
focused tests for:

- Config defaults include `access_reactions`.
- `implingset access add` stores a message/emoji/role mapping.
- `implingset access remove` deletes one mapping and cleans up empty message
  entries.
- Reaction add grants the configured role.
- Reaction remove removes the configured role.
- Unrelated message IDs, unrelated emoji, bot users, missing members, and
  missing roles are ignored without raising.

The tests should not require a live Discord bot, Discord connection, or network
access.

## Documentation

Update `implingfinder/README.md` with a short access-message setup section and
include:

- The new commands.
- That admins keep using their existing access message.
- The `Manage Roles` and role hierarchy requirement.
- A reminder that channel access still comes from Discord role permissions.

## Rollout Notes

After deployment, configure the current access message with one mapping per
private impling role. Existing users who already reacted should keep their
current manually assigned roles until Discord sends future reaction events; the
cog does not backfill all historical reactions in this design.
