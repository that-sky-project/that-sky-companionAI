# -*- coding: utf-8 -*-
"""国服每日任务自动领取。移植 SkyTaskApi.runDailyQuests（2026-05-16 抓包定版）。

业务序列（全部走 skyPost RC4 业务 HTTP）：
  get_season_quests 拉清单 → check_for_previous_completion 预查已完成 →
  对每个 quest：create 拿 quest_id/target → set_achievement_stats 推进度 → update_quest 领奖。

「转在线（UDP 进房）」由编排层先行保证（CnOnlineSession.ensureOnline 语义）：
  get_user_feeds 2xx 即在线，且 UDP 断开后服务端保留在线态，故转在线窗口内跑完 HTTP 即可。
会话失效（401/403/423）向上抛出，由任务层续期重试（对齐 TaskRunController.runWithSessionReplay）。
"""
from __future__ import annotations

import threading
import time

from . import seasonhome
from . import session as cnsession
from .skyapi import SkyApiError

QUEST_MAX_ATTEMPTS = 2
CREDENTIAL_STATUSES = (401, 403, 423)

# 任务实例列表：create 不回 quest_id 时，实例只能从这里按 quest_name 取（见 find_player_quest）
QUEST_LIST_PATH = "/service/quest/api/v1/get_player_quests"
# 桶名即实例状态；active 可继续推进，这两个桶说明「其实已经完成」——不能当失败
QUEST_DONE_BUCKETS = ("succeeded", "collected")
QUEST_BUCKETS = ("active",) + QUEST_DONE_BUCKETS + ("failed", "canceled", "suspended", "inactive")


def _credential_error(e: Exception) -> bool:
    return isinstance(e, SkyApiError) and e.status in CREDENTIAL_STATUSES


def _post_or_none(client, path: str, body: dict, include_level_id: bool) -> dict | None:
    """skyPost 包裹：网络/解码异常吞成 None（业务按失败处理），会话失效上抛。"""
    try:
        return client.post(path, body, include_level_id=include_level_id)
    except SkyApiError as e:
        if e.status in CREDENTIAL_STATUSES:
            raise
        return None
    except Exception:
        return None


def _data_obj(resp: dict) -> dict:
    """响应取业务对象：顶层有 data 对象则进一层，否则用响应本身。"""
    d = resp.get("data")
    return d if isinstance(d, dict) else resp


def _data_array(resp: dict, key: str) -> list:
    """响应取业务数组：先在响应本身找 key，没有再进 data 层找。"""
    v = resp.get(key)
    if isinstance(v, list):
        return v
    d = resp.get("data")
    if isinstance(d, dict):
        v = d.get(key)
        if isinstance(v, list):
            return v
    return []


def ensure_online_3x(client, log) -> bool:
    """短连接转在线，3 次尝试（对齐 TaskRunController.ensureShortOnline）。"""
    msg = "未尝试"
    for attempt in range(1, 4):
        online, _cached, msg = cnsession.ensure_online(client)
        if online:
            log("✓ " + msg)
            return True
        if attempt < 3:
            log("转在线重试 %d/3：%s" % (attempt, msg))
            time.sleep(0.8)
    log("✗ " + msg)
    return False


def fetch_completed_today(client, names: list[str]) -> set[str]:
    """check_for_previous_completion → 今日已完成的 def_id 集合。"""
    if not names:
        return set()
    resp = _post_or_none(
        client, "/service/quest/api/v1/check_for_previous_completion",
        {"quest_names": list(names)}, include_level_id=False,
    )
    if resp is None:
        return set()
    out: set[str] = set()
    for item in _data_array(resp, "results"):
        if not isinstance(item, dict) or not item.get("completed_today"):
            continue
        name = str(item.get("quest_name") or item.get("daily_quest_def_id") or "").strip()
        if name:
            out.add(name)
    return out


