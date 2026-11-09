# -*- coding: utf-8 -*-
# 来源：Codex 工程 sky-terrain-20261103/src/skyterrain/scene.py
# 上游 sha256：bf53f5dcb7e8c771a885b31adde28b5e155a7ba481a92f8623837bafdfcbc61e
# 只做一件事：把 APK 里的 Objects.level.bin（TGCL v1）解析成对象/字段，
# 不执行 Lua、不联网、不写文件。本文件无第三方依赖。
"""Bounded, non-executing TGCL v1 scene reader.

Format reference: that-sky-compiler @ c3dcfec843446d6f1979e67ab4f513601371cbad,
sldl-objects/src/levelObjects.js (header, class/member tables and member kinds).
This implementation does not import or execute that repository or Lua. Unknown
fixed-width fields remain bytes; no engine version's external type database is
assumed. Collision flags describe initial serialized candidates, not an evaluated
physics scene or a navigation mesh. Object references form a graph, not a tree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import math
import struct
from typing import Any


class SceneFormatError(ValueError):
    """Malformed, unsupported or over-budget scene."""


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 64 * 1024 * 1024
    max_objects: int = 200_000
    max_classes: int = 8192
    max_members: int = 100_000
    max_string_bytes: int = 65536
    max_array_items: int = 200_000
    max_values: int = 2_000_000
    max_depth: int = 32


@dataclass(frozen=True)
class Member:
    name: str
    kind: int
    size: int
    aux: int


@dataclass(frozen=True)
class ClassSpec:
    name: str
    members: tuple[Member, ...]


@dataclass(frozen=True)
class Ref:
    index: int | None


@dataclass(frozen=True)
class DuplicateMember:
    """One ambiguous-name occurrence, identified by its class-member ordinal.

    Neither first-wins nor last-wins is safe for native consumers. Preserve its
    descriptor, value and exact source bytes outside the unambiguous fields map.
    """
    ordinal: int
    member: Member
    value: Any
    offset: int
    end_offset: int
    raw: bytes


@dataclass(frozen=True)
class Node:
    index: int | None
    name: str
    class_name: str
    fields: dict[str, Any]
    offset: int
    end_offset: int
    duplicate_members: tuple[DuplicateMember, ...] = ()


# Native storage is four consecutive vector4 values, translation in vector 4.
# No Blender conversion, unit conversion, or scene parent composition is applied.
Matrix = tuple[tuple[float, float, float, float], ...]
FLAGS = ("enabled", "render", "collision", "isInvisibleColliderOnly",
         "isTransformOnly", "isCloud", "baked", "animated", "jumpable",
         "jumpAuto", "jumpGuardOnly", "passthrough", "networked",
         "occludeCamera", "scaleXYZ")


@dataclass(frozen=True)
class GeometryNode:
    object_index: int
    name: str
    class_name: str
    resource_names: tuple[str, ...]
    resource_group_indices: tuple[int, ...]
    transform: Matrix | None
    flags: dict[str, bool | None]
    classification: str
    issues: tuple[str, ...]
    native_collision_verified: bool = False


@dataclass
class Scene:
    version: int
    classes: tuple[ClassSpec, ...]
    objects: tuple[Node, ...]
    declared_ref_count: int
    observed_ref_count: int
    source: str | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_bytes(cls, data: bytes, *, limits: Limits | None = None,
                   source: str | None = None) -> "Scene":
        return _Reader(data, limits or Limits()).read(source)

    @classmethod
    def from_file(cls, path: str | Path, *, limits: Limits | None = None) -> "Scene":
        lim = limits or Limits()
        p = Path(path)
        if p.stat().st_size > lim.max_bytes:
            raise SceneFormatError("file_budget")
        with p.open("rb") as f:
            data = f.read(lim.max_bytes + 1)
        return cls.from_bytes(data, limits=lim, source=str(p))

    def resolve(self, ref: Ref) -> Node | None:
        if not isinstance(ref, Ref):
            raise TypeError("reference_required")
        if ref.index is not None and (type(ref.index) is not int or not 0 <= ref.index < len(self.objects)):
            raise SceneFormatError("invalid_object_reference")
        return None if ref.index is None else self.objects[ref.index]

    def references(self) -> tuple[tuple[int, str, int], ...]:
        """Edges (source object index, nested field path, target index)."""
        out = []
        for obj in self.objects:
            for path, value in _walk_node(obj):
                if isinstance(value, Ref) and value.index is not None:
                    out.append((obj.index, path, value.index))
        return tuple(out)

    def geometry_nodes(self) -> tuple[GeometryNode, ...]:
        # Resolve only the documented group -> Clump.data -> instance relation.
        # Do not invent a generic parent or evaluate dynamic scene events.
        groups: dict[int, list[tuple[int, str]]] = {}
        for obj in self.objects:
            if obj.class_name != "LevelInstanceModelGroup":
                continue
            resource = obj.fields.get("meshName")
            refs = obj.fields.get("levelInstanceModels")
            if not isinstance(resource, str) or not isinstance(refs, Ref):
                continue
            target = self.resolve(refs)
            if target is None:
                continue
            candidates = [target]
            if target.class_name == "Clump":
                candidates = [self.resolve(x) for x in target.fields.get("data", ())
                              if isinstance(x, Ref)]
            for node in candidates:
                if node is not None and node.class_name == "LevelInstanceModel":
                    groups.setdefault(node.index, []).append((obj.index, resource))
        result = []
        for obj in self.objects:
            if obj.class_name not in ("LevelMesh", "LevelInstanceModel"):
                continue
            issues = ["ambiguous_member:" + key for key in
                      sorted({d.member.name for d in obj.duplicate_members}
                             & (set(FLAGS) | {"transform", "resourceName"}))]
            flags = {key: _flag(obj.fields.get(key)) for key in FLAGS}
            for key in FLAGS:
                if key in obj.fields and flags[key] is None:
                    issues.append("invalid_flag:" + key)
            raw_transform = obj.fields.get("transform")
            transform = _matrix(raw_transform)
            if transform is None:
                issues.append("invalid_or_missing_transform")
            if flags["enabled"] is None:
                issues.append("missing_or_invalid_enabled")
            refs = groups.get(obj.index, [])
            if obj.class_name == "LevelMesh":
                name = obj.fields.get("resourceName")
                resources = (name,) if isinstance(name, str) and name else ()
            else:
                resources = tuple(dict.fromkeys(name for _, name in refs if name))
            if len(resources) != 1:
                issues.append("missing_or_ambiguous_resource")
            if flags["enabled"] is False or flags["isTransformOnly"] is True:
                category = "disabled_or_transform_only"
            elif flags["collision"] is False:
                category = "collision_disabled"
            elif flags["collision"] is not True:
                category = "collision_unknown"
            elif issues:
                category = "incomplete_collision_candidate"
            elif flags["animated"] is True or flags["networked"] is True:
                category = "dynamic_collision_candidate"
            elif flags["isCloud"] is True:
                category = "cloud_collision_candidate"
            else:
                category = "static_collision_candidate"
            result.append(GeometryNode(obj.index, obj.name, obj.class_name,
                resources, tuple(i for i, _ in refs), transform, flags,
                category, tuple(issues)))
        return tuple(result)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "tgcl-scene-v1", "format_version": self.version,
                "source": self.source,
                "declared_ref_count": self.declared_ref_count,
                "observed_ref_count": self.observed_ref_count,
                "warnings": self.warnings[:],
                "classes": [{"name": c.name, "members": [m.__dict__.copy() for m in c.members]}
                            for c in self.classes],
                "objects": [_json_value(x) for x in self.objects],
                "references": self.references()}


def _walk_node(node: Node, prefix: str = ""):
    yield from _walk(node.fields, prefix)
    for item in node.duplicate_members:
        # Ordinal-based paths keep both reference edges even when values match.
        key = f"@member[{item.ordinal}]:{item.member.name}"
        yield from _walk({key: item.value}, prefix)


def _walk(fields: dict[str, Any], prefix: str = ""):
    for key, value in fields.items():
        path = prefix + key
        if isinstance(value, Node):
            yield from _walk_node(value, path + ".")
        elif isinstance(value, tuple):
            for i, item in enumerate(value):
                subpath = f"{path}[{i}]"
                if isinstance(item, Node):
                    yield from _walk_node(item, subpath + ".")
                else:
                    yield subpath, item
        else:
            yield path, value


def _json_value(value):
    if isinstance(value, bytes):
        return {"raw_hex": value.hex(), "byte_length": len(value)}
    if isinstance(value, Ref):
        return {"object_index": value.index}
    if isinstance(value, Node):
        out = {"index": value.index, "name": value.name,
               "class_name": value.class_name, "offset": value.offset,
               "end_offset": value.end_offset,
               "fields": {k: _json_value(v) for k, v in value.fields.items()}}
        if value.duplicate_members:
            out["duplicate_members"] = [
                {"ordinal": item.ordinal, "member": item.member.__dict__.copy(),
                 "offset": item.offset, "end_offset": item.end_offset,
                 "raw_hex": item.raw.hex(), "value": _json_value(item.value)}
                for item in value.duplicate_members]
        return out
    if isinstance(value, tuple):
        return [_json_value(x) for x in value]
    return value


def _flag(value) -> bool | None:
    return {b"\0": False, b"\1": True}.get(value) if isinstance(value, bytes) else None


def _matrix(value) -> Matrix | None:
    if not isinstance(value, bytes) or len(value) != 64:
        return None
    vals = struct.unpack("<16f", value)
    if not all(math.isfinite(x) for x in vals):
        return None
    # A perspective/non-affine matrix is preserved in raw fields, but never
    # exposed as a usable geometry placement.
    if any(abs(vals[i]) > 1e-5 for i in (3, 7, 11)) or abs(vals[15] - 1) > 1e-5:
        return None
    return tuple(tuple(vals[i:i + 4]) for i in range(0, 16, 4))


def transform_point(matrix: Matrix, point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply native four-vector storage; translation is matrix[3][:3]."""
    if len(matrix) != 4 or any(len(v) != 4 for v in matrix) or len(point) != 3:
        raise ValueError("matrix_or_point_shape")
    if not all(math.isfinite(x) for v in matrix for x in v) or not all(math.isfinite(x) for x in point):
        raise ValueError("non_finite_transform")
    if any(abs(matrix[c][3]) > 1e-5 for c in range(3)) or abs(matrix[3][3] - 1) > 1e-5:
        raise ValueError("non_affine_transform")
    result = tuple(sum(matrix[c][r] * point[c] for c in range(3)) + matrix[3][r]
                   for r in range(3))
    if not all(math.isfinite(x) for x in result):
        raise ValueError("transform_overflow")
    return result


