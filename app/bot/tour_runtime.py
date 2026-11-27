"""T70 巡游模式：小光带主人按顺序跑完所有图，一张跑完自动带去下一张。

只用已有的两条路：
  - 图内：一律能飞就飞（主人 11-19 定）：朝路点每次 fly_to 一段 20 m，飞不起来缩到 10 m，
    还不行才 walk 一段再回到飞；
    路点来自 control/tour_routes.json（本机 tour_build.py 由 TerrainStudio 蜡烛/暗植坐标生成）；
  - 图间：合成一条主人的“/带我去<下一张>”，走 travel_chat_v25.Router.route 原样的带路校验与
    牵手 + ChangeLevel 换图（不新增任何传送方式）。
等主人：换图后等主人出现在新房间；跑图时主人离小光 >30 m 就停下等，回到 12 m 内再走。
命令（只认主人，带“/”）：/巡游 /巡游全部 /从XX开始巡游 /停下(/暂停巡游) /继续跑(/继续巡游) /跳过 /停止巡游 /巡游到哪了
开关：环境变量 CLAUDEWORK_TOUR=0 时整个命令不接（默认开）。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path

SCHEMA = "xg-tour-routes-v1"
ARRIVE_M = 0.5            # 离路点水平 0.5 m 内算到了（主人 11-20：要靠近花和蜡烛）
ARRIVE_STUCK_M = 1.5      # 实在落不到点上时，1.5 m 内也算到
OWNER_GONE_S = 5.0        # 主人不在小光房间名单里这么久：暂停并提示传送
DWELL_S = float(os.environ.get("CLAUDEWORK_TOUR_DWELL", "0"))   # 每个点落地后停几秒（主人 11-20 晚：不用停，默认 0）
RELEASE_DEBOUNCE_S = 1.5  # 连续这么久没牵着才算主人松手（角色帧偶尔断一下不算）
INVITE_EVERY_S = 20.0
APPROACH_EVERY_S = 3.0    # 主人松手后，每隔这么久按他的新位置重新飞过去
APPROACH_NEAR_M = 3.0     # 已在主人这么近就不再飞     # 主人松手后，小光跟在身边每隔这么久发一次牵手邀请
OWNER_NEAR_M = 12.0       # 主人回到这么近再接着走
OWNER_WAIT_S = 180.0      # 等主人跟上最多等这么久，超时就暂停巡游
OWNER_ABSENT_S = 60.0     # 主人在房间里消失/坐标不新鲜这么久 → 暂停
MAP_WAIT_S = 200.0        # 带路提交后等换图（带路命令本身 180 s 过期）
OWNER_JOIN_S = 120.0      # 换图后等主人进新房间
NOT_MOVING_S = 3.0        # 发了一段还没动多久算这一段没进展
START_GRACE_S = 2.5       # 发命令后先给地形层准备的时间
PROGRESS_M = 1.0          # 一段走完离路点近了这么多就算有进展，直接发下一段
BREAKPOINT_TOL_M = 8.0    # 继续跑时离断点这么近就不专门回去
STEP_M = 20.0             # 每次朝路点最多飞这么远
STEP_SHORT_M = 10.0       # 20 m 飞不起来时缩短到这么远
MAX_STALLS = 6            # 连续这么多段没进展（飞/短飞/走轮流两轮）就跳过这个点
WALK_SPEED = 3.5
TRAVEL_TRIES = 2
POLL_S = 0.5

START_WORDS = ("巡游", "开始巡游", "带我巡游", "带我跑图", "巡游吧", "去巡游")
ALL_WORDS = ("巡游全部", "全部巡游", "巡游所有图", "全图巡游")
STOP_WORDS = ("停止巡游", "结束巡游", "不巡游了", "别巡游了", "巡游结束")
PAUSE_WORDS = ("暂停巡游", "巡游暂停", "先停一下巡游")
RESUME_WORDS = ("继续巡游", "巡游继续", "接着巡游")
# 口语说法：只在巡游进行中才接（没巡游时“停下”仍是聊天里原来的停下动作）
PAUSE_SOFT = ("停下", "停一下", "先停下", "停", "等一下", "等等", "等等我", "先等等")
RESUME_SOFT = ("继续跑", "接着跑", "继续走", "接着走", "继续", "走吧")
SKIP_WORDS = ("跳过", "跳过这张", "跳过这张图", "下一张", "下一张图", "换下一张")
STATUS_WORDS = ("巡游到哪了", "巡游状态", "巡游进度", "到哪了")
FROM_RE = re.compile(r"^(?:从|由)(?P<name>.{1,24}?)(?:开始)?(?:巡游|跑图)(?P<all>全部)?$|^巡游(?P<all2>全部)?从(?P<name2>.{1,24}?)开始$")
TAIL = re.compile(r"[\s～~!！。.…?？,，吧呀啦嘛哦呢]+$")


def enabled():
    return os.environ.get("CLAUDEWORK_TOUR", "0") == "1"  # 客户端默认关闭巡游（哥哥 11-28：exe 不带巡游）


def parse(content):
    """返回 (op, arg) 或 None。op: start/stop/pause/resume/skip/status。"""
    text = TAIL.sub("", (content or "").strip())
    text = re.sub(r"^(?:小光|光光)[\s，,、：:]*", "", text)
    if not text or len(text) > 40:
        return None
    if text in START_WORDS:
        return ("start", {"all": False, "from": None})
    if text in ALL_WORDS:
        return ("start", {"all": True, "from": None})
    m = FROM_RE.match(text)
    if m:
        name = (m.group("name") or m.group("name2") or "").strip()
        return ("start", {"all": bool(m.group("all") or m.group("all2")), "from": name})
    for words, op in ((STOP_WORDS, "stop"), (PAUSE_WORDS, "pause"), (RESUME_WORDS, "resume"),
                      (SKIP_WORDS, "skip"), (STATUS_WORDS, "status")):
        if text in words:
            return (op, None)
    if text in PAUSE_SOFT:
        return ("pause", "soft")
    if text in RESUME_SOFT:
        return ("resume", "soft")
    return None


def load_routes(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA or not isinstance(doc.get("maps"), list):
        raise ValueError("tour_routes_schema")
    for m in doc["maps"]:
        if (not isinstance(m.get("scene"), str) or type(m.get("map_id")) is not int
                or not isinstance(m.get("travel_query"), str) or not isinstance(m.get("waypoints"), list)):
            raise ValueError("tour_routes_row")
        for p in m["waypoints"]:
            if not (isinstance(p, list) and len(p) == 3 and all(
                    type(v) in (int, float) and math.isfinite(v) for v in p)):
                raise ValueError("tour_routes_point")
    return doc


def _key(s):
    return re.sub(r"[\s·・:：()（）\-_]+", "", str(s or "")).casefold()


def select_plan(doc, *, include_all=False, start_from=None, current_map=None):
    """按顺序挑要去的图；start_from 可以是图名/中文名/区域名。返回 (plan, error)。"""
    maps = [m for m in doc["maps"] if include_all or m.get("default", True) or m.get("auto_when_event")]
    if start_from:
        k = _key(start_from)
        idx = None
        for i, m in enumerate(maps):
            names = {_key(m["scene"]), _key(m.get("label")), _key(m.get("travel_query")),
                     _key(m.get("note")), _key(m.get("region"))}
            if k in names or (len(k) >= 2 and any(k and k in n for n in names if n)):
                idx = i
                break
        if idx is None:
            # 点名的是默认不去的图：从全部清单里找，找到就把它插进来
            for m in doc["maps"]:
                names = {_key(m["scene"]), _key(m.get("label")), _key(m.get("travel_query"))}
                if k in names:
                    allm = [x for x in doc["maps"] if include_all or x.get("default", True) or x.get("auto_when_event") or x is m]
                    return allm[allm.index(m):], None
            return None, "unknown_start"
        return maps[idx:], None
    if current_map is not None:
        for i, m in enumerate(maps):
            if m["map_id"] == current_map:
                return maps[i:], None
    return maps, None


# ---------------------------------------------------------------- 季节蜡烛 / 宝藏大蜡烛（按活动时间表）
FLY_H, FLY_UP, FLY_DOWN, STOP_S = 8.0, 3.5, 12.0, 1.5
COVER_R, COVER_DY = 2.5, 2.0


def leg_time(a, b):
    """同 tour_build：爬升、平飞、下降依次进行，时间相加。"""
    h = math.hypot(b[0] - a[0], b[2] - a[2])
    dy = b[1] - a[1]
    return h / FLY_H + (dy / FLY_UP if dy > 0 else -dy / FLY_DOWN) + STOP_S


def active_events(schedule):
    """活动时间表（/account/get_event_schedule 的 server_time/base_time/events）→ 此刻进行中的活动名集合。"""
    now, base = schedule.get("server_time"), schedule.get("base_time")
    if type(now) is not int or type(base) is not int or not isinstance(schedule.get("events"), list):
        raise ValueError("event_schedule_schema")
    out = set()
    for e in schedule["events"]:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str):
            continue
        try:
            dur = int(e.get("duration")) * 60
        except (TypeError, ValueError):
            continue
        for off in e.get("when") or []:
            if type(off) is int and base + off <= now < base + off + dur:
                out.add(e["name"])
    return out



def _t103_route(m):
    return "flower_parts" in (m.get("source") or {})


def _t103_event_map(m, active):
    """活动站只在当期加入，不拆开同一株的部件，也不把活动蜡烛归到花站。"""
    points = [list(p) for p in m["waypoints"]]
    detail = [dict(row) for row in m["waypoint_detail"]]
    if len(detail) != len(points):
        raise ValueError("路线与站点说明数量不一致")
    added = 0
    for name, group in (m.get("event_candles") or {}).items():
        if name not in active:
            continue
        refs = (m.get("event_candle_detail") or {}).get(name, ())
        for j, p in enumerate(group):
            ref = dict(refs[j], event=name)
            near = next((i for i,q in enumerate(points) if detail[i].get("kind") != "soultrap"
                         and math.dist(q,p) <= 0.5), None)
            if near is not None:
                d = detail[near]
                d["active_event_refs"] = [*d.get("active_event_refs", ()), ref]
                continue
            options = []
            for i in range(len(points)+1):
                if 0 < i < len(points):
                    flower = detail[i-1].get("flower_index")
                    if flower is not None and detail[i].get("flower_index") == flower:
                        continue
                a = points[i-1] if i else m["spawn"]
                cost = leg_time(a,p)
                if i < len(points):
                    cost += leg_time(p,points[i])-leg_time(a,points[i])
                options.append((cost,i))
            i = min(options)[1]
            points.insert(i,list(p))
            detail.insert(i,{"kind":"event_candle", "candles":0, "soultraps":0,
                             "active_event_refs":[ref]})
            added += 1
    return dict(m, waypoints=points, waypoint_detail=detail), added

def merge_event_candles(m, active):
    """把这张图此刻亮着的季节/宝藏蜡烛插进路线：已在某站 2.5 m 内的不重复加，其余按飞行用时最便宜的位置插入。"""
    if _t103_route(m):
        return _t103_event_map(m, active)
    extra = []
    for name, pts in (m.get("event_candles") or {}).items():
        if name in active:
            extra += [list(p) for p in pts]
    if not extra:
        return m, 0
    wps = [list(p) for p in m["waypoints"]]
    added = 0
    for p in extra:
        if any(hdist(w, p) <= COVER_R and abs(w[1] - p[1]) <= COVER_DY for w in wps):
            continue
        seq = [m["spawn"]] + wps
        best = None
        for i in range(len(seq)):
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            cost = leg_time(seq[i], p) + ((leg_time(p, nxt) - leg_time(seq[i], nxt)) if nxt is not None else 0.0)
            if best is None or cost < best[0]:
                best = (cost, i)
        wps.insert(best[1], p)
        added += 1
    out = dict(m)
    out["waypoints"] = wps
    return out, added


def hdist(a, b):
    return math.hypot(a[0] - b[0], a[2] - b[2])


class Stop(Exception):
    pass


class Skip(Exception):
    pass


class TourController:
    """线程化巡游。所有外部动作都经注入的函数，便于离线测试。

    read_state() -> pose.state.json 的 dict
    control(cmd) -> 写 pose.json
    say(text)    -> 对主人说话
    travel(m)    -> (accepted: bool, reply: str|None, reason: str)
    keepalive()  -> 让“自己逛”别插进来
    """

    def __init__(self, doc, *, owner, read_state, control, say, travel, log,
                 keepalive=lambda: None, clock=time.monotonic, sleep=time.sleep,
                 state_path=None, home=None, route_mode=lambda: False):
        self.doc, self.owner = doc, owner
        self.read_state, self.control, self.say = read_state, control, say
        self.travel, self.log, self.keepalive = travel, log, keepalive
        self.clock, self.sleep = clock, sleep
        self.route_mode = route_mode
        self.state_path = Path(state_path) if state_path else None
        self.home = home if home is not None else doc.get("home")
        self.lock = threading.RLock()
        self.thread = None
        self.plan, self.index, self.wp_index = [], 0, 0
        self.phase = "idle"
        self._stop = self._skip = self._paused = False
        self.pause_reason = None
        self.done_maps, self.skipped_maps = [], []
        self.started_at = None

    # ---------------- 主人命令 ----------------
    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, plan):
        with self.lock:
            if self.active:
                return False
            self.plan, self.index, self.wp_index = list(plan), 0, 0
            self._stop = self._skip = self._paused = False
            self.pause_reason = None
            self.done_maps, self.skipped_maps = [], []
            self._breakpoint = None
            self.started_at = time.time()
            self.phase = "starting"
            self.thread = threading.Thread(target=self._run, name="xg-tour", daemon=True)
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            self._stop = True

    def skip(self):
        with self.lock:
            self._skip = True
            self._paused = False

    def pause(self, reason="owner"):
        with self.lock:
            self._paused, self.pause_reason = True, reason
        if reason == "owner":
            self.control({"pose": "stop", "src": "tour"})   # 先停稳，断点由巡游线程随后按停下的位置记
        self._write_status()

    def resume(self):
        with self.lock:
            self._paused, self.pause_reason = False, None
        self._write_status()

    def status_text(self):
        with self.lock:
            if not self.active:
                return "现在没在巡游～说 /巡游 就出发"
            m = self.plan[self.index] if self.index < len(self.plan) else None
            if m is None:
                return "巡游快结束啦，正在回遇境"
            total = len(m["waypoints"])
            where = f"第{self.index + 1}/{len(self.plan)}张：{m['label']}"
            if self.phase == "run":
                where += f"，第{min(self.wp_index + 1, total)}/{total}个点"
            elif self.phase == "travel":
                where += "，正在带你过去"
            if self._paused:
                where += "（停着呢，说 /继续跑）"
            return where

    # ---------------- 内部 ----------------
    def _write_status(self):
        if self.state_path is None:
            return
        with self.lock:
            m = self.plan[self.index] if self.index < len(self.plan) else None
            doc = {"active": self.active or self.phase not in ("idle", "finished", "stopped"),
                   "phase": self.phase, "paused": self._paused, "pause_reason": self.pause_reason,
                   "index": self.index, "total": len(self.plan),
                   "scene": m and m["scene"], "label": m and m["label"],
                   "waypoint": self.wp_index, "waypoints": m and len(m["waypoints"]),
                   "done": list(self.done_maps), "skipped": list(self.skipped_maps),
                   "route_revision": self.doc.get("route_revision"),
                   "plan_scenes": [x["scene"] for x in self.plan],
                   "cur_waypoints": m and [list(p) for p in m["waypoints"]],
                   "started_at": self.started_at, "updated_at": time.time()}
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    def _check(self):
        """每个等待点都调：停止/跳过立即生效；暂停就原地等。暂停过返回 True。"""
        was_paused = False
        while True:
            with self.lock:
                if self._stop:
                    raise Stop()
                if self._skip:
                    self._skip = False
                    raise Skip()
                paused = self._paused
            if not paused:
                return was_paused
            if not was_paused:
                self._note_breakpoint()
            was_paused = True
            self.keepalive()
            self.sleep(POLL_S)

    def _note_breakpoint(self):
        """刚停下时记下断点（图 + 小光位置）。回断点途中再停，不覆盖最初的断点。"""
        if self.phase != "run" or getattr(self, "_breakpoint", None) is not None:
            return
        try:
            self.control({"pose": "stop", "src": "tour"})     # 补发一次：防止和刚发出的下一段撞车
            self.sleep(1.0)                    # 等停止命令生效（飞行中会先刹停），再读停下的位置
            st, room = self._room()
            me, _ = self._self_pos(st)
        except Exception:
            return
        if me is not None and room.get("map_id") is not None:
            self._breakpoint = {"map_id": room.get("map_id"), "pos": [float(v) for v in me[:3]],
                                "waypoint": self.wp_index}
            self.log("tour_breakpoint", map_id=room.get("map_id"), waypoint=self.wp_index,
                     pos=[round(v, 1) for v in me[:3]])

    def _return_to_breakpoint(self, m):
        """继续跑：小光被挪走/换了图，先回到停下的那个点，再接着跑原来的路点。"""
        bp = getattr(self, "_breakpoint", None)
        if bp is None:
            return
        st, room = self._room()
        me, _ = self._self_pos(st)
        if (self._map_ok(room, bp["map_id"]) and me is not None
                and hdist(me, bp["pos"]) <= BREAKPOINT_TOL_M):
            self._breakpoint = None
            return
        self.log("tour_return_breakpoint", scene=m["scene"], waypoint=bp["waypoint"])
        self.say("我先回到刚才停下的地方～")
        if not self._map_ok(room, m["map_id"]):
            self._go(m)                                  # 被带去别的图了：先带回这张图
        self._walk_point(m, bp["pos"], returning=True)
        self._breakpoint = None

    def _wait(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            self._check()
            self.keepalive()
            self.sleep(min(POLL_S, max(0.0, end - self.clock())))

    def _room(self):
        st = self.read_state() or {}
        room = st.get("room") if isinstance(st.get("room"), dict) else {}
        return st, room

    def _map_ok(self, room, map_id):
        return room.get("connected") is True and room.get("map_id") == map_id

    def _owner_pos(self, room):
        slots = room.get("slots") or {}
        players = room.get("players") or {}
        for slot, uid in slots.items():
            if uid == self.owner:
                pos = ((players.get(slot) or {}).get("position") or {})
                v = pos.get("value")
                if pos.get("freshness") == "fresh" and isinstance(v, list) and len(v) >= 3:
                    return v
        return None

    @staticmethod
    def _self_pos(st):
        me = st.get("self") if isinstance(st.get("self"), dict) else {}
        p = me.get("position")
        return (p if isinstance(p, list) and len(p) >= 3 else None), bool(me.get("moving"))

    def _pause_and_wait(self, reason, text):
        self.log("tour_paused", reason=reason)
        self.say(text)
        self.pause(reason)
        self._check()        # 停在这里直到 /继续巡游、/跳过 或 /停止巡游

    def _run(self):
        try:
            self.say(f"巡游出发！一共{len(self.plan)}张图，跟紧我～"
                     "（/停下 等你拿，/继续跑 接着走，/跳过 换下一张，/停止巡游 结束）")
            self.log("tour_started", maps=[m["scene"] for m in self.plan])
            while self.index < len(self.plan):
                m = self.plan[self.index]
                try:
                    self._do_map(m)
                    self.done_maps.append(m["scene"])
                except Skip:
                    self._breakpoint = None       # 跳过了这张图，断点作废
                    self.log("tour_skip", scene=m["scene"])
                    self.skipped_maps.append(m["scene"])
                    self.control({"pose": "stop", "src": "tour"})
                with self.lock:
                    self.index += 1
                    self.wp_index = 0
                self._write_status()
            self.phase = "home"
            self._write_status()
            if self.home:
                try:
                    self._go(self.home, label="遇境")
                except Skip:
                    pass
            self.phase = "finished"
            self.say(f"巡游走完啦！跑了{len(self.done_maps)}张图"
                     + (f"，跳过{len(self.skipped_maps)}张" if self.skipped_maps else "") + "～")
            self.log("tour_finished", done=len(self.done_maps), skipped=len(self.skipped_maps))
        except Stop:
            self.phase = "stopped"
            self.control({"pose": "stop", "src": "tour"})
            self.say("好，巡游先停在这儿～")
            self.log("tour_stopped", index=self.index)
        except Exception as exc:  # 不让线程悄悄死掉
            self.phase = "stopped"
            self.log("tour_crashed", error=repr(exc)[:200])
            self.say("巡游出了点问题，先停下了")
        finally:
            self._write_status()

    def _go(self, m, *, label=None):
        """带主人去 m（已经在这张图就不动）。失败重试一次，再失败就暂停等主人决定。"""
        label = label or m.get("label") or m["scene"]
        _st, room = self._room()
        if self._map_ok(room, m["map_id"]):
            return
        self.phase = "travel"
        self._write_status()
        while True:
            for attempt in range(1, TRAVEL_TRIES + 1):
                self._check()
                # 带路要求主人此刻在同一房间、坐标新鲜：先等主人在
                self._wait_owner_present(OWNER_JOIN_S, why="before_travel")
                accepted, reply, reason = self.travel(m)
                self.log("tour_travel", scene=m["scene"], attempt=attempt, accepted=accepted, reason=reason)
                if accepted:
                    self.say(f"下一站：{label}，牵好我～")
                    if self._wait_map(m["map_id"]):
                        return
                    reason = "map_not_changed"
                elif reply:
                    self.say(reply)
                self._wait(8)
            self._pause_and_wait("travel_failed",
                                 f"去{label}没接上（{reason}）。说 /继续跑 再试，/跳过 换下一张")

    def _wait_map(self, map_id):
        end = self.clock() + MAP_WAIT_S
        while self.clock() < end:
            self._check()
            self.keepalive()
            _st, room = self._room()
            if self._map_ok(room, map_id):
                return True
            self.sleep(POLL_S)
        return False

    def _wait_owner_present(self, seconds, why):
        end = self.clock() + seconds
        told = False
        while True:
            self._check()
            _st, room = self._room()
            if self._owner_pos(room) is not None:
                return True
            if self.clock() >= end:
                self._pause_and_wait("owner_absent_" + why,
                                     "我这边看不到你啦，可能我掉线换了房间～从好友列表传送到我这儿，再说 /继续跑")
                end = self.clock() + seconds
                continue
            if not told and self.clock() > end - seconds + 20:
                told = True
                self.say("我在这儿等你进来～")
            self.keepalive()
            self.sleep(POLL_S)

    def _do_map(self, m):
        self._go(m)
        self.phase = "arrive"
        self._write_status()
        self._wait_owner_present(OWNER_JOIN_S, why="arrive")
        self._wait(2.0)
        wps = m["waypoints"]
        self.say(f"到{m['label']}啦！这张有{len(wps)}个点，牵好我～")
        self.phase = "run"
        self._ensure_hand(m, first=True)
        for k, wp in enumerate(wps):
            with self.lock:
                self.wp_index = k
            self._write_status()
            ok = self._walk_point(m, wp)
            self.log("tour_waypoint", scene=m["scene"], index=k, ok=ok)
            if ok and DWELL_S > 0:
                self._wait(DWELL_S)               # 每株花、每簇蜡烛落地后停一下（主人 11-20）
        self.control({"pose": "stop", "src": "tour"})
        self.log("tour_map_done", scene=m["scene"], waypoints=len(wps))

    def _step_cmd(self, mode, me, wp):
        """朝路点发一段：远的路点切成小目标（长直线航线会撞地形被否；地面层一次也只走几米）。
        mode: fly（20 m 一段）/ fly_short（10 m 一段）/ walk。"""
        to = [float(v) for v in wp]
        if mode == "fly":
            # 直接给蜡烛点：room_pose 自己分段、在空中接下一段，一路飞到不落地（T70-3）
            return {"pose": "fly_to", "to": [round(v, 3) for v in to], "src": "tour"}
        step = STEP_SHORT_M if mode == "fly_short" else STEP_M
        if me is not None:
            d = hdist(me, wp)
            if d > step:
                f = step / d
                to = [me[0] + (wp[0] - me[0]) * f, me[1] + (wp[1] - me[1]) * f,
                      me[2] + (wp[2] - me[2]) * f]
        to = [round(v, 3) for v in to]
        if mode in ("fly", "fly_short"):
            return {"pose": "fly_to", "to": to, "src": "tour"}
        return {"pose": "walk", "to": to, "ground": True, "src": "tour"}

    def _walk_point(self, m, wp, returning=False):
        """去一个路点：巡游一律能飞就飞（主人 11-19 定：直接飞到蜡烛那儿）。

        每次朝路点飞一段（20 m）；停下时比上一段起点更近就立刻接着发下一段（不算失败）；
        这一段飞不起来就缩成 10 m 再飞，还不行才在地上走一段（走完回到飞）；
        轮了几次都没进展才跳过这个点（离路点 4 m 内算到了）。"""
        prefer_fly = True
        modes = ["fly", "fly_short", "walk"]
        mode_i, stalls = 0, 0
        st, room = self._room()
        me, _moving = self._self_pos(st)
        budget = (hdist(me, wp) if me else 60.0) / WALK_SPEED * 3.0 + 30.0
        self.log("tour_move", scene=m["scene"], mode="fly_first" if prefer_fly else "walk_first",
                 dist=None if me is None else round(hdist(me, wp), 1))
        state = {}

        def issue(me_now):
            cmd = self._step_cmd(modes[mode_i], me_now, wp)
            with self.lock:
                blocked = self._paused or self._stop
            if not blocked:                      # 刚被 /停下：不能再发移动（_check 下一轮会停住，恢复时重发）
                self.control(cmd)
            state.update(cmd=cmd, at=self.clock(), from_d=(hdist(me_now, wp) if me_now else None),
                         seen_moving=False)

        issue(me)
        end = self.clock() + budget
        while True:
            resumed = self._check()
            if resumed or (not returning and getattr(self, "_breakpoint", None) is not None):
                # 暂停后恢复（也包括在“等主人”时停下的）：先回断点，再从当前位置接着发；不算没进展
                self.log("tour_resumed", scene=m["scene"], index=self.wp_index)
                if not returning:
                    self._return_to_breakpoint(m)
                st, room = self._room()
                issue(self._self_pos(st)[0])
                end = self.clock() + budget
                continue
            self.keepalive()
            self.sleep(POLL_S)
            st, room = self._room()
            if not self._map_ok(room, m["map_id"]):
                # 图被换了（主人另外喊了带路、掉线搬房间等）：回到这张图再继续
                self.log("tour_map_lost", scene=m["scene"], now=room.get("map_id"))
                self._go(m)
                st, room = self._room()
                issue(self._self_pos(st)[0])
                end = self.clock() + budget
                continue
            me, moving = self._self_pos(st)
            flying = bool((st.get("self") or {}).get("flying"))
            now = self.clock()
            strict = not returning and _t103_route(m)
            distance = (math.dist(me[:3], wp) if strict else hdist(me, wp)) if me is not None else math.inf
            if distance <= ARRIVE_M and not flying:
                return True                       # 落了地、站在蜡烛/花边上才算到
            owner = self._owner_pos(room)
            if self._owner_gone(m, room, state):
                st, room = self._room()
                issue(self._self_pos(st)[0])
                end = self.clock() + budget
                continue
            if self._hand_state(st) == "leading":
                state["unheld_since"] = None
            elif state.get("unheld_since") is None:
                state["unheld_since"] = now
            elif now - state["unheld_since"] >= RELEASE_DEBOUNCE_S:
                # 主人松手了（持续 1.5 s 没牵着）：停下，跟到主人身边，等主人重新牵手再接着巡游
                state["unheld_since"] = None
                self._ensure_hand(m)
                st, room = self._room()
                issue(self._self_pos(st)[0])
                end = self.clock() + budget
                continue
            if owner is None and now - getattr(self, "_owner_seen", now) > OWNER_ABSENT_S:
                self._wait_owner_present(OWNER_JOIN_S, why="run")
            if owner is not None:
                self._owner_seen = now
            if now > end:
                return me is not None and hdist(me, wp) <= ARRIVE_STUCK_M
            if moving:
                state["seen_moving"] = True
                continue
            since = now - state["at"]
            # 刚发的命令还在准备（地形层冷规划 1~2 s）：别急着判
            if not state["seen_moving"] and since < START_GRACE_S:
                continue
            d = hdist(me, wp) if me is not None else None
            if d is not None and state["from_d"] is not None and state["from_d"] - d >= PROGRESS_M:
                stalls = 0
                mode_i = 0                        # 有进展：下一段还是先飞
                issue(me)                         # 这一段走完了、离目标更近：接着下一段
                continue
            if not state["seen_moving"] and since < NOT_MOVING_S:
                continue
            # 这一段没进展
            if d is not None and d <= ARRIVE_STUCK_M:
                return True                       # 路点在台子/烛台上，站到旁边就算到
            stalls += 1
            if stalls >= MAX_STALLS:
                return False                      # 走/跳/飞轮流都过不去：跳过这个点
            mode_i = (mode_i + 1) % len(modes)    # 换另一种走法再试
            self.log("tour_stall", scene=m["scene"], index=self.wp_index, stalls=stalls,
                     next_mode=modes[mode_i], dist=None if d is None else round(d, 1))
            issue(me)

    def _owner_in_room(self, room):
        return self.owner in ((room.get("slots") or {}).values())

    def _owner_gone(self, m, room, state):
        """主人不在小光的房间名单里（多半是小光掉线重连进了别的房间）：5 s 内暂停并提示主人传送过来。"""
        if self._owner_in_room(room):
            state["gone_since"] = None
            return False
        now = self.clock()
        if state.get("gone_since") is None:
            state["gone_since"] = now
            return False
        if now - state["gone_since"] < OWNER_GONE_S:
            return False
        state["gone_since"] = None
        self.control({"pose": "stop", "src": "tour"})
        self.log("tour_owner_gone", scene=m["scene"])
        self.say("我这边看不到你啦，可能我掉线换了房间～从好友列表传送到我这儿，再说 /继续跑")
        self.pause("owner_gone")
        self._check()                            # 等 /继续跑 /跳过 /停止巡游
        return True

    def _hand_state(self, st):
        """leading=小光领着主人的手；owner_led=主人领着小光；inviting=小光的邀请等主人接受；none=没牵。"""
        me = st.get("self") if isinstance(st.get("self"), dict) else {}
        if self.owner in (me.get("led_by_me") or ()):
            return "leading"                     # 主人角色帧里领路人就是小光（含带路后原生领路）
        act = st.get("active_offer") if isinstance(st.get("active_offer"), dict) else {}
        if act.get("kind") == "hold" and act.get("requester") == self.owner:
            if act.get("phase") == "accepted":
                return "leading"
            if act.get("phase") in ("queued", "waiting_accept"):
                return "inviting"
        if (st.get("pose") == "social" and st.get("kind") in ("hold", "carry")
                and str(st.get("with") or "") == self.owner[:8]):
            return "owner_led"
        return "none"

    def _invite(self):
        import uuid
        self.control({"pose": "active_offer", "request_id": uuid.uuid4().hex, "requester": self.owner,
                      "target": self.owner, "action": "牵手", "travel_relead": True, "src": "tour"})
        self._last_invite = self.clock()

    def continue_from_follow(self):
        """主人说 /继续跑：在等牵手时立刻再发一次牵手邀请。"""
        with self.lock:
            if getattr(self, "_waiting_hand", False):
                self._invite_now = True
                return True
        return False

    def _approach_owner(self, owner, me):
        """主人松手后：每 3 s 按主人当前位置直线飞到他身边 1.5 m（不管碰撞）；已在 3 m 内就不动。"""
        if owner is None or me is None or hdist(owner, me) <= APPROACH_NEAR_M:
            return
        if self.clock() - getattr(self, "_last_approach", -1e9) < APPROACH_EVERY_S:
            return
        d = hdist(owner, me)
        f = 1.5 / d
        to = [owner[0] + (me[0] - owner[0]) * f, owner[1], owner[2] + (me[2] - owner[2]) * f]
        self.control({"pose": "fly_to", "to": [round(v, 3) for v in to], "src": "tour"})
        self._last_approach = self.clock()

    def _ensure_hand(self, m, first=False):
        """确保小光领着主人的手。没牵（主人松手/刚进图）：停下 → 飞到主人身边（主人去哪飞到哪）→
        发牵手邀请（每 20 s 一次，/继续跑 立即再发）；主人自己牵小光就换成小光领路再请他接受。"""
        st, _room = self._room()
        if self._hand_state(st) == "leading":
            return
        self.log("tour_hand_lost", scene=m["scene"], first=first, state=self._hand_state(st))
        self.control({"pose": "stop", "src": "tour"})
        self._last_approach = -1e9
        self.say("牵着我的手才能带你跑哦～我跟着你，接一下我的牵手就出发" if first
                 else "你松手啦，我先跟着你～接一下我的牵手就接着巡游")
        with self.lock:
            self._waiting_hand, self._invite_now = True, False
        self._last_invite = -1e9
        gone_state = {}
        try:
            while True:
                self._check()
                self.keepalive()
                st, room = self._room()
                hand = self._hand_state(st)
                if hand == "leading":
                    self.log("tour_hand_back", scene=m["scene"])
                    if not first:
                        self.say("牵好啦，接着巡游～")
                    return
                me, _ = self._self_pos(st)
                owner = self._owner_pos(room)
                near = owner is not None and me is not None and hdist(owner, me) <= 6.0
                with self.lock:
                    now_req, self._invite_now = self._invite_now, False
                if hand == "none":
                    self._approach_owner(owner, me)   # 飞到主人身边（地面跟随常因落点贴地形走不动）
                if hand in ("none", "owner_led") and (
                        now_req or hand == "owner_led" and self.clock() - self._last_invite > 8.0
                        or near and self.clock() - self._last_invite > INVITE_EVERY_S):
                    self._invite()
                    self.log("tour_invite", scene=m["scene"], why="command" if now_req else hand)
                if self._owner_gone(m, room, gone_state):
                    self._last_approach = -1e9
                    continue
                if owner is None and self._map_ok(room, m["map_id"]):
                    if self.clock() - getattr(self, "_owner_seen", self.clock()) > OWNER_ABSENT_S:
                        self._wait_owner_present(OWNER_JOIN_S, why="hand")
                else:
                    self._owner_seen = self.clock()
                self.sleep(POLL_S)
        finally:
            with self.lock:
                self._waiting_hand = False


# ---------------------------------------------------------------- 聊天进程接线

def _routes_path(root):
    p = Path(root) / "control" / "tour_routes.json"
    if p.is_file():
        return p
    bundled = Path(__file__).resolve().parent.parent / "config" / "tour_routes.json"   # 客户端随包路线
    return bundled if bundled.is_file() else p


def route_mode_on(root):
    """/开启跑图模式 写的两个开关（ai_companion.handle_route_mode_command）：任一开着就算跑图模式。"""
    base = Path(root) / "control"
    for name, key in (("light_candle.json", "auto"), ("zero_darkstones.json", "enabled")):
        try:
            if json.loads((base / name).read_text(encoding="utf-8")).get(key) is True:
                return True
        except (OSError, ValueError, AttributeError):
            pass
    return False


def read_schedule(bot, level_id):
    """只读：用小光自己的账号读一次活动时间表（同 sky-play season_job_policy）。"""
    raw = bot.client.post("/account/get_event_schedule", {}, include_level_id=True,
                          level_id=level_id, attempts=1, timeout=10.0)
    d = raw.get("data", raw) if isinstance(raw, dict) else {}
    if isinstance(d, dict) and isinstance(d.get("event_schedule"), dict):
        d = d["event_schedule"]
    if not isinstance(d, dict):
        raise ValueError("event_schedule_schema")
    return d


def _with_active_events(bot, plan, read_state, log):
    """把此刻亮着的季节蜡烛/宝藏大蜡烛插进各图路线；读不到时间表就只跑常驻红蜡烛。"""
    if not any(m.get("event_candles") for m in plan):
        return plan, None
    try:
        level = ((read_state() or {}).get("room") or {}).get("map_id") or 3526133726
        active = active_events(read_schedule(bot, int(level)))
    except Exception as exc:
        log("tour_schedule_failed", error=repr(exc)[:160])
        return [m for m in plan if not m.get("auto_when_event")], "活动时间表没读到，这次先只跑常驻的红蜡烛～"
    out, total, names = [], 0, set()
    for m in plan:
        m2, n = merge_event_candles(m, active)
        if m.get("auto_when_event") and not m2["waypoints"]:
            continue
        out.append(m2)
        total += n
        names |= {k for k in (m.get("event_candles") or {}) if k in active}
    log("tour_events", active=sorted(names), added=total)
    if not total:
        return out, None
    season = sum(1 for k in names if k.startswith("season_"))
    tre = sum(1 for k in names if "_tcandles" in k)
    return out, f"今天亮着的季节蜡烛{season}组、大蜡烛{tre}组也加进路线啦（多{total}个点）"


def escort_travel(bot, owner, query, ch):
    """合成主人的“/带我去<query>”，走 travel_chat_v25 原样路由（同 _confirm_proposal 的做法）。"""
    import uuid
    import travel_chat_v25 as tc
    raw = "/带我去" + query
    intent = tc.parse(raw)
    if intent is None or intent.operation != "start":
        return False, None, "tour_parse_failed"
    with bot.lock:
        if not getattr(bot, "_travel_chat_session", None):
            bot._travel_chat_session = str(uuid.uuid4())
        bot._travel_ingress_revision = getattr(bot, "_travel_ingress_revision", 0) + 1
        session, rev = bot._travel_chat_session, bot._travel_ingress_revision
    origin = tc.Origin(owner, raw, f"tour-{rev}", ch, session, rev, time.monotonic())
    router = getattr(bot, "escort_travel_router", None)
    if not isinstance(router, tc.Router):
        router = tc.default_router(bot)
        bot.escort_travel_router = router

    def current():
        return (getattr(bot, "_travel_chat_session", None) == session
                and getattr(bot, "_travel_ingress_revision", None) == rev)
    decision = router.route(origin, intent, still_current=current)
    return bool(getattr(decision, "receiver_accepted", False)), decision.reply, decision.reason or ""


RESUME_MAX_AGE_S = 24 * 3600


def _launch(bot, doc, plan, *, owner, root, read_state, log, say):
    def keepalive():
        w = getattr(bot, "wanderer", None)
        if w is not None:
            w.ordered()

    tour = TourController(
        doc, owner=owner, read_state=read_state,
        control=lambda cmd: bot.control(cmd, kind="tour", source="tour"),
        say=say, travel=lambda m: escort_travel(bot, owner, m["travel_query"], bot._tour_ch),
        log=log, keepalive=keepalive, state_path=Path(root) / "control" / "tour.state.json",
        route_mode=lambda: route_mode_on(root))
    bot._tour = tour
    tour.start(plan)
    return tour


def saved_resume_plan(doc, saved, now=None):
    """从进度文件恢复：没跑完的那趟（不是主人 /停止巡游、也不是跑完了的），24 h 内有效。
    返回 (plan, 说明) 或 (None, 原因)。当前那张图从记下的那个点接着跑。"""
    now = time.time() if now is None else now
    if not isinstance(saved, dict) or saved.get("phase") in ("finished", "stopped", "idle", None):
        return None, "no_unfinished_tour"
    if not isinstance(saved.get("updated_at"), (int, float)) or now - saved["updated_at"] > RESUME_MAX_AGE_S:
        return None, "too_old"
    if doc.get("route_revision") and saved.get("route_revision") != doc["route_revision"]:
        return None, "route_revision_changed"
    scenes = saved.get("plan_scenes")
    idx = saved.get("index")
    if not isinstance(scenes, list) or type(idx) is not int or not 0 <= idx < len(scenes):
        return None, "bad_state"
    by = {m["scene"]: m for m in doc["maps"]}
    plan = [by[sc] for sc in scenes[idx:] if sc in by]
    if not plan or plan[0]["scene"] != scenes[idx]:
        return None, "map_gone"
    first = dict(plan[0])
    cur = saved.get("cur_waypoints")
    k = saved.get("waypoint") if type(saved.get("waypoint")) is int else 0
    k_note = 0
    if isinstance(cur, list) and cur and saved.get("scene") == first["scene"]:
        if saved.get("phase") == "run":
            first["waypoints"] = [list(p) for p in cur[max(0, k):]] or [list(cur[-1])]
            k_note = k
        else:
            first["waypoints"] = [list(p) for p in cur]     # 换图/到达阶段：整张图（含当天季蜡/大蜡烛）
    plan[0] = first
    return plan, f"接着上次的巡游：从{first.get('label', first['scene'])}第{k_note + 1}个点继续～"


def _resume_saved(bot, *, owner, root, read_state, log, say, ch):
    try:
        doc = load_routes(_routes_path(root))
        saved = json.loads((Path(root) / "control" / "tour.state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    plan, note = saved_resume_plan(doc, saved)
    if plan is None:
        log("tour_resume_unavailable", reason=note)
        if note == "route_revision_changed":
            say("巡游路线已更新，旧断点不会套到新站序；说 /巡游 或 /从圣岛开始巡游 重新选起点。")
            return True
        return False
    bot._tour_ch = ch
    rest, extra = _with_active_events(bot, plan[1:], read_state, log) if len(plan) > 1 else (plan[1:], None)
    plan = [plan[0]] + list(rest)
    say(note)
    if extra:
        say(extra)
    _launch(bot, doc, plan, owner=owner, root=root, read_state=read_state, log=log, say=say)
    log("tour_command", op="resume_saved", maps=len(plan), scene=plan[0]["scene"])
    return True


def handle_command(bot, sender, content, ch, *, owner, owner_title, root, read_state, log):
    """ai_companion.on_chat 调用；处理了返回 True。"""
    if not enabled():
        return False
    parsed = parse(content)
    if parsed is None:
        return False
    op, arg = parsed
    if sender != owner:
        bot.send(f"巡游要{owner_title}说才行哦～", ch, recipient=sender)
        return True
    tour = getattr(bot, "_tour", None)

    def say(text):
        bot.send(text, getattr(bot, "_tour_ch", ch), recipient=owner)

    if op == "status":
        say(tour.status_text() if tour is not None else "现在没在巡游～说 /巡游 就出发")
        return True
    if op == "resume" and (tour is None or not tour.active):
        # 小光重启/掉线后：有没跑完的那趟就从断点接着跑
        if _resume_saved(bot, owner=owner, root=root, read_state=read_state, log=log, say=say, ch=ch):
            return True
    if arg == "soft" and (tour is None or not tour.active):
        return False                      # 没在巡游：交给原来的“停下/继续”处理
    if op in ("stop", "pause", "resume", "skip"):
        if tour is None or not tour.active:
            say("现在没在巡游～说 /巡游 就出发")
            return True
        if op == "stop":
            tour.stop()
        elif op == "pause":
            tour.pause("owner")
            say("好，我停在这儿等你～拿好了说 /继续跑")
        elif op == "resume":
            if tour.continue_from_follow():
                return True                       # 跟着主人时 /继续跑 = 牵手信号（巡游线程会说“接着巡游”）
            if not tour._paused:
                say("我没停着呀，正在跑～")
                return True
            tour.resume()
            say("好，接着跑～")
        else:
            tour.skip()
            say("好，这张跳过～")
        return True
    # start
    if tour is not None and tour.active:
        say("已经在巡游啦～" + tour.status_text())
        return True
    try:
        doc = load_routes(_routes_path(root))
    except (OSError, ValueError) as exc:
        log("tour_routes_unavailable", error=str(exc)[:120])
        say("巡游路线还没装好，暂时去不了")
        return True
    st = read_state() or {}
    cur = (st.get("room") or {}).get("map_id")
    plan, err = select_plan(doc, include_all=arg["all"], start_from=arg["from"],
                            current_map=None if arg["from"] else cur)
    if err:
        say(f"巡游路线里没找到“{arg['from']}”～")
        return True
    bot._tour_ch = ch
    plan, extra_note = _with_active_events(bot, plan, read_state, log)
    if extra_note:
        say(extra_note)
    if not plan:
        say("当前没有可去的常驻点或开放的活动点。")
        return True
    _launch(bot, doc, plan, owner=owner, root=root, read_state=read_state, log=log, say=say)
    log("tour_command", op="start", maps=len(plan), all=arg["all"], start_from=arg["from"])
    return True
