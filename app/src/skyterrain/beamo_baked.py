"""Neutral LOD0 side-attribute expansion for an explicitly supplied Mesh submesh0.

Evidence: research/native/authority-beamo-baked-dataflow-review.json (CN0.16.7).
This does not choose resources, interpret opaque bytes, or produce geometry.
Legacy reader keys mentioning color/uv are only compatibility field names.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
import numpy as np
from .formats import MeshData, FormatError


@dataclass(frozen=True)
class BakedLimits:
    max_vertices: int = 2_000_000
    max_input_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024

    def __post_init__(self):
        for name, ceiling in (("max_vertices", 8_000_000),
                              ("max_input_bytes", 128 * 1024 * 1024),
                              ("max_output_bytes", 128 * 1024 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise FormatError("invalid_baked_limit:" + name)


@dataclass(frozen=True)
class BakedAttributes:
    status: str
    attributes: np.ndarray
    opaque_raw: bytes
    runtime_opaque: bytes
    first_count: int
    prop11_count: int | None
    render_vertex_count: int | None
    compression_flag: int
    selected_resource: str
    mesh_source: str | None
    resource_binding_verified: bool = field(default=False, init=False)
    collision_verified: bool = field(default=False, init=False)
    geometry_created: bool = field(default=False, init=False)


def _uint(value, name, maximum=0xffffffff):
    if type(value) is not int or not 0 <= value <= maximum:
        raise FormatError("invalid_baked_" + name)
    return value


def _raw(value, name, maximum, expected):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise FormatError("invalid_baked_" + name + "_bytes")
    size = value.nbytes if isinstance(value, memoryview) else len(value)
    if size > maximum:
        raise FormatError("baked_input_budget")
    if size != expected:
        raise FormatError("baked_" + name + "_length_mismatch")
    return bytes(value)


def _array(raw, count):
    # An immutable bytes owner prevents callers from restoring WRITEABLE.
    return np.frombuffer(raw, dtype=np.uint8).reshape((count, 12))


def expand_first_submesh_attributes(mesh, lod_sub, *, compression_flag,
                                    selected_resource, limits=BakedLimits()):
    """Expand supported submesh0 attributes, never an implicit resource lookup.

    lod_sub is the raw subrecord from formats._read_lod_links. Exact first_count
    mismatch and absent Mesh yield an empty *runtime* subrecord; original
    opaque bytes stay separately available. Malformed streams/budgets raise
    FormatError. Only .mesh v0x20's first decoded payload is supported.
    """
    if not isinstance(limits, BakedLimits):
        raise FormatError("BakedLimits_required")
    if not isinstance(selected_resource, str) or not selected_resource or len(selected_resource) > 4096:
        raise FormatError("explicit_selected_resource_required")
    if not isinstance(lod_sub, Mapping):
        raise FormatError("baked_subrecord_required")
    required = ("ordinal", "uv_count_raw", "opaque_byte_count_raw",
                "color_records_raw", "opaque_records_raw")
    if any(key not in lod_sub for key in required):
        raise FormatError("incomplete_baked_subrecord")
    if _uint(lod_sub["ordinal"], "submesh_ordinal") != 0:
        raise FormatError("only_first_submesh_supported")
    flag = _uint(compression_flag, "compression_flag", 1)
    first = _uint(lod_sub["uv_count_raw"], "first_count", limits.max_vertices)
    second = _uint(lod_sub["opaque_byte_count_raw"], "opaque_count", limits.max_input_bytes)
    if flag and first == 0:
        raise FormatError("unsupported_compressed_zero_first_count")
    expected_attributes = 12 if flag else first * 12
    if expected_attributes + second > limits.max_input_bytes:
        raise FormatError("baked_input_budget")
    raw = _raw(lod_sub["color_records_raw"], "attribute", limits.max_input_bytes, expected_attributes)
    opaque = _raw(lod_sub["opaque_records_raw"], "opaque", limits.max_input_bytes, second)
    empty = _array(b"", 0)
    if mesh is None:
        return BakedAttributes("missing_mesh", empty, opaque, b"", first, None,
                               None, flag, selected_resource, None)
    if not isinstance(mesh, MeshData) or mesh.version != 0x20:
        raise FormatError("unsupported_baked_mesh_schema")
    metadata = mesh.metadata
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("flags"), Mapping):
        raise FormatError("missing_mesh_counts")
    counts = metadata["flags"].get("counts_8_raw")
    if not isinstance(counts, (tuple, list)) or len(counts) != 8:
        raise FormatError("invalid_mesh_counts")
    counts = tuple(_uint(value, "mesh_count") for value in counts)
    vertex_count, index_count, index32, _, prop11, *_ = counts
    _uint(vertex_count, "vertex_count", limits.max_vertices)
    _uint(prop11, "prop11_count", limits.max_vertices)
    if index32 not in (0, 1) or index_count % 3:
        raise FormatError("invalid_mesh_index_layout")
    if (not isinstance(mesh.positions, np.ndarray) or mesh.positions.shape != (vertex_count, 3)
            or not isinstance(mesh.triangles, np.ndarray)
            or mesh.triangles.shape != (index_count // 3, 3)):
        raise FormatError("mesh_geometry_count_mismatch")
    if first != prop11:
        return BakedAttributes("count_mismatch", empty, opaque, b"", first,
                               prop11, vertex_count, flag, selected_resource, mesh.source)
    if vertex_count * 12 > limits.max_output_bytes:
        raise FormatError("baked_output_budget")
    if flag:
        # Native compressed branch repeats one record, without reading remap.
        expanded = raw * vertex_count
    else:
        optional = metadata.get("optional_streams_raw")
        if not isinstance(optional, Mapping) or "prop11_indices" not in optional:
            raise FormatError("missing_prop11_remap")
        width = 4 if index32 else 2
        if len(raw) + len(opaque) + vertex_count * width > limits.max_input_bytes:
            raise FormatError("baked_input_budget")
        remap_raw = _raw(optional["prop11_indices"], "prop11_remap",
                         limits.max_input_bytes, vertex_count * width)
        remap = np.frombuffer(remap_raw, dtype="<u4" if index32 else "<u2")
        if len(remap) and int(remap.max()) >= first:
            raise FormatError("prop11_remap_out_of_range")
        records = np.frombuffer(raw, dtype=np.uint8).reshape((first, 12))
        expanded = records[remap].tobytes()
    return BakedAttributes("expanded", _array(expanded, vertex_count), opaque,
                           opaque, first, prop11, vertex_count, flag,
                           selected_resource, mesh.source)
