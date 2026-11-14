"""有界动态道具几何；运行时纯内存，无文件 I/O。

观测 profile 默认关闭，并绑定样本放置者、资源与变换。未知道具不生成
猜测碰撞体；观测面或渲染候选网格均不声称原生碰撞已验证。"""
from __future__ import annotations

import math
import hashlib
import json
import re
import copy
import threading
import zlib
from pathlib import Path
from collections import OrderedDict
from collections.abc import Mapping
import numpy as np
from .ground import GroundSurface

MAX_PROPS = 64
MAX_VERTICES_PER_PROP = 2048
MAX_TRIANGLES_PER_PROP = 2560
MAX_VERTICES = 32768
MAX_TRIANGLES = 16384
MAX_COORDINATE = 5000.0

_DEFAULT_CATALOG = {'schema': 'skyterrain-props-v1', 'coordinate_space': 'sky_native', 'runtime_default_enabled': False, 'props': {}}


def _vector(value, count, label):
    if not isinstance(value, (tuple, list)) or len(value) != count:
        raise ValueError(label)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in value):
        raise ValueError(label)
    return np.asarray(value, dtype=np.float64)


def _rotation(value):
    q = _vector(value, 4, "prop_rotation")
    norm = float(np.linalg.norm(q))
    if not 0.5 <= norm <= 1.5:
        raise ValueError("prop_rotation_norm")
    x, y, z, w = q / norm
    return np.asarray(((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)),
                       (2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)),
                       (2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y))))


def _observation_matches(prop, profile, position, rotation, scale):
    match = profile.get("observed_placement")
    if not isinstance(match, Mapping):
        return False
    def owner(value):
        import uuid
        try:
            return uuid.UUID(str(value)).hex
        except (ValueError, TypeError, AttributeError):
            return None
    if owner(prop.get("owner_uuid")) is None or owner(prop.get("owner_uuid")) != owner(match.get("owner_uuid")):
        return False
    if prop.get("resource_id") != match.get("resource_id"):
        return False
    try:
        p = _vector(match.get("position"), 3, "observed_position")
        r = _rotation(match.get("rotation"))
        return (float(np.linalg.norm(position-p)) <= 0.025
                and float(np.max(np.abs(rotation-r))) <= 0.002
                and abs(scale-float(match["scale"])) <= 0.001)
    except (ValueError, TypeError, KeyError):
        return False


MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_CATALOG_PROFILES = 128
MAX_CATALOG_VERTICES = 131072
MAX_CATALOG_TRIANGLES = 262144
MAX_INDEX_PROFILES = 1024
MAX_SHARD_BYTES = 512 * 1024
MAX_PRELOAD_RAW_BYTES = 64 * 1024 * 1024
MAX_PRELOAD_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DECODED_PROFILES = 32
MAX_JSON_DEPTH = 32
_PROFILE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,95}\Z")


class IndexedPropCatalog(dict):
    """已冷读验证的索引和压缩 RAM 几何；热调用只有有界内存 LRU 解码。

    编码池没有路径读取能力。每个 binding deepcopy 后拥有独立 LRU/锁，
    不共享调用者可变 profile；只共享不可变压缩 bytes。
    """
    def __init__(self, doc, encoded, preload_raw_bytes):
        super().__init__(doc)
        self._encoded = dict(encoded)
        self._decoded = OrderedDict()
        self._cache_lock = threading.RLock()
        self._preload_raw_bytes = preload_raw_bytes
        self._hits = self._misses = self._evictions = 0

    def __deepcopy__(self, memo):
        found = memo.get(id(self))
        if found is not None:
            return found
        result = type(self)({}, self._encoded, self._preload_raw_bytes)
        memo[id(self)] = result
        result.update(copy.deepcopy(dict(self), memo))
        return result

    def resolve(self, name):
        row = self.get("props", {}).get(name)
        if not isinstance(row, Mapping) or row.get("runtime_eligible") is not True or name not in self._encoded:
            return None
        with self._cache_lock:
            if name in self._decoded:
                self._hits += 1
                value = self._decoded.pop(name)
                self._decoded[name] = value
                return copy.deepcopy(value)
            self._misses += 1
            # 数据来自冷加载时已验证并重新编码的 bytes，不接受 RPC 压缩流。
            unpacker = zlib.decompressobj()
            try:
                raw = unpacker.decompress(self._encoded[name], MAX_SHARD_BYTES + 1)
            except zlib.error as error:
                raise ValueError("prop_memory_decode_invalid") from error
            if len(raw) > MAX_SHARD_BYTES or not unpacker.eof or unpacker.unused_data or unpacker.unconsumed_tail:
                raise ValueError("prop_memory_decode_budget")
            value = json.loads(raw)
            self._decoded[name] = value
            if len(self._decoded) > MAX_DECODED_PROFILES:
                self._decoded.popitem(last=False)
                self._evictions += 1
            return copy.deepcopy(value)

    def cache_info(self):
        with self._cache_lock:
            return {"mode": "cold_verified_compressed_ram_with_memory_lru", "preloaded": len(self._encoded),
                    "raw_bytes": self._preload_raw_bytes,
                    "compressed_bytes": sum(map(len, self._encoded.values())),
                    "decoded": len(self._decoded), "capacity": MAX_DECODED_PROFILES,
                    "hits": self._hits, "misses": self._misses, "evictions": self._evictions}


