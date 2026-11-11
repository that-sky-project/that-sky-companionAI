"""Opt-in local Actor adapter. No imports/hooks into room_pose and no network I/O.

Legacy jump fields stay unchanged. Explicit v22 aerial profiles reuse only
capture-verified motion codes and the existing grounded walk renderer; other
air bits remain untouched. Ground contact authority comes only from HopMotion.
"""
from dataclasses import dataclass, field
import copy
import hashlib
import math
import re
from bisect import bisect_left

CODEC_SHA256 = '0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed'
CAPTURE_PINS = (
    '4f5f754f09061066f2c90855a196ed2ba4c8d3caf6d599a79a163fd6f9ca791c',
    '11180f2f9d8b41f78e31d0cc67574cc2ed000a15d9851f30316c4c6a6b786479',
)
FLAP_CAPTURE_SHA256 = 'fc392085d9e14450e094d82d5d327903d6db7a66c4c63505f1dd6d6b244f5d73'
FLAP_KEY = 0x49a9b58938e359c2
AERIAL_PROFILES = ('drop_v22','flap_v22','jump_drop_v23')
MANUAL_CAPTURE_PINS = ('c61d4855555a4a66c3d54438f368e52c390c72db971ebf6febc774fc4baa949f',)
_PROFILES = {
    # Manual button capture; contact remains KCC-owned and clears immediately.
    'manual_stand_r': ('LeapFromStandFirstR', 0x5d86b99b0f7a3e4d,
                       'JumpDescendFirstR', 0xc4891a7f0d038461),
    'up_small_r2': ('LeapFromRunUpSmlR2', 0x33b7a1032a510f93,
                    'JumpDescendUpSmlR2', 0x0df9587589a09404),
    'diagonal_l': ('LeapFromRun45L', 0xad833d01b201356d,
                   'JumpDescend45L', 0x46887827eb129912),
}
_PROFILE_CAPTURE_PINS = {
    'up_small_r2': CAPTURE_PINS, 'diagonal_l': CAPTURE_PINS,
    'manual_stand_r': MANUAL_CAPTURE_PINS,
}
# JumpLandR 0xc72da53ac09a97e2 was observed, but this profile intentionally
# does not synthesize landing weight/hold timing. Existing ClearEvent handoff
# runs immediately after true KCC contact; no new idle/hold delay is added.
ALLOWED_FIELDS = frozenset(('ox', 'oy', 'oz', 'ax', 'ay', 'az',
                            'vx', 'vy', 'vz', 'key', 'gen', 'start',
                            'yaw1', 'yaw2', 'yaw3', 'fx', 'fy', 'fz', 'code90'))
UNVERIFIED_UNCHANGED = ('native_air_ground_bits', 'force_codes', 'pose_components',
                        'code90_blending', 'history_sync', 'native_land_key')


