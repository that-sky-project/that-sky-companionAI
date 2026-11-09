"""Opt-in ground trial entry; original runner/chat/social source is unchanged.

python -m bot.experimental_ground_entry --pilot-config PATH -- <runner args>
The config watcher performs disk I/O off the Actor publishing thread. A changed
Y offset revokes old motion and cold-rebuilds the bound backend, never blending
two coordinate conversions. All geometry/motor parameters remain engineering
approximations, not native equivalence.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import importlib
import json
import math
import os
import re
from pathlib import Path

import xg_paths
import sys
import threading
import time
import types
import uuid

# The production package layout is retained; sibling helpers are independent.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_runtime import BoundedPilotRuntime, ObservationStore
from pilot_contract import experimental_admission, check_seal, finite, immutable, require, vec
from skyterrain.bound_factory import BoundBackendFactory
from skyterrain.catalog import Catalog, scene_hash
from skyterrain.integration import Decision
from skyterrain.session import SceneScope
from skyterrain.tracker_lifecycle import TrackerLifecycleProvider

PROFILE = 'experimental_ground_trial_v1'


SESSION_STOP_ENV = 'SKY_AERIAL_TRIAL_STOP_AT_UNIX'

@dataclass(frozen=True)
class LaunchSessionDeadline:
    monotonic: float
    unix: float

def launch_session_deadline(config, *, environ=None, clock=time.monotonic, wall_clock=time.time):
    """Bind once to the supervisor's existing absolute stop, never a new duration."""
    raw = (os.environ if environ is None else environ).get(SESSION_STOP_ENV)
    if raw is None:
        return None  # Existing one-hour standalone behavior stays unchanged.
    hop = config.get('hopV20')
    require(type(hop) is dict and hop.get('enabled') is True
            and bool(hop.get('aerialProfiles')), 'session_deadline_requires_aerial_opt_in')
    require(type(raw) is str and bool(raw.strip()), 'session_deadline_unix_invalid')
    try:
        stop_unix = float(raw)
    except (TypeError, ValueError):
        raise ValueError('session_deadline_unix_invalid')
    now, wall = clock(), wall_clock()
    require(finite(stop_unix) and finite(now) and finite(wall), 'session_deadline_unix_invalid')
    remaining = stop_unix - wall
    require(0 < remaining <= 6*3600., 'session_deadline_outside_authorized_window')
    return LaunchSessionDeadline(now + remaining, stop_unix)

_WORKER_FAILURES = frozenset(('worker_timeout','worker_exited','reply_budget',
    'worker_protocol','worker_write_failed','worker_start_failed','worker_termination_failed',
    'unsolicited_reply','worker_queue_full','reply_id_mismatch','worker_closed',
    'worker_busy_timeout','worker_not_prepared','worker_missing','unknown_operation'))

_TRANSIENT_ADMISSION = frozenset((
    'engineering_height_difference', 'tracker_busy', 'tracker_unavailable',
    'tracker_read_failed', 'lifecycle_not_fresh', 'lifecycle_unavailable',
    'lifecycle_changed_during_read', 'target_missing_or_ambiguous',
    'target_scene_unverified', 'target_incarnation_missing',
    'position_observation_invalid', 'position_stale_or_future',
    'target_changed_during_read', 'target_unavailable',
    'subject_binding_or_observation_changed', 'observation_stale_or_future',
    'own_pose_changed_during_read', 'room_unavailable', 'engine_unavailable',
    'engine_changed_during_read'))

def _safe_worker_failure(value):
    return value if isinstance(value, str) and value in _WORKER_FAILURES else ('other_worker_failure' if value else None)

def _worker_snapshot(backend):
    transport = getattr(backend, 'transport', None)
    if transport is None:
        return None
    proc = getattr(transport, '_proc', None)
    try: poll = proc.poll() if proc is not None else None
    except Exception: poll = None
    return dict(failure=_safe_worker_failure(getattr(transport, '_failure', None)),
        generation=getattr(transport, 'generation', None), loaded_epoch=getattr(backend, '_loaded_epoch', None),
        process_returncode=poll if type(poll) is int else None,
        process_present=proc is not None, closed=getattr(transport, '_closed', None))

def _diagnosed_worker_call(backend, request, log, op, payload):
    started = time.perf_counter()
    try:
        reply = request(op, **payload)
    except Exception as error:
        diagnostic = dict(operation=op if op in ('load','preparePlan','plan','sample','retarget','cancel','invalidate','ground','ping','prepareHop','startHop','sampleHop','softCancelHop','hardCancelHop') else 'other',
            status='exception', failure=_safe_worker_failure(getattr(backend.transport, '_failure', None) or str(error)),
            elapsed_ms=round((time.perf_counter()-started)*1000., 3))
        backend.last_route_diagnostic = diagnostic
        backend.last_worker_failure = diagnostic
        log('experimental_ground_worker_failure', **diagnostic, worker=_worker_snapshot(backend))
        raise
    if op in ('preparePlan', 'plan', 'retarget', 'prepareHop', 'startHop'):
        reason = str(reply.get('reason', ''))[:256]
        backend.last_route_diagnostic = dict(operation=op, status=str(reply.get('status', ''))[:24],
            reason=reason if all(ch.isalnum() or ch in '_-.:' for ch in reason) else 'worker_reason_redacted',
            elapsed_ms=round((time.perf_counter()-started)*1000., 3))
        if op == 'preparePlan':
            backend.last_prepare_elapsed_ms = backend.last_route_diagnostic['elapsed_ms']
    return reply


def _data_candidates(rel):
    """`data:` 前缀的实际位置：安装版地形在 data/game/<版本>/terrain 下，
    而配置里写的是 data:terrain / data:multimap-catalog.json —— 依次试几个候选。"""
    rel = str(rel or "").lstrip("/\\")
    base = Path(xg_paths.DATA)
    cands = [base / rel]
    game = base / "game"
    try:
        vers = sorted((p for p in game.iterdir() if p.is_dir()), reverse=True) if game.is_dir() else []
    except OSError:
        vers = []
    for v in vers:
        cands += [v / rel, v / "terrain" / rel, v / "terrain" / Path(rel).name]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def _expand_runtime_paths(doc):
    """把安装版配置里的 data:/app: 前缀换成实际绝对路径（服务器配置本来就是绝对路径）。"""
    if not isinstance(doc, dict):
        return doc
    out = dict(doc)
    for key in ('assetRoot', 'engineRoot', 'catalogPath'):
        value = out.get(key)
        if isinstance(value, str) and value.startswith('data:'):
            p = _data_candidates(value[5:])
            # assetRoot 指的是"资产根"：安装版地形把素材放在 <terrain>/assets 下，
            # 目录行里的 asset_relpath 是 beamo-world-all-v1/... 这种相对 assets 的路径。
            if key == 'assetRoot' and (p / 'assets').is_dir():
                p = p / 'assets'
            out[key] = str(p)
        elif isinstance(value, str) and value.startswith('app:'):
            out[key] = str(xg_paths.APP / value[4:])
    if out.get('nodeExecutable') == 'node':
        found = xg_paths.node_exe()
        if found:
            out['nodeExecutable'] = found
    hop = out.get('hopV20')
    if isinstance(hop, dict):
        hop = dict(hop)
        proof = hop.get('proofDirectory')
        if isinstance(proof, str) and proof.startswith('data:'):
            hop['proofDirectory'] = str(xg_paths.DATA / proof[5:])
        out['hopV20'] = hop
    # 安装包里的 catalogSHA256 只对开发机那次地形构建成立。用户机用自己的 APK 重新生成
    # 地形后 sha 必然不同，固定的 sha 会让目录校验失败（blocked_scene_catalog），
    # 表现就是小光站着不动、air_plan no_terrain。这里改为按本机实际文件取 sha。
    try:
        _cp = out.get('catalogPath')
        if isinstance(_cp, str) and os.path.isfile(_cp):
            with open(_cp, 'rb') as _f:
                _sha = hashlib.sha256(_f.read()).hexdigest()
            if out.get('catalogSHA256') != _sha:
                out['catalogSHA256'] = _sha
    except Exception:
        pass
    return out


