# Server snapshot offline suite, client imports only; assertions unchanged.
import sys as _sys
from pathlib import Path as _Path
_app = _Path(__file__).resolve().parents[1] / "app"
_sys.path[:0] = [str(_app), str(_app / "bot"), str(_app / "src")]
"""离线单元测试：换图后“到达贴身”（假 tracker、假时钟、假控制器，不连服务器、不读抓包）。

跑法（在 bot 目录下）：
    python -m unittest test_arrival_snap -v
"""
import sys
import threading
import unittest
from pathlib import Path

# client import bootstrap above
import arrival_snap as A  # noqa: E402

LEADER = "10000000-0000-4000-8000-000000000002"
OWNER = "10000000-0000-4000-8000-000000000003"
OWN = "10000000-0000-4000-8000-000000000004"
MAP_NEW = 762301879          # “风之试炼”式的目标图 id（随便一个整数）
MAP_OLD = 123456789
T0 = 1000.0                  # 假单调时钟起点


class FakeTracker:
    """只放 RoomTracker 里这次要用的字段；行为（resolve）照真实现抄。"""

    def __init__(self, own_uuid=OWN):
        self.lock = threading.Lock()
        self.own_uuid = own_uuid
        self.ready = True
        self.confirmed_map_id = MAP_NEW
        self.slots = {}                 # slot -> uuid
        self.origins = {}               # slot -> (x, y, z, 观测时刻墙钟)
        self.position_observed = {}     # slot -> (墙钟, 单调钟)
        self.yaws = {}                  # slot -> 6 位偏航码

    def put(self, slot, who, origin=None, seen=None, yaw=None):
        self.slots[slot] = who
        if origin is not None:
            self.origins[slot] = (origin[0], origin[1], origin[2], 0.0)
        if seen is not None:
            self.position_observed[slot] = (0.0, seen)
        if yaw is not None:
            self.yaws[slot] = yaw
        return self

    def resolve(self, target):
        if isinstance(target, str) and "-" in target:
            slot = next((s for s, u in self.slots.items() if u == target), None)
        else:
            slot = int(str(target), 16) if str(target).lower().startswith("0x") else int(target)
        if slot is None:
            raise LookupError("目标不在当前房间名单里")
        origin = self.origins.get(slot)
        if origin is None:
            raise LookupError("还没收到目标的坐标")
        return slot, origin[:3]

class FakeCtl:
    def __init__(self, tracker):
        self.tracker = tracker
        self.position = (0.0, 0.0, 0.0)
        self.heading = None
        self.motion = None
        self.route = []
        self.after_arrive = None
        self.follow = None
        self.social = None
        self.state = {"pose": "idle"}
        self.base = b"seed"
        self.map_id = MAP_NEW
        self.arrival = None
        self.logs = []
        self.overrides = 0
        self.terrain = {}                # (x, z) -> 真实地形高度（假地形查询）

    def log(self, event, **fields):
        self.logs.append((event, fields))

    def terrain_ground(self, x, z, near_y=None):
        return self.terrain.get((round(x, 1), round(z, 1)))

    def ground_offset(self):
        # 旧偏移故意留在假控制器上：新逻辑如果又调它，测试位置会多出 0.31。
        return 0.31

    def _set_override(self, body):
        self.overrides += 1

    def events(self, name):
        return [f for e, f in self.logs if e == name]


def escort_for(who):
    """假 LeaderTravelRuntime：只放 current.binding.speaker_uuid（真实对象在 leader_travel_runtime.py）。"""
    binding = type("Binding", (), {"speaker_uuid": who})()
    current = type("Current", (), {"binding": binding})()
    return type("Escort", (), {"current": current})()


def arm_before_arrival(ctl, at):
    """换图那一刻他的新坐标还没来：先把名单里的坐标藏起来再 arm（否则会被当成“换图前的旧位置”）。"""
    saved = dict(ctl.tracker.origins)
    ctl.tracker.origins.clear()
    A.arm(ctl, MAP_NEW, now=at)
    ctl.tracker.origins.update(saved)


