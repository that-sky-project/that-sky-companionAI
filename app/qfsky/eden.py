# -*- coding: utf-8 -*-
"""伊甸献祭：光翼获取 → 逐个献祭 → 清理剩余 → 兑换红蜡。

移植 core/src/main/java/com/qingfeng/luoying/account/data/game/EdenSacrificeApi.kt
  + WingCollectApi.kt（国服，纯 HTTP wing_buffs 系，无需进图保活，短转在线即可）。

协议要点（原版实证）：
- wing_buffs/collect 与 get_map_defs 用默认请求头（带 x-sky-level-id）；
- deposit / get / drop / convert 一律不带 x-sky-level-id（include_level_id=False）；
- deposit_id 是 64 位掩码，[WINGS] 分两组各 63 项、掩码各自从 1 重来（清单里有重复
  depositId，靠 name 区分）；最大 2^63，Python int 原生无损（原版用 BigInteger）；
- 献祭不掉的光翼留在身上会挡住 convert（国服实测）→ convert 前必须 drop 干净并复读确认；
- 献祭成功与否以服务端为准：回执判定 + 一次 wing_buffs/get 复核「清单内 deposited 新增了几个」。
  本地"请求没抛异常"不等于服务端已收 —— 线上反馈「日志 126/126、账户却一根红蜡都没多」
  就是这么来的：deposit 的 200 回执里带着拒绝理由，旧版只判 None 就一律记成功；
- convert 的结算粒度没有实证回执（国服抓包里没有 deposit/convert 响应样本），若它一次只结算一根，
  单次调用就会让 126 项献祭只产出 1 根。故按「红蜡不再增长」循环结算（上限 CONVERT_MAX 次）；
- ⚠ 消耗光翼不可逆：光翼 → 升华蜡烛（红蜡，get_currency 的 prestige 字段；碎光 prestige_wax）。
"""
from __future__ import annotations

import json
import time

from .daily import ensure_online_3x

DEPOSIT_PATH = "/account/wing_buffs/deposit"
CONVERT_PATH = "/account/wing_buffs/convert"
GET_PATH = "/account/wing_buffs/get"
DROP_PATH = "/account/wing_buffs/drop"
GET_MAP_DEFS_PATH = "/account/get_map_defs"
COLLECT_PATH = "/account/wing_buffs/collect"

DROP_BATCH = 20          # drop 每批 names 上限（原版同值）
DEPOSIT_PAUSE = 0.12     # 逐个献祭间隔（原版不节流；稍留间隔更稳）
CONVERT_MAX = 126        # convert 最多重复调用次数（= 清单长度：单次只换一根时也够换完一轮）
CONVERT_NG_STOP = 2      # 连续 N 次兑换没有红蜡进账就认为已换完，停止重复调用
CONVERT_PAUSE = 0.12     # 重复兑换间隔
SAMPLE_MAX = 200         # 回执样本落日志的截断长度（诊断用，别把整包刷进日志）

