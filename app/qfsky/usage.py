# -*- coding: utf-8 -*-
"""功能使用台账 —— 「这台被封号的机位，历史上用过站点的哪些功能」。

背景（2026-09-23 用户要求）：
  封神榜（qfsky.bans / 管理面板「封禁」分区）只能告诉你「哪台机位被封了、封到
  什么时候」，判断不了**是哪个功能把号跑没的**。要回答「那些封号的账号历史都用
  了哪些功能」，就得在请求进站时把「谁 · 哪台机位 · 哪个功能 · 哪一天」记下来。

埋点位选在 main.auth_guard：它是全部 /api/ 请求的必经处（和风控、维护闸门同一条
思路）。散在各个路由里加埋点必然漏，而且每加一个功能都要改一处 —— 这里只维护一
张「路由 → 功能」的映射表。

★ 仅管理员可见：
  · 读接口只有 /api/admin/**（_require_admin 把关）在用；
  · 公开的封神榜 /api/bans 完全不含这些字段（bans.list_public 里压根没有）；
  · 前端只在管理面板里渲染。

落档 data/usage.json（0600）：
  {
    "v": 1,
    "saved": <ts>,
    "accounts": { <acc_id>: {"owner","first","last","f": {feat: 明细}} },
    "users":    { <uid>:    {"first","last","f": {feat: 明细}} }
  }
  明细 = {"n": 次数, "first": ts, "last": ts, "d": {"20260923": 12, ...}}
  「d」是**北京时间**自然日 → 次数（服务器是 UTC，直接用 localtime 会错 8 小时，
  与 main._CN_TZ 的口径一致），只保留最近 KEEP_DAYS 天。

写入策略：热路径（note）只做 dict 运算，零 IO；落盘由心跳（tick）与管理员读取
（flush(force=True)）驱动，所以面板上看到的一定是最新值。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.path.join(_DATA_DIR, "usage.json")

_CN = timezone(timedelta(hours=8))   # 北京时间：与 main._CN_TZ 同一口径
KEEP_DAYS = 60          # 逐日明细保留天数
FLUSH_SECS = 20         # 最短落盘间隔（心跳 30s 一次，所以常态就是 30s 一写）
MAX_ACCOUNTS = 3000     # 机位条目上限（超了丢最久没动的）
MAX_USERS = 5000        # 站点用户条目上限
TOP_N = 6               # 摘要里列几个功能

_lock = threading.RLock()
_io_lock = threading.Lock()      # 串行化落盘，保证旧快照不覆盖新快照（见 flush）
_accounts: dict[str, dict] = {}
_users: dict[str, dict] = {}
_loaded = False
_dirty = False
_last_flush = 0.0
_written_ts = 0.0                # 已经写盘的那份快照的时刻


# ── 功能目录：key → 中文名（服务端是权威，前端只显示）──
FEATURES: dict[str, str] = {
    # 机位维度（跑图机位上的动作）
    "race": "跑图",
    "pickup": "领取奖励",
    "daily": "每日任务",
    "season_home": "季心家园",
    "activity": "精灵任务",
    "activity_task": "活动任务",
    "wings": "收集光翼",
    "eden": "伊甸献祭",
    "tokens": "代币烛火",
    "newacc": "新号开荒",
    "wingless": "强制无翼（管理员）",
    "renew": "续期登录态",
    "refresh": "刷新登录态",
    "rename": "机位改名",
    "friends": "好友列表",
    "friend_edit": "好友改名/删除",
    "friend_heart": "送心（免前置）",
    "friend_add": "加好友（好友码）",
    "invite_new": "生成好友码",
    "invite_del": "撤销好友码",
    "invite_check": "校验好友码",
    "invites": "好友码列表",
    "outfit": "衣柜",
    "proxy": "设置出口 IP",
    "proxy_test": "出口测活",
    "account_view": "查看机位",
    "account_list": "机位列表",
    "account_del": "删除机位",
    # 站点维度
    "login": "渠道登录",
    "notice_ack": "公告/规范确认",
    "config": "参数配置",
    "devices": "设备管理",
    "proxy_prefer": "出口偏好",
    "job_stop": "任务停止",
    "maps": "地图查看",
    "feedback": "问题反馈",
    "chat": "公共聊天室",
    "minigame": "接烛火小游戏",
}


def zh_of(key: str) -> str:
    return FEATURES.get(key) or key


# ── 路由 → 功能映射 ──
# 两条表：机位维度（捕获组 1 = acc_id）与站点维度。全部用 $ 锚定，互不重叠。
_ACC_RULES: list[tuple[str, re.Pattern, str]] = [
    ("POST", re.compile(r"^/api/accounts/([^/]+)/race$"), "race"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/pickup$"), "pickup"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/daily$"), "daily"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/season-home$"), "season_home"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/activity$"), "activity"),
    # 活动任务（活动中心 jingling 桥，2026-10-04）：起任务 + 只读清单都记成同一个功能键，
    # 否则「用户到底有没有在用这个功能」会被拆成两行看。
    ("POST", re.compile(r"^/api/accounts/([^/]+)/activitytask$"), "activity_task"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)/activity/events$"), "activity_task"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/wings$"), "wings"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/eden$"), "eden"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/tokens$"), "tokens"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/newacc$"), "newacc"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/renew$"), "renew"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/refresh$"), "refresh"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/rename$"), "rename"),
    # 好友的三条写路由要在列表之前（都用 $ 锚定，其实互斥，顺序只是给人看的）
    ("POST", re.compile(r"^/api/accounts/([^/]+)/friends/heart$"), "friend_heart"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/friends$"), "friend_add"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/invites$"), "invite_new"),
    ("DELETE", re.compile(r"^/api/accounts/([^/]+)/invites/[^/]+$"), "invite_del"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/friends/(rename|delete)$"), "friend_edit"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)/friends$"), "friends"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)/invites$"), "invites"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)/invites/[^/]+$"), "invite_check"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)/outfit$"), "outfit"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/proxy/test$"), "proxy_test"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)/proxy$"), "proxy"),
    ("POST", re.compile(r"^/api/accounts/([^/]+)/proxy$"), "proxy"),
    ("DELETE", re.compile(r"^/api/accounts/([^/]+)$"), "account_del"),
    ("GET", re.compile(r"^/api/accounts/([^/]+)$"), "account_view"),
    ("GET", re.compile(r"^/api/accounts$"), "account_list"),
]

_USER_RULES: list[tuple[str, re.Pattern, str]] = [
    # ★ 这里**故意没有** /api/auth/login|register|logout|change-password|reset-password
    #   与 /api/auth/email-code：这五个都在 main._PUBLIC_API 里，走的是未登录分支，
    #   而 usage.note() 只在 auth_guard 的**已登录**分支被调用 —— 写了也永远不会命中，
    #   只会让人误以为「登录会被记」。用户维度的第一条真实记录是他登录之后的第一个
    #   需登录请求（config / account_list / …）。真要记登录，得在那几个路由里单独埋点。
    ("POST", re.compile(r"^/api/notice/ack$"), "notice_ack"),
    ("POST", re.compile(r"^/api/announcement/ack$"), "notice_ack"),
    # 读接口里同样要剔掉**轮询**：控制台每 4s 拉一次流水线、任务每 1.2s 轮询一次、
    # 聊天每 2~5s 拉增量。它们不是「用户主动用了某个功能」，记进来会把每个用户的
    # 功能榜刷成 pipeline/chat 的计数（实测一小时 ~900 次），把真正的信号淹掉。
    ("POST", re.compile(r"^/api/config/params$"), "config"),
    ("GET", re.compile(r"^/api/devices$"), "devices"),
    ("POST", re.compile(r"^/api/devices$"), "devices"),
    ("DELETE", re.compile(r"^/api/devices/[^/]+$"), "devices"),
    ("GET", re.compile(r"^/api/proxy/options$"), "proxy_prefer"),
    ("POST", re.compile(r"^/api/proxy/prefer$"), "proxy_prefer"),
    ("POST", re.compile(r"^/api/jobs/[^/]+/stop$"), "job_stop"),
    ("POST", re.compile(r"^/api/feedback$"), "feedback"),
    ("POST", re.compile(r"^/api/chat$"), "chat"),
    ("POST", re.compile(r"^/api/chat/[^/]+/recall$"), "chat"),
    ("GET", re.compile(r"^/api/minigame/board$"), "minigame"),
    ("POST", re.compile(r"^/api/minigame/start$"), "minigame"),
    ("POST", re.compile(r"^/api/minigame/score$"), "minigame"),
]

# 前缀整段归一：渠道登录链（网易/B站/vivo/4399/九游/华为/荣耀/应用宝/微信小游戏…）
# 每条链内部都是十几跳握手，逐条列没意义，统一记成「渠道登录」。
_LOGIN_PREFIXES = (
    "/api/login/", "/api/huawei/", "/api/honor/", "/api/myapp/", "/api/yyb/",
)

# 明确不记的噪声/基础设施：鉴权探活、心跳、前端错误上报、静态图、管理面板自身的读写
# （管理面板不是「被封号用户用过的功能」，记进来只会把信号淹掉），
# 以及全部**轮询型读接口**（前端定时器在打，不代表用户在用什么）。
#
# ★ 这里只放**方法无关**的前缀（/api/chat 那种「POST 是功能、GET 是轮询」的路径
#   不能放进来 —— 前缀匹配不看方法，放进来会把发言一起剔掉）。方法相关的剔除法
#   写在下面的 extRules 里。
_SKIP_PREFIXES = (
    "/api/auth/me", "/api/health", "/api/online", "/api/client-log", "/api/maintenance",
    "/api/ann/img/", "/api/admin/",
    "/api/pipeline",        # 控制台每 4s 轮询
    "/api/jobs",            # 任务状态每 1.2s 轮询；「停止任务」靠 extRules 的**更长
                            # 前缀优先级**排在前面命中，所以这里写裸 /api/jobs 就够
    "/api/notice",          # 公告读取/确认（确认靠 extRules 命中）
    "/api/config",          # 参数读取（改参数靠 extRules 命中）
    "/api/stats",           # 顶栏统计轮询
    "/api/bans",            # 封神榜小组件探活
    "/api/proxy",           # 出口选项读取 + 偏好设置（见 extRules）
)

# 方法/形状相关的补充规则：(正则, 排除的方法或 None, 功能 key 或 None=剔除)。
#
# ★ 顺序敏感，且**不看 tail（方法）**：前缀剔除 / 机位维度 / 站点维度三段各自独立，
#   这里只负责把「前缀那条线覆盖不到的」补上。**按前缀从长到短排**：长前缀代表更具体
#   的动作（/api/jobs/{id}/stop 是「停止任务」），必须先于它所属的前缀族（/api/jobs）
#   被检查 —— 结构上必须排在 _SKIP_PREFIXES 的循环**之前**，见 classify()。
_EXT_RULES: list[tuple[re.Pattern, str | None, str | None]] = [
    # ★管理员面板「强制无翼」（2026-10-04）：两条都必须排在 _SKIP_PREFIXES 的
    #   /api/admin/ 之前，否则会被整族剔除、统计里根本看不到这个功能被用过。
    (re.compile(r"^/api/admin/wingless/account/[^/]+$"), None, "wingless"),   # POST 起任务
    (re.compile(r"^/api/admin/wingless/accounts$"), None, "wingless"),        # 管理员主动拉的体检清单
    (re.compile(r"^/api/jobs/[^/]+/stop$"), None, "job_stop"),   # 长前缀优先
    (re.compile(r"^/api/jobs/[^/]+$"), None, None),              # 1.2s 轮询
    (re.compile(r"^/api/chat$"), "GET", None),                   # GET 拉增量 = 轮询
    (re.compile(r"^/api/chat$"), None, "chat"),                  # POST 发言 = 功能
    (re.compile(r"^/api/chat/[^/]+/recall$"), None, "chat"),
    (re.compile(r"^/api/feedback$"), "GET", None),               # 列表轮询
    (re.compile(r"^/api/feedback$"), None, "feedback"),
    (re.compile(r"^/api/config/params$"), None, "config"),       # 改参数 = 功能
    (re.compile(r"^/api/notice/ack$"), None, "notice_ack"),
    (re.compile(r"^/api/announcement/ack$"), None, "notice_ack"),
    (re.compile(r"^/api/devices(/[^/]+)?$"), None, "devices"),
    (re.compile(r"^/api/proxy/prefer$"), None, "proxy_prefer"),  # 长前缀优先
    (re.compile(r"^/api/proxy/options$"), "GET", None),
    (re.compile(r"^/api/maps$"), None, None),                    # GET 地图列表是读接口
    (re.compile(r"^/api/minigame/(board|start|score)$"), None, "minigame"),
]


def classify(method: str, path: str) -> tuple[str, str] | None:
    """HTTP 请求 → (功能 key, 机位 id)。不构成「功能使用」时返回 None。

    纯函数、无状态，便于单测；调用方（main.auth_guard）负责只对**已登录**的请求
    调用它 —— 免登录端点在中间件里没有 request.state.auth，天然不会被记。

    四段判定：① 方法/形状补充规则（比前缀族更具体的动作在这里命中）；② 方法无关
    前缀剔除；③ 机位维度写操作；④ 站点维度写操作 + 渠道登录链前缀。
    **纯 GET 一律不算功能使用**：读接口基本都被前端定时器在打（流水线 4s、任务 1.2s、
    聊天 2~5s…），只有写操作和登录链握手才是用户主动动作。
    """
    m = (method or "GET").upper()
    p = path or ""
    for rx, skip_m, feat in _EXT_RULES:
        if not rx.match(p):
            continue
        if skip_m is not None and m == skip_m:
            return None
        if feat is not None:
            return feat, ""
    for pre in _SKIP_PREFIXES:
        if p.startswith(pre):
            return None
    # 剩下的一律只认**已登录的**写操作（POST/PUT/DELETE…）；GET 全部按读取处理
    if m == "GET":
        return None
    for r, rx, key in _ACC_RULES:
        if m == r:
            hit = rx.match(p)
            if hit:
                # 无捕获组的规则（如 ^/api/accounts$）没有机位维度 → acc_id 空串
                return key, (hit.group(1) if hit.groups() else "")
    for r, rx, key in _USER_RULES:
        if m == r and rx.match(p):
            return key, ""
    for pre in _LOGIN_PREFIXES:
        if p.startswith(pre):
            return "login", ""
    return None


# ── 持久化 ──

def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _atomic_write(txt: str) -> None:
    """原子落盘（0600）。调用方必须已经把 JSON 序列化好、且不持锁。"""
    _ensure_dir()
    tmp = FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(txt)
        # ★ 先 chmod 再 replace：Windows 上 chmod 是空操作（本机自检会看到 0666），
        #   但在 Linux 上是真的 —— 只有落在临时文件上才能保证**落地那一刻**就是 0600，
        #   不留下「文件已存在但权限还没收紧」的窗口。
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, FILE)
        try:
            os.chmod(FILE, 0o600)
        except OSError:
            pass
    except Exception as e:      # 台账写失败绝不影响主流程
        print("[usage] 落盘失败：%r" % e, flush=True)


def _load_once() -> None:
    """首次使用时读盘一次。

    ★ _loaded 只在**读成功**（含文件不存在）之后才置位：一次瞬时读失败（磁盘/权限
    抖动）不该把加载永久关掉 —— 那样下一次心跳就会拿空台账覆盖掉好数据。
    """
    global _loaded
    if _loaded:
        return
    if not os.path.exists(FILE):
        _loaded = True
        return
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("[usage] 台账读取失败（下次再试）：%r" % e, flush=True)
        return
    _loaded = True
    if not isinstance(data, dict):
        return
    for name, dst in (("accounts", _accounts), ("users", _users)):
        src = data.get(name)
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            # v["f"] 必须是 dict；它内部的每个值还要能当 dict 用（_feature_rows /
            # _prune_days 都按 dict 遍历），半坏的档就地丢掉那一条而不是让它后面炸
            if isinstance(v, dict) and isinstance(v.get("f"), dict) \
                    and all(isinstance(x, dict) for x in v["f"].values()):
                dst[str(k)] = v


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, _CN).strftime("%Y%m%d")


def _num(v, default: float = 0.0) -> float:
    """把任意输入安全转成数字（坏档里的 "abc"/None/[] 一律当 default）。

    为什么到处用它而不是裸 int()/float()：usage.json 是**可以被运维/人手改的文件**，
    一个坏标量不该让整个管理面板 500（读接口第一件事就是 flush(force=True)）。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default: int = 0) -> int:
    return int(_num(v, default))


