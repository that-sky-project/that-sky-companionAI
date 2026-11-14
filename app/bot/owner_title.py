"""主人称呼：基本格式校验始终启用，用户本地词表可用时追加词表预检。

公共安装包不带游戏词表。词表缺失允许保存，但必须明确说明未做词表审核；
已有词表损坏、编译失败或匹配异常仍拒绝设置，避免把错误当作放行。
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
RULES_UNAVAILABLE_MESSAGE = "仅完成基本格式校验；本地词表未生成，未进行本地词表审核，游戏内仍可能屏蔽或拦截。"
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
    import textcheck
    try:
        checker = textcheck.engine()
        checker.require_ready()
    except textcheck.RulesUnavailable:
        return True, RULES_UNAVAILABLE_MESSAGE
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
    message = "已改为“" + title.strip() + "”"
    if why == RULES_UNAVAILABLE_MESSAGE:
        message += "。" + why
    return True, message
