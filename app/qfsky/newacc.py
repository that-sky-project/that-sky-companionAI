# -*- coding: utf-8 -*-
"""新号服务：地图解锁 / 全部动作 / 全部先祖 / 向导爱心（多选组合执行）+ 赛道奖励。

移植自 core/src/main/java/com/qingfeng/luoying/account/data/game/：
- MapUnlockApi.kt    → run_map_unlock：64 候选逐个 add_unlock {ack:false, name}
- ActionUnlockApi.kt → run_actions_unlock：emote_upgrade 动作 − 已拥有 → collect_collectible
- （新增）全部先祖     → run_spirits_all：非 questap 免费节点拓扑后逐个 purchase_spirit_shop_item
- GuideHeartApi.kt   → run_guide_hearts：questap 免费节点拓扑兑换（含 3 条状态 unlock 前置）
- SkyTaskApi.kt      → run_race：赛道奖励 claim_world_quest_reward（国服不带 x-sky-level-id）

全部为纯业务 HTTP（不需 UDP 在线；赛道领奖与每日任务同通道，执行前短转在线）。
v2 实测校准（159安卓 真跑 2543 节点）：
- HTTP 431 = 国服网关对写接口的限流（令牌桶型：连发越多后期越容易触发）→ 写请求间 0.5s 步进 +
  431 重试两次（1.2s/2.4s 退避），仍 431 记「限流」不计失败。
- result=="invalid_event_item" = 活动先祖节点仅在活动窗口内可领 → 记「活动期外」不计失败。
- result=="already"（含 200/400 体）= 已拥有 → 已有。
- result=="denied"（400 体）= 前置任务未完成（进度门，老账号大量存在）→ 记「前置未完成」不计失败。

v3 修复（2026-10-06 线上定案，起因「最新的季节的任务都不能完成」）：
- **旧子功能对 questap28+ 全盲**：`_status_unlock_names` 硬编码 `ap06unlock_` 标记，
  新命名（`quest_apNN_fetch_MM`）不含它 → 339 个免费向导节点里 132 个拿不到状态解锁。
  现在两代都认（旧代行为逐字节不变）。
- **新季节兑换被 denied 的真闸门 = 向导信任等级**：节点按 `tt`（tier）放行，而
  `/service/trust/api/v1/get {"npcs":["questap31"]}` 显示等级**不随经验自动涨**
  （账号 xp=800、tier 仍 1）→ 除首节点外全被拒；`tier_up` 一级后同一节点立刻
  `{"result":"ok"}`。tier_up 的 body 必须同时给顶层 `type`/`target` 与 `npcs` 列表。
- **新季节任务只有在「转在线」时才开得起来**：不转在线时 create 一律
  `400 Invalid Request`（当天每日任务同样如此）；转在线后
  `{"success": true, "quest_type": "scheduled_event_quest"}`，完成一个季节任务
  给向导加信任经验（`spirit_unlock_200` = +200），经验够了才能升级。
  所以向导爱心现在是「转在线 → 补季节任务 → 升信任等级 → 兑换节点」四步闭环。
"""
from __future__ import annotations

import re
import threading
import time

from .daily import claim_one_quest, ensure_online_3x
from .skyapi import SkyApiError

_ADD_UNLOCK = "/service/status/api/v1/add_unlock"
_PURCHASE = "/account/purchase_spirit_shop_item"
_COLLECT = "/account/collect_collectible"
_SHOPS = "/account/get_spirit_shops"
_SHOPS_PAGE = 1000
_CLAIM_RACE = "/service/quest/api/v1/claim_world_quest_reward"
_RACE_NAMES = ("sunset_flyrace", "sunset_race")  # 抓包验证：sunset_flyrace +15 / sunset_race +8 白蜡烛火
_RACE_SOFT = ("already", "cooldown", "claimed", "completed", "done", "collected", "no_progress")

_PACE = 0.5          # 写请求步进（防国服网关 431 限流；0.3s 时 700+ 连发后期仍会令牌桶耗尽）
_431_RETRY = 1       # 431 重试次数（159安卓 两轮实测：部分节点 431 为确定性拒绝（活动期后端），重试无用，只留一次快试）
_BODY_RESULT_RE = re.compile(r'"result"\s*:\s*"([^"]+)"')
_LEGACY_MARKER = "ap06unlock_"      # questap06…questap27 节点名固定前缀（不是季节序号）
_QUEST_NODE_PREFIX = "quest_"       # questap28+ 节点名：quest_apNN_fetch_MM
_STATUS_SUFFIXES = ("_claimFx", "_quest_done", "_quest_started")

# 向导信任（新季节闸门）── 2026-10-06 线上定案
_TRUST_GET = "/service/trust/api/v1/get"
_TRUST_TIER_DEFS = "/account/trust/get_trust_tier_defs"
_TRUST_TIER_UP = "/service/trust/api/v1/tier_up"
_TIER_UP_MAX = 8          # 单次运行最多升 8 级（tier 表最多 5 条/向导，防死循环）