# 献祭光翼清单（对齐易语言 #伊甸献祭参数 / EdenSacrificeApi.kt L50-177，126 项两组）
WINGS: tuple[tuple[str, str], ...] = (
    # 第一组：掩码 1 → 2^63
    ("l_SunsetEnd2_0", "1"),
    ("l_Dawn_TrialsEarth_0", "2"),
    ("l_MainStreet_0", "4"),
    ("l_Sunset_Citadel_0", "8"),
    ("l_Sunset_Citadel_1", "16"),
    ("l_DayHubCave_0", "32"),
    ("l_Sunset_Theater_0", "64"),
    ("l_SunsetColosseum_0", "256"),
    ("l_SunsetEnd_0", "512"),
    ("l_CandleSpace_0", "1024"),
    ("l_SunsetRace_0", "2048"),
    ("l_Sunset_2", "4096"),
    ("l_Sunset_1", "8192"),
    ("l_Sunset_0", "16384"),
    ("l_Sunset_YetiPark_1", "32768"),
    ("l_Sunset_YetiPark_0", "65536"),
    ("l_RainEnd_0", "131072"),
    ("l_RainMid_1", "262144"),
    ("l_RainMid_0", "524288"),
    ("l_RainMid_2", "1048576"),
    ("l_DuskGraveyard_3", "2097152"),
    ("l_DuskOasis_1", "4194304"),
    ("l_DuskGraveyard_2", "8388608"),
    ("l_DuskOasis_0", "16777216"),
    ("l_DuskGraveyard_1", "33554432"),
    ("l_Dawn_3", "67108864"),
    ("l_DuskGraveyard_0", "134217728"),
    ("l_Dawn_2", "268435456"),
    ("l_Dawn_1", "536870912"),
    ("l_Dawn_0", "1073741824"),
    ("l_DuskGraveyard_5", "2147483648"),
    ("l_Dusk_Triangle_1", "4294967296"),
    ("l_DuskGraveyard_4", "8589934592"),
    ("l_Dawn_6", "00000000000"),
    ("l_Dusk_Triangle_0", "34359738368"),
    ("l_Dawn_5", "68719476736"),
    ("l_Dawn_4", "137438953472"),
    ("l_Rain_BaseCamp_0", "274877906944"),
    ("l_Rain_BaseCamp_1", "549755813888"),
    ("l_Prairie_NestAndKeeper_1", "1099511627776"),
    ("l_Prairie_NestAndKeeper_0", "2199023255552"),
    ("l_Dawn_TrialsFire_0", "4398046511104"),
    ("l_Dusk_CrabField_1", "8796093022208"),
    ("l_Dusk_CrabField_0", "17592186044416"),
    ("l_Dusk_CrabField_2", "35184372088832"),
    ("l_RainShelter_0", "70368744177664"),
    ("l_RainShelter_1", "140737488355328"),
    ("l_Dusk_0", "281474976710656"),
    ("l_Dusk_1", "562949953421312"),
    ("l_Night_1", "1125899906842624"),
    ("l_Night_0", "2251799813685248"),
    ("l_Prairie_Island_1", "4503599627370496"),
    ("l_Prairie_Island_0", "9007199254740992"),
    ("l_Prairie_Island_3", "18014398509481984"),
    ("l_Prairie_Island_2", "36028797018963968"),
    ("l_Prairie_Island_5", "72057594037927936"),
    ("l_Prairie_Island_4", "144115188075855872"),
    ("l_Prairie_Island_7", "288230376151711744"),
    ("l_Prairie_Island_6", "576460752303423488"),
    ("l_Prairie_WildLifePark_0", "1152921504606846976"),
    ("l_Prairie_WildLifePark_1", "2305843009213693952"),
    ("l_Prairie_WildLifePark_2", "4611686018427387904"),
    ("l_Night_StoryBook_0", "9223372036854775808"),
    # 第二组：掩码从 1 重来
    ("l_SunsetVillage_2", "1"),
    ("l_Sunset_FlyRace_0", "2"),
    ("l_SunsetVillage_1", "4"),
    ("l_Sunset_FlyRace_1", "8"),
    ("l_SunsetVillage_0", "16"),
    ("l_Rain_BlueBirdTheater_0", "32"),
    ("l_StormStart_0", "64"),
    ("l_Dawn_TrialsWater_0", "256"),
    ("l_NightArchive_1", "512"),
    ("l_Night_PaintedWorld_1", "1024"),
    ("l_NightArchive_0", "2048"),
    ("l_Night_PaintedWorld_0", "4096"),
    ("l_Night_PaintedWorld_2", "8192"),
    ("l_Rain_1", "16384"),
    ("l_Rain_0", "32768"),
    ("l_DuskMid_0", "65536"),
    ("l_DuskMid_1", "131072"),
    ("l_Storm_8", "262144"),
    ("l_Night2_1", "524288"),
    ("l_Storm_6", "1048576"),
    ("l_Night2_0", "2097152"),
    ("l_Night_Shelter_0", "4194304"),
    ("l_Storm_7", "8388608"),
    ("l_Night2_3", "16777216"),
    ("l_RainForest_0", "33554432"),
    ("l_Storm_4", "67108864"),
    ("l_Night2_2", "134217728"),
    ("l_RainForest_1", "268435456"),
    ("l_Storm_5", "536870912"),
    ("l_RainForest_2", "1073741824"),
    ("l_Storm_2", "2147483648"),
    ("l_RainForest_3", "4294967296"),
    ("l_Storm_3", "8589934592"),
    ("l_Storm_0", "00000000000"),
    ("l_Storm_1", "34359738368"),
    ("l_Prairie_Cave_1", "68719476736"),
    ("l_Prairie_Village_4", "137438953472"),
    ("l_Prairie_Cave_0", "274877906944"),
    ("l_Prairie_Village_0", "549755813888"),
    ("l_Prairie_Village_1", "1099511627776"),
    ("l_Prairie_Village_2", "2199023255552"),
    ("l_Prairie_Village_3", "4398046511104"),
    ("l_Credits_0", "8796093022208"),
    ("l_Skyway_1", "17592186044416"),
    ("l_Skyway_0", "35184372088832"),
    ("l_DuskEnd_0", "70368744177664"),
    ("l_Prairie_ButterflyFields_2", "140737488355328"),
    ("l_Prairie_ButterflyFields_0", "281474976710656"),
    ("l_Prairie_ButterflyFields_1", "562949953421312"),
    ("l_NightDesert_0", "1125899906842624"),
    ("l_NightDesert_1", "2251799813685248"),
    ("l_NightDesert_2", "4503599627370496"),
    ("l_Dawn_TrialsAir_0", "9007199254740992"),
    ("l_Rain_Cave_3", "18014398509481984"),
    ("l_Rain_Cave_2", "36028797018963968"),
    ("l_Rain_Cave_1", "72057594037927936"),
    ("l_Rain_Cave_0", "144115188075855872"),
    ("l_StormEvent_VoidSpace_1", "288230376151711744"),
    ("l_StormEvent_VoidSpace_0", "576460752303423488"),
    ("l_StormEvent_VoidSpace_3", "1152921504606846976"),
    ("l_StormEvent_VoidSpace_2", "2305843009213693952"),
    ("l_StormEvent_VoidSpace_5", "4611686018427387904"),
    ("l_StormEvent_VoidSpace_4", "9223372036854775808"),
)


