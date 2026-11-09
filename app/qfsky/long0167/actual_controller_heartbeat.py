"""RAM-only native Game12 controller description for one actual room.

This describes accepted controller input, not scene loading or local authority.
No account, file, network, historical heartbeat, or transport access occurs here.
"""
import struct
from .scope import ConnectionScope as RecordedUplinkScope
from .level_authority import LevelRoleObservation


class ActualControllerHeartbeat:
    def __init__(self, *, scope):
        if type(scope) is not RecordedUplinkScope:
            raise ValueError('heartbeat_scope_invalid')
        scope.validate()
        self._scope = scope
        self._ready = False
        self._owner = self._version = self._updates = 0
        self._last_kind = None

    def _check(self, scope_provider, current_roster):
        current = scope_provider()
        if type(current) is not RecordedUplinkScope:
            raise ValueError('heartbeat_scope_invalid')
        current.validate()
        before = self._scope
        if (current.owner_uuid != before.owner_uuid
                or current.peer_identity is not before.peer_identity
                or current.room_generation is not before.room_generation
                or current.auth_generation is not before.auth_generation
                or current.peer_epoch != before.peer_epoch
                or current.own_slot != before.own_slot
                or current.map_id != before.map_id):
            raise ValueError('heartbeat_scope_changed')
        if type(current_roster) not in (tuple, list, frozenset, set):
            raise ValueError('heartbeat_roster_invalid')
        if (not 1 <= len(current_roster) <= 8
                or any(type(i) is not int or not 1 <= i <= 255 for i in current_roster)
                or len(set(current_roster)) != len(current_roster)
                or current.own_slot not in current_roster):
            raise ValueError('heartbeat_roster_invalid')
        return current, frozenset(current_roster)

    def accept(self, observation, *, scope_provider, current_roster):
        """Caller passes the exact observation from its actual bound receiver."""
        current, roster = self._check(scope_provider, current_roster)
        if type(observation) is not LevelRoleObservation:
            raise ValueError('heartbeat_observation_invalid')
        if (observation.kind not in ('LevelData_Elect', 'LevelData_Revoke', 'LevelData_Snapshot')
                or observation.level_id != current.map_id
                or type(observation.sender_network_id) is not int
                or (observation.sender_network_id != 0 and observation.sender_network_id not in roster)
                or type(observation.owner_network_id) is not int
                or (observation.owner_network_id != 0 and observation.owner_network_id not in roster)
                or type(observation.version) is not int
                or not 0 <= observation.version <= 65535):
            raise ValueError('heartbeat_observation_not_current')
        # Native Revoke invalidates owner, but preserves its received version
        # and marks the controller initialized. It does not reset all fields.
        self._owner = 0 if observation.kind == 'LevelData_Revoke' else observation.owner_network_id
        self._version = observation.version
        self._ready = True
        self._updates += 1
        self._last_kind = observation.kind
        return self.metadata()

    def build_payload(self, *, scope_provider, current_roster):
        current, roster = self._check(scope_provider, current_roster)
        if self._owner and self._owner not in roster:
            # A later roster cannot revive a departed handle. Require new
            # actual role input before adopting any owner again.
            self._owner = 0
        return (bytes((12, current.peer_epoch, 0, int(self._ready), self._owner))
                + struct.pack('<IH', current.map_id, self._version))

    def metadata(self):
        return {'actual_input_seen': self._updates > 0,
                'native_controller_initialized': self._ready,
                'owner_is_own': self._owner == self._scope.own_slot,
                'owner_is_unassigned': self._owner == 0,
                'version_from_actual_input': self._updates > 0,
                'actual_updates': self._updates,
                'last_actual_kind': self._last_kind,
                'scene_loaded_verified': False,
                'local_authority_granted': False,
                'historical_heartbeat_used': False}
