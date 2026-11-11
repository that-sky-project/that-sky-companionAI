"""Read-only build421629 current Scene + own Actor collector.

Device access occurs only in capture()/main(), never on import. Payloads and
account binding stay in a new private record; stdout is a redacted summary.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

from . import native_cache as native  # noqa: E402


def load_core(source=None):
    source = Path(source) if source else APP
    if not (source / 'qfsky/map_long0167/current_inputs.py').is_file():
        raise ValueError('current_actor_source_not_found_use_source')
    if str(source.resolve()) not in sys.path:
        sys.path.insert(0, str(source.resolve()))
    from qfsky.map_long0167 import current_inputs
    return current_inputs


def binding_now(d, n):
    c = d.pointer(n + 0x28, 'actor_join_context')
    raw = bytearray(d.read(c + 0x380, 16, 'actor_uid_domain_binding_only', digest=False))
    try:
        if len(raw) != 16 or not any(raw):
            raise ValueError('current_actor_empty_account')
        return native.account_binding(bytes(raw))
    finally:
        raw[:] = bytes(len(raw))


class PairedNativeReader:
    """One bounded ADB command, ten actual reads; no stop-the-world claim.

    Consecutive whole Actor writer reads include len, body and tick. The pure
    checker receives slices of the corresponding first/second real reads.
    All pointer values must still match before/after. No arbitrary shell text.
    """
    ACTOR_START = 0x48
    ACTOR_SPAN = 0x2484 - ACTOR_START

    def __init__(self, d, n, level, cache):
        self.d, self.n, self.level, self.cache = d, n, level, cache
        self.values = None
        self.calls = {}

    def _collect(self):
        d, n, level, cache = self.d, self.n, self.level, self.cache
        if type(d.pid) is not int or not 0 < d.pid <= 2**31 - 1:
            raise ValueError('current_actor_pid')
        blocks = [('p0', n+0x30, 24), ('l0', level+0x9a10, 4), ('v0', n+0x99b78, 12),
                  ('s0', cache+0xc, 0x2010), ('a0', n+0x48, self.ACTOR_SPAN),
                  ('a1', n+0x48, self.ACTOR_SPAN), ('s1', cache+0xc, 0x2010),
                  ('p1', n+0x30, 24), ('l1', level+0x9a10, 4), ('v1', n+0x99b78, 12)]
        commands = []
        for label, address, size in blocks:
            if (type(address) is not int or not 0 < address < 2**64-size or
                not 0 < size <= 16384 or not any(m['lo'] <= address and address+size <= m['hi']
                    and 'r' in m['flags'] for m in d.maps)):
                raise ValueError('current_actor_unreadable_batch_region')
            commands.append(f'dd if=/proc/{d.pid}/mem bs=1 skip={address} count={size} 2>/dev/null')
        # PID, address and count are strict bounded integers; shell grammar fixed.
        out = d.call(['exec-out', "su -c '" + ' && '.join(commands) + "'"])
        if len(out) != sum(size for _, _, size in blocks):
            raise RuntimeError('current_actor_short_batch_read')
        values, cursor = {}, 0
        for label, address, size in blocks:
            values[label] = out[cursor:cursor+size]
            cursor += size
            d.reads.append({'label': 'actor_pair_'+label, 'address': hex(address),
                            'size': size, 'sha256': hashlib.sha256(values[label]).hexdigest()})
        self.values = values

    def __call__(self, address, size):
        if self.values is None:
            self._collect()
        ordinal = self.calls.get(address, 0)
        if ordinal > 1:
            raise ValueError('current_actor_unexpected_third_read')
        self.calls[address] = ordinal + 1
        suffix = str(ordinal)
        n = self.n
        if address == n+0x30 and size == 8:
            return self.values['p'+suffix][:8]
        if address == n+0x40 and size == 8:
            return self.values['p'+suffix][16:24]
        if address == self.level+0x9a10 and size == 4:
            return self.values['l'+suffix]
        if address == n+0x99b78 and size == 12:
            return self.values['v'+suffix]
        if address == self.cache+0xc and size == 0x2010:
            return self.values['s'+suffix]
        if ((address == n+0x2480 or address == n+0x54) and size == 4 or
            address == n+0x58 and type(size) is int and 1 <= size <= 0x528):
            offset = address-(n+self.ACTOR_START)
            return self.values['a'+suffix][offset:offset+size]
        raise ValueError('current_actor_unexpected_core_read')


def _json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False)+'\n').encode('utf-8')


def collect(a, *, validator=None, core=None):
    """Validate and collect without writing; injectable only for offline tests."""
    core = core or load_core(getattr(a, 'source', None))
    d, bias, game, n, nv_raw, observation = (validator or native.validated_context)(a)
    nv = list(struct.unpack('<6H', nv_raw))
    account_hash = binding_now(d, n)
    level = d.pointer(n+0x30, 'actor_level_context')
    cache = d.pointer(n+0x40, 'actor_level_cache')
    scope = object()

    def context():
        current_n = d.pointer(game+0x7ec0, 'actor_current_net_module')
        if current_n != n:
            raise ValueError('current_actor_net_module_changed')
        current_level = d.pointer(n+0x30, 'actor_current_level_context')
        current_id = struct.unpack('<I', d.read(current_level+0x9a10, 4, 'actor_current_level_id'))[0]
        if current_level != level or current_id != observation['level_id']:
            raise ValueError('current_actor_level_context_changed')
        return core.CurrentContext(binding_now(d, n), n, scope, a.scene,
                                   a.resource_revision, current_id)

    paired = PairedNativeReader(d, n, level, cache)
    inputs = core.capture_current_inputs(paired, context,
        expected_account_binding=account_hash, expected_scene=a.scene,
        expected_resource_revision=a.resource_revision, expected_netversion=nv)
    data = {'cache.bin': inputs.cache_bytes(), 'scene.bin': inputs.scene_bytes(),
            'actor.bin': inputs.actor_bytes()}
    checks = inputs.metadata()
    # The backend is validated here, not attested by the independent pure helper.
    checks['capture_backend_origin_attested_by_helper'] = False
    record = {'schema': 1, 'kind': 'sky-current-scene-actor-pair',
        'classification': 'private-current-account-session-state', 'apk_build': native.BUILD,
        'native_sha256': native.NATIVE_SHA256, 'resource_revision': a.resource_revision,
        'resource_revision_source': 'explicit-operator-input-not-read-from-memory',
        'scene': a.scene, 'level_id': observation['level_id'], 'netversion_u16': nv,
        'captured_utc': datetime.now(timezone.utc).isoformat(),
        'account_binding': {'algorithm': native.BINDING_ALGORITHM, 'value': account_hash,
                            'source': 'native-join-builder-uid16'},
        'files': {name: {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                  for name, body in data.items()}, 'checks': checks,
        'scene_observation': {'binding': observation['scene_binding'],
                              'native_timestamp': observation['native_timestamp']},
        'native_build_and_root_validated': True, 'runtime_full_wire_captured': False,
        'custom_service_join_verified': False, 'native_presence_flags_verified': False,
        'atomic_snapshot_proven': False}
    evidence = {'pid': d.pid, 'library_bias': hex(bias), 'reads': d.reads,
        'native_sha256': native.NATIVE_SHA256, 'memory_write': False,
        'uid_standalone_saved': False, 'payload_may_contain_identity_and_position': True,
        'paired_reader': 'one-command-ten-reads-two-consecutive-actor-writer-spans',
        'scope_generation': 'same-observed-native-pointers-only-not-lifecycle-counter'}
    data.update({'record.json': _json(record), 'capture_evidence.json': _json(evidence)})
    public = {'schema': 1, 'apk_build': native.BUILD, 'resource_revision': a.resource_revision,
        'scene': a.scene, 'level_id': observation['level_id'], 'netversion_u16': nv,
        'cache_length': len(data['cache.bin']), 'scene_length': len(data['scene.bin']),
        'actor_length': len(data['actor.bin']), 'native_instructions_and_vtable_verified': True,
        'account_binding_saved_privately': True, 'device_memory_write': False,
        'custom_service_join_verified': False, **checks}
    return data, public


def capture(a, *, validator=None, core=None):
    # Refuse bad destinations before opening any device. commit_private_record
    # repeats the no-overwrite validation at the actual atomic commit boundary.
    root = Path(a.private_root).resolve()
    if not root.is_dir() or not re.fullmatch('[A-Za-z0-9_-]+', a.record_dir):
        raise ValueError('current_actor_private_destination')
    if (root/a.record_dir).exists():
        raise FileExistsError('current_actor_private_record_already_exists')
    if getattr(a, 'summary_out', None) and Path(a.summary_out).resolve().is_relative_to(root):
        raise ValueError('current_actor_summary_must_be_outside_private_root')
    data, public = collect(a, validator=validator, core=core)
    native.commit_private_record(root, a.record_dir, data)
    public['private_record_committed'] = True
    if getattr(a, 'summary_out', None):
        try:
            native.atomic_bytes(a.summary_out, _json(public))
            public['summary_file_written'] = True
        except OSError as error:
            public['summary_file_written'] = False
            public['summary_write_error_class'] = type(error).__name__
    return public


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adb', type=Path, required=True)
    p.add_argument('--serial', required=True)
    p.add_argument('--native-library', type=Path, required=True)
    p.add_argument('--private-root', type=Path, required=True)
    p.add_argument('--record-dir', required=True)
    p.add_argument('--scene', required=True)
    p.add_argument('--resource-revision', required=True)
    p.add_argument('--source', type=Path, help='directory containing qfsky')
    p.add_argument('--summary-out', type=Path)
    a = p.parse_args(argv)
    if not re.fullmatch('[A-Za-z0-9_]+', a.scene) or not re.fullmatch('V[0-9]+', a.resource_revision):
        p.error('invalid scene or resource revision')
    try:
        public = capture(a)
    except Exception as error:
        # Never emit exception details: a codec could include private token data.
        print(json.dumps({'ok': False, 'error_class': type(error).__name__,
            'error_code': str(error) if re.fullmatch('current_[a-z_]+', str(error)) else 'redacted_failure',
            'capture_failed': True, 'details_redacted': True, 'network_used': False}))
        return 1
    print(json.dumps(public, indent=2))
    return 2 if public.get('summary_file_written') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
