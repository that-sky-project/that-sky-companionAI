"""小光自己逛（2026-10-31）：闲着的时候由 DeepSeek 按人设自己决定干什么——在图里走走、找人点火、
鞠躬打招呼、做个动作、找椅子坐下聊天，或者就待着；可以顺口说一句。

代码只管边界：
- 只在“闲着”时问模型：没在牵手/背背/跟随/举烛/坐着，最近 90 秒没人跟她说话、3 分钟内没人给她下动作指令；
- 动作都走现有姿态控制（control/pose.json），和聊天里 [动作:...] 同一套；目标只能是房间名单里的人；
- 自言自语最多 3 分钟一句；主动坐下且旁边有人时，必须提醒“聊天要在前面加/”（10 分钟最多一次），
  模型自己的话里没带“/”就换成固定提醒；不加“/”的话照样记进记忆当上下文，只是不回。
开关：哥哥说“/去逛逛”开、“/别乱跑”关（control/wander，默认开）。
"""
import json
import math
import random
import re
import threading
import time
from pathlib import Path

import xg_paths

ROOT = xg_paths.DATA
STATE = xg_paths.POSE_STATE
SWITCH = ROOT / "control/wander"

TICK = (12.0, 30.0)           # 两次自主决定之间随机等这么久（秒）
QUIET_AFTER_CHAT = 45.0       # 有人跟她说话后这么久不乱跑
QUIET_AFTER_ORDER = 45.0      # 有人给她下动作指令后这么久不乱跑
SIT_MINUTES = (2.0, 4.0)      # 主动坐下后至少坐这么久
CANDLE_SECONDS = 8.0          # 帮人点火后举这么久放下
SAY_EVERY = 180.0             # 自言自语最多这么久一句
SIT_HINT_EVERY = 600.0        # 坐下提醒“加/”最多 10 分钟一次
REVISIT = 300.0               # 同一个人 5 分钟内不重复去缠
SIT_HINT = "我在这儿坐一会儿～想跟我聊天的话，话前面加个“/”哦，比如“/你好”"
ACTIONS = {
    "candle": "走到某人身边举蜡烛帮他点火（要 target）",
    "greet": "走到某人身边做个打招呼的动作（要 target，emote 选一个）",
    "emote": "原地做个动作（emote 选一个）",
    "stroll": "沿着地面走到某人附近逛逛（要 target）",
    "explore": "沿着地面到附近随便走走，不找谁（3~12 米外的地方）",
    "sit": "找附近的空椅子坐下，和旁边的人聊聊（只有 can_sit 为真才能选）",
    "stay": "就待着发会儿呆，什么也不做",
}


def enabled():
    try:
        return SWITCH.read_text().strip() != "off"
    except OSError:
        return True


def set_enabled(on):
    SWITCH.parent.mkdir(parents=True, exist_ok=True)
    SWITCH.write_text("on" if on else "off")


