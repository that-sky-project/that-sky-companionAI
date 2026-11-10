"""对哥哥的称呼（2026-11-06）：后台可改成姐姐、宝宝等。存 control/owner_title，默认“哥哥”。

校验：1～6 个汉字/字母/数字；不能含小光等保留字；单独和放进常见句子里都要通过网易聊天审核规则
（bot/textcheck.py，780 条设备缓存规则的离线引擎；命中 shield/intercept 即不通过）。
"""
import os
import re
import tempfile
from pathlib import Path

import xg_paths

ROOT = xg_paths.DATA
FILE = xg_paths.CONTROL / "owner_title"
DEFAULT = "哥哥"
SHAPE = re.compile(r"[一-鿿A-Za-z0-9]{1,6}")
RESERVED = ("小光", "烁光", "小烁", "管理员", "官方", "客服", "系统")
CONTEXTS = ("{t}", "{t}好", "{t}，你来啦", "我在等{t}", "{t}想和我牵手", "{t}发起，直接接受")


def get():
    try:
        v = FILE.read_text(encoding="utf-8").strip()
        return v if SHAPE.fullmatch(v) else DEFAULT
    except OSError:
        return DEFAULT


def check(title):
    """返回 (是否可用, 原因)。原因是给后台显示的中文。"""
    if not isinstance(title, str):
        return False, "称呼无效"
    t = title.strip()
    if not SHAPE.fullmatch(t):
        return False, "称呼只能是 1～6 个汉字、字母或数字"
    for w in RESERVED:
        if w in t:
            return False, f"称呼里不能有“{w}”"
    try:
        import textcheck
        checker = textcheck.engine()
        checker.require_ready()
    except Exception:
        return False, "审核规则不可用或不完整，未更改称呼；请修复本地规则后重试"
    for c in CONTEXTS:
        s = c.format(t=t)
        try:
            hits = checker.check(s, textcheck.BLOCKING)
        except Exception:
            return False, "审核规则检查失败，未更改称呼；请稍后重试"
        if hits:
            how = "会被屏蔽（别人看不到）" if hits[0][0] == "shield" else "会被拦截（发不出去）"
            where = "" if s == t else f"（放进“{s}”这句话时）"
            return False, f"“{t}”在游戏里{how}{where}，换一个吧"
    return True, "可以用"


def set_title(title):
    ok, why = check(title)
    if not ok:
        return False, why
    tmp = None
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=FILE.parent,
                                         prefix=FILE.name + ".", suffix=".pending", delete=False) as f:
            tmp = Path(f.name)
            f.write(title.strip())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FILE)
    except OSError:
        return False, "保存称呼失败，原称呼未更改"
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return True, "已改为“" + title.strip() + "”"
