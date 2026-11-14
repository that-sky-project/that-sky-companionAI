"""道具 active 集的空中冷规划桥；不改变空中运动/所有权算法。"""
import math


def backend(adapter):
    queue=[adapter];seen=set()
    while queue and len(seen)<16:
        obj=queue.pop(0)
        if obj is None or id(obj) in seen:continue
        seen.add(id(obj))
        if callable(getattr(obj,'focus_placeables',None)):
            return obj
        queue.extend(getattr(obj,name,None) for name in ('_inner','backend','_backend'))
    return None


def selection(adapter):
    found=backend(adapter)
    return None if found is None else found.placeable_selection_revision


def focus(adapter,start,goal):
    found=backend(adapter)
    return True if found is None else found.focus_placeables(start,goal)


def covers(adapter,plan):
    found=backend(adapter)
    if found is None:return True
    before=found.placeable_selection_revision
    points=[]
    phases=plan.get('phases')
    if not isinstance(phases,(list,tuple)) or not 1<=len(phases)<=1024:return False
    for phase in phases:
        # 既有Trajectory仅有这两种模式，两者XZ均严格线性；端点段审查是连续的。
        if phase.get('mode') not in (None,'linear','ballistic'):return False
        for key in ('from','to'):
            point=phase.get(key)
            if not isinstance(point,(tuple,list)) or len(point)!=3:return False
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in point):return False
            points.append(tuple(point))
    return bool(found.placeable_path_covered(points) and before==found.placeable_selection_revision)
