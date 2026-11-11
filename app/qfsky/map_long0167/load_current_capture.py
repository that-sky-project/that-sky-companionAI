"""Strict private Scene+Actor record loader; no ADB/session/network access.

Revalidation of saved bytes is not evidence that the client remains in that
scene. Address zero and a StoredCaptureScope mark this as a non-live input.
The expected account binding must come from the caller's chosen account.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import struct

from . import current_inputs as core
from qfsky.join0167_profile import BINDING_ALGORITHM


@dataclass(frozen=True, repr=False)
class StoredCaptureScope:
    record_sha256: str = field(repr=False)
    captured_utc: str
    join_fields: tuple | None = field(default=None,repr=False)
    scene_observation: tuple | None = field(default=None,repr=False)


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('stored_capture_duplicate_json_key')
        out[key] = value
    return out


def _bounded_file(root, directory, name, maximum):
    if not re.fullmatch('[A-Za-z0-9_.-]+', name):
        raise ValueError('stored_capture_invalid_filename')
    path = directory/name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('stored_capture_file_outside_private_root')
    if path.stat().st_size > maximum:
        raise ValueError('stored_capture_file_size')
    with path.open('rb') as stream:
        data = stream.read(maximum+1)
    if len(data) > maximum:
        raise ValueError('stored_capture_file_size')
    return data


def load_current_capture(private_root, record_dir, *, expected_account_binding,
                         expected_scene, expected_resource_revision,
                         expected_level_id, expected_netversion):
    """Return CurrentInputs after binding/hash/codec revalidation.

    All expectations are explicit; no inferred account, Home fallback, map hash
    substitution, session lookup, profile rebinding or native address reuse.
    """
    if type(expected_account_binding) is not str or not re.fullmatch('[0-9a-f]{64}', expected_account_binding):
        raise ValueError('stored_capture_expected_account_binding')
    if (type(expected_scene) is not str or not re.fullmatch('[A-Za-z0-9_]+', expected_scene) or
        type(expected_resource_revision) is not str or not re.fullmatch('V[0-9]+', expected_resource_revision) or
        type(expected_level_id) is not int or not 0 < expected_level_id <= 0xffffffff):
        raise ValueError('stored_capture_expected_scene_scope')
    if (type(expected_netversion) not in (tuple, list) or len(expected_netversion) != 6 or
        any(type(v) is not int or not 0 <= v <= 65535 for v in expected_netversion)):
        raise ValueError('stored_capture_expected_netversion')
    root = Path(private_root).resolve()
    if (not root.is_dir() or type(record_dir) is not str or
        not re.fullmatch('[A-Za-z0-9_-]+', record_dir)):
        raise ValueError('stored_capture_private_location')
    directory = root/record_dir
    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
        raise ValueError('stored_capture_private_directory')
    evidence_path = directory/'capture_evidence.json'
    if (evidence_path.is_symlink() or not evidence_path.is_file() or
        not evidence_path.resolve().is_relative_to(root)):
        raise ValueError('stored_capture_incomplete_record')
    raw = _bounded_file(root, directory, 'record.json', 65536)
    try:
        record = json.loads(raw.decode('utf-8'), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('stored_capture_invalid_json') from None
    if type(record) is not dict:
        raise ValueError('stored_capture_record_object')
    if (type(record.get('schema')) is not int or record['schema'] != 1 or
        record.get('kind') != 'sky-current-scene-actor-pair' or
        record.get('classification') != 'private-current-account-session-state' or
        type(record.get('apk_build')) is not int or record['apk_build'] != 421629 or
        record.get('native_sha256') != core.NATIVE_SHA256):
        raise ValueError('stored_capture_current_native_required')
    binding = record.get('account_binding')
    if (type(binding) is not dict or binding.get('algorithm') != BINDING_ALGORITHM or
        binding.get('source') != 'native-join-builder-uid16' or
        type(binding.get('value')) is not str or
        not hmac.compare_digest(binding['value'], expected_account_binding)):
        raise ValueError('stored_capture_account_mismatch')
    nv = record.get('netversion_u16')
    if (record.get('scene') != expected_scene or record.get('resource_revision') != expected_resource_revision or
        type(record.get('level_id')) is not int or record['level_id'] != expected_level_id or
        type(nv) is not list or len(nv) != 6 or any(type(v) is not int for v in nv) or
        tuple(nv) != tuple(expected_netversion)):
        raise ValueError('stored_capture_scene_or_version_mismatch')
    checks = record.get('checks')
    true_fields = ('scene_current_schema_checked', 'actor_current_schema_checked',
        'scene_actor_same_account_map_revision', 'paired_equal_reads',
        'actor_map_prefix_matches_native_level_id', 'actual_current_wire_preserved')
    if (type(checks) is not dict or any(checks.get(key) is not True for key in true_fields) or
        record.get('native_build_and_root_validated') is not True):
        raise ValueError('stored_capture_missing_capture_checks')
    if record.get('atomic_snapshot_proven') is not False or record.get('runtime_full_wire_captured') is not False:
        raise ValueError('stored_capture_invalid_live_claim')
    captured = record.get('captured_utc')
    try:
        if type(captured) is not str or datetime.fromisoformat(captured).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('stored_capture_timestamp') from None
    declarations = record.get('files')
    limits = {'cache.bin': 0x2010, 'scene.bin': 0x2000, 'actor.bin': core.ACTOR_NATIVE_MAX}
    if type(declarations) is not dict or set(declarations) != set(limits):
        raise ValueError('stored_capture_payload_allowlist')
    data = {}
    for name, maximum in limits.items():
        declaration = declarations[name]
        if (type(declaration) is not dict or set(declaration) != {'size', 'sha256'} or
            type(declaration['size']) is not int or not 0 < declaration['size'] <= maximum or
            type(declaration['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', declaration['sha256'])):
            raise ValueError('stored_capture_payload_declaration')
        body = _bounded_file(root, directory, name, maximum)
        if len(body) != declaration['size'] or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), declaration['sha256']):
            raise ValueError('stored_capture_payload_hash')
        data[name] = body
    cache, scene, actor = (data[k] for k in ('cache.bin', 'scene.bin', 'actor.bin'))
    if len(cache) != 0x2010:
        raise ValueError('stored_capture_cache_extent')
    size = struct.unpack_from('<H', cache, 0x200e)[0]
    if not 4 <= size <= 0x2000 or cache[14:14+size] != scene:
        raise ValueError('stored_capture_cache_scene_disagree')
    level_record = core.LevelRecord(struct.unpack_from('<H', cache)[0], cache[2],
        struct.unpack_from('<I', cache, 4)[0], cache[8], struct.unpack_from('<H', cache, 10)[0],
        struct.unpack_from('<H', cache, 12)[0], scene)
    core.check_scene_record(level_record, resource_tag=nv[0], native_record_size=nv[5],
                            account_hash=expected_account_binding)
    observed = core.inspect_actor_wire(actor, verified_profile_sha256=core.NATIVE_SHA256,
        check_zero_padding=True, require_canonical_writer=True)
    if (not observed.matches_prefix_u32(expected_level_id) or
        core.reencode_actor_wire(observed, verified_profile_sha256=core.NATIVE_SHA256) != actor):
        raise ValueError('stored_capture_actor_map_or_roundtrip')
    timed = dict(observed._counts).get('timed_raw9_records', 0)
    for name, actual in (('scene_bytes', len(scene)), ('actor_bytes', len(actor)), ('actor_timed_record_count', timed)):
        if type(checks.get(name)) is not int or checks[name] != actual:
            raise ValueError('stored_capture_declared_counts_mismatch')
    # No native address is restored and no lifecycle epoch is invented.
    join_fields=None;scene_observation=None
    if 'player_fields' in record:
        if record.get('capture_variant')!='current-profile-player-pair-v1':
            raise ValueError('map_capture_join_capture_variant')
        player=record['player_fields']
        if (type(player)is not dict or set(player)!={'spawn_hex','matrix_hex','level_flag'} or
            type(player.get('spawn_hex'))is not str or not re.fullmatch('[0-9a-fA-F]{32}',player['spawn_hex']) or
            type(player.get('matrix_hex'))is not str or not re.fullmatch('[0-9a-fA-F]{128}',player['matrix_hex']) or
            type(player.get('level_flag'))is not int or not 0<=player['level_flag']<=255):
            raise ValueError('map_capture_join_fields_shape')
        if(checks.get('join_player_fields_same_capture')is not True or
           checks.get('join_player_fields_paired_equal_reads')is not True):
            raise ValueError('map_capture_join_fields_unverified')
        obs=record.get('scene_observation')
        if(type(obs)is not dict or obs.get('binding')!='adjacent-native-init-lines-exact-timestamp-pid-tid' or
           obs.get('scene')!=expected_scene or type(obs.get('level_id'))is not int or
           obs['level_id']!=expected_level_id or obs.get('netversion_u16')!=nv):
            raise ValueError('map_capture_join_scene_observation')
        join_fields=(bytes.fromhex(player['spawn_hex']),bytes.fromhex(player['matrix_hex']),player['level_flag'])
        scene_observation=(obs['binding'],expected_scene,expected_level_id,tuple(nv))
    scope = StoredCaptureScope(hashlib.sha256(raw).hexdigest(), captured,join_fields,scene_observation)
    context = core.CurrentContext(expected_account_binding, 0, scope, expected_scene,
                                  expected_resource_revision, expected_level_id)
    return core.CurrentInputs(context, cache, level_record, actor, tuple(nv), observed._counts)


def stored_capture_metadata(inputs):
    if (type(inputs) is not core.CurrentInputs or type(inputs._context) is not core.CurrentContext or
        type(inputs._context.world_generation) is not StoredCaptureScope or inputs._context.net_module_address != 0):
        raise ValueError('stored_capture_inputs_required')
    return {**inputs.metadata(), 'loaded_from_private_record': True,
        'live_native_context': False, 'offline_schema_revalidated': True,
        'native_address_restored': False, 'scope_is_native_lifecycle_counter': False,
        'capture_checks_refer_to_saved_record': True}