def _parse_catalog_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("prop_catalog_duplicate_key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("prop_catalog_nonfinite_json:" + value)
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise ValueError("prop_catalog_json:" + str(error)[:120]) from error


def _check_catalog_depth(doc):
    # 迭代检查，避免未知审计 metadata 的深层容器击穿后续 deepcopy。
    stack = [(iter((doc,)), 0)]
    while stack:
        iterator, depth = stack[-1]
        try:
            value = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        if isinstance(value, (dict, list)):
            if depth >= MAX_JSON_DEPTH:
                raise ValueError("prop_catalog_depth_budget")
            stack.append((iter(value.values() if isinstance(value, dict) else value), depth + 1))


def _reject_link_components(path):
    """冷路径逐级拒绝 symlink/junction，避免本地名称绕到 UNC 或目录外。"""
    for component in reversed((path, *path.parents)):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("prop_catalog_link_path")


def _load_index_catalog(path, doc):
    """明确冷路径：所有 eligible shard 在发布目录前完成读入/hash/结构验证。"""
    if (doc.get("coordinate_space") != "sky_native" or type(doc.get("props")) is not dict
            or not 1 <= len(doc["props"]) <= MAX_INDEX_PROFILES):
        raise ValueError("prop_index_schema")
    entries = doc["props"]
    declared_bytes = 0
    for name, row in entries.items():
        if type(name) is not str or not _PROFILE_NAME.fullmatch(name) or type(row) is not dict:
            raise ValueError("prop_index_profile")
        status = row.get("status")
        if (status not in ("ready", "no_geometry", "ineligible", "ambiguous")
                or type(row.get("runtime_eligible")) is not bool
                or row["runtime_eligible"] != (status == "ready")):
            raise ValueError("prop_index_status")
        reasons = row.get("reasons", [])
        if type(reasons) is not list or len(reasons) > 32 or any(type(r) is not str or len(r) > 256 for r in reasons):
            raise ValueError("prop_index_reasons")
        if status != "ready":
            continue
        length = row.get("byte_length")
        if type(length) is not int or not 0 < length <= MAX_SHARD_BYTES:
            raise ValueError("prop_index_shard_size")
        declared_bytes += length
        if declared_bytes > MAX_PRELOAD_RAW_BYTES:
            raise ValueError("prop_index_preload_raw_budget")
    root = path.parent.resolve(strict=True)
    document, encoded = copy.deepcopy(doc), {}
    failures, raw_total, compressed_total = [], 0, 0
    for name, row in document["props"].items():
        if row["runtime_eligible"] is not True:
            continue
        try:
            rel = row.get("shard")
            if (type(rel) is not str or not 1 <= len(rel) <= 256 or "\\" in rel or ":" in rel
                    or rel.startswith("/") or any(p in ("", ".", "..") for p in rel.split("/"))
                    or not rel.endswith(".json")):
                raise ValueError("prop_shard_relative_path")
            candidate = root.joinpath(*rel.split("/"))
            _reject_link_components(candidate)
            if not candidate.resolve(strict=True).is_relative_to(root):
                raise ValueError("prop_shard_path_escape")
            digest = row.get("sha256")
            if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("prop_shard_sha256")
            if row.get("source_kind") not in ("extracted_collision", "render_mesh_candidate", "observation_patch"):
                raise ValueError("prop_shard_source_kind")
            remaining = MAX_PRELOAD_RAW_BYTES - raw_total
            if remaining <= 0:
                raise ValueError("prop_index_preload_raw_budget")
            with candidate.open("rb") as stream:
                raw = stream.read(min(MAX_SHARD_BYTES, remaining) + 1)
            raw_total += len(raw)
            if raw_total > MAX_PRELOAD_RAW_BYTES:
                raise ValueError("prop_index_preload_raw_budget")
            if len(raw) != row["byte_length"] or len(raw) > MAX_SHARD_BYTES:
                raise ValueError("prop_shard_byte_length")
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("prop_shard_hash_mismatch")
            shard_doc = _parse_catalog_json(raw)
            _check_catalog_depth(shard_doc)
            shard = _validate_inline_catalog(shard_doc)
            if set(shard["props"]) != {name}:
                raise ValueError("prop_shard_name_binding")
            profile = shard["props"][name]
            if (type(row.get("vertex_count")) is not int or type(row.get("triangle_count")) is not int
                    or row["vertex_count"] != len(profile["positions"]) // 3
                    or row["triangle_count"] != len(profile["triangles"]) // 3
                    or row["source_kind"] != profile["source_kind"]):
                raise ValueError("prop_shard_profile_binding")
            kept = {k: profile[k] for k in ("source_kind", "coordinate_space", "native_collision_verified",
                "resource_path", "positions", "triangles", "observed_placement",
                "collision_representation", "approximation") if k in profile}
            canonical = json.dumps(kept, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(canonical) > MAX_SHARD_BYTES:
                raise ValueError("prop_shard_canonical_budget")
            packed = zlib.compress(canonical, 6)
            compressed_total += len(packed)
            if compressed_total > MAX_PRELOAD_COMPRESSED_BYTES:
                raise ValueError("prop_index_preload_compressed_budget")
            encoded[name] = packed
        except (OSError, ValueError, TypeError, OverflowError) as error:
            reason = str(error)[:256]
            row.update(status="no_geometry", runtime_eligible=False)
            row["reasons"] = list(row.get("reasons", []))[:31] + [reason]
            failures.append({"name": name, "reason": reason})
    document["load_errors"] = failures
    return IndexedPropCatalog(document, encoded, raw_total)


def load_catalog(path, *, expected_sha256=None):
    """明确冷路径：读取本地目录与其受限分片，严校验后返回内存目录。

    本函数不会被 build_colliders 或高度/空中查询自动调用。禁止外部
    数组引用；索引仅读取同目录下带哈希/大小的 JSON 分片，不打开 APK。
    """
    try:
        p = Path(path)
    except (TypeError, ValueError):
        raise ValueError("prop_catalog_path") from None
    if not p.is_absolute() or str(p).startswith(("\\\\", "//")) or p.suffix.lower() != ".json" or p.is_symlink():
        raise ValueError("prop_catalog_local_absolute_json_required")
    _reject_link_components(p)
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
        raise ValueError("prop_catalog_sha256")
    try:
        with p.open("rb") as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
    except OSError as error:
        raise ValueError("prop_catalog_read:" + type(error).__name__) from error
    if not raw or len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("prop_catalog_byte_budget")
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("prop_catalog_hash_mismatch")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("prop_catalog_duplicate_key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("prop_catalog_nonfinite_json:" + value)
    try:
        doc = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise ValueError("prop_catalog_json:" + str(error)[:120]) from error
    try:
        _check_catalog_depth(doc)
        if isinstance(doc, dict) and doc.get("schema") == "skyterrain-props-index-v1":
            return _load_index_catalog(p, doc)
        return _validate_inline_catalog(doc)
    except (OverflowError, RecursionError) as error:
        raise ValueError("prop_catalog_numeric_budget") from error


def load_generated_catalog(root, *, expected_build=None):
    """冷路径统一验收本机生成结果；完成标记、版本、目录与全部分片都须有效。

    不搜包目录、不打开 APK、不返回标记中可能存在的源文件路径。
    UI 状态与运行时调用同一入口，避免只凭 generation.json 存在就启用。
    """
    folder = Path(root)
    if not folder.is_absolute() or str(folder).startswith(('\\\\', '//')):
        raise ValueError('props_generated_local_root')
    _reject_link_components(folder)
    marker = folder / 'generation.json'
    _reject_link_components(marker)
    try:
        with marker.open('rb') as stream:
            raw = stream.read(65537)
    except OSError:
        raise ValueError('props_generated_missing') from None
    if not raw or len(raw) > 65536:
        raise ValueError('props_generated_marker_budget')
    try:
        doc = _parse_catalog_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('props_generated_marker_invalid') from None
    if (not isinstance(doc, dict) or doc.get('schema') != 'xg-props-generation-v1'
            or doc.get('status') != 'complete' or type(doc.get('build')) is not int
            or doc['build'] <= 0):
        raise ValueError('props_generated_incomplete')
    if expected_build is not None and (type(expected_build) is not int or doc['build'] != expected_build):
        raise ValueError('props_generated_build_mismatch')
    digest = doc.get('catalog_sha256')
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('props_generated_catalog_hash')
    catalog = load_catalog(folder / 'catalog.json', expected_sha256=digest)
    if catalog.get('load_errors') or not available_prop_names(catalog=catalog):
        raise ValueError('props_generated_geometry_incomplete')
    return catalog


def _approximation_error(profile):
    """简化误差证书是冷目录的一部分；世界误差门不可随配置提高。"""
    representation = profile.get("collision_representation")
    error = profile.get("approximation")
    if representation != "bounded_edge_collapse":
        if error is not None:
            raise ValueError("prop_approximation_representation")
        if representation not in (None, "raw_render_mesh", "covered_zero_area_removal"):
            raise ValueError("prop_collision_representation")
        return None
    if (profile.get("source_kind") != "render_mesh_candidate" or not isinstance(error, Mapping)
            or error.get("method") != "topology_preserving_edge_collapse"):
        raise ValueError("prop_approximation_certificate")
    local, world = error.get("max_local_error"), error.get("max_world_error")
    if (type(local) not in (int, float) or not math.isfinite(local) or not 0 <= local <= .02
            or type(world) not in (int, float) or not math.isfinite(world) or world != .02):
        raise ValueError("prop_approximation_error_budget")
    return float(local)


def _validate_inline_catalog(doc):
    if (not isinstance(doc, dict) or doc.get("schema") != "skyterrain-props-v1"
            or doc.get("coordinate_space") != "sky_native"
            or not isinstance(doc.get("props"), dict)
            or not 1 <= len(doc["props"]) <= MAX_CATALOG_PROFILES):
        raise ValueError("prop_catalog_schema")
    total_v = total_t = 0
    for name, row in doc["props"].items():
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_]{1,80}", name) is None \
                or not isinstance(row, dict):
            raise ValueError("prop_catalog_profile")
        if row.get("coordinate_space", "sky_native") != "sky_native":
            raise ValueError("prop_catalog_coordinate_space")
        kind = row.get("source_kind")
        if kind not in ("extracted_collision", "render_mesh_candidate", "observation_patch"):
            raise ValueError("prop_catalog_source_kind")
        if kind == "render_mesh_candidate" and row.get("native_collision_verified") is not False:
            raise ValueError("render_candidate_native_claim")
        if kind == "observation_patch" and not isinstance(row.get("observed_placement"), dict):
            raise ValueError("prop_catalog_observation_binding")
        _approximation_error(row)
        resource = row.get("resource_path")
        if resource is not None and (not isinstance(resource, str)
                or len(resource) > 512 or not resource.startswith("assets/Data/")
                or "\\" in resource or ":" in resource
                or any(part in ("", ".", "..") for part in resource.split("/"))):
            raise ValueError("prop_catalog_resource_path")
        points, triangles = row.get("positions"), row.get("triangles")
        if not isinstance(points, list) or len(points) % 3 or not 9 <= len(points) <= MAX_VERTICES_PER_PROP*3:
            raise ValueError("prop_catalog_vertex_budget")
        if not isinstance(triangles, list) or len(triangles) % 3 or not 3 <= len(triangles) <= MAX_TRIANGLES_PER_PROP*3:
            raise ValueError("prop_catalog_triangle_budget")
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or abs(v)>50 for v in points):
            raise ValueError("prop_catalog_local_vertex")
        if any(type(v) is not int or not 0 <= v < len(points)//3 for v in triangles):
            raise ValueError("prop_catalog_triangle_index")
        tri = np.asarray(points, dtype=np.float64).reshape(-1, 3)[np.asarray(triangles).reshape(-1, 3)]
        if np.any(np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)<1e-9):
            raise ValueError("prop_catalog_degenerate_triangle")
        total_v += len(points)//3; total_t += len(triangles)//3
        if total_v > MAX_CATALOG_VERTICES or total_t > MAX_CATALOG_TRIANGLES:
            raise ValueError("prop_catalog_total_budget")
    return doc


def available_prop_names(*, catalog=None, allow_observed=False):
    """返回内置目录中可启用的名字；观测 profile 必须显式开启。"""
    names = []
    definitions = (_DEFAULT_CATALOG if catalog is None else catalog)
    if not isinstance(definitions, Mapping) or not isinstance(definitions.get("props"), Mapping):
        return ()
    for name, profile in definitions["props"].items():
        if not isinstance(name, str) or not isinstance(profile, Mapping):
            continue
        if isinstance(definitions, IndexedPropCatalog):
            if profile.get("runtime_eligible") is True and name in definitions._encoded:
                if profile.get("source_kind") != "observation_patch" or allow_observed is True:
                    names.append(name)
            continue
        kind = profile.get("source_kind")
        if kind in ("extracted_collision", "render_mesh_candidate") \
                or (kind == "observation_patch" and allow_observed is True):
            if profile.get("positions") and profile.get("triangles"):
                names.append(name)
    return tuple(sorted(names))


def known_prop_names(*, catalog=None):
    """所有已声明名称，包括明确无可用几何项；不是碰撞白名单。"""
    definitions = (_DEFAULT_CATALOG if catalog is None else catalog)
    if not isinstance(definitions, Mapping) or not isinstance(definitions.get("props"), Mapping):
        return ()
    return tuple(sorted(name for name in definitions["props"] if type(name) is str and _PROFILE_NAME.fullmatch(name)))


def catalog_registry_names(*, catalog=None, allow_observed=False):
    """向接收侧只传小型名称/可用性表，不传文件路径或几何解码器。"""
    definitions = _DEFAULT_CATALOG if catalog is None else catalog
    available = set(available_prop_names(catalog=catalog, allow_observed=allow_observed))
    result = {}
    for name in known_prop_names(catalog=catalog):
        row = definitions["props"][name]
        status = row.get("status", "no_geometry") if isinstance(row, Mapping) else "no_geometry"
        result[name] = "ready" if name in available else status if status in ("no_geometry", "ineligible", "ambiguous") else "no_geometry"
    return result


def build_colliders(props, catalog=None, *, allow_observed=False):
    """返回 (colliders, rejected)。catalog 可由冷路径加载或使用内嵌观测 profile。
    
    返回 positions/triangles 为扁平数组，位置已乘 scale、旋转并平移为
    sky_native 世界坐标。Actor offset 沿用现有后端，绝不重复应用。"""
    rejected, colliders, ids = [], [], set()
    total_vertices = total_triangles = 0
    if not isinstance(props, (list, tuple)):
        return [], [{"reason": "props_not_sequence"}]
    if catalog is None:
        catalog = _DEFAULT_CATALOG
    definitions = catalog.get("props", {}) if isinstance(catalog, Mapping) else {}
    if not isinstance(definitions, Mapping):
        return [], [{"reason": "prop_catalog_shape"}]
    for prop in props[:MAX_PROPS]:
        ident = prop.get("id") if isinstance(prop, Mapping) else None
        try:
            if not isinstance(prop, Mapping):
                raise ValueError("prop_shape")
            if not isinstance(ident, str) or not 1 <= len(ident) <= 192:
                raise ValueError("prop_id")
            if ident in ids:
                raise ValueError("prop_duplicate_id")
            name = prop.get("name")
            profile = definitions.get(name) if isinstance(name, str) else None
            if not isinstance(profile, Mapping):
                raise ValueError("unknown_prop")
            if isinstance(catalog, IndexedPropCatalog):
                if profile.get("runtime_eligible") is not True:
                    raise ValueError("prop_no_geometry:" + str(profile.get("status", "no_geometry")))
                profile = catalog.resolve(name)
                if profile is None:
                    raise ValueError("prop_no_geometry:not_preloaded")
            source = profile.get("source_kind")
            if source not in ("extracted_collision", "render_mesh_candidate", "observation_patch"):
                raise ValueError("prop_geometry_source")
            approximation_error = _approximation_error(profile)
            p = _vector(prop.get("position"), 3, "prop_position")
            if float(np.max(np.abs(p))) > MAX_COORDINATE:
                raise ValueError("prop_coordinate_budget")
            r = _rotation(prop.get("rotation"))
            scale = prop.get("scale")
            if isinstance(scale, bool) or not isinstance(scale, (int, float)) \
                    or not math.isfinite(scale) or not 0.05 <= scale <= 20:
                raise ValueError("prop_scale")
            if source == "observation_patch":
                if allow_observed is not True:
                    raise ValueError("observed_prop_disabled")
                if not _observation_matches(prop, profile, p, r, scale):
                    raise ValueError("observed_placement_mismatch")
            verts, inds = profile.get("positions"), profile.get("triangles")
            if not isinstance(verts, (list, tuple)) or not 9 <= len(verts) <= MAX_VERTICES_PER_PROP*3 \
                    or len(verts) % 3:
                raise ValueError("prop_vertex_budget")
            if not isinstance(inds, (list, tuple)) or not 3 <= len(inds) <= MAX_TRIANGLES_PER_PROP*3 \
                    or len(inds) % 3:
                raise ValueError("prop_triangle_budget")
            if any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or abs(v) > 50 for v in verts):
                raise ValueError("prop_local_vertex")
            if any(type(v) is not int or v < 0 or v >= len(verts)//3 for v in inds):
                raise ValueError("prop_index")
            count_v, count_t = len(verts)//3, len(inds)//3
            if total_vertices+count_v > MAX_VERTICES or total_triangles+count_t > MAX_TRIANGLES:
                raise ValueError("prop_total_geometry_budget")
            world = np.asarray(verts, dtype=np.float64).reshape(-1, 3)*scale @ r.T + p
            if not np.isfinite(world).all() or float(np.max(np.abs(world))) > MAX_COORDINATE:
                raise ValueError("prop_world_coordinate_budget")
            tri = world[np.asarray(inds).reshape(-1, 3)]
            area = np.linalg.norm(np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]), axis=1)
            if np.any(area < 1e-9):
                raise ValueError("prop_degenerate_triangle")
            positions = [round(float(v), 7) for v in world.ravel()]
            # 与 dynamic_colliders.checked 精确同阶段：先传输 p 的跨度，
            # 再 Float32Array(p) 的顶点，用 double 差值/叉积/hypot 判退化。
            # 坏姿态整件忽略；不删三角形、不使同批好道具也被 worker 拒绝。
            packed_world = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
            if np.any(np.max(packed_world, axis=0) - np.min(packed_world, axis=0) > 32.):
                raise ValueError("prop_worker_extent")
            physics_world = packed_world.astype(np.float32).astype(np.float64)
            if approximation_error is not None:
                rounding_error = float(np.max(np.linalg.norm(physics_world-world, axis=1)))
                if approximation_error*scale + rounding_error > .02:
                    raise ValueError("prop_approximation_world_error_budget")
            physics_tri = physics_world[np.asarray(inds).reshape(-1, 3)]
            physics_cross = np.cross(physics_tri[:, 1]-physics_tri[:, 0],
                                     physics_tri[:, 2]-physics_tri[:, 0])
            if any(math.hypot(float(v[0]), float(v[1]), float(v[2])) <= 1e-8 for v in physics_cross):
                raise ValueError("prop_worker_float32_degenerate_triangle")
            colliders.append({"id": ident, "name": name,
                              "positions": positions,
                              "triangles": list(inds), "source_kind": source,
                              "standable": True})
            ids.add(ident)
            total_vertices += count_v
            total_triangles += count_t
        except (ValueError, TypeError, OverflowError) as error:
            rejected.append({"id": ident if isinstance(ident, str) else None,
                             "reason": str(error)})
    if len(props) > MAX_PROPS:
        rejected.append({"reason": "prop_count_budget", "ignored_count": len(props)-MAX_PROPS})
    return colliders, rejected


