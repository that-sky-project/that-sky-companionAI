"""Bounded LVL0 v60/v61 reader. GEO is render geometry, never implicit collision.
No LGPL decoder is embedded: supply a decoder, or install an MIT meshoptimizer binding.
LOD mesh associations are read from the MIT SkyBstbake documented layout.
"""
from dataclasses import replace
import struct
import numpy as np
from ._types import DEFAULT_LIMITS, FormatError, UnsupportedVersionError, MissingDecoderError, MeshData, MeshBundle, MeshChunk
from ._binary import Cursor, load_input, checked_count, readonly, validate_positions, validate_indices, bounds
from ._lz4 import decompress_lz4_block

# Raw header variants observed in CN 0.16.7/0.16.8, not an unrestricted low-bit mask.
# The high 16 bits are retained without inventing their semantics.
_LAYOUT_VERSIONS = {60:60, 61:61, 0x0001003c:60, 0x0002003d:61}
SUPPORTED_MESHES_VERSIONS = tuple(_LAYOUT_VERSIONS)


def _default_decoder(count,stride,data):
    try:
        import meshoptimizer
    except ImportError as exc:
        raise MissingDecoderError('meshopt_decoder_required: inject NodeMeshoptDecoder or MIT meshoptimizer binding') from exc
    return meshoptimizer.decode_vertex_buffer(count,stride,data)


def _decode_vertices(decoder,count,data,limits):
    if not data or data[0] & 0xf0 != 0xa0 or data[0] & 15 not in (0,1):
        raise FormatError('unsupported_meshopt_vertex_stream')
    if count*36 > limits.max_uncompressed_bytes:
        raise FormatError('meshopt_expanded_size_limit')
    try:
        decoded=(decoder or _default_decoder)(count,36,data)
    except MissingDecoderError:
        raise
    except Exception as exc:
        raise FormatError('meshopt_decoder_failed') from exc
    if isinstance(decoded,np.ndarray):
        if decoded.nbytes != count*36:
            raise FormatError('meshopt_output_size_mismatch')
        raw=decoded.tobytes(order='C')
    elif isinstance(decoded,(bytes,bytearray,memoryview)):
        if len(decoded) != count*36:
            raise FormatError('meshopt_output_size_mismatch')
        raw=bytes(decoded)
    else:
        raise FormatError('meshopt_decoder_must_return_bytes_or_array')
    return raw,data[0]&15


