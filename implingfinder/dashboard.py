from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Optional

from aiohttp import web


log = logging.getLogger("red.implingfinder.dashboard")

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8765
MAX_EVENTS_LIMIT = 200
MAX_HOURS = 30 * 24

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class DashboardServer:
    def __init__(
        self,
        metrics_store,
        *,
        health_provider: Optional[Callable[[], Any]] = None,
        host: str = DASHBOARD_HOST,
        port: int = DASHBOARD_PORT,
    ):
        self.metrics_store = metrics_store
        self.health_provider = health_provider or (lambda: {})
        self.host = str(host)
        self.port = int(port)
        self.runner = None
        self.site = None

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/summary", self.handle_summary)
        app.router.add_get("/api/hourly", self.handle_hourly)
        app.router.add_get("/api/events", self.handle_events)
        app.router.add_get("/healthz", self.handle_health)
        return app

    async def start(self) -> None:
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self.create_app())
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        try:
            await self.site.start()
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
            raise

    async def stop(self) -> None:
        if self.runner is None:
            return
        await self.runner.cleanup()
        self.runner = None
        self.site = None

    async def handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=DASHBOARD_HTML,
            content_type="text/html",
            headers=dict(SECURITY_HEADERS),
        )

    async def handle_summary(self, request: web.Request) -> web.Response:
        hours = _query_int(request, "hours", 24, minimum=1, maximum=MAX_HOURS)
        guild_id = _query_text(request, "guild_id")
        summary = await self.metrics_store.summary(hours=hours, guild_id=guild_id)
        servers = await self.metrics_store.servers()
        return _json_response(
            {
                **summary,
                "hours": hours,
                "guild_id": guild_id,
                "servers": servers,
                "health": await self._combined_health(),
            }
        )

    async def handle_hourly(self, request: web.Request) -> web.Response:
        hours = _query_int(request, "hours", 24, minimum=1, maximum=MAX_HOURS)
        guild_id = _query_text(request, "guild_id")
        series = await self.metrics_store.hourly(hours=hours, guild_id=guild_id)
        return _json_response(
            {
                "generated_at": _now_iso(),
                "hours": hours,
                "guild_id": guild_id,
                "series": series,
            }
        )

    async def handle_events(self, request: web.Request) -> web.Response:
        limit = _query_int(request, "limit", 50, minimum=1, maximum=MAX_EVENTS_LIMIT)
        guild_id = _query_text(request, "guild_id")
        events = await self.metrics_store.recent_events(limit=limit, guild_id=guild_id)
        return _json_response(
            {
                "generated_at": _now_iso(),
                "limit": limit,
                "guild_id": guild_id,
                "events": events,
            }
        )

    async def handle_health(self, _request: web.Request) -> web.Response:
        return _json_response(await self._combined_health())

    async def _combined_health(self) -> dict[str, Any]:
        health = dict(self.metrics_store.health())
        try:
            extra = self.health_provider()
            if inspect.isawaitable(extra):
                extra = await extra
            if extra:
                health.update(dict(extra))
        except Exception as exc:
            health["status"] = "degraded"
            health["health_provider_error"] = type(exc).__name__
            log.exception("Impling Finder dashboard health provider failed")
        health["generated_at"] = _now_iso()
        return health


