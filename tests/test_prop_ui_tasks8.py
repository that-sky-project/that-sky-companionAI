"""Props task/UI contracts against synthetic data and AST-isolated production code."""
from pathlib import Path
import argparse
import ast
import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'


def subset(rel,names,env=None):
    source=(APP/rel).read_text(encoding='utf-8')
    nodes=[]
    for n in ast.walk(ast.parse(source)):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names:
            n=copy.deepcopy(n);n.decorator_list=[];nodes.append(n)
    ns={'Path':Path,**(env or {})}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),rel,'exec'),ns)
    return ns


class PropsTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='props-ui8-',dir=ROOT/'tests')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.props=self.root/'props'
        self.paths=types.SimpleNamespace(props_dir=lambda:self.props)

    def test_full_data_plan_preserves_resume_and_adds_optional_props_last(self):
        ns=subset('launcher/tasks.py',{'command_for'},{'xg_paths':self.paths})
        plan=ns['command_for']('data',apk='fixture.apk',resume=True)
        self.assertEqual(plan[:2],[['names','--apk','fixture.apk'],['terrain','--apk','fixture.apk','--resume']])
        self.assertEqual(plan[2],['props','--apk','fixture.apk','--output',str(self.props),'--optional'])

    def test_props_plan_never_regenerates_names_or_terrain(self):
        ns=subset('launcher/tasks.py',{'command_for'},{'xg_paths':self.paths})
        self.assertEqual(ns['command_for']('props',apk='fixture.apk'),[['props','--apk','fixture.apk','--output',str(self.props)]])
        source=(APP/'launcher/tasks.py').read_text(encoding='utf-8')
        value=next(n.value for n in ast.parse(source).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='LOCK_KINDS' for t in n.targets))
        self.assertIn('props',ast.literal_eval(value))

    def cli(self,optional,generate):
        rows=[]
        common=types.ModuleType('xg_gamedata.common')
        common.Progress=lambda *a:types.SimpleNamespace(emit=lambda *a,**kw:None,finish=lambda *a:None)
        common.game_dir=lambda data:Path(data)/'game/fixture-version'
        common.resolve_data=lambda _:self.root
        generator=types.ModuleType('xg_gamedata.props');generator.generate=generate
        class Error(Exception):
            def __init__(self,msg,code):self.code=code
        ns=subset('xg_gamedata/__main__.py',{'cmd_props'},{'__name__':'xg_gamedata.__main__','__package__':'xg_gamedata',
                  'GameDataError':Error,'final':lambda ok,**kw:rows.append({'ok':ok,**kw})})
        with mock.patch.dict(sys.modules,{'xg_gamedata.common':common,'xg_gamedata.props':generator}):
            code=ns['cmd_props'](types.SimpleNamespace(apk='fixture.apk',output=str(self.props),data=None,optional=optional))
        return code,rows

    def test_optional_props_failure_preserves_core_chain_success_but_not_props_ready(self):
        def fail(*a,**kw):raise RuntimeError('fixture private path not to display')
        code,rows=self.cli(True,fail)
        self.assertEqual(code,0);self.assertTrue(rows[-1]['ok']);self.assertFalse(rows[-1]['props']['ready'])
        self.assertIn('warning',rows[-1]);self.assertNotIn('fixture private',json.dumps(rows))

    def test_independent_props_failure_is_failure(self):
        code,rows=self.cli(False,lambda *a,**kw:(_ for _ in ()).throw(ValueError('fixture')))
        self.assertEqual(code,1);self.assertFalse(rows[-1]['ok']);self.assertFalse(rows[-1]['props']['ready'])

    def test_successful_props_task_reports_actual_count(self):
        call=mock.Mock(return_value={'ok':True,'ready':3,'known':8})
        code,rows=self.cli(False,call)
        self.assertEqual(code,0);self.assertEqual(rows[-1]['props']['count'],3)
        self.assertEqual(call.call_args.kwargs['output_dir'],self.props)

    def test_cli_parser_requires_apk_and_supports_independent_output(self):
        ns=subset('xg_gamedata/__main__.py',{'build_parser'},{'argparse':argparse})
        args=ns['build_parser']().parse_args(['props','--apk','fixture.apk','--output',str(self.props)])
        self.assertEqual(args.func,'props');self.assertFalse(args.optional)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            ns['build_parser']().parse_args(['props'])

    def status(self,validator,proof=None):
        common=types.ModuleType('xg_gamedata.common');common.APP_ROOT=APP
        props=types.ModuleType('skyterrain.props');props.load_generated_catalog=validator
        ns=subset('xg_gamedata/status.py',{'props_status'},{'__name__':'xg_gamedata.status','__package__':'xg_gamedata',
                    'read_json':lambda *_:proof or {'counts':{'ready':3,'known':8}}})
        with mock.patch.dict(sys.modules,{'xg_gamedata.common':common,'skyterrain.props':props}):
            return ns['props_status'](self.props,{'build':421629})

    def test_missing_props_does_not_claim_ready_from_task_history(self):
        validator=mock.Mock();result=self.status(validator)
        self.assertFalse(result['ready']);self.assertEqual(result['state'],'missing');validator.assert_not_called()

    def test_state_uses_shared_full_runtime_validator(self):
        self.props.mkdir();(self.props/'generation.json').write_text('{}');(self.props/'catalog.json').write_text('{}')
        validator=mock.Mock(return_value={})
        result=self.status(validator)
        self.assertTrue(result['ready']);self.assertEqual(result['count'],3)
        validator.assert_called_once_with(self.props,expected_build=421629)

    def test_bad_shard_or_wrong_build_stays_invalid(self):
        self.props.mkdir();(self.props/'generation.json').write_text('{}');(self.props/'catalog.json').write_text('{}')
        result=self.status(mock.Mock(side_effect=ValueError('synthetic missing shard')))
        self.assertFalse(result['ready']);self.assertEqual(result['state'],'invalid')

    def wizard(self):
        reset={};saved=[]
        def save(doc):reset.clear();reset.update(copy.deepcopy(doc));saved.append(copy.deepcopy(doc))
        ns=subset('launcher/wizard.py',{'after_data_reset','resource_reset_status','_reset_steps','_reset_started','_reset_complete'},
            {'LOCK':threading.RLock(),'time':time,'tasks':types.SimpleNamespace(current=lambda:{'task':None}),
             '_reset_doc':lambda:copy.deepcopy(reset),'_save_reset':save,'active_account':lambda:'fixture-account',
             '_capture_inventory':lambda:{'current':[],'stale':[]}})
        return ns,reset,saved

    def test_props_only_cleanup_never_invalidates_data_or_capture_catalog(self):
        ns,reset,_=self.wizard()
        result=ns['after_data_reset']('execute',{'props':True},batch_id='fixture')
        self.assertEqual(result['steps'],['props']);self.assertTrue(result['selection']['props'])
        self.assertNotIn('catalog_approved_accounts',reset)

    def test_props_task_does_not_enable_automatic_catalog_rebuild(self):
        ns,reset,_=self.wizard();ns['after_data_reset']('execute',{'props':True},batch_id='fixture')
        ns['_reset_started']('props',{'task':{'id':'props-task'}})
        self.assertTrue(reset['auto_suppressed']);self.assertIn('props-task',reset['allowed_task_ids'])
        ns['_reset_complete']('props');self.assertEqual(reset['steps'],[])

    def test_restore_preserves_previous_nonprops_gates(self):
        ns,reset,_=self.wizard();ns['after_data_reset']('execute',{'names':True},batch_id='names')
        ns['after_data_reset']('execute',{'props':True},batch_id='props')
        result=ns['after_data_reset']('restore',{'props':True},batch_id='props')
        self.assertEqual(result['steps'],['data','catalog'])


