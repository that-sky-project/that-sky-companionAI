"""换图后“到达贴身”：带路人在新房间一有新鲜位置，就把落单的小光挪到他身边（每次换图一次）。

2026-11-06 实测：`/带我去风之试炼` 牵着小光换图，进图过场动画会把牵手强制松开，之后
哥哥在 (-0.47, 30.33, -36.88)、小光还在她 placement 的落点 (-11.77, 68.83, -119.98)，
水平差 84 米、高差 38 米，找不到也牵不回来。大多数图进图不会把玩家固定放到某个传送口，
所以每张图事先算的落点经常对不上玩家实际出现的位置。

做法：换图后最多等 ARRIVAL_TIMEOUT 秒，等到“带她换图的那个人”在新房间里的新鲜位置
（换图之后才观测到的；上一张图的旧位置不算），把小光直接放到他身后/侧面 ARRIVAL_SIDE 米，
高度用当前场景真实地形（查不到就用他的 y），不加任何站立偏移，朝向对着他。每次换图只挪一次。
正在牵手/背/抱、她的位置由附着关系决定时先不挪，等松开再判断。

escort 带路、跟随换图、重连回对方房间最后都会调用 PoseController.switch_map，所以挂在
switch_map 上就覆盖了所有换图路径。这里只用假 tracker / 假时钟就能跑，方便离线单测。
"""
import xg_paths
import math
import time
import uuid as uuidlib

ARRIVAL_TIMEOUT = 60.0      # 换图后最多等这么久（秒）
ARRIVAL_NEAR = 4.0          # 水平离带路人这么近就不挪（米）
ARRIVAL_HEIGHT = 2.0        # 高差超过这么多才挪（米）
ARRIVAL_SIDE = 1.2          # 挪到带路人身后/侧面这么远（米）
ARRIVAL_GROUND_LAYER = 2.0  # 查到的地面和带路人脚下差这么多以上，就当查错了，改用他的高度
ARRIVAL_STALE = 3.0         # 和换图前他最后的位置差不到这么多：多半是他客户端还没加载完、发的还是上一张图的坐标
ARRIVAL_STALE_GRACE = float("inf")   # 不再兜底接受旧坐标（2026-11-06 珍珠海域：他不在小光房间，20 秒后按他上一张图的坐标贴错了）
ARRIVAL_SETTLE = 1.0        # 他的位置要稳住这么久（秒）才贴：刚进图他还在落地，高度会往下掉（实测 30.47→30.32）
SETTLE_MOVE, SETTLE_DROP = 0.5, 0.05   # 稳住 = 水平挪动 < 0.5 米、高度变化 < 0.05 米
ARRIVAL_MAX_SNAPS = 3       # 60 秒窗口里最多贴这么多次（防止来回跳）
ATTACH_KINDS = ("hold", "carry")   # 位置由附着关系决定的互动：牵手 / 背背·公主抱


def _uuid(value):
    """只认标准 uuid 字符串；槽位号、空值都不算。"""
    if type(value) is not str:
        return None
    try:
        return value if str(uuidlib.UUID(value)) == value else None
    except (ValueError, AttributeError, TypeError):
        return None


def _yaw_heading(yaw):
    """6 位偏航码 → heading（度）。同 anim_pose.yaw_heading；这里不 import 它，免得把 qfsky 编解码带进单测。"""
    return (yaw + 0.5) / 63.0 * 360.0 - 180.0


def _heading_to(src, dst):
    """从 src 看 dst 的 heading（度，atan2(dx, dz)，和 anim_pose.heading_to 一致）。"""
    return math.degrees(math.atan2(dst[0] - src[0], dst[2] - src[2]))


def _target_uuid(ctl, target):
    """跟随目标可能是 uuid，也可能是 slot（十六进制/十进制）：统一换成 uuid——换图后 slot 会变，uuid 不会。"""
    who = _uuid(target)
    if who is not None or target is None:
        return who
    try:
        slot, _origin = ctl.tracker.resolve(target)
    except (LookupError, ValueError, TypeError, AttributeError):
        return None
    with ctl.tracker.lock:
        return ctl.tracker.slots.get(slot)


def leader_uuid(ctl):
    """这次换图带她的人：escort 记录的对方（不一定是 OWNER）→ 当前牵手/背背对象 → 当前跟随目标。"""
    own = _uuid(getattr(getattr(ctl, "tracker", None), "own_uuid", None))
    escort = getattr(ctl, "_escort_travel", None)
    current = getattr(escort, "current", None) if escort is not None else None
    candidates = [getattr(getattr(current, "binding", None), "speaker_uuid", None)]
    social = getattr(ctl, "social", None)
    if isinstance(social, dict):
        candidates.append(social.get("who"))
    follow = getattr(ctl, "follow", None)
    if isinstance(follow, dict):
        candidates.append(_target_uuid(ctl, follow.get("target")))
    for who in candidates:
        who = _uuid(who)
        if who is not None and who != own:
            return who
    return None


