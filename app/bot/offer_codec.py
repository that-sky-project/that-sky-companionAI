"""Private, network-free native status1 construction for an observed status0.

This module is independent of friend_offer_codec's round-trip-only contract.
Native acceptance preserves the original offerer/recipient body roles B,A.
The current accepting A's same-response session3 supplies the terminal; UUID
and tag remain zero in the ordinary native constructor. No server rewrite,
successful unlock, outgoing Game2 context, transport authentication, or send
is inferred here. No network/file I/O or logging is implemented.

The trusted caller must hold its current account/room lock, supply the actual
current authenticated transport's raw PlayerSync and GameMsg, and invalidate
on transport/roster changes. Raw packet parsing proves equality and framing,
not server provenance. Scope references and generation counters are LOCAL
binding values; none is a protocol ID. Synthetic inputs are for tests only.
"""
# Isolated v3 research copy; formal service helper is unchanged.
# Original public source SHA256: 72f37d661363791fd68c4336ef72efbe3c113d1166967ceca0fd4035e9ffce7b
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import re
import struct
import uuid

class OnlineAuthContext:  # claudework：只用于类型检查，构造改由 offer_accept.py 完成
    pass


OFFER_KEY = 0x20D9B9B3
MAX_RPC_BODY = 0x1500
MAX_GAME_BYTES = 20000
MAX_RPC_FRAMES = 512
REFERENCE = re.compile(r"[A-Za-z0-9_-]{40,64}\Z")
REASONS = frozenset({"invalid_scope", "invalid_clock", "invalid_roster",
    "invalid_game", "stale_epoch", "wrong_source", "invalid_context",
    "invalid_stream", "invalid_offer", "invalid_auth_context", "own_mismatch",
    "no_pending_offer", "pending_expired", "pending_changed", "scope_changed",
    "authorization_mismatch", "acceptance_already_built", "invalid_terminal", "cancelled"})


class AcceptanceError(ValueError):
    def __init__(self, reason="invalid_offer"):
        self.reason = reason if reason in REASONS else "invalid_offer"
        super().__init__(self.reason)

    def __repr__(self):
        return "AcceptanceError(" + self.reason + ")"


def _uint(value, bits, positive=False, reason="invalid_scope"):
    if type(value) is not int or not int(positive) <= value < 1 << bits:
        raise AcceptanceError(reason)
    return value


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise AcceptanceError("invalid_clock")
    return float(value)


@dataclass(frozen=True, repr=False)
class AcceptanceScope:
    owner_id: int
    account_identity: str = field(repr=False)
    auth_generation: int
    room_reference: str = field(repr=False)
    room_generation: int
    roster_generation: int
    expected_level_id: int
    peer_epoch: int
    own_uuid: bytes = field(repr=False)
    friend_uuid: bytes = field(repr=False)

    def __post_init__(self):
        for value in (self.owner_id, self.auth_generation, self.room_generation,
                      self.roster_generation):
            _uint(value, 63, positive=True)
        _uint(self.expected_level_id, 32, positive=True)
        _uint(self.peer_epoch, 8)
        for value in (self.account_identity, self.room_reference):
            if type(value) is not str or not REFERENCE.fullmatch(value):
                raise AcceptanceError("invalid_scope")
        for value in (self.own_uuid, self.friend_uuid):
            if type(value) is not bytes or len(value) != 16 or not any(value):
                raise AcceptanceError("invalid_scope")
        if self.own_uuid == self.friend_uuid:
            raise AcceptanceError("invalid_scope")

    def __repr__(self):
        return "AcceptanceScope(<private current binding>)"


def _roster(raw, scope):
    if type(scope) is not AcceptanceScope:
        raise AcceptanceError("invalid_scope")
    if type(raw) is not bytes or not raw or raw[0] & 0xf0:
        raise AcceptanceError("invalid_roster")
    count = raw[0] & 15
    if not 2 <= count <= 8 or len(raw) != 1 + count * 37:
        raise AcceptanceError("invalid_roster")
    by_uuid, by_slot = {}, {}
    for index in range(count):
        offset = 1 + 37 * index
        slot, user = raw[offset], raw[offset + 1:offset + 17]
        level = struct.unpack_from("<I", raw, offset + 33)[0]
        if not slot or not any(user) or slot in by_slot or user in by_uuid:
            raise AcceptanceError("invalid_roster")
        by_uuid[user], by_slot[slot] = (slot, level), level
    own, friend = by_uuid.get(scope.own_uuid), by_uuid.get(scope.friend_uuid)
    if (own is None or friend is None or own[1] != scope.expected_level_id
            or friend[1] != scope.expected_level_id):
        raise AcceptanceError("invalid_roster")
    return own[0], friend[0], by_slot, hashlib.sha256(raw).digest()