def extract_target_value(create_data: dict, stat_type: str) -> int:
    """create 响应里按 stat_type 取真实 target_value（缺省 1）。"""
    stats = create_data.get("stats")
    targets = stats.get("target_values") if isinstance(stats, dict) else None
    if isinstance(targets, list):
        for tv in targets:
            if isinstance(tv, dict) and str(tv.get("stat_type") or "") == stat_type:
                try:
                    return max(1, int(tv.get("value", 1)))
                except (TypeError, ValueError):
                    return 1
    return 1


def resolve_stat_type(create_data: dict, stat_type: str) -> str:
    """create 响应里解析真实 stat_type。

    部分每日任务（如 color_voted_for_kite）不在 season_quests 清单里（只在
    has_season_quests_rewards 出现），无从预知 stat_type；实测它的 stat_type
    就是 create 响应 target_values 里的第一个。清单里给了 stat_type 且能在
    target_values 命中则原样用，否则回退 target_values 第一个。
    """
    stats = create_data.get("stats")
    targets = stats.get("target_values") if isinstance(stats, dict) else None
    types = [
        str(t.get("stat_type") or "").strip()
        for t in (targets or [])
        if isinstance(t, dict) and str(t.get("stat_type") or "").strip()
    ]
    if stat_type and (not types or stat_type in types):
        return stat_type
    return types[0] if types else stat_type


def _quest_anchor_date(quests: list[dict]) -> str:
    for q in quests:
        d = str(q.get("date") or "").strip()
        if d:
            return d
    return ""


def build_quests(resp: dict) -> list[dict]:
    """任务清单 = season_quests + has_season_quests_rewards 补缺。

    09-09 实测：当天 4 个任务只回来 3 个（color_voted_for_kite 缺席），
    但 has_season_quests_rewards 完整列出 4 个（rewarded 标志不可靠，
    已领也常是 false）。缺的任务以 def_id 补入、stat_type 留空由 create
    自解析；date 与 season_quests 锚定日期不一致的旧条目不补。
    """
    quests = [
        q for q in _data_array(resp, "season_quests")
        if isinstance(q, dict)
        and str(q.get("daily_quest_def_id") or "").strip()
        and str(q.get("stat_type") or "").strip()
    ]
    listed = {str(q.get("daily_quest_def_id")) for q in quests}
    anchor = _quest_anchor_date(quests)
    for r in _data_array(resp, "has_season_quests_rewards"):
        if not isinstance(r, dict):
            continue
        def_id = str(r.get("daily_quest_def_id") or "").strip()
        if not def_id or def_id in listed:
            continue
        rdate = str(r.get("date") or "").strip()
        if anchor and rdate and rdate != anchor:
            continue
        quests.append({"daily_quest_def_id": def_id, "stat_type": ""})
        listed.add(def_id)
    return quests


def find_player_quest(client, def_id: str) -> tuple[dict, str]:
    """在任务列表里按 quest_name 找今天的实例 → (实例条目, 所在桶)。找不到 → ({}, "")。

    ★ 2026-10-05 实测（docs/知识库/游戏功能/95）：**同一次「在线窗口」内对同一
      def_id 发第二次 create，服务端只回去重回执 `{"result": "ok"}` —— 里面没有
      quest_id**（第一发才回完整对象：quest_id + stats.target_values）。而
      `claim_one_quest` 的失败重试第 2 发正好撞上它，于是把「已经建好的实例」当失败
      丢掉 —— 线上症状就是 `(3/4) recharge_by_shroom·领取失败(无 quest_id)`，
      重试越勤越容易触发（跑图/其它任务先建过实例时，第 1 发就已经是去重回执）。

    实例条目里同样带 `stats`（含 target_values/completion_requirements），所以补
    target 值也靠它，不会退化成 1。

    会话失效（401/403/423）照常上抛给任务层续期 —— 这正是 `_post_or_none` 的语义。
    """
    resp = _post_or_none(client, QUEST_LIST_PATH, {}, include_level_id=False)
    if not isinstance(resp, dict):
        return {}, ""
    fallback: tuple[dict, str] = ({}, "")
    for bucket in QUEST_BUCKETS:
        rows = resp.get(bucket)
        if not isinstance(rows, list):
            continue
        for it in rows:
            if not isinstance(it, dict):
                continue
            name = str(it.get("quest_name") or it.get("quest_def_id") or "").strip()
            if name != def_id:
                continue
            if bucket == "active":
                return it, bucket
            if not fallback[0]:
                fallback = (it, bucket)
    return fallback


