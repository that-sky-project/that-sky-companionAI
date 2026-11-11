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

import xg_exec
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
    b'inaccessible or not found',
)


def _shell_error(raw):
    head = (raw or b'')[:512]
    for marker in _SHELL_ERROR_MARKERS:
        if marker in head:
            return head.decode('utf-8', 'replace').strip()[:300]
    return ''


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
        m=re.match(r'([0-9a-f]+)-([0-9a-f]+) ([rwxps-]+) ([0-9a-f]+) \S+ \d+(?:\s+(.*))?$',line)
        if m: out.append({'lo':int(m[1],16),'hi':int(m[2],16),'flags':m[3],
                         'offset':int(m[4],16),'path':m[5] or ''})
    return out


def load_bias(mappings, loads):
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
    lines=[]
    for line in logtext.splitlines():
        m=re.match(r'^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\s+(\d+)\s+',line)
        if m and int(m[1])==pid: lines.append(line)
    rows=bind(lines,revision,'entered-world')
    row=rows[-1]
    if row['scene']!=scene: raise ValueError('latest exact native scene event differs from requested scene')
    return row


class Device:
    def __init__(self, adb, serial):
        # adb 可以是路径，也可以是命令前缀列表（测试用 python 脚本模拟 adb）
        self.adb = list(adb) if isinstance(adb, (list, tuple)) else [str(adb)]
        self.serial = serial
        self.reads = []

    def _run(self, args):
        # 2026-11-08 Claude：录图后台是无控制台进程（DETACHED_PROCESS），adb.exe 拿不到有效的
        # 标准输入句柄会失败（“adb 命令失败”，前台跑就好）。统一走 xg_exec：Windows 上
        # stdin=DEVNULL、CREATE_NO_WINDOW；POSIX 不变。
        return xg_exec.run([*self.adb, '-s', self.serial, *args],
                           capture_output=True, timeout=30)

    def _root_shell(self):
        """adbd 自己是不是 root（MuMu 开 Root 后是 uid=0，且往往没有 su 可执行文件）。

        2026-11-08 Claude：`exec-out` 不回传远端退出码，`su: inaccessible or not found`
        会以返回码 0 + 报错文本的形式回来，被当成内存数据（short native read）。
        所以先探测一次：adbd 已是 root 就把所有 `su -c` 改成 `sh -c`。
        """
        if getattr(self, "_rootsh", None) is None:
            try:
                p = self._run(['shell', 'id'])
                text = p.stdout or b''
                self._rootsh = (p.returncode == 0 and
                                (b'uid=0(' in text or re.search(rb'\buid=0\b', text) is not None))
            except Exception:
                self._rootsh = False
        return self._rootsh

    def call(self, args):
        if args and isinstance(args[-1], str) and args[-1].startswith("su -c ") and self._root_shell():
            args = [*args[:-1], "sh -c " + args[-1][len("su -c "):]]
        p = self._run(args)
        if p.returncode and args and isinstance(args[-1], str) and args[-1].startswith("su -c "):
            # MuMu 开了 Root 后 adbd 自己就是 root，sh -c 与 su -c 等价；
            # 有些实例没有 su 可执行文件，这里自动降级一次。
            p = self._run([*args[:-1], "sh -c " + args[-1][len("su -c "):]])
        if p.returncode:
            raise RuntimeError('adb operation failed; no game data printed; inspect local device connection/root status')
        if len(p.stdout) > 16 * 1024 * 1024:
            raise RuntimeError('adb response exceeds bound')
        shell_error = _shell_error(p.stdout)
        if shell_error:
            raise RuntimeError('remote root shell unavailable: ' + shell_error)
        return p.stdout

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
    pid=d.call(['shell','pidof '+PACKAGE]).decode().strip()
    if not re.fullmatch(r'\d+',pid): raise ValueError('expected exactly one running official game process')
    d.pid=int(pid)
    package=d.call(['shell','dumpsys package '+PACKAGE]).decode('utf-8','replace')
    if not re.search(r'\bversionCode=421629\b',package): raise ValueError('installed package build is not421629')
    del package
    d.maps=parse_maps(d.call(['shell',f"su -c 'cat /proc/{d.pid}/maps'"]).decode())
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
    logs=d.call(['logcat','-d','-v','threadtime','--pid='+pid]).decode('utf-8','replace')
    observation=latest_scene(logs,pid=d.pid,scene=a.scene,revision=a.resource_revision)
    del logs
    if observation['netversion_u16']!=nv: raise ValueError('native scene event/NetVersion mismatch')
    return d,bias,game,n,nv_raw,observation


