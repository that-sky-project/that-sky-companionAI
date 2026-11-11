# -*- coding: utf-8 -*-
"""从用户 APK 现场生成 115 张图的 scene/cache 输入（catalog-inputs）。

这是 T9 ``build_catalog_candidates_v32.py`` 的本机化版本，只保留生成所需算法：

  * ``libBootloader.so`` 从用户的 APK 里读（不读程序目录、不落盘 .so）；
  * 用 venv 里的 ``unicorn`` 在进程内模拟执行 APK 里**指定的** native 构造器
    和 cache 序列化函数；执行前用 ``capstone`` 做静态指令/控制流白名单校验，
    执行时再用 block 白名单 + 内存写白名单兜底；
  * 用 APK 里的 ``assets/Data/Levels/<Scene>/Objects.level.bin``（TGCL v1）
    算 netversion / finite group counts；
  * 输出 ``<cache_dir>/<Scene>.native.bin/.scene.bin/.cache.bin/.json`` 与
    ``index.json``，可续跑。

没有 vendor 目录、没有 Codex 工程路径、没有网络/游戏/系统写操作。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import struct
import time
from collections import Counter
from pathlib import Path

from .common import (APK_BUILD, GameDataError, Progress, atomic_write, canonical_json,
                     game_dir, sha256_bytes)
from .spawn_loader import fnv32
from .tgcl_scene import Ref, Scene

SCHEMA = "sky-catalog-partial-scene-inputs-v32"
INDEX_SCHEMA = "xg-catalog-scene-inputs-index-v1"
PHASE = "finite_initializers_only_before_first_event_update"
MISSING = [
    "non-finite object initialization and MPVar membership/values",
    "native event allocation order and enabled/onLoad/first-update effects",
    "fixed-array map-specific initialization and entity dictionary",
    "resource revision agreement and post-connection room state reconciliation",
]
CACHE_HEADER = bytes.fromhex("c063000000000000000005007d00")
CACHE_SIZE = 8208
NATIVE_RECORD_SIZE = 25536
NATIVE_MEMBER = "lib/arm64-v8a/libBootloader.so"
EVIDENCE_NAME = "scene_evidence.json"

# 序列化入口与 review 过的函数地址（来自 T9 emulate_scene_cache_v32.py 的 ALLOWED；
# 来源：scene-serializer-deps-01/02.asm.txt、levelobjects-factory-and-scene-serializer-02.asm.txt）
ENTRY = 0x1DA5F48  # 0.16.8 serializer wrapper (0.16.7 0x1DA5774, +0x7d4)
ALLOWED = [
    0x1DA5F48, 0x3260B14, 0x1DA4A38, 0x32606AC,
    0x12B4D30, 0x12EE9C4, 0x17EEDE4, 0x19A3220, 0x19C2318, 0x19C24D0,
    0x1DF46DC, 0x1DF4A34, 0x1DF4DDC, 0x1EB8350, 0x1F0157C, 0x1F7C9EC,
    0x1F9013C, 0x1F925D8, 0x1F92930, 0x21758DC, 0x222BF50, 0x22C2FE0,
    0x276CC7C, 0x27A2B2C, 0x28430BC, 0x29F3578, 0x2FAE4A0, 0x2FB5AEC,
    0x325FB54, 0x325FD04, 0x325FED8, 0x32601B4, 0x3260EE8, 0x3260F40,
    0x3260F6C, 0x3260F98, 0x3260FC4, 0x3261024, 0x32610D8, 0x326118C,
    0x3261B5C, 0x3261CB4, 0x3262090, 0x326233C, 0x3262420, 0x32624FC,
    0x32626B4, 0x3263274, 0x33EA168,
]
# 构造器入口（T9 emulate_scene_defaults_v32.py 的 CONSTRUCTORS）
CTOR_ENTRY = 0x1DA3BC4  # 0.16.8 (+0x7d4)
CTOR_ALLOWED = [
    0x1DA3BC4, 0x12EE99C, 0x19A31B8, 0x19C22C0, 0x19C247C, 0x1EB82F0,
    0x1F01528, 0x22C2FC0, 0x276CC18, 0x284306C, 0x29F3528, 0x2FAE47C,
]
# 被显式接管、不真正执行的几个地址
HOTPATCH_STUB = 0x311DB48
MEMSET_STUB = 0x35E4AE0
MEMCPY_STUB = 0x35E48E0
ENTITY_CHECK = 0x1F9013C
SPECIAL = {HOTPATCH_STUB, MEMSET_STUB, MEMCPY_STUB, ENTITY_CHECK}
FORBIDDEN = {"br", "blr", "eret", "svc", "hlt", "brk", "udf", "drps"}

# 构造器里 4 个数据哨兵全局的零初始化指令（T9 scene_default_globals_v32.py）
ZERO_INITIALIZERS = (
    (0x49391B0, 0x19B308C, "x20", 0x4939000, 0x1B0, 0x19B30E8),
    (0x4BD3910, 0x1EBE654, "x20", 0x4BD3000, 0x910, 0x1EBE6BC),
    (0x4C6F320, 0x2799D1C, "x21", 0x4C6F000, 0x320, 0x2799D78),
    (0x4BDE900, 0x1F9069C, "x19", 0x4BDE000, 0x900, 0x1F906F8),
)

DEST = 0x10000000
SIZE = 0x63C0
CACHE = 0x11000000
TABLE = 0x12000000
STACK = 0x20000000
STOP = 0x30000000
CONSTRUCTOR_STEPS = 200001
SERIALIZER_STEPS = 2000001
BLOCK_BUDGET = 300000
# T9 的 SPECS 迭代顺序（同 rebuild_all 的 applied_groups/native_count_writes 顺序）
GROUP_ORDER = ("move_rails", "floaty", "rotations")


def _require_native_deps():
    try:
        import unicorn  # noqa: F401
        import capstone  # noqa: F401
    except ImportError as exc:
        raise GameDataError(
            "缺少 native 模拟依赖：%s。请运行 pip install -r requirements.txt"
            "（unicorn==2.1.4、capstone>=5.0）。" % exc, "native_deps_missing") from None
    return unicorn, capstone


# ---------------------------------------------------------------- APK / native
class NativeImage:
    """只读 ELF64 视图：段、函数 span、重定位。raw 来自 APK，写任何文件。"""

    def __init__(self, raw: bytes, expected_sha: str):
        self.raw = raw
        self.sha = sha256_bytes(raw)
        if self.sha != expected_sha:
            raise GameDataError("APK 里的 libBootloader.so 不是本版本：%s" % self.sha,
                                "native_sha_mismatch")
        phoff = struct.unpack_from("<Q", raw, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", raw, 54)
        self.segs = [struct.unpack_from("<II6Q", raw, phoff + i * phentsize) for i in range(phnum)]
        eh = next((s for s in self.segs if s[0] == 0x6474E550), None)
        if eh is None:
            raise GameDataError("libBootloader.so 缺 .eh_frame（无法定位函数边界）", "native_eh_frame")
        table = self.read_va(eh[3], eh[5])
        count = struct.unpack_from("<I", table, 8)[0]
        spans = []
        for j in range(count):
            sd, fd = struct.unpack_from("<ii", table, 12 + 8 * j)
            fva = eh[3] + fd
            _length, _cie, rel, extent = struct.unpack("<IIiI", self.read_va(fva, 16))
            lo = fva + 8 + rel
            if lo == eh[3] + sd:
                spans.append((lo, lo + extent))
        self.spans = sorted(spans)
        self.starts = [s[0] for s in self.spans]

    def read_va(self, addr: int, size: int) -> bytes:
        for t, _fl, off, va, _pa, fs, _ms, _al in self.segs:
            if t == 1 and va <= addr and addr + size <= va + fs:
                return self.raw[off + addr - va: off + addr - va + size]
        raise GameDataError("native 地址不可读：0x%x+%d" % (addr, size), "native_unmapped")

    def span(self, addr: int):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i >= 0 and addr < self.spans[i][1]:
            return self.spans[i]
        return None

    def relocations(self) -> dict:
        if hasattr(self, "_relocations"):
            return self._relocations
        seg = next((s for s in self.segs if s[0] == 2), None)
        if seg is None:
            self._relocations = {}
            return self._relocations
        raw = self.read_va(seg[3], seg[5])
        tags = {}
        for i in range(0, len(raw), 16):
            tag, value = struct.unpack_from("<QQ", raw, i)
            if not tag:
                break
            tags[tag] = value
        out = {}
        for ptrkey, sizekey in ((7, 8), (23, 2)):
            if ptrkey not in tags:
                continue
            blob = self.read_va(tags[ptrkey], tags[sizekey])
            for i in range(0, len(blob), 24):
                where, info, addend = struct.unpack_from("<QQq", blob, i)
                out[where] = {"type": info & 0xFFFFFFFF, "symbol": info >> 32, "addend": addend}
        self._relocations = out
        return out

    def disassemble(self, lo: int, hi: int):
        _unicorn, capstone = _require_native_deps()
        md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
        return list(md.disasm(self.read_va(lo, hi - lo), lo))


def _sentinel_proof(native: NativeImage) -> dict:
    """capstone 断言：4 个数据哨兵全局的零初始化指令与 T9 证明一致。"""
    checks = []
    for address, start, register, page, delta, store in ZERO_INITIALIZERS:
        lines = ["%08x: %-8s %s" % (i.address, i.mnemonic, i.op_str)
                 for i in native.disassemble(start, store + 4)]
        expected = {
            start: "adrp     %s, #%s" % (register, hex(page)),
            start + 4: "add      %s, %s, #%s" % (register, register, hex(delta)),
            store: "str      wzr, [%s]" % register,
        }
        for location, text in expected.items():
            actual = lines[(location - start) // 4].split(": ", 1)[1]
            if actual != text:
                raise GameDataError(
                    "capstone 哨兵指令校验失败：0x%x 期望 %r 实际 %r" % (location, text, actual),
                    "native_sentinel_mismatch")
        for line in lines[2:-1]:
            _, instruction = line.split(": ", 1)
            mnemonic, *operands = instruction.split()
            if operands and operands[0].rstrip(",") == register and mnemonic not in ("str", "stp", "cmp"):
                raise GameDataError("capstone 哨兵基址被改写：%s" % line, "native_sentinel_clobber")
        checks.append({"address": hex(address), "zero_store": hex(store), "value_u32": 0})
    return {"schema": "sky-scene-default-data-globals-v32", "nativeSHA256": native.sha,
            "globals": checks, "runtime_hotpatch_guards_not_inferred": True}


def static_whitelist(native: NativeImage, entry: int, allowed: list[int]) -> dict:
    """执行前的 capstone 静态白名单校验。

    只检查可静态判定的事实，任何一条不满足都失败关闭：
      1. 每个 review 过的入口都能落到一个 .eh_frame 函数 span 内、4 字节对齐；
      2. span 内每条指令都能被 capstone 解码，且没有 br/blr/eret/svc/hlt/brk/udf
         （这些会让控制流离开可静态检查的范围）；
      3. 入口确实是 span 内的指令起点。
    对直接 b/bl/条件跳转，记录目标是否在 span 内；不在的目标由运行时 block
    白名单在真正进入前拒绝（并给出中文错误）。
    """
    spans = sorted({native.span(a) for a in allowed if native.span(a)})
    if not spans:
        raise GameDataError("native 函数 span 为空", "native_no_spans")
    starts = set()
    indirect = []
    branch_targets = Counter()
    outside_targets = set()
    total = 0
    for lo, hi in spans:
        insns = native.disassemble(lo, hi)
        covered = 0
        for ins in insns:
            covered += ins.size
            total += 1
            starts.add(ins.address)
            if ins.mnemonic in FORBIDDEN:
                indirect.append((hex(ins.address), ins.mnemonic))
            if ins.mnemonic == "b" or ins.mnemonic == "bl" or ins.mnemonic.startswith("b."):
                if "#" in ins.op_str:
                    try:
                        target = int(ins.op_str.rsplit("#", 1)[1], 16)
                    except ValueError:
                        continue
                    branch_targets[ins.mnemonic] += 1
                    if target not in SPECIAL and not any(a <= target < b for a, b in spans):
                        outside_targets.add(target)
        if covered != hi - lo:
            raise GameDataError("native span 0x%x..0x%x 有未解码字节" % (lo, hi),
                                "native_undecodable")
    if indirect:
        raise GameDataError("native 白名单里出现间接控制流：%s" % indirect[:4],
                            "native_indirect_flow")
    if entry not in starts:
        raise GameDataError("native 入口 0x%x 不是指令起点" % entry, "native_entry")
    for a in allowed:
        if a not in starts:
            raise GameDataError("native 白名单地址 0x%x 不是指令起点" % a, "native_allowed_entry")
    sentinel = _sentinel_proof(native)
    return {
        "schema": "xg-native-static-whitelist-v1",
        "native_sha256": native.sha,
        "entry": hex(entry),
        "allowed_entries": [hex(a) for a in allowed],
        "function_spans": [[hex(lo), hex(hi)] for lo, hi in spans],
        "instructions_verified": total,
        "indirect_control_flow": [],
        "direct_branch_targets": dict(branch_targets),
        "outside_span_targets_not_executed": sorted(hex(a) for a in outside_targets),
        "executed_by": "unicorn 2.1.4（进程内，映射 APK 的 PT_LOAD；block 白名单 + 内存写白名单兜底）",
        "capstone_sentinel": sentinel,
    }


# ---------------------------------------------------------------- emulator
class SceneEmulator:
    """一次映射 libBootloader.so，反复跑构造器/序列化；无法写 .so、无法碰系统。"""

    def __init__(self, native: NativeImage, progress: Progress | None = None):
        unicorn, _capstone = _require_native_deps()
        self.u = unicorn
        from unicorn import arm64_const
        self.R = arm64_const
        self.native = native
        self.uc = unicorn.Uc(unicorn.UC_ARCH_ARM64, unicorn.UC_MODE_ARM)
        ranges = []
        for t, _fl, _off, va, _pa, _fs, ms, _al in native.segs:
            if t == 1:
                ranges.append((va & ~4095, (va + ms + 4095) & ~4095))
        merged = []
        for lo, hi in sorted(ranges):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        for lo, hi in merged:
            self.uc.mem_map(lo, hi - lo)
        for t, _fl, off, va, _pa, fs, _ms, _al in native.segs:
            if t == 1:
                self.uc.mem_write(va, native.raw[off:off + fs])
        for where, rel in native.relocations().items():
            if rel["type"] == 1027 and not rel["symbol"]:
                self.uc.mem_write(where, struct.pack("<Q", rel["addend"]))
        for base, size in ((DEST, 0x10000), (CACHE, 0x3000), (TABLE, 0x30000),
                           (STACK, 0x10000), (STOP, 0x1000)):
            self.uc.mem_map(base, size)
        self.zero_proof = _sentinel_proof(native)
        for address in self.zero_proof["globals"]:
            self.uc.mem_write(int(address["address"], 16), bytes(4))
        # 显式“没有性能追踪器”的正常路径策略
        self.uc.mem_write(0x4BDEBC0, bytes(8))
        self.serializer_spans = self._spans(ALLOWED)
        self.constructor_spans = self._spans(CTOR_ALLOWED)
        self.phase = "serialize"
        self.failures: list[str] = []
        self.blocks = 0
        self.written: set[int] = set()
        self.entity_zero_lookups = 0
        self._hook()
        if progress:
            progress.emit("native 已映射（%.1f MB）" % (sum(hi - lo for lo, hi in merged) / 1048576.0))

    def _spans(self, allowed):
        spans = sorted({self.native.span(a) for a in allowed if self.native.span(a)})
        return spans, [s[0] for s in spans], [s[1] for s in spans]

    def _inside(self, addr: int, size: int) -> bool:
        spans, starts, ends = self.serializer_spans if self.phase == "serialize" else self.constructor_spans
        i = bisect.bisect_right(starts, addr) - 1
        return i >= 0 and addr + size <= ends[i]

    def _scoped(self, addr: int, size: int) -> bool:
        if DEST <= addr and addr + size <= DEST + SIZE:
            return True
        if STACK <= addr and addr + size <= STACK + 0x10000:
            return True
        if self.phase == "serialize" and CACHE <= addr and addr + size <= CACHE + 0x2010:
            return True
        return False

    def _stop(self, reason: str):
        self.failures.append(reason)
        self.uc.emu_stop()

    def _hook(self):
        u = self.uc
        R = self.R

        def block(uc, addr, size, _):
            self.blocks += 1
            if self.blocks > BLOCK_BUDGET:
                self._stop("block_budget")
                return
            if addr in SPECIAL:
                return
            if not self._inside(addr, size):
                self._stop("unreviewed_native_block_at_" + hex(addr))

        def stub(uc, addr, size, _):
            if addr == HOTPATCH_STUB:
                uc.reg_write(R.UC_ARM64_REG_X1, 0)
                uc.reg_write(R.UC_ARM64_REG_PC, uc.reg_read(R.UC_ARM64_REG_LR))
                return
            if addr == ENTITY_CHECK:
                self.entity_zero_lookups += 1
                if uc.reg_read(R.UC_ARM64_REG_X1) != 0:
                    self._stop("nonzero_entity_requires_current_dictionary")
                return
            if addr in (MEMSET_STUB, MEMCPY_STUB):
                dst = uc.reg_read(R.UC_ARM64_REG_X0)
                arg = uc.reg_read(R.UC_ARM64_REG_X1)
                length = uc.reg_read(R.UC_ARM64_REG_X2)
                if length > 0x10000 or not self._scoped(dst, length):
                    self._stop("libc_write_outside_scoped_memory")
                    return
                data = bytes([arg & 255]) * length if addr == MEMSET_STUB else bytes(uc.mem_read(arg, length))
                uc.mem_write(dst, data)
                if CACHE <= dst and dst + length <= CACHE + 0x2010:
                    self.written.update(range(dst - CACHE, dst - CACHE + length))
                uc.reg_write(R.UC_ARM64_REG_PC, uc.reg_read(R.UC_ARM64_REG_LR))

        def write(uc, _access, addr, size, _value, _):
            if not self._scoped(addr, size):
                self._stop("write_outside_serializer_memory_at_" + hex(addr))
                return
            if CACHE <= addr and addr + size <= CACHE + 0x2010:
                self.written.update(range(addr - CACHE, addr - CACHE + size))

        def invalid(uc, _access, addr, size, _value, _):
            self._stop("unmapped_access_at_" + hex(addr))
            return False

        u.hook_add(self.u.UC_HOOK_BLOCK, block)
        for addr in sorted(SPECIAL):
            u.hook_add(self.u.UC_HOOK_CODE, stub, begin=addr, end=addr + 4)
        u.hook_add(self.u.UC_HOOK_MEM_WRITE, write)
        u.hook_add(self.u.UC_HOOK_MEM_INVALID, invalid)

    def _reset(self, body: bytes):
        R = self.R
        self.failures = []
        self.blocks = 0
        self.written = set()
        self.entity_zero_lookups = 0
        self.uc.mem_write(STACK, bytes(0x10000))
        self.uc.mem_write(DEST, body)
        self.uc.mem_write(CACHE, b"\xa5" * 0x2010)
        self.uc.reg_write(R.UC_ARM64_REG_X0, DEST)
        self.uc.reg_write(R.UC_ARM64_REG_X1, CACHE)
        self.uc.reg_write(R.UC_ARM64_REG_X2, TABLE)
        self.uc.reg_write(R.UC_ARM64_REG_SP, STACK + 0xF000)
        self.uc.reg_write(R.UC_ARM64_REG_LR, STOP)

    def _run(self, entry: int, limit: int):
        R = self.R
        try:
            self.uc.emu_start(entry, STOP, count=limit)
        except self.u.UcError as exc:
            self.failures.append("unicorn:" + str(exc))
        pc = self.uc.reg_read(R.UC_ARM64_REG_PC)
        if self.failures:
            raise GameDataError("native 模拟被安全白名单拦下：%s" % "; ".join(self.failures[:4]),
                                "native_blocked")
        if pc != STOP:
            raise GameDataError("native 模拟没有正常返回（pc=0x%x）" % pc, "native_not_finished")

    def serialize(self, body: bytes) -> tuple[bytes, dict]:
        if len(body) != SIZE:
            raise GameDataError("native scene body 长度不对：%d" % len(body), "native_body_size")
        self.phase = "serialize"
        self._reset(body)
        self._run(ENTRY, SERIALIZER_STEPS)
        cache = bytes(self.uc.mem_read(CACHE, 0x2010))
        length = struct.unpack_from("<H", cache, 0x200E)[0]
        if cache[:14] != CACHE_HEADER:
            raise GameDataError("native cache 头不对：%s" % cache[:14].hex(), "native_cache_header")
        stats = {
            "finished": True,
            "instruction_limit": SERIALIZER_STEPS,
            "blocks": self.blocks,
            "serialized_length": length,
            "cache_written_bytes": len(self.written),
            "entity_zero_lookups": self.entity_zero_lookups,
            "entityDictionaryPolicy": "explicit empty baseline; reject nonzero native IDs",
        }
        if not 0 < length <= 0x2000:
            raise GameDataError("native 序列化长度不合法：%d" % length, "native_serialized_length")
        return cache, stats

    def constructor(self) -> tuple[bytes, dict]:
        self.phase = "constructor"
        self._reset(b"\xa5" * SIZE)
        self._run(CTOR_ENTRY, CONSTRUCTOR_STEPS)
        body = bytes(self.uc.mem_read(DEST, SIZE))
        stats = {"finished": True, "instruction_limit": CONSTRUCTOR_STEPS, "blocks": self.blocks,
                 "written_bytes": len(self.written)}
        return body, stats


# ---------------------------------------------------------------- evidence / groups
def evidence_path() -> Path:
    return Path(__file__).with_name(EVIDENCE_NAME)


def load_evidence() -> dict:
    path = evidence_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GameDataError("版本证据读不出来：%s（%s）" % (path, exc), "scene_evidence") from None
    if (doc.get("schema") != "xg-scene-inputs-evidence-v1"
            or doc.get("apk_build") != APK_BUILD
            or not isinstance(doc.get("scenes"), dict)):
        raise GameDataError("版本证据格式不对：%s" % path, "scene_evidence_shape")
    return doc


def _bool(obj, field: str) -> bool:
    if field in {x.member.name for x in obj.duplicate_members}:
        raise ValueError("ambiguous_field:" + field)
    value = obj.fields.get(field)
    if value not in (b"\0", b"\1"):
        raise ValueError("missing_boolean:" + field)
    return value == b"\1"


def _clump_input(scene, obj, group: str) -> dict:
    ref = obj.fields.get("objects")
    if not isinstance(ref, Ref):
        raise ValueError("objects_clump_unknown")
    if ref.index is None:
        if group == "move_rails":
            return {"clump_size": 0, "clump_is_null": True}
        raise ValueError("native_initializer_dereferences_null_clump")
    if not 0 <= ref.index < len(scene.objects):
        raise ValueError("clump_reference_out_of_range")
    clump = scene.objects[ref.index]
    if clump.class_name != "Clump" or clump.duplicate_members:
        raise ValueError("clump_type_or_members_unknown")
    values = clump.fields.get("data")
    if not isinstance(values, tuple) or len(values) > 65535 or not all(isinstance(v, Ref) for v in values):
        raise ValueError("clump_layout_unknown")
    return {"clump_size": len(values), "clump_is_null": False}


class FiniteGroups:
    """T3/T9 的三组 finite initializer 投影；规则逐条对齐 finite_catalog_groups_v32。"""

    def __init__(self, evidence: dict):
        self.evidence = evidence
        self.specs = {name: evidence["group_specs"][name] for name in GROUP_ORDER}
        self.known = set(evidence["known_classes"])
        self.derived = evidence.get("group_derived_classes") or {}

    def ancestry(self, name: str) -> list[str]:
        return [name] + list(self.derived.get(name) or [])

    def project(self, scene_name: str, member_bytes: bytes, expected: dict,
                constructor: bytes) -> dict:
        row = self.evidence["scenes"].get(scene_name)
        if row is None:
            raise GameDataError("版本证据里没有场景 %s" % scene_name, "scene_unknown")
        if sha256_bytes(member_bytes) != row["member_sha256"]:
            raise GameDataError("APK 里的场景 %s 与版本证据不一致" % scene_name, "scene_member_sha")
        if len(constructor) != SIZE:
            raise GameDataError("构造器基线长度不对", "constructor_scene_size")
        for spec in self.specs.values():
            if constructor[spec["count"]] or any(
                    constructor[spec["slots"]:spec["slots"] + spec["stride"] * spec["capacity"]]):
                raise GameDataError("构造器基线里的 finite group 不是零", "constructor_not_pristine")
        scene = Scene.from_bytes(member_bytes, source=row["member"])
        unknown = sorted({o.class_name for o in scene.objects if o.class_name not in self.known})
        groups = {}
        initializer_inputs = []
        derived_classes = set()
        for group, spec in self.specs.items():
            members = []
            errors = []
            source_counts = Counter()
            disabled_networked = 0
            defaults = []
            for obj in scene.objects:
                if spec["class_name"] not in self.ancestry(obj.class_name):
                    continue
                source_counts[obj.class_name] += 1
                if obj.class_name != spec["class_name"]:
                    derived_classes.add(obj.class_name)
                    errors.append({"serialized_index": obj.index,
                                   "reason": "derived_initializer_not_proven:" + obj.class_name})
                    continue
                try:
                    if not _bool(obj, "networked"):
                        continue
                    auto = _bool(obj, "autoStart")
                    allow = None
                    allow_source = "not_used"
                    if group != "rotations":
                        if group == "floaty" and "DANGER_allowDisabledWhenNetworked" not in obj.fields:
                            if "DANGER_allowDisabledWhenNetworked" in {
                                    x.member.name for x in obj.duplicate_members}:
                                raise ValueError("ambiguous_field:DANGER_allowDisabledWhenNetworked")
                            allow, allow_source = False, "native_ctor_0x29fb3dc_clears_0x87"
                        else:
                            allow, allow_source = _bool(obj, "DANGER_allowDisabledWhenNetworked"), "serialized_field"
                    if allow_source.startswith("native_ctor"):
                        defaults.append({"serialized_index": obj.index,
                                         "field": "DANGER_allowDisabledWhenNetworked",
                                         "value": allow, "source": allow_source})
                    if allow is False and not auto:
                        raise ValueError("native_networked_disabled_guard")
                    disabled_networked += not auto
                    entry = {"serialized_index": obj.index, "class_name": obj.class_name,
                             "group": group, "autoStart": auto,
                             "allowDisabledWhenNetworked": allow, "DANGER_source": allow_source,
                             **_clump_input(scene, obj, group)}
                    members.append(entry)
                except (ValueError, IndexError) as exc:
                    errors.append({"serialized_index": obj.index, "reason": str(exc)})
            if len(members) > spec["capacity"]:
                errors.append({"reason": "native_capacity_exceeded", "count": len(members),
                               "capacity": spec["capacity"]})
            if unknown:
                errors.append({"reason": "native_type_ancestry_unknown", "classes": unknown})
            supported = not errors
            groups[group] = {
                "supported": supported,
                "count": len(members) if supported else None,
                "eligible_members_observed": len(members),
                "capacity": spec["capacity"],
                "source_class_counts": dict(source_counts),
                "disabled_networked_allowed": disabled_networked,
                "proven_constructor_defaults_used": defaults,
                "member_serialized_indices_unordered": [o["serialized_index"] for o in members],
                "native_count_offset": spec["count"],
                "native_slots_offset": spec["slots"],
                "native_stride": spec["stride"],
                "initial_values_permutation_invariant": supported,
                "native_initial_slot_hex": "00" * spec["stride"] if supported else None,
                "native_order_recovered": False,
                "missing": errors,
            }
            if supported:
                initializer_inputs.extend(members)
        if {k: g["count"] for k, g in groups.items()} != expected:
            raise GameDataError(
                "场景 %s 的 finite group 计数与版本证据不一致：%s != %s"
                % (scene_name, {k: g["count"] for k, g in groups.items()}, expected),
                "finite_counts_mismatch")
        serialized_only = sorted({o.class_name for o in scene.objects}
                                 & set(self.evidence.get("serialized_only_type_classes") or []))
        result = {
            "schema": "sky-catalog-finite-groups-v32",
            "scene": scene_name,
            "phase": PHASE,
            "native_sha256": self.evidence["native_sha256"],
            "source_member": row["member"],
            "scene_member_sha256": row["member_sha256"],
            "registry_sha256": self.evidence["registry_sha256_of_source"],
            "type_ancestry_sha256": sha256_bytes(canonical_json({
                "known_classes": self.evidence["known_classes"],
                "group_derived_classes": self.derived,
            })),
            "objects_parsed": len(scene.objects),
            "inheritance_derived_candidates": sorted(derived_classes),
            "unresolved_native_type_classes": unknown,
            "groups": groups,
            "finite_groups_supported": all(g["supported"] for g in groups.values()),
            "initializer_inputs": initializer_inputs,
            "native_count_writes": [
                {"offset": g["native_count_offset"], "before_hex": "00",
                 "after_hex": bytes([g["count"]]).hex()}
                for g in groups.values() if g["supported"]],
            "missing": [
                "all non-finite collections and event/onLoad/first-update effects",
                "native TGCL-to-event complete allocation traversal order",
                "loaded-room/entity/account state",
            ],
            "complete_scene": False,
            "loaded_scene_snapshot": False,
            "capture_generated": False,
            "device_used": False,
            "server_changed": False,
        }
        if serialized_only:
            result["serialized_only_type_classes"] = serialized_only
            result["serialized_only_evidence"] = [
                row for row in (self.evidence.get("serialized_only_evidence") or [])
                if row.get("class_name") in serialized_only]
        return result


def fnv32_names(names) -> int:
    h = 0x811C9DC5
    for name in names:
        for b in name.encode("utf-8"):
            h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def fold16(value: int) -> int:
    return (value ^ (value >> 16)) & 0xFFFF


def assemble_body(baseline: bytes, tag: int, finite: dict, evidence: dict) -> bytes:
    if len(baseline) != NATIVE_RECORD_SIZE or sha256_bytes(baseline) != evidence["baseline_sha256"]:
        raise GameDataError("构造器基线与版本证据不一致", "constructor_baseline")
    if type(tag) is not int or not 0 <= tag <= 0xFFFF:
        raise GameDataError("resource tag 不是 u16", "resource_tag")
    if (finite.get("native_sha256") != evidence["native_sha256"]
            or finite.get("phase") != PHASE
            or finite.get("finite_groups_supported") is not True
            or finite.get("complete_scene") is not False
            or finite.get("loaded_scene_snapshot") is not False):
        raise GameDataError("finite group 阶段未证明", "finite_phase")
    if set(finite.get("groups", {})) != set(evidence["group_specs"]):
        raise GameDataError("finite group 集合不一致", "finite_group_set")
    body = bytearray(baseline)
    for name in GROUP_ORDER:
        spec = evidence["group_specs"][name]
        group = finite["groups"][name]
        count = group.get("count")
        if (group.get("supported") is not True or type(count) is not int
                or not 0 <= count <= spec["capacity"]
                or group.get("native_count_offset") != spec["count"]
                or group.get("initial_values_permutation_invariant") is not True):
            raise GameDataError("finite group 未证明：" + name, "finite_group")
        body[spec["count"]] = count
    struct.pack_into("<I", body, 0x10, tag)
    allowed = set(range(0x10, 0x14)) | {s["count"] for s in evidence["group_specs"].values()}
    if any(i not in allowed for i, (a, b) in enumerate(zip(baseline, body)) if a != b):
        raise GameDataError("native body 出现白名单外的改写", "native_mutation")
    return bytes(body)


def _scene_wire():
    from .common import xg_paths
    xg_paths()
    from qfsky.long0167.codec.scene_wire import inspect_scene_wire, reencode_scene_wire
    return inspect_scene_wire, reencode_scene_wire


def check_artifacts(body: bytes, cache: bytes, wire: bytes, report: dict, evidence: dict) -> None:
    if report.get("schema") != SCHEMA:
        raise GameDataError("场景输入证明 schema 不对", "partial_schema")
    for key in ("complete_scene_initialization", "loaded_scene_snapshot", "full_connection_ready",
                "capture_generated", "transport_used", "server_changed", "device_used"):
        if report.get(key) is not False:
            raise GameDataError("场景输入证明的 readiness 字段必须为 false：" + key,
                                "unproved_eligibility")
    for kind, value in (("native", body), ("cache", cache), ("wire", wire)):
        item = report["artifacts"][kind]
        if sha256_bytes(value) != item["sha256"] or len(value) != item["bytes"]:
            raise GameDataError("场景输入 artifact 摘要不对：" + kind, "artifact_digest")
    if len(body) != NATIVE_RECORD_SIZE or len(cache) != CACHE_SIZE or cache[14:14 + len(wire)] != wire:
        raise GameDataError("cache 与 scene 不匹配", "cache_binding")
    if cache[:14] != CACHE_HEADER:
        raise GameDataError("cache 头不是规范值", "cache_header")
    if struct.unpack_from("<H", cache, 0x200E)[0] != len(wire):
        raise GameDataError("cache 长度字段不对", "cache_length")
    inspect_scene_wire, reencode_scene_wire = _scene_wire()
    observation = inspect_scene_wire(wire, verified_profile_sha256=evidence["native_sha256"],
                                     check_zero_padding=True)
    if reencode_scene_wire(observation) != wire:
        raise GameDataError("scene.bin 不是规范编码", "wire_not_canonical")
    if not observation.matches_resource_tag(report["netversion_u16"][0]):
        raise GameDataError("scene.bin 的 resource tag 不对", "wire_resource_tag")
    if dict(observation._counts) != report["wire_variable_group_counts"]:
        raise GameDataError("scene 的 group counts 与证明不一致", "wire_count_metadata")
    for name in GROUP_ORDER:
        spec = evidence["group_specs"][name]
        if dict(observation._counts)[name] != body[spec["count"]]:
            raise GameDataError("scene/native 的 group 计数不一致：" + name, "wire_native_count")
    if not report.get("missing_scene_inputs"):
        raise GameDataError("证明必须列出缺失输入", "missing_uncertainty")


def _pins() -> dict:
    base = Path(__file__).resolve().parent
    names = ("scene_inputs_build.py", "tgcl_scene.py", EVIDENCE_NAME)
    return {"xg_gamedata/" + name: sha256_bytes((base / name).read_bytes()) for name in names}


def _plan_scene(zf, scene: str, evidence: dict, finite: FiniteGroups, emulator: SceneEmulator,
                baseline: bytes, pins: dict) -> dict:
    """生成一张图的 4 个文件内容（不写盘）。"""
    row = evidence["scenes"][scene]
    member = row["member"]
    try:
        raw = zf.read(member)
    except KeyError:
        raise GameDataError("APK 里没有 %s 的 Objects.level.bin" % scene, "scene_member_missing") from None
    if sha256_bytes(raw) != row["member_sha256"]:
        raise GameDataError("APK 里的 %s 场景成员与版本证据不一致" % scene, "scene_member_sha")
    parsed = Scene.from_bytes(raw, source=member)
    aliases = {}
    types = []
    for obj in parsed.objects:
        name = obj.class_name
        if name in evidence["type_aliases"]:
            aliases[name] = aliases.get(name, 0) + 1
            name = evidence["type_aliases"][name]
        types.append(name)
    netversion = [fold16(fnv32_names(types)), int(evidence["rpc_hash16"]),
                  23536, 0, 0, NATIVE_RECORD_SIZE]
    if netversion != row["netversion_u16"]:
        raise GameDataError(
            "场景 %s 的 netversion 与版本证据不一致：%s != %s"
            % (scene, netversion, row["netversion_u16"]), "netversion_mismatch")
    map_id = fnv32(scene)
    if map_id != row["map_id"]:
        raise GameDataError("场景 %s 的 map_id 不对" % scene, "map_id_mismatch")
    provenance = {
        "kind": "apk_static_derived_v32",
        "apk_archive_directory_sha256": evidence["apk_archive_directory_sha256"],
        "native_sha256": evidence["native_sha256"],
        "apk_build": APK_BUILD,
        "scene_member": member,
        "scene_member_sha256": row["member_sha256"],
        "resource_revision": evidence["provenance_resource_revision_note"],
        "object_count": len(parsed.objects),
        "type_alias_applications": aliases,
        "level_hash_source": evidence["level_hash_source"],
        "rpc_hash_source": evidence["rpc_hash_source"],
        "native_record_comparison": bool(row["native_record_comparison"]),
        "runtime_capture": False,
        "account_specific_data": False,
    }
    finite_report = finite.project(scene, raw, row["finite_group_counts"], baseline)
    body = assemble_body(baseline, netversion[0], finite_report, evidence)
    cache, serializer = emulator.serialize(body)
    wire = cache[14:14 + serializer["serialized_length"]]
    inspect_scene_wire, reencode_scene_wire = _scene_wire()
    observation = inspect_scene_wire(wire, verified_profile_sha256=evidence["native_sha256"],
                                     check_zero_padding=True)
    report = {
        "schema": SCHEMA,
        "scene": scene,
        "map_id": map_id,
        "label": row["label"],
        "resource_revision": evidence["resource_revision"],
        "native_sha256": evidence["native_sha256"],
        "netversion_u16": netversion,
        "provenance": provenance,
        "source_pins": pins,
        "phase": PHASE,
        "baseline_sha256": evidence["baseline_sha256"],
        "baseline_semantics": evidence["baseline_semantics"],
        "applied_groups": list(GROUP_ORDER),
        "finite_groups": finite_report,
        "wire_variable_group_counts": dict(observation._counts),
        "canonical_roundtrip": reencode_scene_wire(observation) == wire,
        "missing_scene_inputs": MISSING,
        "complete_scene_initialization": False,
        "loaded_scene_snapshot": False,
        "full_connection_ready": False,
        "capture_generated": False,
        "transport_used": False,
        "server_changed": False,
        "device_used": False,
        "serializer": serializer,
        "build_seconds": 0.0,
        "artifacts": {
            "native": {"file": scene + ".native.bin", "sha256": sha256_bytes(body), "bytes": len(body)},
            "cache": {"file": scene + ".cache.bin", "sha256": sha256_bytes(cache), "bytes": len(cache)},
            "wire": {"file": scene + ".scene.bin", "sha256": sha256_bytes(wire), "bytes": len(wire)},
        },
    }
    check_artifacts(body, cache, wire, report, evidence)
    return {"report": report, "body": body, "cache": cache, "wire": wire}


def _reuse(paths: dict, scene: str, evidence: dict, pins: dict) -> bool:
    """续跑：只有证明/4 个产物都能自洽复验才复用。"""
    try:
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    row = evidence["scenes"].get(scene)
    if (row is None or report.get("schema") != SCHEMA or report.get("scene") != scene
            or report.get("map_id") != row["map_id"] or report.get("native_sha256") != evidence["native_sha256"]
            or report.get("source_pins") != pins
            or (report.get("provenance") or {}).get("scene_member_sha256") != row["member_sha256"]
            or report.get("netversion_u16") != row["netversion_u16"]):
        return False
    for kind, key in (("native", "native"), ("cache", "cache"), ("wire", "wire")):
        try:
            raw = paths[key].read_bytes()
        except OSError:
            return False
        item = (report.get("artifacts") or {}).get(kind) or {}
        if sha256_bytes(raw) != item.get("sha256") or len(raw) != item.get("bytes"):
            return False
    wire = paths["wire"].read_bytes()
    cache = paths["cache"].read_bytes()
    if len(cache) != CACHE_SIZE or cache[14:14 + len(wire)] != wire:
        return False
    if cache[:14] != CACHE_HEADER:
        return False
    if struct.unpack_from("<H", cache, 0x200E)[0] != len(wire):
        return False
    try:
        inspect_scene_wire, reencode_scene_wire = _scene_wire()
        observation = inspect_scene_wire(wire, verified_profile_sha256=evidence["native_sha256"],
                                         check_zero_padding=True)
        if reencode_scene_wire(observation) != wire:
            return False
        if dict(observation._counts) != report.get("wire_variable_group_counts"):
            return False
    except Exception:  # noqa: BLE001 - 复验失败就重建
        return False
    return True


def peak_working_set_mb() -> float | None:
    """Windows 进程峰值工作集（MB）；非 Windows 或调用失败返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi")
        fn = psapi.GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not fn(handle, ctypes.byref(counters), counters.cb):
            return None
        return round(counters.PeakWorkingSetSize / 1048576.0, 1)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- public API
