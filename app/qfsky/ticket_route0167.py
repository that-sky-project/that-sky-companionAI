"""T74-R2：票面 origin 与拨号地址分离、原生迁移 Join 形状、拨号阶梯。

纯本地模块：无 socket、无文件写入（日志文件需显式开启）、不联网。

背景（第二轮 door.pcap，见 work/round2/REPORT.md）：
- 原生每一条游戏连接实际拨号的是 Pharos/SDK 选出的 ``speed_host``，
  而 JoinGame 里的 ``ticket_ipv4``/``previous_address`` 写的是票面 ``origin``。
  两者**不是同一个地址**（12697: origin 42.186.163.111，实际拨 115.227.13.24）。
- 机器人的 R1/R2 ticket join 把 ``ticket['uri']`` 同时当拨号地址与
  ``ticket_ipv4``，并且把 ``previous_address`` 留成 fresh 形状（tag=0/空/port=0）。
  原生四条 Join 的 previous_address 全是 tag=1 + **上一条连接的票面 origin**。
- 原生在新 peer 失败后会回 ``0x0a MoveReply(result=4)`` 告知旧连接“这次迁移失败”。

本模块只做三件事：
1) ``ticket_origin`` / ``dial_candidates``：把票面地址与拨号地址分开；
2) ``merge_others`` / ``migration_tail_fields``：原生 Join 的 others/tail 形状；
3) ``RouteBook``：origin→dial 的学习/配置/已知边缘 IP 阶梯。
"""
from __future__ import annotations

import ipaddress
import json
import os
import struct

MAX_OTHERS = 7
_CENTRAL_ALTAR = 2394719185
_RELAY_TABLE = os.path.join(os.path.dirname(__file__), 'ticket_relay_routes0167.json')

# 官方客户端响应里可能出现的拨号地址字段（按可信度排序）。
DIAL_FIELDS = ("speed_host", "speed_ip", "connect_addr", "dial_host", "pharos_ip", "ip")
# 票面 origin 字段。
ORIGIN_FIELDS = ("origin_host", "public_ip")

_DEFAULT_TRIES = 3
_DEFAULT_CONNECT_MS = 900
_DEFAULT_ENTER_MS = 3000
_DEFAULT_GONE_SEC = 2.5
# T74-R3：配对票（服务器“两人一起搬”）专用预算。
# 原生 Join 后等 EnterGame 的窗口实测正好 6.0s；但 VERIFY 只要几十毫秒，
# 所以拨号阶段必须短。机器人的 origin 直连已在 in-band Move 路径上反复成功，
# 因此配对票优先拨 origin（不先猜 known_edge）。
_DEFAULT_PAIRING_CONNECT_MS = 1500  # 11-21 实测 origin 建连约 0.6-0.7s，450 会错过
_DEFAULT_PAIRING_ENTER_MS = 4000
_DEFAULT_PAIRING_TRIES = 4


def _env_int(name, default, lower, upper):
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, value))


def connect_timeout_sec():
    return _env_int("SKY_T74_R2_CONNECT_MS", _DEFAULT_CONNECT_MS, 300, 8000) / 1000.0


def enter_timeout_sec():
    return _env_int("SKY_T74_R2_ENTER_MS", _DEFAULT_ENTER_MS, 800, 12000) / 1000.0


def dial_tries():
    return _env_int("SKY_T74_R2_DIAL_TRIES", _DEFAULT_TRIES, 1, 8)


def owner_gone_sec():
    return _env_int("SKY_T74_R2_OWNER_GONE_SEC", int(_DEFAULT_GONE_SEC * 1000), 500, 30000) / 1000.0


def pairing_connect_timeout_sec():
    """配对票每个拨号候选的等待时间（默认 450ms）。"""
    return _env_int("SKY_T74_R3_PAIRING_CONNECT_MS",
                    _DEFAULT_PAIRING_CONNECT_MS, 150, 3000) / 1000.0


def pairing_enter_timeout_sec():
    """配对票 Join 后等 EnterGame 的上限（默认 4000ms；原生窗口 6.0s）。"""
    return _env_int("SKY_T74_R3_PAIRING_ENTER_MS",
                    _DEFAULT_PAIRING_ENTER_MS, 800, 7000) / 1000.0


