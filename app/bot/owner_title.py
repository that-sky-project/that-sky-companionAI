"""对哥哥的称呼（2026-11-06）：后台可改成姐姐、宝宝等。存 control/owner_title，默认“哥哥”。

校验：1～6 个汉字/字母/数字；不能含小光等保留字；单独和放进常见句子里都要通过网易聊天审核规则
（bot/textcheck.py，780 条设备缓存规则的离线引擎；命中 shield/intercept 即不通过）。
"""
import re
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
    except Exception:
        return False, "审核规则暂时加载不了，先别改"
    for c in CONTEXTS:
        s = c.format(t=t)
        try:
            hits = textcheck.blocked(s)
        except Exception:
            # Windows 本机没有 pcre2 审核引擎/规则文件：只做字数、保留字预检，
            # 真正的游戏内审核交给游戏服务器；不因为本机缺引擎就把所有称呼都判死。
            return True, "可以用（本机没有审核规则，没做游戏内审核预检）"
        if hits:
            how = "会被屏蔽（别人看不到）" if hits[0][0] == "shield" else "会被拦截（发不出去）"
            where = "" if s == t else f"（放进“{s}”这句话时）"
            return False, f"“{t}”在游戏里{how}{where}，换一个吧"
    return True, "可以用"


def set_title(title):
    ok, why = check(title)
    if not ok:
        return False, why
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(title.strip(), encoding="utf-8")
    tmp.replace(FILE)
    return True, "已改为“" + title.strip() + "”"
