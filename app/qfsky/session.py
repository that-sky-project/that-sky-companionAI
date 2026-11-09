# -*- coding: utf-8 -*-
"""国服进图会话：拉票 → JoinGame → burst → 保活池。移植 CnOnlineSession/PreviousJoin/KeepAliveSessions。

协议参数默认值对齐 JoinProtocolDefaults.kt（sv/cv 随大版本可能失效，允许设置页/环境覆盖）。
"""
from __future__ import annotations

import os
import struct
import threading
import time
import uuid

from . import enet, joingame, version, current_join
from .skyapi import SkyClient
from .util import derive_peer_key

# 每图真机 JoinGame 样本目录：assets/jg_templates/<map_id>.bin（整包明文，含当前 levelHash+blob）。
# 背景（知识库 42/43 篇 + 09-15 实测）：8 张 0x09 秒断图（晨岛/小镇等）是服务端更新场景后
# levelHash 变化（如小镇 24303→52846），旧 blob+旧 hash 一律被踢；唯一修法=重抓真机样本整包复用
# （rewrite 只替换 7 个会话字段+版本+levelId，样本自带 levelHash@0x7A 与 blob.levelHash@0xD7 保持自洽）。
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def load_jg_template(map_level_id: int) -> bytes | None:
    """读取该图真机 JoinGame 样本；无样本返回 None（走通用 CN 模板）。"""
    if current_join.mode() != "legacy":
        raise current_join.CaptureRequired("历史 jg_templates 目录仅允许显式 legacy 模式")
    p = os.path.join(_ASSETS_DIR, "jg_templates", "%d.bin" % (map_level_id & 0xFFFFFFFF))
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None

# 进图协议默认参数（JoinProtocolDefaults.kt 基线 + 代际实测更新）：
#   08-19 实测 433/5111 → 200；2026-09-06 实测已漂移到 438/5124（当时游戏 0.16.3.412289 未变，服务端静默 bump）。
#   2026-09-24 游戏切到 0.16.5.412659 正式版 —— 代际**未观察到新的漂移**，仍保持 438/5124；
#   一旦线上出现 418，由 _auto_fix_generation 扫描出新代际并写回 override（内存态）。
#   418 = 代际过期。漂移时 fetch_ticket 会自动扫描新代际并写回 override（见 _auto_fix_generation）。
CN_SERVER_VERSION = 442
CN_CLIENT_VERSION = 5127
CN_LEVEL_HASH = 55942
CN_LEVEL_ID = 3526133726

HANDSHAKE_ATTEMPTS = 3
HANDSHAKE_RETRY_GAP_MS = 0.35

KEEPALIVE_MS = 0.05
MAX_ALIVE_MS = 150.0

_PARAMS_LOCK = threading.Lock()
_params_override: dict[str, int] = {}


def set_params_override(server_version: int | None = None, client_version: int | None = None,
                        level_hash: int | None = None, level_id: int | None = None) -> None:
    with _PARAMS_LOCK:
        if server_version:
            _params_override["sv"] = int(server_version)
        if client_version:
            _params_override["cv"] = int(client_version)
        if level_hash:
            _params_override["lh"] = int(level_hash)
        if level_id:
            _params_override["lid"] = int(level_id)


def get_params_override() -> dict[str, int]:
    with _PARAMS_LOCK:
        return dict(_params_override)


def _p(key: str, default: int) -> int:
    with _PARAMS_LOCK:
        v = _params_override.get(key)
    return v if v else default


def resolve_build(preferred_version: str = "") -> int | None:
    v = (preferred_version or "").strip() or version.cn_version()
    b = version.build_from_version(v)
    if b:
        return b
    version.refresh_cn_version()
    return version.build_from_version(version.cn_version())


# ── 保活会话池 ──

class _Session:
    def __init__(self, client: enet.EnetClient):
        self.client = client
        self.keepalive_stop = threading.Event()
        self.deadline = time.monotonic() + MAX_ALIVE_MS
        self.thread: threading.Thread | None = None


