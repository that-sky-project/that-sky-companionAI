"""Offline ADB discovery: all files, processes, registry and ADB replies are synthetic."""
from pathlib import Path
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
sys.path.insert(0,str(APP))
from capture import device
PROCESS=device._process_executable_paths
REGISTRY=device._registry_roots

class FakeKernel:
    def __init__(self,rows=(),*,snapshot=111,deny=(),query_fail=(),query_raise=(),next_raise=False):
        self.rows=list(rows);self.snapshot=snapshot;self.deny=set(deny);self.query_fail=set(query_fail)
        self.query_raise=set(query_raise);self.next_raise=next_raise;self.index=0;self.opened=[];self.closed=[]
    def CreateToolhelp32Snapshot(self,flags,pid):
        self.flags=(flags,pid);return self.snapshot
    def fill(self,entry):
        if self.index>=len(self.rows):return 0
        pid,name,path=self.rows[self.index];entry._obj.th32ProcessID=pid;entry._obj.szExeFile=name;return 1
    def Process32FirstW(self,snapshot,entry):self.index=0;return self.fill(entry)
    def Process32NextW(self,snapshot,entry):
        if self.next_raise:raise PermissionError('synthetic enumeration denied')
        self.index+=1;return self.fill(entry)
    def OpenProcess(self,rights,inherit,pid):
        if pid in self.deny:return 0
        self.opened.append((pid+1000,rights,inherit));return pid+1000
    def QueryFullProcessImageNameW(self,handle,flags,buffer,length):
        pid=handle-1000
        if pid in self.query_raise:raise PermissionError('synthetic query denied')
        if pid in self.query_fail:return 0
        value=next(p for i,n,p in self.rows if i==pid);buffer.value=str(value);length._obj.value=len(str(value));return 1
    def CloseHandle(self,handle):self.closed.append(handle);return 1

class OfflineBase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='adb12-',dir=ROOT/'tests');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.calls=[]
        self.cfg=self.mock('_capture_config',return_value={})
        self.proc=self.mock('_process_executable_paths',return_value=[])
        self.mock('_registry_roots',return_value=[])
        self.mock('_registry_uninstall_roots',return_value=[])
        self.mock('xg_adb._server_protocol',return_value=None)
        self.mock('shutil.which',return_value=None)
        self.exec_guard=patch.object(device.xg_exec,'run',side_effect=AssertionError('real external execution forbidden')).start();self.addCleanup(patch.stopall)
        patch.dict(os.environ,{},clear=True).start()
        patch.object(ctypes,'WinDLL',side_effect=AssertionError('real process API forbidden')).start()
        # Filesystem queries beyond this private fixture are absent, never forwarded.
        real_stat=Path.stat;real_scandir=os.scandir
        def allowed(path):
            try:return Path(path).absolute().is_relative_to(self.root)
            except (TypeError,ValueError):return False
        def stat(path,*args,**kw):
            if not allowed(path):raise FileNotFoundError('outside synthetic fixture')
            return real_stat(path,*args,**kw)
        def scandir(path):
            if not allowed(path):raise FileNotFoundError('outside synthetic fixture')
            return real_scandir(path)
        patch.object(Path,'stat',stat).start();patch.object(os,'scandir',scandir).start()
    def mock(self,name,**kw):
        target=device
        parts=name.split('.')
        for part in parts[:-1]:target=getattr(target,part)
        p=patch.object(target,parts[-1],**kw);value=p.start();self.addCleanup(p.stop);return value
    def put(self,relative):
        p=self.root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'synthetic-not-executable');return p
    def install(self,name,version):return self.put(name+'/nx_device/'+version+'/shell/adb.exe')
    def select(self,roots,ready=(),processes=()):
        self.proc.return_value=list(processes)
        self.mock('candidate_roots',return_value=list(roots))
        def probe(adb,timeout=2):
            self.calls.append((Path(adb),timeout));return ['emulator-9000'] if Path(adb) in set(ready) else []
        self.mock('_probe_adb_devices',side_effect=probe)
        return device.find_adb()

