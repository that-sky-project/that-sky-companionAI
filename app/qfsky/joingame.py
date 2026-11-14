# -*- coding: utf-8 -*-
"""JoinGame 模板改写 + 首组状态包 burst + 授权续约应答（国服）。

移植 PreviousJoinTemplate.kt（CN 4526B 模板内嵌）/ ReplayBurst.kt / RandomJoinPackets.kt。

关键认知（知识库 42/43 篇 + 08-19 实测定论，勿改）：
- fixNetVersion 必须保持 False：模板内嵌 NetVersion [., 13441, 23472, ., ., 25536] 与包内 blob 自洽；
  覆写成官方 35318/23520 会让 collect 恒空。
- burst 里的 level_id 必须 patch 成目标图 map_id（不是短 level_hash）；与上一条是合取条件。
- JoinGame 明文发出之后才 enableAppRc4。
"""
from __future__ import annotations

import base64
import struct
import zlib
import socket

from . import enet
from .util import write_varint

CN_SIZE = 4526

OFF_SERVER_VERSION = 0x72
OFF_CLIENT_VERSION = 0x74
OFF_BUILD_NUMBER = 0x76
OFF_NETVERSION = 0x7A  # Previous JoinGame layout; requires new-version capture validation
OFF_JOIN_LEVEL_ID = 0x7A
OFF_LEVEL_ID = 0x87
OFF_W17_JOIN_LEVEL_ID = 0xD7


def _others_shift(buf: bytes) -> int:
    """@0x55 u32 LE = m_othersCount；count=N 时其后跟 N×16B 记录，0x59 起所有字段整体 +16N。
    doc42 的固定偏移表只对 count=0 样本成立（09-16 cap4 实测：晨岛 count=1、音乐大厅一次 count=2）。"""
    if len(buf) < 0x59:
        raise ValueError("JoinGame template missing othersCount")
    n, = struct.unpack_from("<I", buf, 0x55)
    if n > 16 or 0x8B + 16 * n > len(buf):
        raise ValueError("JoinGame othersCount or shifted fields exceed supported template bounds")
    return 16 * n


def template_level_hash(tpl: bytes) -> int:
    """样本自带 levelHash（偏移感知；小镇等 count=0 与 0x7A 一致）。"""
    return struct.unpack_from("<H", tpl, 0x7A + _others_shift(tpl))[0]

# levelId 在 JoinGame 里出现两次：@0x87 与包尾最后 4B（官方样本 6/6 两处一致）。
# NetVersion must come from a complete observed tuple, never a version-number guess.

# 历史 4526B 模板，实际内嵌 build=381089；结构不兼容 421629，禁止默认复用。
CN_TEMPLATE_ZLIB_B64 = 'eJzT8VPzZ2agFOj+3Xx3i67S9c+ZcKH/MICudjnjG+GFF1gZ4r42mmyIBokcSGa4165ziYGhwR7TaNLFjggwAA0HijB+7Xl1+GvNLcdBwlD48Z/yoB4FIxa8+f///v76///+/v7+fpQxyhhljDJGNoN/oIvkUTAKhgVwGATMUTAKhj34//f7/fv35UeZNGIObqCgwPD/PxtxahsZOIGkgR4YWhmAhkgAEEO6lw=='

_CACHED_CN: bytes | None = None


def get_cn_template(*, legacy: bool = False) -> bytes:
    if not legacy:
        raise ValueError("内嵌模板实际 build=381089，仅允许显式 legacy=True；不能冒充0.16.8")
    global _CACHED_CN
    if _CACHED_CN is None:
        raw = base64.b64decode(CN_TEMPLATE_ZLIB_B64)
        out = zlib.decompress(raw)
        if len(out) != CN_SIZE:
            raise ValueError("内嵌 JoinGame 模板长度异常: %d != %d" % (len(out), CN_SIZE))
        _CACHED_CN = out
    return _CACHED_CN


def _hex_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s)


def _resolve_ip(host: str) -> bytes:
    return socket.gethostbyname(host).encode("ascii")