def _query_int(
    request: web.Request,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(request.query.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _query_text(request: web.Request, name: str) -> Optional[str]:
    value = str(request.query.get(name, "")).strip()
    return value or None


def _json_response(payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload, headers=dict(SECURITY_HEADERS))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ImplingFinder Performance</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111315;
      --panel: #191c1f;
      --panel-2: #202428;
      --border: #343a40;
      --text: #f2f4f5;
      --muted: #9da5ad;
      --green: #64d49a;
      --amber: #f1bd69;
      --red: #ef7c82;
      --cyan: #69c5d4;
      --blue: #7fa9ec;
      --purple: #b898dc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      border-bottom: 1px solid var(--border);
      background: #15181a;
    }
    .topbar, main {
      width: min(1500px, calc(100% - 32px));
      margin: 0 auto;
    }
    .topbar {
      min-height: 72px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    h1, h2 { margin: 0; font-weight: 650; }
    h1 { font-size: 21px; }
    h2 { font-size: 15px; }
    .subtitle, .muted { color: var(--muted); }
    .subtitle { margin-top: 3px; font-size: 12px; }
    .controls { display: flex; gap: 8px; align-items: center; }
    select {
      height: 34px;
      border: 1px solid var(--border);
      border-radius: 5px;
      background: var(--panel);
      color: var(--text);
      padding: 0 30px 0 10px;
    }
    main { padding: 22px 0 48px; }
    .statusline {
      min-height: 30px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
      flex: 0 0 auto;
    }
    .dot.degraded { background: var(--amber); }
    .dot.error { background: var(--red); }
    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
      margin: 10px 0 22px;
    }
    .metric, .panel {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
    }
    .metric { padding: 13px 14px; min-height: 84px; }
    .metric .label { color: var(--muted); font-size: 11px; text-transform: uppercase; }
    .metric .value { margin-top: 8px; font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }
    .metric .detail { margin-top: 3px; color: var(--muted); font-size: 11px; }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.65fr) minmax(300px, .75fr);
      gap: 12px;
      margin-bottom: 12px;
    }
    .panel { min-width: 0; overflow: hidden; }
    .panel-head {
      min-height: 48px;
      padding: 0 14px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .panel-body { padding: 14px; }
    canvas { width: 100%; height: 230px; display: block; }
    .health-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1px;
      background: var(--border);
    }
    .health-item {
      background: var(--panel);
      padding: 13px 14px;
      min-height: 68px;
    }
    .health-item span { display: block; color: var(--muted); font-size: 11px; }
    .health-item strong { display: block; margin-top: 6px; font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 920px; }
    th, td { border-bottom: 1px solid var(--border); padding: 10px 12px; text-align: left; white-space: nowrap; }
    th { color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; background: var(--panel-2); }
    td { font-variant-numeric: tabular-nums; }
    tbody tr:last-child td { border-bottom: 0; }
    .tag { display: inline-flex; padding: 2px 7px; border-radius: 4px; background: var(--panel-2); border: 1px solid var(--border); font-size: 11px; }
    .ok { color: var(--green); }
    .error-text { color: var(--red); }
    @media (max-width: 1100px) {
      .metrics { grid-template-columns: repeat(3, minmax(120px, 1fr)); }
      .grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 650px) {
      .topbar, main { width: min(100% - 20px, 1500px); }
      .topbar { align-items: flex-start; flex-direction: column; justify-content: center; padding: 14px 0; }
      .controls { width: 100%; }
      select { min-width: 0; flex: 1; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric .value { font-size: 20px; }
      .health-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<header>
  <div class="topbar">
    <div>
      <h1>ImplingFinder Performance</h1>
      <div class="subtitle">Fetch, processing, render, and Discord delivery health</div>
    </div>
    <div class="controls">
      <select id="server" aria-label="Server"><option value="">All servers</option></select>
      <select id="range" aria-label="Time range">
        <option value="24">Last 24 hours</option>
        <option value="168">Last 7 days</option>
        <option value="720">Last 30 days</option>
      </select>
    </div>
  </div>
</header>
<main>
  <div class="statusline"><span id="status-dot" class="dot"></span><span id="status">Loading metrics...</span></div>
  <section id="metrics" class="metrics"></section>
  <section class="grid">
    <article class="panel">
      <div class="panel-head"><h2>Pipeline latency</h2><span class="muted">Hourly averages</span></div>
      <div class="panel-body"><canvas id="latency-chart" width="1000" height="300"></canvas></div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>Process health</h2><span id="health-state" class="tag">Loading</span></div>
      <div id="health" class="health-grid"></div>
    </article>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>Recent events</h2><span id="event-count" class="muted"></span></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Outcome</th><th>Server</th><th>Channel</th><th>Impling</th><th>World</th><th>Location</th><th>Duration</th><th>Age at fetch</th><th>End-to-end</th></tr></thead>
        <tbody id="events"></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const state = { summary: null, hourly: [], events: [], timer: null };
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const ms = value => value == null ? "—" : value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
const count = value => new Intl.NumberFormat().format(value ?? 0);
const bytes = value => {
  if (!value) return "0 B";
  const units = ["B","KB","MB","GB"]; let index = 0; let n = value;
  while (n >= 1024 && index < units.length - 1) { n /= 1024; index++; }
  return `${n.toFixed(index ? 1 : 0)} ${units[index]}`;
};
function query(extra="") {
  const params = new URLSearchParams({hours: $("range").value});
  if ($("server").value) params.set("guild_id", $("server").value);
  if (extra) params.set("limit", extra);
  return params.toString();
}
async function getJson(path) {
  const response = await fetch(path, {headers: {"Accept":"application/json"}, cache:"no-store"});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
function metric(label, value, detail="") {
  return `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="detail">${esc(detail)}</div></div>`;
}
function renderSummary(data) {
  const totals = data.totals || {}; const latency = data.latency_ms || {};
  const botAfterFetch = latency.end_to_end?.average == null || latency.age_at_fetch?.average == null
    ? null
    : Math.max(0, latency.end_to_end.average - latency.age_at_fetch.average);
  $("metrics").innerHTML = [
    metric("Fetches", count(totals.fetches), `${count(totals.errors)} errors`),
    metric("Posts sent", count(totals.posts), `${count(totals.routed)} routed`),
    metric("Fetch average", ms(latency.fetch?.average), `max ${ms(latency.fetch?.maximum)}`),
    metric("Age at fetch", ms(latency.age_at_fetch?.average), `max ${ms(latency.age_at_fetch?.maximum)}`),
    metric("Bot after fetch", ms(botAfterFetch), "avg post gap"),
    metric("Render average", ms(latency.render?.average), `max ${ms(latency.render?.maximum)}`),
    metric("Discord send", ms(latency.send?.average), `max ${ms(latency.send?.maximum)}`),
    metric("Discovery to post", ms(latency.end_to_end?.average), `max ${ms(latency.end_to_end?.maximum)}`)
  ].join("");
  updateServers(data.servers || []);
  renderHealth(data.health || {});
}
function updateServers(servers) {
  const select = $("server"); const current = select.value;
  const options = [`<option value="">All servers</option>`].concat(servers.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`));
  select.innerHTML = options.join(""); select.value = current;
}
function renderHealth(health) {
  const status = health.status || "unknown";
  $("health-state").textContent = status;
  $("health-state").className = `tag ${status === "ok" ? "ok" : "error-text"}`;
  $("health").innerHTML = [
    ["Bot uptime", `${Math.floor((health.bot_uptime_seconds || 0) / 3600)} h`],
    ["RSS memory", bytes(health.rss_bytes)],
    ["Event-loop lag", ms(health.event_loop_lag_ms)],
    ["Active backoffs", count(health.active_backoffs)],
    ["Poll runners", count(health.poll_runners)],
    ["Screenshot queue", count(health.screenshot_queue_depth)],
    ["Maintenance queue", count(health.maintenance_queue_depth)],
    ["Metrics queue", `${count(health.queue_depth)} / ${count(health.queue_capacity)}`],
    ["Dropped metrics", count(health.dropped_events)],
    ["Database", bytes(health.database_bytes)],
    ["Write failures", count(health.write_failures)]
  ].map(([label,value]) => `<div class="health-item"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
}
function renderEvents(data) {
  $("event-count").textContent = `${data.length} shown`;
  $("events").innerHTML = data.map(event => `<tr>
    <td>${esc(new Date(event.occurred_at).toLocaleString())}</td>
    <td><span class="tag">${esc(event.kind)}</span></td>
    <td class="${event.outcome === "ok" ? "ok" : "error-text"}">${esc(event.outcome)}</td>
    <td>${esc(event.guild_name)}</td><td>${esc(event.channel_name)}</td><td>${esc(event.impling_type)}</td>
    <td>${esc(event.world)}</td><td>${esc(event.location)}</td><td>${esc(ms(event.duration_ms))}</td><td>${esc(ms(event.age_at_fetch_ms))}</td><td>${esc(ms(event.end_to_end_ms))}</td>
  </tr>`).join("") || `<tr><td colspan="11" class="muted">No events in this range.</td></tr>`;
}
function renderChart(rows) {
  const canvas = $("latency-chart"), ctx = canvas.getContext("2d");
  const width = canvas.width, height = canvas.height, pad = 38;
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = "#191c1f"; ctx.fillRect(0, 0, width, height);
  const fields = [["fetch","#69c5d4"],["age_at_fetch","#f1c36d"],["process","#7fa9ec"],["render","#b898dc"],["send","#64d49a"]];
  const kinds = {fetch:"fetch", age_at_fetch:"post", process:"poll", render:"render", send:"post"};
  const points = fields.map(([field,color]) => [field,color,rows.filter(r => r.kind === kinds[field] && r.latency_ms?.[field]?.average != null).map(r => [new Date(r.hour).getTime(), r.latency_ms[field].average])]);
  const all = points.flatMap(p => p[2]); if (!all.length) { ctx.fillStyle="#9da5ad"; ctx.fillText("No latency samples", pad, height/2); return; }
  const minX = Math.min(...all.map(p=>p[0])); let maxX = Math.max(...all.map(p=>p[0])); if (maxX === minX) maxX = minX + 3600000; const maxY = Math.max(...all.map(p=>p[1]), 10);
  ctx.strokeStyle="#343a40"; ctx.fillStyle="#9da5ad"; ctx.font="12px system-ui";
  for (let i=0;i<=4;i++) { const y=pad+(height-pad*2)*i/4; ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(width-pad,y); ctx.stroke(); ctx.fillText(`${Math.round(maxY*(1-i/4))} ms`,2,y+4); }
  points.forEach(([field,color,series]) => { if (!series.length) return; ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath(); series.forEach(([x,y],i)=>{ const px=pad+(x-minX)/(maxX-minX)*(width-pad*2), py=height-pad-y/maxY*(height-pad*2); i?ctx.lineTo(px,py):ctx.moveTo(px,py); }); ctx.stroke(); });
  fields.forEach(([field,color],i)=>{ ctx.fillStyle=color; ctx.fillRect(pad+i*120,height-18,10,10); ctx.fillStyle="#9da5ad"; ctx.fillText(field,pad+15+i*120,height-9); });
}
async function refresh() {
  try {
    const [summary, hourly, events] = await Promise.all([
      getJson(`/api/summary?${query()}`), getJson(`/api/hourly?${query()}`), getJson(`/api/events?${query("75")}`)
    ]);
    state.summary=summary; state.hourly=hourly.series; state.events=events.events;
    renderSummary(summary); renderChart(hourly.series); renderEvents(events.events);
    $("status-dot").className=`dot ${summary.health?.status === "ok" ? "" : "degraded"}`;
    $("status").textContent=`Live · updated ${new Date(summary.generated_at).toLocaleTimeString()}`;
  } catch (error) {
    $("status-dot").className="dot error"; $("status").textContent=`Dashboard data unavailable · ${error.message}`;
  }
}
$("server").addEventListener("change", refresh); $("range").addEventListener("change", refresh);
refresh(); state.timer=setInterval(refresh,10000);
</script>
</body>
</html>
"""
