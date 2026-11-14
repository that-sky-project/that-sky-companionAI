"""Authenticated loopback cleanup API tests; only synthetic workspace fixtures."""
from pathlib import Path
import http.client
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT/'app'
sys.path[:0] = [str(APP), str(APP/'bot'), str(APP/'launcher')]
import control_api as api


class CleanupApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(api.Handler):
            server_token = 'synthetic-local-test-token'
            def log_message(self, *_args):
                pass
        cls.server = api.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xg-reset-api-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.terrain = self.data/'game/0.16.8-421629/terrain'
        self.terrain.mkdir(parents=True)
        self.mesh = self.terrain/'fixture.mesh.bin'
        self.mesh.write_bytes(b'synthetic-terrain')
        self.selection = {'terrain':True,'names':False,'catalog':False,'captures':False,'props':False}
        self.online = Mock(return_value=False)
        self.busy = Mock(return_value=False)
        self.after_reset = Mock()
        self.patches = [patch.object(api.xg_paths, 'DATA', self.data),
                        patch.object(api, '_CLEANUP_SERVICE', None),
                        patch.object(api, '_CLEANUP_ROOT', None),
                        patch.object(api, '_cleanup_online', self.online),
                        patch.object(api, '_cleanup_busy', self.busy),
                        patch.object(api.wizard, 'after_data_reset', self.after_reset)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def request(self, action, payload=None, *, auth=True, origin=None):
        headers = {'Content-Type':'application/json',
                   'Origin':origin or f'http://127.0.0.1:{self.port}'}
        if auth:
            headers['Cookie'] = api.COOKIE+'=synthetic-local-test-token'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('POST', '/xiaoguang/api/wizard/cleanup/'+action,
                         json.dumps(payload or {}).encode(), headers)
            response=conn.getresponse()
            return response.status,json.loads(response.read())
        finally:
            conn.close()

    def preview(self):
        status,doc=self.request('preview', {'selection':self.selection})
        self.assertEqual(status,200,doc)
        self.assertTrue(doc['ok'],doc)
        self.assertTrue(doc['plan_id'])
        self.assertTrue(doc['items'])
        self.assertGreater(doc['total_bytes'],0)
        for row in doc['items']:
            self.assertIsInstance(row['path'],str)
            self.assertIsInstance(row['category'],str)
            self.assertIsInstance(row['files'],int)
            self.assertGreaterEqual(row['files'],0)
        return doc

    def execute(self):
        plan=self.preview()
        status,doc=self.request('execute',{'plan_id':plan['plan_id'],'confirm':True})
        self.assertEqual(status,200,doc)
        self.assertTrue(doc['ok'],doc)
        return doc

    def test_auth_required_before_preview(self):
        code,_=self.request('preview',{'selection':self.selection},auth=False)
        self.assertEqual(code,403)
        self.assertTrue(self.mesh.exists())
        self.assertFalse((self.data/'trash').exists())

    def test_foreign_origin_rejected(self):
        code,_=self.request('preview',{'selection':self.selection},origin='https://not-local.invalid')
        self.assertEqual(code,403)
        self.assertTrue(self.mesh.exists())

    def test_global_lock_rejects_overlapping_operation(self):
        with api.LOCK:
            code,doc=self.request('preview',{'selection':self.selection})
        self.assertEqual(code,409,doc)
        self.assertTrue(self.mesh.exists())

    def test_confirmation_must_be_literal_boolean_true(self):
        for confirm in (False,'true',1,None):
            with self.subTest(confirm=confirm):
                plan=self.preview()
                code,doc=self.request('execute',{'plan_id':plan['plan_id'],'confirm':confirm})
                self.assertEqual(code,409,doc)
                self.assertFalse(doc['ok'])
                self.assertTrue(self.mesh.exists())
        self.after_reset.assert_not_called()

    def test_online_transition_after_preview_rejected(self):
        plan=self.preview()
        self.online.return_value=True
        code,doc=self.request('execute',{'plan_id':plan['plan_id'],'confirm':True})
        self.assertEqual(code,409,doc)
        self.assertFalse(doc['ok'])
        self.assertTrue(self.mesh.exists())
        self.after_reset.assert_not_called()

    def test_running_capture_or_task_rejected(self):
        plan=self.preview()
        self.busy.return_value=True
        code,doc=self.request('execute',{'plan_id':plan['plan_id'],'confirm':True})
        self.assertEqual(code,409,doc)
        self.assertTrue(self.mesh.exists())

    def test_move_restore_and_state_hook(self):
        moved=self.execute()
        self.assertFalse(self.terrain.exists())
        self.assertTrue(list((self.data/'trash').rglob('fixture.mesh.bin')))
        self.after_reset.assert_called_once_with('execute',self.selection,batch_id=moved['batch_id'])
        code,plan=self.request('restore-preview')
        self.assertEqual(code,200,plan)
        code,doc=self.request('restore',{'plan_id':plan['plan_id'],'confirm':True})
        self.assertEqual(code,200,doc)
        self.assertEqual(self.mesh.read_bytes(),b'synthetic-terrain')
        self.assertEqual(self.after_reset.call_args.args,('restore',self.selection))
        self.assertEqual(self.after_reset.call_args.kwargs,{'batch_id':moved['batch_id']})

    def test_purge_requires_second_confirmation(self):
        self.execute()
        code,plan=self.request('purge-preview')
        self.assertEqual(code,200,plan)
        self.assertTrue(all(isinstance(row['files'],int) and row['category']=='trash'
                            for row in plan['items']))
        code,doc=self.request('purge',{'plan_id':plan['plan_id']})
        self.assertEqual(code,409,doc)
        self.assertTrue(list((self.data/'trash').rglob('fixture.mesh.bin')))
        code,plan=self.request('purge-preview')
        self.assertEqual(code,200,plan)
        code,doc=self.request('purge',{'plan_id':plan['plan_id'],'confirm':True})
        self.assertEqual(code,200,doc)
        self.assertFalse(list((self.data/'trash').rglob('fixture.mesh.bin')))

    def test_unknown_action_and_bad_selection_are_noops(self):
        for action,payload in [('delete-everything',{}),('preview',{'selection':[]} ),
                               ('execute',{'plan_id':'../secrets','confirm':True})]:
            with self.subTest(action=action):
                code,doc=self.request(action,payload)
                self.assertEqual(code,409,doc)
                self.assertFalse(doc['ok'])
                self.assertTrue(self.mesh.exists())

    def test_state_hook_failure_does_not_hide_completed_move(self):
        self.after_reset.side_effect=OSError('synthetic state marker access error')
        result=self.execute()
        self.assertFalse(self.terrain.exists())
        self.assertIn('state_warning',result)
        self.assertTrue(result['ok'])

    def test_empty_cleanup_can_reset_registration_state(self):
        selection={'terrain':False,'names':False,'catalog':False,'captures':True,'props':False}
        code,plan=self.request('preview',{'selection':selection})
        self.assertEqual(code,200,plan)
        self.assertEqual(plan['items'],[])
        code,result=self.request('execute',{'plan_id':plan['plan_id'],'confirm':True})
        self.assertEqual(code,200,result)
        self.after_reset.assert_called_once_with('execute',selection,batch_id=result['batch_id'])
        self.assertTrue(self.mesh.exists())

    def test_incomplete_rollback_sets_resource_reset_gate(self):
        service=Mock()
        service.execute.return_value={'ok':False,'error_code':'rollback_incomplete',
                                      'selection':self.selection,'batch_id':'synthetic-batch'}
        with patch.object(api,'_cleanup_service',return_value=service):
            code,result=self.request('execute',{'plan_id':'synthetic-plan','confirm':True})
        self.assertEqual(code,409,result)
        self.after_reset.assert_called_once_with('execute',self.selection,batch_id='synthetic-batch')


if __name__ == '__main__':
    unittest.main(verbosity=2)