def ensure_scene_inputs(apk: Path, cache_dir: Path, progress: Progress | None = None,
                        force: bool = False) -> dict:
    """从 APK 生成/补齐 catalog-inputs；返回 index。可反复调用（续跑）。"""
    started = time.monotonic()
    apk = Path(apk)
    cache_dir = Path(cache_dir)
    _require_native_deps()
    evidence = load_evidence()
    from .spawn_loader import load as load_pack
    scenes = sorted(load_pack()["placements"])
    missing = [s for s in scenes if s not in evidence["scenes"]]
    if missing:
        raise GameDataError("落点包里的场景没有版本证据：%s" % missing[:6], "scene_evidence_missing")
    own = Progress("scene-inputs", len(scenes)) if progress is None else progress
    own.total = len(scenes)

    import zipfile
    try:
        zf = zipfile.ZipFile(apk)
    except (OSError, zipfile.BadZipFile):
        raise GameDataError("APK 打不开：%s" % apk, "apk_bad_zip") from None
    try:
        directory = hashlib.sha256(json.dumps(
            sorted((i.filename, i.file_size, i.CRC) for i in zf.infolist()),
            separators=(",", ":")).encode()).hexdigest()
        if directory != evidence["apk_archive_directory_sha256"]:
            raise GameDataError("APK 目录身份与版本证据不一致（不是 0.16.8/421629）",
                                "apk_identity_mismatch")
        own.emit("校验 APK 与 native…")
        native_raw = zf.read(NATIVE_MEMBER)
        native = NativeImage(native_raw, evidence["native_sha256"])
        own.emit("capstone 静态白名单校验…")
        static = static_whitelist(native, ENTRY, ALLOWED)
        ctor_static = static_whitelist(native, CTOR_ENTRY, CTOR_ALLOWED)
        emulator = SceneEmulator(native)
        finite = FiniteGroups(evidence)
        pins = _pins()

        cache_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = cache_dir / ".baseline.bin"
        if not force and baseline_path.is_file() and sha256_bytes(
                baseline_path.read_bytes()) == evidence["baseline_sha256"]:
            baseline = baseline_path.read_bytes()
            ctor_stats = {"reused": True}
        else:
            own.emit("重建 native 构造器基线…")
            baseline, ctor_stats = emulator.constructor()
            if sha256_bytes(baseline) != evidence["baseline_sha256"]:
                raise GameDataError("构造器基线 sha 与版本证据不一致", "constructor_baseline")
            atomic_write(baseline_path, baseline)

        rows = []
        generated = reused = 0
        for idx, scene in enumerate(scenes, 1):
            paths = {
                "json": cache_dir / (scene + ".json"),
                "native": cache_dir / (scene + ".native.bin"),
                "cache": cache_dir / (scene + ".cache.bin"),
                "wire": cache_dir / (scene + ".scene.bin"),
            }
            t0 = time.monotonic()
            if not force and all(p.is_file() for p in paths.values()) and _reuse(paths, scene, evidence, pins):
                reused += 1
                status = "reused"
                report = json.loads(paths["json"].read_text(encoding="utf-8"))
                seconds = round(time.monotonic() - t0, 3)
            else:
                item = _plan_scene(zf, scene, evidence, finite, emulator, baseline, pins)
                report = item["report"]
                seconds = round(time.monotonic() - t0, 3)
                report["build_seconds"] = seconds
                atomic_write(paths["native"], item["body"])
                atomic_write(paths["wire"], item["wire"])
                atomic_write(paths["cache"], item["cache"])
                atomic_write(paths["json"], json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
                generated += 1
                status = "generated"
            rows.append({
                "scene": scene,
                "map_id": report["map_id"],
                "label": report["label"],
                "status": status,
                "seconds": seconds,
                "wire_bytes": report["artifacts"]["wire"]["bytes"],
                "cache_bytes": report["artifacts"]["cache"]["bytes"],
            })
            own.emit("%s %s" % (scene, "复用" if status == "reused" else "生成"),
                     done=idx, total=len(scenes), force=True)

        elapsed = round(time.monotonic() - started, 1)
        index = {
            "schema": INDEX_SCHEMA,
            "apk": str(apk),
            "apk_build": APK_BUILD,
            "native_sha256": evidence["native_sha256"],
            "resource_revision": evidence["resource_revision"],
            "source_pins": pins,
            "static_whitelist": {
                "serializer": {k: static[k] for k in
                               ("instructions_verified", "function_spans", "indirect_control_flow",
                                "outside_span_targets_not_executed")},
                "constructor": {k: ctor_static[k] for k in
                                ("instructions_verified", "function_spans", "indirect_control_flow",
                                 "outside_span_targets_not_executed")},
            },
            "constructor": ctor_stats,
            "scene_count": len(rows),
            "generated": generated,
            "reused": reused,
            "scenes": rows,
            "total_seconds": elapsed,
            "peak_memory_mb": peak_working_set_mb(),
            "cache_dir": str(cache_dir),
        }
        atomic_write(cache_dir / "index.json",
                     json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
        own.emit("场景输入完成：%d 生成 / %d 复用，%.1fs" % (generated, reused, elapsed),
                 done=len(scenes), force=True)
        return index
    finally:
        zf.close()
