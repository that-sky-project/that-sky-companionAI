# -*- coding: utf-8 -*-
"""T23-3 背包魔法（只读清单 + 询问 + 记录意图）。

结论（证据见 work3/REPORT.md）：两份 captures 只有下行 GameMsg，能看到
  * AccountBuffRpc 里的 buff（漂浮魔法 1860519737、长大成人 891098028 …，含 giver/到期时间）；
  * 真人 Actor 的 prop 部位短暂变成 烟花法杖 (2643573525) 约 35 秒；
但**没有任何 UseItemRpc / GrantBuffRpc / SocialApplyBuffRpc / LaunchFireworkRpc 下行**，
也没有 consume_item1 的上行抓包，客户端“先 consume 再发什么”的完整链路无法确认。
因此本模块只做：列出背包里数量>0 的魔法、按中文名/俗称解析、记录使用意图；
**不调用 consume_item1，不改数量，不绕冷却**。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import xg_paths

try:                                        # T24-2：按号存（5b12 还是 control/）
    import xg_account
except Exception:
    xg_account = None

P_CONSUMABLES = "/account/consumable/get_consumables1"
P_DEFS = "/account/consumable/get_consumable_defs"
CACHE_SECONDS = 300.0


def _default_root():
    env = os.environ.get("SKY_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2]
    if (here / "analysis3").exists() or (here / "live").exists():
        return here
    return xg_paths.DATA

# 俗称/简称 → def 内部名（只用于解析；数量与冷却始终以服务器读回为准）
ALIASES = {
    "烟花": "fireworks", "放烟花": "fireworks", "烟花魔法": "fireworks", "浪漫烟花": "fireworks",
    "篝火": "bonfire", "火堆": "bonfire", "篝火魔法": "bonfire", "放篝火": "bonfire",
    "能量": "energy", "元气": "energy", "元气满满": "energy", "回能": "energy",
    "漂浮": "gravity", "反重力": "gravity", "飘起来": "gravity", "跳高": "gravity",
    "变大": "huge", "大只佬": "huge", "长高": "grow", "长大": "grow", "成人": "grow",
    "变小": "shrink", "小不点": "shrink", "返老还童": "shrink",
    "发光": "glow", "闪光": "glow", "璀璨": "glow", "星光": "glow",
    "彩虹": "rainbow", "彩虹魔法": "rainbow",
    "体型": "resize_potion", "重塑": "resize_potion", "身高": "resize_potion", "体型重塑": "resize_potion",
    "留影": "memory_candle", "留影蜡烛": "memory_candle", "蜡烛": "memory_candle",
    "共享空间": "sharedspace_candle", "空间": "sharedspace_candle",
    "桌子": "table", "长桌": "table", "畅谈长桌": "table",
    "呼吸": "breath_potion", "氧气": "breath_potion", "呼吸药剂": "breath_potion",
}


def _data(resp):
    if not isinstance(resp, dict):
        return {}
    for key in ("data", "result"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v
    return resp


def _rows(resp, key):
    d = _data(resp)
    v = d.get(key) if isinstance(d, dict) else None
    if v is None and isinstance(resp, dict):
        v = resp.get(key)
    return v if isinstance(v, list) else []


def _norm(text):
    import re
    s = str(text or "").strip().lower()
    s = s.replace("／", "/").replace("　", " ")
    s = re.sub(r"[\s，,。.!！?？~～、；;：:'\"“”‘’（）()\[\]【】]+", "", s)
    return s


class MagicBook:
    def __init__(self, client=None, root=None, control_dir=None, strings_path=None, clock=time.monotonic):
        self.client = client
        self.clock = clock
        self.root = Path(root) if root else _default_root()
        if control_dir:
            self.control = Path(control_dir)
        elif xg_account is not None:
            self.control = xg_account.state_dir(root=self.root)     # 按号：记录存到当前号自己的目录
        else:
            self.control = self.root / "control"
        self.strings_path = Path(strings_path) if strings_path else xg_paths.DATA / "analysis3" / "client_strings_zh.json"
        self._strings = None
        self._items = None
        self._at = -1e9

    def strings(self):
        if self._strings is None:
            try:
                d = json.loads(self.strings_path.read_text(encoding="utf-8"))
                self._strings = d.get("") if isinstance(d.get(""), dict) else (d if isinstance(d, dict) else {})
            except (OSError, ValueError, AttributeError):
                self._strings = {}
        return self._strings

    def zh_name(self, def_name):
        hit = self.strings().get("consumable_name_" + str(def_name))
        return hit or str(def_name)

    def items(self, force=False):
        """背包里的魔法/道具（含数量为 0 的，调用方自己过滤）：中文名、数量、冷却、buff_id。"""
        now = self.clock()
        if not force and self._items is not None and now - self._at < CACHE_SECONDS:
            return list(self._items)
        if self.client is None:
            rows, defs = [], []
        else:
            rows = _rows(self.client.post(P_CONSUMABLES, {}), "get_consumables")
            defs = _rows(self.client.post(P_DEFS, {}), "get_consumable_defs")
        defmap = {int(d.get("id")): d for d in defs if isinstance(d, dict) and d.get("id") is not None}
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                cid = int(r.get("consumable_id"))
            except (TypeError, ValueError):
                continue
            d = defmap.get(cid, {})
            name = d.get("name") or ""
            out.append({"id": cid, "def_name": name, "name": self.zh_name(name) if name else f"#{cid}",
                        "category": d.get("category") or "", "buff_id": int(d.get("buff_id") or 0),
                        "icon": d.get("icon") or "", "event": d.get("event") or "",
                        "qty": int(r.get("quantity") or 0),
                        "cooldown_until": int(r.get("cooldown_until") or 0)})
        self._items, self._at = out, now
        return list(out)

    def available(self):
        """数量 >0 的（不代表现在能用：冷却在界面提示，不绕过）。"""
        return [it for it in self.items() if it["qty"] > 0]

    def resolve(self, text):
        """中文名/俗称/内部名 → 背包里的一件（数量为 0 也返回，调用方给“用完了”）；歧义返回 (None, 候选)。"""
        q = _norm(text)
        if not q:
            return None, []
        q = q.replace("魔法", "") or q
        hits = []
        for it in self.items():
            words = {_norm(it["name"]), _norm(it["def_name"])}
            base = _norm(it["name"]).replace("魔法", "")
            if base:
                words.add(base)
            for alias, want in ALIASES.items():
                if want == it["def_name"]:
                    words.add(_norm(alias))
            score = 0
            for w in words:
                if not w:
                    continue
                if w == q:
                    score = max(score, 100 + len(w))
                elif len(q) >= 2 and len(w) >= 2 and (q in w or w in q):
                    score = max(score, 50 + min(len(q), len(w)))
            if score:
                hits.append((score, it))
        hits.sort(key=lambda x: (-x[0], -x[1]["qty"], x[1]["id"]))
        if not hits:
            return None, []
        if len(hits) > 1 and hits[0][0] == hits[1][0]:
            return None, [it for _s, it in hits[:4]]
        return hits[0][1], []

    def record_intent(self, magic, *, requested_by, approved_by, channel, note=""):
        """只记录意图，不消耗。落 control/magic_intent.json（最多留 50 条）。"""
        path = self.control / "magic_intent.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except (OSError, ValueError):
            rows = []
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "magic_id": magic.get("id"),
               "magic": magic.get("name"), "def_name": magic.get("def_name"),
               "qty": magic.get("qty"), "requested_by": requested_by, "approved_by": approved_by,
               "channel": channel, "executed": False, "note": note}
        rows.append(row)
        rows = rows[-50:]
        try:
            self.control.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return row

    def list_lines(self, max_len=48, max_parts=3):
        rows = [f"{it['name']}×{it['qty']}" for it in self.available()]
        lines, cur = [], ""
        for row in rows:
            if len(cur) + len(row) + 1 > max_len:
                if cur:
                    lines.append(cur)
                cur = row
            else:
                cur = (cur + " " + row).strip()
        if cur:
            lines.append(cur)
        if len(lines) > max_parts:
            extra = len(lines) - max_parts + 1
            lines = lines[:max_parts - 1] + [f"…还有{extra}条"]
        return lines or ["背包里没有能用的魔法"]
