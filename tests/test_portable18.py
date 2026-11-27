"""本地配置和位姿测试：不用游戏采集文件、连接、进程或账号。"""
import copy
import io
import json
from pathlib import Path
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'),str(ROOT/'app/bot'),str(ROOT/'app/src')]
import xg_paths as paths


class LocalTables(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.control = Path(self.tmp.name)/'中文 用户'/ 'control'
        self.control.mkdir(parents=True)
        p = mock.patch.object(paths,'CONTROL',self.control)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(paths,'CONFIG_DIR',ROOT/'app/config')
        p.start(); self.addCleanup(p.stop)

    def test_published_routes_minimal(self):
        d=paths.read_tour_routes()
        self.assertEqual(d['schema'],'xg-tour-routes-v1')
        self.assertGreater(len(d['maps']),0)
        for m in d['maps']:
            self.assertFalse({'source','event_candles','waypoint_detail'} & m.keys())
        self.assertFalse(list(self.control.iterdir()))

    def test_replacement_works_without_install_writes(self):
        m=copy.deepcopy(paths.read_tour_routes()['maps'][0])
        p=self.control/'tour_routes.json'
        paths.write_json(p,{'schema':'xg-tour-routes-v1','maps':[m]})
        self.assertEqual(paths.read_tour_routes()['maps'],[m])
        v={'schema':'xg-tour-routes-v1','maps':[]}
        paths.write_json(p,v)
        self.assertEqual(paths.read_tour_routes(),v)

    def test_invalid_and_oversized_routes_use_bundle(self):
        expected=paths.read_tour_routes()
        p=self.control/'tour_routes.json'
        for raw in [b'\xff',b'[]',b'x'*(2*1024*1024+1),json.dumps(
            {'schema':expected['schema'],'maps':[{'scene':'A','map_id':1,'travel_query':'A','waypoints':[[0,float('nan'),0]]}]}).encode()]:
            p.write_bytes(raw)
            self.assertEqual(paths.read_tour_routes(),expected)

    def test_absent_resources_leave_empty_route_and_original_height(self):
        with mock.patch.object(paths,'CONFIG_DIR',self.control/'空'):
            self.assertEqual(paths.read_tour_routes()['maps'],[])
            self.assertEqual(paths.corrected_spawn_y(7,8),8)

    def test_special_names_rejected_before_io(self):
        with self.assertRaises(ValueError):
            list(paths._table_document('../outside.json'))

    def test_height_only_at_known_spawn_near_wire(self):
        rows=json.loads((paths.CONFIG_DIR/'spawn_y_fix.json').read_text(encoding='utf-8'))
        for key,v in rows.items():
            self.assertAlmostEqual(paths.corrected_spawn_y(int(key),v['wire']),v['wire']+v['delta'])
            self.assertEqual(paths.corrected_spawn_y(int(key),v['wire']+1),v['wire']+1)
        self.assertEqual(paths.corrected_spawn_y(9,10),10)

    def test_bad_height_bounds_types_and_overrides(self):
        p=self.control/'spawn_y_fix.json'
        for v in [-1,0.02,0.51,True,float('inf'),'0.2',10**200]:
            p.write_text(json.dumps({'7':{'wire':5,'delta':v}}),encoding='utf-8')
            self.assertEqual(paths.corrected_spawn_y(7,5),5)
        paths.write_json(p,{'7':{'wire':5,'delta':0.2}})
        self.assertEqual(paths.corrected_spawn_y(7,5),5.2)

    def test_data_dirs_do_not_share_account_specific_table(self):
        paths.write_json(self.control/'tour_routes.json',{'schema':'xg-tour-routes-v1','maps':[]})
        with mock.patch.object(paths,'CONTROL',self.control.parent/'另一个用户'):
            self.assertGreater(len(paths.read_tour_routes()['maps']),0)
        self.assertEqual(paths.read_tour_routes()['maps'],[])


class PortableIO(unittest.TestCase):
    def test_helpers_resolve_from_install_and_user_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)/'中文 空格'
            with mock.patch.dict(os.environ,{'LOCALAPPDATA':str(base)}):
                self.assertEqual(paths.default_desktop_data(),base/'XiaoGuangPrivate/data')
            bundled=base/'runtime/python/python.exe'
            bundled.parent.mkdir(parents=True)
            bundled.touch()  # 仅查位置，绝不运行
            with mock.patch.object(paths,'INSTALL_ROOT',base),mock.patch.object(paths,'IS_WINDOWS',True),mock.patch.dict(os.environ,{'XG_PYTHON':''}):
                self.assertEqual(paths.python_exe(),str(bundled))
                os.environ['XG_PYTHON']='显式设置'
                self.assertEqual(paths.python_exe(),'显式设置')

    @unittest.skipUnless(os.name=='nt','仅 Windows 使用盘符和扩展路径')
    def test_drive_and_unc_layout_lexically_only(self):
        for letter in ('D','Z'):
            p=letter+':'+chr(92)+'中文 空格'+chr(92)+'配置.json'
            self.assertEqual(paths.long_path(p,force=True),chr(92)*2+'?'+chr(92)+p)
        sl=chr(92);share=2*sl+'test.invalid'+sl+'share'+sl+'本地演算'
        self.assertEqual(paths.long_path(share,force=True),2*sl+'?'+sl+'UNC'+sl+share[2:])

    @unittest.skipUnless(os.name=='nt','仅 Windows 使用扩展路径')
    def test_long_normalization_and_unc_short_fallback_without_io(self):
        import ctypes
        sl=chr(92)
        prefix=2*sl+'?'+sl
        self.assertEqual(paths.long_path('Z:'+sl+'甲'+sl+'..'+sl+'乙',force=True),prefix+'Z:'+sl+'乙')
        share=2*sl+'test.invalid'+sl+'本地演算'+sl+'测试'
        with mock.patch.object(ctypes.windll.kernel32,'GetShortPathNameW',return_value=0) as get:
            self.assertEqual(paths.short_path(prefix+'UNC'+sl+share[2:]),share)
            self.assertEqual(get.call_args.args[0],share)

    @unittest.skipUnless(os.name=='nt','Windows API 按 UTF-16 计路径长度')
    def test_path_prefix_uses_utf16_units(self):
        sl=chr(92)
        lexical='Z:'+sl+(chr(0x1f308)*90+sl)*2+'资料.json'
        self.assertLess(len(lexical),paths.LONG_PATH_THRESHOLD)
        self.assertGreater(len(lexical.encode('utf-16-le'))//2,260)
        self.assertEqual(paths.long_path(lexical),2*sl+'?'+sl+lexical)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            dirp=Path(tmp)/('新 用户 '+chr(0x1f308)*110)
            dest=dirp/'资料.json'
            self.assertTrue(paths.long_path(dest).startswith(chr(92)*2+'?'+chr(92)))
            try:
                paths.write_json(dest,{'名':'资料'})
                self.assertEqual(paths.read_json(dest),{'名':'资料'})
            finally:
                import shutil
                if os.path.isdir(paths.long_path(dirp,force=True)):
                    shutil.rmtree(paths.long_path(dirp,force=True))

    @unittest.skipUnless(os.name=='nt','Windows 暂存名比最终文件名长')
    def test_short_destination_uses_extended_staging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=paths.LONG_PATH_THRESHOLD-1-len(str(tmp).encode('utf-16-le'))//2-1
            self.assertGreater(m,0)
            dest=Path(tmp)/('甲'*m)/'配置.json'
            self.assertFalse(paths.long_path(dest.parent).startswith(chr(92)*2+'?'+chr(92)))
            try:
                with mock.patch.object(paths.tempfile,'NamedTemporaryFile',wraps=tempfile.NamedTemporaryFile) as stage:
                    paths.write_json(dest,{'名':'资料'})
                self.assertEqual(stage.call_args.kwargs['dir'],paths.long_path(dest.parent,force=True))
                self.assertEqual(paths.read_json(dest),{'名':'资料'})
            finally:
                import shutil
                if os.path.isdir(paths.long_path(dest.parent,force=True)):
                    shutil.rmtree(paths.long_path(dest.parent,force=True))

    def test_legacy_stdout_no_encoding_exception(self):
        b=io.BytesIO()
        s=io.TextIOWrapper(b,encoding='cp437')
        paths.safe_console(s)
        s.write('中文');s.flush()
        self.assertTrue(b.getvalue())
        s.detach()
        paths.safe_console(None)

    def test_concurrent_writes_use_distinct_staging_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'空 格.json'
            gate=threading.Barrier(2,timeout=4)
            replace=paths._replace_retry
            names=[];errs=[]
            def replace_checked(src,dst):
                names.append(src)
                gate.wait()
                replace(src,dst)
            def run(v):
                try: paths.write_json(dest,{'n':v})
                except Exception as e: errs.append(type(e).__name__)
            with mock.patch.object(paths,'_replace_retry',replace_checked):
                jobs=[threading.Thread(target=run,args=(i,)) for i in range(2)]
                for t in jobs:t.start()
                for t in jobs:t.join(8)
            self.assertFalse(errs)
            self.assertEqual(len(set(names)),2)
            self.assertIn(json.loads(dest.read_text(encoding='utf-8')),({'n':0},{'n':1}))
            self.assertEqual(list(dest.parent.glob('*.pending')),[])

    def test_long_path_write_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/('多字节目录 '*12+'一')/('甲'*75)/('乙'*75)/('丙'*75)/'配置.json'
            self.assertGreater(len(str(p)),260)
            try:
                paths.write_json(p,{'名':'中文'})
                self.assertEqual(paths.read_json(p),{'名':'中文'})
            finally:
                import shutil
                first=Path(tmp)/p.relative_to(tmp).parts[0]
                if os.path.isdir(paths.long_path(first,force=True)):
                    shutil.rmtree(paths.long_path(first,force=True))

    @unittest.skipUnless(os.name=='nt','仅 Windows 有共享锁')
    def test_real_windows_deny_keeps_original(self):
        import ctypes
        from ctypes import wintypes as w
        lib=ctypes.WinDLL('kernel32',use_last_error=True)
        op=lib.CreateFileW
        op.restype=w.HANDLE
        op.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
        lib.CloseHandle.argtypes=[w.HANDLE]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'被占用.json'
            paths.write_json(p,{'原':1})
            handle=op(str(p),0x80000000,1,None,3,0x80,None)
            self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
            try:
                with mock.patch('time.sleep'),self.assertRaises(PermissionError):
                    paths.write_json(p,{'新':2})
            finally:
                lib.CloseHandle(handle)
            self.assertEqual(paths.read_json(p),{'原':1})
            paths.write_json(p,{'新':2})
            self.assertEqual(paths.read_json(p),{'新':2})


class RegistrationIdempotence(unittest.TestCase):
    def _feed(self, saved, sequence, terminal=None):
        from qfsky.home_handoff_runtime0167 import _RoomHandoffPump
        buffered=[]
        slot=SimpleNamespace(enter_body=saved,stage_deadline=20,token=None,
                             transport=SimpleNamespace(reliable_status=lambda unused: 'pending'))
        def fail(reason):
            raise ValueError(reason)
        stub=SimpleNamespace(active=slot,_terminal=terminal or (lambda m:None),_stats={},_fatal=fail,
                             _clock=lambda:10,_buffer=lambda slot,items:buffered.extend(items))
        _RoomHandoffPump._advance_registration(stub,sequence,10)
        return buffered,stub._stats

    def test_only_identical_retransmissions_removed(self):
        duplicate=SimpleNamespace(message_type=17,body=b'A')
        a,b=[SimpleNamespace(message_type=1,body=bytes([i])) for i in (0,1)]
        kept,stats=self._feed(b'A',[a,duplicate,b,duplicate])
        self.assertEqual(kept,[a,b])
        self.assertEqual(stats['registration_dup_enter'],2)

    def test_distinct_messages_and_unknown_previous_still_fail(self):
        for saved,kind,body in [(None,17,b'A'),(b'A',17,b'B')]:
            with self.assertRaisesRegex(ValueError,'registration_unexpected_control'):
                self._feed(saved,[SimpleNamespace(message_type=kind,body=body)])
        # tf18：线上版注册期收到 Move 走“回忙”路径，坏正文同样失败（报正文长度错）
        with self.assertRaises(ValueError):
            self._feed(b'A',[SimpleNamespace(message_type=9,body=b'A')])

    def test_terminal_checked_before_deduplication(self):
        terminal=mock.Mock(side_effect=ValueError('终止'))
        with self.assertRaisesRegex(ValueError,'终止'):
            self._feed(b'A',[SimpleNamespace(message_type=17,body=b'A')],terminal)
        terminal.assert_called_once()


class LocalPose(unittest.TestCase):
    def setUp(self):
        import room_pose
        self.mod=room_pose

    def test_same_map_engine_rebuild_vs_genuine_map_change(self):
        p=self.mod
        for old,new,keep in [(7,7,True),(7,8,False),(None,7,False)]:
            tr=SimpleNamespace(ready=False,closed_map_id=old,lock=threading.RLock(),seat_book={},
                               confirmed_map_id=new,begin_room=mock.Mock(return_value=True))
            ctl=SimpleNamespace(map_id=old,position=(1,2,3),reset=mock.Mock(),log=mock.Mock())
            with mock.patch.object(p.map_long_engine,'OUTBOX',[]):
                self.assertTrue(p._apply_validated_room(tr,ctl,object(),b''))
            self.assertEqual(ctl.reset.call_args.kwargs['keep_pose'],keep)
            self.assertEqual(ctl.log.call_args.kwargs['engine_rebuilt'],keep)
            self.assertIsNone(tr.closed_map_id)

    def test_connection_boundary_clears_marker(self):
        tr=self.mod.RoomTracker()
        engine=object()
        tr._engine=engine;tr.ready=True;tr.confirmed_map_id=7
        self.assertFalse(tr.end_room(object()))
        self.assertTrue(tr.end_room(engine))
        self.assertEqual(tr.closed_map_id,7)
        tr.end_segment();self.assertIsNone(tr.closed_map_id)
        tr.closed_map_id=8
        tr.begin_segment(9);self.assertIsNone(tr.closed_map_id)

    def test_side_stays_put_until_a_new_hold(self):
        p=self.mod
        ctl=SimpleNamespace(tracker=SimpleNamespace(origins={0:(0,0,0,0)},yaws={}),
                            terrain_ground=lambda *args:0,log=mock.Mock())
        with mock.patch.object(p,'HOLD_FOLLOWER_FLAGS',False):
            for start in (-2,2):
                state={'slot':0}
                a=p.PoseController._hold_attach(ctl,state,1,(start,0,0),0,b'',False)[0]
                b=p.PoseController._hold_attach(ctl,state,2,(-start,0,0),0,b'',False)[0]
                self.assertEqual(a,b);self.assertGreater(a[0]*start,0)

    def test_spawn_updates_local_values_together(self):
        ctl=SimpleNamespace(home=(1,2,3),seed=b'S',base=b'B',map_id=7,holding=False,social=None,
                            _set_override=mock.Mock(),log=mock.Mock())
        with mock.patch.object(self.mod.xg_paths,'corrected_spawn_y',return_value=2.1),mock.patch.object(
                self.mod.anim_pose,'write_origin',side_effect=lambda blob,home:blob+b'!'):
            self.assertTrue(self.mod.PoseController._spawn_y_fix(ctl,'测试'))
        self.assertEqual(ctl.home,ctl.position);self.assertEqual(ctl.home,(1,2.1,3))
        self.assertEqual(ctl.base,b'B!');self.assertEqual(ctl.seed,b'S!')

    def test_spawn_leaves_original_on_partial_pose_input(self):
        ctl=SimpleNamespace(home=(1,2,3),seed=b'S',base=None,map_id=7)
        with mock.patch.object(self.mod.xg_paths,'corrected_spawn_y',return_value=2.1),mock.patch.object(
                self.mod.anim_pose,'write_origin',side_effect=[b'changed',ValueError('missing')]):
            self.assertFalse(self.mod.PoseController._spawn_y_fix(ctl,'测试'))
        self.assertEqual(ctl.home,(1,2,3));self.assertEqual(ctl.seed,b'S');self.assertIsNone(ctl.base)
