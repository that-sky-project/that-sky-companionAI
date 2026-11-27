"""Account deletion uses only disposable local fixtures and mocked remote calls."""
from __future__ import annotations
import ast
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/"app"
sys.path[:0]=[str(APP),str(APP/"bot"),str(APP/"launcher")]
import xg_auth_client as auth
import xg_paths
import account_preferences as prefs
import account_login as login
import control_api as api

A="fixture-delete-a"
B="fixture-keep-b"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="account-delete14-",dir=ROOT/"tests")
        self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)
        self.cache=self.data/"runtime/runtime-data/accounts.json"
        self.active=self.data/"control/active_account"
        self.labels=self.data/"control/account_nicknames.json"
        self.cache.parent.mkdir(parents=True);self.active.parent.mkdir(parents=True)
        self.original={"accounts":[{"id":A,"alias":"delete fixture","session":"synthetic-session-a"},
                                   {"id":B,"alias":"keep fixture","session":"synthetic-session-b"}]}
        self.cache.write_text(json.dumps(self.original),encoding="utf-8")
        self.active.write_text(A+"\n",encoding="utf-8")
        self.labels.write_text(json.dumps({A:"delete-label",B:"keep-label"}),encoding="utf-8")
        self.preserved=[self.data/"captures/fixture/record.json",self.data/"control/persona.json",
                        self.data/"logs/memory.json",self.data/"runtime/private/owner_uuid"]
        for p in self.preserved:
            p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b"synthetic-preserved-value")
        for owner,name,value in ((auth,"data_root",lambda:self.data),(xg_paths,"DATA",self.data),
                                 (xg_paths,"CONTROL",self.data/"control"),(xg_paths,"ACTIVE_ACCOUNT",self.active),
                                 (xg_paths,"ACCOUNTS_JSON",self.cache)):
            p=mock.patch.object(owner,name,value);p.start();self.addCleanup(p.stop)
        self.remote=object.__new__(auth.Client)
        self.remote.request=mock.Mock(return_value={"ok":True,"deleted":True,"account_id":A})
        p=mock.patch.object(login,"Client",return_value=self.remote);p.start();self.addCleanup(p.stop)

    def read(self):
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def delete(self):
        return login.cmd_delete({"account":A,"confirm":True})


class RemoteProtocolTests(Fixture):
    def test_exact_owner_scoped_route_and_success_receipt(self):
        out=self.remote.delete(A)
        self.remote.request.assert_called_once_with("POST","/v1/accounts/"+A+"/delete",{},timeout=30)
        self.assertTrue(out["deleted"]);self.assertFalse(out["not_found"])

    def test_explicit_404_not_found_is_confirmed_absence(self):
        self.remote.request.side_effect=auth.AuthError("not_found",404)
        out=self.remote.delete(A)
        self.assertTrue(out["not_found"]);self.assertEqual(out["account_id"],A)

    def test_403_unauthorized_timeout_and_other404_are_not_success(self):
        for error in (auth.AuthError("not_found",403),auth.AuthError("unauthorized",401),
                      auth.AuthError("auth_service_unavailable"),auth.AuthError("other_error",404)):
            self.remote.request.side_effect=error
            with self.subTest(code=error.code,status=error.status),self.assertRaises(auth.AuthError):
                self.remote.delete(A)

    def test_mismatched_or_incomplete_receipt_rejected(self):
        for receipt in ({"ok":True,"deleted":True,"account_id":B},{"ok":True,"account_id":A},
                        {"ok":False,"deleted":True,"account_id":A},{"ok":1,"deleted":True,"account_id":A}):
            self.remote.request.return_value=receipt
            with self.subTest(receipt=receipt),self.assertRaises(auth.AuthError) as ctx:
                self.remote.delete(A)
            self.assertEqual(ctx.exception.code,"invalid_delete_receipt")

    def test_invalid_id_cannot_change_route(self):
        for identity in ("../other","a/b","",None,"a?x","a"*65):
            with self.subTest(identity=identity),self.assertRaises(auth.AuthError):
                self.remote.delete(identity)
        self.remote.request.assert_not_called()


