# -*- coding: utf-8 -*-
"""任务管理：跑图/刷新等后台任务，内存日志 + 状态轮询。单账号同时一个任务。"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
import uuid

from . import activity
from . import bans
from . import cnlogin
from . import daily
from . import eden
from . import eventtoken
from . import gameinfo
from . import jingling
from . import newacc
from . import outfit
from . import pickup
from . import seasonhome
from . import session as cnsession
from . import stats
from . import wingless
from .skyapi import SkyApiError
from .store import get_account, replace_session, set_status, update_account

# ── 任务日志落盘 ──────────────────────────────────────────────────────────────
# _jobs 原来是纯内存，进程一重启任务连同日志全没——而「重启」每次后端部署都会发生。
# 落盘后任务与日志跨刷新、跨重启都在，到期由调度线程清理。
# 目录与 store.py 的 data/ 同级：StaticFiles 只挂了 static/，data/ 不在 web 根下。
JOBS_DIR = os.path.join(os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")), "jobs")
RETENTION_SECS = 6 * 3600      # 任务结束满 6 小时连日志一起删
PRUNE_EVERY_SECS = 3600        # 清理频率：每小时扫一次（频率 ≠ 保留期，扫勤只会更及时）
FLUSH_EVERY_SECS = 2.0         # 运行中最多攒 2 秒落一次盘，不每条都写
LOG_RING_MAX = 2000            # 内存里最多留多少条（与 to_json 回给前端的 400 条窗口无关）
_META_LOCK = threading.Lock()  # 多个任务线程同时写各自的 tmp 文件，replace 前串一下

DATA_DIR = os.path.dirname(JOBS_DIR)  # .../data

# ── 并发闸门（分层）───────────────────────────────────────────────────────────
# 原来所有任务共用一个 Semaphore(2)。但长任务和短任务差三个数量级：
#   pickup 中位 453s、newacc 中位 784s   vs   renew 中位 1.3s、refresh 中位 1.1s
# 共用一个闸门 → 2 秒的续期要排在 7.5 分钟的跑图后面。真实台账实测：111 个短任务里
# 有 23 个（21%）与长任务抢位。分成两个独立池后互不阻塞。
POOL_LONG = "long"                 # 主长池：跑图 / 造号（上限随出口容量走）
POOL_LONG_DIRECT = "long_direct"   # 直连子池：**不走代理出口**的长任务
POOL_SHORT = "short"
# kind → 池：不在这里的一律走短池（renew / refresh / daily / tokens / race / wings / eden）
_LONG_KINDS = ("pickup", "newacc")
# 上限的默认值 / 合法区间。
#   long = **0 是特殊值**：自动跟随「出口容量」（每出口上限 max_pick_per_proxy ×
#          存活出口数）。出口池关了，容量就等于直连上限。正数 = 管理员手设的死上限。
#   long_direct = 2：不走代理出口的长任务（直连）只有一个服务器 IP 可用，并发拉高
#          就是「同一 IDC IP 同时挂着几十路游戏会话」的机群签名（见知识库 56 第三节），
#          所以守住历史上那个保守值 2。用户明确要求：「走代理每 IP 两个账号、总共 80 个
#          跑图位；不走代理只有 2 个跑图位」。
POOL_DEFAULT = {POOL_LONG: 0, POOL_LONG_DIRECT: 2, POOL_SHORT: 16}
POOL_MIN = {POOL_LONG: 0, POOL_LONG_DIRECT: 1, POOL_SHORT: 1}
POOL_MAX = {POOL_LONG: 512, POOL_LONG_DIRECT: 64, POOL_SHORT: 256}   # 手滑写 1000 时的护栏
POOL_ZH = {POOL_LONG: "长任务", POOL_LONG_DIRECT: "直连长任务", POOL_SHORT: "短任务"}

# 热改配置：改 data/concurrency.json 即生效，不用重启。带 mtime 缓存，
# 因为每开一个任务都会读一次。
CONC_FILE = os.path.join(DATA_DIR, "concurrency.json")
_conc_cache: dict = {"mtime": -1, "val": None}


def pool_of(kind: str) -> str:
    return POOL_LONG if kind in _LONG_KINDS else POOL_SHORT


def pool_limits() -> dict:
    """读 data/concurrency.json（mtime 缓存）。读不到 / 写坏了 → 回落默认值，绝不抛。"""
    try:
        mtime = os.stat(CONC_FILE).st_mtime_ns
    except OSError:
        return dict(POOL_DEFAULT)
    if mtime != _conc_cache["mtime"]:
        val = dict(POOL_DEFAULT)
        try:
            with open(CONC_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("配置不是对象：%r" % (raw,))
            # ★ 旧格式迁移：老文件的 {"long": 6} 里那个 6 是「长任务总并发数」，
            #   新版 long 是「0 = 自动跟随出口容量 / 正数 = 手设死上限」。直接沿用
            #   会把旧的 6 当成管理员手设的上限 → 跑图位永远停在 6，本次修复形同
            #   没有生效（线上就是这样）。所以**没有 long_direct 键的文件一律按旧格式
            #   处理，丢弃那个 long**，让新默认（自动）接管。第一次从面板保存后，
            #   文件里就有 long_direct 了，此后 long 一律按新语义解释。
            legacy = POOL_LONG_DIRECT not in raw
            for k, dflt in POOL_DEFAULT.items():
                if legacy and k == POOL_LONG:
                    continue
                v = raw.get(k, dflt)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError("%s 不是数字：%r" % (k, v))
                val[k] = max(POOL_MIN[k], min(int(v), POOL_MAX[k]))
        except Exception as e:
            print("[jobs] 并发配置不可用，回落默认 %r：%r" % (POOL_DEFAULT, e), flush=True)
            val = dict(POOL_DEFAULT)
        _conc_cache["mtime"] = mtime
        _conc_cache["val"] = val
    return dict(_conc_cache["val"] or POOL_DEFAULT)


def save_pool_limits(d: dict) -> dict:
    """原子写并发配置（0600）。只认已知池名，非法值抛 ValueError。

    `long` 允许 0（= 自动跟随出口容量）；其余池下限为 1。
    """
    cur = pool_limits()
    for k, v in (d or {}).items():
        if k not in POOL_DEFAULT:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("%s 必须是数字" % k)
        try:
            iv = int(v)
        except (OverflowError, ValueError):
            # JSON 里的 1e999 会被 Python 解析成 inf、NaN 有自己的字面量，
            # int() 对它们分别抛 OverflowError / ValueError；端点只把 ValueError
            # 翻成 400，所以这里统一成 ValueError —— 否则管理员一个手写的请求
            # 就能换回 500 和一整段栈。
            raise ValueError("%s 不是有限数：%r" % (k, v))
        cur[k] = max(POOL_MIN[k], min(iv, POOL_MAX[k]))
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONC_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONC_FILE)
    try:
        os.chmod(CONC_FILE, 0o600)
    except OSError:
        pass
    _conc_cache["mtime"] = -1      # 下次读立刻重载
    return cur


def _fmt_secs(s: float) -> str:
    s = int(max(0, s))
    return ("%ds" % s) if s < 60 else ("%d分%d秒" % (s // 60, s % 60))


# ── 出口容量（跑图位的真正上限来源）─────────────────────────────────────────
# 「跑图位」的上限不另拍脑袋，它等于出口池能同时承载多少路会话：
#     出口容量 = 每出口上限 max_pick_per_proxy × 存活出口数
# 线上实测 2 × 40 = 80 —— 正是用户要的「每个 IP 两个账号、总共 80 个跑图位」。
# 这个数要在闸门热路径上反复读（每次 acquire、每次取水位），而算它得遍历整个
# 出口池并逐条判活，所以加 5 秒 TTL：够新（改出口配置 5 秒内生效）又不烧 IO。
_EXIT_CAP_TTL = 5.0
_exit_cap_cache: dict = {"at": 0.0, "val": None, "on": False}


def exit_capacity() -> "tuple[int, bool]":
    """返回 (出口容量, 出口池是否开着)。读不到 → 当作「池关、容量 = 直连上限」。

    ★ 锁序固定为 Gate._cv → proxypool 的 _SLOT_CV：proxypool 侧从不反向来拿
      Gate 的锁，所以本函数即使在闸门持锁时被调用也不会死锁；何况有 TTL 兜底，
      绝大多数调用根本不会真的进到出口池里。
    """
    now = time.time()
    if _exit_cap_cache["val"] is not None and now - _exit_cap_cache["at"] < _EXIT_CAP_TTL:
        return int(_exit_cap_cache["val"]), bool(_exit_cap_cache["on"])
    cap = int(POOL_DEFAULT[POOL_LONG_DIRECT])
    on = False
    try:
        from . import proxypool
        on = bool(proxypool.enabled())
        if on:
            cap = int(proxypool.slot_state().get("capacity") or 0)
    except Exception as e:                       # noqa: BLE001
        # 出口池读不出来绝不能把整个任务系统带下水：按直连上限保守放行。
        print("[jobs] 出口容量读取失败，跑图位按直连上限 %d 处理：%r" % (cap, e), flush=True)
        on = False
    if cap <= 0:
        cap = int(POOL_DEFAULT[POOL_LONG_DIRECT])
    _exit_cap_cache.update({"at": now, "val": cap, "on": on})
    return cap, on


def long_limit() -> int:
    """跑图位上限：配置里写了正数就用手设值，否则实时跟随出口容量。

    出口池关着时 `exit_capacity()` 退回直连上限，于是「不用代理只有 2 个跑图位」
    这条规则自动成立，不需要在这里再写一遍分支。
    """
    ov = pool_limits().get(POOL_LONG, 0)
    try:
        ov = int(ov)
    except (TypeError, ValueError):
        ov = 0
    if ov > 0:
        return ov
    cap, _on = exit_capacity()
    return max(1, cap)


class Gate:
    """一个并发池：FIFO 排队 + 可热改上限 + 等待时长可见。

    用 Condition + 票号而不是 Semaphore：Semaphore 初值写死，改上限只能重建，
    而重建会和正在持位的线程打架。`_running` 计数 + 票号队列可以随时改上限。
    """

    def __init__(self, name: str, default: int, limit_fn=None):
        self.name = name
        self._default = default
        # 动态上限：跑图位要跟着出口容量走，所以它的上限不是一个常数，
        # 而是每次现算（带 TTL 缓存）。None = 老老实实读配置文件。
        self._limit_fn = limit_fn
        self._cv = threading.Condition()
        self._running = 0
        self._tickets: list[int] = []
        self._seq = 0
        self._done = 0
        self._exec: "collections.deque[float]" = collections.deque(maxlen=20)

    def limit(self) -> int:
        if self._limit_fn is not None:
            try:
                return max(POOL_MIN.get(self.name, 1), int(self._limit_fn()))
            except Exception as e:               # noqa: BLE001
                # 算出不来绝不能把闸门卡死：回落静态配置值。
                print("[jobs] %s 上限计算失败，回落配置值：%r" % (self.name, e), flush=True)
        return pool_limits().get(self.name, self._default)

    def est_wait(self, ahead: int) -> float:
        """估算还要等多久：前面 ahead 个 × 单次真实执行耗时 ÷ 并发位。纯展示用。"""
        n = len(self._exec)
        if n == 0 or ahead <= 0:
            return 0.0
        return ahead * (sum(self._exec) / n) / max(1, self.limit())

    def acquire(self, job, log) -> bool:
        """拿到位子返回 True；排队期间任务被 stop 返回 False。"""
        with self._cv:
            if not self._tickets and self._running < self.limit():
                self._running += 1
                job.queued = False
                job.queue_ahead = 0
                return True
            self._seq += 1
            mine = self._seq
            self._tickets.append(mine)
            ahead = self._tickets.index(mine)
            job.queued = True
            job.queue_ahead = ahead
        est = self.est_wait(ahead)
        log("并发已满（%s池 %d 个位全占），排队等待…前面还有 %d 个%s"
            % (POOL_ZH.get(self.name, self.name), self.limit(), ahead,
               ("，预计约 %s" % _fmt_secs(est)) if est else ""))
        waited = 0
        while not job.stop_flag.is_set():
            with self._cv:
                if self._tickets and self._tickets[0] == mine and self._running < self.limit():
                    self._tickets.pop(0)
                    self._running += 1
                    job.queued = False
                    job.queue_ahead = 0
                    self._cv.notify_all()
                    return True
                self._cv.wait(timeout=2.0)
            waited += 2
            if waited % 10 == 0:
                with self._cv:
                    ahead = self._tickets.index(mine) if mine in self._tickets else 0
                    job.queue_ahead = ahead
                est = self.est_wait(ahead)
                log("排队中 %ds：前面还有 %d 个（%s池 %d 个位）%s，可随时停止"
                    % (waited, ahead, POOL_ZH.get(self.name, self.name), self.limit(),
                       ("，预计还需约 %s" % _fmt_secs(est)) if est else ""))
        with self._cv:
            if mine in self._tickets:
                self._tickets.remove(mine)
            job.queued = False
            job.queue_ahead = 0
            self._cv.notify_all()
        return False

    def release(self, exec_secs: float = 0.0) -> None:
        with self._cv:
            if self._running > 0:
                self._running -= 1
            self._done += 1
            if exec_secs and exec_secs > 0:
                self._exec.append(float(exec_secs))
            self._cv.notify_all()

    def stats(self) -> dict:
        with self._cv:
            return {
                "pool": self.name,
                "pool_zh": POOL_ZH.get(self.name, self.name),
                "limit": self.limit(),
                "running": self._running,
                "waiting": len(self._tickets),
                "done": self._done,
                "avg_exec_secs": round(sum(self._exec) / len(self._exec), 1) if self._exec else 0.0,
            }


GATE_LONG = Gate(POOL_LONG, POOL_DEFAULT[POOL_LONG], limit_fn=long_limit)
GATE_LONG_DIRECT = Gate(POOL_LONG_DIRECT, POOL_DEFAULT[POOL_LONG_DIRECT])
GATE_SHORT = Gate(POOL_SHORT, POOL_DEFAULT[POOL_SHORT])


def gate_of(kind: str) -> Gate:
    """主池：长任务（跑图/造号）走长池，其余走短池。

    ★ 直连的长任务还要额外挤一个「直连跑图位」子池，见 direct_sub_gate()。
    """
    return GATE_LONG if kind in _LONG_KINDS else GATE_SHORT


def direct_sub_gate(kind: str, acc: "dict | None") -> "Gate | None":
    """不走代理出口的长任务要额外挤的子池。

    返回 None = 不需要子池（短任务，或这个任务会走代理出口）。
    用户的规则：「走代理每 IP 两个账号、总共 80 个跑图位；不走代理只有 2 个跑图位」——
    主池那 80 个位是给代理出口摊出去的，直连只有一个服务器 IP，必须单独守住 2。
    """
    if kind not in _LONG_KINDS or acc is None:
        return None
    if _slot_eligible_kind(kind, acc):
        return None                              # 会抢出口位 → 只吃主池
    return GATE_LONG_DIRECT


def gates_stats() -> dict:
    cap, on = exit_capacity()
    try:
        auto = not int(pool_limits().get(POOL_LONG, 0) or 0)
    except (TypeError, ValueError):
        auto = True
    # 三个池：主长池（跟随出口容量）/ 直连子池 / 短池。前端顶栏与管理员面板
    # 都按名字取，顺序稳定（主长池永远第一个）。
    return {"pools": [GATE_LONG.stats(), GATE_LONG_DIRECT.stats(), GATE_SHORT.stats()],
            "long_kinds": list(_LONG_KINDS),
            "defaults": dict(POOL_DEFAULT),
            "mins": dict(POOL_MIN),
            "maxs": dict(POOL_MAX),
            "exit_capacity": cap,
            "pool_enabled": on,
            "long_auto": auto}


class Job:
    def __init__(self, kind: str, account_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.account_id = account_id
        self.status = "running"
        self.logs: list[dict] = []
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.summary: dict | None = None
        self.summary_text: str = ""
        self.error: str | None = None
        self.stop_flag = threading.Event()
        # ── 并发池 ──
        self.pool = pool_of(kind)     # "long" / "short"
        self.queued = False           # 正在排队等并发位（前端显示「排队中」）
        self.queue_ahead = 0          # 前面还有几个在等
        # ── 落盘状态 ──
        self._plock = threading.Lock()
        self._pending: list[dict] = []  # 已进内存、还没追加进 jsonl 的日志
        self._last_flush = time.time()

    @property
    def meta_path(self) -> str:
        return os.path.join(JOBS_DIR, "%s.meta.json" % self.id)

    @property
    def jsonl_path(self) -> str:
        return os.path.join(JOBS_DIR, "%s.jsonl" % self.id)

    def log(self, message: str) -> None:
        # t 存绝对时间戳（epoch 秒）：前端按真实时钟渲染。
        # 曾经存「相对 started_at 的秒数」，前端当时间戳渲染成 08:00:00 起每条 +1s 的假时钟。
        line = {"t": round(time.time(), 3), "msg": str(message)}
        with self._plock:
            self.logs.append(line)
            if len(self.logs) > LOG_RING_MAX:
                del self.logs[:500]
            # 先记进待写队列再交给 _maybe_flush 批量落盘。
            # 注意不能靠 self.logs 切片推未写起点：环形缓冲会从头部丢，下标对不上。
            self._pending.append(line)
        self._maybe_flush()

    def _maybe_flush(self, force: bool = False) -> None:
        """把待写日志追加进 jsonl。攒够 FLUSH_EVERY_SECS 或 force 时才真写。"""
        now = time.time()
        with self._plock:
            if not self._pending:
                return
            if not force and now - self._last_flush < FLUSH_EVERY_SECS:
                return
            batch = self._pending
            self._pending = []
            self._last_flush = now
        try:
            os.makedirs(JOBS_DIR, exist_ok=True)
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                for line in batch:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as e:  # 落盘失败不能让任务本身挂掉
            print("[jobs] 日志落盘失败 %s：%r" % (self.id, e), flush=True)

    def save_meta(self) -> None:
        """原子写元数据（状态/摘要/结束时间）。小文件，tmp + os.replace 与 store.py 一致。"""
        meta = self.to_json(with_logs=False)
        tmp = self.meta_path + ".tmp"
        try:
            os.makedirs(JOBS_DIR, exist_ok=True)
            with _META_LOCK:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
                os.replace(tmp, self.meta_path)
        except Exception as e:
            print("[jobs] 元数据落盘失败 %s：%r" % (self.id, e), flush=True)

    def to_json(self, with_logs: bool = True) -> dict:
        out = {
            "id": self.id, "kind": self.kind, "account_id": self.account_id,
            "status": self.status, "started_at": self.started_at, "finished_at": self.finished_at,
            "error": self.error, "summary": self.summary, "summary_text": self.summary_text,
            "log_count": len(self.logs),
            "pool": self.pool, "queued": self.queued, "queue_ahead": self.queue_ahead,
        }
        if with_logs:
            out["logs"] = self.logs[-400:]
        return out


def _summarize(kind: str, result) -> str:
    """把任务结果 dict 压成一行人类可读文本；前端 toast/任务面板只吃字符串。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)
    if kind == "refresh":
        c = result.get("currency") if isinstance(result.get("currency"), dict) else {}
        parts = []
        # 红蜡烛（prestige）与白蜡/爱心/季蜡同属「整货币」：账户卡片一直有这一格，
        # 刷新摘要漏了它，用户就只看到「献祭跑完 126/126」却看不到红蜡有没有多（线上反馈）。
        for key, label in (("candles", "白蜡烛"), ("heart", "爱心"),
                           ("season_candle", "季蜡"), ("season_heart", "季心"),
                           ("prestige", "红蜡烛")):
            if c.get(key) is not None:
                parts.append("%s %s" % (label, c[key]))
        # 红蜡颗粒（prestige_wax，碎光）：只在非 0 时显示 —— 它是「献祭产出的是碎光、
        # 还没换成整根」的唯一可见证据，0 时显示只会把摘要撑长。
        pw = str(c.get("prestige_wax") or "").strip()
        if pw and pw not in ("0", "0.0"):
            parts.append("红蜡颗粒 %s" % pw)
        # 活动代币：event_candle_* 键合并计数（蜡烛键 + _wax 键同代币求和）
        try:
            ev = {}
            for k, v in c.items():
                if isinstance(k, str) and k.startswith("event_candle_") and isinstance(v, (int, float)) and v:
                    base = k[:-4] if k.endswith("_wax") else k
                    ev[base] = ev.get(base, 0) + int(v)
            if ev:
                parts.append("活动代币 %d" % sum(ev.values()))
        except Exception:
            pass
        for key, label in (("friends_count", "好友"), ("collectibles_count", "收藏品"),
                           ("wing_buffs_count", "光翼")):
            if result.get(key) is not None:
                parts.append("%s %s" % (label, result[key]))
        o = result.get("outfit")
        if isinstance(o, dict):
            h = o.get("height") if isinstance(o.get("height"), dict) else {}
            if h.get("current") and h["current"] != "--":
                parts.append("身高 %s" % h["current"])
            if o.get("wingless"):
                parts.append("无翼")
            elif o.get("part_count") is not None:
                parts.append("装扮 %s 件" % o["part_count"])
        return " · ".join(parts) if parts else "数据已更新"
    s = result.get("summary")
    if isinstance(s, str) and s:
        if isinstance(result.get("succ"), int) and isinstance(result.get("total"), int) and result["total"]:
            s += "（成功 %d/%d）" % (result["succ"], result["total"])
        return s
    if result.get("renewed"):
        return "续期成功"
    per = result.get("per_map")
    if isinstance(per, list) and per:
        ok_n = sum(1 for p in per if isinstance(p, dict) and p.get("ok"))
        got = sum(int(p.get("got") or 0) for p in per if isinstance(p, dict))
        skip_n = sum(1 for p in per if isinstance(p, dict) and p.get("skip"))
        txt = "收集 %d 颗 · %d/%d 张图成功" % (got, ok_n, len(per))
        if skip_n:
            txt += "（%d 图已收满自动跳过）" % skip_n
        return txt
    try:
        return json.dumps(result, ensure_ascii=False)[:160]
    except Exception:
        return str(result)[:160]


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._big_lock = threading.Lock()
        self._last_prune = time.time()
        self._load()

    def _unlink(self, job_id: str) -> None:
        """把一个任务的磁盘痕迹全删掉（元数据 / 日志 / 可能残留的 tmp）。"""
        if not job_id:
            return
        for suf in (".meta.json", ".jsonl", ".meta.json.tmp"):
            try:
                os.remove(os.path.join(JOBS_DIR, job_id + suf))
            except FileNotFoundError:
                pass
            except Exception as e:
                print("[jobs] 删除失败 %s%s：%r" % (job_id, suf, e), flush=True)

    def _load(self) -> None:
        """启动时把磁盘上的任务读回内存。

        两条关键规则：
          · 过期（结束满 RETENTION_SECS）的直接删文件，不占内存；
          · 磁盘上还写着 running 的，进程肯定已经重启过，标成「服务重启中断」并落盘 ——
            用户看到的是「任务因重启中断」，而不是面板凭空消失。
        """
        try:
            names = os.listdir(JOBS_DIR)
        except FileNotFoundError:
            return
        except Exception as e:
            print("[jobs] 读取任务目录失败：%r" % e, flush=True)
            return
        now = time.time()
        loaded = expired = interrupted = 0
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            jid = name[: -len(".meta.json")]
            try:
                with open(os.path.join(JOBS_DIR, name), encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                # 半个文件（上次写盘被打断）：删掉，别让它每次启动都卡一下
                print("[jobs] 元数据损坏 %s：%r" % (jid, e), flush=True)
                self._unlink(jid)
                continue
            fin = meta.get("finished_at")
            if fin and now - fin > RETENTION_SECS:
                self._unlink(jid)
                expired += 1
                continue
            job = Job(meta.get("kind") or "?", meta.get("account_id") or "")
            job.id = meta.get("id") or jid
            job.started_at = meta.get("started_at") or now
            job.finished_at = fin
            job.summary = meta.get("summary")
            job.summary_text = meta.get("summary_text") or ""
            job.error = meta.get("error")
            job.status = meta.get("status") or "failed"
            try:
                lines = []
                with open(job.jsonl_path, encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if raw:
                            try:
                                lines.append(json.loads(raw))
                            except Exception:
                                pass  # 单行坏掉不影响其余日志
                job.logs = lines[-LOG_RING_MAX:]
            except FileNotFoundError:
                job.logs = []
            except Exception as e:
                print("[jobs] 日志读取失败 %s：%r" % (jid, e), flush=True)
                job.logs = []
            if job.status == "running":
                job.status = "failed"
                job.error = "服务重启中断"
                job.finished_at = now
                job.log("任务因服务重启中断")
                job._maybe_flush(force=True)
                job.save_meta()
                interrupted += 1
            self._jobs[job.id] = job
            loaded += 1
        if loaded:
            print("[jobs] 已恢复 %d 个任务（过期清理 %d · 重启中断标记 %d）"
                  % (loaded, expired, interrupted), flush=True)

    def start(self, kind: str, account_id: str, fn, payload: dict) -> Job:
        with self._big_lock:
            for j in self._jobs.values():
                if j.status == "running" and j.account_id == account_id:
                    raise RuntimeError("该账号已有任务在跑（%s），请等待完成或先停止" % j.kind)
            job = Job(kind, account_id)
            self._jobs[job.id] = job
        job.save_meta()  # 立刻落一条记录：进程若在任务刚起就挂，也知道跑过什么（磁盘 IO 放在锁外）
        t = threading.Thread(target=self._run, args=(job, fn, payload), name="job-%s" % job.id, daemon=True)
        t.start()
        return job

    def _run(self, job: Job, fn, payload: dict):
        gate = gate_of(job.kind)
        sub = None
        sub_acquired = False
        acquired = False
        t_exec = 0.0
        slot_key = ""                 # ★ 必须在 try 之前初始化：finally 无条件读它
        try:
            # 先把账号取出来 —— 它决定这个任务是**走代理出口**还是**直连**，
            # 而这两条路是不同的跑图位池（用户规则：代理吃出口容量 80，直连只有 2）。
            acc = get_account(job.account_id)
            if acc is None:
                raise RuntimeError("账号不存在")
            # 直连的长任务额外挤「直连跑图位」子池。
            # ★ 顺序固定为 子池 → 主池，且从不反向获取，所以不会死锁；
            #   子池只让直连任务自己等，走代理的任务照常吃主池，谁也不会被饿死。
            sub = direct_sub_gate(job.kind, acc)
            if sub is not None and not sub.acquire(job, job.log):
                job.log("排队中收到停止信号，任务已取消")
                job.summary = {"cancelled": True}
                job.summary_text = "排队中已取消"
                job.status = "done"
                return
            sub_acquired = sub is not None
            # 排队等并发位：期间可被停止取消。分池后短任务不再被 7.5 分钟的跑图堵住。
            # （旧版是共用的 Semaphore(2)：排队时不产日志、且 stop() 唤醒不了无限阻塞。）
            acquired = gate.acquire(job, job.log)
            if not acquired:  # 排队期间点了停止
                job.log("排队中收到停止信号，任务已取消")
                job.summary = {"cancelled": True}
                job.summary_text = "排队中已取消"
                job.status = "done"
                return
            t_exec = time.time()
            # 出口并发位：机位勾了「用代理」时，跑图前抢一个出口位，跑完归还并让出口休息。
            # 挂在 gate 之后（gate 管的是本机跑图并发，这管的是「哪个出口 IP」），
            # 且**纯排队**——没有可用出口就一直等，不直连（用户明确选了这个语义）。
            # 池子关了 / 机位没勾代理 / 跑图外的短任务 → 这里全是 no-op，行为与从前一致。
            try:
                slot_key = _acquire_exit_slot(job, acc)
            except Exception as slot_err:                # noqa: BLE001
                # 抢位本身失败绝不能挡跑图：记一行日志然后按直连继续
                job.log("出口分配异常，本次按直连执行：%s" % str(slot_err)[:120])
                slot_key = ""
            result = fn(job, acc, payload, job.log)
            job.summary = result
            job.summary_text = _summarize(job.kind, result)
            job.status = "done"
            if job.kind == "pickup":
                # 收烛统计（烛光口径）：记当日真实入账烛光（Σcc；already 不重复计）。
                # 颗数 got 只进日志，不入统计（统计口径为本次入账烛光量，见 stats.record_pickup）。
                # per_map[].cts = 本图各货币类型的颗数（原始 ct，含碎片键与活动代币），
                # 一并入统计，供工作台「每个账号跑了多少货币、分类清楚」用。
                try:
                    per = result.get("per_map") if isinstance(result, dict) else None
                    if isinstance(per, list):
                        wax = sum(int(p.get("wax") or 0) for p in per if isinstance(p, dict))
                        cts: dict = {}
                        for p in per:
                            if not isinstance(p, dict):
                                continue
                            got_cts = p.get("cts")
                            if not isinstance(got_cts, dict):
                                continue
                            for ct, n in got_cts.items():
                                key = str(ct or "").strip()
                                if key:
                                    cts[key] = cts.get(key, 0) + int(n or 0)
                    else:
                        wax, cts = 0, {}
                    stats.record_pickup(job.account_id, wax, cts, job.finished_at)
                except Exception as e:
                    print("[stats] 收烛统计记录失败：%r" % e, flush=True)
        except Exception as e:
            job.error = str(e) or e.__class__.__name__
            job.status = "failed"
            job.log("任务失败：%s" % job.error)
            if cnlogin.is_platform_rest_down(e):
                # 0.16.5 平台级故障（REST 业务层整体关闭，418/422/423）：
                # 这不是「账号出事」，别让机位停在旧的红色状态里误导用户去重新登录。
                # 统一在这一层归因，所有任务（跑图/每日/送心/装扮/刷新…）都覆盖到。
                try:
                    cur = get_account(job.account_id)
                    if cur is not None:
                        update_account(cur["id"], status="warn",
                                       status_note=cnlogin.platform_rest_down_note(e))
                except Exception as mark_err:
                    print("[tasks] 平台级失败归因落盘失败：%r" % mark_err, flush=True)
        finally:
            if slot_key:
                # 归还出口：该出口进入严格冷却（默认 10 分钟），并唤醒队首。
                # 放在 gate.release 之前：先让出 IP，再让出本机并发位。
                try:
                    _release_exit_slot(slot_key, job.log)
                except Exception as rel_err:             # noqa: BLE001
                    print("[proxypool] 出口归还失败 %s：%r" % (slot_key, rel_err), flush=True)
            if acquired:
                # 记的是「真实执行耗时」，不含排队 —— 估等待时长要靠它，掺进排队会正反馈放大。
                gate.release(time.time() - t_exec if t_exec else 0.0)
            if sub_acquired:
                # 反向释放：先主池、后子池。子池只做「直连最多 2 路」的门禁，
                # 不喂估时样本（它的限流口径和跑图耗时无关）。
                sub.release(0.0)
            job.finished_at = time.time()
            # 落盘收尾：终态 + 最后一批日志必须写进去。
            # 少了这一步，用户翻回来看「任务完成没有」时拿到的还是 running 的旧记录。
            job._maybe_flush(force=True)
            job.save_meta()

    def stop(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if j and j.status == "running":
            j.stop_flag.set()
            return True
        return False

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def snapshot(self) -> list[Job]:
        """只读快照（按启动时间倒序）：流水线/同跑共时感视图用，不修改任务。"""
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def prune(self) -> int:
        """删掉结束满 RETENTION_SECS 的任务：内存与磁盘一起删。返回删了几个。

        注意这个方法以前**从来没被调用过**（定义了就是死代码），导致 _jobs 只涨不降。
        现在由 tick() 周期触发，tick() 由调度线程调用——见 scheduler._loop。
        """
        now = time.time()
        with self._big_lock:
            expired = [jid for jid, j in self._jobs.items()
                       if j.finished_at and now - j.finished_at > RETENTION_SECS]
            for jid in expired:
                del self._jobs[jid]
        for jid in expired:
            self._unlink(jid)
        return len(expired)

    def tick(self) -> None:
        """调度线程周期调用：刷运行中任务的待写日志 + 到点清理过期任务。"""
        for j in list(self._jobs.values()):
            try:
                j._maybe_flush(force=True)
            except Exception as e:
                print("[jobs] 周期刷盘失败 %s：%r" % (j.id, e), flush=True)
        if time.time() - self._last_prune >= PRUNE_EVERY_SECS:
            self._last_prune = time.time()
            n = self.prune()
            if n:
                print("[jobs] 清理过期任务 %d 个" % n, flush=True)


MANAGER = JobManager()


# ── 具体任务 ──

def make_client(acc: dict) -> "object":
    from .skyapi import SkyClient
    return SkyClient(acc["user_uuid"], acc["session"], acc["session2"], acc.get("data_version") or "")


def account_credentials_expired(err: Exception) -> bool:
    if isinstance(err, SkyApiError) and err.status in (401, 403, 423):
        return True
    s = str(err)
    return "HTTP 401" in s or "HTTP 403" in s or "HTTP 423" in s


def account_session_maybe_stale(err: Exception) -> bool:
    """会话**可能**过期（比 `account_credentials_expired` 宽一档，用于「先续期再判」）。

    ★ 2026-10-04 实测（真机 + 生产日志）：会话过期时不同路径回的码**不一样** ——
        · `/account/*`  → `HTTP 422`（也可能只是 IP 限流）
        · `/service/*` → `HTTP 400 {"status":"Invalid Request"}`
      而 `account_credentials_expired` 只认 401/403/423，于是 `/service/*` 那条
      **永远不触发续期**：好友列表（`get_all_friend_gist`）在 session 过期后
      一直 502，面板上显示成「这个机位还没有好友」——看起来像空数据，其实是
      会话失效被静默吞掉。

    所以 `_sync_renewed` 用本函数做**第一道判据**：命中就先续期、用新会话重试一次。
    续期后仍失败才把原始错误抛出去 —— 这样「400 是会话过期」和「400 是参数真错」
    两种情形都能得到正确结果，不会把真错误吞成成功。

    故意**不含 429**：那是明确的限流信号，该退避重试而不是重取会话
    （skyapi.post 内部已对 429/422/5xx 自动重试）。
    """
    if isinstance(err, SkyApiError) and err.status in (400, 401, 403, 423):
        return True
    s = str(err)
    return any(t in s for t in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 423"))


def _renew_mark(acc, log):
    from xg_auth_client import Client, AuthError
    try:
        renewed = Client().renew(acc["id"])
    except AuthError as exc:
        update_account(acc["id"], status="warn", status_note=exc.code)
        log("账号续期未完成：" + exc.code)
        raise
    replace_session(acc["id"], renewed)
    return renewed


# ── 拉票失败 → 自动续期（跑图 / 活动代币这类「进图」长任务）────────────────────
# 用户 2026-10-09 报：跑图遇到「拉票失败」整图就失败，得用户自己去点「续期」。
# 真根因是**会话被顶**（22 篇 §10）：库里那把 session 被后开的客户端作废，
# 原地重试多少次都没用，只能重登（续期）+ replace_session。
# 钩子由 `session.enter_map` 在「拉票失败」当场调用，续期成功就用同一个 client 重拉票。
_STALE_RENEW_MAX = 2          # 单次任务最多自动续期几次（防「一图一次」的续期风暴）
_STALE_RENEW_COOLDOWN = 20.0  # 两次自动续期的最小间隔（秒）


def _apply_renewed(client, renewed: dict) -> None:
    """把续期结果**就地**套回已有 client。

    跑图是一次几十分钟的长任务，client 被几十个调用点共用（就绪探针 / collect /
    染料 / 钱包核验…），换对象要改一大圈；而 `SkyClient` 每发一个请求都现读
    `self.session` / `self.session2` / `self.uid`（skyapi.py:139-156），
    所以就地改字段与「换一把新会话」等价。
    """
    for k, attr in (("session", "session"), ("session2", "session2"), ("user_uuid", "uid")):
        v = renewed.get(k)
        if v:
            setattr(client, attr, v)


def make_stale_renewer(acc: dict, log, max_n: int = _STALE_RENEW_MAX,
                       cooldown: float = _STALE_RENEW_COOLDOWN):
    """造一个 `on_stale(client)`：拉票失败 → 续期 → 新会话套回 client（成功返回 True）。

    三道刹车（都是为了让「自动续期」不变成新的问题）：
    · **平台级 / 限流不续期**：HTTP 418/422 那天续期入口本身也是 418，白打一次网关
      还会把账号标成「待续期」（与 `with_renew` 同一判据）；
    · **次数与间隔**：单次任务最多 `max_n` 次、两次之间至少 `cooldown` 秒 ——
      否则 51 张图会连着续 51 次；
    · **续期失败不改语义**：只记日志（`_renew_mark` 已把账号标成 warn/error），
      本次进图照旧按「拉票失败」失败，跑图不会因为续期失败整轮崩掉。
    """
    state = {"n": 0, "at": 0.0}

    def on_stale(client) -> bool:
        err = cnsession.last_ticket_error()
        status = getattr(err, "status", None)
        if err is not None and (cnlogin.is_platform_rest_down(err) or status in (418, 422)):
            log("拉票失败（HTTP %s，平台级/出口限流）——续期无用，按进图失败继续"
                % (status if status is not None else "?"))
            return False
        now = time.time()
        if state["n"] >= max_n:
            log("拉票失败：本次任务已自动续期 %d 次，不再重复续期" % state["n"])
            return False
        if state["at"] and (now - state["at"]) < cooldown:
            log("拉票失败：%.0f 秒前刚续过期，等新会话生效（按进图失败继续）" % (now - state["at"]))
            return False
        state["n"] += 1
        state["at"] = now
        log("拉票失败（会话可能已被顶掉，非限流）——自动续期中…")
        try:
            renewed = _renew_mark(acc, log)
        except Exception as e:                                    # noqa: BLE001
            log("自动续期失败：%s（本次按进图失败继续）"
                % (" ".join(str(e).split())[:160] or "续期失败"))
            return False
        _apply_renewed(client, renewed)
        log("续期成功，用新会话重拉票…")
        return True

    return on_stale


# ── 出口并发位（跑图节流）────────────────────────────────────────────────
# 只有「真的会产生目标站流量的任务」才需要抢出口位；纯本地/预览类不抢。
# 抢位是**纯排队**语义：拿不到就一直等（用户明确要求，宁可等也不暴露真实 IP）。
# 池子关了、或该机位没勾代理 → 全部 no-op，与引入本机制之前的行为完全一致。
_SLOT_KINDS = ("pickup", "daily", "seasonhome", "wings", "eden", "tokens",
               "activity", "activitytask", "race", "refresh", "renew")


def _slot_eligible_kind(kind: str, acc: "dict | None") -> bool:
    """这个任务会不会去抢出口位 —— 抢 = 走代理出口，不抢 = **直连**。

    ★ 必须与 `_acquire_exit_slot` 共用同一套判据：两处一旦不一致，就会出现
      「按代理池放行、结果却直连跑」的漏洞 —— 直连那条路就没人守住 2 个位的上限了。
    """
    if acc is None or kind not in _SLOT_KINDS:
        return False
    try:
        from . import proxypool
        if not proxypool.enabled():
            return False
    except Exception:                                    # noqa: BLE001
        return False
    if str(acc.get("proxy_mode") or "") not in ("auto", "manual"):
        return False                                     # 没勾代理 → 直连
    return bool(str(acc.get("user_uuid") or acc.get("id") or ""))


def _acquire_exit_slot(job: Job, acc: dict) -> str:
    """给这个机位抢一个出口一并发位。返回 slot_key（空 = 本次不走代理）。"""
    try:
        from . import proxypool
    except Exception:                                    # noqa: BLE001
        return ""
    if not _slot_eligible_kind(job.kind, acc):
        return ""
    key = str(acc.get("user_uuid") or acc.get("id") or "")

    def on_wait(ahead: int) -> None:
        secs = int(proxypool._load().get("cooldown_secs") or 0)
        job.queued = True
        job.queue_ahead = ahead
        # cap_display：0 = 不限。内部哨兵是 10**6，直接透出去会写成
        # 「每出口最多 1000000 个机位」——操作员会以为是坏数据。
        cap = proxypool._slot_cap_display()
        job.log("出口已满（%s，且刚用完的出口要休息 %d 分钟），"
                "排队等待空闲出口…前面还有 %d 个"
                % ("每出口不限并发" if not cap else "每出口最多 %d 个机位" % cap,
                   secs // 60, ahead))

    r = proxypool.acquire_slot(key, on_wait=on_wait, stop_flag=job.stop_flag)
    if not r.get("ok"):
        # ★ 括号必须包住 `or`：`%` 的优先级高于 `or`，写成
        #   `"…%s…" % r.get("reason") or "未知原因"` 时，先算 % 再算 or ——
        #   于是 reason 为 None 时日志会打印字面的「None」，
        #   「未知原因」这个兜底永远不会生效。
        job.log("没拿到出口（%s），本次直连执行" % (r.get("reason") or "未知原因"))
        job.queued = False
        return ""
    job.queued = False
    job.queue_ahead = 0
    if r.get("waited"):
        job.log("已排到出口（等了 %ds）：%s" % (r["waited"], proxypool.display(r["url"])))
    return key


def _release_exit_slot(key: str, job_log) -> None:
    """归还出口并进入冷却。"""
    from . import proxypool
    r = proxypool.release_slot(key)
    if r.get("url"):
        job_log("已归还出口 %s，该出口休息 %d 分钟后才能再接机位"
                % (proxypool.display(r["url"]), int(r.get("cooldown") or 0) // 60))


def with_renew(fn, acc: dict, job_log):
    """执行 fn(client)，凭证过期时自动续期重试一次。

    平台级例外：0.16.5 起网易关闭了 REST 业务层，业务端点对所有账号一律返回
    418/422；此时续期入口 auth/login 本身也是 418 —— 重试必然再失败一次，
    白白多打一次网关、还会把账号标成「待续期」。所以直接抛出，由上层归因。
    """
    client = make_client(acc)
    try:
        return fn(client), False
    except Exception as e:
        if cnlogin.is_platform_rest_down(e):
            job_log("平台级失败：%s" % cnlogin.platform_rest_down_note(e))
            raise
        if not account_credentials_expired(e):
            raise
        job_log("会话过期，自动续期中…")
        renewed = _renew_mark(acc, job_log)
        client = make_client({**acc, **renewed})
        return fn(client), True


def run_pickup_job(job: Job, acc: dict, payload: dict, log) -> dict:
    scenes = payload.get("scenes") or None
    dye = bool(payload.get("dye", False))
    # ★ 2026-10-09：拉票失败（= 会话被顶）当场自动续期，然后继续跑这张图 ——
    #   不再让用户自己发现「跑图全失败」再去点「续期」。
    on_stale = make_stale_renewer(acc, log)

    def work(client):
        return pickup.run_pickup(client, scenes=scenes, collect_dye=dye, log=log,
                                 stop_flag=job.stop_flag, on_stale=on_stale)

    (summary, renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def daily_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """每日任务：转在线 → 领奖闭环。会话失效自动续期重试（对齐 TaskRunController.runWithSessionReplay：
    整体失败时用 get_currency 探测，疑似会话失效才续期重试一次）。"""
    preview = bool(payload.get("preview", False))

    def work(client):
        return daily.run_daily(client, log=log, stop_flag=job.stop_flag, preview=preview)

    try:
        summary = work(make_client(acc))
    except Exception as e:
        if cnlogin.is_platform_rest_down(e):
            log("平台级失败：%s" % cnlogin.platform_rest_down_note(e))
            if not preview:
                update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"),
                               status="warn", status_note=cnlogin.platform_rest_down_note(e))
            return {"ok": False, "error": str(e),
                    "platform_rest_down": True, "note": cnlogin.platform_rest_down_note(e)}
        if not account_credentials_expired(e):
            raise
        log("会话过期，自动续期中…")
        renewed = _renew_mark(acc, log)
        summary = work(make_client({**acc, **renewed}))
    if not summary.get("ok") and not (job.stop_flag and job.stop_flag.is_set()):
        expired = False
        probe_err = None
        try:
            make_client(acc).get_currency()
        except Exception as e:
            probe_err = e
            expired = account_credentials_expired(e)
        if not expired and cnlogin.is_platform_rest_down(probe_err):
            # 0.16.5：get_currency 对所有账号 418/422，探测已失去区分度。
            # 续期必然同样失败，不再空转。
            note = cnlogin.platform_rest_down_note(probe_err)
            log("每日任务未完成（平台级）：%s" % note)
            if not preview:
                update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"),
                               status="warn", status_note=note)
            return {"ok": False, "platform_rest_down": True, "note": note,
                    "detail": summary}
        if expired:
            log("会话疑似失效，自动续期后重试…")
            renewed = _renew_mark(acc, log)
            summary = work(make_client({**acc, **renewed}))
    if not preview:
        # ★ 只有真的跑成功才标 ok —— 失败却标 ok 会伪造「任务已完成」。
        if summary.get("ok"):
            update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"),
                           status="ok", status_note="")
        else:
            why = " ".join(str(summary.get("error") or summary.get("note") or "未完成")).strip()[:120]
            update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"),
                           status="warn", status_note=why)
    return summary


def seasonhome_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """遇境神坛季蜡（独立任务）：转在线 → 收 global pickup 4180017154 → 季蜡差值判成败。

    有季卡的号每日额外 1 根；无季卡服务端没有这个拾取物（差值 0 → 「无季卡·无可领取」）。
    每日任务任务链末尾也会顺带领一次（见 daily.run_daily），此入口供用户单独点。
    """

    def work(client):
        return seasonhome.claim(client, log=log, stop_flag=job.stop_flag)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def wings_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """光翼获取（独立任务）：收集全地图光翼（wing_buff），collect 前先短转在线。"""
    def work(client):
        return eden.run_wing_collect(client, log=log, stop_flag=job.stop_flag)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def eden_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """伊甸献祭（独立任务）：献祭 → 清理 → 兑换红蜡（不含光翼获取，见 wings_job）。⚠ 消耗光翼不可逆（端点已要求 confirm）。"""

    def work(client):
        return eden.run_sacrifice(client, log=log, stop_flag=job.stop_flag)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def wingless_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """强制无翼（管理员面板）：把机位当前持有的光翼全部 drop 掉并维持 collected=0。

    ★ 全程只用 wing_buffs/drop（改标记，可逆），**绝不 deposit/convert**（不可逆）。
    ★ drop 只能把号打到「0 态」（行留着、collected 全 false）——真·null 态在已知通道上
      做不到（2026-10-04 C 阶段定案，见 docs/知识库/游戏功能/92 篇）。
    payload.collectible_only（默认 True）：只清在 get_map_defs 清单里的翼，保证可复原。
    """
    collectible_only = bool(payload.get("collectible_only", True))

    def work(client):
        return wingless.ensure_wingless(client, log=log, stop_flag=job.stop_flag,
                                       collectible_only=collectible_only)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def tokens_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """活动代币：对路线表中 Event_ 前缀活动图跑拾取（复用跑图链路，只要拾取）。"""

    # ★ 2026-10-09：活动图的「进图失败：拉票失败」同样是会话被顶（22 篇 §10），
    #   原注释「多为服务端限流」是错的 —— 交给同一个钩子自动续期后重拉票。
    on_stale = make_stale_renewer(acc, log)

    def work(client):
        return eventtoken.run_tokens(client, log=log, stop_flag=job.stop_flag, on_stale=on_stale)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def activity_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """精灵任务（联动寻宝）：转在线 → 读任务定义 → 挑候选 → create/set_stat/update。

    **只完成、不领奖**（用户 2026-10-03 明确）：update_quest 一律 collect=False，
    绝不调用 claim_jingling_mission_reward（该路由在国服现网关已 404）。
    preview=只读清单；limit>0 单点实跑（先跑 1 条验证）；only=id 子串过滤。
    """
    preview = bool(payload.get("preview", False))
    all_in = bool(payload.get("all", False))
    only = str(payload.get("only") or "")[:40]
    limit = payload.get("limit")
    try:
        limit = max(0, min(int(limit or 0), jingling.MAX_QUESTS))
    except (TypeError, ValueError):
        limit = 0

    def work(client):
        return jingling.run_spirit(client, log=log, stop_flag=job.stop_flag,
                                   preview=preview, limit=limit, only=only,
                                   all_in=all_in, acc_id=acc.get("id", ""))

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"),
                   status="ok", status_note="")
    return summary


def activitytask_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """活动任务（活动中心 jingling 桥）：完成 MC 联动等活动里的里程碑任务。

    **只完成、不领奖**（用户 2026-10-03/10-04 明确）：全程只有一个写资源
    `set_player_task`；`claim_jingling_mission_reward` 在国服根本不存在（404），也不调用。

    通道 = `POST /account/jingling/process_web_request`，
    body `{resource, content:<字符串化 JSON>, callback_id:<随机 int>}`（缺 callback_id → 404，
    content 传对象 → 业务码 500）。细则见 `qfsky/activity.py` 文件头与知识库 23 篇第 9 节。

    preview=只读清单（不写）；event=<活动 id> 只跑这一期；all=true 跑 MC 联动全家桶。
    """
    preview = bool(payload.get("preview", False))
    event = str(payload.get("event") or "")[:60]
    all_in = bool(payload.get("all", False))

    def work(client):
        return activity.run_activity(client, log=log, stop_flag=job.stop_flag,
                                     preview=preview, event=event, all_in=all_in,
                                     acc_id=acc.get("id", ""))

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"),
                   status="ok", status_note="")
    return summary


def newacc_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """新号服务：地图解锁 / 全部动作 / 全部先祖 / 向导爱心 多选组合（全部免费零消耗，纯 HTTP）。"""
    modes = [m for m in (payload.get("modes") or []) if m in newacc.MODE_KEYS]
    if not modes:
        raise ValueError("新号服务：至少选择一项（map/actions/spirits/guides）")
    log("新号服务：%s" % "、".join(dict(newacc.MODE_META)[m] for m in modes))

    def work(client):
        return newacc.run_newacc(client, modes, log=log, stop_flag=job.stop_flag)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def race_job(job: Job, acc: dict, payload: dict, log) -> dict:
    """赛道奖励：转在线后领取霞谷赛道每日奖励（sunset_flyrace + sunset_race）。"""

    def work(client):
        return newacc.run_race(client, log=log, stop_flag=job.stop_flag)

    (summary, _renewed) = with_renew(work, acc, log)
    update_account(acc["id"], data=acc.get("data"), data_at=acc.get("data_at"), status="ok", status_note="")
    return summary


def refresh_job(job: Job, acc: dict, payload: dict, log) -> dict:

    def work(client):
        s = gameinfo.get_full_summary(client)
        # 装扮/无翼/身高：随刷新自动获取（用户要求加到功能上面）
        try:
            s["outfit"] = outfit.summarize_outfit(outfit.get_or_init_outfit(client))
            from . import outfitcatalog
            outfitcatalog.enrich_summary(s["outfit"])
        except Exception as e:
            s["outfit_error"] = str(e)
        log("数据刷新完成")
        return s

    (summary, renewed) = with_renew(work, acc, log)
    cur = summary.get("currency")
    if not isinstance(cur, dict) or not cur:
        # get_full_summary 单接口失败不抛出：货币缺失时标黄（半失效会话的典型症状）
        raw_err = summary.get("currency_error")
        why = " ".join(str(raw_err or "未知")).strip()[:60]
        if cnlogin.is_platform_rest_down(raw_err) or cnlogin.is_platform_rest_down(why):
            # 0.16.5（2026-09-24）：网易关闭 REST 业务层，**所有账号**的
            # get_currency 都返回 418/422（生产实测 4/4）。这不是「会话半失效」，
            # 提示「重新登录」会误导用户去白耗登录配额，也不能进封禁统计。
            note = cnlogin.platform_rest_down_note(why)
            log("货币未获取到（平台级）：%s" % note)
            update_account(acc["id"], data=summary, data_at=int(time.time()),
                           status="warn", status_note=note)
            return summary
        try:
            bans.maybe_record(acc, str(summary.get("currency_error") or ""))
        except Exception:
            pass
        log("货币未获取到（%s），需重新登录后再刷新" % why)
        update_account(acc["id"], data=summary, data_at=int(time.time()), status="warn", status_note="重新登录")
        return summary
    update_account(acc["id"], data=summary, data_at=int(time.time()), status="ok", status_note="")
    return summary


def renew_job(job: Job, acc: dict, payload: dict, log) -> dict:
    log("手动续期：重新校验凭证 → auth/login…")
    renewed = _renew_mark(acc, log)
    log("续期成功")
    return {"renewed": True}