def validate_config(doc):
    doc = _expand_runtime_paths(doc)
    require(type(doc) is dict and doc.get('profile') == PROFILE
            and doc.get('enabled') is True, 'explicit_experimental_opt_in_required')
    catalog_mode = 'catalogPath' in doc or 'catalogSHA256' in doc
    if catalog_mode:
        require(type(doc.get('catalogPath')) is str and Path(doc['catalogPath']).is_absolute(),
                'absolute_catalog_required')
        require(type(doc.get('catalogSHA256')) is str and
                re.fullmatch(r'[0-9a-f]{64}', doc['catalogSHA256']), 'catalog_sha_required')
    else:
        require(doc.get('sceneName') == 'CandleSpace' and doc.get('sceneHash') == 3526133726,
                'loaded_scene_only')
    require(all(doc.get(k) is False for k in ('nativeCollisionVerified', 'calibrationVerified',
                                             'runtimeReady')), 'native_flags_remain_false')
    require(doc.get('parameterSource') == 'engineering_trial_not_native_matched', 'parameter_source')
    offset = vec(doc.get('originOffset'))
    require(offset[0] == offset[2] == 0 and -2 <= offset[1] <= 2, 'trial_y_offset_range')
    if not catalog_mode:
        require(type(doc.get('bindingRow')) is dict, 'pinned_binding_row_required')
        require(doc['bindingRow'].get('scene_hash') == doc['sceneHash']
                and doc['bindingRow'].get('scene_name') == doc['sceneName'], 'row_scene_mismatch')
    for key in ('assetRoot', 'engineRoot', 'nodeExecutable'):
        require(type(doc.get(key)) is str and bool(doc[key]), 'runtime_path_required')
    movement = doc.get('movement', {})
    require(type(movement) is dict, 'movement_config')
    speed, segment, travel = (movement.get('walkSpeed', .25),
        movement.get('maxSegmentMeters', .8), movement.get('travelBudgetMeters', .8))
    require(all(finite(x) for x in (speed, segment, travel)) and 0 < speed <= 3.
            and 0 < segment <= 6. and segment <= travel <= 8., 'movement_limits')
    if 'followDistance' in movement:
        require(finite(movement['followDistance']) and .5 <= movement['followDistance'] <= 3., 'follow_distance')
    if 'followSlack' in movement:
        require(finite(movement['followSlack']) and .15 <= movement['followSlack'] <= 1., 'follow_slack')
    from hop_follow_runtime_v20 import validate_entry_hop_config
    validate_entry_hop_config(doc)
    return doc


def _load_scene_catalog(path, expected_sha):
    """Cold-only pinned snapshot. The Actor never reads this file or hashes assets."""
    p = Path(path)
    require(p.is_file() and 0 < p.stat().st_size <= 8*1024*1024, 'catalog_budget')
    with p.open('rb') as stream:
        raw = stream.read(8*1024*1024+1)
    require(len(raw) <= 8*1024*1024 and hashlib.sha256(raw).hexdigest() == expected_sha,
            'catalog_pin_mismatch')
    doc = json.loads(raw)
    require(type(doc) is dict and doc.get('format') == 'skyterrain-catalog-v1', 'catalog_format')
    catalog = Catalog(doc.get('entries'))
    rows = {}
    for row in catalog.entries:
        require(scene_hash(row['scene_name']) == row['scene_hash'], 'catalog_scene_hash_mismatch')
        rows[row['scene_hash']] = row
    return rows


class _ClosedGate:
    enabled = True
    last_decision = Decision('unavailable', reason='experimental_preparing_or_stopped')
    def choose_motion(self, *a, **kw): return None
    def bind_scope(self, token): return False
    def invalidate(self): pass
    def set_social_owned(self, owned): pass


class EngineeringObservations(ObservationStore):
    """Existing UDP peer freshness plus generated bot origin, NOT native reads."""
    def __init__(self, c, provider, admission, *, clock=time.monotonic):
        super().__init__()
        self.c, self.provider, self.admission, self.clock = c, provider, admission, clock
        self.last_reason = 'not_observed'

    def _missing(self, reason):
        self.last_reason = reason
        return None

    def snapshot(self):
        life = self.provider()
        if life is None:
            return self._missing(getattr(self.provider, 'last_reason', 'lifecycle_unavailable'))
        c, t, ctx = self.c, self.c.tracker, self.admission.context
        if not t.lock.acquire(False):
            return self._missing('tracker_busy')
        try:
            slot = ctx['target']['slot']
            origin, stamp = t.origins.get(slot), t.position_observed.get(slot)
            if (t._own_social_binding_locked() != ctx['bot']['slot']
                    or t.own_uuid != ctx['bot']['uuid'] or t.slots.get(slot) != ctx['target']['uuid']
                    or t._slot_versions.get(slot) != ctx['target']['incarnation']
                    or origin is None or stamp is None or origin[3] != stamp[0]):
                return self._missing('subject_binding_or_observation_changed')
            target, observed = vec(origin[:3]), stamp[1]
        except Exception:
            return self._missing('tracker_read_failed')
        finally:
            t.lock.release()
        if self.provider() != life or not finite(observed):
            return self._missing('lifecycle_changed_during_read')
        own = vec(c.position)
        # Different XZ locations can have different valid ground elevations.
        # Relative Y is not a flight detector. Fresh observed origins are only
        # planning inputs: actual endpoint support, complete/collision-preflight
        # routes and the motor's own ground-contact/step budgets remain mandatory.
        self.last_reason = 'ready'
        def subject(role, position):
            return {'identity': ctx[role], 'position': position,
                    'supportKind': 'empirical_provisional_patch', 'locomotion': 'dry_ground'}
        return {'context': ctx, 'certificateSHA256': self.admission.certificate_sha,
                'evidenceKind': 'engineering_state', 'captureStart': observed,
                'captureEnd': observed, 'bot': subject('bot', own),
                'target': subject('target', target)}


