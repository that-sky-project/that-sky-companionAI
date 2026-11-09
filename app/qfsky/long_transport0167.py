"""Bounded, ordered application groups for the current long-connection driver.

This layer receives COMPLETE plaintext application packets. It never creates or
changes peer/type/application-counter bytes, performs receive/service, or waits
for ACKs inside a group. The ordinary owner loop services reliable ACKs later.

Evidence: the supplied native_enet C wrapper passes flags 1 or 0, then flushes.
The actual supplied ELF (SHA below) enet_peer_send@0x5c60 chooses fragment data
limit MTU-32 with checksum enabled; at 0x5d00 flags&9==8 is required for CMD12.
Thus wrapper flags=0 selects CMD8|ACK (0x5d14), even when business reliable=False.
For small flags=0 packets, unreliable sequence 65535 selects CMD6|ACK (0x5ec0).
enet_peer_setup_outgoing_command@0x5a39 resets the channel unreliable sequence
to zero for every reliable command. These native transport fallbacks are explicit
in receipts/metadata; the caller's business reliable flag is never rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import socket
import struct
import time
from . import enet
from .handshake0167_transport import ProbeTransport, ProbeTransportError


MAX_APPLICATION_BYTES = 0x2720
MAX_UNRELIABLE_BYTES = enet.MTU - 32
NATIVE_ENET_SHA256 = '56d0b170c7e0b76796d009629bcd1286c5cae12d5e030964393ea2514ba67bd1'


@dataclass(frozen=True)
class OutboundFrame:
    payload: bytes = field(repr=False)
    channel: int = 0
    reliable: bool = False


@dataclass(frozen=True)
class GroupSendResult:
    sent_frames: int
    sent_datagrams: int
    native_fragment_fallback: int = 0
    native_sequence_fallback: int = 0


@dataclass(frozen=True)
class ReliableToken:
    """Local receipt identity, not an application counter or server acceptance."""
    kind: str
    channel: int
    sequences: tuple[int, ...]


class GroupSendError(ProbeTransportError):
    """A terminal application-send failure; the whole group MUST NOT be replayed."""
    code = 'frame_group_send_failed'

    def __init__(self, sent_frames, sent_datagrams, failed_frame_index):
        super().__init__(self.code)
        self.sent_frames = sent_frames
        self.sent_datagrams = sent_datagrams
        self.failed_frame_index = failed_frame_index
        self.replay_allowed = False


def _bound(value, lower, upper, label):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(label + ' bound is invalid')
    return value


class LongTransport(ProbeTransport):
    """Synchronous enqueue/send only; no receive thread or automatic group retry."""

    def __init__(self, *args, max_group_frames=16, max_group_bytes=32768,
                 max_group_datagrams=64, max_groups=8192,
                 max_pending_commands=128, max_pending_bytes=131072,
                 max_upstream_datagrams=16384, **kwargs):
        if os.environ.get('CLAUDEWORK_LONG') == '1':  # CLAUDEWORK_LONG_T
            _span = int(os.environ.get('CLAUDEWORK_MAX_DURATION', '86400'))
            if max_groups == 8192: max_groups = max(max_groups, _span * 40)  # 只放大默认值
            if max_upstream_datagrams == 16384: max_upstream_datagrams = max(max_upstream_datagrams, _span * 64)
            _gcap = _ucap = 2**31
        else:
            _gcap, _ucap = 16384, 65536
        self.max_group_frames = _bound(max_group_frames, 1, 64, 'frame group')
        self.max_group_bytes = _bound(max_group_bytes, 6, 65536, 'group bytes')
        self.max_group_datagrams = _bound(max_group_datagrams, 1, 128, 'group datagrams')
        self.max_groups = _bound(max_groups, 1, _gcap, 'group count')
        self.max_pending_commands = _bound(max_pending_commands, 1, 512, 'pending commands')
        self.max_pending_bytes = _bound(max_pending_bytes, 1, 2 * 1024 * 1024, 'pending bytes')
        self.max_upstream_datagrams = _bound(max_upstream_datagrams, 1, _ucap, 'upstream datagrams')
        self.groups_sent = 0
        self.group_send_failed = False
        self.native_fragment_fallbacks = 0
        self.native_sequence_fallbacks = 0
        self._receipts = {}
        self._disconnect_phase = 'idle'
        self._disconnect_reason = None
        self._disconnect_key = None
        self._disconnect_acked = False
        self._forced_close = False
        self.disconnect_sent = False
        super().__init__(*args, **kwargs)
        # One owner can pump old/new transports without a blocking receive wait.
        self.socket.settimeout(0.0)

    def send_frame_group(self, frames):
        """Send a bounded list/tuple in order, with no recv/service/wait between.

        Validation and queue-capacity checks cover the entire group before the
        first send. All planned transport sequences are reserved before sending;
        a partial failure never rolls them back and permanently stops further
        application groups on this transport. A successful return means queued
        locally/sent, not that the peer acknowledged or accepted the application.
        """
        if not isinstance(frames, (list, tuple)):
            raise ValueError('frames must be a bounded list or tuple')
        if not 1 <= len(frames) <= self.max_group_frames:
            raise ValueError('frame group count bound reached')
        # Freeze caller-owned mutable lists before validation/sequence planning.
        frames = tuple(frames)
        if not 1 <= len(frames) <= self.max_group_frames:
            raise ValueError('frame group count bound reached')
        total_bytes = 0
        for frame in frames:
            if not isinstance(frame, OutboundFrame):
                raise ValueError('frames must contain OutboundFrame values')
            if type(frame.payload) is not bytes or not 6 <= len(frame.payload) <= MAX_APPLICATION_BYTES:
                raise ValueError('complete application packet size/type is invalid')
            if type(frame.channel) is not int or frame.channel not in (0, 1):
                raise ValueError('application channel must be 0 or 1')
            if type(frame.reliable) is not bool:
                raise ValueError('reliable must be a boolean')
            total_bytes += len(frame.payload)
        if total_bytes > self.max_group_bytes:
            raise ValueError('frame group byte bound reached')

        with self.io_lock, self.seq_lock:
            if self._disconnect_phase != 'idle':
                raise ProbeTransportError('disconnect requested; new application sends forbidden')
            if self.group_send_failed:
                raise ProbeTransportError('application group transport is terminal')
            self._check()
            if self.state != enet.ST_CONNECTED or not self.join_sent or not self.registration_acked:
                raise ProbeTransportError('frame group requires an acknowledged current registration')
            if not isinstance(self.app_rc4_key, bytes) or len(self.app_rc4_key) != 32:
                raise ProbeTransportError('current application RC4 key is unavailable')
            if self.groups_sent >= self.max_groups:
                raise ProbeTransportError('application group count bound reached')
            reliable = dict(self.outgoing_reliable_seq)
            unreliable = dict(self.unreliable_seq)
            plan = []
            keys = set(self.pending_cmds)
            pending_count = len(keys)
            pending_bytes = sum(len(row[0].encode()) for row in self.pending_cmds.values())
            for frame in frames:
                # Fresh RC4 state for every full application packet, before any
                # reliable fragmentation. Application counters remain untouched.
                encrypted = self._codec_app_payload(frame.payload)
                channel = frame.channel
                fragment_fallback = not frame.reliable and len(encrypted) > MAX_UNRELIABLE_BYTES
                sequence_fallback = not frame.reliable and not fragment_fallback and unreliable.get(channel, 0) >= 0xffff
                tracked = frame.reliable or fragment_fallback or sequence_fallback
                if tracked:
                    start = (reliable.get(channel, 0) + 1) & 0xffff
                    commands = ([enet.build_send_reliable(channel, start, encrypted)]
                                if len(encrypted) <= enet.MTU - 32 else
                                enet.build_send_fragment_commands(channel, start, encrypted))
                    reliable[channel] = (start + len(commands) - 1) & 0xffff
                    unreliable[channel] = 0
                    for command in commands:
                        key = (channel, command.reliable_seq)
                        if key in keys:
                            raise ProbeTransportError('reliable sequence is still pending')
                        keys.add(key)
                        pending_count += 1
                        pending_bytes += len(command.encode())
                else:
                    previous = unreliable.get(channel, 0)
                    unreliable[channel] = previous + 1
                    commands = [enet.build_send_unreliable(channel, reliable.get(channel, 0), previous + 1, encrypted)]
                plan.append((tracked, commands, int(fragment_fallback), int(sequence_fallback)))
            datagrams = sum(len(row[1]) for row in plan)
            if datagrams > self.max_group_datagrams or self.pkt_sent + datagrams > self.max_upstream_datagrams:
                raise ProbeTransportError('application group datagram bound reached')
            if pending_count > self.max_pending_commands or pending_bytes > self.max_pending_bytes:
                raise ProbeTransportError('application reliable queue bound reached')

            self.outgoing_reliable_seq = reliable
            self.unreliable_seq = unreliable
            sent_frames = sent_datagrams = 0
            fragments_used = sequences_used = 0
            try:
                for frame_index, (tracked, commands, fragment_fallback, sequence_fallback) in enumerate(plan):
                    for command in commands:
                        self._check()
                        self._emit([command], track=tracked)
                        sent_datagrams += 1
                    sent_frames += 1
                    fragments_used += fragment_fallback
                    sequences_used += sequence_fallback
                    self.native_fragment_fallbacks += fragment_fallback
                    self.native_sequence_fallbacks += sequence_fallback
            except Exception:
                self.group_send_failed = True
                self.transport_failed_reason = 'application frame group send failed'
                raise GroupSendError(sent_frames, sent_datagrams, frame_index) from None
            self.groups_sent += 1
            return GroupSendResult(sent_frames, sent_datagrams, fragments_used, sequences_used)


    def _emit(self, commands, is_new_conn=False, track=True):
        if commands and self.pkt_sent >= self.max_upstream_datagrams:
            raise ProbeTransportError('upstream datagram bound reached')
        return super()._emit(commands, is_new_conn=is_new_conn, track=track)

    def _queue_handshake(self, payload, kind):
        if type(payload) is not bytes or not 6 <= len(payload) <= MAX_APPLICATION_BYTES:
            raise ValueError('complete handshake payload size/type is invalid')
        with self.io_lock, self.seq_lock:
            self._check()
            if self._disconnect_phase != 'idle' or self.group_send_failed:
                raise ProbeTransportError('handshake transport is terminal')
            if self.state != enet.ST_CONNECTED:
                raise ProbeTransportError('ENet is not connected')
            if kind == 'join' and self.join_sent:
                raise ProbeTransportError('JoinGame may be queued only once')
            if kind == 'registration' and (not self.join_sent or kind in self._receipts or self.registration_acked):
                raise ProbeTransportError('registration requires one preceding JoinGame')
            if not isinstance(self.app_rc4_key, bytes) or len(self.app_rc4_key) != 32:
                raise ProbeTransportError('current application RC4 key is unavailable')
            wire = payload if kind == 'join' else self._codec_app_payload(payload)
            start = (self.outgoing_reliable_seq.get(0, 0) + 1) & 0xffff
            commands = ([enet.build_send_reliable(0, start, wire)] if len(wire) <= enet.MTU-32
                        else enet.build_send_fragment_commands(0, start, wire))
            keys = {(0, c.reliable_seq) for c in commands}
            pending_bytes = sum(len(row[0].encode()) for row in self.pending_cmds.values())
            if keys.intersection(self.pending_cmds):
                raise ProbeTransportError('reliable sequence is still pending')
            if len(self.pending_cmds)+len(commands)>self.max_pending_commands or pending_bytes+sum(len(c.encode()) for c in commands)>self.max_pending_bytes:
                raise ProbeTransportError('handshake reliable queue bound reached')
            if len(commands)>self.max_group_datagrams or self.pkt_sent+len(commands)>self.max_upstream_datagrams:
                raise ProbeTransportError('handshake datagram bound reached')
            token = ReliableToken(kind, 0, tuple(c.reliable_seq for c in commands))
            self._receipts[kind] = {'token':token, 'pending':set(keys), 'state':'pending'}
            self.outgoing_reliable_seq[0] = commands[-1].reliable_seq
            self.unreliable_seq[0] = 0
            if kind == 'join':
                # Receive RC4 was enabled in __init__; this marker precedes ANY send.
                self.join_sent = True
            sent = 0
            try:
                for command in commands:
                    self._check()
                    self._emit([command], track=True)
                    sent += 1
            except Exception:
                self._receipts[kind]['state']='failed'
                self.group_send_failed=True
                self.transport_failed_reason='handshake queue send failed'
                raise GroupSendError(0, sent, 0) from None
            return token

    def queue_join(self, payload):
        """Atomically emit plaintext Join fragments; return without recv/ACK wait."""
        return self._queue_handshake(payload, 'join')

    def queue_registration(self, payload):
        """Full caller-owned plaintext packet, independent RC4; no counter edits."""
        return self._queue_handshake(payload, 'registration')

    def reliable_status(self, token):
        """Only observed matching-channel ACKs establish 'acked', never queue absence."""
        with self.io_lock:
            row = self._receipts.get(getattr(token, 'kind', None))
            if row is None or row['token'] is not token:
                raise ValueError('reliable token does not belong to this transport')
            if row['state']=='pending' and (self.closed or self.group_send_failed or self.receive_error or
                    self.transport_failed_reason or self.state==enet.ST_DISCONNECTED):
                row['state']='failed'
            return row['state']

    def _dispatch_command(self, command, sent_time):
        key = None
        if command.cmd_type == enet.CMD_ACKNOWLEDGE and len(command.body)==4:
            key=(command.chan,struct.unpack_from('>H',command.body)[0])
        was_pending=key is not None and key in self.pending_cmds
        super()._dispatch_command(command,sent_time)
        if was_pending:
            for kind,row in self._receipts.items():
                if row['state']=='pending' and key in row['pending']:
                    row['pending'].remove(key)
                    if not row['pending']:
                        row['state']='acked'
                        if kind=='registration':self.registration_acked=True
            if key==self._disconnect_key and self._disconnect_phase=='disconnect_sent':
                self._disconnect_acked=True
                self._disconnect_phase='graceful_complete'
                self.state=enet.ST_DISCONNECTED

    def disconnect_later(self, reason=2):
        """Retain queued reliable commands; caller must keep pumping service().

        Actual ELF: enet_peer_disconnect_later@0x6120 checks outgoing work,
        stores native state6/data when nonempty, otherwise calls disconnect.
        disconnect@0x6060 uses reliable CMD4|0x80 on channel255 for connected
        peers, big-endian reason, then state7. Socket closure is NOT flush.
        """
        if type(reason) is not int or not 0<=reason<=0xffffffff:
            raise ValueError('disconnect reason must be a uint32')
        with self.io_lock, self.seq_lock:
            if self._disconnect_phase!='idle':
                if reason!=self._disconnect_reason:
                    raise ValueError('disconnect reason cannot change after request')
                return self.disconnect_status()
            self._check()
            if self.state!=enet.ST_CONNECTED:
                raise ProbeTransportError('disconnect_later requires connected peer')
            self._disconnect_reason=reason
            self._disconnect_phase='draining'
            return self.flush()

    def _flush_acks(self):
        if len(self.pending_acks)>256:
            raise ProbeTransportError('pending ACK queue bound reached')
        while self.pending_acks:
            items=self.pending_acks[:8]
            self._emit([enet.build_ack_command(seq,stamp,chan) for seq,stamp,chan in items],track=False)
            del self.pending_acks[:len(items)]

    def flush(self):
        """Only local sends, no recv/wait or RTO processing; not an ACK guarantee.

        The native host_flush@0x7a10 calls outgoing-send(checkTimeouts=0),
        not host_service. Our payloads are already emitted when queued; flush
        drains local ACKs and emits one DISCONNECT after reliable work drains.
        """
        with self.io_lock, self.seq_lock:
            if self.closed or self._disconnect_phase=='graceful_complete':
                return self.disconnect_status()
            try:
                self._flush_acks()
                self._check()
                if self._disconnect_phase=='draining' and not self.pending_cmds:
                    sequence=(self.outgoing_reliable_seq.get(0xff,0)+1)&0xffff
                    command=enet.build_disconnect_command(0xff,sequence,self._disconnect_reason)
                    if len(command.encode())>self.max_pending_bytes:
                        raise ProbeTransportError('disconnect queue byte bound reached')
                    self.outgoing_reliable_seq[0xff]=sequence
                    self._disconnect_key=(0xff,sequence)
                    self._emit([command],track=False)
                    # Existing ENet emitter tracks only app reliable/fragment;
                    # explicitly track the native reliable disconnect command.
                    self.pending_cmds[self._disconnect_key]=(command,time.monotonic(),1)
                    self.disconnect_sent=True
                    self._disconnect_phase='disconnect_sent'
            except Exception:
                if self._disconnect_phase!='idle':self._disconnect_phase='failed'
                raise
            return self.disconnect_status()

    def service(self, max_drain=32):
        """Bounded nonblocking owner pump, suitable for interleaving two peers."""
        if type(max_drain) is not int or max_drain<0:
            raise ValueError('service drain bound is invalid')
        # Inherited ENet wait/settle paths request 128 (burst defaults to 96).
        # Accept their request size but retain our per-pump hard limit of 32.
        max_drain=min(32,max_drain)
        with self.io_lock:
            if self.closed or self._disconnect_phase=='graceful_complete':
                return self.disconnect_status()
            try:
                self._check()
                for _ in range(max_drain):
                    self._check()
                    try:data,sender=self.socket.recvfrom(enet.RECV_BUF)
                    except (BlockingIOError,socket.timeout,InterruptedError):break
                    if sender!=self.remote:
                        self.foreign_datagrams+=1
                        continue
                    self.pkt_recv+=1
                    if self.pkt_recv>self.max_datagrams:
                        raise ProbeTransportError('datagram count bound reached')
                    self._handle_datagram(data)
                    if self.state==enet.ST_DISCONNECTED:break
                self.flush()
                if self._disconnect_phase=='graceful_complete':return self.disconnect_status()
                self._retransmit_overdue()
                self._check()
                if self._disconnect_phase=='idle' and self.state==enet.ST_CONNECTED and time.monotonic()-self.last_ping_send>=enet.PING_INTERVAL_SEC:
                    sequence=(self.outgoing_reliable_seq.get(0xff,0)+1)&0xffff
                    self.outgoing_reliable_seq[0xff]=sequence
                    self._emit([enet.build_ping_command(0xff,sequence)],track=False)
                    self.last_ping_send=time.monotonic()
            except Exception:
                if self._disconnect_phase!='idle' and self._disconnect_phase!='graceful_complete':self._disconnect_phase='failed'
                raise
            return self.disconnect_status()

    def disconnect_status(self):
        """Fixed local state only; socket_closed is checked, not inferred from flag."""
        with self.io_lock:
            try:socket_closed=self.socket.fileno()==-1
            except Exception:socket_closed=False
            return {'phase':self._disconnect_phase,'reason':self._disconnect_reason,
                'disconnect_sent':self.disconnect_sent,'disconnect_acked':self._disconnect_acked,
                'graceful_complete':self._disconnect_phase=='graceful_complete',
                'pending_reliable':len(self.pending_cmds),'pending_ack_flush':len(self.pending_acks),
                'local_outbound_flushed':not self.pending_acks and self._disconnect_phase in ('disconnect_sent','graceful_complete'),
                'socket_closed':socket_closed,'forced_close':self._forced_close}

    def force_close(self):
        """Close socket now without a fake flush/ACK completion or duplicate packet."""
        with self.io_lock:
            if self.disconnect_status()['socket_closed']:return self.disconnect_status()
            if self._disconnect_phase!='graceful_complete':self._forced_close=True
            self.closed=True
            try:self.socket.close()
            except Exception:
                if self._disconnect_phase!='graceful_complete':self._disconnect_phase='failed'
                raise
            finally:self.state=enet.ST_DISCONNECTED
            return self.disconnect_status()

    def close(self, send_disconnect=True):
        # Legacy close remains best-effort; once graceful close was requested it
        # must neither overwrite its reason nor send a second DISCONNECT.
        with self.io_lock:
            if self._disconnect_phase!='idle' or self.closed:
                return self.force_close()
            self._forced_close=True
            return super().close(send_disconnect=send_disconnect)

    def group_metadata(self):
        """Fixed counters only; no payloads, identifiers, endpoints or templates."""
        return {'groups_sent':self.groups_sent,'group_send_failed':self.group_send_failed,
                'native_fragment_fallback':self.native_fragment_fallbacks,
                'native_sequence_fallback':self.native_sequence_fallbacks}
