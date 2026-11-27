"""清理安全回归。所有文件都是在临时工作区新建的合成数据。"""
import ast
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP / 'launcher'))
import data_cleanup as dc


class Cleanup18Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='清理 安全-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.start()

    def start(self):
        self.s = dc.CleanupService(self.root, is_online=lambda:False, is_busy=lambda:False)

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def execute(self):
        p = self.s.preview({'terrain':True})
        self.assertTrue(p['ok'], p)
        r = self.s.execute(p['plan_id'], True)
        self.assertTrue(r['ok'], r)
        return r

    def purge(self):
        p = self.s.purge_preview()
        self.assertTrue(p['ok'], p)
        r = self.s.purge(p['plan_id'],True)
        self.assertTrue(r['ok'], r)

    def test_check_lexical_data_root_including_control_api(self):
        api_file = APP / 'bot' / 'control_api.py'
        func = next(i for i in ast.parse(api_file.read_text(encoding='utf-8-sig')).body
                    if isinstance(i, ast.FunctionDef) and i.name=='_cleanup_service')
        other = self.root / '转接路径'
        other.mkdir()
        globs = {'Path':Path, '_CLEANUP_SERVICE':None, '_CLEANUP_ROOT':None,
                 '_cleanup_online':lambda:False, '_cleanup_busy':lambda:False,
                 'xg_paths':SimpleNamespace(DATA=other)}
        exec(compile(ast.Module(body=[func],type_ignores=[]), str(api_file), 'exec'),globs)
        real = Path.lstat
        def junction(path,*args,**kwargs):
            if path==other:
                return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400,st_nlink=1)
            return real(path,*args,**kwargs)
        with patch.object(Path, 'lstat', junction):
            with self.assertRaises(dc.CleanupError) as error:globs['_cleanup_service']()
        self.assertEqual(error.exception.code, 'unsafe_link')
        with self.assertRaises(dc.CleanupError):
            dc.CleanupService(other/'..',is_online=lambda:False,is_busy=lambda:False)

    def test_real_hop_proof_roundtrip_including_large_and_pending(self):
        for suffix, schema in [('', 'bounded-hop-proof-v21-contact'),('.pending','bounded-aerial-proof-v22')]:
            hid = str(uuid4())
            name = 'hop-proofs/hop-'+hid+'.json'+suffix
            receipt = json.dumps({'schema':schema,'hopId':hid,'trace':[]}).encode('utf-8') + b' ' * dc.SCAN_LIMIT
            path = self.put(name, receipt)
            self.execute()
            self.assertFalse(path.exists())
            preview = self.s.restore_preview();self.assertTrue(preview['ok'],preview)
            restored = self.s.restore(preview['plan_id'],True);self.assertTrue(restored['ok'],restored)
            self.assertEqual(path.read_bytes(),receipt)
            path.unlink()

    def test_private_config_blobs_and_unrecognized_proofs_untouched(self):
        protected = [self.put(rel,b'x'*(dc.SCAN_LIMIT+1)) for rel in
                     ('control/tour_routes.json','control/spawn_y_fix.json','runtime/private/scene_blobs/23.bin',
                      'control/light_candle.json','control/zero_darkstones.json',
                      'hop-proofs/notes.json', 'hop-proofs/hop-'+'-'*36+'.json')]
        self.put('game/0.16.8/terrain/pos.f32', b'ok')
        self.execute();self.purge()
        self.assertTrue(all(p.exists() and p.stat().st_size==dc.SCAN_LIMIT+1 for p in protected))

    def test_private_names_inside_generated_roots_are_not_opened(self):
        for leaf in ('tour_routes.json.pending','spawn_y_fix.json.pending','light_candle.json',
                     'zero_darkstones.json.pending','scene_blobs/23.bin'):
            with self.subTest(leaf=leaf):
                path=self.put('game/0.16.8/terrain/'+leaf,b'{"keep":true}')
                real=Path.open
                def forbid_open(candidate,*args,**kwargs):
                    if candidate==path:raise AssertionError('受保护文件不得打开')
                    return real(candidate,*args,**kwargs)
                with patch.object(Path,'open',forbid_open):preview=self.s.preview({'terrain':True})
                self.assertEqual(preview['error_code'],'sensitive_data',preview)
                self.assertEqual(path.read_bytes(),b'{"keep":true}')
                path.unlink()

    def test_empty_first_install_can_preview_without_writing(self):
        result=self.s.preview({kind:True for kind in dc.CATEGORIES})
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['total_files'],0)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_device_aliases_are_never_relative_data_names(self):
        for name in ('CoNIn$.json','CONOUT$','COM¹.json','lPt².bin','Nul .json'):
            self.assertFalse(self.s._safe_relative('game/0.16.8/terrain/'+name),name)
        self.assertTrue(self.s._safe_relative('game/0.16.8/terrain/ком1.json'))

    @unittest.skipUnless(os.name=='nt','Windows 设备路径语义')
    def test_valid_extended_drive_and_invalid_device_namespace(self):
        prefix=chr(92)*2+'?'+chr(92)
        opts={'is_online': lambda:False,'is_busy': lambda:False}
        self.assertEqual(dc.CleanupService(prefix+str(self.root),**opts).root,self.root)
        for name in (prefix+'GLOBALROOT'+chr(92)+'Device',chr(92)*2+'.'+chr(92)+'C:'+chr(92)+'test'):
            with patch.object(dc._DiskPath,'lstat',side_effect=AssertionError('不应访问设备')):
                with self.assertRaises(dc.CleanupError) as error:dc.CleanupService(name,**opts)
            self.assertEqual(error.exception.code,'invalid_data_root')

    def test_unclassified_xg_pending_is_neither_opened_nor_deleted(self):
        for suffix in ('.pending','.json.pending'):
            with self.subTest(suffix=suffix):
                leaf='.xg-write-'+uuid4().hex+suffix
                path=self.put('game/0.16.8/terrain/'+leaf,b'synthetic-private-data')
                real=Path.open
                def forbid_open(candidate,*args,**kwargs):
                    if candidate==path:raise AssertionError('未知临时文件不得打开')
                    return real(candidate,*args,**kwargs)
                with patch.object(Path,'open',forbid_open):preview=self.s.preview({'terrain':True})
                self.assertEqual(preview['error_code'],'unclassified_pending',preview)
                self.assertEqual(path.read_bytes(),b'synthetic-private-data');path.unlink()
        p=self.put('control/.xg-write-'+uuid4().hex+'.pending',b'private')
        self.execute();self.purge();self.assertEqual(p.read_bytes(),b'private')

    def test_receipt_filename_alone_is_not_proof_of_ownership(self):
        path = self.put('hop-proofs/hop-'+str(uuid4())+'.json',b'{"user":"keep"}')
        r = self.s.preview({'terrain':True});self.assertEqual(r['error_code'],'unknown_generated_file',r)
        self.assertTrue(path.exists())

    def test_linked_or_nested_receipt_directory_never_followed(self):
        path = self.root/'hop-proofs'/('hop-'+str(uuid4())+'.json')
        path.mkdir(parents=True)
        preview=self.s.preview({'terrain':True});self.assertEqual(preview['error_code'],'unknown_generated_file',preview)

    def test_atomic_replace_transient_or_persistent_lock(self):
        path = self.put('control/sample.json',b'old'); replace = os.replace; attempts = []
        def locked_then_open(a,b):
            attempts.append(1)
            if len(attempts)<3:raise PermissionError('临时锁')
            return replace(a,b)
        with patch.object(dc.time,'sleep'), patch.object(dc.os,'replace',side_effect=locked_then_open):
            self.s._atomic(path,b'new')
        self.assertEqual(len(attempts),3);self.assertEqual(path.read_bytes(),b'new')
        with patch.object(dc.time,'sleep'), patch.object(dc.os,'replace',side_effect=PermissionError('持续锁')) as call:
            with self.assertRaises(PermissionError):self.s._atomic(path,b'bad')
        self.assertEqual(call.call_count,4);self.assertEqual(path.read_bytes(),b'new')
        self.assertEqual(list(path.parent.glob('.cleanup-*')),[])

    def test_unlisted_file_introduced_after_preview_survives_purge(self):
        self.put('game/0.16.8/terrain/pos.f32', b'ok');result=self.execute();p=self.s.purge_preview()
        real_save=self.s._save_batch
        newcomer=self.root/result['trash_path']/'game/0.16.8/terrain/用户自留.bin'
        def inject(doc):
            real_save(doc)
            if doc['status']=='purging':newcomer.write_bytes(b'keep')
        with patch.object(self.s,'_save_batch',side_effect=inject):r=self.s.purge(p['plan_id'],True)
        self.assertFalse(r['ok']);self.assertEqual(newcomer.read_bytes(),b'keep')

    def test_interrupted_between_removing_journal_and_writing_index_is_resumable(self):
        self.put('game/0.16.8/terrain/pos.f32', b'ok');result=self.execute();p=self.s.purge_preview()
        folder=self.root/result['trash_path'];real=Path.rmdir
        def locked(path):
            if path==folder:raise PermissionError('锁目录')
            return real(path)
        with patch.object(Path,'rmdir',locked):r=self.s.purge(p['plan_id'],True)
        self.assertEqual(r['error_code'],'purge_partial');self.assertFalse((folder/dc.MANIFEST).exists())
        self.start();self.purge();self.assertFalse(folder.exists())

    def test_chinese_space_and_deep_paths_round_trip(self):
        name='game/0.16.8/terrain/'+'/'.join(['长路径 模拟'+str(i)+'甲'*30 for i in range(7)])+'/pos.f32'
        path=dc._DiskPath(self.root)/name
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'123');self.assertGreater(len(str(path)),260)
        try:
            self.execute();p=self.s.restore_preview();self.assertTrue(p['ok'],p)
            restored=self.s.restore(p['plan_id'],True);self.assertTrue(restored['ok'], restored);self.assertEqual(path.read_bytes(),b'123')
            self.execute();self.purge()
        finally:
            # 临时目录库自身无长路径支持，收尾同样使用受限定的扩展路径。
            if os.name=='nt':
                import shutil
                shutil.rmtree('\\\\?\\' + str(self.root))


if __name__=='__main__':unittest.main()
