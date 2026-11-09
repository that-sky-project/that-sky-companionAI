"""Explicit, offline-only scene preparation; never call prepare_scope from packet post.

resolve(scene_hash, scene_name) must return one exact catalog row (or None), with
scene_hash/scene_name/asset_relpath/read_status/collision_status/nav_status and
calibration_verified/native_collision_verified. Only three ``ready`` statuses
allow an offline load. backend_factory(row, scope) must return the existing
WorkerBackend interface. No asset/scene fallback, environment activation, or
controller attachment is performed here. Current workers are OFFLINE ONLY.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
import threading

from .integration import TerrainIntegration


@dataclass(frozen=True)
class SceneScope:
    scene_hash: int
    scene_name: str
    room_generation: int

    def __post_init__(self):
        if type(self.scene_hash) is not int or not 0 <= self.scene_hash <= 0xffffffff:
            raise ValueError("scene_hash")
        if (type(self.scene_name) is not str or not self.scene_name
                or self.scene_name != self.scene_name.strip()
                or len(self.scene_name) > 256 or any(ord(c) < 32 for c in self.scene_name)):
            raise ValueError("scene_name")
        if type(self.room_generation) is not int or self.room_generation < 0:
            raise ValueError("room_generation")


@dataclass(frozen=True)
class SessionState:
    status: str
    reason: str | None
    scope: SceneScope | None
    mode: str
    asset_relpath: str | None = None
    read_status: str | None = None
    collision_status: str | None = None
    nav_status: str | None = None
    calibration_verified: bool = False
    native_collision_verified: bool = False


@dataclass(frozen=True)
class _Binding:
    scope: SceneScope
    backend: object
    adapter: TerrainIntegration
    state: SessionState


class TerrainSession:
    """Cold-path scene owner. mode is mandatory; no default/live activation.

    ``prepared_adapter(scope)`` is a nonblocking, memory-only lookup. Use a
    stable scope/provider in a controller; equal immutable SceneScope values
    resolve the same binding. This does not attach to or change room_pose.
    Switching/invalidate closes the old helper OUTSIDE the state lock. Old
    motions are disabled first; generation tickets reject late prepare results.
    """

    def __init__(self, resolve, backend_factory, *, mode, arrival_tolerance=0.4):
        if mode not in ("offline", "live"):
            raise ValueError("mode")
        if not callable(resolve) or not callable(backend_factory):
            raise TypeError("resolver_and_factory_required")
        self._resolve, self._factory = resolve, backend_factory
        self.mode = mode
        self._arrival_tolerance = arrival_tolerance
        self._lock = threading.Lock()
        self._generation = 0
        self._active = None
        self._closed = False
        self._asset_owners = {}
        self._state = SessionState("unavailable", "unprepared", None, mode)

    @staticmethod
    def _stop(binding):
        if binding is None:
            return True
        ok = True
        # Only references captured for this binding are touched. Never close a
        # newer scene or do I/O while holding the session state lock.
        for operation in (lambda: binding.adapter.set_enabled(False),
                          binding.adapter.invalidate,
                          binding.backend.invalidate,
                          binding.backend.close):
            try:
                operation()
            except Exception:
                ok = False
        return ok

    def snapshot(self):
        if not self._lock.acquire(blocking=False):
            return SessionState("unavailable", "state_busy", None, self.mode)
        try:
            return self._state
        finally:
            self._lock.release()

    def prepared_adapter(self, scope):
        """No prepare/restart/filesystem/worker/property calls on this path."""
        if not isinstance(scope, SceneScope) or not self._lock.acquire(blocking=False):
            return None
        try:
            active = self._active
            if not self._closed and active is not None and active.scope == scope:
                return active.adapter
            return None
        finally:
            self._lock.release()

    def prepared_scope(self, scope):
        """Canonical stable scope object for direct adapter users (not I/O)."""
        if not isinstance(scope, SceneScope) or not self._lock.acquire(blocking=False):
            return None
        try:
            active = self._active
            return active.scope if active is not None and active.scope == scope else None
        finally:
            self._lock.release()

    def _finish(self, ticket, state):
        with self._lock:
            if ticket != self._generation or self._closed:
                return SessionState("unavailable", "superseded", state.scope, self.mode)
            self._state = state
            return state

    def _state_for(self, status, reason, scope, row=None):
        row = row or {}
        return SessionState(status, reason, scope, self.mode,
                            row.get("asset_relpath"), row.get("read_status"),
                            row.get("collision_status"), row.get("nav_status"),
                            row.get("calibration_verified") is True,
                            row.get("native_collision_verified") is True)

    def prepare_scope(self, scope):
        """Explicit COLD operation. Even an unsupported switch retires old map.

        Factory/prepare/close may block; none hold _lock. Concurrent calls may
        prepare different candidates but only the newest ticket can publish.
        A failed or stale result never restores the previous scene.
        """
        with self._lock:
            if self._closed:
                return self._state_for("unavailable", "closed", None)
            self._generation += 1
            ticket, old = self._generation, self._active
            self._active = None
            self._state = self._state_for("preparing", None,
                                           scope if isinstance(scope, SceneScope) else None)
        if not self._stop(old):
            return self._finish(ticket, self._state_for("unavailable", "cleanup_failed", None))
        if not isinstance(scope, SceneScope):
            return self._finish(ticket, self._state_for("unavailable", "invalid_scope", None))
        # Lookup is also outside lock: a future catalog might perform I/O.
        try:
            row = self._resolve(scope.scene_hash, scope.scene_name)
        except Exception:
            return self._finish(ticket, self._state_for("unavailable", "catalog_failed", scope))
        if row is None:
            return self._finish(ticket, self._state_for("unsupported", "scene_unsupported", scope))
        if not isinstance(row, Mapping):
            return self._finish(ticket, self._state_for("unavailable", "invalid_catalog_entry", scope))
        row = dict(row)
        if (type(row.get("scene_hash")) is not int or row.get("scene_hash") != scope.scene_hash
                or row.get("scene_name") != scope.scene_name):
            return self._finish(ticket, self._state_for("unavailable", "scene_identity_mismatch", scope))
        asset = row.get("asset_relpath")
        if (not isinstance(asset, str) or not asset or "\\" in asset or ":" in asset
                or PurePosixPath(asset).is_absolute()
                or any(p in ("", ".", "..") for p in asset.split("/"))
                or any(ord(c) < 32 for c in asset)):
            return self._finish(ticket, self._state_for("unavailable", "asset_missing", scope, row))
        for field, reason in (("read_status", "asset_unreadable"),
                              ("collision_status", "collision_unavailable"),
                              ("nav_status", "navigation_unavailable")):
            if row.get(field) != "ready":
                return self._finish(ticket, self._state_for("unavailable", reason, scope, row))
        # Never let a multi-map catalog silently substitute the same CandleSpace
        # (or other scene) manifest for a different scene identity.
        with self._lock:
            if ticket != self._generation or self._closed:
                return self._state_for("unavailable", "superseded", scope, row)
            owner = self._asset_owners.setdefault(asset, (scope.scene_hash, scope.scene_name))
        if owner != (scope.scene_hash, scope.scene_name):
            return self._finish(ticket, self._state_for("unavailable", "asset_scene_mismatch", scope, row))
        if self.mode == "live":
            reason = ("native_collision_unverified" if row.get("native_collision_verified") is not True
                      else "calibration_unverified" if row.get("calibration_verified") is not True
                      else "offline_backend_not_live")
            return self._finish(ticket, self._state_for("unavailable", reason, scope, row))
        backend = None
        adapter = None
        try:
            backend = self._factory(dict(row), scope)
            if getattr(getattr(backend, "transport", None), "offline", False) is not True:
                failure = "offline_backend_required"
            else:
                result = backend.prepare()
                failure = (None if getattr(result, "status", None) == "ready"
                           and backend.prepared is True else "load_failed")
            if failure is None:
                adapter = TerrainIntegration(backend, enabled=False,
                                             arrival_tolerance=self._arrival_tolerance)
                adapter.bind_scope(scope)
        except Exception:
            failure = "load_failed"
        if failure is not None:
            if backend is not None:
                self._stop(_Binding(scope, backend, adapter or TerrainIntegration(enabled=False),
                                    self._state_for("unavailable", failure, scope, row)))
            return self._finish(ticket, self._state_for("unavailable", failure, scope, row))
        state = self._state_for("ready", None, scope, row)
        binding = _Binding(scope, backend, adapter, state)
        with self._lock:
            current = ticket == self._generation and not self._closed
            if current:
                adapter.set_enabled(True)  # Empty local adapter, no worker or I/O.
                self._active, self._state = binding, state
        if not current:
            self._stop(binding)
            return self._state_for("unavailable", "superseded", scope, row)
        return state

    def invalidate(self, reason="invalidated"):
        # Keep external strings out of status/logs. This is a local owner stop,
        # not a claim that a network/game operation was performed.
        with self._lock:
            self._generation += 1
            old, self._active = self._active, None
            self._state = self._state_for("unavailable", "invalidated", None)
        return self._stop(old)

    def close(self):
        with self._lock:
            self._generation += 1
            self._closed = True
            old, self._active = self._active, None
            self._state = self._state_for("unavailable", "closed", None)
        return self._stop(old)