def _prune_days(ent: dict, cutoff: str) -> None:
    """丢掉 cutoff 之前的逐日明细（字符串比较即可，YYYYMMDD 天然有序）。"""
    src = ent.get("f")
    if not isinstance(src, dict):
        return
    for f in src.values():
        if not isinstance(f, dict):
            continue
        d = f.get("d")
        if not isinstance(d, dict):
            f["d"] = {}
            continue
        for k in [k for k in d if k < cutoff]:
            d.pop(k, None)


def _cap(store: dict, limit: int) -> None:
    """条目超过上限时丢最久没动的（下限是正在被封的号，通常远小于上限）。"""
    if len(store) <= limit:
        return
    def last_of(kv):
        v = kv[1]
        return _num(v.get("last")) if isinstance(v, dict) else 0.0
    for k, _v in sorted(store.items(), key=last_of)[: len(store) - limit]:
        store.pop(k, None)


def _bump(store: dict, key: str, feature: str, now: float, day: str, owner: str = "") -> None:
    ent = store.get(key)
    if ent is None:
        ent = {"first": now, "last": now, "f": {}}
        store[key] = ent
    if owner and ent.get("owner") != owner:
        ent["owner"] = owner
    ent["last"] = now
    f = ent["f"].get(feature)
    if f is None:
        f = {"n": 0, "first": now, "last": now, "d": {}}
        ent["f"][feature] = f
    f["n"] = _int(f.get("n")) + 1
    f["last"] = now
    d = f.get("d")
    if not isinstance(d, dict):
        d = f["d"] = {}
    d[day] = _int(d.get(day)) + 1


