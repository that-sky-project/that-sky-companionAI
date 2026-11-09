# -*- coding: utf-8 -*-
"""活动任务（活动中心 jingling 桥）· **只完成、不领奖**。

★ 2026-10-04 通道打通（详见 `docs/0.16.5_412659知识库/23_精灵任务与活动中心路由定案.md` 第 9 节）
------------------------------------------------------------------
先前判定的「REST 侧做不了」是**误判**：那批 404 是我们自己缺字段造成的。真相是三个字段

  · 请求体 = `{resource, content:<**字符串**化 JSON>, callback_id:<随机 int>}`
      - `callback_id` **必填**；缺失 → 服务端直接 HTTP 404（空 body）
      - `content` 传对象（不是字符串）→ HTTP 200 + 业务码 500
  · 国服整体 RC4(session2) 包 `{"user","body":base64}`（`skyapi.SkyClient.post` 已实现）
  · 响应外层可能是 `{"web_response":"<json 串>"}`，业务码在**里层**

三个资源（2026-10-04 生产机位实测）：

  · `GET  /internal/jingling/get_events_overview`  `{}`              → 全量活动表
  · `GET  /internal/jingling/get_event_detail`     `{"event":<事件>}` → **权威任务表**
  · `SET  /internal/jingling/set_player_task`      `{"source_token":"","source_id":"","task":<id>,"value":<n>}`
                                                                    → `{"value":N,"finished":bool}`

★ 五条硬规则（全部踩过，别再犯）
------------------------------------------------------------------
 1. **`value` 是累加、不是置位**，且服务端**不封顶**（实测发 999 就存 999）
    ⇒ 一律按**差值**写：`delta = max(stages) - value`；`delta <= 0` 直接算已达标。
 2. **同 (resource, content) 短时间内重发 → 业务码 401 `repeat request`**（服务端去重）
    ⇒ detail 带 TTL 缓存；撞上 repeat 先 sleep 再试一次。
 3. **任务 id 绝不猜**，一律以 `get_event_detail` 返回的表为准 —— 事件 b 那期叫
    `collab_minecraft_prairie_day_3`，跟 a/ugc 的 `_mN` **完全不同命名**。
 4. **记账型任务改不了**：`collecting_candles` / `login_days` → `400 invalid task`，
    只能游戏内完成 ⇒ 归「需游戏内」，**不当失败、不重试**。
 5. **零领奖**：本模块只有一个写资源 `set_player_task`；`claim_jingling_mission_reward`
    在国服**根本不存在**（404），也不该调用（用户 2026-10-03 明确：只完成、不领奖）。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time

from .skyapi import SkyApiError

# ── 桥与资源 ────────────────────────────────────────────────────────────────
WEB_PATH = "/account/jingling/process_web_request"    # H5 桥的唯一入口（服务器代 H5 转发资源请求）
R_OVER = "/internal/jingling/get_events_overview"     # 只读：全量活动表
R_DETAIL = "/internal/jingling/get_event_detail"      # 只读：某活动的权威任务表
R_SET = "/internal/jingling/set_player_task"          # 唯一写资源：把任务进度顶上去

# 防御性断言：资源名里出现这些字样一律拒绝（见文件头规则 5）
FORBIDDEN_IN_RESOURCE = ("claim", "_reward")

# 首期白名单 = 0.16.5 MC 联动的三个子活动（2026-10-04 用户指定要做的那一期）
DEFAULT_EVENTS = (
    "activitycenter_collab_minecraft_a",              # 心火相传
    "activitycenter_collab_minecraft_b",              # 方块奇遇
    "activitycenter_ugc_create_collab_minecraft",     # 筑梦金苹果
)
# 整期模式（all=true）的家族前缀：MC 联动全家桶（含父活动与商店活动）
FAMILY_PREFIXES = ("activitycenter_collab_minecraft", "activitycenter_ugc_create_collab_minecraft")

# 记账型任务：服务端**一律 400 invalid task**，只能游戏内完成。
# 不去空打（省一次请求），但**只当「跳过」**，绝不当失败。
NEVER_SETTABLE = ("collecting_candles", "login_days")

SET_PAUSE = 0.35        # 单条写入间隔：给网关留余量
MAX_EVENTS = 12         # 单次跑动活动数硬上限（防跑飞）
MAX_TASKS = 60          # 单次跑动任务条数硬上限
DETAIL_TTL = 20.0       # detail 只读缓存秒数（躲 401 repeat request）

# 网关级拒绝：撞上说明是会话/限流/端点问题，不是单个任务的问题 → 立刻收手
GATE_STATUS = (401, 403, 418, 422, 423, 429, 500, 502, 503, 504)
# 业务码级收手信号：401 repeat request 是去重（重试一次即可），不算收手
BUSINESS_GATE = (403, 423, 429)


class ActivityError(Exception):
    """活动桥的业务错误：带 HTTP 状态或业务码，便于上层分类。"""

    def __init__(self, msg: str, status: int | None = None, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code


_DETAIL_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_CACHE_LOCK = threading.Lock()


# ── 桥调用 ──────────────────────────────────────────────────────────────────
def _unwrap(resp) -> dict:
    """拆外层 `web_response` 串；里层才是业务对象。"""
    if not isinstance(resp, dict):
        return {"code": None, "msg": "响应不是对象"}
    wr = resp.get("web_response")
    if isinstance(wr, str) and wr.strip():
        try:
            inner = json.loads(wr)
        except Exception:
            return {"code": None, "msg": "web_response 不是 JSON", "_raw": wr[:200]}
        if isinstance(inner, dict):
            return inner
    return resp


def web_call(client, resource: str, content: dict, attempts: int = 2) -> dict:
    """H5 桥一次调用：content 串化 + callback_id 必填（见文件头，缺一个就是 404）。"""
    for bad in FORBIDDEN_IN_RESOURCE:
        if bad in resource:
            raise ActivityError("拒绝调用领奖类资源：%s" % resource)
    body = {
        "resource": resource,
        # ★ 必须是**字符串**：传对象 → 200 + 业务码 500
        "content": json.dumps(content, separators=(",", ":"), ensure_ascii=False),
        # ★ 必填随机 int：缺失 → HTTP 404（空 body）
        "callback_id": random.randint(-(2 ** 31), 2 ** 31 - 1),
    }
    last: dict = {}
    for i in range(max(1, attempts)):
        try:
            last = _unwrap(client.post(WEB_PATH, body))
        except SkyApiError as e:
            raise ActivityError("HTTP %s" % e.status, status=e.status)
        # 业务码 401 repeat request = 服务端去重（同 resource+content 短时间重发）→ 等一下再来
        if last.get("code") == 401 and "repeat" in str(last.get("msg") or "").lower():
            if i + 1 < attempts:
                time.sleep(1.0)
                continue
        return last
    return last


def _code(inner) -> int | None:
    try:
        c = inner.get("code")
        return int(c) if c is not None else None
    except (TypeError, ValueError):
        return None


def _msg(inner) -> str:
    return str((inner or {}).get("msg") or "")


def _find_progress(obj) -> dict:
    """在回执里找第一个带 `finished`/`value` 的字典（写回执的进度块）。"""
    if isinstance(obj, dict):
        if "finished" in obj or "value" in obj:
            keys = ("value", "finished", "stages", "status", "task_id")
            if any(k in obj for k in keys):
                return obj
        for v in obj.values():
            got = _find_progress(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_progress(v)
            if got:
                return got
    return {}


def _int(v, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _stages(t: dict) -> list[int]:
    out = []
    for x in (t.get("stages") or []):
        if isinstance(x, bool):
            continue
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out or [1]


def _target_of(t: dict) -> int:
    return max(_stages(t))


# ── 只读：活动表 / 任务表 ───────────────────────────────────────────────────
def events_overview(client) -> dict:
    """全量活动表 → `{"event_data": {...}, "current_time": int, ...}`（data 段原样回）。"""
    inner = web_call(client, R_OVER, {})
    if _code(inner) != 200:
        raise ActivityError("活动总览失败：code=%s %s" % (_code(inner), _msg(inner)),
                            code=_code(inner))
    return inner.get("data") or {}


def is_live(info: dict, now: int) -> bool:
    """上线中 = end_time 在未来。注意总览里的「永久」活动 end_time 是个很大的数（2214 年）。"""
    return _int((info or {}).get("end_time"), 0) > now


def event_detail(client, event: str, acc_id: str = "", use_cache: bool = True) -> list[dict]:
    """某活动的**权威任务表**。带 TTL 缓存（躲 401 repeat request）。"""
    key = (str(acc_id or ""), str(event))
    if use_cache:
        with _CACHE_LOCK:
            hit = _DETAIL_CACHE.get(key)
        if hit and (time.time() - hit[0]) < DETAIL_TTL:
            return hit[1]
    inner = web_call(client, R_DETAIL, {"event": event})
    code = _code(inner)
    if code != 200:
        raise ActivityError("任务表读取失败（%s）：code=%s %s" % (event, code, _msg(inner)),
                            code=code)
    tasks = ((inner.get("data") or {}).get("event_data") or {}).get(event) or []
    if not isinstance(tasks, list):
        tasks = []
    if use_cache:
        with _CACHE_LOCK:
            _DETAIL_CACHE[key] = (time.time(), tasks)
    return tasks


def clear_cache(acc_id: str = "") -> None:
    """跑完一次就清缓存，避免下一次跑动读到旧进度。"""
    with _CACHE_LOCK:
        if not acc_id:
            _DETAIL_CACHE.clear()
            return
        for k in [k for k in _DETAIL_CACHE if k[0] == str(acc_id)]:
            _DETAIL_CACHE.pop(k, None)


def plan_event(client, event: str, acc_id: str = "") -> list[dict]:
    """把一个活动的任务表算成「要做什么」的清单（**只读，不写**）。

    每条：`{task_id, value, target, stages, state, delta}`
      state = 已达标 | 待完成 | 需游戏内 | 非活跃
    """
    rows: list[dict] = []
    for t in event_detail(client, event, acc_id=acc_id):
        tid = str(t.get("task_id") or "").strip()
        if not tid:
            continue
        cur = _int(t.get("value"), 0)
        target = _target_of(t)
        stages = _stages(t)
        status = str(t.get("status") or "").strip()
        if status and status != "active":
            state, delta = "非活跃", 0
        elif cur >= target:
            state, delta = "已达标", 0
        elif tid in NEVER_SETTABLE:
            # 规则 4：记账型 → 只能游戏内完成
            state, delta = "需游戏内", 0
        else:
            state, delta = "待完成", target - cur
        rows.append({"event": event, "task_id": tid, "value": cur, "target": target,
                     "stages": stages, "status": status, "state": state, "delta": delta})
    return rows


def overview_ui(client, events=None, acc_id: str = "") -> dict:
    """给面板的只读清单：白名单（或指定）活动 + 每期任务现状。**不写任何东西。**"""
    data = events_overview(client)
    ov = data.get("event_data") or {}
    now = _int(data.get("current_time"), int(time.time()))
    want = list(events) if events else list(DEFAULT_EVENTS)
    out = []
    for ev in want:
        info = ov.get(ev)
        if not info:
            out.append({"event": ev, "live": False, "missing": True, "tasks": [],
                        "summary": "服务端未返回该活动"})
            continue
        item = {"event": ev, "live": is_live(info, now),
                "is_new": _int(info.get("is_new"), 0),
                "unclaimed": _int(info.get("has_unclaimed_reward"), 0),
                "end_time": _int(info.get("end_time"), 0), "tasks": [], "summary": ""}
        if item["live"]:
            try:
                rows = plan_event(client, ev, acc_id=acc_id)
            except ActivityError as e:
                item["summary"] = "任务表读取失败：%s" % e
                out.append(item)
                continue
            item["tasks"] = [{"task_id": r["task_id"], "value": r["value"],
                              "target": r["target"], "state": r["state"]} for r in rows]
            c = {k: sum(1 for r in rows if r["state"] == k)
                 for k in ("待完成", "已达标", "需游戏内", "非活跃")}
            item["summary"] = ("任务 %d 条 · 已达标 %d · 待完成 %d · 需游戏内 %d"
                               % (len(rows), c["已达标"], c["待完成"], c["需游戏内"]))
        else:
            item["summary"] = "未上线"
        out.append(item)
    return {"now": now, "events": out}


# ── 写：单条任务 ────────────────────────────────────────────────────────────
def set_task(client, task_id: str, delta: int) -> tuple[int | None, dict, str]:
    """把某任务进度**加上** delta（服务端是累加语义）→ `(code, {value,finished}, msg)`。"""
    inner = web_call(client, R_SET, {"source_token": "", "source_id": "",
                                     "task": task_id, "value": int(delta)})
    code = _code(inner)
    if code == 200:
        return code, _find_progress(inner), _msg(inner)
    return code, {}, _msg(inner)


# ── 跑一期 / 跑整批 ─────────────────────────────────────────────────────────
def _resolve_events(ov: dict, now: int, event: str, all_in: bool, log) -> list[str]:
    if event:
        info = ov.get(event)
        if not info:
            raise ActivityError("服务端没有这个活动：%s" % event)
        if not is_live(info, now):
            raise ActivityError("该活动未上线：%s" % event)
        return [event]
    if all_in:
        got = [k for k, v in ov.items()
               if k.startswith(FAMILY_PREFIXES) and is_live(v or {}, now)]
        got.sort(key=lambda k: (0 if "ugc" in k else 1, k))
    else:
        got = [k for k in DEFAULT_EVENTS if k in ov and is_live(ov.get(k) or {}, now)]
        for k in DEFAULT_EVENTS:
            if k not in ov:
                log("[活动任务] 服务端总览里没有 %s（可能已下线）→ 跳过" % k)
            elif not is_live(ov.get(k) or {}, now):
                log("[活动任务] %s 已下线（end_time 已过）→ 跳过" % k)
    return got[:MAX_EVENTS]


def run_activity(client, log=lambda m: None, stop_flag: threading.Event | None = None,
                 preview: bool = False, event: str = "", all_in: bool = False,
                 acc_id: str = "") -> dict:
    """跑「活动任务」：读活动表 → 读权威任务表 → 按差值把任务进度顶到 `max(stages)`。

    ★ 只完成、零领奖：全程只有一个写资源 `set_player_task`。
    preview=True 只出清单，不发任何写请求。
    """
    t0 = time.time()
    stopped = False
    try:
        data = events_overview(client)
    except ActivityError as e:
        log("[活动任务] 总览读取失败：%s" % e)
        return {"ok": False, "summary": "活动总览读取失败：%s" % e, "succ": 0, "fail": 1,
                "total": 0, "skip": 0, "already": 0, "manual": 0, "rows": [],
                "preview": preview, "stopped": False, "gate": ""}

    ov = data.get("event_data") or {}
    now = _int(data.get("current_time"), int(time.time()))
    live_all = sorted([k for k, v in ov.items() if is_live(v or {}, now)])
    log("[活动任务] 上线中活动 %d 个：%s" % (len(live_all), "、".join(live_all[:8])))

    try:
        events = _resolve_events(ov, now, event, all_in, log)
    except ActivityError as e:
        log("[活动任务] %s" % e)
        return {"ok": False, "summary": str(e), "succ": 0, "fail": 1, "total": 0,
                "skip": 0, "already": 0, "manual": 0, "rows": [], "preview": preview,
                "stopped": False, "gate": ""}
    if not events:
        log("[活动任务] 没有要跑的活动（白名单都不在线）")
        return {"ok": True, "summary": "没有要跑的活动", "succ": 0, "fail": 0, "total": 0,
                "skip": 0, "already": 0, "manual": 0, "rows": [], "preview": preview,
                "stopped": False, "gate": ""}

    rows: list[dict] = []
    for ev in events:
        try:
            rows.extend(plan_event(client, ev, acc_id=acc_id))
        except ActivityError as e:
            log("[活动任务] %s 任务表读取失败：%s" % (ev, e))
    todo = [r for r in rows if r["state"] == "待完成"]
    already = sum(1 for r in rows if r["state"] == "已达标")
    manual = sum(1 for r in rows if r["state"] == "需游戏内")
    for r in rows:
        log("[活动任务]   %-10s %-52s %s/%s → %s"
            % (r["event"].replace("activitycenter_", ""), r["task_id"],
               r["value"], r["target"], r["state"]))

    if preview:
        summary = ("活动 %d 期 · 任务 %d 条 · 待完成 %d · 已达标 %d · 需游戏内 %d"
                   % (len(events), len(rows), len(todo), already, manual))
        log("[活动任务] %s（预览，未发任何写请求）" % summary)
        _save_last({"at": int(time.time()), "acc": acc_id, "summary": summary,
                    "preview": True, "rows": rows[:80]})
        return {"ok": True, "summary": summary, "succ": 0, "fail": 0, "total": len(rows),
                "skip": manual, "already": already, "manual": manual, "rows": rows[:80],
                "preview": True, "stopped": False, "gate": "", "events": events}

    done = fail = 0
    gate = ""
    for r in todo[:MAX_TASKS]:
        if stop_flag is not None and stop_flag.is_set():
            stopped = True
            log("[活动任务] 收到停止信号 → 收手")
            break
        try:
            code, prog, msg = set_task(client, r["task_id"], r["delta"])
        except ActivityError as e:
            if e.status in GATE_STATUS:
                gate = "HTTP %s" % e.status
                fail += 1
                log("[活动任务] %s 写入撞上网关级拒绝（HTTP %s）→ 立刻收手"
                    % (r["task_id"], e.status))
                break
            fail += 1
            log("[活动任务] %s 写入异常：%s" % (r["task_id"], e))
            continue
        except Exception as e:                                  # noqa: BLE001
            fail += 1
            log("[活动任务] %s 写入异常：%s" % (r["task_id"], str(e)[:120]))
            continue

        if code == 200:
            if prog.get("finished"):
                r["state"] = "完成"
                r["after"] = _int(prog.get("value"), 0)
                done += 1
                log("[活动任务]   ✅ %s 发差值 %s → finished（value=%s）"
                    % (r["task_id"], r["delta"], r.get("after")))
            else:
                # 服务端收了但还没达标：把差值重新算一遍（值是真加过的，别再重复加）
                r["state"] = "待完成"
                r["value"] = _int(prog.get("value"), 0)
                r["delta"] = max(0, r["target"] - r["value"])
                already += 1
                log("[活动任务]   ➖ %s 未达标（value=%s / %s）"
                    % (r["task_id"], r["value"], r["target"]))
        elif code == 400 and "invalid task" in msg.lower():
            # 规则 4：记账型/不可设任务 → 归「需游戏内」，不当失败
            r["state"] = "需游戏内"
            manual += 1
            log("[活动任务]   ⚪ %s 服务端拒收（invalid task）→ 需游戏内完成" % r["task_id"])
        else:
            fail += 1
            log("[活动任务]   ✗ %s 失败：code=%s %s" % (r["task_id"], code, msg))
            if code in BUSINESS_GATE:
                gate = "code %s" % code
                log("[活动任务] 业务码级拒绝 → 收手")
                break
        time.sleep(SET_PAUSE)

    summary = ("活动 %d 期 · 完成 %d · 已达标 %d · 需游戏内 %d · 失败 %d"
               % (len(events), done, already, manual, fail))
    if gate:
        summary += " · 网关级拒绝（%s）已提前收手" % gate
    if stopped:
        summary += " · 已停止"
    log("[活动任务] %s（耗时 %.1fs）" % (summary, time.time() - t0))
    _save_last({"at": int(time.time()), "acc": acc_id, "summary": summary,
                "preview": False, "rows": rows[:80]})
    clear_cache(acc_id)          # 下次跑动必须读到新进度，不吃旧缓存
    return {"ok": (done > 0) or (fail == 0), "summary": summary, "succ": done,
            "fail": fail, "total": len(rows), "skip": manual, "already": already,
            "manual": manual, "rows": rows[:80], "preview": False, "stopped": stopped,
            "gate": gate, "events": events}


_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
LAST_RUN_FILE = os.path.join(_DATA_DIR, "activity_last.json")


def _save_last(rec: dict) -> None:
    """把最近一次跑动的清单落 data/（0600），作为任务面板之外的第二份证据。"""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = LAST_RUN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, LAST_RUN_FILE)
    except Exception as e:                                      # noqa: BLE001
        print("[activity] 最近跑动落盘失败（不影响结果）：%r" % (e,), flush=True)
