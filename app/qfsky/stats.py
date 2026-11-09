# -*- coding: utf-8 -*-
"""跑图收烛统计：按「北京时间日期 × 机位」聚合每次跑图真实入账的烛光量（wax 口径，Σcc）。

数据来源：tasks.JobManager 在 pickup 任务结束时回填（无论手动触发还是定时触发，
收集到的烛火都是真实入账，一并统计）。存储 data/stats.json（0600）：
    {"days": {"2025-09-07": {"<acc_id>": {"wax": 79, "runs": 1, "last_at": 1725...}}}}
单位=烛光（滴），原样透传给前端。
只保留最近 _KEEP_DAYS 天，写盘原子替换。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Windows 无系统时区库：需 pip tzdata 包（requirements.txt 已列）。缺包时退化为 UTC+8 固定偏移兜底。
try:
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _TZ = timezone(timedelta(hours=8))
_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
STATS_FILE = os.path.join(_DATA_DIR, "stats.json")

_KEEP_DAYS = 400     # 保留最近 400 天明细（跑图热力日历需要 ~365 天窗口）
_TREND_DAYS = 14     # 趋势接口返回最近 14 天

_lock = threading.RLock()


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _load() -> dict:
    if not os.path.exists(STATS_FILE):
        return {"days": {}}
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"days": {}}
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        return {"days": {}}
    return data


def _save(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = STATS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, STATS_FILE)
    try:
        os.chmod(STATS_FILE, 0o600)
    except OSError:
        pass


def _prune(days: dict) -> None:
    if len(days) <= _KEEP_DAYS:
        return
    cutoff = datetime.now(_TZ).strftime("%Y-%m-%d")
    keys = sorted(days.keys())
    # 日期字符串本身就是可排序键（ISO 格式）
    for k in keys:
        if k < cutoff and len(days) > _KEEP_DAYS:
            del days[k]


def record_pickup(account_id: str, wax: int, cts: dict | None = None, ts: float | None = None) -> None:
    """记一次跑图入账（wax 为本次真实入账烛光量 Σcc，可为 0 —— 也算一次跑图）。

    cts 是 pickup 汇总里 ``per_map[].cts`` 的并集：``{货币类型: 颗数}``（原始 ct，
    含 ``color_red_wax`` 这种碎片键与动态 ``event_candle_*``）。烛光本身算在 wax
    里，cts 只是「这一趟分别掉了哪些币种、各多少颗」，供工作台按账号分类展示。
    兼容旧记录：没有 cts 键就只累加 wax。
    """
    if not account_id:
        return
    try:
        wax = max(0, int(wax or 0))
    except (TypeError, ValueError):
        wax = 0
    with _lock:
        data = _load()
        days = data["days"]
        day = _today()
        rec = days.setdefault(day, {}).setdefault(account_id, {"wax": 0, "runs": 0, "last_at": 0})
        rec["wax"] += wax
        rec["runs"] += 1
        rec["last_at"] = round(ts if ts is not None else time.time(), 1)
        if isinstance(cts, dict) and cts:
            acc = rec.get("cts")
            if not isinstance(acc, dict):
                acc = {}
                rec["cts"] = acc
            for ct, n in cts.items():
                key = str(ct or "").strip()
                if not key:
                    continue
                try:
                    cnt = int(n or 0)
                except (TypeError, ValueError):
                    cnt = 0
                if cnt:
                    acc[key] = int(acc.get(key) or 0) + cnt
        _prune(days)
        _save(data)


def aggregate(acc_ids: list[str] | None = None, ndays: int = _TREND_DAYS) -> dict:
    """聚合最近 ndays 天趋势。acc_ids=None 表示不过滤（全部机位）。

    返回 {"days": [{"date","wax","runs"}...（按日期升序，缺数日补 0；wax=烛光量）],
          "today": n, "week": n, "total": n, "runs_today": n}
    兼容读旧格式 candles 键（升格前遗留数据）。
    """
    with _lock:
        data = _load()
    days = data["days"]
    want = None if acc_ids is None else set(acc_ids)
    out = []
    total = week = 0
    today = _today()
    base = datetime.now(_TZ)
    # ★ 本周一（ISO weekday 1=周一）。过去这里误写成本身今天（strftime("now")），
    #   于是 d >= week_start 只放行今天，week 实际退化成「今日」，与 by_account
    #   的 week（本周累计）对不上 —— 工作台顶部「本周烛光」长期只显示今天的量。
    week_start = (base - timedelta(days=base.isoweekday() - 1)).strftime("%Y-%m-%d")
    # 最近 ndays 天（含今天），升序补零
    dates = [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ndays - 1, -1, -1)]
    today_wax = today_runs = 0
    for d in dates:
        wax = runs = 0
        per = days.get(d) or {}
        for acc_id, rec in per.items():
            if want is not None and acc_id not in want:
                continue
            wax += int(rec.get("wax") or rec.get("candles") or 0)
            runs += int(rec.get("runs") or 0)
        out.append({"date": d, "wax": wax, "runs": runs})
        total += wax
        if d == today:
            today_wax, today_runs = wax, runs
        # 自然周（本周一至今）：ISO weekday 1=周一
        if d >= week_start and _is_same_iso_week(d, today):
            week += wax
    return {"days": out, "today": today_wax, "runs_today": today_runs,
            "week": week, "total": total}


def _is_same_iso_week(a: str, b: str) -> bool:
    try:
        da = datetime.strptime(a, "%Y-%m-%d")
        db = datetime.strptime(b, "%Y-%m-%d")
    except ValueError:
        return a == b
    return da.isocalendar()[:2] == db.isocalendar()[:2]


_PER_ACC_DAYS = 30  # 每机位分类统计默认窗口


def by_account(acc_ids: list[str] | None = None, ndays: int = _PER_ACC_DAYS) -> dict:
    """按机位拆分的货币分类统计（工作台「每个账号跑了多少货币都要分类清楚」）。

    ``acc_ids=None`` 表示不过滤（全部机位）。返回：

        {"ndays": 30,
         "dates": ["2026-09-01", ...],          # 升序，含今天，缺数日也出现
         "accounts": [{"acc_id", "wax", "runs", "today", "today_runs", "week",
                       "cts": {"<ct>": 颗数},   # 这段窗口里各货币类型的颗数合计
                       "days": [{"date", "wax", "runs"}, ...]}]}

    ``cts`` 是 ``record_pickup`` 收下来的原始 ct（含 ``color_red_wax`` 这类碎片键
    与动态 ``event_candle_*``），**不是**烛光的另一种写法：烛光在 ``wax`` 里，
    cts 是「这一趟分别掉了哪些币种、各多少颗」。折算成品由 qfsky.forge 完成，
    这里只出原始量。账号按窗口内烛光降序。
    """
    with _lock:
        data = _load()
    days = data["days"]
    want = None if acc_ids is None else set(acc_ids)
    base = datetime.now(_TZ)
    dates = [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ndays - 1, -1, -1)]
    today = dates[-1]

    per: dict[str, dict] = {}
    for d in dates:
        for acc_id, rec in (days.get(d) or {}).items():
            if want is not None and acc_id not in want:
                continue
            row = per.get(acc_id)
            if row is None:
                row = per[acc_id] = {"acc_id": acc_id, "wax": 0, "runs": 0,
                                     "today": 0, "today_runs": 0, "week": 0,
                                     "cts": {}, "byday": {}}
            w = int(rec.get("wax") or rec.get("candles") or 0)
            r = int(rec.get("runs") or 0)
            row["wax"] += w
            row["runs"] += r
            row["byday"][d] = {"wax": w, "runs": r}
            if d == today:
                row["today"] = w
                row["today_runs"] = r
            # ★ 今天也要计入本周 —— 过去这里是 elif，今天被排除在 week 之外，
            #   与 aggregate 的 week（含今天）差一整天的量，工作台顶部「本周烛光」
            #   与机位卡「本周」对不上。
            if _is_same_iso_week(d, today):
                row["week"] += w
            cts = rec.get("cts")
            if isinstance(cts, dict):
                for ct, n in cts.items():
                    key = str(ct or "").strip()
                    if not key:
                        continue
                    try:
                        row["cts"][key] = int(row["cts"].get(key) or 0) + int(n or 0)
                    except (TypeError, ValueError):
                        continue

    out = []
    for row in per.values():
        item = {k: row[k] for k in ("acc_id", "wax", "runs", "today", "today_runs", "week", "cts")}
        item["days"] = [{"date": d, "wax": row["byday"].get(d, {}).get("wax", 0),
                         "runs": row["byday"].get(d, {}).get("runs", 0)} for d in dates]
        out.append(item)
    # 有数据的一天都算：蜡烛光降序，再按机位 id 稳定排序
    out.sort(key=lambda x: (-x["wax"], -x["runs"], x["acc_id"]))
    return {"ndays": ndays, "dates": dates, "accounts": out}