def note(method: str, path: str, user_id: str) -> bool:
    """热路径埋点：记账一次（纯内存）。返回是否记了。**绝不抛异常、绝不 IO。**"""
    global _dirty
    if not user_id:
        return False
    try:
        hit = classify(method, path)
        if not hit:
            return False
        feature, acc_id = hit
        now = time.time()
        day = _day(now)
        with _lock:
            _load_once()
            if acc_id:
                # ★ 机位维度的动作**不重复记到用户维度** —— 否则总览里「跑图」会在
                #   users 和 accounts 两张表各算一遍（次数翻倍、看着像跑了两趟）。
                #   用户维度的功能用量 = 站点级动作 + 由 account_summary 按机位聚合。
                _bump(_accounts, str(acc_id), feature, now, day, owner=str(user_id))
            else:
                _bump(_users, str(user_id), feature, now, day)
            _dirty = True
        return True
    except Exception:
        return False


def flush(force: bool = False) -> bool:
    """按节流落盘。force=True 供管理员读之前调用（保证面板看到的是最新值）。

    ★ 落盘必须在**锁外**（IO 不挡请求），所以写入顺序要自己兜住：两个线程可能
    同时拿到快照（心跳 tick 与管理员读的 force 各一次），若先算出来的那份后写，
    盘上就是旧数据 —— 而内存里的 _dirty 已经被清掉，崩溃/重启就真丢了。
    用 _io_lock + _written_ts 保证「旧快照绝不覆盖新快照」。
    """
    global _written_ts
    data = _snapshot(force)
    if data is None:
        return False
    txt, now = data
    with _io_lock:
        if now <= _written_ts:      # 已经有一份更新的快照写过盘了
            return False
        _written_ts = now
    _atomic_write(txt)
    return True


