# Impling Region and Chunk Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use official OSRS region IDs and human-readable location names while rendering each sighting as its exact 8x8 map chunk with a matching impling icon.

**Architecture:** Core helpers calculate official region IDs, chunk identity, tile position, and location names from bundled Explv labels. The cog loads bundled labels/assets once, uses official region IDs for dedupe and active-message tracking, and renders a single zoom-11 Explv tile with the impling icon placed on the reported game tile.

**Tech Stack:** Python, Red-DiscordBot, aiohttp, Pillow, Explv map labels/tiles, OSRS Wiki PNG assets.

---

### Task 1: Region and Location Helpers

**Files:**
- Modify: `implingfinder/core.py`
- Test: `tests/test_implingfinder_core.py`

- [x] Add failing tests for official region IDs, chunk IDs, tile offsets, location resolution, and region-based sighting keys.
- [x] Run `python3 -m unittest tests.test_implingfinder_core` and confirm the new tests fail.
- [x] Implement the core helpers and rerun the focused tests.

### Task 2: Bundled Labels and Impling Assets

**Files:**
- Create: `implingfinder/data/map_labels.json`
- Create: `implingfinder/assets/*.png`
- Modify: `implingfinder/info.json`

- [x] Generate a compact bundled label file from Explv `map_labels.json`.
- [x] Download matching transparent Magpie, Ninja, Crystal, Dragon, and Lucky impling PNGs from the OSRS Wiki.
- [x] Declare Pillow as a cog requirement.

### Task 3: Discord Output and Chunk Renderer

**Files:**
- Modify: `implingfinder/implingfinder.py`
- Test: `tests/test_implingfinder_import.py`

- [x] Add failing tests for simplified embed fields and exact chunk/icon placement.
- [x] Load location labels and resolve a display location for every spawn.
- [x] Render the exact zoom-11 chunk, upscale it, and place the matching impling asset on the reported tile.
- [x] Remove coordinates, plane, age, NPC ID, and footer from Discord posts and fallback cards.

### Task 4: Verification

**Files:**
- Modify: `implingfinder/README.md`

- [x] Update documentation for official regions, location names, and chunk screenshots.
- [x] Generate and inspect a live sample chunk image.
- [x] Run `python3 -m unittest discover tests`.
- [x] Run `git diff --check`.
