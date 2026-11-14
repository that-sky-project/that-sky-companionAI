"""房间追踪 + 姿态控制（在 long_runner 进程内运行）。

- RoomTracker：从下行 PlayerSync(17) 维护 slot↔uuid，从 Game3 快照维护每个 slot 的最新坐标。
- PoseController：轮询控制文件，切换小光的姿态种子：
    {"pose": "idle"}
    {"pose": "candle", "target": "<玩家 uuid 或 slot 十六进制>"}
    {"pose": "anim", "name": "<动画名，如 TakeCandle>", "seconds": 2.67}
  当前状态写到 <控制文件>.state.json。play() 也供接受好友请求时调用（播接烛动作）。
"""
import copy
import json
import math
import random
import struct
import threading
import time
import uuid as uuidlib
from pathlib import Path

import xg_paths

from qfsky import home_handoff_runtime0167 as pump_mod
from qfsky.map_long0167 import map_long_engine

import anim_pose
from seat_tilt_runtime import SeatTiltControllerMixin, upright_action, seat_command
import prop_motion_guard
import arrival_snap
import candle_pose
import social_accept
import social_pose
import player_status
import social_invite
import terrain_spawn

# 2026-11 T23-1：真人跳/飞/跳下跟随。文件缺失/关闭时这些模块完全不参与，
# 现有行为逐字节不变。导入失败也只是保持旧逻辑。
try:
    import air_motion
    import air_planner
    import air_terrain
except Exception:                       # pragma: no cover - release tree always has them
    air_motion = air_planner = air_terrain = None
try:
    import shout as shout_mod
except Exception:                       # pragma: no cover
    shout_mod = None

try:
    import stuck_recovery
except ImportError:
    stuck_recovery = None

AIR_CONTROL_FILE = "air_follow.json"     # 与 pose.json / arrival_snap.json 同目录
AIR_RETARGET = 1.5                      # 目标挪动超过这么多米就中止当前空中计划
SHOUT_CONTROL_FILE = "shout.json"        # 自己逛时偶尔叫：文件不存在 = 不随机叫（命令照常）
SHOUT_IDLE_MIN_GAP = 90.0                # 两次随机叫至少间隔
SHOUT_IDLE_MEAN_GAP = 270.0              # 平均 3–6 分钟
SHOUT_IDLE_RETRY = 20.0                  # 到点但正忙/有人说话：20 s 后再看
SHOUT_COMMAND_QUIET = 10.0               # 最近有人下命令就不算“没人管”
SHOUT_CHAIN_GAP = (0.30, 0.60)           # 连叫间隔（真人 0.1–0.6 s，取稳一点）

TAKE_CANDLE_SECONDS = anim_pose.TAKE_CANDLE_SECONDS   # 线上策略：64 帧 / 30fps / 速率 0.4
ANIM_GATE_SECONDS = 0.9            # 礼包 gate：动画结束后再等一会儿才允许下一个事件
EXPLORE_MIN_AREA = 30              # 可走块小于这么多格就先去附近的大块
EXPLORE_ENTRY_RANGE = 15.0
WALK_MAX_CLIMB = 2.0               # 自己逛：目的地比现在高这么多就不去（多半是有人飞在那儿）
SOCIAL_MAX_SECONDS = 120.0         # 牵手/背背最长保持（对方按结束小光看不到时的兜底）
SOCIAL_LOST_DISTANCE = 25.0        # 牵手时带路方离开这么远（传送/飞走）就松手（牵手已改成每帧贴身，正常跑不会拉开）
HOLD_DISTANCE = 0.5                # 牵手跟在带路方身边的距离
CANDLE_REPLY_RANGE = 6.0           # 这么近的人对小光举蜡烛，小光就举起来回应
CANDLE_REPLY_COOLDOWN = 30.0       # 同一个人 30 秒内不重复回应
CANDLE_REPLY_MAX = 20.0            # 回应最多举这么久（对方一直不放下的兜底）
CANDLE_REPLY_MIN = 5.0             # 回应至少举这么久（实测只举 2 秒看着像没举）
CANDLE_REPLY_GONE = 1.5            # 对方连续这么久没对着她，才算放下
BOW_ANIM = "BowEmote_2"            # 鞠躬（2 级；3 级看着是磕头，哥哥实测）
ONCE_DISTANCE = 0.4                # 拥抱类：接受时挪到离对方这么近
FOLLOW_SLACK = 1.0                 # 跟随：目标站位变动超过这么多米才重新走
FOLLOW_RUN_DISTANCE = 5.0          # 离目标超过这么远就小跑
FOLLOW_RUN_SPEED = 6.0             # 小跑速度（路人实测 4~6）
CANDLE_REAIM = 0.3                 # 举烛：目标挪动超过这么多米就更新瞄准点
PLAYER_STATUS_MAX_AGE = 5.0       # 只读观测过期后不再输出为当前值；不改变跟随内部坐标
ROOM_STATUS_SCHEMA = "cn0167-room-status-v1"
SEAT_SCAN_SECONDS = 1.0            # 每个玩家每秒解析一次座位状态
HOME_MAP_ID = 3526133726            # 遇境椅子全部为玩家摆件，不是永久地图座位
HOME_SEAT_MAX_AGE = 600.0           # 最近一次实时坐姿观测的有效期（秒，可调）
HOME_SEAT_PICK_RANGE = 30.0
HOME_SEAT_PICK_DY = 10.0
PERMANENT_SEAT_MAPS = set()         # 遇境不得使用永久座位表；其它地图逻辑不变
TABLE_SAME = 3.0                   # 摆件桌：存档座位离实时看到的同桌座位不到这么远才算同一张桌
SEAT_GROUP = 3                     # 同一张桌子的座位号相邻（遇境桌 5545~5548）：有人坐 n，就推断 n±1..±3 也是这桌
TABLE_MOVED = 3.0                  # 某座位号实时出现在离旧记录这么远的地方 → 桌子挪了，整组旧记录作废
SEAT_HEIGHT = 0.44                # 真人坐下时坐标比旁边地面高这么多（1.36 vs 0.92）
SEAT_PICK_RANGE = 30.0            # 只考虑离喊话人这么近的座位；避免坐到别的图/别的岛的坐标上
SEAT_PICK_DY = 10.0               # 座位和喊话人的高差超过这么多就当是别的层/别的图
SIT_TIMEOUT = 20.0                # 收到坐下命令后最多等这么久；到点没坐下就放弃并记 sit_failed
SIT_WALK_GRACE = 10.0             # 地形 worker 连行走对象都没给（准备失败/路线被挡）时的提前放弃线
SIT_DIRECT_GRACE = 1.5            # T23-8：地形层 1.5 s 还没起步，就自己按真实地形走（不再干等 10 s）
SIT_DIRECT_RANGE = 8.0            # 直接走只处理这么近的座位（远处仍交给地形层）
SIT_DIRECT_SAMPLE = 0.25          # 沿直线每 0.25 m 采一次真实地面
SIT_DIRECT_MAX_STEP = 0.35        # 相邻采样点高差超过这么多 = 不是同一层连续地面
SIT_DIRECT_BODY_LOW = 0.15        # 身体高度带（同 air_planner._trajectory_clear）
SIT_DIRECT_BODY_HIGH = 1.8
SIT_DIRECT_BODY_SIDE = 0.35
CANDLE_FOLLOW = 2.5               # 举烛：目标离开超过这么远就举着蜡烛跟过去（停在 1 米处）
SEED_HEADING = 0.0                # 遇境种子朝 +z（偏航 31）；实际用 PoseController.seed_heading（各图种子朝向不同）
HEIGHT_CELL = 1.0                 # 地面高度图格子（米）：用房间里玩家站过的位置拼出来
TRAIL_MIN, TRAIL_MAX = 1.0, 4.0   # 补轨迹：同一玩家相邻两次位置水平距离在这之间才补中间格
TRAIL_MAX_DT, TRAIL_MAX_SPEED, TRAIL_MAX_SLOPE = 1.5, 8.0, 0.5


def _dist(a, b):
    return sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5


class RoomTracker:
    def __init__(self):
        import placeable
        self._placeable_module = placeable
        self.lock = threading.Lock()
        self.slots = {}      # slot -> uuid 字符串
        self.origins = {}    # slot -> (x, y, z, 时间)
        self.heights = {}    # (格 x, 格 z) -> 最近若干个玩家高度
        self.height_total = {}   # (格 x, 格 z) -> 累计记过多少次（记录器靠它判断新样本）
        self.last_pos = {}       # slot -> 上一次位置 (x, y, z, 时间)：补轨迹用
        self.seated = {}     # slot -> 正坐着的座位号（实时）
        self.seat_book = {}  # 座位号 -> (玩家坐着时的坐标, 偏航码, 时间)：实时看到的座位
        self.seat_checked = {}
        self.seat_pose_book = {}  # 当前房间座位字段证据，仅RAM，不进入seat持久文件
        self.seat_witnesses = {}  # seat -> (uuid, slot, incarnation, room_generation, seat_epoch, monotonic)
        self.seat_epoch = 0
        self.home_session = str(uuidlib.uuid4())  # RAM-only：进程/追踪器重建后旧自动坐下凭据失效
        self.offers = {}     # slot -> (邀请类型, 对象 slot, 时间)：谁正对谁发起双人互动
        self.own_uuid = None # 小光自己的 uuid（install 设置），用来认出“邀请的是我”
        self.yaws = {}       # slot -> 偏航码（被背时小光朝向跟背人方一致）
        self.modes = {}      # slot -> 当前动画 mode（双人舞看邀请方是否还在 mode 9）
        self.releases = []   # [(时间, 种类, 发起 slot, 对象 slot)]：主人按了结束（松手 / 放下）
        self.level_changes = []   # [(时间, 发起 slot, 场景名)]：ChangeLevelWithFadeRpc（带路方回遇境/过图）
        self.candle_targets = {}  # slot -> 他举蜡烛对着的 slot（0 = 没举）
        self.pending_map_id = self.confirmed_map_id = self.epoch = None
        self.room_generation = 0
        self.ready = False
        self._engine = None
        self._terrain_slots = set()  # 地图元数据只限制地形采样，不删除同房身份/跟随位置
        self.position_observed = {}  # slot -> (Unix seconds, monotonic seconds)
        self._room_actor_presence_v20 = {}  # RAM-only, current private receiver + occupant binding
        self.player_status = {}      # read-only decoded observations, never used for sending
        self._slot_versions = {}     # slot incarnation; prevents UUID A -> B -> A parse races
        self._next_slot_version = 0
        self.social_events = []       # authenticated current-engine RPC observations
        self.social_event_seq = 0
        self._attachment_events = []  # bounded read-only saved-snapshot observations
        self._attachment_seq = 0       # never reused across room incarnations
        self._attachment_wire_seqs = {}
        self.placeables = placeable.PlaceableRegistry()
        self.placeable_log = None
        self._placeable_log_times = {}
        self.props_assets_ready = False
        self.placeables.set_known_names({})

    def _clear_room_locked(self):
        self.seat_epoch += 1
        self.placeables.clear()
        for name in ("slots", "origins", "heights", "height_total", "last_pos", "seated",
                     "seat_book", "seat_witnesses", "seat_checked", "seat_pose_book", "offers", "yaws", "modes", "candle_targets",
                     "position_observed", "player_status", "_slot_versions"):
            getattr(self, name).clear()
        self.releases.clear()
        self.level_changes.clear()
        self.social_events.clear()
        self._attachment_events.clear()
        self._attachment_wire_seqs.clear()
        self._terrain_slots.clear()
        self._room_actor_presence_v20.clear()
        self.roster_at = 0.0

    def begin_segment(self, map_id):
        global LAST_EPOCH
        with self.lock:
            self._clear_room_locked()
            self.pending_map_id, self.epoch, self.ready = map_id, None, False
            self._engine = None
            LAST_EPOCH = None

    def end_segment(self):
        global LAST_EPOCH
        with self.lock:
            self.ready, self.epoch, self.pending_map_id = False, None, None
            self._engine = None
            self._clear_room_locked()
            LAST_EPOCH = None

    def end_room(self, engine):
        """An old engine closing must not invalidate a newer room instance."""
        global LAST_EPOCH
        with self.lock:
            if engine is not self._engine:
                return False
            self._engine, self.ready, self.epoch = None, False, None
            self._clear_room_locked()
            LAST_EPOCH = None
            return True

    def begin_room(self, engine, body):
        """Only called after the engine validates its own EnterGame membership."""
        global LAST_EPOCH
        with self.lock:
            if engine._map != self.pending_map_id or not engine._match.ready:
                return False
            if self.own_uuid is not None and getattr(engine, "_owner", None) != uuidlib.UUID(str(self.own_uuid)).bytes:
                return False
            self._clear_room_locked()
            self.room_generation += 1
            self.confirmed_map_id, self.epoch = engine._map, engine._epoch
            self.ready, self._engine = True, engine
            self.map_binding_source=getattr(engine,'_membership_provenance','authenticated_player_sync')
            LAST_EPOCH = engine._epoch
        self._roster(body, match=engine._match, level_id=engine._map, engine=engine)
        return True

    def __call__(self, messages):
        """下行只监听有纪元检查的互动；名单和位置由引擎验证后通知。"""
        for m in messages:
            body = bytes(m.body)
            # Roster updates come only after the engine accepts the whole message.
            if m.message_type == 14 and len(body) >= 4 and body[:1] == b"\x02":
                self._rpcs(body)

    RELEASE_KEYS = {0x5BCC196F: "hold", 0x32D2F05A: "carry"}   # AvatarDetachFollowerNetRpc / DisconnectCarriedRpc
    CHANGE_LEVEL = 0x4D84A062                                   # ChangeLevelWithFadeRpc

    def _rpcs(self, body):
        with self.lock:
            if not self.ready or len(body) < 4 or body[1] != self.epoch:
                return
            room_generation = self.room_generation
        off = 4
        while off + 7 <= len(body):
            key, size = struct.unpack_from("<I", body, off)[0], body[off + 5] | body[off + 6] << 8
            if off + 7 + size > len(body):
                return
            kind = self.RELEASE_KEYS.get(key)
            if key == self.CHANGE_LEVEL and size >= 5:
                n = struct.unpack_from("<I", body, off + 8)[0]
                scene = body[off + 12:off + 12 + n]
                if 0 < n <= min(64, size - 5) and len(scene) == n and scene.isascii():
                    with self.lock:
                        if (not self.ready or room_generation != self.room_generation
                                or body[off + 7] not in self.slots):
                            return
                        self.level_changes.append((time.monotonic(), body[off + 7], scene.decode()))
                        del self.level_changes[:-20]
            if kind and size >= 2:
                with self.lock:
                    if (not self.ready or room_generation != self.room_generation
                            or body[off + 7] not in self.slots):
                        return
                    self.releases.append((time.monotonic(), kind, body[off + 7], body[off + 8]))
                    del self.releases[:-20]
            off += 7 + size

    def props_revision(self):
        """热地形查询只比较版本，不复制道具列表。"""
        if not self.lock.acquire(False):
            return None
        try:
            return self.room_generation, self.placeables.revision
        finally:
            self.lock.release()

    def props_snapshot(self):
        """RAM-only；不加入会落盘的 room status 文档。"""
        if not self.lock.acquire(False):
            return None
        try:
            now=time.monotonic()
            observers=[]
            for slot,origin in self.origins.items():
                clocks=self.position_observed.get(slot)
                if (slot not in self.slots or clocks is None or not 0<=now-clocks[1]<=2.5):
                    continue
                cached=self.player_status.get(slot) or {}
                value=(cached.get('value') or {}) if 0<=now-cached.get('observed_mono',-float('inf'))<=2.5 else {}
                observers.append(dict(slot=slot,uuid=self.slots.get(slot),version=self._slot_versions.get(slot),
                    position=list(origin[:3]),observed_mono=clocks[1],
                    seated=bool(value.get('social',{}).get('seated'))))
                if len(observers)>=64:break
            unmodeled=self.placeables.unmodeled_snapshot() if self.ready else []
            return {"room_generation": self.room_generation,
                    "revision": self.placeables.revision,
                    "map_id": self.confirmed_map_id, "validated": self.ready,
                    "props": self.placeables.snapshot() if self.ready else [],
                    "unmodeled_props": unmodeled,
                    "assets_ready": self.props_assets_ready,
                    "unmodeled_revision": self.placeables.unmodeled_revision,
                    "support_observers": observers if self.ready else []}
        finally:
            self.lock.release()

    def set_placeable_names(self, names):
        with self.lock:
            self.placeables.set_known_names(names)
            self.props_assets_ready = bool(self.placeables.geometry_names)

    def _placeable_notice(self, diagnostics):
        # 每种原因最多每 5 秒记录一次，避免畸形包淹没 runner 日志。
        for item in diagnostics:
            if not self.props_assets_ready and item.get('reason') in ('prop_no_geometry', 'prop_unknown_name'):
                continue  # 缺本地资产是正常降级，由冷绑定仅报告一次状态。
            reason, now = item.get("reason", "prop_invalid"), time.monotonic()
            with self.lock:
                last = self._placeable_log_times.get(reason, -float("inf"))
                if now - last < 5.0:
                    continue
                self._placeable_log_times[reason] = now
                logger = self.placeable_log
            if logger is not None:
                try:
                    logger("placeable_ignored", **item)
                except Exception as exc:
                    # 道具诊断失败不能打断已有引擎 ACK/社交分发。
                    import logging
                    logging.getLogger(__name__).warning("placeable logger failed: %s", type(exc).__name__)

    def on_placeable_rpc(self, engine, body):
        """仅当前引擎/纪元/名单；整条记录链验证完毕后才发布。"""
        if type(body) is not bytes or len(body) < 4 or body[0] != 2:
            return
        with self.lock:
            if (not self.ready or engine is not self._engine or getattr(engine, "_closed", False)
                    or body[1] != self.epoch or body[2] not in self.slots
                    or not getattr(getattr(engine, "_match", None), "ready", False)
                    or getattr(engine, "_map", None) != self.confirmed_map_id
                    or self.pending_map_id != self.confirmed_map_id):
                return
            generation = self.room_generation
            identities = {s: (u, self._slot_versions.get(s)) for s, u in self.slots.items()}
        try:
            events, diagnostics = self._placeable_module.parse_game(body)
        except ValueError as exc:
            self._placeable_notice([{"reason": str(exc)}])
            return
        with self.lock:
            if (not self.ready or engine is not self._engine or getattr(engine, "_closed", False)
                    or generation != self.room_generation or body[1] != self.epoch
                    or any(self.slots.get(slot) != identity or self._slot_versions.get(slot) != version
                           for slot, (identity, version) in identities.items())):
                return
            diagnostics.extend(self.placeables.apply(events, self.slots))
        self._placeable_notice(diagnostics)

    def _own_social_binding_locked(self):
        """Caller holds lock. Match/engine identity, not observing our own Actor."""
        engine = self._engine
        match = getattr(engine, "_match", None)
        if (not self.ready or engine is None or getattr(engine, "_closed", False)
                or getattr(match, "ready", False) is not True
                or self.confirmed_map_id != self.pending_map_id
                or getattr(match, "self_level_id", None) != self.confirmed_map_id
                or getattr(engine, "_map", None) != self.confirmed_map_id
                or getattr(engine, "_epoch", None) != self.epoch):
            return None
        try:
            own_bytes = uuidlib.UUID(str(self.own_uuid)).bytes
        except (ValueError, TypeError, AttributeError):
            return None
        own = getattr(match, "self_net_player_id", None)
        if (type(own) is not int or not 1 <= own <= 255 or self.slots.get(own) != self.own_uuid
                or getattr(match, "self_uuid", None) != own_bytes
                or getattr(engine, "_owner", None) != own_bytes):
            return None
        return own

    def _social_source_fresh_locked(self, source):
        own = self._own_social_binding_locked()
        if own is None or source not in self.slots:
            return False
        if source == own:
            return True
        seen = self.position_observed.get(source)
        return bool(seen is not None and 0 <= time.monotonic() - seen[1] <= PLAYER_STATUS_MAX_AGE)

    def on_social_rpc(self, engine, body):
        """Current engine only; parse the entire RPC stream before publishing any event."""
        if not isinstance(body, bytes) or len(body) < 4 or body[0] != 2:
            return
        with self.lock:
            if (not self.ready or engine is not self._engine or getattr(engine, "_closed", False)
                    or body[1] != self.epoch or not body[2]
                    or not self._social_source_fresh_locked(body[2])):
                return
            generation = self.room_generation
            identities = {s: (u, self._slot_versions.get(s)) for s, u in self.slots.items()}
        try:
            accepted = social_invite.parse_accept_game(body)
            releases = []
            off = 4
            while off < len(body):
                key, header, size = struct.unpack_from("<IBH", body, off)
                end = off + 7 + size
                if header != 0 or end > len(body):
                    return
                if key in self.RELEASE_KEYS:
                    released = social_invite.parse_release_body(key, body[off + 7:end])
                    releases.append(dict(released, source_slot=body[2], epoch=body[1]))
                off = end
        except (ValueError, TypeError, KeyError, struct.error):
            return
        with self.lock:
            if (not self.ready or engine is not self._engine or getattr(engine, "_closed", False)
                    or generation != self.room_generation
                    or not self._social_source_fresh_locked(body[2])
                    or any(self.slots.get(slot) != identity or self._slot_versions.get(slot) != version
                           for slot, (identity, version) in identities.items())):
                return
            for event in accepted + releases:
                self.social_event_seq += 1
                self.social_events.append(dict(event, event_seq=self.social_event_seq,
                                               room_generation=generation, observed_mono=time.monotonic(),
                                               event_kind="accept" if "offerer_slot" in event else "release"))
            del self.social_events[:-64]

    def on_snapshot(self, data, *, epoch=None, receive_state=None, snapshot_sequence=None, snapshot_source=None):
        """引擎每接受一帧玩家快照就调用一次（同一份解码结果）。
        以前追踪器自己另开一份解码状态：没有引擎的纪元过滤，一旦和引擎差一帧，服务器引用的基准帧
        就再也对不上（snapshot_baseline_missing），位置从此冻结——墓土人多时“跟着跟着就不跟了”。"""
        with self.lock:
            if not self.ready or epoch != self.epoch:
                return
            # Wire epochs can repeat. Production callbacks must also match the
            # active engine's private snapshot receiver, not merely the byte.
            if receive_state is not None and receive_state is not getattr(self._engine, "_player", None):
                return
            room_generation = self.room_generation
            presence_scope = ((self._engine, receive_state, self.epoch, room_generation, self.confirmed_map_id)
                              if receive_state is not None else None)
            sample = None
            # Transient zero, duplicate and reordered snapshots are not new
            # detachment evidence. Ordinary pose handling remains unchanged.
            if (receive_state is not None and type(snapshot_sequence) is int
                    and 1 <= snapshot_sequence <= 255 and type(snapshot_source) is int
                    and 0 <= snapshot_source <= 255):
                previous = self._attachment_wire_seqs.get(snapshot_source)
                if previous is None or 0 < ((snapshot_sequence - previous) & 255) < 128:
                    self._attachment_wire_seqs[snapshot_source] = snapshot_sequence
                    self._attachment_seq += 1
                    sample = (self._attachment_seq, snapshot_source, snapshot_sequence)
        self._origins(data, room_generation=room_generation, attachment_sample=sample, presence_scope=presence_scope)

    def _roster(self, body, *, match=None, level_id=None, engine=None):
        # 人数按长度算（每人 37 字节）：原先用 body[0] & 15，墓土大厅人一多就算错、整条名单被丢掉
        if not body or (len(body) - 1) % 37:
            self.roster_errors = getattr(self, "roster_errors", 0) + 1
            self.last_roster_error = (len(body), body[0] if body else None)
            return
        if match is None or not match.ready or level_id != self.pending_map_id:
            return
        count = (len(body) - 1) // 37
        slots = {}
        for i in range(count):
            off = 1 + i * 37
            # Peer-level rows can lag behind a real room move. Keep the
            # validated room identity for follow/RPC, but not terrain sampling.
            slots[body[off]] = str(uuidlib.UUID(bytes=body[off + 1:off + 17]))
        terrain_slots = set(match.level_roster_ids)
        with self.lock:
            if not self.ready or level_id != self.confirmed_map_id or engine is not self._engine:
                return
            terrain_changed = self._terrain_slots ^ terrain_slots
            for slot in terrain_changed:
                self.last_pos.pop(slot, None)  # never interpolate across map eligibility changes
            self._terrain_slots = terrain_slots
            for slot in set(self.seated) - terrain_slots:
                self.seated.pop(slot, None)
            changed = {slot for slot in set(self.slots) | set(slots)
                       if self.slots.get(slot) != slots.get(slot)}
            for slot in changed:
                self._next_slot_version += 1
                self._slot_versions.pop(slot, None)
                if slot in slots:
                    self._slot_versions[slot] = self._next_slot_version
            for name in ("origins", "last_pos", "seated", "seat_checked", "offers", "yaws", "modes", "candle_targets",
                         "position_observed", "player_status", "_room_actor_presence_v20"):
                cache = getattr(self, name)
                for slot in changed:
                    cache.pop(slot, None)
            if changed:
                self._attachment_events[:] = [e for e in self._attachment_events if e["slot"] not in changed]
                self.releases.clear()
                self.level_changes.clear()
            self.placeables.retain_owners(slots.values())
            self.slots = slots
            self.roster_at = time.monotonic()

    def _origins(self, data, *, room_generation=None, attachment_sample=None, presence_scope=None):
        off, now, now_mono = 0, time.time(), time.monotonic()
        while off + 5 <= len(data):
            slot, size = data[off], struct.unpack_from("<I", data, off + 1)[0]
            if not size or off + 5 + size > len(data):
                break
            if attachment_sample is not None:
                self._observe_attachment(slot, bytes(data[off + 5:off + 5 + size]),
                                         room_generation, attachment_sample, now_mono)
            if size >= 13:
                x, y, z = struct.unpack_from("<3f", data, off + 5 + 1)
                if not all(abs(v) < 10000 for v in (x, y, z)):   # 不是角色记录（或 NaN），跳过
                    off += 5 + size
                    continue
                with self.lock:
                    if not self.ready or room_generation != self.room_generation:
                        return
                    if slot not in self.slots:
                        off += 5 + size
                        continue
                    identity = self.slots[slot]
                    slot_version = self._slot_versions.get(slot)
                    seat_epoch = self.seat_epoch
                    self.origins[slot] = (x, y, z, now)
                    self.position_observed[slot] = (now, now_mono)
                    self._room_actor_presence_v20.pop(slot, None)
                    if (presence_scope is not None and presence_scope[0] is self._engine
                            and presence_scope[1] is getattr(self._engine, "_player", None)
                            and presence_scope[2:] == (self.epoch, self.room_generation, self.confirmed_map_id)
                            and type(slot_version) is int and slot_version >= 0):
                        self._room_actor_presence_v20[slot] = (*presence_scope, identity, slot_version, now, now_mono)
                    if slot in self._terrain_slots:
                        self._add_height(x, y, z)
                        prev = self.last_pos.get(slot)
                        self.last_pos[slot] = (x, y, z, now)
                        if prev is not None:
                            self._fill_trail(prev, (x, y, z, now))
                    else:
                        self.last_pos.pop(slot, None)
                with self.lock:
                    if (not self.ready or room_generation != self.room_generation
                            or self.slots.get(slot) != identity):
                        return
                    scan_seat = now - self.seat_checked.get(slot, 0) >= SEAT_SCAN_SECONDS
                    if scan_seat:
                        self.seat_checked[slot] = now
                    status_previous = self.player_status.get(slot)
                    scan_status = (status_previous is None or
                                   now_mono - status_previous["observed_mono"] >= SEAT_SCAN_SECONDS)
                if scan_status:
                    # Separate monotonic throttle: wall-clock adjustment must not
                    # freeze the read-only feed or alter the existing social scan.
                    self._update_player_status(slot, bytes(data[off + 5:off + 5 + size]), now, now_mono,
                                               room_generation, identity, slot_version)
                if scan_seat:   # 完整解析较贵，每人每秒一次
                    actor_body = bytes(data[off + 5:off + 5 + size])
                    self._record_owner(slot, actor_body, now)
                    st = anim_pose.social_state(actor_body)
                    seat_profile = None
                    if st is not None and st.get("seat"):
                        try:
                            import seat_tilt
                            seat_profile = seat_tilt.observe(actor_body)
                        except (ValueError, TypeError, OverflowError):
                            pass
                    if st is not None:
                        with self.lock:
                            if (not self.ready or room_generation != self.room_generation
                                    or self.slots.get(slot) != identity):
                                return
                            if (self.confirmed_map_id == HOME_MAP_ID
                                    and (self.seat_epoch != seat_epoch or self._slot_versions.get(slot) != slot_version)):
                                return
                            if st["seat"] and slot in self._terrain_slots:
                                self.seated[slot] = st["seat"]
                                if self.confirmed_map_id != HOME_MAP_ID or identity != self.own_uuid:
                                    self.seat_book[st["seat"]] = ((x, y, z), st["yaw"], now)
                                    if seat_profile is not None:
                                        if st["seat"] not in self.seat_pose_book and len(self.seat_pose_book)>=256:
                                            self.seat_pose_book.pop(next(iter(self.seat_pose_book)))
                                        self.seat_pose_book[st["seat"]] = dict(profile=seat_profile,
                                            room_generation=room_generation, seat_epoch=seat_epoch,
                                            slot=slot, identity=identity, slot_version=slot_version)
                                if self.confirmed_map_id == HOME_MAP_ID and identity != self.own_uuid:
                                    self.seat_witnesses[st["seat"]] = (
                                        identity, slot, slot_version, room_generation, seat_epoch, now_mono)
                            else:
                                self.seated.pop(slot, None)
                            if st.get("mode") is not None:
                                self.modes[slot] = st["mode"]
                            self.yaws[slot] = st.get("yaw", 0)
                            self.candle_targets[slot] = st.get("candle_target") or 0
                            if st["offer"]:
                                self.offers[slot] = (st["offer"][0], st["offer"][1], now)
                            else:
                                self.offers.pop(slot, None)
            off += 5 + size

    @staticmethod
    def _actor_social_fields(body):
        """One native Actor parse; keep identifiers private until projected to booleans."""
        observed = anim_pose.inspect_actor_wire(body, verified_profile_sha256=anim_pose.SHA,
                                                check_zero_padding=False)
        starts, values = anim_pose._starts(observed), {}
        def field(label, kind, width):
            positions = [pos for name, pos in observed._boundaries if name == label]
            if len(positions) != 1:
                raise ValueError("attachment_field_shape")
            index = next(i for i, pos in enumerate(starts) if pos >= positions[0])
            token = observed._tokens[index]
            if (token.kind, token.extent) != (kind, width):
                raise ValueError("attachment_token_shape")
            return int.from_bytes(token.value, "little") if kind == "raw" else token.value
        for label, kind, width, name in (("0x21de648", "raw", 1, "carrier_slot"),
                ("0x21df130", "raw", 1, "leader_slot"), ("0x21df158", "bits", 2, "hold_state")):
            values[name] = field(label, kind, width)
        # Animation failure must not turn valid carry/hold data into detachment.
        try:
            loc = anim_pose._locate(observed)
            animation = {name:anim_pose._val(name, observed._tokens[loc[name]]) if name in loc else None
                         for name in ("mode", "code90", "gen", "key", "start", "h_gen", "h_key", "h_start")}
            animation["once_present"] = bool(field("0x21df190", "bits", 1))
            animation["once_target"] = field("0x21df1a8", "raw", 1) if animation["once_present"] else None
            values["animation"] = animation
        except Exception:
            values["animation"] = None
        return values

    def _observe_attachment(self, slot, body, generation, sample, observed_mono):
        """Decode recipient-side fields; parse failure is unknown, not detached."""
        with self.lock:
            if not self.ready or generation != self.room_generation or slot not in self.slots:
                return
            identity, version = self.slots[slot], self._slot_versions.get(slot)
        try:
            values = self._actor_social_fields(body)
        except Exception:
            values = None
        with self.lock:
            if (not self.ready or generation != self.room_generation
                    or self.slots.get(slot) != identity or self._slot_versions.get(slot) != version):
                return
            self._attachment_events.append({"sample_seq": sample[0], "source_slot": sample[1],
                "wire_sequence": sample[2], "slot": slot, "uuid": identity, "version": version,
                "room_generation": generation, "observed_mono": observed_mono, "value": values})
            del self._attachment_events[:-128]

    def _update_player_status(self, slot, body, observed_at, observed_mono,
                              room_generation, identity, slot_version):
        """Decode outside the lock, then bind the observation to this exact occupant.

        Failure does not silently retain the previous appearance as current. No
        native labels, account secrets or guessed energy fields are added here.
        """
        try:
            value = player_status.read(body)
            reason = None
        except Exception:
            value, reason = None, "decode_failed"
        with self.lock:
            if (not self.ready or room_generation != self.room_generation
                    or self.slots.get(slot) != identity
                    or self._slot_versions.get(slot) != slot_version):
                return
            self.player_status[slot] = {
                "value": value, "observed_at": observed_at,
                "observed_mono": observed_mono, "reason": reason,
            }

    def _add_height(self, x, y, z):
        key = (round(x / HEIGHT_CELL), round(z / HEIGHT_CELL))
        cell = self.heights.setdefault(key, [])
        cell.append(y)
        del cell[:-20]
        self.height_total[key] = self.height_total.get(key, 0) + 1

    def _fill_trail(self, a, b):
        """同一玩家相邻两次位置之间补上经过的格子（只补像是在地上走/跑的段）。"""
        dx, dy, dz, dt = b[0] - a[0], b[1] - a[1], b[2] - a[2], b[3] - a[3]
        d = (dx * dx + dz * dz) ** 0.5
        if not (TRAIL_MIN <= d <= TRAIL_MAX) or not (0 < dt <= TRAIL_MAX_DT) or d / dt > TRAIL_MAX_SPEED \
                or abs(dy) > TRAIL_MAX_SLOPE * d:
            return
        n = int(d / (HEIGHT_CELL * 0.5))
        done = {(round(a[0] / HEIGHT_CELL), round(a[2] / HEIGHT_CELL)), (round(b[0] / HEIGHT_CELL), round(b[2] / HEIGHT_CELL))}
        for i in range(1, n):
            f = i / n
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            key = (round(x / HEIGHT_CELL), round(z / HEIGHT_CELL))
            if key not in done:
                done.add(key)
                self._add_height(x, y, z)

    def _record_owner(self, slot, body, now):
        """研究用：开关 control/record_owner 存在时，把哥哥的 Actor 原始字节记下来。"""
        ctl = Path(xg_paths.CONTROL)
        if not (ctl / "record_owner").exists():
            return
        try:
            owner = (ctl / "owner_uuid").read_text().strip()
        except OSError:
            return
        if self.slots.get(slot) != owner:
            return
        f = ctl / "owner_actor.jsonl"
        with f.open("a") as out:
            out.write(json.dumps({"t": round(now, 2), "hex": body.hex()}) + "\n")

    def ground_y(self, x, z):
        """该处玩家站过的高度（中位数）；没人去过返回 None。飞行中的高度会混进来，所以取中位数。

        2026-11-06 起只留给统计/回放（walk_<map>.json）；walkmap 路线图仍可拿它当参考高度，
        但小光的实际落点统一由 PoseController.terrain_ground 的真实地形决定。
        """
        with self.lock:
            ys = self.heights.get((round(x / HEIGHT_CELL), round(z / HEIGHT_CELL)))
            return sorted(ys)[len(ys) // 2] if ys else None

    def resolve(self, target):
        """target 可以是 uuid 或 slot（十六进制/十进制）。返回 (slot, 坐标) 或抛错。"""
        with self.lock:
            slot = None
            if isinstance(target, str) and "-" in target:
                slot = next((s for s, u in self.slots.items() if u == target), None)
            else:
                slot = int(str(target), 16) if str(target).lower().startswith("0x") else int(target)
            if slot is None:
                raise LookupError("目标不在当前房间名单里")
            o = self.origins.get(slot)
            if o is None:
                raise LookupError("还没收到目标的坐标")
            return slot, o[:3]

    def offers_to_me(self):
        """正在邀请小光做双人互动的人：[{"from": uuid, "type": 关系能力 id}]。"""
        with self.lock:
            me = next((s for s, u in self.slots.items() if u == self.own_uuid), None)
            if me is None:
                return []
            return [{"from": self.slots[s], "type": t} for s, (t, target, _ts) in self.offers.items()
                    if target == me and s in self.slots]

    def snapshot(self):
        with self.lock:
            generated_at, now_mono = time.time(), time.monotonic()

            def observation(value, clocks=None, reason=None):
                observed_at, observed_mono = clocks if clocks is not None else (None, None)
                age = max(0.0, now_mono - observed_mono) if observed_mono is not None else None
                if not self.ready:
                    freshness, reason = "unknown", "disconnected"
                elif value is None or clocks is None:
                    freshness, reason = "unknown", reason or "unobserved"
                elif age > PLAYER_STATUS_MAX_AGE:
                    freshness, reason = "stale", "expired"
                else:
                    freshness, reason = "fresh", None
                return {"value": copy.deepcopy(value) if freshness == "fresh" else None,
                        "observed_at": observed_at,
                        "age_seconds": round(age, 3) if age is not None else None,
                        "freshness": freshness, "reason": reason}

            players, origins, candles = {}, {}, {}
            for slot, identity in self.slots.items():
                key = hex(slot)
                origin = self.origins.get(slot)
                position = observation([round(v, 3) for v in origin[:3]] if origin else None,
                                       self.position_observed.get(slot))
                cached = self.player_status.get(slot)
                status = observation(cached["value"] if cached else None,
                                     (cached["observed_at"], cached["observed_mono"]) if cached else None,
                                     cached["reason"] if cached else None)
                players[key] = {"uuid": identity, "position": position, "status": status}
                if position["freshness"] == "fresh":
                    origins[key] = position["value"]
                if status["freshness"] == "fresh":
                    target = status["value"].get("social", {}).get("candle_target_slot")
                    if target:
                        candles[key] = hex(target)
            return {"map_id": self.confirmed_map_id, "pending_map_id": self.pending_map_id,
                    "room_generation": self.room_generation, "connected": self.ready,
                    "generated_at": generated_at, "status_schema_version": ROOM_STATUS_SCHEMA,
                    "status_max_age_seconds": PLAYER_STATUS_MAX_AGE,
                    "roster_errors": getattr(self, "roster_errors", 0), "last_roster_error": getattr(self, "last_roster_error", None),
                    "slots": {hex(s): u for s, u in self.slots.items()}, "players": players,
                    "origins": origins, "candles": candles}


class _SocialOfferEvent:
    """Verified catalog inviter clip, retained through acceptance rather than replayed.

    Identity uses the existing AnimEvent writer. History advances only after a
    final Actor is built, following native previous-snapshot history semantics.
    """
    def __init__(self, code, previous, created):
        entry = social_invite.resolve_action(code)
        if (not entry.get("supported") or entry.get("gift_or_unlock")
                or not isinstance(entry.get("offer_animation"), str) or not entry["offer_animation"]):
            raise ValueError("unsupported invitation animation")
        self.animation = entry["offer_animation"]
        self.identity = anim_pose.AnimEvent(anim_pose.anim_key(self.animation), previous, created)
        self.t0 = None
        self.history = dict(previous)
        self.published_frames = 0

    def apply(self, body, now, mode=6):
        if self.identity.start is None:
            self.identity.start = anim_pose._required_clock(body)
        if self.t0 is None:
            self.t0 = now
        age = max(0.0, now - self.t0)
        return anim_pose.write(body, gen=self.identity.gen, key=self.identity.key, start=self.identity.start,
                               h_gen=self.history["gen"], h_key=self.history["key"],
                               h_start=self.history["start"] >> 3, mode=mode,
                               code90=anim_pose._code(anim_pose.MODE7_CODES, age))

    def post(self, body, now):
        # No once-duration restore: native invite has Enter then repeating Loop.
        return self.apply(body, now, mode=6)

    def stamp(self):
        return self.identity.stamp()

    def note_published(self, body):
        current = anim_pose.read(body)
        if any(current[key] != self.stamp()[key] for key in ("gen", "key", "start")):
            return False
        self.history = {key:current[key] for key in ("gen", "key", "start")}
        self.published_frames += 1
        return True


def _accepted_social_profile(entry, tail):
    """Only a catalog-validated whole pair may supply generic loop/once flags."""
    if entry.get("accepted_profile") in ("hold", "carrier"):
        return entry["accepted_profile"]
    if not isinstance(tail, (tuple, list, bytes)) or len(tail) != 3 or tail[0] not in (0, 1):
        raise ValueError("unsupported accepted social profile")
    return "loop" if tail[0] == 1 else "once"


class _DanceLoop:
    """Continuous social mode is independent of ordinary animation identity.

    A giver retains its prior event/seed gen, key and start. An accepting avatar
    retains its existing zero-key acceptance event. Only mode/blend is sustained.
    """

    def __init__(self, inner, started=None, previous=None):
        self.inner = inner
        self.t0 = time.monotonic() if started is None else started
        self.previous = dict(previous) if previous is not None else (inner.stamp() if inner is not None else None)

    def post(self, body, now):
        out = self.inner.post(body, now) if self.inner is not None else body
        self.previous = {k:anim_pose.read(out)[k] for k in ("gen", "key", "start")}
        code90 = anim_pose._code(anim_pose.MODE7_CODES, max(0.0, now - self.t0))
        return anim_pose.write(out, mode=9, code90=code90)

    def stamp(self):
        if self.previous is None:
            raise ValueError("dance animation stamp is not available before its first frame")
        return dict(self.previous)



class _OnceSocialEvent(_DanceLoop):
    """Accepted giver retains invitation identity; fresh peer frames end it."""
    def post(self, body, now):
        out = self.inner.post(body, now)
        self.previous = {k:anim_pose.read(out)[k] for k in ("gen", "key", "start")}
        return anim_pose.write(out, mode=7,
                               code90=anim_pose._code(anim_pose.MODE7_CODES, max(0.0, now-self.t0)))


def _control_dir_for(control_path, account=None):
    """按号状态目录：5b12/没传 account 时用 pose.json 所在目录（老位置），其它号用
    xg_account.state_dir(account)（control/accounts/<id>/）。取不到就退回老位置。"""
    base = Path(control_path).resolve().parent
    if account:
        try:
            import xg_account
            return xg_account.state_dir(account)
        except Exception:
            return base
    return base



# 仅接管目录明确列出的单人 Enter/Exit，绝不根据双人动作名称猜测。
SOLO_EMOTE_EXITS = {
    anim_pose.anim_key('AmazedEmote_1Enter'): anim_pose.anim_key('AmazedEmote_1Exit'),
    anim_pose.anim_key('AmazedEmote_2Enter'): anim_pose.anim_key('AmazedEmote_2Exit'),
    anim_pose.anim_key('ApologizeEmote_1Enter'): anim_pose.anim_key('ApologizeEmote_1Exit'),
    anim_pose.anim_key('ApologizeEmote_2Enter'): anim_pose.anim_key('ApologizeEmote_2Exit'),
    anim_pose.anim_key('ArmWaveEmote_1Enter'): anim_pose.anim_key('ArmWaveEmote_1Exit'),
    anim_pose.anim_key('ArmWaveEmote_2Enter'): anim_pose.anim_key('ArmWaveEmote_2Exit'),
    anim_pose.anim_key('BalanceEmote_1Enter'): anim_pose.anim_key('BalanceEmote_1Exit'),
    anim_pose.anim_key('BalanceEmote_2Enter'): anim_pose.anim_key('BalanceEmote_2Exit'),
    anim_pose.anim_key('BallTrickEmote_1Enter'): anim_pose.anim_key('BallTrickEmote_1Exit'),
    anim_pose.anim_key('BallTrickEmote_2Enter'): anim_pose.anim_key('BallTrickEmote_2Exit'),
    anim_pose.anim_key('BaskEmote_1Enter'): anim_pose.anim_key('BaskEmote_1Exit'),
    anim_pose.anim_key('BaskEmote_2Enter'): anim_pose.anim_key('BaskEmote_2Exit'),
    anim_pose.anim_key('BellyScratchEmote_1Enter'): anim_pose.anim_key('BellyScratchEmote_1Exit'),
    anim_pose.anim_key('BellyScratchEmote_2Enter'): anim_pose.anim_key('BellyScratchEmote_2Exit'),
    anim_pose.anim_key('BreakDanceEmote_1Enter'): anim_pose.anim_key('BreakDanceEmote_1Exit'),
    anim_pose.anim_key('BreakDanceEmote_2Enter'): anim_pose.anim_key('BreakDanceEmote_2Exit'),
    anim_pose.anim_key('ButterflyEmote_1Enter'): anim_pose.anim_key('ButterflyEmote_1Exit'),
    anim_pose.anim_key('ButterflyEmote_2Enter'): anim_pose.anim_key('ButterflyEmote_2Exit'),
    anim_pose.anim_key('ColdEmote_1Enter'): anim_pose.anim_key('ColdEmote_1Exit'),
    anim_pose.anim_key('ColdEmote_2Enter'): anim_pose.anim_key('ColdEmote_2Exit'),
    anim_pose.anim_key('ComeWithMeEmote_1Enter'): anim_pose.anim_key('ComeWithMeEmote_1Exit'),
    anim_pose.anim_key('ComeWithMeEmote_2Enter'): anim_pose.anim_key('ComeWithMeEmote_2Exit'),
    anim_pose.anim_key('ConductEmote_1Enter'): anim_pose.anim_key('ConductEmote_1Exit'),
    anim_pose.anim_key('ConductEmote_2Enter'): anim_pose.anim_key('ConductEmote_2Exit'),
    anim_pose.anim_key('CrabwalkEmote_1Enter'): anim_pose.anim_key('CrabwalkEmote_1Exit'),
    anim_pose.anim_key('CrabwalkEmote_2Enter'): anim_pose.anim_key('CrabwalkEmote_2Exit'),
    anim_pose.anim_key('CuteclapEmote_1Enter'): anim_pose.anim_key('CuteclapEmote_1Exit'),
    anim_pose.anim_key('CuteclapEmote_2Enter'): anim_pose.anim_key('CuteclapEmote_2Exit'),
    anim_pose.anim_key('DizzyEmote_1Enter'): anim_pose.anim_key('DizzyEmote_1Exit'),
    anim_pose.anim_key('DizzyEmote_2Enter'): anim_pose.anim_key('DizzyEmote_2Exit'),
    anim_pose.anim_key('DozeEmote_1Enter'): anim_pose.anim_key('DozeEmote_1Exit'),
    anim_pose.anim_key('DozeEmote_2Enter'): anim_pose.anim_key('DozeEmote_2Exit'),
    anim_pose.anim_key('DrawEmote_1Enter'): anim_pose.anim_key('DrawEmote_1Exit'),
    anim_pose.anim_key('DrawEmote_2Enter'): anim_pose.anim_key('DrawEmote_2Exit'),
    anim_pose.anim_key('DuckEmote_1Enter'): anim_pose.anim_key('DuckEmote_1Exit'),
    anim_pose.anim_key('DuckEmote_2Enter'): anim_pose.anim_key('DuckEmote_2Exit'),
    anim_pose.anim_key('FaintEmote_1Enter'): anim_pose.anim_key('FaintEmote_1Exit'),
    anim_pose.anim_key('FaintEmote_2Enter'): anim_pose.anim_key('FaintEmote_2Exit'),
    anim_pose.anim_key('FloatEmote_1Enter'): anim_pose.anim_key('FloatEmote_1Exit'),
    anim_pose.anim_key('FloatEmote_2Enter'): anim_pose.anim_key('FloatEmote_2Exit'),
    anim_pose.anim_key('GriefEmote_1Enter'): anim_pose.anim_key('GriefEmote_1Exit'),
    anim_pose.anim_key('GriefEmote_2Enter'): anim_pose.anim_key('GriefEmote_2Exit'),
    anim_pose.anim_key('HandstandEmote_1Enter'): anim_pose.anim_key('HandstandEmote_1Exit'),
    anim_pose.anim_key('HandstandEmote_2Enter'): anim_pose.anim_key('HandstandEmote_2Exit'),
    anim_pose.anim_key('HeadbobEmote_1Enter'): anim_pose.anim_key('HeadbobEmote_1Exit'),
    anim_pose.anim_key('HeadbobEmote_2Enter'): anim_pose.anim_key('HeadbobEmote_2Exit'),
    anim_pose.anim_key('HypedanceEmote_1Enter'): anim_pose.anim_key('HypedanceEmote_1Exit'),
    anim_pose.anim_key('HypedanceEmote_2Enter'): anim_pose.anim_key('HypedanceEmote_2Exit'),
    anim_pose.anim_key('JellyDanceEmote_1Enter'): anim_pose.anim_key('JellyDanceEmote_1Exit'),
    anim_pose.anim_key('JellyDanceEmote_2Enter'): anim_pose.anim_key('JellyDanceEmote_2Exit'),
    anim_pose.anim_key('JollyDanceEmote_1Enter'): anim_pose.anim_key('JollyDanceEmote_1Exit'),
    anim_pose.anim_key('JollyDanceEmote_2Enter'): anim_pose.anim_key('JollyDanceEmote_2Exit'),
    anim_pose.anim_key('LoopDanceEmote_1Enter'): anim_pose.anim_key('LoopDanceEmote_1Exit'),
    anim_pose.anim_key('LoopDanceEmote_2Enter'): anim_pose.anim_key('LoopDanceEmote_2Exit'),
    anim_pose.anim_key('MarchingEmote_1Enter'): anim_pose.anim_key('MarchingEmote_1Exit'),
    anim_pose.anim_key('MarchingEmote_2Enter'): anim_pose.anim_key('MarchingEmote_2Exit'),
    anim_pose.anim_key('OuchEmote_1Enter'): anim_pose.anim_key('OuchEmote_1Exit'),
    anim_pose.anim_key('OuchEmote_2Enter'): anim_pose.anim_key('OuchEmote_2Exit'),
    anim_pose.anim_key('PeekEmote_1Enter'): anim_pose.anim_key('PeekEmote_1Exit'),
    anim_pose.anim_key('PeekEmote_2Enter'): anim_pose.anim_key('PeekEmote_2Exit'),
    anim_pose.anim_key('PenguinDanceEmote_1Enter'): anim_pose.anim_key('PenguinDanceEmote_1Exit'),
    anim_pose.anim_key('PointEmote_1Enter'): anim_pose.anim_key('PointEmote_1Exit'),
    anim_pose.anim_key('PointEmote_2Enter'): anim_pose.anim_key('PointEmote_2Exit'),
    anim_pose.anim_key('PointupEmote_1Enter'): anim_pose.anim_key('PointupEmote_1Exit'),
    anim_pose.anim_key('PointupEmote_2Enter'): anim_pose.anim_key('PointupEmote_2Exit'),
    anim_pose.anim_key('PrayEmote_1Enter'): anim_pose.anim_key('PrayEmote_1Exit'),
    anim_pose.anim_key('PrayEmote_2Enter'): anim_pose.anim_key('PrayEmote_2Exit'),
    anim_pose.anim_key('PullupEmote_1Enter'): anim_pose.anim_key('PullupEmote_1Exit'),
    anim_pose.anim_key('PullupEmote_2Enter'): anim_pose.anim_key('PullupEmote_2Exit'),
    anim_pose.anim_key('RaiseTheRoofEmote_1Enter'): anim_pose.anim_key('RaiseTheRoofEmote_1Exit'),
    anim_pose.anim_key('RaiseTheRoofEmote_2Enter'): anim_pose.anim_key('RaiseTheRoofEmote_2Exit'),
    anim_pose.anim_key('ReadingEmote_1Enter'): anim_pose.anim_key('ReadingEmote_1Exit'),
    anim_pose.anim_key('ReadingEmote_2Enter'): anim_pose.anim_key('ReadingEmote_2Exit'),
    anim_pose.anim_key('RhythmicClapEmote_1Enter'): anim_pose.anim_key('RhythmicClapEmote_1Exit'),
    anim_pose.anim_key('RhythmicClapEmote_2Enter'): anim_pose.anim_key('RhythmicClapEmote_2Exit'),
    anim_pose.anim_key('SaluteEmote_1Enter'): anim_pose.anim_key('SaluteEmote_1Exit'),
    anim_pose.anim_key('SaluteEmote_2Enter'): anim_pose.anim_key('SaluteEmote_2Exit'),
    anim_pose.anim_key('SlouchEmote_1Enter'): anim_pose.anim_key('SlouchEmote_1Exit'),
    anim_pose.anim_key('SlouchEmote_2Enter'): anim_pose.anim_key('SlouchEmote_2Exit'),
    anim_pose.anim_key('SparklerEmote_1Enter'): anim_pose.anim_key('SparklerEmote_1Exit'),
    anim_pose.anim_key('SparklerEmote_2Enter'): anim_pose.anim_key('SparklerEmote_2Exit'),
    anim_pose.anim_key('SpinTrickEmote_1Enter'): anim_pose.anim_key('SpinTrickEmote_1Exit'),
    anim_pose.anim_key('SpinTrickEmote_2Enter'): anim_pose.anim_key('SpinTrickEmote_2Exit'),
    anim_pose.anim_key('ThumbsUpEmote_2Enter'): anim_pose.anim_key('ThumbsUpEmote_2Exit'),
    anim_pose.anim_key('UseTheForceEmote_1Enter'): anim_pose.anim_key('UseTheForceEmote_1Exit'),
    anim_pose.anim_key('UseTheForceEmote_2Enter'): anim_pose.anim_key('UseTheForceEmote_2Exit'),
    anim_pose.anim_key('VoilaEmote_1Enter'): anim_pose.anim_key('VoilaEmote_1Exit'),
    anim_pose.anim_key('VoilaEmote_2Enter'): anim_pose.anim_key('VoilaEmote_2Exit'),
    anim_pose.anim_key('WaitEmote_1Enter'): anim_pose.anim_key('WaitEmote_1Exit'),
    anim_pose.anim_key('WaitEmote_2Enter'): anim_pose.anim_key('WaitEmote_2Exit'),
    anim_pose.anim_key('WelcomeEmote_1Enter'): anim_pose.anim_key('WelcomeEmote_1Exit'),
    anim_pose.anim_key('WelcomeEmote_2Enter'): anim_pose.anim_key('WelcomeEmote_2Exit'),
    anim_pose.anim_key('WindPoseEmote_1Enter'): anim_pose.anim_key('WindPoseEmote_1Exit'),
    anim_pose.anim_key('WindPoseEmote_2Enter'): anim_pose.anim_key('WindPoseEmote_2Exit'),
    anim_pose.anim_key('WriteEmote_1Enter'): anim_pose.anim_key('WriteEmote_1Exit'),
    anim_pose.anim_key('WriteEmote_2Enter'): anim_pose.anim_key('WriteEmote_2Exit'),
}


def _solo_action(function):
    """单人持续表情的非阻塞动作屏障；没有单人事件时原方法直接运行。"""
    from functools import wraps

    @wraps(function)
    def guarded(self, *args, **kwargs):
        def invoke():
            if function.__name__ == "_air_start":
                return self._air_ground_start(function, args, kwargs)
            return function(self, *args, **kwargs)

        # 已有社交连锁的锁顺序/结束分支完全由旧方法负责。
        if self.social is not None or self._active_running():
            return invoke()
        # _apply 的外观/转向设置不算换动作；社交命令始终由原代码处理。
        if function.__name__ == "_apply" and args and args[0].get("pose") in (
                "face", "voice", "outfit", "cancel_active_offer"):
            return invoke()
        if function.__name__ == "walk_to" and (args[1] if len(args) > 1 else kwargs.get("label")) == "bounded_pilot":
            gate = getattr(self, "_solo_gate", None)
            if gate is not None or isinstance(self.event, anim_pose.ContinuousEmoteEvent):
                adapter = getattr(self, "_terrain_adapter", None)
                prepared = getattr(adapter, "_prepared", None)
                with self.__dict__.setdefault("_solo_lock", threading.RLock()):
                    # 只拒绝这次等活动屏障的提交；收尾结束后的新调用完全走原入口。
                    if prepared is not getattr(adapter, "_prepared", None):
                        return False
                    if self._solo_defer(function, args, kwargs):
                        return None
                    return invoke()
        event = self.event
        solo_play = (function.__name__ == "play" and args and
                     (anim_pose.anim_key(args[0]) if isinstance(args[0], str) else args[0]) in SOLO_EMOTE_EXITS)
        if (not solo_play and not isinstance(event, anim_pose.ContinuousEmoteEvent)
                and getattr(self, "_solo_gate", None) is None):
            return invoke()
        lock = self.__dict__.setdefault("_solo_lock", threading.RLock())
        with lock:
            if function.__name__ in ("switch_map", "reset", "relocate"):
                return self._solo_lifecycle(function, args, kwargs)
            if self._solo_defer(function, args, kwargs):
                return True if function.__name__ in ("_air_start", "_sit_direct") else None
            return invoke()
    return guarded


def _home_seat_action(function):
    """遇境座位事务沿用 solo RLock；非遇境不加锁、不改原方法。"""
    from functools import wraps

    @wraps(function)
    def guarded(self, *args, **kwargs):
        if function.__name__ == "_apply":
            cmd = args[0] if args else kwargs.get("cmd", {})
            home_wander_sit = (cmd.get("pose") == "sit" and ("home_seat_context" in cmd
                               or (cmd.get("src") == "wander" and self._is_home_map())))
            if cmd.get("pose") == "_sit_now" and ("home_request" in cmd or self._is_home_map()):
                # 旧到达回调必须在 solo/active/command_seq/route 的任何副作用之前丢弃。
                with self.__dict__.setdefault("_solo_lock", threading.RLock()):
                    pending = getattr(self, "_home_sit_pending", None)
                    if (not self._is_home_map() or pending is None
                            or cmd.get("home_request") != pending["request"]):
                        self.log("sit_arrival_ignored", reason="stale_home_request", seat=cmd.get("seat"))
                        return
                    if self._home_sit_taken_over():
                        return
                    return self._sit_now(cmd["seat"], cmd["info"], home_request=cmd["home_request"])
            if home_wander_sit or (self._is_home_map() and cmd.get("pose") not in ("shout", "voice")):
                with self.__dict__.setdefault("_solo_lock", threading.RLock()):
                    if home_wander_sit and not self._home_wander_context_matches(cmd.get("home_seat_context")):
                        self._reject_home_wander_sit("遇境座位状态未确认或房间已变化")
                        return
                    self._home_command_generation = getattr(self, "_home_command_generation", 0) + 1
                    command_generation = self._home_command_generation
                    self._home_cancel_sit()
                kwargs = dict(kwargs, _home_command=command_generation)
            return function(self, *args, **kwargs)
        if not self._is_home_map():
            return function(self, *args, **kwargs)
        if function.__name__ == "_home_sit_tick" and self._prop_seat_dynamic_work():
            lock = self.__dict__.setdefault("_solo_lock", threading.RLock())
            if not lock.acquire(False):
                return False
            try:
                return function(self, *args, **kwargs)
            finally:
                lock.release()
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            if function.__name__ == "_sit_now":
                seat, info = args[:2]
                pending = getattr(self, "_home_sit_pending", None)
                if pending is None or kwargs.get("home_request") != pending["request"]:
                    self.log("sit_arrival_ignored", reason="stale_home_request", seat=seat)
                    return
                if self._home_sit_taken_over():
                    return
                # 名单/epoch 更新与坐姿提交共享 tracker.lock，消除检查后换人的空窗。
                if not self._prop_seat_arrival_check():
                    return False
                if not self.tracker.lock.acquire(False):
                    return self._prop_seat_defer_commit(seat, info, kwargs.get("home_request"))
                try:
                    reason = self._home_sit_invalid(seat, info, locked=True)
                    if reason is None:
                        return function(self, *args, **dict(kwargs, _tracker_locked=True))
                finally:
                    self.tracker.lock.release()
                self._home_sit_failed("seat_taken", seat, reason, expected=pending)
                return
            return function(self, *args, **kwargs)
    return guarded


class PoseController(SeatTiltControllerMixin, threading.Thread):
    def __init__(self, control_path, seed, tracker, log, stop, account=None):
        super().__init__(daemon=True, name="pose-control")
        # 2026-11-07 Claude：CONTROL_DIR 原来按 room_pose.py 所在位置推成 release-v20-hop-trial/control，
        # 和聊天进程/wardrobe 用的 sky-ai/control 不是同一个目录（叫声名单读不到、站姿持久化读错目录）。
        # 统一成 pose.json 所在目录。
        # 2026-11-08 T24-2：换装/叫声按号存（5b12 还是 control/，其它号 control/accounts/<id>/）。
        # account 由 long_runner 传进来；没传（老调用/离线测试）就沿用 pose.json 所在目录。
        global CONTROL_DIR
        CONTROL_DIR = _control_dir_for(control_path, account)
        self.raw_seed = seed               # 录制原样（引擎没有覆盖时发的就是它）
        seed = with_appearance(social_pose.stand(seed))     # 归成站立；站姿/装扮按 control/attitude + control/outfit.json
        self.path, self.seed, self.tracker, self.log, self.stop = Path(control_path), seed, tracker, log, stop
        self.seed_heading = anim_pose.yaw_heading(anim_pose.read(seed)["yaw1"])   # 种子录制时的朝向
        self.base = seed                   # 姿态底板（待机或举烛），动画字段由 post 按时间表覆盖
        self.last_mtime = None
        self.state = {"pose": "idle"}
        self.anim_lock = threading.Lock()
        self.event = None                  # 最近一次动画事件；结束后保持其“延续”状态，generation 只增不减
        self.home = anim_pose.read_origin(seed)   # 种子里的站位
        self.position = self.home          # 当前站位（走动结束后更新）
        self.motion = None
        self.heading = None                # 当前朝向（度）；None = 用种子原来的朝向
        # T23-1 空中跟随：只有 control/air_follow.json 的 enabled+follow 都为 true
        # 才会创建 AirMotion。文件不存在时 _air_motion 永远 None。
        self._air_motion = None
        self._air_cfg = None
        self._air_cfg_at = -1e9
        self._air_terrain_view = None
        self._air_terrain_key = None
        self._air_failed_at = None
        self._air_failed_target = None
        self._air_phase = None
        self._air_clips_cache = None
        # T23-2 叫声：命令走 self.event + anim_lock；随机叫只在 explore/idle 且
        # control/shout.json 明确打开时发生（文件不存在 = 永远不随机叫）。
        self._shout_cfg = None
        self._shout_cfg_at = -1e9
        self._shout_next_at = None
        self._last_shout_at = 0.0
        self._last_command_at = 0.0
        self._explore_active = False
        clip = Path(__file__).with_name("walk_clip.json")
        self.walk_clip = anim_pose.WalkClip(clip) if clip.exists() else None
        self.after_arrive = None           # 到达后要执行的命令（dict，同控制文件格式）
        self._home_sit_pending = None      # 仅遇境：走路到落座期间固定现场凭据
        self.follow = None                 # 跟随：{"target": uuid/slot, "distance": 米}；每秒检查一次
        self.arrival = None                # 换图后“到达贴身”：{"map_id", "leader", "since"...}，见 arrival_snap
        self.seats = {}                    # 座位号 -> {"origin": 玩家坐标, "yaw": 6 位偏航}（install 按地图加载）
        self.seat_file = None              # 座位表文件（实时学到的座位会写回）
        self.map_id = None                 # 当前图（换图/同图换落点时更新）
        self.variant = None                # 当前命名落点（None=原点）；交给带路协调器判断是否已到
        self._placement_ref_y = None       # 目录/标注给出的意图高度，只当真实地形选层参考
        self._placement_wire_y = None      # 落点自己的 Actor 原点高度（wire y）；地形没 ready 时的兜底
        self._variant_position = None      # 当前命名落点的实际落点（换种子/传送后由 relocate 记录）
        # 2026-11-07 T22：当前“登记落点”的来源和坐标。reset（validated_room /
        # new_room / rejoin）之后要靠它判断“现在还在不在命名落点”，不能一律
        # 退回引擎原来的主落点 Actor。
        self._landing = None               # {"map_id","variant","origin","source"}
        self._seed_source = "install"      # 诊断：当前 seed 是谁写进来的
        self._landing_applied = None       # 上次落点复核/传送的 (epoch, slot, origin)
        self._grounded = False             # 当前 home 是否已经被真实地形校正过（决定选层参考顺序）
        self._last_reground = 0.0          # 待机时最多每秒按真实地形校正一次
        self.sitting = None                # 正坐着的座位号
        self.stand_spot = None             # 坐下前站的位置（站起后回到这里）
        self._sit_pending = None           # 已接受但还没坐下的 sit 命令：{"since","seat","near","target"}
        self._last_sit_failure = None      # 最近一次坐下失败：{"reason","seat","at"}；写进 pose.state.json
        self._direct_walk_motion = None    # T23-8：绕过地形层、按真实地形直接走向座位的 Motion
        self._walk_ref = None              # T23-8：座位走路的稳定参照（喊坐下的玩家 UUID）
        self._command_seq = 0              # T23-8：命令序号；直走兜底只对“仍然是这次 sit”生效
        self.holding = False               # 正举着蜡烛（姿态不随朝向旋转）
        self.candle = None                 # 举烛目标：{"target", "origin"（上次瞄准时的位置）}
        self.candle_heading = candle_pose.template_heading()   # 举烛模板录制时的朝向
        self.social = None                 # 进行中的持续互动：{"kind": hold/carry, "who", "slot", "own", "since"}
        self._active_lock = threading.RLock()
        self._active_offer = None
        self._active_outcome = None
        self._active_result = None     # latest command result; never replaces a live ticket
        self._active_seen = set()

    def _solo_lifecycle(self, function, args, kwargs):
        """换房接口保持同步；旧房已关闭时在新种子上完成 Exit，不恢复旧命令。"""
        event = self.event
        gate = getattr(self, "_solo_gate", None)
        owned = isinstance(event, anim_pose.ContinuousEmoteEvent) or (
            gate is not None and event is gate["event"])
        if (not owned or getattr(self, "_solo_lifecycle_depth", 0)
                or self.social is not None or self._active_running()):
            return function(self, *args, **kwargs)
        if isinstance(event, anim_pose.ContinuousEmoteEvent):
            event = anim_pose.EmoteExitEvent(event.exit_key, event.stamp(), time.monotonic())
        self._solo_revision = getattr(self, "_solo_revision", 0) + 1
        self._solo_gate = None
        self.event = event
        self._solo_lifecycle_depth = 1
        try:
            return function(self, *args, **kwargs)
        finally:
            self._solo_lifecycle_depth = 0
            if (self.social is None and not self._active_running()
                    and (self.event is None or self.event is event)):
                # 房间相位时钟已换；旧 pending 不跨房，generation 不重复递增。
                event.start = None
                self.event = event
                self._solo_gate = {"event": event, "pending": None, "first_frame": None, "worker": False}
                self._set_override(self.base)
                self.state = {"pose": "anim", "phase": "solo_exit"}

    def _solo_defer(self, function, args, kwargs):
        """只结束自己的持续表情；等待实际构造完 Exit 帧后才执行后续动作。"""
        if self.social is not None or self._active_running():
            return False
        event = self.event
        gate = getattr(self, "_solo_gate", None)
        if isinstance(event, anim_pose.ContinuousEmoteEvent):
            # 重复同一 Enter 不重播、不自发退出。
            if function.__name__ == "play" and args:
                key = anim_pose.anim_key(args[0]) if isinstance(args[0], str) else args[0]
                if key == event.key:
                    return True
            if function.__name__ == "_apply" and args and args[0].get("pose") == "anim":
                if anim_pose.anim_key(args[0].get("name", "")) == event.key:
                    return True
            event = anim_pose.EmoteExitEvent(event.exit_key, event.stamp(), time.monotonic())
            self._solo_revision = getattr(self, "_solo_revision", 0) + 1
            self.event = event
            gate = {"event": event, "pending": None, "first_frame": None, "worker": False}
            self._solo_gate = gate
            self._set_override(self.base)
            self.state = {"pose": "anim", "phase": "solo_exit"}
            self.log("solo_emote_exit", generation=event.gen)
        if gate is None or self.event is not gate["event"]:
            return False
        # 换图/重进房优先于旧房间命令；手动命令优先于自动跟随重规划。
        name = function.__name__
        if name == "_solo_retry_walk" or (name == "walk_to" and (
                args[1] if len(args) > 1 else kwargs.get("label")) == "bounded_pilot"):
            # 地面层实例 wrapper 只排逻辑目标；这里是准备完成后的持许可起步。
            # 屏障拒绝后若留下 _prepared，entry 会永远认为还有请求待消费。
            # 必须经公开 cancel 撤销 request/lease，不能延迟重放这个旧许可。
            self._solo_cancel_ground()
            function = type(self)._solo_retry_walk
        priority = 3 if name in ("switch_map", "reset", "relocate") else 2 if name in (
            "_apply", "play", "_accept_offer", "_request_active_offer") else 1
        if name == "_apply" and args and args[0].get("src") == "wander":
            priority = 0
        if gate["pending"] is None or priority >= gate["pending"][3]:
            # 调用方可复用/修改 cmd，屏障必须持有当时的命令快照。
            gate["pending"] = (function, copy.deepcopy(args), copy.deepcopy(kwargs), priority)
        return True

    def _solo_cancel_ground(self):
        """只撤销单人屏障期间的路线许可，不改变地面层的任何校验。"""
        if getattr(self, "_solo_gate", None) is None:
            return
        cancel = getattr(getattr(self, "_terrain_adapter", None), "cancel", None)
        if callable(cancel):
            cancel("solo_emote_exit")
            self.log("solo_emote_ground_retry", reason="solo_emote_exit")

    def _solo_ground_dispatch(self, dispatch, target, speed):
        """同锁保护地面/独立小跳的准备提交；非单人状态执行原分流一次。"""
        gate = getattr(self, "_solo_gate", None)
        active = gate is not None or isinstance(self.event, anim_pose.ContinuousEmoteEvent)
        adapter = getattr(self, "_terrain_adapter", None)
        prepared = getattr(adapter, "_prepared", None)
        # 锁顺序始终 solo -> air，避免 resume(跳跃) 与 ready(walk_to) 互等。
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            if active and prepared is not getattr(adapter, "_prepared", None):
                return "blocked_start"
            if self._solo_defer(type(self)._solo_retry_walk, (target,), {"speed": speed}):
                return "waiting_for_solo_exit"
            # 不再使用历史 revision/lock 判断屏障；无活动事务不拦截、不撤销。
            return self._air_ground_dispatch(dispatch)

    def _air_ground_start(self, function, args, kwargs):
        """AirMotion 接管事务：与 ready 地面提交互斥，不属于单人收尾屏障。"""
        with self.__dict__.setdefault("_solo_lock", threading.RLock()), self.__dict__.setdefault("_air_ground_lock", threading.RLock()):
            # 复核快速入口与取锁之间新建的单人事件；完整 Exit 后再起跳。
            if self._solo_defer(function, args, kwargs):
                return True
            result = function(self, *args, **kwargs)
            air = getattr(self, "_air_motion", None)
            if air is not None and self.motion is air:
                # 原 invalidate 只清 scope/generation，不清 prepared/lease。
                # 起跳既已接管，旧地面请求必须走原 cancel 生命周期，落地再申请。
                cancel = getattr(getattr(self, "_terrain_adapter", None), "cancel", None)
                if callable(cancel):
                    cancel("air_start")
            return result

    def _air_ground_dispatch(self, dispatch):
        """防止 ready 与 AirMotion 起跳竞争；没有空中所有者时原分流执行一次。"""
        with self.__dict__.setdefault("_air_ground_lock", threading.RLock()):
            air = getattr(self, "_air_motion", None)
            if air is not None and self.motion is air:
                adapter = getattr(self, "_terrain_adapter", None)
                if getattr(adapter, "_prepared", None) is not None:
                    adapter.cancel("air_motion_owned")
                return "waiting_for_air"
            return dispatch()

    def _solo_retry_walk(self, target, label="", face=None, speed=anim_pose.WALK_SPEED):
        """收尾后重走实例逻辑入口，申请新路线；绝不复用 bounded_pilot 许可。"""
        return self.walk_to(target, "follow" if self.follow else "movement", face=face, speed=speed)

    def _solo_frame_begin(self):
        """Exit 时钟从首个真实 post 帧开始；没有帧时不偷偷跳过收尾。"""
        gate = getattr(self, "_solo_gate", None)
        if gate is None:
            return
        with self._solo_lock:
            if getattr(self, "_solo_gate", None) is not gate:
                return
            if self.social is not None or self._active_running() or self.event is not gate["event"]:
                # 社交/其它所有者抢占后绝不执行旧的单人命令。
                self._solo_gate = None
                return
            if gate["first_frame"] is None:
                gate["first_frame"] = gate["event"].t0 = time.monotonic()
            return gate

    def _solo_frame_end(self, rendered_gate, body, now):
        """本帧已完成普通 idle 写法；异步执行后续动作，不阻塞发包线程。"""
        gate = getattr(self, "_solo_gate", None)
        if gate is None or gate is not rendered_gate:
            return
        with self._solo_lock:
            if getattr(self, "_solo_gate", None) is not gate:
                return
            if (self.event is gate["event"] and gate["first_frame"] is not None
                    and gate["event"].phase(now)[0] == "idle"
                    and not gate["worker"]):
                fields = anim_pose.read(body)
                if any(fields.get(k) != v for k, v in {
                        "key": gate["event"].key, "gen": gate["event"].gen,
                        "mode": 0, "code90": 7}.items()):
                    return
                gate["worker"] = True
                threading.Thread(target=self._solo_resume, args=(gate,), daemon=True,
                                 name="solo-emote-next").start()

    def _solo_resume(self, gate):
        with self._solo_lock:
            if getattr(self, "_solo_gate", None) is not gate:
                return
            if (self.stop.is_set() or self.event is not gate["event"]
                    or self.social is not None or self._active_running()):
                self._solo_gate = None
                return
            # 慢冷任务可能直到 Exit 结束还没调用 bounded_pilot。先撤销，
            # 防止旧 follow 准备结果抢在新探索/座位等逻辑请求之前起步。
            self._solo_cancel_ground()
            # 完整 Exit 末帧已实际构造；保留普通 AnimEvent idle 的写入集合，
            # 但当前 key 退到 0，避免 T23-4 落地恢复 event 时重写已完成的 Exit。
            idle = anim_pose.AnimEvent(0, gate["event"].stamp(), time.monotonic() - 1.0, seconds=0.0)
            idle.start = gate["event"].start
            self.event = idle
            self._solo_gate = None
            action = gate["pending"]
            if self.state.get("phase") == "solo_exit":
                self.state = {"pose": "idle"}
            if action is not None:
                function, args, kwargs, _priority = action
                # 外层传入的显式 now 属于排队前，空中/坐下计时从执行时重建。
                if function.__name__ in ("_air_start", "_sit_direct") and len(args) > 1:
                    args = (args[0], time.monotonic(), *args[2:])
                if "now" in kwargs:
                    kwargs["now"] = time.monotonic()
                try:
                    if function.__name__ == "_air_start":
                        # 重新进入实例入口，完整 Exit 后才进行空中接管事务。
                        self._air_start(*args, **kwargs)
                    elif function.__name__ == "_apply":
                        # 外层 HopCommandBinding 必须看到实际执行后的新 follow，
                        # 不能绕过它只恢复 room_pose 正文而留下旧 follow_ref。
                        # 房间内部代次属于排队前的包装调用；重放结构化命令时
                        # 由遇境入口重新生成，不泄漏给只收cmd的外层许可包装。
                        kwargs = {k: v for k, v in kwargs.items() if k != "_home_command"}
                        self._apply(*args, **kwargs)
                    else:
                        function(self, *args, **kwargs)
                except Exception as exc:
                    self.log("solo_emote_next_failed", action=function.__name__, error=str(exc)[:200])

    def _solo_start(self, key, label):
        """返回 True 表示目录里的单人持续动作已处理；完全没有超时收尾。"""
        exit_key = SOLO_EMOTE_EXITS.get(key)
        if exit_key is None or self.social is not None or self._active_running():
            return False
        lock = self.__dict__.setdefault("_solo_lock", threading.RLock())
        with lock:
            if self.motion is not None or self.sitting is not None:
                self.log("anim_skipped", anim=label, reason="solo_emote_moving_or_sitting")
                return True
            # play() 启动线程后若已经收到新的动作，不准把 Exit 覆盖回 Enter。
            if getattr(self, "_solo_gate", None) is not None:
                return True
            previous = self.event.stamp() if self.event is not None else anim_pose.read(self.seed)
            self.event = anim_pose.ContinuousEmoteEvent(key, exit_key, previous, time.monotonic())
            self._set_override(self.base)
            self.state = {"pose": "anim", "anim": label, "continuous": True}
            self.log("anim_start", anim=label, generation=self.event.gen, continuous=True)
            return True

    def _post(self, body):
        """组合后处理：动画事件 + 当前位置（走动中按时间插值）。"""
        if getattr(self, "_home_sit_pending", None) is not None:
            self._home_sit_tick()
        body = self._prop_seat_frame_begin(body)
        solo_gate = self._solo_frame_begin()
        if self.motion is not None:
            self._air_last_moving = time.monotonic()   # T23-5：卡住判断用（每帧记一次最后在动的时间）
        # Consume a received supported acceptance before rendering this Actor rather
        # than waiting for the one-second controller poll. The existing state
        # machine retains every publication/identity/role/timeout check.
        with self._active_lock:
            active = self._active_offer
            if (active is not None and active["phase"] == "waiting_accept"
                    and isinstance(active.get("social_invite_event"), _SocialOfferEvent)):
                self._active_tick()
        # Shared cancellation obligation: lock, generation, pending stop, owner.
        # Lazy initialization also supports the isolated __new__ test harness.
        stop = self.__dict__.get("_terrain_stop")
        if stop is None:
            from threading import RLock
            stop = self.__dict__.setdefault("_terrain_stop", [RLock(), 0, False, None])
        with stop[0]:
            if stop[3] is not None and self.motion is not stop[3]:
                stop[1] += 1
                stop[2], stop[3] = True, None
            terrain_before_sync = self.motion
            terrain_owned_before_sync = (terrain_before_sync is not None
                                         and terrain_before_sync is getattr(self, "_terrain_motion", None))
            terrain_entry_generation = stop[1]
        self._terrain_sync()
        terrain_stopped = terrain_owned_before_sync and self.motion is None
        now = time.monotonic()
        from air_follow_runtime import observe_frame as _observe_air_follow
        _observe_air_follow(self, now)
        self._once_before_frame(now)
        air_early = getattr(self, "_air_motion", None)
        if (air_early is not None and self.motion is air_early
                and (self.social is not None or self.sitting is not None
                     or self._active_running())):
            # Social/sit took over between follow ticks: brake and fall to the
            # real ground instead of freezing mid-air.
            self._air_cancel("social", now)
        air_owns = air_early is not None and self.motion is air_early
        # T23-4：AirMotion 在渲染时自己写动画 key/gen/start。旧的一次性事件
        # （例如别人举烛后要播的鞠躬）如果在空中还逐帧 post，会在起飞/走位段
        # 把空中的动作字段盖回鞠躬，哥哥看到的就是“举着蜡烛鞠躬卡在空中”。
        # 空中只让 air clip 拥有这些字段；落地后再按地面状态继续。
        if self.event is not None and not air_owns:
            body = self.event.post(body, now)
        motion = self.motion
        terrain_sample = None
        if motion is not None and motion is getattr(self, "_terrain_motion", None):
            operation = getattr(self, "_terrain_request_seq", 0)
            scope = getattr(self, "_terrain_scope", None)
            sample = motion.sample(now)
            self._terrain_sync()
            if (self.motion is not motion or operation != getattr(self, "_terrain_request_seq", 0)
                    or self._terrain_scope is not scope):
                terrain_stopped = True
                motion = None
            elif sample.blocked:
                self._terrain_failed(sample.reason or "terrain_blocked", sample.position, expected=motion)
                terrain_stopped = True
                motion = None
            else:
                self.position = sample.position
                terrain_sample = sample
        posed = False
        air = getattr(self, "_air_motion", None)
        if air is not None and motion is air:
            # T23-1: air plan owns position and rendering.  _air_render clears
            # self.motion on the landing frame and re-seeds the ground layer.
            body, pos, heading = self._air_render(air, body, now)
            posed = True
        elif motion is not None:
            pos = terrain_sample.position if terrain_sample is not None else motion.pos(now)
            # 2026-11-06：走路中的 y 也用当前场景真实地形；查不到才保留插值高度。
            # terrain_sample 已经是地形后端算出来的真实地面，不再二次贴地/加偏移。
            g = None if terrain_sample is not None else self.terrain_ground(pos[0], pos[2], pos[1])
            if g is not None and not (terrain_sample.arrived if terrain_sample is not None else motion.arrived(now)):
                pos = (pos[0], g, pos[2])
            heading = (terrain_sample.heading if terrain_sample is not None and terrain_sample.heading is not None
                       else motion.heading if motion.heading is not None else self.heading)
            if not (terrain_sample.arrived if terrain_sample is not None else motion.arrived(now)):
                if self.walk_clip is not None and heading is not None:   # 走路中：回放真人走路姿态，转到当前方向
                    clip = self.walk_clip
                    body = anim_pose.write_pose_fields(
                        body, anim_pose.rotate_pose(clip.at(now - motion.t0), heading - clip.heading))
                    posed = True
                body = anim_pose.write_velocity(body, terrain_sample.velocity if terrain_sample is not None else motion.velocity)
                body = social_pose.force(body, social_pose.WALK_FORCE)   # 告诉别人客户端“在走路”，由它播走路动画
            else:
                if getattr(self, "_direct_walk_motion", None) is motion:
                    self._direct_walk_motion = None
                self.position, self.motion = terrain_sample.position if terrain_sample is not None else motion.b, None
                if motion.final_heading is not None:
                    self.heading = heading = motion.final_heading
                self.log("walk_arrived", at=[round(v, 3) for v in self.position],
                         heading=None if heading is None else round(heading))
                route = getattr(self, "route", None)
                if route:                               # 沿可走路线的下一段（这一帧照常写位置，不提前返回）
                    self.walk_to(route.pop(0), "route")
                then, self.after_arrive = self.after_arrive, None
                if route is not None and (route or self.motion is not None):
                    then = None                         # 路线没走完：不算到达、不回闲着
                if then is None and self.state.get("pose") == "walk" and self.motion is None:
                    self.state = {"pose": "idle"}          # 走到了、没有后续：回到闲着（自己逛才会接着动）
                    if getattr(self, "_explore_active", False) and not getattr(self, "route", None):
                        self._explore_active = False       # 逛完一段：下一段由 wander 再下命令
                if then is not None:                    # 走到了再做的事（如举蜡烛），放到别的线程，别卡发包
                    threading.Thread(target=self._after, args=(then,), daemon=True).start()
        else:
            pos, heading = self.position, self.heading
        soc = self.social
        if soc is not None and soc["kind"] == "hold":
            pos, heading, body, posed = self._hold_attach(soc, now, pos, heading, body, posed)
        if soc is not None and soc["kind"] == "carry":
            o = self.tracker.origins.get(soc["slot"])
            if o is not None:
                pos = self.position = tuple(o[:3])
        if pos != self.home:
            body = anim_pose.write_origin(body, pos)
        if heading is not None:
            # 站着：把底板（种子朝 +z）的身体姿态整体转到当前朝向。
            # 举烛时：上半身取自举烛模板（模板录制时朝 template_heading），按“当前朝向 − 模板朝向”转；其余照常
            if not posed:
                values = anim_pose.pose_values(body)
                if self.holding:
                    upper = {k: v for k, v in values.items() if k.split("|")[0] in candle_pose.UPPER_BODY}
                    rest = {k: v for k, v in values.items() if k not in upper}
                    values = {**anim_pose.rotate_pose(rest, heading - self.seed_heading),
                              **anim_pose.rotate_pose(upper, heading - self.candle_heading)}
                else:
                    values = anim_pose.rotate_pose(values, heading - self.seed_heading)
                body = anim_pose.write_pose_fields(body, values)
            body = anim_pose.write_facing(body, heading)
        body = self._prop_seat_frame_end(body, now)
        if soc is not None and soc["kind"] == "carry":
            body = social_pose.force_carry(body)
        self._terrain_last_position = tuple(pos)  # Constructed position, not a send/ACK claim.
        # Commit movement bytes before the unchanged social publication tail.
        # Codec work stays outside the lock. A competing cancel/new owner causes
        # a bounded recheck; no attempted encoding consumes a pending obligation.
        with stop[0]:
            if (terrain_stopped and self.motion is None and not self._terrain_social_owns()
                    and stop[1] == terrain_entry_generation):
                stop[1] += 1
                stop[2], stop[3] = True, None
        moving_body, stopped_body = body, None
        for _attempt in range(3):
            with stop[0]:
                generation, current_motion = stop[1], self.motion
                social_owned = self._terrain_social_owns()
                must_stop = stop[2] and current_motion is None and not social_owned
            if must_stop and stopped_body is None:
                stopped_body = anim_pose.write_velocity(moving_body, (0.0, 0.0, 0.0))
            with stop[0]:
                if (stop[1] == generation and self.motion is current_motion
                        and self._terrain_social_owns() == social_owned):
                    body = stopped_body if must_stop else moving_body
                    break
        else:
            # Two pre-encoded candidates make the final selection memory-only.
            # Persistent contention never loops in the realtime callback, and a
            # newer motion/social owner is never overwritten by the older stop.
            if stopped_body is None:
                stopped_body = anim_pose.write_velocity(moving_body, (0.0, 0.0, 0.0))
            with stop[0]:
                body = (stopped_body if stop[2] and self.motion is None
                        and not self._terrain_social_owns() else moving_body)
        if getattr(self, "_active_offer", None) is not None:
            body = self._active_post(body)
            self._active_dance_trace(body, now)  # actual final outgoing Actor, not the seed
            self._social_note_published(body)
        # Preserve the last successfully constructed terrain facing. Otherwise
        # cancellation/replanning falls back to the pre-walk idle heading and
        # visibly spins the Actor despite retaining its last position.
        # Legacy motion and social-owned publication keep their existing rules.
        if terrain_sample is not None and heading is not None and not self._terrain_social_owns():
            self.heading = heading
        self._solo_frame_end(solo_gate, body, now)
        return body

    def _fresh_peer_y(self):
        """房间里位置新鲜的玩家脚下 y；优先当前牵手/跟随对象，其次哥哥，再其次任意人。

        只读 tracker 已发布的坐标（age<=2s）；换图后旧房间的坐标会被
        ``switch_map`` 的 same_map 判断挡住，不会跨图拿来当参考。
        """
        try:
            with self.tracker.lock:
                slots = dict(self.tracker.slots)
                origins = dict(self.tracker.origins)
                observed = dict(self.tracker.position_observed)
            now = time.monotonic()
            prefer = []

            def slot_of(who):
                return next((s for s, u in slots.items() if u == who), None)

            social = self.social or {}
            if social.get('slot') is not None:
                prefer.append(social.get('slot'))
            follow = self.follow or {}
            if follow.get('target'):
                prefer.append(slot_of(follow['target']))
            try:
                owner = (Path(self.path).parent / 'owner_uuid').read_text().strip()
            except OSError:
                owner = None
            if owner:
                prefer.append(slot_of(owner))
            prefer.extend(s for s in slots if s not in prefer)
            for slot in prefer:
                if slot is None or slot not in origins:
                    continue
                seen = observed.get(slot)
                if seen is None or not 0 <= now - seen[1] <= 2.0:
                    continue
                y = origins[slot][1]
                if math.isfinite(float(y)):
                    return float(y)
        except Exception:
            return None
        return None

    def _reground(self, why="terrain", ground_ref=None, allow_player_fallback=True,
                  allow_social=False):
        """待机时按真实地形把出生点/站位高度校正一层；不改水平位置、不加偏移。

        还没贴地时先信目录/标注的意图高度，其次带路人，再其次自己/出生点；
        已经贴地后先信自己当前层（不会因为带路人站在下层就被带下去），
        再信目录、带路人。真实地形查不到时才允许用参考玩家的 y 兜底。

        2026-11-07 T20：``allow_social=True`` 时 social（牵手/背/抱）也能跑，
        但只改 home/seed，不碰 social 正在控制的 position；这样到达时即使
        地形 worker 还没 ready，social 一结束就能用正确的落点高度。
        """
        social = self.social is not None
        if social and not allow_social:
            return False
        if self.motion is not None or self.sitting is not None or self.holding:
            return False
        if self._active_running():
            return False
        x, y, z = self.home
        ref = self._placement_ref_y if ground_ref is None else ground_ref
        own_y = (self.position[1] if (not social and tuple(self.position) == tuple(self.home))
                 else None)
        # 2026-11-07 T22：wire 是落点 Actor 自己的原点高度（玩家下行坐标），
        # 和 terrain_ground 同基准。遇境的目录标注提供的是 geometry y
        # (0.6518)，比 wire (1.1518) 低 0.5 m；拿它当 near_y 会在多层地面
        # 里选到下面一层。未贴地时先信 wire，再信目录意图高度。
        wire = self._placement_wire_y
        order = (('own', 'wire', 'placement', 'leader', 'spawn') if self._grounded
                 else ('wire', 'placement', 'leader', 'own', 'spawn'))
        candidates = terrain_spawn.reference_candidates(
            leader_y=self._fresh_peer_y(), placement_ref=ref, own_y=own_y, spawn_y=y,
            wire_ref=wire, order=order)
        new_y, used, source = terrain_spawn.choose_height(
            self.terrain_ground, x, z, candidates,
            allow_player_fallback=allow_player_fallback)
        if source == 'terrain':
            self._grounded = True
        if new_y is None or not math.isfinite(new_y) or abs(new_y - y) < 0.01:
            return False
        if source == 'player_fallback' and abs(new_y - y) > 5.0:
            # 参考玩家和目录落点差太远（例如千鸟城在 658 m 外）：不用他的 y
            # 覆盖目录高度，等真实地形 ready 后按地形选层。
            return False
        old_home = self.home
        self.home = (x, new_y, z)
        # 引擎每帧从 seed 重建 Actor；把两份坐标一起改掉，后面不再逐帧写 origin。
        self.seed = anim_pose.write_origin(self.seed, self.home)
        self.base = anim_pose.write_origin(self.base, self.home)
        if not social and tuple(self.position) == tuple(old_home):
            self.position = self.home
        self._terrain_last_position = self.position
        if not (self.holding or self.social):
            self._set_override(self.base)
        self.log("terrain_reground", why=why, map_id=getattr(self, "map_id", None),
                 variant=getattr(self, "variant", None),
                 y_before=round(y, 3), y_after=round(new_y, 3),
                 reference=used, source=source,
                 placement_ref_y=None if ref is None else round(ref, 3),
                 placement_wire_y=None if wire is None else round(wire, 3),
                 seed_source=self._seed_source, seed_origin=self._seed_origin(),
                 override_origin=self._override_origin())
        return True

    def _reground_position(self, why="social_end"):
        """Social 结束后按当前位置的 x/z 复核 y；不改 x/z，也不把 home 拉过去。

        优先用落点 placement 的 wire y（A 修好后就是上层地板）；地形 ready 后
        再按真实地形校正。离落点较远时先信自己当前层，避免把“走到别处”的
        小光按出生点高度硬拽回去。
        """
        if self.motion is not None or self.sitting is not None or self.holding:
            return False
        x, y, z = self.position
        hx, hy, hz = self.home
        near_home = ((x - hx) ** 2 + (z - hz) ** 2) ** 0.5 <= 3.0
        # 落点附近的 home y 就是权威高度：A 修好后它来自 placement wire y，
        # 地形 ready 后它会被 terrain_reground 校正；两种都比 social 的 position 可信。
        ref = hy if near_home else self._placement_ref_y
        # 落点附近（social 带到后通常就在落点 3 m 内）永远先信 home y：
        # 哥哥可能站在下层/半空，先信自己的 y 会把小光留在下层。走远之后才先信
        # 自己当前层，避免把“走到别处”的小光按出生点高度硬拽回去。
        if near_home:
            order = ('placement', 'own', 'spawn')
        else:
            order = ('own', 'placement', 'spawn')
        candidates = terrain_spawn.reference_candidates(
            leader_y=None, placement_ref=ref, own_y=y, spawn_y=hy, order=order)
        new_y, used, source = terrain_spawn.choose_height(
            self.terrain_ground, x, z, candidates, allow_player_fallback=False)
        if new_y is None:
            # 地形 worker 还没 ready：落点附近直接用 home y（placement / 上次地形校正）。
            if near_home and math.isfinite(hy):
                new_y, used, source = float(hy), 'home', 'placement'
            else:
                return False
        if not math.isfinite(new_y) or abs(new_y - y) < 0.01:
            return False
        if source == 'terrain':
            self._grounded = True
        self.position = (x, new_y, z)
        self._terrain_last_position = self.position
        if near_home:
            self.home = (hx, new_y, hz)
            self.seed = anim_pose.write_origin(self.seed, self.home)
            self.base = anim_pose.write_origin(self.base, self.home)
            if not (self.holding or self.social):
                self._set_override(self.base)
        elif tuple(self.position) != tuple(self.home):
            self._set_override(self.base)
        self.log("terrain_reground", why=why, map_id=getattr(self, "map_id", None),
                 y_before=round(y, 3), y_after=round(new_y, 3),
                 reference=used, source=source)
        return True

    def _reground_tick(self):
        """待机/到达 social 时每秒重查一次：地形 ready 后按落点参考复核楼层。"""
        now = time.monotonic()
        if now - self._last_reground < 1.0:
            return False
        self._last_reground = now
        if self.social is not None:
            # 牵手/背/抱带到后也是到达：只校正 home/seed，position 仍由 social 控制。
            return self._reground("social", allow_player_fallback=False, allow_social=True)
        if self.motion is not None or self.sitting is not None or self.holding:
            return False
        if tuple(self.position) != tuple(self.home):
            return False
        # 地形后端还没 ready 时先保持种子高度（等真实地形）；后端已 ready
        # 但该点查不到几何时，才按参考玩家脚下 y 兜底（不加偏移）。
        return self._reground("idle",
                              allow_player_fallback=bool(getattr(self, "_terrain_prepared", False)))

    def terrain_ground(self, x, z, near_y=None):
        """按 (x, z) 查当前场景的真实地面高度（玩家下行坐标，即 Actor 原点坐标）。

        near_y 用来在双层地面里选离它最近的那层（牵手/贴身/走近某人时传那个人的 y）。
        几何没加载（还没走过路/没有地形后端）、点在网格外或查询失败都返回 None，
        调用方再用参考玩家的脚下 y 兜底。2026-11-06 起所有定点站位都用这个，
        不再加 ground_offset（那套“格子高度 + 站立偏移”已弃用）。
        """
        adapter = getattr(self, "_terrain_adapter", None)
        if not callable(getattr(adapter, "terrain_ground", None)):
            return None
        try:
            from skyterrain.ground import query as _terrain_ground
        except Exception:
            return None
        return _terrain_ground(adapter, x, z, near_y)

    def ground_offset(self):
        """2026-11-06 弃用：小光站位高度改用真实地形（terrain_ground），不再加任何偏移。

        旧逻辑按图读 control/ground_offset.json（遇境 0.31、别的图 0.15），已删除。
        保留这个函数只为兼容旧调用点，固定返回 0.0，不要再往里加配置。
        """
        return 0.0

    WALKMAP_RELOAD = 120.0

    def _walkmap(self):
        """这张图的可走图（control/walk_<map>.json，2 分钟重读一次）；没有数据返回 None。"""
        import walkmap
        c = self.__dict__.setdefault("_wm", {"at": -1e9, "map": None, "wm": None})
        now = time.monotonic()
        if c["map"] != getattr(self, "map_id", None) or now - c["at"] > self.WALKMAP_RELOAD:
            c["at"], c["map"] = now, getattr(self, "map_id", None)
            f = Path(self.path).parent / f"walk_{c['map']}.json"
            try:
                c["wm"] = _SmoothWalkMap.from_json(str(f))
            except (OSError, ValueError):
                c["wm"] = None
        return c["wm"]

    @_solo_action
    def _explore(self, toward=None):
        """沿可走地面逛到附近的随机点（或 toward 那人附近）。"""
        import random
        self._explore_active = True
        wm = self._walkmap()
        here = self.motion.pos(time.monotonic()) if self.motion else self.position
        # 2026-11-06：walk_<map>.json 只留着给 walkmap 路由/统计用；落点高度一律再查真实地形。
        start = (here[0], here[1], here[2])
        route = None
        if wm is not None:
            goal = None
            if toward is not None:
                near = wm.nearest_walkable(tuple(float(v) for v in toward))
                if near is not None:
                    goal = wm.random_target(near, 1.5, 4.0) or near
            else:
                goal = wm.random_target(start, 3.0, 12.0)
            entry = None
            n0 = wm._nearest_node(start)
            if n0 is None or len(wm._component(n0)) < EXPLORE_MIN_AREA:
                # 自己所在的可走块太小（出生点之类没人久留的地方数据少）：先直线走到 15 米内最近的大块，再在里面逛
                best = None
                for i, nd in enumerate(wm._nodes):
                    x, y, z = wm._position(i)
                    d = ((x - start[0]) ** 2 + (z - start[2]) ** 2) ** 0.5
                    if d <= EXPLORE_ENTRY_RANGE and abs(y - start[1]) <= WALK_MAX_CLIMB + 1.0                             and len(wm._component(i)) >= EXPLORE_MIN_AREA and (best is None or d < best[0]):
                        best = (d, (x, y, z))
                if best is not None:
                    entry = best[1]
                    if toward is None:
                        goal = wm.random_target(entry, 3.0, 12.0)
            if goal is not None:
                route = wm.path(entry or start, goal, max_nodes=4000)
                if route and entry is not None:
                    route = [start] + route
        if not route or len(route) < 2:
            comp = None
            if wm is not None:
                n = wm._nearest_node(start)
                comp = None if n is None else len(wm._component(n))
            self.log("explore_no_route", toward=bool(toward), have_map=wm is not None,
                     start=[round(v, 2) for v in start], component=comp)
            if toward is not None:                     # 退回原来的直线走（高度查真实地形）
                x, y, z = (float(v) for v in toward)
                a = random.uniform(0, 6.283)
                x, z = x + 2.5 * __import__("math").sin(a), z + 2.5 * __import__("math").cos(a)
                g = self.terrain_ground(x, z, y)
                if g is not None:
                    y = g
                if y - here[1] <= WALK_MAX_CLIMB:
                    self.walk_to((x, y, z), "point")
                    if self.motion is not None:
                        self.state = {"pose": "walk"}
                        return
            self._explore_active = False
            return
        pts = []
        # 2026-11-07 T22：walk_<map>.json 里混着飞行/别的图的采样（遇境这张
        # 文件里甚至有 y=306 / z=-850 的点）。不能直接拿 p[1] 当 near_y，
        # 否则多层地面会选到下面一层，走起来像陷进地里。以“当前层”为参考；
        # 只有 walkmap 高度离当前层不超过 WALK_MAX_CLIMB 时才采用它。
        layer_ref = here[1]
        for p in route[1:]:
            ref = p[1] if abs(p[1] - layer_ref) <= WALK_MAX_CLIMB else layer_ref
            gy = self.terrain_ground(p[0], p[2], ref)
            if gy is not None and abs(gy - layer_ref) > WALK_MAX_CLIMB:
                # 查到的面离当前层太远（多半是下层/天花板）：换回当前层再查。
                gy = self.terrain_ground(p[0], p[2], layer_ref)
            if gy is not None:
                y = gy
            elif abs(p[1] - layer_ref) <= WALK_MAX_CLIMB:
                y = p[1]
            else:
                break                        # walkmap 噪声点：宁可少走一段，也不飞天/入地
            pts.append((p[0], y, p[2]))
            layer_ref = y
        if not pts:
            self.log("explore_no_route", toward=bool(toward), have_map=wm is not None,
                     start=[round(v, 2) for v in start], component=None,
                     reason="all_route_points_untrusted")
            self._explore_active = False
            return
        self.route = pts[1:]
        self.walk_to(pts[0], "route")
        if self.motion is not None:
            self.state = {"pose": "walk"}
        self.log("explore", toward=bool(toward), waypoints=len(pts),
                 goal=[round(v, 2) for v in pts[-1]])

    def _set_attitude(self, value):
        """在线换站姿：改种子和当前底板里装扮块的站姿值，马上开始发。"""
        if not 0 <= value <= 14:
            return
        (CONTROL_DIR / "attitude").write_text(str(value))
        old_seed = self.seed
        self.seed = with_attitude(old_seed)
        if self.base is old_seed or self.base == old_seed:
            self.base = self.seed
        else:
            self.base = with_attitude(self.base)
        self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") if self.holding or self.social else self.base)
        if not (self.holding or self.social):
            self._set_override(self.base)
        sent = map_long_engine.POSE_OVERRIDE.get("seed") or self.raw_seed
        self.log("attitude_set", value=value, sent_attitude=attitude_of(sent), sent_counter=outfit_counter_of(sent),
                 holding=bool(self.holding), social=self.social is not None, walking=self.motion is not None,
                 override=("seed" in map_long_engine.POSE_OVERRIDE))   # 诊断：实际在发的那份 Actor

    def _set_voice(self, value):
        """在线换叫声（T23-7）：仿 _set_attitude 改种子/底板装扮块的 voice token（bits4）。

        发声顺序按真人抓包：先让带新 voice 的 Actor 帧发出去，``VOICE_COUNTER_DELAY``
        秒后再把外层外观计数 +1（不是同一帧）。计数仍走 set_outfit_counter 的单调规则。
        不站起、不打断走路/坐下/举烛；牵手/背背期间 override 是另一份身体，等互动结束
        下次 _set_override(self.base) 才会带上新 voice。
        """
        try:
            value = int(value)
        except (TypeError, ValueError):
            self.log("voice_skipped", value=value, reason="bad_value")
            return False
        if not 0 <= value <= 15:
            self.log("voice_skipped", value=value, reason="range")
            return False
        if value not in _owned_voice_ids():
            self.log("voice_skipped", value=value, reason="not_owned")
            return False
        try:
            (CONTROL_DIR / "voice").write_text(str(value))
        except OSError:
            pass
        old_seed = self.seed
        seed = _patch_voice(old_seed, value)
        changed = voice_of(seed) != voice_of(old_seed)
        self.seed = seed
        if self.base is old_seed or self.base == old_seed:
            self.base = seed
        else:
            self.base = _patch_voice(self.base, value)
        busy = bool(self.holding or self.social)
        self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") if busy else self.base)
        if not busy:
            self._set_override(self.base)
        sent = map_long_engine.POSE_OVERRIDE.get("seed") or self.raw_seed
        self._voice_gen = getattr(self, "_voice_gen", 0) + 1
        gen = self._voice_gen
        if changed:
            timer = threading.Timer(VOICE_COUNTER_DELAY, self._voice_counter_commit, args=(gen,))
            timer.daemon = True
            self._voice_timer = timer
            timer.start()
        self.log("voice_set", value=value, changed=changed, sent_voice=voice_of(sent),
                 sent_counter=outfit_counter_of(sent), counter_delay=VOICE_COUNTER_DELAY,
                 holding=bool(self.holding), social=self.social is not None,
                 walking=self.motion is not None, sitting=getattr(self, "sitting", None) is not None,
                 override=("seed" in map_long_engine.POSE_OVERRIDE))
        return changed

    def _voice_counter_commit(self, gen):
        """voice 帧发出 0.45 s 后补一次外观计数 +1（真人节奏；gen 变了说明期间换图/又换声，作废）。"""
        try:
            if getattr(self, "_voice_gen", None) != gen:
                return
            before = outfit_counter_of(self.seed)
            seed = set_outfit_counter(self.seed)          # 单调规则 + 写 control/outfit_counter
            n = outfit_counter_of(seed)
            self.seed = seed
            if n is not None:
                self.base = _patch_counter(self.base, n)
            busy = bool(self.holding or self.social)
            self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") if busy else self.base)
            if not busy:
                self._set_override(self.base)
            sent = map_long_engine.POSE_OVERRIDE.get("seed") or self.raw_seed
            self.log("voice_counter", value=voice_of(seed), before=before, counter=n,
                     delay=VOICE_COUNTER_DELAY, gen=gen,
                     sent_counter=outfit_counter_of(sent))
        except Exception as exc:                            # 计数器失败不能拖垮姿态线程
            self.log("voice_counter_error", error=str(exc)[:160])

    def _set_outfit(self, outfit, changes=None):
        """在线换装（T23-3）：整套装扮对象写进 control/outfit.json，改种子/底板的部位 id，
        外观指纹变了计数 +1（别人客户端才会重新读）。changes 给了就只改这些部位（脱掉写 0）；
        legs/arms 在 Actor 块里没有，只服务器改。"""
        if not isinstance(outfit, dict) or not outfit:
            return
        only = None
        if isinstance(changes, list) and changes:
            only = {c.get("slot") for c in changes if isinstance(c, dict) and c.get("slot")}
        try:
            CONTROL_DIR.mkdir(parents=True, exist_ok=True)
            (CONTROL_DIR / "outfit.json").write_text(json.dumps(outfit, ensure_ascii=False), encoding="utf-8")
            if only:
                p = CONTROL_DIR / "outfit_slots.json"
                try:
                    prev = json.loads(p.read_text())
                    prev = prev if isinstance(prev, list) else []
                except (OSError, ValueError):
                    prev = []
                p.write_text(json.dumps(sorted(set(prev) | set(only))), encoding="utf-8")
        except OSError:
            pass
        old_seed = self.seed
        self.seed = with_outfit(old_seed, outfit, only)
        if self.base is old_seed or self.base == old_seed:
            self.base = self.seed
        else:
            self.base = with_outfit(self.base, outfit, only)
        self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") if self.holding or self.social else self.base)
        if not (self.holding or self.social):
            self._set_override(self.base)
        sent = map_long_engine.POSE_OVERRIDE.get("seed") or self.raw_seed
        ids = outfit_ids_of(sent) or {}
        self.log("outfit_set", sent_ids={k: v for k, v in ids.items()}, sent_counter=outfit_counter_of(sent),
                 fingerprint=outfit_fingerprint(sent), holding=bool(self.holding), social=self.social is not None,
                 walking=self.motion is not None, override=("seed" in map_long_engine.POSE_OVERRIDE))

    def linked_with(self, who, within=20.0):
        """正和 who 牵着/背着，或刚结束不到 within 秒（过图时对方先从房间里消失、互动先断）。"""
        ls = getattr(self, "last_social", None)
        if not ls or ls["who"] != who or ls["kind"] not in ("hold", "carry"):
            return False
        return ls["ended"] is None or time.monotonic() - ls["ended"] < within

    def set_landing(self, map_id, variant, origin, source):
        """登记“当前应该站在哪个落点”。换图/换落点/开局都写这里。

        ``origin`` 是落点的 Actor 原点（wire）坐标；``source`` 只用于诊断
        （install / switch_map / same_peer_level_update / move_switch ...）。
        """
        point = None
        try:
            point = tuple(float(v) for v in origin[:3])
            if len(point) != 3 or not all(math.isfinite(v) for v in point):
                point = None
        except (TypeError, ValueError, IndexError):
            point = None
        self._landing = {"map_id": map_id, "variant": variant,
                         "origin": point, "source": source}
        self._seed_source = source
        self._landing_applied = None
        return self._landing

    def _named_landing(self):
        """当前是否还处在一个已登记的命名落点（有 variant 的那种）。

        如果房间已经确认换到另一张图、而 ctl 还停在旧图（没有经过
        switch_map 的异常路径），这里返回 False，避免把旧图变体写回去。
        """
        landing = self._landing
        tracker = getattr(self, "tracker", None)
        if (tracker is not None and getattr(tracker, "ready", False)
                and getattr(tracker, "confirmed_map_id", None) is not None
                and tracker.confirmed_map_id != self.map_id):
            return False
        return bool(isinstance(landing, dict) and self.variant is not None
                    and landing.get("map_id") == self.map_id
                    and landing.get("variant") == self.variant)

    def _seed_origin(self, body=None):
        """诊断：种子里的 Actor 原点；异常只返回 None，绝不干扰姿态。"""
        try:
            return [round(v, 3) for v in anim_pose.read_origin(self.seed if body is None else body)]
        except Exception:
            return None

    def _override_origin(self):
        body = map_long_engine.POSE_OVERRIDE.get("seed")
        return None if body is None else self._seed_origin(body)

    def _landing_diag(self):
        landing = self._landing if isinstance(self._landing, dict) else {}
        origin = landing.get("origin")
        return {"landing_source": landing.get("source"),
                "landing_map_id": landing.get("map_id"),
                "landing_variant": landing.get("variant"),
                "landing_origin": None if origin is None else [round(v, 3) for v in origin]}

    def _reapply_named_landing(self, why):
        """房间复位/重新校验后，把当前命名落点重新写回服务器。

        只写 ``POSE_OVERRIDE`` 还不够：13:57:23 的 ``validated_room`` 会把
        服务器侧的小光重新登记到房间主落点（slot 169→235）。这里再补一次
        原生 ``AvatarTeleportRpc``；epoch/自己 slot 不成立时只记日志，不发。
        """
        if not self._named_landing():
            return False
        origin = tuple(self.home)
        epoch = LAST_EPOCH
        own = None
        if epoch is not None:
            try:
                with self.tracker.lock:
                    own = next((s for s, u in self.tracker.slots.items()
                                if u == self.tracker.own_uuid), None)
            except Exception:
                own = None
        key = (epoch, own, tuple(round(v, 3) for v in origin))
        sent = False
        if epoch is not None and own is not None and key != self._landing_applied:
            try:
                heading = self.heading if self.heading is not None else self.seed_heading
                h = math.radians(heading or 0.0)
                social_accept.send_teleport(epoch, own, origin,
                                            (math.sin(h), 0.0, math.cos(h)))
                sent = True
                self._landing_applied = key
            except Exception as exc:
                self.log("pose_landing_reapply_failed", why=why, error=str(exc)[:120])
        self.log("pose_landing_reapplied", why=why, map_id=self.map_id,
                 variant=self.variant, at=[round(v, 3) for v in origin],
                 override_seed=("seed" in map_long_engine.POSE_OVERRIDE),
                 override_origin=self._override_origin(),
                 epoch=epoch, own_slot=own, teleport_sent=sent,
                 **self._landing_diag())
        return sent

    def snapshot_relocation(self):
        """同图传送前的 ctl 状态快照；传送被游戏/对端拒绝时用于回滚。"""
        return {
            "map_id": self.map_id,
            "variant": self.variant,
            "raw_seed": self.raw_seed,
            "seed": self.seed,
            "base": self.base,
            "home": tuple(self.home),
            "position": tuple(self.position),
            "heading": self.heading,
            "placement_ref_y": self._placement_ref_y,
            "placement_wire_y": self._placement_wire_y,
            "grounded": bool(self._grounded),
            "variant_position": (None if getattr(self, "_variant_position", None) is None
                                 else tuple(self._variant_position)),
            "state": dict(self.state or {}),
            "seed_source": self._seed_source,
            "landing": None if not isinstance(self._landing, dict) else dict(self._landing),
        }

    def restore_relocation(self, snapshot):
        """恢复传送前的本地种子/落点（不改服务器，也不发传送）。

        2026-11-07 T21：同图传送到千鸟城时，游戏/对端可能只在屏幕上闪一下
        就把人拉回神殿；本地 ctl 却还停在 85.49。这里让本地状态和哥哥看到的
        位置重新一致，下一次请求才会真的重新执行，而不是对着 660 m 外的
        坐标报 target_too_far。

        2026-11-07 T22：快照只能恢复“同一张图”的传送前状态。如果期间已经
        真的换过图（rejoin / move_switch / 服务器复位），把旧图的 map_id/
        种子/坐标写回来会让小光出现在另一张图的坐标上（遇境“很深的地底”
        的可疑路径之一）。map_id 不同就丢弃并记日志。
        """
        if not isinstance(snapshot, dict):
            return False
        current_map = getattr(self, "map_id", None)
        snapshot_map = snapshot.get("map_id")
        if snapshot_map is not None and current_map is not None and snapshot_map != current_map:
            self.log("relocate_revert_dropped", reason="map_changed",
                     snapshot_map_id=snapshot_map, map_id=current_map,
                     variant=getattr(self, "variant", None),
                     at=[round(v, 3) for v in self.position])
            return False
        try:
            self.map_id = snapshot["map_id"]
            self.variant = snapshot["variant"]
            self.raw_seed = snapshot["raw_seed"]
            self.seed = snapshot["seed"]
            self.base = snapshot["base"]
            self.home = tuple(snapshot["home"])
            self.position = tuple(snapshot["position"])
            self.heading = snapshot.get("heading")
            self._placement_ref_y = snapshot.get("placement_ref_y")
            self._placement_wire_y = snapshot.get("placement_wire_y")
            self._grounded = bool(snapshot.get("grounded"))
            vp = snapshot.get("variant_position")
            self._variant_position = None if vp is None else tuple(vp)
            self.state = dict(snapshot.get("state") or {"pose": "idle"})
        except (KeyError, TypeError, ValueError):
            return False
        self.motion = self.follow = self.after_arrive = None
        self.route = []
        self.arrival = None
        self._last_reground = 0.0
        self._terrain_last_position = self.position
        self._seed_source = "restore_relocation"
        landing = snapshot.get("landing")
        if isinstance(landing, dict):
            self._landing = {"map_id": landing.get("map_id", self.map_id),
                             "variant": landing.get("variant", self.variant),
                             "origin": (None if landing.get("origin") is None
                                        else tuple(landing["origin"])),
                             "source": "restore_relocation"}
        else:
            self.set_landing(self.map_id, self.variant, self.position, "restore_relocation")
        self._set_override(self.base, force=True)
        self.log("relocate_reverted", map_id=self.map_id, variant=self.variant,
                 at=[round(v, 3) for v in self.position],
                 seed_source=self._seed_source, seed_origin=self._seed_origin(),
                 override_origin=self._override_origin(), **self._landing_diag())
        return True

    @_solo_action
    @upright_action
    def switch_map(self, seed, map_id, *, ground_ref=None, variant=None, source=None):
        """跟着换图/同图换命名落点：换成目标种子，状态清空。

        ``ground_ref`` 是目录/标注的意图高度（只当真实地形选层参考）；
        ``variant`` 是命名落点（None=原点）；``source`` 只用于诊断（是谁
        换的图/落点：install / same_peer_level_update / move_switch / rejoin）。
        同图换落点时立刻按真实地形校正高度，跨图则等新房间的坐标到了再由
        _reground_tick 校正。
        """
        if self._is_home_map():
            self._clear_home_seats()
        same_map = map_id == getattr(self, "map_id", None)
        self._voice_gen = getattr(self, "_voice_gen", 0) + 1   # T23-7：换图套用 control/voice 时作废旧计时器
        self._seed_source = source or ("switch_map_variant" if variant is not None else "switch_map")
        self._grounded = False
        self.raw_seed = seed
        self.seed = with_appearance(social_pose.stand(seed))
        self.seed_heading = anim_pose.yaw_heading(anim_pose.read(self.seed)["yaw1"])
        self.home = anim_pose.read_origin(self.seed)
        self._placement_wire_y = self.home[1]   # 目标种子自己的 Actor 原点高度（wire y）
        self.seats = load_seats(map_id)
        self.map_id = map_id
        self.variant = variant
        self._placement_ref_y = ground_ref
        self.seat_file = None if map_id == HOME_MAP_ID else (xg_paths.BOT_DATA / f"seats_{map_id}.json")
        self.sitting = self.stand_spot = None
        arrival_snap.arm(self, map_id)           # 换图后最多等 60 秒把人贴身放过去；reset 会清 social/follow，先记
        # 先登记新落点再 reset：命名落点的 reset 直接强制写回目标种子，
        # 不会出现“先 pop 再补写”的空窗。
        self.set_landing(map_id, variant, self.home, source or self._seed_source)
        self.reset("switch_map")
        if same_map:
            self._reground("switch_map", ground_ref=ground_ref,
                           allow_player_fallback=False)
        # 目标命名落点的实际落点；协调器用它判断“variant 切了但人没到”。
        self._variant_position = tuple(self.home)
        # 2026-11-07 T21：同图换落点不换引擎，引擎内部 _actor_seed 还是旧房间
        # 的种子。reset 里“seed==raw_seed 就 pop override”的优化会让引擎继续
        # 逐帧发旧位置——AvatarTeleportRpc 闪一下，随后又被旧 Actor 帧拉回去。
        # 这里必须强制把目标种子写进 override，哪怕它等于 raw_seed。跨图会
        # 新建 engine，不需要也不应该在旧 engine 上强写。
        if same_map:
            self._set_override(self.seed, force=True)
        self.set_landing(map_id, variant, self._variant_position,
                         source=source or self._seed_source)
        self.log("pose_switch_map", map_id=map_id, seats=len(self.seats),
                 variant=variant, same_map=same_map,
                 seed_source=self._seed_source, seed_origin=self._seed_origin(),
                 ground_ref=ground_ref, override_seed=("seed" in map_long_engine.POSE_OVERRIDE),
                 override_origin=self._override_origin(), **self._landing_diag())

    def _peer_attachment_probe(self):
        """诊断：房间里最近 2 秒的互动帧（优先 leader/carrier 指向自己的）。"""
        try:
            tr = self.tracker
            now = time.monotonic()
            with tr.lock:
                own = tr._own_social_binding_locked()
                evs = [e for e in tr._attachment_events
                       if 0 <= now - e.get('observed_mono', -1e9) <= 2.0 and e.get('value')]
                if not evs:
                    return {}
                def rank(e):
                    v = e.get('value') or {}
                    return (v.get('leader_slot') == own or v.get('carrier_slot') == own,
                            e.get('sample_seq', 0))
                value = max(evs, key=rank).get('value') or {}
                return {key: value.get(key) for key in
                        ('hold_state', 'leader_slot', 'carrier_slot') if key in value}
        except Exception:
            return {}

    @_solo_action
    @upright_action
    def relocate(self, seed, map_id, *, variant=None, ground_ref=None):
        """同图换命名落点：换种子 + 真实地形贴地 + 原生 AvatarTeleportRpc。

        不走网络换图/重连，房间和牵手关系保持；小光领头时由对方客户端跟随。
        同图重进同一 level 的 same-peer 迁移被 ``source._map == target.map_id``
        明确拒绝，因此这里复用“本地换种子 + 原生传送”这条已存在的机制。
        """
        same_map = map_id == getattr(self, "map_id", None)
        self._seed_source = "relocate"
        rpc_y = None
        if same_map:
            # 同一个场景的地形后端还挂着：先按目标 xz 查好真实层，再把种子里的
            # 两份坐标改掉。switch_map 会 invalidate 地形，必须在此之前查。
            x, y, z = anim_pose.read_origin(seed)
            # 2026-11-07 T20：目标落点自己的高度是种子 wire y；旧位置的 own/leader
            # 高度绝不能当兜底（日志 12:42:24 曾把 136.087 带到千鸟城）。
            candidates = terrain_spawn.reference_candidates(
                placement_ref=ground_ref, spawn_y=y, order=('placement', 'spawn'))
            new_y, _used, _source = terrain_spawn.choose_height(
                self.terrain_ground, x, z, candidates, allow_player_fallback=False)
            if new_y is None and math.isfinite(y):
                new_y = y                       # 地形没 ready：用目标 placement 的 wire y
            if new_y is not None and math.isfinite(new_y):
                seed = anim_pose.write_origin(seed, (x, new_y, z))
                rpc_y = float(new_y)
        self.switch_map(seed, map_id, ground_ref=ground_ref, variant=variant, source="relocate")
        if not same_map:
            return False
        # 地形后端已被 switch_map 失效；种子高度已在上面按真实地形写好，
        # 后续 _reground_tick 等后端重新 ready 后再复核，避免用玩家 y 覆盖。
        if rpc_y is None:
            rpc_y = float(self.position[1])
        if LAST_EPOCH is not None:
            try:
                with self.tracker.lock:
                    own = next((s for s, u in self.tracker.slots.items()
                                if u == self.tracker.own_uuid), None)
                if own is not None:
                    heading = self.heading if self.heading is not None else self.seed_heading
                    h = math.radians(heading or 0.0)
                    social_accept.send_teleport(LAST_EPOCH, own,
                                                (self.position[0], rpc_y, self.position[2]),
                                                (math.sin(h), 0.0, math.cos(h)))
            except Exception as exc:
                self.log("relocate_teleport_failed", error=str(exc)[:120])
        self._variant_position = tuple(self.position)
        self.set_landing(map_id, variant, self._variant_position, "relocate")
        probe = self._peer_attachment_probe()
        override = map_long_engine.POSE_OVERRIDE.get("seed")
        override_at = None
        if override is not None:
            try:
                override_at = [round(v, 3) for v in anim_pose.read_origin(override)]
            except Exception:
                override_at = None
        self.log("relocate", map_id=map_id, variant=variant, same_map=same_map,
                 at=[round(v, 3) for v in self.position],
                 rpc_y=round(rpc_y, 3), override_seed=override is not None,
                 override_at=override_at, seed_source=self._seed_source,
                 seed_origin=self._seed_origin(), **probe, **self._landing_diag())
        return True

    def _arrival_snap_tick(self):
        """换图后“到达贴身”：等到带路人在新房间的新鲜位置，就把落单的小光挪到他身边（每次换图一次）。"""
        return arrival_snap.tick(self)

    def _hold_attach(self, soc, now, pos, heading, body, posed):
        """牵手：站到带路方右侧、同朝向；按带路方的位移估速度，在动就播走路（同 walk 的写法）。"""
        o = self.tracker.origins.get(soc["slot"])
        if o is None:
            return pos, heading, body, posed
        yaw = self.tracker.yaws.get(soc["slot"])
        h = anim_pose.yaw_heading(yaw) if yaw is not None else (heading or 0.0)
        import math
        r = math.radians(h)
        x, z = o[0] + math.cos(r) * HOLD_DISTANCE, o[2] - math.sin(r) * HOLD_DISTANCE
        # 他脚下就是真实地面：优先查当前场景地形；查不到再用他的 y。都不加站立偏移。
        g = self.terrain_ground(x, z, o[1])
        if g is None and not self._grounded and self._placement_wire_y is not None:
            # 2026-11-07 T20：地形 worker 还没 ready 时，牵手/背/抱到达落点附近
            # 先信落点自己的 placement wire y；哥哥可能站在下层，不能把他 y 当小光的高度。
            hx, _hy, hz = self.home
            if (x - hx) ** 2 + (z - hz) ** 2 <= 9.0:
                g = self._placement_wire_y
        pos = (x, g if g is not None else o[1], z)
        # 速度按带路方坐标更新之间算（对方坐标约 10 次/秒才来一次，两次之间保持上次的速度）
        prev = getattr(self, "hold_prev", None)
        v = getattr(self, "hold_v", None) or (0.0, 0.0, 0.0)
        if prev is None or o[3] != prev[3]:
            if prev is not None and o[3] - prev[3] > 1e-3:
                v = tuple((a - b) / (o[3] - prev[3]) for a, b in zip(o[:3], prev[:3]))
            self.hold_prev = o
        if time.time() - o[3] > 0.5:                               # 对方坐标半秒没更新 = 停着（tracker 用 time.time）
            v = (0.0, 0.0, 0.0)
        self.hold_v = v
        self.motion, self.follow, self.position, self.heading = None, None, pos, h
        if (v[0] ** 2 + v[2] ** 2) ** 0.5 > 0.4:
            if self.walk_clip is not None:
                clip = self.walk_clip
                body = anim_pose.write_pose_fields(body, anim_pose.rotate_pose(clip.at(now), h - clip.heading))
                posed = True
            body = anim_pose.write_velocity(body, v)
            body = social_pose.force(body, social_pose.WALK_FORCE)
        return pos, h, body, posed

    def _safe_post(self, body):
        """引擎每帧调用。姿态出错绝不能拖垮连接（墓土曾因坏帧掉线、重连后又立刻再掉）：
        记日志、清掉姿态状态，这一帧原样发出。"""
        try:
            return self._post(body)
        except Exception as exc:
            self.log("pose_post_error", error=str(exc)[:200])
            self.reset("post_error")
            return body

    @_solo_action
    @upright_action
    def reset(self, why="new_room", *, keep_pose=False, keep_seat=None):
        """回到干净状态。``keep_pose`` = 同一张图/同一房间重新校验：不传回出生点。

        2026-11-07 线上 18:44:00：长连接没断，只是 engine 重新校验了房间，
        ``on_room`` 无条件 ``reset("validated_room")`` 把小光从座位
        (108.37, 196.18, -437.21) 传回云野出生点 (119.03, 201.29, -453.60)，
        坐下状态同时被清掉。之后地面层从出生点走回桌子要下降 5 m，寻路一直
        ``goal_support_missing``，sit 永远到不了座位。同图重校验必须保留位置/
        朝向：坐着且座位还在就继续坐着，否则站起在座位旁，状态和实际一致。
        """
        if self._is_home_map():
            self._clear_home_seats()
        self._stuck_reset_intent()
        saved = None
        self._voice_gen = getattr(self, "_voice_gen", 0) + 1   # T23-7：换图/复位后作废还没补的叫声计数
        if keep_pose:
            try:
                saved = {"position": tuple(self.position),
                         "heading": self.heading if self.heading is None else float(self.heading),
                         "sitting": self.sitting,
                         "stand_spot": None if self.stand_spot is None else tuple(self.stand_spot),
                         "event": self.event,
                         "state": dict(self.state or {}),
                         # 2026-11-07 Claude：墓土 19:17:36 搬房间时小光正在飞 → 飞行被清掉、跟随被清掉。
                         # 同图搬房间：正在进行的 AirMotion 照常飞完落地（不悬停、不回出生点），跟随保持（同一个对象，
                         # hop 绑定的 follow_ref 仍有效，地面跟随许可不失效）。
                         "air": (self._air_motion if getattr(self, "_air_motion", None) is not None
                                 and self.motion is self._air_motion else None),
                         "follow": self.follow}
            except (TypeError, ValueError):
                saved = None
        pending = getattr(self, "_sit_pending", None)
        if pending is not None:
            # 复位打断了一次还没坐下的 sit：状态里必须留下失败原因。
            self._last_sit_failure = {"reason": "room_reset",
                                      "seat": pending.get("seat"),
                                      "at": time.time()}
            self.log("sit_failed", reason="room_reset", seat=pending.get("seat"),
                     elapsed=round(time.monotonic() - pending.get("since", time.monotonic()), 1))
        self._sit_pending = None
        self._terrain_invalidate(clear_prepared=True)
        if getattr(self, "_air_motion", None) is not None and not (saved and saved.get("air") is not None):
            # Room reset / map change / disconnect: no next frame to descend in;
            # drop the plan and let the new room seed decide.  Logged for audit.
            self.log("air_cancel", reason="reset_" + str(why)[:32],
                     at=[round(v, 2) for v in self.position])
        self._air_motion = None
        self._air_phase = None
        self._air_failed_at = self._air_failed_target = None
        if getattr(self, "_active_offer", None) is not None:
            self._active_end("room_reset")
        self.event = self.motion = self.after_arrive = self.follow = self.candle = self.social = None
        self.sitting, self.holding, self.heading = None, False, None
        self.room_since = time.time()                  # 新房间：之前房间看到的座位不算
        self.route = []
        self.position, self.base = self.home, self.seed
        self._terrain_last_position = self.home
        o = map_long_engine.POSE_OVERRIDE
        o.pop("post", None)
        named = self._named_landing()
        if named:
            # 2026-11-07 T22：validated_room / new_room / rejoin 会换一个
            # engine（它自己的 _actor_seed 是房间主落点，例如神殿）。当前
            # 还在千鸟城这类命名落点时，绝不能把 override 清掉退回主落点；
            # 必须强制写回当前落点种子。
            o["seed"] = self.seed
        elif self.seed == self.raw_seed:
            o.pop("seed", None)
        else:
            o["seed"] = self.seed              # 站立版种子
        self.state = {"pose": "idle"}
        resumed_sit = False
        stood_up = False
        if saved is not None:
            position = saved["position"]
            if (isinstance(position, tuple) and len(position) == 3
                    and all(math.isfinite(float(v)) for v in position)):
                self.position = position
                self._terrain_last_position = position
            if saved["heading"] is not None:
                self.heading = saved["heading"]
            seat = saved["sitting"]
            seat_known = (not self._is_home_map() and seat is not None
                          and (bool(keep_seat) or seat in self.seats
                               or isinstance(saved["event"], anim_pose.SitEvent)))
            if seat is not None and seat_known:
                # 座位还在：保持坐着。座位事件按新 engine 的时钟重新钉起点，
                # 位置仍用复位前真实的座位坐标（不查地形，椅面不是地面）。
                try:
                    prev = (saved["event"].stamp() if isinstance(saved["event"], anim_pose.SitEvent)
                            else {k: anim_pose.read(self.seed)[k] for k in ("gen", "key", "start")})
                except Exception:
                    prev = {k: anim_pose.read(self.seed)[k] for k in ("gen", "key", "start")}
                self.sitting = int(seat)
                self.stand_spot = saved["stand_spot"]
                self.event = anim_pose.SitEvent(int(seat), prev, time.monotonic())
                self.state = {"pose": "sit", "seat": int(seat)}
                self._last_sit_failure = None
                self._set_override(self.base)
                resumed_sit = True
            else:
                if seat is not None:
                    # 座位记录不在了：站起在座位旁（优先回到坐下前站的位置，
                    # 否则留在原座位坐标），不能继续声称坐着。
                    stood_up = True
                    spot = saved["stand_spot"]
                    if (isinstance(spot, tuple) and len(spot) == 3
                            and all(math.isfinite(float(v)) for v in spot)
                            and _dist(spot, saved["position"]) <= 3.0):
                        self.position = spot
                        self._terrain_last_position = spot
                    self.log("sit_resume_stand", reason="seat_missing", seat=seat,
                             at=[round(v, 3) for v in self.position])
                self._set_override(self.base)
            if not resumed_sit and saved.get("follow") is not None:
                self.follow = saved["follow"]
                self.state = {"pose": "follow", "target": self.follow.get("target")}
            if not resumed_sit and saved.get("air") is not None:
                air = saved["air"]
                self.motion = self._air_motion = air
                self.position = air.pos(time.monotonic())
                self._terrain_last_position = self.position
                self._set_override(self.base)
                self.log("air_kept_on_room_reset", why=str(why)[:32],
                         at=[round(v, 2) for v in self.position])
            if not resumed_sit and getattr(self, "_terrain_adapter", None) is not None:
                # 已经站起来了：地面层不能还停在 waiting_for_social_release。
                try:
                    self._terrain_adapter.set_social_owned(False)
                except Exception:
                    pass
                self._terrain_owned = False
        self.log("pose_reset", why=why, map_id=self.map_id, variant=self.variant,
                 seed_source=self._seed_source, seed_origin=self._seed_origin(),
                 home=[round(v, 3) for v in self.home],
                 position=[round(v, 3) for v in self.position],
                 override_seed=("seed" in o), override_origin=self._override_origin(),
                 named_landing=named, preserve=bool(saved is not None),
                 resumed_sit=resumed_sit, stood_up=stood_up, **self._landing_diag())
        if named and why in ("validated_room", "new_room", "rejoin") and saved is None:
            # （同图搬房间保留位置时不再传送回命名落点，否则又被拉走。）
            # 复位后服务器会重新登记玩家（日志里 slot 169→235）。只写逐帧
            # override 不够，再补一次落点传送；否则哥哥看到的小光被放回主落点。
            self._reapply_named_landing(why)

    def _set_override(self, body, *, force=False):
        o = map_long_engine.POSE_OVERRIDE
        body = self.seed if body is None else body
        # 2026-11-07 T22：命名落点期间，任何“回到 idle/站立底板”的
        # _set_override(self.base) 也不能把 override pop 掉；否则一次举烛、
        # 坐站、动画或协调器 accept 之后，引擎又逐帧发房间主落点（神殿）。
        named = self._named_landing()
        if force or (named and body in (self.raw_seed, self.base)):
            o["seed"] = body
        elif body == self.raw_seed:
            o.pop("seed", None)
        else:
            o["seed"] = body
        if (self.event is None and self.motion is None and self.position == self.home
                and self.heading is None and self.social is None and not self._active_running()):
            o.pop("post", None)
        else:
            o["post"] = self._safe_post

    def attach_terrain(self, adapter, scope_provider=None, *, prepared=False):
        """Explicit offline-candidate injection only; never auto-load/enable anything.

        scope_provider returns a fast hashable room identity key. Equal keys
        (including newly allocated equal tuples) share a controller-owned token.
        Call backend.prepare() outside post, then explicitly mark prepared=True.
        The injected sampler must honor the realtime <=50ms budget.
        """
        if type(prepared) is not bool:
            raise ValueError("terrain_prepared_bool")
        if adapter is not None:
            if not callable(scope_provider) or any(not callable(getattr(adapter, name, None))
                    for name in ("choose_motion", "bind_scope", "invalidate", "set_social_owned")):
                raise ValueError("terrain_adapter_contract")
        self._terrain_invalidate()
        self._terrain_adapter, self._terrain_scope_provider = adapter, scope_provider
        self._terrain_prepared = prepared if adapter is not None else False
        self._terrain_scope_key = self._terrain_scope = self._terrain_motion = None
        if adapter is not None:
            adapter.invalidate()
            if getattr(adapter, "enabled", False):
                self._terrain_sync()

    def set_terrain_prepared(self, prepared):
        """Pure flag: does not spawn or contact a worker."""
        if type(prepared) is not bool:
            raise ValueError("terrain_prepared_bool")
        if prepared and getattr(self, "_terrain_adapter", None) is None:
            raise ValueError("terrain_not_attached")
        if not prepared:
            self._terrain_invalidate()
        self._terrain_prepared = prepared

    def _terrain_invalidate(self, *, clear_prepared=False):
        adapter = getattr(self, "_terrain_adapter", None)
        if adapter is None:
            return
        stop = self.__dict__.get("_terrain_stop")
        if stop is None:
            from threading import RLock
            stop = self.__dict__.setdefault("_terrain_stop", [RLock(), 0, False, None])
        with stop[0]:
            stop[1] += 1
            self._terrain_request_seq = getattr(self, "_terrain_request_seq", 0) + 1
            owned = getattr(self, "_terrain_motion", None)
            if owned is not None and self.motion is owned:
                stop[2], stop[3] = True, None
                self.motion = None
                self.route, self.after_arrive = [], None
            elif stop[3] is not None and self.motion is None:
                stop[2], stop[3] = True, None
            self._terrain_scope_key = self._terrain_scope = self._terrain_motion = None
            if clear_prepared:
                self._terrain_prepared = False
        # Adapter invalidation may acquire other locks: never hold the stop lock.
        adapter.invalidate()

    def _terrain_social_owns(self):
        active = getattr(self, "_active_offer", None)
        return (getattr(self, "social", None) is not None
                or getattr(self, "sitting", None) is not None
                or active is not None and (active["phase"] in ("queued", "waiting_accept")
                    or active["phase"] == "accepted" and active.get("accepted_profile") in ("once", "loop")))

    def _terrain_sync(self):
        adapter = getattr(self, "_terrain_adapter", None)
        if adapter is None:
            return None
        air = getattr(self, "_air_motion", None)
        if air is not None and self.motion is air:
            # Air motion owns the Actor: never let the ground backend cancel or
            # replan it.  Landing calls _air_after_land -> _terrain_invalidate.
            try:
                adapter.set_social_owned(False)
            except Exception:
                pass
            return adapter, None, False
        direct = getattr(self, "_direct_walk_motion", None)
        if direct is not None and self.motion is direct:
            # T23-8：地形层 1.5 s 没起步的最近座位，已经按真实地形自行走。
            # 这一段是有意绕过地形计划的保底，不能让下面的 legacy 取消逻辑把它停掉；
            # 只对这一个 Motion 生效，其他走路仍走原来的地形校验。
            try:
                adapter.set_social_owned(False)
            except Exception:
                pass
            return adapter, None, False
        if not getattr(adapter, "enabled", False):
            # No new ownership logic may alter legacy behavior while disabled.
            if self.motion is not None and self.motion is getattr(self, "_terrain_motion", None):
                self._terrain_failed("terrain_disabled", getattr(self, "_terrain_last_position", self.position))
            return adapter, None, False
        owned = self._terrain_social_owns()
        if owned != getattr(self, "_terrain_owned", False):
            self._terrain_request_seq = getattr(self, "_terrain_request_seq", 0) + 1
            self._terrain_owned = owned
        adapter.set_social_owned(owned)
        if self.motion is not None and self.motion is not getattr(self, "_terrain_motion", None):
            # Enabling cannot leave a pre-existing unchecked straight line alive.
            # Use the last constructed position, not a new preview of that line.
            position = getattr(self, "_terrain_last_position", self.position)
            self._terrain_request_seq = getattr(self, "_terrain_request_seq", 0) + 1
            if not owned:
                self._terrain_failed("legacy_motion_cancelled", position)
            else:
                self.motion = self._terrain_motion = None
                self.route, self.after_arrive = [], None
        if owned:
            stop = self.__dict__.get("_terrain_stop")
            if stop is None:
                from threading import RLock
                stop = self.__dict__.setdefault("_terrain_stop", [RLock(), 0, False, None])
            with stop[0]:
                if self.motion is not None:
                    self.motion = self._terrain_motion = None
                    self.route, self.after_arrive = [], None
                if stop[2] or stop[3] is not None:
                    stop[1] += 1
                    stop[2], stop[3] = False, None
        if not getattr(self, "_terrain_prepared", False):
            return adapter, None, owned
        try:
            key = self._terrain_scope_provider()
            if key is not None:
                hash(key)
            same = key is not None and key == getattr(self, "_terrain_scope_key", None)
        except Exception:
            key, same = None, False
        if not same:
            self._terrain_invalidate()
            if key is not None:
                self._terrain_scope_key, self._terrain_scope = key, object()
                adapter.bind_scope(self._terrain_scope)
        return adapter, getattr(self, "_terrain_scope", None), owned

    def _terrain_failed(self, reason, position=None, *, expected=None):
        stop = self.__dict__.get("_terrain_stop")
        if stop is None:
            from threading import RLock
            stop = self.__dict__.setdefault("_terrain_stop", [RLock(), 0, False, None])
        reason = reason if isinstance(reason, str) and 0 < len(reason) <= 96 and all(
            c.isascii() and (c.isalnum() or c in "_-.: ") for c in reason) else "terrain_blocked"
        with stop[0]:
            if expected is not None and self.motion is not expected:
                return False
            # Keep diagnostic failure timing only for an accepted current stop.
            # Idle duration is never permission to bypass verified ground routing.
            if self.follow:
                target = (self.follow or {}).get("target")
                now = time.monotonic()
                # 线上 walk_to 被地形层包装（只排队目标），下面 walk_to 里“成功清零”不会执行：
                # 记首次/最近一次失败，最近一次超过 3 s 就当失败窗口已结束、重新计时。
                if (self._air_failed_target != target or self._air_failed_at is None
                        or now - (getattr(self, "_air_failed_last", None) or now) > 3.0):
                    self._air_failed_at, self._air_failed_target = now, target
                self._air_failed_last = now
            owned = getattr(self, "_terrain_motion", None)
            if (owned is not None and self.motion is owned) or stop[3] is not None:
                stop[1] += 1
                stop[2], stop[3] = True, None
            if position is not None:
                try:
                    import math
                    position = tuple(float(v) for v in position)
                    if len(position) == 3 and all(math.isfinite(v) for v in position):
                        self.position = position
                except (ValueError, TypeError, OverflowError):
                    pass
            if getattr(self, "_direct_walk_motion", None) is self.motion:
                self._direct_walk_motion = None
            self.motion = self._terrain_motion = None
            self.route, self.after_arrive = [], None
            self.state = (dict(self.state, movement_reason=reason) if getattr(self, "holding", False)
                          and self.state.get("pose") == "candle" else {"pose": "idle", "movement_reason": reason})
        self.log("terrain_move_stopped", reason=reason)
        self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") or self.base)
        return True

    @_solo_action
    @upright_action
    def walk_to(self, target, label="", face=None, speed=anim_pose.WALK_SPEED):
        """Common movement factory. Terrain is opt-in; disabled preserves legacy Motion.

        座位走路的稳定参照放在实例属性 ``_walk_ref``（喊她坐下的人）；不新增
        位置参数，旧调用/旧测试的 lambda 不会被关键字参数弄坏。
        """
        self._stuck_note_walk(target, label, face, speed)
        if label != "seat":
            self._walk_ref = None             # 只有座位走路带参照，别的走路不继承
        now = time.monotonic()
        terrain = self._terrain_sync()
        motion_before = self.motion
        operation = getattr(self, "_terrain_request_seq", 0)
        start = motion_before.pos(now) if motion_before is not None else self.position
        if terrain is not None and getattr(terrain[0], "enabled", False):
            if operation != getattr(self, "_terrain_request_seq", 0) or self.motion is not motion_before:
                return False
        factory = lambda: anim_pose.Motion(start, target, now, speed=speed, face_after=face)
        if terrain is None:
            motion = factory()
            self._terrain_motion = None
        else:
            adapter, scope, social_owned = terrain
            enabled = getattr(adapter, "enabled", False)
            if social_owned:
                adapter.choose_motion(scope, start, target, label, {}, factory, social_owns_movement=True)
                return False
            if getattr(adapter, "enabled", False) and not getattr(self, "_terrain_prepared", False):
                self._terrain_failed("terrain_not_prepared", start)
                return False
            self._terrain_request_seq = operation + 1
            operation = self._terrain_request_seq
            options = {"now": now, "speed": speed, "face_after": face}
            ref = getattr(self, "_walk_ref", None)
            if ref is not None:
                options["ref"] = ref
            motion = adapter.choose_motion(scope, start, target, label,
                options, factory, social_owns_movement=False)
            if getattr(adapter, "enabled", False) != enabled:
                return False
            if enabled:
                self._terrain_sync()
                if (self._terrain_adapter is not adapter or operation != self._terrain_request_seq
                        or self._terrain_scope is not scope or self._terrain_social_owns()
                        or not getattr(self, "_terrain_prepared", False)):
                    return False
            elif self._terrain_adapter is not adapter or operation != self._terrain_request_seq:
                return False
            if motion is None:
                decision = adapter.last_decision
                self._terrain_failed(getattr(decision, "reason", None) or "terrain_unavailable", start)
                return False
            decision = adapter.last_decision
        stop = self.__dict__.get("_terrain_stop")
        if stop is None:
            from threading import RLock
            stop = self.__dict__.setdefault("_terrain_stop", [RLock(), 0, False, None])
        with stop[0]:
            if terrain is not None and (operation != getattr(self, "_terrain_request_seq", 0)
                    or self._terrain_adapter is not adapter):
                return False
            self._terrain_motion = (motion if terrain is not None
                and getattr(decision, "status", None) == "ready" and decision.motion is motion else None)
            self._once_before_motion("walk")
            self.motion = motion
            stop[1] += 1
            stop[2], stop[3] = False, self._terrain_motion
        if motion is not None:
            # A successful ground plan means the >=2 s terrain-failure window
            # for this follow target ended.
            self._air_failed_at = None
            self._air_failed_target = None
        self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") or self.base)
        self.log("walk_start", to=[round(v, 3) for v in target], seconds=round(self.motion.seconds, 1), why=label)
        return True if terrain is not None and getattr(adapter, "enabled", False) else None

    @_solo_action
    def play(self, name_or_key, seconds=TAKE_CANDLE_SECONDS):
        """持续表情原路；一次性动画保存请求代次，旧启动线程不抢新动作。"""
        key=anim_pose.anim_key(name_or_key) if isinstance(name_or_key,str) else name_or_key
        if key in SOLO_EMOTE_EXITS and self.social is None and not self._active_running():
            self._solo_revision=getattr(self,"_solo_revision",0)+1
            return self._play(key,float(seconds),str(name_or_key),self._solo_revision)
        revision=getattr(self,"_solo_revision",0)
        threading.Thread(target=self._play,args=(key,float(seconds),str(name_or_key),revision,
            getattr(self,"_command_seq",0)),daemon=True).start()

    def _is_home_map(self):
        map_id = getattr(self, "map_id", None)
        return map_id is not None and map_id == HOME_MAP_ID

    @_home_seat_action
    def _home_cancel_sit(self):
        self._prop_seat_pending_air = None
        self._prop_seat_cancel_approach("command_replaced")
        self._home_sit_pending = None
        self._sit_pending = None

    def _home_sit_taken_over(self):
        # 自动接受社交也可能直接调用旧入口，不经过 _apply；旧座位请求不能夺回动作。
        if self.social is not None or self._active_running() or self.follow or self.holding:
            self._home_cancel_sit()
            return True
        return False

    def _home_wander_context_matches(self, context):
        """仅自动坐下的跨进程房间凭据；主人不带 context 的指令不走此门禁。"""
        if not self._is_home_map() or not isinstance(context, dict):
            return False
        if (not isinstance(context.get("home_session"), str)
                or type(context.get("room_generation")) is not int
                or type(context.get("seat_epoch")) is not int):
            return False
        with self.tracker.lock:
            tracker = self.tracker
            return bool(tracker.ready and tracker.confirmed_map_id == HOME_MAP_ID
                        and context["home_session"] == tracker.home_session
                        and context["room_generation"] == tracker.room_generation
                        and context["seat_epoch"] == tracker.seat_epoch)

    def _reject_home_wander_sit(self, detail):
        # 过期控制文件不能取消当前路线/社交/主人请求；仅发布失败原因。
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            if (getattr(self, "_home_sit_pending", None) is None
                    and getattr(self, "_sit_pending", None) is None):
                self._last_sit_failure = {"reason": "no_seat", "seat": None,
                                          "at": time.time(), "detail": str(detail)}
        self.log("sit_failed", reason="no_seat", seat=None, detail=str(detail),
                 src="wander", ignored=True)

    def _home_seat_public_state(self):
        """发布可跨进程校验的资格摘要；room 快照期间换房/换名单则保守计零。"""
        tracker = self.tracker
        with tracker.lock:
            before = (tracker.home_session, tracker.room_generation, tracker.seat_epoch)
        room = tracker.snapshot()
        with tracker.lock:
            current = (tracker.home_session, tracker.room_generation, tracker.seat_epoch)
            free, book, _proofs, _rejected = self._home_seat_snapshot(locked=True)
            coherent = (before == current and self._is_home_map()
                        and room.get("map_id") == HOME_MAP_ID
                        and room.get("room_generation") == tracker.room_generation
                        and room.get("connected") and tracker.ready
                        and room.get("slots") == {hex(s): u for s, u in tracker.slots.items()})
            if not coherent:
                free = {}
            now, mono = time.time(), time.monotonic()
            expires_at = min((min(book[sid][2] + HOME_SEAT_MAX_AGE,
                                  now + HOME_SEAT_MAX_AGE - (mono - tracker.seat_witnesses[sid][5]))
                              for sid in free), default=now)
            summary = {"schema": "home-seat-live-v1", "map_id": HOME_MAP_ID,
                       "home_session": tracker.home_session,
                       "room_generation": tracker.room_generation, "seat_epoch": tracker.seat_epoch,
                       "count": len(free), "observed_at": now, "expires_at": expires_at}
        return room, summary

    @_home_seat_action
    def _clear_home_seats(self):
        """仅清遇境本次房间的观测；epoch 阻止 reset 前尚在解码的帧写回来。"""
        self._home_sit_pending = None
        self._home_command_generation = getattr(self, "_home_command_generation", 0) + 1
        with self.tracker.lock:
            for name in ("seat_book", "seat_witnesses", "seated", "seat_checked"):
                getattr(self.tracker, name, {}).clear()
            self.tracker.seat_epoch = getattr(self.tracker, "seat_epoch", 0) + 1

    def _home_seat_snapshot(self, *, locked=False):
        """候选与身份凭据一次取样；不读文件、不推断相邻座号。"""
        if not locked:
            with self.tracker.lock:
                return self._home_seat_snapshot(locked=True)
        tracker = self.tracker
        book = dict(tracker.seat_book)
        witnesses = dict(getattr(tracker, "seat_witnesses", {}))
        slots = dict(tracker.slots)
        versions = dict(getattr(tracker, "_slot_versions", {}))
        occupied = set(tracker.seated.values())
        generation = tracker.room_generation
        epoch = getattr(tracker, "seat_epoch", 0)
        ready = tracker.ready and tracker.confirmed_map_id == HOME_MAP_ID
        free, observed, proofs, rejected = {}, {}, {}, {}
        now, mono = time.time(), time.monotonic()
        since = getattr(self, "room_since", 0.0)
        for sid, (origin, yaw, seen) in book.items():
            witness = witnesses.get(sid)
            reason = None
            if not ready or witness is None or seen < since:
                reason = "本次进房后没有实时确认过"
            else:
                identity, slot, version, room, seat_epoch, observed_mono = witness
                if identity == getattr(tracker, "own_uuid", None):
                    reason = "自己的坐姿不能确认现场椅子"
                elif room != generation or seat_epoch != epoch:
                    reason = "座位记录来自旧房间"
                elif slots.get(slot) != identity or versions.get(slot) != version:
                    reason = "坐过这里的玩家已离开或房间名单已换人"
                elif not (0 <= mono - observed_mono <= HOME_SEAT_MAX_AGE
                          and 0 <= now - seen <= HOME_SEAT_MAX_AGE):
                    reason = f"超过 {HOME_SEAT_MAX_AGE / 60:g} 分钟未实时确认"
                elif sid in occupied:
                    reason = "座位当前有人坐"
            if reason is not None:
                rejected[sid] = reason
                continue
            free[sid] = (tuple(origin), yaw, "live")
            observed[sid] = (tuple(origin), yaw, seen)
            proofs[sid] = witness[:5]
        return free, observed, proofs, rejected

    def _home_no_seat_reason(self, rejected=None):
        if rejected is None:
            rejected = self._home_seat_snapshot()[3]
        detail = "；".join(dict.fromkeys(rejected.values()))
        return "附近没有能坐的椅子（" + (detail or "只认本次进房后实时看到有人坐过的座位") + "）"

    def _home_pick_seat(self, near):
        free, _book, _proofs, rejected = self._home_seat_snapshot()
        if not free:
            raise LookupError(self._home_no_seat_reason(rejected))
        # root 旧分支没有距离过滤；新增限制只作用于遇境。
        usable = {sid: info for sid, info in free.items()
                  if sum((float(info[0][i]) - float(near[i])) ** 2 for i in (0, 2)) <= HOME_SEAT_PICK_RANGE ** 2
                  and abs(float(info[0][1]) - float(near[1])) <= HOME_SEAT_PICK_DY}
        if not usable:
            raise LookupError("附近没有能坐的椅子（实时座位距离太远或不在同一层）")
        seat = min(usable, key=lambda sid: _dist(usable[sid][0], near))
        return seat, usable[seat]

    @_home_seat_action
    def _home_prepare_sit(self, seat, info, *, context=None):
        free, _book, proofs, rejected = self._home_seat_snapshot()
        if free.get(seat) != info:
            self._home_sit_failed("no_seat", seat, rejected.get(seat, "座位在选座时已经变化"))
            return False
        if context is not None and (context.get("home_session") != self.tracker.home_session
                or (context.get("room_generation"), context.get("seat_epoch")) != proofs[seat][3:5]):
            self._reject_home_wander_sit("选座期间房间已变化，放弃自动坐下")
            return False
        self._home_sit_sequence = getattr(self, "_home_sit_sequence", 0) + 1
        self._home_sit_pending = {"seat": seat, "info": info, "proof": proofs[seat],
                                  "request": self._home_sit_sequence}
        self._last_sit_failure = None
        return True

    def _home_sit_invalid(self, seat=None, info=None, *, locked=False):
        pending = getattr(self, "_home_sit_pending", None)
        if pending is None or (seat is not None and pending["seat"] != seat):
            return "本次坐下请求已失效"
        seat = pending["seat"]
        free, _book, proofs, rejected = self._home_seat_snapshot(locked=locked)
        if seat not in free:
            return rejected.get(seat, "现场座位记录已清空")
        if proofs[seat] != pending["proof"]:
            return "走过去途中坐过座位的玩家已换人"
        if free[seat] != pending["info"] or (info is not None and info != pending["info"]):
            return "走过去途中座位位置或朝向已变化"
        return None

    @_home_seat_action
    def _home_sit_failed(self, reason, seat, detail, *, expected=None):
        if expected is not None and getattr(self, "_home_sit_pending", None) is not expected:
            return
        if reason == "seat_taken" and detail != "座位当前有人坐":
            reason = "no_seat"
        self._home_sit_pending = None
        # 取消地形 worker 的已准备请求，避免下一帧又接回旧路线。
        self._terrain_invalidate(clear_prepared=True)
        self.route = []
        if hasattr(self, "_sit_failed"):
            self._sit_failed(reason, seat=seat, stop=True, detail=detail)
            self._last_sit_failure["detail"] = str(detail)
        else:
            self.motion = self.after_arrive = None
            self._last_sit_failure = {"reason": str(reason), "seat": seat,
                                      "at": time.time(), "detail": str(detail)}
            self.state = {"pose": "idle", "sit_failed": dict(self._last_sit_failure)}
            self.log("sit_failed", reason=str(reason), seat=seat, detail=str(detail))

    @_home_seat_action
    def _home_sit_tick(self):
        pending = getattr(self, "_home_sit_pending", None)
        if pending is None:
            return False
        if self._home_sit_taken_over():
            return True
        if not self.tracker.lock.acquire(blocking=not self._prop_seat_dynamic_work()):
            return False
        try:
            reason = self._home_sit_invalid(locked=True)
        finally:
            self.tracker.lock.release()
        if reason is None:
            return False
        self._home_sit_failed("seat_taken", pending["seat"], reason, expected=pending)
        return True

    @_home_seat_action
    def _apply_home_sit(self, cmd, command_generation):
        if ((cmd.get("src") == "wander" or "home_seat_context" in cmd)
                and not self._home_wander_context_matches(cmd.get("home_seat_context"))):
            self._reject_home_wander_sit("遇境座位状态已失效，放弃排队中的自动坐下")
            return
        if (command_generation != getattr(self, "_home_command_generation", None)
                or self.social is not None or self._active_running()):
            self.log("sit_command_ignored", reason="home_action_replaced")
            return
        if self.sitting is not None:
            return
        air = getattr(self, "_air_motion", None)
        if air is not None and self.motion is air:
            # TASK-j：保留新坐下意图，当前安全飞行继续，落地后有界重试。
            return self._prop_seat_queue_after_air(cmd)
        here = self.motion.pos(time.monotonic()) if self.motion else self.position
        near_slot, near = (self.tracker.resolve(cmd["near"]) if cmd.get("near")
                           else (None, here))
        # T23-8：座位走路的参照对象固定成“喊她坐下的人”（near），不再让
        # 地形层用会随新鲜度切换的 _idle_target；找不到 UUID 时留空，由地形
        # 层沿用旧兜底，不改变校验强度。
        ref = None
        if near_slot is not None:
            try:
                with self.tracker.lock:
                    ref = self.tracker.slots.get(near_slot)
            except Exception:
                ref = None
        try:
            seat, info = self._pick_seat(near)
        except LookupError as exc:
            # 找不到空座位：状态里写 sit_failed，聊天侧不能再说“坐下了”。
            if self._is_home_map():
                self._home_sit_failed("no_seat", None, str(exc)[:120])
            else:
                self._sit_failed("no_seat", detail=str(exc)[:120])
            return
        if self._is_home_map() and not self._home_prepare_sit(seat, info, context=cmd.get("home_seat_context")):
            return
        if self._prop_seat_try_interact(seat, info, here, takeover=True):
            return
        (x, y, z), _yaw, src = info
        if src == "guess":
            # 只知道同桌别人的位置：走到那人身边离 0.8 米（朝小光这侧）站好，坐下时客户端按座位号吸附
            d = max(((here[0] - x) ** 2 + (here[2] - z) ** 2) ** 0.5, 1e-6)
            x, z = x + (here[0] - x) / d * 0.8, z + (here[2] - z) / d * 0.8
        # 走到座位旁的高度是“站地面”，不是座位面：参考座位 y - SEAT_HEIGHT 查真实地形。
        g = self.terrain_ground(x, z, y - SEAT_HEIGHT)
        stand = (x, g if g is not None else y - SEAT_HEIGHT, z)
        try:
            stand = self._prop_seat_approach(seat, info, stand, here)
        except (ValueError, TypeError, KeyError) as exc:
            self._home_sit_failed("unsafe_approach", seat, str(exc)[:120])
            return
        self.follow, self.holding, self.candle = None, False, None
        self.after_arrive = {"pose": "_sit_now", "seat": seat, "info": info}
        if self._is_home_map():
            self.after_arrive["home_request"] = self._home_sit_pending["request"]
        self._set_override(self.base)
        self._walk_ref = ref
        started = self.walk_to(stand, "seat")
        if started is False:
            if self._is_home_map():
                self._home_sit_pending = None
            self._sit_failed("walk_rejected", seat=seat)
            return
        # target/info 留着给 1.5 s 直走兜底：地形层不启动也能自己走到座位旁。
        self._sit_pending = {"since": time.monotonic(), "seat": seat, "near": tuple(near),
                             "target": tuple(stand), "info": info, "ref": ref,
                             "command_seq": self._command_seq, "direct_checked": False}
        self._last_sit_failure = None
        if self.motion is not None:
            self.state = {"pose": "walk", "to_seat": seat}
        self.log("seat_pick", seat=seat, source=src, near=[round(v, 2) for v in near])
        return

    def _seat_candidates(self):
        """空座位 {座位号: (估计坐标, 偏航码, 来源)}。来源：live = 刚看到有人坐过；file = 抓包记录；
        guess = 同桌推断（客户端按座位号吸附，坐标大致对就行）。正被别人坐着的座位不在里面。"""
        if self._is_home_map():
            free, book, _proofs, _rejected = self._home_seat_snapshot()
            return free, book
        with self.tracker.lock:
            occupied, book = set(self.tracker.seated.values()), dict(self.tracker.seat_book)
        since = getattr(self, "room_since", 0.0)
        book = {k: v for k, v in book.items() if v[2] >= since}    # 只要进这个房间之后看到的
        # 只用确实存在过的座位号（记录里的 + 实时见过的）：凭空推断的号（如 5544）客户端吸附不到，会掉到地底
        # 每条带“看到的时间”（文件记录算 0）。按时间从旧到新过一遍实时观测：同桌（号差 ≤ SEAT_GROUP）里
        # 比它旧、又离它超过 TABLE_MOVED 的记录 = 桌子挪走前的位置，改按最新位置估计（坐下时客户端按号吸附）
        if getattr(self, "map_id", None) in PERMANENT_SEAT_MAPS:
            known = {sid: (tuple(s["origin"]), s["yaw"], "file", 0.0) for sid, s in self.seats.items()}
        else:
            # 摆件桌椅：这个房间里有人真坐在 n 号，存档里 n±SEAT_GROUP、离 n 号不到 TABLE_SAME 米的座位就是同一张桌的空椅子
            known = {}
            for sid, s_ in self.seats.items():
                o = tuple(s_["origin"])
                if any(abs(sid - k) <= SEAT_GROUP and _dist(o, v[0]) <= TABLE_SAME for k, v in book.items()):
                    known[sid] = (o, s_["yaw"], "file", 0.0)
        for sid, (o, yaw, t) in sorted(book.items(), key=lambda kv: kv[1][2]):
            for k in range(sid - SEAT_GROUP, sid + SEAT_GROUP + 1):
                old = known.get(k)
                if k != sid and old and old[3] < t and _dist(old[0], o) > TABLE_MOVED:
                    known[k] = (o, yaw, "guess", t)
            known[sid] = (o, yaw, "live", t)
        return {k: v[:3] for k, v in known.items() if k not in occupied}, book

    def _learn_seats(self):
        """把实时见过的座位并进座位表并存盘，座位号越攒越全（桌子挪了也更新到最新位置）。

        2026-11-07 T22：只能写回“当前确认房间”对应的 seats_<map>.json。
        换图瞬间旧 room 的 seat_book 如果先到一步，会被写进另一张图的文件
        （线上 seats_3526133726.json 里混进了雨林/暮土的座位坐标），下次
        在遇境就可能按别人的坐标坐下。map 不匹配时整轮跳过。
        """
        if self._is_home_map():
            return
        with self.tracker.lock:
            if (not self.tracker.ready or self.map_id is None
                    or self.tracker.confirmed_map_id != self.map_id):
                return
            book = dict(self.tracker.seat_book)
        seat_file = self.seat_file
        if (seat_file is None or Path(seat_file).name != f"seats_{self.map_id}.json"):
            return
        changed = False
        for sid, (o, yaw, _t) in book.items():
            entry = {"origin": [round(v, 3) for v in o], "yaw": yaw}
            if self.seats.get(sid) != entry:
                self.seats[sid] = entry
                changed = True
        if changed and self.seat_file is not None:
            tmp = self.seat_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({str(k): v for k, v in sorted(self.seats.items())}, indent=1), encoding="utf-8")
            tmp.replace(self.seat_file)
            self.log("seats_learned", seats=sorted(self.seats))

    def _seat_in_range(self, origin, near):
        """座位是否离 near 足够近、高差足够小；防止用别的图/别的岛的记录。"""
        try:
            dx = float(origin[0]) - float(near[0])
            dy = float(origin[1]) - float(near[1])
            dz = float(origin[2]) - float(near[2])
        except (TypeError, ValueError, IndexError):
            return False
        return (math.isfinite(dx) and math.isfinite(dy) and math.isfinite(dz)
                and (dx * dx + dz * dz) ** 0.5 <= SEAT_PICK_RANGE
                and abs(dy) <= SEAT_PICK_DY)

    def _pick_seat(self, near):
        """离 near（喊她的人）最近的空座位；同桌推断的座位按座位号差略加惩罚（越近的号越可能真存在）。

        2026-11-07 T22：只接受 near 30 m / 10 m 高差内的座位。线上遇境
        座位表混进了雨林/暮土的坐标（y=70/217），旧代码永远选“最近的
        一个”，会把人送到别的图的层高上，看起来在很深的地底。
        """
        if self._is_home_map():
            return self._home_pick_seat(near)
        free, book = self._seat_candidates()
        if not free:
            raise LookupError("附近没有空座位（这张图还没见过椅子，或都坐满了）")
        usable = {sid: v for sid, v in free.items() if self._seat_in_range(v[0], near)}
        if not usable:
            raise LookupError("附近没有可信的空座位（座位记录都不在本地图范围内）")

        def score(sid):
            o, _yaw, src = usable[sid]
            if src != "guess":
                return _dist(o, near)
            return _dist(o, near) + 1.0   # 估计坐标就是同桌那人的位置，比真实座位偏近，加点惩罚
        sid = min(usable, key=score)
        return sid, usable[sid]

    @_solo_action
    @_home_seat_action
    def _sit_now(self, seat, info, *, home_request=None, _tracker_locked=False):
        """已走到座位旁：吸附到座位、朝座位方向，开始 GoSit2 保持。走的路上被人占了就就近换一个。"""
        if not self._is_home_map():
            free, _book = self._seat_candidates()
            near = self.position
            if seat not in free or not self._seat_in_range(free[seat][0], near):
                seat, info = self._pick_seat(near)
                self.log("seat_taken_repick", seat=seat)
        origin, yaw, src = info
        ready, prop_seat_binding = self._prop_seat_prepare_for_commit(
            seat, info, locked=_tracker_locked, home_request=home_request)
        if not ready:
            return False
        prev = self.event.stamp() if self.event is not None else {k: anim_pose.read(self.seed)[k] for k in ("gen", "key", "start")}
        event = anim_pose.SitEvent(seat, prev, time.monotonic())
        self.stand_spot = self._prop_seat_saved_return(self.position)
        x, y, z = origin
        # 座位是真人坐上去的坐标（椅面高度），不是地面：直接用真人的 y，不查地形、不加偏移。
        # （查地形会拿到椅子底下的地面，遇境座位 0.996 → 0.802，坐下会陷进椅子）
        self.position = (x, y, z)
        if yaw is not None:
            self.heading = anim_pose.yaw_heading(yaw)
        self.sitting = seat
        self.event = event
        self._prop_seat_begin(prop_seat_binding, seat)
        self._set_override(self.base)
        self.state = {"pose": "sit", "seat": seat}
        self._home_sit_pending = None
        self._sit_pending = None
        self._last_sit_failure = None
        self.log("sit", seat=seat, source=src, at=[round(v, 3) for v in self.position],
                 heading=None if self.heading is None else round(self.heading))
        self._prop_seat_commit_complete()

    @_home_seat_action
    def _sit_failed(self, reason, *, seat=None, stop=False, **fields):
        """记一次坐下失败：写状态 + 打 ``sit_failed``，聊天侧据此不再说“坐下了”。

        ``stop=True`` 用于 20 s 超时：放弃还没坐下的座位路线，停在当前位置，
        不继续走向一个到不了的座位。
        """
        self._prop_seat_cancel_approach("sit_" + str(reason))
        if self._is_home_map():
            self._home_sit_pending = None
        pending = getattr(self, "_sit_pending", None)
        if seat is None and pending is not None:
            seat = pending.get("seat")
        elapsed = None if pending is None else round(time.monotonic() - pending.get("since", time.monotonic()), 1)
        self._sit_pending = None
        if (isinstance(self.after_arrive, dict)
                and self.after_arrive.get("pose") == "_sit_now"):
            self.after_arrive = None
        air = getattr(self, "_air_motion", None)
        if (stop and air is not None and self.motion is air and air.plan.get("stuck_recovery")):
            # 座位失效立即撤销坐下意图，但不能把新的脱困动画硬停在半空。
            # 已验证的固定近邻落点继续完成；落地后 _stuck_current 拒绝旧座位续接。
            stop = False
            self.log("stuck_recovery_intent_cancelled", reason="seat_" + str(reason))
        if stop and self.motion is not None:
            try:
                self.position = tuple(self.motion.pos(time.monotonic()))
            except Exception:
                pass
            self._direct_walk_motion = None
            self.motion = None
        self._last_sit_failure = {"reason": str(reason), "seat": seat, "at": time.time(),
                                  "elapsed": elapsed}
        self.state = {"pose": "idle", "sit_failed": str(reason)}
        self.log("sit_failed", reason=str(reason), seat=seat, elapsed=elapsed, **fields)

    def _direct_seat_path(self, start, target):
        """T23-8：用真实地形检查“从这里直接走到座位站位”是否可行。

        返回 ``(ok, reason, goal_y, samples, max_step)``。只读已加载的
        ``air_terrain.terrain_view``（和地形层同一份 GroundSurface）：
        沿直线每 0.25 m 采地面，相邻高差 > 0.35 m 就算不同层；每个采样点
        按 air_planner._trajectory_clear 的身体高度带 [y+0.15, y+1.8]、中心
        与左右 0.35 m 检查有没有面挡住。没有碰撞面数据时不能证明能走，拒绝。
        """
        if air_planner is None or air_terrain is None:
            return False, "terrain_unavailable", None, 0, 0.0
        try:
            dist = math.hypot(float(target[0]) - float(start[0]),
                              float(target[2]) - float(start[2]))
        except (TypeError, ValueError, IndexError):
            return False, "bad_target", None, 0, 0.0
        if not math.isfinite(dist):
            return False, "bad_target", None, 0, 0.0
        if dist > SIT_DIRECT_RANGE:
            return False, "too_far", None, 0, 0.0
        terrain = self._air_terrain()
        if terrain is None:
            return False, "terrain_unavailable", None, 0, 0.0
        if not getattr(terrain, "has_hits", False):
            # 只有 height、没有面数据：无法证明身体高度带没被挡，保持原路径。
            return False, "no_collision_data", None, 0, 0.0
        count = max(1, int(math.ceil(dist / SIT_DIRECT_SAMPLE)))
        dx, dz = target[0] - start[0], target[2] - start[2]
        n = math.hypot(dx, dz)
        sx, sz = ((-dz / n) * SIT_DIRECT_BODY_SIDE, (dx / n) * SIT_DIRECT_BODY_SIDE) if n > 1e-6 else (0.0, 0.0)
        prev_y = float(start[1])
        max_step = 0.0
        goal_y = None
        for i in range(count + 1):
            f = i / count
            x = start[0] + dx * f
            z = start[2] + dz * f
            h = terrain.height(x, z, prev_y)
            if h is None or not math.isfinite(float(h)):
                return False, "no_ground", None, i + 1, max_step
            h = float(h)
            step = abs(h - prev_y)
            max_step = max(max_step, step)
            if step > SIT_DIRECT_MAX_STEP + 1e-9:
                return False, "step_too_high", None, i + 1, max_step
            for ox, oz in ((0.0, 0.0), (sx, sz), (-sx, -sz)):
                if air_planner._has_face(terrain, x + ox, z + oz,
                                         h + SIT_DIRECT_BODY_LOW, h + SIT_DIRECT_BODY_HIGH):
                    return False, "blocked", None, i + 1, max_step
            prev_y = h
            goal_y = h
        return True, None, goal_y, count + 1, max_step

    @_solo_action
    @_home_seat_action
    @upright_action
    def _sit_direct(self, pending, now):
        """地形层 1.5 s 没起步时的兜底：按真人走路姿态直接走到座位旁。

        只处理已选好座位、当前没有 motion、距离 <= 8 m 且真实地形连续的
        情况；走到后仍走原来的 ``after_arrive = _sit_now`` 坐下。走不了就
        返回 False，保留原来的 10 s 等待和 ``sit_failed``。
        """
        if self._is_home_map():
            if getattr(self, "_home_sit_pending", None) is None or self._home_sit_tick():
                return False
        seat, info = pending.get("seat"), pending.get("info")
        draw = pending.get("target")
        if self.motion is not None:
            return False
        if not isinstance(info, (tuple, list)) or len(info) != 3 or draw is None:
            return False
        start = self.motion.pos(now) if self.motion is not None else self.position
        try:
            distance = math.hypot(float(draw[0]) - float(start[0]),
                                  float(draw[2]) - float(start[2]))
        except (TypeError, ValueError, IndexError):
            distance = None
        if distance is None or not math.isfinite(distance):
            self.log("sit_direct_walk", ok=False, seat=seat, reason="bad_target")
            return False
        ok, reason, goal_y, samples, max_step = self._direct_seat_path(start, draw)
        if not ok:
            if reason == "blocked":
                pending["stuck_blocked"] = True
            self.log("sit_direct_walk", ok=False, seat=seat, reason=reason,
                     distance=round(distance, 2), elapsed=round(now - pending.get("since", now), 2))
            return False
        goal = (float(draw[0]), float(goal_y), float(draw[2]))
        motion = anim_pose.Motion(start, goal, now)
        self._direct_walk_motion = motion
        self._terrain_motion = None
        self.motion = motion
        self.after_arrive = {"pose": "_sit_now", "seat": seat, "info": info}
        if self._is_home_map():
            self.after_arrive["home_request"] = self._home_sit_pending["request"]
        self.state = {"pose": "walk", "to_seat": seat}
        self.log("sit_direct_walk", ok=True, seat=seat, reason="walk_not_started",
                 distance=round(distance, 2), elapsed=round(now - pending.get("since", now), 2),
                 samples=samples, max_step=round(max_step, 3),
                 start=[round(v, 3) for v in start], to=[round(v, 3) for v in goal])
        return True

    @_home_seat_action
    def _sit_tick(self, now=None):
        """坐下超时/中途丢失的兜底：最迟 SIT_TIMEOUT 秒放弃并写 sit_failed。"""
        now = time.monotonic() if now is None else now
        if self._prop_seat_air_tick(now):
            return False
        if self._prop_seat_rollback_tick(now):
            return True
        if self._prop_seat_commit_tick(now):
            return False
        pending = getattr(self, "_sit_pending", None)
        if pending is None:
            return False
        if self._prop_seat_approach_air_wait(pending):
            return False
        if self._is_home_map() and self._home_sit_tick():
            return True
        if self.sitting is not None:
            self._sit_pending = None
            self._last_sit_failure = None
            return False
        now = time.monotonic() if now is None else now
        if getattr(self, "_stuck_flight", None) is not None:
            return False  # 由 AirMotion 连续落地；不在半空执行座位超时的 stop。
        if self._prop_seat_pending_owner_guard(pending):
            return False
        here = self.motion.pos(now) if self.motion else self.position
        if self._prop_seat_try_interact(pending["seat"], pending["info"], here):
            return False
        elapsed = now - pending.get("since", now)
        ground_elapsed = now - pending.get("stuck_ground_since", pending.get("since", now))
        # 下一个命令把 after_arrive 清掉/换掉了：这次 sit 已经没人执行，立即记失败。
        nxt = self.after_arrive
        if isinstance(nxt, dict) and nxt.get("pose") != "_sit_now":
            self._sit_failed("after_arrive_replaced", stop=False)
            return True
        if elapsed >= SIT_TIMEOUT:
            self._sit_failed("timeout", stop=True, timeout=SIT_TIMEOUT)
            return True
        # T23-8：座位已选好、地形层 1.5 s 还没起步（没有 motion）：
        # 先用真实地形确认最近这段能走，能走就自己走过去；走不了继续原等待。
        if (self.motion is None and ground_elapsed >= SIT_DIRECT_GRACE
                and self._prop_seat_approach_jump(pending, now)):
            return False
        if (self.motion is None and ground_elapsed >= SIT_DIRECT_GRACE
                and not pending.get("direct_checked")
                and getattr(self, "_prop_seat_transaction", None) is None
                and pending.get("command_seq") == getattr(self, "_command_seq", 0)):
            pending["direct_checked"] = True
            if self._sit_direct(pending, now):
                return False
        # 地面层一直没给出行走对象（路线被挡/规划失败）：超过宽限期就记失败，
        # 不要挂着一个 after_arrive 等到 20 s 才放弃。
        if self.motion is None and ground_elapsed >= SIT_WALK_GRACE:
            self._sit_failed("walk_not_started", stop=True)
            return True
        return False

    def _stand(self):
        """站起：播 GetUpL（mode 7 约 1.2 秒），回到坐下前的站位。"""
        if getattr(self, "_prop_seat_state", None) is not None:
            self._prop_seat_exit("stand")
            return
        if self.sitting is None:
            return
        prev = self.event.stamp()
        self.event = anim_pose.AnimEvent(anim_pose.GET_UP, prev, time.monotonic(), anim_pose.GET_UP_SECONDS)
        self.log("stand", seat=self.sitting)
        self._sit_pending = None
        self.sitting = None
        if self.stand_spot is not None:
            self.position = self.stand_spot
        self._set_override(self.base)
        self.state = {"pose": "idle"}

    def _play(self, key, seconds, label, _solo_revision=None, _once_request=None):
        air=getattr(self,"_air_motion",None)
        if air is not None and self.motion is air:
            self.log("anim_skipped",anim=label,reason="air");return
        if self.sitting is not None:
            self.log("anim_skipped",anim=label,reason="坐着不做表情");return
        if not self.anim_lock.acquire(blocking=False):
            self.log("anim_skipped",anim=label,reason="上一个动画还没播完");return
        action=None
        try:
            with self.__dict__.setdefault("_solo_lock",threading.RLock()):
                if _solo_revision is not None and _solo_revision!=getattr(self,"_solo_revision",0):return
                if self._solo_start(key,label):return
                prev=self.event.stamp() if self.event is not None else anim_pose.read(self.seed)
                ev=anim_pose.AnimEvent(key,prev,time.monotonic(),seconds)
                action=self._once_begin(ev,"anim",label,_once_request)
                if action is None:return
                self.log("anim_start",anim=label,generation=ev.gen,seconds=round(seconds,2))
            complete=self._once_wait(action,ev.t0+seconds+.45+ANIM_GATE_SECONDS)
            if complete or action["cancel"].is_set() or self.stop.is_set():
                self.log("anim_done",anim=label,generation=ev.gen,start=ev.start,cut=not complete)
        except Exception as exc:self.log("anim_error",anim=label,error=str(exc)[:200])
        finally:
            if action is not None:self._once_finish(action,"done_or_cancelled")
            else:self.anim_lock.release()

    # ------------------------------------------------------------------ #
    # T23-2 shout: short / long / medium calls.  Command path: control
    # {"pose":"shout","kind":"short"|"long"|"medium","count":1..5}.  Idle
    # path is opt-in through <control>/shout.json (missing file = never).
    # ------------------------------------------------------------------ #
    def _once_begin(self, event, kind, label, expected_seq=None):
        """一次性动作只借用动画字段；不抹掉地面移动/跟随意图。"""
        from threading import Event
        lock=self.__dict__.setdefault("_once_lock",threading.RLock())
        with lock:
            if expected_seq is not None and expected_seq!=getattr(self,"_command_seq",0):
                self.log("once_action_skipped",reason="stale_command",kind=kind)
                return None
            previous=dict(self.state)
            previous_event,previous_state=self.event,self.state
            previous_override=dict(map_long_engine.POSE_OVERRIDE)
            action=dict(event=event,kind=kind,label=label,previous_state=previous,
                follow=self.follow,command_seq=getattr(self,"_command_seq",0),
                room_generation=getattr(self.tracker,"room_generation",None),
                cancel=Event(),lock_owned=True)
            self._once_action=action
            self.event=event
            if self.follow is not None:
                self.state=dict(pose="follow",target=str(self.follow.get("target",""))[:8])
            elif self.motion is not None or previous.get("pose")=="walk":
                self.state=previous if previous.get("pose")=="walk" else dict(pose="walk")
            else:
                self.state=dict(pose="shout" if kind=="shout" else "anim",anim=label)
            action["display_state"]=self.state
            try:
                self._set_override(self.base)
            except Exception:
                # begin未返回给调用方；由调用方释放刚取得的anim_lock，绝不留半发布owner。
                if getattr(self,"_once_action",None) is action:
                    owns_override=self.event is event and self.state is action["display_state"]
                    self._once_action=None
                    if self.event is event:self.event=previous_event
                    if self.state is action["display_state"]:self.state=previous_state
                    if owns_override:
                        map_long_engine.POSE_OVERRIDE.clear();map_long_engine.POSE_OVERRIDE.update(previous_override)
                action["cancel"].set();action["lock_owned"]=False
                raise
            return action

    def _once_release(self, action):
        if action is None:return
        with self.__dict__.setdefault("_once_lock",threading.RLock()):
            if action.get("lock_owned"):
                action["lock_owned"]=False
                self.anim_lock.release()

    def _once_idle_event(self, event):
        """工程化收尾：普通idle字段/底板，不重放已结束ShoutCharge或鞠躬。"""
        base=self.base
        previous=event.stamp()
        module=globals().get("shout_mod")
        class IdleAfterOnce(anim_pose.AnimEvent):
            def __init__(self):
                super().__init__(0,previous,time.monotonic()-1.,seconds=0.)
                self._ordinary=anim_pose.read(base)
                self._pose=anim_pose.pose_values(base)
                self._shout_values=({module.FLAG_TOKEN:module._read_value(base,module.FLAG_TOKEN),
                    module.TYPE_TOKEN:module._read_value(base,module.TYPE_TOKEN)} if module else None)
            def post(self, body, now):
                body=super().post(body,now)
                present=anim_pose.read(body)
                values=dict(emote_hold=0,emote_state=0,h_key=0)
                for name in ("code_f48","code_f70"):
                    if name in self._ordinary:values[name]=self._ordinary[name]
                body=anim_pose.write(body,**{k:v for k,v in values.items() if k in present})
                body=anim_pose.write_pose_fields(body,self._pose)
                if self._shout_values is not None:body=module._patch_values(body,self._shout_values)
                return body
        return IdleAfterOnce()

    def _once_finish(self, action, reason="done"):
        if action is None:return False
        lock=self.__dict__.setdefault("_once_lock",threading.RLock())
        with lock:
            # Timer返回不代表动画时钟已到；首帧时钟未推进时保留给帧门收尾。
            if (reason=="done_or_cancelled" and getattr(self,"_once_action",None) is action
                    and not action["cancel"].is_set() and not self.stop.is_set()
                    and time.monotonic()<action.get("deadline",-float("inf"))):
                return False
            action.setdefault("finish_reason",reason)
            action["cancel"].set()
            current=getattr(self,"_once_action",None) is action
            try:
                if current:
                    self._once_action=None
                    owned=self.event is action["event"]
                    same_room=action["room_generation"]==getattr(self.tracker,"room_generation",None)
                    if owned and not same_room:self.event=None
                    if owned and same_room:
                        self.event=self._once_idle_event(action["event"])
                        if self.state is action["display_state"]:
                            if self.follow is not None:
                                self.state=dict(pose="follow",target=str(self.follow.get("target",""))[:8])
                            elif self.motion is not None or reason=="movement:walk":
                                self.state=dict(pose="walk")
                            elif action["command_seq"]==getattr(self,"_command_seq",0):
                                prior=action["previous_state"]
                                self.state=dict(prior) if prior.get("pose") not in ("shout","anim") else dict(pose="idle")
                            else:self.state=dict(pose="idle")
                        self._set_override(self.base)
                    self.log("once_action_finished",kind=action["kind"],reason=reason,
                        restored=bool(owned and same_room),follow=self.follow is not None)
            except Exception as exc:
                if current and self.event is action["event"]:self.event=None
                self.log("once_cleanup_error",kind=action["kind"],error=str(exc)[:160])
            finally:self._once_release(action)
            return current

    def _once_wait(self, action, deadline):
        action["deadline"]=deadline
        if action["cancel"].is_set():return action.get("finish_reason")=="frame_deadline"
        stopped=self.stop.wait(max(0.,deadline-time.monotonic()))
        if action["cancel"].is_set():return action.get("finish_reason")=="frame_deadline"
        return not stopped and time.monotonic()>=deadline

    def _once_before_motion(self, source="walk"):
        action=getattr(self,"_once_action",None)
        if action is not None:self._once_finish(action,"movement:"+source)

    def _once_command(self, cmd):
        if cmd.get("pose") in ("follow","walk","explore","approach","sit","_sit_now",
                "stand","stop","idle","candle","accept_offer","active_offer","release"):
            self._once_before_motion(str(cmd.get("pose")))

    def _once_before_frame(self, now):
        action=getattr(self,"_once_action",None)
        if action is None:return
        if (self.event is not action["event"]
                or action["room_generation"]!=getattr(self.tracker,"room_generation",None)):
            self._once_finish(action,"owner_changed")
        elif self.motion is not None or self.social is not None or self._active_running() or self.sitting is not None:
            self._once_finish(action,"movement_or_social")
        elif (action.get("remaining",1)<=1
                and now>=action["event"].t0+action["event"].seconds+(.35 if action["kind"]=="shout" else .45+ANIM_GATE_SECONDS)):
            self._once_finish(action,"frame_deadline")

    def _shout_config(self):
        """control/shout.json -> dict 或 None（文件不存在/没开 idle_enabled）。"""
        now = time.monotonic()
        if now - self._shout_cfg_at < 1.0:
            return self._shout_cfg
        self._shout_cfg_at = now
        try:
            doc = json.loads((Path(self.path).parent / SHOUT_CONTROL_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if not isinstance(doc, dict) or doc.get("idle_enabled") is not True:
            self._shout_cfg = None
            return None
        try:
            min_gap = max(1.0, float(doc.get("min_gap", SHOUT_IDLE_MIN_GAP)))
        except (TypeError, ValueError):
            min_gap = SHOUT_IDLE_MIN_GAP
        try:
            mean_gap = max(min_gap, float(doc.get("mean_gap", SHOUT_IDLE_MEAN_GAP)))
        except (TypeError, ValueError):
            mean_gap = max(min_gap, SHOUT_IDLE_MEAN_GAP)
        self._shout_cfg = {"min_gap": min_gap, "mean_gap": mean_gap}
        return self._shout_cfg

    def _shout_busy_reason(self):
        """不能叫的原因（命令和随机叫共用）；None = 可以叫。"""
        if shout_mod is None:
            return "no_module"
        if self.sitting is not None:
            return "sitting"
        if self.social is not None or self._active_running():
            return "social"
        air = getattr(self, "_air_motion", None)
        if air is not None or (self.motion is not None and self.motion is air):
            return "air"
        if self.holding or self.candle is not None:
            return "candle"
        if self.anim_lock.locked():
            return "anim_busy"
        return None

    def _shout_command(self, cmd):
        kind = str(cmd.get("kind") or "short").lower()
        if kind not in ("short", "long", "medium"):
            self.log("shout_skipped", kind=kind, reason="bad_kind")
            return False
        try:
            count = int(cmd.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(5, count))
        return self._shout(kind, count, source="command")

    def _shout_gap(self, min_gap, mean_gap):
        try:
            gap = random.expovariate(1.0 / mean_gap)
        except (ValueError, ZeroDivisionError):
            gap = mean_gap
        return max(min_gap, gap)

    def _shout_roll(self):
        """独自叫的口味：1/10 大叫，1/6 连叫 2–3 声，其余短叫一声。"""
        r = random.random()
        if r < 0.10:
            return "long", 1
        if r < 0.10 + 1.0 / 6.0:
            return "short", random.choice((2, 3))
        return "short", 1

    @_solo_action
    def _shout(self, kind, count=1, source="command", now=None):
        """一次性叫声，不覆盖跟随意图；移动/新动作可有界中断。"""
        now=time.monotonic() if now is None else now
        reason=self._shout_busy_reason()
        if reason is not None:
            self.log("shout_skipped",kind=kind,count=count,source=source,reason=reason)
            return False
        if not self.anim_lock.acquire(blocking=False):
            self.log("shout_skipped",kind=kind,count=count,source=source,reason="anim_busy")
            return False
        action=None;started=False
        try:
            prev=self.event.stamp() if self.event is not None else anim_pose.read(self.seed)
            ev=shout_mod.ShoutEvent(kind,prev,now)
            action=self._once_begin(ev,"shout",kind)
            if action is None:return False
            action["remaining"]=count
            self._shout_spawn(kind,action)
            self._last_shout_at=now
            self.log("shout_start",kind=kind,index=1,count=count,source=source,generation=ev.gen,seconds=round(ev.seconds,2))
            threading.Thread(target=self._shout_run,args=(ev,kind,count,source,action),daemon=True,name="shout").start()
            started=True
            return True
        except Exception as exc:
            self.log("shout_error",kind=kind,error=str(exc)[:200])
            return False
        finally:
            if not started:
                if action is not None:self._once_finish(action,"start_error")
                else:self.anim_lock.release()

    def _shout_spawn(self, kind, action=None):
        """延迟光晕沿用原时序；已取消事件的Timer不再发旧SpawnShout。"""
        def current():
            return action is None or (getattr(self,"_once_action",None) is action
                and not action["cancel"].is_set() and self.event is action["event"])
        def send():
            if not current():return
            try:
                with self.tracker.lock:own=self.tracker._own_social_binding_locked()
                epoch=LAST_EPOCH
                if own is None or epoch is None:
                    self.log("shout_spawn_skipped",kind=kind,own=own,epoch=epoch)
                    return
                if not current():return
                game=shout_mod.send_spawn_shout(epoch,own,kind)
                self.log("shout_spawn",kind=kind,own=own,body=game[-8:].hex())
            except Exception as exc:self.log("shout_spawn_error",kind=kind,error=str(exc)[:160])
        if kind=="long":threading.Timer(shout_mod.CHARGE_SECONDS,send).start()
        else:send()

    def _shout_run(self, first, kind, count, source, action=None):
        """按原叫声时序播放；过期worker只释放自己的锁，不恢复旧意图。"""
        action=action or getattr(self,"_once_action",None)
        if action is None:return
        ev,started=first,first.t0
        try:
            for index in range(1,count+1):
                action["remaining"]=count-index+1
                gap=random.uniform(*SHOUT_CHAIN_GAP) if index<count else 0.
                duration=(ev.seconds+shout_mod.GATE_SECONDS+gap if kind=="long" or index==count else gap)
                complete=self._once_wait(action,started+duration)
                self.log("shout_done",kind=kind,index=index,count=count,generation=ev.gen,start=ev.start,
                    cut=not complete or (index<count and kind!="long"))
                if not complete or index==count:break
                with self.__dict__.setdefault("_once_lock",threading.RLock()):
                    if getattr(self,"_once_action",None) is not action or self.event is not ev:break
                    reason=self._shout_busy_reason()
                    if reason is not None and reason!="anim_busy":break
                    ev=shout_mod.ShoutEvent(kind,ev.stamp(),time.monotonic())
                    action["event"]=self.event=ev;started=ev.t0
                    self._set_override(self.base)
                    self.log("shout_start",kind=kind,index=index+1,count=count,source=source,
                        generation=ev.gen,seconds=round(ev.seconds,2))
                self._shout_spawn(kind,action)
        except Exception as exc:self.log("shout_error",kind=kind,error=str(exc)[:200])
        finally:self._once_finish(action,"done_or_cancelled")

    def _shout_idle_ready(self, now, min_gap):
        """独自叫的前提：没人管、没跟随、在 explore/idle、房间里有别人。"""
        if now - self._last_shout_at < min_gap:
            return "gap"
        if now - self._last_command_at < SHOUT_COMMAND_QUIET:
            return "commanded"
        if self.follow is not None:
            return "following"
        reason = self._shout_busy_reason()
        if reason is not None:
            return reason
        if self.motion is not None and not self._explore_active:
            return "moving"
        if not self._explore_active and self.state.get("pose") not in ("idle", "explore"):
            return "busy_pose"
        tracker = self.tracker
        if not getattr(tracker, "ready", False):
            return "room_not_ready"
        try:
            with tracker.lock:
                slots = dict(tracker.slots)
                own = getattr(tracker, "own_uuid", None)
        except Exception:
            return "room_unknown"
        if not any(u and u != own for u in slots.values()):
            return "empty_room"
        return None

    def _shout_idle_tick(self, now=None):
        """自己逛/闲着时偶尔叫：指数间隔，至少 min_gap，文件不存在永不触发。"""
        now = time.monotonic() if now is None else now
        cfg = self._shout_config()
        if cfg is None:
            self._shout_next_at = None
            return False
        min_gap, mean_gap = cfg["min_gap"], cfg["mean_gap"]
        if self._shout_next_at is None:
            gap = self._shout_gap(min_gap, mean_gap)
            self._shout_next_at = now + gap
            self.log("shout_idle_scheduled", gap=round(gap, 1))
            return False
        if now < self._shout_next_at:
            return False
        reason = self._shout_idle_ready(now, min_gap)
        if reason is not None:
            self._shout_next_at = now + SHOUT_IDLE_RETRY
            return False
        kind, count = self._shout_roll()
        since = None if not self._last_shout_at else round(now - self._last_shout_at, 1)
        if self._shout(kind, count, source="idle", now=now):
            next_gap = self._shout_gap(min_gap, mean_gap)
            self.log("shout_idle", kind=kind, count=count, since=since,
                     gap=round(next_gap, 1))
            self._shout_next_at = now + next_gap
            return True
        self._shout_next_at = now + SHOUT_IDLE_RETRY
        return False

    def run(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while not self.stop.is_set():
            try:
                self._poll()
            except Exception as exc:
                self.log("pose_error", error=str(exc)[:200])
            try:
                self._learn_seats()
            except Exception as exc:
                self.log("seat_learn_error", error=str(exc)[:200])
            try:
                self._arrival_snap_tick()
            except Exception as exc:
                self.log("arrival_snap_error", error=str(exc)[:200])
            try:
                self._reground_tick()
            except Exception as exc:
                self.log("terrain_reground_error", error=str(exc)[:200])
            try:
                self._follow_tick()
            except LookupError:
                pass                               # 目标暂时没坐标（如刚换房间），下一秒再看
            except Exception as exc:
                self.log("follow_error", error=str(exc)[:200])
            try:
                self._shout_idle_tick()
            except Exception as exc:
                self.log("shout_idle_error", error=str(exc)[:200])
            try:
                self._prop_seat_tick()
                self._sit_tick()
            except Exception as exc:
                self.log("sit_tick_error", error=str(exc)[:200])
            try:
                self._write_state()
            except OSError as exc:
                self.log("pose_state_write_failed", error=str(exc)[:200])
            self.stop.wait(1.0)
        map_long_engine.POSE_OVERRIDE.clear()

    def _spot_near(self, target, distance):
        """目标身边、在目标与小光连线上离目标 distance 的站位（已换成小光的站立高度）和目标位置。"""
        _slot, (tx, ty, tz) = self.tracker.resolve(target)
        motion = self.motion
        # The pose-control thread must not advance the stateful terrain worker.
        # Actor post is its sole sampler; follow reads its last committed origin.
        terrain_owned = motion is not None and motion is getattr(self, "_terrain_motion", None)
        cx, cy, cz = (self.position if terrain_owned or motion is None
                      else motion.pos(time.monotonic()))
        dx, dz = cx - tx, cz - tz
        d = max((dx * dx + dz * dz) ** 0.5, 1e-6)
        terrain_goal = (getattr(getattr(self, "_terrain_adapter", None), "enabled", False)
                        and not self._terrain_social_owns())
        if terrain_goal:
            distance = min(distance, getattr(self, "_terrain_follow_distance", distance))
        ex, ez = tx + dx / d * distance, tz + dz / d * distance
        if terrain_goal:
            # Terrain goals stay in the observed Actor-origin coordinate space.
            # The backend alone converts origin to feet; the legacy peer-height
            # table and its extra standing offset would shift the nav endpoint.
            return (ex, ty, ez), (tx, ty, tz), d
        # 2026-11-06：不再用玩家踩点拼的高度表/站立偏移；真实地形查不到才用对方的 y。
        g = self.terrain_ground(ex, ez, ty)
        return (ex, g if g is not None else ty, ez), (tx, ty, tz), d

    @_solo_action
    def _aim_candle(self, target, slot, origin):
        """按目标当前位置重建举烛姿态（瞄准点 = 目标位置），身体转向目标。"""
        body, _ = candle_pose.build(self.base, target_slot=slot, target_origin=origin)
        if not self.holding and self.event is not None and not isinstance(self.event, anim_pose.ClearEvent):
            self.event = anim_pose.ClearEvent(self.event.stamp())   # 上一个动作还挂着会盖住举烛：先清成“没在播动作”
        self.candle = {"target": target, "origin": origin}
        here = self.motion.pos(time.monotonic()) if self.motion else self.position
        if self.motion is None:
            self.heading = anim_pose.heading_to(here, origin)
        self._set_override(body)

    def _candle_tick(self):
        """举烛中每秒：目标挪动就更新瞄准点；走远了就举着蜡烛跟过去。"""
        c = self.candle
        if not self.holding or c is None:
            return
        slot, origin = self.tracker.resolve(c["target"])
        if sum((a - b) ** 2 for a, b in zip(origin, c["origin"])) ** 0.5 > CANDLE_REAIM:
            self._aim_candle(c["target"], slot, origin)
            self.state = dict(self.state, target_origin=[round(v, 3) for v in origin])
        here = self.motion.b if self.motion else self.position
        if ((here[0] - origin[0]) ** 2 + (here[2] - origin[2]) ** 2) ** 0.5 > CANDLE_FOLLOW:
            spot, who, d = self._spot_near(c["target"], 1.0)
            self.walk_to(spot, "candle_follow", face=who,
                         speed=FOLLOW_RUN_SPEED if d > FOLLOW_RUN_DISTANCE else anim_pose.WALK_SPEED)

    def _relink_tick(self):
        """跟着过图后：哥哥在新房间出现，就恢复成过图前的牵手/背背（只改小光自己的状态，不发邀请/接受）。"""
        r = getattr(self, "relink", None)
        if not r:
            return
        if time.monotonic() > r["until"]:
            self.relink = None
            self.log("social_relink_timeout", who=r["who"][:8])
            return
        with self.tracker.lock:
            slots = dict(self.tracker.slots)
            there = None
            theirs = next((s for s, u in slots.items() if u == r["who"]), None)
            if theirs is not None:
                there = self.tracker.origins.get(theirs)
        own = next((s for s, u in slots.items() if u == self.tracker.own_uuid), None)
        if theirs is None or own is None or there is None:
            return
        if getattr(self.tracker, "roster_at", 0) <= r.get("after", 0):
            return                                     # 还是旧房间的名单
        self.relink = None
        kind = r["kind"]
        self.motion = self.after_arrive = self.follow = self.candle = None
        g = self.terrain_ground(there[0], there[2], there[1])
        self.position = (there[0], g if g is not None else there[1], there[2])
        social = social_pose.build(self.base, theirs, kind)
        self.social = {"kind": kind, "who": r["who"], "slot": theirs, "own": own, "since": time.monotonic()}
        self.hold_prev = None
        self.last_social = {"who": r["who"], "kind": kind, "since": time.monotonic(), "ended": None}
        self._set_override(social)
        self.state = {"pose": "social", "kind": kind, "with": r["who"][:8]}
        self.log("social_relinked", kind=kind, who=r["who"][:8], slot=hex(theirs))

    def _candle_reply_tick(self):
        """别人对小光举蜡烛：小光闲着就举起来对着他；对方放下（或走了、20 秒兜底）就放下并鞠躬。

        T23-4：跳/飞/跳下进行中不开始回应，也不在空中补鞠躬。落地后的下一次
        tick 再看对方是否还举着；还举着就正常回应（或已放下就什么都不做）。
        """
        air = getattr(self, "_air_motion", None)
        if air is not None and self.motion is air:
            if getattr(self, "candle_reply", None) is not None:
                self.log("candle_reply_skipped", reason="air")
                self.candle_reply = None
                if self.holding:
                    self.holding, self.candle = False, None
            return
        if self._active_running():
            return
        now = time.monotonic()
        with self.tracker.lock:
            targets, slots = dict(self.tracker.candle_targets), dict(self.tracker.slots)
            origins = dict(self.tracker.origins)
        own = next((s for s, u in slots.items() if u == self.tracker.own_uuid), None)
        r = getattr(self, "candle_reply", None)
        if r is not None:
            if now - r["since"] < CANDLE_REPLY_MAX:
                # 对方的举烛字段每秒才读一次、还会闪：至少举 CANDLE_REPLY_MIN 秒，且对方连续 CANDLE_REPLY_GONE 秒没在对着她才放下
                if targets.get(r["slot"]) == own and r["slot"] in slots:
                    r["gone"] = None
                    return
                r["gone"] = r.get("gone") or now
                if now - r["since"] < CANDLE_REPLY_MIN or now - r["gone"] < CANDLE_REPLY_GONE:
                    return
            self.candle_reply = None
            if self.holding and self.candle and self.candle.get("target") == r["who"]:
                self.holding, self.candle = False, None
                self._set_override(self.base)
                self.state = {"pose": "idle"}
                self.play(BOW_ANIM, 4.0)
                self.log("candle_reply_done", who=r["who"][:8], held=round(now - r["since"], 1))
            if r.get("follow"):
                self.follow = r["follow"]
                self.state = {"pose": "follow", "target": str(r["follow"]["target"])[:8]}
            return
        # 注意：self.event 播完动作后也一直保留（延续状态），不能拿它判断“在忙”——之前因此鞠躬一次后再也不回应
        if own is None or not any(t == own for t in targets.values()):
            return                                    # 没人对着她举：什么都不动（之前这里会每秒打断她走路）
        busy = ("互动中" if self.social else "坐着" if self.sitting is not None
                else "举着蜡烛" if self.holding else None)
        if busy is None and self.motion is not None and not self.motion.arrived(now):
            if getattr(self, "route", None):          # 只打断自己逛的路线；别人让她走的（走过去/跟随）不打断
                self.route = []
                self.position, self.motion = tuple(self.motion.pos(now)), None
            elif not self.follow:
                busy = "走路中"
        if busy:
            if now - getattr(self, "_busy_diag", -1e9) > 10:
                self._busy_diag = now
                self.log("candle_seen_busy", why=busy)
            return
        cd = self.__dict__.setdefault("candle_reply_cd", {})
        here = self.motion.pos(now) if self.motion is not None else self.position
        for slot, t in targets.items():
            o = origins.get(slot)
            if t != own or slot not in slots or o is None:
                continue
            d = ((here[0] - o[0]) ** 2 + (here[2] - o[2]) ** 2) ** 0.5
            why = ("冷却中" if now - cd.get(slot, -1e9) < CANDLE_REPLY_COOLDOWN else
                   "太远" if d > CANDLE_REPLY_RANGE else None)
            if why:
                diag = self.__dict__.setdefault("candle_diag", {})
                if now - diag.get(slot, -1e9) > 10:
                    diag[slot] = now
                    self.log("candle_seen_no_reply", slot=hex(slot), why=why, dist=round(d, 1))
                continue
            with self.__dict__.setdefault("_solo_lock", threading.RLock()):
                if self._solo_defer(type(self)._candle_reply_tick, (), {}):
                    return
            cd[slot] = now
            who = slots[slot]
            self._aim_candle(who, slot, tuple(o[:3]))
            self.holding = True
            self.state = {"pose": "candle", "target_slot": hex(slot), "reply": True}
            self.candle_reply = {"slot": slot, "who": who, "since": now, "follow": self.follow}
            self.follow = None                         # 跟随中被举烛：先停下回应，完了接着跟
            self.log("candle_reply", who=who[:8], slot=hex(slot), dist=round(d, 1))
            return

    WALK_SAVE_EVERY = 60.0
    WALK_LAYER_GAP = 0.8

    def _walk_record_tick(self):
        """把追踪器里新看到的地面高度并进 control/walk_<map>.json（每格每层一个中位数 + 观测次数）。"""
        now = time.monotonic()
        if now - getattr(self, "_walk_saved", 0.0) < self.WALK_SAVE_EVERY or getattr(self, "map_id", None) is None:
            return
        self._walk_saved = now
        with self.tracker.lock:
            if not self.tracker.ready or self.tracker.confirmed_map_id != self.map_id:
                return
            map_id = self.map_id
            room_generation = self.tracker.room_generation
            cells = {k: list(v) for k, v in self.tracker.heights.items()}
            totals = dict(self.tracker.height_total)
        if getattr(self, "_walk_seen_room", None) != room_generation:
            self._walk_seen, self._walk_seen_room = {}, room_generation
        seen = self.__dict__.setdefault("_walk_seen", {})
        fresh = {}
        for k, ys in cells.items():                     # 按累计次数取新样本（列表只留最近 20 个）
            new = totals.get(k, 0) - seen.get(k, 0)
            if new > 0:
                fresh[k] = ys[-min(new, len(ys)):]
        seen.update(totals)
        fresh = {k: ys for k, ys in fresh.items() if ys}
        if not fresh:
            return
        f = Path(self.path).parent / f"walk_{map_id}.json"
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = {"map_id": map_id, "cell": HEIGHT_CELL, "points": []}
        layers = {}
        for x, y, z, c in d["points"]:
            layers.setdefault((round(x / HEIGHT_CELL), round(z / HEIGHT_CELL)), []).append([y, c])
        for key, ys in fresh.items():
            cell = layers.setdefault(key, [])
            for y in ys:
                hit = next((l for l in cell if abs(l[0] - y) <= self.WALK_LAYER_GAP), None)
                if hit is None:
                    cell.append([y, 1])
                else:                                   # 滑动平均到中间值附近，次数 +1
                    hit[0] = round(hit[0] + (y - hit[0]) / (hit[1] + 1), 3)
                    hit[1] += 1
        d["points"] = [[k[0] * HEIGHT_CELL, y, k[1] * HEIGHT_CELL, c] for k, ls in sorted(layers.items()) for y, c in ls]
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(f)

    # ------------------------------------------------------------------ #
    # T23-1 air follow.  Everything here is inert unless
    # <control>/air_follow.json says {"enabled": true, "follow": true}.
    # ------------------------------------------------------------------ #
    def _stuck_binding(self):
        return (getattr(self, "map_id", None),
                getattr(self.tracker, "room_generation", None),
                getattr(self, "_command_seq", 0))

    def _stuck_reset_intent(self):
        # reset/switch_map/relocate 的旧点目标不能复活；保留中的空中动画仍由原逻辑落地。
        self._stuck_intent = self._stuck_tracker = None
        self._stuck_request = self._stuck_cold_wait = self._stuck_poll = None
        self._stuck_resume = self._stuck_flight = None
        self._stuck_goal_seen = self._stuck_follow_safe_goal = None
        self._stuck_unknown_support = self._stuck_support_observers = None

    def _stuck_note_walk(self, goal, label="", face=None, speed=anim_pose.WALK_SPEED):
        """只记录逻辑目标；不把 bounded_pilot 分段许可当成新意图。"""
        if label == "bounded_pilot" or stuck_recovery is None:
            return
        try:
            goal = tuple(float(v) for v in goal)
            if len(goal) != 3 or not all(math.isfinite(v) for v in goal):
                return
        except (TypeError, ValueError, OverflowError):
            return
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            follow = self.follow or {}
            kind = label if label in ("follow", "seat") else "movement"
            subject = follow.get("target") if kind == "follow" else kind
            key = (self._stuck_binding(), kind, subject)
            previous = getattr(self, "_stuck_intent", None)
            if previous is not None and previous["key"] == key:
                tracker = getattr(self, "_stuck_tracker", None)
                if (tracker is not None and tracker.exhausted and kind == "movement"
                        and label not in ("seat_exit", "seat_rollback")
                        and _dist(previous["goal"], goal) > .05):
                    self._stuck_tracker = None
                    self._stuck_limit_logged = False
                    self._stuck_rejected_landings = []
                    self.log("stuck_recovery_rearmed", reason="new_movement_goal")
                previous.update(goal=goal, label=label, face=face, speed=speed)
                return
            old_tracker = getattr(self, "_stuck_tracker", None)
            if (previous is not None and old_tracker is not None and old_tracker.exhausted
                    and previous["binding"][:2] == self._stuck_binding()[:2]
                    and previous["binding"][2] != self._stuck_binding()[2]):
                self.log("stuck_recovery_rearmed", reason="new_movement_command", intent=label)
            self._stuck_intent = dict(key=key, binding=self._stuck_binding(), goal=goal,
                                     label=label, face=face, speed=speed,
                                     command_started_at=time.monotonic())
            self._stuck_tracker = None
            self._stuck_limit_logged = False
            self._stuck_rejected_landings = []

    def _stuck_request_bind(self, runtime):
        intent = getattr(self, "_stuck_intent", None)
        self._stuck_request = (runtime, getattr(runtime, "_request_id", None),
                               None if intent is None else intent["key"],
                               getattr(getattr(runtime, "backend", None), "last_route_diagnostic", None))
        self._stuck_cold_wait = (runtime, self._stuck_request[2], time.monotonic())

    def _stuck_current(self, intent):
        if intent is None or intent["binding"] != self._stuck_binding():
            return False
        current = getattr(self, "_stuck_intent", None)
        if current is None or current["key"] != intent["key"]:
            return False
        if intent["label"] == "follow":
            return bool(self.follow and self.follow.get("target") == intent["key"][2])
        if intent["label"] == "seat":
            pending = getattr(self, "_sit_pending", None)
            return bool(pending and pending.get("command_seq") == self._command_seq)
        return True

    def _stuck_deferred(self, reason):
        if reason != getattr(self, "_stuck_deferred_reason", None):
            self._stuck_deferred_reason = reason
            self.log("stuck_recovery_deferred", reason=reason)

    def _stuck_pause(self, runtime, now):
        intent, tracker = getattr(self, "_stuck_intent", None), getattr(self, "_stuck_tracker", None)
        if intent is not None and tracker is not None:
            tracker.observe(intent["key"], self.position, now, moving=False)
        self._stuck_seen_prepare = (runtime, getattr(runtime, "last_prepare_decision", None))
        self._stuck_cold_wait = self._stuck_poll = None

    def _stuck_goal_geometry_revision(self):
        revision = getattr(self.tracker, "props_revision", None)
        if callable(revision):
            try:
                return revision()
            except Exception:
                return None
        terrain = self._air_terrain()
        return ("static", id(getattr(terrain, "surface", None))) if terrain is not None else None

    def _stuck_safe_follow_goal(self, goal):
        """缓存只属于当前原目标；道具/位置/指令变化立即失效。"""
        cached = getattr(self, "_stuck_follow_safe_goal", None)
        if cached is None:
            return goal
        revision = self._stuck_goal_geometry_revision()
        if (cached["binding"] != self._stuck_binding() or not self.follow
                or cached["target"] != self.follow.get("target")
                or time.monotonic() >= cached["until"] or revision is None
                or revision != cached["geometry_revision"] or _dist(goal, cached["original"]) > .25):
            self._stuck_follow_safe_goal = None
            return goal
        return cached["goal"]

    def _stuck_invalid_goal(self, runtime, label=None, desired=None, now=None):
        """目的地失败交回选点，不取得空中/座位/社交所有权。"""
        now = time.monotonic() if now is None else now
        if stuck_recovery is None:
            return None
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            intent = getattr(self, "_stuck_intent", None)
            if (not self._stuck_current(intent) or self._terrain_social_owns()
                    or self._air_motion is not None or self._air_block_reason(now) is not None
                    or getattr(self, "_solo_gate", None) is not None
                    or isinstance(self.event, anim_pose.ContinuousEmoteEvent)):
                return None
            permission = getattr(runtime, "_permission", None)
            if not callable(permission) or permission() is None:
                return None
            decision = getattr(runtime, "last_prepare_decision", None)
            if decision is None or getattr(decision, "status", None) == "ready":
                return None
            seen = getattr(self, "_stuck_goal_seen", None)
            if seen is not None and seen[0] is runtime and seen[1] is decision:
                return None
            bound = getattr(self, "_stuck_request", None)
            if (bound is None or bound[0] is not runtime or bound[2] != intent["key"]
                    or bound[1] != getattr(runtime, "_future_request", None)):
                return None
            reason = getattr(decision, "reason", "") or ""
            diagnostic = getattr(getattr(runtime, "backend", None), "last_route_diagnostic", None)
            if (reason == "worker_prepare_failed" and isinstance(diagnostic, dict)
                    and diagnostic is not bound[3] and diagnostic.get("operation") == "preparePlan"
                    and diagnostic.get("status") in ("blocked", "unavailable")):
                reason = diagnostic.get("reason") or reason
            if stuck_recovery.classify_failure(reason) != "invalid_goal":
                return None
            self._stuck_goal_seen = (runtime, decision)
            tracker = getattr(self, "_stuck_tracker", None)
            if tracker is not None:
                tracker.note_failure(reason, now, token=(id(runtime), bound[1]))
            result = dict(action="await_new_goal", goal=None, reason=reason, label=intent["label"])
            if intent["label"] == "seat":
                result["action"] = "seat_existing_policy"
            elif intent["label"] == "follow":
                try:
                    original, face, _ = self._spot_near(self.follow["target"], self.follow["distance"])
                    revision = self._stuck_goal_geometry_revision()
                    selected = stuck_recovery.nearest_supported_goal(self._air_terrain(), original)
                    if (not self._stuck_current(intent) or permission() is None
                            or self._terrain_social_owns() or self._air_motion is not None
                            or self._air_block_reason(now) is not None):
                        return None
                    if revision is None or revision != self._stuck_goal_geometry_revision():
                        selected = dict(goal=None, reason="goal_geometry_changed", tested=0, candidates=0)
                    goal = selected["goal"]
                    self._stuck_follow_safe_goal = dict(binding=self._stuck_binding(),
                        target=self.follow["target"], original=tuple(original), goal=goal,
                        geometry_revision=revision, until=math.inf if goal is not None else now + 2.0)
                    result.update(action="follow_near_support", goal=goal, original_goal=tuple(original), face=face,
                                  selected_reason=selected["reason"], tested=selected["tested"])
                except (LookupError, KeyError, ValueError, TypeError):
                    result["action"] = "follow_target_unavailable"
            elif getattr(self, "_explore_active", False) or intent["label"] in ("route", "target"):
                result["action"] = "reselect_explore"
            log_key = (intent["key"], reason, result["action"])
            logged = getattr(self, "_stuck_goal_log", None)
            if logged is None or logged[0] != log_key or now-logged[1] >= 5.0:
                self._stuck_goal_log = (log_key, now)
                self.log("ground_goal_invalid", reason=reason, intent=intent["label"],
                         action=result["action"], old_goal=list(intent["goal"]),
                         new_goal=list(result["goal"]) if result["goal"] is not None else None,
                         recovery_started=False)
            return result

    def _stuck_unknown_prop_support(self, now):
        """短时疑似支撑只压住误触发；从不把观测点拼成可行走大面。"""
        reader=getattr(self.tracker,'props_snapshot',None)
        if not callable(reader):return None
        binding=self._stuck_binding()[:2]
        old=getattr(self,'_stuck_unknown_support',None)
        try:snapshot=reader()
        except Exception:snapshot=None
        if snapshot is None:
            return (old['value'] if old and old['binding']==binding
                    and 0<=now-old['at']<=.25 and _dist(self.position,old['position'])<=.05 else None)
        if (not snapshot.get('validated') or snapshot.get('room_generation')!=binding[1]
                or snapshot.get('map_id')!=binding[0]):
            self._stuck_unknown_support=None
            return None
        props=list(snapshot.get('props',[]))+list(snapshot.get('unmodeled_props',[]))
        if not props:
            self._stuck_unknown_support=None
            return None
        previous=getattr(self,'_stuck_support_observers',None) or {}
        observers=[];samples={}
        for row in snapshot.get('support_observers',[])[:64]:
            if not isinstance(row,dict):continue
            at=row.get('observed_mono');point=row.get('position');slot=row.get('slot')
            if (type(at) not in (int,float) or not 0<=now-at<=2.5
                    or not isinstance(point,(list,tuple)) or len(point)!=3):continue
            identity=(binding,slot,row.get('uuid'),row.get('version'))
            before=previous.get(identity)
            stationary=bool(before and .15<=at-before[0]<=2.5 and _dist(point,before[1])<=.05)
            samples[identity]=(at,point)
            observers.append(dict(row,stationary=stationary))
        self._stuck_support_observers=samples
        terrain=self._air_terrain()
        value=stuck_recovery.unmodeled_support_observation(terrain,self.position,
            props,observers)
        self._stuck_unknown_support=(dict(value=value,binding=binding,at=now,
            position=tuple(self.position)) if value else None)
        return value

    def _stuck_ground_tick(self, runtime, label=None, desired=None, now=None):
        """Actor 线程的独立脱困通道：不伪造旧 groundFailure，不改正常行走。"""
        now = time.monotonic() if now is None else now
        if stuck_recovery is None:
            return False
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            if getattr(self, "_stuck_flight", None) is not None:
                if (self._air_motion is not None and self.motion is self._air_motion
                        and self._stuck_flight.get("air") is self._air_motion):
                    return True
                flight, self._stuck_flight = self._stuck_flight, None
                flight["tracker"].failed(now, "air_owner_lost")
                self.log("stuck_recovery_failed", reason="air_owner_lost", attempt=flight["tracker"].attempts)
            resume = getattr(self, "_stuck_resume", None)
            if resume is not None:
                self._stuck_resume = None
                self._stuck_resume_ground(resume, now)
            self._prop_seat_ground_confirm(runtime, label, desired, now)
            intent = getattr(self, "_stuck_intent", None)
            if not self._stuck_current(intent):
                return False
            if not self._air_enabled():
                self._stuck_pause(runtime, now)
                return False
            config = (self._air_config() or {}).get("stuck_recovery", {})
            if not isinstance(config, dict) or config.get("enabled", True) is not True:
                self._stuck_pause(runtime, now)
                return False
            tracker = getattr(self, "_stuck_tracker", None)
            if tracker is None:
                try:
                    fields = stuck_recovery.RecoveryPolicy.__dataclass_fields__
                    policy = stuck_recovery.RecoveryPolicy(**{k: v for k, v in config.items() if k in fields})
                except (TypeError, ValueError):
                    self._stuck_deferred("invalid_config")
                    return False
                tracker = self._stuck_tracker = stuck_recovery.RecoveryTracker(policy)
            busy = (self._terrain_social_owns() or self._air_motion is not None
                    or self._air_block_reason(now) is not None
                    or getattr(self, "_solo_gate", None) is not None
                    or isinstance(self.event, anim_pose.ContinuousEmoteEvent))
            # 沿用当前准入，过期目标/换房/冷加载不被解释成地形卡住。
            permission = getattr(runtime, "_permission", None)
            if busy or not callable(permission) or permission() is None:
                tracker.observe(intent["key"], self.position, now, moving=False)
                self._stuck_seen_prepare = (runtime, getattr(runtime, "last_prepare_decision", None))
                self._stuck_poll = (intent["key"], now, False)
                return False
            if intent["label"] == "seat" and self._is_home_map() and self._home_sit_tick():
                return False
            goal = intent["goal"]
            if intent["label"] == "follow":
                try:
                    goal, _who, _distance = self._spot_near(self.follow["target"], self.follow["distance"])
                    intent["goal"] = tuple(goal)
                except (LookupError, KeyError):
                    tracker.observe(intent["key"], self.position, now, moving=False)
                    return False
            moving = _dist(self.position, goal) > (getattr(self, "_terrain_follow_slack", .6)
                                                  if intent["label"] == "follow" else .35)
            unknown=self._stuck_unknown_prop_support(now)
            explicit_departure=bool(unknown and intent['label'] not in ('seat','seat_rollback')
                and 0<=now-intent.get('command_started_at',-float('inf'))<=2.
                and (_dist(self.position,goal)>1.5 or goal[1]<self.position[1]-.25))
            if unknown and not explicit_departure:
                tracker.observe(intent['key'],self.position,now,moving=False)
                self._stuck_seen_prepare=(runtime,getattr(runtime,'last_prepare_decision',None))
                if now-getattr(self,'_stuck_unknown_logged_at',-float('inf'))>=3.:
                    self._stuck_unknown_logged_at=now
                    self.log('unknown_prop_support_wait',**unknown)
                return False
            waiting = getattr(runtime, "_future", None) is not None
            poll = getattr(self, "_stuck_poll", None)
            cold = getattr(self, "_stuck_cold_wait", None)
            since = None
            if cold is not None and cold[0] is runtime and cold[1] == intent["key"]:
                since = cold[2]
                self._stuck_cold_wait = (runtime, cold[1], now) if waiting else None
            if poll is not None and poll[0] == intent["key"] and (waiting or poll[2]):
                since = poll[1] if since is None else min(since, poll[1])
            if (since is not None and tracker.intent == intent["key"]
                    and tracker.last_progress is not None):
                # 只冻结无进展时钟，不清失败计数；冷准备完成的最后一段也扣除。
                tracker.last_progress = min(now, tracker.last_progress + max(0., now - since))
            self._stuck_poll = (intent["key"], now, waiting)
            tracker.observe(intent["key"], self.position, now, moving=moving)
            if not moving:
                return False
            if tracker.rearm_after_cooldown(now, self.position):
                self._stuck_limit_logged = False
                self._stuck_rejected_landings = []
                # Keep receipt identity and tracker tokens: a cached worker
                # failure cannot become fresh evidence after the cooldown.
                self.log("stuck_recovery_rearmed", reason="exhausted_cooldown",
                         cycle=tracker.cycles,
                         cooldown=max(tracker.policy.exhausted_cooldown_seconds,
                                      2*tracker.policy.cooldown_seconds))
            decision = getattr(runtime, "last_prepare_decision", None)
            seen = getattr(self, "_stuck_seen_prepare", None)
            if unknown is not None:
                self._stuck_seen_prepare=(runtime,decision)
            if unknown is None and decision is not None and (seen is None or seen[0] is not runtime or seen[1] is not decision):
                self._stuck_seen_prepare = (runtime, decision)
                bound = getattr(self, "_stuck_request", None)
                if (bound is not None and bound[0] is runtime and bound[1] == getattr(runtime, "_future_request", None)
                        and bound[2] == intent["key"] and getattr(decision, "status", None) != "ready"):
                    reason = getattr(decision, "reason", "") or ""
                    diagnostic = getattr(getattr(runtime, "backend", None), "last_route_diagnostic", None)
                    if (reason == "worker_prepare_failed" and isinstance(diagnostic, dict)
                            and diagnostic is not bound[3] and diagnostic.get("operation") == "preparePlan"
                            and diagnostic.get("status") in ("blocked", "unavailable")):
                        reason = diagnostic.get("reason") or reason
                    if ((stuck_recovery.classify_failure(reason)=='invalid_start'
                            or 'nearest_polygon_missing' in str(reason).lower())
                            and 'start_overlaps_collision' not in str(reason).lower()
                            and stuck_recovery.dynamic_support_valid(self._air_terrain(),self.position)):
                        tracker.observe(intent['key'],self.position,now,moving=False)
                        tracker.observe(intent['key'],self.position,now,moving=True)
                        if now-getattr(self,'_stuck_platform_logged_at',-float('inf'))>=3.:
                            self._stuck_platform_logged_at=now
                            self.log('dynamic_prop_support_wait',reason='navigation_start_missing_not_support',
                                     route_reason=reason,at=list(self.position))
                        return False
                    category = tracker.note_failure(reason, now, token=(id(runtime), bound[1]))
                    if category != "ignore":
                        # Count every fresh receipt; log the first two, then one summary per five seconds.
                        log_key = (intent["key"], category, reason)
                        logged = getattr(self, "_stuck_failure_log", None)
                        count = logged[2]+1 if logged and logged[0] == log_key else 1
                        if logged is None or logged[0] != log_key or count <= 2 or now-logged[1] >= 5.0:
                            self._stuck_failure_log = (log_key, now, count)
                            self.log("stuck_ground_failure", category=category, reason=reason,
                                     failures=tracker.failures, invalid_failures=tracker.invalid_failures,
                                     intent=intent["label"])
                        else:
                            self._stuck_failure_log = (log_key, logged[1], count)
                    else:
                        # worker/生命周期错误中断连续几何失败，不能借旧证据乱跳。
                        tracker.observe(intent["key"], self.position, now, moving=False)
                        tracker.observe(intent["key"], self.position, now, moving=True)
                elif getattr(decision, "status", None) == "ready":
                    tracker.observe(intent["key"], self.position, now, moving=False)
                    tracker.observe(intent["key"], self.position, now, moving=True)
            # blocked 直走是真实几何证据，但同一检查只能计一次，不按帧伪造连续失败。
            pending = getattr(self, "_sit_pending", None)
            if intent["label"] == "seat" and pending and pending.get("stuck_blocked"):
                tracker.note_failure("blocked", now, token=("seat_direct", id(pending)))
            reason = 'unknown_prop_departure' if explicit_departure else tracker.trigger(now)
            if tracker.exhausted:
                if not getattr(self, "_stuck_limit_logged", False):
                    self._stuck_limit_logged = True
                    self.log("stuck_recovery_stopped", reason="attempt_limit", attempts=tracker.attempts,
                             intent=intent["label"])
                return False
            if reason is None or getattr(runtime, "_future", None) is not None:
                return False
            # 等待冷启动/身份验证不是无进展；只有实际地面运动或几何失败允许这一触发。
            if reason not in ("invalid_start","unknown_prop_departure"):
                active_route = self.motion is not None and self.motion is getattr(self, "_terrain_motion", None)
                if (not active_route or tracker.last_progress is None
                        or now - tracker.last_progress < tracker.policy.stall_seconds):
                    return False
                reason = "no_progress"
            clips, terrain = self._air_clips(), self._air_terrain()
            if clips is None or not clips.ok or terrain is None:
                self._stuck_deferred("no_air_clips" if clips is None or not clips.ok else "no_terrain")
                return False
            if not tracker.begin_attempt(now):
                return False
            try:
                rejected = getattr(self, "_stuck_rejected_landings", [])
                plan = stuck_recovery.plan_recovery(terrain, self.position, goal, policy=tracker.policy,
                    escape_near_prop=(reason=='invalid_start'),
                    reject=lambda point: any(_dist(point, old) < .5 for old in rejected))
            except Exception as exc:
                tracker.failed(now, "geometry_error")
                self.log("stuck_recovery_failed", reason="geometry_error", error=str(exc)[:160],
                         attempt=tracker.attempts)
                return False
            if plan.get("kind") not in ("jump", "fly"):
                why = plan.get("reason", "no_safe_landing")
                tracker.failed(now, why)
                self.log("stuck_recovery_failed", reason=why, attempt=tracker.attempts)
                return False
            # 几何搜索之后再次检查所有者；_air_start 仍经既有 solo -> air 双锁事务。
            if (not self._stuck_current(intent) or permission() is None
                    or self._air_block_reason(now) is not None or getattr(self, "_solo_gate", None) is not None):
                tracker.failed(now, "owner_changed")
                return False
            flight = dict(intent=dict(intent), tracker=tracker, started=now,
                          route=copy.deepcopy(self.route), after=copy.deepcopy(self.after_arrive),
                          landing=tuple(plan["landing"]))
            if intent["label"] == "seat" and pending:
                flight["after"] = {"pose": "_sit_now", "seat": pending["seat"], "info": pending["info"]}
                if self._is_home_map():
                    flight["after"]["home_request"] = self._home_sit_pending["request"]
            plan["stuck_recovery"] = True
            plan["recovery_intent"] = intent["label"]
            self._stuck_flight = flight
            started = self._air_start(plan, now)
            if not started or self._air_motion is None or self.motion is not self._air_motion:
                self._stuck_flight = None
                tracker.failed(now, "air_start_rejected")
                self.log("stuck_recovery_failed", reason="air_start_rejected", attempt=tracker.attempts)
                return False
            flight["air"] = self._air_motion
            self.__dict__.setdefault("_stuck_rejected_landings", []).append(flight["landing"])
            self._stuck_deferred_reason = None
            self.log("stuck_recovery_start", reason=reason, kind=plan["kind"], attempt=tracker.attempts,
                     start=list(plan["start"]), landing=list(plan["landing"]), intent=intent["label"])
            return True

    def _stuck_after_land(self, air, now):
        flight = getattr(self, "_stuck_flight", None)
        if flight is None or flight.get("air") is not air:
            return
        self._stuck_flight = None
        tracker = flight["tracker"]
        terrain = self._air_terrain()
        clearance = air.plan.get("prop_exterior_landing")
        if (getattr(air, "cancelled", False) or
                not stuck_recovery.landing_valid(terrain, self.position, policy=tracker.policy)
                or clearance and not stuck_recovery.prop_exterior_valid(
                    terrain, self.position, clearance=clearance, policy=tracker.policy)):
            tracker.failed(now, "landing_not_validated")
            self.__dict__.setdefault("_stuck_rejected_landings", []).append(flight["landing"])
            self.log("stuck_recovery_failed", reason="landing_not_validated", attempt=tracker.attempts)
            self._prop_seat_recovery_landing_failed(flight, now, "landing_not_validated")
            return
        tracker.landed(now, self.position)
        flight["landed_at"] = now
        # 下一 Actor 才续接，确保真实 landing 帧先经过原有 post 提交；不复用旧 lease。
        self._stuck_resume = flight
        self.log("stuck_recovery_landed", at=list(self.position), attempt=tracker.attempts,
                 intent=flight["intent"]["label"])

    def _stuck_overlap_frame_guard(self, air, now):
        """Only this recovery's owned AirMotion gets the local-contact proof."""
        if air.plan.get("static_overlap_escape") and self._air_motion is air and self.motion is air:
            why = None
            try:
                if getattr(air, "_static_overlap_binding", None) != self._stuck_binding()[:2]:
                    why = "static_overlap_room_changed"
                else:
                    terrain = self._air_terrain()
                    if terrain is None:
                        why = "static_overlap_geometry_unavailable"
                    elif not prop_motion_guard.covers(getattr(self, "_terrain_adapter", None), air.plan):
                        why = "static_overlap_coverage_changed"
                    else:
                        state, why = stuck_recovery.static_overlap_frame(
                            terrain, air, self.position, now-air.t0,
                            getattr(air, "_static_overlap_frame", None))
                        if why is None:
                            air._static_overlap_frame = state
            except Exception as exc:
                why = "static_overlap_frame_error"
                self.log("static_overlap_frame_error", error=str(exc)[:160])
            if why is not None:
                if self._air_motion is air and self.motion is air:
                    self._air_motion = self.motion = self._air_phase = None
                    self._terrain_last_position = self.position
                    flight = getattr(self, "_stuck_flight", None)
                    if flight is not None and flight.get("air") is air:
                        self._stuck_flight = None
                        flight["resume_cancelled"] = True
                        flight["tracker"].failed(now, why)
                    self.log("air_static_overlap_interrupted", reason=why, at=list(self.position))
                return False
        seat_approach=air.plan.get('prop_seat_approach')
        platform=air.plan.get('prop_platform_transfer') or seat_approach
        if platform and self._air_motion is air and self.motion is air:
            why=None
            try:
                if tuple(platform.get('binding',()))!=self._stuck_binding()[:2]:
                    why='prop_platform_room_changed'
                else:
                    terrain=self._air_terrain()
                    if terrain is None:why='prop_platform_geometry_unavailable'
                    else:
                        check=(stuck_recovery.prop_seat_approach_frame if seat_approach else stuck_recovery.prop_platform_frame)
                        state,why=check(terrain,air,self.position,
                            now-air.t0,getattr(air,'_prop_platform_frame',None))
                        if why is None:air._prop_platform_frame=state
            except Exception as exc:
                why='prop_platform_frame_error'
                self.log('prop_platform_frame_error',error=str(exc)[:160])
            if why is not None:
                if self._air_motion is air and self.motion is air:
                    self._air_motion=self.motion=self._air_phase=None
                    self._terrain_last_position=self.position
                    self.log('prop_platform_interrupted',reason=why,at=list(self.position))
                return False
            return True
        flight = getattr(self, "_stuck_flight", None)
        if (flight is None or flight.get("air") is not air
                or self._air_motion is not air or self.motion is not air
                or not air.plan.get("prop_overlap_escape")):
            return True
        why, next_state = None, None
        try:
            if flight["intent"]["binding"][:2] != self._stuck_binding()[:2]:
                why = "prop_overlap_room_changed"
            else:
                before = (self._stuck_goal_geometry_revision(),
                          prop_motion_guard.selection(getattr(self, "_terrain_adapter", None)))
                terrain = self._air_terrain()
                if terrain is None:
                    why = "prop_overlap_geometry_unavailable"
                elif not prop_motion_guard.covers(getattr(self, "_terrain_adapter", None), air.plan):
                    why = "prop_overlap_coverage_changed"
                else:
                    next_state, why = stuck_recovery.prop_overlap_frame(
                        terrain, air, self.position, now-air.t0,
                        flight.get("overlap_frame"), policy=flight["tracker"].policy)
                    after = (self._stuck_goal_geometry_revision(),
                             prop_motion_guard.selection(getattr(self, "_terrain_adapter", None)))
                    if before != after:
                        why = "prop_overlap_geometry_changed_during_frame"
        except Exception as exc:
            why = "prop_overlap_frame_error"
            self.log("stuck_recovery_frame_error", error=str(exc)[:160])
        if why is None:
            flight["overlap_frame"] = next_state
            return True
        # Hold the last emitted position. Never render/cancel into an unchecked
        # new trajectory, and never clear another social/air owner's state.
        if (self._stuck_flight is flight and self._air_motion is air and self.motion is air):
            self._stuck_flight = None
            self._air_motion = self.motion = self._air_phase = None
            flight["resume_cancelled"] = True
            if getattr(self, "_stuck_resume", None) is flight:
                self._stuck_resume = None
            self._terrain_last_position = self.position
            flight["tracker"].failed(now, why)
            self.log("stuck_recovery_interrupted", reason=why, at=list(self.position),
                     attempt=flight["tracker"].attempts)
        return False

    def _stuck_seat_rollback(self, goal, now=None):
        """Control-tick rollback of a failed seat; never revive its sit event."""
        now = time.monotonic() if now is None else now
        if stuck_recovery is None:
            return False
        with self.__dict__.setdefault("_solo_lock", threading.RLock()):
            if (not self._air_enabled() or self._terrain_social_owns()
                    or self._air_motion is not None or self._air_block_reason(now) is not None
                    or getattr(self, "_solo_gate", None) is not None
                    or isinstance(self.event, anim_pose.ContinuousEmoteEvent)):
                return False
            terrain = self._air_terrain()
            if terrain is None:
                return False
            if stuck_recovery.landing_valid(terrain, self.position):
                self._stuck_rollback_tracker = None
                return True
            clips = self._air_clips()
            if clips is None or not clips.ok:
                return False
            binding = self._stuck_binding()
            key = (binding, "seat_rollback", tuple(goal))
            saved = getattr(self, "_stuck_rollback_tracker", None)
            if saved is None or saved[0] != key:
                saved = (key, stuck_recovery.RecoveryTracker())
                self._stuck_rollback_tracker = saved
            tracker = saved[1]
            tracker.observe(key, self.position, now, moving=True)
            if tracker.rearm_after_cooldown(now, self.position):
                self.log("stuck_recovery_rearmed", reason="seat_rollback_cooldown", cycle=tracker.cycles)
            if not tracker.begin_attempt(now):
                return False
            try:
                plan = stuck_recovery.plan_recovery(terrain, self.position, goal, policy=tracker.policy)
            except Exception as exc:
                tracker.failed(now, "seat_rollback_geometry_error")
                self.log("stuck_recovery_failed", reason="seat_rollback_geometry_error", error=str(exc)[:160])
                return False
            if plan.get("kind") not in ("jump", "fly"):
                tracker.failed(now, plan.get("reason", "no_safe_landing"))
                self.log("stuck_recovery_failed", reason=plan.get("reason"), intent="seat_rollback",
                         attempt=tracker.attempts)
                return False
            if (binding != self._stuck_binding() or self._terrain_social_owns()
                    or self._air_motion is not None or self._air_block_reason(now) is not None):
                tracker.failed(now, "owner_changed")
                return False
            intent = dict(key=key, binding=binding, goal=tuple(goal), label="seat_rollback",
                          face=None, speed=anim_pose.WALK_SPEED)
            flight = dict(intent=intent, tracker=tracker, started=now, route=[], after=None,
                          landing=tuple(plan["landing"]), seat_rollback=True)
            plan.update(stuck_recovery=True, recovery_intent="seat_rollback")
            self._stuck_flight = flight
            if not self._air_start(plan, now) or self._air_motion is None or self.motion is not self._air_motion:
                self._stuck_flight = None
                tracker.failed(now, "air_start_rejected")
                return False
            flight["air"] = self._air_motion
            self.log("stuck_recovery_start", reason="seat_rollback_invalid_stand", kind=plan["kind"],
                     start=list(plan["start"]), landing=list(plan["landing"]), intent="seat_rollback",
                     attempt=tracker.attempts)
            return True

    def _stuck_resume_ground(self, flight, now):
        if self._prop_seat_resume_after_recovery(flight):
            if getattr(self,'_stuck_resume',None) is flight:
                self._stuck_resume=None
            return
        if flight.get("seat_rollback"):
            latest = getattr(self, "_stuck_intent", None)
            if (not self._stuck_current(latest) or latest["label"] in ("seat", "seat_rollback")
                    or flight.get("resume_cancelled")):
                self.log("stuck_recovery_resume_skipped", reason="seat_rollback_no_new_movement")
                return
            # Read the latest intent, not the failed seat's saved continuation.
            flight = dict(flight, intent=dict(latest), route=[], after=None)
        if self._prop_seat_recovery_resume(flight, now):
            return
        intent = flight["intent"]
        if (flight.get("resume_cancelled") or not self._stuck_current(intent) or self._terrain_social_owns()
                or self._air_motion is not None or not self._air_enabled()):
            self.log("stuck_recovery_resume_skipped", reason="intent_or_owner_changed")
            return
        goal = intent["goal"]
        if intent["label"] == "follow":
            try:
                goal, face, _ = self._spot_near(self.follow["target"], self.follow["distance"])
                intent["face"] = face
            except (LookupError, KeyError):
                self.log("stuck_recovery_resume_skipped", reason="target_lost")
                return
        if intent["label"] == "seat":
            if self._is_home_map() and self._home_sit_tick():
                return
            pending = self._sit_pending
            pending["stuck_ground_since"] = now
            pending["direct_checked"] = False
            pending.pop("stuck_blocked", None)
            self._walk_ref = pending.get("ref")
        result = self.walk_to(goal, intent["label"], face=intent["face"], speed=intent["speed"])
        if result is not False and self._stuck_current(intent):
            self.route, self.after_arrive = flight["route"], flight["after"]
            self.log("stuck_recovery_replan", intent=intent["label"], at=list(self.position),
                     goal=list(goal), old_lease_reused=False)
        else:
            self.log("stuck_recovery_failed", reason="ground_requeue_rejected", attempt=flight["tracker"].attempts)

    def _air_config(self):
        now = time.monotonic()
        if now - self._air_cfg_at < 1.0:
            return self._air_cfg
        self._air_cfg_at = now
        try:
            doc = json.loads((Path(self.path).parent / AIR_CONTROL_FILE).read_text())
        except (OSError, ValueError):
            doc = None
        self._air_cfg = doc if isinstance(doc, dict) else None
        return self._air_cfg

    def _air_enabled(self):
        if air_planner is None or air_motion is None:
            return False
        cfg = self._air_config() or {}
        return cfg.get("enabled") is True and cfg.get("follow") is True

    def _air_terrain(self):
        adapter = getattr(self, "_terrain_adapter", None)
        if air_terrain is None or adapter is None:
            return None
        key = (id(adapter), bool(getattr(adapter, "enabled", False)),
               prop_motion_guard.selection(adapter))
        revision_reader = getattr(getattr(self, "tracker", None), "props_revision", None)
        revision = None
        if callable(revision_reader):
            try:
                revision = revision_reader()
            except Exception:
                revision = None
            if (type(revision) is not tuple or len(revision) != 2
                    or any(type(value) is not int or value < 0 for value in revision)):
                # 未取得当前房间版本时不能继续用旧桌面，也不能把忙当作有效空表。
                self._air_terrain_key = self._air_terrain_view = None
                return None
            key += revision
        if key != self._air_terrain_key or not getattr(self._air_terrain_view, "has_hits", False):
            view = air_terrain.terrain_view(adapter)
            if prop_motion_guard.selection(adapter) != key[2]:
                self._air_terrain_key = self._air_terrain_view = None
                return None
            if callable(revision_reader):
                try:
                    after = revision_reader()
                except Exception:
                    after = None
                if (type(after) is not tuple or len(after) != 2
                        or any(type(value) is not int or value < 0 for value in after)
                        or after != revision):
                    self._air_terrain_key = self._air_terrain_view = None
                    return None
            self._air_terrain_key, self._air_terrain_view = key, view
        return self._air_terrain_view

    def _air_clips(self):
        if air_motion is None:
            return None
        if self._air_clips_cache is None:
            self._air_clips_cache = air_motion.AirClips(Path(__file__).with_name("air_clips.json"))
        return self._air_clips_cache

    def _air_fail_seconds(self, target):
        if self._air_failed_at is None or self._air_failed_target != target:
            return 0.0
        now = time.monotonic()
        last = getattr(self, "_air_failed_last", None) or self._air_failed_at
        if now - last > 3.0:
            return 0.0                        # 最近 3 s 没再失败：地面后端已经能走
        return max(0.0, now - self._air_failed_at)

    def _air_target_settled(self, who, terrain, now):
        """目标站稳了才规划空中动作：脚下离真实地面 <=0.6 m，且最近 1 s 挪动 <0.8 m。
        哥哥自己在跳/飞时不追着起飞（否则会反复起飞又因 target_moved 取消）。"""
        hist = self.__dict__.setdefault("_air_target_hist", [])
        hist.append((now, tuple(who)))
        # 跟随 tick 约 1 s 一次（还要加处理时间），窗口必须比两次 tick 的间隔长，
        # 否则每次都只剩当前这一条，永远 target_history_short（17:43 上线后实测一次没飞）。
        while hist and now - hist[0][0] > 2.5:
            hist.pop(0)
        # 2026-11-07：只和“上一次判断时”的位置比（0.5 s 以前的最近一条），
        # 不再要求整个 2.5 s 窗口都没动——否则哥哥落地后要等飞行中的记录全部过期才起飞，反应慢。
        older = [(t, p) for t, p in hist if now - t >= 0.5]
        if not older:
            return False, "target_history_short"
        if _dist(older[-1][1], who) >= 0.8:
            return False, "target_moving"
        try:
            g = terrain.height(who[0], who[2], who[1])
        except Exception:
            g = None
        if g is None or abs(float(who[1]) - g) > 0.6:
            return False, "target_airborne"
        return True, None

    def _air_log_plan(self, now, **fields):
        """air_plan 日志限流：jump/drop/fly 每次都记；walk/none 原因变了或每 30 s 记一次。"""
        key = (fields.get("kind"), fields.get("reason"))
        last = getattr(self, "_air_plan_last", None)
        if key[0] in ("jump", "drop", "fly") or last is None or last[0] != key or now - last[1] >= 30.0:
            self._air_plan_last = (key, now)
            self.log("air_plan", **fields)

    def _air_cancel(self, reason, now=None):
        """Abort the current air plan; airborne plans brake/fall, never hover."""
        air = self._air_motion
        if air is None or self.motion is not air:
            return False
        now = time.monotonic() if now is None else now
        # 地面参考点：空中改航的计划用哥哥脚下，普通计划用起飞点（小光刚站过的地面）。
        prefer = air.plan.get("ground_ref") or (None if air.plan.get("airborne_start") else air.plan.get("takeoff"))
        ok = air.cancel(now, self._air_terrain(), reason, prefer=prefer)
        if not ok:
            # No ground below: keep the last safe plan instead of teleporting.
            return False
        return True

    def _air_tick(self, now):
        """While an air plan owns movement: cancel on interrupt, else keep it."""
        from air_follow_runtime import tick as _tick_air_follow
        follow_result = _tick_air_follow(self, now)
        if follow_result is not None:
            return follow_result
        air = self._air_motion
        if air is None:
            return False
        if self.motion is not air:
            self._air_motion = None
            return False
        if air.arrived(now):
            return True                       # _post renders the landing frame
        if getattr(air, "cancelled", False):
            return True                       # 已在刹停下落：落完再说，不重复取消
        if not self._air_enabled():
            self._air_cancel("disabled", now)
            return True
        if (self.social is not None or self.sitting is not None or self.holding
                or self.candle is not None or self._active_running()):
            self._air_cancel("social", now)
            return True
        if air.plan.get("stuck_recovery"):
            flight = getattr(self, "_stuck_flight", None)
            if flight is None or not self._stuck_current(flight["intent"]):
                # 目标取消只取消续接：短程静态落点已经验证，不因座位/点目标失效悬停。
                if flight is not None:
                    flight["resume_cancelled"] = True
            return True  # 固定就近落点；不追着跟随目标改航，也不要求 seat/explore 有 follow。
        follow = self.follow or {}
        if not follow:
            self._air_cancel("no_follow", now)
            return True
        try:
            _slot, origin = self.tracker.resolve(follow["target"])
        except Exception:
            self._air_cancel("target_lost", now)
            return True
        if _dist(origin, tuple(air.plan.get("target") or origin)) > AIR_RETARGET:
            # 2026-11-07：空中改航线飞向哥哥新位置，不再半空落地重飞。
            # 还在起飞前的走路段就照旧取消（在地上，取消就是停下）；改航失败就按原计划飞完。
            phase = air.phase(now)
            if phase == "walk":
                self._air_cancel("target_moved", now)
                return True
            terrain = self._air_terrain()
            settled, _why = (self._air_target_settled(origin, terrain, now)
                             if terrain is not None else (False, None))
            if settled and now - getattr(self, "_air_retarget_at", 0.0) >= 1.0:
                self._air_retarget_at = now
                new = air_planner.plan_from_air(terrain, air.pos(now), origin,
                                                follow.get("distance", 1.2))
                if new is not None:
                    self._air_log_plan(now, **air_planner.describe(new))
                    self._air_start(new, now)
            return True
        return True

    def _hop_running(self):
        """experimental_ground_entry 注入的 hop 状态探针；隔离测试里没有就是 False。"""
        probe = getattr(self, "_hop_active_probe", None)
        try:
            return bool(probe()) if callable(probe) else False
        except Exception:
            return False

    def _hop_hard_cancel(self, reason):
        cancel = getattr(self, "_hop_cancel_probe", None)
        if not callable(cancel):
            return False
        try:
            cancel(reason)
            return True
        except Exception as exc:
            self.log("hop_cancel_failed", reason=reason, error=str(exc)[:120])
            return False

    def _air_block_reason(self, now):
        """T23-4：这些状态下不能启动 AirMotion。

        空中接管 Actor 时，旧的一次性动画（anim_lock / AnimEvent 未结束）或
        举烛回应会把动画字段写回鞠躬；hop_follow_runtime_v20 的小跳也会和
        AirMotion 同时驱动位置。返回 None 才允许起飞。
        """
        if self.sitting is not None:
            return "sitting"
        if self.social is not None or self._active_running():
            return "social"
        if self.holding or self.candle is not None:
            return "candle"
        if getattr(self, "candle_reply", None) is not None:
            return "candle_reply"
        lock = getattr(self, "anim_lock", None)
        if lock is not None and lock.locked():
            return "anim_busy"
        event = self.event
        if isinstance(event, anim_pose.AnimEvent):
            try:
                if now < event.t0 + event.seconds + 0.45:
                    return "event_busy"
            except (TypeError, AttributeError):
                return "event_busy"
        if self._hop_running():
            return "hop_active"
        return None

    @_solo_action
    @upright_action
    def _air_start(self, plan, now):
        """Create AirMotion from a planner result and take movement ownership."""
        self._once_before_motion('air')
        if not prop_motion_guard.covers(getattr(self, "_terrain_adapter", None), plan):
            self._air_log_plan(now, kind="none", reason="props_path_coverage_missing")
            return False
        # T23-4：AirMotion 和旧 hop 不能同时驱动。这里先 hard-cancel hop 再接管；
        # hop 是旧的“小跳补台阶”实现，AirMotion 是本次跟随的完整轨迹，保留
        # AirMotion 才能保证位置/落地连续，hop 的半步只会在空中抢帧。
        if self._hop_running():
            if self._hop_hard_cancel("air_start"):
                self.log("air_hop_cancelled", reason="air_start")
        # Invalidate any terrain-owned plan first; the worker must not pull the
        # Actor back while the air motion is driving.
        if getattr(self, "_terrain_adapter", None) is not None:
            self._terrain_invalidate()
        from air_follow_runtime import motion_type as _air_motion_type
        motion = _air_motion_type(plan, air_motion.AirMotion)(
            plan, now, clips=self._air_clips(), walk_clip=self.walk_clip,
            terrain=self._air_terrain(), log=self.log)
        if plan.get("static_overlap_escape"):
            motion._static_overlap_binding = self._stuck_binding()[:2]
        stop = self.__dict__.get("_terrain_stop")
        if stop is None:
            from threading import RLock
            stop = self.__dict__.setdefault("_terrain_stop", [RLock(), 0, False, None])
        with stop[0]:
            self.motion = self._air_motion = motion
            self._terrain_motion = None
            self.route, self.after_arrive = [], None
            stop[1] += 1
            stop[2], stop[3] = False, None
        self._air_phase = None
        self._set_override(self.base)
        self.log("air_start", kind=plan.get("kind"),
                 pos=[round(v, 2) for v in plan.get("start", ())],
                 takeoff=None if plan.get("takeoff") is None else [round(v, 2) for v in plan["takeoff"]],
                 landing=None if plan.get("landing") is None else [round(v, 2) for v in plan["landing"]],
                 cruise=None if plan.get("cruise_height") is None else round(plan["cruise_height"], 2),
                 seconds=round(motion.seconds, 2), phases=len(plan.get("phases") or ()))
        return True

    def _air_maybe(self, spot, who, distance, now):
        """Ask the planner; start an AirMotion when it says jump/drop/fly."""
        if not self._air_enabled():
            return False
        if self._air_motion is not None:
            return True
        blocked = self._air_block_reason(now)
        if blocked is not None:
            # T23-4：空中动画/鞠躬/举烛回应/hop 进行中都不起飞。音效/事件结束
            # 后下一次跟随 tick 会重新规划，不用在半空中硬接。
            self._air_log_plan(now, kind="none", reason=blocked, dist=round(distance, 2))
            return False
        clips = self._air_clips()
        if clips is None or not clips.ok:
            self._air_log_plan(now, kind="none", reason="no_clips",
                               dist=round(distance, 2))
            return False
        if not prop_motion_guard.focus(getattr(self, "_terrain_adapter", None), self.position, who):
            self._air_log_plan(now, kind="none", reason="props_focus_unavailable")
            return False
        terrain = self._air_terrain()
        follow = self.follow or {}
        target_key = follow.get("target")
        # 地形层的运动对象不一定有 pos()；self.position 每帧由 _post 更新。
        start = self.position
        if terrain is None:
            self._air_log_plan(now, kind="none", reason="no_terrain",
                               start=[round(v, 2) for v in start], dist=round(distance, 2))
            return False
        from air_follow_runtime import maybe_start as _start_air_follow
        if _start_air_follow(self, terrain, who, now):
            return True
        settled, why = self._air_target_settled(who, terrain, now)
        if not settled:
            self._air_log_plan(now, kind="none", reason=why, dist=round(distance, 2))
            return False
        failed = self._air_fail_seconds(target_key)
        result = air_planner.plan(terrain, start, who, follow.get("distance", 1.2),
                                  # A stop/timeout is not a bound groundFailure receipt.
                                  # Preserve height-driven jump/drop/fly, not timer-driven flat flight.
                                  terrain_failed_seconds=0.0)
        result = stuck_recovery.plan_prop_transfer(terrain,start,result)
        if result.get('prop_platform_transfer'):
            result['prop_platform_transfer']['binding']=self._stuck_binding()[:2]
        summary = air_planner.describe(result)
        self._air_log_plan(now, dist=round(distance, 2), failed=round(failed, 2), **summary)
        if result.get("kind") not in ("jump", "drop", "fly"):
            return False
        return self._air_start(result, now)

    def _air_step_maybe(self, spot, who, now):
        """Do not turn idle/cold-planning time into an unproved air route.

        Real obstacle hops remain owned by FollowHopCoordinator and its bound
        groundFailure receipt. Height-driven air plans use _air_maybe. This
        compatibility entry only records why the old timeout rescue is deferred.
        """
        if self._air_motion is not None:
            return True
        if (self.motion is not None or self._air_block_reason(now) is not None
                or self.sitting is not None or self.holding or self.candle is not None
                or self.social is not None or self._active_running()):
            self._air_stall_since = None
            return False
        if _dist(self.position, spot) <= 1.2:
            self._air_stall_since = None
            return False
        last = getattr(self, "_air_last_moving", None)
        since = getattr(self, "_air_stall_since", None)
        if since is None:
            since = self._air_stall_since = now if last is None else min(now, last)
        if last is not None and last > since:
            since = self._air_stall_since = last
        stall = now - since
        if stall >= 0.5:
            logged = getattr(self, "_air_rescue_deferred_at", None)
            if logged is None or now - logged >= 30.0:
                self._air_rescue_deferred_at = now
                self.log("air_rescue_deferred", reason="ground_failure_proof_required",
                         stall=round(stall, 2), policy="bounded_ground_failure_only")
        return False

    def _air_after_land(self):
        """Hand movement back to the ground layer at the new position."""
        self.route, self.after_arrive = [], None
        self._terrain_motion = None
        self._terrain_last_position = self.position
        self._air_failed_at = None
        self._air_failed_target = None
        self._air_failed_last = None
        if getattr(self, "_terrain_adapter", None) is not None:
            self._terrain_invalidate()

    def _air_render(self, air, body, now):
        """Render one air frame; owns position/heading and the landing handoff."""
        from air_follow_runtime import frame as _frame_air_follow
        follow_result = _frame_air_follow(self, air, body, now)
        if follow_result is not None:
            return follow_result
        if not self._stuck_overlap_frame_guard(air, now):
            return air._stand(body), self.position, self.heading
        if air.arrived(now):
            body = air.render(body, now)
            pos, heading = air.b, air.final_heading
            self.position = pos
            if heading is not None:
                self.heading = heading
            self.motion = None
            self._air_motion = None
            self._air_phase = None
            self.log("air_landed", kind=air.kind,
                     at=[round(v, 2) for v in pos],
                     seconds=round(now - air.t0, 2))
            self._air_after_land()
            self._stuck_after_land(air, now)
            return body, pos, heading
        pos = air.pos(now)
        heading = air.heading if air.heading is not None else self.heading
        body = air.render(body, now)
        self.position = pos
        if heading is not None:
            self.heading = heading
        phase = air.phase(now)
        if phase != self._air_phase:
            self._air_phase = phase
            self.log("air_phase", phase=phase, kind=air.kind,
                     at=[round(v, 2) for v in pos])
        return body, pos, heading

    def _follow_tick(self):
        """跟随：目标离开当前站位/目的地超过一定距离就重新追过去；离得远小跑。"""
        from air_follow_runtime import maintenance as _maintain_air_follow
        _maintain_air_follow(self, time.monotonic())
        try:
            self._walk_record_tick()
        except Exception as exc:
            self.log("walk_record_error", error=str(exc)[:160])
        self._active_tick()
        active = getattr(self, "_active_offer", None)
        if active and (active["phase"] in ("queued", "waiting_accept") or
                       (active["phase"] == "accepted" and active.get("accepted_profile") in ("once", "loop"))):
            return
        try:
            self._candle_reply_tick()
        except Exception as exc:
            self.log("candle_reply_error", error=str(exc)[:160])
        self._relink_tick()
        self._candle_tick()
        self._social_tick()
        now = time.monotonic()
        if self._air_motion is not None and self._air_tick(now):
            return
        f = self.follow
        if f is None:
            return
        spot, who, d = self._spot_near(f["target"], f["distance"])
        # T23-1：开关打开时先问 planner；walk 继续走原逻辑。
        if self._air_enabled() and self._air_maybe(spot, who, d, now):
            return
        if self._air_enabled() and self._air_step_maybe(spot, who, now):
            return
        goal = self.motion.b if self.motion is not None else self.position
        slack = FOLLOW_SLACK
        if (getattr(getattr(self, "_terrain_adapter", None), "enabled", False)
                and not self._terrain_social_owns()):
            slack = getattr(self, "_terrain_follow_slack", slack)
        if ((spot[0] - goal[0]) ** 2 + (spot[2] - goal[2]) ** 2) ** 0.5 < slack:
            return
        speed = FOLLOW_RUN_SPEED if d > FOLLOW_RUN_DISTANCE else anim_pose.WALK_SPEED
        self.walk_to(spot, "follow", face=who, speed=speed)

    def _active_running(self):
        active = getattr(self, "_active_offer", None)
        return bool(active and active["phase"] in ("queued", "waiting_accept", "accepted"))

    def _active_publish(self, active, phase, reason=None):
        active["phase"] = phase
        self._active_outcome = {"request_id": active["request_id"], "phase": phase,
                                "action": active["entry"]["name"], "action_code": active["entry"]["code"],
                                "kind": active["entry"]["kind"], "reason": reason,
                                "room_generation": active["room_generation"], "updated_at": time.time(),
                                "requester": active["peer_uuid"], "target": active["peer_uuid"],
                                "initiation_live_verified": False}
        if (getattr(self, "_active_result", None) is None or
                self._active_result.get("request_id") == active["request_id"]):
            self._active_result = dict(self._active_outcome)
        self.log("active_offer_state", **{k:self._active_outcome[k] for k in
                                         ("request_id", "phase", "action", "action_code", "kind", "reason", "room_generation")})

    def active_offer_status(self):
        with self._active_lock:
            return dict(self._active_outcome) if self._active_outcome is not None else None

    def _active_binding(self, active, *, require_fresh=True):
        """Called under the active lock; acquire tracker second, never the reverse."""
        with self.tracker.lock:
            if (self.stop.is_set() or not self.tracker.ready or self.tracker._engine is not active["engine"]
                    or self.tracker.room_generation != active["room_generation"]
                    or self.tracker.epoch != active["epoch"]
                    or getattr(active["engine"], "_closed", False)
                    or self.tracker._own_social_binding_locked() != active["own_slot"]):
                return False
            for role in ("own", "peer"):
                slot = active[role + "_slot"]
                if (self.tracker.slots.get(slot) != active[role + "_uuid"]
                        or self.tracker._slot_versions.get(slot) != active[role + "_version"]):
                    return False
            if require_fresh:
                seen = self.tracker.position_observed.get(active["peer_slot"])
                if seen is None or not 0 <= time.monotonic() - seen[1] <= PLAYER_STATUS_MAX_AGE:
                    return False
            return True

    def _duet_distance_metadata(self, here, peer, age):
        """Anonymous diagnostic only; never feeds movement or accept decisions."""
        import math
        fields = {"valid": False, "distance_3d": None, "horizontal_distance": None,
                  "absolute_vertical_distance": None, "peer_position_age_seconds": None,
                  "peer_position_fresh": False, "above_native_2unit_limit": None}
        def finite(value):
            if type(value) not in (int, float):
                return False
            try:
                return math.isfinite(value)
            except OverflowError:
                return False
        if finite(age):
            fields["peer_position_age_seconds"] = round(age, 3)
            fields["peer_position_fresh"] = 0 <= age <= PLAYER_STATUS_MAX_AGE
        if not fields["peer_position_fresh"]:
            return fields
        if (type(here) not in (tuple, list) or type(peer) not in (tuple, list)
                or len(here) != 3 or len(peer) != 3 or not all(finite(v) for v in (*here, *peer))):
            return fields
        dx, dy, dz = (here[i] - peer[i] for i in range(3))
        distance, horizontal, vertical = math.hypot(dx, dy, dz), math.hypot(dx, dz), abs(dy)
        if not all(finite(v) for v in (distance, horizontal, vertical)):
            return fields
        fields.update(valid=True, distance_3d=round(distance, 3), horizontal_distance=round(horizontal, 3),
                      absolute_vertical_distance=round(vertical, 3), above_native_2unit_limit=distance > 2.0)
        return fields

    def _duet_peer_clip_kind(self, entry, animation, *, fresh=False):
        """Classify only a bound new Duet sample; never expose its raw key."""
        if (fresh is not True or not isinstance(entry, dict) or entry.get("code") not in (56, 57, 94, 95)
                or not isinstance(animation, dict) or type(animation.get("key")) is not int
                or not 0 <= animation["key"] <= 0xffffffffffffffff):
            return "unknown"
        key = animation["key"]
        if key == 0:
            return "zero"
        names = {pair[field] for pair in entry.get("accept_pairs", []) if isinstance(pair, dict)
                 for field in ("animation_a", "animation_b")
                 if type(pair.get(field)) is str and 0 < len(pair[field]) <= 96}
        if any(key == anim_pose.anim_key(name) for name in names):
            return "paired_clip"
        if any(key == anim_pose.anim_key(name + "Exit") for name in names):
            return "paired_exit"
        return "other"

    @_solo_action
    @upright_action
    def _request_active_offer(self, cmd):
        request = cmd.get("request_id")
        requester, target = cmd.get("requester"), cmd.get("target")
        preflight = {"action_code": None, "own_binding_ok": None,
                     "peer_in_current_roster": None, "peer_level_metadata_matches": None,
                     "peer_position_fresh": None}
        def record_preflight(reason):
            self.log("active_offer_preflight", reason=reason, **preflight)
        def reject(reason):
            record_preflight(reason)
            rejected = {"request_id": request if isinstance(request, str) else None,
                        "phase": "failed", "action": None, "reason": reason,
                        "updated_at": time.time(), "initiation_live_verified": False}
            self._active_result = rejected
            if not self._active_running():
                self._active_outcome = dict(rejected)
            return dict(rejected)
        with self._active_lock:
            if (not isinstance(request, str) or not 8 <= len(request) <= 80 or
                    any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in request)):
                return reject("invalid_request_id")
            if requester != target or not isinstance(requester, str) or requester == self.tracker.own_uuid:
                return reject("requester_target_mismatch")
            try:
                if str(uuidlib.UUID(requester)) != requester:
                    return reject("invalid_requester")
                entry = social_invite.resolve_action(cmd.get("action"))
            except (ValueError, TypeError, AttributeError):
                return reject("unsupported_action")
            preflight["action_code"] = entry["code"]
            if entry["code"] in (56, 57, 94, 95):
                preflight["duet_distance"] = None
            if not entry.get("supported"):
                return reject(entry.get("reason") or "unsupported_action")
            if request in self._active_seen:
                record_preflight("duplicate_request")
                return self.active_offer_status()
            soc = self.social
            other_hand = False
            if (cmd.get("travel_relead") is True and isinstance(soc, dict) and soc.get("kind") in ("hold", "carry")
                    and self.sitting is None and not self.holding):
                if soc.get("who") == requester:
                    # 发命令的人自己领着小光（他发的邀请）：小光带不动他过图，先松开再由小光伸手（他只需点接受）。
                    self._end_social("travel_relead")
                    self.log("travel_relead", request_id=request, kind=soc.get("kind"))
                elif soc.get("kind") == "hold":
                    # 牵着小光的是别人：游戏里两只手能各牵一个，不松开对方，用另一只手向发命令的人发邀请。
                    other_hand = True
                    self.log("travel_other_hand", request_id=request)
            if self._active_running() or (self.social is not None and not other_hand) or self.sitting is not None or self.holding or self.anim_lock.locked():
                # 新命令覆盖旧命令：上一条没完成的邀请直接作废，不再挡新请求
                if self._active_running():
                    self._active_end("superseded_by_new_command")
                    self.log("active_offer_superseded", request_id=request if isinstance(request, str) else None)
                if self._active_running() or (self.social is not None and not other_hand) or self.sitting is not None or self.holding or self.anim_lock.locked():
                    return reject("busy")
            if len(self._active_seen) >= 256:
                return reject("request_limit")
            with self.tracker.lock:
                if not self.tracker.ready or self.tracker._engine is None:
                    return reject("disconnected")
                own = self.tracker._own_social_binding_locked()
                preflight["own_binding_ok"] = own is not None
                if own is None:
                    return reject("own_binding_invalid")
                peer = next((s for s,u in self.tracker.slots.items() if u == target), None)
                preflight["peer_in_current_roster"] = peer is not None and peer != own
                if peer is None or own == peer:
                    return reject("target_not_in_room")
                level_slots = getattr(self.tracker._engine._match, "level_roster_ids", ())
                preflight["peer_level_metadata_matches"] = peer in level_slots
                now = time.monotonic()
                seen, position = self.tracker.position_observed.get(peer), self.tracker.origins.get(peer)
                preflight["peer_position_fresh"] = bool(seen is not None and position is not None
                                                        and 0 <= now - seen[1] <= PLAYER_STATUS_MAX_AGE)
                if not preflight["peer_position_fresh"]:
                    return reject("target_state_stale")
                here = self.motion.pos(now) if self.motion is not None else self.position
                if entry["code"] in (56, 57, 94, 95):
                    preflight["duet_distance"] = self._duet_distance_metadata(here, position[:3], now - seen[1])
                if _dist(here, position[:3]) > 6.0:
                    return reject("target_too_far")
                active = {"request_id": request, "entry": entry, "phase": "queued",
                          "engine": self.tracker._engine, "epoch": self.tracker.epoch,
                          "room_generation": self.tracker.room_generation,
                          "own_uuid": self.tracker.own_uuid, "peer_uuid": target,
                          "own_slot": own, "peer_slot": peer,
                          "own_version": self.tracker._slot_versions.get(own),
                          "peer_version": self.tracker._slot_versions.get(peer),
                          "event_seq": self.tracker.social_event_seq,
                          "created": now, "deadline": now + 20.0, "clear_pending": False,
                          "event": None, "accepted_at": None}
                if entry["code"] in (56, 57, 94, 95):
                    active["duet_initial_distance"] = dict(preflight["duet_distance"])
            try:
                previous = self.event.stamp() if self.event is not None else {
                    key:anim_pose.read(self.seed)[key] for key in ("gen", "key", "start")}
                active["social_invite_event"] = _SocialOfferEvent(entry["code"], previous, now)
                active["event"] = active["social_invite_event"]
            except Exception:
                return reject("actor_patch_failed")
            self._active_seen.add(request)
            self._active_offer = active
            self._active_result = None
            self.event = active["social_invite_event"]
            self.position, self.motion = tuple(here), None
            self.follow = self.after_arrive = None
            self.route = []
            record_preflight("queued")
            self._active_publish(active, "queued")
            self._set_override(self.base)
            return self.active_offer_status()

    def _cancel_active_offer(self, cmd):
        with self._active_lock:
            active = self._active_offer
            if (active is None or cmd.get("request_id") != active["request_id"]
                    or cmd.get("requester") != active["peer_uuid"]
                    or cmd.get("target", cmd.get("requester")) != active["peer_uuid"]):
                return False
            self._active_end("cancelled")
            return True

    def _active_end(self, reason, *, failed=False, end_source=None):
        with self._active_lock:
            active = self._active_offer
            if active is None or active["phase"] not in ("queued", "waiting_accept", "accepted"):
                return
            if active.get("event") is not None and self.event is active["event"]:
                self.event = anim_pose.ClearEvent(self.event.stamp())
                active["social_cleanup_event"] = self.event
            active["clear_pending"] = True
            self._active_publish(active, "failed" if failed else "ended", reason)
            if end_source is not None:
                self._active_outcome["end_source"] = end_source
                if self._active_result.get("request_id") == active["request_id"]:
                    self._active_result["end_source"] = end_source
                self.log("active_offer_end_observation", action_code=active["entry"]["code"],
                         reason=reason, end_source=end_source)
            # No guessed detach/teleport is sent; an accepted peer's native
            # relationship release is not claimed from local cleanup alone.
            if active.get("accepted_at") is not None and active.get("accepted_profile") in ("hold", "carrier"):
                self._active_outcome["peer_release_confirmed"] = False
                if self._active_result.get("request_id") == active["request_id"]:
                    self._active_result["peer_release_confirmed"] = False

    def _active_animation_tick(self, active, now):
        """A validated loop flag enters mode9, preserving the owned invitation."""
        if (active["phase"] == "accepted" and active.get("accepted_profile") == "loop"
                and active.get("event") is not None and not active.get("loop_started")
                and active.get("continuous_mode_verified") and self.event is active["event"]):
            active["event"] = _DanceLoop(active["event"], started=active["social_invite_event"].t0)
            active["loop_started"] = True
            self.event = active["event"]

    def _active_post(self, body):
        with self._active_lock:
            active = self._active_offer
            if active is None:
                return body
            if active["phase"] == "accepted":
                if not self._active_binding(active):
                    self._active_end("binding_lost", failed=True)
                else:
                    self._active_animation_tick(active, time.monotonic())
                    if self.event is active.get("event"):
                        profile = active.get("accepted_profile")
                        invite = active.get("social_invite_event")
                        if (profile in ("loop", "once") and isinstance(invite, _SocialOfferEvent)
                                and isinstance(self.event, _DanceLoop) and self.event.inner is invite):
                            # Acceptance may cross an old render: restore full owned
                            # identity atomically, without replay or new generation.
                            body = invite.apply(body, time.monotonic(), mode=9 if profile == "loop" else 7)
                        elif profile in ("hold", "carrier"):
                            body = self.event.post(body, time.monotonic())
                    body = social_invite.clear_offer(body)
            if active["phase"] in ("queued", "waiting_accept"):
                if time.monotonic() >= active["deadline"]:
                    self._active_end("invitation_expired", failed=True)
                elif not self._active_binding(active, require_fresh=True):
                    self._active_end("binding_lost", failed=True)
                else:
                    try:
                        invite = active.get("social_invite_event")
                        if not isinstance(invite, _SocialOfferEvent) or self.event is not invite:
                            raise ValueError("invitation event ownership changed")
                        body = invite.apply(body, time.monotonic(), mode=6)
                        out = social_invite.arm_actor(body, active["entry"]["code"], active["peer_slot"],
                                                      own_slot=active["own_slot"],
                                                      known_slots={active["own_slot"], active["peer_slot"]})
                    except Exception:
                        self._active_end("actor_patch_failed", failed=True)
                    else:
                        if not self._active_binding(active, require_fresh=True):
                            self._active_end("binding_lost", failed=True)
                            cleanup = active.get("social_cleanup_event")
                            if cleanup is not None and self.event is cleanup:
                                body = cleanup.post(body, time.monotonic())
                            return social_invite.clear_offer(body)
                        if active["phase"] == "queued":
                            with self.tracker.lock:
                                active["event_seq"] = self.tracker.social_event_seq
                            active["published_at"] = time.monotonic()
                            self._active_publish(active, "waiting_accept")
                        if not active.get("invite_logged"):
                            active["invite_logged"] = True
                            values = anim_pose.read(out)
                            name = "duet_invite_published" if active["entry"]["code"] in (56,57,94,95) else "social_invite_published"
                            self.log(name, action_code=active["entry"]["code"], own_mode=values["mode"],
                                     own_key_category="invite" if values["key"] == invite.identity.key else "other",
                                     offer_type=social_invite.inspect_offer(out)["type_code"])
                        return out
            if active["phase"] in ("ended", "failed"):
                try:
                    cleanup = active.get("social_cleanup_event")
                    if cleanup is not None and self.event is cleanup:
                        body = cleanup.post(body, time.monotonic())
                    body = social_invite.clear_offer(body)
                except Exception:
                    return body
                active["clear_pending"] = False
                return body
            if active["clear_pending"]:
                try:
                    body = social_invite.clear_offer(body)
                except Exception:
                    return body
                active["clear_pending"] = False
            return body

    def _active_tick(self):
        if not getattr(self, "_active_offer", None):
            return
        with self._active_lock:
            active = self._active_offer
            if not self._active_running():
                return
            now = time.monotonic()
            if active["phase"] in ("queued", "waiting_accept") and now >= active["deadline"]:
                return self._active_end("invitation_expired", failed=True)
            if not self._active_binding(active):
                return self._active_end("binding_lost", failed=True)
            with self.tracker.lock:
                events = [dict(e) for e in self.tracker.social_events if e["event_seq"] > active["event_seq"]]
            for event in events:
                active["event_seq"] = max(active["event_seq"], event["event_seq"])
                if (event["room_generation"] != active["room_generation"] or event["epoch"] != active["epoch"]
                        or event["source_slot"] not in (active["own_slot"], active["peer_slot"])
                        or event["observed_mono"] < active.get("published_at", active["created"])):
                    continue
                if event["event_kind"] == "release":
                    # Native carry prefix is carrier,carried. A native bool can
                    # permit carried-origin release; accept only a bound member's
                    # observed exit intent, not a claim of full RPC validation.
                    pair = (event["actor_slot"], event["target_slot"])
                    matches = (pair == (active["own_slot"], active["peer_slot"]) if event["kind"] == "carry"
                               else set(pair) == {active["own_slot"], active["peer_slot"]})
                    if active["phase"] == "accepted" and event["kind"] == {"carrier":"carry", "loop":"dance"}.get(active.get("accepted_profile"), active.get("accepted_profile")) and matches:
                        self._active_end("observed_peer_release", end_source="release_rpc")
                        self._active_outcome["release_observed"] = True
                        if self._active_result.get("request_id") == active["request_id"]:
                            self._active_result["release_observed"] = True
                        return
                    continue
                if (active["phase"] != "waiting_accept" or event["source_slot"] != active["peer_slot"]
                        or event["offerer_slot"] != active["own_slot"]
                        or event["acceptor_slot"] != active["peer_slot"]):
                    continue
                if not social_invite.validate_accept(active["entry"]["code"], event):
                    if not active.get("pattern_mismatch_logged"):
                        active["pattern_mismatch_logged"] = True
                        # The pure decoder already bounds these ASCII identifiers
                        # and numeric tail. Never log UUIDs or whole wire payloads.
                        self.log("active_accept_pattern_mismatch", action_code=active["entry"]["code"],
                                 animation_a=event["animation_a"], animation_b=event["animation_b"],
                                 tail=list(event["tail"]))
                    continue
                if not self._active_binding(active):
                    return self._active_end("binding_lost", failed=True)
                invite = active.get("social_invite_event")
                if (not isinstance(invite, _SocialOfferEvent) or self.event is not invite
                        or invite.published_frames < 1 or invite.t0 is None):
                    return self._active_end("actor_patch_failed", failed=True)
                profile = _accepted_social_profile(active["entry"], event["tail"])
                active["accepted_profile"] = profile
                active["accepted_at"], active["clear_pending"] = now, True
                active["attachment_accept_at"] = event["observed_mono"]
                active["continuous_mode_verified"] = profile == "loop"
                active["peer_animation_key"] = anim_pose.anim_key(event["animation_b"]) if event["animation_b"] else None
                if profile == "loop" and getattr(self, "_dance_trace_sessions", 0) < 3:
                    self._dance_trace_sessions = getattr(self, "_dance_trace_sessions", 0) + 1
                    active["trace_until"], active["trace_count"] = now + 6.0, 0
                if profile in ("loop", "once"):
                    wrapper = _DanceLoop if profile == "loop" else _OnceSocialEvent
                    active["event"] = wrapper(invite, started=invite.t0, previous=invite.stamp())
                    active["loop_started"] = profile == "loop"
                else:
                    # Native giver remains leader/carrier, not a receiver on its
                    # peer. Capture has a new zero-key identity; the transient7
                    # is not reproduced here, using the existing immediate idle writer.
                    active["event"] = anim_pose.ClearEvent(invite.stamp())
                self.event = active["event"]
                name = "duet_invite_accepted" if active["entry"]["code"] in (56,57,94,95) else "social_invite_accepted"
                self.log(name, action_code=active["entry"]["code"], phase="accepted", accepted_profile=profile,
                         invitation_wait_seconds=round(max(0.0, now-active["published_at"]), 3),
                         rpc_processing_delay_seconds=round(max(0.0, now-event["observed_mono"]), 3))
                self._set_override(self.base)
                self._active_publish(active, "accepted")
            if active["phase"] == "accepted":
                if self._active_attachment_tick(active, now):
                    return
                self._active_animation_tick(active, now)
                elapsed = now - active["accepted_at"]
                profile = active.get("accepted_profile")
                if profile == "once" and elapsed > 8.0 and not active.get("attachment_observed"):
                    self._active_end("once_start_unconfirmed", failed=True)
                elif profile == "once" and elapsed > SOCIAL_MAX_SECONDS:
                    # Bounded observation, not the native duration or completion.
                    self._active_end("once_observation_timeout", failed=True)
                elif profile == "loop" and elapsed > 8.0 and not active.get("attachment_observed"):
                    self._active_end("dance_start_unconfirmed", failed=True)
                elif elapsed > SOCIAL_MAX_SECONDS and profile not in ("loop", "once"):
                    self._active_end("local_timeout")

    def _active_attachment_tick(self, active, now):
        """End a previously observed relationship, never an unseen invitation."""
        profile = active.get("accepted_profile")
        if profile not in ("carrier", "hold", "loop", "once"):
            return False
        with self.tracker.lock:
            samples = [dict(e) for e in self.tracker._attachment_events
                       if e["slot"] == active["peer_slot"]
                       and e["sample_seq"] > active.get("attachment_seen", 0)]
        for sample in samples:
            if sample["sample_seq"] <= active.get("attachment_seen", 0):
                continue  # duplicate slot records in one frame are not two observations
            active["attachment_seen"] = sample["sample_seq"]
            observed_at = sample["observed_mono"]
            if (sample["room_generation"] != active["room_generation"]
                    or sample["uuid"] != active["peer_uuid"] or sample["version"] != active["peer_version"]
                    or observed_at < active["attachment_accept_at"]
                    or not 0 <= now - observed_at <= PLAYER_STATUS_MAX_AGE):
                continue
            source = active.get("attachment_source")
            if source is not None and sample["source_slot"] != source:
                continue
            value, state = sample["value"], None
            if value is not None:
                if profile == "carrier":
                    if value["carrier_slot"] == active["own_slot"]:
                        state = "attached"
                    elif value["carrier_slot"] == 0:
                        state = "detached"
                elif profile == "hold":
                    if value["leader_slot"] == active["own_slot"] and value["hold_state"] in (0, 1):
                        state = "attached"
                    elif value["leader_slot"] == 0 and value["hold_state"] == 0:
                        state = "detached"
                else:
                    animation = value.get("animation")
                    if animation is not None:
                        expected_mode = 9 if profile == "loop" else 7
                        state = "attached" if animation["mode"] == expected_mode else "detached"
            if state == "attached":
                if not active.get("attachment_observed"):
                    active["attachment_source"] = sample["source_slot"]
                    self.log("active_attachment_observed", action_code=active["entry"]["code"],
                             source="saved_actor_snapshot")
                active["attachment_observed"] = True
                active["detach_samples"], active["detach_since"] = 0, None
            elif state == "detached" and active.get("attachment_observed"):
                last = active.get("detach_last")
                if last is not None and observed_at - last > PLAYER_STATUS_MAX_AGE:
                    active["detach_samples"], active["detach_since"] = 0, None
                active["detach_samples"] = active.get("detach_samples", 0) + 1
                active["detach_last"] = observed_at
                if active.get("detach_since") is None:
                    active["detach_since"] = observed_at
                    self.log("active_attachment_detach_candidate", action_code=active["entry"]["code"],
                             source="saved_actor_snapshot")
                if active["detach_samples"] >= 2 and observed_at - active["detach_since"] >= 0.5:
                    if not self._active_binding(active):
                        return False
                    reason = ("observed_peer_dance_exit" if profile == "loop" else
                              "observed_peer_once_end" if profile == "once" else "observed_actor_detachment")
                    self._active_end(reason, end_source="actor_attachment_transition")
                    return True
            else:
                # Malformed/ambiguous observations interrupt consecutive evidence.
                active["detach_samples"], active["detach_since"] = 0, None
        return False

    def _active_dance_trace(self, body, now):
        """Bounded diagnostics: first three accepted dances, six seconds, <=24 paired rows each."""
        with self._active_lock:
            active = self._active_offer
            if active is not None and active.get("accepted_profile") == "loop":
                now = time.monotonic()
            if (not active or active.get("accepted_profile") != "loop" or "trace_until" not in active
                    or not 0 <= now - active["accepted_at"] < 6.0
                    or active.get("trace_count", 0) >= 24
                    or now - active.get("trace_at", -1e30) < 0.25
                    or not self._active_binding(active)):
                return
            active["trace_at"] = now
            active["trace_count"] += 1
            try:
                own = self.tracker._actor_social_fields(body).get("animation")
            except Exception:
                own = None
            peer, age = None, None
            with self.tracker.lock:
                samples = [e for e in self.tracker._attachment_events
                           if e["slot"] == active["peer_slot"]
                           and e["sample_seq"] > active.get("trace_peer_seen", 0)
                           and e["room_generation"] == active["room_generation"]
                           and e["uuid"] == active["peer_uuid"] and e["version"] == active["peer_version"]
                           and e["observed_mono"] >= active["attachment_accept_at"]
                           and 0 <= now - e["observed_mono"] <= PLAYER_STATUS_MAX_AGE]
                if samples:
                    sample = samples[-1]
                    active["trace_peer_seen"] = sample["sample_seq"]
                    peer = sample["value"].get("animation") if sample["value"] else None
                    age = round(now - sample["observed_mono"], 3)
            duet_diagnostics = {}
            if active["entry"]["code"] in (56, 57, 94, 95):
                try:
                    actual_origin = anim_pose.read_origin(body)
                except Exception:
                    actual_origin = None
                peer_origin, position_age, duet_bound = None, None, False
                with self.tracker.lock:
                    # Recheck after decoding the outgoing body, in case the bound
                    # slot/incarnation changed while diagnostic work was running.
                    duet_bound = (not self.stop.is_set() and self.tracker.ready
                                  and self.tracker._engine is active["engine"]
                                  and self.tracker.room_generation == active["room_generation"]
                                  and self.tracker.epoch == active["epoch"]
                                  and not getattr(active["engine"], "_closed", False)
                                  and self.tracker._own_social_binding_locked() == active["own_slot"]
                                  and all(self.tracker.slots.get(active[role + "_slot"]) == active[role + "_uuid"]
                                          and self.tracker._slot_versions.get(active[role + "_slot"]) == active[role + "_version"]
                                          for role in ("own", "peer")))
                    if duet_bound:
                        current_origin = self.tracker.origins.get(active["peer_slot"])
                        current_seen = self.tracker.position_observed.get(active["peer_slot"])
                        if isinstance(current_origin, (tuple, list)) and len(current_origin) >= 3:
                            peer_origin = current_origin[:3]
                        if isinstance(current_seen, (tuple, list)) and len(current_seen) >= 2:
                            try:
                                position_age = now - current_seen[1]
                            except (TypeError, ValueError):
                                pass
                duet_distance = self._duet_distance_metadata(actual_origin, peer_origin, position_age)
                duet_diagnostics = {"duet_distance": duet_distance,
                                    "duet_initial_distance": dict(active.get("duet_initial_distance") or {}),
                                    "peer_clip_kind": self._duet_peer_clip_kind(active["entry"], peer,
                                        fresh=duet_bound and bool(samples) and duet_distance["peer_position_fresh"])}
            giver = anim_pose.anim_key(active["entry"]["give_animation"]) if active["entry"].get("give_animation") else None
            taker = active.get("peer_animation_key")
            def project(value, partner):
                if value is None:
                    return None
                def key_label(key):
                    return "zero" if key == 0 else "giver" if key == giver else "taker" if key == taker else "other"
                return {"mode":value["mode"], "code90":value["code90"],
                        "gen":value["gen"], "key":key_label(value["key"]), "start":value["start"],
                        "history":{"gen":value["h_gen"], "key":key_label(value["h_key"]), "start":value["h_start"]},
                        "once_present":value["once_present"],
                        "once_target_is_partner":value["once_target"] == partner if value["once_present"] else None}
            self.log("active_dance_trace", action_code=active["entry"]["code"],
                     elapsed=round(now-active["accepted_at"],3), phase=active["phase"],
                     own=project(own, active["peer_slot"]), peer=project(peer, active["own_slot"]),
                     peer_new_saved_frame=bool(samples), peer_age_seconds=age,
                     command=active.get("trace_command"), **duet_diagnostics)

    def _social_note_published(self, body):
        """Commit history after final Actor construction, not network send/ACK."""
        with self._active_lock:
            active = self._active_offer
            if not active or active["phase"] not in ("waiting_accept", "accepted"):
                return
            invite = active.get("social_invite_event")
            owns = (self.event is invite or isinstance(self.event, _DanceLoop)
                    and self.event is active.get("event") and self.event.inner is invite)
            if not isinstance(invite, _SocialOfferEvent) or not owns or not self._active_binding(active):
                return
            try:
                invite.note_published(body)
            except Exception:
                pass

    def _active_note_command(self, cmd):
        with self._active_lock:
            active = self._active_offer
            if (not active or "trace_until" not in active or time.monotonic() >= active["trace_until"]):
                return
            # Never log free-form source/pose strings, targets, animation names or chat.
            poses = {"active_offer", "cancel_active_offer", "idle", "stand", "stop", "walk", "explore",
                     "face", "attitude", "voice", "anim", "accept_offer", "release", "sit", "candle", "follow"}
            sources = {"wander", "ai", "chat", "manual", "control"}
            pose, source = cmd.get("pose"), cmd.get("src")
            active["trace_command"] = {"pose":pose if type(pose) is str and pose in poses else "other",
                "src":source if type(source) is str and source in sources else "unspecified_or_other", "at":time.time()}

    @_solo_action
    @upright_action
    def _accept_offer(self, who, offer_type):
        """接受双人互动：确认对方此刻仍在邀请小光，进入对应状态，再发 SocialAcceptOfferRpc。
        once（拥抱类）：挪到对方面前，“互动对象”结构保持约 1 秒；hold（牵手）/ carry（背背、公主抱）：一直保持到结束。"""
        if self._active_running():
            self._active_end("passive_offer")
        with self.tracker.lock:
            slots = dict(self.tracker.slots)
            offers = dict(self.tracker.offers)
            there = self.tracker.origins.get(next((s for s, u in slots.items() if u == who), None))
        own = next((s for s, u in slots.items() if u == self.tracker.own_uuid), None)
        theirs = next((s for s, u in slots.items() if u == who), None)
        live = offers.get(theirs)
        if own is None or theirs is None or LAST_EPOCH is None:
            raise LookupError("接受邀请：房间名单里找不到双方")
        if not live or live[1] != own:
            raise LookupError("接受邀请：对方已经不在邀请小光了")
        entry = social_invite.resolve_action(live[0])
        if not entry.get("supported") or entry.get("gift_or_unlock"):
            raise LookupError("接受邀请：该互动未实现")
        profile = _accepted_social_profile(entry, entry["accept_pairs"][0]["tail"])
        legacy_kind = social_pose.kind_of(live[0])
        kind = {"loop":"dance", "carrier":"carry"}.get(profile, profile)
        if self.social is not None:
            self._end_social("new_offer")
        self.motion = self.after_arrive = self.follow = self.candle = None
        self.holding = False
        here = self.position
        if there is not None:
            self.heading = anim_pose.heading_to(here, there[:3])
            if legacy_kind == "once":          # 保留现有接受方对齐，不推断新的位移
                d = max(((here[0] - there[0]) ** 2 + (here[2] - there[2]) ** 2) ** 0.5, 1e-6)
                if d > ONCE_DISTANCE:
                    k = ONCE_DISTANCE / d
                    self.position = (there[0] + (here[0] - there[0]) * k, here[1], there[2] + (here[2] - there[2]) * k)
        social = social_pose.build(self.base, theirs, "once" if kind == "dance" else kind)
        if kind in ("dance", "once"):
            with self.tracker.lock:
                if (self.tracker.slots.get(own) != self.tracker.own_uuid
                        or self.tracker.slots.get(theirs) != who):
                    raise LookupError("接受舞蹈：本次房间玩家绑定已变化")
                self.social = {"kind": kind, "who": who, "slot": theirs, "own": own, "since": time.monotonic(),
                               "exit": (social_accept.anims_for(live[0])[1] + "Exit") if kind == "dance" and entry.get("has_loop_exit_anim") else None,
                               "action_code": live[0], "accepted_profile": profile,
                               "engine": self.tracker._engine, "epoch": self.tracker.epoch,
                               "room_generation": self.tracker.room_generation,
                               "own_slot": own, "peer_slot": theirs,
                               "own_uuid": self.tracker.own_uuid, "peer_uuid": who,
                               "own_version": self.tracker._slot_versions.get(own),
                               "peer_version": self.tracker._slot_versions.get(theirs),
                               "attachment_seen": max((e["sample_seq"] for e in self.tracker._attachment_events), default=0),
                               "attachment_observed": False, "detach_samples": 0, "detach_since": None}
        if kind in ("once", "dance"):
            prev = self.event.stamp() if self.event is not None else {k: anim_pose.read(self.seed)[k] for k in ("gen", "key", "start")}
            # Human accepting avatars already increment their zero-key event
            # before the paired RPC; they do not stream its wire-B clip as a new
            # ordinary animation key. The loop below preserves this zero event.
            self.event = anim_pose.AnimEvent(0, prev, time.monotonic(), social_pose.HOLD_SECONDS)
            if kind == "once":
                # The controller expires only the temporary relationship marker;
                # the wrapper's mode7 persists until fresh observation/cleanup.
                self.event = _OnceSocialEvent(self.event, started=self.social["since"], previous=self.event.stamp())
                self.social["event"] = self.event
                self.social["marker_expired"] = False
        else:
            self.social = {"kind": kind, "who": who, "slot": theirs, "own": own, "since": time.monotonic()}
            if kind == "hold":
                self.hold_prev = None              # 牵手：_post 每帧贴着对方，不再用 follow 追
            self.last_social = {"who": who, "kind": kind, "since": time.monotonic(), "ended": None}
        if kind == "dance":
            # Native paired-loop acceptance sets mode9 immediately. A mode7
            # prelude after the offer clears can make the peer clean both sides.
            self._dance_loop(self.social)
        else:
            self._set_override(social)
        game = social_accept.send_accept(LAST_EPOCH, own, theirs, live[0])
        self.state = {"pose": "social", "kind": kind, "with": who[:8], "type": live[0]}
        self.log("social_accept_sent", target=who[:8], type=live[0], kind=kind, own=hex(own), offerer=hex(theirs),
                 game=game.hex())

    def _passive_dance_tick(self, soc, now):
        """Only fresh saved frames for this accepted dance can confirm/exit it."""
        if self.social is not soc:
            return
        if not self._active_binding(soc):
            return self._end_social("binding_lost")
        with self.tracker.lock:
            released = any(t >= soc["since"] and k == "dance" and a == soc["slot"] and b == soc["own"]
                           for t, k, a, b in self.tracker.releases)
            samples = [dict(e) for e in self.tracker._attachment_events
                       if e["slot"] == soc["peer_slot"] and e["sample_seq"] > soc["attachment_seen"]]
        if released:
            return self._end_social("partner_pressed_end")
        for sample in samples:
            if sample["sample_seq"] <= soc["attachment_seen"]:
                continue
            soc["attachment_seen"] = sample["sample_seq"]
            observed_at = sample["observed_mono"]
            if (sample["room_generation"] != soc["room_generation"]
                    or sample["uuid"] != soc["peer_uuid"] or sample["version"] != soc["peer_version"]
                    or observed_at < soc["since"] or not 0 <= now - observed_at <= PLAYER_STATUS_MAX_AGE):
                continue
            source = soc.get("attachment_source")
            if source is not None and sample["source_slot"] != source:
                continue
            value = sample["value"]
            animation = value.get("animation") if value is not None else None
            if animation is not None and animation["mode"] == (9 if soc["kind"] == "dance" else 7):
                if not soc["attachment_observed"]:
                    self.log("passive_dance_state", action_code=soc["action_code"], phase="observed", sample_count=1)
                soc["attachment_observed"] = True
                soc["attachment_source"] = sample["source_slot"]
                soc["detach_samples"], soc["detach_since"] = 0, None
            elif animation is not None and soc["attachment_observed"]:
                last = soc.get("detach_last")
                if last is not None and observed_at - last > PLAYER_STATUS_MAX_AGE:
                    soc["detach_samples"], soc["detach_since"] = 0, None
                soc["detach_samples"] += 1
                soc["detach_last"] = observed_at
                if soc["detach_since"] is None:
                    soc["detach_since"] = observed_at
                    self.log("passive_dance_state", action_code=soc["action_code"], phase="exit_candidate",
                             sample_count=soc["detach_samples"])
                if soc["detach_samples"] >= 2 and observed_at - soc["detach_since"] >= .5:
                    if self._active_binding(soc):
                        return self._end_social("observed_peer_dance_exit" if soc["kind"] == "dance" else "observed_peer_once_end")
                    return self._end_social("binding_lost")
            else:
                soc["detach_samples"], soc["detach_since"] = 0, None
        if now - soc["since"] > 8.0 and not soc["attachment_observed"]:
            # Local failure to confirm startup, never a claim of peer cancellation.
            return self._end_social("dance_start_unconfirmed" if soc["kind"] == "dance" else "once_start_unconfirmed")
        if soc["kind"] == "once" and now - soc["since"] > SOCIAL_MAX_SECONDS:
            return self._end_social("once_observation_timeout")

    def _end_once(self, expected=None):
        """Inert compatibility callback: stale timers must never write global state.

        Marker TTL is handled by the existing controller's _social_tick instead.
        """
        return

    def _social_tick(self):
        """持续互动的兜底结束：超时、对方离开房间、牵手时对方跑远（传送/飞走）。"""
        soc = self.social
        if soc is None:
            return
        if soc["kind"] in ("dance", "once"):
            now = time.monotonic()
            if (soc["kind"] == "once" and not soc.get("marker_expired")
                    and now-soc["since"] >= social_pose.HOLD_SECONDS
                    and self.social is soc and self.event is soc.get("event")
                    and self._active_binding(soc)):
                # Same controller thread as _apply/_accept_offer: no detached
                # Timer can overwrite a newly accepted relation's template.
                self._set_override(self.base)
                soc["marker_expired"] = True
                self.log("social_hold_end")
            return self._passive_dance_tick(soc, now)
        o = self.tracker.origins.get(soc["slot"])
        if soc["slot"] not in self.tracker.slots or o is None:
            return self._end_social("partner_left")
        if time.monotonic() - soc["since"] > SOCIAL_MAX_SECONDS:
            return self._end_social("timeout")
        with self.tracker.lock:
            released = any(t >= soc["since"] and k == soc["kind"] and a == soc["slot"] and b == soc["own"]
                           for t, k, a, b in self.tracker.releases)
        if released:
            return self._end_social("partner_pressed_end")
        if soc["kind"] == "carry" and soc["slot"] in self.tracker.yaws:
            self.heading = anim_pose.yaw_heading(self.tracker.yaws[soc["slot"]])
        if soc["kind"] == "hold":
            here = self.motion.pos(time.monotonic()) if self.motion else self.position
            if ((here[0] - o[0]) ** 2 + (here[2] - o[2]) ** 2) ** 0.5 > SOCIAL_LOST_DISTANCE:
                return self._end_social("partner_far")

    def _end_social(self, why="release"):
        """结束牵手/背背：撤掉关系字段。背背下来时站到背人方旁边，并像真机一样发一次 AvatarTeleportRpc。"""
        soc, self.social = self.social, None
        if soc is None:
            return
        if getattr(self, "last_social", None) and self.last_social.get("who") == soc["who"]:
            self.last_social["ended"] = time.monotonic()
        self.follow = None
        if soc["kind"] == "carry":
            o = self.tracker.origins.get(soc["slot"])
            x, y, z = o[:3] if o is not None else self.position
            h = self.heading if self.heading is not None else 0.0
            import math
            fx, fz = math.sin(math.radians(h)), math.cos(math.radians(h))
            spot = (x - fx * 0.6, y, z - fz * 0.6)     # 落在背人方身后一点
            g = self.terrain_ground(spot[0], spot[2], y)
            if g is not None:
                spot = (spot[0], g, spot[2])
            self.position = spot
            if LAST_EPOCH is not None:
                social_accept.send_teleport(LAST_EPOCH, soc["own"], spot, (fx, 0.0, fz))
        if isinstance(self.event, _DanceLoop) and self.event is soc.get("event"):
            if soc.get("exit"):
                self.event = self.event.inner
            else:
                self.event = anim_pose.ClearEvent(self.event.stamp())
        self._set_override(self.base)
        self.state = {"pose": "idle"}
        # 2026-11-07 T20：social 结束补一次当前位置的高度校正。牵手/背/抱到达时
        # 地形 worker 可能还没 ready；这里优先用落点 placement 的 wire y，ready
        # 后再按真实地形选层，并且只改 y、不改 x/z。
        try:
            self._reground_position("social_end")
        except Exception as exc:
            self.log("terrain_reground_social_failed", error=str(exc)[:120])
        if soc["kind"] == "dance" and soc.get("exit"):
            try:
                self.play(soc["exit"], 1.5)
            except Exception as exc:
                self.log("social_exit_anim_failed", error=str(exc)[:120])
        self.log("social_end", kind=soc["kind"], why=why, held=round(time.monotonic() - soc["since"], 1))

    def _dance_loop(self, expected=None):
        """双人舞：拥抱式结构之后进入 mode 9 循环（真机接受方 0.3 秒后切过去），一直到结束。"""
        if self.social is None or self.social["kind"] != "dance":
            return
        if expected is not None and self.social is not expected:
            return
        if isinstance(self.event, _DanceLoop):
            return
        previous = self.event.stamp() if self.event is not None else {
            k:anim_pose.read(self.seed)[k] for k in ("gen", "key", "start")}
        self.event = _DanceLoop(self.event, started=self.social["since"], previous=previous)
        self.social["event"] = self.event
        self._set_override(self.base)             # 拥抱式结构同时撤掉（真机同一帧）
        self.log("social_dance_loop")

    def _after(self, cmd):
        try:
            self._apply(cmd)
        except LookupError as exc:
            # 走到座位旁时座位被占/记录没了：_sit_now 重新挑也失败。
            self.log("pose_error", error=str(exc)[:200], step="after_arrive")
            if isinstance(cmd, dict) and cmd.get("pose") == "_sit_now":
                self._sit_failed("seat_taken", seat=cmd.get("seat"), detail=str(exc)[:120])
        except Exception as exc:
            self.log("pose_error", error=str(exc)[:200], step="after_arrive")
            if isinstance(cmd, dict) and cmd.get("pose") == "_sit_now":
                self._sit_failed("sit_error", seat=cmd.get("seat"), detail=str(exc)[:120])

    def _poll(self):
        if not self.path.exists():
            return
        mtime = self.path.stat().st_mtime
        if mtime == self.last_mtime:
            return
        self.last_mtime = mtime
        self._apply(json.loads(self.path.read_text(encoding="utf-8")))

    @_home_seat_action
    @_solo_action
    @seat_command
    def _apply(self, cmd, *, _home_command=None):
        if (cmd.get("pose") == "sit" and "home_seat_context" in cmd
                and not self._home_wander_context_matches(cmd["home_seat_context"])):
            self._reject_home_wander_sit("执行前房间已变化，放弃自动坐下")
            return
        if getattr(self, "_active_offer", None) is not None:
            self._active_note_command(cmd)
        pose = cmd.get("pose")
        self._once_command(cmd)
        if cmd.get("src") != "wander":
            # 自己逛是“没人管”的一部分，不算外部命令；否则逛起来永远不会偶尔叫。
            self._last_command_at = time.monotonic()
        if pose not in ("explore",):
            self._explore_active = False          # 任何别的命令都结束“自己在逛”
        if pose == "shout":
            # 叫声只走 Actor 字段，不打断走路/跟随路线；坐着、社交、空中会跳过。
            return self._shout_command(cmd)
        if pose == "voice":
            # T23-7：换叫声只改装扮块 voice，不打断走路/坐下/举烛；计数 0.45 s 后另发。
            return self._set_voice(cmd.get("value"))
        # T23-8：叫声/换声不打断 sit；其余命令都算新命令，1.5 s 直走兜底
        # 不能把已经被别的动作取代的旧 sit 又捡起来走。
        self._command_seq = getattr(self, "_command_seq", 0) + 1
        if pose == "active_offer":
            return self._request_active_offer(cmd)
        if pose == "cancel_active_offer":
            return self._cancel_active_offer(cmd)
        active = getattr(self, "_active_offer", None)
        if self._active_running() and not (active["phase"] == "accepted" and
                active.get("accepted_profile") in ("hold", "carrier") and
                pose in ("walk", "explore", "face", "attitude", "anim")):
            self._active_end("other_command")
        if pose not in ("anim", "explore"):
            self.route = []                            # 别的命令打断正在走的路线
        own_end = (pose == "idle" and self.holding) or (pose == "stand" and self.sitting is not None)
        if cmd.get("src") == "wander" and (getattr(self, "candle_reply", None) or self.social or self.follow or (
                not own_end and (self.holding or self.sitting is not None))):   # 自己逛只能放下自己的蜡烛/从自己的座位站起
            self.log("wander_cmd_ignored", pose=pose, why="正忙（回应举烛/牵手/坐着/跟随）")
            return
        if pose == "_sit_now":                 # 走到座位旁后的内部步骤
            return self._sit_now(cmd["seat"], cmd["info"], home_request=cmd.get("home_request"))
        if self.sitting is not None and pose not in ("anim", "sit"):
            self._stand()                      # 坐着时要走/举烛/停下等：先站起来
            if pose in ("stand", "stop", "idle"):
                return
        if pose == "stand":
            return
        if pose == "accept_offer":
            return self._accept_offer(cmd["from"], int(cmd["type"]))
        if pose == "release":
            return self._end_social("release")
        if self.social is not None and pose not in ("anim",):
            self._end_social("other_command")
        if pose == "sit" and (self._is_home_map() or "home_seat_context" in cmd):
            return self._apply_home_sit(cmd, _home_command)
        if pose == "sit":
            if self.sitting is not None:
                return
            air = getattr(self, "_air_motion", None)
            if air is not None and self.motion is air:
                return self._prop_seat_queue_after_air(cmd)
            here = self.motion.pos(time.monotonic()) if self.motion else self.position
            near_slot, near = (self.tracker.resolve(cmd["near"]) if cmd.get("near")
                               else (None, here))
            # T23-8：座位走路的参照对象固定成“喊她坐下的人”（near），不再让
            # 地形层用会随新鲜度切换的 _idle_target；找不到 UUID 时留空，由地形
            # 层沿用旧兜底，不改变校验强度。
            ref = None
            if near_slot is not None:
                try:
                    with self.tracker.lock:
                        ref = self.tracker.slots.get(near_slot)
                except Exception:
                    ref = None
            try:
                seat, info = self._pick_seat(near)
            except LookupError as exc:
                # 找不到空座位：状态里写 sit_failed，聊天侧不能再说“坐下了”。
                if self._is_home_map():
                    self._home_sit_failed("no_seat", None, str(exc)[:120])
                else:
                    self._sit_failed("no_seat", detail=str(exc)[:120])
                return
            if self._is_home_map() and not self._home_prepare_sit(seat, info, context=cmd.get("home_seat_context")):
                return
            if self._prop_seat_try_interact(seat, info, here, takeover=True):
                return
            (x, y, z), _yaw, src = info
            if src == "guess":
                # 只知道同桌别人的位置：走到那人身边离 0.8 米（朝小光这侧）站好，坐下时客户端按座位号吸附
                d = max(((here[0] - x) ** 2 + (here[2] - z) ** 2) ** 0.5, 1e-6)
                x, z = x + (here[0] - x) / d * 0.8, z + (here[2] - z) / d * 0.8
            # 走到座位旁的高度是“站地面”，不是座位面：参考座位 y - SEAT_HEIGHT 查真实地形。
            g = self.terrain_ground(x, z, y - SEAT_HEIGHT)
            stand = (x, g if g is not None else y - SEAT_HEIGHT, z)
            try:
                stand = self._prop_seat_approach(seat, info, stand, here)
            except (ValueError, TypeError, KeyError) as exc:
                self._sit_failed("unsafe_approach", seat=seat, stop=True, detail=str(exc)[:120])
                return
            self.follow, self.holding, self.candle = None, False, None
            self.after_arrive = {"pose": "_sit_now", "seat": seat, "info": info}
            if self._is_home_map():
                self.after_arrive["home_request"] = self._home_sit_pending["request"]
            self._set_override(self.base)
            self._walk_ref = ref
            started = self.walk_to(stand, "seat")
            if started is False:
                if self._is_home_map():
                    self._home_sit_pending = None
                self._sit_failed("walk_rejected", seat=seat)
                return
            # target/info 留着给 1.5 s 直走兜底：地形层不启动也能自己走到座位旁。
            self._sit_pending = {"since": time.monotonic(), "seat": seat, "near": tuple(near),
                                 "target": tuple(stand), "info": info, "ref": ref,
                                 "command_seq": self._command_seq, "direct_checked": False}
            self._last_sit_failure = None
            if self.motion is not None:
                self.state = {"pose": "walk", "to_seat": seat}
            self.log("seat_pick", seat=seat, source=src, near=[round(v, 2) for v in near])
            return
        self.after_arrive = None if cmd.get("pose") != "approach" else self.after_arrive
        if cmd.get("pose") not in ("follow", "anim"):
            self.follow = None                 # 除了做表情，别的命令都结束跟随
        if cmd.get("pose") != "anim":
            self.holding, self.candle = False, None   # 举烛状态：下面 candle 分支再置回
        if cmd.get("pose") == "follow":
            self.follow = {"target": cmd["target"], "distance": float(cmd.get("distance", 1.5))}
            self._set_override(self.base)
            self.state = {"pose": "follow", "target": str(cmd["target"])[:8]}
            self.log("pose_set", **self.state)
            self._follow_tick()
            return
        if cmd.get("pose") == "stop":          # 停在原地（不回出生点）
            if self.motion is not None:
                self.walk_to(self.motion.pos(time.monotonic()), "stop")
            self.state = {"pose": "idle"}
            self.log("pose_set", pose="stop")
            return
        if cmd.get("pose") == "approach":
            # 走到某人身边，到了再执行 then（如 {"pose": "candle", "target": 同一人}）
            self.after_arrive = cmd.get("then")
            cmd = {"pose": "walk", "target": cmd["target"], "distance": cmd.get("distance", 0.8)}
            self._set_override(self.base)      # 走之前先放下蜡烛之类的姿态
        if cmd.get("pose") == "candle":
            slot, origin = self.tracker.resolve(cmd["target"])
            self._aim_candle(cmd["target"], slot, origin)
            self.holding = True
            self.state = {"pose": "candle", "target_slot": hex(slot), "target_origin": [round(v, 3) for v in origin]}
        elif cmd.get("pose") == "anim":
            self.play(cmd["name"], cmd.get("seconds", TAKE_CANDLE_SECONDS))
            return
        elif cmd.get("pose") == "attitude":
            return self._set_attitude(int(cmd.get("value", 0)))
        elif cmd.get("pose") == "outfit":
            return self._set_outfit(cmd.get("outfit") or {}, cmd.get("changes"))
        elif cmd.get("pose") == "explore":
            return self._explore(cmd.get("toward"))
        elif cmd.get("pose") == "walk":
            if "to" in cmd:
                x, y, z = (float(v) for v in cmd["to"])
                if cmd.get("ground"):                  # y 是别的玩家的下行坐标（地面）
                    g = self.terrain_ground(x, z, y)   # 真实地形；查不到才用他的 y
                    if g is not None:
                        y = g
                    here = self.motion.pos(time.monotonic()) if self.motion else self.position
                    if y - here[1] > WALK_MAX_CLIMB:
                        self.log("walk_too_high", to=[round(x, 2), round(y, 2), round(z, 2)], here_y=round(here[1], 2))
                        return
                self.walk_to((x, y, z), "point")
            elif "target" in cmd:
                # 走到某人身边：停在对方与自己连线上、离对方 distance 的位置
                spot, who, _d = self._spot_near(cmd["target"], float(cmd.get("distance", 0.8)))
                self.walk_to(spot, "target", face=who)
            else:
                self.walk_to(self.home, "home")
            if self.motion is not None:
                self.state = {"pose": "walk"}
            return
        elif cmd.get("pose") == "face":
            # 原地转身：朝某人，或直接给角度（度，0 = +z，90 = +x）
            if "target" in cmd:
                _slot, origin = self.tracker.resolve(cmd["target"])
                self.heading = anim_pose.heading_to(self.position, origin)
            else:
                self.heading = float(cmd["heading"])
            self._set_override(map_long_engine.POSE_OVERRIDE.get("seed") or self.base)
            self.state = {"pose": "face", "heading": round(self.heading)}
        else:
            self._set_override(self.base)
            self.state = {"pose": "idle"}
        self.log("pose_set", **self.state)

    def _write_state(self):
        home_seat_status = None
        if self._is_home_map():
            room, home_seat_status = self._home_seat_public_state()
            seats_here = home_seat_status["count"]
        else:
            try:
                seats_here = len(self._seat_candidates()[0])
            except Exception:
                seats_here = 0
            room = self.tracker.snapshot()
        state = dict(self.state, room=room, offers=self.tracker.offers_to_me(), seats_here=seats_here,
                     ts=time.strftime("%H:%M:%S"), generated_at=room["generated_at"])
        if home_seat_status is not None:
            state["home_seat_status"] = home_seat_status
            failure = getattr(self, "_last_sit_failure", None)
            if failure is not None and self.sitting is None:
                state["sit_failed"] = dict(failure)
        # T23-4：pose/sitting 必须和真实 sitting 字段一致，聊天进程只看这里。
        # 坐着时 state["pose"] 永远是 "sit"；已经站起来时绝不会残留 "sit"。
        if self.sitting is not None:
            state["pose"] = "sit"
            state["seat"] = self.sitting
        elif state.get("pose") == "sit":
            state["pose"] = "idle"
        state["sitting"] = self.sitting is not None
        state["sitting_seat"] = self.sitting
        pending = getattr(self, "_sit_pending", None)
        if pending is not None:
            state["sit_pending"] = {"seat": pending.get("seat"),
                                    "elapsed": round(time.monotonic() - pending.get("since", time.monotonic()), 1)}
        failure = getattr(self, "_last_sit_failure", None)
        if failure is not None and self.sitting is None:
            state["sit_failed"] = dict(failure)
        if getattr(self, "_active_outcome", None) is not None:
            state["active_offer"] = self.active_offer_status()
        if getattr(self, "_active_result", None) is not None:
            with self._active_lock:
                state["active_offer_result"] = dict(self._active_result)
        tmp = self.path.with_suffix(".state.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path.with_suffix(".state.json"))


def _walkmap_cls():
    import walkmap

    class SmoothWalkMap(walkmap.WalkMap):
        SMOOTH_HEIGHT_TOLERANCE = 0.35     # 真实地面采样有噪声：0.05 时路线几乎不合并、每米一拐（看着像之字走）
    return SmoothWalkMap


class _LazyWalkMap:
    def __getattr__(self, name):
        return getattr(_walkmap_cls(), name)


_SmoothWalkMap = _LazyWalkMap()


OUTFIT_GROUP = "0x21df444|1"         # Actor 装扮块（同服务器 get_outfit：各部位，再 height scale voice attitude refreshVersion seed）
ATTITUDE_FROM_END = 2                # 站姿 = 装扮块倒数第 3 个 token（bits4）；倒数第 4 个是 voice（2026-11-01 用 seed 定位）
VOICE_FROM_END = 3                   # T23-7：叫声 = 装扮块倒数第 4 个 token（bits4，0..15）
VOICE_COUNTER_DELAY = 0.45           # T23-7：真人切换时 voice 先变、外观计数约 0.37–0.54 s 后才 +1，这里取 0.45 s
# T23-3：装扮块里 10 个部位的固定顺序（每条记录 = raw4 id + tex/pat/mask 三个 bits2 + raw4 字符串长度 + raw12 染料串）。
# 2026-11-07 用 analysis3/xg_actor_DuskStart.bin + analysis3/xg_get_outfit.json 逐 token 对齐：
# body 3691343236 / wing 2245279351 / hair 1229053584 / mask 3663390882 / neck 3800884691 /
# feet 1532093990 / horn 3680499229 / face 3599986885 / prop 2035109393 / hat 1470462579，与服务器值一一相等。
# get_outfit 里的 legs / arms 不在 Actor 装扮块里（块固定 10 条记录），只改服务器，见 REPORT。
OUTFIT_ACTOR_SLOTS = ("body", "wing", "hair", "mask", "neck", "feet", "horn", "face", "prop", "hat")
OUTFIT_RECORD_TOKENS = 6             # 每条部位记录占 6 个 token
OUTFIT_TAIL_TOKENS = 6               # 块尾：height bits8、scale bits8、voice bits4、attitude bits4、refreshVersion raw4、seed raw2
OUTFIT_GROUP_TOKENS = len(OUTFIT_ACTOR_SLOTS) * OUTFIT_RECORD_TOKENS + OUTFIT_TAIL_TOKENS   # 66


OUTFIT_COUNTER = "0x21df464|1"       # 装扮块外的 raw4：外观变了就 +1（哥哥换站姿时 6→7 与 attitude 同一帧）
CONTROL_DIR = xg_paths.CONTROL   # control/attitude、control/outfit.json、control/outfit_counter


def _actor_obs(body):
    from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
    return inspect_actor_wire(body, verified_profile_sha256=anim_pose.SHA, check_zero_padding=False)


def outfit_ids_of(body):
    """读 Actor 装扮块里 10 个部位的 id（顺序同 OUTFIT_ACTOR_SLOTS）；块形状不对返回 None。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP)
    if not ids or len(ids) != OUTFIT_GROUP_TOKENS:
        return None
    out = {}
    for k, slot in enumerate(OUTFIT_ACTOR_SLOTS):
        t = o._tokens[ids[k * OUTFIT_RECORD_TOKENS]]
        if t.kind != "raw" or t.extent != 4:
            return None
        out[slot] = int.from_bytes(bytes(t.value), "little")
    return out


def outfit_fingerprint(body):
    """整套外观指纹：装扮块 66 个 token 的 kind/extent/value（含站姿、身高、scale、seed）。
    换装/换站姿只要任何一个 token 变了，指纹就变。"""
    import hashlib
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP) or []
    h = hashlib.sha1()
    for i in ids:
        t = o._tokens[i]
        h.update(t.kind.encode() + b"|" + str(t.extent).encode() + b"|")
        h.update(bytes(t.value) if t.kind == "raw" else int(t.value).to_bytes(4, "little"))
    return h.hexdigest()[:16]


def bump_outfit_counter(body, by=1):
    """把外层计数 +by（uint32 LE）。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_COUNTER)
    if not ids or len(ids) != 1 or o._tokens[ids[0]].kind != "raw" or o._tokens[ids[0]].extent != 4:
        return body
    n = (int.from_bytes(bytes(o._tokens[ids[0]].value), "little") + by) & 0xFFFFFFFF
    return social_pose.set_groups(body, {OUTFIT_COUNTER: [n.to_bytes(4, "little").hex()]})


def attitude_of(body):
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP)
    return o._tokens[ids[-1 - ATTITUDE_FROM_END]].value if ids else None


def voice_of(body):
    """读 Actor 装扮块里的叫声编号（bits4，倒数第 4 个 token）；块形状不对返回 None。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP)
    if not ids or len(ids) <= VOICE_FROM_END:
        return None
    t = o._tokens[ids[-1 - VOICE_FROM_END]]
    return t.value if t.kind == "bits" else None


def outfit_counter_of(body):
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_COUNTER)
    if not ids or len(ids) != 1 or o._tokens[ids[0]].kind != "raw" or o._tokens[ids[0]].extent != 4:
        return None
    return int.from_bytes(bytes(o._tokens[ids[0]].value), "little")


def _patch_outfit_ids(body, outfit, only_slots=None):
    """只改部位记录里的 raw4 id，其余 token（tex/pat/mask/染料/身高/scale/voice/attitude/refresh/seed）原样。
    only_slots 给了就只碰这些部位（换装命令带 changes 时用，脱掉写 0 也照写）；
    没给（启动时套 control/outfit.json）只写非 0 的 id，避免把服务器 0 覆盖掉 Actor 里的默认件。
    服务器 get_outfit 里没有的部位（legs/arms）或 id 非法的值直接跳过。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP)
    if not ids or len(ids) != OUTFIT_GROUP_TOKENS:
        return body
    vals = [bytes(o._tokens[i].value).hex() if o._tokens[i].kind == "raw" else o._tokens[i].value for i in ids]
    changed = False
    for k, slot in enumerate(OUTFIT_ACTOR_SLOTS):
        if only_slots is not None and slot not in only_slots:
            continue
        part = outfit.get(slot) if isinstance(outfit, dict) else None
        if isinstance(part, dict):
            raw = part.get("id")
        elif isinstance(part, int) and not isinstance(part, bool):
            raw = part
        else:
            continue
        try:
            fid = int(raw if raw is not None else 0)
        except (TypeError, ValueError):
            continue
        if not 0 <= fid <= 0xFFFFFFFF:
            continue
        if fid == 0 and only_slots is None:
            continue
        at = k * OUTFIT_RECORD_TOKENS
        if vals[at] == fid.to_bytes(4, "little").hex():
            continue
        vals[at] = fid.to_bytes(4, "little").hex()
        changed = True
    return social_pose.set_groups(body, {OUTFIT_GROUP: vals}) if changed else body


def _read_control_json(name):
    try:
        return json.loads((CONTROL_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _patch_attitude(body, v):
    """只改装扮块里的站姿（bits4），不碰计数。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP)
    if not ids or len(ids) < 6:
        return body
    vals = [bytes(o._tokens[i].value).hex() if o._tokens[i].kind == "raw" else o._tokens[i].value for i in ids]
    k = len(ids) - 1 - ATTITUDE_FROM_END
    t = o._tokens[ids[k]]
    if t.kind != "bits" or t.extent != 4:
        return body
    if vals[k] == v:
        return body
    vals[k] = v
    return social_pose.set_groups(body, {OUTFIT_GROUP: vals})


def _patch_voice(body, v):
    """只改装扮块里的叫声（bits4，倒数第 4 个），不碰站姿，也不碰计数。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_GROUP)
    if not ids or len(ids) < 6:
        return body
    vals = [bytes(o._tokens[i].value).hex() if o._tokens[i].kind == "raw" else o._tokens[i].value for i in ids]
    k = len(ids) - 1 - VOICE_FROM_END
    t = o._tokens[ids[k]]
    if t.kind != "bits" or t.extent != 4:
        return body
    if vals[k] == v:
        return body
    vals[k] = v
    return social_pose.set_groups(body, {OUTFIT_GROUP: vals})


def _patch_counter(body, n):
    """把外层外观计数直接写成 n（不读写 control/outfit_counter）。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_COUNTER)
    if not ids or len(ids) != 1 or o._tokens[ids[0]].kind != "raw" or o._tokens[ids[0]].extent != 4:
        return body
    if int.from_bytes(bytes(o._tokens[ids[0]].value), "little") == (int(n) & 0xFFFFFFFF):
        return body
    return social_pose.set_groups(body, {OUTFIT_COUNTER: [(int(n) & 0xFFFFFFFF).to_bytes(4, "little").hex()]})


def with_attitude(seed):
    """control/attitude 有数字（0~14）就把 Actor 装扮块里的站姿改成它，外观变了计数 +1。"""
    try:
        v = int((CONTROL_DIR / "attitude").read_text().strip())
    except (OSError, ValueError):
        return seed
    if not 0 <= v <= 14:
        return seed
    out = _patch_attitude(seed, v)
    return set_outfit_counter(out)


def with_outfit(seed, outfit=None, only_slots=None):
    """把 Actor 装扮块里 10 个部位 id 改成 outfit（缺省读 control/outfit.json），外观变了计数 +1。
    only_slots 给了只改这些部位（允许写 0 = 脱掉）；没给只写非 0 id。
    服务器侧 legs/arms 无法写进 Actor（块里没有这两个部位），只改服务器。"""
    if outfit is None:
        outfit = _read_control_json("outfit.json")
    if not isinstance(outfit, dict):
        return seed
    out = _patch_outfit_ids(seed, outfit, only_slots)
    return set_outfit_counter(out)


def _owned_voice_ids():
    """wardrobe 写叫声前按拥有清单核对后写的 control/voices_owned.json；没有/坏了只认默认叫声 0。
    （红线：Actor 里也绝不出现小光没拥有的叫声，就算有人直接写 control/pose.json。）"""
    try:
        doc = json.loads((CONTROL_DIR / "voices_owned.json").read_text(encoding="utf-8"))
        ids = {int(v) for v in doc.get("owned", []) if 0 <= int(v) <= 15}
    except (OSError, ValueError, TypeError, AttributeError):
        ids = set()
    return ids | {0}


def _control_voice():
    """control/voice 里的叫声编号（0~15，且在已拥有名单里）；没有/非法返回 None。"""
    try:
        v = int((CONTROL_DIR / "voice").read_text().strip())
    except (OSError, ValueError):
        return None
    return v if 0 <= v <= 15 and v in _owned_voice_ids() else None


def with_voice(seed):
    """control/voice 有数字（0~15）就把 Actor 装扮块里的叫声改成它，外观变了计数 +1。"""
    v = _control_voice()
    if v is None:
        return seed
    out = _patch_voice(seed, v)
    return set_outfit_counter(out)


def with_appearance(seed):
    """启动/换图时一次性套用 control/attitude + control/voice + control/outfit.json，整套原子加一次计数。
    如果 control/outfit_slots.json 记录了显式改过的部位，只写这些部位（允许 0 = 脱掉）；
    没有记录时才退回“只写非 0 id”，避免把服务器 0 覆盖掉 Actor 里的默认件。"""
    try:
        v = int((CONTROL_DIR / "attitude").read_text().strip())
        if not 0 <= v <= 14:
            v = None
    except (OSError, ValueError):
        v = None
    vv = _control_voice()
    outfit = _read_control_json("outfit.json")
    out = _patch_voice(seed, vv) if vv is not None else seed
    out = _patch_attitude(out, v) if v is not None else out
    if isinstance(outfit, dict):
        slots = _read_control_json("outfit_slots.json")
        only = {s for s in slots if isinstance(s, str)} if isinstance(slots, list) and slots else None
        out = _patch_outfit_ids(out, outfit, only)
    return set_outfit_counter(out)


def set_outfit_counter(body, fingerprint=None):
    """外层计数单调递增：control/outfit_counter 记 {"counter": 用过的最大值, "fingerprint": 上次整套外观指纹}。
    外观指纹变了用 最大值+1；没变（重连/换图/只是改朝向）沿用。旧文件只有 "attitude" 字段时视为指纹缺失，迁移一次。"""
    import synth_capture
    o = _actor_obs(body)
    ids = synth_capture._index(o).get(OUTFIT_COUNTER)
    if not ids or len(ids) != 1 or o._tokens[ids[0]].kind != "raw" or o._tokens[ids[0]].extent != 4:
        return body
    here = int.from_bytes(bytes(o._tokens[ids[0]].value), "little")
    fp = fingerprint or outfit_fingerprint(body)
    f = CONTROL_DIR / "outfit_counter"
    try:
        rec = json.loads(f.read_text())
        n, last = int(rec["counter"]), rec.get("fingerprint")
    except (OSError, ValueError, KeyError, TypeError):
        n, last = here, None
    if last != fp:
        n = max(n, here) + 1
        try:
            CONTROL_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"counter": n, "fingerprint": fp}))
        except OSError:
            pass
    n = max(n, here) & 0xFFFFFFFF
    return social_pose.set_groups(body, {OUTFIT_COUNTER: [n.to_bytes(4, "little").hex()]})


def load_seats(map_id):
    """bot/seats_<map_id>.json（seat_scan.py 从抓包生成）：{座位号: {"origin": [x,y,z], "yaw": n}}。"""
    if map_id == HOME_MAP_ID:
        return {}
    path = xg_paths.BOT_DATA / f"seats_{map_id}.json"
    if map_id is None or not path.exists():
        return {}
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


SNAPSHOT_HOOKS = []
ROSTER_HOOKS = []
SOCIAL_RPC_HOOKS = []
PLACEABLE_RPC_HOOKS = []
LAST_EPOCH = None


def _hook_engine_roster():
    """Publish roster only after Engine.handle accepts identity continuity.

    Initial EnterGame is delivered by begin_room after constructor validation;
    the pure parse helper must never publish a not-yet-accepted candidate.
    """
    original = map_long_engine.MapLongEngine.handle
    if getattr(original, "_claudework_roster_hooked", False):
        return

    def handle(engine, outer_type, payload, now):
        result = original(engine, outer_type, payload, now)
        if (outer_type == 17 or (outer_type == 8 and hasattr(engine,"_roster_body"))) and not engine._closed:
            for hook in list(ROSTER_HOOKS):
                try:
                    hook(getattr(engine,"_roster_body",bytes(payload)), match=engine._match, level_id=engine._map, engine=engine)
                except Exception:
                    pass
        if outer_type == 14 and not engine._closed:
            for hook in list(globals().get("SOCIAL_RPC_HOOKS", ())):
                try:
                    hook(engine, bytes(payload))
                except Exception:
                    pass
        if outer_type == 14 and not engine._closed:
            for hook in list(globals().get("PLACEABLE_RPC_HOOKS", ())):
                # 此钩子自身处理输入错误并记限速日志，不影响原社交钩子。
                try:
                    hook(engine, bytes(payload))
                except Exception as exc:
                    tracker = getattr(hook, "__self__", None)
                    if tracker is not None:
                        tracker._placeable_notice([{"reason": "prop_hook_error", "error": type(exc).__name__}])
        return result
    handle._claudework_roster_hooked = True
    map_long_engine.MapLongEngine.handle = handle


def _hook_engine_snapshots():
    """包一层引擎里的 accept_player_state_delta：原样调用，成功后把解码结果分给 SNAPSHOT_HOOKS。只装一次。"""
    original = map_long_engine.accept_player_state_delta
    if getattr(original, "_claudework_hooked", False):
        return

    def accept(payload, *, receive_state):
        accepted = original(payload, receive_state=receive_state)
        for hook in list(SNAPSHOT_HOOKS):
            try:
                hook(accepted.data, epoch=payload[1], receive_state=receive_state,
                     snapshot_sequence=payload[3] if accepted.saved else 0,
                     snapshot_source=payload[2])
            except Exception:
                pass                           # 追踪出错不能影响引擎发 ACK
        return accepted
    accept._claudework_hooked = True
    map_long_engine.accept_player_state_delta = accept


def _hook_engine_lifecycle(tracker):
    """Install once; each validated engine lifetime starts fresh room-local data."""
    original = map_long_engine.MapLongEngine.__init__
    if not getattr(original, "_claudework_room_hooked", False):
        def init(engine, *args, **kwargs):
            original(engine, *args, **kwargs)
            try:
                for hook in list(ROOM_HOOKS):
                    hook(engine, kwargs["enter_game_body"])
            except Exception:
                engine.close()
                raise
        init._claudework_room_hooked = True
        map_long_engine.MapLongEngine.__init__ = init
    original_close = map_long_engine.MapLongEngine.close
    if not getattr(original_close, "_claudework_room_close_hooked", False):
        def close(engine):
            result = original_close(engine)
            for hook in list(ROOM_CLOSE_HOOKS):
                try:
                    hook(engine)
                except Exception:
                    pass
            return result
        close._claudework_room_close_hooked = True
        map_long_engine.MapLongEngine.close = close
    ROOM_HOOKS[:] = [tracker.begin_room]
    ROOM_CLOSE_HOOKS[:] = [tracker.end_room]


ROOM_HOOKS = []
ROOM_CLOSE_HOOKS = []


def _apply_validated_room(tracker, ctl, engine, body):
    """T23-4：engine 重新校验房间时的复位策略。

    旧实现只要 ``begin_room`` 成功就 ``reset("validated_room")``，不管还是不是
    同一张图/同一个房间。线上 18:44:00 的长连接没断、map_id 也没变，却被传回
    云野出生点，坐下状态一起丢了。这里把“同图重校验”和“真的换图”分开：
    同图保留当前位置/朝向（坐着且座位还在就继续坐）；换图才回新房间种子。
    """
    was_ready = bool(getattr(tracker, "ready", False))
    old_map = getattr(tracker, "confirmed_map_id", None) if was_ready else getattr(ctl, "map_id", None)
    old_sitting = getattr(ctl, "sitting", None)
    old_event = getattr(ctl, "event", None)
    try:
        with tracker.lock:
            old_seat_book = dict(getattr(tracker, "seat_book", {}) or {})
    except Exception:
        old_seat_book = {}
    if not tracker.begin_room(engine, body):
        return False
    new_map = getattr(tracker, "confirmed_map_id", None)
    same_room = bool(was_ready and old_map is not None and new_map == old_map)
    keep_seat = bool(new_map != HOME_MAP_ID and old_sitting is not None and (
        old_sitting in getattr(ctl, "seats", {})
        or old_sitting in old_seat_book
        or isinstance(old_event, anim_pose.SitEvent)))
    map_long_engine.OUTBOX.clear()   # 旧房间未发的互动包不跨生命周期
    ctl.reset("validated_room", keep_pose=same_room, keep_seat=keep_seat)
    ctl.last_social = None
    ctl.candle_reply = None           # 不恢复旧房间举烛前暂停的跟随
    ctl.hold_prev = ctl.hold_v = None
    ctl.log("validated_room_reset", same_room=same_room, keep_seat=keep_seat,
            old_map_id=old_map, map_id=new_map,
            old_sitting=old_sitting,
            at=[round(v, 3) for v in ctl.position])
    return True


def install(control_path, seed, log, stop, map_id=None, own_uuid=None,
            ground_ref=None, variant=None, account=None):
    tracker = RoomTracker()
    tracker.placeable_log = log
    tracker.own_uuid = own_uuid
    tracker.begin_segment(map_id)
    pump_mod.CLAUDEWORK_LISTENERS.append(tracker)
    _hook_engine_lifecycle(tracker)
    _hook_engine_snapshots()
    _hook_engine_roster()
    SNAPSHOT_HOOKS[:] = [tracker.on_snapshot]   # 每次重连新建追踪器，只留最新的
    ROSTER_HOOKS[:] = [tracker._roster]
    SOCIAL_RPC_HOOKS[:] = [tracker.on_social_rpc]
    PLACEABLE_RPC_HOOKS[:] = [tracker.on_placeable_rpc]
    ctl = PoseController(control_path, seed, tracker, log, stop, account=account)
    def on_room(engine, body):
        _apply_validated_room(tracker, ctl, engine, body)
    ROOM_HOOKS[:] = [on_room]
    ctl.map_id = map_id
    ctl.variant = variant
    ctl._placement_ref_y = ground_ref
    ctl._placement_wire_y = anim_pose.read_origin(seed)[1]
    ctl.set_landing(map_id, variant, ctl.home, "install")
    ctl.reset("start")                        # 一开始就装上站立版种子
    ctl._reground("install", ground_ref=ground_ref,
                  allow_player_fallback=False)   # 地形还没挂时是 no-op，首个 tick 再补
    ctl.seats = load_seats(map_id)
    if map_id is not None and map_id != HOME_MAP_ID:
        ctl.seat_file = xg_paths.BOT_DATA / f"seats_{map_id}.json"
    log("seats_loaded", map_id=map_id, seats=sorted(ctl.seats))
    log("pose_install", map_id=map_id, variant=variant, ground_ref=ground_ref,
        seed_source=ctl._seed_source, seed_origin=ctl._seed_origin(),
        home=[round(v, 3) for v in ctl.home],
        override_seed=("seed" in map_long_engine.POSE_OVERRIDE),
        override_origin=ctl._override_origin(), **ctl._landing_diag())
    ctl.start()
    return tracker, ctl
