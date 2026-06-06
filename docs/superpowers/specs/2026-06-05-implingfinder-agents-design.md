# ImplingFinder AGENTS.md Design

## Goal

Create a root-level `AGENTS.md` that lets a future coding session resume ImplingFinder work safely without needing the prior conversation. Although the file is at the repository root so agents see it immediately, its instructions apply only to ImplingFinder-related work.

## Scope

The guide will cover:

- `implingfinder/`
- `tests/test_implingfinder_core.py`
- `tests/test_implingfinder_import.py`
- the ImplingFinder sections of `README.md`
- ImplingFinder plans and documentation under `docs/`

It will explicitly direct agents to leave unrelated cogs untouched.

## Required Handoff Context

The guide will record the current product behavior and decisions:

- Poll the read-only Oracle ORDS Impling Finder backend.
- Track Magpie, Ninja, Crystal, Dragon, and Lucky implings.
- Deduplicate by NPC ID, world, plane, and official OSRS region ID.
- Delete tracked Discord messages when sightings disappear from successful backend data.
- Display human-readable location names without coordinates, NPC ID, age, plane, or source footer.
- Render the exact current `8x8` OSRS chunk with the matching impling icon on the reported tile.
- Preserve migration support for previously stored exact and coarse sighting keys.

## Architecture Guidance

The guide will explain the responsibilities of:

- `implingfinder/core.py`: pure parsing, filtering, dedupe, region, location, and Explv coordinate helpers.
- `implingfinder/implingfinder.py`: Red cog configuration, polling, Discord messages, state migration, and image rendering.
- `implingfinder/data/map_labels.json`: bundled Explv location labels.
- `implingfinder/assets/*.png`: bundled matching impling icons.
- ImplingFinder tests: dependency-stubbed cog behavior and pure core logic.

New pure behavior should remain in `core.py` where practical and receive focused unit tests.

## Verification and Delivery

Before claiming completion or committing ImplingFinder changes, agents must run:

```text
python3 -m unittest discover tests
python3 -m compileall -q implingfinder
python3 -m json.tool implingfinder/info.json
python3 -m json.tool implingfinder/data/map_labels.json
git diff --check
```

Map-rendering changes also require generating and visually inspecting a representative output image. Agents should inspect `git status` before committing, stage only relevant files, and never revert unrelated user changes.

## External Sources

The guide will retain the relevant upstream references:

- Impling Finder backend and RuneLite plugin
- Explv map labels
- Explv OSRS map tiles
- Old School RuneScape Wiki impling assets