class KeepAlivePool:
    def __init__(self):
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def open(self, client: enet.EnetClient) -> str:
        sid = uuid.uuid4().hex[:12]
        s = _Session(client)
        with self._lock:
            self._sessions[sid] = s

        def run():
            while not s.keepalive_stop.is_set():
                if client.state == enet.ST_DISCONNECTED:
                    self.close(sid)
                    return
                try:
                    client.service(96)
                except Exception:
                    self.close(sid)
                    return
                s.keepalive_stop.wait(KEEPALIVE_MS)

        s.thread = threading.Thread(target=run, name="keepalive-%s" % sid, daemon=True)
        s.thread.start()
        return sid

    def is_alive(self, sid: str) -> bool:
        s = self._sessions.get(sid)
        return s is not None and s.client.state != enet.ST_DISCONNECTED

    def touch(self, sid: str) -> None:
        s = self._sessions.get(sid)
        if s:
            s.deadline = time.monotonic() + MAX_ALIVE_MS

    def check_watchdog(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [sid for sid, s in self._sessions.items() if now > s.deadline]
        for sid in expired:
            self.close(sid)

    def close(self, sid: str) -> bool:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s is None:
            return False
        s.keepalive_stop.set()
        try:
            s.client.close()
        except Exception:
            pass
        return True

    def close_all(self) -> None:
        with self._lock:
            sids = list(self._sessions)
        for sid in sids:
            self.close(sid)

    def active_count(self) -> int:
        return len(self._sessions)


POOL = KeepAlivePool()


# ── 进图 ──

class JoinHandle:
    def __init__(self, client: enet.EnetClient | None, message: str):
        self.client = client
        self.message = message


def _on_app_message_factory(cli: enet.EnetClient, effective_level_id: int, seq_holder: list[int]) -> None:
    def cb(msg_type: int, size: int, payload: bytes, chan: int, reliable_seq: int) -> None:
        if msg_type == 0x0E and payload and (payload[0] & 0xFF) in (0x11, 0x09):
            echo = payload[-2:] if len(payload) >= 2 else b"\x01\x00"
            seq_holder[0] = (seq_holder[0] + 1) & 0xFF
            try:
                cli.send_app_reliable(0, 0x0E, seq_holder[0], joingame.auth_renew_reply(effective_level_id & 0xFFFFFFFF, echo))
            except Exception:
                pass
    return cb


def resolve_join_level_id(template: bytes | None, override: int | None = None) -> int | None:
    """决定 JoinGame 的 joinLevelId（会被写进 @0x7A 与 blob 头 @0xD7）。

    ★ per-map 模板自带 levelHash/blob：绝不能写 → 返回 override（enter_map 传 None）。
      写成 CN_LEVEL_HASH(55942) 会让 JoinGame 与同会话注册包/票上下文不自洽 → 服务端 0x09 秒踢
      （09-27 生产 A/B 定论：写 55942 必失败、保留样本 hash 0.5s 进图）。
    通用模板（无样本图）仍按 override → CN_LEVEL_HASH。
    """
    if template is not None:
        return override
    return override if override is not None else _p("lh", CN_LEVEL_HASH)


def join(
    client: SkyClient,
    uid: str,
    session_hex: str,
    ticket: dict,
    keep_alive: bool,
    build_number: int | None,
    map_level_id: int | None = None,
    join_level_id_override: int | None = None,
    server_version: int | None = None,
    client_version: int | None = None,
    template: bytes | None = None,
    prepared_current=None,
) -> JoinHandle:
    modern = current_join.mode() == "current"
    level_id = map_level_id if map_level_id is not None else _p("lid", CN_LEVEL_ID)
    peer = derive_peer_key(uid)
    if modern:
        if template is not None:
            return JoinHandle(None, "新版入口不接受历史整包模板")
        if keep_alive:
            return JoinHandle(None, "新版注册首组包/保活链尚未验证，不发送旧版 burst")
        try:
            prepared_current = prepared_current or current_join.prepare(level_id, uid,
                build_number if build_number is not None else resolve_build(client.client_version))
            payload = joingame.build_current_payload(prepared_current, uid=uid,
                session_hex=session_hex, ticket=ticket, peer_key=peer)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return JoinHandle(None, str(exc))
    else:
        sv = server_version if server_version is not None else _p("sv", CN_SERVER_VERSION)
        cv = client_version if client_version is not None else _p("cv", CN_CLIENT_VERSION)
        lh = resolve_join_level_id(template, join_level_id_override)
        level_id = map_level_id if map_level_id is not None else _p("lid", CN_LEVEL_ID)
    
        peer = derive_peer_key(uid)
        payload = bytes(joingame.rewrite_payload(
            template=template if template is not None else joingame.get_cn_template(legacy=True),
            peer_key_u32=peer,
            user_uuid=uid,
            session_hex=session_hex,
            sig_hex=ticket["sig"],
            server_ip=ticket["ip"],
            server_port=ticket["port"],
            move_ts=ticket["move_ts"],
            server_version=sv,
            client_version=cv,
            build_number=build_number if (build_number or 0) > 0 else None,
            join_level_id=lh,
            level_id=level_id,
            fix_net_version=False,
            legacy=True,
        ))
    seq_holder = [0x20]
    picked: enet.EnetClient | None = None
    for attempt in range(1, HANDSHAKE_ATTEMPTS + 1):
        c = None
        try:
            c = enet.EnetClient(ticket["ip"], ticket["port"], peer, on_app_message=None,
                                egress_key=uid)
            cb = _on_app_message_factory(c, level_id, seq_holder)
            c.on_app_message = cb
            c.connect()
            if c.wait_until_connected():
                picked = c
                break
            c.close()
        except Exception:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        if attempt < HANDSHAKE_ATTEMPTS:
            time.sleep(HANDSHAKE_RETRY_GAP_MS)
    cli = picked
    if cli is None:
        return JoinHandle(None, "UDP 握手超时")

    try:
        cli.send_reliable(0, payload)
        # 顺序不能反：JoinGame 必须明文发出，之后才启用 app 层 RC4
        cli.enable_app_rc4(ticket["sig"])
        if keep_alive and not modern:
            if template is not None:
                # per-map 模板：静态 burst 的 keyframe/坐标/levelHash 属于旧图，发出即 0x09；
                # 改发真机注册包（peer/levelHash/nv/levelId 全部对齐本图模板）。
                joingame.send_template_registration(cli, level_id, peer, template)
            else:
                # burst level 必须 patch 成目标 map_id（合取条件之一，与 fixNetVersion=False）；
                # burst 0x04 内嵌 NetVersion 必须与本次 JoinGame 用的模板一致（否则 0x09）。
                joingame.send_success_burst(cli, level_id, lh, nv_bytes=bytes(payload[0x7C:0x86]))
        settle = cli.wait_until_join_settled(chan=0)
        if not settle["settled"]:
            cli.close()
            return JoinHandle(None, "JoinGame 后断开" if settle["disconnected"] else "JoinGame 未就绪")
        return JoinHandle(cli, "")
    except Exception as t:
        try:
            cli.close()
        except Exception:
            pass
        return JoinHandle(None, "UDP 异常：%s" % (t))


# ── 拉票失败的**原因**（同线程）─────────────────────────────────────────────
# fetch_ticket 把异常吞成 None，于是「拉票失败」这四个字丢掉了 HTTP 状态：
# 418/422（代际过期 / 出口限流，平台级）与 401（会话被顶）长得一模一样。
# 2026-10-09：跑图遇到拉票失败要**自动续期**，可平台级 418 那天续期入口本身也是 418
# （与 tasks.with_renew 同一判据）—— 续期只会白打一次网关、还把账号标成「待续期」。
# 所以把最后一次异常留在 thread-local 里，供任务层的续期钩子做判据（同线程调用）。
_TICKET_ERR = threading.local()


def last_ticket_error() -> Exception | None:
    """上一次拉票失败抛出的异常（同线程）。

    `None` 有两种含义：从没失败过 / 成功过；或 HTTP 200 但返回体里没有可用票
    （sig/uri/move_ts 缺失）——后者原因不明，交给续期钩子按「会话可能被顶」处理。
    """
    return getattr(_TICKET_ERR, "err", None)


def fetch_ticket(client: SkyClient, rejoin_uid: str | None = None,
                 level_hash: int | None = None, level_id: int | None = None,
                 server_version: int | None = None, client_version: int | None = None) -> dict | None:
    if current_join.mode() == "current" and (level_hash is None or level_id is None):
        _TICKET_ERR.err = current_join.CaptureRequired("当前模式拉票前必须匹配本图实测缓存")
        return None
    try:
        resp = client.find_previous_or_empty(
            server_version=server_version if server_version is not None else _p("sv", CN_SERVER_VERSION),
            client_version=client_version if client_version is not None else _p("cv", CN_CLIENT_VERSION),
            level_hash=level_hash if level_hash is not None else _p("lh", CN_LEVEL_HASH),
            level_id=level_id if level_id is not None else _p("lid", CN_LEVEL_ID),
            rejoin_uid=rejoin_uid,
        )
    except Exception as e:
        _TICKET_ERR.err = e
        status = getattr(e, "status", None)
        if status == 418 and current_join.mode() == "legacy" and _auto_fix_generation(client, level_id):
            return fetch_ticket(client, rejoin_uid=rejoin_uid, level_hash=level_hash, level_id=level_id)
        return None
    _TICKET_ERR.err = None
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    sig = str(data.get("sig") or data.get("signature") or "")
    uri = str(data.get("uri") or "")
    ip = str(data.get("ip") or "")
    port = int(data.get("port") or 0)
    move_ts = int(data.get("move_ts") or 0)
    if not sig:
        return None
    if ip and port:
        pass
    elif uri and ":" in uri:
        h, _, p = uri.rpartition(":")
        ip, port = h, int(p)
    else:
        return None
    if move_ts == 0 or port <= 0:
        return None
    ticket = {"sig": sig, "ip": ip, "port": port, "move_ts": move_ts}
    # Preserve native current-ticket fields; no guessed default for platform_filter.
    if "uri" in data:
        ticket["uri"] = data["uri"]
    if "platform_filter" in data:
        ticket["platform_filter"] = data["platform_filter"]
    return ticket


_GEN_LOCK = threading.Lock()


def _auto_fix_generation(client: SkyClient, level_id: int | None) -> bool:
    """拉票 418 = sv/cv 代际过期（网易服务端静默 bump，versionCode 不变）。
    以当前基点为锚做限幅 2D 扫描，命中即写回 override。历史代际：
    (423,5100) → (431,5109) → (433,5111) → (438,5124)。"""
    with _GEN_LOCK:
        base_sv = _p("sv", CN_SERVER_VERSION)
        base_cv = _p("cv", CN_CLIENT_VERSION)
        lh = _p("lh", CN_LEVEL_HASH)
        lid = level_id if level_id is not None else _p("lid", CN_LEVEL_ID)
        tried = {(base_sv, base_cv)}

        def probe(sv: int, cv: int) -> bool:
            if (sv, cv) in tried:
                return False
            tried.add((sv, cv))
            try:
                client.find_previous_or_empty(server_version=sv, client_version=cv, level_hash=lh, level_id=lid)
                set_params_override(server_version=sv, client_version=cv)
                return True
            except Exception:
                return False

        # 先扫同行/同列，再扩散环（cv 变化幅度历史上比 sv 大，先宽 cv）
        for r in range(1, 11):
            for sv, cv in [(base_sv, base_cv + r), (base_sv, base_cv - r),
                           (base_sv + r, base_cv), (base_sv - r, base_cv)]:
                if probe(sv, cv):
                    return True
        for r in range(1, 7):
            for dcv in range(-4 * r, 4 * r + 1, 2):
                for sv, cv in [(base_sv + r, base_cv + dcv), (base_sv - r, base_cv + dcv)]:
                    if probe(sv, cv):
                        return True
        return False


def enter_map(
    client: SkyClient,
    map_level_id: int,
    join_level_id: int = 0,
    rejoin_uid: str | None = None,
    build_number: int | None = None,
    on_stale=None,
) -> tuple[bool, str, str]:
    """进图 + 保活。返回 (ok, sessionId|'', message)。

    `on_stale(client)`：**拉票失败时的自动续期钩子**（可选，由任务层提供）。
    用户报「跑图遇到拉票失败」= 会话被顶掉（`accounts` 里那把 session 已被后开的
    客户端作废，见 docs/0.16.5_412659知识库/22 篇 §10），不是限流 —— 单靠
    `enter_map` 自己重试永远失败，只能重登（续期）+ `replace_session`。
    钩子返回真值时用**同一个 client**（会话字段已被就地改新）重拉一次票；
    返回假值/抛异常则保持原语义（照旧返回「拉票失败」）。
    """
    if current_join.mode() == "current":
        try:
            current_join.prepare(map_level_id, client.uid,
                build_number if build_number is not None else resolve_build(client.client_version),
                require_registration=True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return False, "", str(exc)
        return False, "", "新版注册首组包/保活链尚未接入，未发网络请求"
    tpl = load_jg_template(map_level_id)
    # 模板图：票的 level_hash 对齐模板当前 hash（服务端按票上下文校验进图/注册）。
    tpl_hash = joingame.template_level_hash(tpl) if tpl is not None else None

    def _attempt():
        ticket = fetch_ticket(client, rejoin_uid=rejoin_uid, level_id=map_level_id, level_hash=tpl_hash)
        if ticket is None:
            return None, "拉票失败"
        h = join(
            client=client,
            uid=client.uid,
            session_hex=client.session,
            ticket=ticket,
            keep_alive=True,
            build_number=build_number if build_number is not None else resolve_build(client.client_version),
            map_level_id=map_level_id,
            # 有样本则整包复用（样本自带当前 levelHash+blob，忽略路线 join 值）；
            # 否则走通用 CN 模板 + 路线 join_level_id。
            join_level_id_override=None if tpl is not None else (join_level_id if join_level_id > 0 else None),
            template=tpl,
        )
        return h, h.message

    h, msg = _attempt()
    if h is None and msg == "拉票失败" and on_stale is not None:
        # ★ 拉票失败 = 会话被顶（22 篇 §10）：续期一次再拉，成功则本次进图继续。
        try:
            renewed = bool(on_stale(client))
        except Exception as e:                                    # noqa: BLE001
            renewed = False
            print("[session] 拉票失败后的自动续期钩子异常：%s" % str(e)[:160], flush=True)
        if renewed:
            h, msg = _attempt()
    if h is not None and h.client is None and tpl is not None:
        # 服务端对同图快速重进有偶发冷却（1s 内重试约半数 0x09），拉开后至多重试两次
        for _ in range(2):
            time.sleep(6)
            h, msg = _attempt()
            if h is not None and h.client is not None:
                break
    if h is None or h.client is None:
        return False, "", msg
    return True, POOL.open(h.client), "进图成功"


def ensure_online(client: SkyClient) -> tuple[bool, bool, str]:
    """短连接转在线（国服每日任务用）。返回 (online, cached, message)。"""
    prepared = None
    if current_join.mode() == "current":
        try:
            prepared = current_join.prepare(CN_LEVEL_ID, client.uid, resolve_build(client.client_version))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return False, False, str(exc)
    ticket = fetch_ticket(client, level_id=CN_LEVEL_ID if prepared else None,
        level_hash=prepared["level_hash"] if prepared else None,
        server_version=prepared["fields"]["server_version"] if prepared else None,
        client_version=prepared["fields"]["client_version"] if prepared else None)
    if ticket is None:
        return False, False, "拉票失败"
    h = join(
        client=client,
        uid=client.uid,
        session_hex=client.session,
        ticket=ticket,
        keep_alive=False,
        build_number=resolve_build(client.client_version),
        map_level_id=CN_LEVEL_ID,
        prepared_current=prepared,
    )
    if h.client is None:
        return False, False, h.message
    try:
        online = False
        for _ in range(23):
            if h.client.state == enet.ST_DISCONNECTED:
                break
            try:
                h.client.service(96)
            except Exception:
                pass
            if _probe_online(client):
                online = True
                break
            time.sleep(0.35)
        return online, False, "在线成功" if online else "进房成功但未就绪"
    finally:
        try:
            h.client.close()
        except Exception:
            pass


def _probe_online(client: SkyClient) -> bool:
    try:
        client.post("/service/social-feed/api/v1/get_user_feeds", {
            "pool_type": "RecordingCandle", "pool_name": "CandleSpace", "skip": 0, "batch_size": 1,
        })
        return True
    except Exception:
        return False
