"""Build 421589 JoinGame serializer recovered from ARM64 static instructions.

Pure local codec: no sockets, no default session, no map/hash fallback. A successful
round trip proves codec consistency, NOT server acceptance. Native packet/runtime
comparison must be recorded separately. Default CLI output excludes identifiers,
session bytes, signature, endpoint and opaque level-state bytes.
"""
import argparse
import hashlib
import json
import os
import struct
from pathlib import Path

BUILD = 421589
MAX_OTHERS = 7
MAX_BLOB = 0x2000


def uint(value, bits, name):
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError(f'{name} must be uint{bits}')
    return value


def octets(value, size, name):
    if not isinstance(value, (bytes, bytearray)) or len(value) != size:
        raise ValueError(f'{name} must contain exactly {size} bytes')
    return bytes(value)


class Reader:
    def __init__(self, data):
        self.data = bytes(data)
        self.pos = 0
        self.bit = 0
        self.padding = []

    def align(self):
        if self.bit:
            # Native readers ignore padding. Record it rather than reject captures.
            self.padding.append((self.pos, self.bit, self.data[self.pos] >> self.bit))
            self.pos += 1
            self.bit = 0

    def raw(self, size):
        self.align()
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError(f'truncated JoinGame at offset {self.pos}, need {size}')
        out = self.data[self.pos:self.pos + size]
        self.pos += size
        return out

    def number(self, fmt):
        return struct.unpack('<' + fmt, self.raw(struct.calcsize('<' + fmt)))[0]

    def bits(self, count):
        out = 0
        for shift in range(count):
            if self.pos >= len(self.data):
                raise ValueError('truncated bit field')
            out |= ((self.data[self.pos] >> self.bit) & 1) << shift
            self.bit += 1
            if self.bit == 8:
                self.bit = 0
                self.pos += 1
        return out


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.bit = 0

    def align(self):
        self.bit = 0

    def raw(self, value):
        self.align()
        self.data.extend(value)

    def number(self, value, fmt, name):
        sizes = {'B': 8, 'H': 16, 'I': 32, 'Q': 64}
        self.raw(struct.pack('<' + fmt, uint(value, sizes[fmt], name)))

    def bits(self, value, count, name):
        uint(value, count, name)
        for shift in range(count):
            if self.bit == 0:
                self.data.append(0)
            self.data[-1] |= ((value >> shift) & 1) << self.bit
            self.bit = (self.bit + 1) % 8


def decode(data, *, frame=False, require_build=BUILD):
    r = Reader(data)
    result = {}
    offsets = {}
    if frame:
        result['peer_key'] = r.number('I')
        result['msg_type'] = r.number('B')
        result['counter'] = r.number('B')
        if result['msg_type'] != 3:
            raise ValueError('app frame msg_type must be 3 (JoinGame)')
    offsets['body'] = r.pos
    result['uid'] = r.raw(16)
    result['session'] = r.raw(16)
    result['signature'] = r.raw(32)
    result['ticket_ipv4'] = r.raw(4)
    result['ticket_port'] = r.number('H')
    offsets['move_ts'] = r.pos
    result['move_ts'] = r.number('Q')
    result['ticket_flag'] = r.number('B')
    offsets['others_count'] = r.pos
    count = r.number('I')
    if count > MAX_OTHERS:
        raise ValueError('othersCount exceeds native maximum 7')
    result['others'] = [r.raw(16) for _ in range(count)]
    result['ticket_extra_u32_a'] = r.number('I')
    result['ticket_extra_u8'] = r.number('B')
    result['ticket_extra_u32_b'] = r.number('I')
    offsets['spawn'] = r.pos
    result['spawn'] = r.raw(16)
    offsets['server_version'] = r.pos
    result['server_version'] = r.number('H')
    result['client_version'] = r.number('H')
    result['build'] = r.number('I')
    if require_build is not None and result['build'] != require_build:
        raise ValueError(f"unexpected build {result['build']}, expected {require_build}")
    offsets['netversion'] = r.pos
    result['netversion'] = [r.number('H') for _ in range(6)]
    result['level_flag'] = r.number('B')
    result['level_id'] = r.number('I')
    result['matrix'] = r.raw(64)
    result['state_extra_u32_a'] = r.number('I')
    result['state_extra_u32_b'] = r.number('I')
    offsets['blob_length'] = r.pos
    size = r.number('I')
    if not 4 <= size <= MAX_BLOB:
        raise ValueError('level blob length outside 4..8192')
    offsets['blob'] = r.pos
    result['blob'] = r.raw(size)
    if struct.unpack_from('<I', result['blob'])[0] != result['netversion'][0]:
        raise ValueError('blob u32 levelHash does not match NetVersion[0]')
    offsets['tail'] = r.pos
    result['tail_bool_a'] = r.bits(1)
    result['tail_bool_b'] = r.bits(1)
    result['tail_u8'] = r.bits(8)
    result['previous_address_tag'] = r.bits(2)
    naddr = {0: 0, 1: 4, 2: 16, 3: 0}[result['previous_address_tag']]
    result['previous_address'] = r.raw(naddr) if naddr else b''
    has_port = r.bits(1)
    result['previous_port'] = r.number('H') if has_port else 0
    result['previous_level_id'] = r.number('I')
    r.align()
    if r.pos != len(data):
        raise ValueError(f'extra bytes after native JoinGame tail: {len(data) - r.pos}')
    result['_offsets'] = offsets
    result['_padding'] = r.padding
    result['_frame'] = frame
    return result