class PropsApiTests(unittest.TestCase):
    def request(self,*,online=False,busy=False,auth=True,origin=True,locked=False,path='/api/wizard/props/start'):
        start=mock.Mock(return_value={'ok':True,'task':{'kind':'props','running':True}})
        lock=threading.Lock()
        if locked:lock.acquire()
        ns=subset('bot/control_api.py',{'do_POST'},{'json':json,'urlsplit':urlsplit,'LOCK':lock,
            '_cleanup_online':lambda:online,'_cleanup_busy':lambda:busy,
            'wizard':types.SimpleNamespace(start_props=start,start_data=start)})
        output=[]
        handler=types.SimpleNamespace(path=path,headers={'Content-Length':'2'},rfile=io.BytesIO(b'{}'),
            authed=lambda:auth,same_origin=lambda:origin,reply=lambda code,obj:output.append((code,obj)))
        ns['do_POST'](handler)
        if locked:lock.release()
        return output[-1],start

    def test_offline_authenticated_props_task_starts(self):
        (code,_),start=self.request();self.assertEqual(code,200);start.assert_called_once_with()

    def test_auth_origin_lock_online_and_busy_fail_before_start(self):
        for options,expected in [({'auth':False},403),({'origin':False},403),({'locked':True},409),
                                 ({'online':True},409),({'busy':True},409)]:
            with self.subTest(options=options):
                (code,_),start=self.request(**options);self.assertEqual(code,expected);start.assert_not_called()

    def test_full_data_including_props_uses_same_offline_gate(self):
        (code,_),start=self.request(online=True,path='/xiaoguang/api/wizard/data/start')
        self.assertEqual(code,409);start.assert_not_called()


