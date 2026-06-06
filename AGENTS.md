# ImplingFinder Agent Guide

## Scope

This root guide applies only to ImplingFinder work:

- `implingfinder/`
- `tests/test_implingfinder_core.py`
- `tests/test_implingfinder_import.py`
- ImplingFinder sections in `README.md`
- ImplingFinder plans and documentation under `docs/`

Leave unrelated cogs untouched unless the user explicitly expands the task.

## Current Product Contract

- Poll the read-only Oracle ORDS backend; never POST sighting data.
- Track Magpie, Ninja, Crystal, Dragon, and Lucky implings.
- Deduplicate sightings by NPC ID, world, plane, and official OSRS region ID:
  `((x >> 6) << 8) | (y >> 6)`.
- Edit tracked Discord messages to say the impling despawned after a successful
  backend response no longer contains the fresh sighting. Keep messages tracked
  when the edit fails transiently or from missing permissions.
- After a successful backend response, keep configured feed channels clean by
  deleting non-pinned messages that are not active live impling posts or recent
  despawn notices. This includes human messages and requires Manage Messages
  plus Read Message History. Backend failures must not trigger feed cleanup.
- On cog load or reload, perform a one-time feed scrub against stored active
  message IDs so configured feed channels are cleaned promptly. This startup
  scrub must not delete tracked active messages or clear active state without a
  successful backend response.
- Discord spawn posts display World, human-readable Location, coordinate
  hyperlink to Explv zoom 7, and relative Discovered only. Do not display NPC
  ID, plane, a source footer, or an absolute discovered time.
- Screenshot-enabled feed posts send the Discord message immediately, then edit
  that same message with the generated map attachment when rendering finishes.
  Screenshot attachments show the current `32x32` OSRS area centered on the
  sighting and place a matching impling icon centered on the reported game tile.
- Preserve migration support for previously stored exact dedupe keys and coarse
  area keys when changing sighting-state behavior.

## Architecture

- `implingfinder/core.py`: pure parsing, filtering, dedupe, official-region,
  location-resolution, and Explv coordinate helpers. Put new pure behavior here
  where practical.
- `implingfinder/implingfinder.py`: Red cog configuration, polling, Discord
  messages, tracked-message cleanup, state migration, network requests, and
  Pillow image rendering.
- `implingfinder/data/map_labels.json`: bundled compact Explv location labels.
- `implingfinder/assets/*.png`: matching transparent Magpie, Ninja, Crystal,
  Dragon, and Lucky impling images.
- `tests/test_implingfinder_core.py`: focused tests for pure behavior.
- `tests/test_implingfinder_import.py`: dependency-stubbed tests for cog and
  Discord behavior.

## Important Implementation Details

- The minimum polling interval is `5` seconds; default is `5`.
- Location resolution prefers the nearest same-plane label in the same official
  region, then `Near <nearest same-plane label>`, then `Unknown area`.
- Region-based sighting keys deliberately allow a moving impling to update
  coordinates without creating duplicate Discord posts inside the same region.
- Despawn marking must only follow a successful backend response. Backend
  failures must not mark or delete active Discord messages.
- Feed-channel cleanup must only follow a successful backend response. Do not
  delete pinned messages or recent despawn notices, and do not scan channels
  outside configured ImplingFinder feed channels. The only exception is the
  one-time startup scrub, which may clean non-pinned messages that are not
  stored as active message IDs.
- Map screenshots use an Explv zoom-10 crop, which corresponds to a `32x32`
  game-tile area. The matching impling asset is composited at the center of the
  final image.
- Plain-text fallbacks include the same coordinate hyperlink. Generated-card
  fallbacks must still avoid exposing coordinates, NPC ID, age, or plane.
- Keep bundled asset names aligned with `ImplingSpawn.type_key`:
  `magpie.png`, `ninja.png`, `crystal.png`, `dragon.png`, and `lucky.png`.
- The read-only performance dashboard starts automatically on `0.0.0.0:8765`.
  It is protected by the external Traefik/VoidAuth layer, not by cog routes.
- Metrics producers must use the bounded non-blocking queue. Never await a
  metrics write from fetch, processing, rendering, despawn, or posting paths.
  Screenshot attachment edit metrics must include guild, channel, impling type,
  world, human-readable location, render timing, edit timing, and end-to-end
  latency.
- Detailed metric events are retained for 7 days and hourly aggregates for 30
  days in `cog_data_path(self) / "metrics.sqlite3"`.
- Dashboard routes must remain GET-only and must not change settings, trigger
  polls, clear data, or expose coordinates, NPC ID, or plane.

## Change Workflow

1. Inspect `git status` and the relevant ImplingFinder files first.
2. Add or update focused tests before changing behavior.
3. Keep edits scoped to ImplingFinder and work with existing user changes.
4. For map-rendering changes, generate and visually inspect a representative
   chunk image and fallback card.
5. Update `implingfinder/README.md` when commands, behavior, data sources, or
   operational expectations change.
6. For dashboard changes, populate sample metrics and inspect desktop and mobile
   layouts before deployment.

## Required Verification

Run before claiming completion or committing:

```bash
python3 -m unittest discover tests
python3 -m compileall -q implingfinder
python3 -m json.tool implingfinder/info.json >/dev/null
python3 -m json.tool implingfinder/data/map_labels.json >/dev/null
git diff --check
```

Also inspect `git status --short --branch` and stage only relevant files.

## External References

- Backend and RuneLite plugin:
  <https://github.com/Hablapatabla/ImplingFinder>
- Explv labels:
  <https://github.com/Explv/Explv.github.io/blob/master/public/resources/map_labels.json>
- Explv map tiles:
  <https://github.com/Explv/osrs_map_tiles>
- OSRS Wiki impling assets:
  <https://oldschool.runescape.wiki/>
