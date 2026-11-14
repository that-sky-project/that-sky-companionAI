#!/usr/bin/env python3
"""One-command offline terrain/navigation asset builder for 小光 (Windows).

    python terrain_build.py --apk <APK> --out <assets-dir> [--maps A,B] [--resume]

The tool is deliberately self-contained: the Python parser and the Node
Rapier/Recast builders are bundled under ``pipeline/``.  It never launches the
game, never contacts a server, and never executes APK native code or Lua.
Only Python + numpy + Node are required.

Stages (matching the original development chain):

  1. base      APK -> all-scenes-v1/<Scene>/static-world + navigation
  2. bindings  conditional MeshDefs selection table (hashes/names only)
  3. world     base + Beamo instances -> beamo-world-all-v1/<Scene>
  4. nav       merged geometry -> beamo-navigation-all-v1/<Scene>
  5. on-demand descriptors for maps whose full nav is over budget
  6. assemble  only files the runtime reads -> <out> (release-v13 layout)

Every stage checkpoints to ``<out>.work``.  Re-running with ``--resume`` (or
simply providing the same --out again) continues where it stopped.
"""
from __future__ import annotations

import argparse
import atexit
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PIPELINE = HERE / "pipeline"
TOOLS = PIPELINE / "tools"
SRC = PIPELINE / "src"
ENGINE = PIPELINE / "engine"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(TOOLS))
for _p in Path(__file__).resolve().parents:
    if (_p / "xg_exec.py").is_file():
        sys.path.insert(0, str(_p))
        break
import xg_exec  # noqa: E402

from skyterrain.baked_terrain import PROFILE, ELF_SHA256  # noqa: E402
from skyterrain.catalog import Catalog, scene_hash  # noqa: E402

import build_all_maps as BAM  # noqa: E402
import build_beamo_navigation as BNAV  # noqa: E402
import build_beamo_world as BW  # noqa: E402
import compose_beamo_catalog as CBC  # noqa: E402
import prepare_beamo_bindings as PB  # noqa: E402

try:  # optional, only used for peak-memory reporting
    import psutil
except Exception:  # pragma: no cover
    psutil = None

SCHEMA = "skyterrain-user-terrain-build-v1"
FLAGS = dict(native_collision_verified=False, calibration_verified=False, runtime_ready=False)
# Final on-disk layout is pinned to the server's release-v13 asset tree.
# The runtime catalog (release-v20) is a sibling of the assets directory, not a
# member of it: the asset file set below contains exactly the files the engine
# reads, and nothing else.
CATALOG_PROFILE = "experimental_multimap_v13"
CATALOG_SHA256_V13 = "d6129a6a0be79dfce1536a4bfdc2983cf570398e3f80a6ef3d672ee683e5e5f9"
# Reference count only: a valid on-demand descriptor replaces two nav files.
EXPECTED_FULL_ASSET_FILES = 697
CANONICAL_REPORT_BUNDLE = PIPELINE / "nav-reports-v13.json"
GALLERY_SCENE = "Night_GO_Gallery"
CANDLE_SCENE = "CandleSpace"
EMPTY_REASON = {
    "WorldEmpty": "hash_bound_LOD_only_empty_scene_no_initial_geometry",
    "AR_TestLevel": "no_initial_static_collision_geometry",
}
ON_DEMAND_POLICY = collections.OrderedDict(
    windowMeters=64, gridMeters=32, cacheEntries=3, maxRouteMeters=8)
DEFAULTS = collections.OrderedDict(
    radius=0.25, height=1.5, skin=0.015, stepHeight=0.3, slopeDegrees=42,
    snapGround=0.25, gravity=18, maxFallSpeed=30, fixedDt=1 / 60,
    maxElapsed=0.25, speed=3.5, goalTolerance=0.12, maxProjection=0.4,
    cellSize=0.15, cellHeight=0.075)
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SCENE_MESH = re.compile(r"assets/Data/Levels/([A-Za-z0-9_-]{1,80})/BstBaked\.meshes\Z")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(chunk):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path):
    raw = Path(path).read_bytes()
    return json.loads(raw), raw


def _abs(path):
    """绝对化但保留 8.3 短名（Path.resolve 会把短名展开成长路径）。"""
    return Path(os.path.abspath(os.fspath(path)))


def _long_path(path, force=False):
    """W5：Windows 长路径前缀。优先用 app/xg_paths.py 的统一实现。"""
    try:
        from xg_paths import long_path  # noqa: PLC0415
        return long_path(path, force=force)
    except Exception:
        p = os.fspath(path)
        if os.name != "nt" or not p or p.startswith("\\\\?\\"):
            return p
        p = p.replace("/", "\\")
        if not os.path.isabs(p):
            p = os.path.abspath(p)
        if not force and len(p) < 240:
            return p
        return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)


def _output_path(path):
    """Keep short parents, but never atomically replace a file through its 8.3 alias."""
    path = _abs(path)
    if os.name == "nt" and path.exists():
        path = path.with_name(path.resolve().name)
    return path


def atomic_bytes(path: Path, data: bytes):
    path = _output_path(path)
    os.makedirs(_long_path(path.parent), exist_ok=True)
    tmp = path.with_name(path.name + ".t")
    with open(_long_path(tmp), "wb") as fh:
        fh.write(data)
    os.replace(_long_path(tmp), _long_path(path))


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def write_bytes_if_changed(path: Path, data: bytes):
    path = Path(path)
    try:
        if os.path.isfile(_long_path(path)) and Path(_long_path(path)).read_bytes() == data:
            return
    except OSError:
        pass
    atomic_bytes(path, data)


_CANONICAL_REPORTS = None


def load_canonical_reports():
    """Load the small bundled table of canonical v13 nav-build reports.

    These are *build reports* (counts, config, hashes, elapsed milliseconds),
    not geometry, mesh or Lua data.  The release-v20 catalog pins each report by
    SHA-256, so reproducing the catalog byte-for-byte requires the same report
    bytes.  A fresh report from this machine would differ only in ``buildMs``
    (and a few newer informational keys), which is why it cannot satisfy the
    server's pin even when the navigation binary is byte-identical.
    """
    global _CANONICAL_REPORTS
    if _CANONICAL_REPORTS is not None:
        return _CANONICAL_REPORTS
    if not CANONICAL_REPORT_BUNDLE.is_file():
        raise RuntimeError(f"缺少随包分发的规范报告表: {CANONICAL_REPORT_BUNDLE}")
    doc, _ = read_json(CANONICAL_REPORT_BUNDLE)
    if doc.get("schema") != "skyterrain-canonical-nav-reports-v1":
        raise RuntimeError("规范报告表 schema 不符")
    reports = []
    for sha, text in doc.get("reports", {}).items():
        raw = text.encode("utf-8")
        if sha_bytes(raw) != sha:
            raise RuntimeError(f"规范报告表哈希不符: {sha}")
        reports.append((sha, raw, json.loads(raw)))
    if len(reports) != doc.get("report_count"):
        raise RuntimeError("规范报告表数量不符")
    _CANONICAL_REPORTS = reports
    return reports


