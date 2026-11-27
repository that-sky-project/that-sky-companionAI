"""T73-R2：主人房“真实入服票”的纯解析与决策（无 I/O、无 socket）。

背景（见 work/round2/REPORT.md）：
- 第一轮把聊天 WS 同图票塞给 handoff0167.prepare_*_join，实测 100% 抛
  begin_failed，票一张都没进 Join；01:46:16 服务器又把小光单独 Move 走。
- 真正能改变服务器登记的是“用一张真票重新走 Join/EnterGame”，并在
  EnterGame 的 PlayerSync 名单里确认主人在同一实例。本模块只负责：
  1) 解析 /account/join_friend_game 返回的 move_to_game；
  2) 判断这张票能不能交给 pump 做 make-before-break；
  3) 给出 others 应该带谁。
真正的连接/回滚在 connection0167 + handoff pump。
"""

STAGE = "stage"                    # 可以交给 pump 试新 peer
SAME_ENDPOINT = "same_endpoint"    # 票指向当前房间：不换，留着当前连接
OTHER_MAP = "other_map"            # 跨图不在本轮范围
STALE = "stale"                    # 票太旧
MALFORMED = "malformed"
NO_OWNER = "no_owner"

MAX_AGE = 20.0


def _as_int(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def parse_friend_ticket(payload):
    """从 join_friend_game 响应中取 move_to_game；失败返回 None。

    只保留公开字段 uri/move_ts/sig/platform_filter/level/players；
    不记录账号、session 或任何凭据。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    move = data.get("move_to_game")
    if not isinstance(move, dict):
        return None
    ip, port = move.get("public_ip"), _as_int(move.get("port"))
    sig, move_ts = move.get("sig"), _as_int(move.get("move_ts"))
    level = _as_int(move.get("level"))
    if not isinstance(ip, str) or not ip or port is None or not 1 <= port <= 65535:
        return None
    if not isinstance(sig, str) or len(sig) != 64:
        return None
    if move_ts is None:
        return None
    players = [p for p in (move.get("players") or []) if isinstance(p, str)][:7]
    # T74-R2：public_ip 是票面 origin；ip/speed_host 才是拨号地址（两者不能混）。
    dial = move.get("ip") or move.get("speed_ip") or move.get("speed_host") or ip
    return {"uri": f"{ip}:{port}", "origin_host": ip, "origin_port": port,
            "ip": dial, "port": port,
            "move_ts": move_ts, "sig": sig,
            "platform_filter": _as_int(move.get("platform_filter")) or 0,
            "level": level, "with_owner": True,
            "other_players": players, "players": players}


def ticket_decision(ticket, *, current_endpoint, current_map, now, current_origin=None):
    """判断一张主人房票能否作为 make-before-break 的真实入服票。

    返回 (action, reason)；只有 action == STAGE 才应该建新 peer。
    current_endpoint 是 (host, port) 或 None。
    """
    if not isinstance(ticket, dict) or ticket.get("error"):
        return MALFORMED, "ticket_error"
    uri, sig, move_ts = ticket.get("uri"), ticket.get("sig"), ticket.get("move_ts")
    if not isinstance(uri, str) or ":" not in uri:
        return MALFORMED, "uri"
    if not isinstance(sig, str) or len(sig) != 64:
        return MALFORMED, "sig"
    move_ts = _as_int(move_ts)
    if move_ts is None:
        return MALFORMED, "move_ts"
    if abs(float(now) - (move_ts / 1000.0 if move_ts > 10**11 else move_ts)) > MAX_AGE:
        return STALE, "ticket_age"
    level = _as_int(ticket.get("level"))
    if level is not None and current_map is not None and level != current_map:
        return OTHER_MAP, "level"
    host, port = uri.rsplit(":", 1)
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        return MALFORMED, "endpoint"
    endpoint = (host, int(port))
    # T74-R2：是否“已经同房”要比**票面 origin**，不是拨号地址。
    same = current_origin if current_origin is not None else current_endpoint
    if same is not None and tuple(same) == endpoint:
        return SAME_ENDPOINT, "already_there"
    if current_endpoint is not None and tuple(current_endpoint) == endpoint:
        return SAME_ENDPOINT, "already_there"
    return STAGE, "fresh_ticket"


def others_for_owner(owner_raw, *, enabled=True):
    """Join 里 others 的取值：16 字节 UUID 列表；关闭时返回空列表。"""
    if not enabled or not isinstance(owner_raw, (bytes, bytearray)) or len(owner_raw) != 16:
        return []
    return [bytes(owner_raw)]


def should_request_after_move(*, owner_in_roster, external, attempts, limit,
                              now, cooldown_until):
    """服务器 Move 提交后是否要立刻要一张主人房票。

    只在“真实 Move（不是我们自己的票）+ 新名单里没有主人 + 还有次数 +
    不在冷却”时触发。主人本来就在名单里时绝不打断当前房间。
    """
    if owner_in_roster is not False:
        return False
    if external:
        return False
    if attempts >= limit:
        return False
    if now < cooldown_until:
        return False
    return True
