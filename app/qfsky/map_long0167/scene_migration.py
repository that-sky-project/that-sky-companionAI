"""Strict typed Scene migration, map-neutral and entirely offline.

Adapted from the reviewed supplied _room0167/codec/scene_migration_0167.py.
No new map identity is inferred from a filename or arbitrary hash. The caller
must provide current resource-fold, added bool and every rail FP64 value.
Preservation-only mode exposes review bytes but refuses candidate_bytes().
Neither mode supplies Actor, a Scene producer, identity binding or readiness.
"""
from dataclasses import dataclass,field,replace
import math
import struct
from qfsky.long0167.codec import scene_wire as scene

OLD_SHA=scene.PUBLIC_ELF_SHA256
NEW_SHA=scene.PUBLIC_ELF_SHA256_0168

def rail_token_indices(observed):
    positions=[];bit=0
    for token in observed._tokens:
        if token.kind=='raw':bit=(bit+7)&~7
        positions.append(bit);bit+=token.extent*(8 if token.kind=='raw'else 1)
    start_pos=dict(observed._boundaries)['move_rails']
    indices=[i for i,p in enumerate(positions)if p==start_pos]
    if len(indices)!=1:raise ValueError('migration_rail_boundary')
    start=indices[0];count=dict(observed._counts)['move_rails'];tokens=observed._tokens
    if(tokens[start].kind,tokens[start].extent,tokens[start].value)!=('bits',5,count):
        raise ValueError('migration_rail_count_shape')
    result=[]
    for n in range(count):
        b=start+1+2*n;f=b+1
        if (f>=len(tokens)or tokens[b].kind!='bits'or tokens[b].extent!=1 or
                tokens[b].value not in(0,1)or tokens[f].kind!='raw'or tokens[f].extent!=8):
            raise ValueError('migration_rail_element_shape')
        result.append(f)
    return tuple(result)

@dataclass(frozen=True,repr=False)
class SceneMigration:
    _body:bytes=field(repr=False)
    _review_only:bool=field(repr=False)
    _metadata:dict=field(repr=False)
    def review_bytes(self):return self._body
    def candidate_bytes(self):
        if self._review_only:raise ValueError('preserved_legacy_review_not_current_candidate')
        return self._body
    def metadata(self):return dict(self._metadata)

def migrate_legacy_scene(body,*,target_resource_tag,anonymous_bool_0167,
                         rail_f64_values=None,preserve_legacy_review_only=False,
                         verified_source_sha=OLD_SHA,verified_target_sha=NEW_SHA,
                         verified_target_build=421629):
    if verified_source_sha!=OLD_SHA or verified_target_sha!=NEW_SHA or verified_target_build!=421629:
        raise ValueError('migration_version_or_schema_unverified')
    if type(target_resource_tag)is not int or not 0<=target_resource_tag<=65535:
        raise ValueError('migration_explicit_current_resource_tag_required')
    if type(anonymous_bool_0167)is not bool or type(preserve_legacy_review_only)is not bool:
        raise ValueError('migration_explicit_added_bool_required')
    if preserve_legacy_review_only:
        if rail_f64_values is not None:raise ValueError('migration_conflicting_modes')
    elif type(rail_f64_values)is not tuple:
        raise ValueError('migration_explicit_current_rails_required')
    old=scene.inspect_scene_wire(body,verified_profile_sha256=OLD_SHA,check_zero_padding=True)
    if scene.reencode_scene_wire(old)!=body:raise ValueError('migration_old_roundtrip')
    rails=rail_token_indices(old);tokens=list(old._tokens)
    if not tokens or tokens[0].kind!='raw'or tokens[0].extent!=4:raise ValueError('migration_resource_header')
    if not preserve_legacy_review_only:
        if len(rail_f64_values)!=len(rails):raise ValueError('migration_rail_count')
        if any(type(x)is not float or math.isnan(x)for x in rail_f64_values):raise ValueError('migration_rail_f64')
        for index,value in zip(rails,rail_f64_values):tokens[index]=replace(tokens[index],value=struct.pack('<d',value))
    tokens[0]=replace(tokens[0],value=struct.pack('<I',target_resource_tag))
    migrated=scene.reencode_scene_wire(replace(old,_tokens=tuple(tokens)),verified_profile_sha256=NEW_SHA,
                                       anonymous_bool_0167=anonymous_bool_0167)
    checked=scene.inspect_scene_wire(migrated,verified_profile_sha256=NEW_SHA,check_zero_padding=True)
    if scene.reencode_scene_wire(checked)!=migrated or checked._counts!=old._counts:
        raise ValueError('migration_current_roundtrip_or_counts')
    insertion=checked._home_owner_end_token_index;retained=list(checked._tokens)
    if retained[insertion].kind!='bits'or retained[insertion].value!=int(anonymous_bool_0167):
        raise ValueError('migration_added_bool')
    del retained[insertion]
    if tuple(retained)!=tuple(tokens):raise ValueError('migration_other_token_changed')
    allowed={0,*rails}
    if any(old._tokens[i]!=tokens[i]for i in range(len(tokens))if i not in allowed):
        raise ValueError('migration_unrequested_old_token_change')
    return SceneMigration(migrated,preserve_legacy_review_only,{
        'strict_current_roundtrip':True,'old_tokens_preserved_except_explicit_fields':True,
        'group_counts_preserved':True,'rail_count':len(rails),'scene_bytes':len(migrated),
        'review_only':preserve_legacy_review_only,'current_inputs_origin_attested':False,
        'native_scene_producer_reproduced':False,'actor_source_present':False,
        'full_connection_ready':False,'network_used':False})