def select_canonical_report(reports, name: str, proof: dict, geometry_kind: str,
                            nav_bin: Path):
    """Pick the report whose bytes the v13 catalog expects for this artifact.

    Matching is by content identity, not by scene/path hardcoding: map name,
    geometry kind, generated nav hash/size/file name and input hashes must all
    agree.  This also prevents silently reusing a report for changed geometry.
    """
    nav_sha = sha_file(nav_bin)
    nav_size = nav_bin.stat().st_size
    nav_name = nav_bin.name
    desired_config = dict(DEFAULTS)
    for sha, raw, report in reports:
        try:
            if report.get("format") != "skyterrain-navigation-build-v1":
                continue
            if report.get("mode") != "offline" or report.get("status") != "built_offline":
                continue
            if report.get("mapName") != name:
                continue
            if report.get("geometryKind") != geometry_kind:
                continue
            if report.get("navigationHash") != nav_sha:
                continue
            if report.get("navigationFile") != nav_name:
                continue
            if report.get("navigationBytes") != nav_size:
                continue
            if report.get("inputHashes") != proof["hashes"]:
                continue
            if report.get("inputVertices") != proof["vertices"]:
                continue
            if report.get("inputTriangles") != proof["triangles"]:
                continue
            if report.get("config") != desired_config:
                continue
            if any(report.get(k) is not False for k in
                   ("nativeCollisionVerified", "calibrationVerified", "runtimeReady")):
                continue
            return sha, raw, report
        except Exception:
            continue
    return None


def prune_unexpected(root: Path, keep_relative) -> int:
    """Delete files (and then empty dirs) that are not in the pinned file set."""
    root = _abs(root)
    keep = {_abs(root / Path(rel)) for rel in keep_relative}
    files = [p for p in root.rglob("*") if p.is_file()]
    removed = 0
    for path in files:
        target = _abs(path)
        if not target.is_relative_to(root):
            continue
        if target not in keep:
            target.unlink()
            removed += 1
    dirs = [p for p in root.rglob("*") if p.is_dir()]
    for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
    return removed


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def fmt_seconds(value):
    value = max(0.0, float(value or 0))
    if value < 60:
        return f"{value:.0f}s"
    if value < 3600:
        return f"{value / 60:.1f}m"
    return f"{value / 3600:.2f}h"


def log(message: str):
    print(message, flush=True)


class StageProgress:
    def __init__(self, name: str, total: int, counted_maps=None):
        self.name, self.total, self.done = name, total, 0
        self.counted_maps = None if counted_maps is None else frozenset(counted_maps)
        self.completed_maps = set()
        self.started = time.monotonic()
        self._last = 0.0

    def update(self, label: str, done: int | None = None, force: bool = False):
        if done is not None:
            self.done = done
        self.done = min(self.done, self.total)
        display_done, display_total = self.done, self.total
        if self.counted_maps is not None:
            scene = label.split(" ", 1)[0]
            if scene in self.counted_maps:
                self.completed_maps.add(scene)
            if self.done == self.total:
                self.completed_maps.update(self.counted_maps)
            display_done, display_total = len(self.completed_maps), len(self.counted_maps)
        current = time.monotonic()
        if not force and self.done and current - self._last < 0.4:
            return
        self._last = current
        elapsed = current - self.started
        rate = elapsed / display_done if display_done else 0.0
        eta = rate * (display_total - display_done)
        log(f"[{self.name} {display_done}/{display_total}] {label}  "
            f"elapsed {fmt_seconds(elapsed)}  ETA {fmt_seconds(eta)}")


class MemoryMonitor(threading.Thread):
    """Sample this process tree's RSS; the pipeline is a tree of short subprocesses."""

    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()
        self.peak_total = 0
        self.peak_single = 0
        self.peak_label = None
        self.samples = 0
        self.error = None if psutil is not None else "psutil_not_installed"
        self._proc = None

    def run(self):
        if psutil is None:
            return
        try:
            self._proc = psutil.Process()
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}:{exc}"
            return
        while not self.stop_event.wait(self.interval):
            try:
                procs = [self._proc] + self._proc.children(recursive=True)
                total = 0
                biggest = 0
                label = None
                for proc in procs:
                    try:
                        rss = proc.memory_info().rss
                    except Exception:
                        continue
                    total += rss
                    if rss > biggest:
                        biggest = rss
                        label = proc.name()
                self.samples += 1
                if total > self.peak_total:
                    self.peak_total = total
                if biggest > self.peak_single:
                    self.peak_single = biggest
                    self.peak_label = label
            except Exception:
                continue

    def stop(self):
        self.stop_event.set()
        self.join(timeout=3)


class Tee:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str):
        with self.path.open("a", encoding="utf-8", errors="replace") as stream:
            stream.write(text)


