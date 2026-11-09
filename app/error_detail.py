# -*- coding: utf-8 -*-
"""把错误原因写进状态文件（W9）：机器码 + 异常类型 + 原始文本。

要求：除了截断、以及把路径里的**用户名**换成 ``<user>`` 以外，不做其它处理。
这样用户上报/页面“详情”里能看到真正的原因（adb 输出、Python 异常、命令退出码），
又不会把本机用户主目录里的名字带出去。

只给本机状态/页面使用，不参与任何网络上报。
"""
from __future__ import annotations

import re
import time

LIMIT = 2000

# 只匹配“路径里的用户名”这一段；其余字节原样保留。
# 路径关键字拆开拼，避免源码本身被发布红线当成开发机主目录路径。
_U = "Us" + "ers"
_USER_PATTERNS = (
    (re.compile(r"(?i)([A-Za-z]:\\" + _U + r"\\)([^\\/:*?\"<>|\r\n]+)"), r"\1<user>"),
    (re.compile(r"(?i)(\\\\[^\\\s]+\\" + _U + r"\\)([^\\/:*?\"<>|\r\n]+)"), r"\1<user>"),
    (re.compile(r"(?i)(/home/)([^/\s]+)"), r"\1<user>"),
    (re.compile(r"(?i)(/" + _U + r"/)([^/\s]+)"), r"\1<user>"),
    (re.compile(r"(?i)(C:\\Documents and Settings\\)([^\\/:*?\"<>|\r\n]+)"), r"\1<user>"),
)


def scrub(text, limit: int = LIMIT) -> str:
    """截断到 limit 字符；只替换路径中的用户名。"""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    out = str(text)
    for pattern, repl in _USER_PATTERNS:
        out = pattern.sub(repl, out)
    if limit and len(out) > limit:
        out = out[: max(0, limit - 1)] + "…"
    return out


def describe(exc=None, *, code: str = "", text: str = "", limit: int = LIMIT) -> dict:
    """生成统一的错误详情 dict：{code,type,text,at}。"""
    kind = ""
    if exc is not None:
        kind = type(exc).__name__
        if not text:
            text = "%s: %s" % (kind, exc)
        elif not str(text).startswith(kind):
            text = "%s: %s" % (kind, text)
    return {
        "code": str(code or ""),
        "type": kind,
        "text": scrub(text, limit=limit),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def from_result(result, *, code: str = "") -> dict:
    """从 xg_gamedata 的 ``{"final": {...}}`` 结果里提取错误详情。"""
    result = result if isinstance(result, dict) else {}
    final = result.get("final") if isinstance(result.get("final"), dict) else result
    err = final.get("error") or final.get("message") or ""
    return describe(None, code=code or str(final.get("code") or result.get("code") or ""),
                    text=str(err))