def _read_geo(data,name,version,source,decoder,limits):
    cursor=Cursor(data,label=name)
    index_count,vertex_count,terrain_count,cloud_count,subchunk_count=cursor.unpack('5I')
    checked_count(index_count,limits.max_indices,'indices')
    checked_count(vertex_count,limits.max_vertices,'vertices')
    checked_count(terrain_count+cloud_count,limits.max_chunks,'chunks')
    checked_count(subchunk_count,limits.max_subchunks,'subchunks')
    if index_count%3:
        raise FormatError('geo_index_count_not_triangular')
    meshopt_version=None;encoded_size=0
    if vertex_count:
        encoded_size=cursor.u32()
        encoded=cursor.take(encoded_size)
        raw_vertices,meshopt_version=_decode_vertices(decoder,vertex_count,encoded,limits)
    else:
        raw_vertices=b''
        if index_count or terrain_count or cloud_count or subchunk_count:
            raise FormatError('nonempty_geo_without_vertices')
    base_size=index_count+56*(terrain_count+cloud_count)+8*subchunk_count
    if cursor.remaining < base_size or (_LAYOUT_VERSIONS[version]==61 and cursor.remaining < base_size+4):
        raise FormatError('geo_section_size_mismatch')
    local_raw=cursor.take(index_count)
    local=np.frombuffer(local_raw,dtype=np.uint8)
    records=np.frombuffer(raw_vertices,dtype=np.uint8).reshape((-1,36))
    positions=readonly(np.frombuffer(raw_vertices,dtype='<f4').reshape((-1,9))[:,:3],dtype=np.float32)
    validate_positions(positions)
    chunks=[];faces=[];triangle_cursor=0
    for ordinal in range(terrain_count+cloud_count):
        raw=cursor.take(56)
        idx_start,vtx_start,sub_start,idx_count,vtx_count,sub_count=struct.unpack_from('<IIIHBB',raw)
        aabb=np.asarray(struct.unpack_from('<6f',raw,16),dtype=np.float64).reshape((2,3))
        if not np.isfinite(aabb).all() or np.any(aabb[1]<aabb[0]):
            raise FormatError('invalid_chunk_bounds')
        if (idx_count%3 or idx_start+idx_count>index_count or vtx_start+vtx_count>vertex_count
                or sub_start+sub_count>subchunk_count):
            raise FormatError('chunk_range_out_of_bounds')
        chunk_indices=local[idx_start:idx_start+idx_count]
        validate_indices(chunk_indices,vtx_count)
        checked_count((triangle_cursor+idx_count//3)*3,limits.max_indices,'expanded_chunk_indices')
        triangles=chunk_indices.astype(np.uint32).reshape((-1,3))+vtx_start
        faces.append(triangles)
        flags={'byte15_raw':sub_count,'tail_words_raw':struct.unpack_from('<4I',raw,40),
               'tail_raw':raw[40:56],'collision_verified':False,'discard_interpretation':'unverified'}
        chunks.append(MeshChunk(group=name,kind='terrain_render' if ordinal<terrain_count else 'cloud_render',
                                vertex_start=vtx_start,vertex_count=vtx_count,index_start=idx_start,index_count=idx_count,
                                triangle_start=triangle_cursor,triangle_count=idx_count//3,
                                subchunk_start=sub_start,subchunk_count=sub_count,
                                bounds_min=tuple(aabb[0]),bounds_max=tuple(aabb[1]),flags=flags,raw_descriptor=raw))
        triangle_cursor+=idx_count//3
    subchunks_raw=cursor.take(subchunk_count*8)
    # Real files contain an optional extension after all geometry arrays.
    # v60 occurs both with no extension and a four-byte value; v61 has at
    # least that value, or flag 1 + count + fixed 20-byte opaque records.
    # Preserve bytes: no surface/height/collision meaning has been established.
    trailer_raw=cursor.take(cursor.remaining)
    extension_flag=extension_count=None;extension_records=b''
    if not trailer_raw:
        extension_layout='absent'
    elif len(trailer_raw)==4:
        extension_flag=struct.unpack('<I',trailer_raw)[0]
        extension_layout='opaque_u32'
    elif len(trailer_raw)>=8:
        extension_flag,extension_count=struct.unpack_from('<II',trailer_raw)
        checked_count(extension_count,limits.max_subchunks,'geo_extension_records')
        if extension_flag!=1 or len(trailer_raw)!=8+extension_count*20:
            raise FormatError('geo_extension_size_or_tag')
        extension_records=trailer_raw[8:]
        extension_layout='tag1_count20_raw'
    else:
        raise FormatError('geo_extension_size_or_tag')
    subchunks=readonly(np.frombuffer(subchunks_raw,dtype=np.uint8),shape=(-1,8))
    triangles=readonly(np.concatenate(faces,axis=0) if faces else np.empty((0,3)),dtype=np.uint32)
    validate_indices(triangles,vertex_count)
    attributes={'vertex_records_raw':raw_vertices,'normals_packed':readonly(records[:,12:16]),
                'material_ids':readonly(records[:,16:20]),'material_weights_raw':readonly(records[:,20:24]),
                'color_ao_raw':readonly(records[:,24:28]),'light_raw':readonly(records[:,28:32]),
                'metadata_raw':readonly(records[:,32:36]),'local_indices_raw':local_raw,
                'subchunks_raw':subchunks_raw,'subchunk_fields_raw':subchunks}
    lo,hi=bounds(positions)
    metadata={'group':name,'meshopt_version':meshopt_version,'encoded_vertex_bytes':encoded_size,
              'version61_trailer_raw':trailer_raw,'version61_trailer_u32_raw':extension_flag,
              'extension_layout':extension_layout,'extension_flag_raw':extension_flag,
              'extension_record_count_raw':extension_count,'extension_records_raw':extension_records,
              'extension_semantics':'unverified; raw records are not collision surfaces',
              'terrain_chunk_count':terrain_count,'cloud_chunk_count':cloud_count,
              'subchunk_count':subchunk_count,'index_buffer_count':index_count,'bounds_min':lo,'bounds_max':hi,
              'collision_verified':False,'subchunk_field_order':('material_id','triangle_count','vertex_count','triangle_start','triangle_end','vertex_start','vertex_end','padding')}
    return MeshData(positions,triangles,source,version,tuple(chunks),attributes,metadata)


def _read_lod_links(data,name,limits):
    expanded=decompress_lz4_block(data,max_output=limits.max_uncompressed_bytes)
    cursor=Cursor(expanded,label=name)
    mesh_count=checked_count(cursor.u32(),limits.max_chunks,'baked_meshes')
    associations=[];total_submeshes=0
    for ordinal in range(mesh_count):
        begin=cursor.pos
        length=cursor.u32()
        if length>4096:
            raise FormatError('baked_mesh_name_limit')
        name_raw=cursor.take(length)
        guid,sub_count=cursor.unpack('II')
        total_submeshes+=sub_count
        checked_count(total_submeshes,limits.max_chunks,'baked_submeshes')
        compressed=cursor.unpack('B')[0]
        if compressed not in (0,1):
            raise FormatError('invalid_baked_color_compression_flag')
        subs=[]
        for index in range(sub_count):
            uv_count,index_bytes=cursor.unpack('II')
            checked_count(uv_count,limits.max_vertices,'baked_uvs')
            checked_count(index_bytes,limits.max_indices*2,'baked_index_bytes')
            colors=cursor.take(12 if compressed else 12*uv_count)
            indices_raw=cursor.take(index_bytes)
            subs.append({'ordinal':index,'uv_count_raw':uv_count,'opaque_byte_count_raw':index_bytes,
                         'color_records_raw':colors,'opaque_records_raw':indices_raw,
                         'opaque_semantics':'unverified; real odd-length records rule out an unconditional uint16 index interpretation'})
        associations.append({'ordinal':ordinal,'name':name_raw.decode('utf-8','replace'),'name_raw':name_raw,
                             'bst_guid_raw':guid,'compression_flag_raw':compressed,'submeshes':tuple(subs),
                             'record_raw':expanded[begin:cursor.pos]})
    return {'baked_meshes':tuple(associations),'expanded_bytes':len(expanded),
            'mesh_table_end':cursor.pos,'unparsed_tail_raw':cursor.take(cursor.remaining),
            'tail_semantics':'cloud/skirt/occlusion streams retained, not classified as collision'}


def read_meshes(path_or_bytes, decoder=None, *, limits=DEFAULT_LIMITS, decode_lod_links=True):
    raw,source,digest=load_input(path_or_bytes,limits)
    cursor=Cursor(raw,label='LVL0_header')
    if cursor.take(4)!=b'LVL0':
        raise FormatError('invalid_LVL0_magic')
    version=cursor.u32()
    if version not in SUPPORTED_MESHES_VERSIONS:
        raise UnsupportedVersionError(f'unsupported_meshes_version_{version}')
    toc_raw=cursor.take(100)
    toc=Cursor(toc_raw,label='LVL0_toc')
    # MIT SkyBstbake reads count as data[8]; bytes 9..11 can contain
    # nonzero data in genuine files. Do not discard or interpret those bytes.
    count=checked_count(toc.unpack('B')[0],min(limits.max_segments,8),'toc_entries')
    toc_count_reserved= toc.take(3)
    entries=[];seen=set()
    for _ in range(count):
        tag=toc.take(4)
        try:name=tag.decode('ascii')
        except UnicodeDecodeError as exc:raise FormatError('nonascii_segment_name') from exc
        offset,length=toc.unpack('II')
        if name in seen or not all(c.isupper() or c.isdigit() for c in name):
            raise FormatError('invalid_or_duplicate_segment_name')
        if offset<136 or offset>len(raw) or length>len(raw)-offset:
            raise FormatError('segment_bounds')
        seen.add(name);entries.append({'name':name,'offset':offset,'length':length})
    pad=cursor.take(4);header_bounds_raw=cursor.take(24)
    previous=136
    for entry in sorted(entries,key=lambda v:v['offset']):
        if entry['offset']<previous and entry['length']:
            raise FormatError('overlapping_segments')
        previous=max(previous,entry['offset']+entry['length'])
    sections={v['name']:raw[v['offset']:v['offset']+v['length']] for v in entries}
    geo_names=sorted(name for name in sections if name.startswith('GEO') and name[3].isdigit())
    if not geo_names:
        raise FormatError('no_supported_GEO_section')
    meshes=[];chunks=[];vertices=[];triangles=[];vbase=tbase=ibase=0
    for name in geo_names:
        mesh=_read_geo(sections[name],name,version,source,decoder,limits)
        checked_count(vbase+len(mesh.positions),limits.max_vertices,'aggregate_vertices')
        checked_count((tbase+len(mesh.triangles))*3,limits.max_indices,'aggregate_indices')
        meshes.append(mesh);vertices.append(mesh.positions);triangles.append(mesh.triangles+np.uint32(vbase))
        chunks.extend(replace(c,vertex_start=c.vertex_start+vbase,index_start=c.index_start+ibase,
                              triangle_start=c.triangle_start+tbase) for c in mesh.chunks)
        vbase+=len(mesh.positions);tbase+=len(mesh.triangles);ibase+=mesh.metadata['index_buffer_count']
    positions=readonly(np.concatenate(vertices) if vertices else np.empty((0,3)),dtype=np.float32)
    indices=readonly(np.concatenate(triangles) if triangles else np.empty((0,3)),dtype=np.uint32)
    lod_links={name:_read_lod_links(data,name,limits) for name,data in sections.items()
               if decode_lod_links and name.startswith('LOD') and name[3].isdigit()}
    lo,hi=bounds(positions)
    metadata={'sha256':digest,'version_header_raw':version,'layout_version':_LAYOUT_VERSIONS[version],
              'version_high16_raw':version>>16,'toc_count_raw':count,'toc_count_reserved_raw':toc_count_reserved,
              'sections':tuple(entries),'raw_sections':sections,'toc_raw':toc_raw,
              'header_padding_raw':pad,'header_bounds_raw':header_bounds_raw,'lod_links':lod_links,
              'unparsed_sections':tuple(name for name in sections if name not in geo_names),
              'coverage':'GEO render geometry and optional LOD mesh association table; no collision semantics asserted',
              'collision_verified':False,'bounds_min':lo,'bounds_max':hi}
    return MeshBundle(positions,indices,source,version,tuple(meshes),tuple(chunks),metadata)
