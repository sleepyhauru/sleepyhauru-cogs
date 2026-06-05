from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


DEFAULT_ENDPOINT = (
    "https://puos0bfgxc2lno5-implingdb.adb.us-phoenix-1.oraclecloudapps.com/"
    "ords/impling/implingdev/dev"
)
DEFAULT_ID_ENDPOINT = f"{DEFAULT_ENDPOINT}/"

MAX_SEEN_KEYS = 1000
MIN_POLL_INTERVAL_SECONDS = 10
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_MAX_AGE_SECONDS = 15 * 60
DEFAULT_MAP_ZOOM = 7
SIGHTING_REGION_SIZE = 128
EXPLV_TILE_BASE_URL = "https://raw.githubusercontent.com/Explv/osrs_map_tiles/master"
EXPLV_MIN_ZOOM = 4
EXPLV_MAX_ZOOM = 11
EXPLV_TILE_SIZE = 256
EXPLV_GAME_TILE_PIXELS_AT_MAX_ZOOM = 32
EXPLV_X_ORIGIN = 960
EXPLV_Y_ORIGIN = 6208
EXPLV_PIXEL_Y_ORIGIN = 364_544


@dataclass(frozen=True)
class ImplingInfo:
    key: str
    name: str
    npcid: int
    color: int


IMPLINGS: dict[str, ImplingInfo] = {
    "magpie": ImplingInfo("magpie", "Magpie impling", 1642, 0xD8DEE9),
    "ninja": ImplingInfo("ninja", "Ninja impling", 1643, 0x4C566A),
    "crystal": ImplingInfo("crystal", "Crystal impling", 8741, 0x88C0D0),
    "dragon": ImplingInfo("dragon", "Dragon impling", 1644, 0xBF616A),
    "lucky": ImplingInfo("lucky", "Lucky impling", 7233, 0xEBCB8B),
}
IMPLING_ORDER = ["magpie", "ninja", "crystal", "dragon", "lucky"]
NPC_ID_TO_TYPE = {info.npcid: key for key, info in IMPLINGS.items()}
ALIASES = {
    "magpie": "magpie",
    "mag": "magpie",
    "ninja": "ninja",
    "nin": "ninja",
    "crystal": "crystal",
    "crys": "crystal",
    "dragon": "dragon",
    "drag": "dragon",
    "dimp": "dragon",
    "lucky": "lucky",
    "luck": "lucky",
    "limp": "lucky",
    "all": "all",
    "rare": "all",
}


@dataclass(frozen=True)
class ImplingSpawn:
    npcid: int
    world: int
    xcoord: int
    ycoord: int
    plane: int
    discovered: datetime

    @property
    def dedupe_key(self) -> str:
        epoch = int(self.discovered.astimezone(timezone.utc).timestamp())
        return f"{self.npcid}:{self.world}:{self.xcoord}:{self.ycoord}:{self.plane}:{epoch}"

    @property
    def sighting_key(self) -> str:
        region_x = sighting_region(self.xcoord)
        region_y = sighting_region(self.ycoord)
        return f"{self.npcid}:{self.world}:{self.plane}:{region_x}:{region_y}"

    @property
    def type_key(self) -> str | None:
        return NPC_ID_TO_TYPE.get(self.npcid)

    @property
    def impling_name(self) -> str:
        type_key = self.type_key
        if type_key is None:
            return f"NPC {self.npcid}"
        return IMPLINGS[type_key].name

    @property
    def discovered_epoch(self) -> int:
        return int(self.discovered.astimezone(timezone.utc).timestamp())


@dataclass(frozen=True)
class ExplvTile:
    tile_x: int
    tile_y: int
    url_y: int
    paste_x: int
    paste_y: int
    url: str


def parse_discovered_epoch(value: Any) -> datetime:
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("discoveredtime must be Unix epoch seconds") from exc
    return datetime.fromtimestamp(epoch, timezone.utc)


