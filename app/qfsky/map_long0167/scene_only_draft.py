"""Network-free multi-map Scene experiment, not a production long Engine.

Input is a validated current account/map cache, never a renamed Home seed.
No Actor fabrication exists here. full_connection_ready is always False.
The parent must add a proven current-map Actor source, current resource
provenance and regression coverage before any integration into production.
"""
from dataclasses import dataclass,field
import hmac
import math
import struct

from qfsky.join0167_profile import validate_record,account_binding,NATIVE_SHA256
from qfsky.long0167.codec.scene_wire import inspect_scene_wire,reencode_scene_wire
from qfsky.long0167.level_authority import LevelRecord,LevelRoleReceiver
from qfsky.long0167.room_join import parse_player_sync
from qfsky.long0167.scene_header import serialize_cached_level_header
from qfsky.long0167.delta_send import SnapshotSendState

_SEAL=object()

def check_scene_record(record, *, resource_tag, native_record_size, account_hash):
    if type(record)is not LevelRecord:raise ValueError('map_scene_record_type')
    for value,bits in ((record.slot_0,16),(record.slot_2,8),(record.slot_4,32),
                       (record.slot_8,8),(record.slot_a,16),(record.slot_c,16)):
        if type(value)is not int or not 0<=value<1<<bits:raise ValueError('map_scene_record_slot')
    if record.slot_0!=native_record_size:raise ValueError('map_scene_record_size')
    observed=inspect_scene_wire(record.data,verified_profile_sha256=NATIVE_SHA256,check_zero_padding=True)
    if not observed.matches_resource_tag(resource_tag)or reencode_scene_wire(observed)!=record.data:
        raise ValueError('map_scene_resource_or_roundtrip')
    owner=observed._tokens[observed._home_owner_end_token_index-1]
    if owner.kind!='raw'or owner.extent!=16:raise ValueError('map_scene_owner_shape')
    # A nonzero foreign home-owner field needs a separately established
    # visitor/house policy. Do not erase/rebind it or assume every map uses0.
    if any(owner.value)and not hmac.compare_digest(account_binding(owner.value),account_hash):
        raise ValueError('map_scene_nonown_home_owner_requires_policy')
    return observed

@dataclass(frozen=True,repr=False)
class SceneBaseline:
    scene:str
    resource_revision:str
    map_id:int
    resource_tag:int
    native_record_size:int
    _account_hash:str=field(repr=False)
    _record:LevelRecord=field(repr=False)
    _seal:object=field(repr=False)

def prepare_scene_baseline(record,cache,*,account_uuid,scene,resource_revision):
    """Strict validated profile -> exact Scene record, without per-map SHA.

    Caller supplies the current authenticated account UUID; neither cache
    account IDs nor stale template UID bytes may substitute for that input.
    Native capture/log provenance is checked by the existing profile loader.
    This validates a cached snapshot, not that the snapshot is live/fresh now.
    """
    profile=validate_record(record,cache,scene=scene,resource_revision=resource_revision,
                            account_uuid=account_uuid,apk_build=421589)
    full=LevelRecord(struct.unpack_from('<H',cache,0)[0],cache[2],
        struct.unpack_from('<I',cache,4)[0],cache[8],
        struct.unpack_from('<H',cache,10)[0],struct.unpack_from('<H',cache,12)[0],profile.blob)
    tag=record['netversion_u16'][0];native_size=record['netversion_u16'][5]
    check_scene_record(full,resource_tag=tag,native_record_size=native_size,account_hash=profile.binding)
    return SceneBaseline(scene,resource_revision,record['level_id'],tag,native_size,profile.binding,full,_SEAL)

@dataclass(frozen=True,repr=False)
class DraftFrame:
    payload:bytes=field(repr=False) # Full nested body, always outer14/channel0.
    reliable:bool=False

@dataclass(frozen=True,repr=False,eq=False)
class DraftPlan:
    frames:tuple[DraftFrame,...]=field(repr=False)

