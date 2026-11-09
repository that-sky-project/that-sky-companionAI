# -*- coding: utf-8 -*-
"""ENet 传输层（Sky 私有协议）Python 移植，逐字节对齐 :core EnetClient/EnetPacket。

- 包头：peerIdWord(BE u16: flags 高4bit | peerId 低12bit) [+ sentTime u16 BE] + crc32(BE) + commands
- 命令：cmdByte(type|flags) + chan + reliableSeq(BE u16) + body
- 国服：JoinGame 明文发出后才 enable_app_rc4(sig)；其后 app 层 NetMsg 整体 RC4(sig 32B)
"""
from __future__ import annotations

import random
import socket
import struct
import threading
import time
import zlib

from . import netcfg

from .util import rc4

# ── 常量（EnetConstants.kt）──
MTU = 1024
RECV_BUF = 65536
PING_INTERVAL_SEC = 0.75
CONNECT_TIMEOUT_SEC = 18.0
SOCKET_RECV_TIMEOUT_MS = 2
RETRANSMIT_RTO_SEC = 0.42
RETRANSMIT_MAX = 14

FLAG_SENT_TIME = 0x8000
FLAG_COMPRESSED = 0x4000
FLAG_SESSION = 0x3000
MASK_PEER_ID = 0x0FFF
NEW_CONNECTION = 0x0FFF

CMD_ACKNOWLEDGE = 1
CMD_CONNECT = 2
CMD_VERIFY_CONNECT = 3
CMD_DISCONNECT = 4
CMD_PING = 5
CMD_SEND_RELIABLE = 6
CMD_SEND_UNRELIABLE = 7
CMD_SEND_FRAGMENT = 8
CMD_SEND_UNSEQUENCED = 9
CMD_BANDWIDTH_LIMIT = 10
CMD_THROTTLE_CONFIGURE = 11
CMD_SEND_UNRELIABLE_FRAGMENT = 12

FLAG_CMD_ACK_REQ = 0x80
MULBERRY_STEP = 0x6D2B79F5

ST_INIT = 0
ST_CONNECT_SENT = 1
ST_CONNECTED = 2
ST_DISCONNECTED = 3

MAX_FRAGMENT_SLOTS = 16
MAX_TOTAL_LENGTH = 1 << 20
MAX_FRAGMENTS = 1024


def rotate32_left(x: int, bits: int) -> int:
    b = bits % 32
    x &= 0xFFFFFFFF
    return ((x << b) | (x >> (32 - b))) & 0xFFFFFFFF if b else x


