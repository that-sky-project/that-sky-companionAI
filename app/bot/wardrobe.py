# -*- coding: utf-8 -*-
"""T23-3 小光的衣柜：只穿自己拥有的装扮。

红线（本文件负责）：
  * 拥有判定 = 内部名在 unlocks/get_all 里，或现在正穿着（get_outfit 里 id≠0），或 id=0（脱掉，仅允许为空的部位）；
  * 另外把“第一次换装前的原始套装”（control/outfit_original.json）算作拥有，否则“换回原来那套”永远回不去；
  * 任何计划只改目标部位的 id，其余字段（tex/pat/mask/dye/height/scale/voice/attitude/seed）原样深拷贝；
  * 写操作只有 set_outfit，整套覆盖：先拉完整装扮 → 深拷贝 → 只改目标部位 → 备份 → 整套写回 → 再读回核对。

数据：
  * unlocks：`/service/inventory/api/v1/unlocks/get_all` 的 `unlocks[].name`（默认缓存 300 秒）；
  * 装扮 id = FNV-1a32(内部名)（已验证：CharSkyKid_Hair_TwoTails → 1415190429）；
  * 中文名：同目录 outfit_names_zh.json（id → name/key）。

命令行：
  wardrobe.py list              # 分部位列出能穿的
  wardrobe.py show              # 当前穿着
  wardrobe.py try <说法>         # 只打印计划，不写
  wardrobe.py apply <说法>       # 备份 → set_outfit → 读回核对
  wardrobe.py voices            # 16 种叫声（哪些拥有）
  wardrobe.py try-voice <说法>   # 只打印叫声计划，不写
  wardrobe.py apply-voice <说法> # 备份 → set_outfit（只改 voice）→ 读回核对
  wardrobe.py restore <备份>     # 用 control/outfit_backup_*.json 整套写回
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
from pathlib import Path

import xg_paths

try:                                        # T24-2：按号存（5b12 还是 control/）
    import xg_account
except Exception:                           # 单独拷出去跑也不至于起不来
    xg_account = None

P_OUTFIT = "/account/get_outfit"
P_SET_OUTFIT = "/account/set_outfit"
P_UNLOCKS = "/service/inventory/api/v1/unlocks/get_all"

UNLOCK_CACHE_SECONDS = 300.0


def _default_root():
    env = os.environ.get("SKY_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2]
    if (here / "live").exists() or (here / "analysis3").exists():
        return here
    return xg_paths.DATA

# 显示顺序：官方 9 格 + face/legs/arms（legs/arms 不在 Actor 块里，但仍能写服务器）
SLOTS = [("hair", "发型"), ("wing", "斗篷"), ("body", "裤子"),
         ("horn", "头饰"), ("neck", "颈部"), ("mask", "面具"),
         ("hat", "发饰"), ("prop", "背饰"), ("feet", "鞋子"),
         ("face", "脸"), ("legs", "腿"), ("arms", "手臂")]
SLOT_LABEL = dict(SLOTS)
LABEL_SLOT = {label: slot for slot, label in SLOTS}
# 允许 id=0（脱掉）的部位；body/hair 是核心外观，不允许空
EMPTY_OK = {"wing", "prop", "hat", "horn", "neck", "mask", "feet", "face", "legs", "arms"}

SLOT_ALIASES = {
    "wing": ("斗篷", "翅膀", "披风", "翼"), "hair": ("发型", "头发", "发"), "body": ("裤子", "裤", "下装", "衣服", "裤装"),
    "horn": ("头饰", "角", "犄角", "角饰"), "neck": ("颈部", "脖子", "项链", "围巾", "颈饰"),
    "mask": ("面具", "面罩", "脸饰"), "hat": ("发饰", "帽子", "头巾", "头饰发"), "prop": ("背饰", "背挂", "背部", "道具", "背"),
    "feet": ("鞋子", "鞋", "脚", "靴子"), "face": ("脸", "面部", "面饰"),
    "legs": ("腿", "腿部", "下装腿"), "arms": ("手臂", "手", "袖子", "臂"),
}
# 俗称/简称 → 中文名（只影响解析，不绕过拥有判定）
ITEM_ALIASES = {
    "双马尾": "双麻花辫发型", "双麻花辫": "双麻花辫发型", "麻花辫": "双麻花辫发型", "双辫": "双麻花辫发型",
    "白斗篷": "白色斗篷", "经典白斗篷": "白色斗篷", "白鸟斗篷": "白色斗篷", "经典斗篷": "白色斗篷",
    "短裤": "经典短裤", "经典裤": "经典短裤", "阔腿裤": "阔腿长裤", "长裤": "阔腿长裤",
    "小矮人": "小矮人面具", "矮人面具": "小矮人面具", "基础面": "基础面具",
    "钢琴": "电子琴", "琴": "电子琴", "相机": "相机", "照相机": "相机",
}
# 内部名 -> 中文兜底（名表缺项时）
KEY_ZH = {"CharSkyKid_Prop_None": "空背饰"}

# ---------- T23-7 叫声（VoiceType） ----------
# 编号/英文来自 work9 对 analysis6/libBootloader.so 反射构造代码的还原（0..15，16 是 Count 哨兵）。
# 中文：work9 在客户端简中里查到的官方名（物品名 / 先祖名）；没有官方专名的照实写“无独立官方名”。
# unlock：永久拥有的解锁内部名；0 Default 是天然默认值，不需要解锁。
# short：聊天里“XX叫声”的短称呼；colloquial：常用俗称（用于回复/列表）。
VOICES = [
    {"id": 0, "en": "Default", "zh": "默认", "short": "默认", "colloquial": "默认叫声",
     "unlock": None, "aliases": ("默认", "默认叫声", "普通", "普通叫声", "原本", "原来", "初始", "default")},
    {"id": 1, "en": "Bird", "zh": "鸟语者", "short": "鸟", "colloquial": "鸟叫",
     "unlock": "emote_upgrade_bird_01",
     "aliases": ("鸟叫", "鸟鸣", "小鸟叫", "鸟语", "鸟语者", "鸟的叫声", "鸟的声音",
                 "小鸟的叫声", "bird")},
    {"id": 2, "en": "Manta", "zh": "遥鲲呼唤", "short": "遥鲲", "colloquial": "蝠鲼叫",
     "unlock": "emote_upgrade_manta_01",
     "aliases": ("蝠鲼叫", "蝠鲼", "鳐叫", "鳐鱼叫", "遥鲲", "遥鲲呼唤", "鳐语者", "manta")},
    {"id": 3, "en": "Whale", "zh": "鲸语者", "short": "鲸鱼", "colloquial": "鲸鱼叫",
     "unlock": "emote_upgrade_whale_01",
     "aliases": ("鲸鱼叫", "鲸叫", "鲸鱼", "鲸语者", "鲸的叫声", "鲸的声音",
                 "鲸鱼的叫声", "whale")},
    {"id": 4, "en": "Ghost", "zh": "灵语者", "short": "幽灵", "colloquial": "幽灵叫",
     "unlock": "emote_upgrade_ghost_01",
     "aliases": ("幽灵叫", "鬼叫", "幽灵", "灵语", "灵语者", "ghost")},
    {"id": 5, "en": "Crab", "zh": "螃蟹呼唤", "short": "螃蟹", "colloquial": "螃蟹叫",
     "unlock": "emote_upgrade_crabvoice_01",
     "aliases": ("螃蟹叫", "螃蟹", "蟹叫", "蟹语者", "crab")},
    {"id": 6, "en": "Jelly", "zh": "水母呼唤", "short": "水母", "colloquial": "水母叫",
     "unlock": "emote_upgrade_jelly_01",
     "aliases": ("水母叫", "水母", "母语者", "jelly", "jellyfish")},
    {"id": 7, "en": "Ai", "zh": "爱的呼唤", "short": "爱", "colloquial": "爱的呼唤",
     "unlock": None, "aliases": ("爱的呼唤", "爱叫", "kizuna", "ai")},
    {"id": 8, "en": "Baby", "zh": "光之低语者", "short": "光之低语", "colloquial": "光之低语",
     "unlock": None, "aliases": ("光之低语", "低语", "baby", "babymanta")},
    {"id": 9, "en": "NightBird", "zh": "夜行鸟语者", "short": "夜行鸟", "colloquial": "夜行鸟叫",
     "unlock": None, "aliases": ("夜行鸟叫", "夜行鸟", "nightbird")},
    {"id": 10, "en": "BlueBird", "zh": "青鸟（无独立官方叫声名）", "short": "青鸟", "colloquial": "青鸟叫",
     "unlock": None, "aliases": ("青鸟叫", "青鸟", "bluebird")},
    {"id": 11, "en": "AuroraStar", "zh": "欧若拉之声", "short": "欧若拉", "colloquial": "欧若拉之声",
     "unlock": None, "aliases": ("欧若拉", "极光", "aurora", "aurorastar")},
    {"id": 12, "en": "Manatee", "zh": "海牛叫声", "short": "海牛", "colloquial": "海牛叫",
     "unlock": None, "aliases": ("海牛叫", "海牛叫声", "海牛", "manatee")},
    {"id": 13, "en": "ManateeStar", "zh": "海牛星（无独立官方叫声名）", "short": "海牛星", "colloquial": "海牛星叫",
     "unlock": None, "aliases": ("海牛星叫", "海牛星", "manateestar")},
    {"id": 14, "en": "Lighthorn", "zh": "迁徙钟匠", "short": "迁徙钟匠", "colloquial": "钟匠叫",
     "unlock": None, "aliases": ("迁徙钟匠", "钟匠", "lighthorn")},
    {"id": 15, "en": "LighthornAv", "zh": "迁徙钟匠·Av（无独立官方叫声名）", "short": "钟匠Av", "colloquial": "钟匠Av叫",
     "unlock": None, "aliases": ("钟匠av", "lighthornav")},
]
VOICE_BY_ID = {v["id"]: v for v in VOICES}
VOICE_MAX = 15

_SLOT_BY_KEYWORD = {"hair": "hair", "wing": "wing", "body": "body", "mask": "mask", "neck": "neck",
                    "feet": "feet", "horn": "horn", "face": "face", "prop": "prop", "hat": "hat",
                    "legs": "legs", "arms": "arms"}
EMPTY_PROP_ID = 2035109393                          # = fnv1a32("CharSkyKid_Prop_None")：服务器用来表示“没背饰”
QUANT_RE = re.compile(r"^(?:一个|一件|一条|一顶|一双|一只|一把|一张|一套|个|件|条|顶|双|只|把|张|套|些)")


def fnv1a32(text):
    h = 0x811C9DC5
    for b in str(text).encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def _data(resp):
    if not isinstance(resp, dict):
        return {}
    for key in ("data", "result"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v
    return resp


def outfit_set(resp):
    """get_outfit / set_outfit 响应里的装扮对象（dict 或数组首项）。"""
    d = _data(resp)
    v = d.get("set_outfit") if isinstance(d, dict) else None
    if v is None and isinstance(resp, dict):
        v = resp.get("set_outfit")
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0]
    return v if isinstance(v, dict) else None


def _id_of(part):
    if isinstance(part, dict):
        try:
            return int(part.get("id") or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(part, int) and not isinstance(part, bool):
        return int(part)
    return 0


def _norm(text):
    s = str(text or "").strip().lower()
    s = s.replace("／", "/").replace("　", " ")
    s = re.sub(r"[\s，,。.!！?？~～、；;：:'\"“”‘’（）()\[\]【】]+", "", s)
    return s


def _clean_request(text):
    """去掉命令外壳和语气词，留下“要换的东西”。"""
    s = str(text or "").strip()
    s = re.sub(r"^[/／]\s*", "", s)
    s = re.sub(r"^(小光|光崽|宝贝|你|喂)[，,、\s]*", "", s)
    s = re.sub(r"^(帮我|给我|请你|麻烦你?|能不能|可以)[，,、\s]*", "", s)
    s = re.sub(r"(一下|吧|呀|啊|哦|呢|嘛|啦|呗|好不好|可以吗|行吗|好吗)+$", "", s)
    return s.strip()


def _strip_quant(text):
    return QUANT_RE.sub("", str(text or "").strip()).strip()


def _voice_int(value, default=None):
    """服务器读回来的 voice 可能是 int 或数字字符串；非法返回 default。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if 0 <= n <= VOICE_MAX else default


