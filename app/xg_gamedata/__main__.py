# -*- coding: utf-8 -*-
"""命令行入口：python -m xg_gamedata <子命令>。"""
from __future__ import annotations

import argparse
import sys

from .common import GameDataError, final


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m xg_gamedata",
        description="小光游戏数据本机生成（APK / MuMu 录制 → DATA）")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=None,
                        help="DATA 目录（默认 XG_DATA_DIR 或 xg_paths.DATA）")

    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-apk", parents=[common], help="校验国服 0.16.8/421629 APK")
    p.add_argument("--apk", required=True)
    p.set_defaults(func="check")

    p = sub.add_parser("pull-apk", parents=[common], help="从 MuMu 拉用户自己的安装包")
    p.add_argument("--adb", required=True)
    p.add_argument("--serial", default=None)
    p.set_defaults(func="pull")

    p = sub.add_parser("names", parents=[common], help="从 APK 生成名称表")
    p.add_argument("--apk", required=True)
    p.set_defaults(func="names")

    p = sub.add_parser("dictionary", parents=[common],
                       help="从 APK 提取 type-2 CompressDictionary.zst 到 DATA")
    p.add_argument("--apk", required=True)
    p.set_defaults(func="dictionary")

    p = sub.add_parser("terrain", parents=[common], help="从 APK 生成地形/导航资产")
    p.add_argument("--apk", required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--maps", default=None, help="只生成这些场景（逗号分隔；调试/验收用）")
    p.add_argument("--workers", type=int, default=1, choices=(1, 2))
    p.set_defaults(func="terrain")

    p = sub.add_parser("capture-register", parents=[common],
                       help="登记一次 MuMu 录制（按录制里的账号绑定认号）")
    p.add_argument("--dir", default=None, help="一次录制目录")
    p.add_argument("--pending", action="store_true",
                   help="补登记 current_profiles 里所有还没写进登记表的录制")
    p.set_defaults(func="capture")

    p = sub.add_parser("catalog", parents=[common],
                       help="用这个号自己的遇境真录生成 115 张按号目录")
    p.add_argument("--apk", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--donor", default=None, help="指定源录制目录名（默认最近一次遇境真录）")
    p.add_argument("--scene-inputs", default=None,
                   help="仅调试：指定 catalog-inputs 目录（默认 DATA/game/<版本>/catalog-inputs；"
                        "缺失/失效的条目会从 APK 现场生成）")
    p.set_defaults(func="catalog")

    p = sub.add_parser("status", parents=[common], help="哪些已完成")
    p.set_defaults(func="status")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "data", None):
            import os
            from pathlib import Path
            os.environ["XG_DATA_DIR"] = str(Path(args.data).expanduser().resolve())
        if args.func == "check":
            from .apk import cmd_check
            return cmd_check(args)
        if args.func == "pull":
            from .apk import cmd_pull
            return cmd_pull(args)
        if args.func == "names":
            from .names import cmd_names
            return cmd_names(args)
        if args.func == "dictionary":
            from .dictionary import cmd_dictionary
            return cmd_dictionary(args)
        if args.func == "terrain":
            from .terrain import cmd_terrain
            return cmd_terrain(args)
        if args.func == "capture":
            from .captures import cmd_capture_register
            return cmd_capture_register(args)
        if args.func == "catalog":
            from .catalog import cmd_catalog
            return cmd_catalog(args)
        if args.func == "status":
            from .status import cmd_status
            return cmd_status(args)
        raise GameDataError("未知子命令：%s" % args.func, "unknown_command")
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except KeyboardInterrupt:
        final(False, error="已中断；重新运行同一命令可以续跑。", code="interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - 给安装向导一行中文错误，不喷 traceback
        final(False, error="出错了：%s: %s" % (type(exc).__name__, str(exc)[:400]),
              code="unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
