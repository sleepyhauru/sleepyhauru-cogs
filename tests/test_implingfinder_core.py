from datetime import datetime, timedelta, timezone
import unittest

import implingfinder.core as core
from implingfinder.core import (
    DEFAULT_ENDPOINT,
    ImplingSpawn,
    MapLabel,
    build_map_url,
    build_id_endpoint,
    collapse_duplicate_sightings,
    explv_chunk_tile,
    filter_stale_spawns,
    impling_icon_center,
    matching_channel_ids,
    parse_backend_payload,
    parse_discovered_epoch,
    parse_impling_types,
    region_id_from_xy,
    resolve_location_name,
    select_unseen_spawns,
    sanitize_endpoint_url,
)


class ImplingFinderCoreTest(unittest.TestCase):
    def test_parse_discovered_epoch_returns_utc_datetime(self):
        discovered = parse_discovered_epoch(1_715_000_000)

        self.assertEqual(discovered.tzinfo, timezone.utc)
        self.assertEqual(int(discovered.timestamp()), 1_715_000_000)

    def test_dedupe_key_uses_epoch_seconds(self):
        spawn = ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=3210,
            ycoord=3420,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )

        self.assertEqual(spawn.dedupe_key, "1644:489:3210:3420:0:1715000000")

    def test_sighting_key_uses_official_osrs_region_id(self):
        first = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1289,
            ycoord=3158,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_000, timezone.utc),
        )
        moved = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1282,
            ycoord=3155,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_030, timezone.utc),
        )
        adjacent_region = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1259,
            ycoord=3160,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_040, timezone.utc),
        )
        elsewhere = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1360,
            ycoord=3158,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_060, timezone.utc),
        )

        self.assertEqual(region_id_from_xy(1289, 3158), 5169)
        self.assertEqual(first.region_id, 5169)
        self.assertEqual(first.sighting_key, "1642:324:0:5169")
        self.assertEqual(first.sighting_key, moved.sighting_key)
        self.assertNotEqual(first.sighting_key, adjacent_region.sighting_key)
        self.assertNotEqual(first.sighting_key, elsewhere.sighting_key)

    def test_collapse_duplicate_sightings_keeps_latest_row_per_region(self):
        older = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1282,
            ycoord=3155,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_000, timezone.utc),
        )
        newer = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1289,
            ycoord=3158,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_030, timezone.utc),
        )
        separate = ImplingSpawn(
            npcid=1642,
            world=324,
            xcoord=1360,
            ycoord=3158,
            plane=0,
            discovered=datetime.fromtimestamp(1_780_000_060, timezone.utc),
        )

        self.assertEqual(
            collapse_duplicate_sightings([older, newer, separate]),
            [separate, newer],
        )

    def test_resolve_location_name_prefers_same_region_then_nearest_label(self):
        spawn = ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=3210,
            ycoord=3420,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        labels = [
            MapLabel("Varrock", 3211, 3450, 0),
            MapLabel("Grand Exchange", 3164, 3487, 0),
            MapLabel("Upstairs", 3210, 3420, 1),
        ]

        self.assertEqual(resolve_location_name(spawn, labels), "Varrock")

        distant_spawn = ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=3000,
            ycoord=3200,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        self.assertEqual(resolve_location_name(distant_spawn, labels), "Near Varrock")
        self.assertEqual(resolve_location_name(distant_spawn, []), "Unknown area")

    def test_explv_chunk_tile_and_icon_center_match_exact_game_tile(self):
        spawn = ImplingSpawn(
            npcid=1644,
            world=489,
            xcoord=2914,
            ycoord=3323,
            plane=0,
            discovered=datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )

        tile = explv_chunk_tile(spawn)

        self.assertEqual(tile.tile_x, 244)
        self.assertEqual(tile.url_y, 263)
        self.assertEqual(
            tile.url,
            "https://raw.githubusercontent.com/Explv/osrs_map_tiles/master/0/11/244/263.png",
        )
        self.assertEqual(impling_icon_center(spawn, canvas_size=512), (176, 288))

    def test_parse_backend_payload_reads_items_and_ignores_unknown_implings(self):
        payload = {
            "items": [
                {
                    "npcid": 1644,
                    "world": "489",
                    "xcoord": 3210,
                    "ycoord": 3420,
                    "plane": 0,
                    "discoveredtime": 1_715_000_000,
                },
                {
                    "npcid": 42,
                    "world": 301,
                    "xcoord": 1,
                    "ycoord": 2,
                    "plane": 0,
                    "discoveredtime": 1_715_000_000,
                },
                {"npcid": 1644},
            ]
        }

        spawns = parse_backend_payload(payload)

        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0].npcid, 1644)
        self.assertEqual(spawns[0].world, 489)
        self.assertEqual(spawns[0].discovered, datetime.fromtimestamp(1_715_000_000, timezone.utc))

    def test_parse_impling_types_supports_aliases_all_and_rare(self):
        self.assertEqual(parse_impling_types(["drag", "limp"]), ["dragon", "lucky"])
        self.assertEqual(
            parse_impling_types(["rare"]),
            ["magpie", "ninja", "crystal", "dragon", "lucky"],
        )
        self.assertEqual(
            parse_impling_types(["all", "dragon"]),
            ["magpie", "ninja", "crystal", "dragon", "lucky"],
        )

    def test_parse_impling_types_rejects_unknown_alias(self):
        with self.assertRaisesRegex(ValueError, "Unknown impling type"):
            parse_impling_types(["gourmet"])

    def test_filter_stale_spawns_uses_max_age_seconds(self):
        now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        fresh = ImplingSpawn(1644, 489, 3210, 3420, 0, now - timedelta(minutes=5))
        stale = ImplingSpawn(7233, 489, 3211, 3421, 0, now - timedelta(minutes=20))

        self.assertEqual(filter_stale_spawns([fresh, stale], now, 900), [fresh])

    def test_matching_channel_ids_routes_by_npcid(self):
        spawn = ImplingSpawn(
            1644,
            489,
            3210,
            3420,
            0,
            datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )
        channels = {
            "111": [1644, 7233],
            "222": [1642],
            "bad": [1644],
        }

        self.assertEqual(matching_channel_ids(channels, spawn), [111])

    def test_build_map_url_uses_explv_coordinates(self):
        spawn = ImplingSpawn(
            1644,
            489,
            2914,
            3323,
            0,
            datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )

        self.assertEqual(
            build_map_url(spawn),
            "https://explv.github.io/?centreX=2914&centreY=3323&centreZ=0&zoom=7",
        )

    def test_explv_tiles_for_crop_uses_tms_tile_y(self):
        spawn = ImplingSpawn(
            1644,
            489,
            2914,
            3323,
            0,
            datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )

        self.assertTrue(hasattr(core, "explv_tiles_for_crop"))

        tiles = core.explv_tiles_for_crop(spawn, width=1, height=1, zoom=7)

        self.assertEqual(len(tiles), 1)
        tile = tiles[0]
        self.assertEqual(tile.tile_x, 15)
        self.assertEqual(tile.tile_y, 111)
        self.assertEqual(tile.url_y, 16)
        self.assertEqual(
            tile.url,
            "https://raw.githubusercontent.com/Explv/osrs_map_tiles/master/0/7/15/16.png",
        )

    def test_select_unseen_spawns_marks_existing_without_announcing_first_run(self):
        spawn = ImplingSpawn(
            1644,
            489,
            3210,
            3420,
            0,
            datetime.fromtimestamp(1_715_000_000, timezone.utc),
        )

        to_announce, updated_seen = select_unseen_spawns([spawn], [], announce_existing=False)

        self.assertEqual(to_announce, [])
        self.assertEqual(updated_seen, [spawn.sighting_key])

        to_announce, updated_seen = select_unseen_spawns([spawn], [], announce_existing=True)

        self.assertEqual(to_announce, [spawn])
        self.assertEqual(updated_seen, [spawn.sighting_key])

    def test_sanitize_endpoint_url_requires_https(self):
        self.assertEqual(sanitize_endpoint_url(DEFAULT_ENDPOINT), DEFAULT_ENDPOINT)
        self.assertEqual(build_id_endpoint(DEFAULT_ENDPOINT), f"{DEFAULT_ENDPOINT}/")

        with self.assertRaisesRegex(ValueError, "https"):
            sanitize_endpoint_url("http://example.com/ords/impling")

        with self.assertRaisesRegex(ValueError, "username"):
            sanitize_endpoint_url("https://user@example.com/ords/impling")


if __name__ == "__main__":
    unittest.main()