def voice_label(row):
    """列表里的一条：官方名（俗称）；默认只用俗称。"""
    if int(row["id"]) == 0:
        return "默认叫声"
    if row.get("colloquial") and row["colloquial"] != row.get("zh"):
        return f"{row['zh']}（{row['colloquial']}）"
    return row["zh"]


def voice_usage_text(rows=None):
    rows = [v for v in VOICES if v["id"] == 0] if rows is None else rows
    return "、".join(voice_label(v) for v in rows)


def _voice_target_text(text):
    """从“换成恐龙的叫声”里剥出“恐龙”，只用于“我还没有XX叫声呢”的回复。"""
    s = str(text or "").strip()
    s = re.sub(r"^[/／]\s*", "", s)
    s = re.sub(r"^(?:小光|光崽|宝贝|你|喂)[，,、\s]*", "", s)
    s = re.sub(r"^(?:把)?(?:叫声|声音|呼唤)(?:给|帮)?(?:我)?"
               r"(?:换成|换为|改成|改为|变成|变|换|改用|用)", "", s)
    s = re.sub(r"^(?:换成|换为|改成|改为|变成|变|换个|换|用|使用|来)(?:个|一个)?", "", s)
    s = re.sub(r"(?:的)?(?:叫声|声音|呼唤)(?:吧|呀|啊|哦|呢|嘛|啦)?$", "", s)
    return s.strip()