# ── 响应解析（data 对象兜底：{...} 或 {"data": {...}}）──

def _data_obj(resp: dict) -> dict:
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return resp if isinstance(resp, dict) else {}


def _post_or_none(client, path: str, inner: dict, include_level_id: bool):
    try:
        return client.post(path, inner, include_level_id=include_level_id)
    except Exception:
        return None


def _wing_buff_names_from_map_defs(resp: dict) -> list:
    """get_map_defs → type=="wing_buff" 的 name（保序去重，对齐后端 wing_buff_names_from_map_defs）。"""
    rows = _data_obj(resp).get("get_map_defs")
    names, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").strip() != "wing_buff":
            continue
        name = str(row.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _num(v):
    """整数化；无法解析返回 None —— 区分「服务端没给这个键」与「服务端给了 0」。

    旧版走 gameinfo.parse_currency：缺键被补成 0，"拉不到余额" 与 "余额为 0" 混在一起，
    排查「献祭没产出」时正好是最需要分清的两件事。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _show(v):
    return "?" if v is None else str(v)


def _short(obj) -> str:
    """回执样本：压成一行、截断（诊断用，别把整包刷进日志）。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(obj)
    return s[:SAMPLE_MAX]


def _collected_names_from_rows(rows) -> list:
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("collected"):
            name = str(row.get("name") or "").strip()
            if name:
                out.append(name)
    return out


def _collected_names(resp: dict) -> list:
    """wing_buffs/get → collected==true 的 name 列表（对齐后端 collected_wing_buff_names）。"""
    return _collected_names_from_rows(_data_obj(resp).get("wing_buffs"))


def fetch_buff_rows(client) -> list:
    """读 wing_buffs/get 的原始行（GET_PATH，include_level_id=False）。失败返回 []。"""
    resp = _post_or_none(client, GET_PATH, {}, False)
    rows = _data_obj(resp).get("wing_buffs") if resp is not None else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def fetch_collected_names(client) -> list:
    """读当前真实持有的光翼名（GET_PATH，include_level_id=False）。献祭后必须重读服务端，
    不能用本地"哪些献祭成功"推算 —— 请求没抛异常与服务端真实状态未必一致。"""
    resp = _post_or_none(client, GET_PATH, {}, False)
    return _collected_names(resp) if resp is not None else []


def _prestige(client):
    """当前红蜡（prestige / 升华蜡烛）余额；读不到返回 None。"""
    return _wallet(client)[0]


def _wallet(client):
    """(红蜡烛 prestige, 红蜡颗粒 prestige_wax)；各自读不到为 None。

    直接读原始 currency：gameinfo.parse_currency 会把缺键补成 0（见 _num 注释）。
    """
    try:
        resp = client.get_currency()
    except Exception:
        return None, None
    c = resp.get("currency") if isinstance(resp, dict) and isinstance(resp.get("currency"), dict) else resp
    if not isinstance(c, dict):
        return None, None
    return _num(c.get("prestige")), _num(c.get("prestige_wax"))


# ── 回执判定 ──

def _deposit_verdict(resp, name: str) -> str:
    """献祭单个光翼的回执判定："ok" / "reject" / "unknown"。

    旧版只有 `resp is None` 两条路：服务端只要回 200（哪怕正文写着拒绝），一律记成功。
    deposit 的成功证据按优先级取：
      1. update_wing_buffs 里该 name 的 deposited=true（服务端明确记下了这次献祭）；
      2. update_wing_buffs 里该 name 的 collected=false（献祭已把它从身上拿走）；
      3. 正文里带 result=="ok" 且没有错误字段。
    认不出来就是 "unknown"（不冒充成功）。
    """
    if resp is None:
        return "reject"
    d = _data_obj(resp)
    rows = d.get("update_wing_buffs")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "").strip() != name:
            continue
        if row.get("deposited") is True or row.get("collected") is False:
            return "ok"
        if row.get("deposited") is False and row.get("collected") is True:
            return "reject"
    res = d.get("result") if isinstance(d.get("result"), str) else None
    if any(k in d for k in ("error", "err_code", "err_msg", "failure")):
        return "reject"
    if res is not None:
        return "ok" if res.strip().lower() in ("ok", "success") else "reject"
    return "unknown"


