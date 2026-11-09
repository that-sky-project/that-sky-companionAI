"""Evidence-only destination identity resolver. No RPC, transport, or movement.

``resolved`` certifies a name/scene identity, NOT permission to move a party.
Scene arrivals are native scene_spawn requests, never invented coordinates.
The router must still require accepted attachment and current room ownership.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import xg_paths

SCHEMA = "sky-destination-registry-v25"
COMMAND = re.compile(r"^\s*[/／]\s*(?:请)?(?:带我去|带我们去|牵着我去|背我去|背着我去|抱我去|抱着我去|公主抱我去)\s*(.+?)\s*[！!。．.]?\s*$")
RUNTIME_REQUIREMENTS = (
    "requester_current_room_and_identity",
    "accepted_leader_attachment",
    "account_scene_capture_and_destination_ticket",
    "new_room_peer_presence_after_handoff",
)


def _key(value: str) -> str:
    # 2026-11-06：宽松比对——忽略空格、括号、冒号、间隔点、横线，以及结尾的“场景”二字。
    s = unicodedata.normalize("NFKC", value).casefold()
    s = re.sub(r"[\s·・\.:：()（）\[\]【】,，、\-_]+", "", s)
    return re.sub(r"场景$", "", s)


class DestinationResolver:
    def __init__(self, registry: dict):
        if not isinstance(registry, dict) or registry.get("schema") != SCHEMA:
            raise ValueError("destination_registry_schema")
        records = registry.get("records")
        if not isinstance(records, list) or len(records) > 1024:
            raise ValueError("destination_registry_records")
        self._records = {}
        self._aliases = {}
        for incoming in records:
            row = copy.deepcopy(incoming)
            rid = row.get("id")
            if not isinstance(rid, str) or not rid or rid in self._records:
                raise ValueError("destination_duplicate_id")
            if row.get("kind") not in {"scene", "landmark"}:
                raise ValueError("destination_kind")
            status = row.get("verification")
            if status not in {"scene_identity_verified", "unverified_landmark"}:
                raise ValueError("destination_verification")
            if status == "scene_identity_verified":
                scene_hash = row.get("sceneHash")
                if (row["kind"] != "scene" or not isinstance(row.get("sceneName"), str)
                        or not row["sceneName"] or type(scene_hash) is not int
                        or not 0 <= scene_hash <= 0xFFFFFFFF
                        or row.get("arrival") != {"kind": "scene_spawn", "position": None}):
                    raise ValueError("destination_scene_identity")
            elif row.get("arrival") is not None:
                raise ValueError("unverified_destination_has_executable_arrival")
            if not isinstance(row.get("provenance"), list) or not row["provenance"]:
                raise ValueError("destination_provenance_missing")
            aliases = row.get("aliases")
            if not isinstance(aliases, list) or not aliases:
                raise ValueError("destination_aliases")
            self._records[rid] = row
            for alias in aliases:
                if not isinstance(alias, str) or not alias.strip() or len(alias) > 160:
                    raise ValueError("destination_alias")
                ids = self._aliases.setdefault(_key(alias), [])
                if rid not in ids:
                    ids.append(rid)

    @classmethod
    def from_file(cls, path: str | Path, expected_sha256: str | None = None):
        raw = Path(path).read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("destination_registry_too_large")
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("destination_registry_pin_mismatch")
        return cls(json.loads(raw))

    def resolve(self, destination: str) -> dict:
        """Resolve an already-extracted name. Does not imply slash authorization."""
        base = {"navigationAuthorized": False, "runtimeRequirements": list(RUNTIME_REQUIREMENTS)}
        if not isinstance(destination, str) or not destination.strip() or len(destination) > 160:
            return {**base, "status": "unknown", "query": None, "destination": None}
        ids = self._aliases.get(_key(destination), [])
        if not ids:
            return {**base, "status": "unknown", "query": destination.strip(), "destination": None}
        if len(ids) != 1:
            return {**base, "status": "ambiguous", "query": destination.strip(),
                    "destination": None, "candidates": [copy.deepcopy(self._records[r]) for r in ids]}
        row = copy.deepcopy(self._records[ids[0]])
        status = "resolved" if row["verification"] == "scene_identity_verified" else "unverified_landmark"
        return {**base, "status": status, "query": destination.strip(), "destination": row}

    def resolve_command(self, text: str) -> dict:
        """Only explicit slash-prefixed escort commands enter the resolver."""
        if not isinstance(text, str) or len(text) > 512 or "\n" in text or "\r" in text:
            return {"status": "not_travel_command", "navigationAuthorized": False, "destination": None}
        match = COMMAND.fullmatch(text)
        if not match:
            return {"status": "not_travel_command", "navigationAuthorized": False, "destination": None}
        result = self.resolve(match.group(1))
        result["explicitSlashCommand"] = True
        return result


def load_default() -> DestinationResolver:
    p = xg_paths.destination_records()
    if not Path(p).is_file():
        raise FileNotFoundError("destination-records-v25.json 还没生成")
    return DestinationResolver.from_file(Path(p))
