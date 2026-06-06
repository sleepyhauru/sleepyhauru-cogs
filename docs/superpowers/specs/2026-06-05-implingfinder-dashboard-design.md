# ImplingFinder Performance Dashboard Design

## Goal

Add a read-only performance dashboard to ImplingFinder so fetch, processing,
rendering, Discord posting, discovery-to-post latency, errors, and lightweight
Red process health can be monitored over time.

The dashboard runs inside the ImplingFinder cog and is exposed privately through
the existing Unraid Traefik, VoidAuth, Technitium, Cloudflare, and Tailscale
stack.

## Confirmed Deployment

- Red DiscordBot and Traefik run on the same Unraid server.
- The Red container already publishes unused host port `8765`.
- The dashboard starts automatically on `0.0.0.0:8765` when the cog loads.
- Traefik routes `implings.hauru.app` to `http://100.70.109.15:8765`.
- The Traefik router uses the existing `voidauth-forward` middleware.
- The dashboard implements no authentication because access control belongs to
  the reverse proxy.
- Failure to start the dashboard must not prevent ImplingFinder polling or
  posting from running.

## Product Scope

The dashboard is read-only. It has no endpoint or UI control that changes feed
settings, clears data, pauses polling, triggers test polls, or performs another
operational action.

The combined overview shows all ImplingFinder activity. A server selector
provides per-server drill-down. Channel information is visible in recent event
details but does not receive a separate dashboard section.

## Metrics

Track:

- Backend fetch duration, outcome, HTTP/error category, and returned row count.
- Poll processing duration and total poll duration.
- Map download and image-render duration.
- Discord send duration and send outcome.
- Discovery-to-post latency.
- Posts sent, duplicate sightings suppressed, routed sightings, and despawn
  messages deleted.
- Backend backoff state and failures.
- Bot uptime, current RSS memory, event-loop lag, metrics queue depth and drops,
  and metrics database size.

Detailed post events contain impling type, world, human-readable location,
server, channel, outcome, and timing values. They do not expose coordinates,
NPC ID, or plane.

## Critical-Path Safety

Instrumentation uses `time.monotonic()` around existing operations. Producers
enqueue metrics with `put_nowait()` into a bounded async queue. If the queue is
full, metrics are dropped and the drop count is exposed; sighting posts are
never delayed while waiting for metrics storage.

SQLite writes and dashboard queries run outside the event loop through
`asyncio.to_thread()`. Dashboard or database errors are logged and isolated from
polling and Discord posting.

## Storage and Retention

Store SQLite at `cog_data_path(self) / "metrics.sqlite3"` inside Red's
persistent `/data` volume.

Tables:

- `events`: individual fetch, poll, post, and despawn events retained for
  7 days.
- `hourly_metrics`: hourly grouped counts, sums, minima, and maxima retained for
  30 days.

Each accepted event updates its hourly aggregate during the same queued write.
A periodic maintenance task prunes expired rows. SQLite uses WAL mode and
reasonable indexes for time, event kind, and server drill-down queries.

## Components

### `implingfinder/metrics.py`

- Immutable metric event model.
- Bounded non-blocking event queue.
- SQLite schema initialization, queued batch writer, hourly aggregation, and
  retention cleanup.
- Read queries for overview summaries, hourly charts, server list, and recent
  events.
- Lightweight process-health snapshot and event-loop-lag heartbeat.

### `implingfinder/dashboard.py`

- Read-only `aiohttp.web` application.
- `GET /`: self-contained responsive dashboard HTML/CSS/JavaScript.
- `GET /api/summary`: combined or per-server summary.
- `GET /api/hourly`: hourly time series.
- `GET /api/events`: recent detailed events.
- `GET /healthz`: dashboard and metrics-store health.
- Security and no-cache response headers.

The UI uses a quiet operational layout with status indicators, compact metric
tiles, latency trend canvases, outcome breakdowns, and a recent-events table.
It has no forms or controls beyond time range and server drill-down selectors.

### `implingfinder/implingfinder.py`

- Starts and stops metrics/dashboard lifecycle with the cog.
- Adds timing probes around fetch, poll processing, map rendering, Discord send,
  and despawn deletion.
- Records metrics without changing existing product behavior.

## API Behavior

All routes accept `GET` only. API responses are JSON and include a generated-at
timestamp. Unknown server IDs return empty filtered results rather than errors.
The events endpoint has a fixed maximum limit to prevent expensive requests.

The dashboard refreshes automatically every 10 seconds. API failures display a
visible stale/error state while preserving the last successfully rendered data.

## Testing

- Unit-test event serialization, non-blocking queue drops, SQLite writes,
  hourly aggregation, retention, summary queries, and server filtering.
- Test dashboard routes, read-only methods, response headers, and required page
  sections.
- Test cog lifecycle and instrumentation with dependency stubs/fakes.
- Run the complete repository test suite and existing ImplingFinder checks.
- Start a local dashboard against sample data and inspect desktop and mobile
  screenshots before deployment.

## Live Verification

After pushing and updating the cog:

1. Reload or restart Red and confirm `0.0.0.0:8765` listens inside the container.
2. Confirm `http://100.70.109.15:8765/healthz` responds from Unraid.
3. Add the Traefik `implings.hauru.app` router and service with
   `voidauth-forward`.
4. Validate Traefik configuration and reload it.
5. Confirm the hostname resolves through the existing DNS/tunnel setup.
6. Confirm the routed dashboard requires VoidAuth and loads after
   authentication.

