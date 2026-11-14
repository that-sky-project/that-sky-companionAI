"""Offline reset/state certification regressions. Only disposable fixture data."""
from __future__ import annotations
import ast
import copy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (APP, APP / "bot", APP / "escort-v31-server"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from qfsky.version_profile import CURRENT_BUILD, profile_for_build
from qfsky.join0167_profile import account_binding
from xg_gamedata import catalog_state

PROFILE = profile_for_build(CURRENT_BUILD)
UUID = "11111111-1111-4111-8111-111111111111"


def write_json(path, doc, **kwargs):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError): return default


def source_subset(path, env, constants=()):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), env)
    return types.SimpleNamespace(**env)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reset-state-fixture-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.writes = []
        def recording_write(path, value, **kwargs):
            self.writes.append((Path(path), copy.deepcopy(value)))
            write_json(path, value)
        self.xg = types.SimpleNamespace(DATA=self.data, APP=APP, ROOT=ROOT,
            CONTROL=self.data / "control", LOGS=self.data / "logs",
            WIZARD_TASK=self.data / "control/wizard_task.json",
            CURRENT_PROFILES=self.data / "runtime/private/current_profiles",
            CAPTURE_STATUS=self.data / "control/capture.json",
            ASSET_RELEASE=self.data / "runtime/terrain-experimental-20261104/release-v20-hop-trial",
            ACCOUNTS_JSON=self.data / "runtime/runtime-data/accounts.json",
            read_json=read_json, write_json=recording_write, ensure_dirs=lambda: None,
            python_exe=lambda: sys.executable,
            props_dir=lambda: self.data / "game/fixture-version/props")
        self.proc = types.SimpleNamespace(identity=lambda pid: None, argv_of=lambda pid: [],
                                         terminate=mock.Mock(), psutil=types.SimpleNamespace(pid_exists=lambda pid: False))
        self.env = dict(json=json, os=os, secrets=secrets, sys=sys, time=time, Path=Path,
            copy=copy, threading=threading, wraps=wraps, xg_paths=self.xg, xg_proc=self.proc,
            error_detail=types.SimpleNamespace(describe=lambda error, **kw: kw),
            gamedata=types.SimpleNamespace(env=lambda: {}), __file__=str(APP / "launcher/tasks.py"))
        self.tasks = source_subset(APP / "launcher/tasks.py", self.env,
            {"CHAIN", "LOCK_KINDS", "TAIL_BYTES", "_TASK_LOCK"})

    def task_doc(self, **overrides):
        task_id = "fixture-nonce"
        plan = self.data / "control" / f"task_{task_id}.plan.json"
        log = self.data / "logs" / "task.jsonl"
        write_json(plan, [["names"]]); log.parent.mkdir(parents=True, exist_ok=True); log.write_text("", encoding="utf-8")
        doc = dict(id=task_id, pid=12345, kind="data", running=True, ok=None, plan=str(plan), log=str(log),
                   create_time=100.0, started="1970-01-01 00:01:40", started_epoch=100.0)
        doc.update(overrides); write_json(self.xg.WIZARD_TASK, doc)
        return doc

    def alive(self, doc, *, create_time=None, argv=None):
        self.proc.identity = lambda pid: {"pid": pid, "create_time": doc.get("create_time") if create_time is None else create_time, "state": "S"}
        self.proc.argv_of = lambda pid: argv or [sys.executable, "-B", str(self.env["CHAIN"]), "--kind", doc["kind"], "--plan", doc["plan"]]

    def wizard(self):
        self.cap = {"state": "idle", "catalog": {"done": True, "account": "fixture-a"}}
        self.cfg = {"wizard": {"steps": {}}, "ai": {"marker": "dummy-preserved"}}
        self.wenv = dict(json=json, os=os, re=re, sys=sys, hashlib=hashlib, subprocess=types.SimpleNamespace(),
            threading=threading, time=time, Path=Path, xg_paths=self.xg, tasks=self.tasks,
            xg_account=types.SimpleNamespace(active_id=lambda: "fixture-a"),
            capture_ctl=types.SimpleNamespace(status=lambda: copy.deepcopy(self.cap), start=lambda: {"ok": True}),
            gamedata=types.SimpleNamespace(run=lambda *a, **kw: {"ok": True, "final": {"account": "fixture-a", "scene": "CandleSpace", "recorded": {"scenes": ["CandleSpace"]}}}),
            error_detail=types.SimpleNamespace(from_result=lambda r: {}))
        self.xg.config = lambda: copy.deepcopy(self.cfg)
        self.xg.save_config = lambda cfg: self.cfg.update(copy.deepcopy(cfg))
        self.wizard_mod = source_subset(APP / "launcher/wizard.py", self.wenv,
            {"WIZARD_VERSION", "STEPS", "STEP_IDS", "LOCK", "_CATALOG_HASH_CACHE"})
        self.wenv["_current_game_profile"] = lambda: PROFILE
        self.wenv["_accounts"] = lambda: [{"id": "fixture-a", "user_uuid": UUID}]
        self.wenv["_apk_path"] = lambda: str(self.data / "fixture.apk")
        return self.wizard_mod

    def capture(self, name="home-current", **updates):
        record = dict(apk_build=PROFILE["build"], native_sha256=PROFILE["native_sha256"],
            resource_revision=PROFILE["resource_revision"], scene="CandleSpace", level_id=PROFILE["home_level_id"],
            netversion_u16=list(PROFILE["home_netversion"]),
            account_binding={"value": account_binding(UUID)}, captured_utc="2026-11-14T00:00:00Z")
        record.update(updates)
        path = self.xg.CURRENT_PROFILES / name
        write_json(path / "record.json", record)
        return path


