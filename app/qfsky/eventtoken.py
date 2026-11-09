# -*- coding: utf-8 -*-
"""活动代币（拾取路径）：按 assets/event_tokens.json 的官方活动数据收集活动代币。

════════════════════════════════════════════════════════════════════════════
★ 2026-09-25 实测定案（月光节，level_id=1694829989）——以前收不到的唯一原因
════════════════════════════════════════════════════════════════════════════
`POST /account/collect_pickup_batch` **必须先 enter_map 进图**，否则不管 body 里
放什么（15 个真 id / 32 槽 / emitters），服务端一律返回 **`{}` 空响应**（HTTP 200）。
进图后同一批 id 立即返回逐项 `batch_results`：

    进图前：{}                                        ← 空响应，看起来像"接口没配"
    进图后：{"batch_results":[{"w":16841,"r":"ok","id":360617922,
              "ct":"event_candle_days_of_moonlight","cid":"","cc":1}, …×15]}
    钱包：  event_candle_days_of_moonlight  0 → 15  ★ 真入账

所以旧版直接在未进图状态调用、拿到 `{}` 就判"活动未开放"，是错判。

## emitters 是什么（已实测，不再是猜测）
emitters = **动态发射器**拾取物，与 pickup_ids（地图里预放的固定拾取物）是两条独立路径。
body 形状：`{"emitter_name": str, "event_name": str, "chunk_seq": int, "pickup_seq": int}`。
`chunk_seq` = 第几批发射（发射器按 `emit_interval` 秒/批往外吐，`last_chunk_seq` 从 -1 递增），
`pickup_seq` = 该批里的第几个（通常 0）。

服务端对每个 emitter 条目的回显 cid 是 5 段展开：
    `{emitter_name}:{event_name}:{???}:{chunk_seq}:{pickup_seq}`
    → "NatureMicrodarkness:global_nature_2026:0:32:0"
认不出该发射器就回 `{"r":"unknown_emitter","id":0,"cid":"<展开串>"}`。

★ 实时发射器表的权威来源是 **`POST /account/get_level_pickups {"level": <level_id>}`**，
返回 `pickup_emitters`：每个 `{emitter_name, events:[{event_name, currency_type,
pool_name, pool_amount, emit_interval, chunks_per_emit, chunk_size_amount,
last_chunk_seq, collected, active_duration, preactive_duration}]}`。
也可以用它读 `last_chunk_seq` 判断该发射器吐到第几批了。

## 本账号实测的 emitter 结论
`global_nature_2026` / `NatureMicrodarkness` 在国服 0.16.5 当前**不存在**：
`get_level_pickups(1694829989)` 的 9 个发射器是 FashionECReward_Fast/Slow、
MoonLight_MessageLantern/Region/SharedSpaceMural(_create)、ShareMoonCake、
SocialFeedChallenge、TreasureShovel —— 没有 Nature*。该名字也不在国服
`libBootloader.so` 里（`global_nature_2026` 只在字符串表出现 1 次，无 emitter 名），
说明 emitter_name 由**服务端下发**、客户端不硬编码。
⇒ 国服当前发 emitters 只会拿到 `unknown_emitter`，代币获取走 pickup_ids 就够了。
   等该活动在国服开放后，只需要把 `get_level_pickups` 里读到的 emitter_name/event_name
   填进 event_tokens.json 的 `emitters`，本模块会照发。

## 资产格式（活动轮换时整体替换 assets/event_tokens.json）
{
  "event": "global_moonlight", "label": "月光节",
  "scene": "MoonLight", "scene_zh": "月光节活动图",
  "map_id": 1694829989,          ← 同时用作 collect 的 level_id 与 enter_map 的图
  "join_level_id": 55942,        ← enter_map 用的进图 join id（= CN_LEVEL_HASH）
  "currency_key": "event_candle_days_of_moonlight",
  "pickup_ids": [15 个代币 guid],
  "emitters": []                 ← 可选；形状见上
}
钱包入账键 = currency_key（不带 `_wax` 的那个）。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import mapcfg
from . import session as cnsession

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
_TOKENS_PATH = os.path.join(_ASSETS_DIR, "event_tokens.json")
BATCH_SIZE = 32          # collect_pickup_batch 单批 32 稳定
READY_SEC = 12           # 进图后就绪门最长等待（实测 ~2s；空响应=未就绪）
_log = lambda m: None  # noqa: E731  （模块级默认日志，避免引用未定义）

# 逐项结果分类
_R_OK = ("ok", "succeeded", "success")
_R_DUP = ("already", "duplicate", "duplicated", "claimed", "collected", "done")
_R_BAD = ("unknown_pickup", "unknown_emitter", "error", "invalid", "forbidden")


def load_event() -> dict:
    """活动代币配置 = 资产表 + 本地覆盖（管理员在面板上改过就以本地为准）。

    本地覆盖放 data/map_overrides.json 而不是直接改 assets/：部署会把 assets/** 传上去，
    改资产下次部署就被仓库旧值覆盖（见 mapcfg 模块头注释）。
    """
    try:
        with open(_TOKENS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = mapcfg.apply_token(cfg)
    ids = cfg.get("pickup_ids")
    has_ids = isinstance(ids, list) and any(str(x).strip().lstrip("-").isdigit() for x in ids)
    has_em = isinstance(cfg.get("emitters"), list) and bool(cfg.get("emitters"))
    if not (has_ids or has_em) or not (cfg.get("map_id") or cfg.get("level_id")):
        return {}
    return cfg


def event_scenes() -> list:
    """当前活动配置（供 main.py /tokens 守卫判空）。"""
    cfg = load_event()
    return [cfg] if cfg else []


def _base(s: str) -> str:
    return s[:-4] if s.endswith("_wax") else s


def _pretty_ct(ct: str, cfg: dict) -> str:
    """ct → 活动 label（ct 与 currency_key 归一化 _wax 后匹配），匹配不到原样返回。"""
    key = str(cfg.get("currency_key") or "")
    if key and _base(ct) == _base(key):
        return str(cfg.get("label") or cfg.get("event") or key)
    return ct


def _currency_obj(client) -> dict:
    try:
        resp = client.get_currency()
    except Exception:
        return {}
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    cur = data.get("currency") if isinstance(data.get("currency"), dict) else {}
    return cur if isinstance(cur, dict) else {}


def _event_balance(client, cfg: dict):
    """get_currency → currency_key 值（+ _wax 键兜底相加）；失败返回 None。"""
    key = str(cfg.get("currency_key") or "")
    if not key:
        return None
    cur = _currency_obj(client)
    if not cur:
        return None
    v, w = cur.get(key), cur.get(key + "_wax")
    return (int(v) if isinstance(v, (int, float)) else 0) + (int(w) if isinstance(w, (int, float)) else 0)


def _int_ids(raw) -> list:
    out = []
    for x in (raw or []):
        s = str(x).strip()
        if s.lstrip("-").isdigit():
            out.append(int(s))
    return out


def _tally(resp, got_ids, cts, uniq):
    """把一次 collect 的 batch_results 归入统计。返回 (新增 ok 数, 该批结果列表)。"""
    rows = resp.get("batch_results") if isinstance(resp, dict) else None
    rows = rows if isinstance(rows, list) else []
    n_ok = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        st = str(r.get("r") or "").lower()
        pid = r.get("id")
        ct = str(r.get("ct") or "")
        cc = r.get("cc")
        cc = int(cc) if isinstance(cc, (int, float)) else 0
        if st in _R_OK:
            n_ok += 1
            if isinstance(pid, int):
                got_ids.add(pid)
            if ct:
                cts[ct] = cts.get(ct, 0) + (cc or 1)
        elif st in _R_DUP:
            if isinstance(pid, int):
                uniq.setdefault(pid, "already")
        elif st in _R_BAD:
            uniq.setdefault(pid if isinstance(pid, int) else str(r.get("cid") or "?"), st)
    return n_ok, rows


# ════════════════════════════════════════════════════════════════════════════
# ★ 每日代币 = 挂机烛火（emitters 扫池）—— 2026-09-25 生产实测定案
# ════════════════════════════════════════════════════════════════════════════
# 月光节的**每日**代币不是 pickup_ids（那 15 颗是一次性的），而是**发射器池**：
#   pool_name=moonlight_ec_pool, pool_amount=60，每日由 global_daily_reset_time 重置；
#   发射器 MoonLight_Region / MoonLight_SharedSpaceMural(_create) **共享同一个池**。
#   收满 60 颗 `event_candle_days_of_moonlight_wax`（"代币烛火"）后
#   `get_forge_rates` 显示 `event_..._moonlight_wax → event_candle_days_of_moonlight`
#   的 `pending_candles = 5`（60 ÷ forge_cost 12）= **每日 5 个代币**（与游戏内一致）。
#
# ★ 不需要等 last_chunk_seq 前进。服务端**不预发射**，客户端直接按
#   (emitter_name, event_name, chunk_seq, pickup_seq) 请求即可，服务端按池记账：
#     r=ok            收到（cc=1）
#     r=already       这条本账号已收过（幂等）
#     r=collected_max 池已满（pool_amount - collected 用完）
#     r=inactive_event 该活动当前没开（例：global_fashion）
#   `collected` 是**按池共享**的计数（同 pool 的发射器读到同一个值）；
#   单次请求服务端最多回 32 项 ⇒ 必须按 32 切批。
# ★ 池按**账号 + 天**：对照号 84100d47 当天 moonlight_ec_pool collected=0，
#   本号已 60/60 —— 证明是每账号每天独立的额度。
SWEEP_CHUNKS = 64          # 单发射器最多扫 chunk_seq 0..63
MAX_DAILY_BATCHES = 140    # 每日烛火最多发多少批（32 项/批），防失控
_R_SKIP = ("collected_max", "inactive_event", "not_ready", "cooldown",
           "pool_empty", "unknown_pickup", "unknown_emitter")


def _num(v):
    for f in (int, float):
        try:
            return f(v)
        except Exception:
            pass
    return None


def _csa_max(ev) -> int:
    """chunk_size_amount → 该批里最多几个 pickup（[1,0,0] → 1）。"""
    v = ev.get("chunk_size_amount")
    if isinstance(v, dict):
        v = list(v.values())
    if isinstance(v, list):
        n = [int(x) for x in v if isinstance(x, (int, float))]
    elif isinstance(v, (int, float)):
        n = [int(v)]
    else:
        n = []
    return max(n) if n and max(n) > 0 else 1


def _daily_cfg(cfg: dict) -> dict:
    d = cfg.get("daily") if isinstance(cfg.get("daily"), dict) else {}
    try:
        sw = int(d.get("sweep_chunks"))
    except Exception:
        sw = SWEEP_CHUNKS
    return {
        "enabled": bool(d.get("enabled", True)),
        "include_wax": bool(d.get("include_wax", False)),
        "sweep_chunks": max(4, min(256, sw)),
        "skip_pools": [str(x) for x in (d.get("skip_pools") or [])],
    }


def _want_bases(cfg: dict, d: dict) -> set:
    """要收的货币（统一去掉 _wax 后比较）。默认 = 本活动的代币烛火。"""
    key = str(cfg.get("currency_key") or "")
    raw = [str(x) for x in (cfg.get("daily_currency_filter") or [])]
    if not raw and key:
        raw = [key]
    want = {_base(x) for x in raw if x}
    if d.get("include_wax"):
        want.add("wax")
    return want


def _get_emitters(client, level_id: int) -> list:
    """实时发射器表（权威）：POST /account/get_level_pickups {"level": id}。"""
    r = client.post("/account/get_level_pickups", {"level": level_id})
    ems = r.get("pickup_emitters") if isinstance(r, dict) else None
    return [e for e in ems if isinstance(e, dict)] if isinstance(ems, list) else []


def _daily_sweep(client, level_id: int, cfg: dict, log, stop_flag) -> dict:
    """按实时发射器表逐池扫「每日烛火」。已在图内才调用。"""
    d = _daily_cfg(cfg)
    out = {"ok": 0, "rows": 0, "skip": 0, "batches": 0, "cts": {}, "pools": {},
           "enabled": False, "reason": ""}
    if not d["enabled"]:
        out["reason"] = "daily.enabled=false"
        return out
    want = _want_bases(cfg, d)
    try:
        emitters = _get_emitters(client, level_id)
    except Exception as e:
        out["reason"] = "读实时发射器表失败：%s" % str(e)[:120]
        return out
    if not emitters:
        out["reason"] = "该图实时发射器表为空"
        return out
    out["enabled"] = True

    pools: dict = {}
    for e in emitters:
        name = str(e.get("emitter_name") or "")
        for ev in (e.get("events") or []):
            if not isinstance(ev, dict):
                continue
            ct = str(ev.get("currency_type") or "")
            if _base(ct) not in want:
                continue
            pool = str(ev.get("pool_name") or name)
            if pool in d["skip_pools"]:
                continue
            p = pools.setdefault(pool, {"amount": 0, "collected": 0, "units": []})
            p["amount"] = max(p["amount"], int(_num(ev.get("pool_amount")) or 0))
            p["collected"] = max(p["collected"], int(_num(ev.get("collected")) or 0))
            p["units"].append({"name": name, "event": str(ev.get("event_name") or ""),
                               "csa": _csa_max(ev), "ct": ct})
    if not pools:
        out["reason"] = "发射器表里没有目标货币（%s）" % "、".join(sorted(want))
        return out

    log("[活动代币] 每日烛火：%d 个池 → %s" % (len(pools), "、".join(
        "%s %d/%d" % (k, v["collected"], v["amount"]) for k, v in pools.items())))

    for pool, p in sorted(pools.items(), key=lambda kv: -kv[1]["amount"]):
        need = max(0, p["amount"] - p["collected"]) if p["amount"] else d["sweep_chunks"]
        if p["amount"] and need <= 0:
            out["pools"][pool] = {"amount": p["amount"], "collected": p["collected"],
                                  "need": 0, "ok": 0, "skipped": "当日池已满"}
            log("[活动代币] 池 %s 当日已满（%d/%d），跳过" % (pool, p["collected"], p["amount"]))
            continue
        p_ok, dead = 0, False
        for u in p["units"]:
            if dead or out["batches"] >= MAX_DAILY_BATCHES or (
                    stop_flag is not None and stop_flag.is_set()):
                break
            n_chunk = min(d["sweep_chunks"], need + 2)
            units = [(cs, ps) for cs in range(0, n_chunk) for ps in range(0, u["csa"])]
            u_ok = 0
            for i in range(0, len(units), BATCH_SIZE):
                if stop_flag is not None and stop_flag.is_set():
                    break
                ems = [{"emitter_name": u["name"], "event_name": u["event"],
                        "chunk_seq": cs, "pickup_seq": ps} for cs, ps in units[i:i + BATCH_SIZE]]
                try:
                    resp = client.collect_pickup_batch(level_id, [], emitters=ems)
                except Exception as e:
                    log("[活动代币] %s 批%d 异常：%s" % (u["name"], i // BATCH_SIZE + 1, str(e)[:120]))
                    break
                rows = resp.get("batch_results") if isinstance(resp, dict) else None
                rows = rows if isinstance(rows, list) else []
                out["batches"] += 1
                out["rows"] += len(rows)
                n_ok = 0
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    st = str(r.get("r") or "").lower()
                    if st in _R_OK:
                        n_ok += 1
                        ct = str(r.get("ct") or u["ct"])
                        out["cts"][ct] = out["cts"].get(ct, 0) + (int(_num(r.get("cc")) or 1))
                    else:
                        out["skip"] += 1
                u_ok += n_ok
                p_ok += n_ok
                out["ok"] += n_ok
                if n_ok == 0:            # 本池已收满 / 本条已收过
                    dead = True
                    break
                if p_ok >= need:
                    dead = True
                    break
                time.sleep(0.2)
            log("[活动代币] 池 %s · %s → +%d" % (pool, u["name"], u_ok))
        out["pools"][pool] = {"amount": p["amount"], "collected": p["collected"],
                              "need": need, "ok": p_ok}
    return out


def _forge_info(client, cfg: dict) -> dict:
    """get_forge_rates：本活动代币的 pending_candles（服务端记账的"待铸代币"）。"""
    key = str(cfg.get("currency_key") or "")
    out = {"pending_tokens": None, "forged_tokens": None, "pending_wax": None,
           "reset_acked": None, "cost": None, "raw": None}
    try:
        r = client.post("/account/get_forge_rates", {})
    except Exception:
        return out
    for x in (r.get("forge_rates") or []):
        if not isinstance(x, dict):
            continue
        st = str(x.get("source_type") or "")
        if key and st == key + "_wax":
            out["pending_tokens"] = _num(x.get("pending_candles"))
            out["forged_tokens"] = _num(x.get("candles_forged"))
            out["reset_acked"] = bool(x.get("reset_acknowledged"))
            out["raw"] = x
        elif st == "wax":
            out["pending_wax"] = _num(x.get("pending_candles"))
    return out


def run_tokens(client, log=_log, stop_flag: threading.Event | None = None, on_stale=None) -> dict:
    """收集当前活动代币：**先进图** → 收一次性 pickup_ids → 扫每日发射器池 → 钱包核验。

    on_stale：拉票失败的自动续期钩子（任务层注入，见 `session.enter_map`）。
    """
    cfg = load_event()
    if not cfg:
        return {"ok": False, "summary": "当前无活动代币配置（assets/event_tokens.json 缺失或格式错误）"}

    label = str(cfg.get("label") or cfg.get("event") or cfg.get("scene") or "活动")
    level_id = int(cfg.get("level_id") or cfg.get("map_id") or 0)
    join_id = int(cfg.get("join_level_id") or 0) or cnsession.CN_LEVEL_HASH
    ids = _int_ids(cfg.get("pickup_ids"))
    emitters = [dict(e) for e in (cfg.get("emitters") or []) if isinstance(e, dict)]
    if not ids and not emitters:
        return {"ok": False, "summary": "活动配置 pickup_ids 与 emitters 均为空"}

    bal0 = _event_balance(client, cfg)
    if bal0 is not None:
        log("[活动代币] %s 起始余额 %d（level_id=%d）" % (label, bal0, level_id))

    # ── ★ 必须先进图，否则 collect 恒返回 {} 空响应 ──
    #    活动图没有 join 模板 ⇒ session.enter_map 内部的「同图快速重进冷却」重试不生效，
    #    所以这里自己退避重试。
    #    ★ 2026-10-09 更正：早先这里写「拉票失败多为服务端限流，隔几秒会恢复」——**错的**。
    #    真根因是**会话被顶**（用户重开游戏让库里的 session 作废，见 22 篇 §10），
    #    退避重试再多次也不会好；正确做法是续期（重登 + replace_session）后重拉票，
    #    由 `on_stale` 钩子在 enter_map 内部完成（见 session.enter_map 的说明）。
    log("[活动代币] 进入活动图 level_id=%d（join=%d）…" % (level_id, join_id))
    ok, sid, msg = False, "", ""
    for attempt in range(1, 4):
        try:
            ok, sid, msg = cnsession.enter_map(client, level_id, join_level_id=join_id,
                                               on_stale=on_stale)
        except Exception as e:
            ok, msg = False, "异常：%s" % str(e)[:120]
        if ok:
            break
        log("[活动代币] 进图失败（第 %d/3 次）：%s" % (attempt, msg))
        if attempt < 3:
            time.sleep(4 * attempt)
    if not ok:
        fgf = _forge_info(client, cfg)
        return {"ok": False, "summary": "进图失败：%s（稍后重试）" % msg,
                "event": cfg.get("event"), "label": label, "level_id": level_id,
                "enter_msg": msg, "pending_tokens": fgf.get("pending_tokens"),
                "pending_wax": fgf.get("pending_wax")}
    log("[活动代币] 进图成功，开始拾取")

    got_ids: set = set()
    cts: dict = {}
    uniq: dict = {}
    batches = 0
    rows_total = 0
    dup_n = 0

    # 收集单元：先 pickup_ids 批，再 emitter 批
    units: list = [(ids[i:i + BATCH_SIZE], []) for i in range(0, len(ids), BATCH_SIZE)]
    units += [([], emitters[i:i + BATCH_SIZE]) for i in range(0, len(emitters), BATCH_SIZE)]

    def _send(bid, bems):
        return client.collect_pickup_batch(level_id, bid, emitters=bems)

    def _acc(resp, label):
        nonlocal batches, rows_total, dup_n
        n, rows = _tally(resp, got_ids, cts, uniq)
        batches += 1
        rows_total += len(rows)
        dup_n += sum(1 for r in rows if isinstance(r, dict)
                     and str(r.get("r") or "").lower() in _R_DUP)
        log("[活动代币] %s → ok %d（响应 %d 项）" % (label, n, len(rows)))
        return n, rows

    dstat: dict = {}
    ack_ok = False
    try:
        # ★ 就绪门：进图后服务端需短暂就绪（实测约 2s），期间 collect 返回 `{}` 空响应。
        #   空响应严格等于「未就绪」——已领取的图会回 already 逐项结果，不是空响应。
        pending = list(units)
        if pending:
            bid0, bems0 = pending[0]
            t_end = time.time() + READY_SEC
            tries = 0
            while True:
                tries += 1
                if stop_flag is not None and stop_flag.is_set():
                    log("[活动代币] 收到停止信号")
                    pending = []
                    break
                try:
                    resp0 = _send(bid0, bems0)
                except Exception as e:
                    log("[活动代币] 就绪探测异常：%s" % str(e)[:140])
                    resp0 = {}
                rows0 = resp0.get("batch_results") if isinstance(resp0, dict) else None
                if rows0:
                    log("[活动代币] 就绪（第 %d 次探测）" % tries)
                    _acc(resp0, "第 1 单元 %d 颗" % (len(bid0) or len(bems0)))
                    pending = pending[1:]
                    break
                if time.time() >= t_end:
                    log("[活动代币] 就绪超时（%ds，%d 次探测均空响应）" % (READY_SEC, tries))
                    pending = pending[1:]
                    break
                time.sleep(0.4)

        for idx, (bid, bems) in enumerate(pending, start=2):
            if stop_flag is not None and stop_flag.is_set():
                log("[活动代币] 收到停止信号")
                break
            try:
                resp = _send(bid, bems)
            except Exception as e:
                log("[活动代币] 第 %d 单元异常：%s" % (idx, str(e)[:140]))
                continue
            _acc(resp, "第 %d 单元 %d 颗" % (idx, len(bid) or len(bems)))
            time.sleep(0.2)   # 单元间轻冷却，避免触发风控

        # ── ★ 每日代币：扫实时发射器池（必须还在图内） ──
        try:
            dstat = _daily_sweep(client, level_id, cfg, log, stop_flag)
        except Exception as e:
            log("[活动代币] 每日烛火异常：%s" % str(e)[:160])
            dstat = {"ok": 0, "rows": 0, "batches": 0, "skip": 0, "cts": {}, "pools": {},
                     "enabled": False, "reason": "异常：%s" % str(e)[:120]}

        # 真客户端每天都会 ack 一次「合成速率重置」，服务端据此结算当天的 forge
        try:
            ack = client.post("/account/ack_forge_rate_reset", {})
            ack_ok = str(ack.get("result") or "") == "ok"
            log("[活动代币] ack_forge_rate_reset → %s" % (ack.get("result") or "?"))
        except Exception as e:
            ack_ok = False
            log("[活动代币] ack 合成速率重置失败：%s" % str(e)[:120])
    finally:
        if sid:
            try:
                cnsession.POOL.close(sid)
            except Exception:
                pass

    bal1 = _event_balance(client, cfg)
    delta = (bal1 - bal0) if (bal0 is not None and bal1 is not None) else None

    got = len(got_ids)
    bad = {k: v for k, v in uniq.items() if v != "already"}
    all_dup = bool(rows_total) and not got and not bad and dup_n == rows_total
    empty_resp = not rows_total and not dstat.get("rows")

    d_ok = int(dstat.get("ok") or 0)
    d_cts = dstat.get("cts") or {}
    key = str(cfg.get("currency_key") or "")
    wax_key = (key + "_wax") if key else ""
    wax_bal = _num(_currency_obj(client).get(wax_key)) if wax_key else None
    fg = _forge_info(client, cfg)
    pend = fg.get("pending_tokens")

    parts = []
    if got:
        parts.append("一次性代币 %d 颗" % got)
    elif all_dup:
        parts.append("一次性代币 今日已领取（%d 颗全 already，未重复入账）" % dup_n)
    elif empty_resp:
        parts.append("代币未收到数据（进图后 collect 仍空响应：活动未开放或该图无此拾取物）")
    else:
        parts.append("一次性代币 0 颗")
    if d_ok:
        tgt = _pretty_ct(wax_key, cfg) if wax_key else "每日烛火"
        parts.append("★每日%s +%d 颗" % (tgt, d_ok))
    elif dstat.get("enabled"):
        parts.append("每日烛火 0 颗（当日池已满或已收过）")
    elif dstat.get("reason"):
        parts.append("每日烛火未执行（%s）" % dstat["reason"])
    all_cts = dict(cts)
    for k, v in d_cts.items():
        all_cts[k] = all_cts.get(k, 0) + v
    if all_cts:
        parts.append(" · ".join("%s×%d" % (_pretty_ct(str(k), cfg), v)
                                for k, v in sorted(all_cts.items(), key=lambda kv: -kv[1])))
    if wax_bal is not None and wax_key:
        parts.append("%s 余额 %s" % (_pretty_ct(wax_key, cfg), wax_bal))
    if pend:
        parts.append("待铸代币 %s 个" % pend)
    if fg.get("pending_wax"):
        parts.append("待铸蜡烛 %s 根" % fg["pending_wax"])
    if delta is not None:
        parts.append("余额 %d → %d（+%d）" % (bal0, bal1, delta))
    if bad:
        sample = list(bad.items())[:4]
        parts.append("异常 %d 项（例：%s）" % (len(bad), "、".join("%s=%s" % (k, v) for k, v in sample)))

    # ok 语义：真收到（一次性或每日）/ 钱包入账 / 逐项已领取（幂等成功）都算成功；
    #          只有空响应或纯异常才算失败。
    ok = bool(got or d_ok or (delta is not None and delta > 0) or all_dup)

    summary = {
        "ok": ok,
        "summary": " · ".join(parts),
        "event": cfg.get("event"),
        "label": label,
        "level_id": level_id,
        "pickup_total": len(ids),
        "pickup_got": got,
        "pickup_already": dup_n,
        "rows_total": rows_total,
        "empty_response": empty_resp,
        "emitters_total": len(emitters),
        "batches": batches,
        "cts": cts,
        "bad": {str(k): v for k, v in bad.items()},
        "balance_before": bal0,
        "balance_after": bal1,
        "delta": delta,
        # ── 每日代币（挂机烛火）──
        "daily_enabled": bool(dstat.get("enabled")),
        "daily_reason": dstat.get("reason") or "",
        "daily_ok": d_ok,
        "daily_rows": int(dstat.get("rows") or 0),
        "daily_skip": int(dstat.get("skip") or 0),
        "daily_batches": int(dstat.get("batches") or 0),
        "daily_pools": dstat.get("pools") or {},
        "daily_cts": d_cts,
        "token_wax_key": wax_key,
        "token_wax_balance": wax_bal,
        "pending_tokens": pend,
        "forged_tokens": fg.get("forged_tokens"),
        "pending_wax": fg.get("pending_wax"),
        "forge_reset_acked": fg.get("reset_acked"),
        "forge_ack_sent": ack_ok,
        "currency": _currency_obj(client),
    }
    log("[活动代币] %s" % summary["summary"])
    return summary
