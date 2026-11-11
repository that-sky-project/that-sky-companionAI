# -*- coding: utf-8 -*-
"""本机录图后台进程：找 MuMu → 认当前图 → 录制 → 本机登记。

  python -m capture.worker [--once]

由 control_api 起子进程；状态写 DATA/control/capture.json，网页轮询。
停止：control_api 写 DATA/control/capture.stop（或直接结束本进程）。
不连服务器/不 scp；录完只调 `python -m xg_gamedata capture-register --dir D`。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

from . import device, errors, gamedata, native_lib  # noqa: E402
import error_detail  # noqa: E402

PACKAGE = "com.netease.sky"
HOME_SCENE = "CandleSpace"
HOME_NAME = "遇境"
SCENE_NAMES = {"CandleSpace": "遇境"}


def recorded_hint(scene, count):
    if scene == HOME_SCENE:
        return "遇境采集成功，已自动登记。只需采集一次遇境，不需要逐张跑图；可停止采集，并查看地图目录构建进度。"
    return "「%s」已经录制（已录 %d 张）。安装只需要遇境录制，无需逐张跑图。" % (scene_name(scene), count)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _status_path():
    import xg_paths
    return xg_paths.CAPTURE_STATUS


def write_status(doc):
    import xg_paths
    doc = dict(doc)
    doc["updated"] = _now()
    try:
        xg_paths.write_json(xg_paths.CAPTURE_STATUS, doc, private_file=False)
    except OSError:
        pass


def read_status():
    import xg_paths
    return xg_paths.read_json(xg_paths.CAPTURE_STATUS, {}) or {}


def stop_requested():
    import xg_paths
    return xg_paths.CAPTURE_STOP.exists()


def scene_name(scene):
    if not scene:
        return ""
    if scene in SCENE_NAMES:
        return SCENE_NAMES[scene]
    try:
        import xg_paths
        doc = xg_paths.read_json(xg_paths.NAMES / "scene_names.json", {}) or {}
        if isinstance(doc, dict) and doc.get(scene):
            return str(doc[scene])
    except Exception:
        pass
    return scene


def _cfg():
    import xg_paths
    cfg = xg_paths.config() or {}
    return cfg, (cfg.get("capture") or {}), (cfg.get("gamedata") or {}) if isinstance(cfg.get("gamedata"), dict) else {}


def _apk_path(cfg, gd):
    for v in (gd.get("apk"), ((cfg.get("wizard") or {}).get("apk") or {}).get("path")):
        if v and Path(str(v)).is_file():
            return str(v)
    try:
        import xg_paths
        for p in sorted(xg_paths.APK_DIR.glob("*.apk")):
            return str(p)
    except Exception:
        pass
    return None


def _fake_record_dir():
    return os.environ.get("XG_CAPTURE_FAKE_DIR") or ""


def _copy_fake_record(dest: Path, scene: str):
    src = Path(_fake_record_dir())
    if not src.is_dir():
        raise ValueError("XG_CAPTURE_FAKE_DIR 不存在")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    record = dest / "record.json"
    if not record.is_file():
        record.write_text(json.dumps({
            "schema": 1, "kind": "sky-current-scene-actor-pair", "scene": scene,
            "apk_build": 421629, "account_binding": {"value": "0" * 64},
        }, ensure_ascii=False), encoding="utf-8")
    return dest


def _binding_of(local_dir: Path):
    doc = json.loads((local_dir / "record.json").read_text(encoding="utf-8"))
    return str(((doc.get("account_binding") or {}).get("value")) or "")


def _catalog_dir(account):
    import xg_paths
    return xg_paths.CATALOG_DATA / str(account or "")


def _record_once(adb, serial, pid, scene, revision, fake):
    """返回 (record_dir, binding, 中文说明, 原始错误详情)。"""
    import xg_paths
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    record_dir = f"{scene}_auto_{stamp}"
    dest = xg_paths.CURRENT_PROFILES / record_dir
    if dest.exists():
        return (None, None, "同名录制目录已经存在，等一秒再试",
                error_detail.describe(None, code="capture_dir_exists",
                                      text="record dir already exists: " + record_dir))
    if fake:
        try:
            _copy_fake_record(dest, scene)
            return dest, _binding_of(dest), None, None
        except Exception as exc:
            return (None, None, errors.explain(exc),
                    error_detail.describe(exc, code="capture_fake"))
    try:
        from . import profile_capture as C
        a = argparse.Namespace(
            adb=device.adb_argv(adb), native_library=native_lib.native_path(),
            private_root=xg_paths.CURRENT_PROFILES, serial=serial, record_dir=record_dir,
            scene=scene, resource_revision=revision,
            summary_out=xg_paths.LOGS / (record_dir + "_summary.json"),
            pause_during_pair_read=True)
        C.capture(a)
    except Exception as exc:
        msg = str(exc)
        # 失败时尽量不留半成品；commit_private_record 自己也是原子提交
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return (None, None, errors.explain(msg),
                error_detail.describe(exc, code="capture_failed"))
    try:
        return dest, _binding_of(dest), None, None
    except Exception:
        return dest, "", None, None


def _register(record_dir: Path):
    r = gamedata.run(["capture-register", "--dir", str(record_dir)], timeout=600)
    return r


def run(once=False):
    import xg_paths
    xg_paths.ensure_dirs()
    try:
        xg_paths.CAPTURE_STOP.unlink()
    except OSError:
        pass
    started = _now()
    status = {"state": "starting", "worker_pid": os.getpid(), "started": started,
              "message": "正在找 MuMu…", "recorded": [], "catalog": {"needed": False},
              "mumu": {}, "scene": None, "scene_name": "", "last_error": "",
              "last_error_code": "", "last_error_detail": None,
              "last_record_dir": "", "last_registered": None}
    write_status(status)
    cfg, cap, gd = _cfg()
    fake = bool(_fake_record_dir())
    revision = str(cap.get("resource_revision") or "V76318")
    serial = pid = None
    adb = None
    mumu_dir = None
    tools_ready = False
    native_ready = False
    recorded = set()
    last_fail = 0.0
    last_hint = 0.0

    while not stop_requested():
        # control_api 可能已经把 catalog 标成 started/done；不要用进程里的旧副本覆盖它。
        prev = read_status()
        if isinstance(prev.get("catalog"), dict) and (prev["catalog"].get("started")
                                                     or prev["catalog"].get("done")):
            status["catalog"] = prev["catalog"]
        if serial is None:
            status.update(state="searching", message="正在找 MuMu 和 adb…", mumu={})
            write_status(status)
            adb, mumu_dir = device.find_adb()
            if not adb:
                status.update(state="error", last_error="没找到 MuMu/adb",
                              last_error_code="adb_missing",
                              last_error_detail=error_detail.describe(
                                  None, code="adb_missing",
                                  text="device.find_adb() returned none"),
                              message="没找到 MuMu：确认装了 MuMu 12，或在上面的输入框手填 adb.exe 路径。")
                write_status(status)
                if once:
                    return 1
                time.sleep(5)
                continue
            game = device.find_game(adb)
            if not game:
                if time.time() - last_hint > 10:
                    last_hint = time.time()
                    status.update(state="searching",
                                  last_error_code="game_missing",
                                  last_error_detail=error_detail.describe(
                                      None, code="game_missing",
                                      text="pidof %s did not return a pid" % PACKAGE),
                                  message="找到 adb（%s），但没找到正在跑光遇的 MuMu。打开游戏、登进遇境。" % adb)
                    write_status(status)
                if once:
                    return 1
                time.sleep(5)
                continue
            serial, pid = game["serial"], game["pid"]
            if not device.ensure_root(adb, serial):
                status.update(state="error", last_error="MuMu 拿不到 root",
                              last_error_code="root_unavailable",
                              last_error_detail=error_detail.describe(
                                  None, code="root_unavailable",
                                  text="adb shell id did not report uid=0 for %s" % serial),
                              message="MuMu 拿不到 root：设置 → 其他设置 → 打开「手机 Root 权限」，重启模拟器。")
                write_status(status)
                serial = pid = None
                if once:
                    return 1
                time.sleep(8)
                continue
            status.update(state="ready", mumu={"adb": str(adb), "mumu_dir": str(mumu_dir or ""),
                                               "serial": serial, "pid": pid, "root": True},
                          message="找到 MuMu（%s），游戏进程 %s，已拿到 root。" % (serial, pid))
            write_status(status)
            if not fake and not native_ready:
                try:
                    apk = _apk_path(cfg, gd)
                    native_lib.ensure_native_library(apk)
                    native_ready = True
                except Exception as exc:
                    status.update(state="error", last_error=str(exc),
                                  last_error_code="native_library",
                                  last_error_detail=error_detail.describe(
                                      exc, code="native_library"),
                                  message=errors.explain(exc))
                    write_status(status)
                    if once:
                        return 1
                    time.sleep(10)
                    serial = pid = None
                    continue

        # 游戏是否还活着
        code, out, _ = device.adb_run(adb, "shell", "pidof " + PACKAGE, serial=serial, timeout=8)
        if code != 0 or not re.fullmatch(r"\d+", out or ""):
            status.update(state="searching", message="游戏关了或模拟器断开，重新寻找…")
            write_status(status)
            serial = pid = None
            recorded = set()
            if once:
                return 1
            time.sleep(3)
            continue
        if int(out) != pid:
            pid = int(out)
            recorded = set()

        scene, level_id = device.current_scene(adb, serial, pid, revision)
        if not scene:
            if time.time() - last_hint > 8:
                last_hint = time.time()
                status.update(state="waiting", message="还没认出在哪张图（进图完成后才认得出），走进一张图就行。")
                write_status(status)
            if once:
                return 1
            time.sleep(3)
            continue
        status.update(scene=scene, scene_name=scene_name(scene), level_id=level_id,
                      state="ready", message="在「%s」。" % scene_name(scene))
        write_status(status)
        if scene in recorded:
            if time.time() - last_hint > 8:
                last_hint = time.time()
                status["message"] = recorded_hint(scene, len(recorded))
                write_status(status)
            if once:
                return 0
            time.sleep(4)
            continue
        if time.time() - last_fail < 6:
            time.sleep(2)
            continue

        status.update(state="recording",
                      message="在「%s」，开始录（站稳别动，几十秒）…" % scene_name(scene),
                      last_error="", last_error_code="", last_error_detail=None)
        write_status(status)
        dest, binding, why, why_detail = _record_once(adb, serial, pid, scene, revision, fake)
        if dest is None:
            status.update(state="error", last_error=why,
                          last_error_code=(why_detail or {}).get("code") or "capture_failed",
                          last_error_detail=why_detail,
                          message="「%s」没录成：%s" % (scene_name(scene), why))
            write_status(status)
            last_fail = time.time()
            if once:
                return 1
            continue

        status.update(state="registering", last_record_dir=str(dest),
                      message="录好了，在本机登记…")
        write_status(status)
        result = _register(dest)
        if not result.get("ok"):
            final = result.get("final") or {}
            why = final.get("message") or final.get("error") or "capture-register 失败"
            detail = error_detail.from_result(result)
            detail["record_dir"] = str(dest)
            status.update(state="error", last_error=str(why),
                          last_error_code=detail.get("code") or "capture_register",
                          last_error_detail=detail,
                          message="登记失败：%s（录制还在 %s，可重试）" % (why, dest.name))
            write_status(status)
            last_fail = time.time()
            if once:
                return 1
            continue
        final = result.get("final") or {}
        rec = final.get("recorded") or {}
        names = rec.get("names") or []
        scenes = rec.get("scenes") or []
        recorded = set(scenes) | set(status.get("recorded") or []) | {scene}
        account = str(final.get("account") or "")
        if not account:
            try:
                import xg_account
                account = xg_account.active_id() or ""
            except Exception:
                account = ""
        status.update(recorded=sorted(recorded), last_error="",
                      last_error_code="", last_error_detail=None,
                      last_registered={"record_dir": str(dest), "scene": scene,
                                       "map": final.get("map") or scene_name(scene),
                                       "account": account or "",
                                       "at": _now()},
                      message="「%s」已登记到 %s 名下（这个号已录 %d 张）。" % (
                          final.get("map") or scene_name(scene),
                          final.get("alias") or account or "本机", len(recorded)))
        if scene == HOME_SCENE:
            status["message"] = recorded_hint(scene, len(recorded))
        apk = _apk_path(cfg, gd)
        need_catalog = bool(scene == HOME_SCENE and account and apk
                            and not _catalog_dir(account).is_dir())
        status["catalog"] = {"needed": need_catalog, "account": account, "apk": apk,
                             "started": False, "done": not need_catalog}
        if need_catalog:
            status["message"] += " 下一步：在“地图目录”查看或启动自动构建，等绿色“构建成功 · 校验通过”。"
        elif scene == HOME_SCENE and not account:
            status["message"] += " 还没登录游戏号，登录后才能生成这个号的地图目录。"
        write_status(status)
        if once:
            return 0
        last_hint = 0.0

    status.update(state="stopped", message="录图已停止。", worker_pid=None)
    write_status(status)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="小光本机录图")
    ap.add_argument("--once", action="store_true", help="只录当前这一张就退出（测试/手动用）")
    a = ap.parse_args(argv)
    try:
        return run(once=a.once)
    except KeyboardInterrupt:
        try:
            st = read_status()
            st.update(state="stopped", message="录图已停止。", worker_pid=None)
            write_status(st)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
