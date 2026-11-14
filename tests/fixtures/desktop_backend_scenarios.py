"""Real local Handler + real PCRE2 tests, synthetic data only, no game/services."""
from pathlib import Path
import ast, contextlib, concurrent.futures, hashlib, http.client, json, os
import socket, subprocess, sys, tempfile, threading, time, unittest
from unittest import mock

S=Path(__file__).resolve().parents[2]
# All fixture state, including native rule input, remains inside the workspace.
TEMP_ROOT=S.parent/'test-tmp'; TEMP_ROOT.mkdir(parents=True,exist_ok=True)
T=tempfile.TemporaryDirectory(prefix='xg-desktop-backend-',dir=TEMP_ROOT)
D=Path(T.name)
os.environ['XG_DATA_DIR']=str(D/'data')
os.environ['SKY_AI_ROOT']=str(S/'app')
os.environ['XG_STARTUP_DIR']=str(D/'Startup')
for name in ('XG_RELEASE','XG_PILOT_CONFIG','XG_SUPERVISOR','XG_PYTHONW','WSCRIPT_EXE'):
    os.environ.pop(name,None)
sys.path[:0]=[str(S/'app'),str(S/'app/bot'),str(S/'app/launcher')]
real_connect=socket.socket.connect

def connect_local(sock,address):
    if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','localhost','::1'):
        raise AssertionError('test forbids upstream socket connection')
    return real_connect(sock,address)
network_guard=mock.patch.object(socket.socket,'connect',connect_local)
process_guard=mock.patch.object(subprocess,'Popen',side_effect=AssertionError('test forbids subprocess launch'))
network_guard.start();process_guard.start()
import control_api,owner_title,textcheck,autostart,xg_paths

# Authored synthetic rules only. The counts exercise readiness without shipping,
# opening or deriving any SDK/cache rule data. Most rules target impossible test
# markers; one deliberate intercept exercises the existing blocked-title cases.
SYNTHETIC_COUNTS={'shield':689,'intercept':88,'replace':2,'remind':1}
SYNTHETIC_SUBJECT = """# Synthetic normalization, not an SDK implementation.
import unicodedata
def make_subject(text, group, *, level='1', channel='chat_max'):
    return unicodedata.normalize('NFKC', text).replace('\\u200b', '')
"""

