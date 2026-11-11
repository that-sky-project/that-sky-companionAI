"""Regressions for real startup failures: iOS login and append-only log isolation."""
import ast
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]


def function(path,name,namespace):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


class PlatformValidationTests(unittest.TestCase):
    def functions(self):
        for part in ('qfsky','escort-v31-server/qfsky'):
            namespace={'BUILD':421629,'current_join':SimpleNamespace(mode=lambda:'current'),
                       'uuid_raw':lambda s:uuid.UUID(s).bytes,'netcfg':SimpleNamespace(enabled=lambda:False),
                       'os':SimpleNamespace(environ={'QFLY_LOCAL_NETWORK_MODE':'enabled'})}
            yield function(ROOT/'app'/part/'connection0167.py','_validate_account',namespace)

    def account(self,platform):
        return {'id':'fixture-owned-account','renew':{'broker_account_id':'fixture-owned-account'},
                'channel':'netease','platform':platform,'user_uuid':'00000000-0000-4000-8000-000000000001',
                'session':'01'*16,'session2':'fixture-session2'}

    def test_ios_and_android_keep_original_platform_and_session(self):
        for validate in self.functions():
            for platform in ('ios','android'):
                row=self.account(platform);before=copy.deepcopy(row)
                validate(row,421629);self.assertEqual(row,before)

    def test_unknown_platform_and_channel_still_rejected(self):
        for validate in self.functions():
            for fields in ({'platform':'pc'},{'platform':''},{'channel':'other'}):
                with self.assertRaises(ValueError):validate({**self.account('ios'),**fields},421629)

    def test_build_and_ownership_and_session_still_checked(self):
        for validate in self.functions():
            with self.assertRaises(ValueError):validate(self.account('ios'),999999)
            for fields in ({'renew':{}},{'renew':{'broker_account_id':'other'}},{'user_uuid':'bad'},{'session':'bad'},{'session2':''}):
                with self.assertRaises(ValueError):validate({**self.account('ios'),**fields},421629)

    def test_network_offline_and_proxy_guard_still_checked(self):
        for validate in self.functions():
            for fields in ({'proxy_id':'other'},{'proxy_mode':'auto'},{'proxy_mode':'manual'}):
                with self.assertRaises(ValueError):validate({**self.account('ios'),**fields},421629)
            validate.__globals__['os'].environ['QFLY_LOCAL_NETWORK_MODE']='offline'
            with self.assertRaises(ValueError):validate(self.account('ios'),421629)


class StartupFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='xg-startup-log-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.go=self.root/'go.json';self.log=self.root/'go.log';self.runner=self.root/'candidate-runner.out'
        self.log.write_text('{"step":"lease","status":"stopped"}',encoding='utf-8')
        self.write_attempt()
        ns={'_json':lambda p:json.loads(p.read_text()),'GO':self.go,'GO_LOG':self.log,'OUT':self.root,
            'json':json,'xg_account':SimpleNamespace(active_id=lambda:'fixture-account')}
        self.failure=function(ROOT/'app/bot/xg_runtime.py','startup_failure',ns)

    def write_attempt(self,**extra):
        self.go.write_text(json.dumps({'account':'fixture-account','started_at':time.time()-2,'log_offset':0,**extra}),encoding='utf-8')

    def write_trace(self,message):
        return 'Traceback (most recent call last):\n  File "fixture.py", line 1\nValueError: '+message+'\n'

    def test_legacy_mixed_log_reports_current_platform_not_old_version(self):
        self.runner.write_text(self.write_trace('仅支持当前 0.16.8 / 421629 模式')+self.write_trace('当前已验证入口为网易安卓渠道'),encoding='utf-8')
        self.assertEqual(self.failure()['code'],'unsupported_account_platform')

    def test_new_attempt_reads_only_own_runner_bytes(self):
        old=self.write_trace('仅支持当前 0.16.8 / 421629 模式');new=self.write_trace('session_expired')
        self.runner.write_text(old+new,encoding='utf-8');self.write_attempt(runner_log_offset=len(old.encode()))
        self.assertEqual(self.failure()['code'],'login_expired')

    def test_new_unknown_error_never_falls_back_to_old_known_error(self):
        self.runner.write_text(self.write_trace('401 Unauthorized')+self.write_trace('unclassified_failure SECRET_SESSION'),encoding='utf-8')
        result=self.failure();self.assertEqual(result['code'],'startup_failed');self.assertNotIn('SECRET',json.dumps(result))

    def test_rotated_log_with_smaller_size_can_report_fresh_error(self):
        self.write_attempt(runner_log_offset=999999)
        self.runner.write_text(self.write_trace('map_capture_not_recorded_for_account'),encoding='utf-8')
        self.assertEqual(self.failure()['code'],'account_capture_missing')

    def test_unchanged_runner_file_adds_no_stale_failure(self):
        old=self.write_trace('仅支持当前 0.16.8 / 421629 模式')
        self.runner.write_text(old,encoding='utf-8');self.write_attempt(runner_log_offset=len(old.encode()))
        self.assertEqual(self.failure()['code'],'startup_failed')

    def test_old_attempt_or_other_account_does_not_leak(self):
        self.write_attempt(account='different');self.assertIsNone(self.failure())
        self.write_attempt();self.runner.write_text(self.write_trace('仅支持当前 0.16.8 / 421629 模式'),encoding='utf-8')
        os.utime(self.runner,(1,1));self.assertEqual(self.failure()['code'],'startup_failed')

    def test_good_launch_is_not_an_error_even_with_old_trace(self):
        self.log.write_text('{"step":"lease","status":"running"}',encoding='utf-8')
        self.runner.write_text(self.write_trace('401 Unauthorized'),encoding='utf-8')
        self.assertIsNone(self.failure())


if __name__=='__main__':unittest.main()