def _rows_by_name(rows) -> dict:
    by = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        n = str(r.get("name") or "").strip()
        if n and n not in by:
            by[n] = r
    return by


def _skinny(rows) -> dict:
    """只读体检：清单内的持有/已献祭/未持有/deposit_id 是否与本地清单一致 + 全部持有数。"""
    by = _rows_by_name(rows)
    held = {n for n, r in by.items() if r.get("collected")}
    held_in = set()
    dep_in = set()
    bare = 0
    mismatch = 0
    for name, dep in WINGS:
        r = by.get(name)
        if r is None:
            bare += 1
            continue
        if r.get("collected"):
            held_in.add(name)
        if r.get("deposited"):
            dep_in.add(name)
        srv = _num(r.get("deposit_id"))
        if srv is not None and srv != int(dep):
            mismatch += 1
    return {"readable": bool(rows), "held_all": len(held), "held_in_list": held_in,
            "deposited_in_list": dep_in, "missing": bare, "id_mismatch": mismatch}


def audit_state(client, log=None) -> dict:
    """只读体检（不改任何状态）：红蜡烛/红蜡颗粒 + 清单内 持有/已献祭/未持有 + id 不匹配。

    用来回答「为什么献祭不出红蜡」：如果清单 126 项已全部是 deposited，那就是本周期服务端
    没有可结算的东西（不是流程写错）；如果少了/名字对不上，就是清单或头部规则的问题。
    """
    prestige, wax = _wallet(client)
    sk = _skinny(fetch_buff_rows(client))
    out = dict(sk, prestige=prestige, prestige_wax=wax)
    if log is not None:
        log("[伊甸体检] 红蜡烛 %s · 红蜡颗粒 %s" % (_show(prestige), _show(wax)))
        if sk["readable"]:
            log("[伊甸体检] 持有光翼 %d · 清单内 持有 %d / 已献祭 %d / 未持有 %d · deposit_id 不匹配 %d"
                % (sk["held_all"], len(sk["held_in_list"]), len(sk["deposited_in_list"]),
                   sk["missing"], sk["id_mismatch"]))
        else:
            log("[伊甸体检] ✗ 读不到光翼状态（wing_buffs/get 失败）")
    return out


