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

## Commands

- `[p]implingset enable`
- `[p]implingset disable`
- `[p]implingset interval <seconds>` - minimum `5`, default `5`
- `[p]implingset maxage <seconds>` - default `900`
- `[p]implingset addchannel <#channel> <types...>`
- `[p]implingset removechannel <#channel>`
- `[p]implingset list`
- `[p]implingset screenshots <true|false>`
- `[p]implingset endpoint <url>` - must be `https`
- `[p]implingset resetendpoint`
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

If `[p]implingset screenshots true` is enabled, this cog attaches a `16x16` OSRS area centered on the sighting. A small matching impling image is placed on the reported game tile. The attached map does not display coordinates or plane. If the map cannot be rendered quickly, the Discord post is sent without waiting for the attachment.

If the map tile cannot be downloaded, the cog falls back to a generated card containing only the impling name, world, and location. Neither attachment is a real in-game RuneLite screenshot. A future RuneLite companion plugin would be needed for real screenshot uploads.

Pillow is installed as a cog dependency.

## Duplicate and Despawn Handling

Each sighting is deduplicated by NPC ID, world, plane, and the official OSRS region ID:

```text
((x >> 6) << 8) | (y >> 6)
```

The backend can report the same moving impling several times with slightly different coordinates or timestamps, so the cog keeps the newest row for each region and avoids posting older rows from that region as duplicates.

When a tracked sighting disappears from the latest successful backend response, the cog deletes the Discord message it posted for that sighting and removes the stored message ID. Messages are kept for retry if Discord rejects the delete because of permissions or a transient API error.

After each successful backend response, configured ImplingFinder feed channels are also cleaned so they only contain active live impling posts. The cleanup deletes non-pinned bot and human messages from recent channel history when the bot has Manage Messages and Read Message History. Backend failures do not trigger feed cleanup.

For competitive hunting, set the interval to the minimum:

```text
[p]implingset interval 5
```

## Location and Asset Sources

Discord posts display a human-readable location resolved from bundled [Explv map labels](https://github.com/Explv/Explv.github.io/blob/master/public/resources/map_labels.json). A label in the same region and plane is preferred; otherwise the nearest label is displayed with a `Near` prefix. If no label is available, the location is shown as `Unknown area`.

The chunk background uses [Explv OSRS map tiles](https://github.com/Explv/osrs_map_tiles). Matching transparent impling images are bundled from the [Old School RuneScape Wiki](https://oldschool.runescape.wiki/).

## Performance Dashboard

ImplingFinder automatically starts a read-only performance dashboard on
`0.0.0.0:8765`. Put it behind a private reverse proxy and authentication layer;
the dashboard does not implement its own authentication.

The dashboard tracks backend fetches, poll processing, duplicate suppression,
routed sightings, map download/render work, Discord posting, discovery-to-post
latency, despawn deletion, feed cleanup, errors, active backend backoffs, event-loop lag,
memory use, queue health, and database size.

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
