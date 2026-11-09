"""小光的动作、站姿名称解析（Python 3.12 标准库）。

只匹配完整名称、别名或本模块列出的包装格式，不把任意聊天子串当指令。
目录位于本文件旁，读取路径不依赖调用者的当前工作目录。

catalog.json 的 seconds 是每级总时长；resolve_emote 的 seconds 是与 anims
逐段对应的估计时长。多段按等时分配，最后一段吸收浮点余数，总时长保持不变。
这些值只是播放调度估计，不是从游戏资源测得的动画长度。
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import xg_paths
import re
import unicodedata
from typing import Any

_CATALOG_PATH = xg_paths.name_table("catalog.json")
_NUMBER = r"[+-]?\d+|[零〇一二两三四五六七八九十百千万]+"
_PREFIX_LEVEL = re.compile(rf"^(?:第)?(?P<number>{_NUMBER})\s*级\s*[·・.：:]?\s*(?P<name>.+)$")
_SUFFIX_LEVEL = re.compile(rf"^(?P<name>.+?)\s*[·・.：:]?\s*(?P<number>{_NUMBER})\s*(?:级)?$")
_TAG = re.compile(r"^\[\s*(?P<kind>动作|站姿|姿势)\s*:\s*(?P<value>.*?)\s*\]$")
_STANCE_PREFIXES = (
    "请帮我切换成", "请帮我切换为", "请帮我切换到", "请帮我换成", "请帮我换为",
    "帮我切换成", "帮我切换为", "帮我切换到", "帮我换成", "帮我换为",
    "请切换成", "请切换为", "请切换到", "请换成", "请换为", "请使用",
    "切换成", "切换为", "切换到", "换成", "换为", "使用",
)
_STANCE_SUFFIXES = ("的站姿", "的姿势", "站姿", "姿势")
_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _input_text(text: Any, kind: str) -> str | None:
    if not isinstance(text, str):
        return None
    value = _normalise(text)
    if not value:
        return None
    tag = _TAG.fullmatch(value)
    if tag:
        accepted = {"动作"} if kind == "emotes" else {"站姿", "姿势"}
        if tag["kind"] not in accepted:
            return None
        value = tag["value"].strip()
    return value or None


@lru_cache(maxsize=1)
def _load_catalog() -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    try:
        with _CATALOG_PATH.open("r", encoding="utf-8-sig") as handle:
            catalog = json.load(handle)
    except (OSError, ValueError):
        catalog = {"emotes": {}, "stances": {}}
    indexes: dict[str, dict[str, str]] = {"emotes": {}, "stances": {}}
    used: dict[str, tuple[str, str]] = {}
    for kind, index in indexes.items():
        for name, entry in catalog[kind].items():
            for spelling in (name, *entry.get("aliases", [])):
                key = _normalise(spelling)
                if not key:
                    raise ValueError(f"目录存在空名称：{kind}/{name}")
                owner = (kind, name)
                if key in used and used[key] != owner:
                    raise ValueError(f"目录名称或别名冲突：{spelling}")
                used[key] = owner
                index[key] = name
    return catalog, indexes


def _integer(token: str) -> int | None:
    """解析阿拉伯数字及常见中文整数；过长数字饱和为大数用于最高级夹取。"""
    if re.fullmatch(r"[+-]?\d+", token):
        negative = token.startswith("-")
        digits = token.lstrip("+-").lstrip("0") or "0"
        if len(digits) > 12:
            return -10**12 if negative else 10**12
        value = int(digits)
        return -value if negative else value
    if all(char in _DIGITS for char in token):
        result = 0
        for char in token:
            result = result * 10 + _DIGITS[char]
            if result > 10**12:
                return 10**12
        return result
    # 递减位权形式，如十三、一百零二、一万二千；拒绝十十、百十百等错序。
    total = section = digit = 0
    previous_unit = 10000
    seen_wan = False
    have_digit = False
    for char in token:
        if char in _DIGITS:
            number = _DIGITS[char]
            if have_digit and digit != 0:
                return None
            digit = number
            have_digit = True
            continue
        unit = _UNITS.get(char)
        if unit is None:
            return None
        if unit == 10000:
            if seen_wan:
                return None
            section += digit
            total += (section or 1) * unit
            section = digit = 0
            previous_unit = 10000
            seen_wan = True
            have_digit = False
        else:
            if unit >= previous_unit:
                return None
            section += (digit if have_digit else 1) * unit
            digit = 0
            previous_unit = unit
            have_digit = False
    return total + section + digit


def _level_for(entry: dict[str, Any], requested: int | None) -> str:
    if requested is None:
        return str(entry["default"])
    levels = sorted((int(level), level) for level in entry["levels"])
    # 越界夹到最小/最大有效级；如有缺档，使用不高于请求的最近有效级。
    selected = levels[0][1]
    for numeric, key in levels:
        if numeric > requested:
            break
        selected = key
    return str(selected)


def _segment_seconds(total: int | float, count: int) -> list[int | float]:
    if count == 1:
        return [total]
    share = total / count
    return [share] * (count - 1) + [total - share * (count - 1)]


def resolve_emote(text: str) -> dict[str, Any] | None:
    """解析名称/别名及前后缀等级；未知输入返回 None。

    支持：鞠躬、弯腰、鞠躬3、鞠躬3级、鞠躬·2级、三级鞠躬、
    [动作:鞠躬]。省略等级使用目录默认级；越界夹取到有效级。
    """
    value = _input_text(text, "emotes")
    if value is None:
        return None
    catalog, indexes = _load_catalog()
    index = indexes["emotes"]
    name = index.get(value)
    requested: int | None = None
    if name is None:
        for pattern in (_PREFIX_LEVEL, _SUFFIX_LEVEL):
            match = pattern.fullmatch(value)
            if not match:
                continue
            candidate = index.get(match["name"].strip())
            number = _integer(match["number"])
            if candidate is not None and number is not None:
                name, requested = candidate, number
                break
    if name is None:
        return None
    entry = catalog["emotes"][name]
    level = _level_for(entry, requested)
    anims = list(entry["levels"][level])
    return {
        "name": name,
        "level": level,
        "anims": anims,
        "seconds": _segment_seconds(entry["seconds"][level], len(anims)),
    }


def resolve_stance(text: str) -> dict[str, Any] | None:
    """解析完整站姿名/别名，允许“换成…”和“…的姿势”等限定包装。"""
    value = _input_text(text, "stances")
    if value is None:
        return None
    catalog, indexes = _load_catalog()
    index = indexes["stances"]
    name = index.get(value)
    if name is None:
        candidates = [value]
        for prefix in _STANCE_PREFIXES:
            if value.startswith(prefix):
                candidates.append(value[len(prefix):].strip())
                break
        for candidate in candidates:
            name = index.get(candidate)
            if name is not None:
                break
            for suffix in _STANCE_SUFFIXES:
                if candidate.endswith(suffix):
                    stem = candidate[:-len(suffix)].strip()
                    name = index.get(stem)
                    if name is None and suffix.startswith("的"):
                        # “害羞”可能是动作名；仅借用已有“害羞姿势”等站姿别名，
                        # 接受“害羞的姿势”，不把裸“害羞”新增为站姿别名。
                        name = index.get(stem + suffix[1:])
                    if name is not None:
                        break
            if name is not None:
                break
    if name is None:
        return None
    return {"name": name, "id": catalog["stances"][name]["id"]}


def prompt_lists() -> tuple[str, str]:
    """返回仅含主名的动作/站姿提示词列表（英文逗号分隔）。"""
    catalog, _ = _load_catalog()
    return ",".join(catalog["emotes"]), ",".join(catalog["stances"])
