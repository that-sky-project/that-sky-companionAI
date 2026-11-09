"""Pure one-shot new-room leader confirmation decision; no codec or I/O.

Grant facts MUST come from the trusted trip/commit observer, not chat/LLM data.
``confirm_once`` is a proposal, never success or permission to write peer Actor.
The native sender must atomically recheck its binding; failure cancels this gate.
"""
import math
from uuid import UUID


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def identity(v):
    try:
        return type(v) is str and str(UUID(v)) == v
    except (ValueError, TypeError, AttributeError):
        return False


def scene(v):
    return (isinstance(v, (tuple, list)) and len(v) == 4
        and type(v[0]) is int and 0 < v[0] < 2**32
        and type(v[1]) is str and bool(v[1])
        and all(type(x) is int and x >= 0 for x in v[2:]))


def occupant(v):
    return (isinstance(v, (tuple, list)) and len(v) == 2
        and type(v[0]) is int and 0 < v[0] < 256
        and type(v[1]) is int and v[1] >= 0)


class HandholdResumeGate:
    def __init__(self, grant, *, stable_seconds=.35, max_age=2.0):
        g = dict(grant)
        times = [g.get(k) for k in ('accepted_at', 'attached_at', 'commit_at', 'deadline')]
        if (g.get('kind') != 'handhold'
                or any(g.get(k) is not True for k in ('accepted', 'was_attached', 'committed'))
                or not all(finite(t) for t in times) or not times[0] <= times[1] <= times[2] < times[3]
                or not identity(g.get('bot_uuid')) or not identity(g.get('peer_uuid'))
                or g['bot_uuid'] == g['peer_uuid'] or not scene(g.get('source_scene'))
                or not occupant(g.get('source_bot')) or not occupant(g.get('source_peer'))
                or g['source_bot'][0] == g['source_peer'][0]
                or type(g.get('destination_map')) is not int or not 0 < g['destination_map'] < 2**32
                or g['destination_map'] == g['source_scene'][0]
                or type(g.get('revision')) is not int or g['revision'] < 1
                or type(g.get('request_id')) is not str or not 8 <= len(g['request_id']) <= 80
                or not finite(stable_seconds) or not 0 < stable_seconds <= 1
                or not finite(max_age) or not 0 < max_age <= 2):
            raise ValueError('invalid_handhold_resume_grant')
        g.update(source_scene=tuple(g['source_scene']), source_bot=tuple(g['source_bot']), source_peer=tuple(g['source_peer']))
        self._g, self.stable_seconds, self.max_age = g, stable_seconds, max_age
        self.state, self.reason = 'wait', 'fresh_destination_pair_required'
        self.last_now, self.domain, self.since = g['commit_at'], None, None
        self.first_seq, self.last_seq, self.issued_at = None, None, None

    def _result(self, action=None, **kw):
        return dict(action=action or self.state, reason=self.reason, **kw)

    def cancel(self, reason='cancelled_by_owner'):
        if self.state not in ('restored', 'cancelled', 'expired'):
            self.state, self.reason = 'cancelled', reason
        return self._result()

    def tick(self, now, pair, *, current_revision, owner_current, explicit_release=False):
        if self.state in ('restored', 'cancelled', 'expired'):
            return self._result()
        if not finite(now) or now < self.last_now:
            return self.cancel('clock_regressed_or_invalid')
        self.last_now = now
        g = self._g
        if type(current_revision) is not int or current_revision != g['revision'] or owner_current is not True:
            return self.cancel('ownership_changed')
        if explicit_release is True:
            return self.cancel('explicit_release')
        if now >= g['deadline']:
            self.state, self.reason = 'expired', 'trip_deadline'
            return self._result()
        if not isinstance(pair, dict) or pair.get('connected', True) is not True:
            self.since = None
            return self._result('wait')
        if pair.get('bot_uuid') != g['bot_uuid'] or pair.get('peer_uuid') != g['peer_uuid']:
            return self.cancel('identity_changed')
        s, a, b, seq = pair.get('scene'), pair.get('bot'), pair.get('peer'), pair.get('sample_seq')
        valid = (scene(s) and isinstance(a, (tuple, list)) and isinstance(b, (tuple, list))
            and len(a) == len(b) == 3 and occupant(a[:2]) and occupant(b[:2]) and a[0] != b[0]
            and all(finite(t) and g['commit_at'] <= t <= now and now-t <= self.max_age for t in (a[2], b[2]))
            and type(seq) is int and seq >= 0)
        if not valid:
            self.since = None
            return self._result('wait')
        s, a, b = tuple(s), tuple(a), tuple(b)
        if s == g['source_scene'] or s[1:] == g['source_scene'][1:]:
            self.since = None
            self.reason = 'new_room_domain_required'
            return self._result('wait')
        if s[0] != g['destination_map']:
            return self.cancel('unexpected_destination_map')
        leader, hold, carrier, stamp = (pair.get(k) for k in
            ('leader_slot', 'hold_state', 'carrier_slot', 'attachment_at'))
        if (type(leader) is not int or not 0 <= leader < 256
                or type(hold) is not int or hold not in (0, 1)
                or type(carrier) is not int or not 0 <= carrier < 256
                or not finite(stamp) or not g['commit_at'] <= stamp <= now or now-stamp > self.max_age):
            self.since = None
            self.reason = 'fresh_decoded_relationship_required'
            return self._result('wait')
        if carrier > 0 or leader not in (0, a[0]):
            return self.cancel('peer_attached_elsewhere')
        if (leader, hold) not in ((0, 0), (a[0], 0), (a[0], 1)):
            self.since = None
            self.reason = 'inconsistent_relationship_fields'
            return self._result('wait')
        domain = (s, a[:2], b[:2])
        if self.issued_at is not None and domain != self.domain:
            return self.cancel('new_room_binding_changed')
        if domain != self.domain or self.since is None:
            self.domain, self.since, self.first_seq, self.last_seq = domain, now, seq, seq
        if seq < self.last_seq:
            return self.cancel('observation_sequence_regressed')
        self.last_seq = seq
        if (leader == a[0] and hold == 1
                and finite(stamp) and max(g['commit_at'], self.issued_at or g['commit_at']) <= stamp <= now
                and now-stamp <= self.max_age):
            self.state, self.reason = 'restored', 'fresh_native_peer_attachment_observed'
            return self._result()
        if self.issued_at is not None:
            self.reason = 'awaiting_actual_peer_attachment'
            return self._result('wait')
        if now-self.since < self.stable_seconds or seq <= self.first_seq:
            self.reason = 'new_room_pair_stabilizing'
            return self._result('wait')
        self.issued_at, self.reason = now, 'one_leader_confirmation_proposed_not_success'
        return self._result('confirm_once', binding=dict(request_id=g['request_id'], revision=g['revision'],
            bot_uuid=g['bot_uuid'], peer_uuid=g['peer_uuid'], scene=s, bot=a[:2], peer=b[:2],
            issued_at=now, deadline=g['deadline']))