class Wanderer(threading.Thread):
    def __init__(self, bot, log, *, llm, persona, situation, emotes, resolve_emote, seats_for):
        super().__init__(daemon=True, name="wander")
        self.bot, self.log, self.llm = bot, log, llm
        self.persona, self.situation, self.emotes = persona, situation, emotes
        self.resolve_emote, self.seats_for = resolve_emote, seats_for
        self.last_chat = self.last_order = -1e9          # 上线不用等
        self.wake = threading.Event()
        self.last_say = self.last_hint = -1e9
        self.busy_until = 0.0
        self.sitting_since = None
        self.visited = {}                     # uuid -> 上次被小光主动找的时间
        self.done = []                        # 最近自己做过的事（给模型看，别老重复）

    # ---------- 外部通知 ----------
    def heard(self):
        self.last_chat = time.monotonic()

    def kick(self):
        """哥哥让她去逛：开开关、不等安静期，马上决定一次。"""
        set_enabled(True)
        self.last_chat = self.last_order = -1e9
        self.wake.set()

    def ordered(self):
        self.last_order = time.monotonic()
        self.sitting_since = None
        self.busy_until = 0.0

    # ---------- 状态 ----------
    def _state(self):
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _people(self, st):
        """房间里除了小光自己的人：{名字: uuid}（有位置的才算）。重名加后缀区分。"""
        room = st.get("room") or {}
        me = self.bot.account["user_uuid"]
        origins = room.get("origins") or {}
        out = {}
        for s, u in (room.get("slots") or {}).items():
            if u == me or s not in origins:
                continue
            name = self.bot.name_of(u)
            out[name if name not in out else f"{name}#{u[:4]}"] = (u, origins[s])
        return out

    # ---------- 主循环 ----------
    def run(self):
        self.log("wander_start", on=enabled())
        first = True
        while True:
            self.wake.wait(10.0 if first else random.uniform(*TICK))
            first = False
            self.wake.clear()
            try:
                self.tick()
            except Exception as exc:              # 逛不成不影响聊天
                self.log("wander_error", error=str(exc)[:160])

    def tick(self):
        if not enabled():
            return
        st = self._state()
        now = time.monotonic()
        pose = str(st.get("pose") or "idle")
        if self.sitting_since is not None:     # 自己坐下的：没坐够、或旁边有人在聊，就继续坐着
            if not pose.startswith("sit"):
                self.sitting_since = None
            elif now < self.busy_until or now - self.last_chat < QUIET_AFTER_CHAT:
                return
        elif pose == "candle" and getattr(self, "my_candle", False) and now > self.busy_until:
            self.my_candle = False                  # 只放下自己举的；放下后一定鞠躬
            self._do("put_down", {"pose": "idle"})
            threading.Timer(1.0, self.bot.control, args=({"pose": "anim", "name": "BowEmote_2", "seconds": 4.0},),
                            kwargs={"kind": "wander_bow"}).start()
            return
        if now - self.last_chat < QUIET_AFTER_CHAT or now - self.last_order < QUIET_AFTER_ORDER:
            return
        if self.sitting_since is None and pose not in ("idle", "face"):
            return                             # 在跟随/牵手/走路/举烛（别人让的）：不打扰
        self.decide(st, sitting=self.sitting_since is not None)

    # ---------- 让模型决定 ----------
    def decide(self, st, sitting):
        people = self._people(st)
        now = time.monotonic()
        can_sit = (st.get("seats_here") or 0) > 0 and not sitting   # 这个房间里确实有空椅子
        roster = []
        for name, (u, _o) in people.items():
            note = self.bot.memory.person_note(u) if hasattr(self.bot.memory, "person_note") else ""
            recent = "（你几分钟前刚找过他）" if now - self.visited.get(u, -1e9) < REVISIT else ""
            dark = "" if u in self.bot.names or u == self.bot.owner else "（小黑：还不是好友、没点亮）"
            roster.append(f"- {name}{'（哥哥）' if u == self.bot.owner else ''}{dark}{recent}{('：' + note) if note else ''}")
        history = "\n".join(("你" if m["role"] == "assistant" else "") + m["content"]
                            for m in self.bot.memory.history()[-10:])
        prompt = (
            self.persona() + self.situation() +
            "\n\n现在没人在跟你说话，你在自己逛。按你的性格决定接下来做什么，像真人玩家一样自然、有变化，"
            "别每次都一样，也别太闹腾（有时候待着发呆也很正常）。看到小黑可以主动去帮他点火（点完你会自动鞠躬）。“小黑”只是你心里对还没点亮的人的叫法，说话时别这么叫人，也别老念叨“小黑”。\n"
            f"【你现在】{'坐在椅子上' if sitting else '站着'}；can_sit={'是' if can_sit else '否'}\n"
            f"【房间里的人】\n{chr(10).join(roster) or '（只有你自己）'}\n"
            f"【最近的聊天】\n{history or '（没有）'}\n"
            f"【你刚才自己做过的事】{'；'.join(self.done[-5:]) or '（还没有）'}\n"
            "【能做的事】\n" + "\n".join(f"- {k}：{v}" for k, v in ACTIONS.items())
            + ("\n- stand：从椅子上站起来去逛" if sitting else "") +
            f"\n【能用的动作 emote】{'、'.join(self.emotes)}\n"
            "可以顺口说一句话（say，20 字以内，口语，可以留空；大多数时候留空）。"
            "只输出一个 JSON：{\"action\": \"...\", \"target\": \"房间里的人名或空\", \"emote\": \"动作名或空\", \"say\": \"...\"}")
        msg = self.llm.chat([{"role": "user", "content": prompt}], max_tokens=150, temperature=0.95, timeout=20)
        raw = msg.get("content") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            d = json.loads(m.group()) if m else {}
        except ValueError:
            d = {}
        act, target = str(d.get("action") or "stay"), str(d.get("target") or "").strip()
        emote, say = str(d.get("emote") or "").strip(), str(d.get("say") or "").strip()[:40]
        who = people.get(target, (None, None))[0]
        self.log("wander_decide", action=act, target=target, emote=emote, say=say)
        self._execute(act, who, target, emote, say, people, sitting)

    def _execute(self, act, who, target, emote, say, people, sitting):
        now = time.monotonic()
        anim, seconds = self.resolve_emote(emote) if emote else (None, 0)
        if act in ("candle", "greet", "stroll") and who is None:
            act = "emote" if anim else "stay"            # 目标不在名单里：退化成原地
        if act == "candle":
            self.visited[who] = now
            self.busy_until = now + CANDLE_SECONDS + 10
            self.my_candle = True
            self._do("candle", {"pose": "approach", "target": who, "distance": 1.0,
                                "then": {"pose": "candle", "target": who}}, who=target)
        elif act == "greet":
            self.visited[who] = now
            then = {"pose": "anim", "name": anim, "seconds": seconds} if anim else None
            self._do("greet", {"pose": "approach", "target": who, "distance": 1.2, "then": then}, who=target, emote=emote)
        elif act == "stroll":
            o = people[target][1]
            a, r = random.uniform(0, 2 * math.pi), random.uniform(2.0, 4.0)
            self._do("stroll", {"pose": "explore", "toward": list(o)}, who=target)
        elif act == "explore":
            self._do("explore", {"pose": "explore"})
        elif act in ("emote", "stay") and anim:
            self._do("emote", {"pose": "anim", "name": anim, "seconds": seconds}, emote=emote)
        elif act == "sit" and not sitting:
            near = people.get(target, (None,))[0] or (next(iter(people.values()))[0] if people else None)
            self.sitting_since = now
            self.busy_until = now + random.uniform(*SIT_MINUTES) * 60
            self._do("sit", {"pose": "sit", **({"near": near} if near else {})}, who=target)
            if people and now - self.last_hint > SIT_HINT_EVERY:      # 主动坐下：必须提醒“加/”
                self.last_hint = self.last_say = now
                line = say if ("/" in say or "／" in say) else (say + " 想跟我聊天的话，话前面加个“/”哦～" if say else SIT_HINT)
                threading.Timer(6.0, self.bot.send, args=(line,)).start()
                return
        elif act == "stand" and sitting:
            self.sitting_since = None
            self._do("stand", {"pose": "stand"})
        else:
            self.done.append("待着发了会儿呆")
        if say and now - self.last_say > SAY_EVERY and people:
            self.last_say = now
            threading.Timer(2.0, self.bot.send, args=(say,)).start()

    def _do(self, what, cmd, **info):
        self.log("wander_act", act=what, **info)
        self.done.append({"candle": "帮{who}点了火", "greet": "跟{who}打了招呼", "stroll": "走到{who}附近逛了逛",
                          "emote": "做了个{emote}", "explore": "在附近随便走了走", "sit": "找椅子坐下了", "stand": "站起来了",
                          "put_down": "放下了蜡烛"}.get(what, what).format(who=info.get("who", ""), emote=info.get("emote", "")))
        del self.done[:-10]
        self.bot.control({**cmd, "src": "wander"}, kind="wander_" + what, target=str(info.get("who", ""))[:8])