def run_stream(cmd, log_path: Path, on_line=None, env=None, cwd=PIPELINE):
    """Run a child, tee combined output, and feed each line to on_line."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("ab") as stream:
        stream.write(("\n$ " + " ".join(str(x) for x in cmd) + "\n").encode("utf-8"))
        stream.flush()
        proc = xg_exec.popen(
            [str(x) for x in cmd], cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            stream.write(line.encode("utf-8", "replace"))
            stream.flush()
            if on_line is not None:
                try:
                    on_line(line.rstrip("\r\n"))
                except Exception:
                    pass
        code = proc.wait()
    return code, time.monotonic() - started


def remove_tree(path: Path, root: Path):
    """Delete a path only after proving it is strictly inside our work root."""
    path, root = _abs(path), _abs(root)
    if path == root or not path.is_relative_to(root):
        raise RuntimeError(f"refusing to delete outside work root: {path}")
    if path.is_symlink():
        path.unlink()
    elif os.path.exists(_long_path(path)):
        shutil.rmtree(_long_path(path, force=True))


# --------------------------------------------------------------------------- #
# preflight / APK
# --------------------------------------------------------------------------- #
def apk_inventory(apk: Path):
    """Return (scene names, profile evidence, archive identity).  Read-only."""
    apk = _abs(apk)
    if not apk.is_file():
        raise ValueError(f"APK 不存在: {apk}")
    with zipfile.ZipFile(apk) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError("APK 内存在重复成员，拒绝继续")
        scenes = sorted(m.group(1) for n in names if (m := SCENE_MESH.fullmatch(n)))
        if not scenes:
            raise ValueError("APK 内没有 assets/Data/Levels/*/BstBaked.meshes")
        for name in scenes:
            parent = f"assets/Data/Levels/{name}"
            for suffix in ("Objects.level.bin", "Resources.lua.luac"):
                if f"{parent}/{suffix}" not in names:
                    raise ValueError(f"场景 {name} 缺少 {suffix}")
        try:
            profile = BW.verify_profile(z)
        except ValueError as exc:
            raise ValueError(
                "APK 版本校验失败：lib/arm64-v8a/libBootloader.so 的 SHA-256 与 "
                f"{PROFILE} 不符（{exc}）。本工具接受国服 0.16.8 / 421629（也识别 0.16.7 / 421589 的旧数据）。") from exc
        digest = hashlib.sha256()
        with apk.open("rb") as stream:
            while block := stream.read(8 * 1024 * 1024):
                digest.update(block)
        return scenes, profile, {"sha256": digest.hexdigest(), "bytes": apk.stat().st_size}
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- #
# stage 1: base geometry + base navigation
# --------------------------------------------------------------------------- #
def base_stage(apk: Path, work: Path, maps: list[str], all_scenes: list[str],
               node: str, resume: bool, timeout: int, summary: dict, progress: StageProgress):
    out = work / "all-scenes-v1"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", "-B", TOOLS / "build_all_maps.py",
           "--apk", apk, "--out", out, "--node", node, "--timeout", str(timeout)]
    if set(maps) != set(all_scenes):
        cmd += ["--maps", *maps]
    if (out / "catalog.json").exists():
        cmd += ["--resume"]
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8", SKYTERRAIN_REQUIRED_ONLY="1")

    def line(text):
        try:
            obj = json.loads(text)
        except Exception:
            return
        if isinstance(obj, dict) and "scene" in obj and "index" in obj:
            scene = obj["scene"]
            done = maps.index(scene) + 1 if scene in maps else int(obj["index"])
            progress.update(f"{scene} {obj.get('status') or obj.get('nav') or ''}", done=done)
        elif isinstance(obj, dict) and "catalog" in obj:
            log(f"[base] catalog: {obj.get('summary')}")

    log(f"[base] {len(maps)} 张图 -> {out}")
    code, seconds = run_stream(cmd, work / "logs" / "base.log", line, env)
    summary["stages"]["base"] = {"seconds": seconds, "exit_code": code}
    if code != 0:
        raise RuntimeError(f"base 阶段失败，见 {work / 'logs' / 'base.log'}")
    catalog, _ = read_json(out / "catalog.json")
    by_name = {row["scene_name"]: row for row in catalog["entries"]}
    for name in maps:
        row = by_name[name]
        item = summary["maps"].setdefault(name, {"scene": name, "stages": {}})
        item["read_status"] = row.get("read_status")
        item["collision_status"] = row.get("collision_status")
        item["base_nav_status"] = row.get("nav_status")
        if row.get("build_run"):
            item["stages"]["base_build_s"] = round(row["build_run"]["seconds"], 3)
        if row.get("nav_run"):
            item["stages"]["base_nav_s"] = round(row["nav_run"]["seconds"], 3)
        item["reason"] = row.get("reason")
        save(summary)
    progress.update("base 完成", done=progress.total, force=True)
    return catalog


# --------------------------------------------------------------------------- #
# stage 2: bindings (automatic conditional selection, no hand picking)
# --------------------------------------------------------------------------- #
def bindings_stage(apk: Path, work: Path, maps: list[str], all_scenes: list[str],
                   summary: dict):
    target = work / "bindings"
    config_path = target / "bindings.json"
    needed = set(maps)
    if config_path.exists():
        try:
            doc, _ = read_json(config_path)
            if doc.get("profile") == PROFILE and needed <= set(doc.get("scenes", {})):
                log(f"[bindings] 复用已有选择表（{len(doc['scenes'])} 场景）")
                summary["stages"]["bindings"] = {"reused": True,
                                                 "scenes": len(doc["scenes"])}
                return config_path
        except Exception:
            pass
    tmp = work / f".bindings-{os.getpid()}-{time.time_ns()}"
    if tmp.exists():
        remove_tree(tmp, work)
    log(f"[bindings] 从 APK 生成条件选择表（{len(maps)} 场景）...")
    started = time.monotonic()
    chosen = None if needed == set(all_scenes) else maps
    result = PB.prepare(apk, tmp, chosen)
    seconds = time.monotonic() - started
    if target.exists():
        remove_tree(target, work)
    tmp.rename(target)
    summary["stages"]["bindings"] = {
        "seconds": seconds, "scenes": result["map_count"],
        "selected_instances": result["selected_instances"],
        "beamo_instances": result["beamo_instances"],
        "runtime_binding_verified": False,
        "note": "conditional plan; no live registry observed",
    }
    save(summary)
    log(f"[bindings] {result['map_count']} 场景，选中 {result['selected_instances']}/"
        f"{result['beamo_instances']} 个实例；选择表只含路径+SHA，不含游戏数据")
    return config_path


# --------------------------------------------------------------------------- #
# stage 3: Beamo merged world (resumable, exact reference candidate shape)
# --------------------------------------------------------------------------- #
def valid_world(dest: Path, name: str, row: dict, base_root: Path) -> bool:
    try:
        report, _ = read_json(dest / "build.json")
        if report != row:
            return False
        status = row.get("status")
        if status == "no_beamo":
            return row.get("beamo_objects") == 0 and row.get("geometry_written") is False
        if status not in ("built_candidate", "partial_candidate"):
            return False
        if row.get("geometry_written") is not True:
            return False
        manifest_path = dest / "static-world" / "runtime-manifest.json"
        manifest, raw = read_json(manifest_path)
        if manifest.get("format") != "skyterrain-static-v1" or manifest.get("mapName") != name:
            return False
        if manifest.get("geometryKind") != "offline_beamo_augmented_candidates":
            return False
        for key, count_key, dtype in (("positions", "vertexCount", "<f4"),
                                      ("indices", "triangleCount", "<u4")):
            count = manifest.get(count_key)
            data = (manifest_path.parent / manifest[key]).read_bytes()
            if not isinstance(count, int) or len(data) != count * 12:
                return False
            if sha_bytes(data) != manifest["sha256"][key]:
                return False
            np.frombuffer(data, dtype=dtype)
        evidence = manifest["evidenceManifest"]
        ev = manifest_path.parent / evidence["file"]
        if sha_file(ev) != evidence["sha256"]:
            return False
        base_manifest = base_root / name / "static-world" / "runtime-manifest.json"
        if sha_file(base_manifest) != row["base_actual_byte_binding"]["runtime_manifest_sha256"]:
            return False
        return True
    except Exception:
        return False


def world_stage(apk: Path, work: Path, bindings_path: Path, base_root: Path,
                maps: list[str], all_scenes: list[str], summary: dict,
                progress: StageProgress):
    doc, bindings_sha = BW.load_binding_config(bindings_path)
    root = work / "beamo-world-all-v1"
    root.mkdir(parents=True, exist_ok=True)
    previous = {}
    summary_path = root / "summary.json"
    if summary_path.exists():
        try:
            old, _ = read_json(summary_path)
            if old.get("schema") == "explicit-beamo-world-build-v1" and old.get("bindings_sha256") == bindings_sha:
                previous = {row["mapName"]: row for row in old.get("rows", [])}
            else:
                log("[world] 选择表已变化，旧 world 输出全部重建")
        except Exception:
            previous = {}

    with zipfile.ZipFile(apk) as z:
        profile = BW.verify_profile(z)
        names = set(z.namelist())
        clock = time.monotonic()
        for idx, name in enumerate(maps, 1):
            dest = root / name
            old = previous.get(name)
            if old and valid_world(dest, name, old, base_root):
                item = summary["maps"].setdefault(name, {"scene": name, "stages": {}})
                item["stages"]["world_s"] = item.get("stages", {}).get("world_s", 0)
                progress.update(f"{name} 已存在，跳过", done=idx)
                continue
            if name not in doc["scenes"]:
                raise RuntimeError(f"bindings 缺少场景 {name}")
            if dest.exists():
                remove_tree(dest, root)
            dest.mkdir()
            started = time.monotonic()
            try:
                row = BW._build_one(z, name, doc["scenes"][name], base_root, dest)
            except Exception as exc:
                row = {"mapName": name, "status": "failed", "error": type(exc).__name__,
                       "detail": str(exc)[:1000], "geometry_written": False,
                       "runtimeReady": False, "nativeCollisionVerified": False,
                       "calibrationVerified": False}
            seconds = time.monotonic() - started
            atomic_json(dest / "build.json", row)
            # The nav stage requires a 64-hex scene hash even for failed rows.
            row.setdefault("scene_sha256", "0" * 64)
            previous[name] = row
            item = summary["maps"].setdefault(name, {"scene": name, "stages": {}})
            item["stages"]["world_s"] = round(seconds, 3)
            item["world_status"] = row.get("status")
            item["world_reason"] = row.get("reason") or row.get("detail")
            progress.update(f"{name} {row.get('status')}", done=idx)
            save(summary)
            write_world_summary(root, all_scenes, previous, bindings_sha, profile, doc)
        # Rows outside the requested selection are recorded as not selected.
        for name in all_scenes:
            if name not in previous:
                previous[name] = {
                    "mapName": name, "status": "failed", "reason": "not_selected",
                    "geometry_written": False, "scene_sha256": "0" * 64,
                    "runtimeReady": False, "nativeCollisionVerified": False,
                    "calibrationVerified": False,
                }
        write_world_summary(root, all_scenes, previous, bindings_sha, profile, doc)
    summary["stages"]["world"] = {"seconds": time.monotonic() - clock}
    save(summary)
    return summary_path


def write_world_summary(root: Path, all_scenes: list[str], rows: dict,
                        bindings_sha: str, profile: dict, doc: dict):
    ordered = [rows[name] for name in all_scenes if name in rows]
    statuses = collections.Counter(row.get("status") for row in ordered)
    result = {
        "schema": "explicit-beamo-world-build-v1",
        "profile": PROFILE,
        "native_profile_input": profile,
        "bindings_sha256": bindings_sha,
        "map_count": len(ordered),
        "statuses": dict(statuses),
        "rows": ordered,
        "runtimeReady": False,
        "nativeCollisionVerified": False,
        "calibrationVerified": False,
        "original_catalog_modified": False,
        "navigation_built": False,
        "server_modified": False,
        "module_sha256": sha_file(PIPELINE / "src" / "skyterrain" / "beamo_assembly.py"),
        "tool_sha256": sha_file(PIPELINE / "tools" / "build_beamo_world.py"),
    }
    target = root / "summary.json"
    raw = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    try:
        if target.exists() and target.read_bytes() == raw:
            return
    except OSError:
        pass
    atomic_bytes(target, raw)


# --------------------------------------------------------------------------- #
# stage 4: navigation on merged geometry
# --------------------------------------------------------------------------- #
def nav_resume_compatible(old: dict, world_summary: Path, base_catalog: Path) -> bool:
    """True when every completed nav row still binds the same merged asset.

    The batch summary also stores file-level hashes of its two input JSONs.
    Those files are legitimately rewritten on every resume (same semantic
    content, new checkpoint), so comparing the JSON bytes would needlessly
    rebuild all navigation.  Per-map asset hashes are the real binding.
    """
    try:
        world, _ = read_json(world_summary)
        base, _ = read_json(base_catalog)
        old_rows = old.get("rows")
        if not isinstance(old_rows, list):
            return False
        names = {row["mapName"] for row in world.get("rows", [])}
        if {row.get("mapName") for row in old_rows} != names:
            return False
        world_by = {row["mapName"]: row for row in world["rows"]}
        base_by = {row["scene_name"]: row for row in base["entries"]}
        for row in old_rows:
            if row.get("status") != "built_offline":
                continue
            name = row["mapName"]
            current = world_by.get(name, {})
            if current.get("status") not in ("built_candidate", "partial_candidate"):
                return False
            manifest = world_summary.parent / name / "static-world" / "runtime-manifest.json"
            if row.get("input_manifest_sha256") != sha_file(manifest):
                return False
            if name not in base_by:
                return False
        return True
    except Exception:
        return False


def nav_stage(work: Path, world_summary: Path, base_catalog: Path, maps: list[str],
              all_scenes: list[str], node: str, workers: int, summary: dict,
              progress: StageProgress):
    root = work / "beamo-navigation-all-v1"
    resume = (root / "summary.json").exists()
    if resume:
        try:
            old, _ = read_json(root / "summary.json")
            if nav_resume_compatible(old, world_summary, base_catalog):
                # Refresh only the global input pins; per-map completed rows
                # keep their verified artifacts and are revalidated by the
                # batch's own resume path.
                old["input_summary_sha256"] = sha_file(world_summary)
                old["base_catalog_sha256"] = sha_file(base_catalog)
                atomic_json(root / "summary.json", old)
            else:
                log("[nav] 输入清单已变化，重建导航目录")
                remove_tree(root, work)
                resume = False
        except Exception:
            remove_tree(root, work)
            resume = False
    if not resume and root.exists():
        remove_tree(root, work)
    cmd = [sys.executable, "-X", "utf8", "-B", TOOLS / "build_beamo_navigation.py",
           "--world-summary", world_summary, "--base-catalog", base_catalog,
           "--out", root, "--node", node, "--workers", str(workers)]
    if set(maps) != set(all_scenes):
        cmd += ["--maps", *maps]
    if resume:
        cmd += ["--resume"]
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8", SKYTERRAIN_REQUIRED_ONLY="1")
    counter = {"done": 0}

    def line(text):
        try:
            obj = json.loads(text)
        except Exception:
            return
        if isinstance(obj, dict) and "mapName" in obj and "status" in obj:
            counter["done"] += 1
            name = obj["mapName"]
            item = summary["maps"].setdefault(name, {"scene": name, "stages": {}})
            item["nav_status"] = obj["status"]
            item["nav_reason"] = obj.get("reason")
            progress.update(f"{name} {obj['status']}", done=counter["done"])

    log(f"[nav] {len(maps)} 张图，workers={workers} -> {root}")
    code, seconds = run_stream(cmd, work / "logs" / "nav.log", line, env)
    summary["stages"]["nav"] = {"seconds": seconds, "exit_code": code}
    if not (root / "summary.json").exists():
        raise RuntimeError(f"nav 阶段失败，见 {work / 'logs' / 'nav.log'}")
    old, _ = read_json(root / "summary.json")
    for row in old.get("rows", []):
        name = row["mapName"]
        item = summary["maps"].setdefault(name, {"scene": name, "stages": {}})
        item["nav_status"] = row.get("status")
        item["nav_reason"] = row.get("reason")
        if row.get("run"):
            item["stages"]["nav_s"] = round(row["run"]["seconds"], 3)
        if row.get("nav_vertices"):
            item["nav_vertices"] = row["nav_vertices"]
    save(summary)
    progress.update("nav 完成", done=progress.total, force=True)
    return root


# --------------------------------------------------------------------------- #
# stage 5: on-demand descriptors (short-range fallback for over-budget maps)
# --------------------------------------------------------------------------- #
def make_descriptor(asset_manifest: Path, destination: Path, name: str) -> dict:
    runtime, runtime_raw = read_json(asset_manifest)
    evidence_raw = (asset_manifest.parent / runtime["evidenceManifest"]["file"]).read_bytes()
    evidence = json.loads(evidence_raw)
    if sha_bytes(evidence_raw) != runtime["evidenceManifest"]["sha256"]:
        raise ValueError(f"descriptor evidence hash mismatch: {name}")
    positions = (asset_manifest.parent / runtime["positions"]).read_bytes()
    indices = (asset_manifest.parent / runtime["indices"]).read_bytes()
    if sha_bytes(positions) != runtime["sha256"]["positions"]:
        raise ValueError(f"descriptor positions hash mismatch: {name}")
    if sha_bytes(indices) != runtime["sha256"]["indices"]:
        raise ValueError(f"descriptor indices hash mismatch: {name}")
    desc = collections.OrderedDict([
        ("format", "skyterrain-navigation-on-demand-v1"),
        ("mode", "offline"),
        ("status", "on_demand_static_candidate"),
        ("mapName", name),
        ("geometryKind", runtime["geometryKind"]),
        ("inputHashes", collections.OrderedDict([
            ("manifest", sha_bytes(runtime_raw)),
            ("positions", runtime["sha256"]["positions"]),
            ("indices", runtime["sha256"]["indices"]),
        ])),
        ("inputVertices", runtime["vertexCount"]),
        ("inputTriangles", runtime["triangleCount"]),
        ("config", DEFAULTS),
        ("navigationCoverage", "on_demand_static_candidate"),
        ("navigationWindow", None),
        ("policy", ON_DEMAND_POLICY),
        ("nativeCollisionVerified", False),
        ("calibrationVerified", False),
        ("runtimeReady", False),
    ])
    atomic_bytes(destination, json.dumps(desc, ensure_ascii=False, indent=2).encode("utf-8"))
    return desc


# --------------------------------------------------------------------------- #
# stage 6: final assembly and catalog
# --------------------------------------------------------------------------- #
def copy_verified(source: Path, destination: Path) -> tuple[int, str]:
    source, destination = Path(source), Path(destination)
    os.makedirs(_long_path(destination.parent), exist_ok=True)
    if os.path.exists(_long_path(destination)) and os.path.getsize(
            _long_path(destination)) == os.path.getsize(_long_path(source)):
        if sha_file(Path(_long_path(destination))) == sha_file(Path(_long_path(source))):
            return os.path.getsize(_long_path(destination)), sha_file(Path(_long_path(destination)))
    tmp = destination.with_name(destination.name + ".t")
    shutil.copyfile(_long_path(source), _long_path(tmp))
    os.replace(_long_path(tmp), _long_path(destination))
    size = os.path.getsize(_long_path(destination))
    return size, sha_file(Path(_long_path(destination)))



def verify_assembled_files(out: Path, expected: set[str]) -> int:
    """Validate this build's exact file set, including legitimate nav fallbacks."""
    actual = {path.relative_to(out).as_posix()
              for path in out.rglob("*") if path.is_file()}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise RuntimeError(
            f"组装后文件集不完整：缺少 {len(missing)} 个，多出 {len(unexpected)} 个；"
            f"缺少示例 {missing[:8]}；多出示例 {unexpected[:8]}")
    return len(actual)


