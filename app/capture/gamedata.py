# -*- coding: utf-8 -*-
"""调用游戏数据生成包 `python -m xg_gamedata <子命令>`（W5 已把真实 xg_gamedata
并进 app；不再依赖替身）。

约定：每行一个 JSON 进度 `{"stage","done","total","eta_s","msg"}`，最后一行
`{"ok":true/false,...}`。这里只负责起子进程、收集 JSON 行、把最后一行当结果。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import xg_exec


def _extra_paths():
    import xg_paths
    cfg = (xg_paths.config() or {}).get("gamedata") or {}
    out = []
    for v in (os.environ.get("XG_GAMEDATA_PYTHONPATH"), cfg.get("pythonpath")):
        if v:
            out.extend(str(v).split(os.pathsep))
    for cand in (xg_paths.APP / "gamedata", xg_paths.DATA / "gamedata",
                 xg_paths.APP.parent, Path.home() / "xg_gamedata"):
        if cand.is_dir():
            out.append(str(cand))
    return out


def env():
    import xg_paths
    e = dict(os.environ)
    parts = _extra_paths() + [str(xg_paths.APP)]
    old = e.get("PYTHONPATH")
    if old:
        parts.append(old)
    e.update({"PYTHONPATH": os.pathsep.join(parts), "PYTHONUTF8": "1",
              "PYTHONIOENCODING": "utf-8", "SKY_AI_ROOT": str(xg_paths.ROOT),
              "XG_DATA_DIR": str(xg_paths.DATA)})
    return e


def python_exe():
    import xg_paths
    return xg_paths.python_exe()


def command(args):
    args = [str(a) for a in args]
    # 生产：直接用 app\xg_gamedata（python -m，cwd=APP）。
    # 测试/高级覆盖：显式指定了 XG_GAMEDATA_PYTHONPATH 或 config.gamedata.pythonpath
    # 时，用一个小 bootstrap 把覆盖目录放到 sys.path[0]；否则 cwd=APP 会让 app
    # 里的真包盖过覆盖目录（W4b 的假替身曾被这一点挡住）。
    import xg_paths
    cfg = (xg_paths.config() or {}).get("gamedata") or {}
    override = os.environ.get("XG_GAMEDATA_PYTHONPATH") or cfg.get("pythonpath")
    if override:
        parts = _extra_paths() + [str(xg_paths.APP)]
        code = ("import sys;sys.path[:0]=%r;"
                "from xg_gamedata.__main__ import main;"
                "raise SystemExit(main(sys.argv[1:]))" % parts)
        return [python_exe(), "-c", code, *args]
    return [python_exe(), "-m", "xg_gamedata", *args]


def parse_lines(text):
    rows, final = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        rows.append(doc)
        if "ok" in doc:
            final = doc
    return rows, final


def run(args, timeout=600, cwd=None):
    try:
        p = xg_exec.run(command(args), cwd=str(cwd or _app()), env=env(),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout)
        rows, final = parse_lines((p.stdout or "") + "\n" + (p.stderr or ""))
        if final is None:
            raise ValueError("xg_gamedata 没有返回结果：" + ((p.stdout or p.stderr or "")[-300:]))
        return {"ok": bool(final.get("ok")), "final": final, "rows": rows,
                "code": p.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "final": {"ok": False, "error": f"{type(exc).__name__}"},
                "rows": [], "code": -1}


def _app():
    import xg_paths
    return xg_paths.APP
