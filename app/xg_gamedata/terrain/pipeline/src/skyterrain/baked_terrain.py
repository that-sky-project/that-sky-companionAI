"""CN 0.16.7 baked ordinary-terrain registration projection.

This is a version-pinned static observation, NOT native avatar physics parity.
GEO geometry stays render_only in the decoder. This layer cites the independent
native registration path to build a separate candidate asset, excluding clouds.
"""
from __future__ import annotations
import copy
import numpy as np
from .assembly import Assembly, AssemblyLimits
from .formats._types import MeshBundle, FormatError

PROFILE = "netease-android-0.16.7-421589"
# Native reader keeps only low16 for version testing; this candidate accepts
# only the four exact raw headers observed in the current APK, not arbitrary flags.
RAW_VERSION_LAYOUTS = {60: 60, 61: 61, 0x0001003C: 60, 0x0002003D: 61}
ELF_SHA256 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"
EVIDENCE = {
    "profile": PROFILE, "elf_sha256": ELF_SHA256,
    "report": "research/native/authority-native-terrain-registration-review.json",
    "load_geo": "0x28978d0..0x2899068",
    "registration_helper": "0x289bd1c..0x289bfc4",
    "geometry_call": "0x289bf3c -> 0x327db20",
    "instance_call": "0x289bf68 -> 0x327e2f4",
    "mask_consumer": "0x3275fdc..0x3275fec",
    "ordinary_mask": 0x10, "cloud_mask": 0x20,
    "header_report": "research/native/authority-native-header-review.json",
    "header_version_gate": "0x1d7767c..0x1d7768c: low16 stored, (raw & 0xfffe)==60",
    "toc_count_reader": "0x1d77754 and 0x1d77784: byte count",
    "geo_extension_reader": "0x1d7848c..0x1d785a4: groups of count*20 raw bytes",
    "accepted_raw_versions": sorted(RAW_VERSION_LAYOUTS),
    "native_static_registration_observed": True,
    "client_runtime_compared": False,
}