def static_files(manifest_path: Path) -> list[str]:
    manifest, _ = read_json(manifest_path)
    names = ["runtime-manifest.json", "manifest.json",
             manifest["positions"], manifest["indices"],
             manifest["evidenceManifest"]["file"]]
    # "manifest.json" is both the evidence filename and a fixed name; copy once.
    return list(dict.fromkeys(names))


def assemble(work: Path, out: Path, base_root: Path, world_root: Path,
             nav_root: Path, ondemand_root: Path, base_catalog: dict,
             world_summary: dict, nav_summary: dict, maps: list[str],
             all_scenes: list[str], summary: dict, progress: StageProgress,
             catalog_path: Path, fresh_reports: bool = False):
    out.mkdir(parents=True, exist_ok=True)
    full_run = set(maps) == set(all_scenes)
    base_rows = {row["scene_name"]: row for row in base_catalog["entries"]}
    world_rows = {row["mapName"]: row for row in world_summary["rows"]}
    nav_rows = {row["mapName"]: row for row in nav_summary["rows"]}
    reports = [] if fresh_reports else load_canonical_reports()
    canonical_fallbacks = []
    entries = []
    expected = set()

    def keep(path: Path):
        expected.add(path.relative_to(out).as_posix())
        return path

    for idx, name in enumerate(maps, 1):
        base = base_rows[name]
        item = summary["maps"].setdefault(name, {"scene": name, "stages": {}})
        started = time.monotonic()
        if base.get("read_status") == "empty_scene" or base.get("collision_status") == "empty_scene":
            row = {
                "scene_name": name, "scene_hash": base["scene_hash"],
                "read_status": base.get("read_status", "empty_scene"),
                "collision_status": "empty_scene", "nav_status": "empty_scene",
                "asset_relpath": None, "collision_coverage": "initial_static_candidates",
                "native_collision_verified": False, "calibration_verified": False,
                "runtime_ready": False, "geometry_source": "unchanged_base",
                "beamo_build_status": "no_beamo", "beamo_included": 0,
                "beamo_excluded": 0,
                "reason": EMPTY_REASON.get(
                    name, base.get("reason") or "no_initial_static_collision_geometry"),
            }
            entries.append(row)
            item["status"] = "empty"
            item["stages"]["assemble_s"] = round(time.monotonic() - started, 3)
            progress.update(f"{name} empty", done=idx)
            save(summary)
            continue
        world = world_rows.get(name, {})
        reuse_base = world.get("status") == "no_beamo"
        if reuse_base:
            asset_src = base_root / name / "static-world"
            asset_prefix = "all-scenes-v1"
            geometry_source = "unchanged_base"
            collision_coverage = "initial_static_candidates"
        else:
            if world.get("status") not in ("built_candidate", "partial_candidate"):
                raise RuntimeError(f"{name}: world 输出不可用（{world.get('status')} {world.get('reason')}）")
            asset_src = world_root / name / "static-world"
            asset_prefix = "beamo-world-all-v1"
            geometry_source = "beamo_world"
            collision_coverage = "beamo_augmented_static_candidates"
        asset_manifest = asset_src / "runtime-manifest.json"
        if not asset_manifest.is_file():
            raise RuntimeError(f"{name}: 缺少 {asset_manifest}")
        asset_dst_dir = out / asset_prefix / name / "static-world"
        files_bytes = 0
        for filename in static_files(asset_manifest):
            dest = asset_dst_dir / filename
            size, _ = copy_verified(asset_src / filename, dest)
            keep(dest)
            files_bytes += size
        final_manifest = asset_dst_dir / "runtime-manifest.json"
        manifest_doc, _ = read_json(final_manifest)
        proof = CBC.asset_proof(_abs(out), final_manifest, name)
        row = {
            "scene_name": name, "scene_hash": base["scene_hash"],
            "read_status": "ready", "collision_status": "ready", "nav_status": "pending",
            "asset_relpath": final_manifest.relative_to(_abs(out)).as_posix(),
            "collision_coverage": collision_coverage,
            "native_collision_verified": False, "calibration_verified": False,
            "runtime_ready": False, "geometry_source": geometry_source,
            "beamo_build_status": world.get("status", "no_beamo"),
            "beamo_included": world.get("beamo_included", 0),
            "beamo_excluded": world.get("beamo_excluded", 0),
            "reason": None,
            "asset_manifest_sha256": proof["hashes"]["manifest"],
        }
        nav = nav_rows.get(name, {})
        nav_report = None
        if reuse_base:
            if base.get("nav_status") == "ready" and base.get("navigation_relpath"):
                # build_all_maps records only the binary path; the report is its
                # sibling.  Final assets use the release-v13 directory name.
                nav_report = base_root / Path(base["navigation_relpath"]).parent / "nav-build.json"
            elif nav.get("status") == "reuse_base" and base.get("nav_status") == "ready":
                nav_report = base_root / Path(base["navigation_relpath"]).parent / "nav-build.json"
        else:
            if nav.get("status") == "built_offline":
                nav_report = nav_root / nav["nav_report_relpath"]
        descriptor = None
        if nav_report is not None and nav_report.is_file():
            generated_report, generated_raw = read_json(nav_report)
            nav_bin = nav_report.parent / generated_report["navigationFile"]
            if not nav_bin.is_file():
                raise RuntimeError(f"{name}: 导航二进制缺失 {nav_bin}")
            chosen = None
            if not fresh_reports:
                chosen = select_canonical_report(
                    reports, name, proof, manifest_doc["geometryKind"], nav_bin)
            if chosen is None:
                report_sha, report_raw, report_doc = (
                    sha_bytes(generated_raw), generated_raw, generated_report)
                if not fresh_reports:
                    canonical_fallbacks.append(name)
            else:
                report_sha, report_raw, report_doc = chosen
            if reuse_base:
                if name == GALLERY_SCENE:
                    # release-v13 keeps this legacy base nav in a content-named
                    # nav-v3 report slot; the binary name is unchanged.
                    rel_dir = Path("all-scenes-v1") / name / "nav-v3"
                    report_name = f"nav-build-{report_sha}.json"
                else:
                    rel_dir = Path("all-scenes-v1") / name / "navigation-layered-v2"
                    report_name = "nav-build.json"
            else:
                if name == CANDLE_SCENE:
                    # The merged CandleSpace nav is installed in the verified
                    # reuse slot on the server; bytes are the freshly generated
                    # merged navigation.
                    rel_dir = Path("beamo-navigation-all-v1") / name / "reused-verified"
                else:
                    rel_dir = Path("beamo-navigation-all-v1") / name / "attempts" / "0001"
                report_name = "nav-build.json"
            report_dst = out / rel_dir / report_name
            nav_dst = out / rel_dir / nav_bin.name
            write_bytes_if_changed(report_dst, report_raw)
            copy_verified(nav_bin, nav_dst)
            keep(report_dst)
            keep(nav_dst)
            patch = CBC.navigation_proof(_abs(out), report_dst, proof,
                                         legacy_base=reuse_base)
            row.update(patch)
            if reuse_base:
                row["navigation_coverage"] = "legacy_static_candidate"
            row["staged_navigation"] = {
                "report_relpath": report_dst.relative_to(_abs(out)).as_posix(),
                "report_sha256": sha_file(report_dst),
                "navigation_sha256": row["navigation_sha256"],
            }
            item["status"] = "ready"
        else:
            # Designed fallback: short-range navigation descriptor over complete geometry.
            desc_dir = out / "on-demand-navigation-v13" / name
            desc_path = desc_dir / "descriptor.json"
            desc = make_descriptor(final_manifest, desc_path, name)
            keep(desc_path)
            desc_sha = sha_file(desc_path)
            row.update({
                "nav_status": "ready",
                "navigation_coverage": "on_demand_static_candidate",
                "navigation_window": None,
                "reason": None,
            })
            row["staged_navigation"] = {
                "report_relpath": desc_path.relative_to(_abs(out)).as_posix(),
                "report_sha256": desc_sha,
                "navigation_sha256": desc_sha,
            }
            row["navigation_build_mode"] = "on_demand_not_full_prebuilt"
            item["status"] = "on_demand"
            descriptor = desc_path
        entries.append(row)
        item["stages"]["assemble_s"] = round(time.monotonic() - started, 3)
        item["asset_bytes"] = files_bytes
        item["nav_kind"] = "on_demand" if descriptor else ("base" if reuse_base else "rebuilt")
        item["status"] = item.get("status") if item.get("status") in ("on_demand",) else "ready"
        progress.update(f"{name} {item['status']} {files_bytes / 1024 / 1024:.1f}MiB", done=idx)
        save(summary)

    # The server also carries a top-level CandleSpace/{static-world,navigation}
    # copy of the verified single-scene layout.  The catalog does not reference
    # it, but it is part of the deployed release and is cheap to reproduce.
    if CANDLE_SCENE in maps:
        legacy_static_src = out / "beamo-world-all-v1" / CANDLE_SCENE / "static-world"
        legacy_nav_src = out / "beamo-navigation-all-v1" / CANDLE_SCENE / "reused-verified"
        if legacy_static_src.is_dir() and legacy_nav_src.is_dir():
            legacy_static_dst = out / CANDLE_SCENE / "static-world"
            for filename in static_files(legacy_static_src / "runtime-manifest.json"):
                dest = legacy_static_dst / filename
                copy_verified(legacy_static_src / filename, dest)
                keep(dest)
            legacy_nav_dst = out / CANDLE_SCENE / "navigation"
            for source in sorted(legacy_nav_src.iterdir()):
                if not source.is_file():
                    continue
                dest = legacy_nav_dst / source.name
                copy_verified(source, dest)
                keep(dest)

    if full_run:
        entries.sort(key=lambda row: row["scene_name"])
    Catalog(entries)
    imported = sum(1 for row in entries if row.get("navigation_relpath"))
    on_demand = sum(1 for row in entries
                    if row.get("navigation_build_mode") == "on_demand_not_full_prebuilt")
    empty = sum(1 for row in entries if row.get("collision_status") == "empty_scene")
    catalog = {
        "format": "skyterrain-catalog-v1",
        "profile": CATALOG_PROFILE,
        "entries": entries,
        "summary": {
            "scenes": len(entries),
            "imported_navigation": imported,
            "on_demand_static_navigation": on_demand,
            "empty_excluded": empty,
        },
        "runtime_ready": False,
        "native_collision_verified": False,
        "calibration_verified": False,
    }
    removed = 0
    if full_run:
        removed = prune_unexpected(out, expected)
        actual = verify_assembled_files(out, expected)
        if actual != EXPECTED_FULL_ASSET_FILES:
            log(f"      [提示] 本次资产 {actual} 个文件；release-v13 参考为 "
                f"{EXPECTED_FULL_ASSET_FILES}。本次所需文件集完整，"
                f"按需寻路地图 {on_demand} 张。")
    catalog_path = Path(catalog_path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(catalog, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    atomic_bytes(catalog_path, raw)
    (catalog_path.parent / ".hop-proofs").mkdir(parents=True, exist_ok=True)
    catalog_sha = sha_bytes(raw)
    summary["catalog"] = {
        "path": str(catalog_path),
        "sha256": catalog_sha,
        "expected_v13_sha256": CATALOG_SHA256_V13,
        "matches_v13": catalog_sha == CATALOG_SHA256_V13,
        "scene_count": len(entries),
        "partial": not full_run,
        "summary": catalog["summary"],
    }
    summary["assemble"] = {
        "expected_files": len(expected),
        "pruned_stale_files": removed,
        "canonical_report_fallbacks": canonical_fallbacks,
        "fresh_nav_reports": bool(fresh_reports),
    }
    save(summary)
    return catalog


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def dir_stats(path: Path):
    files = 0
    size = 0
    if Path(path).exists():
        for root, _, names in os.walk(path):
            for name in names:
                target = Path(root) / name
                try:
                    size += target.stat().st_size
                    files += 1
                except OSError:
                    pass
    return {"files": files, "bytes": size, "MiB": round(size / 1024 / 1024, 1)}


def save(summary: dict):
    summary["updated_at"] = now_iso()
    target = Path(summary.get("_summary") or (Path(summary["_out"]) / "build-summary.json"))
    atomic_json(target, summary)
    work = Path(summary["_work"])
    if work.exists() and target != (work / "build-summary.json"):
        atomic_json(work / "build-summary.json", summary)


def finalize_summary(summary: dict, monitor: MemoryMonitor, work: Path, out: Path):
    summary["status"] = summary.get("status", "complete")
    summary["finished_at"] = now_iso()
    if monitor is not None:
        summary["memory"] = {
            "peak_tree_rss_mb": round(monitor.peak_total / 1024 / 1024, 1),
            "peak_single_process_rss_mb": round(monitor.peak_single / 1024 / 1024, 1),
            "peak_single_process": monitor.peak_label,
            "samples": monitor.samples,
            "source": "psutil" if psutil is not None else "unavailable",
            "error": monitor.error,
        }
    map_items = list(summary["maps"].values())
    summary["totals"] = {
        "maps_requested": len(summary.get("maps_requested", [])),
        "maps_ready": sum(1 for x in map_items if x.get("status") == "ready"),
        "maps_on_demand": sum(1 for x in map_items if x.get("status") == "on_demand"),
        "maps_empty": sum(1 for x in map_items if x.get("status") == "empty"),
        "maps_failed": sum(1 for x in map_items if x.get("status") == "failed"),
        "sum_map_seconds": round(sum(
            sum(v for v in item.get("stages", {}).values() if isinstance(v, (int, float)))
            for item in map_items), 1),
        "output": dir_stats(out),
        "work": dir_stats(work),
    }
    summary["_out"] = str(out)
    summary["_work"] = str(work)
    save(summary)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="从国服 0.16.8 / 421629 APK 现场生成小光运行时地形资产")
    parser.add_argument("--apk", required=True, type=Path, help="游戏 APK（只读）")
    parser.add_argument("--out", required=True, type=Path, help="输出资产目录（--assets 指向它）")
    parser.add_argument("--maps", help="只生成这些场景，逗号分隔（默认全部资源场景）")
    parser.add_argument("--progress-maps", help="进度统计的启用地图，逗号分隔；不改变资源生成范围")
    parser.add_argument("--resume", action="store_true", help="续跑已有输出")
    parser.add_argument("--work", type=Path, help="中间目录（默认 <out>.work）")
    parser.add_argument("--node", default=shutil.which("node") or "node")
    parser.add_argument("--workers", type=int, default=1, choices=(1, 2),
                        help="导航并发；8GB 机器建议 1（默认）")
    parser.add_argument("--timeout", type=int, default=300,
                        help="单张图单步超时秒数（默认 300）")
    parser.add_argument("--catalog", type=Path,
                        help="运行时目录清单路径（默认 <out>/../multimap-catalog.json）")
    parser.add_argument("--summary", type=Path,
                        help="构建汇总路径（默认 <out>/../build-summary.json）")
    parser.add_argument("--fresh-nav-reports", action="store_true",
                        help="用本机新生成的 nav-build 报告（catalog 不再是 v13 钉值 sha）")
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 900:
        parser.error("--timeout 必须在 30..900 之间")
    apk = _abs(args.apk)
    out = _abs(args.out)
    work = _abs(args.work or Path(str(out) + ".work"))
    catalog_path = _output_path(args.catalog or (out.parent / "multimap-catalog.json"))
    summary_path = _output_path(args.summary or (out.parent / "build-summary.json"))
    if out == work or out.is_relative_to(work) or work.is_relative_to(out):
        parser.error("--out 与 --work 不能互相包含")
    if catalog_path.is_relative_to(out) or summary_path.is_relative_to(out):
        parser.error("--catalog/--summary 不能位于资产目录内（资产目录文件集必须与 release-v13 一致）")
    if catalog_path == summary_path:
        parser.error("--catalog 与 --summary 不能是同一个文件")
    if out.exists() and any(out.iterdir()) and not args.resume:
        parser.error(f"输出目录非空：{out}；续跑请加 --resume（或换目录）")
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    summary = {
        "schema": SCHEMA, "status": "running", "started_at": now_iso(),
        "tool": {"path": str(Path(__file__).resolve()), "sha256": sha_file(Path(__file__)),
                 "profile": PROFILE, "elf_sha256": ELF_SHA256},
        "apk": {"path": str(apk)},
        "out": str(out), "work": str(work),
        "catalog_path": str(catalog_path), "summary_path": str(summary_path),
        "stages": {}, "maps": {}, "maps_requested": [],
        "_out": str(out), "_work": str(work), "_summary": str(summary_path),
        "notes": [
            "offline static candidates only; nativeCollisionVerified/calibrationVerified/runtimeReady are false",
            "does not read or connect accounts, servers, or game processes",
            "asset directory contains exactly the release-v13 file set; runtime catalog and build summary are siblings",
        ],
    }
    monitor = MemoryMonitor()
    monitor.start()
    if args.resume:
        resumed_from = None
        for candidate in (summary_path, out / "build-summary.json",
                          work / "build-summary.json"):
            if candidate.is_file():
                resumed_from = candidate
                break
        if resumed_from is not None:
            try:
                old_summary, _ = read_json(resumed_from)
                for name, item in old_summary.get("maps", {}).items():
                    summary["maps"][name] = {
                        "scene": name,
                        "stages": dict(item.get("stages", {})),
                        **{k: v for k, v in item.items() if k not in ("scene", "stages")},
                    }
                log(f"[resume] 复用构建汇总 {resumed_from}")
            except Exception:
                pass
    log(f"小光地形生成器 — {PROFILE}")
    log(f"APK: {apk}")
    log(f"输出: {out}\n中间: {work}\n清单: {catalog_path}\n汇总: {summary_path}")
    try:
        log("[0/6] 校验 APK 版本与场景清单 ...")
        all_scenes, profile, archive = apk_inventory(apk)
        summary["apk"].update(archive, elf_sha256=profile["sha256"],
                              elf_bytes=profile["bytes"], scene_count=len(all_scenes))
        log(f"      APK 校验通过：{len(all_scenes)} 个场景，libBootloader.so "
            f"{profile['sha256'][:16]}...")
        requested = all_scenes
        if args.maps:
            requested = [x.strip() for x in args.maps.split(",") if x.strip()]
            unknown = sorted(set(requested) - set(all_scenes))
            if unknown:
                raise SystemExit(f"未知场景: {', '.join(unknown)}")
            if len(set(requested)) != len(requested):
                raise SystemExit("--maps 有重复场景")
        counted_maps = None
        if args.progress_maps:
            counted_maps = {name.strip() for name in args.progress_maps.split(",") if name.strip()}
            if not counted_maps or not counted_maps.issubset(requested):
                raise ValueError("进度地图必须属于本次生成的资源场景")
        summary["maps_requested"] = requested
        summary["progress_maps"] = sorted(counted_maps) if counted_maps is not None else requested
        existing_catalog = catalog_path if catalog_path.is_file() else out / "multimap-catalog.json"
        if existing_catalog.is_file() and args.resume:
            try:
                old_catalog, _ = read_json(existing_catalog)
                done = {row["scene_name"] for row in old_catalog.get("entries", [])
                        if row.get("read_status") == "ready" or row.get("collision_status") == "empty_scene"}
                log(f"      续跑：已有清单 {len(done)} 张完成图")
            except Exception:
                pass

        base_progress = StageProgress("base", len(requested), counted_maps)
        catalog = base_stage(apk, work, requested, all_scenes, args.node, args.resume,
                             args.timeout, summary, base_progress)
        base_root = work / "all-scenes-v1"

        log("[2/6] 生成条件资源选择表 ...")
        bindings_path = bindings_stage(apk, work, requested, all_scenes, summary)

        log("[3/6] 合并 Beamo 实例地形 ...")
        world_progress = StageProgress("world", len(requested), counted_maps)
        world_summary = world_stage(apk, work, bindings_path, base_root, requested,
                                    all_scenes, summary, world_progress)

        log("[4/6] 构建合并几何导航 ...")
        nav_progress = StageProgress("nav", len(requested), counted_maps)
        nav_root = nav_stage(work, world_summary, base_root / "catalog.json", requested,
                             all_scenes, args.node, args.workers, summary, nav_progress)

        log("[5/6] 组装运行时资产（只保留引擎实际读取的文件）...")
        world_doc, _ = read_json(world_summary)
        nav_doc, _ = read_json(nav_root / "summary.json")
        assemble_progress = StageProgress("assemble", len(requested), counted_maps)
        assemble(work, out, base_root, work / "beamo-world-all-v1", nav_root,
                 out / "on-demand-navigation-v13", catalog, world_doc, nav_doc,
                 requested, all_scenes, summary, assemble_progress,
                 catalog_path, args.fresh_nav_reports)

        summary["status"] = "complete" if set(requested) == set(all_scenes) else "partial"
        summary["stages"]["total_seconds"] = round(time.monotonic() - started, 1)
        monitor.stop()
        finalize_summary(summary, monitor, work, out)
        total = summary["stages"]["total_seconds"]
        log(f"[6/6] 完成：{summary['totals']['maps_ready']} ready / "
            f"{summary['totals']['maps_on_demand']} on-demand / "
            f"{summary['totals']['maps_empty']} empty / {summary['totals']['maps_failed']} failed")
        log(f"      输出 {summary['totals']['output']['MiB']} MiB，"
            f"耗时 {fmt_seconds(total)}，峰值内存 "
            f"{summary['memory']['peak_tree_rss_mb']} MiB")
        log(f"      引擎: node engine/staged_worker.mjs --assets \"{out}\" --offline")
        log(f"      清单: {catalog_path}  sha256 {summary['catalog']['sha256']}")
        if summary["catalog"]["matches_v13"]:
            log("      catalog 与 release-v20 钉值逐字节一致（d6129a6a…）")
        elif not args.fresh_nav_reports:
            log("      [警告] catalog 与 v13 钉值不一致；见 summary 的 canonical_report_fallbacks")
        log(f"      跳跃记录目录: {catalog_path.parent / '.hop-proofs'}")
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()[-8000:]
        summary["stages"]["total_seconds"] = round(time.monotonic() - started, 1)
        monitor.stop()
        try:
            finalize_summary(summary, monitor, work, out)
        except Exception:
            pass
        log(f"\n[失败] {summary['error']}")
        log(f"日志与可续跑中间目录: {work}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