def rewrite_payload(
    template: bytes,
    peer_key_u32: int,
    user_uuid: str,
    session_hex: str,
    sig_hex: str,
    server_ip: str,
    server_port: int,
    move_ts: int,
    server_version: int | None = None,
    client_version: int | None = None,
    build_number: int | None = None,
    join_level_id: int | None = None,
    level_id: int | None = None,
    fix_net_version: bool = False,
    anim_state15: bytes | None = None,
    net_version: tuple[int, int, int, int, int, int] | None = None,
    legacy: bool = False,
) -> bytearray:
    if not legacy:
        raise ValueError("rewrite_payload 仅用于历史布局；新版使用 current_join.build_payload")
    if len(template) < 0x90:
        raise ValueError("JoinGame 模板过短: %d" % len(template))
    buf = bytearray(template)
    sh = _others_shift(buf)
    struct.pack_into("<I", buf, 0, peer_key_u32 & 0xFFFFFFFF)
    buf[4] = 3
    buf[5] = 0
    uuid_bin = _hex_to_bytes(user_uuid.replace("-", ""))
    if len(uuid_bin) != 16:
        raise ValueError("user_uuid 须为 16B")
    buf[6:22] = uuid_bin
    sess_bin = _hex_to_bytes(session_hex)
    if len(sess_bin) != 16:
        raise ValueError("session 须为 32 hex")
    buf[22:38] = sess_bin
    sig_bin = _hex_to_bytes(sig_hex)
    if len(sig_bin) != 32:
        raise ValueError("sig 须为 64 hex")
    buf[38:70] = sig_bin
    ip_bytes = bytes(int(x) for x in server_ip.split(".")) if _is_dotted(server_ip) else socket.inet_aton(server_ip)
    if len(ip_bytes) != 4:
        raise ValueError("server_ip 须为 IPv4")
    buf[70:74] = ip_bytes
    struct.pack_into("<H", buf, 74, server_port & 0xFFFF)
    struct.pack_into("<I", buf, 0x4C, move_ts & 0xFFFFFFFF)
    if server_version is not None:
        struct.pack_into("<H", buf, OFF_SERVER_VERSION + sh, server_version & 0xFFFF)
    if client_version is not None:
        struct.pack_into("<H", buf, OFF_CLIENT_VERSION + sh, client_version & 0xFFFF)
    if build_number is not None:
        struct.pack_into("<I", buf, OFF_BUILD_NUMBER + sh, build_number & 0xFFFFFFFF)
    if join_level_id is not None:
        v = join_level_id & 0xFFFF
        struct.pack_into("<H", buf, OFF_JOIN_LEVEL_ID + sh, v)
        struct.pack_into("<I", buf, OFF_W17_JOIN_LEVEL_ID + sh, v)
    if level_id is not None:
        v = level_id & 0xFFFFFFFF
        struct.pack_into("<I", buf, OFF_LEVEL_ID + sh, v)
        struct.pack_into("<I", buf, len(buf) - 4, v)
    if anim_state15 is not None:
        if len(anim_state15) != 15:
            raise ValueError("animState15 须为 15B，实际 %d" % len(anim_state15))
        buf[0x63 + sh:0x63 + sh + 15] = anim_state15
    # NetVersion 放最后：避免被 joinLevelId / build 覆写
    if fix_net_version:
        if net_version is None or len(net_version) != 6 or any(type(v) is not int or not 0 <= v <= 0xFFFF for v in net_version):
            raise ValueError("fix_net_version requires six observed uint16 values; legacy hardcoded values are disabled")
        if join_level_id is not None and net_version[0] != (join_level_id & 0xFFFF):
            raise ValueError("NetVersion level hash does not match join_level_id")
        struct.pack_into("<6H", buf, OFF_NETVERSION + sh, *net_version)
    return buf