class DirectoryTests(OfflineBase):
    def test_numeric_versions_15_12_9_10_descend_not_lexical(self):
        for v in ['9.9','12.0','9.10','15.0','15.10','15.2']:self.install('MuMu',v)
        out=device._adb_candidates_under(self.root/'MuMu')
        self.assertEqual([p.parts[-3] for p in out],['15.10','15.2','15.0','12.0','9.10','9.9'])
        self.assertEqual(device._adb_under(self.root/'MuMu'),out[0])
    def test_all_known_layouts(self):
        for i,layout in enumerate(['nx_main/adb.exe','nx_main/shell/adb.exe','shell/adb.exe','adb.exe','emulator/nemu/shell/adb.exe']):
            with self.subTest(layout=layout):
                p=self.put(f'layout{i}/{layout}');self.assertIn(p,device._adb_candidates_under(self.root/f'layout{i}'))
    def test_multiple_main_and_device_paths_retained(self):
        paths=[self.install('MuMu','15.0'),self.put('MuMu/nx_main/adb.exe'),self.put('MuMu/nx_main/shell/adb.exe')]
        self.assertEqual(set(device._adb_candidates_under(self.root/'MuMu')),set(paths))
    def test_legacy_unknown_bin_fallback_stays_supported(self):
        p=self.put('Legacy/bin/adb.exe');self.assertEqual(device._adb_candidates_under(self.root/'Legacy'),[p])
    def test_unknown_deeper_than_five_parts_is_not_scanned(self):
        self.put('Legacy/a/b/c/d/e/adb.exe');self.assertEqual(device._adb_candidates_under(self.root/'Legacy'),[])
    def test_known_layout_prevents_unnecessary_recursive_search(self):
        p=self.install('MuMu','15.0');self.put('MuMu/random/bin/adb.exe')
        with patch.object(device,'_bounded_adb_fallback',side_effect=AssertionError('fallback must not run')):
            self.assertEqual(device._adb_candidates_under(self.root/'MuMu'),[p])
    def test_fallback_exact_five_parts_boundary(self):
        p=self.put('Legacy/a/b/c/d/adb.exe');self.assertEqual(device._bounded_adb_fallback(self.root/'Legacy'),[p])
    def test_fallback_directory_budget(self):
        root=self.root/'Legacy'
        for i in range(10):(root/str(i)).mkdir(parents=True)
        original=device.os.scandir;visited=[]
        def scan(path):visited.append(Path(path));return original(path)
        with patch.object(device,'MAX_FALLBACK_DIRECTORIES',3),patch.object(device.os,'scandir',side_effect=scan):
            self.assertEqual(device._bounded_adb_fallback(root),[])
        self.assertLessEqual(len(visited),3)
    def test_fallback_entry_processing_budget(self):
        root=self.root/'Legacy';root.mkdir();seen=[]
        class Entry:
            name='file.txt';path=str(root/'file.txt')
            def stat(self,**kw):seen.append(1);return types.SimpleNamespace(st_file_attributes=0)
            def is_symlink(self):return False
            def is_dir(self,**kw):return False
        class Entries:
            def __enter__(self):return iter([Entry() for _ in range(20)])
            def __exit__(self,*args):pass
        with patch.object(device,'MAX_FALLBACK_ENTRIES',3),patch.object(device.os,'scandir',return_value=Entries()):
            self.assertEqual(device._bounded_adb_fallback(root),[])
        self.assertEqual(len(seen),3)
    def test_fallback_does_not_descend_symlink_or_windows_reparse_entry(self):
        root=self.root/'Legacy';good=self.put('Legacy/good/adb.exe');original=device.os.scandir;visited=[]
        with original(root) as entries:normal=list(entries)
        class Entry:
            def __init__(self,name,reparse,link):self.name=name;self.path=str(root/name);self.reparse=reparse;self.link=link
            def stat(self,**kw):return types.SimpleNamespace(st_file_attributes=0x400 if self.reparse else 0)
            def is_symlink(self):return self.link
            def is_file(self,**kw):return False
            def is_dir(self,**kw):return True
        class Entries:
            def __enter__(self):return iter([Entry('junction',True,False),Entry('symlink',False,True)]+normal)
            def __exit__(self,*args):pass
        def scan(path):
            path=Path(path);visited.append(path)
            if path==root:return Entries()
            if path.name in ('junction','symlink'):raise AssertionError('followed linked fixture')
            return original(path)
        with patch.object(device.os,'scandir',side_effect=scan):self.assertEqual(device._bounded_adb_fallback(root),[good])
        self.assertNotIn(root/'junction',visited);self.assertNotIn(root/'symlink',visited)
    def test_missing_and_permission_denied_directory_are_tolerated(self):
        self.assertEqual(device._adb_candidates_under(self.root/'absent'),[])
        with patch.object(device.os,'scandir',side_effect=PermissionError('synthetic denied')):
            self.assertEqual(device._adb_candidates_under(self.root/'absent'),[])
    def test_custom_running_nx_main_path(self):
        root=self.root/'custom space/模拟器MuMu';self.put('custom space/模拟器MuMu/nx_main/MuMuManager.exe')
        self.assertEqual(device._running_mumu_roots([root/'nx_main/MuMuManager.exe']),[root])
    def test_running_device_version_reconstructs_install_root(self):
        adb=self.install('CustomMuMu','15.0');exe=self.put('CustomMuMu/nx_device/15.0/shell/nx_device.exe')
        self.assertEqual(device._running_mumu_roots([exe]),[self.root/'CustomMuMu'])
    def test_full_executable_path_not_only_qemu_basename(self):
        self.install('CustomMuMu','15.0');exe=self.put('CustomMuMu/nx_device/15.0/shell/qemu-system-x86_64.exe')
        self.assertEqual(device._running_mumu_roots([exe]),[self.root/'CustomMuMu'])
    def test_legacy_nemu_process_shell_root(self):
        self.put('LegacyNemu/shell/adb.exe');exe=self.put('LegacyNemu/shell/NemuHeadless.exe')
        self.assertIn(self.root/'LegacyNemu',device._running_mumu_roots([exe]))
    def test_duplicate_processes_and_unrelated_executable(self):
        self.install('MuMu','15.0');exe=self.put('MuMu/nx_main/MuMuPlayer.exe');other=self.put('Unrelated/app.exe')
        self.assertEqual(device._running_mumu_roots([exe,exe,other]),[self.root/'MuMu'])
    def test_candidate_roots_running_env_and_config_are_deduplicated(self):
        root=self.root/'CustomMuMu';root.mkdir();self.cfg.return_value={'mumu_dir':str(root)}
        with patch.dict(os.environ,{'XG_MUMU_DIR':str(root)}):
            out=device.candidate_roots(running_roots=[root])
        self.assertEqual(out,[root]);self.proc.assert_not_called()