class TaskSelfHealingTests(FixtureCase):
    def test_real_disposable_process_exit_is_persisted(self):
        import xg_proc
        script = self.data / "gamedata_chain.py"
        script.write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
        doc = self.task_doc()
        proc = subprocess.Popen([sys.executable, "-B", str(script), "--kind", "data", "--plan", doc["plan"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            ident = xg_proc.identity(proc.pid)
            self.assertIsNotNone(ident)
            doc.update(pid=proc.pid, create_time=ident["create_time"])
            write_json(self.xg.WIZARD_TASK, doc)
            self.env["xg_proc"] = xg_proc; self.env["CHAIN"] = script
            self.assertTrue(self.tasks.current()["running"])
            proc.terminate(); proc.wait(timeout=5)
            self.assertFalse(self.tasks.current()["running"])
            self.assertEqual(read_json(self.xg.WIZARD_TASK)["error"], "任务进程已退出")
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=5)

    def test_access_denied_identity_stays_busy_and_is_not_killed(self):
        doc = self.task_doc()
        self.proc.psutil.pid_exists = lambda pid: True
        before = self.xg.WIZARD_TASK.read_bytes()
        out = self.tasks.current()
        self.assertTrue(out["running"]); self.assertEqual(out["process_state"], "unknown")
        self.assertEqual(before, self.xg.WIZARD_TASK.read_bytes())
        self.assertFalse(self.tasks.stop()["ok"])
        self.assertFalse(self.tasks.clear()["ok"])
        self.proc.terminate.assert_not_called()

    def test_unreadable_argv_stays_busy(self):
        doc = self.task_doc(); self.alive(doc)
        self.proc.argv_of = lambda pid: []
        self.assertTrue(self.tasks.current()["running"])
        self.assertEqual(self.tasks.current()["process_state"], "unknown")
    def test_dead_task_is_persisted_once(self):
        self.task_doc()
        first = self.tasks.current()
        saved = read_json(self.xg.WIZARD_TASK)
        self.assertFalse(first["running"]); self.assertFalse(saved["running"])
        self.assertEqual(saved["error"], "任务进程已退出")
        self.assertEqual(saved["error_code"], "task_process_gone")
        self.assertNotIn("task", saved); self.assertNotIn("lines", saved)
        count = len(self.writes); before = self.xg.WIZARD_TASK.read_bytes()
        self.tasks.current()
        self.assertEqual(len(self.writes), count); self.assertEqual(before, self.xg.WIZARD_TASK.read_bytes())

    def test_pid_reuse_and_wrong_chain_are_dead(self):
        for mismatch in ("birth", "chain", "plan"):
            with self.subTest(mismatch=mismatch):
                doc = self.task_doc()
                self.alive(doc, create_time=101 if mismatch == "birth" else 100,
                    argv=[sys.executable, "not-gamedata_chain.py"] if mismatch == "chain" else
                         [sys.executable, str(self.env["CHAIN"]), "--kind", "data", "--plan", "wrong-plan"] if mismatch == "plan" else None)
                self.assertFalse(self.tasks.current()["running"])
                self.assertFalse(read_json(self.xg.WIZARD_TASK)["running"])

    def test_legacy_record_requires_nonce_plan_and_creation_window(self):
        doc = self.task_doc(create_time=None)
        self.alive(doc, create_time=100.0)
        self.assertTrue(self.tasks.current()["running"])
        self.assertFalse(self.tasks.stop()["ok"])
        self.proc.terminate.assert_not_called()
        self.alive(doc, create_time=900.0)
        self.assertFalse(self.tasks.current()["running"])

    def test_legacy_missing_timestamp_is_not_assumed_alive(self):
        doc = self.task_doc(create_time=None, started_epoch=None, started=None)
        self.alive(doc, create_time=100.0)
        self.assertFalse(self.tasks.current()["running"])

    def test_live_child_success_does_not_finish_whole_chain(self):
        doc = self.task_doc(); self.alive(doc)
        Path(doc["log"]).write_text('{"ok":true}\n', encoding="utf-8")
        self.assertTrue(self.tasks.current()["running"])
        self.assertTrue(read_json(self.xg.WIZARD_TASK)["running"])

    def test_finished_chain_result_is_persisted(self):
        doc = self.task_doc()
        Path(doc["log"]).write_text('{"ok":true,"result":"fixture"}\n', encoding="utf-8")
        self.assertTrue(self.tasks.current()["task"]["ok"])
        self.assertTrue(read_json(self.xg.WIZARD_TASK)["ok"])

    def test_partial_chain_is_persisted_failed(self):
        doc = self.task_doc(); write_json(doc["plan"], [["names"], ["terrain"]])
        Path(doc["log"]).write_text('{"ok":true}\n', encoding="utf-8")
        self.assertEqual(self.tasks.current()["error_code"], "task_partial")
        self.assertFalse(read_json(self.xg.WIZARD_TASK)["ok"])

    def test_stale_reader_cannot_overwrite_new_task(self):
        doc = self.task_doc()
        new = {**doc, "id": "new-task", "pid": 23456, "running": False, "ok": True}
        def replaced(_):
            write_json(self.xg.WIZARD_TASK, new)
            return False
        original = self.env["_alive"]
        calls = []
        def once(value):
            if not calls: calls.append(True); return replaced(value)
            return original(value)
        self.env["_alive"] = once
        out = self.tasks.current()
        self.assertEqual(out["id"], "new-task")
        self.assertEqual(read_json(self.xg.WIZARD_TASK), new)


class WizardResetStateTests(FixtureCase):
    def test_sequential_reset_and_last_restore_preserve_prior_capture_gate(self):
        w = self.wizard(); self.capture()
        w.after_data_reset("execute", {"captures": True}, "batch-captures")
        w.after_data_reset("execute", {"names": True}, "batch-names")
        self.assertEqual(w.resource_reset_status()["steps"], ["data", "capture", "catalog"])
        w.after_data_reset("restore", {"names": True}, "batch-names")
        self.assertEqual(w.resource_reset_status()["steps"], ["capture", "catalog"])
        self.assertTrue(w.resource_reset_status()["auto_suppressed"])
        self.assertEqual(w.capture_status()["recorded"], [])
        w.after_data_reset("restore", {"captures": True}, "batch-captures")
        self.assertEqual(w.resource_reset_status()["steps"], [])
        with mock.patch.object(catalog_state, "certify", return_value={"certified": False}) as check:
            self.assertFalse(w.catalog_status("fixture-a")["certified"])
            check.assert_called_once()

    def test_unknown_restore_batch_does_not_discard_unrelated_requirements(self):
        w = self.wizard(); self.capture()
        w.after_data_reset("execute", {"captures": True}, "captures-1")
        w.after_data_reset("execute", {"names": True}, "names-2")
        expected = w.resource_reset_status()["steps"]
        w.after_data_reset("restore", {"captures": True}, "captures-1")
        self.assertEqual(w.resource_reset_status()["steps"], expected)
        self.assertTrue(w._reset_doc()["restore_scope_unknown"])

    def test_unrelated_reset_preserves_account_capture_approvals(self):
        w = self.two_accounts()
        w.after_data_reset("execute", {"captures": True}, "capture")
        w._reset_complete("capture", "fixture-a")
        w.after_data_reset("execute", {"names": True}, "names")
        self.assertNotIn("capture", w.resource_reset_status()["steps"])
        self.wenv["xg_account"].active_id = lambda: "fixture-b"
        self.assertIn("capture", w.resource_reset_status()["steps"])

    def two_accounts(self):
        w = self.wizard()
        other_uuid = "22222222-2222-4222-8222-222222222222"
        self.wenv["_accounts"] = lambda: [{"id": "fixture-a", "user_uuid": UUID},
                                        {"id": "fixture-b", "user_uuid": other_uuid}]
        self.capture("old-a")
        self.capture("old-b", account_binding={"value": account_binding(other_uuid)})
        return w

    def test_new_capture_a_never_reexposes_b_or_approves_b(self):
        w = self.two_accounts()
        w.after_data_reset("execute", {"captures": True}); w.start_capture()
        fresh = self.capture("new-a")
        self.cap["last_registered"] = {"record_dir": str(fresh), "account": "fixture-a"}
        inventory = w._capture_inventory(PROFILE)
        accepted = w._capture_after_reset(inventory)
        self.assertNotIn("old-b", [r["record_dir"] for r in accepted["current"]])
        self.assertNotIn("capture", w.resource_reset_status()["steps"])
        again = w._capture_after_reset(inventory)
        self.assertNotIn("old-b", [r["record_dir"] for r in again["current"]])
        self.wenv["xg_account"].active_id = lambda: "fixture-b"
        self.assertIn("capture", w.resource_reset_status()["steps"])
        self.assertEqual(w._capture_after_reset(inventory)["current"], [])
        self.assertEqual(w.start_catalog("fixture-b", "fixture.apk")["code"], "capture_required")

    def test_capture_registration_account_must_match_requested_account(self):
        w = self.two_accounts(); w.after_data_reset("execute", {"captures": True}); w.start_capture()
        fresh = self.capture("new-a")
        self.cap["last_registered"] = {"record_dir": str(fresh), "account": "fixture-b"}
        self.assertEqual(w._capture_after_reset(w._capture_inventory(PROFILE))["current"], [])
        self.assertIn("capture", w.resource_reset_status()["steps"])

    def test_a_catalog_task_cannot_unlock_b_checker(self):
        w = self.two_accounts(); w.after_data_reset("execute", {"catalog": True})
        w._reset_started("catalog", {"task": {"id": "catalog-a"}})
        task = {"task": {"id": "catalog-a", "kind": "catalog", "ok": True, "running": False},
                "meta": {"account": "fixture-a"}}
        with mock.patch.object(catalog_state, "certify", return_value={"certified": True}) as checker:
            self.assertFalse(w.catalog_status("fixture-b", task)["certified"])
            checker.assert_not_called()
            self.assertTrue(w.catalog_status("fixture-a", task)["certified"])
            self.assertEqual(checker.call_args.args[1], "fixture-a")
        w._reset_complete("catalog", "fixture-a")
        self.wenv["xg_account"].active_id = lambda: "fixture-b"
        self.assertIn("catalog", w.resource_reset_status()["steps"])

    def test_explicit_registration_only_approves_its_account(self):
        w = self.two_accounts(); w.after_data_reset("execute", {"captures": True})
        self.wenv["ensure_catalog"] = lambda: None
        self.wenv["_catalog_ready"] = lambda *a, **kw: False
        self.assertTrue(w.register_capture("old-a")["ok"])
        self.assertNotIn("capture", w.resource_reset_status()["steps"])
        self.wenv["xg_account"].active_id = lambda: "fixture-b"
        self.assertIn("capture", w.resource_reset_status()["steps"])

    def completion_fixture(self):
        w = self.wizard()
        self.cfg["consent"] = {"accepted": True}
        self.cfg["wizard"].update(completed_at="fixture-history", steps={"apk": {"done": True}, "mumu": {"done": True}})
        self.xg.OWNER_UUID = self.data / "owner.fixture"; self.xg.OWNER_UUID.write_text("dummy", encoding="utf-8")
        self.xg.GAME_VERSION = PROFILE["version"]
        self.wenv["_data_complete"] = lambda *a, **kw: True
        self.wenv["_catalog_ready"] = lambda *a, **kw: True
        self.wenv["_ai_configured"] = lambda: True
        self.wenv["_game_data_state"] = lambda profile, complete: {"complete": complete, "needs_rebuild": False}
        self.wenv["_device_status"] = lambda: {}
        self.wenv["_autostart_status"] = lambda: {}
        return w

    def test_historical_completion_rechecks_actual_prerequisites(self):
        w = self.completion_fixture()
        self.assertTrue(w.is_complete())
        self.wenv["_catalog_ready"] = lambda *a, **kw: False
        rows, _ = w.step_status()
        self.assertFalse(next(r["done"] for r in rows if r["id"] == "finish"))
        self.assertFalse(w.is_complete(rows)); self.assertFalse(w.is_complete())
        self.assertEqual(self.cfg["wizard"]["completed_at"], "fixture-history")

    def test_cleanup_invalidates_summary_finish_without_changing_config(self):
        w = self.completion_fixture(); saved = copy.deepcopy(self.cfg)
        w.after_data_reset("execute", {"terrain": True})
        result = w.summary()
        self.assertFalse(result["completed"])
        self.assertFalse(next(r["done"] for r in result["steps"] if r["id"] == "finish"))
        self.assertFalse(result["gameData"]["complete"])
        self.assertEqual(self.cfg, saved)

    def test_cleanup_retains_raw_files_but_blocks_green_state(self):
        w = self.wizard(); raw = self.capture()
        before = (raw / "record.json").read_bytes(); cfg = copy.deepcopy(self.cfg)
        result = w.after_data_reset("execute", {"captures": True, "catalog": True})
        self.assertEqual(result["steps"], ["capture", "catalog"])
        self.assertEqual((raw / "record.json").read_bytes(), before)
        self.assertEqual(self.cfg, cfg)
        self.assertFalse(w.capture_status()["catalog"]["certified"])
        self.assertEqual(w.capture_status()["recorded"], [])
        self.assertIsNone(w.ensure_catalog())

    def test_start_capture_does_not_recertify_old_record(self):
        w = self.wizard(); raw = self.capture()
        self.cap["last_registered"] = {"record_dir": str(raw), "account": "fixture-a"}
        w.after_data_reset("execute", {"captures": True})
        w.start_capture()
        self.assertIn("capture", w.resource_reset_status()["steps"])
        self.assertEqual(w.capture_status()["recorded"], [])

    def test_new_capture_and_registration_releases_capture_gate(self):
        w = self.wizard(); self.capture("old")
        w.after_data_reset("execute", {"captures": True}); w.start_capture()
        fresh = self.capture("new", captured_utc="2026-11-14T01:00:00Z")
        self.cap["last_registered"] = {"record_dir": str(fresh), "account": "fixture-a"}
        w.capture_status()
        self.assertNotIn("capture", w.resource_reset_status()["steps"])
        self.assertIn("catalog", w.resource_reset_status()["steps"])

    def test_explicit_registration_can_reuse_retained_current_record(self):
        w = self.wizard(); raw = self.capture()
        w.after_data_reset("execute", {"captures": True})
        self.wenv["ensure_catalog"] = lambda: None
        self.wenv["_catalog_ready"] = lambda *a, **kw: False
        out = w.register_capture(str(raw))
        self.assertTrue(out["ok"])
        self.assertNotIn("capture", w.resource_reset_status()["steps"])

    def test_stale_home_tag_not_current_even_with_current_build(self):
        w = self.wizard(); self.capture(netversion_u16=[36851, 49307, 23536, 0, 0, 25536])
        inv = w._capture_inventory(PROFILE)
        self.assertFalse(inv["current"]); self.assertEqual(len(inv["stale"]), 1)
        out = w.start_catalog("fixture-a", "fixture.apk")
        self.assertFalse(out["ok"]); self.assertEqual(out["code"], "capture_stale")

    def test_saved_success_with_incomplete_current_capture_is_not_ready(self):
        w = self.wizard(); self.capture(netversion_u16=None)
        self.cfg["wizard"]["steps"]["capture"] = {"done": True, "detail": {"account": "fixture-a", "result": {"ok": True}}}
        self.assertFalse(w.catalog_status("fixture-a")["certified"])
        self.assertEqual(w.start_catalog("fixture-a", "fixture.apk")["code"], "capture_required")

    def test_explicit_completed_catalog_action_rebuilds_not_already(self):
        w = self.wizard(); self.capture()
        self.wenv["_catalog_ready"] = lambda *a, **kw: True
        starts = []
        self.tasks.start = lambda *a, **kw: starts.append((a, kw)) or {"ok": True, "task": {"id": "new-rebuild"}}
        out = w.start_catalog("fixture-a", "fixture.apk")
        self.assertTrue(out["ok"]); self.assertNotIn("already", out); self.assertEqual(len(starts), 1)

    def test_foreign_account_capture_does_not_start_catalog(self):
        w = self.wizard(); self.capture(account_binding={"value": "different-binding"})
        out = w.start_catalog("fixture-a", "fixture.apk")
        self.assertEqual(out["code"], "capture_required")

    def test_old_success_task_does_not_repopulate_wizard(self):
        w = self.wizard()
        doc = self.task_doc(running=False, ok=True)
        w.after_data_reset("execute", {"terrain": True})
        out = w.after_task(self.tasks.current())
        self.assertIsNone(out); self.assertEqual(self.cfg["wizard"]["steps"], {})

    def test_restore_requires_fresh_runtime_validation(self):
        w = self.wizard(); w.after_data_reset("restore", {"catalog": True})
        with mock.patch.object(catalog_state, "certify", return_value={"certified": False, "state": "invalid", "account": "fixture-a"}) as checker:
            out = w.catalog_status("fixture-a")
        self.assertFalse(out["certified"]); checker.assert_called_once()
        self.assertIsNone(w.ensure_catalog())


class RuntimeCatalogueStateTests(FixtureCase):
    def setUp(self):
        super().setUp()
        catalog_state._CACHE.clear()
        self.folder = self.xg.ASSET_RELEASE / "asset-catalog-acct/fixture-a"
        self.manifest = self.folder / "manifest.json"
        write_json(self.manifest, {"apk_build": PROFILE["build"], "native_sha256": PROFILE["native_sha256"]})

    def test_presence_without_runtime_validation_is_not_certified(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=ValueError("fixture_invalid")):
            row = catalog_state.certify(self.data, "fixture-a", UUID, PROFILE)
        self.assertFalse(row["certified"]); self.assertEqual(row["message"], catalog_state.REBUILD_MESSAGE)

    def test_old_catalog_build_rejected_without_loader(self):
        write_json(self.manifest, {"apk_build": 421589, "native_sha256": "legacy"})
        with mock.patch.object(catalog_state, "_runtime_check") as runtime:
            row = catalog_state.certify(self.data, "fixture-a", UUID, PROFILE)
        self.assertFalse(row["certified"]); self.assertEqual(row["state"], "stale"); runtime.assert_not_called()

    def test_real_child_process_rejects_legacy_manifest(self):
        write_json(self.manifest, {"apk_build": 421589, "native_sha256": "legacy"})
        request = {"data": str(self.data), "account": "fixture-a", "uuid": UUID, "build": CURRENT_BUILD}
        with self.assertRaisesRegex(ValueError, "catalog_version_stale"):
            catalog_state._runtime_check(request)

    def test_real_child_rejects_36851_current_build_capture(self):
        self.capture(netversion_u16=[36851, 49307, 23536, 0, 0, 25536])
        profiles = {"apk_build": PROFILE["build"], "resource_revision": PROFILE["resource_revision"],
                    "maps": {str(PROFILE["home_level_id"]): {"scene": "CandleSpace",
                        "current_capture": {"by_account": {UUID: {"record_dir": "home-current"}}}}}}
        write_json(self.data / "runtime/private/connection0167_profiles.local.json", profiles)
        request = {"data": str(self.data), "account": "fixture-a", "uuid": UUID, "build": CURRENT_BUILD}
        with self.assertRaisesRegex(ValueError, "donor_version"):
            catalog_state._runtime_check(request)

    def test_status_does_not_certify_directory_presence_or_another_account(self):
        from xg_gamedata import status
        write_json(self.folder.parent / "fixture-other/manifest.json", {"entries": {"1": {}}})
        with mock.patch.object(catalog_state, "certify", return_value={"certified": False, "state": "invalid", "account": "fixture-a"}) as check:
            result = status.collect(self.data, account="fixture-a", account_uuid=UUID, profile=PROFILE)
        self.assertFalse(result["done"]["catalog"])
        self.assertEqual(len(result["catalogs"]), 2)
        self.assertEqual(check.call_count, 1)
        other = next(r for r in result["catalogs"] if r["account"] == "fixture-other")
        self.assertFalse(other["certified"]); self.assertEqual(other["state"], "not_current_account")

    def test_success_cached_only_until_any_output_changes(self):
        with mock.patch.object(catalog_state, "_runtime_check", return_value={"maps": 115, "variants": 2}) as runtime:
            self.assertTrue(catalog_state.certify(self.data, "fixture-a", UUID, PROFILE)["certified"])
            self.assertTrue(catalog_state.certify(self.data, "fixture-a", UUID, PROFILE)["certified"])
            self.assertEqual(runtime.call_count, 1)
            (self.folder / "changed.bin").write_bytes(b"changed")
            self.assertTrue(catalog_state.certify(self.data, "fixture-a", UUID, PROFILE)["certified"])
            self.assertEqual(runtime.call_count, 2)

    def test_change_during_validation_is_not_certified(self):
        def mutate(request):
            (self.folder / "new.bin").write_bytes(b"changed")
            return {"maps": 115, "variants": 2}
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=mutate):
            row = catalog_state.certify(self.data, "fixture-a", UUID, PROFILE)
        self.assertFalse(row["certified"]); self.assertEqual(row["code"], "catalog_changed_during_validation")

    def test_subprocess_environment_does_not_mutate_parent(self):
        prior = os.environ.get("QFLY_RESOURCE_REVISION")
        captured = []
        def run(argv, **kwargs):
            captured.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0, stdout='{"ok":true,"checked":{"maps":1,"variants":0}}')
        request = {"data": str(self.data), "account": "fixture-a", "uuid": UUID, "build": CURRENT_BUILD}
        with mock.patch.object(catalog_state.xg_exec, "run", side_effect=run):
            self.assertEqual(catalog_state._runtime_check(request)["maps"], 1)
        self.assertEqual(os.environ.get("QFLY_RESOURCE_REVISION"), prior)
        self.assertEqual(captured[0][1]["env"]["XG_DATA_DIR"], str(self.data))
        self.assertNotIn(UUID, " ".join(captured[0][0]))

    def test_real_verify_all_installs_current_resource_env_and_restores_it(self):
        from xg_gamedata import catalog
        import hashlib as h
        raw = b"fixture"
        digest = h.sha256(raw).hexdigest()
        entry = {"actor_sha256": digest, "cache_sha256": digest, "scene_sha256": digest}
        manifest = {"entries": {"1": entry}, "variants": {}}
        fake_profile = types.SimpleNamespace(binding="binding", record={"actor_sha256": digest}, cache=raw, blob=raw)
        seen = []
        def loader(uuid, map_id, **kwargs):
            seen.append(dict(os.environ))
            return None, fake_profile, entry
        xg = catalog_state._paths(self.data)
        previous = {k: os.environ.get(k) for k in ("QFLY_RESOURCE_REVISION", "QFLY_JOIN_CONFIG", "QFSKY_ASSET_CATALOG_MANIFEST")}
        with mock.patch.dict(sys.modules, {"qfsky.asset_catalog_runtime_v32c": types.SimpleNamespace(load_catalog_trial=loader)}):
            checked = catalog._verify_all(xg, UUID, "binding", manifest, self.manifest, PROFILE["resource_revision"], None)
        self.assertEqual(checked, {"maps": 1, "variants": 0})
        self.assertEqual(seen[0]["QFLY_RESOURCE_REVISION"], PROFILE["resource_revision"])
        self.assertEqual(seen[0]["QFLY_JOIN_CONFIG"], str(xg.PROFILES_JSON))
        self.assertEqual(seen[0]["QFLY_JOIN_MODE"], "current")
        self.assertEqual(previous, {k: os.environ.get(k) for k in previous})


if __name__ == "__main__":
    unittest.main(verbosity=2)