# MapUnlockApi.kt CANDIDATES verbatim（64 项；unlock_key, 中文名）
MAP_CANDIDATES = (
    ("Realms_found", "七图总开关"),
    ("CandleSpace_found", "遇境"),
    ("Dawn_found", "晨岛"),
    ("Day_found", "云野"),
    ("Rain_found", "雨林"),
    ("Sunset_found", "霞谷"),
    ("Dusk_found", "暮土"),
    ("Night_found", "禁阁"),
    ("Storm_found", "伊甸"),
    ("Dawn_Shrine_found", "晨岛地图神龛"),
    ("Prairie_Village_Shrine_found", "云野地图神龛"),
    ("DayStatueForm", "云野神龛形态"),
    ("RainStatueForm", "雨林神龛形态"),
    ("SunsetStatueForm", "霞谷神龛形态"),
    ("DuskStatueForm", "暮土神龛形态"),
    ("NightStatueForm", "禁阁神龛形态"),
    ("StormStatueForm", "伊甸神龛形态"),
    ("Prairie_ButterflyFields_found", "云野：蝴蝶平原"),
    ("Prairie_Village_found", "云野：仙乡"),
    ("Prairie_Cave_found", "云野：幽光山洞"),
    ("Prairie_NestAndKeeper_found", "云野：云顶浮石"),
    ("Prairie_Island_found", "圣岛（云野衍生）"),
    ("DayHubCave_found", "云野：中央神坛"),
    ("RainForest_found", "雨林：荧光森林"),
    ("RainShelter_found", "雨林：秘密花园"),
    ("Rain_Cave_found", "雨林：地下溶洞"),
    ("RainMid_found", "雨林：密林遗迹"),
    ("RainEnd_found", "雨林神殿"),
    ("Sunset_Citadel_found", "霞谷：霞光城"),
    ("Sunset_FlyRace_found", "霞谷：飞行赛道"),
    ("SunsetRace_found", "霞谷：滑雪赛道"),
    ("SunsetColosseum_found", "霞谷：落日竞技场"),
    ("SunsetEnd2_found", "霞谷神殿"),
    ("DuskStart_found", "暮土起点"),
    ("DuskOasis_found", "暮土：失落方舟"),
    ("DuskGraveyard_found", "暮土：巨兽荒原"),
    ("Dusk_CrabField_found", "暮土：黑水港湾"),
    ("DuskMid_found", "暮土：远古战场"),
    ("DuskEnd_found", "暮土神殿"),
    ("NightArchive_found", "禁阁：档案阁"),
    ("Night2_found", "禁阁（拓展）"),
    ("TGCOffice_found", "秘密基地"),
    ("StormStart_found", "伊甸起点"),
    ("DawnCave_found", "晨岛：预言山谷"),
    ("Dawn_TrialsWater_found", "水之试炼"),
    ("Dawn_TrialsEarth_found", "土之试炼"),
    ("Dawn_TrialsAir_found", "风之试炼"),
    ("Dawn_TrialsFire_found", "火之试炼"),
    ("SunsetVillage_found", "霞谷：圆梦村"),
    ("Sunset_YetiPark_found", "霞谷：雪隐峰"),
    ("StormEnd_found", "伊甸之眼"),
    ("CandleSpaceEnd_found", "CandleSpaceEnd"),
    ("Credits_found", "Credits"),
    ("SunsetEnd_found", "霞谷：落日竞技场"),
    ("Rain_BaseCamp_found", "雨林：大树屋"),
    ("NightDesert_found", "星光沙漠"),
    ("Night_JarCave_found", "星光沙漠：罐子洞穴"),
    ("NightDesert_Beach_found", "星光沙漠：海滩"),
    ("NightDesert_Planets_found", "星光沙漠：星球"),
    ("Skyway_found", "风行网道"),
    ("Dusk_Triangle_found", "暮土：藏宝岛礁"),
    ("Sunset_Theater_found", "霞谷：圆梦村剧场"),
    ("SunsetVillage_MusicShop_found", "霞谷：音乐大厅"),
    ("Night_Shelter_found", "庇护所"),
)

MODE_META = (("map", "地图解锁"), ("actions", "全部动作"), ("spirits", "全部先祖"), ("guides", "向导爱心"))
MODE_KEYS = tuple(m for m, _ in MODE_META)


def _stopped(stop_flag) -> bool:
    return stop_flag is not None and stop_flag.is_set()


def _data(resp) -> dict:
    """响应取业务对象：顶层有 data 对象则进一层，否则用响应本身（对齐 Kotlin dataObj）。"""
    if isinstance(resp, dict):
        d = resp.get("data")
        if isinstance(d, dict):
            return d
        return resp
    return {}


