# -*- coding: utf-8 -*-
"""IP 代理池：管理员维护出口池 → 用户/机位按需粘性绑定 → 跑图链路走指定出口。

为什么要有这一层（与 netcfg 的分工）
------------------------------------
`netcfg` 解决的是**全局**问题：全站一个出口（server2 美国机房 IP）→ 137 个国服账号
被网易按出口聚类批量封禁（2026-09-14 实测，见 netcfg 模块头）。它的绑定方式是
`sha256(fp_salt|key|egress) % len(pool)`——**不落档**，改池子大小所有账号全部重新分配。

本模块解决的是**运营**问题，两者不重叠：

  1. 管理员要能 **贴一批代理进来**（`host:port` 一行一个）、批量测活、看每个出口
     现在挂了哪些账号、被谁在用；
  2. 用户登录/跑图前可以 **勾选「用独立 IP 出口」**，勾了就只给**他这一台机位**
     粘一个出口并**落档在账号记录里**（`proxy_mode/proxy_id/proxy_url`）；
  3. 绑定的出口死了 → **自动换一个可用的**（用户明确要求），换不掉才退回直连并留痕。

衔接方式只有一个回调：`netcfg.set_resolver(proxypool.resolve_for_key)`。
`netcfg.proxy_url(key)` 先问显式绑定，没有绑定再走它原来的全局池 —— 所以
**一台机位都没绑定、或总开关关着时，netcfg 的行为与改造前逐字节一致**。

落档位置
--------
- `data/proxypool.json`（0600）：池子本身 + 开关 + 每个出口的体检结果与统计 +
  用户级偏好（谁勾了「用代理」）。**不含任何账号凭证**。
- 机位绑定写在 `data/accounts.json` 的账号记录上（`proxy_mode/proxy_id/proxy_url/
  proxy_note/proxy_at`），`store.upsert_account_slot` 刷新凭证时不会碰这些字段，
  所以重新登录 / 续期都不会掉绑定。

已知边界（诚实写在这里，界面上也要写）
------------------------------------
- 只有 **HTTP(S)/SOCKS5 能覆盖的请求**会走这个出口，即 `SkyClient` 的全部 REST
  调用（跑图、领奖、刷新、装扮、好友…，key = 账号 `user_uuid`）与 `bans.py`。
- **登录那一跳（9393 裸 TCP）** 发生在机位落档**之前**，那一刻没有 `user_uuid`。
  所以它走一条**独立的 prelogin 解析链路**：按 `prelogin:<owner_uid>:<device_id>`
  认身份（这两个值在 9393 跑之前就都在手上，且同人同设备天然粘性），**只认 socks5**
  （HTTP 代理带不了裸 TCP）。登录完成、机位落档后，`apply_login_pref` 会把机位正式
  绑到同一个 socks5 上，于是「登录跳」和「跑图跳」落在同一出口。
  入口：`proxypool.prelogin_exit()`，经 `netcfg.set_tcp_resolver()` 接进
  `netcfg.tcp_proxy_url()`；出口经 `netcfg.socket_factory()` 变成 PySocks socket，
  由 `sky9393._send_and_recv()` 真正使用。
  **没有可用 socks5 时该跳退回直连**（真实 IP 暴露），`stats()["tcp_ready"]` 会把这件事
  暴露给面板。
- **进图的 UDP（enet 中继）走的是本机网卡**，HTTP/SOCKS5 代理管不到，要覆盖只能配
  `netcfg.uplinks`（多网卡源 IP 绑定）。所以 `udp_ok` 恒为 False，界面不能假装
  整条链路都换了 IP。
- 两类出口的**能力差异**（面板按此分组说明）：
  · **socks5** 覆盖「REST 调用 + 9393 裸连登录」两条链路，覆盖最完整；
  · **http** 只覆盖 REST 调用，9393 那一跳会退回直连。
"""
from __future__ import annotations

import concurrent.futures as _fut
import hashlib
import json
import os
import re
import threading
import time
from urllib.parse import quote, urlparse

import requests

from . import netcfg, store

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
CFG_FILE = os.path.join(_DATA_DIR, "proxypool.json")

_lock = threading.RLock()
_cache: dict | None = None
_cache_sig: tuple | None = None

# ── 默认配置 ──
DEFAULTS = {
    # 总开关。关了 = 所有显式绑定立即失效（机位退回直连），但**池子与绑定都保留**，
    # 重新打开就恢复。用于「出口池整体出问题，先全站绕开」这种应急。
    "enabled": False,
    # 勾选「用代理」时自动分配一个可用出口（用户勾选但没指定具体出口时）
    "auto_assign": True,
    # 是否允许用户在出口列表里自己挑（关掉则只能「自动分配」）
    "allow_user_choice": True,
    # 粘性：绑定后不主动换，只在出口不可用时才换
    "sticky": True,
    # 一个出口最多挂几台机位（0 = 不限）。这是本模块的核心价值：
    # 「137 个号挤一个 IP」就是没有这个上限造成的。
    "max_pick_per_proxy": 0,
    # 失败惩罚期（秒）：期间不再分配给新机位
    "punish_secs": 300,
    "probe_timeout": 8.0,
    # 出口归还后的休息时长（秒）。用户指定 600：跑完图的出口 10 分钟内不接新机位，
    # 避免「同一 IP 连着换账号跑」这个特征。0 = 关闭该节流。
    "cooldown_secs": 600,
    "updated": 0,
    "items": [],
    # 用户级偏好：{user_id: {use, proxy_id, at}}（谁是「勾了自启用独立出口」的）
    "user_prefs": {},
}

_CONFIG_KEYS = ("enabled", "auto_assign", "allow_user_choice", "sticky",
                "max_pick_per_proxy", "punish_secs", "probe_timeout",
                "cooldown_secs")

# ── 解析 ──
_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")
# 端口 → 协议启发式。只用于**没写协议前缀**的行；管理员写完可以在界面上改。
_SOCKS_PORTS = {1080, 1081, 1088, 2080, 4145, 9050, 9051, 10808, 10809, 20170, 20171}
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._\-]{0,252})$")
_PORT_RE = re.compile(r"^\d{1,5}$")

PROBE_TIMEOUT = 8.0
# 出口跑完图归还后的休息时长（秒）。用户指定 10 分钟：同一个出口刚服务过一个机位，
# 立刻换下一个机位接着用，在目标站看来就是「同一 IP 高频换账号」——这正是要避开的特征。
# 所以是**严格**语义：归还后这段时间内该出口完全不接新机位，而不是「优先避开」。
COOLDOWN_SECS = 600

# ── 协议分类与用途（面板分组展示要用，别把这段意义写进前端硬编码）──
# 为什么要分：SOCKS5 是**传输层**代理，能承载任意 TCP 连接 —— qfly 登录时的 9393
# 裸 TCP 短连接、以及 REST 请求都能走它；HTTP 代理只认 HTTP 请求，9393 那种裸连
# **走不了**。所以分组不是美观问题，是「这个出口能不能覆盖登录链路」的实质差别。
SCHEME_ZH = {
    "http": "HTTP 代理",
    "https": "HTTPS 代理",
    "socks5": "SOCKS5 代理",
    "socks5h": "SOCKS5h 代理",
    "socks4": "SOCKS4 代理",
    "socks4a": "SOCKS4a 代理",
}
SCHEME_HINT = {
    "http": "只转发 HTTP/HTTPS 请求。跑图、领奖、刷新等 REST 调用可以走它；"
            "登录时的 9393 裸 TCP 短连接**走不了**（那一跳会退回直连、暴露真实 IP）。",
    "https": "同 HTTP 代理，只转发 HTTP/HTTPS 请求；9393 裸连走不了（退回直连）。",
    "socks5": "传输层代理，能承载任意 TCP：REST 调用**和** 9393 裸连登录都能走它，"
              "覆盖最完整（登录与跑图落在同一出口）。UDP（enet 中继）依然不走代理，"
              "只能靠 `uplinks` 绑出口源 IP。",
    "socks5h": "同 SOCKS5，且域名交给代理端解析（本机不泄露 DNS）；同样能带 9393 裸连。",
    "socks4": "老式 SOCKS4，只支持 IPv4 和 TCP；可用性一般。",
    "socks4a": "SOCKS4a，支持域名远端解析；可用性一般。",
}


class PoolError(ValueError):
    """贴进来的行/参数不合法（前端直接把 message 显示给管理员）。"""


def _norm_line(raw: str) -> str:
    """去掉行内注释（# 或 ），返回裸行。"""
    t = str(raw or "").replace("\r", "").strip()
    if not t:
        return ""
    t = t.split("#", 1)[0].strip()
    if t.startswith("//"):
        return ""
    return t


def _default_scheme(port: int, mode: str = "auto") -> str:
    if mode and mode != "auto":
        if mode not in _SCHEMES:
            raise PoolError("未知的默认协议：%s" % mode)
        return mode
    if port == 9050:
        return "socks5h"        # Tor 的 SOCKS 口，远端解析域名
    if port in _SOCKS_PORTS:
        return "socks5"
    return "http"


def norm_url(raw: str, default_scheme: str = "auto") -> str:
    """任意写法的代理 → 规范 URL。

    支持（TARGET 占位，真实值由管理员贴入）：
        host:port
        host:port:user:pass          ← 很多代理站的导出格式
        host/port/user/pass          ← 2026-09-25 加：付费池面板就是这种斜杠格式
                                          （如 192.0.2.1/1080/example/not-a-real-password），
                                          直接贴会全判非法，运营商文档也爱这么印）
        user:pass@host:port
        scheme://[user:pass@]host:port
    不支持 IPv6 字面量（写成 [::1]:1080 会被判为格式错误并原样退回给管理员，
    不静默丢掉——静默丢行是这类导入功能的头号坑）。
    """
    t = _norm_line(raw)
    if not t:
        raise PoolError("空行")
    # ★ 斜杠格式先归一成冒号格式：只在**没有 scheme**（没有 `://`）、
    #   且确实是 3 段以上 `a/b/c[/d]` 时才转，避免误伤路径或 `://` 里的斜杠。
    if "://" not in t and t.count("/") >= 2:
        seg = [x for x in t.split("/") if x != ""]
        if len(seg) >= 3 and seg[0] and seg[1].isdigit():
            t = ":".join(seg)
    user = pwd = ""
    scheme = ""
    rest = t
    if "://" in t:
        scheme, _, rest = t.partition("://")
        scheme = scheme.strip().lower()
        if scheme not in _SCHEMES:
            raise PoolError("不支持的协议：%s" % scheme)
    if "@" in rest:
        cred, _, rest = rest.rpartition("@")
        if ":" in cred:
            user, _, pwd = cred.partition(":")
        else:
            user = cred
    parts = rest.split(":")
    if len(parts) == 2:
        host, port_s = parts
    elif len(parts) >= 4 and not user:
        # host:port:user:pass（很多代理站的导出格式）；pass 里含冒号也一并吃掉
        host, port_s, user = parts[0], parts[1], parts[2]
        pwd = ":".join(parts[3:])
    elif len(parts) == 1:
        raise PoolError("缺端口（要 host:port）")
    else:
        raise PoolError("格式不认识（要 host:port 或 host:port:user:pass）")
    host = host.strip().strip("[]").lower()
    port_s = port_s.strip()
    if not host or not _HOST_RE.match(host):
        raise PoolError("主机名不合法：%s" % (host or "(空)"))
    if not _PORT_RE.match(port_s):
        raise PoolError("端口不合法：%s" % (port_s or "(空)"))
    port = int(port_s)
    if port < 1 or port > 65535:
        raise PoolError("端口越界：%d" % port)
    scheme = scheme or _default_scheme(port, default_scheme)
    auth = ""
    if user:
        auth = quote(user, safe="")
        if pwd:
            auth += ":" + quote(pwd, safe="")
        auth += "@"
    return "%s://%s%s:%d" % (scheme, auth, host, port)


