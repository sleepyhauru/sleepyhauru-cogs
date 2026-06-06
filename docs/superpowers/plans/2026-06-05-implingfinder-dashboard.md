# ImplingFinder Performance Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add and deploy a read-only embedded dashboard that records ImplingFinder pipeline performance without delaying sighting posts.

**Architecture:** A bounded non-blocking metrics queue feeds SQLite writes in background threads. A separate embedded `aiohttp.web` component serves a self-contained dashboard and read-only JSON APIs on port `8765`; the existing Unraid Traefik and VoidAuth stack provides private routed access.

**Tech Stack:** Python, asyncio, sqlite3, aiohttp, Red-DiscordBot, unittest, HTML/CSS/JavaScript, Traefik.

---

### Task 1: Metrics Store

**Files:**
- Create: `implingfinder/metrics.py`
- Create: `tests/test_implingfinder_metrics.py`

- [x] Write failing tests proving that a bounded queue drops instead of blocking, accepted events reach SQLite, hourly aggregates update, server filters work, and retention removes expired rows.
- [x] Run `python3 -m unittest tests.test_implingfinder_metrics` and confirm failure because `implingfinder.metrics` does not exist.
- [x] Implement `MetricEvent` and `MetricsStore` with a bounded `asyncio.Queue`, background batch writer, SQLite schema, hourly upserts, retention, queries, process health, and event-loop-lag tracking.
- [x] Run `python3 -m unittest tests.test_implingfinder_metrics` and confirm all metrics tests pass.

### Task 2: Read-Only Dashboard

**Files:**
- Create: `implingfinder/dashboard.py`
- Create: `tests/test_implingfinder_dashboard.py`

- [x] Write failing tests for `GET /`, `/api/summary`, `/api/hourly`, `/api/events`, and `/healthz`; verify non-GET requests return `405`, required security headers exist, and the page contains no operational forms.
- [x] Run `python3 -m unittest tests.test_implingfinder_dashboard` and confirm failure because `implingfinder.dashboard` does not exist.
- [x] Implement the `aiohttp.web` application, read-only handlers, security headers, responsive operational dashboard, 10-second refresh, server drill-down, trend canvases, and recent-event table.
- [x] Run `python3 -m unittest tests.test_implingfinder_dashboard` and confirm all dashboard tests pass.

### Task 3: Cog Lifecycle and Instrumentation

**Files:**
- Modify: `implingfinder/implingfinder.py`
- Modify: `implingfinder/info.json`
- Modify: `tests/test_implingfinder_import.py`

- [x] Add failing lifecycle and instrumentation tests proving the cog starts/stops metrics and dashboard components, fetch failures are recorded, successful post events contain timings, and metrics failures do not block posts.
- [x] Run `python3 -m unittest tests.test_implingfinder_import` and confirm the new tests fail.
- [x] Start the metrics store at `cog_data_path(self) / "metrics.sqlite3"` and dashboard at `0.0.0.0:8765` during `cog_load`; stop both during cleanup while isolating startup/shutdown failures.
- [x] Instrument fetch, poll processing, duplicate suppression, routed sightings, render, Discord send, discovery-to-post latency, and successful despawn deletion with non-blocking event recording.
- [x] Update the end-user data statement to disclose operational performance storage.
- [x] Run `python3 -m unittest tests.test_implingfinder_import` and confirm all import/instrumentation tests pass.

### Task 4: Documentation and Visual Verification

**Files:**
- Modify: `implingfinder/README.md`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [x] Document dashboard behavior, retention, port, private reverse-proxy expectation, metrics scope, and lack of write controls.
- [x] Add dashboard-specific lifecycle, retention, and critical-path rules to `AGENTS.md`.
- [x] Populate a temporary metrics database, run the dashboard locally, and inspect desktop and mobile screenshots.
- [x] Run `python3 -m unittest discover tests`.
- [x] Run `python3 -m compileall -q implingfinder`, JSON validation, and `git diff --check`.

### Task 5: Commit, Push, and Live Unraid Deployment

**Files:**
- Modify on Unraid: `/boot/config/plugins/compose.manager/projects/hauru-private/dynamic.yml`
- Deploy to Red: `/mnt/user/appdata/redbot/cogs/CogManager/cogs/implingfinder/`

- [ ] Commit repository changes and push `main`.
- [ ] Copy the updated ImplingFinder cog into Red's persistent cog directory and restart Red.
- [ ] Verify the dashboard listens through host port `8765` and `/healthz` responds.
- [ ] Back up and update Traefik `dynamic.yml` with an `implings.hauru.app` router using `voidauth-forward` and a service targeting `http://100.70.109.15:8765`.
- [ ] Validate/reload Traefik and verify routed access requires VoidAuth and serves the dashboard.
