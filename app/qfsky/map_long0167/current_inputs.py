"""Current Scene+own-Actor consistent-read adapter; no device/network backend.

Caller injects an already bounded memory reader and genuine lifecycle context.
No account is discovered; no process is enumerated, opened, modified or started.
This reads exact current serialized Actor bytes, never a Home remap. Two equal
reads are a consistency check, NOT a stop-the-world/atomicity guarantee.
"""
from dataclasses import dataclass,field
import hmac
import re
import struct
from qfsky.join0167_profile import account_binding,NATIVE_SHA256
from qfsky.long0167.level_authority import LevelRecord
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire,reencode_actor_wire
from .scene_only_draft import check_scene_record

ACTOR_NATIVE_MAX=0x528  # 1f9cbb0 cmp length,#0x529; b.hs rejects.

@dataclass(frozen=True,repr=False)
class CurrentContext:
    account_binding_hash:str=field(repr=False)
    net_module_address:int=field(repr=False)
    world_generation:object=field(repr=False)
    scene:str
    resource_revision:str
    level_id:int
    native_sha256:str=NATIVE_SHA256
    apk_build:int=421589

@dataclass(frozen=True,repr=False)
class CurrentInputs:
    _context:CurrentContext=field(repr=False)
    _cache:bytes=field(repr=False)
    _record:LevelRecord=field(repr=False)
    _actor:bytes=field(repr=False)
    _netversion:tuple[int,...]=field(repr=False)
    _actor_counts:tuple=field(repr=False)
    def cache_bytes(self):return self._cache
    def scene_bytes(self):return self._record.data
    def actor_bytes(self):return self._actor
    def metadata(self):
        return {'scene_current_schema_checked':True,'actor_current_schema_checked':True,
            'scene_actor_same_account_map_revision':True,'paired_equal_reads':True,
            'actor_map_prefix_matches_native_level_id':True,'actual_current_wire_preserved':True,
            'scene_bytes':len(self._record.data),'actor_bytes':len(self._actor),
            'actor_timed_record_count':dict(self._actor_counts).get('timed_raw9_records',0),
            'native_actor_presence_flags_verified':False,'atomic_snapshot_proven':False,
            'capture_backend_origin_attested_by_helper':False,'full_connection_ready':False,
            'network_used_by_helper':False,'memory_write':False,'device_started':False}

def capture_current_inputs(read_memory,context_provider,*,expected_account_binding,
                           expected_scene,expected_resource_revision,expected_netversion):
    """One attempt; reject torn data/map changes. Caller controls any retry.

    Static chain (421589): Game+7ec0=N; N+30=Level; Level+9a10=current
    levelId; N+99b78=NV; N+40=cache_object; cache_object+c is 0x2010 bytes.
    Own Actor writer=N+48, raw_len=N+54, raw_body=N+58, tick=N+2480.
    Caller verifies the actual mapped native build and pointer chain first.
    """
    if type(expected_account_binding)is not str or not re.fullmatch('[0-9a-f]{64}',expected_account_binding):
        raise ValueError('current_input_expected_account_binding')
    expected_hash=expected_account_binding
    if not callable(read_memory)or not callable(context_provider):raise ValueError('current_input_callbacks')
    if(type(expected_netversion)not in(tuple,list)or len(expected_netversion)!=6 or
       any(type(x)is not int or not 0<=x<=65535 for x in expected_netversion)):
        raise ValueError('current_input_expected_nv')
    def context():
        c=context_provider()
        if type(c)is not CurrentContext or c.native_sha256!=NATIVE_SHA256 or c.apk_build!=421589:
            raise ValueError('current_input_version_scope')
        if(c.scene!=expected_scene or c.resource_revision!=expected_resource_revision or
           type(c.account_binding_hash)is not str or
           not hmac.compare_digest(c.account_binding_hash,expected_hash)):
            raise ValueError('current_input_account_or_scene_scope')
        if(type(c.net_module_address)is not int or not 0<c.net_module_address<2**64-0x99b84 or
           c.net_module_address%8 or c.world_generation is None or
           type(c.level_id)is not int or not 0<c.level_id<=0xffffffff):
            raise ValueError('current_input_pointer_scope')
        return c
    def read(address,size):
        if type(address)is not int or not 0<address<2**64-size or not 0<size<=0x2010:
            raise ValueError('current_input_read_bound')
        value=read_memory(address,size)
        if type(value)is not bytes or len(value)!=size:raise ValueError('current_input_short_read')
        return value
    def u32(address):return struct.unpack('<I',read(address,4))[0]
    def ptr(address):return struct.unpack('<Q',read(address,8))[0]
    before=context();n=before.net_module_address
    level=ptr(n+0x30);cache_object=ptr(n+0x40)
    level_id=u32(level+0x9a10);nv=tuple(struct.unpack('<6H',read(n+0x99b78,12)))
    if level_id!=before.level_id or nv!=tuple(expected_netversion):raise ValueError('current_input_native_map_or_nv')
    tick=u32(n+0x2480);length=u32(n+0x54)
    if not 0<length<=ACTOR_NATIVE_MAX:raise ValueError('current_input_actor_native_length')
    cache_a=read(cache_object+0xc,0x2010);actor_a=read(n+0x58,length)
    actor_b=read(n+0x58,length);cache_b=read(cache_object+0xc,0x2010)
    length_b=u32(n+0x54);tick_b=u32(n+0x2480)
    if(cache_a!=cache_b or actor_a!=actor_b or length!=length_b or tick!=tick_b or
       ptr(n+0x30)!=level or ptr(n+0x40)!=cache_object or u32(level+0x9a10)!=level_id or
       tuple(struct.unpack('<6H',read(n+0x99b78,12)))!=nv):
        raise ValueError('current_input_torn_or_changed')
    after=context()
    if(after.world_generation is not before.world_generation or
       after.net_module_address!=n or after.level_id!=level_id):raise ValueError('current_input_generation_changed')
    size=struct.unpack_from('<H',cache_a,0x200e)[0]
    if not 4<=size<=0x2000:raise ValueError('current_input_scene_length')
    record=LevelRecord(struct.unpack_from('<H',cache_a)[0],cache_a[2],
        struct.unpack_from('<I',cache_a,4)[0],cache_a[8],
        struct.unpack_from('<H',cache_a,10)[0],struct.unpack_from('<H',cache_a,12)[0],cache_a[14:14+size])
    check_scene_record(record,resource_tag=nv[0],native_record_size=nv[5],account_hash=expected_hash)
    observed=inspect_actor_wire(actor_a,verified_profile_sha256=NATIVE_SHA256,
                                check_zero_padding=True,require_canonical_writer=True)
    if(not observed.matches_prefix_u32(level_id)or
       reencode_actor_wire(observed,verified_profile_sha256=NATIVE_SHA256)!=actor_a):
        raise ValueError('current_input_actor_map_or_roundtrip')
    return CurrentInputs(before,cache_a,record,actor_a,nv,observed._counts)
