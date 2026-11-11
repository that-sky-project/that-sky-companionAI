# -*- coding: utf-8 -*-
"""terrain：封装 W3 的 terrain_build（Python + Node，离线）。

布局与 W3b 一样：
  DATA/game/<版本>/terrain/assets/            资产目录
  DATA/game/<版本>/terrain/multimap-catalog.json
  DATA/game/<版本>/terrain/build-summary.json
中间目录在 assets 旁边（terrain/assets.work），可续跑。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import (GameDataError, Progress, final, game_dir, human_size, long_path,
                     resolve_data, xg_paths)
from .apk import inspect_apk
from . import spawn_loader
import xg_exec

HERE = Path(__file__).resolve().parent
TERRAIN_DIR = HERE / "terrain"
BUILDER = TERRAIN_DIR / "terrain_build.py"
PROGRESS_RE = re.compile(r"\[(?P<stage>[a-zA-Z_-]+)\s+(?P<done>\d+)/(?P<total>\d+)\]\s*(?P<msg>.*)")


def ensure_pipeline() -> None:
    if not BUILDER.is_file() or not (TERRAIN_DIR / "pipeline").is_dir():
        raise GameDataError("地形流水线文件缺失：%s（安装包不完整）" % TERRAIN_DIR, "terrain_pipeline_missing")
    node = shutil.which("node")
    if not node:
        raise GameDataError("没找到 Node.js。地形生成需要 Node（建议 20+），装好后重试。",
                            "node_missing")


def _maps(value: str | None) -> list[str] | None:
    if not value:
        return None
    out = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out or None


def run(apk: Path, data: Path, maps: list[str] | None, resume: bool, workers: int,
        progress: Progress) -> dict:
    ensure_pipeline()
    inspect_apk(apk, progress=None)
    root = game_dir(data) / "terrain"
    assets = root / "assets"
    catalog = root / "multimap-catalog.json"
    counted_maps = maps or sorted(spawn_loader.load()["placements"])
    summary = root / "build-summary.json"
    work = root / "assets.work"
    for d in (root, assets, work):
        os.makedirs(long_path(d), exist_ok=True)
    # W5：Node 地形引擎与 tempfile 只认 MAX_PATH。用 8.3 短路径把“深 DATA”
    # 映射成短名，实际文件仍落在用户的深目录里；短名取不到时交给长路径实现。
    xg = xg_paths()
    try:
        out_arg = xg.short_path(assets)
        # Only shorten the parent directory. Atomic replace via a file's
        # 8.3 alias can replace its long name with MULTIM~1.JSO on resume.
        output_root = Path(xg.short_path(root))
        catalog_arg = str(output_root / catalog.name)
        summary_arg = str(output_root / summary.name)
        work_arg = xg.short_path(work)
    except Exception:
        out_arg, catalog_arg, summary_arg, work_arg = map(str, (assets, catalog, summary, work))
    cmd = [sys.executable, "-X", "utf8", "-B", str(BUILDER),
           "--apk", str(apk), "--out", out_arg,
           "--catalog", catalog_arg, "--summary", summary_arg,
           "--work", work_arg, "--workers", str(workers),
           "--progress-maps", ",".join(counted_maps)]
    if maps:
        cmd += ["--maps", ",".join(maps)]
    if resume or (assets / "catalog.json").exists():
        cmd += ["--resume"]
    env = dict(os.environ)
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    started = time.monotonic()
    total = len(counted_maps)
    progress.emit("启动地形流水线（%s 张）…" % total, done=0, total=total, force=True)
    log_path = root / "terrain.log"
    with log_path.open("ab") as log:
        log.write(("\n$ " + " ".join(cmd) + "\n").encode("utf-8", "replace"))
        log.flush()
        proc = xg_exec.popen([str(x) for x in cmd], cwd=str(TERRAIN_DIR), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line.encode("utf-8", "replace"))
            log.flush()
            text = line.rstrip("\r\n")
            m = PROGRESS_RE.search(text)
            if m:
                done = int(m.group("done"))
                count = int(m.group("total")) or total
                stage = m.group("stage")
                progress.emit("%s %s" % (stage, m.group("msg").strip()), done=done,
                              total=count if not maps else min(count, total))
            elif text.startswith("[catalog]") or "ETA" in text:
                progress.emit(text[:180], done=progress.done, force=False)
        code = proc.wait()
    if code != 0:
        raise GameDataError("地形生成失败（退出码 %d），日志：%s" % (code, log_path),
                            "terrain_failed")
    if not catalog.is_file():
        raise GameDataError("地形跑完了但没有 multimap-catalog.json：%s" % catalog,
                            "terrain_no_catalog")
    doc = json.loads(catalog.read_text(encoding="utf-8"))
    entries = doc.get("entries") or []
    scenes = {row.get("scene_name") for row in entries if isinstance(row, dict)}
    missing = sorted(set(counted_maps) - scenes)
    if missing:
        raise GameDataError("地形清单缺少启用地图：%s" % ", ".join(missing),
                            "terrain_catalog_incomplete")
    enabled_count = len(set(counted_maps) & scenes)
    files = 0
    bytes_total = 0
    for dirpath, _dirnames, filenames in os.walk(long_path(assets)):
        for name in filenames:
            files += 1
            try:
                bytes_total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    progress.finish("地形完成：%d 张启用地图，%d 个文件" % (enabled_count, files))
    return {
        "ok": True, "terrain_dir": str(root), "assets": str(assets),
        "catalog": str(catalog), "catalog_sha256": _sha(catalog),
        "maps": enabled_count, "resource_maps": len(entries),
        "excluded_maps": len(scenes - set(counted_maps)), "files": files, "bytes": bytes_total,
        "bytes_human": human_size(bytes_total), "seconds": round(time.monotonic() - started, 1),
        "log": str(log_path), "summary": str(summary),
        "catalog_profile": doc.get("profile"),
        "matches_w3_v13": doc.get("profile") == "experimental_multimap_v13",
    }


def _sha(path: Path) -> str:
    from .common import sha256_file
    return sha256_file(path)


def cmd_terrain(args) -> int:
    progress = Progress("terrain", 0)
    try:
        apk = Path(args.apk).expanduser().resolve()
        data = resolve_data(args.data)
        result = run(apk, data, _maps(args.maps), args.resume, args.workers, progress)
        final(True, **{k: v for k, v in result.items() if k != "ok"})
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except OSError as exc:
        final(False, error="地形生成失败：%s" % exc, code="terrain_io")
        return 1
