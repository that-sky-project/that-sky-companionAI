"""仅使用标准库的分层采样地图、可达目的地和保守 A* 寻路。"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
import heapq
import json
import math
from numbers import Real
import os
import random
from statistics import median


Position = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class _Node:
    ix: int
    iz: int
    y: float
    count: int


class WalkMap:
    """从玩家地面采样构建静态图；类参数需在 from_json 前设置。

    格心为 (ix * cell, iz * cell)，最近格量化 floor(v / cell + 0.5)。
    路径端点吸附到可走格心；不附加未经检查的输入坐标。
    """

    MAX_STEP = 0.6
    NOISE_MAX_COUNT = 1
    NOISE_HEIGHT = 1.5
    NOISE_RADIUS = 1
    HEIGHT_PENALTY = 1.0
    GAP_COST_MULTIPLIER = 2.0
    SMOOTH_HEIGHT_TOLERANCE = 0.05
    EPSILON = 1e-9

    _DIRECTIONS = ((1, 0), (0, 1), (-1, 0), (0, -1),
                   (1, 1), (-1, 1), (-1, -1), (1, -1))

    def __init__(self, cell: float = 1.0, map_id=None):
        self.cell = self._number(cell, "cell")
        if self.cell <= 0:
            raise ValueError("cell 必须大于零")
        self.map_id = map_id
        self._step = self._number(self.MAX_STEP, "MAX_STEP")
        self._height_penalty = self._number(self.HEIGHT_PENALTY, "HEIGHT_PENALTY")
        self._gap_multiplier = self._number(self.GAP_COST_MULTIPLIER,
                                             "GAP_COST_MULTIPLIER")
        self._smooth_tolerance = self._number(self.SMOOTH_HEIGHT_TOLERANCE,
                                               "SMOOTH_HEIGHT_TOLERANCE")
        self._eps = self._number(self.EPSILON, "EPSILON")
        if min(self._step, self._height_penalty, self._smooth_tolerance,
               self._eps) < 0 or self._gap_multiplier < 1:
            raise ValueError("步高、惩罚、容差需非负；缺口倍率需 >= 1")
        if (isinstance(self.NOISE_RADIUS, bool)
                or not isinstance(self.NOISE_RADIUS, int)
                or self.NOISE_RADIUS < 0):
            raise ValueError("NOISE_RADIUS 必须是非负整数")
        if self._number(self.NOISE_MAX_COUNT, "NOISE_MAX_COUNT") < 0:
            raise ValueError("NOISE_MAX_COUNT 不能为负")
        if self._number(self.NOISE_HEIGHT, "NOISE_HEIGHT") < 0:
            raise ValueError("NOISE_HEIGHT 不能为负")
        self._nodes: list[_Node] = []
        self._cells: dict[tuple[int, int], list[int]] = {}
        self._edges: dict[int, tuple[tuple[int, float], ...]] = {}
        self._component_ids: dict[int, int] = {}
        self._components: list[tuple[int, ...]] = []

    @staticmethod
    def _number(value, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} 必须是有限数值")
        try:
            result = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{name} 必须是有限数值") from exc
        if not math.isfinite(result):
            raise ValueError(f"{name} 必须是有限数值")
        return result

    def _cell_at(self, x: float, z: float) -> tuple[int, int]:
        return math.floor(x / self.cell + 0.5), math.floor(z / self.cell + 0.5)

    @classmethod
    def from_json(cls, path_or_dict) -> WalkMap:
        """读取 UTF-8 JSON 文件（支持 PathLike）或字典；不修改输入。"""
        if isinstance(path_or_dict, (str, bytes, os.PathLike)):
            with open(path_or_dict, encoding="utf-8-sig") as stream:
                data = json.load(stream)
        else:
            data = path_or_dict
        if not isinstance(data, Mapping) or "points" not in data:
            raise ValueError("输入必须是含 points 的 JSON 对象")
        result = cls(data.get("cell", 1.0), data.get("map_id"))
        points = data["points"]
        if not isinstance(points, (list, tuple)):
            raise ValueError("points 必须是数组")
        # 相同格、相同高度的重复观测合并 count；不同高度不强行平均。
        merged: dict[tuple[int, int, float], int] = {}
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 4:
                raise ValueError("每个采样必须为 [x, y, z, count]")
            x, y, z = (result._number(v, name)
                       for v, name in zip(point[:3], ("x", "y", "z")))
            count = point[3]
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise ValueError("count 必须是正整数")
            ix, iz = result._cell_at(x, z)
            key = ix, iz, y
            merged[key] = merged.get(key, 0) + count
        raw = [_Node(ix, iz, y, count)
               for (ix, iz, y), count in sorted(merged.items())]
        by_cell: dict[tuple[int, int], list[_Node]] = {}
        for node in raw:
            by_cell.setdefault((node.ix, node.iz), []).append(node)
        for node in raw:
            if result._is_noise(node, by_cell):
                continue
            index = len(result._nodes)
            result._nodes.append(node)
            result._cells.setdefault((node.ix, node.iz), []).append(index)
        return result

    def _is_noise(self, node: _Node, cells) -> bool:
        if node.count > self.NOISE_MAX_COUNT:
            return False
        peers = []
        radius = self.NOISE_RADIUS
        for ix in range(node.ix - radius, node.ix + radius + 1):
            for iz in range(node.iz - radius, node.iz + radius + 1):
                peers.extend(other for other in cells.get((ix, iz), ())
                             if other is not node)
        reliable = [other for other in peers if other.count > self.NOISE_MAX_COUNT]
        references = reliable or peers
        if not references:
            return False
        # 保留得到邻近同层点支持的桥面/平台；参考点来自原始快照，过滤不依赖顺序。
        if any(abs(other.y - node.y) <= self._step + self._eps
               for other in references):
            return False
        lowest = {}
        for other in references:
            key = other.ix, other.iz
            lowest[key] = min(lowest.get(key, other.y), other.y)
        baseline = median(lowest.values())
        return node.y - baseline > self.NOISE_HEIGHT + self._eps

    def _position(self, index: int) -> Position:
        node = self._nodes[index]
        return node.ix * self.cell, node.y, node.iz * self.cell

    def _parse_position(self, pos) -> Position:
        if not isinstance(pos, (list, tuple)) or len(pos) != 3:
            raise ValueError("位置必须为三个有限数值 (x, y, z)")
        return tuple(self._number(v, "位置坐标") for v in pos)

    def ground_at(self, x: float, z: float,
                  near_y: float | None = None) -> float | None:
        """查询最近格的采样层；没有 near_y 时选择最低层，缺口不补数据。"""
        x, z = self._number(x, "x"), self._number(z, "z")
        if near_y is not None:
            near_y = self._number(near_y, "near_y")
        indices = self._cells.get(self._cell_at(x, z), ())
        if not indices:
            return None
        if near_y is None:
            return self._nodes[indices[0]].y
        return min((self._nodes[i].y for i in indices),
                   key=lambda y: (abs(y - near_y), y))

    def _nearest_node(self, pos: Position) -> int | None:
        x, y, z = pos
        best = None
        best_key = (2, math.inf)
        for index, node in enumerate(self._nodes):
            dy = node.y - y
            key = (0 if abs(dy) <= self._step + self._eps else 1,
                   (node.ix * self.cell - x) ** 2 + dy ** 2
                   + (node.iz * self.cell - z) ** 2)
            if key < best_key:
                best, best_key = index, key
        return best

    def nearest_walkable(self, pos: Position) -> Position | None:
        """优先选择高度差 <= MAX_STEP 的节点，再比较三维距离。"""
        index = self._nearest_node(self._parse_position(pos))
        return None if index is None else self._position(index)

    def _side_supports(self, cell, a: _Node, b: _Node) -> bool:
        return any(abs(self._nodes[i].y - a.y) <= self._step + self._eps
                   and abs(self._nodes[i].y - b.y) <= self._step + self._eps
                   for i in self._cells.get(cell, ()))

    def _link_cost(self, a: _Node, b: _Node, allow_gap=True) -> float | None:
        dx, dz = b.ix - a.ix, b.iz - a.iz
        ax, az = abs(dx), abs(dz)
        dy = abs(a.y - b.y)
        if dy > self._step + self._eps:
            return None
        if max(ax, az) == 1:
            if ax and az and not (
                self._side_supports((a.ix + dx, a.iz), a, b)
                and self._side_supports((a.ix, a.iz + dz), a, b)
            ):
                return None
            return math.hypot(dx, dz) * self.cell + self._height_penalty * dy
        if (allow_gap and (ax, az) in ((2, 0), (0, 2))
                and (a.ix + dx // 2, a.iz + dz // 2) not in self._cells):
            return ((2 * self.cell + self._height_penalty * dy)
                    * self._gap_multiplier)
        return None

    def _neighbors(self, index: int) -> tuple[tuple[int, float], ...]:
        cached = self._edges.get(index)
        if cached is not None:
            return cached
        node = self._nodes[index]
        edges = []
        directions = self._DIRECTIONS + ((2, 0), (0, 2), (-2, 0), (0, -2))
        for dx, dz in directions:
            for neighbor in self._cells.get((node.ix + dx, node.iz + dz), ()):
                cost = self._link_cost(node, self._nodes[neighbor])
                if cost is not None:
                    edges.append((neighbor, cost))
        cached = tuple(edges)
        self._edges[index] = cached
        return cached

    def _component(self, index: int) -> tuple[int, ...]:
        known = self._component_ids.get(index)
        if known is not None:
            return self._components[known]
        number = len(self._components)
        queue = deque([index])
        self._component_ids[index] = number
        members = []
        while queue:
            current = queue.popleft()
            members.append(current)
            for neighbor, _ in self._neighbors(current):
                if neighbor not in self._component_ids:
                    self._component_ids[neighbor] = number
                    queue.append(neighbor)
        component = tuple(sorted(members))
        self._components.append(component)
        return component

    def random_target(self, pos, min_dist=3.0, max_dist=12.0,
                      rng=None) -> Position | None:
        """在吸附起点的连通分量内按 count 加权抽样；距离是三维直线距离。"""
        pos = self._parse_position(pos)
        low = self._number(min_dist, "min_dist")
        high = self._number(max_dist, "max_dist")
        if low < 0 or high < low:
            raise ValueError("距离范围必须满足 0 <= min_dist <= max_dist")
        start = self._nearest_node(pos)
        if start is None:
            return None
        candidates, weights = [], []
        for index in self._component(start):
            target = self._position(index)
            distance = math.dist(pos, target)
            if low - self._eps <= distance <= high + self._eps:
                candidates.append(index)
                weights.append(self._nodes[index].count)
        if not candidates:
            return None
        generator = random if rng is None else rng
        chosen = generator.choices(candidates, weights=weights, k=1)[0]
        return self._position(chosen)

    def _heuristic(self, index: int, goal: int) -> float:
        a, b = self._nodes[index], self._nodes[goal]
        dx, dz = abs(a.ix - b.ix), abs(a.iz - b.iz)
        diagonal = min(dx, dz)
        return ((max(dx, dz) + (math.sqrt(2) - 1) * diagonal) * self.cell
                + abs(a.y - b.y) * self._height_penalty)

    def path(self, start, goal, max_nodes=5000) -> list[Position] | None:
        """A*；最多扩展 max_nodes 个节点（包含终点），超额或不可达返回 None。"""
        start, goal = self._parse_position(start), self._parse_position(goal)
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int):
            raise ValueError("max_nodes 必须为整数")
        if max_nodes <= 0:
            return None
        first, last = self._nearest_node(start), self._nearest_node(goal)
        if first is None or last is None:
            return None
        costs = {first: 0.0}
        parents: dict[int, int] = {}
        h = self._heuristic(first, last)
        queue = [(h, h, 0, first, 0.0)]
        closed = set()
        serial = 0
        expanded = 0
        while queue and expanded < max_nodes:
            _, _, _, current, cost = heapq.heappop(queue)
            if current in closed or cost != costs[current]:
                continue
            closed.add(current)
            expanded += 1
            if current == last:
                route = [last]
                while route[-1] != first:
                    route.append(parents[route[-1]])
                route.reverse()
                return [self._position(i) for i in self._simplify(route)]
            for neighbor, edge_cost in self._neighbors(current):
                if neighbor in closed:
                    continue
                candidate = cost + edge_cost
                if candidate < costs.get(neighbor, math.inf):
                    costs[neighbor] = candidate
                    parents[neighbor] = current
                    serial += 1
                    h = self._heuristic(neighbor, last)
                    heapq.heappush(queue, (candidate + h, h, serial,
                                           neighbor, candidate))
        return None

    def _line_visible(self, first: int, last: int) -> bool:
        """格心间整数 DDA；保留层身份、角邻格约束和线性插值高度约束。"""
        a, b = self._nodes[first], self._nodes[last]
        dx, dz = b.ix - a.ix, b.iz - a.iz
        ax, az = abs(dx), abs(dz)
        if not ax and not az:
            return first == last
        sx, sz = (1 if dx > 0 else -1), (1 if dz > 0 else -1)
        ix, iz = a.ix, a.iz
        crossed_x = crossed_z = 0
        reachable = (first,)
        denominator = dx * dx + dz * dz
        while (ix, iz) != (b.ix, b.iz):
            left = (2 * crossed_x + 1) * az
            right = (2 * crossed_z + 1) * ax
            if ax and (not az or left < right):
                ix += sx
                crossed_x += 1
            elif az and (not ax or right < left):
                iz += sz
                crossed_z += 1
            else:
                ix += sx
                iz += sz
                crossed_x += 1
                crossed_z += 1
            fraction = ((ix - a.ix) * dx + (iz - a.iz) * dz) / denominator
            ideal_y = a.y + (b.y - a.y) * fraction
            following = []
            for index in self._cells.get((ix, iz), ()):
                node = self._nodes[index]
                if abs(node.y - ideal_y) > self._smooth_tolerance + self._eps:
                    continue
                if any(self._link_cost(self._nodes[previous], node,
                                       allow_gap=False) is not None
                       for previous in reachable):
                    following.append(index)
            if not following:
                return False
            reachable = following
        return last in reachable

    def _simplify(self, route: list[int]) -> list[int]:
        if len(route) < 3:
            return route
        # 缺口两端必须保留；平滑绝不把多个缺口合并成一条未经检查的直线。
        end = [len(route) - 1] * len(route)
        for i in range(len(route) - 2, -1, -1):
            a, b = self._nodes[route[i]], self._nodes[route[i + 1]]
            is_gap = max(abs(a.ix - b.ix), abs(a.iz - b.iz)) > 1
            end[i] = i if is_gap else end[i + 1]
        result = [route[0]]
        anchor = 0
        while anchor < len(route) - 1:
            best = anchor + 1
            bound = end[anchor]
            stride = 2
            # 指数前瞻，首次失败即停止，避免逐点尝试整段导致平方级扫描。
            while best < bound:
                candidate = min(anchor + stride, bound)
                if not self._line_visible(route[anchor], route[candidate]):
                    break
                best = candidate
                stride *= 2
            result.append(route[best])
            anchor = best
        return result