def write_synthetic_rules(root, *, invalid=False):
    root.mkdir(parents=True,exist_ok=True)
    (root/'sdk_subject.py').write_text(SYNTHETIC_SUBJECT,encoding='utf-8')
    (root/'t2s.json').write_text('{}\n',encoding='utf-8')
    rows=[]
    for group,count in SYNTHETIC_COUNTS.items():
        for number in range(count):
            pattern=f'__synthetic_{group}_{number:04d}__'
            if group=='intercept' and number==0: pattern='主人'
            if group=='shield' and number==0: pattern='SyntheticBlock'
            if invalid and group=='shield' and number==0: pattern='('
            rows.append({'group':group,'id':f'synthetic-{group}-{number:04d}',
                         'engine_pattern':pattern,'compileOptions':'00000000'})
    (root/'cached_patterns.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
    return root

SYNTHETIC_RULES=write_synthetic_rules(Path(xg_paths.GAME_DIR)/'text-rules')

# The HTTP handler is real; only runtime/process observations are stubbed.
running_guard=mock.patch.object(control_api,'chat_running',return_value=False)
find_guard=mock.patch.object(control_api.xg_proc,'find_script',return_value=[])
running_guard.start();find_guard.start()
CONTROL=D/'data/control'; CONTROL.mkdir(parents=True,exist_ok=True)

class TestHandler(control_api.Handler):
    server_token='fixture-token-never-used-outside-tests'
    def log_message(self,*args): pass

server=control_api.ThreadingHTTPServer(('127.0.0.1',0),TestHandler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
port=server.server_address[1]

def request(method,path,data=None,token=True,origin=None):
    headers={}
    if token: headers['Cookie']=control_api.COOKIE+'='+TestHandler.server_token
    if origin is not None: headers['Origin']=origin
    raw=None if data is None else json.dumps(data,ensure_ascii=True).encode()
    if raw is not None: headers['Content-Type']='application/json'
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
    try:
        c.request(method,path,body=raw,headers=headers)
        r=c.getresponse();return r.status,json.loads(r.read())
    finally:c.close()

def seed_title(title='哥哥'):
    owner_title.FILE.parent.mkdir(parents=True,exist_ok=True)
    owner_title.FILE.write_text(title,encoding='utf-8')

class TriggerTests(unittest.TestCase):
    def setUp(self):
        for p in (CONTROL/'chat_trigger.json', CONTROL/'chat_trigger.json.pending'):
            p.unlink(missing_ok=True)
    def test_01_default_get_never_writes(self):
        code,out=request('GET','/xiaoguang/chat/trigger')
        self.assertEqual((code,out),(200,{'ok':True,'mode':'prefix','prefix':'/'}))
        self.assertFalse((CONTROL/'chat_trigger.json').exists())
    def test_02_authenticated_prefix_unicode_and_immediate_get(self):
        for prefix in ('/','小光','✨','你好呀','🌤️','!!','  @  '):
            with self.subTest(prefix=prefix):
                code,out=request('POST','/xiaoguang/chat/trigger',{'mode':'prefix','prefix':prefix},origin=f'http://127.0.0.1:{port}')
                self.assertEqual(code,200);self.assertEqual(out['prefix'],prefix.strip())
                self.assertEqual(request('GET','/chat/trigger')[1]['prefix'],prefix.strip())
                self.assertFalse((CONTROL/'chat_trigger.json.pending').exists())
    def test_03_bad_prefix_keeps_previous_value(self):
        request('POST','/chat/trigger',{'prefix':'!'})
        previous=(CONTROL/'chat_trigger.json').read_bytes()
        for prefix in ('','   ','abcd',None,32,[],{},'\t/','/\n','a\r','a\x00','\u200b','a\u2028','a\u2029','\ud800'):
            with self.subTest(prefix=repr(prefix)):
                code,out=request('POST','/chat/trigger',{'mode':'prefix','prefix':prefix})
                self.assertEqual(code,400);self.assertFalse(out['ok'])
                self.assertEqual((CONTROL/'chat_trigger.json').read_bytes(),previous)
    def test_04_modes_are_explicit_and_all_legacy_supported(self):
        self.assertEqual(request('POST','/chat/trigger',{'mode':'all','prefix':'/'} )[0],200)
        self.assertEqual(request('GET','/chat/trigger')[1]['mode'],'all')
        for mode in ('ALL','',None,True,1,{}):
            self.assertEqual(request('POST','/chat/trigger',{'mode':mode,'prefix':'/'})[0],400)
    def test_05_authentication_origin_and_lock(self):
        self.assertEqual(request('POST','/chat/trigger',{'prefix':'/'},token=False)[0],403)
        self.assertEqual(request('GET','/chat/trigger',token=False)[0],403)
        self.assertEqual(request('POST','/chat/trigger',{'prefix':'/'},origin='https://unrelated.invalid')[0],403)
        with control_api.LOCK:
            self.assertEqual(request('POST','/chat/trigger',{'prefix':'/'})[0],409)
        self.assertFalse((CONTROL/'chat_trigger.json').exists())
    def test_06_non_object_payload_rejected(self):
        for value in ([],['a'],'hello',123,False):
            self.assertEqual(request('POST','/chat/trigger',value)[0],400)
    def test_07_atomic_replace_failure_keeps_previous(self):
        request('POST','/chat/trigger',{'prefix':'/'})
        previous=(CONTROL/'chat_trigger.json').read_bytes()
        with mock.patch.object(xg_paths.os,'replace',side_effect=OSError('synthetic disk error')):
            code,out=request('POST','/chat/trigger',{'prefix':'!'})
        self.assertEqual(code,500);self.assertFalse(out['ok'])
        self.assertEqual((CONTROL/'chat_trigger.json').read_bytes(),previous)
    def test_27_other_get_routes_still_dispatch(self):
        with mock.patch.object(control_api,'status',return_value={'ok':True,'online':False}),mock.patch.object(control_api,'persona_cmd',return_value={'ok':True,'active':'mengxin','personas':[]}):
            self.assertEqual(request('GET','/status'),(200,{'ok':True,'online':False}))
            self.assertEqual(request('GET','/personas'),(200,{'ok':True,'active':'mengxin','personas':[]}))
        self.assertEqual(request('GET','/chat/trigger')[0],200)
    def test_08_corrupt_get_recovers_read_only(self):
        p=CONTROL/'chat_trigger.json';p.write_text('[1,2]',encoding='utf-8')
        self.assertEqual(request('GET','/chat/trigger')[1]['prefix'],'/')
        self.assertEqual(p.read_text(),'[1,2]')

class TitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=textcheck.engine();cls.engine.require_ready()
    def setUp(self): seed_title()
    def test_09_real_rules_compile_and_title_outcomes(self):
        e=self.engine
        self.assertEqual(sum(map(len,e.rules.values())),780)
        self.assertFalse(e.failed)
        self.assertEqual({g:len(v) for g,v in e.rules.items()},{'shield':689,'intercept':88,'replace':2,'remind':1})
        self.assertTrue(owner_title.check('哥哥')[0]);self.assertTrue(owner_title.check('姐姐')[0])
        self.assertFalse(owner_title.check('主人')[0])
        self.assertTrue(any(g=='intercept' for g,r in e.check('主人',textcheck.BLOCKING)))
    def test_10_shape_and_all_reserved_words(self):
        for title in ('','七个字就是太长了','hello!!','兄 姐',123,None,*owner_title.RESERVED):
            with self.subTest(title=title):self.assertFalse(owner_title.check(title)[0])
    def test_11_contexts_are_checked_and_blocking_rejects(self):
        fake=mock.Mock();fake.check.side_effect=[[],[],[('shield','test')]]
        with mock.patch.object(textcheck,'engine',return_value=fake):
            ok,msg=owner_title.set_title('姐姐')
        self.assertFalse(ok);self.assertEqual(owner_title.get(),'哥哥')
        self.assertIn('姐姐，你来啦',msg)
        self.assertEqual(fake.check.call_count,3);fake.require_ready.assert_called_once()
    def test_12_missing_rules_basic_checks_and_honest_save(self):
        missing=D/'missing_rules'
        with mock.patch.object(textcheck,'_rules_dir',return_value=missing),mock.patch.object(textcheck,'_engine',None):
            with self.assertRaises(textcheck.RulesUnavailable): textcheck.Engine()
            ok,msg=owner_title.set_title('姐姐')
        self.assertTrue(ok);self.assertEqual(owner_title.get(),'姐姐')
        self.assertIn('未进行本地词表审核',msg)
        self.assertNotIn('审核通过',msg)
    def test_13_real_compile_failure_fail_closed(self):
        rules=write_synthetic_rules(D/'invalid_rules',invalid=True)
        with mock.patch.object(textcheck,'_rules_dir',return_value=rules),mock.patch.object(textcheck,'_engine',None):
            bad=textcheck.engine();self.assertEqual(len(bad.failed),1)
            self.assertFalse(owner_title.set_title('姐姐')[0])
        self.assertEqual(owner_title.get(),'哥哥')
    def test_14_empty_blocking_group_fails_closed(self):
        with mock.patch.object(self.engine,'rules',{'shield':[],'intercept':[]}):
            self.assertFalse(owner_title.set_title('姐姐')[0])
        self.assertEqual(owner_title.get(),'哥哥')
    def test_28_truncated_rule_set_fails_closed(self):
        truncated={g:list(v) for g,v in self.engine.rules.items()}
        truncated['shield'].pop()
        with mock.patch.object(self.engine,'rules',truncated):
            self.assertFalse(owner_title.set_title('姐姐')[0])
        self.assertEqual(owner_title.get(),'哥哥')
    def test_15_match_error_and_question_mark_hit_fail_closed(self):
        with mock.patch.object(self.engine,'check',side_effect=RuntimeError('fixture')):
            self.assertFalse(owner_title.set_title('姐姐')[0])
        with mock.patch.object(self.engine,'check',return_value=[('shield','limit?')]):
            self.assertFalse(owner_title.set_title('姐姐')[0])
        self.assertEqual(owner_title.get(),'哥哥')
    def test_16_parent_creation_atomic_save_and_failure_retains(self):
        file=D/'new_title_parent/deep/owner_title'
        with mock.patch.object(owner_title,'FILE',file):
            self.assertTrue(owner_title.set_title('姐姐')[0]);self.assertEqual(file.read_text(encoding='utf-8'),'姐姐')
            with mock.patch.object(owner_title.os,'replace',side_effect=OSError('fixture')):
                self.assertFalse(owner_title.set_title('哥哥')[0])
            self.assertEqual(file.read_text(encoding='utf-8'),'姐姐')
            self.assertFalse(list(file.parent.glob('*.pending')))
    def test_17_actual_engine_concurrent_checks_stable(self):
        phrases=['哥哥','主人','姐姐','Test12']*20
        expected={p:self.engine.check(p,textcheck.BLOCKING) for p in set(phrases)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda p:self.engine.check(p,textcheck.BLOCKING),phrases))
        self.assertEqual(results,[expected[p] for p in phrases])
    def test_18_match_data_is_serialized(self):
        e=self.engine;state={'active':0,'peak':0};lock=threading.Lock()
        def matching(*args):
            with lock:state['active']+=1;state['peak']=max(state['peak'],state['active'])
            time.sleep(.002)
            with lock:state['active']-=1
            return -1
        with mock.patch.object(e,'rules',{'shield':[('x',1)]}),mock.patch.object(e,'match',side_effect=matching):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _:e.check('哥哥',('shield',)),range(16)))
        self.assertEqual(state['peak'],1)
    def test_19_http_check_does_not_save_and_blocked_keeps_old(self):
        code,out=request('POST','/owner/title',{'title':'姐姐','check':True})
        self.assertEqual(code,200);self.assertTrue(out['ok']);self.assertEqual(owner_title.get(),'哥哥')
        code,out=request('POST','/owner/title',{'title':'主人'})
        self.assertEqual(code,400);self.assertFalse(out['ok']);self.assertEqual(owner_title.get(),'哥哥')
    def test_20_http_running_and_stopped_restart_contract(self):
        with mock.patch.object(control_api,'chat_running',return_value=True),mock.patch.object(control_api,'_run',side_effect=AssertionError('must not restart automatically')):
            code,out=request('POST','/xiaoguang/owner/title',{'title':'姐姐'})
        self.assertEqual(code,200);self.assertTrue(out['chat_running']);self.assertTrue(out['restart_required'])
        self.assertIn('重启聊天生效',out['message']);self.assertEqual(owner_title.get(),'姐姐')
        code,out=request('POST','/owner/title',{'title':'哥哥'})
        self.assertEqual(code,200);self.assertFalse(out['restart_required']);self.assertFalse(out['chat_running'])
        self.assertIn('下次启动聊天',out['message'])
    def test_21_chat_restart_stays_chat_only_and_failure_reported(self):
        with mock.patch.object(control_api,'_run',return_value=(0,'{"ok":true}')) as run:
            code,out=request('POST','/chat/restart',{})
        self.assertEqual(code,200);self.assertTrue(out['ok'])
        self.assertEqual(run.call_args.args[0][-1],'restart-chat')
        self.assertIn('新设置',out['message'])
        with mock.patch.object(control_api,'_run',return_value=(1,'{"ok":false}')):
            code,out=request('POST','/chat/restart',{})
        self.assertEqual(code,500);self.assertFalse(out['ok'])


    def test_29_missing_rules_http_check_then_save_warns(self):
        with mock.patch.object(textcheck,'_rules_dir',return_value=D/'missing_http'),mock.patch.object(textcheck,'_engine',None):
            code,out=request('POST','/xiaoguang/owner/title',{'title':'姐姐','check':True})
            self.assertEqual(code,200);self.assertTrue(out['ok'])
            self.assertEqual(owner_title.get(),'哥哥')
            self.assertIn('基本格式校验',out['message']);self.assertIn('未进行本地词表审核',out['message'])
            self.assertNotIn('审核通过',out['message'])
            code,out=request('POST','/owner/title',{'title':'姐姐'})
            self.assertEqual(code,200);self.assertTrue(out['ok']);self.assertEqual(out['title'],'姐姐')
            self.assertIn('未进行本地词表审核',out['message']);self.assertNotIn('审核通过',out['message'])
            self.assertFalse(out['restart_required']);self.assertEqual(owner_title.get(),'姐姐')

    def test_30_missing_rules_http_shape_and_reserved_still_rejected(self):
        with mock.patch.object(textcheck,'_rules_dir',return_value=D/'missing_shape'),mock.patch.object(textcheck,'_engine',None):
            for title in ('','abcdefg','hello!!','兄 姐','a\u200b','a\x00',*owner_title.RESERVED):
                with self.subTest(title=repr(title)):
                    code,out=request('POST','/owner/title',{'title':title,'check':True})
                    self.assertEqual(code,200);self.assertFalse(out['ok'])
                    code,out=request('POST','/owner/title',{'title':title})
                    self.assertEqual(code,400);self.assertFalse(out['ok'])
                    self.assertEqual(owner_title.get(),'哥哥')

    def test_31_missing_rules_http_auth_origin_and_lock_unchanged(self):
        with mock.patch.object(textcheck,'_rules_dir',return_value=D/'missing_auth'),mock.patch.object(textcheck,'_engine',None):
            self.assertEqual(request('POST','/owner/title',{'title':'姐姐'},token=False)[0],403)
            self.assertEqual(request('POST','/owner/title',{'title':'姐姐'},origin='https://unrelated.invalid')[0],403)
            with control_api.LOCK:
                self.assertEqual(request('POST','/owner/title',{'title':'姐姐'})[0],409)
        self.assertEqual(owner_title.get(),'哥哥')

    def test_32_synthetic_subject_uses_real_pcre2(self):
        # This tests our explicitly synthetic normalization, not SDK equivalence.
        text='ＳｙｎｔｈｅｔｉｃＢｌｏｃｋ'
        self.assertIn(('shield','synthetic-shield-0000'),self.engine.check(text,textcheck.BLOCKING))
        self.assertIn(('shield','synthetic-shield-0000'),self.engine.check('Synthetic\u200bBlock',textcheck.BLOCKING))
        self.assertTrue(all(rid.startswith('synthetic-') for rows in self.engine.rules.values() for rid,_ in rows))

    def test_33_missing_rules_save_failure_retains_old_title(self):
        with mock.patch.object(textcheck,'_rules_dir',return_value=D/'missing_io'),mock.patch.object(textcheck,'_engine',None):
            with mock.patch.object(owner_title.os,'replace',side_effect=OSError('synthetic write failure')):
                code,out=request('POST','/owner/title',{'title':'姐姐'})
        self.assertEqual(code,400);self.assertFalse(out['ok']);self.assertEqual(owner_title.get(),'哥哥')
        self.assertIn('保存称呼失败',out['message'])
        self.assertFalse(list(owner_title.FILE.parent.glob('owner_title.*.pending')))