def _paced_post(client, path, inner, stop_flag=None):
    """带步进与 431 退避重试的写请求；每成功响应后 sleep(_PACE)。非 431 异常直接上抛。"""
    delay = 0.0
    for attempt in range(_431_RETRY + 1):
        if _stopped(stop_flag):
            raise RuntimeError("stopped")
        try:
            if delay > 0:
                time.sleep(delay)
            resp = client.post(path, inner)
            time.sleep(_PACE)
            return resp
        except SkyApiError as e:
            if "HTTP 431" in str(e) and attempt < _431_RETRY:
                delay = 0.8 * (attempt + 1)
                continue
            raise
    raise RuntimeError("unreachable")


def _classify_strict(resp) -> str:
    """add_unlock / purchase 分类：""/ok→succ；already→dup；invalid_event_item→event；其它→fail。"""
    r = str(_data(resp).get("result") or "")
    if r in ("", "ok"):
        return "succ"
    if r == "already":
        return "dup"
    if r == "invalid_event_item":
        return "event"
    return "fail:%s" % (r or "空响应")


def _classify_loose(resp) -> str:
    """collect_collectible 分类：result/ack/status ∈ {already,duplicate,duplicated}→dup；其它→succ。"""
    d = _data(resp)
    raw = ""
    for k in ("result", "ack", "status"):
        raw = str(d.get(k) or "").strip()
        if raw:
            break
    if raw.lower() in ("already", "duplicate", "duplicated"):
        return "dup"
    return "succ"


def _classify_err(e: Exception) -> str:
    """写请求异常分类：431→throttled；错误体含 invalid_event_item→event；denied→gated；其它→fail。"""
    s = str(e)
    if "HTTP 431" in s:
        return "throttled"
    m = _BODY_RESULT_RE.search(s)
    if m and m.group(1) == "invalid_event_item":
        return "event"
    if m and m.group(1) == "denied":
        return "gated"
    return "fail:%s" % ((m.group(1) if m else s.split(":")[-1].strip()[:40]) or "HTTP错误")


def _tally(state: dict, cls: str) -> None:
    """按分类累加计数（state 含 succ/dup/event/throttled/gated/fail 键）。"""
    if cls == "succ":
        state["succ"] += 1
    elif cls == "dup":
        state["dup"] += 1
    elif cls == "event":
        state["event"] += 1
    elif cls == "throttled":
        state["throttled"] += 1
    elif cls == "gated":
        state["gated"] += 1
    else:
        state["fail"] += 1


def _summary(name: str, total: int, st: dict, stopped: bool) -> dict:
    done = st["succ"] + st["dup"]
    extra = ""
    if st.get("event"):
        extra += " · 活动期外 %d" % st["event"]
    if st.get("gated"):
        extra += " · 前置未完成 %d" % st["gated"]
    if st.get("throttled"):
        extra += " · 暂不可领 %d" % st["throttled"]
    if stopped:
        return {"ok": False, "summary": "%s %d/%d（已停止）" % (name, done, total),
                "succ": st["succ"], "dup": st["dup"], "fail": st["fail"],
                "event": st["event"], "throttled": st["throttled"],
                "gated": st["gated"], "total": total, "stopped": True}
    return {"ok": st["fail"] == 0 and done > 0,
            "summary": "%s %d/%d（新 %d/已有 %d%s）" % (name, done, total, st["succ"], st["dup"], extra),
            "succ": st["succ"], "dup": st["dup"], "fail": st["fail"],
            "event": st["event"], "throttled": st["throttled"],
            "gated": st["gated"], "total": total}


def _fresh_state() -> dict:
    return {"succ": 0, "dup": 0, "event": 0, "throttled": 0, "gated": 0, "fail": 0}


def _fetch_shops(client, log, stop_flag) -> list | None:
    """分页拉 /account/get_spirit_shops 全量节点行；首页失败 → None（无法区分空与失败）。"""
    rows: list = []
    total = 0
    version = 0
    offset = 0
    while True:
        if _stopped(stop_flag):
            break
        try:
            resp = client.post(_SHOPS, {"l": _SHOPS_PAGE, "o": offset, "v": version})
        except Exception:
            if offset == 0:
                return None
            break  # 后续页失败：保留已拉到的部分继续
        d = _data(resp)
        if total == 0:
            try:
                total = int(d.get("spirit_shops_total_count") or 0)
            except (TypeError, ValueError):
                total = 0
        if version == 0:
            try:
                version = int(d.get("spirit_shops_version") or 0)
            except (TypeError, ValueError):
                version = 0
        page = d.get("spirit_shops")
        if not isinstance(page, list):
            break
        rows.extend(r for r in page if isinstance(r, dict))
        offset += _SHOPS_PAGE
        if len(page) < _SHOPS_PAGE:
            break
        if total > 0 and offset >= total:
            break
    return rows


# ── 节点过滤 ──

def _action_name_of(row: dict) -> str | None:
    """ActionUnlockApi.actionNameOf：spirit_upgrade + emote_upgrade_* + spirit 非空非 questap*。"""
    nm = str(row.get("nm") or "").strip()
    typ = str(row.get("typ") or "").strip()
    spirit = str(row.get("spirit") or "").strip()
    if typ != "spirit_upgrade" or not nm.startswith("emote_upgrade_") or not spirit:
        return None
    if spirit.lower().startswith("questap"):
        return None
    return spirit