def encode(value, *, frame=False):
    w = Writer()
    if value['build'] != BUILD:
        raise ValueError('codec is specific to build 421589')
    nv = value['netversion']
    if not isinstance(nv, (list, tuple)) or len(nv) != 6:
        raise ValueError('explicit six-element NetVersion is required')
    for i, v in enumerate(nv):
        uint(v, 16, f'netversion[{i}]')
    blob = bytes(value['blob'])
    if not 4 <= len(blob) <= MAX_BLOB:
        raise ValueError('level blob length outside 4..8192')
    if struct.unpack_from('<I', blob)[0] != nv[0]:
        raise ValueError('blob u32 levelHash does not match NetVersion[0]')
    others = value['others']
    if len(others) > MAX_OTHERS:
        raise ValueError('othersCount exceeds native maximum 7')
    if frame:
        w.number(value['peer_key'], 'I', 'peer_key')
        w.number(3, 'B', 'msg_type')
        w.number(value['counter'], 'B', 'counter')
    for key, size in [('uid',16), ('session',16), ('signature',32), ('ticket_ipv4',4)]:
        w.raw(octets(value[key], size, key))
    for key, fmt in [('ticket_port','H'), ('move_ts','Q'), ('ticket_flag','B')]:
        w.number(value[key], fmt, key)
    w.number(len(others), 'I', 'others_count')
    for other in others:
        w.raw(octets(other, 16, 'other uid'))
    for key, fmt in [('ticket_extra_u32_a','I'), ('ticket_extra_u8','B'), ('ticket_extra_u32_b','I')]:
        w.number(value[key], fmt, key)
    w.raw(octets(value['spawn'], 16, 'spawn'))
    for key, fmt in [('server_version','H'), ('client_version','H'), ('build','I')]:
        w.number(value[key], fmt, key)
    for i, number in enumerate(nv):
        w.number(number, 'H', f'netversion[{i}]')
    w.number(value['level_flag'], 'B', 'level_flag')
    w.number(value['level_id'], 'I', 'level_id')
    w.raw(octets(value['matrix'], 64, 'matrix'))
    for key in ['state_extra_u32_a', 'state_extra_u32_b']:
        w.number(value[key], 'I', key)
    w.number(len(blob), 'I', 'blob_length')
    w.raw(blob)
    w.bits(value['tail_bool_a'], 1, 'tail_bool_a')
    w.bits(value['tail_bool_b'], 1, 'tail_bool_b')
    w.bits(value['tail_u8'], 8, 'tail_u8')
    tag = uint(value['previous_address_tag'], 2, 'previous_address_tag')
    w.bits(tag, 2, 'previous_address_tag')
    size = {0: 0, 1: 4, 2: 16, 3: 0}[tag]
    address = octets(value['previous_address'], size, 'previous_address')
    if size:
        w.raw(address)
    port = uint(value['previous_port'], 16, 'previous_port')
    w.bits(int(port != 0), 1, 'has_previous_port')
    if port:
        w.number(port, 'H', 'previous_port')
    w.number(value['previous_level_id'], 'I', 'previous_level_id')
    return bytes(w.data)


def summary(value):
    return {'schema': 1, 'evidence': 'static-derived-codec',
            'runtime_packet_comparison': False, 'custom_service_join_verified': False,
            'build': value['build'], 'server_version': value['server_version'],
            'client_version': value['client_version'], 'netversion_u16': value['netversion'],
            'level_id': value['level_id'], 'others_count': len(value['others']),
            'blob_length': len(value['blob']),
            'blob_level_hash_u32': struct.unpack_from('<I', value['blob'])[0],
            'blob_sha256': hashlib.sha256(value['blob']).hexdigest(),
            'previous_address_tag': value['previous_address_tag'],
            'previous_port_present': value['previous_port'] != 0,
            'offsets': value['_offsets'], 'nonzero_padding': any(x[2] for x in value['_padding']),
            'redacted_fields': ['uid', 'session', 'signature', 'endpoint', 'others', 'spawn', 'matrix', 'blob']}


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def private_json(value):
    return {k: (v.hex() if isinstance(v, bytes) else [x.hex() for x in v] if k == 'others' else v)
            for k, v in value.items() if not k.startswith('_')}


def from_private_json(value):
    for key in ['uid', 'session', 'signature', 'ticket_ipv4', 'spawn', 'matrix', 'blob', 'previous_address']:
        value[key] = bytes.fromhex(value[key])
    value['others'] = [bytes.fromhex(x) for x in value['others']]
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    d = sub.add_parser('decode')
    d.add_argument('--input', type=Path, required=True)
    d.add_argument('--summary-out', type=Path, required=True)
    d.add_argument('--private-json-out', type=Path, help='Explicit opt-in; includes sensitive session/template data; keep local')
    d.add_argument('--frame', action='store_true', help='Input starts with 6-byte app frame, not ENet headers')
    e = sub.add_parser('encode')
    e.add_argument('--private-json-input', type=Path, required=True)
    e.add_argument('--out', type=Path, required=True)
    e.add_argument('--frame', action='store_true')
    a = p.parse_args()
    if a.action == 'decode':
        v = decode(a.input.read_bytes(), frame=a.frame)
        atomic_bytes(a.summary_out, (json.dumps(summary(v), ensure_ascii=False, indent=2) + '\n').encode())
        if a.private_json_out:
            atomic_bytes(a.private_json_out, (json.dumps(private_json(v), ensure_ascii=False, indent=2) + '\n').encode())
        print('Decoded locally; redacted structural summary saved; server acceptance unverified')
    else:
        v = from_private_json(json.loads(a.private_json_input.read_text(encoding='utf-8-sig')))
        packet = encode(v, frame=a.frame)
        decode(packet, frame=a.frame)
        atomic_bytes(a.out, packet)
        print(f'Built {len(packet)} local bytes; nothing sent to network')


if __name__ == '__main__':
    main()