class SelectionTests(OfflineBase):
    def test_ready_device_wins_over_higher_numeric_version(self):
        a=self.install('High','15.0');b=self.install('Low','12.0')
        self.assertEqual(self.select([self.root/'High',self.root/'Low'],ready=[b])[0],b)
    def test_running_install_wins_when_both_ready(self):
        a=self.install('High','15.0');b=self.install('MuMuRunning','12.0');exe=self.put('MuMuRunning/nx_main/MuMuPlayer.exe')
        self.assertEqual(self.select([self.root/'High',self.root/'MuMuRunning'],[a,b],[exe])[0],b)
    def test_actual_running_device_version_beats_newer_unused_version(self):
        a=self.install('MuMu','15.0');b=self.install('MuMu','12.0');exe=self.put('MuMu/nx_device/12.0/shell/qemu-system-x86_64.exe')
        self.assertEqual(self.select([self.root/'MuMu'],[a,b],[exe])[0],b)
    def test_higher_numeric_version_wins_equal_ready_candidates(self):
        a=self.install('MuMu','9.10');b=self.install('MuMu','12.0');c=self.install('MuMu','15.0')
        self.assertEqual(self.select([self.root/'MuMu'],[a,b,c])[0],c)
    def test_ready_nonrunning_root_beats_running_but_offline_root(self):
        a=self.install('MuMuRunning','15.0');b=self.install('Older','12.0');exe=self.put('MuMuRunning/nx_main/MuMuPlayer.exe')
        self.assertEqual(self.select([self.root/'MuMuRunning',self.root/'Older'],[b],[exe])[0],b)
    def test_no_ready_device_returns_best_existing_candidate(self):
        a=self.install('MuMu','15.0');self.install('MuMu','12.0')
        self.assertEqual(self.select([self.root/'MuMu'])[0],a)
    def test_no_candidate_returns_none_pair(self):self.assertEqual(self.select([]),(None,None))
    def test_environment_adb_explicit_override_never_probes_or_enumerates(self):
        p=self.put('manual/adb.exe');self.cfg.return_value={'adb':str(self.put('other/adb.exe'))}
        with patch.dict(os.environ,{'XG_ADB':str(p)}),patch.object(device,'_probe_adb_devices',side_effect=AssertionError('must not probe')):
            self.assertEqual(device.find_adb()[0],p)
        self.proc.assert_not_called()
    def test_missing_environment_override_keeps_valid_config_compatibility(self):
        p=self.put('manual/adb.exe');self.cfg.return_value={'adb':str(p)}
        with patch.dict(os.environ,{'XG_ADB':str(self.root/'missing/adb.exe')}):self.assertEqual(device.find_adb()[0],p)
    def test_configured_adb_legacy_override(self):
        p=self.put('manual/shell/adb.exe');self.cfg.return_value={'adb':str(p)}
        self.assertEqual(device.find_adb(),(p,p.parent.parent));self.proc.assert_not_called()
    def test_path_fallback_is_available_without_install(self):
        p=self.put('PATH/adb.exe');self.mock('candidate_roots',return_value=[]);self.mock('shutil.which',return_value=str(p));self.mock('_probe_adb_devices',return_value=[])
        self.assertEqual(device.find_adb(),(p,None))
    def test_probe_limit_is_bounded(self):
        for i in range(20):self.install('MuMu',str(i)+'.0')
        self.select([self.root/'MuMu']);self.assertLessEqual(len(self.calls),device.MAX_ADB_PROBES);self.assertEqual(len(self.calls),8)
    def test_probe_wall_clock_budget_is_bounded(self):
        for i in range(10):self.install('MuMu',str(i)+'.0')
        self.mock('candidate_roots',return_value=[self.root/'MuMu']);clock=[0.];calls=[]
        def probe(adb,timeout=2):calls.append(timeout);clock[0]+=4.;return []
        self.mock('_probe_adb_devices',side_effect=probe)
        with patch.object(device.time,'monotonic',side_effect=lambda:clock[0]):device.find_adb()
        self.assertLessEqual(len(calls),2);self.assertTrue(all(0<t<=2 for t in calls))
    def test_actual_timeout_adapter_falls_back_to_next_candidate(self):
        a=self.install('MuMu','15.0');b=self.install('MuMu','12.0');self.mock('candidate_roots',return_value=[self.root/'MuMu'])
        def run(argv,**kw):
            if str(a) in argv:raise subprocess.TimeoutExpired(argv,kw['timeout'])
            return types.SimpleNamespace(returncode=0,stdout=b'List of devices attached\nemulator-9000\tdevice\n',stderr=b'')
        with patch.object(device.xg_exec,'run',side_effect=run):self.assertEqual(device.find_adb()[0],b)

