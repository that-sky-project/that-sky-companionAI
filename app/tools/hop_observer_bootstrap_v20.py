"""Opt-in startup-only passive observation service; never sends game traffic.

The public installer derives the managed case from THIS module's location. Arm
files contain no target or destination: the current controller target and a
fresh private scope are the only source. All disk work runs on this background
service; the networking hook is exactly PassiveHopSnapshot.
"""
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from passive_hop_snapshot_v20 import PassiveHopSnapshot, _binding

DURATION = 20.0
SAFETY_SECONDS = 3.0
POSITION_MAX_AGE = 2.0
CONTROL_MAX_AGE = 120.0
ARM_TTL = 10.0
RELATIVE_DIRECTORY = Path('research/integration/mesh-native-traversal-v20/passive-hop-observer')
ARM_FIELDS = {'schema', 'request_id', 'scope_token', 'issued_unix', 'expires_unix'}
DEPLOYED_RELEASE = 'release-v20-observer'
DEPLOYED_RELEASES = frozenset((DEPLOYED_RELEASE, 'release-v20-hop-trial'))


def managed_layout(module_file):
    """Only the verified local layout and explicitly named managed releases."""
    module_root = Path(module_file).resolve().parents[1]
    if module_root.name in DEPLOYED_RELEASES:
        _need(module_root.parent.name == 'terrain-experimental-20261104', 'managed_case_layout')
        return module_root.parent, module_root / 'bot/room_pose.py'
    expected = module_root / 'stage-pilot-candidate-v1/bot/room_pose.py'
    _need(expected.is_file(), 'local_case_layout')
    return module_root, expected


class Rejected(ValueError):
    pass


