"""Current-input, single-map long-connection candidate; no I/O or live claim.

Supply authenticated current-peer bodies and a real account/transport lifetime
callback. The callback is not a fake WSS state. Frames are BODY-only and must
be queued as an ordered, uninterrupted group before commit. No Home fallback,
cross-map replay, resource inference, account discovery or native fill exists.
"""
from ._cw_bounds import LIFE as _CW_LIFE, STEPS as _CW_STEPS  # claudework
import hmac
import hashlib
import math
import struct
from qfsky.join0167_profile import account_binding,NATIVE_SHA256
from qfsky.long0167.engine import Frame,FrameGroup
from qfsky.long0167.room_join import parse_player_sync
from qfsky.long0167.delta_sync import SnapshotReceiveState,accept_player_state_delta,_fnv8
from qfsky.long0167.delta_send import SnapshotSendState
from qfsky.long0167.level_authority import LevelRecord
from .current_inputs import CurrentInputs,CurrentContext,ACTOR_NATIVE_MAX
from .current_actor_update import _checked,advance_current_actor
import os
POSE_OVERRIDE={}  # claudework：{'seed': bytes}，由运行器在同进程内设置
OUTBOX=[]  # claudework：待发的额外上行 Frame（如好友邀请接受），tick 时并入帧组
from .scene_only_draft import SceneBaseline,SceneOnlyDraft,check_scene_record,_SEAL
from .continuity import export_idle,restore_idle


# Explicit diagnostic only: never returns bytes to a producer or decoder.
_SCENE_TRACE_V31T = None