def display(url: str) -> str:
    """面向界面的短表示：只留 host:port（**不带账密**）。"""
    try:
        p = urlparse(str(url or ""))
        if not p.hostname:
            return ""
        return "%s:%d" % (p.hostname, p.port or 0)
    except Exception:
        return ""


def mask_url(url: str) -> str:
    """管理界面用：保留账密结构，但把密码打码（管理员也需要认得出是哪个出口）。"""
    s = str(url or "")
    if "@" not in s:
        return s
    scheme, sep, rest = s.partition("://")
    cred, _, host = rest.rpartition("@")
    user, c, pwd = cred.partition(":")
    if not c:
        return s
    return "%s%s%s:%s@%s" % (scheme, sep, user, "*" * min(len(pwd), 8), host)


def parse_text(text: str, default_scheme: str = "auto") -> dict:
    """批量解析。返回 {ok:[url], dup:[原行], bad:[{line, why}]} —— 去重是**必须**的，
    代理列表里同一行重复出现很常见（实测附件第 21/22 行就是同一台）。"""
    ok: list[str] = []
    dup: list[str] = []
    bad: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(str(text or "").split("\n"), 1):
        line = _norm_line(raw)
        if not line:
            continue
        try:
            u = norm_url(line, default_scheme)
        except PoolError as e:
            bad.append({"line": line[:120], "why": str(e)})
            continue
        except Exception as e:                                  # noqa: BLE001
            bad.append({"line": line[:120], "why": str(e)[:80]})
            continue
        if u in seen:
            dup.append(line[:120])
            continue
        seen.add(u)
        ok.append(u)
    return {"ok": ok, "dup": dup, "bad": bad}


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
        except FileNotFoundError:
            # ★ 文件**不存在**时必须返回空配置，不能退回 _cache。
            #   退回旧缓存意味着「删掉配置/条目后进程还在按旧数据干活」：
            #   曾表现为测试里 reset() 删了 proxypool.json 再只导入 1 条，
            #   结果 _free_exit() 还能挑出上一轮的出口 —— 删掉的数据诈尸。
            #   生产里正常不会删这个文件，但运维手删、备份还原、容器重建都会踩到。
            d = {}
        except Exception:
            # 其它异常（JSON 损坏、权限问题）仍退回缓存：那属于「读不出来」，
            # 用上一次的正常配置继续比全池瘫痪更可取。
            d = _cache if _cache is not None else {}
        merged = dict(DEFAULTS)
        merged.update(d)
        if not isinstance(merged.get("items"), list):
            merged["items"] = []
        if not isinstance(merged.get("user_prefs"), dict):
            merged["user_prefs"] = {}
        _cache = merged
        try:
            _cache_sig = (os.stat(CFG_FILE).st_mtime_ns, os.stat(CFG_FILE).st_size)
        except OSError:
            _cache_sig = None
        return _cache


def _save(d: dict) -> None:
    global _cache, _cache_sig
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = CFG_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_FILE)
        try:
            os.chmod(CFG_FILE, 0o600)
        except OSError:
            pass
        _cache, _cache_sig = None, None


def _cfg() -> dict:
    return _load()


def enabled() -> bool:
    return bool(_load().get("enabled"))


def allow_user_choice() -> bool:
    return bool(_load().get("allow_user_choice", True))


def auto_assign() -> bool:
    return bool(_load().get("auto_assign", True))


def save_config(patch: dict) -> dict:
    """管理员热改开关（只接受白名单字段）。"""
    d = _load()
    for k in _CONFIG_KEYS:
        if k in patch and patch[k] is not None:
            v = patch[k]
            if k in ("enabled", "auto_assign", "allow_user_choice", "sticky"):
                d[k] = bool(v)
            elif k in ("max_pick_per_proxy", "punish_secs", "cooldown_secs"):
                # cooldown_secs 也走这条：挂在白名单里但没进这个分支的话，
                # 管理员在界面上改「休息时长」会**静默不生效**（值被丢掉），
                # 而界面还回显成功 —— 这种静默丢参数最容易骗过验收。
                try:
                    d[k] = max(0, int(v))
                except (TypeError, ValueError):
                    raise PoolError("%s 要是整数" % k)
            elif k == "probe_timeout":
                try:
                    d[k] = min(60.0, max(1.0, float(v)))
                except (TypeError, ValueError):
                    raise PoolError("超时要是个数字")
    d["updated"] = int(time.time())
    _save(d)
    return _load()


# ── 池子读写 ──

def _new_id(url: str) -> str:
    return "px" + hashlib.sha256(url.encode()).hexdigest()[:10]


def list_items() -> list[dict]:
    return [dict(x) for x in (_load().get("items") or []) if isinstance(x, dict)]


def get_item(pid: str) -> dict | None:
    for it in _load().get("items") or []:
        if str(it.get("id")) == str(pid):
            return dict(it)
    return None


def add_urls(urls: list[str], label: str = "", source: str = "import", by: str = "") -> dict:
    """加一批（已规范化的）URL，自动去重。返回 {added:[item], dup:[url]}。"""
    d = _load()
    items = d.get("items") or []
    have = {str(x.get("url")) for x in items if isinstance(x, dict)}
    added, dup = [], []
    now = int(time.time())
    for u in urls:
        u = str(u or "").strip()
        if not u:
            continue
        if u in have:
            dup.append(u)
            continue
        it = {
            "id": _new_id(u), "url": u, "label": str(label or "").strip()[:40],
            "enabled": True, "source": source, "added_at": now, "added_by": str(by or "")[:32],
            "check": {}, "stat": {"uses": 0, "ok": 0, "fail": 0, "last_used": 0, "last_ok": 0, "last_err": ""},
        }
        items.append(it)
        have.add(u)
        added.append(it)
    if added:
        d["items"] = items
        d["updated"] = now
        _save(d)
    return {"added": added, "dup": dup}


def import_text(text: str, default_scheme: str = "auto", label: str = "", by: str = "") -> dict:
    """管理员贴文本导入。返回 {added, dup, bad, total}。"""
    p = parse_text(text, default_scheme)
    r = add_urls(p["ok"], label=label, source="import", by=by)
    return {"added": len(r["added"]), "added_ids": [x["id"] for x in r["added"]],
            "dup": len(p["dup"]) + len(r["dup"]), "dup_lines": (p["dup"] + r["dup"])[:50],
            "bad": p["bad"][:50], "bad_count": len(p["bad"]), "total": len(list_items())}


def update_item(pid: str, patch: dict) -> dict:
    """改别名 / 启停 / 改地址。

    ★ 改地址**保留原 id**：id 是机位绑定的外键，改一下地址就把绑定打断、
    得让所有机位重新分配，是纯粹的坑（管理员最常见的一步就是「地址写错了，改一下」）。
    代价是 id 不再等于 `_new_id(url)` —— 没关系，id 对上层本来就不透明。
    """
    d = _load()
    for it in d.get("items") or []:
        if str(it.get("id")) != str(pid):
            continue
        if "url" in patch and patch["url"]:
            new_u = norm_url(patch["url"])
            for other in d.get("items") or []:
                if str(other.get("id")) != str(pid) and str(other.get("url")) == new_u:
                    raise PoolError("池里已经有这个出口了")
            if new_u != str(it.get("url")):
                it["url"] = new_u
                it["check"] = {}      # 换了地址，旧的体检结果不再有效
        if "label" in patch:
            it["label"] = str(patch["label"] or "").strip()[:40]
        if "enabled" in patch:
            it["enabled"] = bool(patch["enabled"])
        d["updated"] = int(time.time())
        _save(d)
        return dict(it)
    raise PoolError("池中没有这个出口")


def delete_item(pid: str) -> dict:
    """删出口。**顺手解绑**所有绑到它的机位（否则机位会指向一个不存在的出口）。"""
    d = _load()
    before = len(d.get("items") or [])
    d["items"] = [x for x in (d.get("items") or []) if str(x.get("id")) != str(pid)]
    if len(d["items"]) == before:
        raise PoolError("池中没有这个出口")
    d["updated"] = int(time.time())
    _save(d)
    n = unbind_proxy(pid)
    return {"ok": True, "unbound": n}


def _set_item(pid: str, **fields) -> None:
    """内部：改某个出口的字段（不动 updated 之外的元信息）。"""
    d = _load()
    for it in d.get("items") or []:
        if str(it.get("id")) == str(pid):
            it.update(fields)
    d["updated"] = int(time.time())
    _save(d)


def unbind_proxy(pid: str) -> int:
    """把绑在该出口上的机位全部解绑（mode 归位 auto，等下次自动分配）。"""
    n = 0
    for a in store.list_accounts() or []:
        if str(a.get("proxy_id") or "") == str(pid):
            store.update_account(a["id"], proxy_id="", proxy_url="", proxy_mode="",
                                 proxy_note="出口已从池中移除，下次跑图自动重新分配",
                                 proxy_at=int(time.time()))
            _drop_netcfg_session(str(a.get("user_uuid") or ""))
            n += 1
    if n:
        _bump()
    return n


# ── 失败黑名单（进程内，与 netcfg._BAD 同构；TTL 到期自动放回） ──
_BAD: dict[str, float] = {}


def _bad_now(url: str) -> bool:
    if not url:
        return False
    exp = _BAD.get(url)
    if exp is None:
        return False
    if exp < time.time():
        _BAD.pop(url, None)
        return False
    return True


def note_failure(url: str) -> None:
    """netcfg 的回调：某个出口在真实请求里挂了 → 拉黑一小段时间，下次分配跳过它。"""
    if not url:
        return
    # ★ 不能用 `or 300`：punish_secs=0 是**合法值**（管理员明确要关掉失败惩罚），
    #   但 0 是 falsy，`or` 会把它悄悄变成 300 —— 界面上写「关掉」实际还在拉黑。
    #   判空只能用「取不到值」（None）。
    raw = _load().get("punish_secs")
    try:
        ttl = float(300 if raw is None else raw)
    except (TypeError, ValueError):
        ttl = 300.0
    if ttl <= 0:                                  # 明确关掉 → 不拉黑
        _BAD.pop(str(url), None)
        return
    _BAD[str(url)] = time.time() + max(30.0, ttl)