def _free_node(row: dict, guide_only: bool) -> dict | None:
    """免费节点（ap==false 且 cst==0）。guide_only=True 限定 questap 向导树；False 则排除 questap。"""
    try:
        nid = int(row.get("id") or 0)
    except (TypeError, ValueError):
        return None
    spirit = str(row.get("spirit") or "").strip()
    if nid == 0 or not spirit:
        return None
    low = spirit.lower()
    if guide_only:
        if not low.startswith("questap"):
            return None
    elif low.startswith("questap"):
        return None
    if row.get("ap"):
        return None
    try:
        cost = 0 if row.get("cst") is None else int(row.get("cst") or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost != 0:
        return None
    try:
        dep = int(row.get("dep") or 0)
    except (TypeError, ValueError):
        dep = 0
    return {"id": nid, "nm": str(row.get("nm") or "").strip(), "spirit": spirit, "dep": dep}


def _topo_sort(nodes: list) -> list:
    """GuideHeartApi.sortNodes：按 id 去重 → spirit 分组（组序按 (组大小, spirit 名)）→ 组内 dep 拓扑。"""
    dedup: dict = {}
    for n in nodes:
        if n["id"] != 0 and n["id"] not in dedup:
            dedup[n["id"]] = n
    by_spirit: dict = {}
    for n in dedup.values():
        by_spirit.setdefault(n["spirit"], []).append(n)
    ordered: list = []
    for spirit in sorted(by_spirit, key=lambda s: (len(by_spirit[s]), s)):
        group = by_spirit[spirit]
        id_set = {n["id"] for n in group}
        pending = list(group)
        done: set = set()
        while pending:
            progressed = False
            nxt = []
            for n in pending:
                dep = n["dep"]
                if dep == 0 or dep not in id_set or dep in done:
                    ordered.append(n)
                    done.add(n["id"])
                    progressed = True
                else:
                    nxt.append(n)
            if not progressed:  # 环/悬挂依赖：剩余原序追加，防死循环
                ordered.extend(nxt)
                break
            pending = nxt
    return ordered


def _season_quest_id(nm: str) -> str:
    """新代向导节点名 → 季节任务 def id：``quest_ap31_fetch_02`` → ``ap31_fetch_02``。

    旧代（questap06…questap27）节点名形如 ``ap06unlock_ap18_fetch_01``，返回空串。
    """
    text = str(nm or "").strip()
    if not text.startswith(_QUEST_NODE_PREFIX) or "_fetch_" not in text:
        return ""
    return text[len(_QUEST_NODE_PREFIX):]


def _status_unlock_names(nm: str) -> list:
    """节点的 3 条状态 unlock 名（``_claimFx`` / ``_quest_done`` / ``_quest_started``）。

    v3 修复（线上实测）：旧实现硬编码 ``ap06unlock_`` 标记，找不到就返回空——
    但 questap28 起服务端换成新命名（``quest_apNN_fetch_MM``，行 ``typ="quest"``、
    ``ta="zero_trust_quest"``），全量 339 个免费向导节点里有 132 个（全为新代 +
    ``heart_apNN_tier_*`` 一类）拿不到任何状态解锁。现在两代都认：

    - 旧代：行为与 v2 逐字节一致（``ap06unlock_`` 起截，非纯数字尾巴整名当基名）；
    - 新代：``quest_apNN_fetch_MM`` 整名当基名；
    - 其它（心/宝箱/传送/动作/关系能力）本来就没有任务状态名 → 仍返回空。
    """
    text = str(nm or "").strip()
    if not text:
        return []
    pos = text.find(_LEGACY_MARKER)
    if pos >= 0:
        base = text[pos:]
    elif text.startswith(_QUEST_NODE_PREFIX) and "_fetch_" in text:
        base = text
    else:
        return []
    idx = base.rfind("_fetch_")
    if idx < 0:
        return []
    raw = base[idx + len("_fetch_"):]
    if raw.isdigit():
        base = "%s_fetch_%s" % (base[:idx], raw)
    return [base + suffix for suffix in _STATUS_SUFFIXES]


def _season_quest_stats(client, quest_ids: list) -> dict:
    """``get_quest_defs`` → ``{def_id: trust_activity}``：季节任务完成要报的活动名。

    新代季节任务（``category="fetch_quest"``、``completion_type="activity"``）的
    create 响应里 ``stats`` 是空对象，拿不到 stat_type；实测 def 的
    ``trust_activity``（如 ``spirit_unlock_200``）就是 set_achievement_stats 要报的项。
    读不到定义时返回空 dict（任务腿降级为只 create + update）。
    """
    try:
        resp = client.post("/account/quest/get_quest_defs", {})
    except Exception:
        return {}
    data = _data(resp)
    defs = data.get("get_quest_defs") or data.get("quest_defs") or []
    want = set(quest_ids)
    out: dict = {}
    for d in defs if isinstance(defs, list) else []:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "").strip()
        if did in want:
            out[did] = str(d.get("trust_activity") or "").strip()
    return out