def _snapshot(force: bool):
    """加锁：裁剪 + 序列化，返回 (json 文本, 快照时刻) 或 None（无需落盘）。"""
    global _dirty, _last_flush
    with _lock:
        _load_once()
        if not _dirty:
            return None
        now = time.time()
        if not force and now - _last_flush < FLUSH_SECS:
            return None
        cutoff = _day(now - KEEP_DAYS * 86400)
        for ent in list(_accounts.values()) + list(_users.values()):
            if isinstance(ent, dict):
                _prune_days(ent, cutoff)
        _cap(_accounts, MAX_ACCOUNTS)
        _cap(_users, MAX_USERS)
        payload = {"v": 1, "saved": int(now), "accounts": _accounts, "users": _users}
        try:
            txt = json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            print("[usage] 序列化失败：%r" % e, flush=True)
            _dirty = False
            _last_flush = now
            return None
        _dirty = False
        _last_flush = now
        return txt, now


def tick() -> None:
    """心跳钩子（qfsky.scheduler，每 30s）：落盘 + 裁剪。异常自行吞掉。"""
    try:
        flush()
    except Exception as e:
        print("[usage] 心跳落盘异常：%r" % e, flush=True)


# ── 读取（只给 /api/admin/** 用）──

def _feature_rows(ent: dict) -> list[dict]:
    """把一条记录的 "f" 展平成排序好的行。**对畸形数据免疫**（坏档 / 手改过的
    usage.json 不该让管理面板 500），非 dict 的条目直接跳过。"""
    rows = []
    src = ent.get("f")
    if not isinstance(src, dict):
        return rows
    for k, f in src.items():
        if not isinstance(f, dict):
            continue
        d = f.get("d") if isinstance(f.get("d"), dict) else {}
        rows.append({
            "k": k,
            "zh": zh_of(k),
            "n": _int(f.get("n")),
            "first": _int(f.get("first")),
            "last": _int(f.get("last")),
            "days": [{"d": kk, "n": _int(d[kk])} for kk in sorted(d)],
        })
    rows.sort(key=lambda r: (-r["n"], -r["last"]))
    return rows


