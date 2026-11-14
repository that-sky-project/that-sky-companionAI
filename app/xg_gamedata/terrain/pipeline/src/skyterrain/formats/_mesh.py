"""Strict .mesh v0x20 geometry reader, verified against CN 0.16.8 fixtures (layout unchanged from 0.16.7).
Byte-layout reference: SkyVEx mesh_parser.py (MIT), commit b38609d2367a23d667514e9ef24947c43f987b97.
This implementation adds bounded reads/counts and preserves uninterpreted records.
"""
import struct
import numpy as np
from ._types import DEFAULT_LIMITS, FormatError, UnsupportedVersionError, MeshData, MeshChunk
from ._binary import Cursor, load_input, checked_count, readonly, validate_positions, validate_indices, bounds
from ._lz4 import decompress_lz4_block

SUPPORTED_MESH_VERSIONS = (0x20,)


def _skeleton(data, limits):
    if not data:
        return None
    cursor = Cursor(data, label='skeleton')
    header = cursor.take(68)
    count = checked_count(cursor.u32(), limits.max_bones, 'bones')
    header += struct.pack('<I', count) + cursor.take(13)
    cursor.require(count*132)
    bones = []
    for index in range(count):
        name = cursor.take(64)
        matrix = cursor.take(64)
        parent = cursor.u32()
        if parent > count or parent == index+1:
            raise FormatError('invalid_bone_parent')
        if not np.isfinite(np.frombuffer(matrix, dtype='<f4')).all():
            raise FormatError('nonfinite_bind_matrix')
        bones.append({'name_raw': name, 'name': name.split(b'\0',1)[0].decode('utf-8','replace'),
                      'parent_index': parent-1 if parent else -1,
                      'inverse_bind_matrix': readonly(np.frombuffer(matrix,dtype='<f4'),shape=(4,4))})
    # Detect parent cycles without silently changing a malformed skeleton hierarchy.
    parents = [bone['parent_index'] for bone in bones]
    for index in range(count):
        seen = set()
        while index != -1:
            if index in seen:
                raise FormatError('cyclic_bone_parents')
            seen.add(index); index = parents[index]
    return {'header_raw': header, 'bones': tuple(bones), 'trailing_raw': cursor.take(cursor.remaining)}


