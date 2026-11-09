"""光遇国服聊天审核规则（0.16.7 设备缓存，780 条）离线检查：小光发言前先过一遍，命中就换种说法。

规则来源：ref/textrules/cached_patterns.jsonl（GPT 从 ma75_147897_regex.txt 解密还原；engine_pattern 在 PCRE2 10.40 下
复编译与原字节码逐字节一致）。这里用系统 libpcre2-8（10.46）按 compileOptions 编译 engine_pattern 匹配——语法相同，
只是不用原序列化字节码。文本先按 SDK 规范化（ref/textrules/sdk_subject.py：NFKC、繁转简、全转半、汉字码位 +1、
level/channel 前缀）。分组：shield 屏蔽（别人看不到）/ intercept 拦截 / replace 替换 / remind 提醒。
  textcheck.py "要检查的话"     # 命令行自测
"""
import ctypes as C
import importlib.util
import json
import sys
import threading
from pathlib import Path

import xg_paths

def _rules_dir():
    """规则目录：优先用户数据目录，其次安装目录自带的 app/ref/textrules。"""
    for p in (Path(xg_paths.DATA) / "ref/textrules", Path(getattr(xg_paths, "APP", "")) / "ref/textrules"):
        try:
            if (p / "cached_patterns.jsonl").is_file():
                return p
        except OSError:
            continue
    return Path(xg_paths.DATA) / "ref/textrules"


RULES = _rules_dir()
CHAT_GROUPS = ("shield", "intercept", "replace", "remind")
BLOCKING = ("shield", "intercept")          # 这两类会让别人看不到 / 发不出去
_lock = threading.Lock()
_engine = None


def _subject_mod():
    spec = importlib.util.spec_from_file_location("sdk_subject", RULES / "sdk_subject.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Engine:
    def __init__(self):
        lib = None
        _cands = ["libpcre2-8.so.0"]
        _app = Path(getattr(xg_paths, "APP", "") or ".")
        _cands += [str(_app / "bin" / "libpcre2-8-0.dll"), str(_app / "bin" / "pcre2-8.dll"),
                   "libpcre2-8-0.dll", "pcre2-8.dll", "libpcre2-8.dll"]
        _err = None
        for _c in _cands:
            try:
                lib = C.CDLL(_c)
                break
            except OSError as exc:
                _err = exc
        if lib is None:
            raise OSError("pcre2_not_found: %s" % _err)
        pv, size = C.c_void_p, C.c_size_t
        self.compile = lib.pcre2_compile_8
        self.compile.restype = pv
        self.compile.argtypes = [C.c_char_p, size, C.c_uint32, C.POINTER(C.c_int), C.POINTER(size), pv]
        self.match = lib.pcre2_match_8
        self.match.restype = C.c_int
        self.match.argtypes = [pv, C.c_char_p, size, size, C.c_uint32, pv, pv]
        md_create = lib.pcre2_match_data_create_8
        md_create.restype, md_create.argtypes = pv, [C.c_uint32, pv]
        ctx_create = lib.pcre2_match_context_create_8
        ctx_create.restype, ctx_create.argtypes = pv, [pv]
        self.md = md_create(256, None)
        self.ctx = ctx_create(None)
        for name, val in (("pcre2_set_match_limit_8", 500000), ("pcre2_set_depth_limit_8", 1000)):
            f = getattr(lib, name)
            f.argtypes = [pv, C.c_uint32]
            f(self.ctx, val)
        self.subject = _subject_mod().make_subject
        self.rules = {g: [] for g in CHAT_GROUPS}
        self.failed = []
        for line in (RULES / "cached_patterns.jsonl").open(encoding="utf-8"):
            r = json.loads(line)
            if r["group"] not in self.rules:
                continue
            pat = (r.get("engine_pattern") or r["raw_pattern"]).encode("utf-8")
            err, off = C.c_int(), C.c_size_t()
            code = self.compile(pat, len(pat), int(r["compileOptions"], 16), C.byref(err), C.byref(off), None)
            if not code:
                self.failed.append((r["group"], r["id"], err.value))
                continue
            self.rules[r["group"]].append((str(r["id"]), code))

    def check(self, text, groups=CHAT_GROUPS, level="1", channel="chat_max"):
        """[(分组, 规则编号)]；引擎出错（超限等）的规则记为 (分组, 编号+'?')，当作可疑。"""
        hits = []
        for g in groups:
            subj = self.subject(text, g, level=level, channel=channel).encode("utf-8")
            for rid, code in self.rules[g]:
                r = self.match(code, subj, len(subj), 0, 0, self.md, self.ctx)
                if r >= 0:
                    hits.append((g, rid))
                elif r != -1:
                    hits.append((g, rid + "?"))
        return hits


def engine():
    global _engine
    with _lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def blocked(text):
    """会被屏蔽/拦截的命中（空列表 = 本地规则放行）。"""
    return [h for h in engine().check(text, BLOCKING)]


if __name__ == "__main__":
    e = engine()
    print("规则", {g: len(v) for g, v in e.rules.items()}, "编译失败", e.failed)
    for t in sys.argv[1:]:
        print(t, "→", e.check(t) or "未命中")