def pairing_dial_tries():
    """配对票最多试几个拨号候选（默认 4）。"""
    return _env_int("SKY_T74_R3_PAIRING_TRIES", _DEFAULT_PAIRING_TRIES, 1, 8)


def pairing_sig_field():
    """WS 票同时带 sig/signature；默认沿用旧行为 sig，可切 signature。"""
    value = (os.environ.get("SKY_T74_R3_PAIRING_SIG") or "sig").strip().lower()
    return "signature" if value == "signature" else "sig"


def alt_sig_enabled():
    """配对票被服务器拒/等不到 EnterGame 时，换另一个签名字段再试一次。"""
    value = (os.environ.get("SKY_T74_R3_ALT_SIG") or "1").strip().lower()
    return value not in ("0", "off", "false", "never")


def pair_fail_reply_enabled():
    """配对票失败时在旧连接回 0x0a(result=4)（原生每轮都这样回）。"""
    value = (os.environ.get("SKY_T74_R3_PAIR_FAIL_REPLY") or "1").strip().lower()
    return value not in ("0", "off", "false", "never")


def fail_reply_mode():
    """0=从不；move=只在服务器对同一张票发过 in-band Move 时回；always=总是回。"""
    value = (os.environ.get("SKY_T74_R2_FAIL_REPLY") or "move").strip().lower()
    if value in ("0", "off", "never", "false"):
        return "off"
    if value in ("always", "1", "on", "true"):
        return "always"
    return "move"


def parse_ipv4(value):
    """只接受字面 IPv4（与原生 wire 一致，不做 DNS）。"""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        return None


def parse_port(value):
    if type(value) is bool:
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 1 <= value <= 65535:
        return None
    return value


def split_uri(uri):
    if not isinstance(uri, str) or uri.count(":") != 1:
        return None
    host, port = uri.rsplit(":", 1)
    host, port = parse_ipv4(host), parse_port(port)
    if host is None or port is None:
        return None
    return host, port


def endpoint_key(endpoint):
    if not isinstance(endpoint, (tuple, list)) or len(endpoint) != 2:
        return ""
    host, port = parse_ipv4(str(endpoint[0])), parse_port(endpoint[1])
    if host is None or port is None:
        return ""
    return f"{host}:{port}"


def ticket_origin(ticket):
    """票面 origin（写入 Join 的 ticket_ipv4 / previous_address）。

    优先级：origin_host(+origin_port) > public_ip(+port) > uri。
    与原生一致：origin 是票面地址，不是实际拨号地址。
    """
    if not isinstance(ticket, dict):
        return None
    port = parse_port(ticket.get("origin_port")) or parse_port(ticket.get("port"))
    for key in ORIGIN_FIELDS:
        host = parse_ipv4(ticket.get(key))
        if host is not None and port is not None:
            return host, port
    uri = split_uri(ticket.get("uri"))
    if uri is not None:
        return uri
    for key in ORIGIN_FIELDS:
        host = parse_ipv4(ticket.get(key))
        if host is not None and port is not None:
            return host, port
    return None


def ticket_origin_uri(ticket):
    """把 origin 还原成 ``ip:port``，供 ``initial_ticket_fields`` 校验。"""
    origin = ticket_origin(ticket)
    if origin is None:
        return None
    return f"{origin[0]}:{origin[1]}"


def _field_endpoint(ticket, key, default_port):
    raw = ticket.get(key)
    if isinstance(raw, str) and ":" in raw:
        parsed = split_uri(raw)
        if parsed is not None:
            return parsed
        return None
    host = parse_ipv4(raw)
    port = parse_port(ticket.get(key + "_port")) or default_port
    if host is not None and port is not None:
        return host, port
    return None


def explicit_dials(ticket):
    """票里显式给出的拨号地址（speed/pharos/ip…），按可信度排序。"""
    if not isinstance(ticket, dict):
        return []
    origin = ticket_origin(ticket)
    default_port = origin[1] if origin else None
    out = []
    for key in DIAL_FIELDS:
        ep = _field_endpoint(ticket, key, default_port)
        if ep is not None:
            out.append((ep[0], ep[1], "ticket:" + key))
    return out


