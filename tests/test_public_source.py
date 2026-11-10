"""Offline checks for the public client boundary; never log into a game."""
from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
EXCLUDE={'.git','.venv','.build-env','.build','runtime','node_modules','__pycache__'}

class PublicSourceTests(unittest.TestCase):
    def test_python_syntax(self):
        files=[p for p in ROOT.rglob('*.py') if not any(v in EXCLUDE for v in p.relative_to(ROOT).parts)]
        self.assertGreater(len(files),50)
        for path in files:
            with self.subTest(file=path.relative_to(ROOT).as_posix()):
                ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))

    def test_private_implementation_and_runtime_state_absent(self):
        forbidden={'sky9393.py','devfixed.py','devreg.py','bili.py','huawei.py','vivo.py','honor.py'}
        for filename in forbidden:
            self.assertFalse((APP/'qfsky'/filename).exists(),filename)
        self.assertFalse((ROOT/'auth_service').exists())
        self.assertFalse((ROOT/'private').exists())
        self.assertFalse(list(APP.rglob('*.b64')))
        self.assertFalse(list(APP.rglob('accounts.json')))
        pins=APP/'escort-v31-server/qfsky/map_long0167/catalog_account_pins.json'
        self.assertEqual(json.loads(pins.read_text()),{})

    def test_real_client_imports_offline(self):
        code=r'''
import socket,sys
sys.path[:0]=[APP,APP+'/bot']
def denied(*args,**kwargs):raise AssertionError('Unexpected network access')
socket.socket.connect=denied
socket.socket.connect_ex=denied
socket.create_connection=denied
import urllib.request
urllib.request.OpenerDirector.open=denied
import requests
requests.sessions.Session.request=denied
import xg_auth_client,xg_service_identity
from qfsky import cnlogin,store,tasks,skyapi
import account_login,local_device,login_secret,control_api
import runpy
runpy.run_path(APP+'/launcher/supervisor_sixhour.py',run_name='offline_supervisor')
assert not hasattr(cnlogin,'login_with_email')
assert not hasattr(cnlogin,'_auth_login')
assert callable(cnlogin.renew)
assert local_device.status_all()['ok'] is False
assert login_secret.load('fixture-account') is None
assert login_secret.exists('fixture-account') is False
assert account_login.COMMANDS['friends']
assert account_login.COMMANDS['owner']
print('public-imports-offline-ok')
'''
        with tempfile.TemporaryDirectory(prefix='companion-client-test-') as tmp:
            env=dict(os.environ,XG_DATA_DIR=tmp,QFLY_DATA_DIR=str(Path(tmp)/'runtime/runtime-data'),PYTHONDONTWRITEBYTECODE='1')
            result=subprocess.run([sys.executable,'-X','utf8','-B','-c','APP='+repr(str(APP))+'\n'+code],cwd=APP,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
            self.assertEqual(result.returncode,0,result.stderr[-2000:])
            self.assertIn('public-imports-offline-ok',result.stdout)

    def test_legacy_website_identity_graph_removed(self):
        removed={'users','apikeys','avatars','chat','notify'}
        for name in removed:self.assertFalse((APP/'qfsky'/(name+'.py')).exists(),name)
        for path in APP.rglob('*.py'):
            if any(part in EXCLUDE for part in path.relative_to(APP).parts):continue
            tree=ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name,removed,{"path":str(path),"import":alias.name})
                        self.assertNotIn(alias.name,{"qfsky."+n for n in removed})
                elif isinstance(node,ast.ImportFrom):
                    module=node.module or ''
                    self.assertNotIn(module,removed)
                    self.assertNotIn(module,{"qfsky."+n for n in removed})
                    if module in ('','qfsky'):
                        self.assertFalse({a.name for a in node.names}&removed,str(path))
                elif isinstance(node,ast.Constant) and isinstance(node.value,str):
                    self.assertNotIn(node.value,{"qfsky."+n for n in removed},str(path))

    def test_engine_lockfiles_match_declared_dependencies(self):
        for directory in (APP/'engine',APP/'xg_gamedata/terrain/pipeline/engine'):
            package=json.loads((directory/'package.json').read_text())
            lock=json.loads((directory/'package-lock.json').read_text())
            self.assertEqual(lock['packages']['']['dependencies'],package['dependencies'])

    def test_release_and_entrypoints_exist(self):
        self.assertEqual(json.loads((ROOT/'BUILD.json').read_text())['client_revision'],'r10')
        for rel in ('desktop.py','tools/build_desktop.ps1','app/bot/control_api.py','app/bot/account_login.py','app/web/desktop.html','app/web/desktop.js','app/web/desktop.css','app/launcher/supervisor_sixhour.py','LICENSE','NOTICE','THIRD_PARTY_NOTICES'):
            self.assertTrue((ROOT/rel).is_file(),rel)

if __name__=='__main__':unittest.main()