def arm(ctl, map_id, now=None):
    """switch_map 时调用（要在 reset 之前：reset 会清掉 social/follow）：记下带路人和原来的跟随。"""
    now = time.monotonic() if now is None else float(now)
    follow = getattr(ctl, "follow", None)
    leader = leader_uuid(ctl)
    keep = None
    if isinstance(follow, dict) and leader is not None and _target_uuid(ctl, follow.get("target")) == leader:
        try:
            distance = float(follow.get("distance", 1.5))
        except (TypeError, ValueError):
            distance = 1.5
        keep = {"target": leader, "distance": distance}    # 存 uuid：新房间的 slot 会变
    prev = None                          # 换图前他最后的位置：换图后头几帧他发的常常还是这个（2026-11-06 实测进图 0.7 秒时）
    tracker = getattr(ctl, "tracker", None)
    if leader is not None and tracker is not None:
        with tracker.lock:
            slot = next((s for s, who in (getattr(tracker, "slots", None) or {}).items() if who == leader), None)
            origin = (getattr(tracker, "origins", None) or {}).get(slot) if slot is not None else None
        if isinstance(origin, (tuple, list)) and len(origin) >= 3:
            prev = tuple(origin[:3])
    ctl.arrival = {"map_id": map_id, "leader": leader, "since": now,
                   "done": False, "follow": keep, "prev": prev}
    return ctl.arrival


def attached(ctl):
    """正在牵手/背/抱，而且她的位置由附着关系决定（每帧按对方坐标贴）：这时不该挪。"""
    social = getattr(ctl, "social", None)
    return isinstance(social, dict) and social.get("kind") in ATTACH_KINDS


def _here(ctl, now):
    """她现在的位置：正在走就用走路的实时位置，否则就是当前站位。"""
    motion = getattr(ctl, "motion", None)
    if motion is not None:
        try:
            return tuple(motion.pos(now))
        except Exception:
            pass
    return tuple(getattr(ctl, "position", (0.0, 0.0, 0.0)))


def _ground(ctl, x, z, near_y):
    """当前场景真实地形高度；查不到（几何没加载/点在网格外）返回 None。

    2026-11-06 起不再用 RoomTracker.ground_y 的“玩家踩点格子高度”。near_y 用来
    在双层地面里选离带路人脚下最近的那层；结果已经和带路人同一坐标基准，不再加偏移。
    """
    query = getattr(ctl, "terrain_ground", None)
    if not callable(query):
        return None
    try:
        return query(x, z, near_y)
    except Exception:
        return None


CONFIG = str(xg_paths.CONTROL / "arrival_snap.json")


