# -*- coding: utf-8 -*-
"""Optional network configuration. No deployment addresses are included."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time

import requests

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
CFG_FILE = os.path.join(_DATA_DIR, "netcfg.json")

_lock = threading.RLock()
_cache: dict | None = None
_cache_sig: tuple | None = None


# ── 配置读写 ──

def _load() -> dict:
    global _cache, _cache_sig
    with _lock:
        try:
            st = os.stat(CFG_FILE)
            sig = (st.st_mtime_ns, st.st_size)
            if _cache is not None and _cache_sig == sig:
                return _cache
            with open(CFG_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = _cache if _cache is not None else {}
        _cache = d
        try:
            _cache_sig = (os.stat(CFG_FILE).st_mtime_ns, os.stat(CFG_FILE).st_size)
        except OSError:
            _cache_sig = None
        return d


def save(cfg: dict) -> dict:
    """管理员改配置：只接受已知字段，写完立即失效缓存。"""
    global _cache, _cache_sig
    cur = dict(_load())
    for k in ("enabled", "sticky", "fp", "fp_salt", "use_health_pool", "allow_direct_when_empty"):
        if k in cfg:
            cur[k] = cfg[k]
    if "proxies" in cfg:
        raw = cfg["proxies"]
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("\r", "\n").split("\n")]
        cur["proxies"] = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if "uplinks" in cfg:
        raw = cfg["uplinks"]
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("\r", "\n").split("\n")]
        items = []
        for x in (raw or []):
            if isinstance(x, dict):
                items.append({"src_ip": str(x.get("src_ip") or "").strip(),
                              "dev": str(x.get("dev") or "").strip()})
            else:
                # "src_ip" 或 "src_ip@dev" 简写
                t = str(x).strip()
                if t:
                    ip, _, dev = t.partition("@")
                    items.append({"src_ip": ip.strip(), "dev": dev.strip()})
        cur["uplinks"] = [x for x in items if x["src_ip"] or x["dev"]]
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = CFG_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_FILE)
        try:
            os.chmod(CFG_FILE, 0o600)
        except OSError:
            pass
        _cache, _cache_sig = None, None
    return _load()


def enabled() -> bool:
    return bool(_load().get("enabled"))


def sticky() -> bool:
    return bool(_load().get("sticky", True))


def allow_direct_when_empty() -> bool:
    """**整池**没有可用代理时要不要退回直连。

    注意区分两种情况：
    - 「某个代理死了」→ 换一个健康代理重试，**绝不**退回直连（这是默认行为，无开关）；
    - 「整池空了」（巡检还没跑 / 全部过期）→ 退回直连，否则全站登录直接挂掉。

    退回直连时必须留痕（`pool_empty_direct` 会出现在 `/api/admin/risk` 里），
    否则就成了「悄悄把最关键的隐藏层拆掉还不告诉你」。
    """
    return bool(_load().get("allow_direct_when_empty", True))


def proxies() -> list[str]:
    """出站代理池：优先用 `data/proxies.json`（fir-proxy 巡检产出的**健康池**），
    退回 `netcfg.json` 的静态 proxies。"""
    raw = _load().get("proxies")
    static = [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []
    if not _load().get("use_health_pool", True):
        return static
    return health_pool() or static


POOL_FILE = os.path.join(_DATA_DIR, "proxies.json")
_pool_cache: dict | None = None
_pool_sig = None


def health_pool() -> list[str]:
    """读巡检产出的健康池（带 mtime 缓存）。坏文件/空池 → 返回空（调用方退回静态列表）。"""
    global _pool_cache, _pool_sig
    try:
        st = os.stat(POOL_FILE)
        sig = (st.st_mtime_ns, st.st_size)
        if _pool_cache is not None and _pool_sig == sig:
            d = _pool_cache
        else:
            with open(POOL_FILE, encoding="utf-8") as f:
                d = json.load(f)
            _pool_cache, _pool_sig = (d if isinstance(d, dict) else {}), sig
        if not isinstance(d, dict):
            return []
        if int(d.get("expires") or 0) and time.time() > int(d["expires"]):
            return []          # 巡检结果过期 → 宁可退回静态列表，也不用一份陈旧池
        out = d.get("proxies")
        return [str(x).strip() for x in out if str(x).strip()] if isinstance(out, list) else []
    except Exception:
        return []


def health_meta() -> dict:
    try:
        with open(POOL_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {"updated": int(d.get("updated") or 0), "expires": int(d.get("expires") or 0),
                "count": len(d.get("proxies") or []), "with_9393": len(d.get("with_9393") or []),
                "tested": int(d.get("tested") or 0), "alive": int(d.get("alive") or 0)}
    except Exception:
        return {"updated": 0, "expires": 0, "count": 0, "with_9393": 0, "tested": 0, "alive": 0}


# 失败代理黑名单：免费公开代理随时会死，坏掉的短时间内不再分配（到期自动放回）
_BAD: dict[str, float] = {}
_BAD_TTL = 240.0
_empty_warn_at = 0.0


class ProxyUnavailable(RuntimeError):
    """启用了代理但整池无可用出口（且配置禁止退回直连）。"""


def _warn_empty() -> None:
    """整池空 → 退回直连，但必须限频留痕（否则等于悄悄拆掉隐藏层）。"""
    global _empty_warn_at
    now = time.time()
    if now - _empty_warn_at > 300:
        _empty_warn_at = now
        try:
            import logging
            logging.getLogger("qfsky.netcfg").warning(
                "netcfg: 代理池为空，本次出站**退回直连**（真实 IP 暴露）。检查 data/proxies.json 巡检。")
        except Exception:
            pass


def _alive_pool() -> list[str]:
    now = time.time()
    for k in [k for k, v in _BAD.items() if v < now]:
        _BAD.pop(k, None)
    return [p for p in proxies() if p not in _BAD]


_fail_hook = None
_ok_hook = None


def set_hooks(fail=None, ok=None) -> None:
    """注册失败/成功回调（proxypool 用它把自己的黑名单与 netcfg 的 _BAD 同步）。"""
    global _fail_hook, _ok_hook
    if fail is not None:
        _fail_hook = fail
    if ok is not None:
        _ok_hook = ok


def report_failure(proxy: str) -> None:
    if proxy:
        _BAD[str(proxy)] = time.time() + _BAD_TTL
        if _fail_hook is not None:
            try:
                _fail_hook(str(proxy))
            except Exception:
                pass


def report_success(proxy: str) -> None:
    _BAD.pop(str(proxy), None)
    if proxy and _ok_hook is not None:
        try:
            _ok_hook(str(proxy))
        except Exception:
            pass


def bad_count() -> int:
    return len(_BAD)


# ── 显式绑定（qfsky.proxypool 通过回调注册进来） ──
#
# 上面那套是**全局**出口池 + 哈希取模（不落档）。运营商要向用户卖「独立 IP 出口」，
# 就必须能**按机位**指定、并落档在账号记录里 —— 那是 `qfsky.proxypool` 的活。
# 两者只在一个地方交汇：`proxy_url()` 先问显式绑定，没有再走全局池。
#
# ★ 一台机位都没绑定时 `_explicit()` 恒返回 ""，本模块行为与加这层之前**逐字节一致**
#   （包括「默认关闭 → 裸 Session」这条设计原则）。
_resolver = None       # (key, exclude) -> proxy url
_bound_probe = None    # (key) -> bool：这个 key 有没有显式绑定（快查，用于 shared_session 早退）


def set_resolver(fn) -> None:
    """注册显式绑定解析器：`fn(key, exclude) -> url`（空串 = 没有绑定）。"""
    global _resolver
    _resolver = fn


def set_bound_probe(fn) -> None:
    global _bound_probe
    _bound_probe = fn


def _explicit(key: str, exclude: tuple = ()) -> str:
    """该 key 的显式绑定出口（无绑定/解析器没装 → 空串）。"""
    if _resolver is None or not key:
        return ""
    try:
        return str(_resolver(key, tuple(exclude or ())) or "")
    except Exception:
        return ""


def explicit_bound(key: str) -> bool:
    if _bound_probe is None or not key:
        return False
    try:
        return bool(_bound_probe(key))
    except Exception:
        return False


# ── 登录前（prelogin）裸 TCP 出口 ──
#
# `_resolver(key)` 认的是账号 `user_uuid`，而 9393 是登录的第一步、跑在机位落档之前，
# 那时还没有 user_uuid。所以另开一个只服务裸 TCP 的解析回调：
#   `_tcp_resolver(prelogin_key)` → socks5 URL（拿不到就 ""，调用方直连）。
# 传进来的是 `proxypool.prelogin_key(owner, device_id)` 造出来的键。
_tcp_resolver = None


def set_tcp_resolver(fn) -> None:
    global _tcp_resolver
    _tcp_resolver = fn


def _explicit_tcp(key: str) -> str:
    if _tcp_resolver is None or not key:
        return ""
    try:
        return str(_tcp_resolver(key) or "")
    except Exception:
        return ""


# ── 绑定 ──

def _h(key: str, tag: str) -> int:
    salt = str(_load().get("fp_salt") or "qfly-fp-v1")
    return int(hashlib.sha256(("%s|%s|%s" % (salt, str(key or ""), tag)).encode()).hexdigest()[:16], 16)


def proxy_url(key: str = "") -> str:
    """账号 key → 出口代理 URL（粘性）。未启用/无代理时返回空串。

    ★ 先看**显式绑定**（proxypool，按机位落档、管理员/用户指定、挂了自动换），
    没有再走全局池。显式绑定**不受 `enabled` 开关影响** —— 用户勾了「用独立出口」
    就该生效，不需要管理员先把全局池打开。
    """
    ex = _explicit(key)
    if ex:
        return ex
    if not enabled():
        return ""
    pool = _alive_pool()
    if not pool:
        return ""
    if sticky():
        return pool[_h(key, "egress") % len(pool)]
    return pool[int(time.time() // 60) % len(pool)]


def tcp_proxy_url(key: str = "") -> str:
    """裸 TCP（9393）专用出口：**必须是 socks5**，HTTP 代理带不了裸 TCP。

    优先用该账号在 HTTP 层绑定的那个（保持同账号同一出口），但它若不是 socks5，
    就从巡检标出的「实测能连 9393」子集里再粘性挑一个。

    `key` 以 `prelogin:` 开头时（登录还没跑完、没有 user_uuid）走另一条回调，
    见 `set_tcp_resolver`。
    """
    ex = _explicit(key)
    if ex:
        # 有显式绑定就**只用它**：跨到全局健康池会让这个账号的出口不再唯一，
        # 等于把「粘性绑定」这条最重要的性质悄悄破坏掉。
        return ex if ex.lower().startswith("socks") else ""
    # 登录前的 9393：按 owner+device 认身份，只认 socks5。
    if str(key or "").startswith("prelogin:"):
        pt = _explicit_tcp(key)
        return pt if pt.lower().startswith("socks") else ""
    u = proxy_url(key)
    if u.lower().startswith("socks"):
        return u
    hp = health_meta()
    if hp["with_9393"]:
        try:
            with open(POOL_FILE, encoding="utf-8") as f:
                sub = [x for x in (json.load(f).get("with_9393") or []) if x and x not in _BAD]
        except Exception:
            sub = []
        if sub:
            return sub[_h(key, "tcp") % len(sub)]
    return u if u.lower().startswith("socks") else ""


def proxy_map(key: str = "") -> dict | None:
    """requests 的 proxies 参数形态。空 → None（直连）。"""
    u = proxy_url(key)
    if not u:
        return None
    return {"http": u, "https": u}


class _Session(requests.Session):
    """出站 Session：代理**连不上**时换一个健康代理重试一次，**绝不回退直连**。

    为什么只在 ProxyError 重试：公开代理会在转发途中死掉，那种情况重试 POST
    可能造成重复提交（发短信/领奖励都怕重复）。ProxyError = 代理在转发前就拒绝，
    重试是安全的；读超时之类只标记坏代理、不重试。
    """

    def __init__(self, key: str = ""):
        super().__init__()
        self._qfly_key = str(key or "")
        self._qfly_proxy = ""
        self._apply()

    def _apply(self, exclude=()) -> None:
        u = _explicit(self._qfly_key, exclude)      # ★ 显式绑定优先（不受 enabled 影响）
        if not u and enabled():
            pool = [p for p in _alive_pool() if p not in exclude]
            if pool:
                u = (pool[_h(self._qfly_key, "egress") % len(pool)] if sticky()
                     else pool[int(time.time() // 60) % len(pool)])
            elif not allow_direct_when_empty():
                raise ProxyUnavailable("启用了代理但整池无可用出口，且未允许直连")
            else:
                _warn_empty()
        self._qfly_proxy = u
        self.proxies.clear()
        if u:
            self.proxies.update({"http": u, "https": u})

    def request(self, method, url, **kw):
        try:
            r = super().request(method, url, **kw)
            if self._qfly_proxy:
                report_success(self._qfly_proxy)
            return r
        except requests.exceptions.ProxyError:
            return self._retry_other(url, method, **kw)
        except requests.exceptions.ConnectionError as e:
            # ⚠️ 实测坑：SOCKS5 代理连不上时，requests 抛的是 **ConnectionError**
            # （urllib3 把 PySocks 的 socket 错误包成 NewConnectionError），
            # **不是** ProxyError。只认 ProxyError 的话，死掉的 socks5 代理永远不会触发换出口。
            # 判据：错误文本里出现 SOCKS/Proxy —— 说明失败发生在**代理层**；
            # 若只是目标不可达，文本里不会带 SOCKS，那种情况代理本身是好的，不该拉黑。
            msg = str(e)
            if self._qfly_proxy and ("SOCKS" in msg or "roxy" in msg):
                return self._retry_other(url, method, **kw)
            raise
        except requests.exceptions.Timeout:
            if self._qfly_proxy:
                report_failure(self._qfly_proxy)   # 代理挂了/太慢：不重试（可能已送达）
            raise

    def _retry_other(self, url: str, method: str, **kw):
        """换一个健康出口重试一次。连不上代理 = 请求根本没出网，重试 POST 也安全。

        ★ 对显式绑定的机位，`_apply` 会顺带**自动换一个可用出口并落档**
        （proxypool.resolve_for_key → _resolve_acc），用户勾的「挂了自动换」就在这里生效。
        """
        if not self._qfly_proxy:
            raise
        report_failure(self._qfly_proxy)
        self._apply(exclude={self._qfly_proxy})
        if not self._qfly_proxy:
            raise
        r = super().request(method, url, **kw)
        report_success(self._qfly_proxy)
        return r


def session(key: str = "", headers: dict | None = None) -> requests.Session:
    """构造出站 Session。未启用代理时 = 裸 requests.Session()，与改造前完全一致。"""
    s = _Session(key)
    if headers:
        s.headers.update(headers)
    return s


def apply(s: requests.Session, key: str = "") -> requests.Session:
    """给已存在的 Session 挂代理（模块级长连 Session 用）。"""
    p = proxy_map(key)
    if p:
        s.proxies.update(p)
    return s


_plain_sess: requests.Session | None = None


def _plain() -> requests.Session:
    """未启用代理时的全局裸 Session（= 改造前 `requests.Session()` 的行为）。"""
    global _plain_sess
    if _plain_sess is None:
        _plain_sess = requests.Session()
    return _plain_sess


_POOL: dict[str, requests.Session] = {}
_POOL_MAX = 256


def shared_session(key: str = "") -> requests.Session:
    """按 key 缓存的出站 Session（每账号一个，复用连接）。

    - 未启用代理且该 key 没有显式绑定 → 返回全局裸 Session，与改造前逐字节一致；
    - 启用代理或该 key 有显式绑定 → 每 key 一个 Session，粘性绑定到同一出口。
    """
    if not enabled() and not explicit_bound(key):
        return _plain()
    with _lock:
        s = _POOL.get(key)
        if s is None:
            if len(_POOL) >= _POOL_MAX:
                _POOL.clear()
            s = session(key)
            _POOL[key] = s
        elif getattr(s, "_qfly_proxy", "") and s._qfly_proxy in _BAD:
            # 绑定的代理已判死 → 立刻换一个健康出口（不留到下次请求才自愈）
            s._apply(exclude={s._qfly_proxy})
        return s


def drop_session(key: str) -> None:
    """丢弃某个 key 缓存的长连 Session。

    ★ 绑定被**人为改动**（用户开关代理 / 管理员改绑）时必须调这个：
    缓存的 Session 里存着旧出口的 `proxies`，不丢的话会一直用旧出口，直到它自己挂掉。
    """
    if not key:
        return
    with _lock:
        _POOL.pop(str(key), None)


def drop_all_sessions() -> int:
    with _lock:
        n = len(_POOL)
        _POOL.clear()
    return n


# ── 裸 TCP（9393） ──

def socket_factory(key: str = ""):
    """返回 socks5 socket 工厂；不可用返回 None（调用方继续直连）。

    ⚠️ 返回 None 意味着 **9393 登录链走真实 IP** —— 这是最敏感的一条链路。
    所以启用代理却没有 socks5 出口时同样要留痕；`allow_direct_when_empty=false`
    时直接抛错，逼运维把 socks5 补齐，而不是悄悄裸奔。
    """
    u = tcp_proxy_url(key)
    if not u or not u.lower().startswith("socks"):
        if enabled():
            if not allow_direct_when_empty():
                raise ProxyUnavailable("启用了代理但没有 socks5 出口可跑 9393，且未允许直连")
            _warn_empty()
        return None
    try:
        import socks  # PySocks
    except Exception:
        return None
    from urllib.parse import urlparse
    p = urlparse(u)
    try:
        port = int(p.port or 1080)
    except Exception:
        port = 1080
    kind = socks.SOCKS5
    host, user, pwd = p.hostname, (p.username or None), (p.password or None)

    def _factory(dest, timeout=10.0):
        """建一条**已经连上 dest** 的 socks5 连接。

        ★ 必须在这里 connect，别把裸 socket 丢给调用方 —— 这是 2026-09-26
        「大量用户登录提示 [Errno 107] Transport endpoint is not connected」
        的真根因：PySocks 的 socksocket 是**懒连接**的，socks5 协商发生在
        `connect()` 里。工厂只 set_proxy 就返回，调用方拿到的是一个**未连接**
        的 socket，第一个 recv/sendall 立刻 ENOTCONN(107)。

        而直连那条路用的是 `socket.create_connection()`（自带 connect），
        所以同一个函数在直连时完全正常、一走代理就 107 —— 这个不对称
        正是它藏了这么久的原因：单测和体检都跑直连，代理路径没人跑。

        `dest` 是必填的 (host, port)：目标地址属于调用方（sky9393 有 HOST/PORT），
        本模块只负责「经这条 socks5 出口连到它」，不该把某个业务端点写死在这里。
        """
        s = socks.socksocket()
        s.set_proxy(kind, host, port, True, user, pwd)
        s.settimeout(timeout)
        s.connect(tuple(dest))     # ← socks5 协商在这里发生，别省
        return s

    return _factory


# ── 裸 UDP（游戏内 enet 中继） ──
#
# ⚠️ 这一层必须单独说清楚：HTTP/SOCKS5 代理**管不到**游戏内的 enet UDP 中继
# （`enet.EnetClient` 自建 SOCK_DGRAM）。也就是说只配 `proxies` 的话，
# 登录/接口走新 IP、真正进图的流量仍从 server2 原 IP 出——风控看的是后者。
# 所以 UDP 侧要的是「**绑到指定出口**」：多网卡、多 IP，或 wg/隧道接口。
# 配置 `uplinks: [{"src_ip": "...", "dev": "wg0"}, ...]`，账号粘性绑定其中一个；
# 未配置 = 不绑 = 行为与改造前一致。

def uplinks() -> list[dict]:
    raw = _load().get("uplinks")
    out = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict) and (x.get("src_ip") or x.get("dev")):
                out.append({"src_ip": str(x.get("src_ip") or "").strip(),
                            "dev": str(x.get("dev") or "").strip()})
    return out


def uplink_for(key: str = "") -> dict | None:
    pool = uplinks()
    if not pool or not enabled():
        return None
    if sticky():
        return pool[_h(key, "uplink") % len(pool)]
    return pool[int(time.time() // 60) % len(pool)]


def bind_udp(sock, key: str = "") -> dict:
    """把 UDP socket 绑到该账号的出口。返回实际生效的绑定（给日志/体检用）。"""
    up = uplink_for(key)
    if not up:
        return {}
    done: dict = {}
    if up.get("src_ip"):
        try:
            sock.bind((up["src_ip"], 0))
            done["src_ip"] = up["src_ip"]
        except OSError as e:
            done["src_ip_err"] = repr(e)
    if up.get("dev"):
        try:
            # 需要 CAP_NET_RAW（qfly.service 以 root 跑，OK）
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, up["dev"].encode() + b"\x00")
            done["dev"] = up["dev"]
        except OSError as e:
            done["dev_err"] = repr(e)
    return done


# ── 体检 ──

def egress_ip(timeout: float = 12.0) -> dict:
    """当前真实出口（直连 / 首个代理各测一次），给管理员体检页看。"""
    out: dict = {"direct": "", "via_proxy": "", "proxy": ""}
    try:
        out["direct"] = requests.get("https://api.ipify.org", timeout=timeout).text.strip()[:64]
    except Exception as e:
        out["direct"] = "探测失败：%r" % e
    pool = proxies()
    if pool and enabled():
        u = pool[0]
        out["proxy"] = u
        try:
            r = requests.get("https://api.ipify.org", timeout=timeout,
                             proxies={"http": u, "https": u})
            out["via_proxy"] = r.text.strip()[:64]
        except Exception as e:
            out["via_proxy"] = "探测失败：%r" % e
    return out


def _mask(u: str) -> str:
    """脱敏代理 URL（隐藏账号密码），给前端展示。"""
    try:
        from urllib.parse import urlparse
        p = urlparse(u)
        auth = "%s:***@" % p.username if p.username else ""
        return "%s://%s%s%s" % (p.scheme, auth, p.hostname or "", (":%d" % p.port) if p.port else "")
    except Exception:
        return "(无法解析)"


def status() -> dict:
    pool = proxies()
    ups = uplinks()
    hp = health_meta()
    return {
        "enabled": enabled(),
        "sticky": sticky(),
        "count": len(pool),
        "proxies": [_mask(x) for x in pool[:20]],
        "uplink_count": len(ups),
        "uplinks": ["%s%s" % (x["src_ip"] or "-", ("@" + x["dev"]) if x["dev"] else "") for x in ups],
        "socks_ready": _socks_ok(),
        "fp": bool(_load().get("fp", True)),
        "fp_salt": str(_load().get("fp_salt") or "qfly-fp-v1"),
        # fir-proxy 巡检产出的健康池
        "health_pool": hp,
        "bad_now": bad_count(),
        "use_health_pool": bool(_load().get("use_health_pool", True)),
        "allow_direct_when_empty": allow_direct_when_empty(),
        "pool_empty_direct": bool(enabled() and not pool),
    }


def _socks_ok() -> bool:
    try:
        import socks  # noqa: F401
        return True
    except Exception:
        return False
