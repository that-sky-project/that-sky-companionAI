"""Build 421589 receive-only codec recovered from ARM64 static instructions.

Input is already RC4-decrypted ENet payload, NOT a client six-byte frame.
No network, device reads or credential logging. Parsed objects contain private
account/session-related data and deliberately suppress these in their repr.
Compression kind 2 needs its real native dictionary; absent an explicit verified
callback, compressed data is rejected instead of guessing gzip/LZ4/Zstd defaults.
Static-derived + synthetic-tested is not a live server/registration validation.
"""
from dataclasses import dataclass, field
import hmac
import struct
import uuid
from typing import Callable

APK_BUILD = 421589
MAX_ENET_PAYLOAD = 0x2720
MAX_DECOMPRESSED = 0x2F20
MAX_BODY = 0x2700
REGISTRATION_CHAIN_VERIFIED = False

class ReceiveError(ValueError):
    pass

class CompressionRequired(ReceiveError):
    pass

@dataclass(frozen=True)
class ServerMessage:
    message_type: int
    body: bytes = field(repr=False)
    compressed: bool = False
    offset: int = 0

@dataclass(frozen=True)
class EnterPlayer:
    local_id: int
    uuid_raw: bytes = field(repr=False)
    spawn_raw16: bytes = field(repr=False)
    level_id: int


def decode_server_plaintext(data: bytes, *,
        decompress_type2: Callable[[bytes, int], bytes] | None = None,
        apk_build: int = APK_BUILD) -> tuple[ServerMessage, ...]:
    """Decode current S->C envelope, after caller's verified packet decryption.

    Each uncompressed record: bool1 + byte alignment + u8 type + u16LE body
    length + body. Compressed record: bool1 + alignment + u16LE compressed
    length + u16LE uncompressed length + block. The block yields exactly one
    (u8 type, u16LE body length, body), with no repeated compression flag.
    High padding bits are ignored just as native raw reads skip them.
    """
    if apk_build != APK_BUILD:
        raise ReceiveError('unsupported APK build')
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ENET_PAYLOAD:
        raise ReceiveError('invalid decrypted packet length/type')
    result = []
    pos = 0
    while pos < len(data):
        if len(result) >= 256:
            raise ReceiveError('too many response records')
        start = pos
        compressed = bool(data[pos] & 1)
        pos += 1  # next raw field always aligns to byte
        if compressed:
            if pos + 4 > len(data):
                raise ReceiveError('truncated compression header')
            clen, ulen = struct.unpack_from('<HH', data, pos)
            pos += 4
            if not 0 < clen <= MAX_ENET_PAYLOAD or not 3 <= ulen <= MAX_DECOMPRESSED:
                raise ReceiveError('invalid compressed/decompressed length')
            if pos + clen > len(data):
                raise ReceiveError('truncated compressed block')
            block = data[pos:pos+clen]
            pos += clen
            if decompress_type2 is None:
                raise CompressionRequired('native type-2 dictionary decoder required')
            try:
                raw = decompress_type2(block, ulen)
            except Exception as exc:
                raise ReceiveError('type-2 decompression failed') from None
            if not isinstance(raw, bytes) or len(raw) != ulen:
                raise ReceiveError('decompressed length/type mismatch')
            msg_type, length = struct.unpack_from('<BH', raw)
            if length > MAX_BODY or len(raw) != 3 + length:
                raise ReceiveError('invalid decompressed message length')
            body = raw[3:]
        else:
            if pos + 3 > len(data):
                raise ReceiveError('truncated message header')
            msg_type, length = struct.unpack_from('<BH', data, pos)
            pos += 3
            if length > MAX_BODY or pos + length > len(data):
                raise ReceiveError('truncated/oversized message body')
            body = data[pos:pos+length]
            pos += length
        result.append(ServerMessage(msg_type, body, compressed, start))
    return tuple(result)


def decode_enter_game(body: bytes, *, apk_build: int = APK_BUILD) -> tuple[EnterPlayer, ...]:
    """Decode msg 0x11 player list (native serializer 0x1f7f3cc).

    Count is 4 bits, bounded inclusively 0..8; next raw read byte-aligns.
    Exactly count * 37 bytes follow: localId u8, UUID raw16, spawn raw16,
    levelId u32LE. The 40-byte in-memory stride is NOT the wire stride.
    """
    if apk_build != APK_BUILD:
        raise ReceiveError('unsupported APK build')
    if not isinstance(body, bytes) or not body:
        raise ReceiveError('missing EnterGame body')
    count = body[0] & 15
    if count > 8:
        raise ReceiveError('EnterGame count outside native range')
    if len(body) != 1 + 37 * count:
        raise ReceiveError('EnterGame exact length mismatch')
    players = []
    for index in range(count):
        pos = 1 + index * 37
        players.append(EnterPlayer(body[pos], body[pos+1:pos+17],
            body[pos+17:pos+33], struct.unpack_from('<I', body, pos+33)[0]))
    return tuple(players)


def validate_pending_enter_game(message: ServerMessage, *,
        account_uuid: str, expected_level_id: int, pending_state: int,
        apk_build: int = APK_BUILD) -> dict:
    """Strict extra checks for this project's own connection, not native claims.

    The caller MUST bind the connection + new ticket to account/scene/resource.
    A syntactically valid packet alone does not prove its origin/authenticity.
    Returned summary contains no UUID, spawn bytes, signature or raw packet.
    """
    if pending_state != 3:
        raise ReceiveError('EnterGame not expected outside pending JoinGame state 3')
    if message.message_type != 0x11:
        raise ReceiveError('message is not EnterGame')
    if type(expected_level_id) is not int or not 0 <= expected_level_id <= 0xFFFFFFFF:
        raise ReceiveError('invalid explicit expected level')
    try:
        uid = uuid.UUID(account_uuid).bytes
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReceiveError('invalid explicit account UUID') from exc
    players = decode_enter_game(message.body, apk_build=apk_build)
    if len({p.local_id for p in players}) != len(players):
        raise ReceiveError('duplicate player local IDs')
    if len({p.uuid_raw for p in players}) != len(players):
        raise ReceiveError('duplicate player UUIDs')
    matched = [p for p in players if hmac.compare_digest(p.uuid_raw, uid)]
    if len(matched) != 1:
        raise ReceiveError('own account missing or repeated in EnterGame')
    if matched[0].level_id != expected_level_id:
        raise ReceiveError('own scene does not match requested scene')
    return {'apk_build': apk_build, 'message_type': 0x11,
        'player_count': len(players), 'own_local_id': matched[0].local_id,
        'own_level_id': matched[0].level_id, 'account_present_once': True,
        'scene_matches': True, 'enter_game_structure_valid': True,
        'registration_confirmed': False,
        'native_next_step': 'promote pending connection; send current 0x04 registration'}
