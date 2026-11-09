"""Requested versus validated map membership; no network or account access."""
import json
import os
from pathlib import Path
import threading


class MapSessionState:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.segment = 0
        self.scene_revision = 0
        self._same_peer_revision = 0
        self._local_announcement = False
        self.requested = None
        self.confirmed = None
        self.connected = False
        self.phase = "idle"
        self._published = None
        self._open = False

    def _publish(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        state = {"segment": self.segment, "requested_map": self.requested,
                 "confirmed_map": self.confirmed, "connected": self.connected,
                 "phase": self.phase, "scene_revision": self.scene_revision}
        if self._local_announcement:
            state.update(same_peer_scene_revision=self._same_peer_revision,
                         locally_announced_map=self.requested,
                         membership_provenance="self_announced_local_registration_acked",
                         server_roster_received=False)
        encoded = json.dumps(state, sort_keys=True) + "\n"
        if encoded == self._published:
            return
        target = self.directory / "current_map.state.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(encoded, encoding="utf-8")
        os.replace(temp, target)
        self._published = encoded

    def begin(self, map_id, segment):
        if type(map_id) is not int or not 0 < map_id <= 0xffffffff:
            raise ValueError("map_state_invalid_map")
        with self.lock:
            if type(segment) is not int or segment <= self.segment:
                raise ValueError("map_state_stale_segment")
            self.segment, self.requested = segment, map_id
            self.scene_revision += 1
            self._same_peer_revision = 0
            self._local_announcement = False
            self._open = True
            self.connected, self.phase = False, "pending"
            self._publish()

    def announce_same_peer(self, map_id, segment, *, expected_map_id, expected_revision):
        """ACKed local scene change, NOT an authenticated destination roster.

        This method runs after the new engine is installed on the same pump
        thread. Connection segment IDs retain their original meaning.
        """
        if type(map_id) is not int or not 0 < map_id <= 0xffffffff:
            raise ValueError("map_state_invalid_map")
        with self.lock:
            if (type(segment) is not int or segment != self.segment or not self._open
                    or type(expected_revision) is not int
                    or expected_revision != self._same_peer_revision
                    or type(expected_map_id) is not int or expected_map_id != self.requested
                    or map_id == self.requested):
                raise ValueError("map_state_stale_scene")
            self.scene_revision += 1
            self._same_peer_revision += 1
            self._local_announcement = True
            self.requested, self.confirmed = map_id, None
            self.connected, self.phase = True, "local_registration_acked"
            # current_map describes our active local scene, with its explicit
            # weaker provenance recorded alongside it in current_map.state.json.
            target = self.directory / "current_map"
            temp = target.with_suffix(".tmp")
            temp.write_text(str(map_id), encoding="utf-8")
            os.replace(temp, target)
            self._publish()
            return self._same_peer_revision

    def observe(self, segment, update, *, report_accepted=False):
        with self.lock:
            if segment != self.segment or not self._open:
                return False
            if self._local_announcement:
                if (type(update.get("same_peer_scene_revision")) is not int
                        or update["same_peer_scene_revision"] != self._same_peer_revision
                        or type(update.get("map_id")) is not int
                        or update["map_id"] != self.requested):
                    return False
            elif update.get("same_peer_scene_revision", 0) != 0:
                return False
            phase = update.get("phase")
            self.phase = phase or self.phase
            # connection0167 emits this only after validate_pending_enter_game
            # has matched this attempt's own account and requested map.
            valid = update.get("enter_game_valid") is True
            active = phase in ("registration", "observing") and update.get("running") is True
            if self._local_announcement:
                # Never inherit enter_game_valid=True from the source scene.
                # The same-peer ACK proves a local announcement, not native
                # acceptance, a new roster, or the companion's attachment.
                if phase in ("failed", "stopped", "completed"):
                    self.connected, self._open = False, False
                elif active:
                    self.connected = (update.get("local_registration_acked") is True
                        and update.get("membership_provenance") ==
                            "self_announced_local_registration_acked")
                self._publish()
                return True if report_accepted else self.connected
            if valid and active:
                if not self.connected or self.confirmed != self.requested:
                    target = self.directory / "current_map"
                    temp = target.with_suffix(".tmp")
                    temp.write_text(str(self.requested), encoding="utf-8")
                    os.replace(temp, target)
                self.confirmed, self.connected = self.requested, True
            elif phase in ("failed", "stopped", "completed"):
                self.connected, self._open = False, False
            self._publish()
            return True if report_accepted else self.connected

    def finish(self, segment):
        with self.lock:
            if segment != self.segment:
                return False
            self.connected, self._open = False, False
            self.phase = "disconnected"
            self._publish()
            return True