class HopActorError(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise HopActorError(reason)


class Raw12TickUnwrapper:
    """Owner-scoped modulo difference; no wall-clock/tick-rate inference.

    max_delta is an explicit diagnostic bound, not a native tick frequency.
    Three successive unchanged packet clocks fail by default, not invented time.
    """
    def __init__(self, *, max_delta=1024, max_unchanged=2):
        _need(type(max_delta) is int and 1 <= max_delta < 2048, 'clock_delta_bound')
        _need(type(max_unchanged) is int and 0 <= max_unchanged <= 8, 'clock_stall_bound')
        self.max_delta, self.max_unchanged = max_delta, max_unchanged
        self.owner = self.raw = self.tick = None
        self.unchanged, self.closed = 0, False

    def observe_raw(self, raw, *, owner):
        _need(not self.closed, 'clock_closed')
        try:
            _need(type(raw) is int and 0 <= raw < 4096, 'clock_raw12')
            _need(owner is not None, 'clock_owner_missing')
            if self.raw is None:
                self.owner, self.raw, self.tick = owner, raw, raw
                return self.tick
            _need(owner is self.owner, 'clock_owner_changed')
            delta = (raw - self.raw) & 4095
            _need(delta <= self.max_delta, 'clock_reverse_or_large_jump')
            self.unchanged = self.unchanged + 1 if delta == 0 else 0
            _need(self.unchanged <= self.max_unchanged, 'actor_clock_not_advancing')
            self.tick += delta
            self.raw = raw
            return self.tick
        except Exception:
            self.closed = True
            raise

    def observe_body(self, body, anim_pose, *, owner):
        return self.observe_raw(anim_pose._required_clock(body), owner=owner)


@dataclass(frozen=True)
class EngineeringTrial:
    enabled: bool = False
    backup_manifest_sha256: str = ''
    codec_sha256: str = CODEC_SHA256
    capture_pins: tuple = CAPTURE_PINS
    profile_id: str = 'up_small_r2'
    aerial_profiles: tuple = ()

    def validate(self, profile_id):
        _need(self.enabled is True, 'engineering_trial_disabled')
        _need(isinstance(self.backup_manifest_sha256, str)
              and re.fullmatch('[0-9a-f]{64}', self.backup_manifest_sha256), 'backup_pin_required')
        _need(self.codec_sha256 == CODEC_SHA256 and self.capture_pins == _PROFILE_CAPTURE_PINS.get(profile_id),
              'evidence_pin_mismatch')
        _need(self.profile_id == profile_id, 'trial_profile_mismatch')
        _need(type(self.aerial_profiles) is tuple and len(set(self.aerial_profiles))==len(self.aerial_profiles)
              and all(p in AERIAL_PROFILES for p in self.aerial_profiles),'aerial_trial_profiles')


@dataclass(frozen=True)
class HopActorPatch:
    actor_bytes: bytes = field(repr=False)
    phase: str
    grounded_by_kcc: bool
    ground_handoff_required: bool
    native_state_name: str | None
    changed_fields: tuple
    changed_bits: int
    unwrapped_actor_tick: int
    total_step: int
    source_path: str
    experimental: bool = True
    native_physics_equivalent: bool = False
    unchanged_unknown_fields: tuple = UNVERIFIED_UNCHANGED
    heading: float | None = None
    _aerial_overlay: object = field(default=None,repr=False,compare=False)
    _aerial_baseline: object = field(default=None,repr=False,compare=False)


def _source_identity(body, observation, anim_pose):
    """Bind seed provenance, ignoring only the actual engine's clock/revision updates.

    No identity bytes/digest leave the encoder. Unlike ignoring all motion fields,
    this detects a changed seed even when its key/gen/start happen to be equal.
    """
    starts = anim_pose._starts(observation)
    masked = bytearray(body)
    for label, position in observation._boundaries:
        if label not in ('0x1715e80', '0x21dd0c8', '0x21defc0'):
            continue
        i = bisect_left(starts, position)
        _need(i < len(starts), 'source_dynamic_field_missing')
        token = observation._tokens[i]
        clock = label == '0x1715e80'
        _need((token.kind == 'bits' and token.extent == 12) if clock else
              (token.kind == 'raw' and token.extent == 1), 'source_dynamic_field_shape')
        for bit in range(starts[i], starts[i] + (12 if clock else 8)):
            masked[bit // 8] &= ~(1 << (bit % 8))
    return hashlib.sha256(masked).digest()


def _verified_write(body, values, anim_pose, before, loc):
    _need(set(values) <= ALLOWED_FIELDS, 'actor_field_not_allowed')
    _need(all(name in loc for name in values), 'actor_field_absent')
    patched = anim_pose.write(body, **values)
    after = anim_pose.inspect_actor_wire(patched, verified_profile_sha256=CODEC_SHA256,
                                         check_zero_padding=False)
    _need(len(body) == len(patched) and before._counts == after._counts
          and before._boundaries == after._boundaries
          and len(before._tokens) == len(after._tokens), 'actor_structure_changed')
    starts = anim_pose._starts(before)
    allowed = set()
    for name in values:
        index = loc[name]
        token = before._tokens[index]
        width = token.extent * 8 if token.kind == 'raw' else token.extent
        allowed.update(range(starts[index], starts[index] + width))
    changed = {8 * i + bit for i, (a, b) in enumerate(zip(body, patched))
               for bit in range(8) if ((a ^ b) >> bit) & 1}
    _need(changed <= allowed, 'unrelated_actor_bits_changed')
    new_loc = anim_pose._locate(after)
    for name, value in values.items():
        actual = anim_pose._val(name, after._tokens[new_loc[name]])
        _need(math.isclose(actual, value, rel_tol=2e-7, abs_tol=2e-5)
              if name in ('ox', 'oy', 'oz', 'ax', 'ay', 'az') else actual == value,
              'actor_patch_roundtrip')
    return patched, len(changed)


# Observed widths are fixed to the verified 0.16.8 codec. Only current POSE
# control fields are selected; same-label history fields are deliberately excluded.
_RAW_AERIAL_WIDTHS={'0x21dd40c':1,'0x21ddcc8':5,'0x1f80844':8,'0x1715f48':3,'0x1715f70':3}
def _aerial_locations(o,a):
    starts=a._starts(o);bounds=dict(o._boundaries);out={}
    for label,width in _RAW_AERIAL_WIDTHS.items():
        positions=[pos for lab,pos in o._boundaries if lab==label and (label not in ('0x1715f48','0x1715f70') or bounds[a.CURRENT]<=pos<bounds[a.ANIM_BLOCK])]
        _need(len(positions)==1,'aerial_raw_label_scope')
        index=bisect_left(starts,positions[0]);token=o._tokens[index]
        _need(starts[index]==(((positions[0]+7)//8)*8 if label=='0x1f80844' else positions[0]) and ((token.kind=='bits' and token.extent==width) or (label=='0x1f80844' and token.kind=='raw' and token.extent==1)),'aerial_raw_shape')
        out[label]=(index,width)
    return out

def aerial_motion_codes(body,a):
    o=a.inspect_actor_wire(body,verified_profile_sha256=CODEC_SHA256,check_zero_padding=False)
    return {label:int.from_bytes(o._tokens[i].value,'little') if o._tokens[i].kind=='raw' else o._tokens[i].value for label,(i,width) in _aerial_locations(o,a).items()}

def _write_aerial_raw(body,values,a):
    _need(set(values)<=set(_RAW_AERIAL_WIDTHS),'aerial_raw_unknown_field')
    before=a.inspect_actor_wire(body,verified_profile_sha256=CODEC_SHA256,check_zero_padding=False);loc=_aerial_locations(before,a);starts=a._starts(before);out=bytearray(body);allowed=set()
    for label,value in values.items():
        index,width=loc[label];_need(type(value) is int and 0<=value<(1<<width),'aerial_raw_value')
        for shift in range(width):
            bit=starts[index]+shift;allowed.add(bit);byte,offset=divmod(bit,8);out[byte]=(out[byte]&~(1<<offset))|(((value>>shift)&1)<<offset)
    patched=bytes(out);after=a.inspect_actor_wire(patched,verified_profile_sha256=CODEC_SHA256,check_zero_padding=False)
    _need(before._counts==after._counts and before._boundaries==after._boundaries and len(before._tokens)==len(after._tokens),'aerial_raw_structure_changed')
    changed={8*i+bit for i,(x,y) in enumerate(zip(body,patched)) for bit in range(8) if ((x^y)>>bit)&1}
    _need(changed<=allowed,'aerial_raw_unrelated_change')
    codes=aerial_motion_codes(patched,a);_need(all(codes[k]==v for k,v in values.items()),'aerial_raw_roundtrip')
    return patched

def _apply_aerial_overlay(body,overlay,a):
    if overlay is None:return body
    raw,pose,code90=overlay
    body=a.write_pose_fields(body,pose) if pose else body
    body=_write_aerial_raw(body,raw,a)
    if code90 is not None:
        o=a.inspect_actor_wire(body,verified_profile_sha256=CODEC_SHA256,check_zero_padding=False);body,_=_verified_write(body,{'code90':code90},a,o,a._locate(o))
    return body


def _motion_heading(frames, previous):
    """Zero-horizontal-plan fallback only: preserve a known heading, or use
    the first verified real motion. Collision responses never keep steering it.
    """
    if previous is not None:
        return previous
    for frame in frames:
        if frame.phase not in ('takeoff', 'rising', 'falling', 'settling'):
            continue
        vx, _, vz = frame.velocity
        if math.hypot(vx, vz) > .05:
            return math.degrees(math.atan2(vx, vz))
    return None


def _planned_heading(motion):
    # HopMotion already bound this immutable, digest-pinned proof. The contract
    # stores the prevalidated actual terminal as landing, in the same local
    # translation-only frame as start. A bounded hop never retargets midflight.
    start, landing = motion._proof.start, motion._proof.landing
    dx, dz = landing[0]-start[0], landing[2]-start[2]
    return math.degrees(math.atan2(dx, dz)) if math.hypot(dx, dz) > 1e-6 else None


def _facing_values(anim_pose, heading):
    if heading is None:
        return {}
    _need(type(heading) in (float, int) and math.isfinite(heading), 'finite_heading_required')
    yaw, fx, fy, fz = anim_pose.facing_codes(heading)
    return dict(yaw1=yaw, yaw2=yaw, yaw3=yaw, fx=fx, fy=fy, fz=fz)


class HopActorEncoder:
    """One encoder per HopMotion. Both preview and engineering emission are opt-in.

    The input is the current publisher Actor, not a recorded Actor replay.
    emit() must synchronously hand the candidate to the existing guarded sender;
    this module does not install a sender or extend the sample permit lifetime.
    """
    def __init__(self, anim_pose, *, profile_id='up_small_r2', allow_local_preview=False,
                 engineering_trial=None, walk_clip=None, walk_force=None):
        _need(profile_id in _PROFILES, 'unknown_hop_profile')
        _need(anim_pose.SHA == CODEC_SHA256, 'codec_profile_mismatch')
        self.anim_pose, self.profile_id = anim_pose, profile_id
        self.profile = _PROFILES[profile_id]
        for name, key in ((self.profile[0], self.profile[1]), (self.profile[2], self.profile[3])):
            _need(anim_pose.anim_key(name) == key, 'native_name_hash_mismatch')
        self.allow_local_preview, self.engineering_trial = allow_local_preview, engineering_trial
        self.clock = Raw12TickUnwrapper()
        self.motion = self.last_sample = self.expected_animation = None
        self.native_state_name = None
        self.seed_stamp = self.seed_identity = self.output_identity = None
        self.closed = False
        self.heading = None
        self.walk_clip,self.walk_force=walk_clip,copy.deepcopy(walk_force)
        self._aerial_baseline=self._aerial_overlay=None

    def facing_values(self):
        return _facing_values(self.anim_pose, self.heading)

    def project_retained_aerial(self,body):
        _need(not self.closed,'encoder_closed')
        return _apply_aerial_overlay(body,self._aerial_overlay,self.anim_pose)

    def _candidate(self, body, motion, sample):
        from skyterrain.hop_adapter_v20 import HopMotion, HopSample
        _need(not self.closed, 'encoder_closed')
        _need(type(motion) is HopMotion and type(sample) is HopSample
              and motion.is_current_sample(sample), 'latest_sealed_sample_required')
        _need(self.motion is None or motion is self.motion, 'hop_owner_changed')
        _need(sample is not self.last_sample, 'sample_already_encoded')
        _need(type(body) is bytes and 0 < len(body) <= 0x2420, 'actor_size')
        aerial=getattr(motion._proof,'profile_id',None)
        if aerial is not None:
            _need(type(self.engineering_trial) is EngineeringTrial and self.engineering_trial.enabled is True
                  and aerial in self.engineering_trial.aerial_profiles,'aerial_actor_trial_disabled')
            _need(aerial in AERIAL_PROFILES,'aerial_actor_profile')
            if aerial=='jump_drop_v23':
                _need(self.profile_id=='manual_stand_r','jump_drop_manual_profile_required')
            if aerial=='flap_v22':
                _need(motion._proof.source_capture_sha256==FLAP_CAPTURE_SHA256 and self.anim_pose.anim_key('FlapPV2')==FLAP_KEY,'flap_capture_binding')
        frame = sample.validated_frames[-1]
        _need(frame.position == sample.position and frame.total_step == sample.total_step,
              'latest_fixed_frame_mismatch')
        a = self.anim_pose
        before = a.inspect_actor_wire(body, verified_profile_sha256=CODEC_SHA256,
                                      check_zero_padding=False)
        loc = a._locate(before)
        fields = {n: a._val(n, before._tokens[i]) for n, i in loc.items()}
        _need(fields['mode'] == 0 and fields.get('seated', 0) == 0,
              'actor_not_plain_motion')
        stamp = tuple(fields[n] for n in ('key', 'gen', 'start'))
        identity = _source_identity(body, before, a)
        if self.seed_identity is None:
            source_path = 'initial_seed'
        elif stamp == self.seed_stamp and identity == self.seed_identity:
            source_path = 'retained_engine_seed'
        elif stamp == self.expected_animation and identity == self.output_identity:
            source_path = 'previous_local_output'
        else:
            self.closed = True
            motion.invalidate()
            raise HopActorError('actor_animation_taken_over_or_seed_changed')
        owned_stamp = self.expected_animation if self.expected_animation is not None else stamp
        clock = copy.copy(self.clock)
        tick = clock.observe_body(body, a, owner=motion)
        position, velocity = frame.position, frame.velocity
        _need(len(position) == len(velocity) == 3
              and all(math.isfinite(v) and abs(v) < 10000 for v in position)
              and all(math.isfinite(v) and abs(v) <= 100 for v in velocity), 'motion_vector_bound')
        values = dict(zip(('ox', 'oy', 'oz'), position))
        values.update(zip(('ax', 'ay', 'az'), position))
        values.update(zip(('vx', 'vy', 'vz'), a.velocity_codes(velocity)))
        heading = _planned_heading(motion)
        if heading is None:
            heading = _motion_heading(sample.validated_frames, self.heading)
        values.update(_facing_values(a, heading))
        # MapLongEngine re-derives every packet from its retained seed. Reapply
        # only this encoder's owned stamp, while preserving the incoming clock.
        if self.native_state_name is not None:
            values.update(zip(('key', 'gen', 'start'), owned_stamp))
        phase, state_name = frame.phase, None
        approach=aerial in ('drop_v22','jump_drop_v23') and (phase=='approach' or phase=='settling' and frame.grounded and self.native_state_name is None)
        if approach:_need(frame.grounded and frame.state_vertical_speed<=1e-6,'drop_actor_approach_support')
        grounded_phase = phase in ('grounding', 'landed', 'cancelled_landed')
        if phase in ('landed', 'cancelled_landed'):
            _need(frame.grounded is True and sample.arrived, 'kcc_landing_required')
        if not grounded_phase and not approach:
            _need(dict(before._counts)['pose_records'] == 1, 'animation_requires_single_current_pose')
            _need(phase in ('takeoff', 'rising', 'falling', 'settling'), 'unknown_hop_phase')
            falling = phase == 'falling' or (phase == 'settling' and frame.state_vertical_speed <= 0)
            if aerial=='flap_v22':state_name,key='FlapPV2',FLAP_KEY
            elif aerial=='drop_v22':state_name,key='JumpDescendFirstR',0xc4891a7f0d038461
            elif aerial=='jump_drop_v23':state_name,key=_PROFILES['manual_stand_r'][2:4] if falling else _PROFILES['manual_stand_r'][0:2]
            else:state_name, key = self.profile[2:4] if falling else self.profile[0:2]
            if self.native_state_name != state_name:
                # The existing AnimEvent convention is gen+1, start=actual Actor clock.
                _need(owned_stamp[1] < 0xffffffff, 'animation_generation_exhausted')
                values.update(key=key, gen=owned_stamp[1] + 1, start=clock.raw)
        baseline=self._aerial_baseline;overlay=None
        if aerial is not None:
            if baseline is None:
                baseline=(aerial_motion_codes(body,a),{k:v for k,v in a.pose_values(body).items() if '|current|' in k},fields['code90'])
            raw,pose,code90=copy.deepcopy(baseline)
            if approach and phase=='approach':
                _need(self.walk_clip is not None and type(self.walk_force) is dict,'drop_walk_renderer_required')
                _need(self.walk_force=={'0x21dd40c':1,'0x21ddcc8':31,'0x1715f48':0,'0x1715f70':0},'walk_force_evidence_mismatch')
                replay=a.normalize_pose_values(self.walk_clip.at(sample.total_step*motion._proof.dt),legacy_pose_counts=(1,1))
                pose={k:v for k,v in a.rotate_pose(replay,heading-self.walk_clip.heading).items() if '|current|' in k}
                _need(bool(pose),'drop_walk_current_pose_required');raw.update(self.walk_force)
                # Ground renderer carries no action key; do not start the fall key.
                values.update(zip(('key','gen','start'),owned_stamp))
            elif aerial=='flap_v22' and not grounded_phase:
                raw.update({'0x21dd40c':0,'0x21ddcc8':31,'0x1f80844':8,'0x1715f48':0,'0x1715f70':0})
                code90=1 if self.native_state_name!='FlapPV2' else 0
            overlay=(raw,pose,code90)
        patched, changed = _verified_write(body, values, a, before, loc)
        patched=_apply_aerial_overlay(patched,overlay,a)
        changed=sum((x^y).bit_count() for x,y in zip(body,patched))
        _need(motion.is_current_sample(sample), 'sample_revoked_during_patch')
        next_stamp = tuple(values.get(n, fields[n]) for n in ('key', 'gen', 'start'))
        written=set(values)
        if overlay is not None:
            written.update('raw:'+label for label in overlay[0]);written.update('pose:'+key for key in overlay[1]);written.add('code90')
        result = HopActorPatch(patched, phase, frame.grounded,
                               phase in ('landed', 'cancelled_landed'), state_name,
                               tuple(sorted(written)), changed, tick, frame.total_step, source_path, heading=heading, _aerial_overlay=overlay, _aerial_baseline=baseline,
                               unchanged_unknown_fields=UNVERIFIED_UNCHANGED if aerial is None else ('unobserved_air_flags','history_sync','native_land_key'))
        after = a.inspect_actor_wire(patched, verified_profile_sha256=CODEC_SHA256,
                                     check_zero_padding=False)
        return result, clock, next_stamp, identity, _source_identity(patched, after, a), stamp

    def _commit(self, motion, sample, clock, stamp, state_name, source_id, output_id, input_stamp):
        if self.seed_identity is None:
            self.seed_identity, self.seed_stamp = source_id, input_stamp
        self.output_identity = output_id
        self.motion, self.last_sample = motion, sample
        self.clock, self.expected_animation = clock, stamp
        if state_name is not None:
            self.native_state_name = state_name

    def preview(self, body, motion, sample):
        _need(self.allow_local_preview is True, 'local_preview_disabled')
        result, clock, stamp, source_id, output_id, input_stamp = self._candidate(body, motion, sample)
        self._commit(motion, sample, clock, stamp, result.native_state_name, source_id, output_id, input_stamp)
        self.heading = result.heading
        self._aerial_overlay,self._aerial_baseline=result._aerial_overlay,result._aerial_baseline
        return result

    def engineering_trial_emit(self, body, motion, sample, emit):
        _need(type(self.engineering_trial) is EngineeringTrial, 'engineering_trial_disabled')
        self.engineering_trial.validate(self.profile_id)
        _need(callable(emit), 'synchronous_emitter_required')
        result, clock, stamp, source_id, output_id, input_stamp = self._candidate(body, motion, sample)
        _need(motion.consume_publication(sample), 'sample_permit_unavailable')
        self._commit(motion, sample, clock, stamp, result.native_state_name, source_id, output_id, input_stamp)
        self.heading = result.heading
        self._aerial_overlay,self._aerial_baseline=result._aerial_overlay,result._aerial_baseline
        try:
            emit(result)
        except Exception:
            self.closed = True
            raise
        return result