class SceneOnlyDraft:
    """Role-driven offline Scene cycle; no Actor, transport or readiness claim."""
    def __init__(self,baseline,*,account_uuid,enter_game_body,generation,live_check):
        if type(baseline)is not SceneBaseline or baseline._seal is not _SEAL:
            raise ValueError('map_baseline_unvalidated')
        if not hmac.compare_digest(baseline._account_hash,account_binding(account_uuid)):
            raise ValueError('map_scene_account_changed')
        if type(generation)is not int or not 1<=generation<=255:
            raise ValueError('map_scene_generation')
        match=parse_player_sync(enter_game_body,own_uuid=account_uuid,expected_level_id=baseline.map_id)
        if not match.ready or not match.self_net_player_id:raise ValueError('map_scene_membership')
        self._baseline=baseline;self._record=baseline._record
        self._match=match;self._epoch=generation;self._room=object();self._live=live_check
        self._receiver=LevelRoleReceiver(match,room_generation=self._room,peer_epoch=generation)
        self._writer=SnapshotSendState(10)
        self._pending=None;self._pending_data=None;self._closed=False
        self._armed=False;self._ready=False;self._owner=0;self._version=0
        self._actual=False;self._usable=True;self._force=False
        self._last_sent=None;self._last_now=0.0
        self._groups=self._role_events=self._adoptions=self._rejections=self._acks=0
        self._check()

    def _check(self):
        if self._closed:raise ValueError('map_scene_closed')
        if not callable(self._live)or self._live()is not True:
            self.close();raise ValueError('map_scene_scope_inactive')
        if self._pending is not None:
            self.close();raise ValueError('map_scene_pending_delivery')

    def observe_role(self,payload):
        """Only decrypted/authenticated current-peer role or ACK input."""
        self._check()
        if type(payload)is not bytes or len(payload)<3:raise ValueError('map_scene_game_header')
        if payload[1]!=self._epoch:return False
        if payload[2]and payload[2]not in self._match.level_roster_ids:return False
        if payload[0]==14:
            if len(payload)!=5:return False
            matched=self._writer.acknowledge(payload[4]);self._acks+=int(matched);return matched
        if payload[0]not in(8,9,11):return False
        observation=self._receiver.observe(payload,room_generation=self._room,peer_epoch=self._epoch)
        if observation is None:return False
        self._role_events+=1;self._ready=True;self._version=observation.version;self._armed=False
        self._owner=0 if observation.kind=='LevelData_Revoke'else observation.owner_network_id
        if observation.kind=='LevelData_Revoke':
            if observation.owner_network_id==self._match.self_net_player_id and self._actual and self._usable:
                self._force=True
            return True
        if observation.kind=='LevelData_Snapshot'or observation.include_record:
            try:
                check_scene_record(observation.record,resource_tag=self._baseline.resource_tag,
                    native_record_size=self._baseline.native_record_size,account_hash=self._baseline._account_hash)
                if observation.kind=='LevelData_Elect'and(not self._actual or observation.record.slot_4!=self._record.slot_4):
                    raise ValueError('map_scene_included_elect_without_actual_cache')
            except ValueError:
                self._usable=False;self._rejections+=1;return True
            self._record=observation.record;self._actual=True;self._usable=True;self._adoptions+=1
        self._armed=self._usable and self._owner==self._match.self_net_player_id
        return True

    def plan(self,now):
        self._check()
        if type(now)not in(int,float)or not math.isfinite(now)or now<self._last_now:
            self.close();raise ValueError('map_scene_clock')
        self._last_now=float(now)
        if self._last_sent is not None and now-self._last_sent<.113:return None
        active=self._usable and(self._armed or(self._force and self._actual))
        staged=None;forced=False
        if active:
            forced=self._force and self._actual
            header=serialize_cached_level_header(self._record,own_map=self._baseline.map_id,received_version=self._version)
            staged=SnapshotSendState(10)if forced else self._writer.fork()
            staged.update(header);frame=staged.build_frame()
            if forced:self._force=False
            if frame is None:
                if not forced:self._writer=staged
                self._last_sent=now;return None
            aux=bytes((23,self._epoch,0))+struct.pack('<III',self._baseline.map_id,0,0)
            body=bytes((10 if forced else 11,self._epoch,0))+frame
            frames=(DraftFrame(aux,forced),DraftFrame(body,forced))
        else:
            owner=self._owner if self._owner in self._match.level_roster_ids else 0
            frames=(DraftFrame(bytes((12,self._epoch,0,int(self._ready),owner))+
                    struct.pack('<IH',self._baseline.map_id,self._version)),)
        group=DraftPlan(frames);self._pending=group;self._pending_data=(now,staged,forced)
        return group

    def commit(self,plan,delivered_count):
        if(self._closed or plan is not self._pending or plan is None or
           type(delivered_count)is not int or delivered_count!=len(plan.frames)or self._live()is not True):
            self.close();raise ValueError('map_scene_delivery_unknown')
        now,writer,forced=self._pending_data
        if writer is not None and not forced:self._writer=writer
        self._last_sent=now;self._groups+=1;self._pending=self._pending_data=None

    def close(self):
        self._closed=True;self._armed=False;self._pending=self._pending_data=None

    def metadata(self):
        return {'full_connection_ready':False,'actor_source_present':False,'network_used':False,
            'closed':self._closed,'actual_record_received':self._actual,
            'initial_record_from_current_account_cache':not self._actual,
            'native_scene_producer_reproduced':False,'snapshot_freshness_proven':False,
            'own_role_armed':self._armed,'groups_committed':self._groups,'role_events':self._role_events,
            'actual_record_adoptions':self._adoptions,'cache_rejections':self._rejections,'scene_acks':self._acks}