# ── 向导信任（新季节闸门；2026-10-06 线上定案） ──

def _trust_tier_defs(client) -> list:
    """``get_trust_tier_defs`` → 全表（按 ``tier_npc`` 过滤出向导的等级门槛）。"""
    try:
        d = _data(client.post(_TRUST_TIER_DEFS, {}, include_level_id=True))
    except Exception:
        return []
    return d.get("get_trust_tier_defs") or []


def _trust_max_tier(defs: list, npc: str, xp: int) -> int:
    """该向导按当前经验能到的最高等级（tier 表里 xp ≤ 账号经验的最大 tier）。"""
    best = 0
    for t in defs if isinstance(defs, list) else []:
        if not isinstance(t, dict) or str(t.get("tier_npc") or "") != npc:
            continue
        try:
            if int(t.get("xp") or 0) <= int(xp):
                best = max(best, int(t.get("tier") or 0))
        except (TypeError, ValueError):
            continue
    return best


def _trust_state(client, npc: str) -> dict:
    """``{xp, tier}``：``/service/trust/api/v1/get`` 的 npcs 里找该向导（没有则空）。"""
    try:
        d = _data(client.post(_TRUST_GET, {"npcs": [npc]}, include_level_id=True))
    except Exception:
        return {}
    arr = d.get("npcs") or []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict) or str(it.get("target_npc") or "") != npc:
            continue
        try:
            return {"xp": int(it.get("xp") or 0), "tier": int(it.get("tier") or 0)}
        except (TypeError, ValueError):
            return {}
    return {}


def _raise_trust_tier(client, npc: str, defs: list, log, stop_flag) -> dict:
    """把向导信任等级一路升到当前经验允许的最高级；返回 ``{tier, xp}``。

    ★ 新季节兑换的真闸门（线上实测 2026-10-06）：questap28+ 的节点按向导信任等级
    放行（``tt`` 即 tier），而等级不会跟着经验自动涨——账号 xp=800（4 个季节任务
    × ``spirit_unlock_200``）却还停在 tier 1，于是 ``purchase_spirit_shop_item``
    对除首节点外的一切回 400 ``{"result":"denied"}``。升一级后同一个节点立刻
    回 ``{"result":"ok"}``。

    tier_up 的 body 必须**同时**给顶层 ``type``/``target`` 和 ``npcs`` 列表，
    只给 ``npcs``（或只给顶层）服务端一律回 ``{"success": false, "reason":
    "no npcs found"}``。
    """
    st = _trust_state(client, npc)
    if not st:
        return {}
    want = _trust_max_tier(defs, npc, st["xp"])
    tier, ups = st["tier"], 0
    while tier < want and ups < _TIER_UP_MAX:
        if _stopped(stop_flag):
            break
        try:
            r = _data(client.post(_TRUST_TIER_UP, {
                "type": "npc", "target": npc,
                "npcs": [{"type": "npc", "target": npc}],
            }, include_level_id=True))
        except Exception as e:
            log("[向导爱心] 向导信任 %s 升级失败(%s)" % (npc, str(e)[:50]))
            break
        if not r.get("success"):
            log("[向导爱心] 向导信任 %s 未升级（%s）" % (npc, r.get("reason") or "服务端未同意"))
            break
        try:
            tier = int(r.get("tier") or tier + 1)
        except (TypeError, ValueError):
            tier += 1
        ups += 1
        time.sleep(_PACE)
    if ups:
        log("[向导爱心] 向导信任 %s：等级 %d → %d（经验 %d，上限 %d）"
            % (npc, st["tier"], tier, st["xp"], want))
    return {"tier": tier, "xp": st["xp"]}


# ── 四个子功能 ──

