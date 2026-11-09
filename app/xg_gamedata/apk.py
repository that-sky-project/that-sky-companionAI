# -*- coding: utf-8 -*-
"""APK 校验与从 MuMu 拉取（check-apk / pull-apk）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from .common import (APK_BUILD, APK_PACKAGE, APK_VERSION, GameDataError, Progress,
                     atomic_json, final, human_size, long_path, sha256_file)
import xg_exec

# 国服 0.16.7 / 421589 的资产身份（与 W2/W3 的 version_profile 一致）
NATIVE_SHA256 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"
APK_DIRECTORY_SHA256 = "b26db74613f606fd60b942ff58f00d0c41b12a6d83303228a1143d683adefb14"
NATIVE_MEMBER = "lib/arm64-v8a/libBootloader.so"
SCENE_MEMBER = "assets/Data/Levels/CandleSpace/Objects.level.bin"


def archive_directory_sha(zf: zipfile.ZipFile) -> str:
    """APK 目录身份：成员名/大小/CRC 排序后的 sha256（不读 2GB 内容）。"""
    infos = zf.infolist()
    names = [i.filename for i in infos]
    if len(names) != len(set(names)):
        raise GameDataError("APK 里有重复文件成员，拒绝使用。", "apk_duplicate_member")
    payload = json.dumps(sorted((i.filename, i.file_size, i.CRC) for i in infos),
                         separators=(",", ":")).encode("utf-8")
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def inspect_apk(apk: Path, progress: Progress | None = None) -> dict:
    """完整校验；失败抛 GameDataError（中文）。成功返回身份信息。"""
    apk = Path(apk).expanduser().resolve()
    if not apk.is_file():
        raise GameDataError("APK 不存在：%s" % apk, "apk_missing")
    if apk.stat().st_size < 1024 * 1024:
        raise GameDataError("这个文件太小，不像光遇安装包：%s" % apk, "apk_too_small")
    if progress:
        progress.emit("读取 APK 目录…", done=0)
    try:
        with zipfile.ZipFile(apk) as zf:
            names = set(zf.namelist())
            if "AndroidManifest.xml" not in names:
                raise GameDataError("不是 Android 安装包（缺 AndroidManifest.xml）。",
                                    "apk_not_android")
            if "resources.arsc" not in names:
                raise GameDataError("安装包里没有 resources.arsc，可能是 split 或损坏的副本。",
                                    "apk_no_resources")
            if NATIVE_MEMBER not in names:
                raise GameDataError(
                    "安装包缺少 %s：请拉取完整的 base.apk（不是只有 split）。" % NATIVE_MEMBER,
                    "apk_no_native")
            if SCENE_MEMBER not in names:
                raise GameDataError(
                    "安装包里没有 CandleSpace 场景（assets/Data/Levels/...）："
                    "这不是光遇国服完整安装包。", "apk_no_scene")
            directory_sha = archive_directory_sha(zf)
            native_sha = sha256_file_from_zip(zf, NATIVE_MEMBER)
    except zipfile.BadZipFile:
        raise GameDataError("APK 打不开（zip 结构损坏）：%s" % apk, "apk_bad_zip")
    except OSError as exc:
        raise GameDataError("读 APK 失败：%s" % exc, "apk_read_error")

    if progress:
        progress.emit("校验整包 sha256（大文件要几十秒）…", done=1)
    whole_sha = sha256_file(apk)

    problems = []
    if native_sha != NATIVE_SHA256:
        problems.append("libBootloader.so 的 SHA-256 不对（不是国服 0.16.7 / 421589）")
    if directory_sha != APK_DIRECTORY_SHA256:
        problems.append("APK 目录身份（文件名/大小/CRC）不对（不是这份国服 0.16.7 / 421589）")
    if problems:
        raise GameDataError("；".join(problems) + "。只接受国服 0.16.7 / 421589 的安装包。",
                            "apk_identity_mismatch")
    return {
        "ok": True,
        "path": str(apk),
        "package": APK_PACKAGE,
        "version": APK_VERSION,
        "build": APK_BUILD,
        "sha256": whole_sha,
        "bytes": apk.stat().st_size,
        "bytes_human": human_size(apk.stat().st_size),
        "apk_archive_directory_sha256": directory_sha,
        "native_sha256": native_sha,
        "identity_source": "native ELF sha256 + APK archive directory sha256（同 W2/W3 profile）",
    }


def sha256_file_from_zip(zf: zipfile.ZipFile, member: str, chunk: int = 8 * 1024 * 1024) -> str:
    import hashlib
    h = hashlib.sha256()
    with zf.open(member) as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------ pull-apk
def _adb_command(adb: Path) -> list[str]:
    adb = Path(adb).expanduser().resolve()
    if adb.suffix.lower() == ".py":
        return [sys.executable, str(adb)]
    if adb.suffix.lower() in (".cmd", ".bat"):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(adb)]
    return [str(adb)]


def _run_adb(adb: Path, serial: str | None, args: list[str], timeout: int = 120) -> str:
    cmd = _adb_command(adb)
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        proc = xg_exec.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise GameDataError("找不到 adb：%s" % adb, "adb_missing")
    except subprocess.TimeoutExpired:
        raise GameDataError("adb 超时（%d 秒）：%s" % (timeout, " ".join(args)), "adb_timeout")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GameDataError("adb 命令失败（%s）：%s" % (" ".join(args), detail[:500]),
                            "adb_failed")
    return proc.stdout


def _device_list(adb: Path) -> list[str]:
    out = _run_adb(adb, None, ["devices"])
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            rows.append(parts[0])
    return rows


def _parse_pm_path(out: str) -> list[str]:
    paths = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            value = line[len("package:"):].strip()
            if value:
                paths.append(value)
    return paths


def _safe_name(remote: str) -> str:
    name = remote.replace("\\", "/").rstrip("/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-")
    return name or "package.apk"


def _merge_apk_view(base: Path, splits: list[Path], out: Path,
                    progress: Progress | None = None) -> Path:
    """把 base + split 合成一个 zip 视图（同名以 base 优先，split 只补缺）。"""
    tmp = out.with_name(out.name + ".t")
    seen = set()
    with zipfile.ZipFile(long_path(tmp), "w", zipfile.ZIP_DEFLATED, allowZip64=True) as dst:
        for src in [base] + splits:
            with zipfile.ZipFile(src) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.filename in seen:
                        continue
                    seen.add(info.filename)
                    with zf.open(info) as fh, dst.open(info.filename, "w") as wh:
                        shutil.copyfileobj(fh, wh, 8 * 1024 * 1024)
            if progress:
                progress.emit("合并 %s" % src.name, done=progress.done + 1)
    os.replace(long_path(tmp), long_path(out))
    return out


def pull_apk(adb: str, serial: str | None, data_dir: Path) -> dict:
    adb_path = Path(adb)
    if not adb_path.is_file():
        raise GameDataError("adb 不存在：%s" % adb_path, "adb_missing")
    progress = Progress("pull-apk", 6)
    progress.emit("检查 MuMu 设备…", done=0)
    devices = _device_list(adb_path)
    if not devices:
        raise GameDataError(
            "没找到 MuMu/adb 设备。先在 MuMu 里开机、打开开发者调试，"
            "再确认 adb 能连上（adb devices 能看到设备）。", "no_device")
    if serial is None:
        serial = devices[0]
    elif serial not in devices:
        raise GameDataError("设备 %s 不在线；当前在线：%s" % (serial, ", ".join(devices)),
                            "device_not_found")
    progress.emit("查询光遇安装路径…", done=1)
    try:
        out = _run_adb(adb_path, serial, ["shell", "pm", "path", APK_PACKAGE])
    except GameDataError as exc:
        raise GameDataError("查询 %s 失败：%s" % (APK_PACKAGE, exc.message), "pm_path_failed")
    paths = _parse_pm_path(out)
    if not paths:
        raise GameDataError(
            "MuMu 里没有安装光遇（%s）。请先在 MuMu 里装好国服 0.16.7/421589。" % APK_PACKAGE,
            "game_not_installed")
    dest = Path(data_dir) / "game" / "apk"
    dest.mkdir(parents=True, exist_ok=True)
    base_remote = next((p for p in paths if _safe_name(p) == "base.apk"), paths[0])
    ordered = [base_remote] + [p for p in paths if p != base_remote]
    local: list[Path] = []
    for idx, remote in enumerate(ordered):
        name = _safe_name(remote)
        if idx == 0:
            name = "base.apk"
        elif name == "base.apk":
            name = "base_%d.apk" % idx
        target = dest / name
        progress.emit("拉取 %s…" % remote, done=2 + (idx * 2) % 3)
        tmp = target.with_name(target.name + ".t")
        _run_adb(adb_path, serial, ["pull", remote, str(tmp)], timeout=1800)
        if not os.path.isfile(long_path(tmp)) or os.path.getsize(long_path(tmp)) == 0:
            raise GameDataError("拉取失败，文件为空：%s" % remote, "pull_empty")
        os.replace(long_path(tmp), long_path(target))
        local.append(target)
    base = local[0]
    splits = local[1:]
    result = {
        "ok": True, "serial": serial, "package": APK_PACKAGE,
        "remote_paths": ordered, "files": [str(p) for p in local],
        "bytes": sum(p.stat().st_size for p in local),
        "split_count": len(splits),
    }
    atomic_json(dest / "pull-source.json", {"serial": serial, "package": APK_PACKAGE,
                                            "remote_paths": ordered,
                                            "files": [p.name for p in local],
                                            "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    progress.emit("校验 base.apk…", done=5)
    try:
        info = inspect_apk(base, progress=None)
        result.update({"apk": str(base), "check": info, "merged": False})
        progress.finish("base.apk 校验通过")
        return result
    except GameDataError as exc:
        if not splits:
            raise
        progress.emit("base 不完整（%s），合并 split 视图…" % exc.code, done=5)
        merged = dest / "merged.apk"
        _merge_apk_view(base, splits, merged, progress)
        info = inspect_apk(merged, progress=None)
        result.update({"apk": str(merged), "check": info, "merged": True,
                       "base_check_error": exc.message})
        progress.finish("split 合并后校验通过")
        return result


def cmd_check(args) -> int:
    progress = Progress("check-apk", 3)
    try:
        info = inspect_apk(Path(args.apk), progress)
        progress.finish("校验通过")
        info.pop("ok", None)
        final(True, **info)
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1


def cmd_pull(args) -> int:
    from .common import resolve_data
    try:
        data = resolve_data(args.data)
        result = pull_apk(args.adb, args.serial, data)
        result.pop("ok", None)
        final(True, **result)
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
