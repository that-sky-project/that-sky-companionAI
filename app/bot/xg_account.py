# -*- coding: utf-8 -*-
"""按号的东西（2026-11-08 T24-2）：当前号、账号库里的一项、按号的状态目录、能不能上线。

只依赖标准库。其它模块只从这里拿“当前号/账号信息/状态目录”，不各自去读
runtime/runtime-data/accounts.json（里面有会话和密钥，不往外给）。

- active_id()：control/active_account 里的账号 ID（读不到用当前号 5b12）。
- active_uuid()：当前号的 user_uuid。
- account_by_id(id)：账号库里的一项，只返回 id/user_uuid/alias/status/status_note。
- state_dir(id)：这个号的按号状态目录。5b12 还是 control/（保持现有文件位置），
  其它号是 control/accounts/<id>/（自动创建，权限 0700）。
- readiness(id)：这个号能不能上线（有没有能去的图、被封了没、和哥哥是不是好友）。
  地图目录由 TASK1 的 bot/account_catalog.py 提供：
    manifest-for <id> → 打印这个号该用的 manifest，退出码 3 = 没有专属目录；
    status <id>       → {"real_captures","synth_captures","catalog","manifest",...}。
  没有真录过 → ok=False，note 提示先用这个号在 MuMu 里录一张。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))
import xg_exec  # noqa: E402

ROOT = Path(os.environ.get("XG_DATA_DIR") or os.environ.get("SKY_AI_ROOT")
           or "/root/claudework/sky-ai")
try:
    # 安装版：代码在 app\、数据在 %LOCALAPPDATA%\XiaoGuang\data。统一用 xg_paths
    # 的 DATA/BOT_CODE/python_exe；服务器上这些常量仍然等于原路径。
    sys.path.insert(0, str(_APP))
    import xg_paths as _xg_paths
    ROOT = Path(_xg_paths.DATA)
    _PY_DEFAULT = Path(_xg_paths.python_exe())
    _BOT_DEFAULT = Path(_xg_paths.BOT_CODE)
except Exception:
    _PY_DEFAULT = ROOT / ".venv/bin/python"
    _BOT_DEFAULT = ROOT / "bot"
CONTROL = ROOT / "control"
ACCOUNTS = ROOT / "runtime/runtime-data/accounts.json"
PROFILES = ROOT / "runtime/private/connection0167_profiles.local.json"
OWNER_FILE = CONTROL / "owner_uuid"
ACTIVE_FILE = CONTROL / "active_account"

DEFAULT_ACCOUNT = os.environ.get("XG_DEFAULT_ACCOUNT") or ""   # 安装版没有历史默认号
DEFAULT_OWNER = os.environ.get("XG_DEFAULT_OWNER") or ""       # 由向导第 8 步写入

# 账号库/目录工具/好友查询用的解释器和脚本（测试可以用环境变量换成替身）
PY = Path(os.environ.get("SKY_PY") or _PY_DEFAULT)
CATALOG = Path(os.environ.get("SKY_ACCOUNT_CATALOG") or (_BOT_DEFAULT / "account_catalog.py"))
LOGIN = Path(os.environ.get("SKY_ACCOUNT_LOGIN") or (_BOT_DEFAULT / "account_login.py"))

ACCOUNT_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BAN_RE = re.compile(r"禁止登录.*?至\s*([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)")
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")

CATALOG_TTL = 60.0        # account_catalog 结果缓存（秒）：面板轮询时不反复起子进程
FRIENDS_TTL = 120.0       # 好友检查缓存（秒）：friends 要连服务器，别每刷一次都查
NO_CAPTURE_NOTE = ("这个号还没录过图：用这个号在 MuMu 里录一张（遇境最好），"
                   "录完会自动生成全部地图，大约 3 分钟")
FRIEND_NOTE = "新号还没加哥哥好友，好友传送和跟随会用不了"

_st = {"catalog": {}, "friends": {"key": None, "at": 0.0, "value": None}}


def _now():
    return time.monotonic()


def cache_clear():
    """清掉进程内缓存（测试/换号后强制重读用）。"""
    _st["catalog"].clear()
    _st["friends"].update(key=None, at=0.0, value=None)


# ---------- 账号库 ----------
def _items():
    try:
        data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data if isinstance(data, list) else (data.get("accounts", data) if isinstance(data, dict) else [])
    items = list(items.values()) if isinstance(items, dict) else items
    return [x for x in items if isinstance(x, dict)]


def active_id():
    try:
        v = ACTIVE_FILE.read_text().strip()
    except OSError:
        v = ""
    return v or DEFAULT_ACCOUNT


def active_uuid():
    acc = account_by_id(active_id())
    return (acc or {}).get("user_uuid") or None


def account_by_id(account_id):
    """账号库里的一项，只给界面上要显示的字段（会话/密钥不外传）。"""
    if not account_id:
        return None
    for a in _items():
        if a.get("id") == account_id:
            return {"id": a.get("id"), "user_uuid": a.get("user_uuid") or None,
                    "alias": a.get("alias") or "", "status": a.get("status") or "unknown",
                    "status_note": a.get("status_note") or ""}
    return None


def owner_uuid():
    try:
        v = OWNER_FILE.read_text().strip()
        return v if UUID_RE.fullmatch(v) else DEFAULT_OWNER
    except OSError:
        return DEFAULT_OWNER


def _ban_expired(until):
    for fmt in DATE_FORMATS:
        try:
            return time.mktime(time.strptime(until, fmt)) <= time.time()
        except ValueError:
            continue
    return False


def ban_status(acc):
    """(被封了没, 封到什么时候)。status_note 里有“禁止登录至 …”且没过期算被封；
    解析不出到期时间也按被封处理（宁可不让上，也别反复登录被封号）。"""
    note = (acc or {}).get("status_note") or ""
    if "禁止登录" not in note:
        return False, None
    m = BAN_RE.search(note)
    if not m:
        return True, None
    until = m.group(1)
    return (False if _ban_expired(until) else True), until


# ---------- 按号状态目录 ----------
def state_dir(account_id=None, root=None):
    """这个号的按号状态目录：5b12 = <root>/control/，其它号 = <root>/control/accounts/<id>/。

    只给合法账号 ID（字母数字下划线横线），避免拼出奇怪的路径。
    """
    aid = account_id or active_id()
    if not ACCOUNT_ID_RE.fullmatch(str(aid)):
        raise ValueError("账号 ID 不合法")
    base = Path(root) if root else ROOT
    if aid == DEFAULT_ACCOUNT:
        return base / "control"
    d = base / "control/accounts" / aid
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def recorded_maps(uuid):
    """这个号有录制（真录或合成）的地图 ID，升序。profiles 的 by_account 里有它就算。"""
    if not uuid:
        return []
    try:
        maps = (json.loads(PROFILES.read_text(encoding="utf-8")) or {}).get("maps") or {}
    except (OSError, ValueError):
        return []
    out = []
    for mid, e in maps.items():
        cap = (e or {}).get("current_capture") or {}
        if uuid in (cap.get("by_account") or {}) or cap.get("account") == uuid:
            try:
                out.append(int(mid))
            except (TypeError, ValueError):
                pass
    return sorted(out)


def real_recorded_maps(uuid):
    """这个号真录过（不是 synthesized）的图：by_account 里没有 synthesized 的，
    或最早的单号格式 current_capture.account==uuid 的（TASK1 找 donor 的同一套判定）。"""
    if not uuid:
        return []
    try:
        maps = (json.loads(PROFILES.read_text(encoding="utf-8")) or {}).get("maps") or {}
    except (OSError, ValueError):
        return []
    out = []
    for mid, e in maps.items():
        cap = (e or {}).get("current_capture") or {}
        own = (cap.get("by_account") or {}).get(uuid)
        real = (isinstance(own, dict) and own.get("record_dir") and not own.get("synthesized"))
        legacy = (own is None and cap.get("account") == uuid and cap.get("record_dir"))
        if real or legacy:
            try:
                out.append(int(mid))
            except (TypeError, ValueError):
                pass
    return sorted(out)


# ---------- account_catalog.py（TASK1） ----------
def _run(argv, stdin=None, timeout=60):
    try:
        r = xg_exec.run([str(PY), *argv], cwd=str(ROOT), input=stdin,
                        capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                v = json.loads(line)
                return v if isinstance(v, dict) else None
            except ValueError:
                continue
    return None


def catalog_status(account_id=None, force=False):
    """这个号的地图目录状态（account_catalog.py status 的 JSON）；读不到返回 {"_unavailable": True}。"""
    aid = account_id or active_id()
    hit = _st["catalog"].get(("status", aid))
    if hit and not force and _now() - hit[0] < CATALOG_TTL:
        return dict(hit[1])
    code, out, _err = _run([str(CATALOG), "status", aid])
    data = _last_json(out) if code == 0 else None
    data = dict(data) if isinstance(data, dict) else {"_unavailable": True}
    # 2026-11-08 Claude：account_catalog.py status 实际字段名是 real_recording_count / catalog_generated /
    # manifest_path（TASK1 实现），这里映射成本模块用的名字。
    if "_unavailable" not in data:
        data.setdefault("real_captures", data.get("real_recording_count") or 0)
        data.setdefault("synth_captures", data.get("synthesized_source_count") or 0)
        data.setdefault("catalog", bool(data.get("catalog_generated")))
        data.setdefault("manifest", data.get("manifest_path"))
    _st["catalog"][("status", aid)] = (_now(), dict(data))
    return dict(data)


def manifest_for(account_id=None, force=False):
    """这个号该用的目录 manifest 绝对路径；没有专属目录（退出码 3）或工具不可用返回 None。"""
    aid = account_id or active_id()
    hit = _st["catalog"].get(("manifest", aid))
    if hit and not force and _now() - hit[0] < CATALOG_TTL:
        return hit[1]
    code, out, _err = _run([str(CATALOG), "manifest-for", aid])
    path = None
    if code == 0:
        line = [x.strip() for x in (out or "").strip().splitlines() if x.strip()]
        if line:
            p = Path(line[-1])
            path = p if p.is_file() else None
    _st["catalog"][("manifest", aid)] = (_now(), path)
    return path


# ---------- 好友（哥哥在不在列表里） ----------
def friends_ok(account_id=None, force=False):
    """当前号的好友里有没有哥哥：True/False；查不了（不是当前号/读不到）返回 None。

    friends 子命令只能查 active 号，所以非当前号直接返回 None（切换后再查）。
    """
    aid = account_id or active_id()
    if aid != active_id():
        return None
    key = (aid, owner_uuid())
    c = _st["friends"]
    if not force and c.get("key") == key and _now() - c.get("at", 0.0) < FRIENDS_TTL:
        return c.get("value")
    code, out, _err = _run([str(LOGIN), "friends"], stdin="{}\n", timeout=30)
    data = _last_json(out) if code == 0 else None
    value = None
    if isinstance(data, dict) and data.get("ok"):
        value = bool(data.get("ownerIsFriend"))
    c.update(key=key, at=_now(), value=value)
    return value


# ---------- 能不能上线 ----------
def _maps_count(account_id, status):
    """可去张数 = 目录里的图 ∪ 这个号有录制（真录/合成）的图。

    不调 xg_runtime（面板轮询时别为一个数再起一轮子进程）；和 xg_runtime.startable_maps
    的规则一致：没目录就只算录制过的图。
    """
    uuid = (account_by_id(account_id) or {}).get("user_uuid")
    recorded = set(recorded_maps(uuid))
    man = status.get("manifest")
    entries = set()
    if status.get("catalog") and man:
        try:
            entries = {int(k) for k in
                       ((json.loads(Path(man).read_text(encoding="utf-8")) or {}).get("entries") or {})}
        except (OSError, ValueError, TypeError):
            entries = set()
    return len(entries | recorded)


def readiness(account_id=None):
    """{"ok": bool, "maps": 能去几张, "note": 中文说明}（另带 real/synth/catalog/friends 供排查）。"""
    aid = account_id or active_id()
    acc = account_by_id(aid)
    status = catalog_status(aid)
    try:
        real = int(status.get("real_captures") or 0)
    except (TypeError, ValueError):
        real = 0
    try:
        synth = int(status.get("synth_captures") or 0)
    except (TypeError, ValueError):
        synth = 0
    maps = _maps_count(aid, status)
    known = not status.get("_unavailable")        # account_catalog 没上线时别误判“没录过图”
    banned, until = ban_status(acc)
    notes, ok = [], maps > 0
    if acc is None:
        ok = False
        notes.append("账号库里没有这个号：先在后台“添加账号”把它登录进来")
    elif banned:
        ok = False
        notes.append(f"这个号被封了（禁止登录{('至 ' + until) if until else ''}），不能上线")
    elif known and real == 0 and not real_recorded_maps((acc or {}).get("user_uuid")):
        ok = False
        notes.append(NO_CAPTURE_NOTE)
    elif not known and maps <= 0:
        ok = False
        notes.append("读不到这个号的地图目录/录制情况：bot/account_catalog.py 不可用或没有返回")
    elif maps <= 0:
        notes.append(NO_CAPTURE_NOTE)
    f = friends_ok(aid) if aid == active_id() else None
    if f is False:
        notes.append(FRIEND_NOTE)
    return {"ok": bool(ok), "maps": int(maps), "note": "；".join(notes),
            "real": real, "synth": synth, "catalog": bool(status.get("catalog")),
            "friends": f}
