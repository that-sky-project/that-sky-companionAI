"""Pinned, explicitly experimental asset bootstrap; not a loaded/current capture.

No I/O, account discovery or transport occurs here. The caller supplies a REAL
same-account stored source from prepare_current_capture(). Synthetic source
objects are appropriate only for unit fixtures, not for runtime provenance.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
import re
import struct
from types import MappingProxyType

from qfsky.join0167_profile import LoadedProfile, account_binding, NATIVE_SHA256
from qfsky.long0167.level_authority import LevelRecord
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire, reencode_actor_wire
from . import catalog_account_pins as _catalog_pins
from .current_inputs import CurrentContext, CurrentInputs
from .load_current_capture import StoredCaptureScope
from .scene_only_draft import SceneBaseline, check_scene_record, _SEAL as _SCENE_SEAL

ENV_ENABLE = 'QFSKY_ASSET_BOOTSTRAP_TRIAL'
ENV_MANIFEST_SHA = 'QFSKY_ASSET_BOOTSTRAP_MANIFEST_SHA256'
KIND = 'asset-bootstrap-experimental'
MANIFEST_SCHEMA = 'sky-asset-bootstrap-trial-v32'
SCENE = 'NightArchive'
MAP_ID = 2518601
RESOURCE_REVISION = 'apk-v75506-421589'
NV = (36184, 49307, 23536, 0, 0, 25536)
SOURCE_SHA = '97473401054708cbd401f8883766a837a2003cb4203d49a1795f820a107d2ed2'
ACTOR_SHA = '99916c60de406810e07c78e693a1e0726d73475c809c36af26b8cd3c510d7219'
PLACEMENT_SHA = 'c9d5b0d297cea8555739b5662fd107f91d937eaf617bdeb75c6dd621ee848a8d'
MEMBER_SHA = 'd5ee907701715ab751696a4fe8211c14b80c9de78e30a60e1007dff9daad997c'
APK_DIRECTORY_SHA = 'b26db74613f606fd60b942ff58f00d0c41b12a6d83303228a1143d683adefb14'
SOURCE_METADATA_SHA = '0e81fe7d2e7ef41dd4c8297ba9ba1966399b6eebec74c0e393cb9515c7ec2686'
_PHASES = {
    'sky-asset-scene-candidate-v32': (
        'finite_initializers_only_before_first_event_update',
        'native_constructor_with_exact_asset_projections'),
    'sky-selected-onload-scene-candidate-v32': (
        'selected_finite_and_OnLevelLoad_mpvar_initializers_before_updates',
        'native_constructor_with_selected_OnLevelLoad_and_finite_initializers'),
}
_SEAL = object()


def _fail(code):
    raise ValueError('asset_bootstrap_' + code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _pin(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        _fail('sha256_shape')
    return value


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_json_key')
        result[key] = value
    return result


def _utc(value):
    if type(value) is not str:
        _fail('utc_shape')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError):
        _fail('utc_shape')
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
        _fail('utc_timezone')
    return dt


def _gate(raw, now_utc=None):
    if os.environ.get(ENV_ENABLE) != '1':
        _fail('trial_disabled')
    if type(raw) is not bytes or not 2 <= len(raw) <= 65536:
        _fail('manifest_extent')
    if not hmac.compare_digest(_pin(os.environ.get(ENV_MANIFEST_SHA)), _sha(raw)):
        _fail('manifest_environment_pin')
    try:
        m = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_pairs,
                       parse_constant=lambda _: _fail('nonfinite_json'))
    except (UnicodeError, json.JSONDecodeError):
        _fail('manifest_json')
    if type(m) is not dict or m.get('schema') != MANIFEST_SCHEMA:
        _fail('manifest_schema')
    if m.get('kind') != KIND or m.get('classification') != KIND or m.get('enabled') is not True:
        _fail('manifest_experimental_kind')
    if m.get('allowlist') != [SCENE] or m.get('scene') != SCENE:
        _fail('manifest_allowlist')
    now = datetime.now(timezone.utc) if now_utc is None else _utc(now_utc)
    if now >= _utc(m.get('deadline_utc')):
        _fail('trial_deadline')
    if (type(m.get('apk_build')) is not int or m['apk_build'] != 421589 or
            m.get('native_sha256') != NATIVE_SHA256 or
            type(m.get('level_id')) is not int or m['level_id'] != MAP_ID or
            m.get('resource_revision') != RESOURCE_REVISION or
            type(m.get('netversion_u16')) is not list or
            any(type(v) is not int for v in m['netversion_u16']) or
            tuple(m['netversion_u16']) != NV):
        _fail('manifest_native_map_revision')
    for key in ('cache_sha256', 'scene_sha256', 'actor_sha256', 'source_actor_sha256',
                'source_metadata_sha256', 'scene_provenance_sha256', 'placement_sha256',
                'source_account_binding_hash', 'source_capture_record_sha256'):
        _pin(m.get(key))
    if (m['actor_sha256'] != ACTOR_SHA or m['source_actor_sha256'] != SOURCE_SHA or
            m['source_metadata_sha256'] != SOURCE_METADATA_SHA or m['placement_sha256'] != PLACEMENT_SHA):
        _fail('manifest_frozen_actor_or_placement')
    for key in ('complete_scene_initialization', 'loaded_scene_snapshot', 'capture_generated',
                'full_connection_ready', 'trial_validated'):
        if key in m and m[key] is not False:
            _fail('manifest_false_readiness_required')
    return m


def _record(cache, wire, *, require_zero_padding=True):
    if type(cache) is not bytes or len(cache) != 0x2010 or type(wire) is not bytes:
        _fail('cache_extent')
    size = struct.unpack_from('<H', cache, 0x200e)[0]
    if not 4 <= size <= 0x2000 or cache[14:14+size] != wire:
        _fail('cache_scene_disagree')
    if require_zero_padding and (cache[3] or cache[9] or any(cache[14+size:0x200e])):
        _fail('cache_padding')
    return LevelRecord(struct.unpack_from('<H', cache)[0], cache[2],
                       struct.unpack_from('<I', cache, 4)[0], cache[8],
                       struct.unpack_from('<H', cache, 10)[0], struct.unpack_from('<H', cache, 12)[0], wire)


def _actor(raw, map_id):
    if type(raw) is not bytes or not 1 <= len(raw) <= 0x528:
        _fail('actor_extent')
    obs = inspect_actor_wire(raw, verified_profile_sha256=NATIVE_SHA256,
                             check_zero_padding=True, require_canonical_writer=True)
    if (not obs.matches_prefix_u32(map_id) or
            reencode_actor_wire(obs, verified_profile_sha256=NATIVE_SHA256) != raw):
        _fail('actor_map_or_canonical')
    if dict(obs._counts).get('timed_raw9_records', 0):
        _fail('timed_actor_requires_policy')
    return obs


def _pins_for_manifest(manifest):
    """目录 manifest 走按账号 pin；NightArchive 等没有 entries 的保持原常量。"""
    if type(manifest) is not dict or 'entries' not in manifest:
        return None
    return _catalog_pins.load_for_binding(manifest.get('source_account_binding_hash'))


def _source(source, meta, binding, manifest):
    # A bool or context manufactured by this adapter is never accepted.
    if type(source) is not CurrentInputs or type(source._context) is not CurrentContext:
        _fail('stored_same_account_source_required')
    c = source._context
    if (type(c.world_generation) is not StoredCaptureScope or c.net_module_address != 0 or
            c.apk_build != 421589 or c.native_sha256 != NATIVE_SHA256 or
            type(c.account_binding_hash) is not str or
            not hmac.compare_digest(c.account_binding_hash, binding)):
        _fail('source_account_or_stored_scope')
    _pin(c.world_generation.record_sha256)
    if (not hmac.compare_digest(binding, manifest['source_account_binding_hash']) or
            not hmac.compare_digest(c.world_generation.record_sha256, manifest['source_capture_record_sha256'])):
        _fail('source_manifest_account_or_record_pin')
    _utc(c.world_generation.captured_utc)
    pins = _pins_for_manifest(manifest)
    source_sha = SOURCE_SHA if pins is None else pins['source_actor_sha256']
    metadata_sha = SOURCE_METADATA_SHA if pins is None else pins['source_metadata_sha256']
    if (type(meta) is not dict or _sha(canonical_json(meta)) != metadata_sha or
            meta.get('source_kind') != 'existing_native_capture_not_live_current' or
            meta.get('nativeSHA256') != NATIVE_SHA256 or meta.get('actorSHA256') != source_sha or
            meta.get('scene') != c.scene or meta.get('map_id') != c.level_id or
            type(meta.get('bytes')) is not int or meta['bytes'] != len(source.actor_bytes()) or
            meta.get('canonical_roundtrip') is not True or
            meta.get('same_active_bot_account_verified_on_server') is not True):
        _fail('source_metadata')
    if _sha(source.actor_bytes()) != source_sha:
        _fail('source_actor_pin')
    nv = source._netversion
    if (type(nv) is not tuple or len(nv) != 6 or
            any(type(v) is not int or not 0 <= v <= 65535 for v in nv) or
            nv[2:] != (23536, 0, 0, 25536)):
        _fail('source_nv')
    scope = c.world_generation
    if (type(scope.join_fields) is not tuple or len(scope.join_fields) != 3 or
            type(scope.join_fields[0]) is not bytes or len(scope.join_fields[0]) != 16 or
            type(scope.join_fields[1]) is not bytes or len(scope.join_fields[1]) != 64 or
            type(scope.join_fields[2]) is not int or not 0 <= scope.join_fields[2] <= 255 or
            scope.scene_observation != ('adjacent-native-init-lines-exact-timestamp-pid-tid', c.scene, c.level_id, nv)):
        _fail('source_prepared_capture_scope')
    record = _record(source.cache_bytes(), source.scene_bytes(), require_zero_padding=False)
    if source._record != record:
        _fail('source_record')
    check_scene_record(record, resource_tag=nv[0], native_record_size=nv[5], account_hash=binding)
    obs = _actor(source.actor_bytes(), c.level_id)
    if obs._counts != source._actor_counts or dict(obs._counts) != meta.get('variable_group_counts'):
        _fail('source_counts')
    return obs


def _preserved_actor(source, target, source_obs, target_obs, position):
    if (len(source) != len(target) or source_obs._counts != target_obs._counts or
            source_obs._boundaries != target_obs._boundaries):
        _fail('actor_layout_changed')
    starts = []
    bit = 0
    for token in source_obs._tokens:
        if token.kind == 'raw':
            bit = (bit + 7) & ~7
        elif token.kind != 'bits':
            _fail('actor_token_kind')
        starts.append(bit)
        bit += token.extent * 8 if token.kind == 'raw' else token.extent
    bounds = {}
    for label, offset in source_obs._boundaries:
        bounds.setdefault(label, []).append(offset)
    def at(label, kind, extent):
        offsets = bounds.get(label, [])
        if len(offsets) != 1:
            _fail('actor_boundary_shape')
        index = next((i for i, start in enumerate(starts) if start >= offsets[0]), None)
        if index is None or (source_obs._tokens[index].kind, source_obs._tokens[index].extent) != (kind, extent):
            _fail('actor_field_shape')
        return index
    main_map = at('0x21dc8ec', 'raw', 4)
    origin = at('0x21dc858', 'raw', 4)
    history = at('0x21de7c4', 'raw', 1)
    velocity = at('0x21dca9c', 'bits', 8)
    changes = {main_map: struct.pack('<I', MAP_ID), history+1: struct.pack('<I', MAP_ID)}
    for i, value in enumerate(position):
        changes[origin+i] = struct.pack('<f', value)
        changes[history+2+i] = struct.pack('<f', value)
    for i in range(3):
        changes[velocity+i] = 127
    mask = bytearray(len(source))
    # 2026-11-07 好友传送：0x21dee4c 关卡状态头（raw2, level, resource tag）可改成本图值，或保持源值。
    level_state = at('0x21dee4c', 'raw', 2)
    optional = {level_state: bytes(2), level_state+1: struct.pack('<I', MAP_ID),
                level_state+2: struct.pack('<I', NV[0])}
    for i, (before, after) in enumerate(zip(source_obs._tokens, target_obs._tokens)):
        if (before.kind, before.extent) != (after.kind, after.extent):
            _fail('actor_token_shape_changed')
        if i in optional and before != after:
            if after.value != optional[i]:
                _fail('actor_level_state_mismatch')
            mask[starts[i]//8:starts[i]//8+before.extent] = bytes([255])*before.extent
            continue
        if i not in changes:
            if before != after:
                _fail('actor_unrelated_token_changed')
            continue
        value = changes[i]
        if before.kind == 'raw':
            if before.extent != 4 or type(value) is not bytes or after.value != value:
                _fail('actor_allowed_raw_mismatch')
            mask[starts[i]//8:starts[i]//8+4] = b'\xff'*4
        else:
            if before.extent != 8 or after.value != 127:
                _fail('actor_velocity_mismatch')
            for shift in range(before.extent):
                byte, lane = divmod(starts[i]+shift, 8)
                mask[byte] |= 1 << lane
    if len(source_obs._tokens) != len(target_obs._tokens) or any((a ^ b) & ~m for a, b, m in zip(source, target, mask)):
        _fail('actor_unrelated_bits_changed')


def _placement(p, m):
    if type(p) is not dict or _sha(canonical_json(p)) != m['placement_sha256']:
        _fail('placement_pin')
    xyz = p.get('wire_position')
    feet = p.get('feet_position')
    for vector in (xyz, feet):
        if (type(vector) is not list or len(vector) != 3 or
                any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 10000 for x in vector)):
            _fail('placement_finite')
    prov = p.get('provenance', {})
    if (p.get('scene') != SCENE or p.get('map_id') != MAP_ID or
            p.get('native_default_spawn_reproduced') is not False or
            prov.get('entry_type') != 'LevelLink' or prov.get('entry_object_index') != 4992 or
            prov.get('transform_object_index') != 5366 or prov.get('link_name') != 'NightArchiveEntrance' or
            prov.get('source_member_sha256') != MEMBER_SHA or
            prov.get('apk_archive_directory_sha256') != APK_DIRECTORY_SHA or
            prov.get('native_sha256') != NATIVE_SHA256 or
            prov.get('support', {}).get('triangle_id') != 1525 or
            prov.get('support', {}).get('normal_y') != 0.9999999970989094 or
            prov.get('native_collision_verified') is not False or
            prov.get('native_spawn_selector_reproduced') is not False or
            prov.get('wire_origin_offset') != 0.5 or xyz != [feet[0], feet[1]+0.5, feet[2]]):
        _fail('supported_entrance_policy')
    return xyz


def _scene_proof(p, m, observed, cache, wire):
    if type(p) is not dict or _sha(canonical_json(p)) != m['scene_provenance_sha256']:
        _fail('scene_provenance_pin')
    counts = p.get('wire_variable_group_counts')
    if type(counts) is not dict or any(type(v) is not int or v < 0 for v in counts.values()):
        _fail('scene_count_types')
    report_nv = p.get('netversion_u16')
    if type(report_nv) is not list or any(type(v) is not int for v in report_nv):
        _fail('scene_nv_types')
    phase = _PHASES.get(p.get('schema'))
    if phase is None or p.get('candidate_phase') != phase[0] or p.get('candidate_kind') != phase[1]:
        _fail('scene_partial_phase')
    for key in ('complete_scene_initialization', 'loadedSceneSnapshot', 'capture_generated', 'full_connection_ready', 'transport_used'):
        if p.get(key) is not False:
            _fail('scene_false_readiness_required')
    if 'loaded_scene_snapshot' in p and p['loaded_scene_snapshot'] is not False:
        _fail('scene_false_readiness_required')
    if (p.get('scene') != SCENE or p.get('map_id') != MAP_ID or
            p.get('resource_revision') != RESOURCE_REVISION or p.get('netversion_u16') != list(NV) or
            p.get('resource_tag_u16') != NV[0] or p.get('canonical_roundtrip') is not True or
            p.get('wire_sha256') != _sha(wire) or p.get('cache_sha256') != _sha(cache) or
            p.get('wire_bytes') != len(wire) or p.get('wire_variable_group_counts') != dict(observed._counts)):
        _fail('scene_report_binding')
    if (type(p.get('missing_scene_inputs')) is not list or not p['missing_scene_inputs'] or
            any(type(x) is not str or not x for x in p['missing_scene_inputs'])):
        _fail('scene_partial_gaps_required')
    prov = p.get('provenance', {})
    if (prov.get('native_sha256') != NATIVE_SHA256 or prov.get('apk_build') != 421589 or
            prov.get('scene_member_sha256') != MEMBER_SHA or
            prov.get('apk_archive_directory_sha256') != APK_DIRECTORY_SHA or
            prov.get('scene_member') != 'assets/Data/Levels/NightArchive/Objects.level.bin' or
            prov.get('runtime_capture') is not False or prov.get('native_record_comparison') is not False):
        _fail('scene_asset_provenance')


@dataclass(frozen=True, repr=False)
class AssetBootstrapContext:
    account_binding_hash: str = field(repr=False)
    scene: str
    resource_revision: str
    level_id: int
    manifest_sha256: str
    native_sha256: str = NATIVE_SHA256
    apk_build: int = 421589
    source_kind: str = 'saved_same_bot_capture_not_live_current'


@dataclass(frozen=True, repr=False)
class AssetBootstrapInputs:
    _context: AssetBootstrapContext = field(repr=False)
    _cache: bytes = field(repr=False)
    _wire: bytes = field(repr=False)
    _actor: bytes = field(repr=False)
    _manifest: bytes = field(repr=False)
    _source: CurrentInputs = field(repr=False)
    _source_metadata: bytes = field(repr=False)
    _scene_provenance: bytes = field(repr=False)
    _placement: bytes = field(repr=False)
    _actor_counts: tuple = field(repr=False)
    _seal: object = field(repr=False)

    def cache_bytes(self): return self._cache
    def scene_bytes(self): return self._wire
    def actor_bytes(self): return self._actor
    def __repr__(self): return '<AssetBootstrapInputs private experimental asset bootstrap; not capture>'
    def metadata(self):
        proof = json.loads(self._scene_provenance)
        return {'kind': KIND, 'classification': KIND, 'scene': self._context.scene,
                'candidate_phase': proof['candidate_phase'], 'scene_bytes': len(self._wire),
                'actor_bytes': len(self._actor), 'source_kind': self._context.source_kind,
                'source_current_live': False, 'destination_runtime_capture': False,
                'complete_scene_initialization': False, 'loaded_scene_snapshot': False,
                'capture_generated': False, 'full_connection_ready': False, 'trial_validated': False,
                'same_account_stored_source_revalidated': True, 'actor_unrelated_bits_preserved': True,
                'join_player_fields_policy': 'engineering_identity_matrix_flag0_w0_not_native_defaults',
                'native_default_spawn_reproduced': False, 'native_collision_verified': False,
                'resource_runtime_revision_reconciled': False,
                'missing_scene_inputs': list(proof['missing_scene_inputs']), 'transport_used_by_adapter': False}


def _validate(inputs, binding, scene, resource_revision, level_id, netversion, now_utc):
    if type(inputs) is not AssetBootstrapInputs or inputs._seal is not _SEAL or type(inputs._context) is not AssetBootstrapContext:
        _fail('inputs_type_or_seal')
    m = _gate(inputs._manifest, now_utc)
    c = inputs._context
    if (type(scene) is not str or scene != SCENE or resource_revision != RESOURCE_REVISION or
            type(level_id) is not int or level_id != MAP_ID or
            type(netversion) not in (tuple, list) or any(type(v) is not int for v in netversion) or tuple(netversion) != NV or
            c.scene != scene or c.resource_revision != resource_revision or c.level_id != level_id or
            c.native_sha256 != NATIVE_SHA256 or c.apk_build != 421589 or c.manifest_sha256 != _sha(inputs._manifest) or
            c.source_kind != 'saved_same_bot_capture_not_live_current' or
            not hmac.compare_digest(c.account_binding_hash, binding)):
        _fail('account_or_map_scope')
    for data, name in ((inputs._cache, 'cache_sha256'), (inputs._wire, 'scene_sha256'),
                       (inputs._actor, 'actor_sha256'), (inputs._source_metadata, 'source_metadata_sha256'),
                       (inputs._scene_provenance, 'scene_provenance_sha256'), (inputs._placement, 'placement_sha256')):
        if type(data) is not bytes or not hmac.compare_digest(_sha(data), m[name]):
            _fail('payload_pin_' + name)
    source_meta = json.loads(inputs._source_metadata)
    source_obs = _source(inputs._source, source_meta, binding, m)
    xyz = _placement(json.loads(inputs._placement), m)
    actor_obs = _actor(inputs._actor, level_id)
    if actor_obs._counts != inputs._actor_counts:
        _fail('actor_counts_changed')
    _preserved_actor(inputs._source.actor_bytes(), inputs._actor, source_obs, actor_obs, xyz)
    record = _record(inputs._cache, inputs._wire)
    observed = check_scene_record(record, resource_tag=NV[0], native_record_size=NV[5], account_hash=binding)
    _scene_proof(json.loads(inputs._scene_provenance), m, observed, inputs._cache, inputs._wire)
    return SceneBaseline(scene, resource_revision, level_id, NV[0], NV[5], binding, record, _SCENE_SEAL)


def validate_asset_bootstrap_inputs(inputs, *, account_uuid, scene, resource_revision, level_id, netversion, now_utc=None):
    """Explicit Engine branch; deliberately does not call capture validate_record."""
    return _validate(inputs, account_binding(account_uuid), scene, resource_revision, level_id, netversion, now_utc)


def build_asset_bootstrap_inputs(*, manifest, scene_cache, scene_wire, destination_actor,
        account_uuid, source_actor_sha256, source_metadata, placement, scene_provenance,
        verified_same_account_source, enabled=False, now_utc=None):
    if enabled is not True:
        _fail('explicit_trial_required')
    m = _gate(manifest, now_utc)
    if source_actor_sha256 != SOURCE_SHA or source_actor_sha256 != m['source_actor_sha256']:
        _fail('source_expected_pin')
    binding = account_binding(account_uuid)
    actor_obs = _actor(destination_actor, MAP_ID)
    inputs = AssetBootstrapInputs(AssetBootstrapContext(binding, SCENE, RESOURCE_REVISION, MAP_ID, _sha(manifest)),
        scene_cache, scene_wire, destination_actor, manifest, verified_same_account_source,
        canonical_json(source_metadata), canonical_json(scene_provenance), canonical_json(placement), actor_obs._counts, _SEAL)
    validate_asset_bootstrap_inputs(inputs, account_uuid=account_uuid, scene=SCENE,
        resource_revision=RESOURCE_REVISION, level_id=MAP_ID, netversion=NV, now_utc=now_utc)
    return inputs


def _freeze(value):
    if type(value) is dict: return MappingProxyType({key: _freeze(v) for key, v in value.items()})
    if type(value) is list: return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, repr=False)
class AssetBootstrapProfile(LoadedProfile):
    _asset_inputs: AssetBootstrapInputs = field(repr=False)
    def __repr__(self): return '<AssetBootstrapProfile private experimental asset bootstrap; not capture>'
    def __getattribute__(self, name):
        # build_from_cache accesses these inherited fields. Recheck the live gate
        # even if the Profile was prepared before revocation/deadline expiry.
        if name in ('record', 'cache', 'blob', 'binding', 'player_fields'):
            inputs = object.__getattribute__(self, '_asset_inputs')
            if type(inputs) is not AssetBootstrapInputs:
                _fail('profile_inputs_type')
            c = inputs._context
            _validate(inputs, c.account_binding_hash, c.scene, c.resource_revision, c.level_id, NV, None)
            if (object.__getattribute__(self, 'cache') != inputs._cache or
                    object.__getattribute__(self, 'blob') != inputs._wire or
                    object.__getattribute__(self, 'binding') != c.account_binding_hash or
                    object.__getattribute__(self, 'record') != _freeze(_profile_record(inputs))):
                _fail('profile_inherited_fields_changed')
        return super().__getattribute__(name)


def _profile_record(inputs):
    xyz = json.loads(inputs._placement)['wire_position']
    spawn = struct.pack('<4f', *xyz, 0.0)
    matrix = struct.pack('<16f', *(1.0 if row == col else 0.0 for row in range(4) for col in range(4)))
    if not all(math.isfinite(x) for x in struct.unpack('<20f', spawn+matrix)):
        _fail('join_player_fields_finite')
    record = {'schema': 1, 'kind': KIND, 'classification': KIND,
        'apk_build': 421589, 'native_sha256': NATIVE_SHA256, 'scene': SCENE,
        'resource_revision': RESOURCE_REVISION, 'level_id': MAP_ID, 'netversion_u16': list(NV),
        'cache': {'sha256': _sha(inputs._cache)}, 'actor_sha256': _sha(inputs._actor),
        'source_actor_sha256': SOURCE_SHA, 'source_capture_record_sha256': inputs._source._context.world_generation.record_sha256,
        'source_kind': 'saved_same_bot_capture_not_live_current',
        'manifest_sha256': inputs._context.manifest_sha256,
        'player_fields': {'spawn_hex': spawn.hex(), 'matrix_hex': matrix.hex(), 'level_flag': 0},
        'join_player_fields_policy': 'engineering_identity_matrix_flag0_w0_not_native_defaults',
        'candidate_phase': json.loads(inputs._scene_provenance)['candidate_phase'],
        'complete_scene_initialization': False, 'loaded_scene_snapshot': False,
        'capture_generated': False, 'full_connection_ready': False, 'trial_validated': False}
    return record


def build_asset_bootstrap_profile(inputs, *, account_uuid, now_utc=None):
    baseline = validate_asset_bootstrap_inputs(inputs, account_uuid=account_uuid, scene=SCENE,
        resource_revision=RESOURCE_REVISION, level_id=MAP_ID, netversion=NV, now_utc=now_utc)
    record = _profile_record(inputs)
    return AssetBootstrapProfile(_freeze(record), inputs._cache, inputs._wire, baseline._account_hash, inputs)
