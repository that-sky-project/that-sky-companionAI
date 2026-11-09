"""Read-only current Scene+Actor+Join-player capture, separate from frozen r4.

One fixed ADB batch reads both copies; paired Actor spans remain consecutive.
Optional bounded process pause changes scheduling only; no atomicity claim.
No session/ticket read, memory write, business request, launch, scene change
or automatic retry.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import time

from . import actor_capture as previous

native = previous.native


class PairConsistencyError(ValueError):
    """Bounded non-secret diagnostics; never retains reader, pointers or bytes."""
    def __init__(self, diagnostics):
        super().__init__('current_input_torn_or_changed')
        self.diagnostics = dict(diagnostics)


def paired_read_diagnostics(values, *, batch_elapsed_ms):
    """Report equality only, not any identifiers, hashes, positions or wire data."""
    a, b = values['a0'], values['a1']
    length_a, length_b = (struct.unpack_from('<I', value, 0xc)[0] for value in (a, b))
    valid_lengths = 1 <= length_a <= 0x528 and 1 <= length_b <= 0x528
    return {'actor_length_equal': length_a == length_b,
        'actor_lengths_in_native_bound': valid_lengths,
        'actor_body_equal': valid_lengths and length_a == length_b and
                            a[0x10:0x10+length_a] == b[0x10:0x10+length_b],
        'actor_tick_equal': a[0x2438:0x243c] == b[0x2438:0x243c],
        'scene_cache_equal': values['s0'] == values['s1'],
        'native_context_equal': values['q0'] == values['q1'],
        'world_generation_equal': values['g0'] == values['g1'],
        'account_binding_equal': values['u0'] == values['u1'],
        'level_id_equal': values['l0'] == values['l1'],
        'netversion_equal': values['v0'] == values['v1'],
        'actor_reads_consecutive': True, 'batch_read_count': 20,
        'batch_elapsed_ms': max(0, int(batch_elapsed_ms)),
        'actor_only_duration_measured': False, 'atomic_snapshot_proven': False}


def checked_player_fields(values, *, expected_netversion, expected_level_id):
    """Validate real paired native slices and return only required Join fields."""
    decoded = []
    for suffix in ('0', '1'):
        counts, prefix, state = (values[k + suffix] for k in ('j', 'i', 'f'))
        if len(counts) != 12 or len(prefix) != 0x28 or len(state) != 0x70:
            raise ValueError('current_profile_player_slice_length')
        count, local_id, first_id = (struct.unpack_from('<I', counts, 0)[0],
            counts[8:12], prefix[:4])
        if not 1 <= count <= 8 or first_id != local_id:
            raise ValueError('current_profile_local_player_slot')
        if state[:12] != expected_netversion:
            raise ValueError('current_profile_player_netversion')
        level, protected_a, protected_b = (
            struct.unpack_from('<I', state, off)[0] for off in (0x10, 0x20, 0x28))
        if not (level == (protected_a ^ 0x20221130) == (protected_b ^ 0x19260817) == expected_level_id):
            raise ValueError('current_profile_player_level')
        # Exactly the previously recovered native strlcpy16 policy; no stale stack padding.
        spawn_source = prefix[0x15:0x25]
        spawn = spawn_source.split(b'\0', 1)[0][:15].ljust(16, b'\0')
        decoded.append((count, local_id, spawn_source, state[:13], level, protected_a,
                        protected_b, state[0x30:0x70], spawn))
    if decoded[0] != decoded[1]:
        raise ValueError('current_profile_player_changed_during_capture')
    item = decoded[0]
    return {'spawn_hex': item[8].hex(), 'matrix_hex': item[7].hex(), 'level_flag': item[3][12]}


class PairedProfileReader(previous.PairedNativeReader):
    def __init__(self, d, game, n, level, cache, registry, connection, *,
                 account_hash, generation, expected_netversion, expected_level_id,
                 pause_during_pair_read=False):
        super().__init__(d, n, level, cache)
        self.game, self.registry, self.connection = game, registry, connection
        self.account_hash, self.generation = account_hash, generation
        self.expected_netversion, self.expected_level_id = expected_netversion, expected_level_id
        self.player_fields = None
        self.pair_diagnostics = None
        self.pause_during_pair_read = pause_during_pair_read

    def _collect(self):
        d, n, player = self.d, self.n, self.registry + 0x10
        if type(d.pid) is not int or not 0 < d.pid <= 2**31 - 1:
            raise ValueError('current_profile_pid')
        blocks = [('q0', n+0x20, 40), ('g0', self.game+0x99a0, 4),
                  ('u0', self.connection+0x380, 16), ('j0', self.registry+0x2e510, 12),
                  ('i0', player, 0x28), ('f0', player+0x5c30, 0x70),
                  ('l0', self.level+0x9a10, 4), ('v0', n+0x99b78, 12),
                  ('s0', self.cache+0xc, 0x2010), ('a0', n+0x48, self.ACTOR_SPAN),
                  ('a1', n+0x48, self.ACTOR_SPAN), ('s1', self.cache+0xc, 0x2010),
                  ('f1', player+0x5c30, 0x70), ('i1', player, 0x28),
                  ('j1', self.registry+0x2e510, 12), ('u1', self.connection+0x380, 16),
                  ('g1', self.game+0x99a0, 4), ('q1', n+0x20, 40),
                  ('l1', self.level+0x9a10, 4), ('v1', n+0x99b78, 12)]
        commands = []
        for label, address, size in blocks:
            if (type(address) is not int or not 0 < address < 2**64-size or
                not 0 < size <= 16384 or not any(m['lo'] <= address and address+size <= m['hi']
                    and 'r' in m['flags'] for m in d.maps)):
                raise ValueError('current_profile_unreadable_batch_region')
            commands.append(f'dd if=/proc/{d.pid}/mem bs=1 skip={address} count={size} 2>/dev/null')
        batch_started = time.monotonic()
        if self.pause_during_pair_read:
            from .pause import read_paused_pair
            out = bytearray(read_paused_pair(d, commands, sum(size for _, _, size in blocks)))
        else:
            out = bytearray(d.call(['exec-out', "su -c '" + ' && '.join(commands) + "'"]))
        batch_elapsed_ms = (time.monotonic() - batch_started) * 1000
        if len(out) != sum(size for _, _, size in blocks):
            out[:] = bytes(len(out))
            raise RuntimeError('current_profile_short_batch_read')
        values, cursor = {}, 0
        try:
            for label, address, size in blocks:
                piece = out[cursor:cursor+size]
                if label.startswith('u'):
                    try:
                        if not any(piece):
                            raise ValueError('current_profile_empty_account')
                        values[label] = native.account_binding(bytes(piece))
                        # No raw UID hash or standalone UID is stored in evidence.
                        d.reads.append({'label': 'current_profile_'+label, 'address': hex(address),
                            'size': size, 'domain_binding_checked_only': True})
                    finally:
                        piece[:] = bytes(len(piece))
                        out[cursor:cursor+size] = bytes(size)
                else:
                    values[label] = bytes(piece)
                    d.reads.append({'label': 'current_profile_'+label, 'address': hex(address),
                        'size': size, 'sha256': hashlib.sha256(piece).hexdigest()})
                cursor += size
        finally:
            out[:] = bytes(len(out))
        self.pair_diagnostics = paired_read_diagnostics(values, batch_elapsed_ms=batch_elapsed_ms)
        for suffix in ('0', '1'):
            q = struct.unpack('<5Q', values['q'+suffix])
            if (q[0], q[1], q[2], q[4]) != (self.registry, self.connection, self.level, self.cache):
                raise ValueError('current_profile_native_context_changed')
            if struct.unpack('<I', values['g'+suffix])[0] != self.generation:
                raise ValueError('current_profile_world_generation_changed')
            if values['u'+suffix] != self.account_hash:
                raise ValueError('current_profile_account_changed')
            values['p'+suffix] = values['q'+suffix][16:40]
        self.player_fields = checked_player_fields(values,
            expected_netversion=self.expected_netversion, expected_level_id=self.expected_level_id)
        self.values = values


def collect(a, *, validator=None, core=None):
    core = core or previous.load_core(getattr(a, 'source', None))
    d, bias, game, n, nv_raw, observation = (validator or native.validated_context)(a)
    nv = list(struct.unpack('<6H', nv_raw))
    account_hash = previous.binding_now(d, n)
    registry = d.pointer(n+0x20, 'profile_player_registry')
    connection = d.pointer(n+0x28, 'profile_join_context')
    level = d.pointer(n+0x30, 'profile_level_context')
    cache = d.pointer(n+0x40, 'profile_level_cache')
    generation = struct.unpack('<I', d.read(game+0x99a0, 4, 'profile_world_generation'))[0]
    scope = object()

    def context():
        if (d.pointer(game+0x7ec0, 'profile_current_net_module') != n or
            d.pointer(n+0x20, 'profile_current_registry') != registry or
            d.pointer(n+0x28, 'profile_current_connection') != connection or
            d.pointer(n+0x30, 'profile_current_level') != level or
            d.pointer(n+0x40, 'profile_current_cache') != cache):
            raise ValueError('current_profile_native_context_changed')
        current_id = struct.unpack('<I', d.read(level+0x9a10, 4, 'profile_current_level_id'))[0]
        current_generation = struct.unpack('<I', d.read(game+0x99a0, 4, 'profile_current_generation'))[0]
        if current_id != observation['level_id'] or current_generation != generation:
            raise ValueError('current_profile_world_generation_changed')
        return core.CurrentContext(previous.binding_now(d, n), n, scope, a.scene,
                                   a.resource_revision, current_id)

    paired = PairedProfileReader(d, game, n, level, cache, registry, connection,
        account_hash=account_hash, generation=generation, expected_netversion=nv_raw,
        expected_level_id=observation['level_id'],
        pause_during_pair_read=getattr(a, 'pause_during_pair_read', False))
    try:
        inputs = core.capture_current_inputs(paired, context,
            expected_account_binding=account_hash, expected_scene=a.scene,
            expected_resource_revision=a.resource_revision, expected_netversion=nv)
    except ValueError as error:
        if str(error) == 'current_input_torn_or_changed' and paired.pair_diagnostics is not None:
            raise PairConsistencyError(paired.pair_diagnostics) from None
        raise
    if paired.player_fields is None:
        raise ValueError('current_profile_player_fields_not_read')
    data = {'cache.bin': inputs.cache_bytes(), 'scene.bin': inputs.scene_bytes(),
            'actor.bin': inputs.actor_bytes()}
    checks = inputs.metadata()
    checks.update(capture_backend_origin_attested_by_helper=False,
        join_player_fields_same_capture=True, join_player_fields_paired_equal_reads=True,
        native_world_generation_paired_equal_reads=True, account_binding_in_batch_paired=True,
        bounded_process_pause_used=paired.pause_during_pair_read,
        host_resume_confirmed_after_pair=paired.pause_during_pair_read)
    record = {'schema': 1, 'kind': 'sky-current-scene-actor-pair',
        'capture_variant': 'current-profile-player-pair-v1',
        'classification': 'private-current-account-session-state', 'apk_build': native.BUILD,
        'native_sha256': native.NATIVE_SHA256, 'resource_revision': a.resource_revision,
        'resource_revision_source': 'explicit-operator-input-not-read-from-memory',
        'scene': a.scene, 'level_id': observation['level_id'], 'netversion_u16': nv,
        'captured_utc': datetime.now(timezone.utc).isoformat(),
        'account_binding': {'algorithm': native.BINDING_ALGORITHM, 'value': account_hash,
                            'source': 'native-join-builder-uid16'},
        'files': {name: {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                  for name, body in data.items()}, 'checks': checks,
        'player_fields': paired.player_fields,
        'spawn_padding': 'strlcpy16-canonical-zero-padding; native trailing stack padding unspecified',
        'scene_observation': {'binding': observation['scene_binding'],
            'native_timestamp': observation['native_timestamp'], 'scene': a.scene,
            'level_id': observation['level_id'], 'netversion_u16': nv},
        'native_build_and_root_validated': True, 'runtime_full_wire_captured': False,
        'custom_service_join_verified': False, 'native_presence_flags_verified': False,
        'atomic_snapshot_proven': False}
    evidence = {'pid': d.pid, 'library_bias': hex(bias), 'reads': d.reads,
        'native_sha256': native.NATIVE_SHA256, 'memory_write': False,
        'uid_standalone_saved': False, 'session_or_ticket_read': False,
        'payload_may_contain_identity_and_position': True,
        'paired_reader': 'one-command-twenty-reads-consecutive-actor-spans',
        'scope_generation': 'Game+99a0-load-attempt-counter-same-before-batch-after',
        'paired_read_diagnostics': paired.pair_diagnostics,
        'pause_watchdog_seconds': 2 if paired.pause_during_pair_read else 0,
        'pause_duration_separately_measured': False,
        'world_load_late_observed': False}
    data.update({'record.json': previous._json(record), 'capture_evidence.json': previous._json(evidence)})
    public = {'schema': 1, 'apk_build': native.BUILD, 'resource_revision': a.resource_revision,
        'scene': a.scene, 'level_id': observation['level_id'], 'netversion_u16': nv,
        'cache_length': len(data['cache.bin']), 'scene_length': len(data['scene.bin']),
        'actor_length': len(data['actor.bin']), 'native_instructions_and_vtable_verified': True,
        'account_binding_saved_privately': True, 'device_memory_write': False,
        'session_or_ticket_read': False, 'custom_service_join_verified': False,
        'world_load_late_observed': False, **checks}
    return data, public


def capture(a, *, validator=None, core=None):
    root = Path(a.private_root).resolve()
    if not root.is_dir() or not re.fullmatch('[A-Za-z0-9_-]+', a.record_dir):
        raise ValueError('current_profile_private_destination')
    if (root/a.record_dir).exists():
        raise FileExistsError('current_profile_record_already_exists')
    if getattr(a, 'summary_out', None) and Path(a.summary_out).resolve().is_relative_to(root):
        raise ValueError('current_profile_summary_must_be_outside_private_root')
    data, public = collect(a, validator=validator, core=core)
    native.commit_private_record(root, a.record_dir, data)
    public['private_record_committed'] = True
    if getattr(a, 'summary_out', None):
        try:
            native.atomic_bytes(a.summary_out, previous._json(public))
            public['summary_file_written'] = True
        except OSError as error:
            public['summary_file_written'] = False
            public['summary_write_error_class'] = type(error).__name__
    return public


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for option in ('adb', 'native-library', 'private-root'):
        p.add_argument('--'+option, type=Path, required=True)
    for option in ('serial', 'record-dir', 'scene', 'resource-revision'):
        p.add_argument('--'+option, required=True)
    p.add_argument('--source', type=Path)
    p.add_argument('--summary-out', type=Path)
    p.add_argument('--pause-during-pair-read', action='store_true',
        help='Opt-in: pause only the validated MuMu Sky PID around the paired read, with 2s rescue watchdog.')
    a = p.parse_args(argv)
    if not re.fullmatch('[A-Za-z0-9_]+', a.scene) or not re.fullmatch('V[0-9]+', a.resource_revision):
        p.error('invalid scene or resource revision')
    try:
        public = capture(a)
    except Exception as error:
        failure = {'ok': False, 'error_class': type(error).__name__,
            'error_code': str(error) if re.fullmatch('current_[a-z_]+', str(error)) else 'redacted_failure',
            'capture_failed': True, 'private_record_committed': False,
            'details_redacted': True, 'network_used': False}
        if type(error) is PairConsistencyError:
            failure['paired_read_diagnostics'] = error.diagnostics
        print(json.dumps(failure))
        return 1
    print(json.dumps(public, indent=2))
    return 2 if public.get('summary_file_written') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
