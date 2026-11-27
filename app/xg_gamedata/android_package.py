"""Read-only Android package-manager output parsers shared with MuMu checks."""
from __future__ import annotations

import re


_PACKAGE_HEADER = re.compile(r"^[ \t]*Package \[([A-Za-z0-9_.]+)\](?:[ \t]+\([^()\r\n]*\))?:[ \t\r]*$", re.M)
_NEXT_SECTION = re.compile(r"\n(?!Package \[)[^\s\n][^\n]*:[ \t\r]*(?=\n|$)")
_APK_ROW = re.compile(r"package:\s*(/[A-Za-z0-9_./~+=-]+\.apk)")
_PATH = re.compile(r"/[A-Za-z0-9_./~+=-]+")
_ABI_NAMES = frozenset(("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86", "riscv64"))
_ABI_ISA = {"arm64-v8a": "arm64", "armeabi-v7a": "arm", "armeabi": "arm",
            "x86_64": "x86_64", "x86": "x86", "riscv64": "riscv64"}
_PM_UNAVAILABLE = re.compile(
    r"securityexception|permission deni(?:ed|al)|not allowed|unknown (?:option|command)|"
    r"inaccessible or not found|(?:^|\s)not found|can't find service|"
    r"could not access the package manager|not yet running|error:(?![ \t]*unknown package\b)", re.I)


def _safe_android_path(value: str | None) -> str | None:
    if value and _PATH.fullmatch(value) and all(p not in ("", ".", "..") for p in value[1:].split("/")):
        return value
    return None


def parse_apk_paths(output: str) -> list[str]:
    """Parse pm/cmd path rows; preserve randomized install directory names verbatim."""
    result = []
    for line in output.splitlines():
        row = _APK_ROW.fullmatch(line.strip())
        if row:
            path = _safe_android_path(row[1])
            if path and path not in result:
                result.append(path)
    return result


def package_query_unavailable(output: str) -> bool:
    return bool(_PM_UNAVAILABLE.search(output or ""))


def _package_fields(output: str, package: str) -> str:
    marker = re.search(r"^[ \t]*Packages:[ \t\r]*$", output, re.M)
    scope = output
    if marker:
        # The first installed Packages section takes precedence over any
        # Hidden/Disabled system packages with outdated metadata.
        scope = _NEXT_SECTION.split(output[marker.end():], maxsplit=1)[0]
    headers = list(_PACKAGE_HEADER.finditer(scope))
    if not headers:
        # Some dumps, including older emulator output, contain only field rows.
        return scope
    for i, header in enumerate(headers):
        if header[1] != package:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(scope)
        fields = scope[header.end():end]
        # Stay inside the installed package section; do not infer from old,
        # disabled system packages or unrelated subsequent dumpsys sections.
        return _NEXT_SECTION.split(fields, maxsplit=1)[0]
    return ""


def _field(fields: str, name: str, value_pattern: str) -> str | None:
    values = set(re.findall(rf"^[ \t]*{name}[ \t]*[:=][ \t]*({value_pattern})(?=[ \t\r\n]|$)", fields, re.M))
    return next(iter(values)) if len(values) == 1 else None


def parse_package_dump(output: str, package: str) -> dict:
    fields = _package_fields(output, package)
    return {
        "version_code": (_field(fields, "longVersionCode", r"[0-9]+")
                         or _field(fields, "versionCode", r"[0-9]+")),
        "primary_abi": _field(fields, "primaryCpuAbi", r"[A-Za-z0-9_-]+"),
        "secondary_abi": _field(fields, "secondaryCpuAbi", r"[A-Za-z0-9_-]+"),
        "native_library_dir": _safe_android_path(_field(fields, "nativeLibraryDir", r"\S+")),
        "legacy_native_library_dir": _safe_android_path(_field(fields, "legacyNativeLibraryDir", r"\S+")),
        "code_path": _safe_android_path(_field(fields, "codePath", r"\S+")),
    }


def parse_package_list_version(output: str, package: str) -> str | None:
    pattern = re.compile(rf"package:{re.escape(package)}\s+(?:longVersionCode|versionCode)\s*[:=]\s*([0-9]+)")
    versions = {row[1] for line in output.splitlines() if (row := pattern.fullmatch(line.strip()))}
    return next(iter(versions)) if len(versions) == 1 else None


def package_diagnostics(shell, package: str, *, include_abi: bool = False) -> dict:
    """Query one package over the caller's already-selected, bounded ADB shell."""
    rc, out, _ = shell("dumpsys package " + package)
    data = parse_package_dump(out or "", package) if rc == 0 else {}
    if not data.get("version_code") and rc != -1:
        rc, out, _ = shell("cmd package list packages --show-versioncode " + package)
        if rc == 0:
            data["version_code"] = parse_package_list_version(out or "", package)
    # Older dumps may not report ABI; don't guess that the guest CPU and the
    # package use the same instruction set, and don't add a command in that case.
    if include_abi and data.get("primary_abi") in _ABI_NAMES:
        rc, out, _ = shell("getprop ro.product.cpu.abi")
        data["native_abi"] = describe_native_abi(data, (out or "").strip() if rc == 0 else None)
    return data


def describe_native_abi(package_fields: dict, device_abi: str | None) -> dict:
    """Report guest ABI/library layout; this never authorizes native capture."""
    primary = package_fields.get("primary_abi")
    secondary = package_fields.get("secondary_abi")
    primary = primary if primary in _ABI_NAMES else None
    secondary = secondary if secondary in _ABI_NAMES else None
    device_abi = device_abi if device_abi in _ABI_NAMES else None
    mode = {
        ("x86_64", "arm64-v8a"): "arm64_on_x86_64",
        ("arm64-v8a", "arm64-v8a"): "arm64_native",
        ("x86_64", "x86_64"): "x86_64_native",
    }.get((device_abi, primary), "unknown")
    explicit = package_fields.get("native_library_dir")
    legacy = package_fields.get("legacy_native_library_dir")
    code_path = package_fields.get("code_path")
    candidates = [explicit] if explicit else []
    abis = list(dict.fromkeys(p for p in (primary, secondary) if p))
    if legacy:
        candidates += [f"{legacy}/{_ABI_ISA[abi]}" for abi in abis
                       if legacy.rsplit("/", 1)[-1] not in _ABI_ISA.values()]
        candidates.append(legacy)
    if code_path and not code_path.endswith(".apk"):
        candidates += [f"{code_path}/lib/{_ABI_ISA[abi]}" for abi in abis]
    return {"image": device_abi, "primary": primary, "secondary": secondary, "mode": mode,
            "native_library_dir": explicit, "candidate_dirs": list(dict.fromkeys(candidates)),
            "apk_lib_prefixes": [f"lib/{abi}/" for abi in abis]}