class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.pack=Path(tempfile.mkdtemp(prefix='package-',dir=D));self.app=self.pack/'app';self.app.mkdir()
        self.app_patch=mock.patch.object(autostart,'APP',self.app);self.app_patch.start()
    def tearDown(self):self.app_patch.stop()
    def test_22_packaged_exe_precedes_vbs(self):
        (self.pack/'XiaoGuang.exe').write_bytes(b'fixture-not-executable')
        (self.pack/'小光.vbs').write_text('fixture',encoding='utf-8')
        target,args,cwd=autostart._target()
        self.assertEqual(Path(target),self.pack/'XiaoGuang.exe');self.assertEqual(args,'');self.assertEqual(Path(cwd),self.pack)
    def test_23_vbs_and_source_fallback(self):
        vbs=self.pack/'小光.vbs';vbs.write_text('fixture',encoding='utf-8')
        self.assertIn(str(vbs),autostart._target()[1])
        vbs.unlink()
        self.assertIn(str(self.app/'launcher/launcher.py'),autostart._target()[1])
    @unittest.skipUnless(os.name == 'nt', 'Windows shortcuts only')
    def test_24_shortcut_creation_is_temp_only(self):
        exe=self.pack/'XiaoGuang.exe';exe.write_bytes(b'fixture')
        scripts=[]
        def fake_powershell(script):
            scripts.append(script);autostart.shortcut_path().write_bytes(b'fixture-lnk');return 0,''
        with mock.patch.object(autostart,'_powershell',side_effect=fake_powershell):out=autostart.enable()
        self.assertTrue(out['ok']);self.assertEqual(Path(out['target']),exe)
        self.assertTrue(Path(out['path']).is_relative_to(D));self.assertIn(str(exe),scripts[0])
        self.assertTrue(autostart.status()['enabled']);self.assertTrue(autostart.disable()['ok'])
        self.assertFalse(autostart.status()['enabled'])
    def test_25_powershell_failure_is_not_success(self):
        with mock.patch.object(autostart,'_powershell',return_value=(1,'synthetic failure')):out=autostart.enable()
        self.assertFalse(out['ok'])
    @unittest.skipUnless(os.name == 'nt', 'Windows shortcuts only')
    def test_26_http_autostart_contract_uses_temp(self):
        exe=self.pack/'XiaoGuang.exe';exe.write_bytes(b'fixture')
        def fake(script):autostart.shortcut_path().write_bytes(b'fixture');return 0,''
        with mock.patch.object(autostart,'_powershell',side_effect=fake):
            code,out=request('POST','/api/autostart',{'enabled':True})
        self.assertEqual(code,200);self.assertTrue(out['ok']);self.assertEqual(Path(out['target']),exe)
        self.assertTrue(request('GET','/api/autostart')[1]['enabled'])
        self.assertEqual(request('POST','/api/autostart',{'enabled':False})[0],200)
        self.assertFalse(request('GET','/api/autostart')[1]['enabled'])

if __name__=='__main__':
    try:
        suite=unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        summary={'testsRun':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'successful':result.wasSuccessful(),
                 'realPcre2':True,'ruleSource':'synthetic-only','ruleSubject':'synthetic NFKC plus zero-width removal, not SDK behavior','workspaceTemporary':True,'rules':{g:len(v) for g,v in textcheck.engine().rules.items()},
                 'ruleCompilationFailures':textcheck.engine().failed,'http':'real loopback ThreadingHTTPServer',
                 'upstream':'blocked','gameProcesses':'blocked','startupDirectory':'temporary fixture only',
                 'exercisedModules':['app/bot/control_api.py','app/bot/owner_title.py','app/bot/textcheck.py','app/launcher/autostart.py']}
        print('SCENARIO_RESULT='+json.dumps(summary,ensure_ascii=False))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
        find_guard.stop();running_guard.stop();process_guard.stop();network_guard.stop();T.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