class DeviceListTests(OfflineBase):
    def probe(self,text,code=0):
        with patch.object(device,'adb_run',return_value=(code,text,'')):return device._probe_adb_devices(self.root/'adb.exe')
    def test_header_then_only_online_device_lines(self):
        text='* daemon started successfully *\r\nList of devices attached\r\n127.0.0.1:19001\tdevice\r\nemulator-5588 device\r\nR5USB123 device\r\na offline\r\nb unauthorized\r\n'
        self.assertEqual(self.probe(text),['127.0.0.1:19001','emulator-5588','R5USB123'])
    def test_duplicates_are_removed(self):self.assertEqual(self.probe('List of devices attached\na device\na device\n'),['a'])
    def test_nonzero_exit_is_never_ready(self):self.assertEqual(self.probe('List of devices attached\na device',1),[])
    def test_invalid_bare_or_error_output_is_never_ready(self):
        for value in ['foo device','error: device not found','adb devices','List of device attached\nfoo device','List of devices attached\nerror: device not found']:
            with self.subTest(value=value):self.assertEqual(self.probe(value),[])
    def test_long_listing_metadata_remains_compatible(self):
        self.assertEqual(self.probe('List of devices attached\nR5USB device product:test model:fake transport_id:1'),['R5USB'])
    def test_offline_unauthorized_recovery_states_excluded(self):
        self.assertEqual(self.probe('List of devices attached\na offline\nb unauthorized\nc recovery\nd bootloader\ne unknown'),[])

