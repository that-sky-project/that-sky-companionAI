"""Optional local SDK text-rule precheck; public packages contain no rule data.

Rules may be supplied in the user data directory. Missing rules are explicitly
unavailable, not reported as a successful match/verification. The remote service
retains its normal text handling. No package data fallback is used.
"""
import ctypes as C
import importlib.util
import json
import sys
import threading
from pathlib import Path

import xg_paths

def _rules_dir():
    """Only generated/user-local rules, never APK/cache data from app/ref."""
    current = Path(xg_paths.GAME_DIR) / "text-rules"
    for path in (current, Path(xg_paths.DATA) / "ref/textrules"):
        if (path / "cached_patterns.jsonl").is_file():
            return path
    return current


class RulesUnavailable(FileNotFoundError):
    """The optional user-local rule data has not been generated/provided."""


RULES = _rules_dir()
CHAT_GROUPS = ("shield", "intercept", "replace", "remind")
EXPECTED_RULE_COUNTS = {"shield": 689, "intercept": 88, "replace": 2, "remind": 1}
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
        global RULES
        RULES = _rules_dir()
        if not all((RULES / name).is_file() for name in
                   ("cached_patterns.jsonl", "sdk_subject.py", "t2s.json")):
            raise RulesUnavailable("local_text_rules_not_generated")
        # PCRE2 match data/context are mutable; every thread shares this engine.
        self._check_lock = threading.Lock()
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
        if not self.md or not self.ctx:
            raise MemoryError("pcre2_match_buffer_allocation_failed")
        for name, val in (("pcre2_set_match_limit_8", 500000), ("pcre2_set_depth_limit_8", 1000)):
            f = getattr(lib, name)
            f.argtypes = [pv, C.c_uint32]
            f(self.ctx, val)
        self.subject = _subject_mod().make_subject
        self.rules = {g: [] for g in CHAT_GROUPS}
        self.failed = []
        for line in (RULES / "cached_patterns.jsonl").read_text(encoding="utf-8").splitlines():
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

    def require_ready(self):
        """Require the selected user-local rule set to compile completely."""
        if self.failed or any(len(self.rules.get(group, ())) != count
                              for group, count in EXPECTED_RULE_COUNTS.items()):
            raise RuntimeError("text_rules_incomplete")
        if not self.md or not self.ctx:
            raise RuntimeError("text_rules_matcher_unavailable")

    def check(self, text, groups=CHAT_GROUPS, level="1", channel="chat_max"):
        """[(分组, 规则编号)]；引擎出错（超限等）的规则记为 (分组, 编号+'?')，当作可疑。"""
        with self._check_lock:
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