def load_route_mapping(value):
    """接受三种写法：
    - {"42.186.163.111:12697": "115.227.13.24:12697"}
    - {"routes": [{"origin": "...", "dial": "..."}, ...]}
    - [["origin", "dial"], ...]
    只保留字面 IPv4。失败返回空 dict。
    """
    out = {}
    try:
        if isinstance(value, dict):
            items = value.get("routes")
            if isinstance(items, list):
                for row in items:
                    if not isinstance(row, dict):
                        continue
                    origin = endpoint_key(split_uri(row.get("origin")) or ())
                    dial = endpoint_key(split_uri(row.get("dial")) or ())
                    if origin and dial:
                        out[origin] = dial
            else:
                for origin, dial in value.items():
                    o = endpoint_key(split_uri(origin) or ())
                    d = endpoint_key(split_uri(dial) or ())
                    if o and d:
                        out[o] = d
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, (list, tuple)) and len(row) == 2:
                    o = endpoint_key(split_uri(row[0]) or ())
                    d = endpoint_key(split_uri(row[1]) or ())
                    if o and d:
                        out[o] = d
    except (TypeError, ValueError):
        return {}
    return out


def load_routes(path):
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return load_route_mapping(json.load(handle))
    except (OSError, ValueError, TypeError):
        return {}


def dial_candidates(ticket, *, book=None, max_candidates=None):
    """便捷入口：按票面 origin 给出有序拨号候选。

    ``book`` 为空时用一次性 RouteBook（没有学习/已知边缘），主要给调用方/
    测试做纯函数式使用；线上请传进程级 RouteBook（见 ``RouteBook.from_env``）。
    """
    return (book if book is not None else RouteBook()).candidates(
        ticket, max_candidates=max_candidates)


def merge_others(*groups, self_uuid=None, max_others=MAX_OTHERS):
    """把多组 uuid（带连字符字符串或 16B）合并成原生 ``others`` 列表。

    去重、去自己、保序、上限 7；非法项直接跳过（不猜）。
    """
    out, seen = [], set()
    if isinstance(self_uuid, (bytes, bytearray)) and len(self_uuid) == 16:
        seen.add(bytes(self_uuid))
    elif isinstance(self_uuid, str):
        raw = uuid_raw(self_uuid)
        if raw is not None:
            seen.add(raw)
    for group in groups:
        if group is None:
            continue
        if isinstance(group, (bytes, bytearray)):
            group = [group]
        for item in group:
            raw = None
            if isinstance(item, (bytes, bytearray)) and len(item) == 16:
                raw = bytes(item)
            elif isinstance(item, str):
                raw = uuid_raw(item)
            if raw is None or raw in seen:
                continue
            seen.add(raw)
            out.append(raw)
            if len(out) >= max_others:
                return tuple(out)
    return tuple(out)