def claim_one_quest(client, def_id: str, stat_type: str, tag: str, log, stop_flag=None) -> bool:
    """单个每日任务领奖闭环（create → set_achievement_stats → update_quest），失败重试。"""
    quest_id = ""           # ★ 跨重试保留实例：第 2 发 create 只回去重回执，没有 quest_id
    cd: dict = {}
    for attempt in range(1, QUEST_MAX_ATTEMPTS + 1):
        if stop_flag is not None and stop_flag.is_set():
            return False
        last = attempt == QUEST_MAX_ATTEMPTS
        if attempt > 1 and def_id in fetch_completed_today(client, [def_id]):
            log("%s·今日已完成" % tag)
            return True

        if not quest_id:
            create = _post_or_none(client, "/service/quest/api/v1/create", {
                "quest_name": def_id,
                "quest_giver": "",
                "expiry_time": 0,
                "is_scheduled_quest": False,
            }, include_level_id=False)
            if create is None:
                if def_id in fetch_completed_today(client, [def_id]):
                    log("%s·今日已完成" % tag)
                    return True
                if not last:
                    continue
                log("%s·领取失败(create)" % tag)
                return False
            cd = _data_obj(create)
            quest_id = str(cd.get("quest_id") or "").strip()
            if not quest_id:
                # create 没给实例：要么本窗口内已建过（去重回执 {"result": "ok"}），
                # 要么服务端今日不给开（{"is_today_quest": false}）。前者去任务列表
                # 里把实例取回来继续推，后者如实报「未排期」。
                if def_id in fetch_completed_today(client, [def_id]):
                    log("%s·今日已完成" % tag)
                    return True
                inst, bucket = find_player_quest(client, def_id)
                if bucket in QUEST_DONE_BUCKETS and inst:
                    log("%s·今日已完成(%s)" % (tag, bucket))
                    return True
                quest_id = str((inst or {}).get("quest_id") or "").strip()
                if not quest_id:
                    if not last:
                        continue
                    if cd.get("is_today_quest") is False:
                        log("%s·领取失败(今日未排期)" % tag)
                    else:
                        log("%s·领取失败(无 quest_id：create 未回实例，任务列表里也没有)" % tag)
                    return False
                if isinstance((inst or {}).get("stats"), dict):
                    cd = inst        # 去重回执里没有 stats/target_values，用实例里的
                log("%s·create 回去重回执，已从任务列表取回实例" % tag)
        target_value = extract_target_value(cd, stat_type)
        # 补缺任务 stat_type 未知 → 用 create 响应的真实 stat_type
        real_type = resolve_stat_type(cd, stat_type)
        if real_type != stat_type:
            stat_type = real_type
            target_value = extract_target_value(cd, stat_type)

        set_resp = _post_or_none(client, "/service/status/api/v1/set_achievement_stats", {
            "achievement_stats": [{"type": stat_type, "value": target_value}],
        }, include_level_id=False)
        if set_resp is None:
            if not last:
                continue
            log("%s·领取失败(set_stat)" % tag)
            return False

        upd = _post_or_none(client, "/service/quest/api/v1/update_quest", {
            "quest_id": quest_id, "status": "succeeded", "collect": True,
        }, include_level_id=False)
        if upd is None:
            if def_id in fetch_completed_today(client, [def_id]):
                log("%s·今日已完成" % tag)
                return True
            if not last:
                continue
            log("%s·领取失败(update)" % tag)
            return False
        status = str(_data_obj(upd).get("status") or "")
        if status == "collected":
            log("%s·领取成功" % tag)
            return True
        if not last:
            continue
        log("%s·领取失败(status=%s)" % (tag, status or "?"))
    return False