def mulberry32_next(state: int) -> tuple[int, int]:
    s = (state + MULBERRY_STEP) & 0xFFFFFFFF
    n = s
    n = ((n ^ (n >> 15)) * (n | 1)) & 0xFFFFFFFF
    inner = ((n ^ (n >> 7)) * (n | 61)) & 0xFFFFFFFF
    n = (n ^ ((n + inner) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return ((n ^ (n >> 14)) & 0xFFFFFFFF), s


def generate_connect_id() -> int:
    host = random.randint(-(2 ** 31), 2 ** 31 - 1) & 0xFFFFFFFF
    ts = int(time.time()) & 0xFFFFFFFF
    seed = rotate32_left((host + ts) & 0xFFFFFFFF, 16)
    return mulberry32_next(seed)[0]


def compute_crc32(header: bytes, seed_u32: int, commands: bytes) -> int:
    seed_bytes = struct.pack("<I", seed_u32 & 0xFFFFFFFF)
    return zlib.crc32(header + seed_bytes + commands) & 0xFFFFFFFF


class EnetCommand:
    __slots__ = ("cmd_type", "chan", "reliable_seq", "cmd_flags", "body")

    def __init__(self, cmd_type: int, chan: int, reliable_seq: int, cmd_flags: int = 0, body: bytes = b""):
        self.cmd_type = cmd_type
        self.chan = chan
        self.reliable_seq = reliable_seq & 0xFFFF
        self.cmd_flags = cmd_flags
        self.body = body

    @property
    def cmd_byte(self) -> int:
        return (self.cmd_type & 0x0F) | (self.cmd_flags & 0xF0)

    def encode(self) -> bytes:
        return struct.pack(">BBH", self.cmd_byte, self.chan & 0xFF, self.reliable_seq) + self.body


# ── 命令构造（EnetPacket.kt builders）──

def build_connect_command(outgoing_peer_id, in_session_id, out_session_id, mtu, window_size, channel_count,
                          incoming_bw, outgoing_bw, throttle_interval, throttle_accel, throttle_decel,
                          connect_id_u32, data_u32, reliable_seq=1) -> EnetCommand:
    body = struct.pack(
        ">HBBIIIIIIII",
        outgoing_peer_id & 0xFFFF,
        in_session_id & 0xFF,
        out_session_id & 0xFF,
        mtu & 0xFFFFFFFF,
        window_size & 0xFFFFFFFF,
        channel_count & 0xFFFFFFFF,
        incoming_bw & 0xFFFFFFFF,
        outgoing_bw & 0xFFFFFFFF,
        throttle_interval & 0xFFFFFFFF,
        throttle_accel & 0xFFFFFFFF,
        throttle_decel & 0xFFFFFFFF,
    )
    return EnetCommand(CMD_CONNECT, 0xFF, reliable_seq, FLAG_CMD_ACK_REQ,
                       body + struct.pack("<I", connect_id_u32 & 0xFFFFFFFF)
                       + struct.pack(">I", data_u32 & 0xFFFFFFFF))


def parse_connect_body(body: bytes) -> dict:
    if len(body) not in (40, 44):
        raise ValueError("connect body %d" % len(body))
    (outgoing_peer_id, in_sid, out_sid, mtu, window_size, channel_count, incoming_bw, outgoing_bw,
     throttle_interval, throttle_accel, throttle_decel) = struct.unpack_from(">HBBIIIIIIII", body, 0)
    connect_id = struct.unpack_from("<I", body, 36)[0]
    data = struct.unpack_from(">I", body, 40)[0] if len(body) == 44 else None
    return {"outgoing_peer_id": outgoing_peer_id, "in_session_id": in_sid, "out_session_id": out_sid,
            "mtu": mtu, "window_size": window_size, "channel_count": channel_count,
            "incoming_bw": incoming_bw, "outgoing_bw": outgoing_bw,
            "throttle_interval": throttle_interval, "throttle_accel": throttle_accel,
            "throttle_decel": throttle_decel, "connect_id": connect_id, "data": data}


def build_ack_command(acked_seq: int, acked_sent_time: int, chan: int = 0xFF) -> EnetCommand:
    return EnetCommand(CMD_ACKNOWLEDGE, chan, 0, 0, struct.pack(">HH", acked_seq & 0xFFFF, acked_sent_time & 0xFFFF))


def build_ping_command(chan: int = 0xFF, reliable_seq: int = 1) -> EnetCommand:
    return EnetCommand(CMD_PING, chan, reliable_seq, FLAG_CMD_ACK_REQ)


def build_disconnect_command(chan: int = 0xFF, reliable_seq: int = 0, data_u32: int = 0) -> EnetCommand:
    return EnetCommand(CMD_DISCONNECT, chan, reliable_seq, FLAG_CMD_ACK_REQ, struct.pack(">I", data_u32 & 0xFFFFFFFF))


def build_send_reliable(chan: int, reliable_seq: int, app_payload: bytes) -> EnetCommand:
    return EnetCommand(CMD_SEND_RELIABLE, chan, reliable_seq, FLAG_CMD_ACK_REQ,
                       struct.pack(">H", len(app_payload) & 0xFFFF) + app_payload)


def build_send_unreliable(chan: int, reliable_seq: int, unreliable_seq: int, app_payload: bytes) -> EnetCommand:
    return EnetCommand(CMD_SEND_UNRELIABLE, chan, reliable_seq, 0,
                       struct.pack(">HH", unreliable_seq & 0xFFFF, len(app_payload) & 0xFFFF) + app_payload)


def build_send_fragment_commands(chan: int, start_reliable_seq: int, app_payload: bytes,
                                 mtu: int = MTU, overhead_per_pkt: int = 32) -> list[EnetCommand]:
    max_data = mtu - overhead_per_pkt
    total_len = len(app_payload)
    frag_cnt = (total_len + max_data - 1) // max_data
    cmds = []
    for i in range(frag_cnt):
        off = i * max_data
        data = app_payload[off:off + max_data]
        body = struct.pack(">HHIIII",
                           start_reliable_seq & 0xFFFF,
                           len(data) & 0xFFFF,
                           frag_cnt & 0xFFFFFFFF,
                           i & 0xFFFFFFFF,
                           total_len & 0xFFFFFFFF,
                           off & 0xFFFFFFFF) + data
        cmds.append(EnetCommand(CMD_SEND_FRAGMENT, chan, (start_reliable_seq + i) & 0xFFFF, FLAG_CMD_ACK_REQ, body))
    return cmds


def parse_send_fragment_body(body: bytes) -> dict:
    if len(body) < 20:
        raise ValueError("fragment body %d" % len(body))
    start_seq, data_len, frag_cnt, frag_num, total_len, frag_off = struct.unpack_from(">HHIIII", body, 0)
    return {"start_seq": start_seq, "data_len": data_len, "fragment_count": frag_cnt,
            "fragment_number": frag_num, "total_length": total_len, "fragment_offset": frag_off,
            "data": body[20:20 + data_len]}


def parse_send_reliable_body(body: bytes) -> tuple[int, bytes]:
    if len(body) < 2:
        raise ValueError("reliable body %d" % len(body))
    dl = struct.unpack_from(">H", body, 0)[0]
    return dl, body[2:2 + dl]


_FIXED_BODY = {
    CMD_ACKNOWLEDGE: 4,
    CMD_CONNECT: 44,
    CMD_VERIFY_CONNECT: 40,
    CMD_DISCONNECT: 4,
    CMD_PING: 0,
    CMD_BANDWIDTH_LIMIT: 8,
    CMD_THROTTLE_CONFIGURE: 12,
}


def decode_command_body(cmd_type: int, rest: bytes) -> tuple[int, bytes]:
    n = _FIXED_BODY.get(cmd_type)
    if n is not None:
        return n, rest[:min(n, len(rest))]
    if len(rest) < 2:
        return 0, b""
    if cmd_type == CMD_SEND_RELIABLE:
        dl = struct.unpack_from(">H", rest, 0)[0]
        return 2 + dl, rest[:2 + dl]
    if cmd_type == CMD_SEND_UNRELIABLE:
        dl = struct.unpack_from(">H", rest, 2)[0]
        return 4 + dl, rest[:4 + dl]
    if cmd_type == CMD_SEND_UNSEQUENCED:
        dl = struct.unpack_from(">H", rest, 2)[0]
        return 4 + dl, rest[:4 + dl]
    if cmd_type in (CMD_SEND_FRAGMENT, CMD_SEND_UNRELIABLE_FRAGMENT):
        dl = struct.unpack_from(">H", rest, 2)[0]
        return 20 + dl, rest[:20 + dl]
    return 0, b""


def decode_commands(buf: bytes) -> list[EnetCommand]:
    out = []
    off = 0
    while off + 4 <= len(buf):
        cmd_byte = buf[off]
        t = cmd_byte & 0x0F
        f = cmd_byte & 0xF0
        chan = buf[off + 1]
        relseq = struct.unpack_from(">H", buf, off + 2)[0]
        off += 4
        body_len, body = decode_command_body(t, buf[off:])
        out.append(EnetCommand(t, chan, relseq, f, body))
        off += body_len
        if t == 0:
            break
    return out


class EnetPacket:
    __slots__ = ("peer_id", "flags", "sent_time", "connect_id_seed", "commands")

    def __init__(self, peer_id, flags=FLAG_SENT_TIME, sent_time=0, connect_id_seed=0, commands=None):
        self.peer_id = peer_id
        self.flags = flags
        self.sent_time = sent_time
        self.connect_id_seed = connect_id_seed
        self.commands = commands or []

    def encode(self) -> bytes:
        header_size = 4 if (self.flags & FLAG_SENT_TIME) else 2
        header = struct.pack(">H", ((self.flags & 0xF000) | (self.peer_id & 0x0FFF)) & 0xFFFF)
        if self.flags & FLAG_SENT_TIME:
            header += struct.pack(">H", self.sent_time & 0xFFFF)
        cmd_bytes = b"".join(c.encode() for c in self.commands)
        crc = compute_crc32(header, self.connect_id_seed, cmd_bytes)
        return header + struct.pack(">I", crc) + cmd_bytes

    @staticmethod
    def decode(raw: bytes, expected_connect_id: int | None = None) -> tuple["EnetPacket", bool]:
        if len(raw) < 6:
            raise ValueError("too short")
        off = 0
        pid_word = struct.unpack_from(">H", raw, off)[0]
        flags = pid_word & 0xF000
        peer_id = pid_word & 0x0FFF
        off += 2
        sent_time = 0
        if flags & FLAG_SENT_TIME:
            sent_time = struct.unpack_from(">H", raw, off)[0]
            off += 2
        if off + 4 > len(raw):
            raise ValueError("truncated crc")
        wire_crc = struct.unpack_from(">I", raw, off)[0]
        off += 4
        header_bytes = raw[:off - 4]
        cmd_bytes = raw[off:]
        if expected_connect_id is not None:
            seed = expected_connect_id if peer_id < NEW_CONNECTION else 0
        elif peer_id == NEW_CONNECTION:
            seed = 0
        else:
            seed = None
        crc_ok = seed is not None and compute_crc32(header_bytes, seed, cmd_bytes) == wire_crc
        return EnetPacket(peer_id, flags, sent_time, seed or 0, decode_commands(cmd_bytes)), crc_ok


class _Reassembler:
    def __init__(self, total_length: int, fragment_count: int):
        self.total_length = total_length
        self.fragment_count = fragment_count
        self.received: dict[int, bytes] = {}
        self.offsets: dict[int, int] = {}
        self.valid = 1 <= total_length <= MAX_TOTAL_LENGTH and 1 <= fragment_count <= MAX_FRAGMENTS

    def put(self, frag_num: int, frag_off: int, data: bytes) -> bool:
        if not self.valid:
            return False
        if frag_num < 0 or frag_num >= self.fragment_count:
            return False
        if frag_off < 0 or frag_off + len(data) > self.total_length:
            return False
        if frag_num in self.received:
            return False
        self.received[frag_num] = data
        self.offsets[frag_num] = frag_off
        return True

    def complete(self) -> bool:
        return self.valid and len(self.received) == self.fragment_count

    def assemble(self) -> bytes:
        buf = bytearray(self.total_length)
        for fn, data in self.received.items():
            fo = self.offsets.get(fn, 0)
            if fo < 0 or fo + len(data) > len(buf):
                continue
            buf[fo:fo + len(data)] = data
        return bytes(buf)


AppMessageCallback = "callable(msg_type, size, payload, chan, reliable_seq)"


class EnetClient:
    def __init__(self, remote_ip: str, remote_port: int, peer_key_u32: int,
                 connect_id: int | None = None, on_app_message=None, egress_key: str = ""):
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.peer_key = peer_key_u32 & 0xFFFFFFFF
        self.state = ST_INIT
        self.closed = False
        self.crc_fail_count = 0
        self.ack_fallback3_hits = 0
        self.transport_failed_reason: str | None = None
        self.last_disconnect_source: str | None = None
        self.pkt_sent = 0
        self.pkt_recv = 0
        self.downstream_app_count = 0
        self.server_reliable_recv = 0
        self.first_down_msg_type: int | None = None
        # 下行口径计数：国服 4B `[0x00][outer][size LE2]` vs 国际服 3B `[type][size LE2]`
        self.down_cn_hits = 0
        self.down_intl_hits = 0

        self.remote = (remote_ip, remote_port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(SOCKET_RECV_TIMEOUT_MS / 1000.0)
        # 出口绑定：游戏内 enet 中继是自建 UDP，HTTP 代理管不到，
        # 只有绑到指定网卡/源 IP 才能让「进图的流量」也走该账号的出口。
        # 未配置 uplinks 时 = 空操作（与改造前完全一致）。
        self.egress_key = str(egress_key or "")
        self.egress = netcfg.bind_udp(self.socket, self.egress_key) if self.egress_key else {}
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
        except OSError:
            pass

        self.local_peer_id = 0
        self.remote_peer_id = 0
        self.in_session_id = 0xFF
        self.out_session_id = 0xFF
        self.header_session_flag = 0
        self.connect_id = (connect_id if connect_id is not None else generate_connect_id()) & 0xFFFFFFFF

        self.app_rc4_key: bytes | None = None
        self.outgoing_reliable_seq = {0: 0, 0xFF: 0}
        self.unreliable_seq = {0: 0, 0xFF: 0}
        self.seq_lock = threading.RLock()
        self.io_lock = threading.RLock()
        self.reassemblers: dict[int, dict[int, _Reassembler]] = {0: {}, 0xFF: {}}
        self.pending_cmds: dict[tuple[int, int], tuple[EnetCommand, float, int]] = {}
        self.last_ping_send = 0.0
        self.pending_acks: list[tuple[int, int, int]] = []
        self.t0 = time.monotonic()
        self.on_app_message = on_app_message

    # ── app 层 RC4 ──
    def enable_app_rc4(self, sig_hex: str) -> None:
        key = bytes.fromhex(sig_hex)
        if len(key) != 32:
            raise ValueError("move ticket sig 须解出 32B，实得 %d" % len(key))
        self.app_rc4_key = key

    def _codec_app_payload(self, app_payload: bytes) -> bytes:
        if self.app_rc4_key is None:
            return app_payload
        return rc4(self.app_rc4_key, app_payload)

    # ── 发送 ──
    def _sent_time_now(self) -> int:
        return int((time.monotonic() - self.t0) * 1000) & 0xFFFF

    def _emit(self, commands: list[EnetCommand], is_new_conn: bool = False, track: bool = True) -> None:
        if not commands:
            return
        with self.io_lock:
            if self.closed:
                return
            peer_id = NEW_CONNECTION if is_new_conn else self.remote_peer_id
            flags = FLAG_SENT_TIME | (0 if is_new_conn else self.header_session_flag)
            sent_time = self._sent_time_now()
            seed = 0 if is_new_conn else self.connect_id
            pkt = EnetPacket(peer_id, flags, sent_time, seed, commands)
            data = pkt.encode()
            try:
                self.socket.sendto(data, self.remote)
            except OSError:
                return
            self.pkt_sent += 1
            if track and not is_new_conn:
                now = time.monotonic()
                for c in commands:
                    if (c.cmd_flags & FLAG_CMD_ACK_REQ) and c.cmd_type in (CMD_SEND_RELIABLE, CMD_SEND_FRAGMENT):
                        self.pending_cmds[(c.chan, c.reliable_seq)] = (c, now, 1)

    def connect(self, channel_count: int = 2, window_size: int = 65536, mtu: int = MTU) -> None:
        if self.state != ST_INIT:
            raise RuntimeError("invalid state for connect(): %s" % self.state)
        self.outgoing_reliable_seq[0xFF] = 1
        con_cmd = build_connect_command(
            outgoing_peer_id=self.local_peer_id,
            in_session_id=self.in_session_id,
            out_session_id=self.out_session_id,
            mtu=mtu, window_size=window_size, channel_count=channel_count,
            incoming_bw=0, outgoing_bw=0,
            throttle_interval=5000, throttle_accel=2, throttle_decel=2,
            connect_id_u32=self.connect_id, data_u32=self.peer_key, reliable_seq=1,
        )
        self._emit([con_cmd], is_new_conn=True)
        self.state = ST_CONNECT_SENT

    def send_reliable(self, chan: int, app_payload: bytes) -> int:
        if self.state != ST_CONNECTED:
            return -1
        app_payload = self._codec_app_payload(app_payload)
        max_data = MTU - 32
        with self.seq_lock:
            n = (self.outgoing_reliable_seq.get(chan, 0) + 1) & 0xFFFF
            self.outgoing_reliable_seq[chan] = n
        start_seq = n
        if len(app_payload) <= max_data:
            self._emit([build_send_reliable(chan, start_seq, app_payload)])
            self._wait_fragment_ack(chan, start_seq, timeout_ms=3000)
            return start_seq
        cmds = build_send_fragment_commands(chan, start_seq, app_payload)
        with self.seq_lock:
            self.outgoing_reliable_seq[chan] = (start_seq + len(cmds) - 1) & 0xFFFF
        for c in cmds:
            self._emit([c])
            self._wait_fragment_ack(chan, c.reliable_seq, timeout_ms=3500)
            if self.state == ST_DISCONNECTED:
                break
            time.sleep(0.02)
        return start_seq

    def send_unreliable(self, chan: int, app_payload: bytes) -> None:
        if self.state != ST_CONNECTED:
            return
        app_payload = self._codec_app_payload(app_payload)
        with self.seq_lock:
            seq = (self.unreliable_seq.get(chan, 0) + 1) & 0xFFFF
            self.unreliable_seq[chan] = seq
            rseq = self.outgoing_reliable_seq.get(chan, 0)
        self._emit([build_send_unreliable(chan, rseq, seq, app_payload)], track=False)

    def _encode_upstream_app_hdr(self, msg_type: int, seq: int) -> bytes:
        return struct.pack("<IBB", self.peer_key & 0xFFFFFFFF, msg_type & 0xFF, seq & 0xFF)

    def send_app_reliable(self, chan: int, msg_type: int, seq: int, payload: bytes) -> int:
        return self.send_reliable(chan, self._encode_upstream_app_hdr(msg_type, seq) + payload)

    def send_app_unreliable(self, chan: int, msg_type: int, seq: int, payload: bytes) -> None:
        self.send_unreliable(chan, self._encode_upstream_app_hdr(msg_type, seq) + payload)

    def burst_recv_after_join(self, rounds: int = 22, max_drain: int = 96) -> None:
        for _ in range(max(1, rounds)):
            try:
                self.service(max_drain)
            except Exception:
                pass

    def _is_pending(self, chan: int, seq: int) -> bool:
        return (chan, seq) in self.pending_cmds

    def _wait_fragment_ack(self, chan: int, seq: int, timeout_ms: float) -> None:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self.service(128)
            if self.state == ST_DISCONNECTED:
                return
            if not self._is_pending(chan, seq):
                return
            time.sleep(0.008)

    def service(self, max_drain: int = 96) -> None:
        with self.io_lock:
            if self.closed:
                return
            for _ in range(max_drain):
                try:
                    data, _addr = self.socket.recvfrom(RECV_BUF)
                except (socket.timeout, OSError):
                    break
                except InterruptedError:
                    break
                self.pkt_recv += 1
                try:
                    self._handle_datagram(data)
                except Exception:
                    pass
            if self.pending_acks:
                ack_cmds = [build_ack_command(seq, ts, chan) for (seq, ts, chan) in self.pending_acks]
                self.pending_acks.clear()
                for i in range(0, len(ack_cmds), 8):
                    self._emit(ack_cmds[i:i + 8], track=False)
            self._retransmit_overdue()
            now = time.monotonic()
            if self.state == ST_CONNECTED and now - self.last_ping_send >= PING_INTERVAL_SEC:
                with self.seq_lock:
                    n = (self.outgoing_reliable_seq.get(0xFF, 0) + 1) & 0xFFFF
                    self.outgoing_reliable_seq[0xFF] = n
                self._emit([build_ping_command(0xFF, n)], track=False)
                self.last_ping_send = now

    def pending_reliable_count(self, chan: int) -> int:
        return sum(1 for (c, _seq) in self.pending_cmds if c == chan)

    def wait_until_join_settled(self, chan: int = 0, burst_rounds: int = 22, grace_after_ack_ms: float = 0,
                                timeout_sec: float = 8.0) -> dict:
        for _ in range(burst_rounds):
            try:
                self.service(128)
            except Exception:
                pass
            if self.state == ST_DISCONNECTED:
                return self._settle(chan, settled=False, disconnected=True)
        deadline = time.monotonic() + timeout_sec
        pending_zero_since: float | None = None
        while time.monotonic() < deadline:
            self.service(128)
            if self.state == ST_DISCONNECTED:
                return self._settle(chan, settled=False, disconnected=True)
            pending = self.pending_reliable_count(chan)
            if pending == 0:
                if grace_after_ack_ms <= 0:
                    return self._settle(chan, settled=True, disconnected=False)
                if pending_zero_since is None:
                    pending_zero_since = time.monotonic()
                if (time.monotonic() - pending_zero_since) * 1000 >= grace_after_ack_ms:
                    return self._settle(chan, settled=True, disconnected=False)
            else:
                pending_zero_since = None
            time.sleep(0.005)
        if self.pending_reliable_count(chan) == 0 and self.state != ST_DISCONNECTED:
            return self._settle(chan, settled=True, disconnected=False)
        return self._settle(chan, settled=False, disconnected=self.state == ST_DISCONNECTED)

    def _settle(self, chan: int, settled: bool, disconnected: bool) -> dict:
        return {
            "settled": settled,
            "pending_reliable": self.pending_reliable_count(chan),
            "downstream_count": self.downstream_app_count,
            "server_reliable_recv": self.server_reliable_recv,
            "first_down_msg_type": self.first_down_msg_type,
            "down_cn_hits": self.down_cn_hits,
            "down_intl_hits": self.down_intl_hits,
            "disconnected": disconnected,
            "last_disconnect_source": self.last_disconnect_source,
            "pkt_sent": self.pkt_sent,
            "pkt_recv": self.pkt_recv,
        }

    def wait_until_connected(self, timeout_sec: float = CONNECT_TIMEOUT_SEC) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            self.service(128)
            if self.state == ST_CONNECTED:
                return True
            if self.state == ST_DISCONNECTED:
                return False
            time.sleep(0.008)
        return False

    def close(self, send_disconnect: bool = True) -> None:
        with self.io_lock:
            if self.closed:
                return
            self.closed = True
            try:
                if send_disconnect and self.state == ST_CONNECTED:
                    try:
                        with self.seq_lock:
                            n = (self.outgoing_reliable_seq.get(0xFF, 0) + 1) & 0xFFFF
                            self.outgoing_reliable_seq[0xFF] = n
                        self._emit([build_disconnect_command(0xFF, n)], track=False)
                        time.sleep(0.05)
                    except Exception:
                        pass
            finally:
                try:
                    self.socket.close()
                except OSError:
                    pass
                self.state = ST_DISCONNECTED

    # ── 接收 ──
    def _retransmit_overdue(self) -> None:
        now = time.monotonic()
        to_drop: list[tuple[int, int]] = []
        for key, (cmd, last_send, count) in list(self.pending_cmds.items()):
            if now - last_send < RETRANSMIT_RTO_SEC:
                continue
            if count >= RETRANSMIT_MAX:
                to_drop.append(key)
                continue
            try:
                self._emit([cmd], track=False)
                self.pending_cmds[key] = (cmd, now, count + 1)
            except Exception:
                pass
        if to_drop:
            for k in to_drop:
                self.pending_cmds.pop(k, None)
            first = to_drop[0]
            self.transport_failed_reason = (
                "重传 %d 次仍未 ACK · 丢失 %d 条 reliable (chan=%d, seq=%d)"
                % (RETRANSMIT_MAX, len(to_drop), first[0], first[1])
            )
            self.last_disconnect_source = "重传超时断开"
            self.state = ST_DISCONNECTED

    def _handle_datagram(self, data: bytes) -> None:
        try:
            pkt, crc_ok = EnetPacket.decode(data, self.connect_id)
        except Exception:
            return
        if not crc_ok:
            self.crc_fail_count += 1
        for c in pkt.commands:
            try:
                self._dispatch_command(c, pkt.sent_time)
            except Exception:
                pass

    def _dispatch_command(self, c: EnetCommand, sent_time: int) -> None:
        needs_ack = (c.cmd_flags & FLAG_CMD_ACK_REQ) != 0
        if c.cmd_type == CMD_VERIFY_CONNECT:
            d = parse_connect_body(c.body)
            self.remote_peer_id = d["outgoing_peer_id"]
            self.in_session_id = d["in_session_id"]
            self.out_session_id = d["out_session_id"]
            self.header_session_flag = (self.out_session_id & 0x03) << 12
            self.state = ST_CONNECTED
        elif c.cmd_type == CMD_ACKNOWLEDGE:
            if len(c.body) < 2:
                return
            acked_seq = struct.unpack_from(">H", c.body, 0)[0]
            ack_chan = c.chan
            for pred in (
                lambda k: k[1] == acked_seq and k[0] == ack_chan,
                lambda k: k[1] == acked_seq and k[0] == 0,
            ):
                for k in self.pending_cmds:
                    if pred(k):
                        del self.pending_cmds[k]
                        break
                else:
                    continue
                break
            else:
                fallback = next((k for k in self.pending_cmds if k[1] == acked_seq), None)
                if fallback is not None:
                    self.ack_fallback3_hits += 1
                    del self.pending_cmds[fallback]
        elif c.cmd_type == CMD_DISCONNECT:
            self.state = ST_DISCONNECTED
            self.last_disconnect_source = "服务端主动 DISCONNECT"
        elif c.cmd_type == CMD_SEND_RELIABLE:
            self.server_reliable_recv += 1
            _dl, appdata = parse_send_reliable_body(c.body)
            self._deliver_app_data(appdata, c.chan, c.reliable_seq)
        elif c.cmd_type in (CMD_SEND_FRAGMENT, CMD_SEND_UNRELIABLE_FRAGMENT):
            frag = parse_send_fragment_body(c.body)
            self._handle_fragment(c.chan, frag, c.reliable_seq)
        elif c.cmd_type == CMD_SEND_UNRELIABLE:
            if len(c.body) > 4:
                self._deliver_app_data(c.body[4:], c.chan, c.reliable_seq)
        if needs_ack:
            self.pending_acks.append((c.reliable_seq, sent_time, c.chan))

    def _handle_fragment(self, chan: int, frag: dict, reliable_seq: int) -> None:
        start_seq = frag["start_seq"]
        slots = self.reassemblers.setdefault(chan, {})
        if len(slots) > MAX_FRAGMENT_SLOTS:
            for k in sorted(slots)[: len(slots) - MAX_FRAGMENT_SLOTS]:
                slots.pop(k, None)
        ra = slots.get(start_seq)
        if ra is None:
            ra = _Reassembler(frag["total_length"], frag["fragment_count"])
            if not ra.valid:
                return
            slots[start_seq] = ra
        ra.put(frag["fragment_number"], frag["fragment_offset"], frag["data"])
        if ra.complete():
            self.server_reliable_recv += 1
            full = ra.assemble()
            slots.pop(start_seq, None)
            self._deliver_app_data(full, chan, start_seq)

    def _parse_down_app(self, appdata: bytes) -> tuple[str, int, int, bytes]:
        """下行 app 层头部分流（RC4 解密之后调用）→ (mode, msg_type, size, payload)。

        国服 4B：`[0x00][outer_msg_type 1B][size u16 LE]` + payload —— 由服务端实测钉死：
          208 帧经 RC4 解出后全部满足 `appdata[0]==0 and size == len(appdata) - 4`，
          outer ∈ {0x0d, 0x0e}（net time / 权威续约 0x11）。
          知识库 06 篇旧写「5B `[0x00][outer][pad][size LE2]`」是把 size 低字节当成了 pad：
          按 4B 读时 `1c 00 == 28 == len-4`（"Wrong Client NetVersion" 样本）自洽。
        国际服 3B：`[msg_type][size u16 LE]` + payload（旧口径，长度不自洽时回退）。
        """
        if len(appdata) < 3:
            return "short", -1, 0, b""
        if len(appdata) >= 4 and appdata[0] == 0x00:
            size = appdata[2] | (appdata[3] << 8)
            if size == len(appdata) - 4:
                return "cn", appdata[1], size, appdata[4:]
        size = struct.unpack_from("<H", appdata, 1)[0]
        return "intl", appdata[0], size, appdata[3:min(3 + size, len(appdata))]

    def _deliver_app_data(self, appdata_raw: bytes, chan: int, reliable_seq: int) -> None:
        appdata = self._codec_app_payload(appdata_raw)
        if len(appdata) < 3:
            return
        mode, msg_type, size, payload = self._parse_down_app(appdata)
        if mode == "cn":
            self.down_cn_hits += 1
        else:
            self.down_intl_hits += 1
        self.downstream_app_count += 1
        if self.first_down_msg_type is None:
            self.first_down_msg_type = msg_type
        if self.on_app_message:
            try:
                self.on_app_message(msg_type, size, payload, chan, reliable_seq)
            except Exception:
                pass