class LocalDeletionTests(Fixture):
    def test_success_removes_only_selected_cache_active_and_label(self):
        out=self.delete()
        self.assertTrue(out["ok"]);self.assertEqual(out["account"],A)
        self.assertEqual(self.read()["accounts"],[self.original["accounts"][1]])
        self.assertEqual(self.read()["deleted_account_ids"],[A])
        self.assertEqual(self.active.read_text(encoding="utf-8"),"")
        self.assertEqual(json.loads(self.labels.read_text(encoding="utf-8")),{B:"keep-label"})
        for p in self.preserved:self.assertEqual(p.read_bytes(),b"synthetic-preserved-value")

    def test_inactive_delete_keeps_other_active_pointer(self):
        self.active.write_text(B+"\n",encoding="utf-8")
        self.assertTrue(self.delete()["ok"])
        self.assertEqual(self.active.read_text(encoding="utf-8"),B+"\n")

    def test_404_cleanup_uses_same_local_scope(self):
        self.remote.request.side_effect=auth.AuthError("not_found",404)
        out=self.delete()
        self.assertTrue(out["ok"]);self.assertTrue(out["remote_not_found"])
        self.assertEqual([r["id"] for r in self.read()["accounts"]],[B])

    def test_failure_keeps_all_local_bytes(self):
        before={p:p.read_bytes() for p in [self.cache,self.active,self.labels,*self.preserved]}
        for error in (auth.AuthError("auth_service_unavailable"),auth.AuthError("not_found",403)):
            self.remote.request.side_effect=error
            with self.assertRaises(auth.AuthError):self.delete()
            self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_wrong_receipt_does_not_remove_target_or_other_account(self):
        self.remote.request.return_value={"ok":True,"deleted":True,"account_id":B}
        with self.assertRaises(auth.AuthError):self.delete()
        self.assertEqual(self.read(),self.original)

    def test_literal_confirmation_required(self):
        for confirm in (False,None,1,"true"):
            with self.subTest(confirm=confirm),self.assertRaises(auth.AuthError):
                login.cmd_delete({"account":A,"confirm":confirm})
        self.remote.request.assert_not_called()

    def test_unknown_local_account_never_requests_remote_delete(self):
        with self.assertRaises(ValueError):
            login.cmd_delete({"account":"unknown-fixture","confirm":True})
        self.remote.request.assert_not_called()

    def test_local_io_failure_reports_partial_and_retry_completes(self):
        with mock.patch.object(prefs,"remove",side_effect=OSError("synthetic private path")):
            out=self.delete()
        self.assertFalse(out["ok"]);self.assertTrue(out["remote_deleted"])
        self.assertEqual(out["error"],"account_delete_local_failed")
        self.assertNotIn("synthetic private path",out["message"])
        self.assertTrue(auth.cache_is_deleted(A))
        self.assertTrue(self.delete()["ok"])
        self.assertEqual(json.loads(self.labels.read_text(encoding="utf-8")),{B:"keep-label"})

    def test_cache_commit_failure_does_not_claim_local_success(self):
        with mock.patch.object(auth,"atomic",side_effect=OSError("fixture")):
            out=self.delete()
        self.assertFalse(out["ok"]);self.assertTrue(out["remote_deleted"])
        self.assertEqual(self.read(),self.original)

    def test_repeated_delete_has_idempotent_local_effect(self):
        self.delete();before={p:p.read_bytes() for p in (self.cache,self.active,self.labels)}
        self.assertTrue(self.delete()["ok"])
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_tombstone_blocks_delayed_session_cache_update(self):
        self.delete()
        with self.assertRaises(auth.AuthError) as ctx:
            auth.cache_update(A,{"id":A,"session":"late-synthetic-session"})
        self.assertEqual(ctx.exception.code,"account_deleted")
        self.assertEqual([r["id"] for r in self.read()["accounts"]],[B])
        auth.cache_update(B,{"alias":"new keep label"})
        self.assertEqual(self.read()["deleted_account_ids"],[A])

    def test_new_account_id_is_not_blocked_by_old_tombstone(self):
        self.delete();auth.cache_update("fixture-new-id",{"id":"fixture-new-id","alias":"new binding"})
        self.assertIsNotNone(auth.cache_get("fixture-new-id"))
        self.assertIsNone(auth.cache_get(A))

    def test_stale_list_reply_does_not_resurrect_or_redisplay(self):
        self.delete()
        self.remote.request.side_effect=None
        self.remote.request.return_value={"accounts":[{"id":A,"alias":"late"},{"id":B,"alias":"kept"}]}
        out=login.cmd_list({})
        self.assertEqual([r["id"] for r in out["accounts"]],[B])
        self.assertIsNone(auth.cache_get(A))

    def test_inflight_session_finishing_after_delete_cannot_resurrect(self):
        entered=threading.Event();resume=threading.Event();errors=[]
        client=object.__new__(auth.Client)
        def reply(*args,**kwargs):
            entered.set()
            if not resume.wait(5):raise AssertionError("fixture resume timeout")
            return {"account":{"id":A,"session":"late fixture"}}
        client.request=reply
        def run():
            try:client.session(A)
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        self.assertTrue(entered.wait(5))
        try:self.assertTrue(self.delete()["ok"])
        finally:resume.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([x.code for x in errors],["account_deleted"])
        self.assertIsNone(auth.cache_get(A))

    def test_persistent_tombstone_is_seen_by_separate_python_process(self):
        self.delete()
        code="""import sys,json;sys.path.insert(0,sys.argv[1]);import xg_auth_client as a
try:
 a.cache_update(sys.argv[2],{'id':sys.argv[2],'session':'synthetic-late'})
 print(json.dumps({'unexpected_success':True}))
except a.AuthError as e:print(json.dumps({'error':e.code}))
"""
        env={k:os.environ[k] for k in ("SystemRoot","WINDIR") if k in os.environ}
        env.update(XG_DATA_DIR=str(self.data),PYTHONDONTWRITEBYTECODE="1",PYTHONNOUSERSITE="1",PATH="")
        p=subprocess.run([sys.executable,"-B","-X","utf8","-c",code,str(APP),A],cwd=self.data,
                         env=env,text=True,encoding="utf-8",capture_output=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout),{"error":"account_deleted"})
        self.assertIsNone(auth.cache_get(A))


class ControlGateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.patches=[
            mock.patch.object(api.xg_account,"account_by_id",side_effect=lambda i:{"id":i} if i in (A,B) else None),
            mock.patch.object(api.xg_account,"active_id",return_value=A),
            mock.patch.object(api,"_cleanup_busy",return_value=False),
            mock.patch.object(api,"_account_delete_processes",return_value=[]),
            mock.patch.object(api,"account_cmd",return_value={"ok":True,"deleted":True,"account":A}),
            mock.patch.object(api.xg_account,"cache_clear"),
            mock.patch.object(api.xg_runtime,"refresh"),
            mock.patch.object(api.xg_proc,"terminate",side_effect=AssertionError("must never auto-stop"))
        ]
        self.mocks=[p.start() for p in self.patches]
        for p in self.patches:self.addCleanup(p.stop)

    def test_confirmed_offline_account_calls_exact_delete_command(self):
        out=api.delete_account(A,True)
        self.assertTrue(out["ok"])
        self.mocks[4].assert_called_once_with("delete",{"account":A,"confirm":True})

    def test_running_account_requires_explicit_stop_not_auto_kill(self):
        self.mocks[3].return_value=[{"pid":123,"argv":["python","long_runner.py","--account",A]}]
        out=api.delete_account(A,True)
        self.assertEqual(out["error"],"account_running_stop_required")
        self.mocks[4].assert_not_called();self.mocks[7].assert_not_called()

    def test_busy_capture_or_build_refuses_delete(self):
        self.mocks[2].return_value=True
        self.assertEqual(api.delete_account(A,True)["error"],"task_busy")
        self.mocks[4].assert_not_called()

    def test_unknown_id_and_missing_confirmation_do_not_call_command(self):
        self.assertFalse(api.delete_account("unknown-fixture",True)["ok"])
        self.assertFalse(api.delete_account(A,"true")["ok"])
        self.mocks[4].assert_not_called()

    def test_partial_local_failure_invalidates_process_local_account_cache(self):
        self.mocks[4].return_value={"ok":False,"remote_deleted":True,"error":"account_delete_local_failed"}
        self.assertFalse(api.delete_account(A,True)["ok"])
        self.mocks[5].assert_called_once();self.mocks[6].assert_called_once_with(A)

    def test_actual_post_handler_requires_auth_origin_and_lock(self):
        body=json.dumps({"account":A,"confirm":True}).encode()
        def invoke(authed=True,origin=True):
            handler=object.__new__(api.Handler)
            handler.path="/xiaoguang/accounts/delete"
            handler.headers={"Content-Length":str(len(body))}
            handler.rfile=io.BytesIO(body);handler.authed=lambda:authed
            handler.same_origin=lambda:origin
            replies=[];handler.reply=lambda status,obj:replies.append((status,obj))
            handler.do_POST()
            return replies[0]
        self.assertEqual(invoke(False)[0],403)
        self.assertEqual(invoke(True,False)[0],403)
        self.mocks[4].assert_not_called()
        with api.LOCK:
            self.assertEqual(invoke()[0],409)
        self.mocks[4].assert_not_called()
        self.assertEqual(invoke()[0],200)
        self.mocks[4].assert_called_once()

    def test_process_selection_does_not_match_explicit_other_account(self):
        # Exercise the production selector rather than the per-test stub.
        source=(APP/"bot/control_api.py").read_text(encoding="utf-8")
        fn=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=="_account_delete_processes")
        ns={"xg_proc":api.xg_proc,"xg_runtime":api.xg_runtime,"account_process_matches":api.account_process_matches}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),"<account-process-selector>","exec"),ns)
        rows=[{"pid":101},{"pid":102}]
        with mock.patch.object(api.xg_proc,"find_any_script",return_value=rows),mock.patch.object(api.xg_proc,"argv_of",side_effect=lambda pid:["python","fixture.py","--account",A if pid==101 else B]):
            self.assertEqual(ns["_account_delete_processes"](A),[rows[0]])