def enabled():
    """开关：control/arrival_snap.json 的 enabled（没有这个文件时按开启，保持离线单测的原行为）。"""
    try:
        import json
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f).get("enabled", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def _skip(ctl, st, now, reason):
    st["done"] = True
    ctl.log("arrival_snap_skipped", map_id=st["map_id"], leader=(st.get("leader") or "")[:8],
            reason=reason, waited_seconds=round(now - st["since"], 1))
    return "skipped"


def _snap(ctl, st, target, heading, here, distance, now):
    ctl.motion = None                    # 正在走的那段作废：位置已经直接改掉了
    ctl.route = []
    ctl.after_arrive = None
    ctl.position = tuple(target)
    ctl.heading = heading
    if isinstance(getattr(ctl, "state", None), dict) and ctl.state.get("pose") == "walk":
        ctl.state = {"pose": "idle"}
    follow = st.get("follow")
    if follow is not None and getattr(ctl, "follow", None) is None and getattr(ctl, "social", None) is None:
        ctl.follow = dict(follow)        # 换图前正跟着他：挪完接着跟（reset 本来会把 follow 清掉）
    set_override = getattr(ctl, "_set_override", None)
    if callable(set_override):
        set_override(getattr(ctl, "base", None))   # 位置变了：把后处理挂回去，否则发的还是落点种子
    st["snaps"] = st.get("snaps", 0) + 1
    st["cand"] = None                    # 窗口内继续盯：他之后又被吹走/传走，再等他站稳贴一次
    st["done"] = st["snaps"] >= ARRIVAL_MAX_SNAPS
    ctl.log("arrival_snap", map_id=st["map_id"], leader=st["leader"][:8],
            **{"from": [round(v, 3) for v in here], "to": [round(v, 3) for v in target],
               "distance": round(distance, 2), "waited_seconds": round(now - st["since"], 1)})
    return "snapped"


def tick(ctl, now=None):
    """每秒一次：换图后 60 秒内，带路人站稳后离她远了就贴身挪过去（最多 ARRIVAL_MAX_SNAPS 次）。

    返回 "waiting"（还在等）/ "attached"（附着中，先不动）/ "close"（挨着，继续盯）/ "snapped" / "skipped" / None（没在等）。
    """
    st = getattr(ctl, "arrival", None)
    if not isinstance(st, dict) or st.get("done"):
        return None
    now = time.monotonic() if now is None else float(now)
    if not enabled():
        # 2026-11-07 哥哥：取消“进图 60 秒内瞬移到身边”。只保留换图前的跟随，不再挪位置。
        st["done"] = True
        follow = st.get("follow")
        if follow is not None and getattr(ctl, "follow", None) is None and getattr(ctl, "social", None) is None:
            ctl.follow = dict(follow)
        ctl.log("arrival_snap_skipped", map_id=st["map_id"], leader=(st.get("leader") or "")[:8],
                reason="disabled", waited_seconds=round(now - st["since"], 1))
        return "skipped"
    if st.get("leader") is None:
        return _skip(ctl, st, now, "no_leader")
    if now - st["since"] >= ARRIVAL_TIMEOUT:
        if st.get("snaps"):
            st["done"] = True            # 贴过了：窗口结束，不再记跳过
            return None
        return _skip(ctl, st, now, "attached" if attached(ctl) else
                     "already_close" if st.get("close") else "timeout_no_fresh_position")
    if attached(ctl):
        return "attached"                # 等附着结束（比如过场动画强制松手）再判断
    tracker = ctl.tracker
    with tracker.lock:
        ready = bool(getattr(tracker, "ready", False))
        map_id = getattr(tracker, "confirmed_map_id", None)
        slots = dict(getattr(tracker, "slots", None) or {})
        origins = dict(getattr(tracker, "origins", None) or {})
        observed = dict(getattr(tracker, "position_observed", None) or {})
        yaws = dict(getattr(tracker, "yaws", None) or {})
    if not ready or map_id != st["map_id"]:
        return "waiting"                 # 新房间还没起来
    slot = next((s for s, who in slots.items() if who == st["leader"]), None)
    origin = origins.get(slot) if slot is not None else None
    seen = observed.get(slot) if slot is not None else None
    if (not isinstance(origin, (tuple, list)) or len(origin) < 3
            or not isinstance(seen, (tuple, list)) or len(seen) < 2
            or not seen[1] > st["since"]):
        return "waiting"                 # 人还没进新房间 / 只有上一张图的旧位置
    prev = st.get("prev")
    if (prev is not None and math.dist(tuple(origin[:3]), prev) < ARRIVAL_STALE
            and now - st["since"] < ARRIVAL_STALE_GRACE):
        return "waiting"                 # 还是换图前的坐标：他客户端还在加载，等真正的新位置
    cand = st.get("cand")                # 等他站稳：落地前的高度会让小光浮在空中
    if (cand is None or math.hypot(origin[0] - cand[0][0], origin[2] - cand[0][2]) >= SETTLE_MOVE
            or abs(origin[1] - cand[0][1]) >= SETTLE_DROP):
        st["cand"] = (tuple(origin[:3]), now)
        if ARRIVAL_SETTLE > 0:
            return "waiting"
    elif now - cand[1] < ARRIVAL_SETTLE:
        return "waiting"
    here = _here(ctl, now)
    dx, dz = here[0] - origin[0], here[2] - origin[2]
    distance = math.hypot(dx, dz)
    if distance <= ARRIVAL_NEAR and abs(here[1] - origin[1]) <= ARRIVAL_HEIGHT:
        st["close"] = True               # 现在挨着，但窗口内继续盯（2026-11-06 月牙绿洲：进图在云里挨着，
        return "close"                   # 他松手后被风吹到落脚点，小光不会被吹，留在云里）
    yaw = yaws.get(slot)
    if type(yaw) is int:
        r = math.radians(_yaw_heading(yaw))
        x, z = origin[0] - math.sin(r) * ARRIVAL_SIDE, origin[2] - math.cos(r) * ARRIVAL_SIDE   # 他身后 1.2 米
    else:
        d = max(distance, 1e-6)
        x, z = origin[0] + dx / d * ARRIVAL_SIDE, origin[2] + dz / d * ARRIVAL_SIDE              # 没朝向：站他朝她这侧
    ground = _ground(ctl, x, z, origin[1])
    y = ground if ground is not None and abs(ground - origin[1]) <= ARRIVAL_GROUND_LAYER else origin[1]
    target = (x, y, z)                   # 真实地形/他脚下高度：都是玩家坐标，同基准；不加旧版 ground_offset
                                         # （2026-11-06 风之试炼：加 0.15 浮空；走路模块落在 30.33 = 哥哥脚下）
    return _snap(ctl, st, target, _heading_to(target, origin), here, distance, now)