def _is_dotted(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ── ReplayBurst（首组状态包）──

CAPTURE_LEVEL_ID = 2358907137
CAPTURE_JOIN_LEVEL_ID = 55942
DEFAULT_PLAYER_ID = 1
DEFAULT_POSITION = (-100.52648162841797, 627.5330200195312, 268.3623352050781)
SNAPSHOT_LEN = 799

SUCCESS_BURST = [
    (True, 0x04, 3, "AYbagTSwWwAAAADAYwABEZqMAACAPwAAAAAAAAAAAAAAAAAAAAAAAIA/AAAAAAAAAAAAAAAAAAAAAAAAgD8AAAAAAAAAAAAAAAAAAAAAAACAPwA="),
    (False, 0x0E, 4, ""),
    (False, 0x0E, 5, "BwEAAA=="),
    (True, 0x0E, 6, "AgEArSuxbpkAAQCtIvsoVAADAAEAASL7KFQAAwACAAEi+yhUAAMABAABK7FumQABAK0="),
    (False, 0x0C, 7, "2gk="),
    (False, 0x0E, 8, "DAEAAAABEZqMAAA="),
    (False, 0x0E, 9, "DgEAAQE="),
]


class _BinaryWriter:
    def __init__(self):
        self.buf = bytearray()
        self.bit_pos = 0

    def _ensure_byte(self):
        if not self.buf or self.bit_pos == 0:
            self.buf.append(0)

    def bits(self, value: int, bit_count: int) -> None:
        remaining = bit_count
        v = value & ((1 << bit_count) - 1) if bit_count else 0
        while remaining > 0:
            self._ensure_byte()
            avail = 8 - self.bit_pos
            take = min(avail, remaining)
            cur = self.buf[-1]
            merged = cur | (((v & ((1 << take) - 1)) << self.bit_pos) & 0xFF)
            self.buf[-1] = merged
            self.bit_pos += take
            v >>= take
            remaining -= take
            if self.bit_pos >= 8:
                self.bit_pos = 0

    def raw(self, data: bytes) -> None:
        if self.bit_pos != 0:
            self.bit_pos = 0
        self.buf.extend(data)

    def to_bytes(self) -> bytes:
        return bytes(self.buf)


def _zero_rle_compress(buf: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] == 0:
            j = i
            while j < n and buf[j] == 0 and j - i < 0xFF:
                j += 1
            out.append(0)
            out.append(j - i)
            i = j
        else:
            out.append(buf[i])
            i += 1
    return bytes(out)


def _checksum_payload(buf: bytes) -> int:
    checksum = 0xC5
    for b in buf:
        checksum = ((checksum ^ b) * 0x93) & 0xFF
    return checksum


def _encode_snapshot_delta(current_seq: int, current_snapshot: bytes) -> bytes:
    packed = _zero_rle_compress(current_snapshot)
    return bytes([current_seq & 0xFF, 0, _checksum_payload(packed)]) + packed


def _build_min_snapshot(level_id: int, position: tuple[float, float, float]) -> bytes:
    w = _BinaryWriter()
    w.bits(1, 3)
    for v in position:
        w.raw(struct.pack("<f", v))
    w.raw(b"\x00\x00")
    w.raw(b"\x00")
    w.raw(b"\x00")
    w.bits(0, 8)
    w.raw(b"\x00")
    w.raw(b"\x00\x00\x00\x00")
    w.bits(0, 2)
    w.bits(0, 1)
    w.bits(0, 1)
    w.raw(bytes(8))
    w.raw(b"\x00")
    w.raw(b"\x00")
    w.raw(b"\x00")
    w.raw(b"\x00")
    w.bits(0, 1)
    w.bits(0, 1)
    w.bits(0, 6)
    w.bits(0, 1)
    w.raw(b"\x00\x00")
    w.bits(0, 2)
    w.raw(b"\x00")
    w.raw(b"\x00\x00")
    w.raw(struct.pack("<I", level_id & 0xFFFFFFFF))
    snapshot = w.to_bytes()
    if len(snapshot) > SNAPSHOT_LEN:
        raise ValueError("snapshot prefix %d exceeds %d" % (len(snapshot), SNAPSHOT_LEN))
    return snapshot + bytes(SNAPSHOT_LEN - len(snapshot))


def build_inner_player_state_keyframe(level_id: int) -> bytes:
    snapshot = _build_min_snapshot(level_id, DEFAULT_POSITION)
    delta = _encode_snapshot_delta(1, snapshot)
    return bytes([0x03, DEFAULT_PLAYER_ID & 0xFF, 0]) + delta


def _replace_bytes(src: bytes, old: bytes, new: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        if i + len(old) <= n and src[i:i + len(old)] == old:
            out.extend(new)
            i += len(old)
        else:
            out.append(src[i])
            i += 1
    return bytes(out)


def patch_payload_level_fields(
    payload: bytes,
    msg_type: int,
    level_id: int,
    join_level_id: int | None,
    nv_bytes: bytes | None = None,
) -> bytes:
    p = payload
    # 0x04 的 nv[1..5]（@3..13，5×u16 LE）必须与同会话 JoinGame 的 @0x7C..0x86 自洽
    # （43 篇 §四：JoinGame 与 burst 在 NetVersion 上不自洽会被 0x09 踢）。
    if nv_bytes is not None and msg_type == 0x04 and len(p) >= 13 and len(nv_bytes) == 10:
        p = p[:3] + nv_bytes + p[13:]
    if level_id != CAPTURE_LEVEL_ID:
        p = _replace_bytes(p, struct.pack("<I", CAPTURE_LEVEL_ID), struct.pack("<I", level_id & 0xFFFFFFFF))
    if msg_type == 0x04 and join_level_id is not None and len(p) >= 3:
        old = struct.pack("<H", CAPTURE_JOIN_LEVEL_ID)
        if p[1:3] == old:
            p = p[0:1] + struct.pack("<H", join_level_id & 0xFFFF) + p[3:]
    return p


# 真机注册包 0x04 载荷骨架（83B，09-08 手机小镇入场后首条 app 消息去掉 6B 上行头）。
# 结构：[0]=01 [1:3]=levelHash(LE) [3:13]=nv10(LE) [13]=00 [14:18]=levelId(LE)
#       [18:]=float 1.0 与 0 交替。per-map 模板路径据此构造图内注册包。
REG04_SKELETON = bytes.fromhex(
    "016ece21f7e05b00000000c06300977dfc590000803f0000000000000000"
    "00000000000000000000803f000000000000000000000000000000000000"
    "803f000000000000000000000000000000000000803f00"
)


def build_template_registration(level_id: int, template: bytes) -> bytes:
    """用 per-map 模板构造图内注册包 0x04（83B）。

    静态 burst 0x04 的 levelHash/nv 来自旧抓包（如小镇 55846/13441），服务端已更新场景
    （当前 52846/63265），原样发出即被 0x09 踢；真机注册包以模板当前值构造后被接受。
    """
    p = bytearray(REG04_SKELETON)
    sh = _others_shift(template)
    struct.pack_into("<H", p, 1, struct.unpack_from("<H", template, 0x7A + sh)[0])
    p[3:13] = template[0x7C + sh:0x86 + sh]
    struct.pack_into("<I", p, 14, level_id & 0xFFFFFFFF)
    return bytes(p)


def send_template_registration(
    cli: enet.EnetClient,
    level_id: int,
    peer_key_u32: int,
    template: bytes,
) -> int:
    """per-map 模板路径的图内注册（JoinGame 后首条 app 消息，参照真机 seq=1）。"""
    payload = build_template_registration(level_id, template)
    cli.send_app_reliable(0, 0x04, 1, payload)
    return 1


def send_success_burst(
    client: enet.EnetClient,
    level_id: int,
    join_level_id: int | None,
    nv_bytes: bytes | None = None,
) -> int:
    """JoinGame 后首组状态包；inner=0x03 用当前图 keyframe。返回发送条数。"""
    sent = 0
    base_cap_seq = SUCCESS_BURST[0][2]
    for reliable, msg_type, cap_seq, b64 in SUCCESS_BURST:
        if client.state == enet.ST_DISCONNECTED:
            break
        app_seq = 3 + (cap_seq - base_cap_seq)
        if msg_type == 0x0E and cap_seq == 4:
            payload = build_inner_player_state_keyframe(level_id)
        else:
            payload = patch_payload_level_fields(base64.b64decode(b64), msg_type, level_id, join_level_id, nv_bytes)
        if reliable:
            client.send_app_reliable(0, msg_type, app_seq, payload)
        else:
            client.send_app_unreliable(0, msg_type, app_seq, payload)
        sent += 1
        if sent == 1:
            end = time.monotonic() + 0.08
            while time.monotonic() < end:
                try:
                    client.service()
                except Exception:
                    pass
                time.sleep(0.01)
    return sent


import time  # noqa: E402  (send_success_burst 需要)


def auth_renew_reply(level_id: int, echo: bytes) -> bytes:
    """授权续约应答（p0=0x11/0x09 下行触发）。"""
    e = (echo + b"\x00\x00")[:2]
    return bytes([0x11, 0x01, 0x00]) + struct.pack("<I", level_id & 0xFFFFFFFF) + e


def build_current_payload(prepared, *, uid, session_hex, ticket, peer_key):
    """Current-build entry, requiring account/build/revision/scene-bound private capture."""
    from .current_join import build_payload
    return build_payload(prepared, uid=uid, session_hex=session_hex, ticket=ticket, peer_key=peer_key)