def note_success(url: str) -> None:
    if url:
        _BAD.pop(str(url), None)


def bad_count() -> int:
    return len([u for u in list(_BAD) if _bad_now(u)])


# ── 体检（测活） ──
# 三个回显端点依次试：ip-api 走**明文 HTTP**（能穿过只支持绝对 URI 的 HTTP 代理，
# 且免费送国家/城市/ISP），ipify/ifconfig 作为兜底。全部通过**该代理本身**请求，
# 所以拿到的 IP 就是这个出口的真实出口 IP。
_PROBES = (
    # ★★ 顺序必须是 (url, kind)，不是 (kind, url) —— 2026-09-25 线上实测踩到：
    #    tuple 写反了，于是 probe() 拿 "ipapi" 当 URL 去请求 → MissingSchema，
    #    三个探针**全部**在第一个就抛 → 每个出口都记成「不可用」。
    #    97 条出口齐刷刷 0% 存活、0 个出口 IP，就是这个原因（不是代理全死）。
    #
    # ★ 2026-09-25 第二次修正（加了付费货源之后）：把**网易域名**放在最前面。
    #    第三方回显接口（ip-api/ipify/ifconfig）有一类出口就是过不去 ——
    #    实测 113.47.x:3130 那一批打 mkey.163.com 是 414ms 的 200，
    #    但三个回显端点**全部 ReadTimeout**，于是 10 条好出口被整批记成「死」。
    #    判据本来就该是「能不能到网易」而不是「能不能查 IP」，
    #    把网易放第一位后，这类出口会被正确判活（拿到任何 HTTP 状态码即算通）。
    ("https://service.mkey.163.com/", "netease"),
    ("http://ip-api.com/json/?fields=status,message,query,country,regionName,city,isp&lang=zh-CN", "ipapi"),
    ("https://api.ipify.org?format=json", "ipify"),
    ("https://ifconfig.me/ip", "text"),
)


def _ep_host(ep: str) -> str:
    try:
        return urlparse(ep).hostname or ep
    except Exception:
        return ep


def probe(url: str, timeout: float = PROBE_TIMEOUT, geo: bool = True) -> dict:
    """用这个代理试一次「能不能到网易」。返回 {ok, ms, ip, geo, err}。

    判活口径（按重要性排序，逐条都是线上踩出来的）：
      1) **网易域名**能拿到任何 HTTP 状态码 → 算活。这是真正的判据：qfly 要打的
         就是网易，能查 IP 但过不了网易的出口（免费公开代理那 97 条）对我们毫无价值。
      2) 回显端点能拿到任何 HTTP 状态码 → 也算活（典型：socks5 被 GB2312 拦截页
         挡成 403，但打网易是 200）。这种情况 ok=True、ip 留空，不判死。
      3) 全都没响应（超时/连接被拒）→ 才判死。
    """
    s = requests.Session()
    s.proxies.update({"http": url, "https": url})
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    errs: list[str] = []
    reached: list[int] = []          # 拿到了 HTTP 状态码 = 代理转发成功
    netease_ms = 0
    t0 = time.time()
    for ep, kind in _PROBES:
        t_ep = time.time()
        try:
            r = s.get(ep, timeout=timeout, allow_redirects=False)
            reached.append(int(r.status_code))
            if kind == "netease":
                # ★ 网易只要**有响应**就算这个出口能用（200/301/403 都算通；
                #   目标是「出口能否到达网易」而不是「网易是否欢迎我们」）。
                #   拿到状态码就已经证明代理把它转发过去了 → 直接判活。
                netease_ms = int((time.time() - t_ep) * 1000)
                return {"ok": True, "ms": netease_ms, "ip": "", "geo": "",
                        "err": "网易可达 HTTP %d" % r.status_code}
            if r.status_code != 200:
                errs.append("%s HTTP %d" % (_ep_host(ep), r.status_code))
                continue
            ip = ""
            g = ""
            if kind == "ipapi":
                try:
                    d = r.json() or {}
                except Exception:                                # noqa: BLE001
                    d = {}
                if str(d.get("status") or "") != "success":
                    errs.append("ip-api: %s" % str(d.get("message") or "失败")[:60])
                    continue
                ip = str(d.get("query") or "")
                if geo:
                    g = " ".join([str(x) for x in (d.get("country"), d.get("city") or d.get("regionName"),
                                                  d.get("isp")) if x])
            elif kind == "ipify":
                try:
                    ip = str((r.json() or {}).get("ip") or "")
                except Exception:                                # noqa: BLE001
                    ip = (r.text or "").strip()
            else:
                ip = (r.text or "").strip()
            if not ip or len(ip) > 64:
                errs.append("%s 没返回 IP" % _ep_host(ep))
                continue
            return {"ok": True, "ms": int((time.time() - t0) * 1000),
                    "ip": ip, "geo": g[:80], "err": ""}
        except Exception as e:                                   # noqa: BLE001
            errs.append("%s %s" % (_ep_host(ep), str(e)[:70]))
    ms = int((time.time() - t0) * 1000)
    if reached:
        # 代理通、只是回显接口不配合 → 认它活着（拿不到出口 IP，但出口确实能用）
        return {"ok": True, "ms": ms, "ip": "", "geo": "",
                "err": "回显接口返回 %s（代理是通的）" % "/".join(str(x) for x in reached[:3])}
    return {"ok": False, "ms": ms, "ip": "", "geo": "", "err": " | ".join(errs)[:220]}


def test_items(ids: list[str] | None = None, workers: int = 12,
               timeout: float | None = None) -> dict:
    """并发测活。结果一次性写回（96 个出口不能写 96 次文件）。"""
    want = set(str(x) for x in (ids or []))
    items = [it for it in list_items() if (not want or str(it.get("id")) in want)]
    if not items:
        return {"tested": 0, "alive": 0, "dead": 0, "results": []}
    to = float(timeout or _load().get("probe_timeout") or PROBE_TIMEOUT)
    workers = max(1, min(int(workers or 12), 32))
    now = int(time.time())
    results: dict[str, dict] = {}
    with _fut.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, it["url"], to): it for it in items}
        for fu in _fut.as_completed(futs):
            it = futs[fu]
            try:
                r = fu.result()
            except Exception as e:                               # noqa: BLE001
                r = {"ok": False, "ms": 0, "ip": "", "geo": "", "err": str(e)[:160]}
            results[str(it["id"])] = r
    d = _load()
    alive = dead = 0
    for it in d.get("items") or []:
        r = results.get(str(it.get("id")))
        if r is None:
            continue
        it["check"] = {"ok": bool(r["ok"]), "at": now, "ms": int(r.get("ms") or 0),
                       "ip": r.get("ip") or "", "geo": r.get("geo") or "",
                       "err": "" if r["ok"] else (r.get("err") or "")[:200]}
        if r["ok"]:
            alive += 1
            _BAD.pop(str(it.get("url")), None)
        else:
            dead += 1
    d["updated"] = now
    _save(d)
    out = []
    for it in items:
        r = results.get(str(it["id"])) or {}
        out.append({"id": it["id"], "url": display(it["url"]), "label": it.get("label") or "",
                    "ok": bool(r.get("ok")), "ms": int(r.get("ms") or 0),
                    "ip": r.get("ip") or "", "geo": r.get("geo") or "",
                    "err": "" if r.get("ok") else (r.get("err") or "")[:200]})
    out.sort(key=lambda x: (not x["ok"], x["ms"]))
    return {"tested": len(items), "alive": alive, "dead": dead, "results": out}


# ── 可用性与挑选 ──

def _usable(it: dict, exclude: tuple = (), ignore_cooldown: bool = False) -> bool:
    """这个出口现在能不能用。

    ignore_cooldown 是给「**已经在用它的机位**」准备的，见下面 cooldown 的注释。
    """
    if not it or not it.get("enabled", True):
        return False
    u = str(it.get("url") or "")
    if not u or u in (exclude or ()):
        return False
    if _bad_now(u):
        return False
    if not ignore_cooldown and _cooling(u):
        # ★ 冷却的语义是「**不再接新机位**」，不是「这条出口坏了」。
        #   早先这里无条件拦，于是 _resolve_acc（服务**已在用它的**那个机位）
        #   也被拦下：机位跑完一轮、出口刚进冷却，它的下一次请求就判定
        #   「无可用出口」→ **直连**（真实 IP 暴露），或者被自动换到另一个出口
        #   （同一条出口上还挂着别的机位时尤其荒唐）。
        #   所以只有「新分配」（_free_exit / _pick）才看冷却；
        #   已绑定的机位继续用它的出口，冷却只体现在它释放后不再被分给下一个人。
        return False
    ck = it.get("check") or {}
    if ck.get("ok") is False:
        return False
    return True


# ── 出口冷却 + 排队（2026-09-25 用户指定的跑图节流规则）────────────────────
# 规则：
#   · 每个出口最多同时挂 max_pick_per_proxy 个机位（默认 2）
#   · 机位**跑完图归还**出口后，该出口 cooldown_secs（默认 600s）内不再接新机位
#   · 没有可用出口时**纯排队等位**，不直连（用户明确选了这个：宁可等也不暴露真实 IP）
# 为什么用 Condition + 票号而不是 Semaphore：和 tasks.Gate 同款理由 ——
#   Semaphore 初值写死，改上限只能重建，而重建会和正在持位的线程打架。
_COOL: dict[str, float] = {}                 # url → 冷却结束时间戳
_SLOT_CV = threading.Condition()
_SLOT_HOLD: dict[str, str] = {}              # key(机位) → url（当前持位的出口）
_SLOT_SEQ = 0
_SLOT_TICKETS: list[tuple[int, str]] = []    # [(票号, key)]，FIFO
_SLOT_DONE = 0


def _cooling(url: str) -> bool:
    """这个出口是否还在冷却。**故意不加锁**。

    `_cooling()` 被 `_usable()` 在热路径上调用（每次 _free_exit / alive_count
    都会走到）。这里只做一次 dict.get 和一次可能无副作用的 pop：
      · 读：CPython 下 dict.get 是原子的，最坏拿到一个刚过期的值 → 逻辑上
        只影响「这一瞬能不能选它」，不影响正确性；
      · 写：`pop(url, None)` 只在**确认已过期**时才执行，删一个已经该消失的键
        是幂等且无副作用的，与持锁线程并发也不会破坏结构。
    真正需要串行的是「占位/归还」那对操作，它们都在 `_SLOT_CV` 里。
    """
    t = _COOL.get(url)
    if not t:
        return False
    if time.time() >= t:
        _COOL.pop(url, None)                 # 过期即清，避免字典无限涨
        return False                         # 冷却已结束
    return True