def run_map_unlock(client, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """地图解锁：64 候选逐个 add_unlock。成功判据 fail==0 且 done>0。"""
    total = len(MAP_CANDIDATES)
    log("[地图解锁] 共 %d 项" % total)
    st = _fresh_state()
    stopped = False
    for idx, (key, name) in enumerate(MAP_CANDIDATES, 1):
        if _stopped(stop_flag):
            stopped = True
            log("[地图解锁] 已停止（%d/%d）" % (idx - 1, total))
            break
        try:
            cls = _classify_strict(_paced_post(client, _ADD_UNLOCK, {"ack": False, "name": key}, stop_flag))
        except SkyApiError as e:
            cls = _classify_err(e)
        except RuntimeError:
            stopped = True
            log("[地图解锁] 已停止（%d/%d）" % (idx - 1, total))
            break
        except Exception as e:
            cls = "fail:%s" % str(e)[:60]
        _tally(st, cls)
        if cls == "succ":
            log("[地图解锁] (%d/%d) %s · 解锁成功" % (idx, total, name))
        elif cls == "throttled":
            log("[地图解锁] (%d/%d) %s · 暂不可领（跳过）" % (idx, total, name))
        elif cls.startswith("fail"):
            log("[地图解锁] (%d/%d) %s · 解锁失败(%s)" % (idx, total, name, cls[5:]))
    done = st["succ"] + st["dup"]
    log("[地图解锁] 完成 · 解锁 %d/%d（新 %d / 已有 %d）" % (done, total, st["succ"], st["dup"]))
    return _summary("地图解锁", total, st, stopped)


def _collectible_ids(client) -> set:
    """get_collectibles → 已拥有 id 集合（ActionUnlockApi.fetchOwnedCollectibleIds）。"""
    try:
        resp = client.get_collectibles()
    except Exception:
        return set()
    d = _data(resp)
    rows = d.get("collectibles")
    if not isinstance(rows, list):
        return set()
    out = set()
    for r in rows:
        if isinstance(r, dict):
            cid = str(r.get("id") or "").strip()
            if cid:
                out.add(cid)
    return out


def run_actions_unlock(client, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """全部动作：emote_upgrade 动作名 − 已拥有 → 逐个 collect_collectible {ack:"used", name}。"""
    rows = _fetch_shops(client, log, stop_flag)
    if rows is None:
        return {"ok": False, "summary": "拉取兑换节点失败", "total": 0}
    names = []
    seen = set()
    for row in rows:
        n = _action_name_of(row)
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    owned = _collectible_ids(client)
    pending = sorted(n for n in names if n not in owned)
    total = len(pending)
    if total == 0:
        log("[全部动作] 已全部解锁（%d 个动作）" % len(names))
        return {"ok": True, "summary": "动作已全解锁", "succ": 0, "dup": 0, "fail": 0,
                "event": 0, "throttled": 0, "total": 0, "all": True}
    log("[全部动作] 获取到 %d 个可解锁动作" % total)
    st = _fresh_state()
    stopped = False
    for idx, name in enumerate(pending, 1):
        if _stopped(stop_flag):
            stopped = True
            log("[全部动作] 已停止（%d/%d）" % (idx - 1, total))
            break
        try:
            cls = _classify_loose(_paced_post(client, _COLLECT, {"ack": "used", "name": name}, stop_flag))
        except SkyApiError as e:
            cls = _classify_err(e)
        except RuntimeError:
            stopped = True
            log("[全部动作] 已停止（%d/%d）" % (idx - 1, total))
            break
        except Exception:
            cls = "fail"
        _tally(st, cls)
        if cls == "succ":
            log("[全部动作] (%d/%d) %s · 解锁成功" % (idx, total, name))
        elif cls == "throttled":
            log("[全部动作] (%d/%d) %s · 暂不可领（跳过）" % (idx, total, name))
        elif cls.startswith("fail"):
            log("[全部动作] (%d/%d) %s · 解锁失败" % (idx, total, name))
    done = st["succ"] + st["dup"]
    log("[全部动作] 完成 · 解锁 %d/%d（新 %d / 已有 %d）" % (done, total, st["succ"], st["dup"]))
    return _summary("全部动作", total, st, stopped)


def run_spirits_all(client, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """全部先祖：非 questap 免费节点（ap==false、cst==0）拓扑后逐个 purchase（零消耗）。"""
    rows = _fetch_shops(client, log, stop_flag)
    if rows is None:
        return {"ok": False, "summary": "拉取兑换节点失败", "total": 0}
    nodes = [n for n in (_free_node(r, guide_only=False) for r in rows) if n]
    nodes = _topo_sort(nodes)
    total = len(nodes)
    if total == 0:
        log("[全部先祖] 没有可领取的免费先祖节点")
        return {"ok": True, "summary": "无免费先祖节点", "succ": 0, "dup": 0, "fail": 0,
                "event": 0, "throttled": 0, "total": 0, "all": True}
    log("[全部先祖] 获取到 %d 个免费先祖节点" % total)
    st = _fresh_state()
    stopped = False
    for idx, node in enumerate(nodes, 1):
        if _stopped(stop_flag):
            stopped = True
            log("[全部先祖] 已停止（%d/%d）" % (idx - 1, total))
            break
        try:
            cls = _classify_strict(_paced_post(client, _PURCHASE, {"unlock_id": node["id"]}, stop_flag))
        except SkyApiError as e:
            cls = _classify_err(e)
        except RuntimeError:
            stopped = True
            log("[全部先祖] 已停止（%d/%d）" % (idx - 1, total))
            break
        except Exception as e:
            cls = "fail:%s" % str(e)[:60]
        _tally(st, cls)
        if cls == "succ":
            log("[全部先祖] (%d/%d) %s · 领取成功" % (idx, total, node["spirit"]))
        elif cls == "throttled":
            log("[全部先祖] (%d/%d) %s(%d) · 暂不可领（跳过）" % (idx, total, node["spirit"], node["id"]))
        elif cls.startswith("fail"):
            log("[全部先祖] (%d/%d) %s(%d) · 领取失败(%s)" % (idx, total, node["spirit"], node["id"], cls[5:]))
    done = st["succ"] + st["dup"]
    log("[全部先祖] 完成 · 领取 %d/%d（新 %d / 已有 %d）" % (done, total, st["succ"], st["dup"]))
    return _summary("全部先祖", total, st, stopped)


def run_guide_hearts(client, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """向导爱心：questap 免费节点拓扑兑换（旧季节直接兑换，新季节先过信任闸门）。

    三段（新代节点存在时才有后两段的额外请求）：

    1. **季节任务腿**（``quest_apNN_fetch_MM``，questap28 起）：转在线 → create →
       set_achievement_stats(trust_activity) → update_quest。线上实测：不转在线时
       create 一律 ``400 Invalid Request``（连当天每日任务也一样），转在线后
       ``{"success": true, "quest_type": "scheduled_event_quest"}``；每完成一个
       季节任务给向导加信任经验（``spirit_unlock_200`` = +200）。
    2. **信任升级腿**：``get`` 读向导 xp/tier → 按 ``get_trust_tier_defs`` 把等级
       升到经验允许的最高级。**这是新季节兑换被 ``denied`` 的真原因**：等级不随
       经验自动涨，账号 xp 早就够了却停在 tier 1，于是除首节点外全被拒。
    3. **兑换腿**：每节点补 3 条状态 unlock（旧代 ``ap06unlock_*`` + 新代
       ``quest_ap*`` 都出）→ ``purchase_spirit_shop_item``；``denied`` 记前置未完成。
    """
    rows = _fetch_shops(client, log, stop_flag)
    if rows is None:
        return {"ok": False, "summary": "拉取向导节点失败", "total": 0}
    nodes = [n for n in (_free_node(r, guide_only=True) for r in rows) if n]
    nodes = _topo_sort(nodes)
    total = len(nodes)
    if total == 0:
        log("[向导爱心] 没有可兑换的免费向导节点")
        return {"ok": True, "summary": "无免费向导节点", "succ": 0, "dup": 0, "fail": 0,
                "event": 0, "throttled": 0, "total": 0, "all": True}
    log("[向导爱心] 获取到 %d 个免费向导节点" % total)

    quest_ids: list = []
    spirits: list = []
    for n in nodes:
        qid = _season_quest_id(n["nm"])
        if not qid:
            continue
        if qid not in quest_ids:
            quest_ids.append(qid)
        if n["spirit"] and n["spirit"] not in spirits:
            spirits.append(n["spirit"])

    quest_ok = 0
    tiers: dict = {}
    if quest_ids:
        log("[向导爱心] 新代季节任务 %d 项（questap28+）：先转在线补任务（任务给向导加信任经验）"
            % len(quest_ids))
        if ensure_online_3x(client, log):
            quest_stats = _season_quest_stats(client, quest_ids)
            if not quest_stats:
                log("[向导爱心] 未读到季节任务定义：任务腿降级（只 create + update）")
            for qid in quest_ids:
                if _stopped(stop_flag):
                    break
                try:
                    if claim_one_quest(client, qid, quest_stats.get(qid, ""),
                                       "[向导爱心] 季节任务 %s" % qid, log, stop_flag):
                        quest_ok += 1
                except Exception as e:
                    log("[向导爱心] 季节任务 %s · 异常(%s)" % (qid, str(e)[:50]))
        else:
            log("[向导爱心] 转在线失败：跳过季节任务腿（新季节节点仍会尝试信任升级+兑换）")
        for sp in spirits:
            if _stopped(stop_flag):
                break
            st = _raise_trust_tier(client, sp, _trust_tier_defs(client), log, stop_flag)
            if st:
                tiers[sp] = st

    status_done: set = set()
    st = _fresh_state()
    stopped = False
    for idx, node in enumerate(nodes, 1):
        if _stopped(stop_flag):
            stopped = True
            log("[向导爱心] 已停止（%d/%d）" % (idx - 1, total))
            break
        for status_name in _status_unlock_names(node["nm"]):
            if status_name in status_done:
                continue
            status_done.add(status_name)
            try:
                _paced_post(client, _ADD_UNLOCK, {"ack": False, "name": status_name}, stop_flag)
            except Exception:
                pass  # 状态 unlock 失败静默，不影响主流程
        try:
            cls = _classify_strict(_paced_post(client, _PURCHASE, {"unlock_id": node["id"]}, stop_flag))
        except SkyApiError as e:
            cls = _classify_err(e)
        except RuntimeError:
            stopped = True
            log("[向导爱心] 已停止（%d/%d）" % (idx - 1, total))
            break
        except Exception as e:
            cls = "fail:%s" % str(e)[:60]
        _tally(st, cls)
        if cls == "succ":
            log("[向导爱心] (%d/%d) %s · 兑换成功" % (idx, total, node["spirit"]))
        elif cls == "throttled":
            log("[向导爱心] (%d/%d) %s(%d) · 暂不可领（跳过）" % (idx, total, node["spirit"], node["id"]))
        elif cls == "gated" and _season_quest_id(node["nm"]):
            log("[向导爱心] (%d/%d) %s(%d) · 前置未完成（新季节节点：向导信任等级还不够）"
                % (idx, total, node["spirit"], node["id"]))
        elif cls.startswith("fail"):
            log("[向导爱心] (%d/%d) %s(%d) · 兑换失败(%s)" % (idx, total, node["spirit"], node["id"], cls[5:]))
    done = st["succ"] + st["dup"]
    log("[向导爱心] 完成 · 兑换 %d/%d（新 %d / 已有 %d）" % (done, total, st["succ"], st["dup"]))
    res = _summary("向导爱心", total, st, stopped)
    if quest_ids:
        log("[向导爱心] 季节任务 %d/%d 已补" % (quest_ok, len(quest_ids)))
        res["quests_done"] = quest_ok
        res["quests_total"] = len(quest_ids)
        res["summary"] += " · 季节任务 %d/%d" % (quest_ok, len(quest_ids))
    if tiers:
        res["trust"] = tiers
        res["summary"] += " · 向导信任 " + "/".join(
            "%s L%d" % (sp, t.get("tier", 0)) for sp, t in tiers.items())
    return res


# ── 组合入口 / 赛道 ──

_RUNNERS = {"map": run_map_unlock, "actions": run_actions_unlock,
            "spirits": run_spirits_all, "guides": run_guide_hearts}


def run_newacc(client, modes, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """新号服务组合执行：按固定顺序跑选中项，合并 summary（单项失败不阻断后续）。"""
    names = {m: zh for m, zh in MODE_META}
    parts: dict = {}
    texts: list = []
    for mode in MODE_KEYS:  # 固定执行顺序：地图 → 动作 → 先祖 → 向导
        if mode not in modes:
            continue
        zh = names[mode]
        log("[新号服务] ▶ %s" % zh)
        try:
            part = _RUNNERS[mode](client, log=log, stop_flag=stop_flag)
        except Exception as e:
            part = {"ok": False, "summary": "%s失败：%s" % (zh, str(e)[:80]), "total": 0}
        parts[mode] = part
        texts.append(part.get("summary") or zh)
        if _stopped(stop_flag):
            break
    ok = all(bool(p.get("ok")) for p in parts.values()) if parts else False
    summary = " · ".join(texts) if texts else "未选择任何项目"
    return {"ok": ok, "summary": summary, "parts": parts}


def run_race(client, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """赛道奖励：转在线后循环任务池领奖（国服 /service/quest/api/v1/claim_world_quest_reward，
    不带 x-sky-level-id；result==ok 成功；unknown_quest 失败；软失败码 → 今日已领）。"""
    log("[赛道奖励] 短转在线…")
    if not ensure_online_3x(client, log):
        return {"ok": False, "summary": "转在线失败"}
    total = len(_RACE_NAMES)
    log("[赛道奖励] 共 %d 项" % total)
    succ = already = fail = 0
    stopped = False
    for idx, name in enumerate(_RACE_NAMES, 1):
        if _stopped(stop_flag):
            stopped = True
            log("[赛道奖励] 已停止（%d/%d）" % (idx - 1, total))
            break
        try:
            resp = client.post(_CLAIM_RACE, {"name": name}, include_level_id=False)
        except Exception as e:
            fail += 1
            log("[赛道奖励] (%d/%d) %s · 领取失败(%s)" % (idx, total, name, str(e)[:60]))
            continue
        d = _data(resp)
        result = str(d.get("result") or "").lower()
        if result == "ok":
            succ += 1
            try:
                bonus = int(d.get("bonus_wax_amount") or 0)
            except (TypeError, ValueError):
                bonus = 0
            log("[赛道奖励] (%d/%d) %s · 领取成功%s" % (idx, total, name, " +%d 白蜡烛火" % bonus if bonus > 0 else ""))
        elif result == "unknown_quest":
            fail += 1
            log("[赛道奖励] (%d/%d) %s · 领取失败(unknown_quest)" % (idx, total, name))
        elif any(k in result for k in _RACE_SOFT):
            already += 1
            log("[赛道奖励] (%d/%d) %s · 今日已领" % (idx, total, name))
        else:
            fail += 1
            log("[赛道奖励] (%d/%d) %s · 领取失败(%s)" % (idx, total, name, result or "空响应"))
    done = succ + already
    if stopped:
        return {"ok": False, "summary": "赛道奖励 %d/%d（已停止）" % (done, total), "stopped": True}
    log("[赛道奖励] 完成 · 领取 %d/%d（新 %d / 已领 %d）" % (done, total, succ, already))
    return {"ok": fail == 0 and done > 0, "summary": "赛道奖励 %d/%d（新 %d/已领 %d）" % (done, total, succ, already),
            "succ": succ, "already": already, "fail": fail, "total": total}