# ── 光翼获取（独立任务，WingCollectApi.run）──

def run_wing_collect(client, log, stop_flag=None) -> dict:
    """候选 = get_map_defs(type==wing_buff) 减去 wing_buffs/get(collected==true)；逐个 collect。
    ★ collect 是游戏内动作，必须先短转在线（原版 WingCollectApi 同序）——缺这步会全部收集失败。"""
    resp = _post_or_none(client, GET_MAP_DEFS_PATH, {}, True)
    all_names = _wing_buff_names_from_map_defs(resp) if resp is not None else []
    if not all_names:
        log("[光翼获取] ✗ 未拉到光翼定义")
        return {"ok": False, "summary": "光翼定义为空", "succ": 0, "fail": 0, "total": 0}
    owned_resp = _post_or_none(client, GET_PATH, {}, True)
    owned = set(_collected_names(owned_resp)) if owned_resp is not None else set()
    pending = [n for n in all_names if n not in owned]
    total = len(pending)
    log("[光翼获取] 获取到 %d 个光翼，缺少 %d 个" % (len(all_names), total))
    if total == 0:
        log("[光翼获取] 已全部收集")
        return {"ok": True, "summary": "光翼已全收集", "succ": 0, "fail": 0, "total": 0}

    # collect 前确保在线（短连接转在线，3 次尝试）
    if not ensure_online_3x(client, lambda m: log("[光翼获取] " + m)):
        return {"ok": False, "summary": "转在线失败", "succ": 0, "fail": 0, "total": total}

    succ = fail = 0
    for idx, name in enumerate(pending):
        if stop_flag is not None and stop_flag.is_set():
            break
        tag = "[光翼获取] (%d/%d) %s" % (idx + 1, total, name)
        resp = _post_or_none(client, COLLECT_PATH, {"names": [name]}, True)
        rows = _data_obj(resp).get("update_wing_buffs") if resp is not None else None
        ok = False
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("name") or "").strip() == name and row.get("collected"):
                ok = True
                break
        if ok:
            succ += 1
            log(tag + "·收集成功")
        else:
            fail += 1
            log(tag + "·收集失败")
    done = succ
    log("[光翼获取] 完成 · 收集 %d/%d（失败 %d）" % (done, total, fail))
    return {"ok": fail == 0 and done > 0, "summary": "光翼获取 %d/%d" % (done, total),
            "succ": succ, "fail": fail, "total": total}


# ── 伊甸献祭（独立任务，只含 献祭→清理→兑换；光翼获取已拆为上面独立任务）──

