# -*- coding: utf-8 -*-
"""精灵任务（spirit quests / 联动寻宝）· **只完成、不领奖**。

★ 2026-10-03 现场定案：清樱那条「活动中心 H5 桥」路在国服现网关**不存在**。
------------------------------------------------------------------
已用状态码分类法在生产机上钉死（对照 KB 10 篇的体检方法论：假路径 404 / 路由活但
body 不合规 400-422；`docs/0.16.5_412659知识库/10_业务层迁移与UDP双段链路.md`）：

  · `/account/jingling/process_web_request`      → 404   ← 4 种请求体形状 × 2 种头部全 404
  · `/account/jingling/set_task`                 → 404
  · `/account/jingling/claim_jingling_mission_reward` → 404
  · `/account/get_activity_center_data`          → 404
  · `/account/activity_center/set_return_buff_mission` → 404
  · `/account/event_statistics`                  → 404
  对照（同一台机位、同一次运行）：
  · `/account/get_currency` `/account/get_vars` `/account/gameconfig/get_config` → 200
  · `/account/get_season_quests` → 200（今天 4 个每日任务）
  · `/account/get_event_schedule` `/account/get_event_currency_defs` → 200
  · `/account/quest/get_quest_defs` → 200（537 条任务定义）
  · `/service/quest/api/v1/get_player_quests` `/get_world_quests` `/get_quest_schedule` → 200
  · `/service/quest/api/v1/update_quest` `/create` → 400（路由活着，body 不合规）
  · `/service/status/api/v1/set_achievement_stats`（空 body）→ 200（回全量成就统计）
  · `/account/jingling/*` 是**那一族** `/account/` 路由被摘掉（同 `/account/claim_quest_reward`），
    不是 body 写错 —— 4 种形状全 404 就是证据。

★ 所以「精灵任务」走**活着的任务服务**，与项目 `daily.py` 同一套闭环：
------------------------------------------------------------------
  1. `get_quest_defs` 里有一整族精灵/联动任务：
     · `spirit_*`          106 条（spirit_camping1..3 / spirit_navigator1..3 /
       spirit_selfie_crabvoice…，landmark 多为 `Prairie_WildLifePark` = MC 联动地点）
     · `dotreasure_*`      草原/孤岛/村庄/野生动物园/暮土/夜漠/雨林/日落 寻宝（`do_treasure_count`）
     · `treasure_*_scout/_scavenger`  category=event、scheduled_event=`global_treasure`
     完成条件都是客户端上报的 achievement stat，target 多为 1（spirit_ugc=30、spirit_anniversary=60）。
  2. 完成闭环（daily.py 已验证的同一三段）：
     `create(quest_name=def_id)` → `set_achievement_stats(type=<stat>, value=target)`
     → `update_quest(status="succeeded", collect=False)`。
  3. **不领奖**：`collect=False`，且绝不碰 `claim_jingling_mission_reward`（路由已死，也不该用）。
     用户 2026-10-03 明确：精灵任务只完成、不领奖。

★ 与「献祭」的关系（用户最关心的那条）
------------------------------------------------------------------
537 条任务定义里，只有 **`visit_stormy_event`** 一条与风暴/伊甸沾边，
完成统计是 `visit_stormy_event_count`（去一趟风暴区域，target 1）——
**它不校验真的献祭了几根蜡烛**。所以「最后一个任务要献祭」很可能是「去风暴走一趟」，
而不是真跑伊甸；真伪要在管理员机位上做**单点实跑**才能定论（见 `run_spirit` 的 limit 参数）。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import daily
from .skyapi import SkyApiError

# 活着的任务服务（三段闭环 + 两个只读辅助）
DEFS_PATH = "/account/quest/get_quest_defs"
CREATE_PATH = "/service/quest/api/v1/create"
SET_STAT_PATH = "/service/status/api/v1/set_achievement_stats"
UPDATE_PATH = "/service/quest/api/v1/update_quest"
CHECK_PATH = "/service/quest/api/v1/check_for_previous_completion"
HEALTH_PATH = "/account/get_vars"               # 动笔前的只读体检（get_currency 国服会单独 422，不能用它）
PQ_PATH = daily.QUEST_LIST_PATH                       # 活跃任务列表：create 不回 quest_id 时从这里取
SCHEDULE_PATH = "/service/quest/api/v1/get_quest_schedule"

CLAIM_PATH = "/account/jingling/claim_jingling_mission_reward"   # ★ 永不调用（路由已 404）

# 候选任务族：精灵任务 + 联动寻宝（前缀匹配任务定义 id）
PREFIXES = ("spirit_", "dotreasure_", "treasure_")
# 完成态：服务端 update_quest 的 status 文案（`collected` 表示连奖一起领了）
DONE_STATUS = ("succeeded", "collected")

SET_PAUSE = 0.6         # 单任务写入间隔：给网关留余量（2026-10-03 教训，见文件头 ★）
MAX_QUESTS = 120        # 单次跑动任务数硬上限（防跑飞）
DEFAULT_LIMIT = 3       # 未显式指定上限时的**安全写入条数**（要全量必须 all=True）
UNAVAIL_STREAK = 3      # 连续这么多条「今日未排期」就收手（否则会把 120 条候选全部空打一遍）

# 网关级拒绝：撞上这些状态说明是会话/限流/端点问题，不是单个任务的问题 →
# 立刻停手，绝不把整批写请求继续砸出去（每条 422 在 skyapi 里还会重试，会雪上加霜）。
GATE_STATUS = (401, 403, 418, 422, 423, 429, 500, 502, 503, 504)

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
LAST_RUN_FILE = os.path.join(_DATA_DIR, "jingling_last.json")


def _post(client, path: str, body: dict, include_level_id: bool = False):
    """业务口径：网络/解码异常吞成 None（与 daily 同口径，会话失效仍上抛）。"""
    return daily._post_or_none(client, path, body, include_level_id)


def _post_why(client, path: str, body: dict, include_level_id: bool = False) -> tuple[dict | None, str]:
    """同 `_post`，但**把失败原因带回来**。

    2026-10-03 的教训：原先只写「领取失败(create)」，事后完全无法判断到底是被
    网关拒（422/418）、会话失效（401）还是 body 不合规（400），只能靠猜。
    """
    try:
        return client.post(path, body, include_level_id=include_level_id), ""
    except SkyApiError as e:
        return None, "HTTP %s" % (e.status or "?")
    except Exception as e:
        return None, type(e).__name__


def _gate(why: str) -> bool:
    """这个失败原因是不是「网关级」（会话/限流/端点），撞上就该收手。"""
    if not why.startswith("HTTP "):
        return False
    try:
        return int(why.split()[1]) in GATE_STATUS
    except (ValueError, IndexError):
        return False


def health(client, log) -> str:
    """动笔前的只读体检：返回 "" 或 "HTTP xxx"。

    ★ 只作**参考**，不作硬闸门 —— 2026-10-03 实测：同一时刻同一机位
      `/account/get_currency` 会 422/401，而 `/account/get_vars` 200、任务服务也照常。
      拿单个端点当生死判断会误杀整批跑动，这里只用它决定「要不要把上限压到 1 条」。
    """
    resp, why = _post_why(client, HEALTH_PATH, {}, include_level_id=True)
    if resp is not None:
        log("[精灵任务] 体检通过（%s 200）" % HEALTH_PATH)
        return ""
    log("[精灵任务] 体检提示：%s → %s" % (HEALTH_PATH, why or "无响应"))
    return why


def _data(resp) -> dict:
    return daily._data_obj(resp or {})


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── 任务定义 ────────────────────────────────────────────────────────────────

def fetch_defs(client) -> list[dict]:
    resp = _post(client, DEFS_PATH, {}, include_level_id=True)
    if not isinstance(resp, dict):
        return []
    rows = resp.get("get_quest_defs") or resp.get("quest_defs") or []
    return [r for r in rows if isinstance(r, dict)]


def is_target_def(d: dict) -> bool:
    """是不是精灵/联动任务（按 id 前缀；不是每日赛季任务那一套）。"""
    i = str(d.get("id") or "")
    return bool(i) and i.startswith(PREFIXES)


def def_stat(d: dict) -> tuple[str, float | None]:
    """任务定义的 (完成统计键, 目标值)。字段缺失时返回 ("", None)。"""
    types = d.get("completion_stats_types") or []
    vals = d.get("completion_stats_values") or []
    stat = str(types[0]).strip() if types and isinstance(types[0], (str, int)) else ""
    tgt = _num(vals[0]) if vals else None
    return stat, tgt


def select_defs(defs: list[dict], only: str = "", include_event: bool = True) -> list[dict]:
    """挑候选：前缀命中 + （only 非空时）id 子串命中 + （可选）排除 category=event。

    排序刻意把「联动寻宝 / 草原动物乐园 / 拍照」这类本次联动相关项排前面：
    单点实跑（limit=1）时先试的应该是最像「精灵任务」的那一条。
    """
    out = []
    for d in defs:
        if not is_target_def(d):
            continue
        if not include_event and str(d.get("category") or "") == "event":
            continue
        if only and only.lower() not in str(d.get("id")).lower():
            continue
        out.append(d)

    def key(d):
        i = str(d.get("id"))
        land = str(d.get("landmark") or "")
        score = 0
        if "wildlifepark" in land.lower() or "wildlifepark" in i.lower():
            score -= 4          # MC 联动地点
        if i.startswith("dotreasure_"):
            score -= 3          # 联动寻宝
        if "selfie" in i:
            score -= 2          # 精灵拍照（联动地点）
        if i.startswith("spirit_"):
            score -= 1
        return (score, i)

    return sorted(out, key=key)


# ── 完成闭环 ────────────────────────────────────────────────────────────────

def completed_today(client, def_id: str) -> bool:
    """只读预查：今天是否已完成（服务端按 quest_name 回 completed_today）。"""
    return def_id in daily.fetch_completed_today(client, [def_id])


def _quest_id_of(client, def_id: str) -> str:
    """从活跃任务列表里取该任务定义的实例 quest_id（实现收在 daily.find_player_quest，
    与每日任务共用一份：那里的 docstring 记了 create 去重回执的实测结论）。

    这里保留「任何异常都吞掉」的宽松语义（精灵任务跑动不该因为一次列表查询失败而中断）。
    """
    try:
        inst, _bucket = daily.find_player_quest(client, def_id)
    except Exception:
        return ""
    return str((inst or {}).get("quest_id") or "")


def complete_one(client, def_id: str, stat_hint: str = "", target_hint=None,
                 log=lambda m: None, collect: bool = False, stop_flag=None) -> tuple[str, str]:
    """单个精灵任务闭环：create → set_achievement_stats → update_quest(collect=False)。

    返回 ("done"|"skip"|"unavail"|"fail", 原因)。**绝不领奖**（collect 默认 False）。
    `unavail` = 服务端此刻不给开这单（不在今日排期/事件窗口内）——这不是错误，
    而是"今天没这条任务"，上层据此提前收手。
    """
    if stop_flag is not None and stop_flag.is_set():
        return "fail", "已停止"
    if completed_today(client, def_id):
        log("   · %s 今日已完成，跳过" % def_id)
        return "skip", ""

    create, why = _post_why(client, CREATE_PATH, {
        "quest_name": def_id, "quest_giver": "", "expiry_time": 0,
        "is_scheduled_quest": False,
    })
    if create is None:
        if completed_today(client, def_id):
            log("   · %s 今日已完成（create 回执丢失）" % def_id)
            return "skip", ""
        log("   · %s 领取失败（create %s）" % (def_id, why or "无响应"))
        return "fail", why

    cd = _data(create)
    quest_id = str(cd.get("quest_id") or "").strip()
    if not quest_id:
        if cd.get("is_today_quest") is False:
            log("   · %s 今日未排期（服务端 is_today_quest=false，本窗口不给开这单）" % def_id)
            return "unavail", "not-today"
        # {"result": "ok"} 是服务端去重回执（本窗口内已被创建/探测过）→ 去活跃列表找实例
        quest_id = _quest_id_of(client, def_id)
        if not quest_id:
            log("   · %s 拿不到实例（create 未回 quest_id，活跃列表里也没有）" % def_id)
            return "unavail", "no-quest-id"

    stat = daily.resolve_stat_type(cd, stat_hint)
    target = daily.extract_target_value(cd, stat) if stat else None
    if target in (None, 0) and target_hint:
        target = int(target_hint) if float(target_hint).is_integer() else target_hint
    if not stat or target is None:
        log("   · %s 跳过（拿不到完成统计键/目标值：stat=%r target=%r）" % (def_id, stat, target))
        return "skip", ""

    set_resp, why = _post_why(client, SET_STAT_PATH,
                              {"achievement_stats": [{"type": stat, "value": target}]})
    if set_resp is None:
        log("   · %s 失败（set_achievement_stats %s=%s → %s）" % (def_id, stat, target, why or "无响应"))
        return "fail", why

    upd, why = _post_why(client, UPDATE_PATH,
                         {"quest_id": quest_id, "status": "succeeded", "collect": bool(collect)})
    if upd is None:
        if completed_today(client, def_id):
            log("   · %s 已完成（update 回执丢失）" % def_id)
            return "done", ""
        log("   · %s 失败（update_quest → %s）" % (def_id, why or "无响应"))
        return "fail", why
    status = str(_data(upd).get("status") or "")
    if status in DONE_STATUS:
        log("   · %s 完成（%s=%s，status=%s%s）"
            % (def_id, stat, target, status, "，已领奖" if collect else "，未领奖"))
        return "done", ""
    log("   · %s 失败（status=%s）" % (def_id, status or "?"))
    return "fail", "status=%s" % (status or "?")


# ── 主流程 ──────────────────────────────────────────────────────────────────

def run_spirit(client, log=lambda m: None, stop_flag: threading.Event | None = None,
               preview: bool = False, limit: int = 0, only: str = "",
               all_in: bool = False, acc_id: str = "") -> dict:
    """精灵任务：转在线 → 事前体检 → 读任务定义 → 挑候选 → create/set_stat/update（不领奖）。

    preview=True：只读（列候选 + 今日完成态），一个字都不写。
    limit>0：最多写 limit 条；**不传且 all_in=False 时按 DEFAULT_LIMIT（3）小步跑**。
    all_in=True：才允许全量候选（120 条硬上限）。
    only：id 子串过滤（例如 only="camping" 只跑精灵露营那 3 条）。

    ★ 2026-10-03 事故对策（用户实测：120 条全失败 + 机位被网关顶成 warn）：
      ① 默认只写 3 条；② 动笔前先只读体检；③ 第一条就撞上网关级拒绝（401/418/422/…）
      立刻收手，剩下的候选一个都不发。
    """
    if not all_in and (not limit or limit <= 0):
        limit = DEFAULT_LIMIT
    mode = ("预览：只读不写" if preview else
            ("全量实跑（不领奖）" if all_in and not limit else "实跑最多 %d 条（不领奖）" % limit))
    log("[精灵任务] 开始（%s）" % mode)
    if not daily.ensure_online_3x(client, log):
        return {"ok": False, "summary": "转在线失败", "succ": 0, "fail": 0, "total": 0}

    probed = health(client, log)
    if _gate(probed):
        log("[精灵任务] 网关级状态（%s）→ 本次写入上限压到 1 条：只试一条，撞上就收手" % probed)
        limit = 1

    defs = fetch_defs(client)
    if not defs:
        return {"ok": False, "summary": "读不到任务定义（get_quest_defs 异常）",
                "succ": 0, "fail": 0, "total": 0}
    cands = select_defs(defs, only=only)
    if not cands:
        return {"ok": True, "summary": "没有匹配的精灵/联动任务（only=%r）" % only,
                "succ": 0, "fail": 0, "total": 0}
    if len(cands) > MAX_QUESTS:
        log("[精灵任务] 候选 %d 条，按硬上限截到 %d 条" % (len(cands), MAX_QUESTS))
        cands = cands[:MAX_QUESTS]

    log("[精灵任务] 任务定义 %d 条 → 候选 %d 条（精灵 %d / 联动寻宝 %d）"
        % (len(defs), len(cands),
           sum(1 for d in cands if str(d.get("id")).startswith("spirit_")),
           sum(1 for d in cands if not str(d.get("id")).startswith("spirit_"))))

    done = skip = fail = unavail = 0
    rows: list[dict] = []
    stopped = False
    gated = ""
    for d in cands:
        if stop_flag is not None and stop_flag.is_set():
            stopped = True
            break
        if not preview and limit and done + fail + unavail >= limit:
            log("[精灵任务] 达到本次写入上限 %d，停止" % limit)
            stopped = True
            break
        def_id = str(d.get("id"))
        stat, tgt = def_stat(d)
        if preview:
            state = "今日已完成" if completed_today(client, def_id) else "待完成"
            log("   · %-34s stat=%-34s target=%-6s %s" % (def_id, stat or "?", tgt, state))
            rows.append({"id": def_id, "stat": stat, "target": tgt, "state": state})
            continue
        log("   · %s（stat=%s target=%s landmark=%s）"
            % (def_id, stat or "?", tgt, d.get("landmark") or "-"))
        r, why = complete_one(client, def_id, stat_hint=stat, target_hint=tgt, log=log,
                              collect=False, stop_flag=stop_flag)
        if r == "done":
            done += 1
        elif r == "skip":
            skip += 1
        elif r == "unavail":
            unavail += 1
        else:
            fail += 1
        rows.append({"id": def_id, "result": r, "why": why})
        # 网关级拒绝且一条都没成 → 立刻收手，别把剩下的候选也砸出去
        if r == "fail" and done == 0 and _gate(why):
            gated = why
            log("[精灵任务] 第一条就撞上网关级拒绝（%s）→ 立即停止，剩余候选未发出（保护账号）" % why)
            stopped = True
            break
        # 连续「今日未排期」= 服务端今天不给开这批任务（2026-10-03 实测：精灵族 126 条
        # 全是 daily_schedule，只有被排进当天档期才给开）→ 再打下去纯属空转，停。
        if r == "unavail" and done == 0 and unavail >= UNAVAIL_STREAK:
            log("[精灵任务] 连续 %d 条都「今日未排期」→ 本批停止："
                "服务端今天没有把这批任务排进档期，再试也是空打" % unavail)
            stopped = True
            break
        time.sleep(SET_PAUSE)

    total = len(rows) if preview else done + skip + fail + unavail
    if preview:
        pending = sum(1 for r in rows if r.get("state") == "待完成")
        summary = "候选 %d 条 · 待完成 %d · 今日已完成 %d" % (len(rows), pending, len(rows) - pending)
    else:
        summary = "候选 %d 条 · 完成 %d · 跳过 %d · 未排期 %d · 失败 %d" % (total, done, skip, unavail, fail)
        if gated:
            summary += " · 网关级拒绝（%s）已提前收手" % gated
    if stopped:
        summary += " · 已停止"
    log("[精灵任务] %s" % summary)
    _save_last({"at": int(time.time()), "acc": acc_id, "summary": summary, "preview": preview,
                "rows": rows[:60]})
    return {"ok": (done > 0) or (fail == 0 and unavail == 0),
            "summary": summary, "succ": done, "fail": fail, "total": total,
            "skip": skip, "unavail": unavail, "preview": preview, "stopped": stopped,
            "gate": gated, "health": probed, "rows": rows[:60]}


def _save_last(rec: dict) -> None:
    """把最近一次跑动的清单落 data/（前台任务面板之外的第二份证据，0600）。"""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = LAST_RUN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, LAST_RUN_FILE)
    except Exception as e:
        print("[jingling] 最近跑动落盘失败（不影响结果）：%r" % (e,), flush=True)