@dataclass(frozen=True, repr=False)
class _Offer:
    status: int
    ability_id: int
    cost: int
    opaque32: bytes = field(repr=False)
    identity: bytes = field(repr=False)
    fingerprint: bytes = field(repr=False)


def _offers(game, raw, scope, *, completion_sentinel=False):
    own, friend, slots, roster_digest = _roster(raw, scope)
    if type(game) is not bytes or not 4 <= len(game) <= MAX_GAME_BYTES or game[0] != 2:
        raise AcceptanceError("invalid_game")
    if game[1] != scope.peer_epoch:
        raise AcceptanceError("stale_epoch")
    if game[2] != friend:
        raise AcceptanceError("wrong_source")
    # The extra context is a valid current-map row, NOT the body recipient.
    if game[3] not in slots or slots[game[3]] != scope.expected_level_id:
        raise AcceptanceError("invalid_context")
    offset, frames, matches = 4, 0, []
    while offset < len(game):
        if frames >= MAX_RPC_FRAMES or len(game) - offset < 7:
            raise AcceptanceError("invalid_stream")
        key, header, size = struct.unpack_from("<IBH", game, offset)
        end = offset + 7 + size
        if header != 0 or size & 0xe000 or size > MAX_RPC_BODY or end > len(game):
            raise AcceptanceError("invalid_stream")
        body = game[offset + 7:end]
        offset, frames = end, frames + 1
        if key != OFFER_KEY:
            continue
        if size < 40:
            raise AcceptanceError("invalid_offer")
        first, second, status = body[:3]
        if status not in (0, 1, 2, 3):
            raise AcceptanceError("invalid_offer")
        if first != friend or not (second == own or
                (completion_sentinel and status == 2 and second == 0)):
            continue  # Not an addressed B,A offer; sentinel completion is not actionable.
        ability_id, cost = struct.unpack_from("<II", body, 3)
        csize = struct.unpack_from("<I", body, 31)[0]
        if not 1 <= csize <= 32 or 35 + csize + 4 > size:
            raise AcceptanceError("invalid_offer")
        cstring = body[35:35 + csize]
        if cstring[-1:] != b"\0" or b"\0" in cstring[:-1]:
            raise AcceptanceError("invalid_offer")
        terminal_offset = 35 + csize
        terminal_size = struct.unpack_from("<I", body, terminal_offset)[0]
        if terminal_offset + 4 + terminal_size != size:
            raise AcceptanceError("invalid_offer")
        if status == 0 and not ability_id:
            raise AcceptanceError("invalid_offer")
        # Only a private digest is retained; incoming terminal is not retained.
        # A retry with a different irrelevant incoming terminal still denotes
        # the same observed invitation; do not let it revive a consumed ticket.
        matches.append(_Offer(status, ability_id, cost, cstring[:-1],
                              body[11:31], hashlib.sha256(body[:terminal_offset]).digest()))
    return own, friend, roster_digest, tuple(matches)


@dataclass(frozen=True, repr=False)
class PendingTicket:
    generation: int
    _identity: object = field(repr=False)

    def __repr__(self):
        return "PendingTicket(<private local generation>)"


@dataclass(frozen=True, repr=False)
class CompletionReceipt:
    """Private local receipt for ONE actually sent invitation; never serialize it."""
    _scope: AcceptanceScope = field(repr=False)
    _roster_digest: bytes = field(repr=False)
    _ability_id: int
    _cost: int
    _opaque32: bytes = field(repr=False)
    _created_at: float
    _deadline: float
    _generation: int

    def __repr__(self):
        return "CompletionReceipt(<private one invitation; not proof of unlock>)"