class _OverlayCells(Mapping):
    """共享静态 bucket，不重建静态索引；只合并含动态三角的 bucket。"""
    def __init__(self, base, dynamic, offset):
        self.base, self.dynamic, self.offset = base, dynamic, offset
        self.combined = {}
        for key, ids in dynamic.items():
            shifted = ids + offset
            self.combined[key] = (np.concatenate((base[key], shifted)) if key in base else shifted)

    def __getitem__(self, key):
        if key in self.combined:
            return self.combined[key]
        return self.base[key]

    def __iter__(self):
        yield from self.base
        yield from (key for key in self.dynamic if key not in self.base)

    def __len__(self):
        return len(self.base) + sum(key not in self.base for key in self.dynamic)


class PropSurfaceOverlay(GroundSurface):
    """GroundSurface 内存叠加快照，兼容空中和脱困的内部三角接口。
    
    仅 revision 变化且 worker 接受快照后替换。静态数组拼接一次，静态
    _cells 字典共享；原静态对象及旧快照均不原地修改。"""
    def __init__(self, static_surface, colliders):
        if not isinstance(static_surface, GroundSurface):
            raise ValueError("prop_static_surface_required")
        if isinstance(static_surface, PropSurfaceOverlay):
            static_surface = static_surface.static_surface
        if not isinstance(colliders, (list, tuple)) or not 1 <= len(colliders) <= MAX_PROPS:
            raise ValueError("prop_overlay_count")
        vertices, triangles, cursor = [], [], 0
        # 请求级座位退出检查使用只读 face provenance；不改变 worker 的全局碰撞集。
        self.collider_ranges = []
        face_cursor = static_surface.triangle_count
        for row in colliders:
            if not isinstance(row, Mapping):
                raise ValueError("prop_collider_shape")
            p = np.asarray(row["positions"], dtype=np.float64).reshape(-1, 3)
            raw = row["triangles"]
            if any(type(v) is not int or v < 0 or v >= len(p) for v in raw):
                raise ValueError("prop_overlay_index")
            t = np.asarray(raw, dtype=np.uint32).reshape(-1, 3)
            if not len(p) or len(p)>MAX_VERTICES_PER_PROP or not 1 <= len(t) <= MAX_TRIANGLES_PER_PROP:
                raise ValueError("prop_overlay_geometry_budget")
            if not np.isfinite(p).all() or np.any(np.abs(p)>MAX_COORDINATE):
                raise ValueError("prop_overlay_coordinate")
            vertices.append(p); triangles.append(t+cursor); cursor += len(p)
            self.collider_ranges.append(dict(id=row.get('id'),name=row.get('name'),
                start=face_cursor,count=len(t),geometry_sha256=hashlib.sha256(
                    p.astype('<f8').tobytes()+t.astype('<u4').tobytes()).hexdigest()))
            face_cursor += len(t)
        if cursor > MAX_VERTICES or sum(len(t) for t in triangles)>MAX_TRIANGLES:
            raise ValueError("prop_overlay_total_budget")
        dynamic = GroundSurface(np.concatenate(vertices), np.concatenate(triangles),
                                bucket=static_surface.bucket)
        # 动态网格保留已核对的源绕序：仅 +Y 朝向可站。不能沿用静态
        # 资产未知绕序的 abs(ny)，否则桌底面会成为 height/空中支持层。
        # 所有三角形仍留在 _tri/_cells，向下面同样参与身体和轨迹 SAT。
        cross = np.cross(dynamic._tri[:,1]-dynamic._tri[:,0],
                         dynamic._tri[:,2]-dynamic._tri[:,0])
        length = np.linalg.norm(cross, axis=1)
        dynamic._ny = np.divide(cross[:,1], length, out=np.zeros_like(length),
                                where=length > 1e-12)
        self.static_surface, self.dynamic_surface = static_surface, dynamic
        self.bucket = static_surface.bucket
        self.vertex_count = static_surface.vertex_count + dynamic.vertex_count
        self.triangle_count = static_surface.triangle_count + dynamic.triangle_count
        for field in ("_tri", "_lo", "_hi", "_ny"):
            setattr(self, field, np.concatenate((getattr(static_surface, field), getattr(dynamic, field))))
        self._empty = static_surface._empty
        self._cells = _OverlayCells(static_surface._cells, dynamic._cells, static_surface.triangle_count)
        self._global = np.concatenate((static_surface._global,
                                      dynamic._global+static_surface.triangle_count))


def compose_surface(static_surface, colliders):
    """无动态几何返回原静态对象；静态地形未加载时不自行启用。"""
    if isinstance(static_surface, PropSurfaceOverlay):
        static_surface = static_surface.static_surface
    if not colliders or static_surface is None:
        return static_surface
    return PropSurfaceOverlay(static_surface, colliders)
