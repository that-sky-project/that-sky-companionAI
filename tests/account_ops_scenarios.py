"""Offline account operations, with no game or authentication-service requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

APP = Path(__file__).resolve().parents[1] / 'app'
temp = tempfile.TemporaryDirectory(prefix='xg-account-ops-tests-')
os.environ.update(XG_DATA_DIR=temp.name, SKY_AI_ROOT=str(APP), PYTHONDONTWRITEBYTECODE='1')
sys.path[:0] = [str(APP), str(APP/'bot')]
import requests
requests.sessions.Session.request = Mock(side_effect=AssertionError('Unexpected external request'))
import account_login as login
import account_preferences as prefs
import control_api as api
import xg_paths as paths
import xg_runtime as runtime
from qfsky import version


class Cases(unittest.TestCase):
    def setUp(self):
        paths.CONTROL.mkdir(parents=True, exist_ok=True)
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        paths.LEASE_DIR.mkdir(parents=True, exist_ok=True)
        self.row = {'id':'fixture-a','alias':'小光·12345','platform':'netease','status':'ready','created_at':0}
        self.known = patch.object(prefs, 'cache_get', side_effect=lambda i:self.row if i=='fixture-a' else None)
        self.known.start();self.addCleanup(self.known.stop)
        self.active = patch.object(runtime.xg_account, 'active_id', return_value='fixture-a')
        self.active.start();self.addCleanup(self.active.stop)
        for path in (paths.CONTROL/'account_nicknames.json',runtime.GO,runtime.GO_LOG,paths.LEASE_DIR/'candidate-runner.out'):
            path.unlink(missing_ok=True)

    def test_version_never_tracks_store_release(self):
        with patch.object(version,'_huawei_fetch',side_effect=AssertionError('Must not fetch store')):
            version._cached=('9.99.99.999999',time.time())
            self.assertEqual(version.cn_version(),'0.16.7.421589')
            self.assertEqual(version.cn_build(),421589)

    def test_labels_survive_service_alias_refresh(self):
        prefs.save('fixture-a','  大号  ')
        self.assertEqual(login.view(self.row)['alias'],'大号')
        self.row['alias']='小光·NEW'
        value=login.view(self.row)
        self.assertEqual(value['nickname'],'大号')
        self.assertEqual(value['service_alias'],'小光·NEW')
        self.assertEqual(login.view({'id':'fixture-b','alias':'另一个'})['alias'],'另一个')

    def test_label_validation_clear_and_no_account_creation(self):
        for bad in ('x'*41,'a\nb','a\x00b','a\u202eb',None):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):prefs.save('fixture-a',bad)
        with self.assertRaises(ValueError):prefs.save('missing','x')
        prefs.save('fixture-a','大号');prefs.save('fixture-a','   ')
        self.assertEqual(login.view(self.row)['alias'],'小光·12345')
        self.assertFalse((paths.CONTROL/'account_nicknames.json.pending').exists())

    def test_remark_response_never_contains_credentials(self):
        self.row.update(session='SECRET_SESSION',password='SECRET_PASSWORD')
        result=login.cmd_remark({'account':'fixture-a','nickname':'测试号'})
        self.assertTrue(result['ok']);self.assertNotIn('SECRET',json.dumps(result))

    def test_renew_exact_id_no_account_switch_or_secret_response(self):
        paths.ACTIVE_ACCOUNT.write_text('fixture-b',encoding='utf-8')
        client=Mock();client.renew.return_value={**self.row,'session':'SECRET_SESSION','password':'SECRET_PASSWORD'}
        with patch.object(login,'Client',return_value=client):result=login.cmd_renew({'account':'fixture-a'})
        client.renew.assert_called_once_with('fixture-a')
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertEqual(paths.ACTIVE_ACCOUNT.read_text(),'fixture-b')

    def test_renew_unknown_never_contacts_service(self):
        with patch.object(login,'Client') as client:
            with self.assertRaises(ValueError):login.cmd_renew({'account':'missing'})
            client.assert_not_called()

    def test_live_account_is_not_renewed(self):
        with patch.object(api.xg_account,'account_by_id',return_value=self.row),patch.object(api.xg_proc,'find_any_script',return_value=[{'pid':1}]),patch.object(api.xg_proc,'argv_of',return_value=['python','runner','--account','fixture-a']),patch.object(api,'account_cmd') as command:
            result=api.renew_account('fixture-a')
            self.assertFalse(result['ok']);self.assertIn('先让小光下线',result['message']);command.assert_not_called()

    def test_other_online_account_is_not_stopped(self):
        argv={1:['runner','--account=fixture-b'],2:['chat','--account','fixture-a'],3:['chat','--account','fixture-b']}
        with patch.object(api.xg_account,'account_by_id',return_value=self.row),patch.object(api.xg_proc,'find_any_script',return_value=[{'pid':1}]),patch.object(api.xg_proc,'find_script',return_value=[{'pid':2},{'pid':3}]),patch.object(api.xg_proc,'argv_of',side_effect=lambda pid:argv[pid]),patch.object(api.xg_proc,'terminate') as terminate,patch.object(api,'account_cmd',return_value={'ok':True}) as command,patch.object(api.xg_account,'cache_clear'):
            self.assertTrue(api.renew_account('fixture-a')['ok'])
            terminate.assert_called_once_with(2,grace=3.0)
            command.assert_called_once_with('renew',{'account':'fixture-a'})

    def attempt(self,account='fixture-a',log_offset=0,started_at=None):
        paths.write_json(runtime.GO,{'account':account,'started_at':started_at or time.time()-1,'log_offset':log_offset})

    def test_startup_error_version_classification_and_no_log_leak(self):
        self.attempt();runtime.GO_LOG.write_text('{"step":"lease","status":"stopped"}\n',encoding='utf-8')
        (paths.LEASE_DIR/'candidate-runner.out').write_text('SECRET_SESSION\nValueError: 仅支持当前 0.16.7 / 421589 模式',encoding='utf-8')
        result=runtime.startup_failure()
        self.assertEqual(result['code'],'protocol_version_mismatch');self.assertNotIn('SECRET',json.dumps(result))

    def test_old_or_other_account_errors_are_ignored(self):
        runtime.GO_LOG.write_text('Traceback (most recent call last)\n',encoding='utf-8')
        self.attempt(account='fixture-b');self.assertIsNone(runtime.startup_failure())
        self.attempt(log_offset=runtime.GO_LOG.stat().st_size);self.assertIsNone(runtime.startup_failure())

    def test_no_error_on_running_lease(self):
        self.attempt();runtime.GO_LOG.write_text('{"step":"lease","status":"running"}',encoding='utf-8')
        self.assertIsNone(runtime.startup_failure())

    def test_no_stale_runner_trace_misattribution(self):
        self.attempt(started_at=time.time()+5)
        runtime.GO_LOG.write_text('Traceback (most recent call last)',encoding='utf-8')
        (paths.LEASE_DIR/'candidate-runner.out').write_text('401 Unauthorized',encoding='utf-8')
        self.assertEqual(runtime.startup_failure()['code'],'startup_failed')

    def test_go_writes_account_bound_attempt_atomically(self):
        row={'id':3526133726,'name':'遇境','scene':'CandleSpace'}
        with patch.object(runtime,'startable_maps',return_value=[row]),patch.object(runtime,'going',return_value=None),patch('xg_auth_client.Client') as client,patch.object(runtime.xg_proc,'spawn_detached') as spawn:
            runtime.go(row['id'])
            attempt=paths.read_json(runtime.GO,{})
            self.assertEqual(attempt['account'],'fixture-a');self.assertGreater(attempt['started_at'],0)
            client.return_value.session.assert_called_once_with('fixture-a');spawn.assert_called_once()

    def test_live_failure_hidden_during_new_start(self):
        with patch.object(runtime,'pids',return_value=[]),patch.object(runtime,'_json',return_value={}),patch.object(runtime,'startable_maps',return_value=[]),patch.object(runtime,'_records',return_value={}),patch.object(runtime,'going',return_value={'map':1}),patch.object(runtime,'startup_failure') as failure:
            result=runtime.live('')
            self.assertIsNone(result['startupError']);failure.assert_not_called()


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
    print(json.dumps({'passed':result.testsRun-len(result.failures)-len(result.errors),'failed':len(result.failures)+len(result.errors)}))
    temp.cleanup()
    raise SystemExit(not result.wasSuccessful())