def cooldown_left(url: str) -> int:
    """剩余冷却秒数（0 = 没在冷却）。只读，不加锁，理由同 _cooling()。"""
    t = _COOL.get(url)
    if not t:
        return 0
    return max(0, int(t - time.time()))


def _slot_cap() -> int:
    """每个出口同时挂几个机位。

    语义：0 = **不限**（老行为，管理员没设上限）。但出口位机制要的是「最多 N 个」，
    所以 0 在这个函数里被解释成一个足够大的数，而不是 2 —— 否则「不限」会被
    悄悄变成「最多 2」，和管理员在界面上写的配置不一致。
    """
    raw = _load().get("max_pick_per_proxy")
    try:
        n = int(2 if raw is None else raw)
    except (TypeError, ValueError):
        # ★ 配置里被手写成非数字（如 "abc"）时**不能抛**：_free_exit → _slot_cap
        #   在排队等待循环里被调用，一抛就会把队首那张票留在队列里 → 整条队列
        #   永久卡死（见 acquire_slot 的 finally 注释）。这里按默认值兜底并说一声。
        print("[proxypool] max_pick_per_proxy 配置非法（%r），按默认 2 处理" % (raw,), flush=True)
        n = 2
    return n if n > 0 else 10 ** 6


def _slot_cap_display() -> int:
    """给界面看的每出口并发上限：**0 表示不限**。

    内部用 10**6 当「不限」的哨兵值，但直接透出去会让面板显示
    「空闲 999997」「占用 1/1000000」这种没人看得懂的数。对外一律还原成 0。
    """
    return 0 if _slot_cap() >= 10 ** 6 else _slot_cap()


def _cap_text(n: int) -> str:
    """并发上限的人话说法：0/哨兵 → 「不限」。给提示语与日志用。"""
    return "不限" if not n or n >= 10 ** 6 else str(n)


def _slot_used() -> dict[str, int]:
    """每个出口当前挂着几个机位（按 url 计）。

    ★ 必须持锁：`for u in _SLOT_HOLD.values()` 跨多条字节码，跑图线程正在
    acquire/release 时可能抛 `RuntimeError: dictionary changed size during
    iteration`（表现为 /api/admin/proxies 直接 500）。先拍快照再统计。
    """
    with _SLOT_CV:
        snap = list(_SLOT_HOLD.values())
    out: dict[str, int] = {}
    for u in snap:
        out[u] = out.get(u, 0) + 1
    return out


def _free_exit(exclude: tuple = (), prefer_id: str = "") -> dict | None:
    """找**当下就有空位**的出口：未冷却、未黑名单、未测死，且持有数 < 上限。

    ★ 持有数一律按 **url** 对齐（_slot_used 也是按 url 统计的）。曾经这里误用
    it["id"] 去查 used，而 used 的键是 url → 永远查到 0 → 「每出口最多 2 个机位」
    的上限形同虚设（一个出口能被无限塞机位）。单测第 1/2/4/7 组就是抓这个的。
    """
    cap = _slot_cap()
    used = _slot_used()
    cands = [it for it in list_items() if _usable(it, exclude)]
    if prefer_id:
        p = get_item(prefer_id)
        if p and _usable(p, exclude):
            cands.sort(key=lambda it: (0 if str(it.get("id")) == str(prefer_id) else 1,))
    room = [it for it in cands if used.get(str(it.get("url") or ""), 0) < cap]
    if not room:
        return None
    room.sort(key=lambda it: (used.get(str(it.get("url") or ""), 0),
                              cooldown_left(str(it.get("url") or "")),
                              0 if (it.get("check") or {}).get("ok") else 1,
                              _pid_sort(it)))
    return room[0]


def _stopped(stop_flag) -> bool:
    """stop_flag 的取消判据。

    生产调用方（tasks.JobManager）传的是 `threading.Event`，所以这里既能吃 Event
    也能吃普通可调用对象（`lambda: True`）—— 单测和以后别的调用方不会因为
    「传了个函数」而炸在 `'function' object has no attribute 'is_set'` 上。
    这个坑真踩过：测试传 lambda，异常被上层 try/except 吞掉，表现成
    「排队永远等不到」，排查半天。
    """
    if not stop_flag:
        return False
    try:
        if hasattr(stop_flag, "is_set"):
            return bool(stop_flag.is_set())
        return bool(stop_flag())
    except Exception:                                # noqa: BLE001
        return False


def acquire_slot(key: str, exclude: tuple = (), prefer_id: str = "",
                 on_wait=None, stop_flag=None) -> dict:
    """给机位抢一个出口**并发位**。

    返回 {ok, url, proxy_id, waited, queue_ahead}；池子关了 / 没出口时 ok=False。
    纯排队：一直等到有人释放为止。

    stop_flag 可以是 threading.Event，也可以是返回 bool 的可调用对象（见 _stopped）。

    ★ stop_flag=None（签名默认）意味着**永远不会被取消** —— 池子满时这个调用
      会一直等下去，直到进程重启。生产调用方（tasks._acquire_exit_slot）总是传
      job.stop_flag，所以这是给未来调用者的地雷：无界等待请务必传一个可被置位的
      标志；只想「试一下有没有空位」请直接用 _free_exit() 而不是这个函数。
    """
    key = str(key or "")
    if not key:
        return {"ok": False, "url": "", "reason": "无 key"}
    if not enabled():
        return {"ok": False, "url": "", "reason": "池未启用"}
    exclude = tuple(exclude or ())
    global _SLOT_SEQ
    with _SLOT_CV:
        held = _SLOT_HOLD.get(key)
        if held:                                     # 已有位子，直接复用（同一机位并发跑两个任务）
            return {"ok": True, "url": held, "waited": 0, "queue_ahead": 0, "reused": True}
        it = _free_exit(exclude, prefer_id)
        if it is not None:
            _SLOT_HOLD[key] = str(it["url"])
            return {"ok": True, "url": str(it["url"]), "proxy_id": str(it["id"]),
                    "waited": 0, "queue_ahead": 0}
        _SLOT_SEQ += 1
        mine = _SLOT_SEQ
        _SLOT_TICKETS.append((mine, key))
        ahead = [t for t in _SLOT_TICKETS if t[0] < mine].__len__()
    if on_wait:
        try:
            on_wait(ahead)
        except Exception:                            # noqa: BLE001
            pass
    t0 = time.time()
    got: dict | None = None
    try:
        while not _stopped(stop_flag):
            with _SLOT_CV:
                if _SLOT_HOLD.get(key):
                    got = {"ok": True, "url": _SLOT_HOLD[key],
                           "waited": int(time.time() - t0), "queue_ahead": 0}
                    break
                first = _SLOT_TICKETS[0][0] if _SLOT_TICKETS else 0
                if first == mine:
                    it = _free_exit(exclude, prefer_id)
                    if it is not None:
                        _SLOT_HOLD[key] = str(it["url"])
                        got = {"ok": True, "url": str(it["url"]), "proxy_id": str(it["id"]),
                               "waited": int(time.time() - t0), "queue_ahead": 0}
                        break
                _SLOT_CV.wait(timeout=2.0)
    finally:
        # ★ 无论正常拿到、被取消、还是中途抛异常，都必须把自己的票摘掉。
        #   否则这张票会**永久卡在队首**：后面的人 first != mine 永远等不到，
        #   而新来的请求因为 _SLOT_HOLD 里没自己、走上面的快路径又能直接抢位 ——
        #   表现就是「池子里有空闲出口，但排队的人永远不动」，只能重启进程。
        #   触发条件很现实：_free_exit → _slot_cap() → int(配置值)，
        #   配置里被写成非数字就抛 ValueError。
        with _SLOT_CV:
            _SLOT_TICKETS[:] = [t for t in _SLOT_TICKETS if t[0] != mine]
            _SLOT_CV.notify_all()
    if got is not None:
        return got
    return {"ok": False, "url": "", "reason": "排队已取消", "waited": int(time.time() - t0)}


def release_slot(key: str) -> dict:
    """机位跑完图归还出口 → 该出口进入冷却，并唤醒下一个排队者。"""
    key = str(key or "")
    global _SLOT_DONE
    url = ""
    with _SLOT_CV:
        url = _SLOT_HOLD.pop(key, "")
        if url:
            # ★ 必须用「取不到值」判断，不能用 `or`：cooldown_secs=0 是**合法值**
            #   （管理员明确要关掉这个节流），但 0 是 falsy，`or COOLDOWN_SECS`
            #   会把它悄悄变成 600 —— 「关掉冷却」变成「还是冷却 10 分钟」。
            raw = _load().get("cooldown_secs")
            secs = int(COOLDOWN_SECS if raw is None else raw)
            if secs > 0:
                _COOL[url] = time.time() + secs
            else:
                _COOL.pop(url, None)          # 关掉节流 → 不留冷却痕迹
            _SLOT_DONE += 1
        _SLOT_CV.notify_all()
    return {"url": url, "cooldown": cooldown_left(url) if url else 0}


def slot_state() -> dict:
    # ★ 先在锁内只取需要串行的快照；alive_count() 会走 _usable→_cooling（**写** _COOL），
    #   不该在持 _SLOT_CV 时做这种重活（锁序上没死锁，但白占锁、放大竞争）。
    with _SLOT_CV:
        running = len(_SLOT_HOLD)
        waiting = len(_SLOT_TICKETS)
        done = _SLOT_DONE
        rings = list(_SLOT_TICKETS[:10])
        holding = dict(_SLOT_HOLD)
        cool_snap = list(_COOL)
    cap = _slot_cap()
    return {"running": running, "waiting": waiting,
            "done": done, "cap": cap,
            "cap_display": _slot_cap_display(),
            "capacity": cap * max(1, alive_count()),
            "cooling": len([u for u in cool_snap if cooldown_left(u) > 0]),
            # ★ rings 是 [(票号, key)] 明细，**不要**当作「轮数」显示。
            #   界面上曾经把它插值成「已过 [(1,'k2')] 轮」。计数请用 queue_rings。
            "rings": rings,
            "queue_rings": waiting,
            "holding": holding}


# ── 运行实况：把「哪些 IP / 哪些机位」摊开 ──────────────────────────────
# 原来面板上只有计数（在用 3 / 排队 1 / 冷却 2），管理员看不出**具体是谁**占着
# 哪个出口，出问题（某个出口反复失败、某个机位一直排不上队）时无法定位。
# 这一段专门产出可读的明细；只读快照，不加锁做重活。