class LeaseAndErrorTests(unittest.TestCase):
    def test_deleted_account_stops_lease_immediately_for_runner_and_chat(self):
        for refresh in (True,False):
            cancel=threading.Event();notify=mock.Mock();client=mock.Mock()
            client.session.side_effect=auth.AuthError("account_deleted",410)
            client.request.side_effect=auth.AuthError("account_deleted",410)
            auth._heartbeat_loop(A,cancel,notify,refresh_session=refresh,client_factory=lambda:client,clock=lambda:0)
            self.assertTrue(cancel.is_set())
            notify.assert_called_once_with("auth_service_status",error="account_deleted")

    def test_real_runner_segment_observes_heartbeat_cancellation(self):
        cancel=threading.Event()
        client=mock.Mock();client.session.side_effect=auth.AuthError("account_deleted",410)
        auth._heartbeat_loop(A,cancel,client_factory=lambda:client,clock=lambda:0)
        source=(APP/"bot/long_runner.py").read_text(encoding="utf-8")
        fn=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=="watch_segment")
        ns={"time":types.SimpleNamespace(monotonic=lambda:10),"FOLLOW_GRACE":1,"REJOIN_GRACE":1}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),"<real-runner-cancel>","exec"),ns)
        segment=mock.Mock();segment.wait.return_value=False
        ns["watch_segment"](cancel,segment,threading.Event(),None,set(),{})
        segment.set.assert_called_once()
        # Actual live connection receives this segment event, not a private
        # heartbeat-only flag. The chat loop likewise closes its websocket.
        calls=[n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Call)
               and isinstance(n.func,ast.Attribute) and n.func.attr=="run_connection"]
        self.assertGreaterEqual(len(calls),1)   # tf18：线上版只有一个带全部参数的调用
        self.assertTrue(all(len(n.args)>=3 and isinstance(n.args[2],ast.Name)
                            and n.args[2].id=="seg_cancel" for n in calls))
        chat=(APP/"bot/ai_companion.py").read_text(encoding="utf-8")
        self.assertIn("while ws.alive and not cancel.wait(1):",chat)
        self.assertIn("ws.close()",chat)

    def test_email_generic_failure_never_says_check_verification_code(self):
        client=mock.Mock();client.wait_job.return_value={"status":"failed","error":"login_failed"}
        out=login.result({},client)
        self.assertEqual(out["error"],"login_failed")
        self.assertIn("login_failed",out["message"])
        self.assertNotIn("核对验证码",out["message"])

    def test_specific_safe_error_code_is_visible(self):
        client=mock.Mock();client.wait_job.return_value={"status":"failed","error":"invalid_credentials"}
        out=login.result({},client)
        self.assertIn("邮箱或密码",out["message"])
        self.assertIn("invalid_credentials",out["message"])

    def test_binding_and_identity_errors_have_specific_messages(self):
        for code, phrase in (("account_bound_elsewhere","另一位"),("account_identity_changed","身份")):
            out=login.error_result(code)
            self.assertEqual(out["code"],code)
            self.assertIn(phrase,out["message"])
            self.assertIn(code,out["message"])

    def test_unknown_safe_error_code_remains_visible(self):
        out=login.error_result("fixture_upstream_denied")
        self.assertEqual(out["code"],"fixture_upstream_denied")
        self.assertIn("fixture_upstream_denied",out["message"])

    def test_untrusted_error_content_is_not_echoed(self):
        out=login.error_result("private/path <script>")
        self.assertEqual(out["code"],"login_failed")
        self.assertNotIn("private/path",out["message"])


class InterfaceTests(unittest.TestCase):
    def test_real_account_card_delete_scenarios(self):
        result=subprocess.run([str(ROOT/"runtime/node/node.exe"),str(ROOT/"tests/account_delete14_scenarios.cjs")],
                              cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"],8)


if __name__=="__main__":unittest.main()
