"""Offline native incoming Game dispatch guard, before any subtype parser.

Native1ef26d4 saves its actual callback peer inx21. At1ef28a4..28b0,
Gameheader byte1 must equal that peer's byte0; otherwise it returns before
1ef1294. This is separate from the outer application sequence and the own
PlayerState level-change counter. No sockets, game writes or account IDs.
"""


def filter_current_peer_game(game_payload, *, peer_epoch):
    """Return a bound Game payload, or None for the native stale-epoch drop.

    Caller must first authenticate/decrypt the exact incoming peer's packet.
    peer_epoch comes from THAT callback/transport peer's current byte0, not a
    player row, packet sequence, or an unrelated current/pending connection.
    Apply this once before ALL Game subtype handlers, including Nominee,
    PlayerState, LevelData, DeltaAck and RPC. The server sender ID in byte2
    may be nonzero; this guard does not invent a roster/sender restriction.
    Bytes stay in caller-owned RAM; the function never logs or persists them.
    """
    if type(peer_epoch) is not int or not 0 <= peer_epoch <= 255:
        raise ValueError('invalid_game_peer_epoch')
    if type(game_payload) is not bytes or len(game_payload) < 3:
        raise ValueError('invalid_game_header')
    return game_payload if game_payload[1] == peer_epoch else None
