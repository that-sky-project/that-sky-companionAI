"""Initial static collision-*candidate* scene assembly, not native physics.

Only a caller-provided resource resolver is used. No Lua, network, implicit path
search, or game operations occur. Native vectors are rows: [x,y,z,1] @ M.
Baked GEO is NOT merged, deduplicated, or inferred to be collision here.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable
import numpy as np
from .scene import Scene
from .formats._types import MeshData


@dataclass(frozen=True)
class AssemblyLimits:
    max_vertices: int = 2_000_000
    max_triangles: int = 4_000_000
    max_instances: int = 100_000

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in self.__dict__.values()):
            raise ValueError("assembly_limits_must_be_positive")


@dataclass(frozen=True)
class Assembly:
    positions: np.ndarray
    triangles: np.ndarray
    manifest: dict
    native_collision_verified: bool = False

    def write(self, directory: str | Path, *, map_name: str) -> Path:
        """Content-addressed little-endian arrays; manifest is atomic commit marker.

        Readers must consume only files named and hashed by the manifest. A
        failure before its final replace leaves no new committed manifest.
        Existing unrelated files are never removed.
        """
        if not isinstance(map_name, str) or not map_name.strip():
            raise ValueError("explicit_map_name_required")
        p = Path(directory)
        os.makedirs(_long(p), exist_ok=True)
        blobs = {"positions": np.asarray(self.positions, dtype="<f4").tobytes(order="C"),
                 "triangles": np.asarray(self.triangles, dtype="<u4").tobytes(order="C")}
        files = {}
        for key, blob in blobs.items():
            digest = hashlib.sha256(blob).hexdigest()
            name = key + "-" + digest + (".f32" if key == "positions" else ".u32")
            _atomic_write(p / name, blob)
            files[key] = {"file": name, "sha256": digest, "bytes": len(blob),
                          "dtype": "<f4" if key == "positions" else "<u4",
                          "shape": list(getattr(self,key).shape)}
        manifest = dict(self.manifest)
        manifest["files"] = files
        manifest.update(format="skyterrain-static-v1", mapName=map_name,
                        geometryKind="initial_static_collision_candidates",
                        nativeCollisionVerified=False,
                        positions=files["positions"]["file"], indices=files["triangles"]["file"],
                        vertexCount=len(self.positions), triangleCount=len(self.triangles),
                        sha256={"positions": files["positions"]["sha256"],
                                "indices": files["triangles"]["sha256"]})
        target = p / "manifest.json"
        _atomic_write(target, json.dumps(manifest, ensure_ascii=True, indent=2,
                                       allow_nan=False).encode("utf-8"))
        return target


def _long(path):
    p = os.fspath(path)
    if os.name != "nt" or not p or p.startswith("\\\\?\\"):
        return p
    p = p.replace("/", "\\")
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    if len(p) < 240:
        return p
    return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)


def _atomic_write(path, blob):
    path = Path(path)
    os.makedirs(_long(path.parent), exist_ok=True)
    # 固定短前缀：path.name 可能含 64 位 sha，再拼随机后缀会把短路径顶过 260。
    fd, temp = tempfile.mkstemp(prefix=".t-", suffix=".tmp", dir=_long(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, _long(path))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def assemble_initial_static(scene: Scene, resolver: Callable[[str], MeshData], *,
                            limits: AssemblyLimits | None = None) -> Assembly:
    limits = limits or AssemblyLimits()
    positions, triangles, included, excluded = [], [], [], []
    cache = {}
    vertex_total = triangle_total = 0
    # Unknown semantics fail closed. Instance-model fields not serialized in its
    # class are not silently borrowed from LevelMesh defaults.
    true_flags = ("enabled", "collision")
    false_flags = ("isTransformOnly", "isCloud", "animated", "networked",
                   "jumpGuardOnly", "passthrough")
    for node in scene.geometry_nodes():
        base = {"object_index": node.object_index, "name": node.name,
                "class_name": node.class_name, "resource_names": list(node.resource_names),
                "classification": node.classification}
        reasons = []
        for key in true_flags:
            if node.flags[key] is not True:
                reasons.append("flag_not_true:" + key)
        for key in false_flags:
            if node.flags[key] is not False:
                reasons.append("flag_not_false:" + key)
        if node.issues:
            reasons.extend(node.issues)
        if len(node.resource_names) != 1:
            reasons.append("resource_not_unique")
        if node.transform is None:
            reasons.append("transform_unusable")
        if reasons:
            excluded.append(dict(base, reasons=list(dict.fromkeys(reasons))))
            continue
        if len(included) >= limits.max_instances:
            excluded.append(dict(base, reasons=["instance_budget"]))
            continue
        resource = node.resource_names[0]
        if resource not in cache:
            try:
                mesh = resolver(resource)
                if not isinstance(mesh, MeshData):
                    raise TypeError("resolver_must_return_MeshData")
                cache[resource] = (mesh, None)
            except Exception as exc:
                cache[resource] = (None, "resource_unavailable:" + type(exc).__name__)
        mesh, error = cache[resource]
        if error:
            excluded.append(dict(base, reasons=[error]))
            continue
        if mesh.coordinate_space != "sky_native":
            reasons.append("non_native_coordinate_space")
        if mesh.animated:
            reasons.append("animated_mesh")
        if mesh.geometry_kind != "render_only":
            reasons.append("unsupported_geometry_semantics")
        pos = np.asarray(mesh.positions)
        tri = np.asarray(mesh.triangles)
        if pos.ndim != 2 or pos.shape[1:] != (3,) or pos.dtype.kind not in "fiu":
            reasons.append("invalid_positions")
        elif not np.isfinite(pos).all():
            reasons.append("non_finite_positions")
        if tri.ndim != 2 or tri.shape[1:] != (3,) or tri.dtype.kind not in "iu":
            reasons.append("invalid_triangles")
        elif tri.size and (np.any(tri < 0) or pos.ndim < 1 or int(tri.max()) >= len(pos)):
            reasons.append("invalid_triangle_index")
        if pos.ndim < 1 or tri.ndim < 1:
            excluded.append(dict(base, reasons=list(dict.fromkeys(reasons))))
            continue
        if not len(pos) or not len(tri):
            reasons.append("empty_geometry")
        if vertex_total + len(pos) > limits.max_vertices or triangle_total + len(tri) > limits.max_triangles:
            reasons.append("geometry_budget")
        matrix = np.asarray(node.transform, dtype=np.float64)
        determinant = float(np.linalg.det(matrix[:3,:3]))
        if not np.isfinite(determinant) or abs(determinant) <= 1e-12:
            reasons.append("singular_or_nonfinite_transform")
        if reasons:
            excluded.append(dict(base, reasons=list(dict.fromkeys(reasons))))
            continue
        # Native matrix rows, not a Blender/world-axis remapping.
        world = np.asarray(pos, dtype=np.float64) @ matrix[:3,:3] + matrix[3,:3]
        if not np.isfinite(world).all() or np.any(np.abs(world) > np.finfo(np.float32).max):
            excluded.append(dict(base, reasons=["transformed_position_overflow"]))
            continue
        outpos = np.asarray(world, dtype="<f4")
        outtri = np.asarray(tri, dtype=np.uint64).copy()
        reversed_winding = determinant < 0
        if reversed_winding:
            outtri[:,[1,2]] = outtri[:,[2,1]]
        outtri += vertex_total
        if outtri.size and int(outtri.max()) > np.iinfo(np.uint32).max:
            excluded.append(dict(base, reasons=["index_overflow"]))
            continue
        positions.append(outpos)
        triangles.append(np.asarray(outtri, dtype="<u4"))
        included.append(dict(base, resource=resource, mesh_source=mesh.source,
                             mesh_version=mesh.version, transform=node.transform,
                             flags=node.flags, group_indices=node.resource_group_indices,
                             vertex_start=vertex_total, vertex_count=len(pos),
                             triangle_start=triangle_total, triangle_count=len(tri),
                             determinant=determinant, winding_reversed=reversed_winding,
                             world_bounds_min=outpos.min(axis=0).tolist(),
                             world_bounds_max=outpos.max(axis=0).tolist(),
                             geometry_kind="initial_static_collision_candidate",
                             native_collision_verified=False))
        vertex_total += len(pos)
        triangle_total += len(tri)
    p = np.concatenate(positions) if positions else np.empty((0,3), dtype="<f4")
    t = np.concatenate(triangles) if triangles else np.empty((0,3), dtype="<u4")
    p.setflags(write=False);t.setflags(write=False)
    manifest = {"schema_version": "sky-initial-static-assembly-v1", "scene_source": scene.source,
                "coordinate_space": "sky_native", "matrix_convention": "row_vector_xyz1_at_M",
                "geometry_kind": "initial_static_collision_candidate",
                "native_collision_verified": False, "runtime_ready": False,
                "limitations": ["serialized_initial_flags_only", "dynamic_events_not_evaluated",
                                "baked_GEO_not_merged_or_deduplicated",
                                "resource_resolver_provenance_required", "not_navigation_mesh",
                                "not_complete_native_collision_scene"],
                "vertex_count": vertex_total, "triangle_count": triangle_total,
                "included": included, "excluded": excluded}
    return Assembly(p, t, manifest)
