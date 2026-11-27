"""Real synthetic chain children and isolated task/UI error fidelity; no accounts."""
from pathlib import Path
import ast
import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
TEMP=ROOT.parent/'task-errors12-tmp';TEMP.mkdir(exist_ok=True)
ERROR='拉取成功，但安装包校验未通过：缺少所需渠道资源'
CODE='apk_channel_missing'

def functions(rel,names,env):
    tree=ast.parse((APP/rel).read_text(encoding='utf-8'))
    nodes=[copy.deepcopy(n) for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    for n in nodes:n.decorator_list=[]
    assert {n.name for n in nodes}==set(names)
    ns=dict(env);exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(APP/rel),'exec'),ns)
    return ns

class TaskErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='case-',dir=TEMP)
        self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)

    def chain(self,results,plan=None,kind='pull-apk'):
        plan=plan or [['pull-apk']]
        spec=self.base/'spec.json';spec.write_text(json.dumps(results,ensure_ascii=False),encoding='utf-8')
        planfile=self.base/'plan.json';planfile.write_text(json.dumps(plan),encoding='utf-8')
        child=self.base/'synthetic_child.py'
        child.write_text("""import json,sys
from pathlib import Path
row=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))[sys.argv[2]]
for line in row.get('lines',[]):print(json.dumps(line,ensure_ascii=False) if isinstance(line,dict) else line,flush=True)
sys.exit(row.get('exit',0))
""",encoding='utf-8')
        bootstrap=self.base/'chain_bootstrap.py'
        bootstrap.write_text("""import os,runpy,subprocess,sys,types
chain,plan,kind,child,spec=sys.argv[1:]
cap=types.ModuleType('capture');g=types.ModuleType('capture.gamedata')
g.env=lambda:dict(os.environ,PYTHONIOENCODING='utf-8')
g.command=lambda args:[sys.executable,'-B','-X','utf8',child,spec,*args]
cap.gamedata=g;sys.modules['capture']=cap;sys.modules['capture.gamedata']=g
x=types.ModuleType('xg_exec')
def popen(*args,**kwargs):
    if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(*args,**kwargs)
x.popen=popen;sys.modules['xg_exec']=x
sys.argv=[chain,'--kind',kind,'--plan',plan]
runpy.run_path(chain,run_name='__main__')
""",encoding='utf-8')
        env=dict(os.environ,XG_DATA_DIR=str(self.base/'data'),PYTHONDONTWRITEBYTECODE='1',
                 PYTHONIOENCODING='utf-8',TEMP=str(self.base),TMP=str(self.base))
        r=subprocess.run([sys.executable,'-B','-X','utf8',str(bootstrap),str(APP/'launcher/gamedata_chain.py'),
                          str(planfile),kind,str(child),str(spec)],cwd=self.base,env=env,
                         capture_output=True,text=True,encoding='utf-8',timeout=20)
        rows=[]
        for line in r.stdout.splitlines():
            try:doc=json.loads(line)
            except ValueError:continue
            if isinstance(doc,dict):rows.append(doc)
        self.assertTrue(rows,r.stderr)
        return r,rows

    def task_state(self,text,plan=None,**override):
        log=self.base/'task_fixture.jsonl';log.write_text(text,encoding='utf-8')
        planfile=self.base/'task_fixture.plan.json';planfile.write_text(json.dumps(plan or [['pull-apk']]),encoding='utf-8')
        status=self.base/'task.json'
        original={'id':'synthetic-task','kind':'pull-apk','label':'提取安装包','running':True,'ok':None,
                  'log':str(log),'plan':str(planfile),'started':'2000-01-01 00:00:00',**override}
        status.write_text(json.dumps(original,ensure_ascii=False),encoding='utf-8')
        def read_json(p,default):
            try:return json.loads(Path(p).read_text(encoding='utf-8'))
            except (OSError,ValueError):return default
        def write_json(p,doc,**_):
            p=Path(p);q=p.with_suffix('.pending');q.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8');os.replace(q,p)
        spec=importlib.util.spec_from_file_location('task_errors12_detail',APP/'error_detail.py')
        detail=importlib.util.module_from_spec(spec);spec.loader.exec_module(detail)
        spec=importlib.util.spec_from_file_location('task_errors12_install',APP/'install_support.py')
        support=importlib.util.module_from_spec(spec);spec.loader.exec_module(support)
        ns=functions('launcher/tasks.py',{'_tail','_parse','_raw_tail','_plan_count','_persist_if_current','current'},
          {'Path':Path,'json':json,'copy':copy,'time':time,'TAIL_BYTES':4*1024*1024,'_status_path':lambda:status,
           '_alive':lambda doc:False,'error_detail':detail,'install_support':support,
           'xg_paths':types.SimpleNamespace(read_json=read_json,write_json=write_json)})
        return ns['current'](),status,ns

    def test_nonzero_keeps_actual_child_error_code_and_stage(self):
        failure={'ok':False,'error':ERROR,'code':CODE,'stage':'validate_pulled_apk','child_code':CODE}
        r,rows=self.chain({'pull-apk':{'exit':7,'lines':[failure]}})
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['error'],ERROR);self.assertEqual(rows[-1]['code'],CODE)
        self.assertEqual(rows[-1]['stage'],'validate_pulled_apk');self.assertEqual(rows[-1]['child_code'],CODE)
        self.assertEqual(rows[-1]['child_exit_code'],7);self.assertEqual(rows[-1]['chain_command'],'pull-apk')
        self.assertNotIn('core_process_failed',r.stdout)

    def test_nonzero_without_failure_uses_generic(self):
        r,rows=self.chain({'pull-apk':{'exit':7,'lines':['synthetic diagnostic']}})
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],'core_process_failed')

    def test_last_true_cannot_hide_false_when_child_exit_is_nonzero(self):
        r,rows=self.chain({'pull-apk':{'exit':2,'lines':[{'ok':False,'error':ERROR,'code':CODE},{'ok':True}]}})
        self.assertEqual(r.returncode,1);self.assertFalse(rows[-1]['ok']);self.assertEqual(rows[-1]['code'],CODE)

    def test_previous_command_recovered_error_does_not_pollute_next(self):
        plan=[['names'],['terrain'],['props','--optional']]
        r,rows=self.chain({'names':{'lines':[{'ok':False,'error':'old synthetic error','code':'old_error'},{'ok':True}]},
                           'terrain':{'exit':5,'lines':[]},'props':{'lines':[{'ok':True}]}},plan,'data')
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],'core_process_failed')
        self.assertNotIn('old synthetic error',rows[-1]['error'])
        state,_,_=self.task_state(r.stdout,plan,kind='data')
        self.assertEqual(state['task']['error_code'],'core_process_failed')

    def test_success_json_but_nonzero_without_false_does_not_pass(self):
        r,rows=self.chain({'pull-apk':{'exit':4,'lines':[{'ok':True,'apk':'synthetic.apk'}]}})
        self.assertEqual(r.returncode,1);self.assertFalse(rows[-1]['ok']);self.assertEqual(rows[-1]['code'],'core_process_failed')

    def test_explicit_failure_with_exit_zero_is_still_failure(self):
        r,rows=self.chain({'pull-apk':{'exit':0,'lines':[{'ok':False,'error':ERROR,'code':CODE}]}})
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],CODE)

    def test_bad_json_and_non_dict_are_not_failure_events(self):
        r,rows=self.chain({'pull-apk':{'exit':3,'lines':['{bad json','[1,2]','{"ok": true}']}})
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],'core_process_failed')

    def test_core_data_failure_never_downgrades_or_starts_tail(self):
        plan=[['names'],['terrain'],['props','--optional']]
        r,rows=self.chain({'names':{'exit':1,'lines':[{'ok':False,'error':'core rejected','code':'core_fixture'}]},
                          'terrain':{'lines':[{'ok':True}]},'props':{'lines':[{'ok':True}]}},plan,'data')
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],'core_fixture')
        self.assertEqual(len([x for x in rows if x.get('stage')=='chain' and 'done' in x]),1)
        self.assertNotIn('warning',rows[-1])

    def test_optional_props_tail_keeps_success_and_child_reason(self):
        plan=[['names'],['terrain'],['props','--optional']]
        r,rows=self.chain({'names':{'lines':[{'ok':True}]},'terrain':{'lines':[{'ok':True}]},
                          'props':{'exit':3,'lines':[{'ok':False,'error':'synthetic geometry failure','code':'props_fixture'}]}},plan,'data')
        self.assertEqual(r.returncode,0);self.assertTrue(rows[-1]['ok']);self.assertFalse(rows[-1]['props']['ready'])
        self.assertEqual(rows[-1]['warning_code'],'props_fixture');self.assertEqual(rows[-1]['child_failure']['code'],'props_fixture')
        state,_,_=self.task_state(r.stdout,plan,kind='data')
        self.assertTrue(state['task']['ok']);self.assertFalse(state['task']['result']['props']['ready'])

    def test_nonoptional_props_failure_does_not_downgrade(self):
        r,rows=self.chain({'props':{'exit':1,'lines':[{'ok':False,'error':'props rejected','code':'props_fixture'}]}},[['props']],'props')
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],'props_fixture');self.assertFalse(rows[-1]['ok'])

    def test_missing_core_result_stops_before_optional_tail(self):
        plan=[['names'],['terrain'],['props','--optional']]
        r,rows=self.chain({'names':{'lines':[]},'terrain':{'lines':[{'ok':True}]},'props':{'lines':[{'ok':True}]}},plan,'data')
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],'core_result_missing')

    def test_actual_task_state_and_persisted_status_keep_error_code(self):
        r,_=self.chain({'pull-apk':{'exit':9,'lines':[{'ok':False,'error':ERROR,'code':CODE,'stage':'validate_pulled_apk'}]}})
        state,status,ns=self.task_state(r.stdout)
        for doc in (state,state['task'],json.loads(status.read_text(encoding='utf-8'))):
            self.assertFalse(doc['ok']);self.assertFalse(doc['running']);self.assertEqual(doc['error'],ERROR)
            self.assertEqual(doc['error_code'],CODE);self.assertEqual(doc['error_detail']['code'],CODE)
            self.assertEqual(doc['result']['stage'],'validate_pulled_apk')
        self.assertEqual(ns['current']()['task']['error_code'],CODE)

    def test_cancelled_terminal_marker_is_not_overwritten_by_late_log(self):
        text=json.dumps({'ok':False,'error':ERROR,'code':CODE})+'\n'
        state,_,_=self.task_state(text,running=False,ok=False,error='已取消',error_code='cancelled')
        self.assertEqual(state['task']['error'],'已取消');self.assertEqual(state['task']['error_code'],'cancelled')

    def test_keyboard_interrupt_preserves_chain_stop_exit(self):
        class Output:
            def __iter__(self):raise KeyboardInterrupt
        stopped=[]
        proc=types.SimpleNamespace(stdout=Output(),terminate=lambda:stopped.append(True))
        ns=functions('launcher/gamedata_chain.py',{'run_plan','_optional_props_tail','_optional_props_warning'},
          {'APP':APP,'json':json,'gamedata':types.SimpleNamespace(env=lambda:{},command=lambda a:a),
           'xg_exec':types.SimpleNamespace(popen=lambda *a,**k:proc),'subprocess':subprocess})
        with contextlib.redirect_stdout(io.StringIO()):code=ns['run_plan']([['pull-apk']],'pull-apk')
        self.assertEqual(code,130);self.assertEqual(stopped,[True])

    def test_failed_pull_after_task_does_not_mark_apk_success(self):
        wizard={};saves=[]
        ns=functions('launcher/wizard.py',{'after_task'},{'_reset_doc':lambda:{},'_wizard':lambda:wizard,
            '_save':lambda doc:saves.append(copy.deepcopy(doc)),'LOCK':threading.RLock()})
        doc={'task':{'id':'synthetic-failure','kind':'pull-apk','ok':False,'running':False,'error':ERROR,'result':{'code':CODE}}}
        result=ns['after_task'](doc)
        self.assertFalse(result['ok']);self.assertFalse(result['changed']);self.assertNotIn('apk',wizard)
        self.assertEqual(saves,[{'last_task_id':'synthetic-failure'}])

    def test_actual_apk_validation_wrapper_and_cmd_pull_flow_to_chain(self):
        tree=ast.parse((APP/'xg_gamedata/common.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='GameDataError')
        env={};exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'common-error','exec'),env)
        Error=env['GameDataError'];emitted=[]
        ns=functions('xg_gamedata/apk.py',{'_pulled_validation_error','cmd_pull'},
                     {'GameDataError':Error,'__package__':'task_error_fixture',
                      'final':lambda ok,**kw:emitted.append({'ok':ok,**kw})})
        original=Error('缺少所需渠道资源',CODE)
        original.members=[{'name':'assets/critical.bin','reason':'size/CRC changed'}]
        original.difference_count=12
        wrapped=ns['_pulled_validation_error'](original)
        self.assertEqual(wrapped.code,original.code);self.assertEqual(wrapped.message,ERROR)
        def rejected(*args):raise wrapped
        ns['pull_apk']=rejected
        package=types.ModuleType('task_error_fixture');package.__path__=[]
        common=types.ModuleType('task_error_fixture.common');common.resolve_data=lambda value:self.base
        with mock.patch.dict(sys.modules,{'task_error_fixture':package,'task_error_fixture.common':common}):
            result=ns['cmd_pull'](types.SimpleNamespace(data=None,adb='synthetic-adb',serial='synthetic-serial'))
        self.assertEqual(result,1);self.assertEqual(len(emitted),1)
        self.assertEqual(emitted[0]['stage'],'validate_pulled_apk');self.assertEqual(emitted[0]['child_code'],CODE)
        self.assertEqual(emitted[0]['members'],original.members);self.assertEqual(emitted[0]['difference_count'],12)
        r,rows=self.chain({'pull-apk':{'exit':result,'lines':emitted}})
        self.assertEqual(r.returncode,1);self.assertEqual(rows[-1]['code'],CODE)
        state,_,_=self.task_state(r.stdout)
        self.assertEqual(state['task']['error'],ERROR);self.assertEqual(state['task']['error_code'],CODE)
        self.assertEqual(state['task']['result']['members'],original.members);self.assertEqual(state['task']['result']['difference_count'],12)

    def test_manual_check_preserves_diagnostics_before_any_success_side_effect(self):
        apk=self.base/'synthetic.apk';apk.write_bytes(b'not a real APK')
        failure={'ok':False,'error':ERROR,'code':CODE,'members':[{'name':'assets/critical.bin','reason':'missing'}],
                 'difference_count':12,'stage':'validate_pulled_apk','child_code':CODE}
        run=mock.Mock(return_value={'ok':False,'final':failure})
        def forbidden(*args,**kwargs):raise AssertionError('failure may not enter success/profile/config branch')
        ns=functions('launcher/wizard.py',{'check_apk'},{'Path':Path,'gamedata':types.SimpleNamespace(run=run),
           '_version_ok':forbidden,'_cfg':forbidden,'native_lib':types.SimpleNamespace(ensure_native_library=forbidden)})
        result=ns['check_apk'](str(apk))
        self.assertFalse(result['ok']);self.assertEqual(result['message'],ERROR)
        for key in ('code','members','difference_count','stage','child_code'):self.assertEqual(result[key],failure[key])
        self.assertEqual(run.call_args.args[0],['check-apk','--apk',str(apk)])

    def test_manual_check_success_event_still_requires_current_version_gate(self):
        apk=self.base/'synthetic.apk';apk.write_bytes(b'not a real APK')
        final={'ok':True,'path':str(apk),'version':'synthetic-other-version'}
        def forbidden(*args,**kwargs):raise AssertionError('version failure may not save config or load native')
        ns=functions('launcher/wizard.py',{'check_apk'},{'Path':Path,
           'gamedata':types.SimpleNamespace(run=lambda *a,**k:{'ok':True,'final':final}),
           '_version_ok':lambda value:(False,'version gate rejected synthetic fixture'),'_cfg':forbidden,
           'native_lib':types.SimpleNamespace(ensure_native_library=forbidden)})
        result=ns['check_apk'](str(apk))
        self.assertFalse(result['ok']);self.assertEqual(result['message'],'version gate rejected synthetic fixture')
        self.assertEqual(result['detail'],final)

    def test_actual_check_cli_keeps_critical_member_fields(self):
        tree=ast.parse((APP/'xg_gamedata/common.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='GameDataError')
        env={};exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'common-error','exec'),env)
        Error=env['GameDataError'];failure=Error('critical member differs','apk_resource_mismatch')
        failure.members=[{'name':'assets/critical.bin','reason':'missing'}];failure.difference_count=9
        def rejected(*a,**k):raise failure
        emitted=[]
        ns=functions('xg_gamedata/apk.py',{'cmd_check'},{'Path':Path,'GameDataError':Error,
           'Progress':lambda *a:types.SimpleNamespace(finish=lambda *a:None),'inspect_apk':rejected,
           'final':lambda ok,**kw:emitted.append({'ok':ok,**kw})})
        self.assertEqual(ns['cmd_check'](types.SimpleNamespace(apk='synthetic.apk')),1)
        self.assertEqual(len(emitted),1);self.assertEqual(emitted[0]['members'],failure.members)
        self.assertEqual(emitted[0]['difference_count'],9);self.assertEqual(emitted[0]['code'],failure.code)

    def test_actual_ui_error_and_code_scenarios(self):
        r=subprocess.run([str(ROOT/'runtime/node/node.exe'),str(ROOT/'tests/task_errors12_scenarios.cjs')],
                         cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(r.returncode,0,r.stderr[-6000:]);doc=json.loads(r.stdout)
        self.assertEqual(doc['passed'],9);self.assertEqual(doc['failed'],0)

if __name__=='__main__':unittest.main()