def _acc_index() -> dict[str, dict]:
    """机位索引，**同时按 id 和 user_uuid 建键**。

    ★ 两种键都要有：tasks.py 的跑图任务是按 `user_uuid` 抢位的
    （`key = str(acc.get("user_uuid") or acc.get("id") or "")`），而管理接口
    拿到的是记录 id。只按 id 建索引的话，跑图中的机位在「运行实况」里
    会退化成一条光秃秃的 uuid —— 别名、渠道、归属邮箱全是空的。

    ★ 也不要用 `store.get_account()` 在循环里逐条查：它每次都 stat +
    反序列化整个 accounts.json，实测 **200 次 ≈ 88ms**。`list_accounts()`
    自带 mtime 缓存，一次读全表再建索引是 0.0ms 量级（queue_view 因此从
    100ms 降到 1ms，而面板每 8 秒就要轮询一次）。
    """
    try:
        recs = store.list_accounts() or []
    except Exception:                                # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for a in recs:
        rid = str(a.get("id") or "")
        uid = str(a.get("user_uuid") or "")
        if rid:
            out[rid] = a
        if uid and uid not in out:
            out[uid] = a
    return out


def _acc_brief(acc_id: str, index: dict | None = None) -> dict:
    """机位的可读标识。

    注意 store 的 alias 字段**总是有值**（没起名时填 user_uuid 前 8 位），所以
    不能靠「alias 为空」判断有没有起名。这里额外带回 channel 与 uuid 前 8 位，
    让面板能显示「甲号 · netease」这种一眼能认出来的组合；alias 恰好等于
    uuid 前 8 位时说明用户没起名，面板可以据此淡化显示。
    """
    a = (index or _acc_index()).get(str(acc_id))
    if not a:
        return {"acc_id": acc_id, "alias": "", "owner_id": "", "channel": "",
                "uuid8": str(acc_id)[:8], "named": False}
    uid = str(a.get("user_uuid") or "")
    u8 = uid[:8]
    al = str(a.get("alias") or "").strip()
    return {"acc_id": str(a.get("id") or acc_id),
            "alias": al or u8 or str(acc_id)[:8],
            # ★ 字段名是 **owner**，不是 owner_id。
            #   store._acc_fields 落的是 "owner"（站点用户 id），
            #   main._own() 也是按 acc.get("owner") 做归属校验的。
            #   写成 owner_id 会恒取到空串 → 面板上的 owner_email 整列空白，
            #   而这事不会报错，只会静默地什么都不显示。
            "owner_id": str(a.get("owner") or ""),
            "channel": str(a.get("channel") or a.get("channel_id") or ""),
            "uuid8": u8,
            "named": bool(al) and al != u8}


def _key_brief(key: str) -> dict:
    """slot key 可能是机位 uuid，也可能是 prelogin:<owner>:<device>。

    prelogin 那几个字面对管理员毫无意义，翻译成「登录预分配」并带上尾段。
    """
    k = str(key or "")
    if k.startswith("prelogin:"):
        parts = k.split(":", 2)
        return {"kind": "prelogin", "acc_id": "", "alias": "登录预分配",
                "owner_id": (parts[1] if len(parts) > 1 else ""),
                "device": (parts[2] if len(parts) > 2 else ""), "channel": ""}
    b = _acc_brief(k)
    b["kind"] = "acc"
    return b


def _url_by_id(pid: str) -> str:
    it = get_item(pid)
    return str((it or {}).get("url") or "")


def queue_view() -> dict:
    """并发位 / 排队 / 冷却 三块的**逐条明细**，供管理面板直接渲染。

    性能：面板每 8 秒轮询一次，必须避免 O(行数 × 池大小) 的重复扫描。
    出口按 url 建一次索引；机位详情按 acc_id 缓存（同一台机位可能同时出现在
    并发位与排队里，重复查 store 没意义）。100 出口 / 200 占位下实测
    从 ~100ms 降到 ~10ms 量级。
    """
    cap = _slot_cap()
    with _SLOT_CV:
        hold = dict(_SLOT_HOLD)
        tickets = list(_SLOT_TICKETS)
        done = _SLOT_DONE
        cool_raw = dict(_COOL)
    now = time.time()

    # ── 一次建索引 ──
    by_url = {str(it.get("url") or ""): it for it in list_items()}
    idx = _acc_index()
    briefs: dict[str, dict] = {}

    def kbrief(key: str) -> dict:
        k = str(key or "")
        if k.startswith("prelogin:"):
            return _key_brief(k)
        b = briefs.get(k)
        if b is None:
            b = {**_acc_brief(k, idx), "kind": "acc"}
            briefs[k] = b
        return b

    # 1) 并发位：按出口聚合「这台出口上挂了哪些机位」
    slots: dict[str, list] = {}
    for k, u in hold.items():
        slots.setdefault(str(u), []).append(kbrief(k))
    running = []
    for u, users in slots.items():
        p = by_url.get(u)
        running.append({"url": mask_url(u), "display": display(u),
                        "proxy_id": str((p or {}).get("id") or ""),
                        "scheme": str((p or {}).get("scheme") or _scheme_of(u)),
                        "label": str((p or {}).get("label") or ""),
                        "used": len(users), "cap": cap, "users": users})
    # 2) 排队：票号顺序就是真实顺序（Gate 用票号 FIFO，这里同样按 list 顺序）
    waiting = []
    for n, (tick, k) in enumerate(tickets):
        b = dict(kbrief(k))
        b.update({"ticket": tick, "ahead": n})
        waiting.append(b)

    # 3) 冷却：哪些出口在休息、还剩多久
    cooling = []
    for u, until in cool_raw.items():
        left = int(until - now)
        if left <= 0:
            continue
        p = by_url.get(u)
        cooling.append({"url": mask_url(u), "display": display(u),
                        "proxy_id": str((p or {}).get("id") or ""),
                        "scheme": str((p or {}).get("scheme") or _scheme_of(u)),
                        "label": str((p or {}).get("label") or ""),
                        "left": left, "until": int(until)})
    cooling.sort(key=lambda x: x["left"])

    return {"cap": cap, "cap_display": _slot_cap_display(),
            "capacity": cap * max(1, alive_count()),
            "running": running, "waiting": waiting, "cooling": cooling,
            "slots_running": len(hold), "queue_len": len(tickets),
            "cooling_now": len(cooling), "done": done,
            "cooldown_secs": _cooldown_secs(),
            "cooling_min_left": (min((c["left"] for c in cooling), default=0))}


def _scheme_of(url: str) -> str:
    return (str(url).split("://", 1)[0] or "http").lower()


def _item_by_url(url: str) -> dict | None:
    """按 url 反查池内条目。

    ★ 这是 O(n) 线性扫描。**不要在循环里调它** —— queue_view 早先每条 running
    都调一次，100 出口 × 200 占位就退化到 100ms 量级（面板每 8 秒轮询一次）。
    循环场景请自己先建一次 `{url: item}` 索引。
    """
    u = str(url or "")
    if not u:
        return None
    for it in list_items():
        if str(it.get("url") or "") == u:
            return it
    return None


def _cooldown_secs() -> int:
    raw = _load().get("cooldown_secs")
    return max(0, int(COOLDOWN_SECS if raw is None else raw))


def alive_items() -> list[dict]:
    return [it for it in list_items() if _usable(it)]


def alive_count() -> int:
    return len(alive_items())


def _pid_sort(it: dict) -> str:
    return hashlib.sha256(str(it.get("id")).encode()).hexdigest()


def _pick(exclude: tuple = (), prefer_id: str = "") -> dict | None:
    """挑一个可用出口：先看负载（挂了几个机位），负载相同优先「测过且活」的。"""
    exclude = tuple(exclude or ())
    if prefer_id:
        p = get_item(prefer_id)
        if p and _usable(p, exclude):
            return p
    items = [it for it in list_items() if _usable(it, exclude)]
    if not items:
        return None
    load = _load_map()
    cap = int(_load().get("max_pick_per_proxy") or 0)
    if cap > 0:
        room = [it for it in items if load.get(str(it["id"]), 0) < cap]
        if room:
            items = room
    items.sort(key=lambda it: (load.get(str(it["id"]), 0),
                               0 if (it.get("check") or {}).get("ok") else 1,
                               _pid_sort(it)))
    return items[0]


def _load_map() -> dict[str, int]:
    """每个出口当前挂了几台机位（现算，不落档——落档就会和 accounts.json 不同步）。"""
    out: dict[str, int] = {}
    for a in store.list_accounts() or []:
        if str(a.get("proxy_mode") or "") in ("auto", "manual") and a.get("proxy_id"):
            k = str(a["proxy_id"])
            out[k] = out.get(k, 0) + 1
    return out


# ── 机位绑定 ──

# user_uuid → 账号记录 的索引。netcfg 的解析回调在**每次构造出站 Session**时都会调，
# 而 store.list_accounts() 要给每个账号做一次浅拷贝（线上 200+ 机位）→ 加个 2s 的
# 短缓存顶住热路径；任何写操作都会主动失效，所以不会拿到过期绑定。
_uuid_cache: tuple[float, dict] = (0.0, {})


def _uuid_index() -> dict:
    global _uuid_cache
    now = time.time()
    if now - _uuid_cache[0] < 2.0:
        return _uuid_cache[1]
    idx = {}
    for a in store.list_accounts() or []:
        uu = str(a.get("user_uuid") or "")
        if uu:
            idx[uu] = a
    _uuid_cache = (now, idx)
    return idx


def _bump() -> None:
    """任何改动绑定的写操作后清索引（并顺手丢掉 netcfg 缓存的长连 Session）。"""
    global _uuid_cache
    _uuid_cache = (0.0, {})


def _drop_netcfg_session(uuid_: str) -> None:
    try:
        netcfg.drop_session(str(uuid_ or ""))
    except Exception:
        pass


def proxy_bound(key: str) -> bool:
    """★ netcfg 的快查回调：这个 key 有没有显式绑定（用于 shared_session 早退）。"""
    if not enabled():
        return False
    return str(key or "") in _uuid_index()


def _acc_by_uuid(uuid_: str) -> dict | None:
    uu = str(uuid_ or "").strip()
    if not uu:
        return None
    return _uuid_index().get(uu)


def _bind(acc_id: str, it: dict | None, mode: str, note: str = "") -> dict:
    now = int(time.time())
    acc = store.get_account(acc_id) or {}
    if it is None:
        store.update_account(acc_id, proxy_mode="", proxy_id="", proxy_url="",
                             proxy_note=note, proxy_at=now)
        _bump()
        _drop_netcfg_session(str(acc.get("user_uuid") or ""))
        return {}
    store.update_account(acc_id, proxy_mode=mode, proxy_id=str(it["id"]),
                         proxy_url=str(it["url"]), proxy_note=note, proxy_at=now)
    _bump()
    _drop_netcfg_session(str(acc.get("user_uuid") or ""))
    return {"mode": mode, "proxy_id": str(it["id"]), "url": str(it["url"])}


