# Server snapshot offline suite, client imports only; assertions unchanged.
import sys as _sys
from pathlib import Path as _Path
_app = _Path(__file__).resolve().parents[1] / "app"
_sys.path[:0] = [str(_app), str(_app / "bot"), str(_app / "src")]
"""标准库合成地图测试；无需联网、游戏进程或第三方依赖。"""

import json
import math
from pathlib import Path
import random
import tempfile
import time
import unittest

from walkmap import WalkMap


def make_map(points, cell=1.0):
    return WalkMap.from_json({"map_id": 817373972, "cell": cell, "points": points})


def plane(width, depth, y=0.0, count=10, x0=0, z0=0):
    return [[x + x0, y, z + z0, count]
            for x in range(width) for z in range(depth)]


class WalkMapTests(unittest.TestCase):
    def assert_endpoints(self, route, start, goal):
        self.assertIsNotNone(route)
        self.assertEqual(tuple(route[0]), start)
        self.assertEqual(tuple(route[-1]), goal)

    def test_empty_map(self):
        world = make_map([])
        self.assertIsNone(world.ground_at(0, 0))
        self.assertIsNone(world.nearest_walkable((0, 0, 0)))
        self.assertIsNone(world.random_target((0, 0, 0)))
        self.assertIsNone(world.path((0, 0, 0), (1, 0, 1)))

    def test_load_json_path(self):
        # 临时输入始终放在本任务目录，退出后删除。
        data = {"map_id": 123, "cell": 1.0, "points": [[0, 2, 0, 5]]}
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", prefix="wm_",
                dir=Path(__file__).resolve().parent, delete=False) as stream:
            json.dump(data, stream)
            fixture = Path(stream.name)
        try:
            self.assertEqual(WalkMap.from_json(fixture).ground_at(0, 0), 2.0)
        finally:
            fixture.unlink()

    def test_ground_unknown_cell(self):
        world = make_map([[0, 1, 0, 8]])
        self.assertEqual(world.ground_at(0.2, -0.2), 1)
        self.assertIsNone(world.ground_at(2, 0))

    def test_nonunit_cell_and_negative_coordinates(self):
        world = make_map([[-0.5, 1, 0, 8], [0, 2, 0, 8],
                          [0.5, 3, 0, 8]], cell=0.5)
        self.assertEqual(world.ground_at(-0.26, 0), 1)
        self.assertEqual(world.ground_at(-0.24, 0), 2)
        self.assertEqual(world.ground_at(0.26, 0), 3)

    def test_flat_plane_is_simplified(self):
        world = make_map(plane(9, 9))
        route = world.path((0, 0, 0), (8, 0, 8))
        self.assert_endpoints(route, (0, 0, 0), (8, 0, 8))
        self.assertEqual(len(route), 2)

    def test_horizontal_line_is_simplified(self):
        world = make_map(plane(12, 1))
        route = world.path((0, 0, 0), (11, 0, 0))
        self.assert_endpoints(route, (0, 0, 0), (11, 0, 0))
        self.assertEqual(len(route), 2)

    def test_steps_and_slope_both_directions(self):
        world = make_map([[x, x * 0.5, 0, 10] for x in range(8)])
        for start, goal in [((0, 0, 0), (7, 3.5, 0)),
                            ((7, 3.5, 0), (0, 0, 0))]:
            with self.subTest(start=start):
                self.assert_endpoints(world.path(start, goal), start, goal)

    def test_low_count_normal_step_is_kept(self):
        world = make_map([[0, 0, 0, 10], [1, 0.5, 0, 1],
                          [2, 1, 0, 10]])
        self.assertEqual(world.ground_at(1, 0), 0.5)
        self.assertIsNotNone(world.path((0, 0, 0), (2, 1, 0)))

    def test_max_step_inclusive(self):
        world = make_map([[0, 0, 0, 10],
                          [1, WalkMap.MAX_STEP, 0, 10]])
        self.assertIsNotNone(world.path((0, 0, 0),
                                       (1, WalkMap.MAX_STEP, 0)))

    def test_cliff_cannot_be_stepped_down_or_up(self):
        world = make_map([[0, 0, 0, 10], [1, 3, 0, 10]])
        self.assertIsNone(world.path((0, 0, 0), (1, 3, 0)))
        self.assertIsNone(world.path((1, 3, 0), (0, 0, 0)))

    def test_separated_platforms_are_disconnected(self):
        world = make_map(plane(3, 3) + plane(3, 3, x0=5))
        self.assertIsNone(world.path((1, 0, 1), (6, 0, 1)))

    def test_ground_layers_choose_nearest_height(self):
        world = make_map(plane(4, 3) + plane(4, 3, y=3))
        self.assertEqual(world.ground_at(1, 1, near_y=0.2), 0)
        self.assertEqual(world.ground_at(1, 1, near_y=2.8), 3)
        self.assertIn(world.ground_at(1, 1), (0, 3))

    def test_bridge_and_ground_do_not_merge(self):
        world = make_map(plane(5, 3) + plane(5, 3, y=3))
        route = world.path((0, 3, 1), (4, 3, 1))
        self.assert_endpoints(route, (0, 3, 1), (4, 3, 1))
        self.assertTrue(all(point[1] == 3 for point in route))
        self.assertIsNone(world.path((0, 0, 1), (4, 3, 1)))

    def test_nearest_walkable_prefers_same_layer(self):
        world = make_map([[0, 0, 0, 10], [4, 3, 0, 10]])
        self.assertEqual(world.nearest_walkable((0, 3, 0)), (4, 3, 0))

    def test_nearest_walkable_falls_back_to_nearest_3d(self):
        world = make_map([[0, 0, 0, 10], [10, 3, 0, 10]])
        self.assertEqual(world.nearest_walkable((0, 1.5, 0)), (0, 0, 0))

    def test_flying_noise_removed_without_dropping_bridge(self):
        points = plane(5, 5)
        points += [[2, 7, 2, 1], [1, 8, 2, 1]]
        points += [[x, 3, 4, 10] for x in range(5)]
        world = make_map(points)
        self.assertEqual(world.ground_at(2, 2, near_y=7), 0)
        self.assertEqual(world.ground_at(1, 2, near_y=8), 0)
        self.assertEqual(world.ground_at(2, 4, near_y=3), 3)

    def test_isolated_low_count_is_not_automatically_noise(self):
        world = make_map([[10, 9, 10, 1]])
        self.assertEqual(world.ground_at(10, 10), 9)

    def test_single_gap_is_crossable(self):
        world = make_map([[0, 0, 0, 10], [2, 0.5, 0, 10]])
        self.assert_endpoints(world.path((0, 0, 0), (2, 0.5, 0)),
                              (0, 0, 0), (2, 0.5, 0))

    def test_single_gap_with_cliff_is_not_crossable(self):
        world = make_map([[0, 0, 0, 10], [2, 3, 0, 10]])
        self.assertIsNone(world.path((0, 0, 0), (2, 3, 0)))

    def test_two_missing_cells_are_not_crossable(self):
        world = make_map([[0, 0, 0, 10], [3, 0, 0, 10]])
        self.assertIsNone(world.path((0, 0, 0), (3, 0, 0)))

    def test_other_layer_is_not_a_missing_cell(self):
        world = make_map([[0, 3, 0, 10], [1, 0, 0, 10],
                          [2, 3, 0, 10]])
        self.assertIsNone(world.path((0, 3, 0), (2, 3, 0)))

    def test_diagonal_cannot_cut_two_blocked_corners(self):
        world = make_map([[0, 0, 0, 10], [1, 0, 1, 10]])
        self.assertIsNone(world.path((0, 0, 0), (1, 0, 1)))

    def test_diagonal_requires_both_orthogonal_cells(self):
        world = make_map([[0, 0, 0, 10], [1, 0, 0, 10],
                          [1, 0, 1, 10]])
        route = world.path((0, 0, 0), (1, 0, 1))
        self.assert_endpoints(route, (0, 0, 0), (1, 0, 1))
        self.assertGreaterEqual(len(route), 3)

    def test_diagonal_corner_height_must_be_traversable(self):
        world = make_map([[0, 0, 0, 10], [1, 3, 0, 10],
                          [0, 3, 1, 10], [1, 0, 1, 10]])
        self.assertIsNone(world.path((0, 0, 0), (1, 0, 1)))

    def test_simplification_does_not_cross_l_shaped_hole(self):
        world = make_map([[0, 0, 0, 10], [1, 0, 0, 10],
                          [2, 0, 0, 10], [2, 0, 1, 10],
                          [2, 0, 2, 10]])
        route = world.path((0, 0, 0), (2, 0, 2))
        self.assert_endpoints(route, (0, 0, 0), (2, 0, 2))
        self.assertGreaterEqual(len(route), 3)

    def test_simplification_does_not_tunnel_over_height_peak(self):
        world = make_map([[0, 0, 0, 10], [1, 0.5, 0, 10],
                          [2, 1, 0, 10], [3, 0.5, 0, 10],
                          [4, 0, 0, 10]])
        route = world.path((0, 0, 0), (4, 0, 0))
        self.assert_endpoints(route, (0, 0, 0), (4, 0, 0))
        # 直接首尾连线会埋进山脊，必须保留高度转折。
        self.assertTrue(any(point[1] >= 1 for point in route))

    def test_same_start_goal(self):
        world = make_map([[0, 0, 0, 10]])
        self.assertEqual(world.path((0, 0, 0), (0, 0, 0)), [(0, 0, 0)])

    def test_path_snaps_offsets_to_ground_nodes(self):
        world = make_map(plane(5, 1))
        route = world.path((0.1, 0.1, 0.1), (3.9, 0.2, -0.1))
        self.assert_endpoints(route, (0, 0, 0), (4, 0, 0))

    def test_random_target_respects_component_and_distance(self):
        world = make_map(plane(5, 5) + plane(3, 3, x0=8))
        pos = (0, 0, 0)
        rng = random.Random(2026)
        for _ in range(80):
            target = world.random_target(pos, min_dist=2, max_dist=12, rng=rng)
            self.assertIsNotNone(target)
            self.assertLessEqual(target[0], 4)
            self.assertGreaterEqual(math.dist(pos, target), 2)
            self.assertLessEqual(math.dist(pos, target), 12)
            self.assertIsNotNone(world.path(pos, target))

    def test_random_target_layer_is_reachable(self):
        world = make_map(plane(5, 3) + plane(5, 3, y=3))
        for _ in range(20):
            target = world.random_target((0, 3, 1), 1, 5,
                                         rng=random.Random(_))
            self.assertIsNotNone(target)
            self.assertEqual(target[1], 3)

    def test_random_target_seed_is_reproducible(self):
        world = make_map(plane(8, 8))
        first, second = random.Random(42), random.Random(42)
        sequence_a = [world.random_target((0, 0, 0), 3, 7, first)
                      for _ in range(20)]
        sequence_b = [world.random_target((0, 0, 0), 3, 7, second)
                      for _ in range(20)]
        self.assertEqual(sequence_a, sequence_b)

    def test_random_target_prefers_high_count(self):
        world = make_map([[-1, 0, 0, 1], [0, 0, 0, 10],
                          [1, 0, 0, 100]])
        rng = random.Random(21)
        targets = [world.random_target((0, 0, 0), 1, 1, rng)
                   for _ in range(400)]
        self.assertGreater(targets.count((1, 0, 0)), 260)

    def test_random_target_without_candidate_returns_none(self):
        world = make_map([[0, 0, 0, 10]])
        self.assertIsNone(world.random_target((0, 0, 0), 3, 12,
                                              random.Random(1)))

    def test_max_nodes_budget_can_abandon_search(self):
        world = make_map(plane(20, 20))
        self.assertIsNone(world.path((0, 0, 0), (19, 0, 19), max_nodes=1))
        self.assertIsNone(world.path((0, 0, 0), (0, 0, 0), max_nodes=0))

    def test_large_map_path_finishes_within_two_seconds(self):
        world = make_map(plane(200, 200))
        begun = time.perf_counter()
        route = world.path((0, 0, 0), (199, 0, 199), max_nodes=5000)
        elapsed = time.perf_counter() - begun
        self.assertLess(elapsed, 2.0, f"大图寻路耗时 {elapsed:.3f} 秒")
        if route is not None:
            self.assert_endpoints(route, (0, 0, 0), (199, 0, 199))


    def test_invalid_cell_values_are_rejected(self):
        for value in (0, -1, math.nan, math.inf, True, "1"):
            with self.subTest(cell=value):
                with self.assertRaises(ValueError):
                    make_map([], cell=value)

    def test_nonfinite_sample_coordinates_are_rejected(self):
        for column in range(3):
            for value in (math.nan, math.inf, -math.inf):
                point = [0, 0, 0, 10]
                point[column] = value
                with self.subTest(column=column, value=value):
                    with self.assertRaises(ValueError):
                        make_map([point])

    def test_invalid_counts_are_rejected(self):
        for count in (0, -1, 1.5, True, "2"):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    make_map([[0, 0, 0, count]])

    def test_invalid_points_shapes_are_rejected(self):
        for data in ({}, {"points": None}, {"points": "bad"},
                     {"points": [[0, 0, 0]]},
                     {"points": [[0, 0, 0, 1, 2]]},
                     {"points": [None]}, []):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    WalkMap.from_json(data)

    def test_loading_does_not_modify_input(self):
        data = {"map_id": 17, "cell": 1,
                "points": [[0, 0, 0, 10], [1, 3, 0, 1],
                           [0, 0, 0, 2], [0, 3, 0, 10]]}
        before = json.dumps(data, sort_keys=True)
        world = WalkMap.from_json(data)
        world.path((0, 0, 0), (0, 3, 0))
        world.random_target((0, 0, 0), 0, 5, random.Random(0))
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_duplicate_observations_merge_counts_before_filtering(self):
        # 两条同格同高 count=1 合并为 count=2，不再属于低频飞行噪声。
        world = make_map([[0, 0, 0, 10], [0, 7, 0, 1],
                          [0, 7, 0, 1], [1, 0, 0, 10]])
        self.assertEqual(world.ground_at(0, 0, near_y=7), 7)

    def test_subclass_max_step_parameter_takes_effect(self):
        class TallerSteps(WalkMap):
            MAX_STEP = 0.9

        data = {"cell": 1, "points": [[0, 0, 0, 10], [1, 0.8, 0, 10]]}
        self.assertIsNone(WalkMap.from_json(data).path((0, 0, 0), (1, 0.8, 0)))
        self.assertIsNotNone(
            TallerSteps.from_json(data).path((0, 0, 0), (1, 0.8, 0)))

    def test_two_layers_can_connect_via_a_ramp(self):
        points = [[x, 0, 0, 10] for x in range(7)]
        points += [[6, 0.5, 1, 10], [6, 1, 2, 10],
                   [6, 1.5, 3, 10], [6, 2, 4, 10],
                   [5, 2.5, 4, 10]]
        points += [[x, 3, 4, 10] for x in range(5)]
        points += [[0, 3, z, 10] for z in range(4)]
        world = make_map(points)
        self.assertEqual(world.ground_at(0, 0, near_y=0), 0)
        self.assertEqual(world.ground_at(0, 0, near_y=3), 3)
        self.assert_endpoints(world.path((0, 0, 0), (0, 3, 0)),
                              (0, 0, 0), (0, 3, 0))

    def test_gap_endpoints_are_preserved_between_flat_segments(self):
        world = make_map([[x, 0, 0, 10] for x in (0, 1, 2, 4, 5, 6)])
        route = world.path((0, 0, 0), (6, 0, 0))
        self.assert_endpoints(route, (0, 0, 0), (6, 0, 0))
        self.assertIn((2, 0, 0), route)
        self.assertIn((4, 0, 0), route)
        self.assertEqual(route.index((4, 0, 0)),
                         route.index((2, 0, 0)) + 1)

    def test_random_distance_bounds_are_inclusive(self):
        world = make_map([[x, 0, 0, 10] for x in range(4)])
        self.assertEqual(world.random_target((0, 0, 0), 3, 3,
                                             random.Random(1)), (3, 0, 0))
        self.assertEqual(world.random_target((0, 0, 0), 0, 0,
                                             random.Random(1)), (0, 0, 0))

    def test_invalid_random_distance_ranges_are_rejected(self):
        world = make_map([[0, 0, 0, 10]])
        for low, high in ((-1, 2), (3, 2), (0, math.inf),
                          (math.nan, 2)):
            with self.subTest(low=low, high=high):
                with self.assertRaises(ValueError):
                    world.random_target((0, 0, 0), low, high)

    def test_one_node_budget_succeeds_for_same_node(self):
        world = make_map([[0, 0, 0, 10]])
        self.assertEqual(world.path((0, 0, 0), (0, 0, 0), max_nodes=1),
                         [(0, 0, 0)])

if __name__ == "__main__":
    unittest.main()

