"""Opaque same-input/same-map Actor progress; no room/ACK/authority state."""
from ._cw_bounds import LIFE as _CW_LIFE, STEPS as _CW_STEPS  # claudework
from dataclasses import dataclass,field
import math
import re

_SEAL=object()

@dataclass(frozen=True,repr=False,eq=False,slots=True)
class LocalIdle:
    _binding:tuple=field(repr=False)
    _origin:float|None
    _steps:int
    _observed_at:float
    _seal:object=field(repr=False)
    def __repr__(self):return '<map_long0167.LocalIdle local Actor progress RAM-only>'
    def __reduce_ex__(self,protocol):raise TypeError('map_long_idle_not_serializable')

def _finite(value):return type(value)in(int,float)and math.isfinite(value)and value>=0
def _validate(state):
    if type(state)is not LocalIdle or state._seal is not _SEAL:raise ValueError('map_long_idle_type')
    if type(state._binding)is not tuple or len(state._binding)!=8:raise ValueError('map_long_idle_binding')
    if type(state._steps)is not int or not 0<=state._steps<=_CW_STEPS:raise ValueError('map_long_idle_steps')
    if not _finite(state._observed_at):raise ValueError('map_long_idle_clock')
    if state._origin is None:
        if state._steps:raise ValueError('map_long_idle_missing_origin')
    elif(not _finite(state._origin)or state._origin>state._observed_at or
         state._observed_at-state._origin>_CW_LIFE or state._steps<1):raise ValueError('map_long_idle_bound')

def export_idle(binding,origin,steps,observed_at):
    value=LocalIdle(binding,origin,steps,observed_at,_SEAL);_validate(value);return value
def restore_idle(value,*,binding,now):
    _validate(value)
    if value._binding!=binding:raise ValueError('map_long_idle_scope_changed')
    if not _finite(now)or now<value._observed_at:raise ValueError('map_long_idle_restore_clock')
    if value._steps>=_CW_STEPS or(value._origin is not None and now-value._origin>_CW_LIFE):
        raise ValueError('map_long_idle_restore_bound')
    return value._origin,value._steps