def _daily_total(rows: list[dict]) -> list[dict]:
    """把所有功能的逐日次数合成一条时间线（按日聚合），供「哪几天在跑」一眼看。"""
    acc: dict[str, int] = {}
    for r in rows:
        for it in r["days"]:
            acc[it["d"]] = acc.get(it["d"], 0) + it["n"]
    return [{"d": k, "n": acc[k]} for k in sorted(acc)]


def account_detail(acc_id: str) -> dict:
    """一台机位的功能使用台账（全量）。"""
    flush(force=True)
    with _lock:
        _load_once()
        ent = _accounts.get(str(acc_id))
        if not ent:
            return {"account_id": str(acc_id), "owner": "", "first": 0, "last": 0,
                    "total": 0, "features": [], "daily": []}
        rows = _feature_rows(ent)
        total = sum(r["n"] for r in rows)
        first, last = _int(ent.get("first")), _int(ent.get("last"))
        owner = str(ent.get("owner") or "")
    # 面板那边会拿 user_id 再查一次「同一账号的站点级用量」，这里顺手带回去，
    # 省掉一次往返（两个读接口在 UI 上总是一起用）。
    uu = None
    if owner:
        uu = user_summary(owner)
    if not first and uu:
        first, last = uu.get("first", 0), uu.get("last", 0)
    out = {
        "account_id": str(acc_id),
        "owner": owner,
        "first": first,
        "last": last,
        "total": total,
        "features": rows,
        "daily": _daily_total(rows),
    }
    if owner:
        out["user_usage"] = uu
    return out


