# -*- coding: utf-8 -*-
"""后台维护心跳（定时任务已下线）。

**2026-09-16：定时任务整体下线。** 本模块以前是调度器 —— 用户给机位配「每天 HH:MM
自动跑图 / 每日 / 刷新 / 收蜡」，外带一层「只有配了排程的机位才每天自动续期一次」。
按用户要求全部移除，现在全站只剩一种驱动方式：

    **用户自己打开网站，在机位卡片上手动触发。**

所以下面这些都没有了：排程 CRUD（`create_schedule` / `update_schedule` /
`delete_schedule` / `list_views` …）、触发窗口与补跑（`GRACE_SECS` / `_backfill`）、
抖动（`jitter_secs` / `next_run_multi`）、任务链（`CHAIN_WINDOW`）、条件自动续期
（`_auto_renew_pass` / `RENEW_AT`）、以及 `data/schedules.json` 的读写。

**为什么还留一条心跳**：`tasks.MANAGER.tick()` 负责把内存里任务日志的尾部落盘、并清理
过期任务文件。历史上 `MANAGER.prune()` 就是因为「定义了但没人调」而变成死代码，
`_jobs` 只涨不降 —— 这条心跳不能删，也别再让它断掉。

**续期不需要后台保活**：`tasks.with_renew()` 是反应式的 —— 任何任务遇到
401/403/423 会就地续期并重试一次；用户也可以在手动机位卡片上直接点「续期」按钮。
"""
import threading

from . import tasks

TICK_SECS = 30      # 心跳间隔

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_hooks: list = []


def add_hook(fn) -> None:
    """注册一个随心跳执行的回调（目前用于维护模式的定时开关结算）。

    回调必须自己吞掉异常语义：这里统一兜 try/except，心跳绝不能因为某个钩子挂掉而死。
    """
    with _lock:
        if fn not in _hooks:
            _hooks.append(fn)


def _loop() -> None:
    while not _stop.is_set():
        try:
            tasks.MANAGER.tick()
        except Exception as e:  # 心跳线程绝不能死
            print("[upkeep] 任务维护异常：%r" % e, flush=True)
        for fn in list(_hooks):
            try:
                fn()
            except Exception as e:
                print("[upkeep] 心跳钩子异常：%r" % e, flush=True)
        _stop.wait(TICK_SECS)


def start_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="qfly-upkeep", daemon=True)
        _thread.start()
    print("[upkeep] 后台维护心跳已启动（每 %ds · 定时任务已下线，全部改为手动触发）"
          % TICK_SECS, flush=True)