def run_sacrifice(client, log, stop_flag=None) -> dict:
    """伊甸献祭。⚠ 消耗光翼不可逆；兑换成功即整体成功（个别献祭失败是常态）。

    与旧版的差别（都是线上「126/126 却不出红蜡」暴露出来的）：
    1) 献祭成功按**回执**判定（见 _deposit_verdict），不再把「请求没抛异常」当成功；
       并在献祭后读一次 wing_buffs/get，复核「清单内 deposited 新增了几个」，用它作为
       最终的成功数（succ）。请求受理数与服务端确认数会分别写进日志/摘要。
    2) convert 循环调用到「红蜡不再增长」为止（上限 CONVERT_MAX 次，连续 CONVERT_NG_STOP
       次无进账即停）：单次结算一根的实现也能换完整轮，且不会再出现「126 项换 1 根」.
    3) 红蜡 before→after **恒定**写进摘要（旧版只在变化时写，一根没多时看起来像少了这一段），
       并带上红蜡颗粒（prestige_wax）：它能区分「献祭产出的是碎光」与「已换成整根」。
    """
    before, wax_before = _wallet(client)
    log("[伊甸献祭] 余额 · 红蜡烛 %s · 红蜡颗粒 %s" % (_show(before), _show(wax_before)))

    # 转在线（短连接在线，deposit/convert 需要在线状态）
    if not ensure_online_3x(client, lambda m: log("[伊甸献祭] " + m)):
        return {"ok": False, "summary": "转在线失败"}

    if before is None:
        # 开头那次读余额失败（未在线/网络抖动）：兑换循环是靠「红蜡不再增长」收敛的，
        # 没有起点就退化成只兑换一次 —— 那正是「126 项只出一根」的形态，所以这里再读一次。
        before, wax_before = _wallet(client)
        log("[伊甸献祭] 转在线后复读余额 · 红蜡烛 %s · 红蜡颗粒 %s" % (_show(before), _show(wax_before)))

    # 步骤0：只读体检（不改状态）：清单里到底还有多少可献祭的、是不是已经全献过了
    sk = _skinny(fetch_buff_rows(client))
    dep_before = sk["deposited_in_list"]
    if sk["readable"]:
        log("[伊甸献祭] 体检 · 持有光翼 %d · 清单内 持有 %d / 已献祭 %d / 未持有 %d · deposit_id 不匹配 %d"
            % (sk["held_all"], len(sk["held_in_list"]), len(dep_before), sk["missing"], sk["id_mismatch"]))
        if sk["id_mismatch"]:
            log("⚠ 有 %d 项的 deposit_id 与服务端不一致：清单可能过期，这些项大概率被拒" % sk["id_mismatch"])
        if not sk["held_in_list"]:
            # 清单里一个可献祭的都没有：继续跑只会把身上其余光翼 drop 掉（不可逆）却换不到红蜡
            log("✗ 清单内 0 项处于「持有」状态：本次没有任何可献祭的光翼，已中止（避免白丢持有光翼）")
            log("  先跑「光翼获取」把这些光翼收齐，再跑「伊甸献祭」；若已收齐仍为 0，说明清单/头部规则要重新核对")
            return {"ok": False, "succ": 0, "total": len(WINGS), "dropped": 0,
                    "summary": "已中止 · 清单内无持有光翼（先跑「光翼获取」）"}
        if len(dep_before) >= len(WINGS):
            log("⚠ 清单 126 项在本周期已全部献祭过：服务端不会重复结算红蜡（换周期/等重置后才会再产）")
    else:
        log("[伊甸献祭] 体检跳过（读不到光翼状态），照常献祭")

    # 步骤1/3 逐个献祭
    total = len(WINGS)
    log("[伊甸献祭] 步骤1/3 献祭光翼 · 共 %d 项" % total)
    accepted = fail = unknown = 0
    stopped = False
    sample = None
    for idx, (name, deposit_id) in enumerate(WINGS):
        if stop_flag is not None and stop_flag.is_set():
            stopped = True
            break
        resp = _post_or_none(client, DEPOSIT_PATH,
                             {"name_deposit_id_pairs": [[name, int(deposit_id)]]}, False)
        verdict = _deposit_verdict(resp, name)
        if sample is None:
            # 首个回执样本进日志：没有真实抓包样本前，这一行就是判断协议有没有变的唯一依据
            sample = _short(resp) if resp is not None else "(请求异常，无回执)"
            log("[伊甸献祭] 首个回执样本：%s" % sample)
        if verdict == "ok":
            accepted += 1
        elif verdict == "reject":
            fail += 1
            if fail <= 3:
                log("[伊甸献祭] 被拒 (%d/%d) %s · %s" % (idx + 1, total, name, _short(resp)))
        else:
            unknown += 1
        if (idx + 1) % 20 == 0:
            log("[伊甸献祭] 进度 %d/%d" % (idx + 1, total))
        time.sleep(DEPOSIT_PAUSE)
    log("[伊甸献祭] 献祭请求完成 · 受理 %d/%d（被拒 %d · 无判定 %d）" % (accepted, total, fail, unknown))
    if stopped:
        log("[伊甸献祭] 已停止：剩余光翼未献，可稍后重跑（已献祭过的会被服务端拒绝，属正常）")
        return {"ok": False, "stopped": True, "accepted": accepted,
                "summary": "已停止 · 献祭受理 %d/%d" % (accepted, total)}

    # 服务端复核：本轮真正新增了多少条 deposited（这才是「献祭成功」的权威口径）
    rows1 = fetch_buff_rows(client)
    if rows1:
        dep_after = _skinny(rows1)["deposited_in_list"]
        confirmed = len(dep_after - dep_before)
        log("[伊甸献祭] 服务端复核 · 清单内已献祭 %d 个（本轮新增 %d）" % (len(dep_after), confirmed))
        if confirmed == 0 and accepted > 0:
            log("⚠ 本轮 0 项新增献祭：请求没抛异常但服务端状态没变 —— 献祭未生效"
                "（多半是本周期已献过，或清单/deposit_id 被服务端拒收；上面回执样本里有服务端的理由）")
    else:
        confirmed = accepted
        log("[伊甸献祭] 服务端复核失败（读不到光翼状态），本轮以回执受理数计：%d" % confirmed)

    # 步骤2/3 清理剩余（献祭不掉的会挡住 convert —— 复用刚读到的那一份服务端状态）
    log("[伊甸献祭] 步骤2/3 清理剩余光翼…")
    leftover = _collected_names_from_rows(rows1) if rows1 else fetch_collected_names(client)
    dropped = 0
    if not leftover:
        log("[伊甸献祭] 无剩余光翼，跳过清理")
    else:
        log("[伊甸献祭] 剩余 %d 个，开始丢弃" % len(leftover))
        for i in range(0, len(leftover), DROP_BATCH):
            if stop_flag is not None and stop_flag.is_set():
                stopped = True
                break
            batch = leftover[i:i + DROP_BATCH]
            resp = _post_or_none(client, DROP_PATH, {"names": batch}, False)
            ok_count = 0
            if resp is not None:
                rows = _data_obj(resp).get("update_wing_buffs")
                # 以 collected==false 的条数为准（比 result 字段实在，对齐原版）
                ok_count = sum(1 for r in (rows or []) if isinstance(r, dict) and r.get("collected") is False)
            dropped += ok_count
            tail = "" if ok_count >= len(batch) else "（部分未生效）"
            log("[伊甸献祭] 丢弃批次 %d · %d/%d%s" % (i // DROP_BATCH + 1, ok_count, len(batch), tail))
        if not stopped:
            remain = fetch_collected_names(client)
            if remain:
                log("[伊甸献祭] ⚠ 仍剩 %d 个未清掉，兑换可能失败" % len(remain))
    if stopped:
        log("[伊甸献祭] 已停止：未兑换红蜡")
        return {"ok": False, "stopped": True, "accepted": accepted, "succ": confirmed, "total": total,
                "summary": "已停止 · 献祭 %d/%d · 丢弃 %d" % (confirmed, total, dropped)}

    # 步骤3/3 兑换红蜡：循环到「红蜡不再增长」
    log("[伊甸献祭] 步骤3/3 兑换红蜡…")
    # ★只读诊断（2026-10-04 加）：convert 之前/之后各读一次 wing_buffs/get 的「行数 + collected 数」。
    # 目的：关掉「convert 会不会把光翼行删掉」这个问号 —— 如果 convert 后行数归零，说明存在一条
    # **会把行抹掉**的写通道，强制无翼的「真 null 态」路线就还有救；否则行只会留在 collected=false。
    # 纯只读（GET_PATH），不影响任何判定、不新增写调用。
    rows_pre_convert = len(fetch_buff_rows(client))
    log("[伊甸献祭] 兑换前光翼行数 %d（只读诊断）" % rows_pre_convert)
    after = before
    calls = gained = 0
    zero = 0
    convert_ok = False
    # 读不到起始余额就没法判断「有没有再涨」，此时只换一次（保持旧行为，不空转）
    max_calls = 1 if before is None else CONVERT_MAX
    for i in range(max_calls):
        if i and stop_flag is not None and stop_flag.is_set():
            log("[伊甸献祭] 已停止：不再重复兑换")
            break
        resp = _post_or_none(client, CONVERT_PATH, {}, False)
        calls += 1
        if resp is None:
            if not convert_ok:
                log("[伊甸献祭] ✗ 兑换红蜡失败（第 1 次请求异常）")
                break
            log("[伊甸献祭] 兑换第 %d 次请求异常，停止重复兑换" % calls)
            break
        if calls == 1:
            log("[伊甸献祭] 兑换回执：%s" % _short(resp))
        convert_ok = True
        now = _prestige(client)
        if now is None:
            log("[伊甸献祭] 兑换后读不到红蜡余额，停止重复兑换")
            break
        delta = now - after if after is not None else 0
        after = now
        if delta > 0:
            gained += delta
            zero = 0
            log("[伊甸献祭] 兑换第 %d 次 · 红蜡 +%d（累计 +%d）" % (calls, delta, gained))
        else:
            zero += 1
            if zero >= CONVERT_NG_STOP:
                break
        time.sleep(CONVERT_PAUSE)

    _, wax_after = _wallet(client)

    # ★只读诊断收尾：convert 之后的行数 + collected。判定见下方 summary 的 convert_rows 字段。
    rows_post_convert = len(fetch_buff_rows(client))
    log("[伊甸献祭] 兑换后光翼行数 %d（只读诊断）" % rows_post_convert)
    if rows_pre_convert > 0:
        if rows_post_convert == 0:
            log("★★ 发现会抹除光翼行的通道：convert 后 row %d → 0（= 真·null 态可达）"
                % rows_pre_convert)
        elif rows_post_convert < rows_pre_convert:
            log("★ convert 删掉了部分行：row %d → %d" % (rows_pre_convert, rows_post_convert))
        else:
            log("convert 未删行（row %d → %d）⇒ 与 drop/deposit 同性质：只改标记"
                % (rows_pre_convert, rows_post_convert))
    if not convert_ok:
        fail_parts = ["献祭 %d/%d" % (confirmed, total), "丢弃 %d" % dropped]
        if accepted != confirmed:
            fail_parts.append("受理 %d" % accepted)
        fail_parts.append("兑换红蜡失败")
        if rows_pre_convert:
            fail_parts.append("行数 %d→%d" % (rows_pre_convert, rows_post_convert))
        return {"ok": False, "accepted": accepted, "succ": confirmed, "total": total,
                "dropped": dropped, "prestige": after, "convert_rows": [rows_pre_convert, rows_post_convert],
                "summary": " · ".join(fail_parts)}

    # ── 汇总：恒带红蜡 before→after ──
    parts = ["献祭 %d/%d" % (confirmed, total), "丢弃 %d" % dropped]
    if before is not None and after is not None:
        if after != before:
            parts.append("红蜡 %d→%d（+%d）" % (before, after, after - before))
        else:
            parts.append("红蜡 %d→%d" % (before, after))
    else:
        parts.append("红蜡 %s→%s" % (_show(before), _show(after)))
    if wax_before is not None and wax_after is not None and (wax_before or wax_after):
        parts.append("红蜡颗粒 %d→%d" % (wax_before, wax_after))
    if calls > 1:
        parts.append("兑换 %d 次" % calls)
    if accepted != confirmed:
        # 别让「受理 126」被读成「成功 126」：服务端确认数才是结果
        parts.append("受理 %d" % accepted)
    if rows_pre_convert and rows_post_convert != rows_pre_convert:
        parts.append("行数 %d→%d" % (rows_pre_convert, rows_post_convert))
    log("[伊甸献祭] ✓ 兑换红蜡完成 · %s" % " · ".join(parts[1:]))
    return {"ok": True, "accepted": accepted, "succ": confirmed, "total": total,
            "dropped": dropped, "prestige": after, "prestige_before": before,
            "prestige_wax": wax_after, "convert_calls": calls,
            "convert_rows": [rows_pre_convert, rows_post_convert],
            "summary": " · ".join(parts)}
