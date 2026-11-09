"""出生/站位的真实地形选层（纯函数，便于离线测试）。

2026-11-07 T19：霞谷神殿（SunsetEnd2）有上下两层地面。落点 actor 的
``wire_position`` 可能落在下层（T1 选到离原生标记低 2.45 m 的那层），
只用它当参考会把小光放到地底。这里把“目录/标注给出的意图高度”和
“房间里新鲜玩家（带路人）脚下高度”作为参考，交给 skyterrain 的真实
三角形集合选一层最近的；没有几何时只用参考玩家的 y，不加任何偏移，
也不读 walk_<map>.json 的格子高度或旧 ground_offset。
"""
from __future__ import annotations

import math


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _y_of(value):
    if isinstance(value, (tuple, list)) and len(value) >= 3 and _finite(value[1]):
        return float(value[1])
    return None


def placement_reference(placement):
    """目录 placement 里“本来想落哪”的高度（不是最终站位）。

    优先级：原生标记/授权锚点 > 工作台标注的真实几何点 > 录制 wire；
    这些值只当竖直选层的参考，最终 y 必须来自真实地形查询。
    """
    if not isinstance(placement, dict):
        return None
    prov = placement.get('provenance')
    prov = prov if isinstance(prov, dict) else {}
    native = prov.get('native_spawn')
    native = native if isinstance(native, dict) else {}
    annotation = prov.get('annotation')
    annotation = annotation if isinstance(annotation, dict) else {}
    for value in (
            _y_of(prov.get('authored_anchor')),
            _y_of(native.get('marker_position')),
            _y_of(native.get('ground_feet_position')),
            _y_of(annotation.get('geometry_position')),
            _y_of(annotation.get('feet_position')),
            _y_of(placement.get('wire_position')),
            _y_of(placement.get('feet_position')),
    ):
        if value is not None:
            return value
    return None


def placement_wire_reference(placement):
    """placement 的 Actor 原点高度（wire y），只用作“落点自己的高度”兜底。

    2026-11-07 T20：同图换落点/到达后重贴地时，如果目标点的地形 worker 还没
    ready，不能沿用旧位置的 y；必须用目标 placement 的 wire y（A 修好后它就是
    上层地板）。``placement_reference`` 仍返回锚点/标注的“选层参考”，两者不要
    混用：锚点可能比地板高 0.5~0.6 m，wire 才是 Actor 原点。
    """
    if not isinstance(placement, dict):
        return None
    wire = _y_of(placement.get('wire_position'))
    if wire is not None:
        return wire
    feet = _y_of(placement.get('feet_position'))
    return None if feet is None else feet + 0.5


def reference_candidates(*, leader_y=None, placement_ref=None, own_y=None, spawn_y=None,
                         wire_ref=None, order=('leader', 'placement', 'own', 'spawn')):
    """按给定优先级给出 [(label, y, is_player)]；先查地形，查不到才用玩家 y 兜底。

    默认“带路人优先”（跟随/贴身）。出生点还没被真实地形校正过时，调用方
    可以把 placement（目录/标注的意图高度）或者 ``wire_ref``（落点 Actor
    自己的原点高度，玩家下行坐标）放最前，避免带路人站在下层时把小光也
    带下去；已经贴地后则“自己当前 y”优先，保持所在楼层。

    2026-11-07 T22：``wire_ref`` 是落点种子/Actor 的真实原点（wire）高度。
    目录里的 ``annotation.geometry_position`` 是几何坐标，比 Actor 原点低
    0.5 m；遇境这种有多层地面的图，拿几何高度当 ``near_y`` 会选到下面一层，
    小光看起来像陷进地里。``wire_ref`` 和地形查询同基准，优先级高于
    placement 的几何参考。
    """
    table = {'leader': (leader_y, True), 'placement': (placement_ref, False),
             'own': (own_y, True), 'spawn': (spawn_y, True),
             'wire': (wire_ref, False)}
    rows = []
    for label in order:
        value, player = table[label]
        if not _finite(value):
            continue
        value = float(value)
        if any(abs(value - old) < 1e-6 for _l, old, _p in rows):
            continue
        rows.append((label, value, player))
    return rows


def choose_height(query, x, z, candidates, *, allow_player_fallback=True):
    """返回 (y, reference_label, source)。source=terrain / player_fallback / None。

    query 必须是 skyterrain 真实几何查询 ``query(x, z, near_y)``；不会退回
    走路格子、最低层或任何偏移。查不到几何时，只在 allow_player_fallback
    时用参考玩家的脚下 y（换图瞬间种子高度已由目录给出，不该被旧玩家 y 覆盖）。
    """
    for label, near_y, _player in candidates:
        try:
            value = query(x, z, near_y)
            if value is not None and _finite(value):
                return float(value), label, 'terrain'
        except Exception:
            continue
    if allow_player_fallback:
        for label, near_y, player in candidates:
            if player:
                return float(near_y), label, 'player_fallback'
    return None, None, None
