"""Opt-in scheduling guard for the already validated MuMu Sky PID.

No discovery, memory write, session read or retry. Device watchdog, shell traps
and independent host-finally CONT. Not an atomic or hard-real-time guarantee.
"""
import re
import shlex

PACKAGE = b'com.netease.sky'
HEADER = b'SKY_PAIR_STOPPED_V1\n'
TRAILER = b'\nSKY_PAIR_RESUMED_V1\n'
WATCHDOG_SECONDS = 2


def _pid(d):
    # 只允许本机 MuMu 的 adb serial（127.0.0.1:16384 + 32*n / 7555）或模拟器 serial。
    if not isinstance(getattr(d, 'serial', None), str) or not re.fullmatch(
            r'(?:127\.0\.0\.1:\d{4,5}|emulator-\d+)', d.serial):
        raise ValueError('current_profile_pause_device_scope')
    if type(d.pid) is not int or not 0 < d.pid <= 2**31-1:
        raise ValueError('current_profile_pause_pid')
    return d.pid


def parse_identity(raw, pid):
    if type(raw) is not bytes or len(raw) > 8192:
        raise ValueError('current_profile_pause_identity')
    cmd, sep, stat = raw.partition(b'\n')
    if not sep or cmd.split(b'\0', 1)[0] != PACKAGE:
        raise ValueError('current_profile_pause_package')
    match = re.fullmatch(rb'(\d+) \(.*\) ([A-Za-z]) (.+)', stat.rstrip(b'\n'))
    if not match or int(match[1]) != pid:
        raise ValueError('current_profile_pause_identity')
    # stat field 22; captured tail starts at field 4.
    rest = match[3].split()
    if len(rest) < 19 or not rest[18].isdigit():
        raise ValueError('current_profile_pause_identity')
    start = int(rest[18])
    if not 0 < start < 2**64:
        raise ValueError('current_profile_pause_identity')
    if match[2] in (b'T', b't', b'Z', b'X', b'x'):
        raise ValueError('current_profile_pause_preexisting_stop_or_exit')
    return start


def identity_guard(pid, start):
    return f'''set -f
same_process() {{
    [ -r /proc/{pid}/stat ] || return 1
    line=$(cat /proc/{pid}/stat) || return 1
    rest=${{line##*) }}
    set -- $rest
    [ "${{20}}" = "{start}" ]
}}
process_state() {{
    same_process || return 1
    set -- $rest
    printf "%s" "$1"
}}
resume_target() {{
    same_process || return 1
    kill -CONT {pid} 2>/dev/null
}}
'''


def build_scripts(pid, start, commands):
    if type(pid) is not int or not 0 < pid <= 2**31-1 or type(start) is not int or not 0 < start < 2**64:
        raise ValueError('current_profile_pause_identity')
    if type(commands) is not list or len(commands) != 20:
        raise ValueError('current_profile_pause_read_plan')
    for command in commands:
        m = re.fullmatch(rf'dd if=/proc/{pid}/mem bs=1 skip=(\d+) count=(\d+) 2>/dev/null', command)
        if not m or not 0 < int(m[2]) <= 16384 or not 0 < int(m[1]) < 2**64-int(m[2]):
            raise ValueError('current_profile_pause_read_plan')
    guard = identity_guard(pid, start)
    # Rescue armed before STOP; background watchdog does not hold any ADB pipe.
    script = guard + f'''watchdog=
cleanup() {{
    if resume_target >/dev/null 2>&1; then
        if [ -n "$watchdog" ]; then kill "$watchdog" 2>/dev/null || :; fi
    fi
}}
trap cleanup EXIT HUP INT TERM
same_process || exit 61
( sleep {WATCHDOG_SECONDS}; resume_target ) </dev/null >/dev/null 2>&1 &
watchdog=$!
kill -STOP {pid} 2>/dev/null || exit 62
stopped=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    state=$(process_state) || exit 63
    if [ "$state" = T ]; then stopped=1; break; fi
    sleep 0.01
done
[ "$stopped" = 1 ] || exit 64
printf "SKY_PAIR_STOPPED_V1\\n"
''' + ' && '.join(commands) + ''' || exit 65
state=$(process_state) || exit 66
[ "$state" = T ] || exit 67
resume_target || exit 68
resumed=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    state=$(process_state) || exit 69
    case "$state" in T|t|Z|X|x) ;; *) resumed=1; break ;; esac
    sleep 0.01
done
[ "$resumed" = 1 ] || exit 70
printf "\\nSKY_PAIR_RESUMED_V1\\n"
'''
    # Gone/reused PID: do not signal the replacement process.
    rescue = guard + '''if ! same_process; then exit 0; fi
resume_target || exit 71
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    state=$(process_state) || exit 0
    case "$state" in T|t) ;; *) exit 0 ;; esac
    sleep 0.01
done
exit 72
'''
    return script, rescue


def read_paused_pair(d, commands, expected_size):
    pid = _pid(d)
    identity = d.call(['exec-out', 'su -c '+shlex.quote(
        f'cat /proc/{pid}/cmdline; printf "\\n"; cat /proc/{pid}/stat')])
    start = parse_identity(identity, pid)
    script, rescue = build_scripts(pid, start, commands)
    output = None
    try:
        output = d.call(['exec-out', 'su -c '+shlex.quote(script)])
    finally:
        # Always attempted after STOP may have been sent, including interruption.
        try:
            d.call(['exec-out', 'su -c '+shlex.quote(rescue)])
        except BaseException:
            raise ValueError('current_profile_pause_resume_unconfirmed') from None
    if (len(output) != len(HEADER)+expected_size+len(TRAILER) or
        not output.startswith(HEADER) or not output.endswith(TRAILER)):
        raise ValueError('current_profile_pause_frame_or_resume_missing')
    return output[len(HEADER):-len(TRAILER)]