def run_daily_quests(client, log=lambda m: None, stop_flag: threading.Event | None = None) -> dict:
    """跑每日任务领奖。返回统计摘要（ok=False 时任务层按会话探测语义处理）。"""
    log("[每日任务] 拉取任务清单…")
    season = _post_or_none(client, "/account/get_season_quests", {}, include_level_id=True)
    if season is None:
        log("[每日任务] ✗ 拉取任务列表失败")
        return {"ok": False, "summary": "拉取任务列表失败", "succ": 0, "fail": 0, "total": 0, "quests": []}
    quests = build_quests(season)
    if not quests:
        log("[每日任务] 今日没有可领取的任务")
        return {"ok": True, "summary": "今日无任务", "succ": 0, "fail": 0, "total": 0, "quests": []}
    total = len(quests)
    log("[每日任务] 共 %d 项" % total)

    names = [str(q.get("daily_quest_def_id")) for q in quests]
    done_today = fetch_completed_today(client, names)

    succ = 0
    fail = 0
    per: list[dict] = []
    for idx, q in enumerate(quests):
        if stop_flag is not None and stop_flag.is_set():
            log("收到停止信号，终止每日任务")
            break
        def_id = str(q.get("daily_quest_def_id"))
        stat_type = str(q.get("stat_type"))
        tag = "[每日任务] (%d/%d) %s" % (idx + 1, total, def_id)
        item = {"def_id": def_id, "stat_type": stat_type, "status": ""}
        if def_id in done_today:
            log("%s·今日已完成" % tag)
            succ += 1
            item["status"] = "done"
            per.append(item)
            continue
        ok = claim_one_quest(client, def_id, stat_type, tag, log, stop_flag)
        if ok:
            succ += 1
            item["status"] = "ok"
        else:
            fail += 1
            item["status"] = "fail"
        per.append(item)
    log("[每日任务] 完成 · 领取 %d/%d" % (succ, total))
    stopped = stop_flag is not None and stop_flag.is_set()
    return {
        "ok": fail == 0 and succ > 0 and not stopped,
        "summary": "每日任务 %d/%d" % (succ, total),
        "succ": succ, "fail": fail, "total": total,
        "stopped": stopped, "quests": per,
    }


def run_daily(client, log=lambda m: None, stop_flag: threading.Event | None = None, preview: bool = False) -> dict:
    """任务入口：转在线 →（preview ? 只查清单 : 领奖闭环）。"""
    if not ensure_online_3x(client, log):
        return {"ok": False, "summary": "转在线失败", "succ": 0, "fail": 0, "total": 0, "quests": []}
    if preview:
        log("[每日任务] 预览模式：只查清单不领奖")
        season = _post_or_none(client, "/account/get_season_quests", {}, include_level_id=True)
        if season is None:
            return {"ok": False, "summary": "拉取任务列表失败", "preview": True, "quests": []}
        quests = build_quests(season)
        names = [str(q.get("daily_quest_def_id")) for q in quests]
        done = fetch_completed_today(client, names)
        per = [
            {
                "def_id": str(q.get("daily_quest_def_id")),
                "stat_type": str(q.get("stat_type")),
                "status": "done" if str(q.get("daily_quest_def_id")) in done else "pending",
            }
            for q in quests
        ]
        pend = sum(1 for p in per if p["status"] == "pending")
        log("[每日任务] 今日 %d 项，已完成 %d，待领取 %d" % (len(per), len(per) - pend, pend))
        return {"ok": True, "summary": "待领取 %d/%d" % (pend, len(per)), "preview": True, "quests": per}
    res = run_daily_quests(client, log=log, stop_flag=stop_flag)
    # 遇境神坛季蜡（季卡每日额外 1 根）：与每日任务同一次「已转在线」窗口，不再单独转在线
    # （ensure_online=False）。没季卡时服务端无可领取物 —— 差值判据会给出「无季卡·无可领取」。
    # 任何异常都不影响每日任务本身的结果。
    try:
        home = seasonhome.claim(client, log=log, stop_flag=stop_flag, ensure_online=False)
        res["season_home"] = home
        if isinstance(res.get("summary"), str) and home.get("summary"):
            res["summary"] = "%s · %s" % (res["summary"], home["summary"])
    except Exception as e:
        log("[神坛季蜡] 跳过：%r" % (e,))
    return res
