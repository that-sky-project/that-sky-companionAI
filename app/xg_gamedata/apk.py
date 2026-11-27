# -*- coding: utf-8 -*-
"""APK 校验与从 MuMu 拉取（check-apk / pull-apk）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from .common import (APK_BUILD, APK_PACKAGE, APK_VERSION, GameDataError, Progress,
                     atomic_json, final, human_size, long_path, sha256_file)
from qfsky.version_profile import PROFILES as VERSION_PROFILES
from .apk_identity import validate_apk_members
from .android_package import package_query_unavailable, parse_apk_paths
from capture.device_scope import is_supported_emulator_serial
import xg_adb

# 国服 0.16.8 / 421629 与 0.16.7 / 421589 的资产身份。
# 旧值保留只用于识别旧 APK；生成数据仍以 0.16.8 为准。
NATIVE_SHA256 = VERSION_PROFILES[APK_BUILD]["native_sha256"]
APK_DIRECTORY_SHA256 = VERSION_PROFILES[APK_BUILD]["apk_directory_sha256"]
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


def inspect_apk(apk: Path, progress: Progress | None = None, *, consumers=None, expected_build=None) -> dict:
    """Native 主识别；按实际生成用途校验资源子集，整包摘要只记录不作渠道门。"""
    apk = Path(apk).expanduser().resolve()
    if not apk.is_file():
        raise GameDataError("APK 不存在：%s" % apk, "apk_missing")
    if apk.stat().st_size < 1024 * 1024:
        raise GameDataError("这个文件太小，不像光遇安装包：%s" % apk, "apk_too_small")
    if progress:
        progress.emit("读取 APK 目录与关键资源…", done=0)
    try:
        with zipfile.ZipFile(apk) as zf:
            names = set(zf.namelist())
            if "AndroidManifest.xml" not in names:
                raise GameDataError("不是 Android 安装包（缺 AndroidManifest.xml）。", "apk_not_android")
            if "resources.arsc" not in names:
                raise GameDataError("安装包缺 resources.arsc，可能是 split 或损坏副本。", "apk_no_resources")
            if NATIVE_MEMBER not in names:
                raise GameDataError("安装包缺少 %s，请使用完整 base.apk。" % NATIVE_MEMBER, "apk_no_native")
            if SCENE_MEMBER not in names:
                raise GameDataError("安装包缺 CandleSpace 场景，请使用完整 base.apk。", "apk_no_scene")
            identity = validate_apk_members(zf, expected_build=expected_build,
                                            **({"consumers": consumers} if consumers is not None else {}))
    except zipfile.BadZipFile:
        raise GameDataError("APK 打不开（zip 结构损坏）：%s" % apk, "apk_bad_zip") from None
    except OSError as exc:
        raise GameDataError("读 APK 失败：%s" % exc, "apk_read_error") from exc
    if progress:
        progress.emit("记录整包 sha256（不作渠道身份限制）…", done=1)
    whole_sha = sha256_file(apk)
    return {
        "ok": True, "path": str(apk), "package": APK_PACKAGE,
        "version": identity["version"], "version_code": identity["build"],
        "build": identity["build"], "sha256": whole_sha,
        "bytes": apk.stat().st_size, "bytes_human": human_size(apk.stat().st_size),
        **identity,
        "identity_source": ("native ELF sha256 + legacy exact APK directory"
                            if identity["legacy"] else "native ELF sha256 + used resource member subsets"),
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
    adb_args = (["-s", serial] if serial else []) + args
    try:
        proc = xg_adb.run(cmd, adb_args, capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise GameDataError("找不到 adb：%s" % adb, "adb_missing")
    except subprocess.TimeoutExpired:
        raise GameDataError("adb 超时（%d 秒）：%s" % (timeout, " ".join(args)), "adb_timeout")
    if proc.returncode != 0 or (args[0] == "shell" and not (proc.stdout or "").strip()
                               and package_query_unavailable(proc.stderr)):
        detail = (proc.stderr or proc.stdout or "").strip()
        if "xg_adb_server_version_conflict" in detail:
            raise GameDataError("正在运行的 ADB 服务与所选 adb.exe 版本不同，小光没有停止它。"
                                "请使用该 MuMu 自带的 adb.exe，或关闭占用 ADB 的工具后重试。",
                                "adb_server_version_conflict")
        raise GameDataError("adb 命令失败（%s）：%s" % (" ".join(args), detail[:500]),
                            "adb_failed")
    return proc.stdout or ""


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
    return parse_apk_paths(out)


def _query_apk_paths(adb: Path, serial: str) -> list[str]:
    errors = []
    explicitly_absent = False
    for command in (["shell", "pm", "path", APK_PACKAGE],
                    ["shell", "cmd", "package", "path", APK_PACKAGE]):
        try:
            output = _run_adb(adb, serial, command, timeout=12)
        except GameDataError as exc:
            if exc.code in {"adb_missing", "adb_timeout", "adb_server_version_conflict"}:
                raise
            detail = exc.message
            if any(marker in detail.lower() for marker in ("device offline", "device unauthorized",
                                                            "no devices/emulators found")):
                raise
            if "unknown package" in detail.lower():
                explicitly_absent = True
            else:
                errors.append(detail)
            continue
        paths = _parse_pm_path(output)
        if paths:
            return paths
        if "unknown package" in output.lower():
            explicitly_absent = True
        elif output.strip():
            errors.append(output.strip()[:300])
    if explicitly_absent or not errors:
        return []
    raise GameDataError("查询 %s 的 APK 路径失败：%s" % (APK_PACKAGE, "; ".join(errors)),
                        "pm_path_failed")


def _safe_name(remote: str) -> str:
    name = remote.replace("\\", "/").rstrip("/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-")
    return name or "package.apk"


def _merge_apk_view(base: Path, splits: list[Path], out: Path,
                    progress: Progress | None = None) -> Path:
    """把 base + split 合成一个 zip 视图（同名以 base 优先，split 只补缺）。"""
    tmp = out.with_name(out.name + ".t")
    seen = set()
    try:
        with zipfile.ZipFile(long_path(tmp), "w", zipfile.ZIP_DEFLATED, allowZip64=True) as dst:
            for src in [base] + splits:
                with zipfile.ZipFile(long_path(src)) as zf:
                    members = zf.infolist()
                    if len({info.filename for info in members}) != len(members):
                        raise GameDataError("APK 拆分包里有重复文件成员：%s" % src.name,
                                            "apk_duplicate_member")
                    for info in members:
                        if info.is_dir() or info.filename in seen:
                            continue
                        seen.add(info.filename)
                        with zf.open(info) as fh, dst.open(info.filename, "w") as wh:
                            shutil.copyfileobj(fh, wh, 8 * 1024 * 1024)
                if progress:
                    progress.emit("合并 %s" % src.name, done=progress.done + 1)
        os.replace(long_path(tmp), long_path(out))
    except zipfile.BadZipFile as exc:
        raise GameDataError("APK 拆分包损坏：%s" % exc, "apk_bad_zip") from exc
    finally:
        if os.path.exists(long_path(tmp)):
            os.unlink(long_path(tmp))
    return out


def _pulled_validation_error(error):
    """拉取阶段已完成：保留原机器码，避免 UI 把资源校验失败显示成拉取失败。"""
    result = GameDataError("拉取成功，但安装包校验未通过：" + error.message, error.code)
    result.stage = "validate_pulled_apk"
    result.child_code = error.code
    for key in ("members", "difference_count"):
        if hasattr(error, key):
            setattr(result, key, getattr(error, key))
    return result


def pull_apk(adb: str, serial: str | None, data_dir: Path) -> dict:
    scope_message = "拉取安装包只支持本机或私有局域网的 MuMu 实例；不支持公网设备和 USB 真机。请打开 MuMu 后重试。"
    if serial is not None and not is_supported_emulator_serial(serial):
        raise GameDataError(scope_message, "unsupported_device_scope")
    adb_path = Path(adb)
    if not adb_path.is_file():
        raise GameDataError("adb 不存在：%s" % adb_path, "adb_missing")
    progress = Progress("pull-apk", 6)
    progress.emit("检查 MuMu 设备…", done=0)
    online = _device_list(adb_path)
    devices = [value for value in online if is_supported_emulator_serial(value)]
    if not devices:
        if online:
            raise GameDataError(scope_message, "unsupported_device_scope")
        raise GameDataError(
            "没找到 MuMu/adb 设备。先在 MuMu 里开机、打开开发者调试，"
            "再确认 adb 能连上（adb devices 能看到设备）。", "no_device")
    if serial is None:
        if len(devices) > 1:
            raise GameDataError("有多个 MuMu 实例在线。请从安装向导选中运行光遇的实例后重新拉取。",
                                "device_ambiguous")
        serial = devices[0]
    elif serial not in devices:
        raise GameDataError("设备 %s 不在线；当前在线：%s" % (serial, ", ".join(devices)),
                            "device_not_found")
    progress.emit("查询光遇安装路径…", done=1)
    paths = _query_apk_paths(adb_path, serial)
    if not paths:
        raise GameDataError(
            "MuMu 里没有安装光遇（%s）。请先在 MuMu 里装好国服 0.16.8/421629。" % APK_PACKAGE,
            "game_not_installed")
    dest = Path(data_dir) / "game" / "apk"
    dest.mkdir(parents=True, exist_ok=True)
    base_paths = [p for p in paths if _safe_name(p) == "base.apk"]
    if len(base_paths) == 1:
        base_remote = base_paths[0]
    elif len(paths) == 1 and not _safe_name(paths[0]).startswith("split"):
        base_remote = paths[0]  # Older monolithic system APKs may have another name.
    else:
        raise GameDataError("安卓包管理器未返回唯一的 base.apk，请确认安装完整后重试。",
                            "apk_base_path_ambiguous")
    ordered = [base_remote] + [p for p in paths if p != base_remote]
    names = ["base.apk", *(_safe_name(p) for p in ordered[1:])]
    if len({name.casefold() for name in names}) != len(names):
        raise GameDataError("安卓包管理器返回了同名的 APK 拆分包，请重试。", "apk_split_path_ambiguous")
    local: list[Path] = []
    for idx, (remote, name) in enumerate(zip(ordered, names)):
        target = dest / name
        progress.emit("拉取 %s…" % remote, done=2 + (idx * 2) % 3)
        fd, tmp = tempfile.mkstemp(prefix=".pull-", dir=long_path(dest))
        os.close(fd)
        try:
            _run_adb(adb_path, serial, ["pull", remote, str(tmp)], timeout=1800)
            if not os.path.isfile(long_path(tmp)) or os.path.getsize(long_path(tmp)) == 0:
                raise GameDataError("拉取失败，文件为空：%s" % remote, "pull_empty")
            os.replace(long_path(tmp), long_path(target))
        except GameDataError as exc:
            detail = exc.message.lower()
            if "permission denied" in detail and any(marker in detail for marker in (
                    "stat remote", "remote open", "failed to copy")):
                raise GameDataError("当前安卓镜像禁止 ADB 读取安装包。请在向导中选择本地完整 APK。",
                                    "pull_apk_denied") from exc
            raise
        finally:
            if os.path.exists(long_path(tmp)):
                os.unlink(long_path(tmp))
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
            raise _pulled_validation_error(exc) from exc
        progress.emit("base 不完整（%s），合并 split 视图…" % exc.code, done=5)
        merged = dest / "merged.apk"
        try:
            _merge_apk_view(base, splits, merged, progress)
            info = inspect_apk(merged, progress=None)
        except GameDataError as merged_error:
            raise _pulled_validation_error(merged_error) from merged_error
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
        extra = {key: getattr(exc, key) for key in ("members", "difference_count") if hasattr(exc, key)}
        final(False, error=exc.message, code=exc.code, **extra)
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
        extra = {key: getattr(exc, key) for key in ("stage", "child_code", "members", "difference_count") if hasattr(exc, key)}
        final(False, error=exc.message, code=exc.code, **extra)
        return 1
