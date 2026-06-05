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
- `[p]implingset interval <seconds>` - minimum `10`, default `30`
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

If Pillow is installed and `[p]implingset screenshots true` is enabled, this cog attaches a generated Explv world-map crop centered on the sighting. The map image uses the same OSRS coordinates as the linked Explv map.

If the map tiles cannot be downloaded, the cog falls back to the older generated coordinate card. Neither attachment is a real in-game RuneLite screenshot. A future RuneLite companion plugin would be needed for real screenshot uploads.

Pillow is optional and is not listed as a required dependency.

## Duplicate and Despawn Handling

Each spawn is deduplicated by NPC ID, world, coordinates, plane, and discovered timestamp. The cog stores those keys per Discord server so the same backend row is not posted again on later polls.

When a tracked spawn disappears from the latest successful backend response, the cog deletes the Discord message it posted for that spawn and removes the stored message ID. Messages are kept for retry if Discord rejects the delete because of permissions or a transient API error.

For competitive hunting, set the interval to the minimum:

```text
[p]implingset interval 10
```