def _json_raw(value):
    if isinstance(value, bytes):
        return {"raw_hex": value.hex(), "byte_length": len(value)}
    if isinstance(value, (tuple, list)):
        return [_json_raw(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _json_raw(v) for k,v in value.items()}
    if isinstance(value, np.generic):
        return value.item()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported_raw_metadata_type")


def make_baked_candidate(bundle: MeshBundle, *, profile: str,
                         limits: AssemblyLimits | None = None) -> Assembly:
    """Select only decoded ordinary chunks; world-space identity, no TGCL guessing.

    Caller explicitly binds the asset to PROFILE and retains its original source
    bytes/hash. Unknown headers/flags remain the decoder's fail-closed concern.
    """
    limits = limits or AssemblyLimits()
    if profile != PROFILE:
        raise ValueError("unsupported_native_registration_profile")
    if not isinstance(bundle, MeshBundle) or bundle.version not in RAW_VERSION_LAYOUTS:
        raise ValueError("unsupported_baked_bundle")
    layout_version = RAW_VERSION_LAYOUTS[bundle.version]
    if (("layout_version" in bundle.metadata and bundle.metadata["layout_version"] != layout_version)
            or ("version_header_raw" in bundle.metadata and bundle.metadata["version_header_raw"] != bundle.version)):
        raise ValueError("inconsistent_baked_header_metadata")
    if bundle.coordinate_space != "sky_native" or bundle.geometry_kind != "render_only":
        raise ValueError("unsupported_baked_coordinate_or_geometry_semantics")
    digest = bundle.metadata.get("sha256")
    if not isinstance(digest,str) or len(digest)!=64 or any(x not in "0123456789abcdef" for x in digest):
        raise ValueError("baked_source_sha256_required")
    positions, triangles = np.asarray(bundle.positions), np.asarray(bundle.triangles)
    if positions.ndim!=2 or positions.shape[1:]!=(3,) or positions.dtype.kind not in "fiu":
        raise FormatError("invalid_baked_positions")
    if triangles.ndim!=2 or triangles.shape[1:]!=(3,) or triangles.dtype.kind not in "iu":
        raise FormatError("invalid_baked_triangles")
    if not np.isfinite(positions).all():
        raise FormatError("nonfinite_baked_positions")
    if len(positions)>limits.max_vertices or len(triangles)>limits.max_triangles:
        raise FormatError("baked_geometry_budget")
    if triangles.size and (np.any(triangles<0) or int(triangles.max())>=len(positions)):
        raise FormatError("baked_index_out_of_range")
    if len(bundle.chunks)>limits.max_instances:
        raise FormatError("baked_chunk_budget")
    coverage=np.zeros(len(triangles),dtype=np.uint8)
    selected=[];included=[];excluded=[];cursor=0
    for ordinal,chunk in enumerate(bundle.chunks):
        if any(type(x) is not int or x<0 for x in (chunk.triangle_start,chunk.triangle_count,
                chunk.vertex_start,chunk.vertex_count)):
            raise FormatError("invalid_baked_chunk_range")
        start=chunk.triangle_start;end=start+chunk.triangle_count
        if end>len(triangles) or chunk.vertex_start+chunk.vertex_count>len(positions):
            raise FormatError("baked_chunk_range_out_of_bounds")
        if coverage[start:end].any():
            raise FormatError("overlapping_baked_chunk_triangles")
        coverage[start:end]=1
        faces=triangles[start:end]
        if faces.size and (int(faces.min())<chunk.vertex_start or
                           int(faces.max())>=chunk.vertex_start+chunk.vertex_count):
            raise FormatError("baked_chunk_vertex_range_mismatch")
        row={"chunk_ordinal":ordinal,"group":chunk.group,"source_kind":chunk.kind,
             "source_triangle_start":start,"source_triangle_count":chunk.triangle_count,
             "source_vertex_start":chunk.vertex_start,"source_vertex_count":chunk.vertex_count,
             "raw_descriptor_hex":chunk.raw_descriptor.hex(),"raw_flags":_json_raw(chunk.flags)}
        if chunk.kind=="cloud_render":
            excluded.append(dict(row,reason="cloud_registration_mask_0x20_not_rigid_ground",native_mask=0x20))
        elif chunk.kind=="terrain_render":
            if len(chunk.raw_descriptor)!=56:
                raise FormatError("unverified_baked_chunk_descriptor")
            selected.append(faces)
            included.append(dict(row,triangle_start=cursor,triangle_count=len(faces),
                native_mask=0x10,geometry_kind="baked_ordinary_terrain_candidate",
                native_static_registration_observed=True,native_collision_verified=False,
                matrix_convention="native_world_identity"))
            cursor+=len(faces)
        else:
            excluded.append(dict(row,reason="unknown_baked_chunk_kind"))
    if len(triangles) and not coverage.all():
        raise FormatError("uncovered_baked_triangles")
    faces=np.concatenate(selected) if selected else np.empty((0,3),dtype=np.uint32)
    # Compact away unused cloud vertices, so their bounds cannot inflate nav space.
    used=np.unique(faces) if faces.size else np.empty(0,dtype=np.uint32)
    output_positions=np.asarray(positions[used],dtype="<f4")
    if not np.isfinite(output_positions).all():
        raise FormatError("baked_float32_overflow")
    output_triangles=np.asarray(np.searchsorted(used,faces),dtype="<u4")
    output_positions.setflags(write=False);output_triangles.setflags(write=False)
    manifest={"schema_version":"sky-baked-terrain-candidate-v1","source":bundle.source,
        "source_sha256":bundle.metadata.get("sha256"),"source_profile":profile,
        "source_version":bundle.version,"source_layout_version":layout_version,
        "geometry_kind":"partial_static_world_candidate",
        "coordinate_space":"sky_native","matrix_convention":"native_world_identity",
        "native_static_registration_observed":True,"native_collision_verified":False,
        "runtime_ready":False,"evidence":copy.deepcopy(EVIDENCE),
        "vertex_count":len(output_positions),"triangle_count":len(output_triangles),
        "source_vertex_indices":used.tolist(),"included":included,"excluded":excluded,
        "raw_header":{"version_header_raw":bundle.version,
            "version_high16_raw":bundle.version >> 16,"layout_version":layout_version,
            **{k:_json_raw(bundle.metadata[k]) for k in
                ("toc_raw","toc_count_raw","toc_count_reserved_raw",
                 "header_padding_raw","header_bounds_raw") if k in bundle.metadata}},
        "raw_geo_extensions":[{"group":mesh.metadata.get("group"),
            **{k:_json_raw(mesh.metadata[k]) for k in
                ("version61_trailer_raw","version61_trailer_u32_raw",
                 "extension_layout","extension_flag_raw","extension_record_count_raw",
                 "extension_records_raw") if k in mesh.metadata}} for mesh in bundle.meshes],
        "limitations":["static_registration_not_runtime_parity","cloud_mask_0x20_excluded",
            "dynamic_level_objects_not_evaluated","not_complete_native_collision_scene",
            "no_per_TerrainBlobGroup_guid_mapping_invented","opaque_descriptor_tail_uninterpreted",
            "geo_extension_records_not_collision_classifiers",
            "original_source_bytes_required_for_full_lossless_archive"]}
    return Assembly(output_positions,output_triangles,manifest)


def merge_candidate_assemblies(*parts: Assembly, limits: AssemblyLimits | None=None) -> Assembly:
    """Concatenate independently proven candidates, preserving source manifests.

    Does not claim geometric deduplication between BstBaked and mesh instances.
    """
    limits=limits or AssemblyLimits()
    vertices=[];triangles=[];included=[];excluded=[];vbase=tbase=0
    for ordinal,part in enumerate(parts):
        if not isinstance(part,Assembly) or part.native_collision_verified:
            raise ValueError("candidate_assembly_required")
        if part.manifest.get("coordinate_space")!="sky_native":
            raise ValueError("incompatible_candidate_coordinates")
        pp,tt=np.asarray(part.positions),np.asarray(part.triangles)
        if pp.ndim!=2 or pp.shape[1:]!=(3,) or pp.dtype.kind not in "fiu" or not np.isfinite(pp).all():
            raise ValueError("invalid_merged_positions")
        if tt.ndim!=2 or tt.shape[1:]!=(3,) or tt.dtype.kind not in "iu":
            raise ValueError("invalid_merged_triangles")
        if tt.size and (np.any(tt<0) or int(tt.max())>=len(pp)):
            raise ValueError("invalid_merged_index")
        if vbase+len(pp)>limits.max_vertices or tbase+len(tt)>limits.max_triangles:
            raise ValueError("merged_geometry_budget")
        vertices.append(pp)
        triangles.append(np.asarray(tt,dtype=np.uint64)+vbase)
        for item in part.manifest.get("included",[]):
            row=copy.deepcopy(item);row["component_ordinal"]=ordinal
            if "vertex_start" in row:row["vertex_start"]+=vbase
            if "triangle_start" in row:row["triangle_start"]+=tbase
            included.append(row)
        excluded.extend(dict(copy.deepcopy(row),component_ordinal=ordinal)
                        for row in part.manifest.get("excluded",[]))
        vbase+=len(part.positions);tbase+=len(part.triangles)
    p=np.asarray(np.concatenate(vertices) if vertices else np.empty((0,3)),dtype="<f4")
    if not np.isfinite(p).all():
        raise ValueError("merged_float32_overflow")
    raw=np.concatenate(triangles) if triangles else np.empty((0,3),dtype=np.uint64)
    if raw.size and int(raw.max())>np.iinfo(np.uint32).max:raise ValueError("merged_index_overflow")
    t=np.asarray(raw,dtype="<u4");p.setflags(write=False);t.setflags(write=False)
    manifest={"schema_version":"sky-partial-static-world-v1","geometry_kind":"partial_static_world_candidate",
       "coordinate_space":"sky_native","native_collision_verified":False,"runtime_ready":False,
       "native_static_registration_observed":any(x.manifest.get("native_static_registration_observed") is True for x in parts),
       "vertex_count":vbase,"triangle_count":tbase,"included":included,"excluded":excluded,
       "component_manifests":[copy.deepcopy(x.manifest) for x in parts],
       "limitations":["partial_static_world_only","runtime_not_compared","dynamic_events_not_evaluated",
                      "baked_and_mesh_overlap_not_deduplicated","not_native_avatar_controller"]}
    return Assembly(p,t,manifest)