def resolve_voice(text):
    """说法 → (voice_id, matched, bad_id)。

    matched=True 表示认出了是哪种叫声（哪怕没拥有）；bad_id=True 表示写了
    超范围编号（16 以上）。都认不出返回 (None, False, False)。
    """
    s = _norm(text)
    if not s:
        return None, False, False
    best = None
    for v in VOICES:
        words = {_norm(v["zh"]), _norm(v["en"]), _norm(v["short"]),
                 _norm(v["colloquial"]), *(_norm(a) for a in v["aliases"])}
        for w in words:
            if not w:
                continue
            score = 0
            if s == w:
                score = 1000 + len(w)
            elif len(w) >= 2:
                if re.fullmatch(r"[a-z0-9]+", w):
                    hit = re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", s)
                else:
                    hit = w in s
                if hit:
                    score = 100 + len(w)
            if not score and len(s) >= 2 and len(w) >= 2 and s in w:
                score = 50 + len(s)
            if score and (best is None or score > best[0]):
                best = (score, int(v["id"]))
    if best is not None:
        return best[1], True, False
    m = re.search(r"(\d{1,2})\s*(?:号)?", s)
    if m and (re.fullmatch(r"\d{1,2}", s) or "号" in s or "叫声" in s or "声音" in s or "voice" in s):
        n = int(m.group(1))
        if 0 <= n <= VOICE_MAX:
            return n, True, False
        return None, False, True
    return None, False, False


def slot_of_text(text):
    """“斗篷/发型/裤子…”→部位 key；不是部位词返回 None。"""
    s = _norm(text)
    for key, words in SLOT_ALIASES.items():
        if s and s in {_norm(w) for w in words}:
            return key
    if s in LABEL_SLOT:
        return LABEL_SLOT[s]
    return None


def parse_change(text):
    """把一句换装请求拆成 {action, slot, target, restore}；不认识返回 None。"""
    said = _clean_request(text)
    if not said:
        return None
    if re.search(r"(换回|穿回|还原|恢复).{0,4}(原来|之前|原先|刚刚)|原来那套|原来的?装扮", said) \
            or re.fullmatch(r"(?:换|穿|变)回来", said):
        return {"action": "restore", "slot": None, "target": ""}
    m = re.match(r"^(?:把|将)?(.{0,8}?)(?:脱掉|脱了|脱下|摘掉|摘下|拿掉|去掉|不要了?|不穿)(.*)$", said)
    if m:
        head, tail = m.group(1).strip(), m.group(2).strip()
        target = (head + tail) if (head and not tail) else (tail or head)
        target = _strip_quant(re.sub(r"^(这个|那个|我的|你的)", "", target))
        return {"action": "strip", "slot": None, "target": target}
    slot = None
    for key, words in SLOT_ALIASES.items():
        for w in words:
            if w and w in said:
                if slot is None or len(w) > 1:
                    slot = key
                break
    # “斗篷换经典白斗篷”：部位在前，目标在后
    m = re.match(r"^(.{1,6}?)(?:换成|换为|换|改成|改为|穿成|穿上|穿)(.+)$", said)
    if m and slot and any(w in m.group(1) for w in SLOT_ALIASES.get(slot, ())):
        return {"action": "set", "slot": slot, "target": _strip_quant(m.group(2))}
    m = re.match(r"^(?:换成|换为|换|换上|穿上|穿|我要穿|我想穿|我想看你穿|想看你穿|看你穿|你穿|着|戴|背上|带上)(.+)$", said)
    if m:
        word = _strip_quant(m.group(1))
        return {"action": "set", "slot": slot_of_text(word), "target": word}
    if re.search(r"(换成|换上|穿|换|戴)", said) and len(said) <= 20:
        word = _strip_quant(re.sub(r"^.*?(?:换成|换上|换|穿|戴)", "", said))
        return {"action": "set", "slot": slot_of_text(word), "target": word}
    if slot and len(said) <= 12:
        return {"action": "set", "slot": slot, "target": ""}
    return None


