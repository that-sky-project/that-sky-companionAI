# -*- coding: utf-8 -*-
"""把一个“任务计划”里的多条 xg_gamedata 子命令顺序跑完，并逐行转发 JSON 进度。

  python gamedata_chain.py --kind data --plan C:\\...\\plan.json

plan 是 [[子命令, 参数...], ...]。每条子命令的 stdout/stderr 原样打到本进程 stdout，
control_api 从日志里解析每一行 JSON；最后一条 `{"ok":...}` 原样保留。
中间任何一条失败就停下，退出码 1；全部成功最后再补一条
`{"ok":true,"kind":...,"commands":N}`（如果最后一条已经是 ok:true 就不再重复）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

import xg_exec  # noqa: E402
from capture import gamedata  # noqa: E402


def run_plan(plan, kind):
    env = gamedata.env()
    for i, args in enumerate(plan, 1):
        print(json.dumps({"stage": "chain", "done": i - 1, "total": len(plan),
                          "eta_s": None, "msg": "开始：" + " ".join(map(str, args))},
                         ensure_ascii=False), flush=True)
        try:
            proc = xg_exec.popen(gamedata.command(args), cwd=str(APP), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            print(json.dumps({"ok": False, "stage": "chain",
                              "error": f"起不了 xg_gamedata：{type(exc).__name__}"},
                             ensure_ascii=False), flush=True)
            return 1
        final = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line.rstrip("\n"), flush=True)
                line = line.strip()
                if line.startswith("{"):
                    try:
                        doc = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(doc, dict) and "ok" in doc:
                        final = doc
            code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 130
        if final is None:
            print(json.dumps({"ok": False, "stage": "chain",
                              "error": "xg_gamedata 没有输出结果行"}, ensure_ascii=False), flush=True)
            return 1
        if not final.get("ok"):
            return 1
    # 最后一条子命令的 ok 行就是整条链的结果（保留 apk/path 等字段，别再补一条空结果）
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True)
    ap.add_argument("--plan", required=True)
    a = ap.parse_args(argv)
    try:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        if not isinstance(plan, list) or not plan or not all(isinstance(x, list) for x in plan):
            raise ValueError("plan 形状不对")
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"任务计划读不了：{type(exc).__name__}"},
                         ensure_ascii=False), flush=True)
        return 1
    return run_plan(plan, a.kind)


if __name__ == "__main__":
    raise SystemExit(main())