def resolve_for_key(key: str, exclude: tuple = ()) -> str:
    """★ netcfg 的出口解析回调：账号 user_uuid → 这个机位绑定的出口 URL。

    三种情况：
      · 没绑定 / mode=off / 总开关关着 → 返回 ""（netcfg 走它原来的全局池或直连）；
      · 绑定还活着 → 返回它（**粘性**，这就是「后续都用这个跑图」）；
      · 绑定死了 / 被 exclude（刚在这条请求上失败）→ **自动换一个可用出口**并落档，
        换不到就退回 "" 并在机位备注里留痕（不静默假装有代理）。
    """
    if not enabled():
        return ""
    uu = str(key or "").strip()
    if not uu:
        return ""
    acc = _acc_by_uuid(uu)
    if not acc:
        return ""
    return _resolve_acc(acc, exclude=exclude)


# ── 登录前的 9393 裸 TCP 出口（prelogin key） ──
#
# 为什么需要单独一条解析链路：
#   9393 是整个登录流程的**第一步**（拿 user/device/key），发生在机位落档**之前**
#   —— 那一刻 accept/accounts 里根本还没有 user_uuid，`resolve_for_key` 必然返回 ""。
#   所以裸 TCP 这一跳只能按「登录发起人 + 目标设备」来认身份：
#       prelogin:<owner_uid>:<device_id>
#   owner_uid 是当前登录用户（已有），device_id 是前端选中的设备档案（已有）；
#   两者在 9393 跑之前就都在手上，且同一用户同一设备每次登录都相同 —— 天然粘性。
#
# 这条链路**只吃 socks5**：HTTP 代理带不了裸 TCP。若该 prelogin key 已绑定的是
# http 出口，返回 ""（退回直连），但**不再另挑一个 socks5** —— 否则登录跳和后续
# 跑图跳会落在两个不同出口上，「同账号同出口」这条最重要的性质就断了。
# 登录完成后 `_finish_login` → `apply_login_pref` 会把机位正式绑到一个 socks5 上。

_PRELOGIN_PREFIX = "prelogin:"


def prelogin_key(owner: str, device_id: str = "") -> str:
    """登录前 9393 用的身份键。两个入参都缺 → 空串（调用方退回直连）。"""
    o = str(owner or "").strip()
    d = str(device_id or "").strip()
    if not o:
        return ""
    return "%s%s:%s" % (_PRELOGIN_PREFIX, o, d)


def _is_prelogin(key: str) -> bool:
    return str(key or "").startswith(_PRELOGIN_PREFIX)


def _prelogin_env(key: str) -> tuple[str, str]:
    """prelogin key → (owner_uid, device_id)。"""
    raw = str(key or "")[len(_PRELOGIN_PREFIX):]
    owner, _, dev = raw.partition(":")
    return owner, dev


def prelogin_exit(key: str) -> str:
    """★ 9393 裸 TCP 的出口（**只返回 socks5**，拿不到就返回 "" 让调用方直连）。"""
    if not enabled():
        return ""
    k = str(key or "").strip()
    if not _is_prelogin(k):
        return ""
    owner, dev = _prelogin_env(k)
    if not owner:
        return ""
    try:
        # 登录前没有 user_uuid，只能按「该用户在该设备上最近一次登录过的机位」来找
        # 它已经绑好的 socks5 —— 这样二次登录（续期/重登）能复用同一出口。
        acc = _latest_acc_for(owner, dev)
        if acc:
            u = _resolve_acc(acc)
            if u and u.lower().startswith("socks"):
                return u
        # 没有既有绑定 → 从池里挑一个**可用的 socks5**，并把它记在这条 prelogin 上，
        # 登录完成时 `_resolve_acc` 若换出口会顺带把机位绑到同一个 IP 上。
        it = _pick_socks()
        return str(it["url"]) if it else ""
    except Exception as e:                           # noqa: BLE001
        # ★ 不能悄悄 return ""：那表示这次 9393 登录**直连**（真实 IP 暴露）。
        #   而 stats() 的 tcp_ready 是另外算的，面板仍会显示「有可用 socks5」——
        #   管理员看不到任何异常。这里至少留一条日志。
        print("[proxypool] prelogin_exit 失败（本次 9393 登录将直连）：%r" % (e,), flush=True)
        return ""


def _latest_acc_for(owner: str, device_id: str) -> dict | None:
    """该用户在该设备上最近登录过的机位（用它已绑的出口，保持跨次登录粘性）。"""
    try:
        best = None
        best_at = -1
        for a in store.list_accounts(owner=owner) or []:
            if device_id and str(a.get("device_id") or "") != str(device_id):
                continue
            if str(a.get("proxy_mode") or "") not in ("auto", "manual"):
                continue
            at = int(a.get("proxy_at") or a.get("updated") or 0)
            if at >= best_at:
                best, best_at = a, at
        return best
    except Exception:
        return None


def _pick_socks(exclude: tuple = ()) -> dict | None:
    """挑一个可用且未冷却的 **socks5** 出口（裸 TCP 只能走它）。"""
    cands = [i for i in list_items()
             if _usable(i, exclude) and str(i.get("url") or "").lower().startswith("socks")]
    if not cands:
        return None
    used = _slot_used()
    cands.sort(key=lambda i: (used.get(str(i.get("url") or ""), 0),
                              cooldown_left(str(i.get("url") or "")),
                              0 if (i.get("check") or {}).get("ok") else 1,
                              hashlib.sha256(str(i.get("id") or "").encode()).hexdigest()))
    return cands[0]


# ── 登录请求上下文（thread-local） ──
#
# 为什么用 thread-local 而不是给 12 个函数加参数：
#   cnlogin 里调用 9393 的地方（`_finish_to_session` / `login_with_email` /
#   `login_bili_*` / `_vivo_to_sky` / `_u9_to_sky` / `_m4399_to_sky` /
#   `login_huawei_with_hello` / `login_honor_with_mint` / `renew`）+ honorqr/hwqr/
#   myapp 的扫码链路，签名各不相同且层层透传；挨个加参数必然漏掉几条（实测漏
#   QR 链路是这类改动的经典翻车点）。
#   uvicorn 单 worker + 端点全同步 `def` ⇒ 一个请求自始至终在同一个线程上跑完，
#   thread-local 在这个模型下是准确的。`JobManager` 的跑图线程是另一批线程，
#   它们走 `tasks._acquire_exit_slot` 传真实 user_uuid，与本上下文无关。
_TLS = threading.local()


def set_request_ctx(owner: str = "", device_id: str = "") -> None:
    """请求入口调用：记下「谁在登、登哪台设备」，供 9393 那一跳解析 socks5 出口。"""
    _TLS.owner = str(owner or "")
    _TLS.device_id = str(device_id or "")


def clear_request_ctx() -> None:
    """请求结束时**必须**调用，否则身份会串到下一个请求上。

    ★ 为什么必须清（这是个真踩到的坑）：FastAPI 的同步 `def` 端点跑在
    anyio 的**线程池**里，线程是复用的。`_TLS` 只按线程维度存，所以
    「上一个请求留下的 owner/device_id」会被下一个请求读到 —— 如果下一个
    请求恰好在 huawei/honor 这类**没有调 set_request_ctx** 的登录链路上，
    它的 9393 一跳就会用上**别人的** prelogin 键，进而 `_resolve_acc` 可能
    去改别人的机位记录。表现是「某个用户的登录走了另一个用户的出口」。
    所以调用点放在中间件的 finally 里（覆盖所有路径，含异常与 401），
    而不是散在每个端点里 —— 散着写迟早漏掉新加的端点。
    """
    _TLS.owner = ""
    _TLS.device_id = ""


def request_ctx_key() -> str:
    """当前请求的 prelogin 键；没设过上下文/没有 owner → 空串（调用方直连）。"""
    try:
        return prelogin_key(getattr(_TLS, "owner", ""), getattr(_TLS, "device_id", ""))
    except Exception:
        return ""


def _resolve_acc(acc: dict, exclude: tuple = ()) -> str:
    mode = str(acc.get("proxy_mode") or "")
    pid = str(acc.get("proxy_id") or "")
    if mode not in ("auto", "manual") or not pid:
        return ""
    it = get_item(pid)
    url = str((it or {}).get("url") or acc.get("proxy_url") or "")
    # ★ ignore_cooldown=True：这台机位**已经绑着**这个出口，冷却只该阻止它被
    #   分给新人，不该让在用的机位被判「无可用出口」而退回直连。
    if it and _usable(it, exclude, ignore_cooldown=True):
        if _bad_now(url):
            note_success(url)
        return url
    if not bool(_load().get("sticky", True)):
        new = _pick(exclude=exclude)
        if new:
            _bind(acc["id"], new, "auto", note="非粘性模式：按请求轮换")
            return str(new["url"])
    # 绑定不可用 → 自动换（用户明确要求：如不能用将自动分配可用代理）
    new = _pick(exclude=tuple(exclude) + ((url,) if url else ()))
    if new is None:
        if acc.get("proxy_note") != "池中暂无可用出口，本次直连（真实 IP 可能暴露）":
            store.update_account(acc["id"], proxy_note="池中暂无可用出口，本次直连（真实 IP 可能暴露）",
                                 proxy_at=int(time.time()))
            print("[proxypool] 机位 %s 无可用出口 → 本次直连" % acc.get("id"), flush=True)
        return ""
    # ★ note 会原样下发到用户面的机位卡片，所以**绝不能带出口地址**。
    #   2026-09-26 实测抓到的泄露：这里原来是 "…已自动换到 %s" % display(url)，
    #   于是自动换出口过的机位，卡片上又出现了 ip:port（用户报「机位右上角还是暴露IP」）。
    #   需要完整地址的是运维，用下面那行 print 进服务端日志 —— 日志给运维，note 给用户。
    reason = "原出口不可用，已自动换到 %s" % _user_code(new)
    if str(new["id"]) != pid:
        print("[proxypool] 机位 %s 自动换出口 %s → %s" % (acc.get("id"), display(url), display(str(new["url"]))), flush=True)
    _bind(acc["id"], new, mode if mode == "manual" else "auto", note=reason)
    return str(new["url"])


def set_account_proxy(acc_id: str, mode: str, proxy_id: str = "", owner: str = "") -> dict:
    """用户/管理员显式设置某个机位的出口。

    mode: off（不用代理）| auto（自动分配一个可用出口）| manual（指定出口）
    """
    acc = store.get_account(acc_id)
    if not acc:
        raise PoolError("机位不存在")
    mode = str(mode or "off").strip().lower()
    if mode not in ("off", "auto", "manual"):
        raise PoolError("mode 只能是 off / auto / manual")
    if mode == "off":
        _bind(acc_id, None, "", note="")
        return {"ok": True, "mode": "off"}
    if mode == "auto":
        if not auto_assign():
            raise PoolError("管理员已关闭「自动分配出口」")
        it = _pick()
        if it is None:
            _bind(acc_id, None, "", note="池中暂无可用出口，本次直连（真实 IP 可能暴露）")
            raise PoolError("出口池里没有可用代理，请联系管理员（或先不勾选代理）")
        _bind(acc_id, it, "auto", note="自动分配")
        return {"ok": True, "mode": "auto", "proxy_id": str(it["id"]), "display": display(str(it["url"]))}
    if not allow_user_choice() and not owner:
        raise PoolError("管理员已关闭「自选出口」")
    it = get_item(proxy_id)
    if it is None:
        raise PoolError("指定的出口不在池中")
    if not it.get("enabled", True):
        raise PoolError("指定的出口已被管理员停用")
    _bind(acc_id, it, "manual", note="手动指定")
    return {"ok": True, "mode": "manual", "proxy_id": str(it["id"]), "display": display(str(it["url"]))}