class _Reader:
    def __init__(self, data: bytes, limits: Limits):
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > limits.max_bytes:
            raise SceneFormatError("file_budget")
        self.b, self.lim = data, limits
        self.refs: list[int] = []
        self.values = 0
        self.duplicate_raw_bytes = 0

    def span(self, off: int, size: int, end: int | None = None):
        if off < 0 or size < 0 or off + size > (len(self.b) if end is None else end):
            raise SceneFormatError("truncated_or_out_of_bounds")

    def u32(self, off):
        self.span(off, 4)
        return struct.unpack_from("<I", self.b, off)[0]

    def string(self, off, end):
        self.span(off, 1, end)
        stop = self.b.find(b"\0", off, min(end, off + self.lim.max_string_bytes + 1))
        if stop < 0:
            raise SceneFormatError("unterminated_or_over_budget_string")
        try:
            # Surrogate escapes preserve unusual original bytes losslessly.
            value = self.b[off:stop].decode("utf-8", "surrogateescape")
        except UnicodeError as exc:
            raise SceneFormatError("string_encoding") from exc
        return value, stop + 1

    def read(self, source):
        self.span(0, 44)
        if self.b[:4] != b"TGCL":
            raise SceneFormatError("magic")
        version, nc, nm, no, nr, co, mo, so, oo, fs = struct.unpack_from("<10I", self.b, 4)
        if version != 1:
            raise SceneFormatError("unsupported_version")
        if fs != len(self.b):
            raise SceneFormatError("file_size_mismatch")
        if nc > self.lim.max_classes or nm > self.lim.max_members or no > self.lim.max_objects:
            raise SceneFormatError("table_budget")
        if not (44 <= co <= mo <= so <= oo <= fs):
            raise SceneFormatError("section_order")
        self.span(co, nc * 12, mo)
        self.span(mo, nm * 16, so)
        members = []
        for i in range(nm):
            kind, nameoff, size, aux = struct.unpack_from("<4I", self.b, mo + i * 16)
            name, _ = self.string(so + nameoff, oo)
            if not name or kind not in (0, 1, 2, 3):
                raise SceneFormatError("invalid_member")
            if kind == 0 and (size == 0 or size > self.lim.max_bytes):
                raise SceneFormatError("raw_size")
            if kind == 3 and aux != 0xFFFFFFFF and aux >= nc:
                raise SceneFormatError("unknown_array_element_type")
            members.append(Member(name, kind, size, aux))
        classes = []
        duplicate_names = []
        duplicate_warnings = []
        for i in range(nc):
            nameoff, first, count = struct.unpack_from("<3I", self.b, co + i * 12)
            name, _ = self.string(so + nameoff, oo)
            if not name or first + count > nm:
                raise SceneFormatError("invalid_class_members")
            subset = tuple(members[first:first + count])
            seen = set(); repeated = set()
            for member in subset:
                if member.name in seen: repeated.add(member.name)
                seen.add(member.name)
            duplicate_names.append(frozenset(repeated))
            if repeated:
                duplicate_warnings.append("duplicate_member_names_preserved:" + name + ":" + ",".join(sorted(repeated)))
            classes.append(ClassSpec(name, subset))
        self.classes = tuple(classes)
        self.duplicate_names = tuple(duplicate_names)
        nodes, cursor = [], oo
        for i in range(no):
            cls = self.u32(cursor)
            start = cursor
            name, cursor = self.string(cursor + 4, fs)
            node, cursor = self.node(cls, cursor, 0, i, name, start)
            nodes.append(node)
        if cursor != fs:
            raise SceneFormatError("unconsumed_object_bytes")
        if any(x != 0xFFFFFFFF and x >= no for x in self.refs):
            raise SceneFormatError("invalid_object_reference")
        warning = [] if nr == len(self.refs) else ["declared_ref_count_differs_from_serialized_ref_slots"]
        warning.extend(duplicate_warnings)
        return Scene(version, self.classes, tuple(nodes), nr, len(self.refs), source, warning)

    def node(self, cls, cursor, depth, index=None, name="", start=None):
        if depth > self.lim.max_depth:
            raise SceneFormatError("nesting_budget")
        if cls >= len(self.classes):
            raise SceneFormatError("invalid_class_index")
        spec = self.classes[cls]
        fields = {}
        duplicates = []
        ambiguous = self.duplicate_names[cls]
        begin = cursor if start is None else start
        for ordinal, member in enumerate(spec.members):
            member_start = cursor
            self.values += 1
            if self.values > self.lim.max_values:
                raise SceneFormatError("value_budget")
            kind = member.kind
            if kind == 0:
                self.span(cursor, member.size)
                value = self.b[cursor:cursor + member.size]
                cursor += member.size
            elif kind == 1:
                value, cursor = self.string(cursor, len(self.b))
            elif kind == 2:
                value, cursor = self.reference(cursor)
            else:
                count = self.u32(cursor)
                cursor += 4
                if count > self.lim.max_array_items or self.values + count > self.lim.max_values:
                    raise SceneFormatError("array_budget")
                self.values += count
                value = []
                if member.aux == 0xFFFFFFFF:
                    self.span(cursor, 4 * count)
                    for _ in range(count):
                        item, cursor = self.reference(cursor)
                        value.append(item)
                else:
                    for _ in range(count):
                        item, cursor = self.node(member.aux, cursor, depth + 1)
                        value.append(item)
                value = tuple(value)
            if member.name in ambiguous:
                self.duplicate_raw_bytes += cursor - member_start
                if self.duplicate_raw_bytes > self.lim.max_bytes:
                    raise SceneFormatError("duplicate_raw_preservation_budget")
                duplicates.append(DuplicateMember(ordinal, member, value, member_start,
                                                   cursor, self.b[member_start:cursor]))
            else:
                fields[member.name] = value
        return Node(index, name, spec.name, fields, begin, cursor, tuple(duplicates)), cursor

    def reference(self, cursor):
        index = self.u32(cursor)
        self.refs.append(index)
        return Ref(None if index == 0xFFFFFFFF else index), cursor + 4
