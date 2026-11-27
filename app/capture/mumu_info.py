"""Parse read-only MuMuManager instance inventories across CLI layouts."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass

from .device_scope import is_supported_emulator_tcp_serial, is_loopback_emulator_tcp_serial

_MAX_TEXT = 512 * 1024
_MAX_ROWS = 64


@dataclass(frozen=True)
class MuMuInstance:
    candidates: tuple[str, ...]
    running: bool | None
    root_enabled: bool | None
    adb_enabled: bool | None
    android_version: str | None = None


def _key(text):
    text = str(text).strip()
    if re.fullmatch(r"(?:安卓|android)\s*(?:系统)?版本", text, re.I):
        return "android_version"
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


_INDEX = ("index", "vm_index", "instance_index", "vm_id", "instance_id")
_HOST = ("adb_host_ip", "adb_host", "adb_ip", "adb_host_address", "adb_address")
_PORT = ("adb_port", "adb_host_port", "local_adb_port", "forward_adb_port")
_GUEST = ("guest_ip", "guest_ipv4", "vm_ip", "android_ip", "bridge_ip", "network_ip")
_RUNNING = ("is_android_started", "android_started", "android_running", "is_running", "running", "is_started",
            "is_process_started", "process_started", "vm_running", "vm_state", "instance_state", "state", "status")
_ROOT = ("root_enabled", "is_root_enabled", "enable_root", "is_root", "root", "root_permission",
         "root_enable", "root_mode", "system_root_enabled", "system_root", "system_root_mode")
_ADB = ("adb_enabled", "is_adb_enabled", "enable_adb", "adb_debug_enabled", "adb_debug_enable",
        "adb_debug", "adb_debugging", "usb_debugging", "enable_adb_debug", "adb_debug_status")
_ANDROID_RELEASE = ("android_version", "android_release", "android_version_release", "android_os_version",
                    "android_major_version", "android_image_version", "image_android_version", "android",
                    "ro_build_version_release")
_ANDROID_SDK = ("android_sdk", "android_sdk_version", "android_api_level", "android_api",
                "android_version_sdk", "ro_build_version_sdk")
# Android 12L shares the major version number but has a distinct API level.
_SDK_RELEASE = {28: "9", 29: "10", 30: "11", 31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16"}
_INSTANCE_KEYS = set(_INDEX + _HOST + _PORT + _GUEST + _RUNNING[:-2])
_TEXT_KEYS = set(_INDEX + _HOST + _PORT + _GUEST + _RUNNING + _ROOT + _ADB + _ANDROID_RELEASE + _ANDROID_SDK)


def _flatten(row, path="", depth=0):
    result = {}
    if depth > 7:
        return result
    for key, value in row.items():
        key = _key(key)
        full = f"{path}_{key}" if path else key
        if isinstance(value, dict):
            result.update(_flatten(value, full, depth + 1))
        elif isinstance(value, (str, bool, int)):
            result[full] = value
            # Guest/network wrappers sometimes carry names that are already fully qualified.
            result.setdefault(key, value)
            for section in ("android", "android_version"):
                if path.endswith("_" + section):
                    result.setdefault(f"{section}_{key}", value)
    return result


def _json_rows(value, depth=0, instance_hint=False):
    if depth > 8:
        return
    if isinstance(value, list):
        for child in value[:_MAX_ROWS]:
            yield from _json_rows(child, depth + 1, True)
    elif isinstance(value, dict):
        if any(_key(key) in (_TEXT_KEYS if instance_hint else _INSTANCE_KEYS) for key in value):
            yield _flatten(value)
        else:
            # Some CLIs split network and system data into nested dictionaries
            # without a separate index when querying a single running VM.
            if set(_flatten(value)) & set(_HOST + _PORT + _GUEST) and not any(
                    isinstance(child, list) or (str(key).isdigit() and isinstance(child, dict))
                    for key, child in value.items()):
                children = [child for child in value.values() if isinstance(child, dict)]
                if children and (instance_hint or all(
                        not any(isinstance(grandchild, (dict, list)) for grandchild in child.values())
                        for child in children)):
                    if sum(bool(set(_flatten(child)) & set(_HOST + _PORT + _GUEST)) for child in children) <= 1:
                        yield _flatten(value)
                        return
            for key, child in value.items():
                yield from _json_rows(child, depth + 1, str(key).isdigit())


def _text_rows(text):
    # Legacy builds may print a key=value block per VM, or a delimited table.
    lines = text.splitlines()
    for delimiter in (",", "\t", "|"):
        for index, line in enumerate(lines[:20]):
            if delimiter not in line:
                continue
            columns = [_key(key) for key in next(csv.reader([line], delimiter=delimiter))]
            if not set(columns) & set(_HOST + _PORT) or not set(columns) & set(_INDEX):
                continue
            for row in csv.reader(io.StringIO("\n".join(lines[index + 1:])), delimiter=delimiter):
                if len(row) == len(columns):
                    yield dict(zip(columns, row))
            return
    record = {}
    for line in lines:
        if re.fullmatch(r"\s*\[\s*(?:(?:vm|instance)\s*)?\d+\s*\]\s*", line, re.I):
            if record:
                yield record
                record = {}
            continue
        match = re.match(r"\s*[\"']?([\w. -]+?)[\"']?\s*[=:＝：]\s*(.*?)\s*[,;]?\s*$", line)
        if not match:
            continue
        key = _key(match[1])
        if key in _INDEX and set(record) & set(_INDEX):
            yield record
            record = {}
        if key in _TEXT_KEYS:
            record[key] = match[2].strip("\"'")
    if record:
        yield record


def _bool(row, keys, *, state=False):
    positive = ("1", "true", "yes", "on", "enable", "enabled", "开启", "已开启")
    negative = ("0", "false", "no", "off", "disable", "disabled", "关闭", "已关闭")
    if state:
        positive += ("running", "started", "ready", "booted", "启动", "已启动", "运行中")
        negative += ("stopped", "not_running", "notrunning", "powered_off", "shutdown", "exited", "未启动", "已停止")
    for key in keys:
        value = str(row.get(key, "")).casefold().strip().replace(" ", "_")
        if value in positive:
            return True
        if value in negative:
            return False
    return None


def _first(row, keys):
    return next((str(row[k]).strip() for k in keys if k in row and str(row[k]).strip()), "")


def _serial(host, port):
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    serial = f"{host}:{port}"
    return serial if is_supported_emulator_tcp_serial(serial) else None


def _android_version(row):
    # Only fields explicitly tied to the guest Android version are trustworthy;
    # MuMu's client/install version and the user-supplied VM name are not.
    for key in _ANDROID_RELEASE:
        value = str(row.get(key, "")).strip()
        match = re.fullmatch(r"(?:(?:android|安卓)[ _-]*)?([1-9]\d?(?:\.\d+){0,2}|12L)", value, re.I | re.ASCII)
        if match:
            return match[1].upper()
    for key in _ANDROID_SDK:
        value = str(row.get(key, "")).strip()
        if re.fullmatch(r"[0-9]{2}", value) and int(value) in _SDK_RELEASE:
            return _SDK_RELEASE[int(value)]
    return None


def _instance(row):
    running = _bool(row, _RUNNING, state=True)
    adb_enabled = _bool(row, _ADB)
    root_enabled = _bool(row, _ROOT)
    # adb_debug.mode=0 alone does not mean disabled: MuMu can expose guest:5555.
    host, port = _first(row, _HOST), _first(row, _PORT)
    guest = _first(row, _GUEST)
    if not guest and (host_serial := _serial(host, 5555)) and not is_loopback_emulator_tcp_serial(host_serial):
        guest = host
    candidates = tuple(dict.fromkeys(serial for serial in (
        _serial(guest, 5555), _serial(host, port), _serial("127.0.0.1", port),
    ) if serial))
    android_version = _android_version(row)
    if (candidates or running is not None or adb_enabled is not None or root_enabled is not None
            or (android_version and set(row) & set(_INDEX))):
        return MuMuInstance(candidates, running, root_enabled, adb_enabled, android_version)
    return None


def parse_instances(text):
    """Accept keyed/list JSON (including prefixed CLI output), CSV/TSV and key/value."""
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        return ()
    decoder = json.JSONDecoder()
    for marker in list(re.finditer(r"[\[{]", text))[:24]:
        try:
            doc, _ = decoder.raw_decode(text, marker.start())
            rows = tuple(dict.fromkeys(instance for row in _json_rows(doc) if (instance := _instance(row))))
        except (ValueError, RecursionError):
            continue
        if rows:
            return rows[:_MAX_ROWS]
    try:
        return tuple(dict.fromkeys(instance for row in _text_rows(text) if (instance := _instance(row))))[:_MAX_ROWS]
    except csv.Error:
        return ()
