"""Real-geometry view for the air planner.

The walking worker keeps a ``skyterrain.ground.GroundSurface`` in
``BoundWorkerBackend._ground_surface``.  ``PoseController.terrain_ground()``
only exposes ``height(x, z, near_y)``; the air planner additionally needs
``hits(x, z)`` to check headroom and the flight corridor, so this module walks
the existing adapter object graph and wraps the *same* surface.

Coordinate convention: ``GroundSurface`` works in geometry coordinates.  The
walking backend converts with ``offset`` (``origin_offset``/foot offset) before
returning Actor-origin coordinates.  Every method here returns/accepts Actor
origin coordinates, exactly like ``PoseController.terrain_ground``.

If the surface object cannot be reached but the adapter has ``terrain_ground``,
``HeightOnlyTerrain`` is returned.  It has ``has_hits=False``; the planner will
refuse to fly/drop on it (height alone cannot prove headroom), and the caller
falls back to the existing walking logic.
"""
from __future__ import annotations

import math


def _vector(value):
    if value is None:
        return None
    try:
        out = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if len(out) == 3 and all(math.isfinite(v) for v in out):
        return out
    return None


def _surface_candidates(adapter):
    """Yield (surface, offset) pairs reachable from the current adapter.

    Supported live object graphs (0.16.8 release tree):

    * ``BoundWorkerBackend._ground_surface`` (direct backend)
    * ``BoundedPilotRuntime.backend._ground_surface``
    * ``ControllerTerrainBridge._inner.backend._ground_surface``

    The offset lives on the ``WorkerBackend`` instance (``backend.offset``).
    """
    seen = set()
    queue = [adapter]
    while queue:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        surface = getattr(node, "_ground_surface", None)
        if surface is not None and callable(getattr(surface, "hits", None)) \
                and callable(getattr(surface, "height", None)):
            offset = _vector(getattr(node, "offset", None)) or (0.0, 0.0, 0.0)
            yield surface, offset
            continue
        for attr in ("_inner", "backend", "_backend"):
            child = getattr(node, attr, None)
            if child is not None:
                queue.append(child)


class TerrainView:
    """GroundSurface + actor-origin offset; same numbers as terrain_ground."""

    has_hits = True

    def __init__(self, surface, offset=(0.0, 0.0, 0.0), *, source="surface"):
        self.surface = surface
        self.offset = _vector(offset) or (0.0, 0.0, 0.0)
        self.source = source

    def _local(self, x, z):
        ox, _oy, oz = self.offset
        return float(x) - ox, float(z) - oz

    def hits(self, x, z):
        """[(triangle_id, y, |normal_y|)] in actor-origin coordinates, y ascending."""
        lx, lz = self._local(x, z)
        oy = self.offset[1]
        try:
            rows = self.surface.hits(lx, lz)
        except Exception:
            return []
        return [(int(i), float(y) + oy, float(ny)) for i, y, ny in rows]

    def height(self, x, z, near_y=None):
        """Nearest standable ground to ``near_y`` (or the highest layer)."""
        lx, lz = self._local(x, z)
        oy = self.offset[1]
        try:
            raw = (self.surface.height(lx, lz) if near_y is None
                   else self.surface.height(lx, lz, float(near_y) - oy))
        except Exception:
            return None
        if raw is None:
            return None
        value = float(raw) + oy
        return value if math.isfinite(value) else None

    def __repr__(self):
        return f"TerrainView({self.source}, offset={self.offset})"

    def capsule_probe(self):
        """只读共享三角网；与地面 motor 一致的 .25 / 1.5 / .015 胶囊。"""
        from skyterrain.capsule import CapsuleProbe
        return CapsuleProbe(self)


class HeightOnlyTerrain:
    """Adapter fallback: ground height only, no corridor/headroom evidence."""

    has_hits = False

    def __init__(self, method):
        self._method = method
        self.offset = (0.0, 0.0, 0.0)
        self.source = "adapter.terrain_ground"

    def hits(self, x, z):
        return []

    def height(self, x, z, near_y=None):
        try:
            raw = (self._method(x, z) if near_y is None
                   else self._method(x, z, near_y))
            value = float(raw)
        except Exception:
            return None
        return value if math.isfinite(value) else None


def terrain_view(adapter):
    """Return a planner terrain for ``adapter`` or ``None``.

    This never loads geometry; it only exposes what the existing walking
    backend already loaded.  If no surface is reachable the caller must keep
    the legacy walking behaviour.
    """
    if adapter is None:
        return None
    for surface, offset in _surface_candidates(adapter):
        return TerrainView(surface, offset)
    method = getattr(adapter, "terrain_ground", None)
    if callable(method):
        return HeightOnlyTerrain(method)
    return None


def ground_below(terrain, x, z, y, *, max_drop=None, up_normal=0.65):
    """Highest standable hit at/below ``y`` (used by AirMotion.cancel)."""
    best = None
    # 有完整几何时高度命中不再等价于可落脚；取消下落也不得选墙内的脚下点。
    probe = terrain.capsule_probe() if callable(getattr(terrain, 'capsule_probe', None)) else None
    for _i, hy, ny in terrain.hits(x, z):
        if ny < up_normal or hy > y + 0.05:
            continue
        if max_drop is not None and y - hy > max_drop:
            continue
        if best is None or hy > best:
            best = hy
    if best is not None:
        # 最高表面若无法容纳身体，不能悄悄选更低一层并穿过该表面落下。
        return best if probe is None or probe.standable((x,best,z)) else None
    h = terrain.height(x, z, y)
    if h is not None and h <= y + 0.05 and (probe is None or probe.standable((x,h,z))):
        return h
    return None
