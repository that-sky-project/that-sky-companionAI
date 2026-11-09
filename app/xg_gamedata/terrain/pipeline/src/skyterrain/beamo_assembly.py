"""Explicit, offline Beamo first-submesh geometry candidates.

The Mesh selected by meshName supplies positions/indices independently of LOD0
side attributes. Missing/mismatched LOD identity is a diagnostic, not a resource
alias or proof of absent geometry. No runtime gate is evaluated here.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
import numpy as np
from .assembly import Assembly, AssemblyLimits
from .beamo_links import associate_beamo_lod0
from .formats import DEFAULT_LIMITS, FormatError, Limits, read_mesh
from .scene import Scene, FLAGS, _flag, _matrix


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value, label):
    if type(value) is not str or not value.strip() or len(value) > 4096 or '\0' in value:
        raise ValueError('invalid_' + label)
    return value


def _immutable(array, dtype):
    return np.frombuffer(np.asarray(array, dtype=dtype).tobytes(order='C'), dtype=dtype).reshape((-1, 3))


@dataclass(frozen=True, init=False)
class ExplicitBeamoBinding:
    """Byte-checked, immutable first-payload geometry; selection remains assumed.

    Construct only with from_bytes. No filename lookup, file read, or native
    execution occurs. The source digest validates the supplied bytes, NOT that
    this selected source is the client's actual resource-cache result.
    """
    positions: np.ndarray
    triangles: np.ndarray
    selected_source: str
    source_sha256: str
    source_bytes: int
    provenance: str
    mesh_version: int
    animated: bool
    coordinate_space: str
    geometry_kind: str
    header_lod_count_raw: int
    payload_tail_bytes: int
    payload_tail_sha256: str
    outer_tail_bytes: int
    outer_tail_sha256: str
    positions_sha256: str
    triangles_sha256: str

    def __new__(cls, *args, **kwargs):
        raise TypeError('use_ExplicitBeamoBinding_from_bytes')

    @classmethod
    def from_bytes(cls, raw, *, selected_source, expected_sha256, provenance, limits=DEFAULT_LIMITS):
        _text(selected_source, 'selected_source'); _text(provenance, 'provenance')
        if not isinstance(limits, Limits):
            raise TypeError('format_Limits_required')
        if type(expected_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
            raise ValueError('explicit_lowercase_sha256_required')
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise TypeError('explicit_mesh_bytes_required')
        size = raw.nbytes if isinstance(raw, memoryview) else len(raw)
        if size > limits.max_file_bytes:
            raise FormatError('binding_file_budget')
        data = bytes(raw)
        if _sha(data) != expected_sha256:
            raise ValueError('mesh_source_digest_mismatch')
        mesh = read_mesh(data, limits=limits)
        # This version's reader consumes the first payload only and preserves
        # remaining payload bytes. It does not expose all material partitions.
        if mesh.version != 0x20 or len(mesh.chunks) != 1:
            raise FormatError('unidentified_first_mesh_payload')
        c = mesh.chunks[0]
        if (c.vertex_start, c.index_start, c.triangle_start) != (0, 0, 0) or (
            c.vertex_count != len(mesh.positions) or c.triangle_count != len(mesh.triangles)
            or c.index_count != mesh.triangles.size):
            raise FormatError('first_payload_chunk_mismatch')
        pos, tri = _immutable(mesh.positions, '<f4'), _immutable(mesh.triangles, '<u4')
        tail = mesh.metadata['payload_trailing_raw']; outer = mesh.metadata['skeleton_raw']
        obj = object.__new__(cls)
        values = dict(positions=pos, triangles=tri, selected_source=selected_source,
            source_sha256=expected_sha256, source_bytes=size, provenance=provenance,
            mesh_version=mesh.version, animated=mesh.animated,
            coordinate_space=mesh.coordinate_space, geometry_kind=mesh.geometry_kind,
            header_lod_count_raw=mesh.metadata['flags']['lod_count_raw'],
            payload_tail_bytes=len(tail), payload_tail_sha256=_sha(tail),
            outer_tail_bytes=len(outer), outer_tail_sha256=_sha(outer),
            positions_sha256=_sha(pos.tobytes()), triangles_sha256=_sha(tri.tobytes()))
        for key, value in values.items():
            object.__setattr__(obj, key, value)
        return obj


def _lod_diagnostics(scene, table):
    if table is not None:
        if not isinstance(table, Mapping) or not isinstance(table.get('records'), (list, tuple)):
            raise ValueError('invalid_lod_association_table')
        ordinals = set()
        for record in table['records']:
            if not isinstance(record, Mapping): raise ValueError('invalid_lod_record')
            for key in ('bst_guid_raw', 'ordinal'):
                if type(record.get(key)) is not int or not 0 <= record[key] <= 0xffffffff:
                    raise ValueError('invalid_lod_identity')
            if record['ordinal'] in ordinals: raise ValueError('duplicate_lod_ordinal')
            ordinals.add(record['ordinal'])
            if type(record.get('name')) is not str: raise ValueError('invalid_lod_name')
    report = associate_beamo_lod0(scene, table)
    return {row['object_index']: row for row in report['rows']}


def assemble_beamo_candidates(scene, lod_table, bindings, *, limits=None):
    """Build geometry only from explicit meshName bindings, never LOD name aliases.

    LOD identity and unknown flags remain diagnostics. This is deliberately NOT
    a collision-enabled instance list. The resulting Assembly can be merged by
    the offline candidate helper, but remains runtime_ready=False.
    """
    if not isinstance(scene, Scene): raise TypeError('Scene_required')
    if not isinstance(bindings, Mapping): raise TypeError('explicit_binding_mapping_required')
    limits = AssemblyLimits() if limits is None else limits
    if not isinstance(limits, AssemblyLimits): raise TypeError('AssemblyLimits_required')
    if any(type(n.index) is not int or n.index != i for i, n in enumerate(scene.objects)):
        raise ValueError('invalid_scene_object_indexes')
    source_bindings = dict(bindings)
    if any(type(k) is not str or not k for k in source_bindings):
        raise ValueError('invalid_binding_resource_name')
    try:
        associations = _lod_diagnostics(scene, lod_table)
        lod_error = None
    except (ValueError, TypeError, KeyError) as exc:
        # Bad side-data input cannot pick another Mesh or erase valid explicit
        # geometry. No side attributes are applied anywhere in this module.
        associations = {}; lod_error = 'lod_table_invalid:' + type(exc).__name__
    vertices, faces, included, excluded = [], [], [], []
    vertex_total = triangle_total = 0
    checked = {}
    for node in scene.objects:
        if node.class_name != 'Beamo': continue
        relation = associations.get(node.index)
        diag = {'status': relation['status'], 'reasons': list(relation['reasons']),
                'candidate_ordinals': list(relation['lod_candidate_ordinals']),
                'exact_identity': relation['status'] == 'matched'} if relation else {
                'status': 'unavailable', 'reasons': [lod_error or 'lod_unavailable'],
                'candidate_ordinals': [], 'exact_identity': False}
        if diag['exact_identity']:
            record = relation['lod_records'][0]
            diag.update(record_sha256=record.get('record_sha256'),
                        subcount_raw=record.get('subcount_raw'))
        resource = node.fields.get('meshName')
        row = dict(object_index=node.index, name=node.name, class_name='Beamo',
                   resource_name=resource if type(resource) is str else None,
                   lod_identity=diag, lod_side_attributes_applied=False,
                   native_collision_verified=False, collision_verified=False,
                   resource_binding_verified=False, runtime_ready=False)
        problems = []
        duplicate_names = {d.member.name for d in node.duplicate_members}
        for name in ('meshName', 'transform'):
            if name in duplicate_names: problems.append('ambiguous_member:' + name)
        if type(resource) is not str or not resource: problems.append('missing_literal_mesh_name')
        matrix = _matrix(node.fields.get('transform'))
        if matrix is None: problems.append('invalid_or_missing_transform')
        binding = source_bindings.get(resource) if type(resource) is str else None
        if binding is None: problems.append('explicit_resource_binding_missing')
        elif type(binding) is not ExplicitBeamoBinding: problems.append('invalid_explicit_binding')
        if len(included) >= limits.max_instances: problems.append('instance_budget')
        if problems:
            excluded.append(dict(row, reasons=problems)); continue
        if id(binding) not in checked:
            issues = []
            if binding.mesh_version != 0x20: issues.append('unidentified_first_mesh_payload')
            if binding.coordinate_space != 'sky_native': issues.append('non_native_coordinate_space')
            if binding.geometry_kind != 'render_only': issues.append('unsupported_geometry_kind')
            if binding.animated is not False: issues.append('animated_first_payload')
            pos, tri = binding.positions, binding.triangles
            if (pos.dtype.str != '<f4' or pos.ndim != 2 or pos.shape[1:] != (3,)
                or not np.isfinite(pos).all()): issues.append('invalid_first_positions')
            if tri.dtype.str != '<u4' or tri.ndim != 2 or tri.shape[1:] != (3,):
                issues.append('invalid_first_triangles')
            elif tri.size and int(tri.max()) >= len(pos): issues.append('invalid_first_index')
            if _sha(pos.tobytes()) != binding.positions_sha256 or _sha(tri.tobytes()) != binding.triangles_sha256:
                issues.append('binding_geometry_changed')
            if not len(pos) or not len(tri): issues.append('empty_geometry')
            checked[id(binding)] = issues
        problems.extend(checked[id(binding)])
        pos, tri = binding.positions, binding.triangles
        if vertex_total + len(pos) > limits.max_vertices or triangle_total + len(tri) > limits.max_triangles:
            problems.append('geometry_budget')
        m = np.asarray(matrix, dtype=np.float64)
        determinant = float(np.linalg.det(m[:3, :3]))
        if not np.isfinite(determinant) or abs(determinant) <= 1e-12:
            problems.append('singular_or_nonfinite_transform')
        if problems:
            excluded.append(dict(row, reasons=problems)); continue
        with np.errstate(over='ignore', invalid='ignore'):
            world = pos.astype(np.float64) @ m[:3, :3] + m[3, :3]
            outpos = np.asarray(world, dtype='<f4')
        if not np.isfinite(world).all() or not np.isfinite(outpos).all():
            excluded.append(dict(row, reasons=['transformed_position_overflow'])); continue
        outtri = tri.astype(np.uint64)
        reverse = determinant < 0
        if reverse: outtri[:, [1, 2]] = outtri[:, [2, 1]]
        outtri += vertex_total
        if outtri.size and int(outtri.max()) > np.iinfo(np.uint32).max:
            excluded.append(dict(row, reasons=['index_overflow'])); continue
        vertices.append(outpos); faces.append(outtri.astype('<u4'))
        included.append(dict(row, geometry_kind='beamo_first_submesh_geometry_candidate',
            source_choice='explicit_caller_selection_unverified', selected_source=binding.selected_source,
            source_sha256=binding.source_sha256, source_bytes=binding.source_bytes,
            selection_provenance=binding.provenance, decoded_submesh_ordinal=0,
            first_positions_sha256=binding.positions_sha256, first_triangles_sha256=binding.triangles_sha256,
            header_lod_count_raw=binding.header_lod_count_raw,
            unassembled_payload_tail_bytes=binding.payload_tail_bytes,
            unassembled_payload_tail_sha256=binding.payload_tail_sha256,
            unassembled_outer_tail_bytes=binding.outer_tail_bytes,
            unassembled_outer_tail_sha256=binding.outer_tail_sha256,
            transform=matrix, determinant=determinant, winding_reversed=reverse,
            serialized_flags={key: _flag(node.fields.get(key)) for key in FLAGS},
            native_runtime_gate=None, gate_evaluation='not_performed',
            vertex_start=vertex_total, vertex_count=len(pos), triangle_start=triangle_total,
            triangle_count=len(tri), world_bounds_min=outpos.min(axis=0).tolist(),
            world_bounds_max=outpos.max(axis=0).tolist()))
        vertex_total += len(pos); triangle_total += len(tri)
    positions = _immutable(np.concatenate(vertices) if vertices else np.empty((0, 3)), '<f4')
    triangles = _immutable(np.concatenate(faces) if faces else np.empty((0, 3)), '<u4')
    manifest = dict(schema_version='sky-beamo-first-submesh-candidates-v1', scene_source=scene.source,
        coordinate_space='sky_native', matrix_convention='row_vector_xyz1_at_M',
        geometry_kind='beamo_first_submesh_geometry_candidate', native_collision_verified=False,
        collision_verified=False, resource_binding_verified=False, runtime_ready=False,
        vertex_count=vertex_total, triangle_count=triangle_total, included=included, excluded=excluded,
        limitations=['explicit_resource_selection_is_not_runtime_winner_proof',
                     'only_first_decoded_mesh_payload', 'lod_side_attributes_not_applied',
                     'lod_identity_does_not_select_geometry', 'runtime_gate_not_evaluated',
                     'unknown_flags_not_treated_as_false', 'not_complete_native_collision_scene'])
    return Assembly(positions, triangles, manifest)
