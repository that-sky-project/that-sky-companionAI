"""Read-only Beamo/LOD0 identity audit. No collider selection or resource aliases.

A matched relation says only that two serialized records agree. Missing or
ambiguous relations do NOT mean absent geometry, corrupted assets, or disabled
native objects. No _CompOcc mapping or face-mask polarity is inferred here.
"""
from __future__ import annotations
from collections import Counter,defaultdict
import hashlib,re
from .scene import Scene
from .formats import DEFAULT_LIMITS,FormatError,UnsupportedVersionError,SUPPORTED_MESHES_VERSIONS
from .formats._binary import Cursor,load_input,checked_count
from .formats._meshes import _read_lod_links

EMPTY_SHA='fed40a4aa38016626f1a288eb95948e401805973c6fd2e98b08fb80ebe9955f9'
_NAME=re.compile(r'BstNode_(0|[1-9][0-9]*)\Z')
def _hash(value):return hashlib.sha256(value).hexdigest()

def read_lod0_table(path_or_bytes,*,limits=DEFAULT_LIMITS):
    """Validate current known LVL0 TOC; decode ONLY existing bounded LOD0 reader.

    GEO and render vertices are never decoded. Version 51 is allowed solely for
    the exact previously inspected, hash-pinned WorldEmpty empty container.
    """
    raw,source,digest=load_input(path_or_bytes,limits);cursor=Cursor(raw,label='LOD_audit_header')
    if cursor.take(4)!=b'LVL0':raise FormatError('invalid_LVL0_magic')
    version=cursor.u32();empty=version==51 and digest==EMPTY_SHA
    if version not in SUPPORTED_MESHES_VERSIONS and not empty:raise UnsupportedVersionError(f'unsupported_lod_container_{version}')
    header_end=132 if empty else 136
    toc=Cursor(cursor.take(100),label='LOD_audit_TOC');count=checked_count(toc.unpack('B')[0],min(8,limits.max_segments),'toc_entries');reserved=toc.take(3)
    entries=[];seen=set()
    for _ in range(count):
        tag=toc.take(4)
        try:name=tag.decode('ascii')
        except UnicodeDecodeError as exc:raise FormatError('nonascii_segment_name') from exc
        offset,length=toc.unpack('II')
        if name in seen or not all(x.isupper() or x.isdigit() for x in name):raise FormatError('invalid_or_duplicate_segment_name')
        if offset<header_end or offset>len(raw) or length>len(raw)-offset:raise FormatError('segment_bounds')
        entries.append({'name':name,'offset':offset,'length':length});seen.add(name)
    cursor.take(header_end-cursor.pos);previous=header_end
    for entry in sorted(entries,key=lambda x:x['offset']):
        if entry['length'] and entry['offset']<previous:raise FormatError('overlapping_segments')
        previous=max(previous,entry['offset']+entry['length'])
    lod=next((e for e in entries if e['name']=='LOD0'),None)
    if lod is None:raise FormatError('LOD0_missing')
    compressed=raw[lod['offset']:lod['offset']+lod['length']]
    decoded=_read_lod_links(compressed,'LOD0',limits);rows=[]
    for row in decoded['baked_meshes']:
        try:name=row['name_raw'].decode('utf-8');name_valid=True
        except UnicodeDecodeError:name=row['name'];name_valid=False
        subs=[]
        for sub in row['submeshes']:
            opaque=sub['opaque_records_raw'];colors=sub['color_records_raw']
            subs.append({'ordinal':sub['ordinal'],'uv_count_raw':sub['uv_count_raw'],'opaque_count_raw':len(opaque),
                         'opaque_sha256':_hash(opaque),'opaque_byte_histogram':dict(sorted(Counter(opaque).items())),
                         'color_record_bytes':len(colors),'color_records_sha256':_hash(colors)})
        rows.append({'ordinal':row['ordinal'],'name':name,'name_utf8_valid':name_valid,'name_raw_hex':row['name_raw'].hex(),
                     'bst_guid_raw':row['bst_guid_raw'],'compression_flag_raw':row['compression_flag_raw'],
                     'subcount_raw':len(subs),'submeshes':subs,'record_sha256':_hash(row['record_raw'])})
    if empty and rows:raise FormatError('bound_empty_fixture_not_empty')
    return {'schema':'sky-lod0-association-table-v1','source':source,'source_sha256':digest,'raw_version':version,
            'empty_fixture_sha_bound':empty,'toc_count_raw':count,'toc_reserved_hex':reserved.hex(),'lod0_section':lod,
            'lod0_compressed_sha256':_hash(compressed),'expanded_bytes':decoded['expanded_bytes'],'mesh_table_end':decoded['mesh_table_end'],
            'unparsed_tail_bytes':len(decoded['unparsed_tail_raw']),'unparsed_tail_sha256':_hash(decoded['unparsed_tail_raw']),
            'record_count':len(rows),'records':rows,'collision_verified':False}

