"""Narrow adapters for ADB shell output on older and newer Android images."""
from __future__ import annotations

import re

_UID_ZERO = re.compile(r"^uid=0(?:\(|\s|$)")
_ROOT_SHELLS = ("sh -c ", "su -c ", "su 0 sh -c ", "su 0 -c ")
_PID = re.compile(r"[0-9]{1,10}")
THREADTIME = re.compile(
    r"^(?P<timestamp>(?:[0-9]{4}-)?[0-9]{2}-[0-9]{2}[ \t]+"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3,9}"
    r"(?:[ \t]+[+-][0-9]{2}:?[0-9]{2})?)[ \t]+"
    r"(?:(?:[0-9]{1,10}|[A-Za-z_][A-Za-z0-9_.-]{0,31}):?[ \t]+)?"
    r"(?P<pid>[0-9]{1,10})[ \t]+(?P<tid>[0-9]{1,10})[ \t]+[VDIWEFS]"
    r"[ \t]+[^:\r\n]*:[ \t]*(?P<message>.*)$"
)
_LOGCAT_OPTION_ERROR = re.compile(
    r"^\s*(?:(?:logcat:\s*)?(?:unknown|unrecognized|unsupported|invalid|illegal)"
    r"\s+(?:option|argument)[^\n]*--pid\b|usage:\s*logcat)", re.IGNORECASE | re.MULTILINE)
_LOGCAT_ACCESS_ERROR = re.compile(
    r"^\s*(?:(?:logcat(?:\s+[^:\n]+)?|/system/bin/logcat):[^\n]*\b(?:permission denied|"
    r"operation not permitted|access denied)\b|(?:failed|unable) to (?:open|read|connect to)\b"
    r"[^\n]*\b(?:logd|log buffer)\b[^\n]*\b(?:permission denied|operation not permitted|access denied)\b|"
    r"(?:permission denied|operation not permitted|access denied)\s*$)",
    re.IGNORECASE | re.MULTILINE)
_ADB_CONNECTION_ERROR = re.compile(
    r"device (?:offline|unauthorized|not found)|no devices/emulators found|"
    r"device '[^']+' not found|error: closed|connection (?:reset|refused)", re.IGNORECASE)


def _text(output):
    return output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")


def adb_connection_error(out, err):
    return bool(_ADB_CONNECTION_ERROR.search(_text(err)[:512] + "\n" + _text(out)[:512]))


def _pid_number(value):
    if _PID.fullmatch(value):
        n = int(value)
        if 0 < n <= 2**31 - 1:
            return n
    return None


def find_package_pid(run_shell, package):
    """Use pidof when present; otherwise inspect ps for the exact main process."""
    if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
        return None
    code, out, err = run_shell("pidof " + package)
    if adb_connection_error(out, err):
        return None
    tokens = _text(out).split()
    pidof_ids = set()
    if code == 0 and tokens and all(_pid_number(token) is not None for token in tokens):
        pidof_ids = {_pid_number(token) for token in tokens}
        if len(pidof_ids) == 1:
            return next(iter(pidof_ids))

    for command in ("ps -A", "ps"):
        code, out, err = run_shell(command)
        if adb_connection_error(out, err):
            return None
        if code != 0:
            continue
        lines = _text(out).strip().splitlines()
        for index, line in enumerate(lines):
            columns = line.upper().split()
            name_column = next((columns.index(key) for key in ("NAME", "CMDLINE", "ARGS", "CMD", "COMMAND", "COMM")
                                if key in columns), None)
            if columns.count("PID") == 1 and name_column is not None:
                break
        else:
            continue
        pid_column = columns.index("PID")
        ids = set()
        for line in lines[index + 1:]:
            values = line.split()
            if len(values) >= len(columns) and (
                values[name_column] == package or
                # Some legacy ps headings omit the status column directly before NAME.
                columns[-1] == "NAME" and len(values) == len(columns) + 1
                and values[name_column] in ("D", "I", "R", "S", "T", "W", "X", "Z", "t")
                and values[-1] == package
            ):
                value = _pid_number(values[pid_column])
                if value is not None:
                    ids.add(value)
        return next(iter(ids)) if len(ids) == 1 and (not pidof_ids or ids <= pidof_ids) else None
    return None


def read_page_size(run_shell):
    """Read the guest page size without root or a host page-size assumption."""
    code, out, _ = run_shell("getconf PAGESIZE")
    value = _text(out).strip()
    if code != 0 or not re.fullmatch(r"[0-9]{4,6}", value):
        return None
    size = int(value)
    return size if 4096 <= size <= 65536 and size & (size - 1) == 0 else None


def detect_root_shell(run_shell, *, allow_su=True):
    """Select a shell grammar only after Android reports a real uid 0."""
    forms = [("id", _ROOT_SHELLS[0])]
    if allow_su:
        forms += [(prefix + "'id'", prefix) for prefix in _ROOT_SHELLS[1:]]
    for command, prefix in forms:
        code, out, err = run_shell(command)
        if code == 0 and any(_UID_ZERO.match(line.strip()) for line in _text(out).splitlines()):
            return prefix
        if adb_connection_error(out, err):
            break
    return None


def threadtime_lines(output, pid):
    """Keep only the selected PID while preserving that process's adjacency."""
    return [line for line in _text(output).splitlines()
            if (match := THREADTIME.fullmatch(line)) and _pid_number(match['pid']) == pid]


def read_threadtime_log(run_adb, pid, *, diagnostics=None):
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        return ""
    code, out, err = run_adb("logcat", "-d", "-v", "threadtime", "--pid=" + str(pid))
    def reported_error(output, stderr):
        first_lines = [line for line in _text(output).splitlines()[:8]
                       if not THREADTIME.fullmatch(line)]
        return "\n".join([_text(stderr)[:512], *(line[:512] for line in first_lines)])

    report = reported_error(out, err)
    if not (_LOGCAT_ACCESS_ERROR.search(report) or adb_connection_error(out, err)) and _LOGCAT_OPTION_ERROR.search(report):
        # Older logcat does not implement --pid. Bound the fallback dump and
        # filter locally before it reaches the scene parser.
        code, out, err = run_adb("logcat", "-d", "-t", "4096", "-v", "threadtime")
        report = reported_error(out, err)
    lines = threadtime_lines(out, pid) if code == 0 else []
    if not lines and diagnostics is not None and _LOGCAT_ACCESS_ERROR.search(report):
        diagnostics['scene_log_reason'] = 'capture_logcat_denied'
    return "\n".join(lines)