def uuid_raw(value):
    """uuid 字符串/16B → 16B；与 join0167_profile.uuid_raw 的宽松度一致。"""
    if isinstance(value, (bytes, bytearray)) and len(value) == 16:
        return bytes(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("-", "")
    if len(text) != 32:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def migration_tail_fields(previous_endpoint, current_level_id, flag_b=0):
    """原生迁移 Join 的 tail（对照 door.pcap 四条 Join，逐字段一致）。

    native: tail_bool_a=1 previous_address_tag=1
            previous_address=上一条连接**票面 origin** previous_port=同端口
            previous_level_id=当前 level（原生把“已安装 level”同时写进两处）
    ``flag_b`` 是原生 tail 的第二个 bit：door.pcap 里 12616（从 Prairie 回遇境、
    move_type=2 的“返回/重连”）为 1，12697/10736/12259（向前迁移）为 0。
    本补丁的票迁移默认 0。
    previous_endpoint 非法时退化为 tag=0（fresh 形状），调用方应记录告警。
    """
    if type(current_level_id) is not int or not 0 <= current_level_id <= 0xFFFFFFFF:
        raise ValueError("current level id required")
    if type(flag_b) is not int or flag_b not in (0, 1):
        raise ValueError("tail flag b must be a bit")
    endpoint = None
    if isinstance(previous_endpoint, (tuple, list)) and len(previous_endpoint) == 2:
        host, port = parse_ipv4(str(previous_endpoint[0])), parse_port(previous_endpoint[1])
        if host is not None and port is not None:
            endpoint = (host, port)
    if endpoint is None:
        return {"tail_bool_a": 1, "tail_bool_b": flag_b, "previous_address_tag": 0,
                "previous_address": b"", "previous_port": 0,
                "previous_level_id": current_level_id}
    return {"tail_bool_a": 1, "tail_bool_b": flag_b, "previous_address_tag": 1,
            "previous_address": ipaddress.IPv4Address(endpoint[0]).packed,
            "previous_port": endpoint[1], "previous_level_id": current_level_id}


def fail_reply_plaintext(peer_key, previous_counter):
    """旧连接上的 MoveReply(result=4)：原生 02:25:24 / 02:25:38 实测。"""
    if type(peer_key) is not int or not 0 <= peer_key <= 0xFFFFFFFF:
        raise ValueError("peer key width")
    if type(previous_counter) is not int or not 0 <= previous_counter <= 0xFF:
        raise ValueError("counter width")
    counter = previous_counter + 1 if previous_counter < 255 else 1
    return struct.pack("<IBBB", peer_key, 10, counter, 4)


def _relay_tables(document):
    """读取精确表和已验证主机表；不从单独的端点清单自行生成主机规则。"""
    exact = load_route_mapping(document)
    declared = document.get('relay_hosts') if isinstance(document, dict) else None
    if not isinstance(declared, dict):
        return exact, {}
    # 只允许确实有同端口正例、且未与精确表中任何同主机记录冲突的声明。
    observed = {}
    for src, dst in exact.items():
        a, b = split_uri(src), split_uri(dst)
        observed.setdefault(a[0], set()).add(b[0] if a[1] == b[1] else None)
    hosts = {h: d for h, d in declared.items()
             if parse_ipv4(h) and parse_ipv4(d) and h != d and observed.get(h) == {d}}
    return exact, hosts


def infer_relay(origin, relay_hosts):
    """仅替换已见且一致的主机，保留本次票面端口；未知主机不按网段推算。"""
    host, port = origin
    dest = relay_hosts.get(host)
    return (dest, port) if dest else None


class RouteBook:
    """origin→dial 的配置 + 学习 + 已知边缘 IP 阶梯（纯内存，可离线测试）。

    - ``routes``：手工配置（``SKY_T74_R2_ROUTES`` 指向的 JSON）。
    - ``learned``：本进程内每条成功连接的 (origin, 实际拨号) 对。
    - ``known``：本进程连接成功过的边缘 IP（最近优先，带端口），用于
      “已知边缘 IP × 目标端口”的有限候选。
    """

    def __init__(self, routes=None, known_limit=6, *, relay_routes=None):
        self.routes = dict(routes or {})
        # 抓包路由和原全局 routes 隔离；仅在目标地图的换房票上首次读取。
        self._relay_routes = None if relay_routes is None else _relay_tables(relay_routes)
        self.learned = {}
        self.known = []
        self.known_limit = max(1, int(known_limit))
        self.last_source = ""

    @classmethod
    def from_env(cls):
        book = cls(load_routes(os.environ.get("SKY_T74_R2_ROUTES")))
        extra = os.environ.get("SKY_T74_R2_KNOWN_EDGES", "")
        for item in extra.split(","):
            item = item.strip()
            if ":" in item:
                ep = split_uri(item)
                if ep is not None:
                    book._remember_edge(ep[0])
        return book

    @property
    def enabled(self):
        return True

    def _remember_edge(self, host):
        try:
            self.known.remove(host)
        except ValueError:
            pass
        self.known.insert(0, host)
        del self.known[self.known_limit:]

    def learn(self, origin, dial):
        """记录一次成功连接：origin 是票面地址，dial 是实际 remote。"""
        origin_key = endpoint_key(origin)
        if isinstance(dial, str):
            dial = split_uri(dial)
        if isinstance(dial, (tuple, list)) and len(dial) == 2:
            dial = (parse_ipv4(str(dial[0])), parse_port(dial[1]))
        else:
            dial = None
        if not origin_key or not dial or dial[0] is None or dial[1] is None:
            return None
        dial_key = endpoint_key(dial)
        if not dial_key:
            return None
        self.learned[origin_key] = dial_key
        self._remember_edge(dial[0])
        return dial_key

    def _handoff_relay(self, origin):
        """手工精确路由 > 抓包精确路由 > 已见主机推算；均不得改端口。"""
        key = endpoint_key(origin)
        if key in self.routes:
            target = self.routes[key]
        else:
            if self._relay_routes is None:
                try:
                    with open(_RELAY_TABLE, encoding='utf-8') as stream:
                        document = json.load(stream)
                except (OSError, ValueError):
                    document = {}
                self._relay_routes = _relay_tables(document)
            exact, hosts = self._relay_routes
            if key not in exact:
                ep = infer_relay(origin, hosts)
                return (*ep, 'relay') if ep else None
            target = exact[key]
        ep = split_uri(target)
        if ep and ep[0] != origin[0] and ep[1] == origin[1]:
            return (*ep, 'route')
        return None

    def candidates(self, ticket, *, max_candidates=None, prefer_origin=False, handoff_level=None):
        """返回有序拨号候选 ``[(host, port, source), ...]``（已去重）。

        仅实际换房调用方传 handoff_level；中央神坛命中实测路由时，中转、票面依次优先。
        无命中、关闭开关、首登或其他地图时保持旧行为。
        ``prefer_origin=True``（T74-R3 配对票）：旧顺序为 origin 放第一。理由是机器人
        自己的 in-band Move 迁移一直用 Move 的 origin 直拨并成功（door3 实测
        engine_rebuilt），而 WS 配对票只有 origin、没有 Pharos speed 字段；
        先猜 known_edge 会白白烧掉 0.9-1.8s，还可能连到别的实例被 36 拒。
        """
        origin = ticket_origin(ticket)
        if origin is None:
            self.last_source = "no_origin"
            return []
        limit = (max_candidates if max_candidates is not None
                 else pairing_dial_tries() if prefer_origin else dial_tries())
        origin_key = endpoint_key(origin)
        ordered = []
        if (type(handoff_level) is int and handoff_level == _CENTRAL_ALTAR and
                os.environ.get('SKY_T81_RELAY_FIRST', '1').strip().lower()
                not in ('0', 'false', 'off', 'no', 'never')):
            relay = self._handoff_relay(origin)
            if relay is not None:
                # 先保留回退位，再沿用旧候选和次数上限；后面的统一去重保留首发来源。
                ordered.extend((relay, (*origin, 'origin')))
        if prefer_origin:
            ordered.append((origin[0], origin[1], "origin"))
        ordered.extend(explicit_dials(ticket))
        if origin_key in self.routes:
            ep = split_uri(self.routes[origin_key])
            if ep is not None:
                ordered.append((ep[0], ep[1], "route"))
        if origin_key in self.learned:
            ep = split_uri(self.learned[origin_key])
            if ep is not None:
                ordered.append((ep[0], ep[1], "learned"))
        # 已知边缘 IP × 目标端口：只试本进程真正连过的游戏边缘，不扫描陌生地址。
        for host in self.known[:2]:
            ordered.append((host, origin[1], "known_edge"))
        if not prefer_origin:
            ordered.append((origin[0], origin[1], "origin"))

        out, seen = [], set()
        for host, port, source in ordered:
            key = f"{host}:{port}"
            if key in seen:
                continue
            seen.add(key)
            out.append((host, port, source))
        self.last_source = out[0][2] if out else "none"
        return out[:max(1, int(limit))]

    def snapshot(self):
        return {"routes": len(self.routes), "learned": len(self.learned),
                "known": len(self.known), "last_source": self.last_source}