class FindGameTests(OfflineBase):
    def flow(self,ready,game,*,post_connect=None):
        adb=self.root/'adb.exe';connected=[]
        def run(path,*args,serial=None,timeout=20):
            self.calls.append((args,serial,timeout))
            if args==('devices',):
                states=post_connect if connected and post_connect is not None else ready
                return 0,'List of devices attached\n'+'\n'.join(k+' '+v for k,v in states.items()),''
            if args and args[0]=='connect':connected.append(args[1]);return 0,'connected to '+args[1],''
            if args and args[0]=='shell':return (0,str(game[serial]),'') if serial in game else (1,'','not running')
            raise AssertionError(args)
        self.mock('adb_run',side_effect=run);return adb
    def test_nondefault_connected_tcp_serial_is_discovered(self):
        adb=self.flow({'127.0.0.1:19123':'device'},{'127.0.0.1:19123':4321})
        self.assertEqual(device.find_game(adb),{'serial':'127.0.0.1:19123','pid':4321});self.assertFalse(any(c[0][0]=='connect' for c in self.calls))
    def test_emulator_ready_never_connects_before_pid(self):
        serial='emulator-5588';adb=self.flow({serial:'device'},{serial:123})
        self.assertEqual(device.find_game(adb),{'serial':serial,'pid':123})
        self.assertFalse(any(c[0][0]=='connect' for c in self.calls))
    def test_usb_device_is_not_queried(self):
        serial='R5USB123';adb=self.flow({serial:'device'},{serial:123});diag={}
        self.assertIsNone(device.find_game(adb,diagnostics=diag))
        self.assertEqual(diag['reason'],'unsupported_device_scope')
        self.assertFalse(any(c[1]==serial for c in self.calls))
    def test_automatic_local_instance_preferred_when_usb_is_listed_first(self):
        for local in ['127.0.0.1:19123','localhost:19123','[::1]:19123','emulator-5588']:
            with self.subTest(local=local):
                self.calls=[];adb=self.flow({'R5USB123':'device',local:'device'},{'R5USB123':1,local:2})
                self.assertEqual(device.find_game(adb),{'serial':local,'pid':2})
                self.assertFalse(any(c[0][0]=='connect' for c in self.calls))
    def test_explicit_usb_override_is_refused_with_local_fallback(self):
        adb=self.flow({'emulator-5588':'device','R5USB123':'device'},{'emulator-5588':1,'R5USB123':2})
        with patch.dict(os.environ,{'XG_ADB_SERIAL':'R5USB123'}):self.assertEqual(device.find_game(adb)['serial'],'emulator-5588')
        self.assertFalse(any(c[0][0]=='connect' or c[1]=='R5USB123' for c in self.calls))
    def test_ready_instance_without_game_does_not_hide_later_ready_game(self):
        adb=self.flow({'emulator-5588':'device','emulator-5600':'device'},{'emulator-5600':2})
        self.assertEqual(device.find_game(adb)['serial'],'emulator-5600')
    def test_configured_main_instance_preferred_within_ready_list(self):
        self.cfg.return_value={'serial':'emulator-5600'};adb=self.flow({'emulator-5588':'device','emulator-5600':'device'},{'emulator-5588':1,'emulator-5600':2})
        self.assertEqual(device.find_game(adb)['serial'],'emulator-5600')
    def test_environment_serial_beats_config_and_explicit_legacy_list(self):
        self.cfg.return_value={'serial':'emulator-5600'};adb=self.flow({'emulator-5588':'device','emulator-5600':'device'},{'emulator-5588':1,'emulator-5600':2})
        with patch.dict(os.environ,{'XG_ADB_SERIAL':'emulator-5588'}):self.assertEqual(device.find_game(adb,serials=['emulator-5600'])['serial'],'emulator-5588')
    def test_explicit_serial_argument_remains_supported(self):
        adb=self.flow({'emulator-5588':'device','emulator-5600':'device'},{'emulator-5588':1,'emulator-5600':2})
        self.assertEqual(device.find_game(adb,serials=['emulator-5600'])['serial'],'emulator-5600')
    def test_offline_or_unauthorized_never_gets_pid_query(self):
        for state in ['offline','unauthorized']:
            with self.subTest(state=state):
                self.calls=[];adb=self.flow({'127.0.0.1:16384':state},{'127.0.0.1:16384':999})
                self.assertIsNone(device.find_game(adb));self.assertFalse(any(c[0][0]=='shell' for c in self.calls))
    def test_tcp_fallback_requires_new_ready_proof_after_connect(self):
        serial='127.0.0.1:16384';adb=self.flow({}, {serial:456},post_connect={serial:'device'})
        self.assertEqual(device.find_game(adb),{'serial':serial,'pid':456})
        actions=[c[0][0] for c in self.calls];self.assertEqual(actions[:4],['devices','connect','devices','shell'])
    def test_connect_success_text_without_ready_is_not_enough(self):
        adb=self.flow({}, {'127.0.0.1:16384':456},post_connect={})
        self.assertIsNone(device.find_game(adb));self.assertFalse(any(c[0][0]=='shell' for c in self.calls))
    def test_non_tcp_explicit_offline_serial_never_connected(self):
        adb=self.flow({},{});self.assertIsNone(device.find_game(adb,serials=['R5USB123','emulator-5588']))
        self.assertTrue(all(c[0][1] not in ['R5USB123','emulator-5588'] for c in self.calls if c[0][0]=='connect'))
    def test_no_game_search_has_total_deadline(self):
        clock=[0.];adb=self.root/'adb.exe'
        def run(path,*args,**kw):clock[0]+=20;self.calls.append(args);return 1,'','timed out'
        self.mock('adb_run',side_effect=run)
        with patch.object(device.time,'monotonic',side_effect=lambda:clock[0]):self.assertIsNone(device.find_game(adb))
        self.assertLessEqual(len(self.calls),3)