class Wardrobe:
    def __init__(self, client=None, root=None, control_dir=None, name_table=None, clock=time.monotonic):
        self.client = client
        self.clock = clock
        default_root = _default_root()
        self.root = Path(root) if root else default_root
        if control_dir:
            self.control = Path(control_dir)
        elif xg_account is not None:
            self.control = xg_account.state_dir(root=self.root)     # 按号：5b12=control/，其它号=control/accounts/<id>/
        else:
            self.control = self.root / "control"
        if name_table:
            table = Path(name_table)
        else:
            # 名称表在用户机上生成：DATA/game/<版本>/names/（xg_paths.name_table 统一入口）
            table = Path(xg_paths.name_table("outfit_names_zh.json"))
        self._names = None
        self._name_table_path = table
        self._unlocks = None
        self._unlocks_at = -1e9
        self._unlocks_client = None
        self._current = None
        self._current_at = -1e9
        self.last_error = None

    # ---------- 数据 ----------
    def name_table(self):
        if self._names is None:
            try:
                self._names = json.loads(self._name_table_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._names = {}
        return self._names

    def name_of(self, fid, key=None):
        if int(fid) == EMPTY_PROP_ID and not key:
            return "空背饰"
        hit = self.name_table().get(str(fid))
        if isinstance(hit, dict) and hit.get("name"):
            return hit["name"]
        if key in KEY_ZH:
            return KEY_ZH[key]
        if key and key.startswith("CharSkyKid_"):
            tail = key.split("_", 2)[-1]
            if tail.lower() == "none":
                return KEY_ZH.get(key, "空")
            tail = re.sub(r"^(AP\d+|SO\d+|S\d+)", "", tail)
            tail = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tail).strip()
            return tail or key
        return f"#{fid}"

    def load_unlocks(self, force=False):
        now = self.clock()
        if (not force and self._unlocks is not None and self._unlocks_client is self.client
                and now - self._unlocks_at < UNLOCK_CACHE_SECONDS):
            return self._unlocks
        if self.client is None:
            names = []
        else:
            resp = self.client.post(P_UNLOCKS, {})
            data = _data(resp)
            rows = data.get("unlocks") if isinstance(data, dict) else None
            if rows is None and isinstance(resp, dict):
                rows = resp.get("unlocks")
            names = [r.get("name") for r in (rows or []) if isinstance(r, dict) and r.get("name")]
            if not names:
                self.last_error = "unlocks_empty_or_failed"
        self._unlocks, self._unlocks_at, self._unlocks_client = set(names), now, self.client
        return self._unlocks

    def current(self, force=False):
        now = self.clock()
        if not force and self._current is not None and now - self._current_at < 5.0:
            return copy.deepcopy(self._current)
        if self.client is None:
            raise RuntimeError("没有 client，读不到当前装扮")
        resp = self.client.post(P_OUTFIT, {})
        o = outfit_set(resp)
        if o is None:
            raise RuntimeError("读不到当前装扮")
        self._current, self._current_at = copy.deepcopy(o), now
        return copy.deepcopy(o)

    def original_outfit(self):
        try:
            o = json.loads((self.control / "outfit_original.json").read_text(encoding="utf-8"))
            return o if isinstance(o, dict) else None
        except (OSError, ValueError):
            return None

    def _slot_and_key(self, name):
        if not name.startswith("CharSkyKid_"):
            return None, None
        parts = name.split("_")
        if len(parts) < 3:
            return None, None
        slot = _SLOT_BY_KEYWORD.get(parts[1].lower())
        return slot, name

    def owned_items(self, current=None):
        """能穿的全部装扮：[{slot,id,name,key,source,equipped}]。"""
        current = self.current() if current is None else current
        seen, out = set(), []
        for name in sorted(self.load_unlocks()):
            slot, key = self._slot_and_key(name)
            if slot is None:
                continue
            fid = fnv1a32(name)
            if (slot, fid) in seen:
                continue
            seen.add((slot, fid))
            out.append({"slot": slot, "id": fid, "name": self.name_of(fid, key), "key": key,
                        "source": "unlock", "equipped": _id_of(current.get(slot)) == fid})
        for slot, _label in SLOTS:
            fid = _id_of(current.get(slot))
            if fid == 0 or (slot, fid) in seen:
                continue
            key = None
            hit = self.name_table().get(str(fid))
            if isinstance(hit, dict):
                key = hit.get("key")
            seen.add((slot, fid))
            out.append({"slot": slot, "id": fid, "name": self.name_of(fid, key), "key": key,
                        "source": "wearing", "equipped": True})
        orig = self.original_outfit() or {}
        for slot, _label in SLOTS:
            fid = _id_of(orig.get(slot))
            if fid == 0 or (slot, fid) in seen:
                continue
            seen.add((slot, fid))
            key = (self.name_table().get(str(fid)) or {}).get("key")
            out.append({"slot": slot, "id": fid, "name": self.name_of(fid, key), "key": key,
                        "source": "original", "equipped": _id_of(current.get(slot)) == fid})
        return out

    def is_owned(self, slot, fid, current=None, owned=None):
        """拥有判定：0=脱掉（仅 EMPTY_OK）；当前穿着；unlocks 内部名；第一次换装前的原始套装。"""
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            return False
        if fid == 0:
            return slot in EMPTY_OK
        if slot not in SLOT_LABEL:
            return False
        current = self.current() if current is None else current
        if _id_of(current.get(slot)) == fid:
            return True
        owned = self.owned_items(current) if owned is None else owned
        return any(it["slot"] == slot and it["id"] == fid and it["source"] in ("unlock", "original", "wearing")
                   for it in owned)

    # ---------- 叫声（T23-7） ----------
    def owned_voice_rows(self):
        """16 种叫声 + 是否拥有。默认永远算拥有；其余看 unlocks 里有没有 emote_upgrade_<spirit>_01。

        复用 load_unlocks() 的 300 秒缓存，不额外发请求。
        """
        names = self.load_unlocks()
        out = []
        for v in VOICES:
            owned = int(v["id"]) == 0 or bool(v.get("unlock") and v["unlock"] in names)
            out.append(dict(v, owned=owned))
        return out

    def owned_voices(self):
        """小光现在能用的叫声（只含拥有的）。"""
        return [v for v in self.owned_voice_rows() if v["owned"]]

    def voice_lines(self, max_len=48, max_parts=3):
        """聊天列表：只列拥有的叫声，每条 ≤max_len。"""
        rows = self.owned_voices()
        rows.sort(key=lambda v: int(v["id"]))
        prefix = "我会的叫声："
        parts, cur = [], ""
        for v in rows:
            label = voice_label(v)
            piece = label if not cur else cur + "、" + label
            head = prefix if not parts else ""
            if cur and len(head) + len(piece) > max_len:
                parts.append(head + cur)
                cur = label
            else:
                cur = piece
        if cur:
            parts.append((prefix if not parts else "") + cur)
        if len(parts) > max_parts:
            parts = parts[:max_parts - 1] + ["…还有别的叫声，问细一点我再报"]
        return parts

    def voice_report_lines(self):
        """CLI / 报告用：16 种全列，标注拥有与解锁名。"""
        lines = []
        for v in self.owned_voice_rows():
            flag = "可用" if v["owned"] else "未拥有"
            unlock = v.get("unlock") or "默认（无需解锁）"
            alias = "、".join(v.get("aliases") or ())
            lines.append(f"{int(v['id']):>2}  {v['en']:<11} {v['zh']}  [{flag}]  解锁:{unlock}"
                         f"  俗称:{alias}")
        return lines

    def plan_voice(self, request, current=None):
        """说法 → 只改 voice 的新装扮（不写服务器；写之前由 apply_voice 再判一次拥有）。"""
        current = self.current() if current is None else current
        cur = _voice_int(current.get("voice"), 0)
        vid, matched, bad_id = resolve_voice(request)
        if bad_id:
            return {"ok": False, "reason": "bad_id",
                    "message": "叫声编号只有 0 到 15 哦", "owned": voice_usage_text(self.owned_voices())}
        if vid is None:
            target = _voice_target_text(request)
            if target and any(w in str(request) for w in ("叫声", "声音", "呼唤")):
                return {"ok": False, "reason": "not_found", "name": target,
                        "message": f"我还没有{target}叫声呢",
                        "owned": voice_usage_text(self.owned_voices())}
            return {"ok": False, "reason": "unparsed",
                    "message": "没听懂要换哪种叫声，说“换成鸟叫”这样试试？",
                    "owned": voice_usage_text(self.owned_voices())}
        row = VOICE_BY_ID[vid]
        owned = self.owned_voices()
        owned_ids = {int(v["id"]) for v in owned}
        if vid not in owned_ids:
            return {"ok": False, "reason": "not_owned", "voice": vid, "name": row["zh"],
                    "message": f"我还没有{row['short']}叫声呢",
                    "usable": [int(v["id"]) for v in owned],
                    "owned": voice_usage_text(owned)}
        if vid == cur:
            return {"ok": False, "reason": "noop", "voice": vid, "name": row["zh"],
                    "message": f"我现在就是{row['colloquial']}呀",
                    "usable": [int(v["id"]) for v in owned]}
        new = copy.deepcopy(current)
        new["voice"] = int(vid)                    # 整数；attitude 等其它字段原样保留
        return {"ok": True, "action": "voice", "voice": int(vid), "name": row["zh"],
                "colloquial": row["colloquial"], "outfit": new,
                "changes": [{"slot": "voice", "from": cur, "to": int(vid)}],
                "message": f"叫声换成{row['colloquial']}啦～",
                "usable": [int(v["id"]) for v in owned]}

    # ---------- 解析 ----------
    def _candidates(self, text, slot=None, owned=None, current=None):
        text = _norm(text)
        owned = self.owned_items(current) if owned is None else owned
        out = []
        for it in owned:
            if slot and it["slot"] != slot:
                continue
            words = {_norm(it["name"]), _norm(it.get("key") or "")}
            base = re.sub(r"(发型|斗篷|裤子|面具|背饰|鞋子|头饰|颈部|发饰|脸|腿|手臂)$", "", it["name"])
            if base and base != it["name"]:
                words.add(_norm(base))
            key = it.get("key") or ""
            if key.startswith("CharSkyKid_"):
                tail = re.sub(r"^(AP\d+|SO\d+|S\d+)", "", key.split("_", 2)[-1])
                words.add(_norm(tail))
                words.add(_norm(re.sub(r"(?<=[a-z])(?=[A-Z])", "", tail)))
            for alias, want in ITEM_ALIASES.items():
                if want == it["name"]:
                    words.add(_norm(alias))
            score = 0
            for w in words:
                if not w:
                    continue
                if w == text:
                    score = max(score, 100 + len(w))
                elif len(text) >= 2 and len(w) >= 2 and (text in w or w in text):
                    score = max(score, 50 + min(len(text), len(w)))
            if score:
                out.append((score, it))
        out.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [it for _s, it in out]

    def _known_but_not_owned(self, text, slot=None):
        """名表里查得到、但不在拥有清单里的装扮（用来区分“没有这件”和“听不懂”）。"""
        text = _norm(text)
        owned_ids = {it["id"] for it in self.owned_items()}
        hits = []
        for fid, row in self.name_table().items():
            if not isinstance(row, dict) or not row.get("key"):
                continue
            s, key = self._slot_and_key(row["key"])
            if s is None or (slot and s != slot):
                continue
            name = _norm(row.get("name") or "")
            base = re.sub(r"(发型|斗篷|裤子|面具|背饰|鞋子|头饰|颈部|发饰|脸|腿|手臂)$", "", name)
            if text and (text == name or text == base or (len(text) >= 2 and (text in name or name in text))):
                if int(fid) not in owned_ids:
                    hits.append({"slot": s, "id": int(fid), "name": row.get("name"), "key": row["key"]})
        return hits

    # ---------- 计划 ----------
    def _summary_parts(self, outfit):
        out = []
        for slot, label in SLOTS:
            fid = _id_of(outfit.get(slot))
            if fid:
                out.append(f"{label}：{self.name_of(fid)}")
        return out

    def plan_change(self, request, current=None):
        """请求 → 只含拥有物品的新装扮（不写服务器）。"""
        current = self.current() if current is None else current
        parsed = parse_change(request)
        if parsed is None:
            return {"ok": False, "reason": "unparsed", "message": "没听懂要换哪件，说“换双马尾”这样试试？"}
        if parsed["action"] == "restore":
            orig = self.original_outfit()
            if not isinstance(orig, dict) or not orig:
                return {"ok": False, "reason": "no_baseline", "message": "我还没存过“原来那套”，先换一次才有得换回来"}
            owned = self.owned_items(current)
            bad = [slot for slot, _l in SLOTS
                   if _id_of(orig.get(slot)) and not self.is_owned(slot, _id_of(orig.get(slot)), current, owned)]
            if bad:
                return {"ok": False, "reason": "not_owned", "message": "原来那套里有我现在没有的装扮，不能穿回去"}
            return {"ok": True, "action": "restore", "outfit": copy.deepcopy(orig),
                    "changes": [{"slot": s, "from": _id_of(current.get(s)), "to": _id_of(orig.get(s))}
                                for s, _l in SLOTS if _id_of(current.get(s)) != _id_of(orig.get(s))],
                    "message": "换回原来那套啦～"}
        slot, target = parsed["slot"], parsed["target"]
        owned = self.owned_items(current)
        if parsed["action"] == "strip":
            if not target:
                return {"ok": False, "reason": "no_target", "message": "要脱掉哪件呀？"}
            if not slot:
                # 目标名可能是装扮名，也可能是部位名
                hits = self._candidates(target, None, owned, current)
                slots = {it["slot"] for it in hits}
                for key, words in SLOT_ALIASES.items():
                    if _norm(target) in {_norm(w) for w in words}:
                        slots.add(key)
                slots = {s for s in slots if _id_of(current.get(s))}
                if len(slots) != 1:
                    if not slots:
                        return {"ok": False, "reason": "not_wearing", "message": f"我没有戴着{target}呀"}
                    return {"ok": False, "reason": "ambiguous", "message": "你指的是" + "还是".join(
                        SLOT_LABEL.get(s, s) for s in sorted(slots)) + "？"}
                slot = slots.pop()
            if slot not in EMPTY_OK:
                return {"ok": False, "reason": "cannot_empty", "message": f"{SLOT_LABEL.get(slot, slot)}不能脱掉哦"}
            if _id_of(current.get(slot)) == 0:
                return {"ok": False, "reason": "noop", "message": f"我现在就没戴{SLOT_LABEL.get(slot, slot)}呀"}
            new = copy.deepcopy(current)
            before = _id_of(new.get(slot))
            if isinstance(new.get(slot), dict):
                new[slot]["id"] = 0
            else:
                new[slot] = {"id": 0}
            return {"ok": True, "action": "strip", "outfit": new,
                    "changes": [{"slot": slot, "from": before, "to": 0}],
                    "message": f"{SLOT_LABEL.get(slot, slot)}脱掉啦～"}
        # set
        if target and slot and _norm(target) in {_norm(w) for w in SLOT_ALIASES.get(slot, ())}:
            target = ""                     # “换背饰”：只说了部位，没说具体哪件
        if target and not slot:
            s2 = slot_of_text(target)
            if s2:
                slot, target = s2, ""
        hits = self._candidates(target or "", slot, owned, current)
        if not hits and not target and slot:
            hits = [it for it in owned if it["slot"] == slot and not it["equipped"]]
        if len(hits) > 1:
            # 同分同名：优先不在身上的；还多个就问
            rest = [it for it in hits if not it["equipped"]]
            if len(rest) == 1:
                hits = rest
            else:
                names = []
                for it in hits[:4]:
                    if it["name"] not in names:
                        names.append(it["name"])
                if len(names) == 1:
                    hits = [hits[0]]
                else:
                    return {"ok": False, "reason": "ambiguous",
                            "message": f"你说的是{'还是'.join(names[:2])}？",
                            "candidates": [{"slot": it["slot"], "id": it["id"], "name": it["name"]} for it in hits[:4]]}
        if not hits:
            known = self._known_but_not_owned(target or "", slot)
            if known:
                return {"ok": False, "reason": "not_owned",
                        "message": f"我没有{known[0]['name']}呢", "known": known[:3]}
            if slot:
                have = [it["name"] for it in owned if it["slot"] == slot]
                tail = ("；我只有" + "、".join(have)) if have else ""
                return {"ok": False, "reason": "not_found", "message": f"我没有{target or SLOT_LABEL.get(slot, '')}呢{tail}"}
            return {"ok": False, "reason": "not_found", "message": f"我没有{target}呢"}
        it = hits[0]
        if it["equipped"]:
            return {"ok": False, "reason": "noop", "message": f"现在就穿着{it['name']}呢"}
        if not self.is_owned(it["slot"], it["id"], current, owned):
            return {"ok": False, "reason": "not_owned", "message": f"我没有{it['name']}呢"}
        new = copy.deepcopy(current)
        before = _id_of(new.get(it["slot"]))
        if isinstance(new.get(it["slot"]), dict):
            new[it["slot"]]["id"] = it["id"]
        else:
            new[it["slot"]] = {"id": it["id"], "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"}
        return {"ok": True, "action": "set", "outfit": new,
                "changes": [{"slot": it["slot"], "from": before, "to": it["id"]}],
                "message": f"{SLOT_LABEL.get(it['slot'], it['slot'])}换成{it['name']}啦～",
                "item": {"slot": it["slot"], "id": it["id"], "name": it["name"]}}

    # ---------- 写 ----------
    def _backup_name(self):
        return f"outfit_backup_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.json"

    def apply(self, plan=None, request=None):
        """备份 → set_outfit（整套）→ 再读回核对。plan/request 二选一。"""
        if plan is None:
            plan = self.plan_change(request or "")
        if not plan.get("ok"):
            return plan
        current = self.current(force=True)
        owned = self.owned_items(current)
        for ch in plan.get("changes") or []:        # 执行前再判一次拥有（防伪造 plan / 装扮已过期）
            slot, to = ch.get("slot"), int(ch.get("to") or 0)
            if to == 0:
                if slot not in EMPTY_OK:
                    return {"ok": False, "reason": "cannot_empty", "message": f"{SLOT_LABEL.get(slot, slot)}不能脱掉哦"}
            elif not self.is_owned(slot, to, current, owned):
                return {"ok": False, "reason": "not_owned", "message": f"我没有{self.name_of(to)}呢"}
        if plan.get("action") == "restore":
            new = copy.deepcopy(plan["outfit"])
            for slot, _label in SLOTS:              # 恢复的那套也必须每件都还拥有
                fid = _id_of(new.get(slot))
                if fid and not self.is_owned(slot, fid, current, owned):
                    return {"ok": False, "reason": "not_owned", "message": f"原来那套里的{self.name_of(fid)}我现在没有"}
        else:
            new = copy.deepcopy(current)
            for ch in plan.get("changes") or []:
                slot = ch["slot"]
                if _id_of(current.get(slot)) != ch["from"]:
                    return {"ok": False, "reason": "stale",
                            "message": "装扮刚刚变过，重新说一次吧", "slot": slot,
                            "now": _id_of(current.get(slot)), "planned_from": ch["from"]}
                if isinstance(new.get(slot), dict):
                    new[slot]["id"] = ch["to"]
                else:
                    new[slot] = {"id": ch["to"], "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"}
        self.control.mkdir(parents=True, exist_ok=True)
        backup = self.control / self._backup_name()
        backup.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        orig = self.control / "outfit_original.json"
        if not orig.exists():                       # 第一次换装前 = “原来那套”
            orig.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        resp = self.client.post(P_SET_OUTFIT, {"outfit": new})
        after = outfit_set(resp)
        try:
            after = self.current(force=True)        # 任务要求：写完再读回核对
        except RuntimeError as exc:
            return {"ok": False, "reason": "reread_failed", "message": f"写完了但读不回来：{exc}",
                    "backup": str(backup), "before": current, "after": after, "response": str(resp)[:300]}
        changes = plan.get("changes") or []
        mismatches = []
        for ch in changes:
            if _id_of(after.get(ch["slot"])) != ch["to"]:
                mismatches.append({"slot": ch["slot"], "want": ch["to"], "got": _id_of(after.get(ch["slot"]))})
        if plan.get("action") != "restore":
            touched = {ch["slot"] for ch in changes}
            for slot, _label in SLOTS:
                if slot not in touched and _id_of(after.get(slot)) != _id_of(current.get(slot)):
                    mismatches.append({"slot": slot, "want": _id_of(current.get(slot)),
                                       "got": _id_of(after.get(slot)), "why": "unintended_change"})
        return {"ok": not mismatches, "reason": "ok" if not mismatches else "verify_failed",
                "message": plan.get("message") if not mismatches else "换了但读回来对不上，先别信这个结果",
                "backup": str(backup), "original": str(orig) if orig.exists() else None,
                "before": current, "after": after, "changes": changes, "mismatches": mismatches,
                "response": str(resp)[:300]}

    def apply_voice(self, plan=None, request=None):
        """备份 → set_outfit（整套，只改 voice）→ 再读回核对。

        ``plan`` 可以来自 plan_voice，也可以是外部伪造的；这里不信任 plan 里的
        outfit，一律重新拉当前完整装扮再改 voice，并在写之前重新判一次拥有。
        """
        if plan is None:
            plan = self.plan_voice(request or "")
        if not plan.get("ok"):
            return plan
        vid = _voice_int(plan.get("voice"))
        if vid is None:
            return {"ok": False, "reason": "bad_id", "message": "叫声编号只有 0 到 15 哦"}
        current = self.current(force=True)
        cur = _voice_int(current.get("voice"), 0)
        owned = self.owned_voices()                     # 写之前再判一次拥有（防伪造 plan / 解锁过期）
        owned_ids = {int(v["id"]) for v in owned}
        row = VOICE_BY_ID.get(vid)
        if vid not in owned_ids:
            name = row["short"] if row else vid
            return {"ok": False, "reason": "not_owned", "voice": vid,
                    "message": f"我还没有{name}叫声呢", "owned": voice_usage_text(owned)}
        if vid == cur:
            return {"ok": False, "reason": "noop", "voice": vid,
                    "message": f"我现在就是{row['colloquial']}呀"}
        new = copy.deepcopy(current)
        new["voice"] = int(vid)                         # attitude 是字符串，原样保留
        self.control.mkdir(parents=True, exist_ok=True)
        # room_pose 只认这份名单（Claude 加的第二道保险：Actor 里也不会出现没拥有的叫声）。
        tmp = self.control / ".voices_owned.json.tmp"
        tmp.write_text(json.dumps({"owned": sorted(owned_ids), "at": time.time()}), encoding="utf-8")
        tmp.replace(self.control / "voices_owned.json")
        backup = self.control / self._backup_name()
        backup.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        resp = self.client.post(P_SET_OUTFIT, {"outfit": new})
        try:
            after = self.current(force=True)
        except RuntimeError as exc:
            return {"ok": False, "reason": "reread_failed", "message": f"写完了但读不回来：{exc}",
                    "backup": str(backup), "before": current, "response": str(resp)[:300]}
        mismatches = []
        got = _voice_int(after.get("voice"), None)
        if got != vid:
            mismatches.append({"slot": "voice", "want": vid, "got": after.get("voice")})
        touched = {"voice"}
        for key in current:                             # 整套覆盖：voice 以外的字段必须原样
            if key in touched:
                continue
            if after.get(key) != current.get(key):
                mismatches.append({"slot": key, "want": current.get(key),
                                   "got": after.get(key), "why": "unintended_change"})
        return {"ok": not mismatches, "reason": "ok" if not mismatches else "verify_failed",
                "message": plan.get("message") if not mismatches else "换了但读回来对不上，先别信这个结果",
                "backup": str(backup), "before": current, "after": after,
                "voice": vid, "name": row["zh"] if row else None,
                "changes": [{"slot": "voice", "from": cur, "to": vid}],
                "mismatches": mismatches, "response": str(resp)[:300]}

    def restore(self, backup_path):
        """从 control/outfit_backup_*.json 整套写回（只认 control 目录下的备份名）。"""
        p = Path(backup_path)
        if not p.is_absolute():
            p = self.control / p
        p = p.resolve()
        try:
            p.relative_to(self.control.resolve())
        except ValueError:
            return {"ok": False, "reason": "bad_path", "message": "只能恢复 control/ 里的备份"}
        if not (p.name.startswith("outfit_backup_") or p.name == "outfit_original.json"):
            return {"ok": False, "reason": "bad_path", "message": "不是换装备份文件"}
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "reason": "bad_backup", "message": "备份读不出来"}
        if not isinstance(old, dict):
            return {"ok": False, "reason": "bad_backup", "message": "备份格式不对"}
        current = self.current(force=True)
        self.control.mkdir(parents=True, exist_ok=True)
        cur_backup = self.control / self._backup_name()
        cur_backup.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        resp = self.client.post(P_SET_OUTFIT, {"outfit": copy.deepcopy(old)})
        after = self.current(force=True)
        mism = [{"slot": s, "want": _id_of(old.get(s)), "got": _id_of(after.get(s))}
                for s, _l in SLOTS if _id_of(old.get(s)) != _id_of(after.get(s))]
        return {"ok": not mism, "reason": "ok" if not mism else "verify_failed",
                "message": "恢复好啦～" if not mism else "恢复了但读回来对不上",
                "backup_of_current": str(cur_backup), "after": after, "mismatches": mism,
                "response": str(resp)[:300]}

    # ---------- 展示 ----------
    def list_lines(self, max_len=48, max_parts=3):
        """按部位列能穿的，每条 ≤max_len；太长就分条，最多 max_parts 条。"""
        current = self.current()
        owned = self.owned_items(current)
        rows = []
        for slot, label in SLOTS:
            items = [it for it in owned if it["slot"] == slot]
            if not items:
                continue
            names = []
            for it in items:
                if it["id"] == fnv1a32("CharSkyKid_Prop_None"):
                    continue
                names.append(it["name"] + ("（穿着）" if it["equipped"] else ""))
            if names:
                rows.append(f"{label}：" + "、".join(names))
        lines, cur = [], ""
        for row in rows:
            if len(cur) + len(row) + (1 if cur else 0) > max_len:
                if cur:
                    lines.append(cur)
                cur = row if len(row) <= max_len else row[:max_len - 1] + "…"
            else:
                cur = (cur + " " + row).strip()
        if cur:
            lines.append(cur)
        if len(lines) > max_parts:
            extra = len(lines) - max_parts + 1
            lines = lines[:max_parts - 1] + [f"…还有{extra}条，问细一点我再报"]
        return lines

    def show_lines(self, max_len=60):
        current = self.current()
        parts = self._summary_parts(current)
        return [part for part in parts] if parts else ["身上什么都没穿"]

    def list_text(self):
        return "\n".join(self.list_lines())


