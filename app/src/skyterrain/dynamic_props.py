"""房间内存道具 → 冷 worker 同步与共享地面视图；无持久运行时状态。"""
from __future__ import annotations

import json
import threading
import copy
import math

from .props import build_colliders, PropSurfaceOverlay, available_prop_names, MAX_PROPS, MAX_VERTICES, MAX_TRIANGLES

# 留出 request id、op、epoch 和 revision 的帧空间。
MAX_COLLIDER_JSON_BYTES = 500000
MAX_ROOM_CANDIDATES = 256
REQUIRED_CORRIDOR_MARGIN = 2.0
PATH_COVERAGE_MARGIN = 0.75
ACTIVE_HYSTERESIS_METERS = 1.0


class PlaceableBinding:
    def __init__(self, snapshot, revision, *, room_generation, map_id,
                 log=None, allow_observed=False, catalog=None, require_focus=False):
        self.snapshot = snapshot
        self.revision = revision
        self.room_generation = room_generation
        self.map_id = map_id
        self.log = log
        self.allow_observed = allow_observed
        # 已冷加载的真实资产快照；接收/Actor 热路径不读 catalog 文件。
        self.catalog = copy.deepcopy(catalog) if catalog is not None else None
        self.enabled = bool(available_prop_names(catalog=self.catalog,allow_observed=allow_observed))
        self._lock = threading.RLock()
        self._key = None
        self._colliders = []
        self._serial = 0
        self._sent = None
        self._load_epoch = None
        self._published_nonempty = False
        self._room_valid = False
        self._overlay = None
        self._static = None
        self._unavailable_logged = False
        self._legacy_worker = False
        self.require_focus = require_focus
        self._focus = None
        self._selected_focus = None
        self._candidate_key = None
        self._candidates = []
        self._excluded = []
        self._selection_blocked = False

    @property
    def has_excluded_colliders(self):
        return bool(self._excluded)

    @property
    def selection_revision(self):
        # 缓存只读戳：focus微移但active相同不变；待同步时sent=None。
        return (self._serial, self._sent)

    def set_focus(self, start, goal):
        """冷分段准备输入 feet 世界坐标；不发RPC、不改变当前lease。"""
        points = (tuple(start), tuple(goal))
        if any(len(p) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or abs(v) > 10000 for v in p) for p in points):
            raise ValueError('prop_focus_invalid')
        with self._lock:
            self._focus = points

    @staticmethod
    def _bounds(collider):
        p = collider['positions']
        return tuple(min(p[k::3]) for k in range(3)), tuple(max(p[k::3]) for k in range(3))

    @staticmethod
    def _intersects(start, goal, bounds, margin):
        # XZ保守扩盒：不以Y层筛除，覆盖原bounded step和空中抬升。
        lo, hi = bounds
        low = (lo[0]-margin, lo[1]-1.85, lo[2]-margin)
        high = (hi[0]+margin, hi[1]+.35, hi[2]+margin)
        left, right = 0., 1.
        for k in (0, 2):
            d = goal[k]-start[k]
            if abs(d) < 1e-12:
                if not low[k] <= start[k] <= high[k]: return False
            else:
                a, b = (low[k]-start[k])/d, (high[k]-start[k])/d
                left, right = max(left, min(a,b)), min(right, max(a,b))
                if left > right: return False
        return True

    def _select(self, candidates):
        start, goal = self._focus
        old = {c['id'] for c in self._colliders}
        def distance(row, point):
            lo, hi = row[1]
            return math.hypot(max(lo[0]-point[0],0.,point[0]-hi[0]), max(lo[2]-point[2],0.,point[2]-hi[2]))
        # 双端各自距离队列交错；保留项获得1m滞后，不取代关键corridor强制门。
        queues = [sorted(candidates,key=lambda row:(max(0.,distance(row,p)-(ACTIVE_HYSTERESIS_METERS if row[0]['id'] in old else 0.)),row[0]['id'])) for p in (start,goal)]
        rank=[];seen=set()
        for pair in zip(*queues):
            for row in pair:
                if row[0]['id'] not in seen:rank.append(row);seen.add(row[0]['id'])
        required = {c['id'] for c,b in candidates if self._intersects(start,goal,b,REQUIRED_CORRIDOR_MARGIN)}
        rank.sort(key=lambda row:row[0]['id'] not in required)
        accepted=[];excluded=[];rejected=[];vertices=triangles=0;size=2
        for collider,bounds in rank:
            v,t=len(collider['positions'])//3,len(collider['triangles'])//3
            n=len(json.dumps(collider,separators=(',',':'),allow_nan=False).encode('utf8'))+(1 if accepted else 0)
            reason=('active_count_budget' if len(accepted)>=MAX_PROPS else 'active_vertex_budget' if vertices+v>MAX_VERTICES else 'active_triangle_budget' if triangles+t>MAX_TRIANGLES else 'json_frame_budget' if size+n>MAX_COLLIDER_JSON_BYTES else None)
            if reason:
                excluded.append((collider,bounds));rejected.append({'id':collider['id'],'reason':reason,'required':collider['id'] in required})
            else:
                accepted.append(collider);vertices+=v;triangles+=t;size+=n
        self._selection_blocked=any(c['id'] in required for c,b in excluded)
        self._excluded=excluded
        return sorted(accepted,key=lambda c:c['id']),rejected

    def path_covered(self, path):
        """冷prepare返回真实折线路径后复核遗漏实体；命中则failclosed。"""
        if self._focus is None or not self._excluded:return not self._selection_blocked
        if not isinstance(path,(list,tuple)) or not 2 <= len(path) <= 2048:return False
        try:
            points=[tuple(p) for p in path]
            if any(len(p)!=3 or any(not math.isfinite(v) for v in p) for p in points):return False
            hits=[]
            for collider,bounds in self._excluded:
                if any(self._intersects(a,b,bounds,PATH_COVERAGE_MARGIN) for a,b in zip(points,points[1:])):
                    hits.append(collider['id'])
            if hits:self._emit('path_coverage_missing',excluded_ids=hits[:32],excluded_count=len(hits))
            return not hits
        except (TypeError,ValueError,OverflowError):return False

    def _emit(self, reason, **fields):
        if callable(self.log):
            try:
                self.log('props_terrain', reason=reason, **fields)
            except Exception:
                pass  # 日志故障不改变旧寻路结果。

    def _revision(self):
        try:
            key = self.revision()
            if (type(key) is not tuple or len(key) != 2
                    or any(type(n) is not int or n < 0 for n in key)):
                return None
            return key
        except Exception:
            return None

    def applied_current(self, generation):
        # 热路径只读短元组，无 RPC、无网格复制、无文件 I/O。
        if not self.enabled or self._legacy_worker:
            return True
        key = self._revision()
        return (not self._selection_blocked and (not self.require_focus or self._selected_focus == self._focus)
                and key is not None and key[0] == self.room_generation and self._room_valid
                and self._sent == (key, generation, self._load_epoch))

    def on_load(self, load_epoch):
        """同一进程也可能 reload；旧发送凭据在新 load 后立即失效。"""
        with self._lock:
            self._load_epoch = load_epoch
            self._published_nonempty = False
            self._sent = None
            self._legacy_worker = type(load_epoch) is not int
            if self.enabled and self._legacy_worker and not self._unavailable_logged:
                self._emit('worker_without_dynamic_props')
                self._unavailable_logged = True

    def _refresh(self):
        key = self._revision()
        if key is None:
            return False
        if key is not None and key == self._key and self._selected_focus == self._focus:
            return not self._selection_blocked
        try:
            snap = self.snapshot()
            if snap is None:
                return False
            if type(snap) is not dict:
                raise ValueError('snapshot_schema')
            got = (snap.get('room_generation'), snap.get('revision'))
            if key is None or key != got:
                # 接收线程正跨生命周期更新；下一次冷准备重试。
                return False
            props = snap.get('props', [])
            self._room_valid = (snap.get('validated') is True and got[0] == self.room_generation
                                and snap.get('map_id') == self.map_id)
            if not self._room_valid:
                props = []
            if self.require_focus and self._focus is None:
                self._emit('focus_missing');return False
            if self._focus is not None:
                if key != self._candidate_key:
                    candidates=[];rejected=[];seen=set()
                    if not isinstance(props,(list,tuple)):raise ValueError('props_not_sequence')
                    for prop in props[:MAX_ROOM_CANDIDATES]:
                        made,errors=build_colliders([prop],catalog=self.catalog,allow_observed=self.allow_observed)
                        rejected.extend(errors)
                        for collider in made:
                            if collider['id'] in seen:rejected.append({'id':collider['id'],'reason':'prop_duplicate_id'});continue
                            seen.add(collider['id']);candidates.append((collider,self._bounds(collider)))
                    if len(props)>MAX_ROOM_CANDIDATES:rejected.append({'reason':'room_candidate_budget','ignored_count':len(props)-MAX_ROOM_CANDIDATES})
                    self._candidates=candidates;self._candidate_key=key
                else:rejected=[]
                accepted,filtered=self._select(self._candidates);rejected.extend(filtered)
            else:
                colliders,rejected=build_colliders(props,catalog=self.catalog,allow_observed=self.allow_observed)
                accepted=[]
                for collider in colliders:
                    if len(json.dumps(accepted+[collider],separators=(',',':'),allow_nan=False).encode('utf8'))>MAX_COLLIDER_JSON_BYTES:
                        rejected.append({'id':collider.get('id'),'reason':'json_frame_budget'})
                    else:accepted.append(collider)
            changed = accepted != self._colliders or key != self._key
            self._colliders = accepted
            self._key = key
            self._selected_focus = self._focus
            if changed:
                self._serial += 1
                self._sent = None
                self._overlay = None
            if self._selection_blocked:self._emit('required_props_over_budget',excluded_count=len(self._excluded))
            for item in rejected[:32]:
                self._emit('ignored', detail=item)
            return not self._selection_blocked
        except Exception as exc:
            self._colliders = []
            self._overlay = None
            self._excluded = []
            self._selection_blocked = False
            self._selected_focus = self._focus
            self._candidate_key = None
            self._sent = None
            self._key = key
            self._serial += 1
            self._emit('invalid_snapshot', error=type(exc).__name__ + ':' + str(exc)[:160])
            return key is not None

    def surface(self, static):
        if not self.enabled or self._legacy_worker:
            return static
        # 高度和空中检查不可等待冷同步；忙时返回“无可用几何”，不沿用过期面。
        if not self._lock.acquire(blocking=False):
            return None
        try:
            # 正式backend的Actor热查询不建立256候选或改变active集合。
            if self.require_focus and (self._focus is None or self._key != self._revision()
                    or self._selected_focus != self._focus or self._sent is None or self._selection_blocked):
                return None
            if not self._refresh():
                return None
            if not self._room_valid:
                return None
            if static is None or not self._colliders:
                return static
            if self._overlay is None or static is not self._static:
                self._static = static
                try:
                    self._overlay = PropSurfaceOverlay(static, self._colliders)
                except Exception as exc:
                    self._emit('overlay_failed', error=str(exc)[:160])
                    return static
            return self._overlay
        finally:
            self._lock.release()

    def sync(self, transport, load_epoch):
        """仅冷 prepare_route 调用；完整原子替换，不逐件产生中间世界。"""
        if not self.enabled:
            return True
        with self._lock:
            if self._legacy_worker and type(load_epoch) is not int:
                return True
            if load_epoch != self._load_epoch:
                self._load_epoch = load_epoch
                self._sent = None
                self._legacy_worker = type(load_epoch) is not int
            if not self._refresh():
                return False
            key, generation = self._key, transport.generation
            if self._sent == (key, generation, load_epoch):
                return self.applied_current(generation)
            if type(load_epoch) is not int:
                # 与未升级 worker 混用时明确降级，不发送未知指令。
                if self._colliders and not self._unavailable_logged:
                    self._emit('worker_without_dynamic_props')
                    self._unavailable_logged = True
                self._colliders = []
                self._overlay = None
                self._legacy_worker = True
                self._sent = (key, generation, load_epoch)
                return self.applied_current(generation)
            if self._sent is None and not self._colliders and not self._published_nonempty:
                # 新 load 已清空；空房间原协议消息序列保持不变。
                self._sent = (key, generation, load_epoch)
                return self.applied_current(generation)
            colliders, serial = self._colliders, self._serial
        # 不持 surface 锁跨进程等待。快照在此后变化，旧回复只会失效，不会发布旧路线。
        reply = transport.request('setColliders', colliders=colliders,
            revision=serial, expectedLoadEpoch=load_epoch)
        with self._lock:
            if (reply.get('status') != 'ready' or reply.get('colliderEpoch') != load_epoch
                    or reply.get('colliderRevision') != serial
                    or reply.get('colliderCount') != len(colliders)):
                self._emit('worker_rejected', detail=reply.get('reason', 'invalid_receipt'))
                return False
            if self._load_epoch != load_epoch or self._serial != serial:
                return False
            self._sent = (key, generation, load_epoch)
            self._published_nonempty = bool(colliders)
            self._emit('synced', count=len(colliders), revision=serial)
            return self.applied_current(generation)