def require_not_cancelled(cancel_event):
    if cancel_event is None or not callable(getattr(cancel_event, 'is_set', None)):
        raise AcceptanceError('invalid_scope')
    if cancel_event.is_set():
        raise AcceptanceError('cancelled')


def match_current_completion(receipt, game_payload, *, player_sync_payload, scope, now):
    """Validate current B source, exact roster, node/cost and opaque invitation.

    Native status2 may use recipient sentinel0. Relay-written UUID/tag/terminal
    are deliberately not used as invitation identity. No history set is read.
    Returning true still requires fresh official relationship verification.
    """
    if type(receipt) is not CompletionReceipt or type(scope) is not AcceptanceScope:
        return False
    now = _clock(now)
    if scope != receipt._scope or now < receipt._created_at or now >= receipt._deadline:
        return False
    try:
        _, _, digest, offers = _offers(game_payload, player_sync_payload, scope,
                                       completion_sentinel=True)
    except AcceptanceError:
        return False
    return bool(digest == receipt._roster_digest and any(
        offer.status == 2 and offer.ability_id == receipt._ability_id
        and offer.cost == receipt._cost and offer.opaque32 == receipt._opaque32
        for offer in offers))


@dataclass(frozen=True, repr=False)
class AcceptancePlan:
    """An RPC-only result. Its existence proves neither transport send nor unlock."""
    _rpc: bytes = field(repr=False)
    _completion: CompletionReceipt = field(repr=False)

    def private_completion_receipt(self):
        return self._completion

    def private_rpc_bytes(self):
        return self._rpc

    def metadata(self):
        return {"rpc_constructed": True, "rpc_status": 1,
                "outgoing_game_envelope_constructed": False,
                "packet_sent": False, "friend_tree_unlock_confirmed": False}

    def __repr__(self):
        return "AcceptancePlan(<private RPC; not sent; unlock unconfirmed>)"


@dataclass(repr=False)
class _Pending:
    ticket: PendingTicket
    offer: _Offer
    scope: AcceptanceScope
    roster_digest: bytes = field(repr=False)
    own_slot: int
    friend_slot: int
    received_at: float
    deadline: float


