# ImplingFinder AGENTS.md Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a root-level agent handoff guide that applies only to ImplingFinder work and preserves the current product, architecture, testing, and delivery decisions.

**Architecture:** Add one root-level `AGENTS.md` so future coding agents see the guide immediately when opening the repository. The guide will constrain its own scope to ImplingFinder files, describe the boundaries between pure core logic and Red cog integration, and provide exact verification and visual-review requirements.

**Tech Stack:** Markdown, Python unittest, Red-DiscordBot, aiohttp, Pillow, Explv map data.

---

### Task 1: Create and Verify the ImplingFinder Agent Guide

**Files:**
- Create: `AGENTS.md`
- Verify: `implingfinder/core.py`
- Verify: `implingfinder/implingfinder.py`
- Verify: `implingfinder/README.md`
- Verify: `tests/test_implingfinder_core.py`
- Verify: `tests/test_implingfinder_import.py`

- [x] **Step 1: Create the root-level guide**

Create `AGENTS.md` with these sections and rules:

```markdown
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
- Delete tracked Discord messages after a successful backend response no longer
  contains the sighting. Keep messages tracked when deletion fails transiently
  or from missing permissions.
- Discord posts display World, human-readable Location, Discovered, and Map.
  Do not display coordinates, NPC ID, age, plane, or a source footer.
- Screenshot attachments show exactly the current `8x8` OSRS chunk and place a
  matching impling icon on the reported game tile.
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

- The minimum polling interval is `10` seconds; default is `30`.
- Location resolution prefers the nearest same-plane label in the same official
  region, then `Near <nearest same-plane label>`, then `Unknown area`.
- Region-based sighting keys deliberately allow a moving impling to update
  coordinates without creating duplicate Discord posts inside the same region.
- Despawn cleanup must only follow a successful backend response. Backend
  failures must not delete active Discord messages.
- Map screenshots use one Explv zoom-11 tile, which corresponds to one current
  `8x8` game chunk. The image is enlarged with nearest-neighbor scaling before
  compositing the matching impling asset.
- Plain-text and generated-card fallbacks must also avoid exposing coordinates,
  NPC ID, age, or plane.
- Keep bundled asset names aligned with `ImplingSpawn.type_key`:
  `magpie.png`, `ninja.png`, `crystal.png`, `dragon.png`, and `lucky.png`.

## Change Workflow

1. Inspect `git status` and the relevant ImplingFinder files first.
2. Add or update focused tests before changing behavior.
3. Keep edits scoped to ImplingFinder and work with existing user changes.
4. For map-rendering changes, generate and visually inspect a representative
   chunk image and fallback card.
5. Update `implingfinder/README.md` when commands, behavior, data sources, or
   operational expectations change.

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
```

- [x] **Step 2: Verify the guide against the current implementation**

Run:

```bash
rg -n "MIN_POLL_INTERVAL_SECONDS|DEFAULT_POLL_INTERVAL_SECONDS|region_id_from_xy|resolve_location_name|legacy_area_key|explv_chunk_tile|impling_icon_center" implingfinder
rg -n "Coordinates|NPC ID|Age|Plane|footer" tests/test_implingfinder_import.py implingfinder/implingfinder.py
```

Expected: the current implementation confirms the documented interval, region, migration, location, and chunk-rendering rules; prohibited Discord fields appear only in tests asserting their absence or unrelated internal code.

- [x] **Step 3: Validate the documentation change**

Run:

```bash
git diff --check
rg -n "unrelated cogs|Current Product Contract|Required Verification|External References" AGENTS.md
git status --short --branch
```

Expected: no whitespace errors; all major guide sections exist; only the new guide and relevant planning documentation are changed.

- [x] **Step 4: Commit the guide**

Run:

```bash
git add AGENTS.md docs/superpowers/plans/2026-06-05-implingfinder-agents.md
git commit -m "Add ImplingFinder agent guide"
```

Expected: a commit containing only the ImplingFinder agent guide and its implementation plan.