class WindowsHandleTests(OfflineBase):
    def rows(self):return [(10,'MuMuPlayer.exe',self.root/'MuMu/nx_main/MuMuPlayer.exe'),(11,'qemu-system-x86_64.exe',self.root/'MuMu/nx_device/15.0/qemu-system-x86_64.exe'),(12,'notepad.exe',self.root/'Other/notepad.exe')]
    def test_full_process_path_filters_and_all_handles_closed(self):
        api=FakeKernel(self.rows());paths=PROCESS(api)
        self.assertEqual(set(paths),{self.rows()[0][2],self.rows()[1][2]})
        self.assertEqual(set(api.closed),{111,*[h for h,r,i in api.opened]});self.assertEqual(len(api.closed),len(set(api.closed)))
        self.assertTrue(all(rights==0x1000 and inherit is False for h,rights,inherit in api.opened))
    def test_access_denied_process_skipped_other_processes_continue(self):
        api=FakeKernel(self.rows(),deny=[10]);paths=PROCESS(api)
        self.assertIn(self.rows()[1][2],paths);self.assertNotIn(self.rows()[0][2],paths);self.assertNotIn(0,api.closed);self.assertIn(111,api.closed)
    def test_query_failure_and_permission_exception_release_process_handles(self):
        for kwargs in [dict(query_fail=[10]),dict(query_raise=[10])]:
            with self.subTest(kwargs=kwargs):
                api=FakeKernel(self.rows(),**kwargs);PROCESS(api);self.assertIn(1010,api.closed);self.assertIn(111,api.closed)
    def test_enumeration_exception_still_closes_snapshot_and_open_process(self):
        api=FakeKernel(self.rows(),next_raise=True);PROCESS(api);self.assertEqual(set(api.closed),{1010,111})
    def test_invalid_snapshot_is_never_closed(self):
        for value in [0,None,-1,ctypes.c_void_p(-1).value]:
            with self.subTest(value=value):
                api=FakeKernel(self.rows(),snapshot=value);self.assertEqual(PROCESS(api),[]);self.assertEqual(api.closed,[])
    def test_empty_snapshot_is_closed(self):
        api=FakeKernel();self.assertEqual(PROCESS(api),[]);self.assertEqual(api.closed,[111])
    def test_snapshot_first_failure_still_releases_snapshot(self):
        api=FakeKernel(self.rows())
        with patch.object(api,'Process32FirstW',side_effect=PermissionError('denied')):self.assertEqual(PROCESS(api),[])
        self.assertEqual(api.closed,[111]);self.assertEqual(api.opened,[])
    def test_snapshot_creation_failure_returns_without_handle(self):
        api=FakeKernel(self.rows())
        with patch.object(api,'CreateToolhelp32Snapshot',side_effect=PermissionError('denied')):self.assertEqual(PROCESS(api),[])
        self.assertEqual(api.closed,[])
    def test_close_failure_does_not_skip_final_snapshot_close(self):
        api=FakeKernel(self.rows());close=api.CloseHandle
        def closing(handle):
            close(handle)
            if handle==1010:raise PermissionError('close denied')
            return 1
        with patch.object(api,'CloseHandle',side_effect=closing):PROCESS(api)
        self.assertIn(111,api.closed);self.assertEqual(set(api.closed),{111,*[h for h,r,i in api.opened]})
    def test_process_enumeration_cap_is_bounded(self):
        api=FakeKernel(self.rows())
        with patch.object(device,'MAX_PROCESS_ENTRIES',2):PROCESS(api)
        self.assertEqual(len(api.opened),2);self.assertIn(111,api.closed)
    def test_registry_vendor_and_dynamic15_keys_are_exact_and_handles_closed(self):
        keys=[];closed=[];root=self.root/'RegistryMuMu';root.mkdir()
        class Key:
            def __init__(self,hive,name):self.hive=hive;self.name=name
            def __enter__(self):return self
            def __exit__(self,*args):closed.append((self.hive,self.name))
        def open_key(hive,name):keys.append((hive,name));return Key(hive,name)
        def enum(key,index):
            if index:return (_ for _ in ()).throw(OSError('end'))
            return 'MuMuPlayer-15.0'
        def value(key,name):
            if name=='InstallPath':return str(root),1
            raise OSError('missing value')
        fake=types.SimpleNamespace(HKEY_CURRENT_USER=1,HKEY_LOCAL_MACHINE=2,OpenKey=open_key,EnumKey=enum,QueryValueEx=value)
        with patch.dict(sys.modules,{'winreg':fake}):result=REGISTRY()
        self.assertEqual(result,[root]);self.assertEqual(keys,closed)
        for hive in (1,2):
            for vendor in (r'SOFTWARE\Netease',r'SOFTWARE\WOW6432Node\Netease'):
                for suffix in ('',r'\MuMuPlayer',r'\MuMuPlayer-12.0',r'\MuMuPlayer-15.0'):
                    self.assertIn((hive,vendor+suffix),keys)
        self.assertTrue(all(name.startswith((r'SOFTWARE\Netease',r'SOFTWARE\WOW6432Node\Netease')) for hive,name in keys))

if __name__=='__main__':unittest.main()