def read_mesh(path_or_bytes, *, limits=DEFAULT_LIMITS):
    raw, source, digest = load_input(path_or_bytes, limits)
    cursor = Cursor(raw, label='mesh_header')
    version = cursor.u32()
    if version not in SUPPORTED_MESH_VERSIONS:
        raise UnsupportedVersionError(f'unsupported_mesh_version_{version}')
    name_raw = cursor.take(64)
    lod_count = cursor.u32()
    animated, occlusion_flag = cursor.unpack('BB')
    if animated not in (0,1):
        raise FormatError('invalid_animation_flag')
    marker_count = checked_count(cursor.u32(), limits.max_markers, 'markers')
    marker_raw = cursor.take(marker_count*112)
    payload_offset = cursor.pos
    compressed, compressed_size, expanded_size = cursor.unpack('III')
    if compressed not in (0,1):
        raise FormatError('invalid_compression_flag')
    if compressed_size <= 0 or expanded_size <= 0 or expanded_size > limits.max_uncompressed_bytes:
        raise FormatError('invalid_compression_sizes')
    payload = cursor.take(compressed_size)
    skeleton_raw = cursor.take(cursor.remaining)
    if compressed:
        data = decompress_lz4_block(payload, expanded_size, max_output=limits.max_uncompressed_bytes)
    else:
        if len(payload) != expanded_size:
            raise FormatError('uncompressed_size_mismatch')
        data = payload
    block = Cursor(data, label='mesh_payload')
    leading_word = block.u32()  # A distinct first word; not part of the following vec3 bounds.
    aabbs = block.unpack('12f')
    quant_min, quant_max = block.unpack('8f'), block.unpack('8f')
    counts = block.unpack('8I')
    vertex_count, index_count, index32, point_count, prop11, prop12, prop13, prop14 = counts
    checked_count(vertex_count, limits.max_vertices, 'vertices')
    checked_count(index_count, limits.max_indices, 'indices')
    if index32 not in (0,1) or index_count % 3:
        raise FormatError('invalid_index_layout')
    face_count, index_width = index_count//3, (4 if index32 else 2)
    load_normals, load_info2, byte_flag = block.unpack('3B')
    if load_normals not in (0,1) or load_info2 not in (0,1):
        raise FormatError('invalid_optional_stream_flag')
    zip_pos, zip_uv, flag3 = block.unpack('3I')
    reserved = block.take(16)
    if zip_pos not in (0,vertex_count*4) or zip_uv not in (0,vertex_count*4):
        raise FormatError('unsupported_quantized_stream_size')
    attributes = {}
    if not zip_pos:
        records = block.take(vertex_count*16)
        positions = np.frombuffer(records,dtype='<f4').reshape((-1,4))[:,:3]
        attributes['position_records_raw'] = records
    else:
        positions = None
    if load_normals:
        attributes['normals_raw'] = block.take(vertex_count*4)
    if not zip_uv:
        records = block.take(vertex_count*16)
        attributes['uv_records_raw'] = records
        attributes['uv0'] = readonly(np.frombuffer(records,dtype='<f2').reshape((-1,8))[:,:2],dtype=np.float32)
    if animated:
        records = block.take(vertex_count*8)
        skin = np.frombuffer(records,dtype=np.uint8).reshape((-1,8))
        attributes['bone_indices_raw'] = readonly(skin[:,:4])
        attributes['bone_weights_raw'] = readonly(skin[:,4:])
    indices_raw = block.take(index_count*index_width)
    indices = np.frombuffer(indices_raw,dtype='<u4' if index32 else '<u2')
    validate_indices(indices,vertex_count)
    triangles = readonly(indices,dtype=np.uint32,shape=(-1,3))
    optional = {}
    for name,size in (('info2',index_count*index_width if load_info2 else 0),
                      ('point_indices',vertex_count*index_width if point_count else 0),
                      ('prop11_indices',vertex_count*index_width if prop11 else 0),
                      ('prop12',prop12*index_width),('prop13',prop13*4),
                      ('prop14',prop14*(8 if index32 else 4))):
        optional[name] = block.take(size)
    attributes['triangle_metadata_raw'] = block.take(face_count*4)
    if zip_pos:
        quant_bounds = np.asarray(aabbs[6:12],dtype=np.float64).reshape((2,3))
        if not np.isfinite(quant_bounds).all() or np.any(quant_bounds[1]<quant_bounds[0]):
            raise FormatError('invalid_quantization_bounds')
        packed_raw = block.take(vertex_count*4)
        packed = np.frombuffer(packed_raw,dtype='<u4')
        # Bit order in this format is X high, Y middle, Z low; the top two bits stay raw.
        values = np.column_stack(((packed>>20)&1023,(packed>>10)&1023,packed&1023)).astype(np.float64)
        positions = quant_bounds[0] + values/1023.0*(quant_bounds[1]-quant_bounds[0])
        attributes['packed_position_words'] = readonly(packed)
        attributes['position_auxiliary_raw'] = block.take(vertex_count)
    if zip_uv:
        records = np.frombuffer(block.take(vertex_count*4),dtype=np.uint8).reshape((-1,4))
        minimum = np.asarray(quant_min[:2],dtype=np.float64)
        maximum = np.asarray(quant_max[:2],dtype=np.float64)
        if not np.isfinite([minimum,maximum]).all() or np.any(maximum<minimum):
            raise FormatError('invalid_uv_quantization_bounds')
        values = (records[:,:2].astype(np.uint32)<<8)|records[:,2:]
        attributes['uv0'] = readonly(minimum+values/65535.0*(maximum-minimum),dtype=np.float32)
        attributes['uv_quantized_raw'] = records.tobytes()
    positions = readonly(positions,dtype=np.float32,shape=(-1,3))
    validate_positions(positions)
    lo, hi = bounds(positions)
    skeleton = _skeleton(skeleton_raw,limits) if animated and skeleton_raw else None
    flags = {'animated':bool(animated),'occlusion_header_raw':occlusion_flag,'lod_count_raw':lod_count,
             'load_normals':load_normals,'load_info2':load_info2,'byte_flag_raw':byte_flag,
             'zip_position_bytes_raw':zip_pos,'zip_uv_bytes_raw':zip_uv,'flag3_raw':flag3,
             'reserved_16_raw':reserved,'counts_8_raw':counts}
    chunk = MeshChunk(group='mesh',kind='skinned_render' if animated else 'static_render',
                      vertex_start=0,vertex_count=vertex_count,index_start=0,index_count=index_count,
                      triangle_start=0,triangle_count=face_count,bounds_min=lo,bounds_max=hi,flags=flags)
    metadata = {'sha256':digest,'name_raw':name_raw,'name':name_raw.split(b'\0',1)[0].decode('utf-8','replace'),
                'payload_offset':payload_offset,'compressed_bytes':compressed_size,'expanded_bytes':expanded_size,
                'marker_count':marker_count,'markers_raw':marker_raw,'leading_payload_u32_raw':leading_word,
                'aabb_values_raw':aabbs,'quant_min_raw':quant_min,'quant_max_raw':quant_max,
                'optional_streams_raw':optional,'payload_trailing_raw':block.take(block.remaining),
                'skeleton':skeleton,'skeleton_raw':skeleton_raw,
                'flags':flags,'collision_verified':False,'bounds_min':lo,'bounds_max':hi}
    return MeshData(positions,triangles,source,version,(chunk,),attributes,metadata,bool(animated))