def apply_login_pref(acc_id: str, owner: str) -> None:
    """登录成功后按**用户级偏好**给这个机位绑出口。

    ★ 这里刻意做成「一个漏斗」：所有登录链路（手机号/邮箱/B站/vivo/4399/九游/华为/
      荣耀/应用宝/扫码…）最终都汇到 `main._finish_login`，所以在那里统一生效，
      不必在 20 多个调用点各写一遍。扫码链路的 `onDone` 也是走同一个漏斗。
    """
    try:
        p = user_pref(owner)
        if not p.get("use"):
            return
        acc = store.get_account(acc_id)
        if not acc:
            return
        if str(acc.get("proxy_mode") or "") in ("auto", "manual") and acc.get("proxy_id"):
            # 已有绑定 → 保持粘性，只在它死了的时候自愈（顺带把备注刷新）
            _resolve_acc(acc)
            return
        want = str(p.get("proxy_id") or "")
        if want and allow_user_choice():
            set_account_proxy(acc_id, "manual", want)
        elif auto_assign():
            set_account_proxy(acc_id, "auto")
    except Exception as e:                                       # noqa: BLE001
        # 登录已经成功了，代理分配失败**绝不能**把登录结果吞掉
        print("[proxypool] 登录后绑定出口失败（忽略）：%s" % str(e)[:160], flush=True)


# ── 用户级偏好 ──

def user_pref(uid: str) -> dict:
    p = (_load().get("user_prefs") or {}).get(str(uid or ""))
    if not isinstance(p, dict):
        return {"use": False, "proxy_id": "", "at": 0}
    return {"use": bool(p.get("use")), "proxy_id": str(p.get("proxy_id") or ""),
            "at": int(p.get("at") or 0)}


def set_user_pref(uid: str, use: bool, proxy_id: str = "") -> dict:
    d = _load()
    prefs = d.setdefault("user_prefs", {})
    prefs[str(uid)] = {"use": bool(use), "proxy_id": str(proxy_id or ""), "at": int(time.time())}
    d["updated"] = int(time.time())
    _save(d)
    return user_pref(uid)


# ── 视图 ──

# ── 机位徽标 + 出口后台补测（2026-09-26 用户要求）────────────────────────────
# 用户原话：「机位右上角还是会暴露IP 把那个改成IP代理已启用 连的通绿色显示 联不通红色显示」
#
# 两个设计取舍，都写在代码里免得以后被"优化"回去：
#
# ① 机位卡片**永不显示出口 IP**。要区分是哪个出口就给匿名代号 `#0231`（由出口 id 派生，
#    稳定可复述、反推不出 IP）。管理员要看真实 IP 走「出口代理池」面板（/api/admin/proxies
#    与 admin_proxies_accounts，那两处显式传 admin=True 保留 display/ip/geo）——
#    机位卡片是要截图、录屏、给人看的表面，出口 IP 是资产，不该出现在那里。
#
# ② 绿/红不能只看「有没有绑定」，要看**这个出口现在是否真到得了网易**，综合三件事：
#    出口是否被停用、是否在失败惩罚期(_bad_now)、最后一次体检结果。
#    体检结果缓存 ACC_CHECK_TTL 秒，过期就丢后台线程补测 —— 列表接口不阻塞，
#    前端 Console.jsx 每 60s 拉一次 /api/accounts，徽标会在下一轮自己变绿/变红。
ACC_CHECK_TTL = 120.0
_ACC_CHECK_LOCK = threading.Lock()
_ACC_CHECK_GUARD: dict[str, float] = {}      # url → 上次发起补测的时间

# 备注里任何 `ip:port` / `ip` 都要打掉 —— 备注是"数据"，不能成为漏 IP 的暗道。
# （生产实测确实漏过：自动换出口时把新出口地址写进了 proxy_note。）
_RE_ADDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?")


def _scrub_note(note: str) -> str:
    """把用户可见备注里的出口地址抹掉。防御性：即使将来又有谁把地址写进 note，也不外泄。"""
    return _RE_ADDR.sub("该出口", str(note or ""))


def _exit_ok(it: dict | None, url: str) -> bool:
    """这个出口现在到底通不通（徽标绿/红的唯一判据）。"""
    if not it or not url or not it.get("enabled", True):
        return False
    if _bad_now(url):
        # 真实请求刚失败过 → 必定红。这比体检更新鲜，优先采信。
        return False
    ck = it.get("check") or {}
    if ck.get("ok") is False:
        return False
    return True