def _guid(node):
    value=node.fields.get('bstGuid')
    return int.from_bytes(value,'little') if isinstance(value,bytes) and len(value)==4 else None

def associate_beamo_lod0(scene:Scene,table:dict|None):
    """Pure strict association retaining every Beamo and all candidate records."""
    if not isinstance(scene,Scene):raise TypeError('Scene_required')
    records=[] if table is None else table['records'];bylod=defaultdict(list);byguid=defaultdict(list);byname=defaultdict(list)
    for record in records:bylod[record['bst_guid_raw']].append(record)
    for node in scene.objects:
        byname[node.name].append(node.index)
        guid=_guid(node)
        if guid is not None:byguid[guid].append(node.index)
    output=[]
    for node in scene.objects:
        if node.class_name!='Beamo':continue
        reasons=[];guid=_guid(node);name_match=_NAME.fullmatch(node.name);name_guid=int(name_match[1]) if name_match else None
        if name_guid is None or name_guid>0xffffffff:reasons.append('name_noncanonical')
        ambiguous={d.member.name for d in node.duplicate_members}
        if 'bstGuid' in ambiguous:reasons.append('scene_guid_ambiguous_member')
        elif 'bstGuid' not in node.fields:reasons.append('scene_guid_missing')
        elif guid is None:reasons.append('scene_guid_malformed')
        if guid is not None and name_guid is not None and name_guid!=guid:reasons.append('scene_name_guid_mismatch')
        if len(byname[node.name])>1:reasons.append('duplicate_scene_name')
        if guid is not None and len(byguid[guid])>1:reasons.append('duplicate_scene_guid')
        mesh_name=node.fields.get('meshName')
        if 'meshName' in ambiguous:reasons.append('scene_mesh_name_ambiguous_member')
        elif not isinstance(mesh_name,str) or not mesh_name:reasons.append('scene_mesh_name_missing')
        matches=bylod.get(guid,[]) if guid is not None else []
        if table is None:reasons.append('lod_unavailable')
        elif guid is not None:
            if not matches:reasons.append('lod_guid_missing')
            elif len(matches)>1:reasons.append('duplicate_lod_guid')
            else:
                if not matches[0].get('name_utf8_valid',True):reasons.append('lod_name_invalid_utf8')
                if matches[0]['name']!=mesh_name:reasons.append('lod_mesh_name_mismatch')
        output.append({'object_index':node.index,'object_name':node.name,'scene_bst_guid_raw':guid,'object_name_guid':name_guid,
                       'mesh_name':mesh_name,'status':reasons[0] if reasons else 'matched','reasons':reasons,
                       'scene_same_guid_indexes':list(byguid.get(guid,[])),'scene_same_name_indexes':list(byname[node.name]),
                       'lod_candidate_ordinals':[r['ordinal'] for r in matches],'lod_records':matches,'collision_verified':False})
    counts=Counter(r['status'] for r in output);reasons=Counter(reason for r in output for reason in r['reasons'])
    return {'schema':'sky-beamo-lod0-association-v1','beamo_count':len(output),'status_counts':dict(sorted(counts.items())),
            'reason_counts':dict(sorted(reasons.items())),'lod0_record_count':len(records),'rows':output,
            'collision_verified':False,'geometry_created':False,'resource_alias_mapping_performed':False,
            'interpretation':'Unresolved relationships are audit outcomes, not asset corruption or proof of absent geometry.'}
