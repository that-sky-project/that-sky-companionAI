"""按 (x, z) 查已加载静态世界几何的真实地面高度。

数据就是走路 worker 用的同一份 ``skyterrain-static-v1`` 静态世界：
``assetRoot/<scene>/static-world/runtime-manifest.json`` 指向的
``positions``/``indices`` 两个数组。这里不重新解码场景、不重建网格，
只把这两个数组读进内存后用竖直射线取地面，供定点站位使用。

坐标约定：返回值和输入都在**几何坐标**里；调用方（BoundWorkerBackend）
自己加上 ``origin_offset``，把结果换算成玩家下行坐标（Actor 原点）。
这不是 native 碰撞，也没有 headroom/胶囊体校验，只回答“这个 xz 下面
朝上的地面在哪一层”。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARRAY_BYTES = 128 * 1024 * 1024
DEFAULT_BUCKET = 4.0
MAX_CELLS_PER_TRIANGLE = 4096
MIN_UP_NORMAL_Y = 0.65       # 只认朝上的面；和 T1 落点体检的阈值一致
MAX_ABOVE_REFERENCE = 1.0    # 参考高度上面 1 米内仍算这一层（落地/插值误差）
MAX_BELOW_REFERENCE = 60.0   # 参考高度下面超过 60 米就不再往下找（防止掉到远景地面）


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _basename(value, label):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("geometry_" + label + "_path")
    path = Path(value)
    if path.name != value or path.is_absolute():
        raise ValueError("geometry_" + label + "_path")
    return value


class GroundSurface:
    """Static-world triangle set with a sparse XZ bucket index.

    ``height(x, z, near_y)`` chooses, among up-facing triangles hit by a
    vertical line, the one closest to ``near_y`` (双层地面用它选层)。
    ``near_y=None`` 时取最高的一层。点不在任何三角形上返回 None。
    """

    def __init__(self, positions, triangles, *, bucket: float = DEFAULT_BUCKET):
        p = np.asarray(positions, dtype="<f4")
        t = np.asarray(triangles, dtype="<u4")
        if p.ndim != 2 or p.shape[1:] != (3,) or t.ndim != 2 or t.shape[1:] != (3,):
            raise ValueError("geometry_shape")
        if not len(p) or not len(t):
            raise ValueError("geometry_empty")
        if not np.isfinite(p).all():
            raise ValueError("geometry_nonfinite")
        if t.size and int(t.max(initial=0)) >= len(p):
            raise ValueError("geometry_index_out_of_range")
        if isinstance(bucket, bool) or not isinstance(bucket, (int, float)) \
                or not math.isfinite(bucket) or bucket <= 0:
            raise ValueError("geometry_bucket")

        self.vertex_count, self.triangle_count = len(p), len(t)
        self.bucket = float(bucket)
        self._tri = p.astype(np.float64)[t]          # (M, 3, 3)
        cross = np.cross(self._tri[:, 1] - self._tri[:, 0],
                         self._tri[:, 2] - self._tri[:, 0])
        length = np.linalg.norm(cross, axis=1)
        # 走路几何由 RAPIER 当双面 trimesh 用，绕序不可靠；用 |ny| 只排除立面/天花板。
        self._ny = np.divide(np.abs(cross[:, 1]), length,
                             out=np.zeros_like(length), where=length > 1e-12)
        self._lo = self._tri.min(axis=1)
        self._hi = self._tri.max(axis=1)
        self._cells = {}
        self._global = []
        self._build_index()
        self._empty = np.empty(0, dtype=np.int32)
        self._global = np.asarray(self._global, dtype=np.int32)

    def _build_index(self):
        cells, global_ids = {}, []
        b = self.bucket
        for i in range(self.triangle_count):
            x0 = int(math.floor((self._lo[i, 0] - 1e-6) / b))
            x1 = int(math.floor((self._hi[i, 0] + 1e-6) / b))
            z0 = int(math.floor((self._lo[i, 2] - 1e-6) / b))
            z1 = int(math.floor((self._hi[i, 2] + 1e-6) / b))
            if (x1 - x0 + 1) * (z1 - z0 + 1) > MAX_CELLS_PER_TRIANGLE:
                # 超大三角形（远景/整片地面）不展开，查询时和本格一起算。
                global_ids.append(i)
                continue
            for ix in range(x0, x1 + 1):
                for iz in range(z0, z1 + 1):
                    cells.setdefault((ix, iz), []).append(i)
        self._cells = {key: np.asarray(ids, dtype=np.int32) for key, ids in cells.items()}
        self._global = global_ids

    def _ids(self, x: float, z: float):
        ix = int(math.floor(x / self.bucket))
        iz = int(math.floor(z / self.bucket))
        local = self._cells.get((ix, iz), self._empty)
        if not len(self._global):
            return local
        if not len(local):
            return self._global
        return np.concatenate((local, self._global))

    def hits(self, x: float, z: float):
        """所有竖直命中：[(triangle_id, y, |normal_y|), ...]，按 y 升序。"""
        ids = self._ids(x, z)
        if not len(ids):
            return []
        tri = self._tri[ids]
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        d = (b[:, 2] - c[:, 2]) * (a[:, 0] - c[:, 0]) \
            + (c[:, 0] - b[:, 0]) * (a[:, 2] - c[:, 2])
        with np.errstate(divide="ignore", invalid="ignore"):
            u = ((b[:, 2] - c[:, 2]) * (x - c[:, 0])
                 + (c[:, 0] - b[:, 0]) * (z - c[:, 2])) / d
            v = ((c[:, 2] - a[:, 2]) * (x - c[:, 0])
                 + (a[:, 0] - c[:, 0]) * (z - c[:, 2])) / d
            valid = (np.abs(d) > 1e-10) & (u >= -1e-7) & (v >= -1e-7) \
                & (u + v <= 1.0000001)
            ys = u * a[:, 1] + v * b[:, 1] + (1.0 - u - v) * c[:, 1]
        out = [(int(ids[k]), float(ys[k]), float(self._ny[ids[k]]))
               for k in np.flatnonzero(valid)]
        out.sort(key=lambda row: row[1])
        return out

    def height(self, x: float, z: float, near_y: float | None = None):
        """最近一层可站地面的几何 y；不在网格上/没有朝上的面返回 None。"""
        try:
            x, z = float(x), float(z)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(z):
            return None
        hits = [row for row in self.hits(x, z) if row[2] >= MIN_UP_NORMAL_Y]
        if not hits:
            return None
        if near_y is None:
            return hits[-1][1]
        try:
            near_y = float(near_y)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(near_y):
            return None
        candidates = [row for row in hits
                      if near_y - MAX_BELOW_REFERENCE <= row[1] <= near_y + MAX_ABOVE_REFERENCE]
        if not candidates:
            return None
        return min(candidates, key=lambda row: (abs(row[1] - near_y), row[1]))[1]

    ground = height

    @classmethod
    def from_manifest(cls, path, *, expected_sha256: str | None = None,
                      map_name: str | None = None, bucket: float = DEFAULT_BUCKET):
        """从 skyterrain-static-v1 manifest 读同一份走路几何（含哈希校验）。"""
        manifest = Path(path).resolve(strict=True)
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("geometry_manifest_budget")
        raw = manifest.read_bytes()
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or _sha256(raw) != expected_sha256:
                raise ValueError("geometry_manifest_hash")
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("format") != "skyterrain-static-v1":
            raise ValueError("geometry_manifest_format")
        if map_name is not None and doc.get("mapName") != map_name:
            raise ValueError("geometry_manifest_map")
        blobs = []
        for key, label in (("positions", "positions"), ("indices", "indices")):
            target = manifest.parent / _basename(doc.get(key), label)
            if target.stat().st_size > MAX_ARRAY_BYTES:
                raise ValueError("geometry_array_budget")
            blob = target.read_bytes()
            hashes = doc.get("sha256")
            if not isinstance(hashes, dict) or not isinstance(hashes.get(key), str) \
                    or _sha256(blob) != hashes[key]:
                raise ValueError("geometry_array_hash")
            blobs.append(blob)
        positions = np.frombuffer(blobs[0], dtype="<f4").reshape(-1, 3)
        triangles = np.frombuffer(blobs[1], dtype="<u4").reshape(-1, 3)
        if len(positions) != doc.get("vertexCount") or len(triangles) != doc.get("triangleCount"):
            raise ValueError("geometry_array_count")
        return cls(positions, triangles, bucket=bucket)


def query(adapter, x, z, near_y=None):
    """PoseController.terrain_ground 的实际入口：只转发，失败/没有几何返回 None。

    adapter 是走路用的地形适配器（BoundedPilotRuntime / ControllerTerrainBridge）；
    它没有加载几何、没有 terrain_ground 方法或查询抛错时都返回 None，
    调用方再用参考玩家的脚下 y 兜底。
    """
    method = getattr(adapter, "terrain_ground", None)
    if not callable(method):
        return None
    try:
        value = float(method(x, z, near_y))
    except Exception:
        return None
    return value if math.isfinite(value) else None