class PropsChainTests(unittest.TestCase):
    def run_chain(self,results,plan=None,kind='data'):
        plan=plan or [['names','--apk','fixture.apk'],['terrain','--apk','fixture.apk'],['props','--apk','fixture.apk','--optional']]
        items=iter(results)
        def popen(*a,**kw):
            item=next(items)
            if isinstance(item,Exception):raise item
            code,final=item
            return types.SimpleNamespace(stdout=[] if final is None else [json.dumps(final)],wait=lambda:code)
        ns=subset('launcher/gamedata_chain.py',{'run_plan','_optional_props_tail','_optional_props_warning'},
            {'APP':APP,'json':json,'gamedata':types.SimpleNamespace(env=lambda:{},command=lambda args:args),
             'xg_exec':types.SimpleNamespace(popen=popen),'subprocess':types.SimpleNamespace(PIPE=1,STDOUT=2)})
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=ns['run_plan'](plan,kind)
        rows=[json.loads(line) for line in stream.getvalue().splitlines()]
        return code,[row for row in rows if 'ok' in row]

    def test_optional_tail_nonzero_or_killed_preserves_two_proven_core_successes(self):
        for last in [(1,{'ok':False}),(-9,None),(1,{'ok':True}),OSError('fixture spawn denied')]:
            with self.subTest(last=last):
                code,rows=self.run_chain([(0,{'ok':True}),(0,{'ok':True}),last])
                self.assertEqual(code,0);self.assertTrue(rows[-1]['ok']);self.assertFalse(rows[-1]['props']['ready'])
                self.assertIn('warning',rows[-1])

    def test_core_failure_missing_log_or_nonzero_never_becomes_optional_success(self):
        for results in [[(1,{'ok':False})],[(0,None)],[(0,{'ok':True}),(0,None)],
                        [(0,{'ok':True}),(1,{'ok':True})],[(0,{'ok':True}),(0,{'ok':1})]]:
            with self.subTest(results=results):
                code,rows=self.run_chain(results)
                self.assertEqual(code,1);self.assertFalse(any('warning' in row for row in rows))

    def test_nonoptional_or_independent_props_never_softens_failure(self):
        plan=[['names'],['terrain'],['props']]
        code,_=self.run_chain([(0,{'ok':True}),(0,{'ok':True}),(1,{'ok':False})],plan=plan)
        self.assertEqual(code,1)
        code,_=self.run_chain([(1,{'ok':False})],plan=[['props','--optional']],kind='props')
        self.assertEqual(code,1)

    def summary(self,data_done):
        props=mock.Mock(return_value={'ready':False,'state':'missing'})
        rows=[{'id':'data','done':data_done},{'id':'capture','done':True},{'id':'finish','done':False}]
        ns=subset('launcher/wizard.py',{'summary','is_complete','_game_data_state'},
            {'step_status':lambda:(rows,'finish'),'tasks':types.SimpleNamespace(current=lambda:{'task':None}),
             '_current_game_profile':lambda:{'version':'fixture','build':1},'_capture_inventory':lambda p:{},
             'capture_status':lambda *a:{},'catalog_status':lambda **kw:{'certified':True},'_props_status':props,
             '_device_status':lambda:{},'_wizard':lambda:{'completed_at':'fixture'},'WIZARD_VERSION':1,
             'active_account':lambda:'fixture','_apk_path':lambda:'fixture.apk','_ai_configured':lambda:True,
             'xg_paths':types.SimpleNamespace(GAME_VERSION='fixture',GAME_DIR=ROOT/'tests/fixture-game'),
             '_game_data_versions':lambda:[],'resource_reset_status':lambda:{},'_autostart_status':lambda:{}})
        return ns['summary'](),props

    def test_missing_optional_props_keeps_completed_core_wizard_and_validates_once(self):
        result,props=self.summary(True)
        self.assertTrue(result['completed']);self.assertTrue(result['gameData']['complete']);self.assertFalse(result['props']['ready'])
        props.assert_called_once();self.assertIs(result['props'],result['gameData']['props'])

    def test_missing_core_assets_still_blocks_wizard_completion(self):
        result,_=self.summary(False)
        self.assertFalse(result['completed']);self.assertFalse(result['gameData']['complete'])


class PropsUiTests(unittest.TestCase):
    def test_node_scenarios(self):
        result=subprocess.run([str(ROOT/'runtime/node/node.exe'),str(Path(__file__).with_name('prop_ui_tasks8_scenarios.cjs'))],
                              capture_output=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr[-5000:])
        doc=json.loads(result.stdout);self.assertGreaterEqual(doc['passed'],12);self.assertEqual(doc['failed'],0)


if __name__=='__main__':unittest.main()