class PendingAcceptance:
    """One friend pair, bounded private deduplication, no scheduling or retries.

    On room/auth/roster-generation change use invalidate immediately. A changed
    scope also invalidates automatically at every call. A packet construction
    consumes its ticket BEFORE returning; callers must not retry an uncertain
    send. A fresh server relationship resync, not this helper, proves success.
    """
    def __init__(self, *, ttl_seconds=60):
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 1 <= ttl_seconds <= 180):
            raise AcceptanceError("invalid_clock")
        self._ttl = float(ttl_seconds)
        self._scope = None
        self._pending = None
        self._seen = set()
        self._generation = 0
        self._last_now = None

    def __repr__(self):
        return "PendingAcceptance(<private, no transport>)"

    def invalidate(self):
        self._scope = None
        self._pending = None
        self._seen.clear()
        self._generation += 1

    def _current(self, scope, now):
        if type(scope) is not AcceptanceScope:
            raise AcceptanceError("invalid_scope")
        now = _clock(now)
        if self._last_now is not None and now < self._last_now:
            self.invalidate()
            raise AcceptanceError("invalid_clock")
        self._last_now = now
        if scope != self._scope:
            self.invalidate()
            self._scope = scope
        if self._pending is not None and now >= self._pending.deadline:
            self._pending = None
        return now

    def observe(self, game_payload, *, player_sync_payload, scope, now):
        """Validate an entire incoming stream before staging its last offer.

        Duplicate packets never extend TTL or revive retired/consumed offers.
        Returns a private ticket or None; it does not authorize acceptance.
        """
        now = self._current(scope, now)
        try:
            own, friend, roster_digest, offers = _offers(game_payload, player_sync_payload, scope)
        except AcceptanceError:
            self._pending = None
            raise
        if self._pending is not None and roster_digest != self._pending.roster_digest:
            self._pending = None
        # Keep memory bounded. Fail closed until a new explicit room scope.
        if len(self._seen) >= 512:
            self._pending = None
            return None
        for offer in offers:
            if offer.status in (1, 2, 3):
                self._pending = None
                continue
            if offer.status != 0:
                continue
            if offer.fingerprint in self._seen:
                continue
            if len(self._seen) >= 512:
                self._pending = None
                return None
            self._seen.add(offer.fingerprint)
            self._generation += 1
            ticket = PendingTicket(self._generation, object())
            self._pending = _Pending(ticket, offer, scope, roster_digest, own, friend,
                                     now, now + self._ttl)
        return self._pending.ticket if self._pending is not None else None

    def pending_metadata(self, ticket, *, player_sync_payload, scope, now):
        """Only public node/cost, selected by the private exact fresh pending."""
        previous_scope, previous_pending = self._scope, self._pending
        now = self._current(scope, now)
        if previous_scope is not None and previous_scope != scope:
            raise AcceptanceError('scope_changed')
        pending = self._pending
        if pending is None:
            reason = 'pending_expired' if (previous_pending is not None
                     and now >= previous_pending.deadline) else 'no_pending_offer'
            raise AcceptanceError(reason)
        if (type(ticket) is not PendingTicket or ticket is not pending.ticket
                or ticket._identity is not pending.ticket._identity):
            raise AcceptanceError('pending_changed')
        own, friend, _, digest = _roster(player_sync_payload, scope)
        if (digest != pending.roster_digest or own != pending.own_slot
                or friend != pending.friend_slot):
            self._pending = None
            raise AcceptanceError('pending_changed')
        return {'ability_id': pending.offer.ability_id, 'ability_cost': pending.offer.cost}

    def build_once(self, ticket, *, player_sync_payload, scope, now,
                   auth_context, auth, session, expected_ability_id,
                   expected_ability_cost):
        """Build status1 using current private auth; no Game2 envelope or send.

        expected ability/cost must come from the caller's authorized current
        tree-unlock operation and validated ability metadata, never guessed.
        """
        previous_scope, previous_pending = self._scope, self._pending
        now = self._current(scope, now)
        if previous_scope is not None and previous_scope != scope:
            raise AcceptanceError("scope_changed")
        pending = self._pending
        if pending is None:
            reason = "pending_expired" if (previous_pending is not None
                     and now >= previous_pending.deadline) else "no_pending_offer"
            raise AcceptanceError(reason)
        if (type(ticket) is not PendingTicket or ticket is not pending.ticket
                or ticket._identity is not pending.ticket._identity):
            raise AcceptanceError("pending_changed")
        own, friend, _, digest = _roster(player_sync_payload, scope)
        if (digest != pending.roster_digest or own != pending.own_slot
                or friend != pending.friend_slot):
            self._pending = None
            raise AcceptanceError("pending_changed")
        _uint(expected_ability_id, 32, positive=True, reason="authorization_mismatch")
        _uint(expected_ability_cost, 32, reason="authorization_mismatch")
        if (expected_ability_id != pending.offer.ability_id
                or expected_ability_cost != pending.offer.cost):
            raise AcceptanceError("authorization_mismatch")
        if type(auth_context) is not OnlineAuthContext:
            raise AcceptanceError("invalid_auth_context")
        try:
            current = dict(owner_id=scope.owner_id, account_identity=scope.account_identity,
                           generation=scope.auth_generation, auth=auth, session=session)
            auth_context.validate_current(**current)
            if uuid.UUID(auth_context.user_uuid).bytes != scope.own_uuid:
                raise AcceptanceError("own_mismatch")
            terminal = auth_context.private_material(**current)["own_session3_text"].encode("utf8")
        except AcceptanceError:
            raise
        except Exception:
            raise AcceptanceError("invalid_auth_context") from None
        cstring = pending.offer.opaque32 + b"\0"
        body = (bytes((friend, own, 1)) + struct.pack("<II", pending.offer.ability_id,
                pending.offer.cost) + bytes(20) + struct.pack("<I", len(cstring))
                + cstring + struct.pack("<I", len(terminal)) + terminal)
        if not terminal or not 40 <= len(body) <= MAX_RPC_BODY:
            raise AcceptanceError("invalid_terminal")
        rpc = struct.pack("<IBH", OFFER_KEY, 0, len(body)) + body
        self._pending = None
        receipt = CompletionReceipt(scope, pending.roster_digest,
            pending.offer.ability_id, pending.offer.cost, pending.offer.opaque32,
            now, pending.deadline, pending.ticket.generation)
        return AcceptancePlan(rpc, receipt)