def account_summary(acc_id: str) -> dict:
    """紧凑摘要（给封神榜列表内嵌用）：总次数 + 最近时间 + 前几个功能。

    ★ 这里**不**调 flush()：封神榜一次要嵌几十台机位，逐个 force 落盘等于把整个
    台账写几十遍（还是同步 IO）。心跳每 30s 已经会落盘，摘要晚几十秒没关系。
    """
    with _lock:
        _load_once()
        ent = _accounts.get(str(acc_id))
        if not ent:
            return {"total": 0, "last": 0, "first": 0, "top": [], "features": 0}
        rows = _feature_rows(ent)
        return {
            "total": sum(r["n"] for r in rows),
            "first": _int(ent.get("first")),
            "last": _int(ent.get("last")),
            "features": len(rows),
            "top": [{"k": r["k"], "zh": r["zh"], "n": r["n"], "last": r["last"]} for r in rows[:TOP_N]],
        }


def user_detail(user_id: str) -> dict:
    """一个站点用户的站点级功能使用台账（机位维度的用量另由调用方按机位列）。"""
    flush(force=True)
    with _lock:
        _load_once()
        ent = _users.get(str(user_id))
        if not ent:
            return {"user_id": str(user_id), "first": 0, "last": 0, "total": 0,
                    "features": [], "daily": []}
        rows = _feature_rows(ent)
        return {
            "user_id": str(user_id),
            "first": _int(ent.get("first")),
            "last": _int(ent.get("last")),
            "total": sum(r["n"] for r in rows),
            "features": rows,
            "daily": _daily_total(rows),
        }


