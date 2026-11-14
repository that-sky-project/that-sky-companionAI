# Server snapshot offline suite, client imports only; assertions unchanged.
import sys as _sys
from pathlib import Path as _Path
_app = _Path(__file__).resolve().parents[1] / "app"
_sys.path[:0] = [str(_app), str(_app / "bot"), str(_app / "src")]
"""离线单元测试：真实地形地面查询（假几何，不读线上资产、不连服务器）。

跑法（在 bot 目录下，用 DSH 自带 python）：
    python -m unittest test_terrain_ground -v

覆盖：平地、斜坡、双层地面按参考高度选层、网格外/几何没加载返回 None、
manifest 读取与哈希校验、PoseController.terrain_ground 用的 query() 转发。
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

# client import bootstrap above

import numpy as np  # noqa: E402

from skyterrain.ground import GroundSurface, query  # noqa: E402


def quad(y, x0=-10.0, x1=10.0, z0=-10.0, z1=10.0, y1=None):
    """一个朝上的矩形（两个三角形）；y1 给定时沿 x 从 y 斜到 y1。"""
    y1 = y if y1 is None else y1
    positions = np.array([[x0, y, z0], [x1, y1, z0],
                          [x1, y1, z1], [x0, y, z1]], dtype="<f4")
    triangles = np.array([[0, 2, 1], [0, 3, 2]], dtype="<u4")
    return positions, triangles


def merged(*parts):
    positions, triangles, base = [], [], 0
    for p, t in parts:
        positions.append(p)
        triangles.append(t + base)
        base += len(p)
    return np.vstack(positions), np.vstack(triangles)


class GroundSurfaceTest(unittest.TestCase):
    def test_flat_plane(self):
        surface = GroundSurface(*quad(2.0))
        self.assertAlmostEqual(surface.height(0.0, 0.0), 2.0, places=5)
        self.assertAlmostEqual(surface.height(-9.9, 9.9), 2.0, places=5)

    def test_slope(self):
        surface = GroundSurface(*quad(0.0, y1=2.0))
        self.assertAlmostEqual(surface.height(0.0, 0.0), 1.0, places=5)
        self.assertAlmostEqual(surface.height(5.0, 0.0), 1.5, places=5)
        self.assertAlmostEqual(surface.height(-5.0, 0.0), 0.5, places=5)

    def test_double_layer_chooses_nearest_reference(self):
        surface = GroundSurface(*merged(quad(0.0), quad(5.0)))
        self.assertAlmostEqual(surface.height(0.0, 0.0, 0.2), 0.0, places=5)
        self.assertAlmostEqual(surface.height(0.0, 0.0, 4.8), 5.0, places=5)
        self.assertAlmostEqual(surface.height(0.0, 0.0, 0.9), 0.0, places=5)
        self.assertAlmostEqual(surface.height(0.0, 0.0), 5.0, places=5)   # 没参考高度取最高层

    def test_outside_returns_none(self):
        surface = GroundSurface(*quad(0.0))
        self.assertIsNone(surface.height(100.0, 100.0, 0.0))
        self.assertIsNone(surface.height(0.0, 0.0, 100.0))   # 参考高度离地面太远
        self.assertIsNone(surface.height(float("nan"), 0.0, 0.0))

    def test_query_delegates_and_swallows_failures(self):
        surface = GroundSurface(*quad(0.0))

        class Adapter:
            def __init__(self, offset):
                self.offset = offset

            def terrain_ground(self, x, z, near_y=None):
                ox, oy, oz = self.offset
                raw = surface.height(x - ox, z - oz,
                                     None if near_y is None else near_y - oy)
                return None if raw is None else raw + oy

        self.assertAlmostEqual(query(Adapter((0.0, 0.5, 0.0)), 0.0, 0.0, 0.5), 0.5, places=5)
        self.assertAlmostEqual(query(Adapter((0.0, 0.5, 0.0)), 0.0, 0.0, 0.0), 0.5, places=5)
        self.assertIsNone(query(Adapter((0.0, 0.5, 0.0)), 100.0, 100.0, 0.0))
        self.assertIsNone(query(None, 0.0, 0.0, 0.0))
        self.assertIsNone(query(object(), 0.0, 0.0, 0.0))

        class Broken:
            def terrain_ground(self, x, z, near_y=None):
                raise RuntimeError("boom")

        class NonFinite:
            def terrain_ground(self, x, z, near_y=None):
                return float("inf")

        self.assertIsNone(query(Broken(), 0.0, 0.0, 0.0))
        self.assertIsNone(query(NonFinite(), 0.0, 0.0, 0.0))

    def test_from_manifest_uses_same_static_world_files(self):
        positions, triangles = quad(3.0)
        pbytes = positions.astype("<f4").tobytes()
        tbytes = triangles.astype("<u4").tobytes()
        manifest = {
            "format": "skyterrain-static-v1",
            "mapName": "FakeMap",
            "positions": "positions.f32",
            "indices": "indices.u32",
            "vertexCount": len(positions),
            "triangleCount": len(triangles),
            "sha256": {"positions": hashlib.sha256(pbytes).hexdigest(),
                       "indices": hashlib.sha256(tbytes).hexdigest()},
        }
        raw = json.dumps(manifest).encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "positions.f32").write_bytes(pbytes)
            (root / "indices.u32").write_bytes(tbytes)
            path = root / "runtime-manifest.json"
            path.write_bytes(raw)
            surface = GroundSurface.from_manifest(
                path, expected_sha256=hashlib.sha256(raw).hexdigest(), map_name="FakeMap")
            self.assertAlmostEqual(surface.height(0.0, 0.0, 3.0), 3.0, places=5)
            with self.assertRaises(ValueError):
                GroundSurface.from_manifest(path, expected_sha256="0" * 64)

    def test_rejects_bad_geometry(self):
        positions, triangles = quad(0.0)
        with self.assertRaises(ValueError):
            GroundSurface(positions, np.array([[0, 1, 99]], dtype="<u4"))
        with self.assertRaises(ValueError):
            GroundSurface(np.empty((0, 3), dtype="<f4"),
                          np.empty((0, 3), dtype="<u4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