# ---------- 命令行：真机用（测试不碰这里） ----------
def _make_client():
    root = Path(os.environ.get("SKY_ROOT") or xg_paths.DATA)
    sys.path.insert(0, str(xg_paths.QFSKY_CODE.parent))
    sys.path.insert(0, str(xg_paths.BOT_CODE))
    import long_runner as L
    from qfsky.skyapi import SkyClient
    acc = L.load_account((root / "control/active_account").read_text().strip())
    return root, SkyClient(acc["user_uuid"], acc["session"], acc["session2"], acc.get("data_version") or "")


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    root, client = _make_client()
    w = Wardrobe(client, root=root)
    if a[:1] == ["list"]:
        for it in w.owned_items():
            print(f"{SLOT_LABEL.get(it['slot'], it['slot'])}  {it['id']:>10}  {it['name']}  {it['key'] or ''}"
                  + ("  ← 穿着" if it["equipped"] else ""))
        return 0
    if a[:1] == ["show"]:
        for line in w.show_lines():
            print(line)
        return 0
    if a[:1] == ["try"] and len(a) >= 2:
        _print(w.plan_change(" ".join(a[1:])))
        return 0
    if a[:1] == ["apply"] and len(a) >= 2:
        plan = w.plan_change(" ".join(a[1:]))
        _print(w.apply(plan))
        return 0 if plan.get("ok") else 1
    if a[:1] == ["voices"]:
        for line in w.voice_report_lines():
            print(line)
        return 0
    if a[:1] == ["try-voice"] and len(a) >= 2:
        _print(w.plan_voice(" ".join(a[1:])))
        return 0
    if a[:1] == ["apply-voice"] and len(a) >= 2:
        plan = w.plan_voice(" ".join(a[1:]))
        _print(w.apply_voice(plan))
        return 0 if plan.get("ok") else 1
    if a[:1] == ["restore"] and len(a) == 2:
        _print(w.restore(a[1]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