class _SceneTraceV31T:
    # Final plus atomic temporary file remain below one MiB combined.
    MAX_FILE_BYTES = 480000
    SESSION_BYTES = 112000

    def __init__(self):
        import collections, threading, time
        from pathlib import Path
        self.lock = threading.RLock()
        self.active = collections.OrderedDict()
        self.frozen = collections.deque(maxlen=2)
        self.serial = 0
        self.events = 0
        self.evicted = 0
        self.path = Path(os.environ['QFLY_DATA_DIR']) / (
            'scene-trace-v31t-%d-%d.private.json' % (os.getpid(), time.time_ns()))

    def _session(self, engine):
        session = getattr(engine, '_scene_trace_v31t_session', None)
        if session is not None:
            return session
        self.serial += 1
        session = dict(engine_sequence=self.serial, map_id=engine._map,
                       epoch=engine._epoch, closed=False, rows=[], bytes=0,
                       dropped=0, significant=False)
        engine._scene_trace_v31t_session = session
        self.active[self.serial] = session
        while len(self.active) > 2:
            self.active.popitem(last=False)
            self.evicted += 1
        return session

    def append(self, engine, payload, *, committed, reliable=None):
        import json, time
        if type(payload) is not bytes or len(payload) < 3:
            return
        kind = payload[0]
        if kind not in ((10, 11, 23) if committed else (8, 9, 11, 14)):
            return
        with self.lock:
            session = self._session(engine)
            if session['closed']:
                return
            self.events += 1
            row = dict(order=self.events, unix=time.time(), mono=time.monotonic(),
                       map_id=engine._map, epoch=engine._epoch, wire_epoch=payload[1],
                       kind=kind, length=len(payload), committed=bool(committed),
                       direction='sent' if committed else 'received',
                       validation='commit_succeeded' if committed else 'before_semantic_validation')
            if reliable is not None:
                row['reliable'] = bool(reliable)
            # Elect(8) is retained raw without assigning ambiguous offsets.
            if kind in (10, 11) and len(payload) >= 6:
                row.update(sequence=payload[3], base=payload[4], checksum=payload[5])
                row['header_interpretation'] = 'three_byte_game_prefix_then_snapshot_header'
            if kind == 14 and len(payload) == 5:
                row.update(player_ack=payload[3], level_ack=payload[4])
            # Full normal Scene frames fit. Oversize data is explicitly not a
            # reconstructable capture and cannot silently evade the hard cap.
            row['raw_complete'] = len(payload) <= 32768
            row['payload_hex'] = payload[:32768].hex()
            raw = json.dumps(row, separators=(',', ':'), allow_nan=False).encode()
            session['rows'].append(row)
            session['bytes'] += len(raw) + 1
            while session['rows'] and session['bytes'] > self.SESSION_BYTES:
                old = session['rows'].pop(0)
                session['bytes'] -= len(json.dumps(old, separators=(',', ':'), allow_nan=False).encode()) + 1
                session['dropped'] += 1
            if kind in (8, 9, 10, 11, 23):
                session['significant'] = True
            if (committed and kind == 10) or (not committed and kind in (8, 9)):
                self.flush()

    def close(self, engine):
        with self.lock:
            session = getattr(engine, '_scene_trace_v31t_session', None)
            if session is None or session['closed']:
                return
            session['closed'] = True
            self.active.pop(session['engine_sequence'], None)
            # ACK-only idle traffic cannot evict the failed Scene trace.
            if session['significant']:
                self.frozen.append(session)
            self.flush()

    def flush(self):
        import json
        document = dict(schema='private-scene-trace-v31t',
                        classification='private-runtime-protocol',
                        events_seen=self.events, active_sessions_evicted=self.evicted,
                        frozen_sessions=list(self.frozen), active_sessions=list(self.active.values()))
        raw = json.dumps(document, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(raw) > self.MAX_FILE_BYTES:
            raise ValueError('scene_trace_diagnostic_limit')
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(temporary, self.path)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
            raise


def _trace_scene_v31t(engine, payload=None, *, committed=False, reliable=None, closing=False):
    # All telemetry including collecting/serializing fields is isolated. A
    # diagnostic failure never turns an already successful network commit bad.
    try:
        if os.environ.get('SKY_ESCORT_SCENE_TRACE_V31T') != '1':
            return
        global _SCENE_TRACE_V31T
        if _SCENE_TRACE_V31T is None:
            if closing:
                return
            _SCENE_TRACE_V31T = _SceneTraceV31T()
        if closing:
            _SCENE_TRACE_V31T.close(engine)
        else:
            _SCENE_TRACE_V31T.append(engine, payload, committed=committed, reliable=reliable)
    except Exception:
        pass


def validate_inputs(inputs,*,account_uuid,scene,resource_revision,level_id,netversion):
    """Recheck actual current bytes, all binding fields and exact cache extent.

    This validates supplied capture output; it does not attest the external
    memory-reader backend or prove the saved snapshot is still live. Caller
    provenance/age policy remains mandatory before creating a transport.
    """
    from .asset_bootstrap_v32 import AssetBootstrapInputs, validate_asset_bootstrap_inputs
    if type(inputs) is AssetBootstrapInputs:
        return validate_asset_bootstrap_inputs(inputs,account_uuid=account_uuid,scene=scene,
            resource_revision=resource_revision,level_id=level_id,netversion=netversion)
    from .asset_catalog_v32c import CatalogBootstrapInputs, validate_catalog_inputs
    if type(inputs) is CatalogBootstrapInputs:
        return validate_catalog_inputs(inputs,account_uuid=account_uuid,scene=scene,
            resource_revision=resource_revision,level_id=level_id,netversion=netversion)
    if type(inputs)is not CurrentInputs or type(inputs._context)is not CurrentContext:
        raise ValueError('map_long_current_inputs_required')
    c=inputs._context
    if c.apk_build!=421629 or c.native_sha256!=NATIVE_SHA256:
        raise ValueError('map_long_current_build_required')
    if(type(scene)is not str or not scene or type(resource_revision)is not str or not resource_revision or
       type(level_id)is not int or not 0<level_id<=0xffffffff):
        raise ValueError('map_long_expected_map_scope')
    if(type(netversion)not in(tuple,list)or len(netversion)!=6 or
       any(type(x)is not int or not 0<=x<=65535 for x in netversion)):
        raise ValueError('map_long_expected_netversion')
    if(c.scene!=scene or c.resource_revision!=resource_revision or c.level_id!=level_id or
       type(c.account_binding_hash)is not str or
       not hmac.compare_digest(c.account_binding_hash,account_binding(account_uuid))or
       c.world_generation is None or tuple(inputs._netversion)!=tuple(netversion)):
        raise ValueError('map_long_capture_binding_changed')
    cache=inputs.cache_bytes()
    if type(cache)is not bytes or len(cache)!=0x2010:raise ValueError('map_long_cache_extent')
    size=struct.unpack_from('<H',cache,0x200e)[0]
    if not 4<=size<=0x2000:raise ValueError('map_long_scene_extent')
    record=LevelRecord(struct.unpack_from('<H',cache)[0],cache[2],
        struct.unpack_from('<I',cache,4)[0],cache[8],
        struct.unpack_from('<H',cache,10)[0],struct.unpack_from('<H',cache,12)[0],cache[14:14+size])
    if inputs._record!=record:raise ValueError('map_long_cache_record_changed')
    check_scene_record(record,resource_tag=netversion[0],native_record_size=netversion[5],
                       account_hash=c.account_binding_hash)
    observed=_checked(inputs.actor_bytes(),level_id,NATIVE_SHA256)
    if observed._counts!=inputs._actor_counts:raise ValueError('map_long_actor_counts_changed')
    # The current codec retains timed event records, but this idle-only engine
    # has no producer/expiry policy for replaying them over a new connection.
    if dict(observed._counts).get('timed_raw9_records',0):
        raise ValueError('map_long_timed_actor_requires_event_policy')
    return SceneBaseline(scene,resource_revision,level_id,netversion[0],netversion[5],
                         c.account_binding_hash,record,_SEAL)


class MapLongEngine:
    """One current account/map/resource/peer lifetime, with fresh delta state.

    Initial actual Actor at +91ms, Scene/controller, then first prepared-only
    extension in that order; subsequent extensions have a 120ms minimum gap.
    Scene/controller scheduling is 113ms. No missed-tick catch-up burst.
    Any Move/disconnect requires an external new authenticated lifecycle.
    """
    def __init__(self,*,account_uuid,enter_game_body,generation,now,inputs,
                 scene,resource_revision,level_id,netversion,live_check,local_idle=None,local_registration_binding=None):
        baseline=validate_inputs(inputs,account_uuid=account_uuid,scene=scene,
            resource_revision=resource_revision,level_id=level_id,netversion=netversion)
        if type(generation)is not int or not 1<=generation<=255:raise ValueError('map_long_generation')
        self._closed=False;self._pending=self._pending_data=None;self._live=live_check
        self._owner=account_uuid;self._map=level_id;self._epoch=generation
        self._roster_body=bytes(enter_game_body)
        self._local_registration_binding=local_registration_binding
        self._membership_provenance='authenticated_player_sync'
        if local_registration_binding is None:
            self._match=parse_player_sync(enter_game_body,own_uuid=account_uuid,expected_level_id=level_id)
        else:
            from qfsky.same_peer_level_update_v31 import LocalRegistrationBinding
            if type(local_registration_binding)is not LocalRegistrationBinding:raise ValueError('local_registration_type')
            self._match=local_registration_binding.validate(account_uuid=account_uuid,map_id=level_id,
                epoch=generation,roster_body=enter_game_body)
            self._membership_provenance='self_announced_local_registration_acked'
        if not self._match.ready or self._match.self_net_player_id not in self._match.level_roster_ids:
            raise ValueError('map_long_own_membership')
        self._scene=SceneOnlyDraft(baseline,account_uuid=account_uuid,enter_game_body=enter_game_body,
                                  generation=generation,live_check=live_check,local_registration_binding=local_registration_binding)
        if local_registration_binding is None:
            self._player=SnapshotReceiveState()
        else:
            # The sealed binding above proves the SAME authenticated transport,
            # owner and roster slot. Its next PlayerState delta can still name
            # a baseline already ACKed before our local level announcement.
            # Preserve this proven compression history, never room evidence,
            # Scene authority or another connection's buffers.
            previous_player=local_registration_binding.source._player
            if type(previous_player)is not SnapshotReceiveState:
                raise ValueError('same_peer_player_receive_type')
            self._player=previous_player.fork()
        self._actor_seed=inputs.actor_bytes()
        self._actor_writer=SnapshotSendState(10,require_ack=True)
        if local_registration_binding is not None:
            # Current native build stores all four normal codecs inline in
            # NetModule. Same-peer LevelUpdate does not reset them; outer5
            # and NetModule initialization do. The destination Scene receiver
            # above is FRESH; only its immutable compression buffers are forked.
            source=local_registration_binding.source
            previous_scene=source._scene
            if (source.has_pending or previous_scene._pending is not None
                    or previous_scene._closed):
                raise ValueError('same_peer_codec_delivery_pending')
            if (type(source._actor_writer)is not SnapshotSendState
                    or type(previous_scene._writer)is not SnapshotSendState
                    or type(previous_scene._receiver.level_snapshot_state)is not SnapshotReceiveState):
                raise ValueError('same_peer_codec_context_type')
            self._actor_writer=source._actor_writer.fork()
            self._scene._writer=previous_scene._writer.fork()
            self._scene._receiver._level_receive_state=previous_scene._receiver.level_snapshot_state.fork()
            # No old owner, role, record, version, epoch or map is adopted.
            # Forced Game10 still constructs its own temporary fresh writer.
        self._registered_at=self._last_now=self._time(now)
        self._actor_origin=self._actor_started_at=self._last_extension=None;self._actor_sequence=0
        self._revision_base=0;self._local_idle_restored=local_idle is not None
        self._idle_binding=(account_binding(account_uuid),level_id,scene,resource_revision,
            (421629,NATIVE_SHA256),tuple(netversion),hashlib.sha256(inputs.cache_bytes()).hexdigest(),
            hashlib.sha256(inputs.actor_bytes()).hexdigest())
        if local_idle is not None:
            self._actor_origin,self._revision_base=restore_idle(local_idle,
                binding=self._idle_binding,now=self._registered_at)
        self._counts={name:0 for name in ('groups_committed','frames_committed','actor_frames',
            'actor_extensions','controller_frames','scene_pairs','delta_acks','scene_confirmations',
            'nettime_replies','roster_updates','invalid_role_events','stale_generation_drops',
            'unknown_sender_drops','ignored_messages','actor_snapshot_acks','actor_snapshot_ack_misses')}
        self._scope()
        if local_registration_binding is not None:local_registration_binding.consumed=True

    def __repr__(self):return '<MapLongEngine current-input RAM-only candidate>'
    @property
    def has_pending(self):return self._pending is not None
    @staticmethod
    def _time(now):
        if type(now)not in(int,float)or not math.isfinite(now)or now<0:raise ValueError('map_long_clock')
        return float(now)
    def _scope(self):
        if self._closed:raise ValueError('map_long_closed')
        if not callable(self._live)or self._live()is not True:raise ValueError('map_long_scope_inactive')
    def _check(self,now):
        self._scope()
        if self._pending is not None:raise ValueError('map_long_delivery_pending')
        now=self._time(now)
        if now<self._last_now:raise ValueError('map_long_clock_regressed')
        if now-self._registered_at>_CW_LIFE or(self._actor_origin is not None and now-self._actor_origin>_CW_LIFE):
            raise ValueError('map_long_lifetime_limit')
        self._last_now=now;return now
    def _group(self,frames,**staged):
        if not frames:return None
        self._scope();group=FrameGroup(tuple(frames));self._pending=group;self._pending_data=staged
        return group
    def _actor_frame(self,body,writer):
        writer.update(body)
        snapshot=writer.build_frame()
        if snapshot is None:
            # Native raw recovery saves only periodically. Keep sending the
            # current pose as an unsaved raw frame between saves, never stall
            # motion just because ACKs were lost. No baseline is inferred.
            snapshot=bytes((0,0,_fnv8(body)))+body
        return Frame(14,bytes((3,self._epoch,0))+snapshot)
    def _ack(self):
        return Frame(14,bytes((14,self._epoch,0,self._player.acknowledgment,
            self._scene._receiver.level_snapshot_state.acknowledgment)))

    def export_local_idle(self):
        """Export after final full commit; carries no peer, ACK or authority.

        The next same-map instance still waits +91ms after its own registration
        ACK, creates a fresh unacknowledged writer, and then increments the
        retained revision count. No sent baseline is restored across rooms.
        Changed account/map/resource/native/cache/Actor inputs reject reuse.
        """
        self._check(self._last_now)
        return export_idle(self._idle_binding,self._actor_origin,
            self._revision_base+self._counts['actor_extensions'],self._last_now)

    def _cw_seed(self):
        # claudework：长模式下允许运行时换姿态种子（如 KeepCandle），否则用原始种子
        if os.environ.get('CLAUDEWORK_LONG')=='1':
            seed=POSE_OVERRIDE.get('seed')
            if seed:return seed
        return self._actor_seed

    def _cw_post(self,body):
        # claudework：长模式下推进后的后处理（如把一次性动画的起点钉住）；出错则原样返回
        if os.environ.get('CLAUDEWORK_LONG')=='1':
            post=POSE_OVERRIDE.get('post')
            if post:
                try:return post(body)
                except Exception:return body
        return body

    def tick(self,now):
        try:
            now=self._check(now);frames=[];staged={};writer=self._actor_writer.fork()
            first=self._actor_started_at is None and now-self._registered_at>=.091
            if first:
                initial=self._cw_seed()
                if self._actor_origin is not None:
                    initial=advance_current_actor(self._cw_seed(),elapsed_seconds=now-self._actor_origin,
                        tick_steps=self._revision_base,map_id=self._map).wire_bytes()
                initial=self._cw_post(initial)
                frames.append(self._actor_frame(initial,writer));staged['started']=now
            scene_plan=self._scene.plan(now)
            if scene_plan is not None:
                frames.extend(Frame(14,f.payload,f.reliable)for f in scene_plan.frames)
                staged['scene_plan']=scene_plan
            origin=self._actor_origin if self._actor_origin is not None else(now if first else None)
            if (first or self._actor_started_at is not None)and origin is not None and(self._last_extension is None or now-self._last_extension>=.120):
                body=advance_current_actor(self._cw_seed(),elapsed_seconds=now-origin,
                    tick_steps=self._revision_base+self._counts['actor_extensions']+1,map_id=self._map).wire_bytes()
                body=self._cw_post(body)
                frames.append(self._actor_frame(body,writer));staged['extension']=now
            if os.environ.get('CLAUDEWORK_LONG')=='1' and OUTBOX:
                while OUTBOX:frames.append(OUTBOX.pop(0))
            if 'started'in staged or 'extension'in staged:staged['actor_writer']=writer
            return self._group(frames,**staged)
        except BaseException:
            self.close();raise

    def handle(self,outer_type,payload,now):
        try:
            self._check(now)
            if type(outer_type)is not int or not 0<=outer_type<=255:raise ValueError('map_long_outer_type')
            if type(payload)is not bytes or len(payload)>0x10000:raise ValueError('map_long_payload')
            if outer_type==13:
                if len(payload)!=18:raise ValueError('map_long_nettime')
                return self._group([Frame(12,payload[:2])],nettime_replies=1)
            if outer_type==8:
                # Native NetMsg8, not nested Game8/LevelData_Elect.
                if len(payload)!=5:raise ValueError('map_long_level_update_size')
                slot,level=struct.unpack('<BI',payload)
                if slot not in {s for s,_ in self._match.roster}:
                    self._counts['unknown_sender_drops']+=1;return None
                if slot==self._match.self_net_player_id and level!=self._map:
                    raise ValueError('map_long_own_level_conflict')
                from qfsky.same_peer_level_update_v31 import update_level
                self._match=update_level(self._match,slot,level)
                self._scene._match=self._match;self._scene._receiver._roster=self._match.level_roster_ids
                self._counts['roster_updates']+=1;return None
            if outer_type==17:
                match=parse_player_sync(payload,own_uuid=self._owner,expected_level_id=self._map)
                binding=self._local_registration_binding
                if (binding is not None and match.own_match and not match.level_match
                        and match.self_net_player_id==self._match.self_net_player_id
                        and match.self_level_id==binding.source_map
                        and now-binding.acknowledged_at<=2.0):
                    # Old queued sync is retained as an observation, not applied
                    # over our newly acknowledged own announcement.
                    self._counts['stale_generation_drops']+=1;return None
                if(not match.ready or match.self_net_player_id!=self._match.self_net_player_id or
                   any(not 1<=n<=255 for n in match.level_roster_ids)):
                    raise ValueError('map_long_membership_changed')
                self._roster_body=bytes(payload)
                self._match=match;self._scene._match=match;self._scene._receiver._roster=match.level_roster_ids
                self._counts['roster_updates']+=1;return None
            if outer_type in(1,2,9,0x53):raise ValueError('map_long_lifecycle_transition')
            if outer_type!=14:self._counts['ignored_messages']+=1;return None
            if len(payload)<3:raise ValueError('map_long_game_header')
            kind,generation,sender=payload[:3]
            _trace_scene_v31t(self,payload)
            if generation!=self._epoch:self._counts['stale_generation_drops']+=1;return None
            if sender and sender not in self._match.level_roster_ids:
                self._counts['unknown_sender_drops']+=1;return None
            if kind==17:
                if len(payload)!=9:raise ValueError('map_long_scene_confirmation')
                if struct.unpack_from('<I',payload,3)[0]!=self._map:return None
                return self._group([Frame(14,bytes((17,generation,0))+payload[3:],True)],scene_confirmations=1)
            if kind==3:
                accept_player_state_delta(payload,receive_state=self._player)
                return self._group([self._ack()],delta_acks=1)
            if kind==14:
                if len(payload)!=5:raise ValueError('map_long_snapshot_ack')
                matched=self._actor_writer.acknowledge(payload[3])
                self._counts['actor_snapshot_acks'if matched else'actor_snapshot_ack_misses']+=1
                self._scene.observe_role(payload);return None
            if kind in(8,9,11):
                try:accepted=self._scene.observe_role(payload)
                except ValueError:
                    self._scope()
                    if self._scene._closed:raise
                    self._counts['invalid_role_events']+=1;return None
                if not accepted:return None
                if kind==11:return self._group([self._ack()],delta_acks=1)
                if kind==9:
                    body=bytes((17,generation,0))+struct.pack('<IH',self._map,self._scene._version)
                    return self._group([Frame(14,body,True)],scene_confirmations=1)
                return None
            self._counts['ignored_messages']+=1;return None
        except BaseException:
            self.close();raise

    def commit(self,group,sent_count):
        try:
            self._scope()
            if(group is None or group is not self._pending or type(sent_count)is not int or
               sent_count!=len(group.frames)):raise ValueError('map_long_delivery_unknown')
            staged=self._pending_data;plan=staged.get('scene_plan')
            if plan is not None:
                self._scene.commit(plan,len(plan.frames))
                self._counts['scene_pairs'if len(plan.frames)==2 else'controller_frames']+=1
            if 'started'in staged:
                self._actor_started_at=staged['started']
                if self._actor_origin is None:self._actor_origin=self._actor_started_at
                self._counts['actor_frames']+=1
            if 'extension'in staged:
                self._last_extension=staged['extension'];self._counts['actor_extensions']+=1
                self._counts['actor_frames']+=1
            if 'actor_writer'in staged:self._actor_writer=staged['actor_writer']
            for key in('delta_acks','scene_confirmations','nettime_replies'):
                self._counts[key]+=staged.get(key,0)
            self._counts['groups_committed']+=1;self._counts['frames_committed']+=sent_count
            self._pending=self._pending_data=None
            for frame in group.frames:
                if frame.outer_type==14:
                    _trace_scene_v31t(self,frame.payload,committed=True,reliable=frame.reliable)
        except BaseException:
            self.close();raise

    def close(self):
        self._closed=True;self._pending=self._pending_data=None;self._actor_seed=b''
        if hasattr(self,'_scene'):self._scene.close()
        _trace_scene_v31t(self,closing=True)
    def metadata(self):
        result=dict(self._counts)
        result.update(closed=self._closed,delivery_pending=self.has_pending,home_only=False,
            membership_provenance=self._membership_provenance,
            current_scene_actor_inputs=True,legacy_actor_migration=False,synthetic_ws_state=False,
            local_lifecycle_binding=True,actor_clock_revision_only=True,native_actor_producer=False,
            native_scene_producer=False,cross_map_replay=False,local_idle_restored=self._local_idle_restored,
            actor_total_revision_steps=self._revision_base+self._counts['actor_extensions'],
            server_application_verified=False,actor_initial_sent=self._actor_started_at is not None)
        result.update(actor_delta_sender=True,
            actor_acknowledged_baseline=self._actor_writer.metadata()['acknowledged_baseline'])
        for key,value in self._scene.metadata().items():
            if type(value)in(bool,int):result['scene_'+key]=value
        return result