class ExperimentalGroundHook:
    """Per-instance seam: only explicit candidate entry installs this hook."""
    def __init__(self, c, config, *, config_path=None, clock=time.monotonic,
                 backend_factory=BoundBackendFactory, session_deadline=None):
        self.c, self.clock, self.factory = c, clock, backend_factory
        self._config = validate_config(dict(config))
        self._config_generation, self._active_generation = 1, 0
        self._config_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='experimental-ground')
        self._future = self.runtime = None
        self._pending_install_runtime = None
        self._pending_permission_reason = None
        self._closed, self._expired = False, False
        self._launch_session_deadline = session_deadline
        now = clock()
        require(session_deadline is None or type(session_deadline) is LaunchSessionDeadline
                and finite(session_deadline.monotonic) and finite(session_deadline.unix)
                and 0 < session_deadline.monotonic-now <= 6*3600., 'shared_session_deadline_invalid')
        self.deadline = now + 3600. if session_deadline is None else session_deadline.monotonic
        self._desired = None
        self._request_label = 'movement'
        # T23-8：座位走路的稳定参照（喊她坐下的人）。label != seat 时清空，
        # 不允许它会随 _idle_target 在房间里换人。
        self._request_target = None
        self._reported_motion = None
        self._reported_guard = None
        self._reported_advanced = None
        self._reported_status = None
        self._next_follow_refresh = 0.
        self._desired_follow_peer = None
        self._accepted_follow_peer = None
        self._follow_cold_streak = None
        self._last_follow_handoff = -math.inf
        self._follow_settings_generation = None
        self._worker_recovery_attempts = 0
        self._worker_recovery_next = 0.
        self._worker_recovery_pending = self._worker_recovery_build = False
        self._worker_recovery_exhausted = False
        self._worker_recovery_lifecycle = None
        self._worker_recovery_timeout = False
        self._catalog_rows = None
        self._catalog_generation = self._catalog_failed_generation = None
        self._catalog_future = None
        self._scene_signature = None
        self._desired_scene_signature = None
        self._scene_epoch = 0
        self._scene_reset_requested = False
        self._scene_follow_pending = False
        self._cold_build_attempts = 0
        self._cold_build_next = 0.
        self._cold_build_exhausted = False
        self._original_post, self._original_walk = c._post, c.walk_to
        self._original_set_override, self._original_reset = c._set_override, c.reset
        self._pose_override = self._original_set_override.__func__.__globals__['map_long_engine'].POSE_OVERRIDE
        self._watch_stop = threading.Event()
        self.status = 'waiting_for_room_target'
        c.attach_terrain(_ClosedGate(), lambda: None, prepared=False)
        from hop_follow_runtime_v20 import HopCommandBinding, validate_entry_hop_config
        self._hop_dispatch = HopCommandBinding(c) if validate_entry_hop_config(config) else None
        # T23-4：room_pose 需要知道旧的 hop_follow_runtime_v20 小跳是否正在
        # 驱动，才能避免和 AirMotion 同时抢 Actor；空中被接管时由 room_pose
        # 调 hop_cancel_probe 先 hard_cancel hop 再起飞。
        def _hop_active_probe():
            co = getattr(getattr(self, '_hop_dispatch', None), 'coordinator', None)
            return bool(co is not None and getattr(co, 'active', False))
        def _hop_cancel_probe(reason='air_start'):
            co = getattr(getattr(self, '_hop_dispatch', None), 'coordinator', None)
            if co is None:
                return False
            try:
                co.hard_cancel(str(reason)[:64])
                return True
            except Exception:
                return False
        c._hop_active_probe = _hop_active_probe
        c._hop_cancel_probe = _hop_cancel_probe
        # Explicit trial only; shared by initial build and the hop fixed provider.
        c.tracker._follow_room_presence_v20 = self._hop_dispatch is not None
        def post(controller, body):
            self.tick()
            active = self.runtime._motion if self.runtime else None
            from hop_follow_runtime_v20 import post_runtime_hop
            hop_tick = post_runtime_hop(self.runtime, body, now=self.clock(),
                original_post=self._original_post,
                read_origin=self._original_post.__func__.__globals__["anim_pose"].read_origin,
                animation_codec=self._original_post.__func__.__globals__["anim_pose"])
            result = hop_tick.body
            if hop_tick.emitted:
                if hop_tick.needs_follow_resume:
                    self._dispatch_after_hop_landing(hop_tick)
                self._report_status()
                self._remember_handoff_pose(result, grounded=hop_tick.phase == "landed")
                return result  # Hop owns this packet; no ground re-encoding.
            # A completed segment may enqueue the next cold preparation now;
            # never call the motion sampler twice for one outgoing Actor.
            self._dispatch_after_arrival(active)
            runtime = self.runtime
            if (active is not None and active is not self._reported_advanced
                    and not active.blocked and not c._terrain_social_owns()):
                # Parse the bytes actually constructed by the original Actor
                # postprocessor, not merely the proposed motion/controller pose.
                read_origin = self._original_post.__func__.__globals__['anim_pose'].read_origin
                actual = vec(read_origin(result))
                progress = math.hypot(actual[0]-active.a[0], actual[2]-active.a[2])
                if progress >= .05:
                    self._reported_advanced = active
                    c.log('experimental_ground_actor_advanced', start=list(active.a),
                          current=list(actual), horizontal_progress=progress,
                          stage='constructed_actor_before_udp_send', udp_ack_verified=False)
            diagnostic = getattr(runtime, 'last_stop_diagnostic', None) if runtime else None
            if diagnostic is not None:
                key = (id(runtime), diagnostic['operation'])
                if key != self._reported_guard:
                    self._reported_guard = key
                    c.log('experimental_ground_guard_stopped', **diagnostic)
            self._report_status()
            cached = getattr(active, "_sample", None)
            self._remember_handoff_pose(result, grounded=getattr(cached, "grounded", False) is True
                and not getattr(active, "blocked", True))
            return result
        def walk(controller, goal, label='', face=None, speed=.25):
            if label == 'bounded_pilot':
                return self._original_walk(goal, label, face, speed)
            if self._closed or self._expired or self._worker_recovery_exhausted or c._terrain_social_owns():
                return False
            try:
                self._desired = vec(goal)
                self._desired_follow_peer = None
                # T23-8：seat 请求的稳定参照由 room_pose 放在 _walk_ref，别的
                # label 一律清掉，避免上一次座位的人被当成这次走路的 subject。
                ref = getattr(controller, '_walk_ref', None)
                self._request_target = ref if (label == 'seat' and type(ref) in (str, int)) else None
                observed_scene = self._confirmed_scene()
                self._desired_scene_signature = ((self._config_generation, id(observed_scene[0]),
                    observed_scene[1], observed_scene[2], observed_scene[3]) if observed_scene else None)
                self._request_label = label if label in ('follow', 'point', 'target', 'home', 'seat') else 'movement'
                # Command thread only queues the latest logical goal. The Actor
                # owner submits it after the current segment/prepare finishes.
                c._set_override(c.base)
                return True
            except Exception:
                self._desired = None
                self.status = 'blocked_command'
                return False
        def set_override(controller, body, **kw):
            # 2026-11-07 T21：透传 force（同图换落点需强制写入目标种子）
            self._original_set_override(body, **kw)
            # Legacy idle/home optimization removes the callback. This candidate
            # needs engine frames even before its first cold route is ready.
            self._pose_override['post'] = c._safe_post
        def reset(controller, why='new_room', **kw):
            # T23-4：room_pose.reset 现在支持 keep_pose/keep_seat（同图重校验
            # 保留座位/位置）；wrapper 必须原样透传，否则 on_room 传进来会被
            # 悄悄丢掉，又退回出生点。
            continuity = self._capture_handoff_continuity(why)
            self._handoff_pose_checkpoint = None
            binding = getattr(self, '_hop_dispatch', None)
            co = binding.coordinator if binding is not None else None
            if co is not None:
                co.hard_cancel('room_reset')
            result = self._original_reset(why, **kw)
            self._restore_handoff_continuity(continuity)
            # Room lifecycle reset clears POSE_OVERRIDE directly, not through
            # _set_override. Keep the explicit candidate callback alive, while
            # original reset still invalidates terrain and clears follow state.
            self._pose_override['post'] = c._safe_post
            # The Actor owner applies invalidation; this callback may run on
            # the command/reconnect thread and must not close any worker.
            self._scene_reset_requested = True
            return result
        c._post, c.walk_to = types.MethodType(post, c), types.MethodType(walk, c)
        c._set_override, c.reset = types.MethodType(set_override, c), types.MethodType(reset, c)
        c._set_override(c.base)  # _safe_post dispatches to the instance _post.
        c.log('experimental_ground_hook_installed', profile=PROFILE,
              controller_module=c.__class__.__module__, frame_hook='instance_safe_post',
              deadline_source='supervisor_absolute' if session_deadline is not None else 'default_one_hour',
              session_deadline_monotonic=self.deadline, remaining_seconds=max(0.,self.deadline-clock()),
              native_collision_verified=False, calibration_verified=False)
        if config_path is not None:
            self._watcher = threading.Thread(target=self._watch, args=(Path(config_path),),
                                            daemon=True, name='pilot-config-watch')
            self._watcher.start()

    def _bind_admission_deadline(self, admission):
        if getattr(self, '_launch_session_deadline', None) is None:
            return admission
        check_seal(admission, False)
        require(admission.mode == 'experimental_ground_trial', 'session_deadline_engineering_only')
        # This engineering digest excludes time; certificate-mode admissions
        # are never changed. Preserve every context/geometry/movement field.
        bound = replace(admission, expires_wall=self._launch_session_deadline.unix)
        check_seal(bound, False)
        return bound

    def _bind_runtime_deadline(self, runtime):
        # BoundedPilotRuntime.install retains its standalone one-hour default.
        # Only the launch-bound trial replaces that local cap with the SAME
        # supervisor deadline, including after scene/room/worker replacement.
        require(finite(runtime.deadline), 'runtime_deadline_invalid')
        runtime.deadline = (self.deadline if getattr(self, '_launch_session_deadline', None) is not None
                            else min(runtime.deadline, self.deadline))

    @staticmethod
    def _dispose_build_future(future):
        try: future.result()[1].shutdown()
        except Exception: pass

    def _discard_pending_build(self, *, refund=False):
        future, self._future = self._future, None
        self._pending_install_runtime = self._pending_permission_reason = None
        if future is not None:
            future.cancel()
            # Even a completed future must close its process on the cold owner,
            # not via an inline add_done_callback on an Actor/command thread.
            self._executor.submit(self._dispose_build_future, future)
            if refund and not getattr(self, '_worker_recovery_build', False):
                self._cold_build_attempts = max(0, getattr(self, '_cold_build_attempts', 0)-1)

    def _pending_install_action(self, runtime, config):
        """Memory-only lifecycle/observation classification; never lower a gate."""
        c = self.c
        if self._closed or self._expired or self.clock() >= self.deadline:
            return 'cancel', 'deadline_or_close'
        if self._request_label == 'follow' and not c.follow:
            return 'cancel', 'follow_cancelled'
        selector = self._request_selector(config)
        if selector is None:
            return 'cancel', 'target_missing'
        if selector != getattr(runtime, '_entry_target_selector', selector):
            return 'rebind', 'target_selector_changed'
        ctx, t = runtime.admission.context, c.tracker
        life = runtime.lifecycle()
        if life is not None and (life.scene.scene_name != ctx['sceneName']
                or life.scene.scene_hash != ctx['sceneHash']
                or life.scene.room_generation != ctx['roomGeneration']
                or life.target_key != f"{ctx['target']['uuid']}@slot={ctx['target']['slot']}"
                or life.target_incarnation != ctx['target']['incarnation']):
            return 'rebind', 'pending_context_changed'
        if not t.lock.acquire(False):
            return 'wait', 'tracker_busy'
        try:
            own, peer = ctx['bot'], ctx['target']
            if (t._own_social_binding_locked() != own['slot'] or t.own_uuid != own['uuid']
                    or t._slot_versions.get(own['slot']) != own['incarnation']
                    or t.slots.get(peer['slot']) != peer['uuid']
                    or t._slot_versions.get(peer['slot']) != peer['incarnation']):
                return 'rebind', 'pending_subject_binding_changed'
        finally:
            t.lock.release()
        if c._terrain_social_owns():
            return 'wait', 'social_owned'
        if getattr(runtime.backend.transport, 'failed', False):
            return 'fatal', 'pending_worker_failed'
        if runtime._permission() is not None:
            return 'ready', 'ready'
        reason = runtime.last_permission_reason
        return ('wait' if reason in _TRANSIENT_ADMISSION else 'fatal'), reason

    def _defer_pending_install(self, action, reason, runtime):
        if action == 'ready':
            return False
        if action == 'wait':
            self._pending_install_runtime = runtime
            self._pending_permission_reason = reason
            self.last_error = None
            self.status = 'waiting_current_admission'
            return True
        if action in ('cancel', 'rebind'):
            self._discard_pending_build(refund=True)
            self._worker_recovery_build = self._worker_recovery_pending = False
            self._desired = self._desired_follow_peer = None
            self._scene_follow_pending = action == 'rebind' and bool(self.c.follow)
            self._cold_build_next = self.clock()+.2
            self.last_error = None
            self.status = 'waiting_for_fresh_rebind' if action == 'rebind' else 'movement_cancelled'
            return True
        require(False, 'pending_admission_'+str(reason))

    def _change_scene_signature(self, signature):
        if (signature == getattr(self, '_scene_signature', None)
                and not getattr(self, '_scene_reset_requested', False)):
            return
        self._scene_signature = signature
        self._scene_reset_requested = False
        self._scene_epoch = getattr(self, '_scene_epoch', 0)+1
        self._scene_follow_pending = bool(self.c.follow and self._request_label == 'follow')
        # Keep a newly queued point/home request only if the command already
        # observed this exact scene/config. Old-scene coordinates never survive.
        if signature is None or getattr(self, '_desired_scene_signature', None) != signature:
            self._desired = None
        self._next_follow_refresh = 0.
        self._desired_follow_peer = self._accepted_follow_peer = None
        self._follow_cold_streak = None
        self._worker_recovery_pending = self._worker_recovery_build = False
        self._worker_recovery_lifecycle = None
        self._cold_build_attempts = 0
        self._cold_build_next = 0.
        self._cold_build_exhausted = False
        self._revoke('experimental_scene_changed')
        self._discard_pending_build()

    def _remember_handoff_pose(self, body, *, grounded=False):
        """Last successful Actor post construction, never a UDP/server ACK claim.

        This continuity applies only to grounded walking or grounded idle with
        an explicit follow. Airborne, social, and changed commands clear it.
        """
        try:
            c, binding = self.c, self._hop_dispatch
            co = getattr(self.runtime, '_follow_hop', None)
            scene = self._confirmed_scene()
            if (scene is None or scene is False or binding is None or not binding.is_follow_current()
                    or c.motion is not None and c.motion is not getattr(self.runtime, "_motion", None)
                    or c._terrain_social_owns() or co is not None and co.active
                    or self._closed or self._expired or self.clock() >= self.deadline):
                self._handoff_pose_checkpoint = None
                return
            codec = self._original_post.__func__.__globals__['anim_pose']
            position = vec(codec.read_origin(body))
            previous = getattr(self, '_handoff_pose_checkpoint', None)
            same_ground = (previous is not None and previous['engine'] is scene[0]
                and previous['follow'] is c.follow and previous['revision'] == binding.revision
                and c.motion is None and previous['motion'] is None
                and math.dist(position, previous['position']) <= .02)
            if not grounded and not same_ground:
                self._handoff_pose_checkpoint = None
                return
            target = c.follow.get('target')
            if type(target) is not str or str(uuid.UUID(target)) != target:
                self._handoff_pose_checkpoint = None
                return
            owner = getattr(scene[0], '_owner', None)
            if type(owner) is not bytes or len(owner) != 16:
                self._handoff_pose_checkpoint = None
                return
            heading = c.heading if finite(c.heading) else None
            self._handoff_pose_checkpoint = dict(engine=scene[0], map_id=scene[2], owner=owner,
                position=position, heading=heading, motion=c.motion, follow=c.follow, revision=binding.revision,
                target=target, deadline=self.deadline)
        except Exception:
            self._handoff_pose_checkpoint = None

    def _capture_handoff_continuity(self, why):
        """Only an actual same-map Move-created engine can carry logical intent."""
        saved = getattr(self, '_handoff_pose_checkpoint', None)
        binding = getattr(self, '_hop_dispatch', None)
        if saved is None or binding is None or why != 'validated_room':
            return None
        with binding.lock:
            scene = self._confirmed_scene()
            c, co = self.c, getattr(self.runtime, '_follow_hop', None)
            if (scene is None or scene is False or scene[0] is saved['engine']
                    or getattr(saved['engine'], '_closed', None) is not True
                    or getattr(scene[0], '_local_idle_restored', None) is not True
                    or scene[2] != saved['map_id'] or getattr(scene[0], '_owner', None) != saved['owner']
                    or not binding.is_follow_current() or binding.revision != saved['revision']
                    or c.follow is not saved['follow'] or c._terrain_social_owns()
                    or c.motion is not saved['motion']
                    or c.motion is not None and c.motion is not getattr(self.runtime, '_motion', None)
                    or co is not None and co.active or self._closed or self._expired
                    or self.clock() >= min(self.deadline, saved['deadline'])
                    or math.dist(vec(c.position), saved['position']) > .02):
                return None
            return dict(saved, next_engine=scene[0])

    def _restore_handoff_continuity(self, saved):
        if saved is None:
            return False
        binding = self._hop_dispatch
        with binding.lock:
            c, scene = self.c, self._confirmed_scene()
            if (scene is None or scene is False or scene[0] is not saved['next_engine']
                    or binding.revision != saved['revision'] or binding.follow_ref is not saved['follow']
                    or binding.target != saved['target'] or c.follow is not None or c.motion is not None
                    or c._terrain_social_owns() or self._closed or self._expired
                    or self.clock() >= min(self.deadline, saved['deadline'])):
                return False
            # Restore the same explicit command object, not old room physics.
            # The existing new-scene lifecycle waits for this UUID's fresh new
            # room incarnation before building a new worker/permit/proof.
            c.position = c._terrain_last_position = saved['position']
            c.heading, c.follow = saved['heading'], saved['follow']
            c.state = dict(pose='follow', target=saved['target'])
            self._request_label, self._scene_follow_pending = 'follow', True
            self._desired = self._desired_follow_peer = None
            self._next_follow_refresh = 0.
            # Carry only the previously grounded logical pose into this new
            # engine. Otherwise an idle actor has no new motion sample with
            # which to recreate the checkpoint before another same-map Move.
            # A subsequent successful Actor post must still agree with it;
            # this grants no old worker, collision proof, or motion authority.
            self._handoff_pose_checkpoint = dict(engine=scene[0], map_id=scene[2],
                owner=saved['owner'], position=c.position, heading=c.heading,
                motion=None, follow=c.follow, revision=binding.revision,
                target=saved['target'], deadline=min(self.deadline, saved['deadline']))
            c.log('experimental_same_map_handoff_preserved', position=list(c.position),
                scope='grounded_explicit_follow', old_physics_reused=False,
                position_evidence='successful_local_actor_post', udp_ack_verified=False)
            return True

    def _confirmed_scene(self):
        t = self.c.tracker
        if not t.lock.acquire(False):
            return False
        try:
            engine = t._engine
            match = getattr(engine, '_match', None)
            hashed, room = t.confirmed_map_id, t.room_generation
            if (t.ready is not True or type(hashed) is not int or type(room) is not int or room <= 0
                    or t.pending_map_id != hashed or getattr(engine, '_map', None) != hashed
                    or getattr(engine, '_closed', None) is not False or getattr(match, 'ready', None) is not True
                    or getattr(match, 'self_level_id', None) != hashed or type(t.epoch) is not int
                    or not 0 <= t.epoch <= 255 or getattr(engine, '_epoch', None) != t.epoch):
                return None
            return (engine, room, hashed, t.epoch)
        except Exception:
            return None
        finally:
            t.lock.release()

    def _route_scene_config(self, config, generation):
        """Memory-only map selection; catalog loading is a separate cold job."""
        catalog_mode = 'catalogPath' in config
        if catalog_mode and getattr(self, '_catalog_generation', None) != generation:
            self._change_scene_signature(None)
            pending = getattr(self, '_catalog_future', None)
            if pending is not None:
                old_generation, future = pending
                if not future.done():
                    self.status = 'loading_scene_catalog'
                    return None
                self._catalog_future = None
                try: rows = future.result()
                except Exception:
                    if old_generation == generation:
                        self._catalog_failed_generation = generation
                        self.last_error = 'catalog_load_failed'
                else:
                    if old_generation == generation:
                        self._catalog_rows, self._catalog_generation = rows, generation
            if getattr(self, '_catalog_generation', None) != generation:
                if getattr(self, '_catalog_failed_generation', None) != generation:
                    self._catalog_future = (generation, self._executor.submit(
                        _load_scene_catalog, config['catalogPath'], config['catalogSHA256']))
                    self.status = 'loading_scene_catalog'
                else:
                    self.status = 'blocked_scene_catalog'
                return None
        snapshot = self._confirmed_scene()
        if snapshot is False:
            self.status = 'waiting_for_scene_snapshot'
            return None
        if snapshot is None:
            self._change_scene_signature(None)
            self.status = 'waiting_for_confirmed_scene'
            return None
        engine, room, hashed, epoch = snapshot
        signature = (generation, id(engine), room, hashed, epoch)
        self._change_scene_signature(signature)
        row = (self._catalog_rows.get(hashed) if catalog_mode else
               config.get('bindingRow') if hashed == config.get('sceneHash') else None)
        if row is None or any(row.get(k) != 'ready' for k in ('read_status','collision_status','nav_status')):
            self.status = 'unsupported_current_scene'
            return None
        require(row.get('scene_hash') == hashed and scene_hash(row.get('scene_name', '')) == hashed,
                'selected_scene_identity')
        effective = dict(config, sceneName=row['scene_name'], sceneHash=hashed, bindingRow=dict(row),
                         _expected_engine=engine, _room_generation=room)
        return effective, (generation, self._scene_epoch, signature)

    def _idle_target(self):
        """2026-11-06：没在跟随谁时（独自闲逛），用同房间里位置新鲜的哥哥做地形加载的参照，
        让地面在闲逛时也加载、站姿由地形层处理（否则雨林等图待机腿抽搐）。哥哥不在就用其他在场玩家。"""
        try:
            owner = open(xg_paths.OWNER_UUID, encoding='utf-8').read().strip()
            t = self.c.tracker
            now = self.clock()
            def fresh(uid):
                slot = next((k for k, u in t.slots.items() if u == uid), None)
                seen = t.position_observed.get(slot) if slot is not None else None
                return seen is not None and 0 <= now - seen[1] <= 2.0
            if fresh(owner):
                return owner
            # 哥哥不在：其他玩家也看得到小光，就用房间里任意一个位置新鲜的玩家做参照（按 uuid 排序保证稳定）
            for uid in sorted(set(t.slots.values())):
                if uid and uid != t.own_uuid and fresh(uid):
                    return uid
            return None
        except Exception:
            return None

    def _request_selector(self, config=None):
        """这次地面请求的准入 subject。

        T23-8：seat 走路必须用 room_pose 传进来的固定参照（喊坐下的那个人），
        不能再用 ``(c.follow or {}).get('target') or self._idle_target()``：
        ``_idle_target`` 按 2 s 新鲜度/排序在房间里挑人，构建 worker 后一换人
        或者那个人离开，admission 的 target slot/incarnation 就永远对不上，
        seat 走路在 1.5 s 内一次都不会起步。其它 label 保持原逻辑。
        """
        target = (config or {}).get('targetUUID')
        if target:
            return target
        if self._request_label == 'seat' and type(self._request_target) in (str, int):
            return self._request_target
        return (self.c.follow or {}).get('target') or self._idle_target()

    def _fresh_build_target(self, config):
        try:
            t = self.c.tracker
            scene = SceneScope(config['sceneHash'], config['sceneName'], config.get('_room_generation', t.room_generation))
            provider = TrackerLifecycleProvider(t, scene,
                lambda: self._request_selector(config),
                expected_engine=config.get('_expected_engine', t._engine), monotonic=self.clock)
            available = provider() is not None
            self._fresh_build_reason = provider.last_reason
            return available
        except Exception:
            self._fresh_build_reason = 'fresh_build_check_failed'
            return False

    def _watch(self, path):
        previous = None
        while not self._watch_stop.wait(.5):
            try:
                require(path.stat().st_size <= 65536, 'config_budget')
                raw = path.read_bytes()
                require(len(raw) <= 65536, 'config_budget')
                digest = hashlib.sha256(raw).digest()
                if digest == previous and self._config is not None:
                    continue
                doc = validate_config(json.loads(raw))
                previous = digest
                with self._config_lock:
                    if doc != self._config:
                        self._config = doc
                        self._config_generation += 1
            except Exception:
                with self._config_lock:
                    self._config = None
                    self._config_generation += 1

    def _build(self, config, generation):
        c, t = self.c, self.c.tracker
        scene = SceneScope(config['sceneHash'], config['sceneName'], config.get('_room_generation', t.room_generation))
        engine = config.get('_expected_engine', t._engine)
        selector = self._request_selector(config)
        provider = TrackerLifecycleProvider(t, scene,
            lambda: self._request_selector(config),
            expected_engine=engine, monotonic=self.clock)
        life = provider()
        require(life is not None, 'fresh_room_target_required')
        peer, slot = life.target_key.split('@slot=')
        require(t.lock.acquire(False), 'own_binding_busy')
        try:
            own = t._own_social_binding_locked()
            require(own is not None, 'own_udp_binding_required')
            own_incarnation = t._slot_versions.get(own)
        finally:
            t.lock.release()
        context = dict(sceneName=scene.scene_name, sceneHash=scene.scene_hash,
            roomGeneration=scene.room_generation,
            sessionBindingDigest=hashlib.sha256(f'{id(engine)}:{scene.room_generation}'.encode()).hexdigest(),
            clockDomain='server_process_monotonic',
            bot=dict(uuid=t.own_uuid, slot=own, incarnation=own_incarnation),
            target=dict(uuid=peer, slot=int(slot), incarnation=life.target_incarnation))
        row = config['bindingRow']
        from hop_follow_runtime_v20 import entry_hop_backend_options
        backend = self.factory(config['assetRoot'], engine_root=config['engineRoot'],
            node_executable=config['nodeExecutable'], foot_offset=vec(config['originOffset']),
            navigation_window=row.get('navigation_window'),
            worker_profile='preserving_imported_nav', **entry_hop_backend_options(config))(row, scene)
        try:
            snapshot = backend.binding_snapshot()
            require(snapshot.get('scene_name') == scene.scene_name and snapshot.get('scene_hash') == scene.scene_hash
                    and snapshot.get('asset_manifest_sha256') == row['asset_manifest_sha256'], 'geometry_scene_pin_mismatch')
            # All geometry hashes come from the exact validated scene manifest.
            # Broad engineering bounds still require real navigation/collision;
            # no asset or straight-line fallback is introduced.
            patch = dict(polygonXZ=[[-10000.,-10000.],[10000.,-10000.],
                                   [10000.,10000.],[-10000.,10000.]],
                minFeetY=-10000., maxFeetY=10000., supportMarginMeters=.765,
                assetDigests=dict(snapshot['asset_digests']),
                navigationSHA256=row['staged_navigation']['navigation_sha256'])
            movement = config.get('movement', {})
            admission = experimental_admission(context, patch,
                offset=config['originOffset'], enabled=True,
                speed=movement.get('walkSpeed', .25),
                requested_distance=movement.get('maxSegmentMeters', .8),
                distance=movement.get('travelBudgetMeters', .8),
                scene_binding=(scene.scene_name, scene.scene_hash))
            admission = self._bind_admission_deadline(admission)
            if 'catalogPath' in config:
                # Catalog trials budget explicit cold map work, not hot samples.
                # on-demand scenes need 10s; imported scenes need 5s. Do not let
                # the legacy timeout recovery below downgrade an on-demand map.
                backend.configure_map_cold_timeout()
            if ('catalogPath' not in config and getattr(self, '_worker_recovery_build', False)
                    and getattr(self, '_worker_recovery_timeout', False)):
                backend.transport.configure_cold_timeout(5.)
            transport_request = backend.transport.request
            def diagnosed_request(op, **payload):
                return _diagnosed_worker_call(backend, transport_request, c.log, op, payload)
            backend.transport.request = diagnosed_request
            ready = backend.prepare()
            require(ready.status == 'ready', 'bound_backend_not_ready')
            observations = EngineeringObservations(c, provider, admission, clock=self.clock)
            runtime = BoundedPilotRuntime(admission, backend, c, provider, observations,
                enabled=True, clock=self.clock)
            runtime._entry_target_selector = selector
            return generation, runtime
        except Exception:
            backend.close()
            raise

    def _pose_diagnostic(self, runtime=None):
        """Anonymous memory snapshot only; never sample or infer ground height."""
        c = self.c
        try: own = list(vec(c.position))
        except Exception: own = None
        target, age = None, None
        social = getattr(c, 'social', None)
        kind = social.get('kind') if isinstance(social, dict) else None
        kind = kind if kind in ('hold', 'carry', 'dance', 'once') else ('other_social' if social else None)
        active = getattr(c, '_active_offer', None)
        if kind is None and isinstance(active, dict) and active.get('phase') == 'accepted':
            profile = active.get('accepted_profile')
            kind = 'active_'+profile if profile in ('hold', 'carrier', 'loop', 'once') else 'active_other'
        try:
            t = c.tracker
            if t.lock.acquire(False):
                try:
                    if runtime is not None:
                        slot = runtime.admission.context['target']['slot']
                    else:
                        selector = (c.follow or {}).get('target')
                        matches = [k for k, v in t.slots.items() if v == selector]
                        slot = matches[0] if len(matches) == 1 else (selector if type(selector) is int else None)
                    origin, stamp = t.origins.get(slot), t.position_observed.get(slot)
                    if origin is not None and stamp is not None and origin[3] == stamp[0]:
                        target = list(vec(origin[:3]))
                        age = self.clock()-stamp[1]
                        age = round(age, 6) if finite(age) else None
                finally:
                    t.lock.release()
        except Exception:
            target = age = None
        return dict(controller_position=own, target_origin=target,
                    height_delta=(own[1]-target[1] if own is not None and target is not None else None),
                    target_age_seconds=age, social_kind=kind)

    def _report_status(self):
        runtime = self.runtime or getattr(self, '_pending_install_runtime', None)
        decision = getattr(runtime, 'last_decision', None)
        route = getattr(getattr(runtime, 'backend', None), 'last_route_diagnostic', None)
        detail = dict(status=self.status, decision=getattr(decision, 'reason', None),
            permission=(getattr(self, '_pending_permission_reason', None)
                        or getattr(runtime, 'last_permission_reason', None)),
            route=route, prepare_ms=getattr(getattr(runtime, 'backend', None), 'last_prepare_elapsed_ms', None),
            worker=_worker_snapshot(getattr(runtime, 'backend', None)),
            motor_policy=getattr(getattr(runtime, 'backend', None), 'last_motion_policy', None),
            recovery_attempts=getattr(self, '_worker_recovery_attempts', 0),
            scene_hash=(getattr(self, '_scene_signature', None) or (None,)*4)[3],
            initial_load_attempts=getattr(self, '_cold_build_attempts', 0),
            fresh_build_reason=getattr(self, '_fresh_build_reason', 'not_checked'),
            error=getattr(self, 'last_error', None))
        key = json.dumps(detail, sort_keys=True)
        if key != self._reported_status:
            self._reported_status = key
            # Positions do not participate in deduplication: log a snapshot at
            # an existing status transition, not a new event for every step.
            detail.update(self._pose_diagnostic(runtime))
            self.c.log('experimental_ground_status', **detail)

    def _submit_desired(self):
        runtime = self.runtime
        co = getattr(runtime, "_follow_hop", None)
        if co is not None:
            if co.suppress_ground_replan:
                return False
            if self._request_label == "follow":
                from hop_follow_runtime_v20 import authorize_entry_follow
                if not authorize_entry_follow(runtime):
                    return False
        if self.c._terrain_social_owns():
            return
        if self._request_label == 'follow' and not self.c.follow:
            self._desired = None
            return
        if self._desired is None or runtime is None:
            return
        # Do not replace a current physics generation for each one-second
        # follow poll. Keep the latest UNCLIPPED goal, including peer updates.
        # Only this Actor thread owns request/prepare/sample progression.
        if (runtime._future is not None or runtime._intent is not None
                or runtime._prepared is not None or self.c.motion is not None):
            return
        if runtime._permission() is None:
            self.status = 'waiting_for_fresh_observation'
            return
        start, goal = vec(self.c.position), self._desired
        self._desired = None
        follow_intent = None
        if (self._request_label == 'follow' and self.c.follow
                and getattr(runtime.admission, 'mode', None) == 'experimental_ground_trial'):
            # Preserve the fresh full target as planning intent; the bounded
            # proxy below stays only a request/lease identity, not a waypoint.
            try:
                goal, peer, _ = self.c._spot_near(self.c.follow['target'], self.c.follow['distance'])
                goal, peer = vec(goal), vec(peer)
                stand = math.hypot(goal[0]-peer[0], goal[2]-peer[2])
                if stand <= 1.3:
                    follow_intent = dict(goal=goal, peer=peer, distance=stand,
                        segmentLength=runtime.admission.max_requested_distance or runtime.admission.max_distance,
                        maxEndpointDistance=min(1.3, stand+.3))
            except (KeyError, ValueError, LookupError, RuntimeError):
                self.status = 'waiting_for_fresh_observation'
                return
        if follow_intent is not None:
            # A rejected stationary target must not launch expensive preflights
            # every Actor. Only failed cold follow attempts are throttled;
            # successful movement and same-motor hot retarget stay unchanged.
            failure = getattr(runtime, 'last_prepare_decision', None)
            if (failure is not None and failure.status != 'ready'
                    and failure is not getattr(self, '_seen_follow_failure', None)):
                self._seen_follow_failure = failure
                previous = getattr(self, '_last_follow_attempt', None)
                if previous is not None and previous[0] is runtime:
                    self._follow_failure_cooldown = (*previous, self.clock()+.5)
            stopped = getattr(runtime, 'last_stop_diagnostic', None)
            stop_key = (id(runtime), stopped.get('operation')) if stopped is not None else None
            if stop_key is not None and stop_key != getattr(self, '_seen_follow_stop', None):
                self._seen_follow_stop = stop_key
                previous = getattr(self, '_last_follow_attempt', None)
                if previous is not None and previous[0] is runtime:
                    self._follow_failure_cooldown = (*previous, self.clock()+.5)
            cooldown = getattr(self, '_follow_failure_cooldown', None)
            if (cooldown is not None and cooldown[0] is runtime and self.clock() < cooldown[3]
                    and math.dist(start, cooldown[1]) <= .2
                    and math.dist(follow_intent['peer'], cooldown[2]) <= .2):
                self._desired = goal
                self.status = 'follow_failed_route_cooldown'
                return
        distance = math.dist(start, goal)
        slack = (getattr(self.c, '_terrain_follow_slack', .6)
                 if self._request_label == 'follow' else .6)
        if distance < slack:
            self.status = 'goal_within_standing_slack'
            return
        segment = runtime.admission.max_requested_distance or runtime.admission.max_distance
        if distance > segment:
            # Keep a micron inside the exact request bound; floating-point
            # subtraction/norm must not turn 6 m into 6.000000000000001 m.
            goal = tuple(a+(b-a)*((segment-1e-6)/distance) for a,b in zip(start, goal))
        runtime.request(goal, **({'follow_intent': follow_intent} if follow_intent is not None else {}))
        if follow_intent is not None:
            self._last_follow_attempt = (runtime, start, follow_intent['peer'])
        return True

    def _dispatch_after_hop_landing(self, result):
        # The outgoing landing Actor has already been constructed and committed.
        # Enqueue only the next cold job now; never sample or start another hop
        # here, and never interpret cancellation/airborne frames as contact.
        runtime = self.runtime
        co = getattr(runtime, '_follow_hop', None)
        if (not result.emitted or not result.needs_follow_resume or result.phase != 'landed'
                or runtime is None or co is None or co.active or co.suppress_ground_replan
                or self._closed or self._expired or self.clock() >= self.deadline
                or self._request_label != 'follow' or not self.c.follow
                or self.c._terrain_social_owns() or self.c.motion is not None
                or runtime._future is not None or runtime._intent is not None
                or runtime._prepared is not None):
            return False
        try:
            # Existing authorization below checks the current command revision,
            # scene/actor ownership and target freshness. It does not reuse an
            # old ground failure ticket or the previous landing's proof.
            self._next_follow_refresh = 0.
            self._refresh_follow_goal()
            if self._submit_desired():
                self.status = runtime.tick()  # cold executor dispatch only
                self.c.log('experimental_hop_chain_dispatch', phase='landed',
                           same_actor_tick=True, fresh_route_required=True)
                return True
        except Exception as error:
            self.last_error = type(error).__name__ + ':' + str(error)[:120]
            self._revoke('experimental_hop_landing_dispatch_error')
            self.status = 'blocked_runtime_error'
        return False

    def _dispatch_after_arrival(self, active):
        # Inspect only the last cached result from the Actor post above. Calling
        # arrived(now), pos(now), or sample(now) here would advance physics twice.
        cached = getattr(active, '_sample', None)
        if (active is None or getattr(cached, 'arrived', False) is not True
                or active.blocked or self.runtime is None or self.c.motion is not None
                or self._desired is None):
            return False
        try:
            if self._submit_desired():
                self.status = self.runtime.tick()  # dispatch cold work, no sampling
                return True
        except Exception as error:
            self.last_error = type(error).__name__ + ':' + str(error)[:120]
            self._revoke('experimental_arrival_dispatch_error')
            self.status = 'blocked_runtime_error'
        return False

    def _refresh_follow_goal(self):
        """Memory-only target refresh; no control-file/social ticks or sampling."""
        now = self.clock()
        if now < self._next_follow_refresh:
            return
        self._next_follow_refresh = now + .1
        follow, runtime = self.c.follow, self.runtime
        if (not follow or runtime is None or self.c._terrain_social_owns()
                or runtime._permission() is None):
            return
        if self.c.motion is not None and self.c.motion is not getattr(self.c, '_terrain_motion', None):
            return
        try:
            spot, peer, _ = self.c._spot_near(follow['target'], follow['distance'])
            current_goal = self.c.motion.b if self.c.motion is not None else self.c.position
            if math.hypot(spot[0]-current_goal[0], spot[2]-current_goal[2]) >= getattr(self.c, '_terrain_follow_slack', 1.):
                self._desired, self._request_label = vec(spot), 'follow'
                self._desired_follow_peer = vec(peer)
            elif self._request_label == 'follow':
                # The newest peer position supersedes an older queued follow.
                # Do not walk toward that stale goal after reaching this segment.
                self._desired = None
                self._desired_follow_peer = None
        except (KeyError, ValueError, LookupError, RuntimeError):
            # The normal lifecycle check handles a missing/stale peer.
            return

    def _try_follow_cold_handoff(self):
        """Bounded turn-only takeover after this Actor's explicit hot refusal.

        Ordinary straight following keeps its old motor. No second sample and
        no parallel prepare: revoke from the exact last published grounded pose,
        then the normal single-job scheduler prepares the latest follow intent.
        """
        c, runtime, now = self.c, self.runtime, self.clock()
        motion = c.motion
        reason = getattr(runtime, 'last_retarget_reason', None)
        accepted = getattr(self, '_accepted_follow_peer', None)
        peer = getattr(self, '_desired_follow_peer', None)
        def rejected():
            self._follow_cold_streak = None
            return False
        if (reason not in ('retarget_requires_cold_route', 'motor_policy_requires_cold_preflight')
                or self._request_label != 'follow' or not c.follow or self._desired is None
                or motion is None or runtime is None or motion is not runtime._motion
                or motion is not getattr(c, '_terrain_motion', None) or c._terrain_social_owns()
                or self._closed or self._expired or now >= self.deadline
                or runtime._future is not None or runtime._intent is not None or runtime._prepared is not None
                or accepted is None or accepted[0] is not motion or peer is None
                or now-getattr(self, '_last_follow_handoff', -math.inf) < 1.
                or runtime._permission() is None or not runtime._valid(runtime._motion_operation)):
            return rejected()
        sample = getattr(motion, '_sample', None)
        start, goal = vec(c.position), vec(self._desired)
        if (sample is None or sample.grounded is not True or sample.arrived or sample.blocked
                or motion.blocked or tuple(sample.position) != start):
            return rejected()
        old = (motion.b[0]-start[0], motion.b[2]-start[2])
        new = (goal[0]-start[0], goal[2]-start[2])
        remaining, new_distance = math.hypot(*old), math.hypot(*new)
        peer_change = math.hypot(peer[0]-accepted[1][0], peer[2]-accepted[1][2])
        goal_change = math.hypot(goal[0]-motion.b[0], goal[2]-motion.b[2])
        if remaining <= .75 or new_distance < .25 or peer_change < 1.25 or goal_change < 1.:
            return rejected()
        cosine = (old[0]*new[0]+old[1]*new[1])/(remaining*new_distance)
        # Same-direction movement never causes periodic cold-stop chasing.
        if cosine > math.cos(math.radians(30.)):
            return rejected()
        streak = getattr(self, '_follow_cold_streak', None)
        if streak is not None and streak[0] is motion and 0. <= now-streak[2] < .05:
            return False
        count = streak[1]+1 if (streak is not None and streak[0] is motion
                               and .05 <= now-streak[2] <= .35) else 1
        self._follow_cold_streak = (motion, count, now)
        if count < 2:
            return False
        # Recheck permissions/owner immediately before revocation. Never stop a
        # social owner or publish a stale coordinate, even if a command raced.
        if (c.motion is not motion or c._terrain_social_owns() or not c.follow
                or runtime._permission() is None or not runtime._valid(runtime._motion_operation)
                or vec(c.position) != start):
            return rejected()
        runtime.cancel('follow_cold_handoff')
        if not c._terrain_failed('follow_cold_handoff', expected=motion):
            return rejected()
        self._last_follow_handoff = now
        self._follow_cold_streak = None
        self.c.log('experimental_ground_follow_handoff', reason=reason,
                   peer_change=round(peer_change, 3), remaining=round(remaining, 3),
                   turn_degrees=round(math.degrees(math.acos(max(-1., min(1., cosine)))), 1),
                   same_published_position=True)
        return True

    def _revoke(self, reason):
        if self.runtime is not None:
            previous, self.runtime = self.runtime, None
            previous.cancel()
            if previous._motion is not None:
                self.c._terrain_failed(reason, expected=previous._motion)
            self._executor.submit(previous.shutdown)
        self.c.attach_terrain(_ClosedGate(), lambda: None, prepared=False)

    def _exhaust_worker_recovery(self):
        self._worker_recovery_pending = False
        self._worker_recovery_exhausted = True
        self._desired = None
        self._revoke('experimental_worker_recovery_exhausted')
        self.status = 'stopped_worker_recovery_exhausted'
        self.c.log('experimental_ground_worker_recovery_exhausted', attempts=self._worker_recovery_attempts)

    def _rebind_returned_follow_target(self):
        """Rebuild a returned UUID's admission; never reuse its old slot proof."""
        runtime, binding = self.runtime, getattr(self, '_hop_dispatch', None)
        if (runtime is None or binding is None or self._future is not None
                or self._request_label != 'follow' or self._closed or self._expired
                or self.clock() >= self.deadline):
            return False
        with binding.lock:
            c, co = self.c, getattr(runtime, '_follow_hop', None)
            if (not binding.is_follow_current() or c._terrain_social_owns()
                    or co is not None and co.active
                    or c.motion is not None and c.motion is not runtime._motion):
                return False
            ctx, t = runtime.admission.context, c.tracker
            target = c.follow.get('target')
            # Only the explicitly followed full identity may return. Numeric
            # slot reuse must not silently follow a different occupant.
            if (type(target) is not str or target != ctx['target']['uuid']
                    or str(uuid.UUID(target)) != target):
                return False
            life = runtime.lifecycle()
            if (life is None or not life.connected or not life.target_fresh
                    or life.scene.scene_name != ctx['sceneName']
                    or life.scene.scene_hash != ctx['sceneHash']
                    or life.scene.room_generation != ctx['roomGeneration']):
                return False
            peer, slot = life.target_key.split('@slot=')
            if peer != target or (int(slot) == ctx['target']['slot']
                    and life.target_incarnation == ctx['target']['incarnation']):
                return False
            if not t.lock.acquire(False):
                return False
            try:
                own = ctx['bot']
                if (t._own_social_binding_locked() != own['slot'] or t.own_uuid != own['uuid']
                        or t._slot_versions.get(own['slot']) != own['incarnation']):
                    return False
            finally:
                t.lock.release()
            if runtime.lifecycle() != life:
                return False
            # Leave position, explicit follow object/revision, and the absolute
            # session deadline unchanged. All old motion/proof ownership ends.
            if co is not None:
                co.hard_cancel('follow_target_reincarnated')
            self._revoke('experimental_follow_target_reincarnated')
            self._desired = self._desired_follow_peer = self._accepted_follow_peer = None
            self._follow_cold_streak = None
            self._scene_follow_pending = True
            self._next_follow_refresh = 0.
            self._cold_build_attempts, self._cold_build_next, self._cold_build_exhausted = 0, 0., False
            self.status = 'rebuilding_returned_follow_target'
            c.log('experimental_follow_target_rebind', reason='fresh_target_incarnation_changed',
                same_uuid=True, old_physics_reused=False)
            return True

    def _rebind_seat_target(self):
        """T23-8：座位参照的人换了 slot/incarnation（或换了参照）就重建准入。

        旧逻辑只给 follow 做 target reincarnation 重建，seat 请求在参照对象
        换了 slot 后永远 subject_binding_or_observation_changed，10 s 都起不了步。
        这里仍要求当前 scene、own binding 和目标新鲜都成立，只是用新 slot 重新
        build 一次 admission，不放宽任何校验。
        """
        runtime = self.runtime
        if (runtime is None or self._future is not None or self._request_label != 'seat'
                or type(self._request_target) not in (str, int) or self._closed
                or self._expired or self.clock() >= self.deadline):
            return False
        if self.c._terrain_social_owns() or self.c.motion is not None:
            return False
        c, t = self.c, self.c.tracker
        ctx = runtime.admission.context
        target = self._request_target
        life = runtime.lifecycle()
        if (life is None or not life.connected or not life.target_fresh
                or life.scene.scene_name != ctx['sceneName']
                or life.scene.scene_hash != ctx['sceneHash']
                or life.scene.room_generation != ctx['roomGeneration']):
            return False
        peer, slot = life.target_key.split('@slot=')
        if type(target) is str and peer != target:
            return False
        if int(slot) == ctx['target']['slot'] and life.target_incarnation == ctx['target']['incarnation']:
            return False
        if not t.lock.acquire(False):
            return False
        try:
            own = ctx['bot']
            if (t._own_social_binding_locked() != own['slot'] or t.own_uuid != own['uuid']
                    or t._slot_versions.get(own['slot']) != own['incarnation']):
                return False
        finally:
            t.lock.release()
        if runtime.lifecycle() != life:
            return False
        self._revoke('experimental_seat_target_reincarnated')
        self._scene_follow_pending = False
        self._follow_cold_streak = None
        self._next_follow_refresh = 0.
        self._cold_build_attempts, self._cold_build_next, self._cold_build_exhausted = 0, 0., False
        self.status = 'rebuilding_seat_target'
        c.log('experimental_seat_target_rebind', same_uuid=(peer == ctx['target']['uuid']),
              old_slot=ctx['target']['slot'], new_slot=int(slot), old_physics_reused=False)
        return True

    def _recover_failed_worker(self):
        r = self.runtime
        transport = getattr(getattr(r, 'backend', None), 'transport', None)
        if (transport is None or not transport.failed or not self.c.follow
                or self._request_label != 'follow' or self.c._terrain_social_owns()):
            return False
        if r._permission() is None:
            self.status = 'waiting_for_fresh_follow_recovery'
            return True
        if self._worker_recovery_attempts >= 2:
            self._exhaust_worker_recovery()
            return True
        # Snapshot only the current observed target; never sample a motor here.
        try: spot, _, _ = self.c._spot_near(self.c.follow['target'], self.c.follow['distance'])
        except (KeyError, ValueError, LookupError, RuntimeError):
            self.status = 'waiting_for_fresh_follow_recovery'
            return True
        self._desired = vec(spot)
        self._worker_recovery_timeout = getattr(transport, '_failure', None) == 'worker_timeout'
        self._worker_recovery_lifecycle = r.lifecycle
        self._worker_recovery_pending = True
        self._worker_recovery_next = max(self._worker_recovery_next, self.clock()+2.)
        self.c.log('experimental_ground_worker_recovery_scheduled',
                   attempt=self._worker_recovery_attempts+1, worker=_worker_snapshot(r.backend))
        # Existing revoke queues shutdown on the cold executor. It never closes
        # or starts a process from this Actor callback.
        self._revoke('experimental_worker_failed')
        self.status = 'worker_recovery_backoff'
        return True

    def _queue_worker_recovery(self, config, generation):
        if not getattr(self, '_worker_recovery_pending', False):
            return False
        self.status = 'worker_recovery_backoff'
        if self.clock() < self._worker_recovery_next:
            return True
        if (not self.c.follow or self._request_label != 'follow' or self.c._terrain_social_owns()):
            self.status = 'waiting_for_fresh_follow_recovery'
            return True
        try: life = self._worker_recovery_lifecycle()
        except Exception: life = None
        if life is None or not life.connected or not life.target_fresh:
            self.status = 'waiting_for_fresh_follow_recovery'
            return True
        if self._worker_recovery_attempts >= 2:
            self._exhaust_worker_recovery()
            return True
        self._worker_recovery_attempts += 1
        self._worker_recovery_next = self.clock()+2.
        self._worker_recovery_pending = False
        self._worker_recovery_build = True
        self._future = self._executor.submit(self._build, dict(config), generation)
        self.status = 'rebuilding_failed_worker'
        return True

    def tick(self):
        if self._closed:
            return
        if self.clock() >= self.deadline:
            self._expired = True
        try:
            with self._config_lock:
                config, generation = self._config, self._config_generation
            if self._expired or config is None:
                self._discard_pending_build()
                self._desired = self._desired_follow_peer = None
                self._revoke('experimental_expired_or_config_invalid')
                self.status = 'stopped'
                return
            if getattr(self, '_worker_recovery_exhausted', False):
                self._desired = None
                self.status = 'stopped_worker_recovery_exhausted'
                return
            if self._follow_settings_generation != generation:
                movement = config.get('movement', {})
                for key, attribute in (('followDistance', '_terrain_follow_distance'), ('followSlack', '_terrain_follow_slack')):
                    if key in movement:
                        setattr(self.c, attribute, movement[key])
                    else:
                        self.c.__dict__.pop(attribute, None)
                self._follow_settings_generation = generation
            if 'catalogPath' in config or 'bindingRow' in config:
                routed = self._route_scene_config(config, generation)
                if routed is None:
                    return
                config, generation = routed
            if self.runtime is not None and self._active_generation != generation:
                self._revoke('experimental_offset_or_config_changed')
            self._rebind_returned_follow_target()
            if self._rebind_seat_target():
                return
            if self._recover_failed_worker():
                return
            if self._future is None and self._queue_worker_recovery(config, generation):
                return
            if self._future is not None:
                if self._request_label == 'follow' and not self.c.follow:
                    self._discard_pending_build(refund=True)
                    self._desired = self._desired_follow_peer = None
                    self._scene_follow_pending = False
                    self.status = 'movement_cancelled'
                    return
                if not self._future.done():
                    return
                future = self._future
                try:
                    built_generation, runtime = future.result()
                    if generation != built_generation:
                        self._future = None
                        self._executor.submit(runtime.shutdown)
                        if getattr(self, '_worker_recovery_build', False):
                            self._worker_recovery_build = False
                            self._worker_recovery_pending = True
                            self._worker_recovery_next = self.clock()+2.
                        return
                    action, reason = self._pending_install_action(runtime, config)
                    if self._defer_pending_install(action, reason, runtime):
                        return
                    try:
                        runtime.install()
                    except Exception as error:
                        if str(error) != 'pilot_admission_not_current':
                            raise
                        # The observation may change between precheck and install.
                        # Keep the same unloaded-to-Actor runtime; no new process.
                        action, reason = self._pending_install_action(runtime, config)
                        if action == 'ready':
                            action, reason = 'wait', 'admission_changed_during_install'
                        if self._defer_pending_install(action, reason, runtime):
                            return
                        raise
                    self._future = None
                    self._pending_install_runtime = self._pending_permission_reason = None
                    self._bind_runtime_deadline(runtime)
                    self.runtime, self._active_generation = runtime, generation
                    from hop_follow_runtime_v20 import install_entry_hop
                    install_entry_hop(self, runtime, config)
                    self._worker_recovery_build = False
                    self._scene_follow_pending = False
                    self.last_error = None
                    self.status = 'experimental_ready'
                    self.c.log('experimental_ground_ready', offset=list(runtime.admission.bot_offset),
                               scene=config.get('sceneName'),
                               parameter_source='engineering_trial_not_native_matched',
                            native_collision_verified=False, calibration_verified=False,
                            **self._pose_diagnostic(runtime))
                except Exception as error:
                    self._future = None
                    self._pending_install_runtime = self._pending_permission_reason = None
                    self.last_error = type(error).__name__ + ':' + str(error)[:120]
                    if 'runtime' in locals():
                        self._executor.submit(runtime.shutdown)
                    self.status = 'waiting_for_fresh_room_target_or_geometry'
                    if getattr(self, '_worker_recovery_build', False):
                        self._worker_recovery_build = False
                        if self._worker_recovery_attempts >= 2:
                            self._exhaust_worker_recovery()
                        else:
                            self._worker_recovery_pending = True
                            self._worker_recovery_next = self.clock()+2.
                    else:
                        self._cold_build_next = self.clock()+2.
                        if getattr(self, '_cold_build_attempts', 0) >= 2:
                            self._cold_build_exhausted = True
                            self.status = 'blocked_scene_load_exhausted'
                    return
            if self.runtime is None:
                # Build only when a user movement request exists, not on a room
                # connection alone. At most one cold task is outstanding.
                if getattr(self, '_cold_build_exhausted', False):
                    self.status = 'blocked_scene_load_exhausted'
                    return
                if self.clock() < getattr(self, '_cold_build_next', 0.):
                    self.status = 'scene_load_backoff'
                    return
                if self._desired is not None or (getattr(self, '_scene_follow_pending', False) and self.c.follow):
                    if not self._fresh_build_target(config):
                        self.status = 'waiting_for_fresh_room_target_or_geometry'
                        return
                    self._cold_build_attempts = getattr(self, '_cold_build_attempts', 0)+1
                    self._future = self._executor.submit(self._build, dict(config), generation)
                return
            if self.c._terrain_social_owns():
                self._follow_cold_streak = None
                self.status = 'waiting_for_social_release'
                return
            self.status = self.runtime.tick()
            self._refresh_follow_goal()
            if (self._request_label == 'follow' and self._desired is not None
                    and self.c.motion is not None and self.c.follow):
                if self.runtime.try_retarget(self._desired):
                    if getattr(self, '_desired_follow_peer', None) is not None:
                        self._accepted_follow_peer = (self.c.motion, self._desired_follow_peer)
                    self._desired = self._desired_follow_peer = None
                    self._follow_cold_streak = None
                    self.status = 'moving'
                    self.c.log('experimental_ground_retargeted',
                               distance=self.runtime.last_retarget_distance,
                               same_motor=True, time_advanced=False)
                else:
                    self._try_follow_cold_handoff()
            else:
                self._follow_cold_streak = None
            if self._submit_desired():
                # Do not wait another Actor period before starting cold prepare.
                self.status = self.runtime.tick()
            motion = self.runtime._motion
            if motion is not None and self.c.motion is motion and motion is not self._reported_motion:
                self._reported_motion = motion
                attempt = getattr(self, '_last_follow_attempt', None)
                if attempt is not None and attempt[0] is self.runtime:
                    self._accepted_follow_peer = (motion, attempt[2])
                else:
                    self._accepted_follow_peer = None
                self._follow_cold_streak = None
                self.c.log('experimental_ground_route_started', label=self._request_label,
                           engine_kind='imported_nav_engineering_motor',
                           motor_policy=getattr(self.runtime.backend, 'last_motion_policy', 'default_step_0_3'),
                           horizontal_speed_limit=self.runtime.admission.max_speed,
                           native_collision_verified=False, calibration_verified=False)
        except Exception as error:
            self.last_error = type(error).__name__ + ':' + str(error)[:120]
            self._revoke('experimental_runtime_error')
            self.status = 'blocked_runtime_error'

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._watch_stop.set()
        self._revoke('experimental_shutdown')
        self._discard_pending_build()
        # _revoke queues the owned backend shutdown. Cancelling all queued jobs
        # here could cancel that cleanup itself when another cold task is busy.
        # Outstanding builds have the dispose callback above and never install
        # after _closed; drain them and the cleanup rather than leaking Node.
        self._executor.shutdown(wait=False, cancel_futures=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot-config', required=True)
    args, runner_args = parser.parse_known_args(argv)
    path = Path(args.pilot_config).resolve(strict=True)
    require(path.stat().st_size <= 65536, 'config_budget')
    config = validate_config(json.loads(path.read_bytes()))
    session_deadline = launch_session_deadline(config)  # Once per runner, not per room.
    # Load the unchanged production runner FIRST: its top-level bootstrap adds
    # the qfsky/server dependency path required when room_pose imports.
    from bot import long_runner
    # Production long_runner.main uses bare `import room_pose`, NOT
    # `from bot import room_pose`. Patching the package-qualified copy leaves
    # the actual controller completely unhooked. Bind both names to that exact
    # production import identity before invoking the unchanged runner.
    room_pose = importlib.import_module('room_pose')
    require(Path(room_pose.__file__).resolve() == Path(__file__).with_name('room_pose.py').resolve(),
            'candidate_room_pose_module_mismatch')
    sys.modules['bot.room_pose'] = room_pose
    setattr(importlib.import_module('bot'), 'room_pose', room_pose)
    install = room_pose.install
    hooks = []
    def candidate_install(*a, **kw):
        for previous in hooks:
            previous.close()
        hooks.clear()
        tracker, ctl = install(*a, **kw)
        hook = ExperimentalGroundHook(ctl, config, config_path=path, session_deadline=session_deadline)
        hooks.append(hook)
        # Preserve the separately frozen passive observer bootstrap.
        try:
            observer_root = Path(__file__).resolve().parents[1]
            if observer_root.name == 'stage-pilot-candidate-v1':
                observer_root = observer_root.parent
            sys.path.insert(0, str(observer_root / 'tools'))
            from hop_observer_bootstrap_v20 import install_from_context
            observer = install_from_context(room_pose, ctl, hook)
            hooks.append(observer)
        except Exception:
            ctl.log('passive_hop_observer_failed', reason='bootstrap_unavailable')
        return tracker, ctl
    room_pose.install = candidate_install
    sys.argv = [sys.argv[0], *(runner_args[1:] if runner_args[:1] == ['--'] else runner_args)]
    try:
        return long_runner.main()
    finally:
        for hook in hooks:
            hook.close()
        room_pose.install = install


if __name__ == '__main__':
    main()