def sighting_region(coord: int) -> int:
    return (int(coord) + SIGHTING_REGION_SIZE // 2) // SIGHTING_REGION_SIZE


def parse_impling_types(values: Sequence[str] | Iterable[str]) -> list[str]:
    requested = list(values)
    if not requested:
        raise ValueError("Provide at least one impling type.")

    selected: list[str] = []
    seen: set[str] = set()
    for raw_value in requested:
        value = str(raw_value).strip().lower()
        resolved = ALIASES.get(value)
        if resolved is None:
            valid = ", ".join(sorted(ALIASES))
            raise ValueError(f"Unknown impling type `{raw_value}`. Valid types: {valid}.")

        if resolved == "all":
            return list(IMPLING_ORDER)

        if resolved not in seen:
            seen.add(resolved)
            selected.append(resolved)

    return selected


def npc_ids_for_types(type_keys: Iterable[str]) -> list[int]:
    return [IMPLINGS[type_key].npcid for type_key in type_keys]


def type_key_for_npcid(npcid: int) -> str | None:
    return NPC_ID_TO_TYPE.get(npcid)


def parse_backend_payload(payload: Mapping[str, Any]) -> list[ImplingSpawn]:
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Backend payload `items` must be a list.")

    spawns: list[ImplingSpawn] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue

        try:
            npcid = int(item["npcid"])
            if npcid not in NPC_ID_TO_TYPE:
                continue
            spawn = ImplingSpawn(
                npcid=npcid,
                world=int(item["world"]),
                xcoord=int(item["xcoord"]),
                ycoord=int(item["ycoord"]),
                plane=int(item["plane"]),
                discovered=parse_discovered_epoch(item["discoveredtime"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        spawns.append(spawn)

    return sorted(spawns, key=lambda spawn: spawn.discovered, reverse=True)


def filter_stale_spawns(
    spawns: Iterable[ImplingSpawn],
    now: datetime,
    max_age_seconds: int,
) -> list[ImplingSpawn]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    return [
        spawn
        for spawn in spawns
        if (now - spawn.discovered.astimezone(timezone.utc)).total_seconds() <= max_age_seconds
    ]


def collapse_duplicate_sightings(spawns: Iterable[ImplingSpawn]) -> list[ImplingSpawn]:
    newest_by_sighting: dict[str, ImplingSpawn] = {}
    for spawn in spawns:
        current = newest_by_sighting.get(spawn.sighting_key)
        if current is None or spawn.discovered > current.discovered:
            newest_by_sighting[spawn.sighting_key] = spawn
    return sorted(
        newest_by_sighting.values(),
        key=lambda spawn: spawn.discovered,
        reverse=True,
    )


def matching_channel_ids(channels: Mapping[str, Sequence[int]], spawn: ImplingSpawn) -> list[int]:
    channel_ids: list[int] = []
    for raw_channel_id, npc_ids in channels.items():
        if spawn.npcid not in npc_ids:
            continue
        try:
            channel_ids.append(int(raw_channel_id))
        except (TypeError, ValueError):
            continue
    return channel_ids


def select_unseen_spawns(
    spawns: Iterable[ImplingSpawn],
    seen_keys: Sequence[str],
    *,
    announce_existing: bool,
) -> tuple[list[ImplingSpawn], list[str]]:
    seen = set(seen_keys)
    updated = list(seen_keys)
    to_announce: list[ImplingSpawn] = []
    first_run = not updated

    for spawn in spawns:
        key = spawn.sighting_key
        if key in seen:
            continue

        seen.add(key)
        updated.append(key)
        if announce_existing or not first_run:
            to_announce.append(spawn)

    return to_announce, trim_seen_keys(updated)


def sighting_key_from_legacy_dedupe_key(value: str) -> str | None:
    parts = str(value).split(":")
    if len(parts) != 6:
        return None
    try:
        npcid = int(parts[0])
        world = int(parts[1])
        xcoord = int(parts[2])
        ycoord = int(parts[3])
        plane = int(parts[4])
    except ValueError:
        return None
    region_x = sighting_region(xcoord)
    region_y = sighting_region(ycoord)
    return f"{npcid}:{world}:{plane}:{region_x}:{region_y}"


def build_map_url(spawn: ImplingSpawn, *, zoom: int = DEFAULT_MAP_ZOOM) -> str:
    zoom = validate_explv_zoom(zoom)
    return (
        "https://explv.github.io/"
        f"?centreX={spawn.xcoord}&centreY={spawn.ycoord}&centreZ={spawn.plane}&zoom={zoom}"
    )


def validate_explv_zoom(zoom: int) -> int:
    zoom = int(zoom)
    if zoom < EXPLV_MIN_ZOOM or zoom > EXPLV_MAX_ZOOM:
        raise ValueError(
            f"Explv zoom must be between {EXPLV_MIN_ZOOM} and {EXPLV_MAX_ZOOM}."
        )
    return zoom


def explv_pixel_point(
    xcoord: int,
    ycoord: int,
    *,
    zoom: int = DEFAULT_MAP_ZOOM,
) -> tuple[float, float]:
    zoom = validate_explv_zoom(zoom)
    scale = 2 ** (EXPLV_MAX_ZOOM - zoom)
    pixel_x = (
        (float(xcoord) + 0.5 - EXPLV_X_ORIGIN) * EXPLV_GAME_TILE_PIXELS_AT_MAX_ZOOM
        + EXPLV_GAME_TILE_PIXELS_AT_MAX_ZOOM / 4
    )
    pixel_y = EXPLV_PIXEL_Y_ORIGIN - (
        (float(ycoord) + 0.5 - EXPLV_Y_ORIGIN) * EXPLV_GAME_TILE_PIXELS_AT_MAX_ZOOM
    )
    return pixel_x / scale, pixel_y / scale


def explv_tile_url(plane: int, zoom: int, tile_x: int, tile_y: int) -> tuple[str, int]:
    zoom = validate_explv_zoom(zoom)
    url_y = (2**zoom - 1) - int(tile_y)
    url = f"{EXPLV_TILE_BASE_URL}/{int(plane)}/{zoom}/{int(tile_x)}/{url_y}.png"
    return url, url_y


def explv_tiles_for_crop(
    spawn: ImplingSpawn,
    *,
    width: int,
    height: int,
    zoom: int = DEFAULT_MAP_ZOOM,
) -> list[ExplvTile]:
    zoom = validate_explv_zoom(zoom)
    width = int(width)
    height = int(height)
    if width < 1 or height < 1:
        raise ValueError("Crop width and height must be positive.")

    center_x, center_y = explv_pixel_point(spawn.xcoord, spawn.ycoord, zoom=zoom)
    left = center_x - width / 2
    top = center_y - height / 2
    right = left + width
    bottom = top + height
    start_tile_x = math.floor(left / EXPLV_TILE_SIZE)
    end_tile_x = math.floor((right - 1e-9) / EXPLV_TILE_SIZE)
    start_tile_y = math.floor(top / EXPLV_TILE_SIZE)
    end_tile_y = math.floor((bottom - 1e-9) / EXPLV_TILE_SIZE)

    tiles: list[ExplvTile] = []
    for tile_y in range(start_tile_y, end_tile_y + 1):
        for tile_x in range(start_tile_x, end_tile_x + 1):
            url, url_y = explv_tile_url(spawn.plane, zoom, tile_x, tile_y)
            tiles.append(
                ExplvTile(
                    tile_x=tile_x,
                    tile_y=tile_y,
                    url_y=url_y,
                    paste_x=round(tile_x * EXPLV_TILE_SIZE - left),
                    paste_y=round(tile_y * EXPLV_TILE_SIZE - top),
                    url=url,
                )
            )
    return tiles


def sanitize_endpoint_url(value: str) -> str:
    url = str(value).strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("Endpoint URL must use https.")
    if not parsed.netloc:
        raise ValueError("Endpoint URL must include a host.")
    if parsed.username or parsed.password:
        raise ValueError("Endpoint URL must not include a username or password.")
    return url.rstrip("/")


def build_id_endpoint(endpoint: str) -> str:
    return f"{sanitize_endpoint_url(endpoint).rstrip('/')}/"


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes}m"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def trim_seen_keys(keys: list[str]) -> list[str]:
    if len(keys) <= MAX_SEEN_KEYS:
        return keys
    return keys[-MAX_SEEN_KEYS:]
