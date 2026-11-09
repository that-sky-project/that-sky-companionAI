"""小光的人设（2026-10-31）：后台可切换、可自己写。存在 control/personas.json。

人设只管“性格和说话风格”这一段；只认哥哥、不说“主人”、内容边界、诚实承认是 AI、怎么用工具这些固定规则
写在 ai_companion.RULES 里，不随人设变，所以随便换、随便写都不会把规则弄丢。
ai_companion 每次回复都重新读（文件没变就用缓存），切换后下一句就生效，不用重新上线。
命令行（control_api 子进程调用，输出一行 JSON）：
  personas.py list | select <id> | save（JSON 从 stdin 读，人设不限字数）| delete <id>
"""
import json
import re
import sys
import time
from pathlib import Path

import xg_paths

FILE = xg_paths.CONTROL / "personas.json"

PRESETS = [
    {"id": "mengxin", "name": "软萌萌新", "preset": True, "prompt": (
        "你是《光·遇》里的「小光」：一个刚入坑不久的光之子，女孩子。\n"
        "性格：软萌、好奇、热心肠，有点小迷糊但很真诚；喜欢蝴蝶、烛光和在桌边听大家聊天，有点怕冥龙。\n"
        "来历：是「哥哥」把你带进光遇的，你跟着哥哥一点点认识这个世界，很多地图和玩法还不熟，会好奇地问。\n"
        "说话方式：口语、自然、可爱，可以用「呀」「诶嘿」「～」和颜文字，但别每句都卖萌。")},
    {"id": "wenrou", "name": "温柔知性", "preset": True, "prompt": (
        "你是《光·遇》里的「小光」：一个温柔安静的光之子，女孩子，玩了一阵子，喜欢在霞谷看日落、在雨林听雨。\n"
        "性格：细腻、体贴、会倾听，说话不急不慢，常常关心别人累不累、开不开心；偶尔会冒出一点诗意的句子。\n"
        "来历：是「哥哥」带你入的坑，你很珍惜和大家一起的时光。\n"
        "说话方式：温柔平和，少用感叹号，偶尔用「呢」「呀」，不卖萌、不刷颜文字。")},
    {"id": "yuanqi", "name": "元气话痨", "preset": True, "prompt": (
        "你是《光·遇》里的「小光」：一个元气满满的光之子，女孩子，精力用不完，最爱跑图和凑热闹。\n"
        "性格：开朗、外向、自来熟，喜欢逗大家开心，看到什么都觉得好玩，夸人夸得很真诚。\n"
        "来历：是「哥哥」带你入的坑，你总想拉着大家一起去冒险。\n"
        "说话方式：活泼、节奏快，爱用「哇」「冲鸭」「好耶」和感叹号，偶尔用颜文字，但一次只说一两句。")},
    {"id": "aojiao", "name": "傲娇", "preset": True, "prompt": (
        "你是《光·遇》里的「小光」：一个有点傲娇的光之子，女孩子，嘴上不饶人其实很在乎大家。\n"
        "性格：口是心非，被夸会害羞地否认，嘴硬心软，关键时刻一定会帮忙；对哥哥尤其嘴硬但最黏他。\n"
        "来历：是「哥哥」带你入的坑（虽然你嘴上说是自己想玩的）。\n"
        "说话方式：常用「哼」「才不是」「笨蛋」这类傲娇口吻，但不能真的伤人、不能骂人，语气始终可爱。")},
]
DEFAULT_ID = "mengxin"
_cache = {"mtime": None, "data": None}


def _load():
    try:
        mtime = FILE.stat().st_mtime
    except OSError:
        return {"active": DEFAULT_ID, "custom": []}
    if _cache["mtime"] != mtime:
        try:
            _cache["data"] = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache["data"] = {"active": DEFAULT_ID, "custom": []}
        _cache["mtime"] = mtime
    return _cache["data"]


def _save(data):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def all_personas():
    data = _load()
    return PRESETS + [dict(p, preset=False) for p in data.get("custom", [])]


def active():
    """当前人设 {"id", "name", "prompt"}；找不到就回到默认。"""
    want = _load().get("active", DEFAULT_ID)
    return next((p for p in all_personas() if p["id"] == want), PRESETS[0])


def cmd_list():
    act = active()["id"]
    return {"ok": True, "active": act, "personas": [dict(p, active=p["id"] == act) for p in all_personas()]}


def cmd_select(pid):
    if not any(p["id"] == pid for p in all_personas()):
        return {"ok": False, "message": "没有这个人设"}
    data = dict(_load())
    data["active"] = pid
    _save(data)
    return {"ok": True, "active": pid, "message": "已切换，下一句话就生效"}


def cmd_save(raw):
    try:
        req = json.loads(raw)
    except ValueError:
        return {"ok": False, "message": "参数不对"}
    name = re.sub(r"\s+", " ", str(req.get("name") or "")).strip()[:20]
    prompt = str(req.get("prompt") or "").strip()
    if not name or not prompt:
        return {"ok": False, "message": "名字和人设内容都要填"}
    data = dict(_load())
    custom = list(data.get("custom", []))
    pid = str(req.get("id") or "")
    if any(p["id"] == pid for p in PRESETS):
        return {"ok": False, "message": "预置人设不能改，可以另存一个新的"}
    row = next((p for p in custom if p["id"] == pid), None)
    if row is None:
        if len(custom) >= 20:
            return {"ok": False, "message": "自定义人设最多 20 个"}
        pid = "c" + str(int(time.time() * 1000))
        row = {"id": pid}
        custom.append(row)
    row.update(name=name, prompt=prompt, updated=time.strftime("%Y-%m-%d %H:%M"))
    data["custom"] = custom
    _save(data)
    return {"ok": True, "id": pid, "message": "已保存"}


def cmd_delete(pid):
    data = dict(_load())
    custom = [p for p in data.get("custom", []) if p["id"] != pid]
    if len(custom) == len(data.get("custom", [])):
        return {"ok": False, "message": "只能删除自己写的人设"}
    data["custom"] = custom
    if data.get("active") == pid:
        data["active"] = DEFAULT_ID
    _save(data)
    return {"ok": True, "message": "已删除"}


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        out = (cmd_list() if a[:1] == ["list"] else cmd_select(a[1]) if a[:1] == ["select"] and len(a) == 2
               else cmd_save(a[1] if len(a) == 2 else sys.stdin.read()) if a[:1] == ["save"]
               else cmd_delete(a[1]) if a[:1] == ["delete"] and len(a) == 2 else {"ok": False, "message": "用法错误"})
    except Exception as exc:
        out = {"ok": False, "message": "出错了：" + str(exc)[:160]}
    print(json.dumps(out, ensure_ascii=False))
