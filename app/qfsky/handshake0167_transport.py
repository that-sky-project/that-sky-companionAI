"""Bounded single-connection transport for an explicitly requested 421589 probe.
No historical burst or gameplay messages. RC4 resets per reassembled ENet packet.
"""
import os
import socket
import struct
import time
from collections import deque
from . import enet
from .receive0167 import decode_server_plaintext

class ProbeTransportError(RuntimeError):
    pass

class ProbeTransport(enet.EnetClient):
    def __init__(self, ip, port, peer_key, *, sig_hex, decoder, archive,
                 timeout=30.0, max_transmissions=4, max_application_packets=512, max_datagrams=4096):
        _long = os.environ.get('CLAUDEWORK_LONG') == '1'  # CLAUDEWORK_LONG_T
        _tmax, _pmax, _dmax = (86400, 2**31, 2**31) if _long else (180, 8192, 16384)
        if not 3 <= timeout <= _tmax or not 1 <= max_transmissions <= 8:
            raise ValueError('transport bounds are invalid')
        if type(max_application_packets) is not int or not 1 <= max_application_packets <= _pmax:
            raise ValueError('application packet bound is invalid')
        if type(max_datagrams) is not int or not 1 <= max_datagrams <= _dmax:
            raise ValueError('datagram bound is invalid')
        self.max_application_packets = max_application_packets
        self.max_datagrams = max_datagrams
        super().__init__(ip, port, peer_key, egress_key='')
        self.deadline = time.monotonic() + timeout
        self.max_transmissions = max_transmissions
        self.decoder = decoder
        self.archive = archive
        self.messages = deque()
        self.receive_error = None
        self.join_sent = False
        self.registration_acked = False
        self.foreign_datagrams = 0
        self.raw_packets = 0
        self.remote_disconnect_code = None
        # Receive can occur inside reliable-fragment ACK processing. Set its key
        # before sending Join, while _send_payload(encrypt=False) keeps Join clear.
        self.enable_app_rc4(sig_hex)

    def _check(self):
        if getattr(self, 'closed', False):
            raise ProbeTransportError('transport is closed')
        if time.monotonic() >= self.deadline:
            raise ProbeTransportError('overall transport deadline reached')
        if self.receive_error:
            raise ProbeTransportError(self.receive_error)
        if self.transport_failed_reason:
            raise ProbeTransportError('reliable transport failed')
        if self.state == enet.ST_DISCONNECTED:
            raise ProbeTransportError('transport disconnected')

    def _emit(self, commands, is_new_conn=False, track=True):
        if not commands or self.closed:
            return
        sent = self.pkt_sent
        super()._emit(commands, is_new_conn=is_new_conn, track=track)
        if self.pkt_sent == sent:
            raise ProbeTransportError('UDP send failed')

    def _retransmit_overdue(self):
        now = time.monotonic()
        for key, (command, last, count) in list(self.pending_cmds.items()):
            if now - last < enet.RETRANSMIT_RTO_SEC:
                continue
            if count >= self.max_transmissions:
                self.transport_failed_reason = 'bounded reliable ACK timeout'
                self.state = enet.ST_DISCONNECTED
                return
            self._emit([command], track=False)
            self.pending_cmds[key] = (command, now, count + 1)

    def service(self, max_drain=32):
        self._check()
        with self.io_lock:
            if self.closed:
                return
            for _ in range(min(32, max_drain)):
                self._check()
                try:
                    data, sender = self.socket.recvfrom(enet.RECV_BUF)
                except (socket.timeout, InterruptedError):
                    break
                if sender != self.remote:
                    self.foreign_datagrams += 1
                    continue
                self.pkt_recv += 1
                if self.pkt_recv > getattr(self, 'max_datagrams', 4096):
                    raise ProbeTransportError('datagram count bound reached')
                self._handle_datagram(data)
            if self.pending_acks:
                commands = [enet.build_ack_command(seq, stamp, channel)
                            for seq, stamp, channel in self.pending_acks]
                self.pending_acks.clear()
                for index in range(0, len(commands), 8):
                    self._emit(commands[index:index + 8], track=False)
            self._retransmit_overdue()
            now = time.monotonic()
            if self.state == enet.ST_CONNECTED and now - self.last_ping_send >= enet.PING_INTERVAL_SEC:
                n = (self.outgoing_reliable_seq.get(0xff, 0) + 1) & 0xffff
                self.outgoing_reliable_seq[0xff] = n
                self._emit([enet.build_ping_command(0xff, n)], track=False)
                self.last_ping_send = now
        self._check()

    def _handle_datagram(self, data):
        # This verifier must not acknowledge or dispatch corrupted datagrams.
        try:
            packet, crc_ok = enet.EnetPacket.decode(data, self.connect_id)
        except Exception:
            self.receive_error = 'malformed ENet datagram'
            return
        if not crc_ok:
            self.crc_fail_count += 1
            return
        for command in packet.commands:
            try:
                self._dispatch_command(command, packet.sent_time)
            except ProbeTransportError as exc:
                self.receive_error = str(exc)
                return
            except Exception:
                self.receive_error = 'invalid ENet command'
                return
            if self.receive_error or self.state == enet.ST_DISCONNECTED:
                return

    def _dispatch_command(self, command, sent_time):
        if command.cmd_type in (enet.CMD_SEND_FRAGMENT,enet.CMD_SEND_UNRELIABLE_FRAGMENT):
            if len(command.body)<20 or len(command.body)!=20+struct.unpack_from('>H',command.body,2)[0]:
                raise ProbeTransportError('malformed fragment body')
        if command.cmd_type == enet.CMD_DISCONNECT:
            if len(command.body)!=4:raise ProbeTransportError('malformed disconnect command')
            self.remote_disconnect_code=struct.unpack('>I',command.body)[0]
        if command.cmd_type == enet.CMD_ACKNOWLEDGE:
            if len(command.body) != 4:
                raise ProbeTransportError('malformed reliable ACK')
            sequence = struct.unpack_from('>H', command.body)[0]
            # A control-channel ACK must never settle an application packet.
            self.pending_cmds.pop((command.chan, sequence), None)
            return
        if command.cmd_type == enet.CMD_VERIFY_CONNECT:
            connect = enet.parse_connect_body(command.body)
            if connect['connect_id'] != self.connect_id:
                raise ProbeTransportError('VERIFY_CONNECT identity mismatch')
        return super()._dispatch_command(command, sent_time)

    def _handle_fragment(self, channel, frag, reliable_seq):
        # Validate before allocation and before ACK scheduling. Control channel
        # 255 carries ENet control commands, never application fragments.
        total=frag['total_length'];count=frag['fragment_count']
        number=frag['fragment_number'];offset=frag['fragment_offset']
        start=frag['start_seq'];data=bytes(frag['data'])
        if not 0 <= channel < getattr(self,'channel_count',2):
            raise ProbeTransportError('invalid fragment channel')
        if not (1 <= total <= 0x2720 and 1 <= count <= min(128,total)):
            raise ProbeTransportError('fragment bounds exceeded')
        if not (0 <= number < count and 0 <= start <= 0xffff and data and
                frag['data_len']==len(data) and 0 <= offset < total and offset+len(data)<=total):
            raise ProbeTransportError('invalid fragment range')
        slots=self.reassemblers.setdefault(channel,{})
        ra=slots.get(start)
        if ra is None:
            if sum(len(value) for value in self.reassemblers.values()) >= 16:
                raise ProbeTransportError('fragment slot limit reached')
            ra=enet._Reassembler(total,count);slots[start]=ra
        if ra.total_length != total or ra.fragment_count != count:
            raise ProbeTransportError('inconsistent fragment metadata')
        if number in ra.received:
            if ra.offsets[number] != offset or ra.received[number] != data:
                raise ProbeTransportError('conflicting duplicate fragment')
            return
        for index,previous in ra.received.items():
            old=ra.offsets[index]
            if offset < old+len(previous) and old < offset+len(data):
                raise ProbeTransportError('overlapping fragments')
        if not ra.put(number,offset,data):
            raise ProbeTransportError('fragment insertion rejected')
        if ra.complete():
            end=0
            for index in sorted(ra.received,key=lambda index:ra.offsets[index]):
                if ra.offsets[index] != end:
                    raise ProbeTransportError('incomplete fragment coverage')
                end+=len(ra.received[index])
            if end != total:
                raise ProbeTransportError('incomplete fragment coverage')
            full=ra.assemble();slots.pop(start)
            self.server_reliable_recv+=1
            self._deliver_app_data(full,channel,start)

    def _deliver_app_data(self, encrypted, channel, reliable_seq):
        if not self.join_sent:
            self.receive_error = 'application data arrived before JoinGame'
            return
        self.raw_packets += 1
        # 15-second observations exceeded the previous 128-packet capture cap.
        # Default cap remains 512; longer bounded sessions set an explicit cap.
        if self.raw_packets > getattr(self, 'max_application_packets', 512) or len(encrypted) > 0x2720:
            self.receive_error = 'application receive bounds reached'
            return
        plain = self._codec_app_payload(encrypted)
        self.archive(self.raw_packets, encrypted, plain)
        try:
            decoded = decode_server_plaintext(plain, decompress_type2=self.decoder)
        except Exception:
            self.receive_error = 'current server envelope decode failed'
            return
        for message in decoded:
            self.messages.append(message)
            self.downstream_app_count += 1
        if len(self.messages) > 256:
            self.receive_error = 'application message count bound reached'

    def _send_payload(self, payload, *, encrypt):
        self._check()
        if self.state != enet.ST_CONNECTED:
            raise ProbeTransportError('ENet is not connected')
        wire = self._codec_app_payload(payload) if encrypt else bytes(payload)
        start = (self.outgoing_reliable_seq.get(0, 0) + 1) & 0xffff
        if len(wire) <= enet.MTU - 32:
            commands = [enet.build_send_reliable(0, start, wire)]
        else:
            commands = enet.build_send_fragment_commands(0, start, wire)
        self.outgoing_reliable_seq[0] = (start + len(commands) - 1) & 0xffff
        for command in commands:
            self._emit([command])
            while self._is_pending(0, command.reliable_seq):
                self.service()
                time.sleep(0.003)
        self._check()
        return start

    def close(self, send_disconnect=True):
        # Base close sets closed before _emit, which suppresses DISCONNECT.
        # The worker owns cleanup; emit at most once before closing the socket.
        with self.io_lock:
            if self.closed:
                return
            self.disconnect_sent = False
            try:
                if send_disconnect and self.state == enet.ST_CONNECTED:
                    with self.seq_lock:
                        seq = (self.outgoing_reliable_seq.get(0xff, 0) + 1) & 0xffff
                        self.outgoing_reliable_seq[0xff] = seq
                    try:
                        self._emit([enet.build_disconnect_command(0xff, seq)], track=False)
                        self.disconnect_sent = True
                    except Exception:
                        pass
            finally:
                self.closed = True
                try:
                    self.socket.close()
                finally:
                    self.state = enet.ST_DISCONNECTED

    def send_join(self, payload):
        if self.join_sent:
            raise ProbeTransportError('JoinGame may be sent only once')
        self.join_sent = True
        return self._send_payload(payload, encrypt=False)

    def send_registration(self, payload):
        if not self.join_sent:
            raise ProbeTransportError('registration before JoinGame is forbidden')
        result = self._send_payload(payload, encrypt=True)
        self.registration_acked = True
        return result

    def send_scene_confirmation(self, payload):
        if not self.registration_acked:
            raise ProbeTransportError('scene confirmation before registration ACK is forbidden')
        if len(payload) != 15 or payload[4] != 0x0e or payload[6] != 0x11:
            raise ProbeTransportError('invalid scene confirmation frame')
        return self._send_payload(payload, encrypt=True)
