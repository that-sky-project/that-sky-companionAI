import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
spec = importlib.util.spec_from_file_location("verification_account_adapter", ROOT / "app/bot/account_login.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

class FakeClient:
    def __init__(self, status="ready", error=None):self.status=status;self.calls=[];self.error=error
    def request(self, method, path, data=None):
        self.calls.append((method,path))
        if self.error:raise adapter.AuthError(self.error,410)
        return {"status":self.status,"job_id":"fixture-job","account_id":"fixture-account"}
    def wait_job(self, job):return {**job,"status":"ready"}
    def session(self, identity):return {"id":identity,"alias":"fixture","platform":"android","status":"ok","created_at":0}

class VerificationAdapterTests(unittest.TestCase):
    def test_completed_confirmation_reads_result_without_posting(self):
        client=FakeClient()
        with patch.object(adapter,"Client",return_value=client),patch.object(adapter,"view",side_effect=lambda a:a):result=adapter.cmd_confirm({"ticket":"fixture-job"})
        self.assertTrue(result["ok"])
        self.assertEqual([m for m,p in client.calls],["GET"])

    def test_inflight_confirmation_reads_result_without_duplicate_submit(self):
        client=FakeClient("running")
        with patch.object(adapter,"Client",return_value=client),patch.object(adapter,"view",side_effect=lambda a:a):result=adapter.cmd_confirm({"ticket":"fixture-job"})
        self.assertTrue(result["ok"])
        self.assertEqual([m for m,p in client.calls],["GET"])

    def test_waiting_confirmation_submits_once(self):
        client=FakeClient("needs_sms")
        with patch.object(adapter,"Client",return_value=client),patch.object(adapter,"view",side_effect=lambda a:a):result=adapter.cmd_confirm({"ticket":"fixture-job"})
        self.assertTrue(result["ok"])
        self.assertEqual([m for m,p in client.calls],["GET","POST"])

    def test_expired_or_cleaned_task_has_specific_message(self):
        for code in ("login_expired","not_found"):
            client=FakeClient(error=code);output=io.StringIO()
            with patch.object(adapter,"Client",return_value=client),patch.object(sys,"stdin",io.StringIO('{"ticket":"fixture-job"}')),contextlib.redirect_stdout(output):adapter.main(["account_login.py","confirm"])
            result=json.loads(output.getvalue())
            self.assertEqual(result["error"],"login_expired")
            self.assertIn("已过期",result["message"])
            self.assertNotIn("操作未完成",result["message"])

    def test_sync_pending_does_not_claim_sms_was_not_sent(self):
        class Pending:
            def wait_job(self,job):return job
        result=adapter.result({"status":"needs_sms","job_id":"fixture-job","error":"verification_pending","challenge":{"kind":"upload_sms"}},Pending())
        self.assertIn("还在同步",result["message"])
