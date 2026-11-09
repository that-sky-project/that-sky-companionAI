"""接受双人互动邀请（牵手、背背、拥抱…）：上行 SocialAcceptOfferRpc。

格式（2026-10-31 两部真机互做牵手/拥抱/背背/熊抱，小光在旁边抓下行 gamemsg-1031-0345 确认）：
  GameMsg 头 [2][epoch][0][自己 slot] + 条目 [key 0x603e65df][hdr 0][size u16] + 正文：
  [邀请方 slot][自己 slot][u32 len][动画名 1][u32 len][动画名 2][0][0][0]
  牵手：两个空串；拥抱：HugTakeF / HugGiveF；熊抱：BearHugGive / BearHugTake；背背：空串 / JumpOnShoulders_1。
  （10-30 那次 MuMu 上行把背背末尾解成 0xff、只看到一个串，是密钥流弱字节解错了。）
其余支持类型使用 social_catalog 明确配置的完整动画/标志三元组，不按名字猜角色或等级。
"""
import struct
import social_catalog

from qfsky.long0167.engine import Frame
from qfsky.map_long0167 import map_long_engine

SOCIAL_ACCEPT_KEY = 0x603E65DF          # FNV-1a32("SocialAcceptOfferRpc")
AVATAR_TELEPORT_KEY = 0xF75B35F2        # AvatarTeleportRpc
def _default_pair(offer_type):
    entry = social_catalog.resolve_action(offer_type)
    if not entry.get("supported") or entry.get("gift_or_unlock") or not entry.get("accept_pairs"):
        raise ValueError("unsupported social acceptance")
    pair = entry["accept_pairs"][0]
    a, b, tail = pair["animation_a"], pair["animation_b"], pair["tail"]
    if (not isinstance(a, str) or not isinstance(b, str) or len(tail) != 3
            or tail[0] not in (0,1) or tail[1] not in (0,1) or tail[2] not in (0,1,2,4,8)):
        raise ValueError("invalid catalog accept pair")
    return a, b, bytes(tail)


def anims_for(offer_type):
    return _default_pair(offer_type)[:2]


def _tail(offer_type):
    return _default_pair(offer_type)[2]


def _lp(text):
    raw = text.encode()
    return struct.pack("<I", len(raw)) + raw


def accept_body(offerer_slot, own_slot, offer_type):
    if (type(offerer_slot) is not int or type(own_slot) is not int
            or not 1 <= offerer_slot <= 255 or not 1 <= own_slot <= 255 or offerer_slot == own_slot):
        raise ValueError("invalid social slots")
    a, b, tail = _default_pair(offer_type)
    return bytes((offerer_slot, own_slot)) + _lp(a) + _lp(b) + tail


def _game(epoch, own_slot, key, body):
    return bytes((2, epoch, 0, own_slot)) + struct.pack("<IBH", key, 0, len(body)) + body


def accept_game(epoch, own_slot, offerer_slot, offer_type):
    return _game(epoch, own_slot, SOCIAL_ACCEPT_KEY, accept_body(offerer_slot, own_slot, offer_type))


def teleport_game(epoch, own_slot, xyz, forward):
    """AvatarTeleportRpc：[自己 slot][坐标 3f][朝向 3f]（背背跳下来时真机发了一次）。"""
    body = bytes((own_slot,)) + struct.pack("<3f", *xyz) + struct.pack("<3f", *forward)
    return _game(epoch, own_slot, AVATAR_TELEPORT_KEY, body)


def _send(game):
    """放进引擎发件箱（与 offer_accept 发 LevelUpFx 相同），下一帧随 Actor 一起发出。"""
    map_long_engine.OUTBOX.append(Frame(14, game, True))
    return game


def send_accept(epoch, own_slot, offerer_slot, offer_type):
    return _send(accept_game(epoch, own_slot, offerer_slot, offer_type))


def send_teleport(epoch, own_slot, xyz, forward):
    return _send(teleport_game(epoch, own_slot, xyz, forward))
