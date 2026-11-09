"""Opaque local idle progress only; not room authority, network or session state."""
from dataclasses import dataclass, field
import math

_SEAL=object()
_HOME=3526133726

@dataclass(frozen=True,repr=False,eq=False,slots=True)
class LocalIdle:
    _owner:bytes=field(repr=False)
    _map_id:int
    _scene_digest:str=field(repr=False)
    _actor_digest:str=field(repr=False)
    _actor_origin:float|None
    _revision_steps:int
    _observed_at:float
    _seal:object=field(repr=False)

    def __repr__(self):return '<long0167.LocalIdle neutral progress RAM-only>'
    def __reduce_ex__(self,protocol):raise TypeError('long0167_local_idle_not_serializable')

def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0

def _validate(state):
    if type(state) is not LocalIdle or state._seal is not _SEAL:
        raise ValueError('long0167_invalid_local_idle')
    if type(state._owner) is not bytes or len(state._owner)!=16 or not any(state._owner):
        raise ValueError('long0167_invalid_idle_owner')
    if type(state._map_id) is not int or state._map_id!=_HOME:
        raise ValueError('long0167_idle_home_only')
    if type(state._revision_steps) is not int or not 0<=state._revision_steps<=12000:
        raise ValueError('long0167_idle_revision_bound')
    if not _finite(state._observed_at):raise ValueError('long0167_idle_clock_invalid')
    if state._actor_origin is None:
        if state._revision_steps:raise ValueError('long0167_idle_origin_missing')
    elif (not _finite(state._actor_origin) or state._actor_origin>state._observed_at or
          state._observed_at-state._actor_origin>1200 or state._revision_steps<1):
        raise ValueError('long0167_idle_clock_bound')

def _export_idle(owner,map_id,scene_digest,actor_digest,origin,steps,observed_at):
    state=LocalIdle(owner,map_id,scene_digest,actor_digest,origin,steps,observed_at,_SEAL)
    _validate(state)
    return state

def _restore_idle(state,*,owner,map_id,scene_digest,actor_digest,now):
    _validate(state)
    if (state._owner!=owner or state._map_id!=map_id or state._scene_digest!=scene_digest or
            state._actor_digest!=actor_digest):
        raise ValueError('long0167_idle_binding_mismatch')
    if not _finite(now) or now<state._observed_at:
        raise ValueError('long0167_idle_restore_clock_regressed')
    if state._revision_steps>=12000 or (state._actor_origin is not None and now-state._actor_origin>1200):
        raise ValueError('long0167_idle_restore_bound')
    return state._actor_origin,state._revision_steps
