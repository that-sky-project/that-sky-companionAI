"""Read-only, build-specific account-bound Sky cache capture (421629 only).

Requires an explicitly selected adb/device, matching local .so, running official
client, root read access and operator-supplied scene/resource revision. Never starts
or modifies the game, hooks ARM, sends game packets or prints raw account/session.
"""
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import xg_adb
from .android_compat import (adb_connection_error, detect_root_shell,
                             find_package_pid, read_page_size, read_threadtime_log, threadtime_lines)
from .device_scope import is_supported_emulator_serial
from .native_log import bind
from .native_profile import NATIVE_SHA256, BUILD, BINDING_ALGORITHM, account_binding


def atomic_bytes(path, data):
    """把 bytes 原子写到 path（同目录临时文件 + os.replace）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p

TEXT_RVA = 0x1fa0920
TEXT_PROOF = bytes.fromhex('286c01d00990905208694c791f01096a200100544840424a6940434a04080079')
GAME_SLOT = 0x4b6f6f0  # 0.16.8 page 0x4b6f000 + 0x6f0（0.16.7 +0x6c0，+0x30）
GAME_VTABLE = 0x36a3990  # 0.16.8 ctor 写入 adrp 0x36a3000 + 0x990
PACKAGE = 'com.netease.sky'

# exec-out 不回传远端退出码，su/sh 的报错文本会以“正常 stdout + 返回码 0”回来。
# 只认几个明确的 shell 报错前缀，避免把正常二进制数据当错误。
_SHELL_ERROR_MARKERS = (
    b'su: inaccessible or not found',
    b'su: not found',
    b'sh: su: inaccessible or not found',
    b'sh: su: not found',
    b'/system/bin/sh: su: inaccessible or not found',
    b'/system/bin/sh: su: not found',
    b'su: permission denied',
    b'su: invalid uid/gid',
)


def _shell_error(raw):
    return (raw or b'')[:512].strip().lower().startswith(_SHELL_ERROR_MARKERS)


def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while b:=f.read(1024*1024): h.update(b)
    return h.hexdigest()


def elf_loads(path):
    with Path(path).open('rb') as f:
        h=f.read(64)
        if len(h)!=64 or h[:6]!=b'\x7fELF\x02\x01':
            raise ValueError('expected little-endian ELF64')
        fields=struct.unpack('<16sHHIQQQIHHHHHH',h)
        if fields[2]!=183: raise ValueError('expected AArch64 .so')
        phoff, phentsize, phnum=fields[5],fields[9],fields[10]
        if phentsize!=56 or phnum>128: raise ValueError('invalid ELF program-header table')
        f.seek(phoff)
        out=[]
        for _ in range(phnum):
            a=struct.unpack('<IIQQQQQQ',f.read(56))
            if a[0]==1: out.append({'offset':a[2],'vaddr':a[3],'filesz':a[5],'memsz':a[6]})
        return out


def parse_maps(text):
    out=[]
    for line in text.splitlines():
        m=re.fullmatch(r'([0-9a-f]+)-([0-9a-f]+)\s+([rwxps-]+)\s+([0-9a-f]+)\s+\S+\s+\d+(?:\s+(.*))?',
                       line.strip(), flags=re.IGNORECASE)
        if m: out.append({'lo':int(m[1],16),'hi':int(m[2],16),'flags':m[3].lower(),
                         'offset':int(m[4],16),'path':m[5] or ''})
    return out


def load_bias(mappings, loads):
    # Legacy build-pinned capture path; validated_context rejects known non-4 KiB guests.
    biases=[]
    for m in mappings:
        if not m['path'].endswith('/libBootloader.so'): continue
        candidates=[p for p in loads if m['offset']==p['offset'] & ~4095]
        if len(candidates)==1:
            biases.append(m['lo']-(candidates[0]['vaddr'] & ~4095))
    if not biases or len(set(biases))!=1:
        raise ValueError('inconsistent ELF PT_LOAD/map load bias; no file-offset-as-RVA guess')
    return biases[0]



def validate_native_path(path):
    # Android package install directories can contain ~, =, -, _ and +. Keep the
    # exact package/lib scope, reject traversal and every shell metacharacter.
    pattern=r'/data/app/(?:[A-Za-z0-9_~+=-]+/)?com\.netease\.sky-[A-Za-z0-9_~+=-]+/lib/arm64/libBootloader\.so'
    if not isinstance(path,str) or not re.fullmatch(pattern,path):
        raise ValueError('unsupported mapped path; expected this package ARM64 library under /data/app')
    return path


def latest_scene(logtext, *, pid, scene, revision):
    # Restrict to target PID without changing adjacency in that process's log.
    try:
        rows=bind(threadtime_lines(logtext, pid),revision,'entered-world')
    except ValueError as exc:
        if str(exc).startswith('没有相邻的 NetVersion/levelName 行'):
            raise ValueError('capture_scene_log_missing') from None
        raise
    row=rows[-1]
    if row['scene']!=scene: raise ValueError('latest exact native scene event differs from requested scene')
    return row


class Device:
    def __init__(self, adb, serial):
        # adb 可以是路径，也可以是命令前缀列表（测试用 python 脚本模拟 adb）
        if not is_supported_emulator_serial(serial):
            raise ValueError('capture_device_scope')
        self.adb = list(adb) if isinstance(adb, (list, tuple)) else [str(adb)]
        self.serial = serial
        self.reads = []
        self._root_prefix = None

    def _run(self, args):
        # 2026-11-08 Claude：录图后台是无控制台进程（DETACHED_PROCESS），adb.exe 拿不到有效的
        # 标准输入句柄会失败（“adb 命令失败”，前台跑就好）。统一走 xg_exec：Windows 上
        # stdin=DEVNULL、CREATE_NO_WINDOW；POSIX 不变。
        try:
            return xg_adb.run(self.adb, ['-s', self.serial, *args],
                              capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError('capture_adb_timeout') from None
        except FileNotFoundError:
            raise RuntimeError('capture_adb_missing') from None
        except PermissionError:
            raise RuntimeError('capture_adb_permission') from None
        except OSError as exc:
            kind = ('capture_adb_permission' if getattr(exc, 'winerror', None) in (225, 226)
                    else 'capture_adb_launch_failed')
            raise RuntimeError(kind) from None

    def _run_tuple(self, *args):
        p = self._run(list(args))
        if (p.stderr or b'').startswith(xg_adb.SERVER_CONFLICT.encode('ascii')):
            raise RuntimeError('capture_adb_conflict')
        # exec-out may carry binary output; do not scan successful reads for text.
        if adb_connection_error(p.stdout if p.returncode or not args or args[0] != 'exec-out' else b'', p.stderr):
            raise RuntimeError('capture_adb_disconnected')
        return p.returncode, p.stdout or b'', p.stderr or b''

    def call(self, args):
        if args and isinstance(args[-1], str) and args[-1].startswith('su -c '):
            if not self._root_prefix:
                self._root_prefix = detect_root_shell(lambda command: self._run_tuple('shell', command))
            if not self._root_prefix:
                raise RuntimeError('capture_root_unavailable')
            args = [*args[:-1], self._root_prefix + args[-1][len('su -c '):]]
        code, out, err = self._run_tuple(*args)
        if len(out) > 16 * 1024 * 1024:
            raise RuntimeError('adb response exceeds bound')
        if _shell_error(out) or _shell_error(err):
            raise RuntimeError('capture_root_unavailable')
        if args and '/maps' in args[-1]:
            report = (err[:512] + b'\n' + out[:512]).lower()
            if b'permission denied' in report or b'operation not permitted' in report:
                raise RuntimeError('capture_maps_denied')
            if b'no such file or directory' in report:
                raise RuntimeError('capture_game_exited')
        if args and (proc_match := re.search(r'/proc/[0-9]+/mem\b', args[-1])):
            # Binary exec-out is not searched as general text: only an explicit
            # utility error prefix can identify a denial on stdout.
            report = err[:512].lower()
            if out.startswith(b'dd:') and proc_match[0].encode('ascii') in out[:512]:
                report += b'\n' + out[:512].lower()
            if b'permission denied' in report or b'operation not permitted' in report:
                raise RuntimeError('capture_mem_denied')
        if code:
            raise RuntimeError('adb operation failed')
        return out

    def game_pid(self):
        return find_package_pid(lambda command: self._run_tuple('shell', command), PACKAGE)

    def scene_log(self, pid):
        diagnostics = {}
        text = read_threadtime_log(self._run_tuple, pid, diagnostics=diagnostics)
        if diagnostics.get('scene_log_reason') == 'capture_logcat_denied':
            raise RuntimeError('capture_logcat_denied')
        return text

    def read(self,address,size,label,*,digest=True):
        if not 0<size<=16384 or not any(m['lo']<=address and address+size<=m['hi'] and 'r' in m['flags'] for m in self.maps):
            raise ValueError(f'unreadable or out-of-bounds region: {label}')
        out=self.call(['exec-out',f"su -c 'dd if=/proc/{self.pid}/mem bs=1 skip={address} count={size} 2>/dev/null'"])
        if len(out)!=size:
            raise RuntimeError(f'short native read: {label}')
        item={'label':label,'address':hex(address),'size':size}
        if digest:item['sha256']=hashlib.sha256(out).hexdigest()
        self.reads.append(item)
        return out

    def pointer(self,address,label):
        value=struct.unpack('<Q',self.read(address,8,label))[0]
        if value==0 or value&7: raise ValueError(f'null/unaligned pointer: {label}')
        return value


def commit_private_record(private_root,record_dir,files):
    """Write all files in a sibling staging dir, then one rename (no half profile).

    Windows rename refuses an existing target; an existing record is never removed.
    Cleanup is restricted to the newly created, verified staging directory only.
    """
    root=Path(private_root).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',record_dir):raise ValueError('invalid new private record name')
    if not root.is_dir():raise ValueError('private root must already exist')
    out=root/record_dir
    if out.exists():raise FileExistsError('private record already exists')
    stage=Path(tempfile.mkdtemp(prefix='.capture-staging-',dir=root)).resolve()
    if not stage.is_relative_to(root):raise ValueError('staging escaped private root')
    try:
        for name,data in files.items():
            if Path(name).name!=name or name in ('.','..'):raise ValueError('invalid private filename')
            atomic_bytes(stage/name,data)
        if out.exists():raise FileExistsError('private record appeared during capture')
        os.rename(stage,out)
    finally:
        if stage.exists():
            if stage.resolve().parent!=root or not stage.name.startswith('.capture-staging-'):
                raise ValueError('unsafe staging cleanup path')
            shutil.rmtree(stage)
    return out


def validated_context(a):
    if sha256_file(a.native_library)!=NATIVE_SHA256:
        raise ValueError('unknown local native library; redo cross-version analysis instead of reusing offsets')
    loads=elf_loads(a.native_library)
    d=Device(a.adb,a.serial)
    d.pid=d.game_pid()
    if d.pid is None: raise ValueError('expected exactly one running official game process')
    page_size=read_page_size(lambda command: d._run_tuple('shell', command))
    if page_size is not None and page_size!=4096:
        raise RuntimeError('capture_page_size_unsupported')
    package=d.call(['shell','dumpsys package '+PACKAGE]).decode('utf-8','replace')
    if not re.search(r'\bversionCode=421629\b',package): raise ValueError('installed package build is not421629')
    del package
    d.maps=parse_maps(d.call(['shell',f"su -c 'cat /proc/{d.pid}/maps'"]).decode('utf-8','replace'))
    if not d.maps: raise ValueError('capture_maps_empty')
    paths={m['path'] for m in d.maps if m['path'].endswith('/libBootloader.so')}
    if len(paths)!=1: raise ValueError('missing or ambiguous loaded libBootloader path')
    native_path=next(iter(paths))
    d.native_path=native_path
    validate_native_path(native_path)
    line=d.call(['shell',f"su -c 'sha256sum {native_path}'"]).decode().strip()
    if not line or line.split()[0]!=NATIVE_SHA256:
        raise ValueError('installed mapped native SHA256 does not match known build')
    bias=load_bias(d.maps,loads)
    if d.read(bias+TEXT_RVA,len(TEXT_PROOF),'stable_constructor_instructions')!=TEXT_PROOF:
        raise ValueError('runtime text proof mismatch; refusing pointer reads')
    game=d.pointer(bias+GAME_SLOT,'game_root')
    if d.pointer(game,'game_vptr')!=bias+GAME_VTABLE: raise ValueError('Game vtable mismatch')
    n=d.pointer(game+0x7ec0,'netmodule')
    nv_raw=d.read(n+0x99b78,12,'netversion_before')
    nv=list(struct.unpack('<6H',nv_raw))
    if nv[2:]!=[23536,0,0,25536]: raise ValueError('native NetVersion build invariants mismatch')
    logs=d.scene_log(d.pid)
    observation=latest_scene(logs,pid=d.pid,scene=a.scene,revision=a.resource_revision)
    del logs
    if observation['netversion_u16']!=nv: raise ValueError('native scene event/NetVersion mismatch')
    return d,bias,game,n,nv_raw,observation


