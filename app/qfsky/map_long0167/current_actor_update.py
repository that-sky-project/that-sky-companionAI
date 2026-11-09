"""Current->current Actor elapsed/prepared-revision update, build 421589.

No legacy conversion, Home constant, identity rebinding, pose reconstruction,
position rewrite or replacement of new dynamic fields. Input must be the
ORIGINAL retained current Actor, not the previous output.

Native evidence: timestamp1715998 is FP64[0,3]/12-bit (1715980..5998).
Prepared tick1f9de60..7c independently adds1 and STRB truncates current at
state+52db and history atstate+58a9. Wire labels21dc8cc and21de7c4/21df3a4.
This scalar schedule is not a reconstruction of ordinary fill/physics/cache
commits. See runtime_protocol's current native evidence for exact instructions.
"""
from ._cw_bounds import LIFE as _CW_LIFE, STEPS as _CW_STEPS  # claudework
from dataclasses import dataclass,field
import math
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire,reencode_actor_wire
from qfsky.long0167.float_quantization import decode_float64,quantize_float64,NEAREST_EVEN_GRADUAL,_op
from qfsky.join0167_profile import NATIVE_SHA256

def _checked(body,map_id,sha):
    if sha!=NATIVE_SHA256:raise ValueError('current_actor_version_unverified')
    if type(body)is not bytes or not 1<=len(body)<=0x528:raise ValueError('current_actor_native_size')
    if type(map_id)is not int or not 0<map_id<=0xffffffff:raise ValueError('current_actor_map')
    observed=inspect_actor_wire(body,verified_profile_sha256=sha,check_zero_padding=True,require_canonical_writer=True)
    if not observed.matches_prefix_u32(map_id)or reencode_actor_wire(observed,verified_profile_sha256=sha)!=body:
        raise ValueError('current_actor_map_or_canonical')
    return observed

def _token_locations(observed):
    result=[];cursor=0
    for token in observed._tokens:
        before=cursor
        if token.kind=='raw':cursor=(cursor+7)&~7;width=token.extent*8
        elif token.kind=='bits':width=token.extent
        else:raise ValueError('current_actor_token_kind')
        result.append((before,cursor,width,token));cursor+=width
    if cursor!=observed.bit_length:raise ValueError('current_actor_token_extent')
    return result

@dataclass(frozen=True,repr=False)
class CurrentActorUpdate:
    _body:bytes=field(repr=False)
    pose_clocks:int
    timed_records:int
    def wire_bytes(self):return self._body
    def metadata(self):
        return {'current_schema_only':True,'legacy_migration_used':False,'home_template_used':False,
            'map_and_transform_preserved':True,'new_dynamic_fields_preserved':True,
            'pose_clocks_updated':self.pose_clocks,'revision_bytes_updated':2,
            'timed_records_retained':self.timed_records,'prepared_scalar_schedule_only':True,
            'native_full_actor_producer':False,'server_accepted':False,'network_used':False}

def advance_current_actor(body,*,elapsed_seconds,tick_steps,map_id,verified_native_sha256=NATIVE_SHA256):
    if(type(elapsed_seconds)not in(int,float)or not math.isfinite(elapsed_seconds)or not 0<=elapsed_seconds<=_CW_LIFE):
        raise ValueError('current_actor_elapsed')
    if type(tick_steps)is not int or not 1<=tick_steps<=_CW_STEPS:raise ValueError('current_actor_steps')
    observed=_checked(body,map_id,verified_native_sha256);locations=_token_locations(observed)
    clocks=[position for label,position in observed._boundaries if label=='0x1715998']
    counts=dict(observed._counts);expected=counts.get('pose_records',0)+counts.get('history_pose_records',0)
    if not 0<=expected<=8 or len(clocks)!=expected or len(set(clocks))!=expected:
        raise ValueError('current_actor_pose_boundary_count')
    clock_tokens={}
    for position in clocks:
        found=[entry for entry in locations if entry[1]==position]
        if len(found)!=1 or found[0][3].kind!='bits'or found[0][2]!=12:
            raise ValueError('current_actor_pose_timestamp_shape')
        clock_tokens[position]=found[0][3].value
    offsets=[]
    for label in('0x21dc8cc','0x21de7c4'):
        positions=[p for name,p in observed._boundaries if name==label]
        if len(positions)!=1:raise ValueError('current_actor_revision_boundary')
        entries=[entry for entry in locations if entry[0]==positions[0]]
        if len(entries)!=1 or entries[0][3].kind!='raw'or entries[0][3].extent!=1:
            raise ValueError('current_actor_revision_raw_u8')
        offsets.append(entries[0][1]//8)
    if len(set(offsets))!=2:raise ValueError('current_actor_revision_overlap')
    updated=bytearray(body);allowed=bytearray(len(body))
    for start,code in clock_tokens.items():
        phase=decode_float64(code,0,3,0,12,profile=NEAREST_EVEN_GRADUAL)
        shifted=math.fmod(_op(phase,float(elapsed_seconds),'add',53),3.0)
        if not math.isfinite(shifted)or not 0<=shifted<3:raise ValueError('current_actor_clock_phase')
        new=quantize_float64(shifted,0,3,0,12,profile=NEAREST_EVEN_GRADUAL).code
        for bit in range(12):
            index,lane=divmod(start+bit,8);mask=1<<lane;allowed[index]|=mask
            updated[index]=(updated[index]&~mask)|(((new>>bit)&1)<<lane)
    for offset in offsets:
        if allowed[offset]:raise ValueError('current_actor_clock_revision_overlap')
        allowed[offset]=255;updated[offset]=(body[offset]+tick_steps)&255
    result=bytes(updated)
    if len(result)!=len(body)or any((a^b)&~mask for a,b,mask in zip(body,result,allowed)):
        raise ValueError('current_actor_unapproved_bit_change')
    checked=_checked(result,map_id,verified_native_sha256)
    if(checked.bit_length!=observed.bit_length or checked._boundaries!=observed._boundaries or
       checked._counts!=observed._counts):raise ValueError('current_actor_layout_changed')
    return CurrentActorUpdate(result,expected,counts.get('timed_raw9_records',0))
