# -*- coding: utf-8 -*-
"""离线场景：旧版本遇境录制必须提示重录，当前版本录制照常可用。

由 test_stale_capture.py 在临时 XG_DATA_DIR 里用子进程运行，不碰真实数据。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
DATA = Path(os.environ["XG_DATA_DIR"])
sys.path[:0] = [str(APP), str(APP / "bot"), str(APP / "launcher")]

from qfsky.version_profile import CURRENT_BUILD, profile_for_build  # noqa: E402

PROFILE = profile_for_build(CURRENT_BUILD)
OLD_RECORD = "CandleSpace_auto_old"
NEW_RECORD = "CandleSpace_auto_new"


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def record(build, native, revision, captured):
    return {"schema": 1, "kind": "sky-current-scene-actor-pair",
            "classification": "private-current-account-session-state",
            "scene": "CandleSpace", "apk_build": build,
            "native_sha256": native, "resource_revision": revision,
            "captured_utc": captured}


def prepare(mode):
    fake_apk = DATA / "game" / "fake.apk"
    fake_apk.parent.mkdir(parents=True, exist_ok=True)
    fake_apk.write_bytes(b"fixture")
    steps = {}
    if mode == "stale":
        # 模拟 0.16.7 时向导留下的成功状态，必须被旧版本判断覆盖。
        steps["capture"] = {"done": True, "message": "遇境采集成功 · 已自动登记",
                            "detail": {"account": "fixture-a",
                                       "result": {"ok": True, "account": "fixture-a", "maps": 115}}}
    write_json(DATA / "config.json", {
        "consent": {"accepted": True},
        "wizard": {"version": 1, "steps": steps,
                   "apk": {"path": str(fake_apk), "version": str(PROFILE["version"]),
                           "version_code": CURRENT_BUILD}},
    })
    write_json(DATA / "runtime" / "runtime-data" / "accounts.json", {
        "accounts": [{"id": "fixture-a",
                      "user_uuid": "10000000-0000-4000-8000-000000000001",
                      "created_at": 1}]})
    (DATA / "control").mkdir(parents=True, exist_ok=True)
    (DATA / "control" / "active_account").write_text("fixture-a\n", encoding="utf-8")
    profiles = DATA / "runtime" / "private" / "current_profiles"
    if mode == "stale":
        # 0.16.7 旧录制：build / native / resource_revision 都是旧档。
        old = record(421589,
                     "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321",
                     "V75885", "2026-01-01T00:00:00+00:00")
        write_json(profiles / OLD_RECORD / "record.json", old)
        (DATA / "game" / "0.16.7-421589" / "names").mkdir(parents=True, exist_ok=True)
        last = {"record_dir": OLD_RECORD, "scene": "CandleSpace", "account": "fixture-a"}
    else:
        current = record(CURRENT_BUILD, PROFILE["native_sha256"],
                         PROFILE["resource_revision"], "2026-02-02T00:00:00+00:00")
        # A current capture must also identify this fixture account and the
        # current home NetVersion; metadata-only rows cannot certify a catalogue.
        from qfsky.join0167_profile import account_binding
        current.update(level_id=PROFILE["home_level_id"],
                       netversion_u16=list(PROFILE["home_netversion"]),
                       account_binding={"value": account_binding("10000000-0000-4000-8000-000000000001")})
        write_json(profiles / NEW_RECORD / "record.json", current)
        last = {"record_dir": NEW_RECORD, "scene": "CandleSpace", "account": "fixture-a"}
    write_json(DATA / "control" / "capture.json", {
        "state": "ready", "recorded": ["CandleSpace"],
        "message": "遇境采集成功，已自动登记。", "last_registered": last})


def main():
    mode = sys.argv[1]
    prepare(mode)
    import wizard
    # 只验证“允许构建”，不真的起目录构建子进程。
    wizard.tasks.command_for = lambda *args, **kwargs: ["fixture"]
    wizard.tasks.start = lambda *args, **kwargs: {"ok": True, "pid": None}
    summary = wizard.summary()
    steps = {row["id"]: row for row in summary["steps"]}
    out = {
        "mode": mode,
        "current_version": summary["capture"]["current_version"],
        "recorded": summary["capture"]["recorded"],
        "stale": summary["capture"]["stale"],
        "stale_notice": summary["capture"]["stale_notice"],
        "last_registered": summary["capture"]["last_registered"],
        "capture_hint": steps["capture"]["hint"],
        "capture_message": steps["capture"].get("message", ""),
        "capture_done": steps["capture"]["done"],
        "data_hint": steps["data"]["hint"],
        "data_needs_rebuild": steps["data"].get("needs_rebuild", False),
        "login_done": steps["login"]["done"],
        "game_data": summary["gameData"],
        "catalog_start": wizard.start_catalog("fixture-a", str(DATA / "game" / "fake.apk")),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
