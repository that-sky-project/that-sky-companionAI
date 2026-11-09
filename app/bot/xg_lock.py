# -*- coding: utf-8 -*-
"""转发到 app/xg_lock.py（真正的实现在上一层，单一副本）。"""
import importlib.util as _u
import sys as _s
from pathlib import Path as _P

_src = _P(__file__).resolve().parent.parent / "xg_lock.py"
_name = "_xg_impl_" + _P(__file__).stem
if _name not in _s.modules:
    _spec = _u.spec_from_file_location(_name, _src)
    _mod = _u.module_from_spec(_spec)
    _s.modules[_name] = _mod
    _spec.loader.exec_module(_mod)
_impl = _s.modules[_name]
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
