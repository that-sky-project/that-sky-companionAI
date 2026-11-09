# -*- coding: utf-8 -*-
"""在线跑图（pickup·收烛火）国服编排。移植 PickupApi.kt 国服分支。

- 每图：enterMap（保活 UDP 在线）→ 就绪探针（反复 collect 探到非空）→ 逐批 collect → 染料（可选）→ leaveMap
- 就绪探针 22s / 0.4s 间隔，每轮试 4 颗；collect 单批 ≤32；图间冷却 0.35s；断图重试 2 次。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import joingame, mapcfg, session as cnsession
from . import tuning
from .skyapi import SkyClient, SkyApiError
from .util import derive_peer_key  # noqa: F401 (re-export供 tests)

COLLECT_PATH = "/account/collect_pickup_batch"

# 下面是**出厂默认值**（也是 /api/admin/route-rate「标准档」的取值）。
# 运行时一律走 _tune() → tuning.pickup()，管理员能在面板上实时调。
# 保留这些常量只为兼容旧引用（测试/文档还在用），别再拿它们当"当前生效值"。
DYE_BATCH_SIZE = tuning.FIELDS["dye_batch"][0]
BETWEEN_MAPS_COOLDOWN_MS = tuning.FIELDS["between_maps"][0]
READY_PROBE_TIMEOUT_MS = tuning.FIELDS["ready_timeout"][0]
READY_PROBE_INTERVAL_MS = tuning.FIELDS["ready_interval"][0]
PROBE_IDS_PER_ROUND = tuning.FIELDS["probe_ids"][0]
MAP_ENTER_ATTEMPTS = tuning.FIELDS["enter_attempts"][0]


def _tune() -> dict:
    """跑图每一步的节奏。每次现读（tuning 内部 2 秒缓存），管理员改完即时生效。"""
    return tuning.pickup()

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
_routes_lock = threading.Lock()
_cached_routes: list[dict] | None = None
_cached_dye: dict[int, list[list[int]]] | None = None


def load_routes() -> list[dict]:
    global _cached_routes
    with _routes_lock:
        if _cached_routes is None:
            with open(os.path.join(_ASSETS_DIR, "pickup_routes.json"), encoding="utf-8") as f:
                data = json.load(f)
            routes = data if isinstance(data, list) else data.get("routes") or []
            if not isinstance(routes, list):
                routes = []
            _cached_routes = mapcfg.apply_routes(routes)
        return _cached_routes


def reload_routes() -> None:
    """清路线缓存。管理员在面板上改了 map_id / join_level_id 后必须调 —— 否则跑图还用旧表。"""
    global _cached_routes
    with _routes_lock:
        _cached_routes = None


def load_dye() -> dict[int, list]:
    """染料节点表：{map_id: [pickup_id, ...]}。惰性加载 + 缓存。

    ★ 2026-09-18 修：原实现 `table[int(k)] = [list(x) for x in v]`，而 dye_nodes.json
      里每个节点是**纯整数**（如 53721727）—— `list(53721727)` 抛 TypeError，
      又被外层 `except Exception: continue` **逐图静默吞掉**，整张 22 图 / 8683 颗的表
      最终是**空的**。`_collect_dye` 第一句 `table.get(map_id)` 取不到东西 → 直接
      return 0、一个请求都不发 → 用户反馈「收集染料没效果」。
      现在：整数与列表两种结构都认；解析后为空要**吼一声**（这种"静默全丢"正是本 bug
      藏了这么久的原因）。
    """
    global _cached_dye
    with _routes_lock:
        if _cached_dye is None:
            path = os.path.join(_ASSETS_DIR, "dye_nodes.json")
            table: dict[int, list] = {}
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print("[pickup] 染料表读取失败 %s: %r" % (path, e), flush=True)
                raw = {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        mid = int(k)
                    except Exception:
                        continue
                    nodes: list = []
                    for x in (v or []):
                        if isinstance(x, bool):
                            continue
                        if isinstance(x, int):
                            nodes.append(x)                         # ← 本项目实际结构
                        elif isinstance(x, str):
                            try:
                                nodes.append(int(x))
                            except Exception:
                                pass
                        elif isinstance(x, (list, tuple)):
                            try:
                                nodes.append([int(y) for y in x])   # 兼容另一种结构
                            except Exception:
                                pass
                    if nodes:
                        table[mid] = nodes
            if not table:
                print("[pickup] ⚠️ 染料表解析后为空（%s）—— 染料收集会全程跳过，请检查文件结构"
                      % path, flush=True)
            _cached_dye = table
        return _cached_dye


def route_total(route: dict) -> int:
    return sum(len(b) for b in route.get("batches", []))


def _batch_results(resp: dict) -> list | None:
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    arr = data.get("batch_results")
    if isinstance(arr, list) and arr:
        return arr
    return None


def _tally(batch_results: list) -> int:
    got = 0
    for item in batch_results:
        if isinstance(item, dict) and item.get("r") in ("ok", "already"):
            got += 1
    return got


def _tally_ct(batch_results: list) -> dict:
    """按货币类型（batch_results[].ct 字段）统计入账颗数；r in (ok, already) 都算，无 ct 记 '?'。"""
    out: dict[str, int] = {}
    for item in batch_results:
        if isinstance(item, dict) and item.get("r") in ("ok", "already"):
            ct = str(item.get("ct") or "?")
            out[ct] = out.get(ct, 0) + 1
    return out


def _ct_base(ct: str) -> str:
    return ct[:-4] if ct.endswith("_wax") else ct


def _tally_wax(batch_results: list) -> int:
    """烛光（wax）真实入账量 = Σcc（batch_results[].cc = 该 item 实际入账数量，缺省按 1）。

    只认 r=ok 新收；already 是此前已收过、不重复入账。见知识库 18/43 真实样本：
    {"w":83651,"r":"ok","id":847203,"ct":"wax",...,"cc":1}。
    """
    total = 0
    for item in batch_results:
        if not isinstance(item, dict) or item.get("r") != "ok":
            continue
        if _ct_base(str(item.get("ct") or "")) != "wax":
            continue
        cc = item.get("cc")
        try:
            total += int(cc) if cc is not None else 1
        except (TypeError, ValueError):
            total += 1
    return total


def _wait_map_ready(client: SkyClient, route: dict, log) -> tuple[bool, int, bool]:
    """就绪探针：反复 collect 探到非空 batch_results 才算真进图。

    返回 (ready, wax, empty200)：探针轮次 collect 与正式 collect 用同一批 id，探针里 r=ok 的
    烛光已真实入账（正式轮会变 already），不补记就漏账。
    empty200=True = 全程 HTTP 200 空响应——服务端更新场景后暂未配置该图蜡烛
    （09-16 实测：小镇/晨岛/幽光山洞/音乐大厅进图成功但 collect 一律 200 空 body），
    上层按「跳过（未配置）」处理，不判失败。
    """
    pool = route.get("batches") or []
    if not pool:
        return False, 0, False
    ids = [p for b in pool for p in b][:64]
    t = _tune()
    deadline = time.monotonic() + t["ready_timeout"]
    i = 0
    all_empty = True
    while time.monotonic() < deadline:
        probe = ids[i * t["probe_ids"]: (i + 1) * t["probe_ids"]]
        i += 1
        if not probe:
            i = 0
            continue
        code, resp = _collect(client, route["map_id"], probe)
        results = _batch_results(resp) if resp else None
        if results:
            return True, _tally_wax(results), False
        if not (code == 200 and not resp):
            all_empty = False
        time.sleep(t["ready_interval"])
    return False, 0, all_empty


def _collect(client: SkyClient, map_id: int, ids: list[int]) -> tuple[int | None, dict]:
    try:
        return 200, client.collect_pickup_batch(map_id, ids)
    except SkyApiError as e:
        return e.status, {}
    except Exception:
        return None, {}


def _collect_batch_list(client: SkyClient, map_id: int, batches: list[list[int]], log) -> tuple[int, int, int, dict]:
    """逐批 collect。返回 (got 颗, wax 烛光, failed_batches, cts 按货币类型颗数)。"""
    got = 0
    wax = 0
    failed = 0
    cts: dict[str, int] = {}
    for bi, batch in enumerate(batches):
        if not batch:
            continue
        code, resp = _collect(client, map_id, batch)
        results = _batch_results(resp) if resp else None
        if results:
            got += _tally(results)
            wax += _tally_wax(results)
            for ct, n in _tally_ct(results).items():
                cts[ct] = cts.get(ct, 0) + n
            log("    批 %d/%d：HTTP %s，%d/%d 颗入账" % (bi + 1, len(batches), code, _tally(results), len(batch)))
        else:
            failed += 1
            log("    批 %d/%d：HTTP %s，空响应（跳过）" % (bi + 1, len(batches), code))
        time.sleep(_tune()["batch_gap"])
    return got, wax, failed, cts


def _collect_dye(client: SkyClient, map_id: int, log) -> int:
    table = load_dye()
    nodes = table.get(int(map_id)) or []
    if not nodes:
        return 0
    got = 0
    failed = 0
    batches = [nodes[i:i + _tune()["dye_batch"]] for i in range(0, len(nodes), _tune()["dye_batch"])]
    for bi, batch in enumerate(batches):
        # 染料 collect 同接口：id 列表走 pickup_ids（与客户端 PickupApi 一致）
        code, resp = _collect(client, map_id, batch)
        results = _batch_results(resp) if resp else None
        if results:
            got += _tally(results)
        else:
            failed += 1
            log("    染料批 %d/%d：HTTP %s，空响应" % (bi + 1, len(batches), code))
        time.sleep(_tune()["batch_gap"])
    log("    染料：%d 批 %d 颗，入账 %d，失败批 %d" % (len(batches), len(nodes), got, failed))
    return got


LIVE_BATCH_SIZE = tuning.FIELDS["live_batch"][0]   # 出厂默认值，实际走 _tune()


def _live_pickups(client: SkyClient, map_id: int) -> tuple[list[int], int, str]:
    """get_level_pickups 实时清单（静态路线表含过期死 id，真实收益走实时）。

    pickups[] 每项 {id, wait_sec}：wait_sec==0 = 当前可收；>0 = 已收取、wait_sec 秒后刷新
    （实测：已收光的图 259 项 wait_sec 全相同，collect 一律 already）。
    返回 (可收 id 列表, 清单总项数, note)；note="为空"/"获取失败(原因)" 时回退静态表。
    """
    try:
        resp = client.post("/account/get_level_pickups", {"level": map_id},
                           include_level_id=True, level_id=map_id)
    except Exception as e:
        return [], 0, "获取失败(%s)" % (str(e)[:80])
    d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    pks = d.get("pickups") if isinstance(d, dict) else None
    if not isinstance(pks, list):
        return [], 0, "获取失败(响应无 pickups)"
    if not pks:
        return [], 0, "为空"
    ready: list[int] = []
    for it in pks:
        if not isinstance(it, dict):
            continue
        pid = it.get("id")
        if isinstance(pid, int) and pid > 0 and int(it.get("wait_sec") or 0) == 0:
            ready.append(pid)
    return ready, len(pks), ""


def run_pickup(
    client: SkyClient,
    scenes: list[str] | None = None,
    collect_dye: bool = False,
    log=lambda m: None,
    stop_flag: threading.Event | None = None,
    routes_override: list[dict] | None = None,
    on_stale=None,
) -> dict:
    """跑全部（或指定）图。返回统计摘要。

    routes_override：直接使用给定路线（不走 pickup_routes.json、忽略 scenes），
    供活动代币等外部路线（assets/event_tokens.json）复用进图/探针/收集链路。

    on_stale：交给 `session.enter_map` 的**拉票失败自动续期钩子**（任务层注入）。
    跑图是一跑几十分钟的长任务，中途会话被顶掉（用户重开游戏）后每一图都会
    「拉票失败」——钩子续期成功后用同一个 client 继续跑，不必整轮重来。
    """
    if routes_override is not None:
        routes = routes_override
    else:
        routes = load_routes()
        if scenes:
            want = set(scenes)
            routes = [r for r in routes if r.get("scene") in want or r.get("scene_zh") in want]
    build = cnsession.resolve_build(client.client_version)
    summary = {
        "maps_total": len(routes),
        "maps_ok": 0,
        "maps_fail": 0,
        "nodes_total": 0,
        "nodes_got": 0,
        "wax_got": 0,
        "dye_maps": 0,
        "dye_got": 0,
        "cts": {},
        "per_map": [],
    }
    for ri, route in enumerate(routes):
        if stop_flag is not None and stop_flag.is_set():
            log("收到停止信号，终止跑图")
            break
        scene = str(route.get("scene", "?"))
        scene_zh = str(route.get("scene_zh") or scene)
        map_id = int(route["map_id"])
        join_id = int(route.get("join_level_id", 0)) or cnsession._p("lh", cnsession.CN_LEVEL_HASH)
        static_batches = route.get("batches") or []
        static_total = sum(len(b) for b in static_batches)

        # 实时清单优先：静态表含过期死 id；wait_sec>0 = 已收取待刷新（本日已收满）
        if routes_override is None:
            ready_ids, live_total, live_note = _live_pickups(client, map_id)
        else:
            ready_ids, live_total, live_note = [], 0, ""  # 外部固定路线（如活动代币 guids）不走实时
        if ready_ids:
            batches = [ready_ids[i:i + _tune()["live_batch"]] for i in range(0, len(ready_ids), _tune()["live_batch"])]
            node_total = len(ready_ids)
            src = "实时"
        elif live_total > 0:
            # 清单非空但全部 wait_sec>0（本日已收完）→ 不进图，跳过
            log("[%d/%d] %s（%s）map_id=%s 实时 %d 颗均已收取，跳过" % (
                ri + 1, len(routes), scene_zh, scene, map_id, live_total))
            summary["maps_ok"] += 1
            summary["per_map"].append({"scene": scene, "scene_zh": scene_zh, "map_id": map_id,
                                       "got": 0, "wax": 0, "ok": True, "cts": {}, "src": "实时", "skip": "cleared"})
            time.sleep(_tune()["between_maps"])
            continue
        else:
            batches = static_batches
            node_total = static_total
            src = "指定" if routes_override is not None else "静态"
        if not batches:
            log("[%d/%d] %s（%s）map_id=%s 无可用清单，跳过" % (
                ri + 1, len(routes), scene_zh, scene, map_id))
            summary["maps_ok"] += 1
            summary["per_map"].append({"scene": scene, "scene_zh": scene_zh, "map_id": map_id,
                                       "got": 0, "wax": 0, "ok": True, "cts": {}, "src": src, "skip": "empty"})
            time.sleep(_tune()["between_maps"])
            continue
        summary["nodes_total"] += node_total
        log("[%d/%d] %s（%s）map_id=%s %s清单 %d 颗" % (
            ri + 1, len(routes), scene_zh, scene, map_id, src, node_total))
        if src == "实时" and live_total > node_total:
            log("    （API 共 %d 项，其余 wait_sec>0 为已收取）" % live_total)

        got_total = 0
        wax_total = 0
        cts: dict[str, int] = {}
        map_ok = False
        no_pickups = False
        for attempt in range(1, _tune()["enter_attempts"] + 1):
            ok, sid, msg = cnsession.enter_map(client, map_id, join_level_id=join_id,
                                               build_number=build, on_stale=on_stale)
            if not ok:
                log("    进图失败（第 %d 次）：%s" % (attempt, msg))
                time.sleep(0.6)
                continue
            try:
                ready, probe_wax, empty200 = _wait_map_ready(client, {"map_id": map_id, "batches": batches}, log)
                if not ready and empty200:
                    # 服务端更新场景后暂未配置该图蜡烛：collect 一律 200 空响应，按跳过处理
                    log("    服务端暂未配置该图蜡烛（collect 空响应），跳过")
                    no_pickups = True
                    break
                if not ready:
                    log("    进图未就绪（22s 探针无非空 collect）")
                    continue
                got, wax, failed, cts = _collect_batch_list(client, map_id, batches, log)
                got_total += got
                wax_total += wax + probe_wax
                if collect_dye:
                    dg = _collect_dye(client, map_id, log)
                    summary["dye_got"] += dg
                map_ok = True
                break
            finally:
                cnsession.POOL.close(sid)
        if no_pickups:
            summary["maps_ok"] += 1
            summary["per_map"].append({"scene": scene, "scene_zh": scene_zh, "map_id": map_id,
                                       "got": 0, "wax": 0, "ok": True, "cts": {}, "src": src,
                                       "skip": "no_pickups"})
            time.sleep(_tune()["between_maps"])
            continue
        summary["wax_got"] += wax_total
        if map_ok:
            summary["maps_ok"] += 1
            summary["nodes_got"] += got_total
            for ct, n in cts.items():
                summary["cts"][ct] = summary["cts"].get(ct, 0) + n
            summary["per_map"].append({"scene": scene, "scene_zh": scene_zh, "map_id": map_id,
                                       "got": got_total, "wax": wax_total, "ok": True, "cts": cts, "src": src})
            log("    完成：%d/%d 颗入账 · %d 烛光" % (got_total, node_total, wax_total))
        else:
            summary["maps_fail"] += 1
            summary["per_map"].append({"scene": scene, "scene_zh": scene_zh, "map_id": map_id,
                                       "got": got_total, "wax": wax_total, "ok": False, "cts": cts, "src": src})
            log("    本图失败")
        time.sleep(_tune()["between_maps"])
    return summary
