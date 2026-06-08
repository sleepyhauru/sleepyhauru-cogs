# ImplingFinder

`implingfinder` is a Red-DiscordBot cog for posting recent OSRS rare impling sightings to Discord channels.

It uses the same Oracle ORDS backend used by the RuneLite Plugin Hub Impling Finder plugin:

- All/recent GET endpoint: `https://puos0bfgxc2lno5-implingdb.adb.us-phoenix-1.oraclecloudapps.com/ords/impling/implingdev/dev`
- NPC ID GET endpoint prefix: `https://puos0bfgxc2lno5-implingdb.adb.us-phoenix-1.oraclecloudapps.com/ords/impling/implingdev/dev/`
- RuneLite plugin repo: <https://github.com/Hablapatabla/ImplingFinder>

This Discord cog is read-only. It does not POST spawn data.

## Tracked Implings

- Magpie impling: `1642`
- Ninja impling: `1643`
- Crystal impling: `8741`
- Dragon impling: `1644`
- Lucky impling: `7233`

## Setup

Load the cog:

```text
[p]load implingfinder
```

Route Dragon and Lucky implings to one channel:

```text
[p]implingset addchannel #dragon-imps dragon lucky
```

Route all tracked rare implings to another channel:

```text
[p]implingset addchannel #rare-imps magpie ninja crystal dragon lucky
```

You can also use `all` or `rare`:

```text
[p]implingset addchannel #all-imps all
```

Enable polling:

```text
[p]implingset enable
```

Send Puro-Puro sightings to a dedicated channel:

```text
[p]implingset purochannel #puro-puro
[p]implingset puro true
```

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

## Commands

- `[p]implingset enable`
- `[p]implingset disable`
- `[p]implingset interval <seconds>` - minimum `5`, default `5`
- `[p]implingset maxage <seconds>` - default `900`
- `[p]implingset addchannel <#channel> <types...>`
- `[p]implingset puro <true|false>`
- `[p]implingset purochannel <#channel>`
- `[p]implingset removechannel <#channel>`
- `[p]implingset list`
- `[p]implingset screenshots <true|false>`
- `[p]implingset caughtemoji <emoji>` - default `✅`
- `[p]implingset endpoint <url>` - must be `https`
- `[p]implingset resetendpoint`
- `[p]implingset access add <message_id> <emoji> <role>`
- `[p]implingset access remove <message_id> <emoji>`
- `[p]implingset access list`
- `[p]implingset clearseen`
- `[p]implingrecent [type=all] [count=10]`

Type aliases:

- `magpie`, `mag`
- `ninja`, `nin`
- `crystal`, `crys`
- `dragon`, `drag`, `dimp`
- `lucky`, `luck`, `limp`
- `all`, `rare`

## Screenshots

The backend does not provide real RuneLite screenshots. It only provides NPC ID, world, coordinates, plane, and discovered time.

If `[p]implingset screenshots true` is enabled, this cog sends the sighting post immediately, then queues a screenshot worker to edit the same Discord message with a `32x32` OSRS area centered on the sighting after the map is generated. A small matching impling image is centered on the reported game tile. If the map cannot be rendered, the screenshot queue is full, or the sighting message is already gone, the original Discord post is left in place without the attachment.

Discord posts say the impling spawned, link the `{type} Impling spawned` title to Explv at zoom 7, and use only Discord's relative timestamp for `Discovered`, such as "5 minutes ago", without showing the exact discovered time.

If the map tile cannot be downloaded, the cog falls back to a generated card containing only the impling name, world, and location. Neither attachment is a real in-game RuneLite screenshot. A future RuneLite companion plugin would be needed for real screenshot uploads.

Pillow is installed as a cog dependency.

## Duplicate and Despawn Handling

Each sighting is deduplicated by NPC ID, world, plane, and the official OSRS region ID:

```text
((x >> 6) << 8) | (y >> 6)
```

The backend can report the same moving impling several times with slightly different coordinates or timestamps, so the cog keeps the newest row for each region and avoids posting older rows from that region as duplicates.

When a tracked sighting is missing from the fresh sightings in the latest successful backend response, the cog edits the Discord message it posted for that sighting to say the impling despawned, keeps the screenshot in the embed, removes the stored active message ID, and deletes the despawn notice 30 seconds later. Messages are kept for retry if Discord rejects the edit because of permissions or a transient API error.

The bot adds a caught reaction to each spawn post. Any non-bot user who can see
the channel can click that reaction to mark the impling caught; the cog deletes
that spawn post and removes it from active tracking. Configure the reaction with:

```text
[p]implingset caughtemoji 🎯
```

After each successful backend response, new live sightings are posted before despawn and cleanup maintenance. Matching channel sends run concurrently, then post-poll maintenance is queued for a dedicated worker that marks despawns and runs feed cleanup in the background. Recurring feed cleanup is throttled to every 30 seconds per guild and deletes non-pinned bot and human messages from recent channel history when the bot has Manage Messages and Read Message History. Backend failures do not trigger despawn or feed cleanup.

On cog load or reload, the cog also performs a one-time feed scrub against stored active message IDs so configured feed channels are cleaned promptly. The authoritative despawn pass still requires a successful backend response before tracked active messages are deleted or cleared.

Puro-Puro sightings are disabled by default. When enabled and assigned to a Puro-Puro channel, sightings inside the Puro-Puro coordinate box post only to that dedicated channel and do not duplicate into normal type-routed channels. If Puro-Puro is disabled or no Puro-Puro channel is configured, those sightings are skipped.

For competitive hunting, set the interval to the minimum:

```text
[p]implingset interval 5
```

Each enabled guild has its own fixed-start poll runner, so the cog does not sleep
an extra 5 seconds after a slow backend fetch. If a backend read takes longer
than the configured interval, the next poll starts as soon as the previous poll
finishes.

## Location and Asset Sources

Discord posts display a human-readable location resolved from bundled mapped areas in `implingfinder/data/areas.json`. The resolver matches the old ImplingFinder script behavior: it returns the closest mapped area by x/y distance, ignoring plane and the optional `size` field. If no mapped areas are available, the location is shown as `Unknown area`.

The chunk background uses [Explv OSRS map tiles](https://github.com/Explv/osrs_map_tiles). Matching transparent impling images are bundled from the [Old School RuneScape Wiki](https://oldschool.runescape.wiki/).

## Performance Dashboard

ImplingFinder automatically starts a read-only performance dashboard on
`0.0.0.0:8765`. Put it behind a private reverse proxy and authentication layer;
the dashboard does not implement its own authentication.

The dashboard tracks backend fetches, poll processing, duplicate suppression,
routed sightings, map download/render work, Discord posting, screenshot attachment
edits, age-at-fetch latency, bot-after-fetch posting gap, discovery-to-post
latency, despawn edits, feed cleanup, errors, active backend backoffs, poll
runners, worker queue depth, event-loop lag, memory use, metrics queue health,
and database size.

Metrics are written through a bounded non-blocking queue so dashboard storage
cannot delay a sighting post. Individual events are retained for 7 days and
hourly aggregates are retained for 30 days in `metrics.sqlite3` under the cog's
persistent Red data directory.

HTTP routes are read-only:

- `/` - dashboard
- `/api/summary` - totals, latency summary, servers, and process health
- `/api/hourly` - hourly metric series
- `/api/events` - recent detailed events
- `/healthz` - dashboard and metrics-store health

The dashboard contains no feed controls, polling triggers, or destructive
actions. Detailed events include server/channel names, impling type, world, and
human-readable location, but do not expose coordinates, NPC ID, or plane.