def _need(value, code):
    if not value:
        raise Rejected(code)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _read_json(path, maximum=65536):
    """No symlinks, bounded reads, no private values in errors."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    _need(not path.is_symlink(), 'file_symlink')
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        _need(stat.S_ISREG(st.st_mode) and 0 < st.st_size <= maximum, 'file_budget')
        if hasattr(os, 'getuid'):
            _need(st.st_uid == os.getuid() and not st.st_mode & 0o022, 'file_permissions')
        raw = os.read(fd, maximum + 1)
        _need(len(raw) <= maximum, 'file_budget')
        value = json.loads(raw)
        _need(type(value) is dict, 'file_object')
        return value, (st.st_ino, st.st_mtime_ns, st.st_size)
    finally:
        os.close(fd)


def _atomic(path, value):
    # Only controlled names under the managed service directory reach here.
    _need(not path.is_symlink(), 'output_symlink')
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
    _need(len(raw) <= 512 * 1024, 'output_budget')
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _process_identity():
    pid = os.getpid()
    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(') ', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(fields[19]), 'pgid': int(fields[2]), 'session': int(fields[3])}


class _Service:
    """Private dependency-injectable harness. Production uses install_from_context."""
    def __init__(self, room_pose, ctl, ground_hook, case, *, clock=time.monotonic,
                 wall=time.time, identity=None, start_thread=True):
        self.room_pose, self.ctl, self.ground_hook = room_pose, ctl, ground_hook
        self.case = Path(case).resolve(strict=True)
        self.directory = self.case / RELATIVE_DIRECTORY
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _need(self.directory.resolve() == self.directory and not self.directory.is_symlink(), 'directory_binding')
        if hasattr(os, 'getuid'):
            _need(self.directory.stat().st_uid == os.getuid(), 'directory_owner')
            os.chmod(self.directory, 0o700)
        self.arm_path, self.status_path = self.directory / 'arm.json', self.directory / 'status.json'
        self.clock, self.wall = clock, wall
        self.identity = _process_identity() if identity is None else dict(identity)
        self.installed_wall = wall()
        self.stop = threading.Event()
        self.thread = None
        self.active = None
        self.scope = self.scope_token = None
        self.request_id = None
        self.seen = set()
        self.status = None
        self._last_json = None
        self._closed = False
        self._tick_lock = threading.RLock()
        self._publish('waiting', reason='awaiting_current_control_target')
        if start_thread:
            self.thread = threading.Thread(target=self._run, daemon=True, name='passive-hop-observer-v20')
            self.thread.start()

    def _publish(self, state, **values):
        result = {'schema': 'passive-hop-observer-status-v20', 'state': state,
                  'scope_token': self.scope_token, 'duration_seconds': DURATION,
                  'installed_unix': self.installed_wall,
                  'identity_exported': False, 'raw_actor_exported': False, **values}
        encoded = json.dumps(result, sort_keys=True)
        if encoded != self._last_json:
            _atomic(self.status_path, result)
            self._last_json, self.status = encoded, result

    def _lease(self):
        matches = []
        paths = list(self.case.glob('online-v*/*.lease.json'))
        _need(len(paths) <= 64, 'lease_scan_budget')
        for path in paths:
            if path.resolve().parent.parent != self.case or path.parent.is_symlink():
                continue
            try:
                item, _ = _read_json(path)
                runner = item.get('runner', {})
                if all(runner.get(key) == value for key, value in self.identity.items()):
                    matches.append((path, item))
            except (OSError, ValueError, TypeError):
                continue
        _need(len(matches) == 1, 'lease_not_unique')
        path, lease = matches[0]
        _need(lease.get('status') == 'running' and lease.get('connected_timer_started') is True, 'lease_not_running')
        mono, wall = lease.get('deadline_monotonic'), lease.get('deadline_unix')
        _need(_number(mono) and _number(wall), 'lease_deadline')
        remaining = min(mono - self.clock(), wall - self.wall(), self.ground_hook.deadline - self.clock())
        return (str(path), mono, wall), remaining

    def _context(self):
        _need(not self.ctl.stop.is_set() and not self.ground_hook._closed, 'runner_stopping')
        lease, remaining = self._lease()
        _need(remaining > 0, 'session_expired')
        command, stamp = _read_json(Path(self.ctl.path))
        target = command.get('target')
        _need(type(target) in (str, int) and target != '' and len(str(target)) <= 128, 'control_target_missing')
        age = self.wall() - stamp[1] / 1e9
        _need(-2 <= age <= CONTROL_MAX_AGE, 'control_target_expired')
        tracker = self.ctl.tracker
        binding = _binding(tracker, target)
        _need(binding[1] is not None and type(binding[4]) is int, 'receiver_or_map_missing')
        _need(tracker.lock.acquire(blocking=False), 'tracker_busy')
        try:
            seen = tracker.position_observed.get(binding[5])
            origin = tracker.origins.get(binding[5])
            _need(isinstance(seen, (tuple, list)) and len(seen) == 2 and all(_number(n) for n in seen), 'position_missing')
            _need(-.05 <= self.clock() - seen[1] <= POSITION_MAX_AGE and
                  -2 <= self.wall() - seen[0] <= POSITION_MAX_AGE, 'position_expired')
            _need(isinstance(origin, (tuple, list)) and len(origin) >= 3 and
                  all(_number(n) and abs(n) < 10000 for n in origin[:3]), 'position_invalid')
        finally:
            tracker.lock.release()
        _need(_binding(tracker, target) == binding, 'binding_changed')
        # All identifying elements remain process-private, never written/logged.
        return (binding, target, stamp, lease), remaining

    def _take_arm(self):
        if not self.arm_path.exists() and not self.arm_path.is_symlink():
            return None
        value, stamp = _read_json(self.arm_path, 2048)
        current = self.arm_path.lstat()
        _need((current.st_ino, current.st_mtime_ns, current.st_size) == stamp, 'arm_replaced')
        self.arm_path.unlink()  # Consume once; never archive user-provided bytes.
        _need(set(value) == ARM_FIELDS and value.get('schema') == 'passive-hop-arm-v20', 'arm_fields')
        nonce = value.get('request_id')
        _need(type(nonce) is str and re.fullmatch('[0-9a-f]{32}', nonce), 'arm_request_id')
        _need(nonce not in self.seen and len(self.seen) < 256, 'arm_replay_or_budget')
        self.seen.add(nonce)
        issue, expiry = value.get('issued_unix'), value.get('expires_unix')
        _need(_number(issue) and _number(expiry) and self.installed_wall <= issue <= self.wall() + .1 and
              issue <= self.wall() <= expiry and 0 < expiry - issue <= ARM_TTL, 'arm_expired')
        _need(value.get('scope_token') == self.scope_token and self.scope_token is not None, 'arm_scope_changed')
        return nonce

    def _finish(self, reason):
        observer = self.active
        if observer is None:
            return
        # Detach BEFORE writing, including failures or a silent network.
        observer.detach(self.room_pose.SNAPSHOT_HOOKS)
        with observer.lock:
            if observer.stop_reason is None:
                observer.stop_reason = reason
        report = observer.finish()
        self.active = None
        output_name = 'sample-' + self.request_id + '.json'
        _atomic(self.directory / output_name, report)
        self._publish('complete', request_id=self.request_id, reason=report['stop_reason'],
                      sample_count=report['count'], output_file=output_name)

    def tick(self):
        """One background iteration, also used by deterministic local tests."""
        with self._tick_lock:
            return self._tick()

    def _tick(self):
        if self._closed:
            return
        if self.stop.is_set() or self.ctl.stop.is_set() or self.ground_hook._closed:
            self._finish('runner_stopping')
            self._closed = True
            return
        try:
            if self.active is not None and self.clock() - self.active.started >= DURATION:
                self._finish('duration_complete')
                return
            current, remaining = self._context()
            if self.active is not None:
                if current != self.scope:
                    self._finish('scope_changed')
                    self.scope = self.scope_token = None
                    return
                with self.active.lock:
                    elapsed = self.clock() - self.active.started
                    stopped, count = self.active.stop_reason, len(self.active.rows)
                if stopped or elapsed >= DURATION or remaining <= SAFETY_SECONDS:
                    self._finish(stopped or ('duration_complete' if elapsed >= DURATION else 'session_ending'))
                else:
                    self._publish('recording', request_id=self.request_id, map_id=self.scope[0][4],
                                  started_unix=self.started_wall, sample_count=count)
                return
            if current != self.scope:
                self.scope, self.scope_token = current, uuid.uuid4().hex
                self._publish('ready', map_id=current[0][4])
            nonce = self._take_arm()
            if nonce is None:
                return
            _need(remaining >= DURATION + SAFETY_SECONDS, 'insufficient_session_time')
            observer = PassiveHopSnapshot(self.ctl.tracker, current[1], self.room_pose.anim_pose,
                                          duration=DURATION, maximum_hz=10, clock=self.clock)
            observer.arm()
            checked, fresh_remaining = self._context()
            _need(checked == current and observer.binding == current[0], 'scope_changed_during_arm')
            _need(fresh_remaining >= DURATION + SAFETY_SECONDS, 'insufficient_session_time')
            _need(not self.stop.is_set() and not self.ctl.stop.is_set(), 'runner_stopping')
            self.active, self.request_id = observer, nonce
            self.started_wall = self.wall()
            self.room_pose.SNAPSHOT_HOOKS.append(observer)
            self._publish('recording', request_id=nonce, map_id=current[0][4],
                          started_unix=self.started_wall, sample_count=0)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, ValueError) and str(exc) in ('binding_busy', 'tracker_busy'):
                return
            # Error text from filesystem/JSON/codec may include private data.
            reason = str(exc) if isinstance(exc, Rejected) else 'observer_input_unavailable'
            if self.active is not None:
                self._finish('scope_or_session_unavailable')
            self.scope = self.scope_token = None
            self._publish('rejected', reason=reason)

    def _run(self):
        try:
            while not self._closed and not self.stop.wait(.1):
                self.tick()
        except Exception:
            if self.active is not None:
                self.active.detach(self.room_pose.SNAPSHOT_HOOKS)
            # No raw exception or identities enter the existing game logger.
            self.ctl.log('passive_hop_observer_failed', reason='background_storage_or_state_failure')
        finally:
            with self._tick_lock:
                try:
                    self._finish('observer_shutdown')
                except Exception:
                    if self.active is not None:
                        self.active.detach(self.room_pose.SNAPSHOT_HOOKS)
                self._closed = True

    def close(self):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        if self.thread is None:
            self.tick()
        if self.active is not None:
            self.active.detach(self.room_pose.SNAPSHOT_HOOKS)


def install_from_context(room_pose, ctl, ground_hook):
    """Call AFTER the real room_pose.install returns and GroundHook is created."""
    case, expected = managed_layout(__file__)
    _need(Path(room_pose.__file__).resolve() == expected, 'room_pose_module_binding')
    _need(ctl.tracker is ground_hook.c.tracker and ctl is ground_hook.c, 'controller_binding')
    _need(isinstance(room_pose.SNAPSHOT_HOOKS, list) and
          ctl.tracker.on_snapshot in room_pose.SNAPSHOT_HOOKS, 'tracker_snapshot_hook_missing')
    return _Service(room_pose, ctl, ground_hook, case)