def make_ctl(tracker=None, leader=LEADER, at=T0, follow=None, social=None, escort=True):
    tracker = tracker or FakeTracker()
    ctl = FakeCtl(tracker)
    if escort:
        ctl._escort_travel = escort_for(leader)
    ctl.follow, ctl.social = follow, social
    arm_before_arrival(ctl, at)      # 相当于 PoseController.switch_map
    ctl.follow, ctl.social = None, None   # reset("switch_map") 会清掉这两个
    return ctl


class ArrivalSnapTest(unittest.TestCase):
    def setUp(self):
        self._settle = A.ARRIVAL_SETTLE
        A.ARRIVAL_SETTLE = 0              # 这组只测挪不挪、挪到哪；“等他站稳”单独测
        self.addCleanup(setattr, A, "ARRIVAL_SETTLE", self._settle)

    def test_far_snaps_beside_leader(self):
        """远（水平 134 米）→ 挪到他身后 1.2 米、朝向对着他；随后不再重复挪。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 3.0, yaw=31)
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl, now=T0 + 3.0), "snapped")
        # 偏航 31 = 朝向 +z：他身后 1.2 米 = (10, 0-1.2)；地面查不到 → 用他脚下高度 2.0（不加站立偏移）
        self.assertAlmostEqual(ctl.position[0], 10.0, places=3)
        self.assertAlmostEqual(ctl.position[1], 2.0, places=3)
        self.assertAlmostEqual(ctl.position[2], -1.2, places=3)
        self.assertAlmostEqual(ctl.heading, 0.0, places=3)   # atan2(10-10, 0-(-1.2)) = 0°，正好对着他
        self.assertEqual(ctl.overrides, 1)                   # 位置变了必须把后处理挂回去
        snap = ctl.events("arrival_snap")
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["map_id"], MAP_NEW)
        self.assertEqual(snap[0]["leader"], LEADER[:8])
        self.assertEqual(snap[0]["from"], [100.0, 5.0, 100.0])
        self.assertEqual(snap[0]["to"], [10.0, 2.0, -1.2])
        self.assertEqual(snap[0]["distance"], round(((10.0 - 100.0) ** 2 + (0.0 - 100.0) ** 2) ** 0.5, 2))
        self.assertEqual(snap[0]["waited_seconds"], 3.0)
        self.assertEqual(A.tick(ctl, now=T0 + 4.0), "close")  # 挪完挨着：窗口内继续盯，不重复挪
        self.assertEqual(len(ctl.events("arrival_snap")), 1)

    def test_near_does_not_move(self):
        """已经在 4 米/2 米高差内 → 不挪，记 already_close。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0)
        ctl = make_ctl(tr)
        ctl.position = (12.0, 2.5, 0.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "close")
        self.assertEqual(ctl.position, (12.0, 2.5, 0.0))
        self.assertEqual(ctl.overrides, 0)
        self.assertEqual(ctl.logs, [])
        self.assertEqual(A.tick(ctl, now=T0 + 60.0), "skipped")   # 窗口结束一直挨着：记 already_close
        skipped = ctl.events("arrival_snap_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "already_close")
        self.assertIsNone(A.tick(ctl, now=T0 + 61.0))

    def test_old_position_is_not_fresh(self):
        """上一张图观测到的位置（早于换图时刻）不算新鲜：先继续等，换图后的观测到了才挪。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 - 1.0)
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl, now=T0 + 5.0), "waiting")
        self.assertEqual(ctl.position, (100.0, 5.0, 100.0))
        self.assertEqual(ctl.logs, [])
        tr.position_observed[5] = (0.0, T0 + 9.0)             # 换图之后才观测到
        self.assertEqual(A.tick(ctl, now=T0 + 9.0), "snapped")
        self.assertEqual(len(ctl.events("arrival_snap")), 1)

    def test_attached_waits_then_snaps_after_release(self):
        """牵手/背/抱中她的位置由附着关系决定：先不挪；松开后再判断并挪。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0)
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        ctl.social = {"kind": "hold", "who": LEADER, "slot": 5}
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "attached")
        self.assertEqual(ctl.position, (100.0, 5.0, 100.0))
        self.assertEqual(ctl.logs, [])
        ctl.social = None                                     # 过场动画强制松手
        self.assertEqual(A.tick(ctl, now=T0 + 6.0), "snapped")
        self.assertEqual(len(ctl.events("arrival_snap")), 1)
        self.assertEqual(ctl.events("arrival_snap")[0]["waited_seconds"], 6.0)

    def test_60_seconds_timeout(self):
        """60 秒都没等到新鲜位置 → 记超时，不再挪。"""
        tr = FakeTracker().put(5, LEADER)                     # 在名单里，但一帧坐标都没有
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl, now=T0 + 59.0), "waiting")
        self.assertEqual(ctl.logs, [])
        self.assertEqual(A.tick(ctl, now=T0 + 60.0), "skipped")
        self.assertEqual(ctl.position, (100.0, 5.0, 100.0))
        skipped = ctl.events("arrival_snap_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "timeout_no_fresh_position")
        self.assertEqual(skipped[0]["waited_seconds"], 60.0)
        tr.position_observed[5] = (0.0, T0 + 61.0)            # 超时后才来也不挪
        self.assertIsNone(A.tick(ctl, now=T0 + 61.0))

    def test_attached_timeout_reports_attached(self):
        """一直附着到超时 → 记 attached（不是“没等到位置”）。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 1.0)
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        ctl.social = {"kind": "carry", "who": LEADER, "slot": 5}
        self.assertEqual(A.tick(ctl, now=T0 + 1.0), "attached")
        self.assertEqual(A.tick(ctl, now=T0 + 60.0), "skipped")
        self.assertEqual(ctl.events("arrival_snap_skipped")[0]["reason"], "attached")
        self.assertEqual(ctl.position, (100.0, 5.0, 100.0))

    def test_leader_is_escort_peer_not_owner(self):
        """带路人是 escort 记录的对方，不是 OWNER：挪到 escort 那个人身边。"""
        tr = FakeTracker(own_uuid=OWNER)
        tr.put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0, yaw=31)
        tr.put(9, OWNER, origin=(-50.0, 2.0, -50.0), seen=T0 + 2.0, yaw=31)
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(ctl.arrival["leader"], LEADER)       # arm 时记的是 escort 的对方
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "snapped")
        self.assertEqual(ctl.events("arrival_snap")[0]["leader"], LEADER[:8])
        self.assertLess(abs(ctl.position[0] - 10.0) + abs(ctl.position[2] + 1.2), 0.01)
        self.assertGreater(abs(ctl.position[0] - (-50.0)), 1.0)   # 不是 OWNER 的位置

    def test_follow_target_is_leader_and_follow_resumes(self):
        """没有 escort 时用当前跟随对象；挪完继续跟随（reset 把 follow 清掉了，这里补回来）。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0)
        ctl = FakeCtl(tr)
        ctl.follow = {"target": LEADER, "distance": 1.5}
        arm_before_arrival(ctl, T0)
        self.assertEqual(ctl.arrival["leader"], LEADER)
        self.assertEqual(ctl.arrival["follow"], {"target": LEADER, "distance": 1.5})
        ctl.follow = None                                     # reset("switch_map")
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "snapped")
        self.assertEqual(ctl.follow, {"target": LEADER, "distance": 1.5})

    def test_follow_target_slot_resolves_to_uuid(self):
        """跟随目标给的是 slot（换图后会变）时，按当时的名单换成 uuid。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0)
        ctl = FakeCtl(tr)
        ctl.follow = {"target": "0x5", "distance": 1.5}
        A.arm(ctl, MAP_NEW, now=T0)                           # 换图时名单里还是他上一张图的坐标
        self.assertEqual(ctl.arrival["leader"], LEADER)
        tr.origins[5] = (40.0, 2.0, 0.0, 0.0)                 # 换图后才来的新图坐标
        ctl.follow = None
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "snapped")

    def test_no_leader_skips(self):
        """既没有 escort、也没在牵手/跟随：没带路人可贴，记 no_leader。"""
        ctl = make_ctl(FakeTracker(), escort=False)
        self.assertIsNone(ctl.arrival["leader"])
        self.assertEqual(A.tick(ctl, now=T0 + 1.0), "skipped")
        self.assertEqual(ctl.events("arrival_snap_skipped")[0]["reason"], "no_leader")

    def test_other_map_or_unready_room_waits(self):
        """人还没进新房间 / 房间还没起来：等，不挪。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0)
        ctl = make_ctl(tr)
        ctl.position = (100.0, 5.0, 100.0)
        tr.ready = False
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "waiting")
        tr.ready, tr.confirmed_map_id = True, MAP_OLD
        self.assertEqual(A.tick(ctl, now=T0 + 3.0), "waiting")
        tr.confirmed_map_id = MAP_NEW
        self.assertEqual(A.tick(ctl, now=T0 + 4.0), "snapped")

    def test_ground_query_used_only_when_consistent(self):
        """查到真实地形就吸到地形；地形和带路人脚下差太多（层错）就用他的高度，且都不加偏移。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0, yaw=31)
        ctl = make_ctl(tr)
        ctl.terrain[(10.0, -1.2)] = 1.7
        ctl.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "snapped")
        self.assertAlmostEqual(ctl.position[1], 1.7, places=3)   # 不是 1.7 + 0.31

        tr2 = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 2.0, yaw=31)
        ctl2 = make_ctl(tr2)
        ctl2.terrain[(10.0, -1.2)] = 30.0          # 查到别的楼层
        ctl2.position = (100.0, 5.0, 100.0)
        self.assertEqual(A.tick(ctl2, now=T0 + 2.0), "snapped")
        self.assertAlmostEqual(ctl2.position[1], 2.0, places=3)


class StaleLeaderPositionTest(unittest.TestCase):
    def setUp(self):
        self._settle = A.ARRIVAL_SETTLE
        A.ARRIVAL_SETTLE = 0
        self.addCleanup(setattr, A, "ARRIVAL_SETTLE", self._settle)

    def test_previous_map_position_is_ignored_until_real_one_arrives(self):
        """2026-11-06 风之试炼：进图 0.7 秒时他发的还是遇境坐标，按它挪会把小光放进地底。要等真正的新位置。"""
        tr = FakeTracker().put(5, LEADER, origin=(0.72, 1.0, -1.15), seen=T0 - 1.0, yaw=31)   # 换图前在遇境
        ctl = FakeCtl(tr)
        ctl._escort_travel = escort_for(LEADER)
        A.arm(ctl, MAP_NEW, now=T0)
        self.assertEqual(ctl.arrival["prev"], (0.72, 1.0, -1.15))
        ctl.position = (-11.77, 69.33, -119.98)
        tr.position_observed[5] = (0.0, T0 + 0.7)                # 换图后收到的，但还是遇境坐标
        self.assertEqual(A.tick(ctl, now=T0 + 0.7), "waiting")
        tr.origins[5] = (-0.44, 30.33, -36.2, 0.0)               # 7 秒后真正的风之试炼坐标
        tr.position_observed[5] = (0.0, T0 + 7.0)
        self.assertEqual(A.tick(ctl, now=T0 + 7.0), "snapped")
        self.assertLess(abs(ctl.position[1] - (30.33)), 0.01)

    def test_same_spot_accepted_after_grace(self):
        """新图出生点恰好和旧位置很近：也一直等（宁可不贴，也不按可能是上一张图的坐标贴）。"""
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 - 1.0)
        ctl = FakeCtl(tr)
        ctl._escort_travel = escort_for(LEADER)
        A.arm(ctl, MAP_NEW, now=T0)
        ctl.position = (100.0, 5.0, 100.0)
        tr.position_observed[5] = (0.0, T0 + 2.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "waiting")
        self.assertEqual(A.tick(ctl, now=T0 + 20.0), "waiting")      # 不再兜底接受旧坐标（珍珠海域实测会贴错）


class SettleTest(unittest.TestCase):
    def test_waits_until_leader_lands(self):
        """2026-11-06 风之试炼：他刚进图高度 30.47 还在落，站稳是 30.32；按落地前高度贴会浮空，要等他稳住。"""
        tr = FakeTracker().put(5, LEADER, origin=(0.72, 1.0, -1.15), seen=T0 - 1.0, yaw=31)
        ctl = FakeCtl(tr)
        ctl._escort_travel = escort_for(LEADER)
        A.arm(ctl, MAP_NEW, now=T0)
        ctl.position = (-11.77, 69.33, -119.98)
        tr.origins[5] = (-0.47, 30.47, -36.88, 0.0)
        tr.position_observed[5] = (0.0, T0 + 1.0)
        self.assertEqual(A.tick(ctl, now=T0 + 1.0), "waiting")          # 第一次看到：先记下
        tr.origins[5] = (-0.47, 30.32, -36.88, 0.0)
        tr.position_observed[5] = (0.0, T0 + 2.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "waiting")          # 还在往下掉：重新计时
        tr.position_observed[5] = (0.0, T0 + 3.0)
        self.assertEqual(A.tick(ctl, now=T0 + 3.0), "snapped")          # 稳住 1 秒：贴
        self.assertAlmostEqual(ctl.position[1], 30.32, places=3)


class WindTest(unittest.TestCase):
    def setUp(self):
        self._settle = A.ARRIVAL_SETTLE
        A.ARRIVAL_SETTLE = 0
        self.addCleanup(setattr, A, "ARRIVAL_SETTLE", self._settle)

    def test_close_at_arrival_then_leader_blown_away(self):
        """2026-11-06 月牙绿洲：进图两人都在云里挨着；他松手被风吹到落脚点，小光留在云里 → 窗口内再贴过去。"""
        tr = FakeTracker().put(5, LEADER, origin=(-127.0, 387.0, 85.0), seen=T0 + 2.0)
        ctl = make_ctl(tr)
        ctl.position = (-127.74, 387.05, 85.0)
        self.assertEqual(A.tick(ctl, now=T0 + 2.0), "close")
        tr.origins[5] = (-60.0, 300.0, 40.0, 0.0)              # 被风吹到落脚点
        tr.position_observed[5] = (0.0, T0 + 20.0)
        self.assertEqual(A.tick(ctl, now=T0 + 20.0), "snapped")
        self.assertLess(abs(ctl.position[1] - 300.0), 0.01)
        self.assertEqual(A.tick(ctl, now=T0 + 61.0), None)       # 贴过：窗口结束不记跳过
        self.assertEqual(ctl.events("arrival_snap_skipped"), [])

    def test_at_most_three_snaps(self):
        tr = FakeTracker().put(5, LEADER, origin=(10.0, 2.0, 0.0), seen=T0 + 1.0)
        ctl = make_ctl(tr)
        for i in range(3):
            ctl.position = (500.0 + i * 100, 2.0, 0.0)
            self.assertEqual(A.tick(ctl, now=T0 + 1.0 + i), "snapped")
        ctl.position = (900.0, 2.0, 0.0)
        self.assertIsNone(A.tick(ctl, now=T0 + 5.0))
        self.assertEqual(len(ctl.events("arrival_snap")), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