def user_summary(user_id: str) -> dict:
    with _lock:
        _load_once()
        ent = _users.get(str(user_id))
        if not ent:
            return {"total": 0, "last": 0, "first": 0, "top": [], "features": 0}
        rows = _feature_rows(ent)
        return {
            "total": sum(r["n"] for r in rows),
            "first": _int(ent.get("first")),
            "last": _int(ent.get("last")),
            "features": len(rows),
            "top": [{"k": r["k"], "zh": r["zh"], "n": r["n"], "last": r["last"]} for r in rows[:TOP_N]],
        }


# ── 反查（「这个功能被谁用过」/ 总览）──

def accounts_using(feature: str) -> list[dict]:
    """用过某个功能的机位，按次数降序。"""
    flush(force=True)
    out = []
    with _lock:
        _load_once()
        for aid, ent in _accounts.items():
            f = (ent.get("f") or {}).get(feature)
            if not isinstance(f, dict):
                continue
            out.append({"account_id": aid, "owner": str(ent.get("owner") or ""),
                        "n": _int(f.get("n")),
                        "first": _int(f.get("first")),
                        "last": _int(f.get("last"))})
    out.sort(key=lambda r: (-r["n"], -r["last"]))
    return out


def users_using(feature: str) -> list[dict]:
    """用过某个功能的站点用户，按次数降序。"""
    flush(force=True)
    out = []
    with _lock:
        _load_once()
        for uid, ent in _users.items():
            f = (ent.get("f") or {}).get(feature)
            if not isinstance(f, dict):
                continue
            out.append({"user_id": uid, "n": _int(f.get("n")),
                        "first": _int(f.get("first")),
                        "last": _int(f.get("last"))})
    out.sort(key=lambda r: (-r["n"], -r["last"]))
    return out


def overview(banned_ids: set | None = None) -> dict:
    """功能总览：每个功能被多少用户 / 多少机位用过、总次数，以及**被封机位**的占比。

    banned_ids 由调用方（bans.banned_ids()）传入 —— 两个模块互不 import，避免环。
    """
    flush(force=True)
    bid = {str(x) for x in (banned_ids or ())}
    agg: dict[str, dict] = {}
    with _lock:
        _load_once()
        for uid, ent in _users.items():
            for k, f in (ent.get("f") or {}).items():
                if not isinstance(f, dict):
                    continue
                a = agg.setdefault(k, {"k": k, "zh": zh_of(k), "n": 0, "users": 0,
                                       "accounts": 0, "banned_accounts": 0, "last": 0})
                a["n"] += _int(f.get("n"))
                a["users"] += 1
                a["last"] = max(a["last"], _int(f.get("last")))
        for aid, ent in _accounts.items():
            for k, f in (ent.get("f") or {}).items():
                if not isinstance(f, dict):
                    continue
                a = agg.setdefault(k, {"k": k, "zh": zh_of(k), "n": 0, "users": 0,
                                       "accounts": 0, "banned_accounts": 0, "last": 0})
                a["n"] += _int(f.get("n"))
                a["accounts"] += 1
                if aid in bid:
                    a["banned_accounts"] += 1
                a["last"] = max(a["last"], _int(f.get("last")))
    rows = sorted(agg.values(), key=lambda r: (-r["banned_accounts"], -r["n"]))
    return {"features": rows, "banned_total": len(bid)}


# ── 生命周期清理（删用户 / 删机位时同步丢弃，别留孤儿）──

def drop_account(acc_id: str) -> None:
    global _dirty
    with _lock:
        _load_once()
        if _accounts.pop(str(acc_id), None) is not None:
            _dirty = True


def drop_user(user_id: str) -> None:
    global _dirty
    with _lock:
        _load_once()
        if _users.pop(str(user_id), None) is not None:
            _dirty = True


def _total_of(store: dict) -> int:
    n = 0
    for ent in store.values():
        src = ent.get("f") if isinstance(ent, dict) else None
        if not isinstance(src, dict):
            continue
        for f in src.values():
            if isinstance(f, dict):
                n += _int(f.get("n"))
    return n


def stats() -> dict:
    """自检/运维用的一眼概况。"""
    flush(force=True)
    with _lock:
        _load_once()
        return {
            "accounts": len(_accounts),
            "users": len(_users),
            "events": _total_of(_accounts) + _total_of(_users),
            "features": len(FEATURES),
            "keep_days": KEEP_DAYS,
        }