def _stale_exits(owner: str = "") -> list[str]:
    """已绑定、体检过期、且当前没人在测的出口 id。纯内存，不发网络请求。"""
    now = time.time()
    out: list[str] = []
    seen: set[str] = set()
    for a in (store.list_accounts(owner=owner or None) or []):
        if str(a.get("proxy_mode") or "") not in ("auto", "manual"):
            continue
        pid = str(a.get("proxy_id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        it = get_item(pid)
        if not it or not it.get("enabled", True):
            continue
        url = str(it.get("url") or "")
        if not url or _bad_now(url):
            continue
        ck = it.get("check") or {}
        if ck.get("ok") is True and (now - float(ck.get("at") or 0)) < ACC_CHECK_TTL:
            continue                                   # 刚测过且通 → 不重复测
        with _ACC_CHECK_LOCK:
            if now - _ACC_CHECK_GUARD.get(url, 0.0) < ACC_CHECK_TTL:
                continue                               # 刚发起过 → 别打爆
            _ACC_CHECK_GUARD[url] = now
        out.append(pid)
    return out


def kick_bound_checks(owner: str = "") -> int:
    """把「体检过期」的已绑定出口丢后台线程补测，返回本轮条数。★绝不阻塞调用方。

    /api/accounts 是列表接口，几十张卡片同步各测一次会把接口拖到几秒；
    这里只做内存判断，网络请求在 daemon 线程里跑。

    ★ 绝不把空 ids 传给 test_items —— `test_items([])` 的语义是「测**全池**」，
      那会把一次列表刷新变成对 40 个出口的全量探测。
    """
    # Local interactive mode must not turn a list refresh into background network activity.
    # Manual proxy tests remain explicit user actions; maintenance is separately opt-in.
    if os.environ.get("QFLY_LOCAL_NETWORK_MODE") and os.environ.get("QFLY_SCHEDULER_ENABLED") != "1":
        return 0
    try:
        ids = _stale_exits(owner)
    except Exception as e:                                     # noqa: BLE001
        print("[proxypool] 机位出口补测筛选失败（忽略）：%s" % str(e)[:140], flush=True)
        return 0
    if not ids:
        return 0

    def _work(want: list[str]) -> None:
        try:
            r = test_items(want)
            print("[proxypool] 机位出口后台补测 %d 条：可用 %d / 不可用 %d"
                  % (int(r.get("tested") or 0), int(r.get("alive") or 0), int(r.get("dead") or 0)),
                  flush=True)
        except Exception as e:                                 # noqa: BLE001
            print("[proxypool] 机位出口后台补测失败：%s" % str(e)[:140], flush=True)

    threading.Thread(target=_work, args=(ids,), name="px-acc-check", daemon=True).start()
    return len(ids)


def account_proxy(acc: dict, admin: bool = False) -> dict:
    """给前端渲染的机位代理视图。

    ★ 默认（admin=False，所有**用户面**路径）**不下发出口 IP**：只给匿名代号 `code`
      和「已启用 / 通不通」状态，前端徽标显示「IP代理已启用」绿 / 「IP代理连接失败」红。
    ★ admin=True 才带 display/ip/geo —— 只有 /api/admin/proxies/accounts 那个
      「同一出口是不是挂了多个人」的排查表需要，那里没有 IP 就没法排查。
    """
    mode = str(acc.get("proxy_mode") or "")
    pid = str(acc.get("proxy_id") or "")
    it = get_item(pid) if pid else None
    ck = (it or {}).get("check") or {}
    url = str((it or {}).get("url") or acc.get("proxy_url") or "")
    on = bool(mode in ("auto", "manual") and it and url)
    ok = _exit_ok(it, url) if on else False
    v = {
        "mode": mode if on else "",
        "proxy_id": pid if it else "",
        "label": str((it or {}).get("label") or ""),
        # 匿名代号取代原来的 display(ip:port)
        "code": _user_code(it) if it else "",
        "on": on,
        "ok": ok,
        # 前端只认 state/text，别让三处各拼一套文案
        "state": ("on" if ok else "bad") if on else "off",
        "text": "IP代理已启用" if (on and ok) else ("IP代理连接失败" if on else "未启用代理"),
        "checked_at": int(ck.get("at") or 0),
        # ★ 用户面备注必须过一遍地址擦除（防御性，见 _scrub_note）
        "note": _scrub_note(acc.get("proxy_note")),
        "at": int(acc.get("proxy_at") or 0),
        "enabled": enabled(),
        "available": alive_count(),
        # UDP（进图 enet 中继）走本机网卡，HTTP/SOCKS5 覆盖不到 —— 界面必须说实话
        "udp_ok": False,
    }
    if admin:
        v["display"] = display(url)
        v["ip"] = str(ck.get("ip") or "")
        v["geo"] = str(ck.get("geo") or "")
        # 管理员看原样备注（排查用）；用户面是擦除过的
        v["note"] = str(acc.get("proxy_note") or "")
    return v


def item_view(it: dict, usage: list | None = None) -> dict:
    ck = it.get("check") or {}
    st = it.get("stat") or {}
    u = str(it.get("url") or "")
    sch = (u.split("://", 1)[0] or "http").lower()
    cool = cooldown_left(u)
    return {
        "id": str(it.get("id") or ""),
        "url": mask_url(u),
        "host": display(u),
        "scheme": sch,
        # 面板要按协议分组并给出用途说明，所以把分类和提示一起下发，
        # 别让前端自己硬编码 —— 将来加 socks4 之类只改这里。
        "scheme_zh": SCHEME_ZH.get(sch, sch),
        "use_hint": SCHEME_HINT.get(sch, ""),
        # 严格冷却：剩余秒数 > 0 时该出口不接新机位，面板要显示倒计时
        "cooldown_left": cool,
        "cooling": cool > 0,
        "slots_used": _slot_used().get(u, 0),
        "slots_cap": _slot_cap(),
        "label": str(it.get("label") or ""),
        "enabled": bool(it.get("enabled", True)),
        "source": str(it.get("source") or ""),
        "added_at": int(it.get("added_at") or 0),
        "check": {
            "tested": bool(ck.get("at")),
            "ok": bool(ck.get("ok")),
            "at": int(ck.get("at") or 0),
            "ms": int(ck.get("ms") or 0),
            "ip": str(ck.get("ip") or ""),
            "geo": str(ck.get("geo") or ""),
            "err": str(ck.get("err") or ""),
        },
        "stat": {
            "uses": int(st.get("uses") or 0),
            "ok": int(st.get("ok") or 0),
            "fail": int(st.get("fail") or 0),
            "last_used": int(st.get("last_used") or 0),
            "last_ok": int(st.get("last_ok") or 0),
        },
        "bad": _bad_now(u),
        "usage": usage if usage is not None else [],
        "usage_count": len(usage or []),
    }


def bindings() -> dict[str, list]:
    """出口 → 挂在它上面的机位（现算，用来回答「谁在用这个 IP」）。"""
    out: dict[str, list] = {}
    for a in store.list_accounts() or []:
        mode = str(a.get("proxy_mode") or "")
        pid = str(a.get("proxy_id") or "")
        if mode in ("auto", "manual") and pid:
            out.setdefault(pid, []).append({
                "acc_id": str(a.get("id") or ""),
                "alias": str(a.get("alias") or ""),
                "owner_id": str(a.get("owner") or ""),
                "channel": str(a.get("channel") or ""),
                "mode": mode,
                "note": str(a.get("proxy_note") or ""),
                "at": int(a.get("proxy_at") or 0),
            })
    return out


def stats() -> dict:
    items = list_items()
    use = bindings()
    alive = dead = untested = 0
    for it in items:
        ck = it.get("check") or {}
        if not ck.get("at"):
            untested += 1
        elif ck.get("ok"):
            alive += 1
        else:
            dead += 1
    sl = slot_state()
    with _SLOT_CV:                       # ★ 先快照，避免与 acquire/release 竞争
        cool_snap = list(_COOL)
    cooling = [u for u in cool_snap if cooldown_left(u) > 0]
    return {
        "enabled": enabled(),
        "auto_assign": auto_assign(),
        "allow_user_choice": allow_user_choice(),
        "sticky": bool(_load().get("sticky", True)),
        "max_pick_per_proxy": int(_load().get("max_pick_per_proxy") or 0),
        "punish_secs": int(_load().get("punish_secs") or 0),
        "probe_timeout": float(_load().get("probe_timeout") or PROBE_TIMEOUT),
        # ★ 不能写 `or 0` 之外的兜底逻辑，也要用统一的 _cooldown_secs()：
        #   0 是合法值（= 关掉冷却），而它 falsy，任何 `or COOLDOWN_SECS` 都会把
        #   「关掉冷却」悄悄变回「休息 600 秒」。这个坑在 release_slot 里踩过一次。
        "cooldown_secs": _cooldown_secs(),
        "total": len(items),
        "on": sum(1 for x in items if x.get("enabled", True)),
        "alive": alive,
        "dead": dead,
        "untested": untested,
        "bad_now": bad_count(),
        "usable": alive_count(),
        "bound_accounts": sum(len(v) for v in use.values()),
        "distinct_exit": len(use),
        "updated": int(_load().get("updated") or 0),
        # ── 并发位 / 排队 / 冷却（跑图节流）──
        # capacity = 出口数 × 每出口并发上限。用户要的「每个 IP 最多 2 个账号」就是它。
        "slots_cap": sl["cap"],
        # ★ 对外把「不限」还原成 0：内部哨兵 10**6 直接透出去，面板会显示
        #   「空闲 999997」「占用 1/1000000」这种没人看得懂的数。
        "slots_cap_display": sl["cap_display"],
        "slots_capacity": sl["capacity"],
        "slots_running": sl["running"],
        "slots_free": max(0, sl["capacity"] - sl["running"]),
        "queue_len": sl["waiting"],
        # queue_rings = **排队人数**（标量）。历史上这里是 sl["rings"]，那是一串
        # (票号, key) 明细，前端却当轮数插值 → 界面显示「已过 [(1, 'k2')] 轮」。
        "queue_rings": sl["queue_rings"],
        "queue_rings_detail": sl["rings"],
        "cooling_now": len(cooling),
        # 用 cooling 列表算，不要复算 cooldown_left —— 上面已经过滤掉过期项了，
        # 这里再算一遍会拿到同一批数，但表达上容易让人以为有两个来源。
        "cooling_min_left": min([cooldown_left(u) for u in cooling], default=0),
        # 按协议分组：socks5 支持 9393 裸连登录，http 只走 REST —— 面板上要分开显示
        "by_scheme": _by_scheme(items),
        # ── 9393（登录第一跳）覆盖情况 ──
        # tcp_ready = 池里**可用**的 socks5 出口数。为 0 时登录那一跳会退回直连
        # （真实 IP 暴露），面板必须把这件事说出来，不能只显示「池子有 20 条」。
        "tcp_ready": sum(1 for it in items
                         if _usable(it) and str(it.get("url") or "").lower().startswith("socks")),
        "tcp_covered": bool(_load().get("enabled")) and any(
            _usable(it) and str(it.get("url") or "").lower().startswith("socks") for it in items),
    }


def _by_scheme(items: list[dict]) -> dict:
    """按协议分组统计（面板分组展示 + 用途提示的数据源）。"""
    # ★ bound 必须按 **url → 挂了几台机位** 来数。之前没传 usage 进来，这个字段恒为 0，
    #   面板会显示「socks5 0 个机位在用」而实际绑着 —— 属于显示口径错，不是数据错。
    use = bindings()
    by_url: dict[str, int] = {}
    for pid, rows in use.items():
        it = get_item(pid)
        if it:
            by_url[str(it.get("url") or "")] = len(rows)
    out: dict[str, dict] = {}
    for it in items:
        sch = (str(it.get("url") or "").split("://", 1)[0] or "http").lower()
        g = out.setdefault(sch, {"total": 0, "alive": 0, "cooling": 0, "bound": 0, "ports": []})
        g["total"] += 1
        ck = it.get("check") or {}
        if ck.get("ok"):
            g["alive"] += 1
        if cooldown_left(str(it.get("url") or "")) > 0:
            g["cooling"] += 1
        g["bound"] += by_url.get(str(it.get("url") or ""), 0)
        try:
            p = int(str(it.get("url") or "").rsplit(":", 1)[-1].split("/")[0])
            if p not in g["ports"]:
                g["ports"].append(p)
        except Exception:                            # noqa: BLE001
            pass
    for sch, g in out.items():
        g["ports"] = sorted(g["ports"])
        g["zh"] = SCHEME_ZH.get(sch, sch)
        g["use_hint"] = SCHEME_HINT.get(sch, "")
        g["udp_ok"] = False
    return out


def admin_view() -> dict:
    """管理员接口：整池 + 每个出口挂的机位（含脱敏后的 URL）。"""
    use = bindings()
    items = [item_view(it, use.get(str(it.get("id")), [])) for it in list_items()]
    items.sort(key=lambda x: (not x["enabled"], not x["check"]["ok"], x["check"]["ms"], x["host"]))
    return {"config": stats(), "items": items}


def _user_code(it: dict) -> str:
    """给用户起的**匿名代号**：稳定、可读、不可反推真实 IP。

    为什么需要它：用户要能在登录弹窗里「挑一个出口」，就必须有个能区分、能复述的
    标识；但把真实 `ip:port` 下发出去等于把整个出口池的资产清单交给每一个已登录
    用户（用户明确报过这个问题）。所以用户面一律用代号，真实 IP 只在管理面板出现。

    代号由出口 id 哈希派生 → 同一出口永远同一个号，管理员和用户对得上话；
    而哈希不可逆，拿不到 IP。
    """
    pid = str(it.get("id") or "")
    h = int(hashlib.sha256(pid.encode("utf-8")).hexdigest()[:8], 16)
    return "#%04d" % (h % 10000)


def public_options() -> dict:
    """登录弹窗/机位面板用：**不下发任何真实 IP 与账密**。

    ⚠️ 2026-09-26 修（用户报「登录完账号把我所有代理 IP 暴露了」）：这里原来把
    `display(u)`（= 真实 `ip:port`）逐条下发，`count` 还是**全部存活出口**，
    于是任何已登录用户 `GET /api/proxy/options` 就能拿到整池资产清单，
    且 ProxyModal 会把它们全部渲染出来。现在改成：
      · `host` 字段**只给代号**（`_user_code`，如 `#0231`）；
      · 真实 `ip:port` 仅当这条出口**就是该用户自己机位已绑的那条**时才下发
        （`mine_id`），因为他本来就该知道自己在用哪个出口；
      · 其余出口只暴露可比较的抽象信息（地区/延迟/负载/协议），足以做选择。
    管理面板走的是另一条路径（`/api/admin/proxies`），那里保留完整 IP。
    """
    if not enabled():
        return {"enabled": False, "count": 0, "items": [], "allow_choice": False,
                "auto_assign": False, "hint": "管理员未开启 IP 代理出口"}
    load = _load_map()
    used = _slot_used()
    cap = _slot_cap_display()
    items = []
    for it in alive_items():
        ck = it.get("check") or {}
        u = str(it["url"])
        sch = (u.split("://", 1)[0] or "http").lower()
        items.append({
            "id": str(it["id"]),
            # ★ 用户面标识：匿名代号，**不是**真实 IP
            "code": _user_code(it),
            "label": str(it.get("label") or ""),
            "scheme": sch,
            "scheme_zh": SCHEME_ZH.get(sch, sch),
            "use_hint": SCHEME_HINT.get(sch, ""),
            "geo": str(ck.get("geo") or ""),
            "ms": int(ck.get("ms") or 0),
            "load": load.get(str(it["id"]), 0),
            "slots_used": used.get(u, 0),
            "slots_cap": cap,
        })
    items.sort(key=lambda x: (x["slots_used"], x["ms"]))
    sl = slot_state()
    return {"enabled": True, "count": len(items), "items": items,
            "allow_choice": allow_user_choice(), "auto_assign": auto_assign(),
            # 并发位情况：登录弹窗要显示「现在还有几个位」以及需不需要排队
            "slots_capacity": sl["capacity"], "slots_free": max(0, sl["capacity"] - sl["running"]),
            "queue_len": sl["waiting"], "cooldown_secs": _cooldown_secs(),
            "by_scheme": _by_scheme(list_items()),
            "hint": "勾选后这个机位会固定走一个出口 IP，跑图/领奖都用它；出口挂了会自动换一个可用的。"
                    "每个出口最多同时服务 %s 个机位，跑完一次要休息 %d 分钟才会再被分配。"
                    % (_cap_text(cap), _cooldown_secs() // 60)}
