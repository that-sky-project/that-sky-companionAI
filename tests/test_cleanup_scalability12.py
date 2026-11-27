"""Cleanup scalability: workspace-only mathematical data; no live configurations."""
from pathlib import Path
import ctypes
import errno
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]/'app'
sys.path[:0]=[str(APP/'launcher'),str(APP)]
import data_cleanup as dc


class ScalabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cleanup12-',dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve();self.events=[]
        self.s=dc.CleanupService(self.root,is_online=lambda:False,is_busy=lambda:False,progress=self.events.append)
        self.rel='game/0.16.8/terrain';self.base=self.root/self.rel

    def put(self,name='positions.f32',data=b'fixture',rel=None):
        p=self.root/(rel or self.rel)/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p

    def preview(self,**selection):
        result=self.s.preview(selection or {'terrain':True});self.assertTrue(result['ok'],result);return result

    def execute(self,**selection):
        p=self.preview(**selection);r=self.s.execute(p['plan_id'],True);self.assertTrue(r['ok'],r);return r

    def restore(self):
        p=self.s.restore_preview();self.assertTrue(p['ok'],p);r=self.s.restore(p['plan_id'],True);self.assertTrue(r['ok'],r);return r

    def exdev(self,source,target):
        real=os.rename
        def operation(a,b):
            if Path(a)==source and Path(b)==target:raise OSError(errno.EXDEV,'synthetic cross volume')
            return real(a,b)
        return patch.object(dc.os,'rename',side_effect=operation)

    def sparse(self,path,size):
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('wb') as stream:
            if os.name=='nt':
                import msvcrt
                api=ctypes.WinDLL('kernel32',use_last_error=True)
                ioctl=api.DeviceIoControl
                ioctl.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong),ctypes.c_void_p]
                ioctl.restype=ctypes.c_int;returned=ctypes.c_ulong()
                if not ioctl(msvcrt.get_osfhandle(stream.fileno()),0x900c4,None,0,None,0,ctypes.byref(returned),None):
                    self.fail('FSCTL_SET_SPARSE failed; refusing to allocate a giant fixture')
            stream.truncate(size)

    def test_real_sparse_two_gib_preview_move_restore_purge_never_reads_payload(self):
        p=self.base/'huge.f32';self.sparse(p,2*1024**3);real=Path.open;start=time.monotonic()
        def guarded(path,*args,**kwargs):
            if path.name=='huge.f32':raise AssertionError('large payload opened')
            return real(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):
            preview=self.preview();self.assertEqual(preview['total_bytes'],2*1024**3)
            result=self.s.execute(preview['plan_id'],True);self.assertTrue(result['ok'],result)
            self.restore();self.assertEqual(p.stat().st_size,2*1024**3)
            self.execute();pp=self.s.purge_preview();self.assertTrue(pp['ok'],pp)
            self.assertTrue(self.s.purge(pp['plan_id'],True)['ok'])
        print('cleanup12 sparse 2 GiB full cycle seconds',round(time.monotonic()-start,4))

    def test_legacy_v1_large_sha_is_not_reread(self):
        p=self.put('large.bin',b'0'*(dc.SCAN_LIMIT+1));r=self.execute();m=self.root/r['trash_path']/dc.MANIFEST
        doc=json.loads(m.read_bytes());doc.pop('fingerprint_version');doc['items'][0]['fingerprint'][1][4]='a'*64;m.write_text(json.dumps(doc),encoding='utf-8')
        real=Path.open
        def guarded(path,*a,**kw):
            if path.name=='large.bin':raise AssertionError('legacy full hash read')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.restore()
        self.assertEqual(p.stat().st_size,dc.SCAN_LIMIT+1)

    def test_large_metadata_change_rejected(self):
        p=self.put('large.bin',b'0'*(dc.SCAN_LIMIT+1));pre=self.preview();os.utime(p,ns=(1,1))
        self.assertEqual(self.s.execute(pre['plan_id'],True)['error_code'],'preview_stale')

    def test_large_same_metadata_tamper_is_explicitly_not_content_verified(self):
        p=self.put('large.bin',b'0'*(dc.SCAN_LIMIT+1));pre=self.preview();old=p.stat()
        with p.open('r+b') as f:f.write(b'1')
        os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
        self.assertTrue(self.s.execute(pre['plan_id'],True)['ok'])

    def test_generated_large_props_catalog_and_shard_metadata_only(self):
        for name in ('catalog.json','all/runtime/MathematicalBox.json'):
            self.put(name,b'{"vertices":['+b'0,'*(dc.SCAN_LIMIT//2)+b'0]}',rel='game/0.16.8/props')
        real=Path.open
        def guarded(path,*a,**kw):
            if path.name in ('catalog.json','MathematicalBox.json'):raise AssertionError('large JSON read')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.execute(props=True);self.restore()

    def test_unknown_large_json_fails_without_read(self):
        p=self.put('unknown.json',b' '*(dc.SCAN_LIMIT+1));real=Path.open
        def guarded(path,*a,**kw):
            if path==p:raise AssertionError('untrusted large text opened')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.assertEqual(self.s.preview({'terrain':True})['error_code'],'large_text_unverified')

    def test_large_known_text_still_rejected(self):
        self.put('build.txt',b' '*(dc.SCAN_LIMIT+1))
        self.assertEqual(self.s.preview({'terrain':True})['error_code'],'large_text_unverified')

    def test_large_mixed_registration_never_opened(self):
        p=self.root/dc.MIXED['profiles'];p.parent.mkdir(parents=True);p.write_bytes(b' '*(dc.SCAN_LIMIT+1));real=Path.open
        def guarded(path,*a,**kw):
            if path==p:raise AssertionError('private large text opened')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.assertEqual(self.s.preview({'captures':True})['error_code'],'json_too_large')

    def test_small_limit_sensitive_tail_retained(self):
        self.put('actor.bin',b' '*(dc.SCAN_LIMIT-22)+b'{"password":"fixture"}')
        self.assertEqual(self.s.preview({'terrain':True})['error_code'],'sensitive_data')

    def test_small_json_structural_secret_retained(self):
        self.put('manifest.json',b'{"metadata":{"accessToken":"fixture"}}')
        self.assertEqual(self.s.preview({'terrain':True})['error_code'],'sensitive_data')

    def test_large_hardlink_rejected_without_read(self):
        p=self.put('large.bin',b'0'*(dc.SCAN_LIMIT+1));os.link(p,self.root/'alias.bin')
        self.assertEqual(self.s.preview({'terrain':True})['error_code'],'unsafe_link')

    def test_1000_files_tree_lstat_linear(self):
        for n in range(1000):self.put(f'{n//100}/a{n}.bin',b'')
        real=Path.lstat;calls=[];start=time.monotonic()
        def count(path,*a,**kw):calls.append(path);return real(path,*a,**kw)
        with patch.object(Path,'lstat',count):rows=self.s._tree(self.rel)
        self.assertEqual(sum(r[1]=='file' for r in rows),1000);self.assertLess(len(calls),2200)
        print('cleanup12 1000 files inventory seconds',round(time.monotonic()-start,4),'lstat',len(calls))

    def test_same_volume_uses_rename_not_copy(self):
        self.put()
        with patch.object(self.s,'_copy_rows',side_effect=AssertionError('same volume copied')):self.execute();self.restore()

    def test_permission_denied_does_not_copy(self):
        self.put();pre=self.preview()
        with patch.object(dc.os,'rename',side_effect=PermissionError('fixture')),patch.object(self.s,'_copy_rows') as copy:
            r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'operation_rolled_back');copy.assert_not_called();self.assertTrue(self.base.exists())

    def test_exdev_copy_roundtrip_preserves_metadata_and_progress(self):
        p=self.put('large.bin',b'Z'*(2*dc.COPY_CHUNK+13));before=p.stat();real=os.rename
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        with patch.object(dc.os,'rename',side_effect=cross):r=self.execute()
        archived=self.root/r['trash_path']/self.rel
        with self.exdev(archived,self.base):self.restore()
        self.assertEqual(p.stat().st_mtime_ns,before.st_mtime_ns);self.assertEqual(p.read_bytes(),b'Z'*(2*dc.COPY_CHUNK+13))
        copies=[e for e in self.events if e['phase']=='copy'];self.assertGreater(len(copies),4)
        self.assertTrue(any(e['bytes_done']==2*dc.COPY_CHUNK+13 for e in copies))

    def test_exdev_small_file_root(self):
        rel=dc.RELEASE+'/multimap-catalog.json';p=self.root/rel;p.parent.mkdir(parents=True);p.write_text('{}', encoding="utf-8");real=os.rename
        def cross(a,b):
            if Path(a)==p:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        with patch.object(dc.os,'rename',side_effect=cross):self.execute()
        self.restore();self.assertEqual(p.read_text(encoding="utf-8"),'{}')

    def test_copy_failure_removes_only_partial_stage_and_keeps_source(self):
        self.put('a.bin',b'abc');pre=self.preview();real=os.rename
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        def fail(source,target,rows,**kw):target.mkdir();(target/'a.bin').write_bytes(b'a');raise OSError('synthetic copy failure')
        with patch.object(dc.os,'rename',side_effect=cross),patch.object(self.s,'_copy_rows',side_effect=fail):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'operation_rolled_back',r);self.assertEqual((self.base/'a.bin').read_bytes(),b'abc')
        self.assertFalse(list(self.root.rglob('.xg-transfer-*')))

    def test_interrupted_stage_resumes_after_restart(self):
        self.put('a.bin',b'abc');pre=self.preview();real=os.rename
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        def crash(source,target,rows,**kw):target.mkdir();(target/'a.bin').write_bytes(b'a');raise KeyboardInterrupt()
        with patch.object(dc.os,'rename',side_effect=cross),patch.object(self.s,'_copy_rows',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.s.execute(pre['plan_id'],True)
        self.s=dc.CleanupService(self.root,is_online=lambda:False,is_busy=lambda:False)
        self.assertEqual(self.s.purge_preview()['error_code'],'transfer_incomplete');self.restore()
        self.assertEqual((self.base/'a.bin').read_bytes(),b'abc');self.assertFalse(list(self.root.rglob('.xg-transfer-*')))

    def test_source_removal_interruption_recovers_missing_files(self):
        self.put('a.bin',b'aaa');self.put('b.bin',b'bbb');pre=self.preview();real=os.rename;remove=self.s._remove_rows
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        once=[]
        def fail(base,rows):
            if base==self.base and not once:
                once.append(True);(base/'a.bin').unlink();raise OSError('interrupted source removal')
            return remove(base,rows)
        with patch.object(dc.os,'rename',side_effect=cross),patch.object(self.s,'_remove_rows',side_effect=fail):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'operation_rolled_back',r)
        self.assertEqual((self.base/'a.bin').read_bytes(),b'aaa');self.assertEqual((self.base/'b.bin').read_bytes(),b'bbb')

    def test_callback_exception_does_not_abort(self):
        self.s.set_progress(lambda event:(_ for _ in ()).throw(ValueError('fixture')));self.put();self.execute();self.restore()

    def test_progress_shape_and_setter(self):
        self.put();self.execute();self.assertTrue(self.events)
        for event in self.events:self.assertEqual(set(event),{'phase','message','files_done','files_total','bytes_done','bytes_total'})
        count=len(self.events);self.s.set_progress(None);self.restore();self.assertEqual(len(self.events),count)

    def test_relative_aliases_rejected(self):
        for path in ('game//0.16.8/terrain','game/0.16.8./terrain','game/0.16.8/terrain/con.bin','game/0.16.8/terrain/x '):
            with self.assertRaises(dc.CleanupError):self.s._path(path)

    def test_journal_duplicate_fingerprint_alias_rejected(self):
        self.put();r=self.execute();p=self.root/r['trash_path']/dc.MANIFEST;doc=json.loads(p.read_bytes())
        doc['items'][0]['fingerprint'].append(doc['items'][0]['fingerprint'][1]);p.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.s.restore_preview()['error_code'],'untrusted_trash')

    def test_v1_small_hash_still_checked(self):
        self.put();r=self.execute();p=self.root/r['trash_path']/dc.MANIFEST;doc=json.loads(p.read_bytes());doc.pop('fingerprint_version')
        doc['items'][0]['fingerprint'][1][4]='a'*64;p.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.s.restore_preview()['error_code'],'trash_changed')

    def test_source_mutation_during_copy_preserved_and_not_claimed_success(self):
        self.put('a.bin',b'abc');pre=self.preview();real=os.rename;copy=self.s._copy_rows
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        def mutation(source,target,rows,**kw):copy(source,target,rows,**kw);(source/'a.bin').write_bytes(b'new')
        with patch.object(dc.os,'rename',side_effect=cross),patch.object(self.s,'_copy_rows',side_effect=mutation):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'rollback_incomplete');self.assertEqual((self.base/'a.bin').read_bytes(),b'new')

    def test_empty_generated_binary_does_not_open(self):
        p=self.put(data=b'');real=Path.open
        def guarded(path,*a,**kw):
            if path==p:raise AssertionError('empty file opened')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.preview()

    def test_restoring_crossvolume_copy_interrupted_then_restart(self):
        self.put('a.bin',b'abc');r=self.execute();archived=self.root/r['trash_path']/self.rel
        pre=self.s.restore_preview()
        def crash(source,target,rows,**kw):target.mkdir();(target/'a.bin').write_bytes(b'a');raise KeyboardInterrupt()
        with self.exdev(archived,self.base),patch.object(self.s,'_copy_rows',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.s.restore(pre['plan_id'],True)
        self.assertFalse(self.base.exists());self.assertTrue(archived.exists())
        self.s=dc.CleanupService(self.root,is_online=lambda:False,is_busy=lambda:False)
        self.restore();self.assertEqual((self.base/'a.bin').read_bytes(),b'abc')

    def test_partial_json_staging_can_be_recovered(self):
        self.put('manifest.json',b'{"fixture":[1,2,3]}');pre=self.preview();real=os.rename
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        def fail(source,target,rows,**kw):target.mkdir();(target/'manifest.json').write_bytes(b'{"fix');raise OSError('copy interrupted')
        with patch.object(dc.os,'rename',side_effect=cross),patch.object(self.s,'_copy_rows',side_effect=fail):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'operation_rolled_back',r)

    def test_copy_stage_unknown_file_is_preserved_and_blocks_recovery(self):
        self.put('a.bin',b'abc');pre=self.preview();real=os.rename
        def cross(a,b):
            if Path(a)==self.base:raise OSError(errno.EXDEV,'fixture')
            return real(a,b)
        def fail(source,target,rows,**kw):target.mkdir();(target/'unknown.bin').write_bytes(b'a');raise OSError('copy interrupted')
        with patch.object(dc.os,'rename',side_effect=cross),patch.object(self.s,'_copy_rows',side_effect=fail):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'rollback_incomplete',r);self.assertEqual(len(list(self.root.rglob('unknown.bin'))),1)

    def test_winerror17_alone_selects_crossvolume_copy(self):
        self.put();real=os.rename
        def cross(a,b):
            if Path(a)==self.base:
                error=OSError(errno.EACCES,'fixture');error.winerror=17;raise error
            return real(a,b)
        with patch.object(dc.os,'rename',side_effect=cross):self.execute()
        self.restore()

    def test_transfer_id_path_injection_is_rejected(self):
        self.put();r=self.execute();p=self.root/r['trash_path']/dc.MANIFEST;doc=json.loads(p.read_bytes())
        doc['items'][0]['transfer']={'direction':'out','id':'../outside'};p.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.s.restore_preview()['error_code'],'untrusted_trash')

    def test_journal_file_parent_alias_is_rejected(self):
        self.put();r=self.execute();p=self.root/r['trash_path']/dc.MANIFEST;doc=json.loads(p.read_bytes())
        row=doc['items'][0]['fingerprint'][1];row[0]='missing/positions.f32';p.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.s.restore_preview()['error_code'],'untrusted_trash')

    def test_legacy_binary_sensitive_key_spellings(self):
        for text in (b"{'api-key':'x'}",b"{'access-token':'x'}",b'"privatekey":"x"',b"'refresh_token': 'x'",b"'APIKEY':'x'"):
            with self.subTest(text=text):
                self.put('actor.bin',text)
                self.assertEqual(self.s.preview({'terrain':True})['error_code'],'sensitive_data')

    def test_same_volume_post_count_change_keeps_all_data(self):
        self.put('a.bin',b'abc');pre=self.preview();real=os.rename
        def changed(a,b):
            result=real(a,b)
            if Path(a)==self.base:(Path(b)/'extra.bin').write_bytes(b'new')
            return result
        with patch.object(dc.os,'rename',side_effect=changed):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'rollback_incomplete',r)
        self.assertEqual(r['cause'],'move_changed');self.assertEqual(len(list(self.root.rglob('a.bin'))),1)
        self.assertEqual(len(list(self.root.rglob('extra.bin'))),1)

    def test_same_volume_post_size_change_is_not_success(self):
        self.put('a.bin',b'abc');pre=self.preview();real=os.rename
        def changed(a,b):
            result=real(a,b)
            if Path(a)==self.base:(Path(b)/'a.bin').write_bytes(b'longer')
            return result
        with patch.object(dc.os,'rename',side_effect=changed):r=self.s.execute(pre['plan_id'],True)
        self.assertEqual(r['error_code'],'rollback_incomplete',r);self.assertEqual(r['cause'],'move_changed')
        self.assertEqual(next(self.root.rglob('a.bin')).read_bytes(),b'longer')

    def test_known_large_terrain_json_metadata_only_cycle(self):
        names=('assets/all-scenes-v1/MathematicalMap/static-world/manifest.json',
               'assets.work/bindings/scenes/MathematicalMap.json',
               'assets/beamo-world-all-v1/MathematicalMap/static-world/runtime-manifest.json')
        for name in names:self.put(name,b' '*(dc.SCAN_LIMIT+1))
        real=Path.open
        def guarded(path,*a,**kw):
            if path.name in ('manifest.json','runtime-manifest.json','MathematicalMap.json'):raise AssertionError('large known generated JSON opened')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.execute();self.restore()

    def test_large_json_rules_do_not_accept_arbitrary_manifest_or_scene_path(self):
        for rel in ('game/0.16.8/terrain/manifest.json','game/0.16.8/terrain/arbitrary/scenes/Map.json',
                    'game/0.16.8/props/other/catalog.json','game/0.16.8/catalog-inputs/nested/Map.json'):
            self.assertFalse(self.s._large_json_allowed(rel),rel)

    def test_large_scene_input_json_is_known_but_nested_other_text_is_not(self):
        self.put('SyntheticMap.json',b' '*(dc.SCAN_LIMIT+1),rel='game/0.16.8/catalog-inputs')
        self.execute(names=True);self.restore()

    def test_copy_state_check_is_time_bounded_and_forced_at_boundaries(self):
        self.put('large.bin',b'X'*(3*dc.COPY_CHUNK+1));rows=self.s._tree(self.rel);target=self.root/'copy-fixture'
        calls=[]
        with patch.object(self.s,'_state',side_effect=lambda:calls.append(1)),patch.object(dc.time,'monotonic',return_value=1):
            self.s._copy_rows(self.base,target,rows)
        self.assertEqual(len(calls),2)

    def test_known_large_pending_json_metadata_only_roundtrip(self):
        self.put('assets/all-scenes-v1/MathematicalMap/static-world/manifest.json.pending',b' '*(dc.SCAN_LIMIT+1))
        self.put('catalog.json.pending',b' '*(dc.SCAN_LIMIT+1),rel='game/0.16.8/props')
        real=Path.open
        def guarded(path,*a,**kw):
            if path.name.endswith('.json.pending'):raise AssertionError('large known pending JSON opened')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.execute(terrain=True,props=True);self.restore()

    def test_unknown_large_pending_json_does_not_expand_allowlist(self):
        p=self.put('unknown.json.pending',b' '*(dc.SCAN_LIMIT+1));real=Path.open
        def guarded(path,*a,**kw):
            if path==p:raise AssertionError('unknown pending payload opened')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):self.assertEqual(self.s.preview({'terrain':True})['error_code'],'large_text_unverified')
        self.assertFalse(self.s._large_json_allowed('game/0.16.8/props/catalog.json.pending.pending'))

    def test_large_apk_in_selected_generated_root_metadata_only_input_apk_untouched(self):
        p=self.put('cache.apk',b'Z'*(dc.SCAN_LIMIT+1));input_apk=self.put('base.apk',b'input-apk-fixture',rel='game/0.16.8/apk')
        real=Path.open
        def guarded(path,*a,**kw):
            if path.suffix=='.apk':raise AssertionError('APK payload opened by cleanup')
            return real(path,*a,**kw)
        with patch.object(Path,'open',guarded):
            self.execute();self.assertFalse(p.exists());self.assertTrue(input_apk.exists());self.restore()
        self.assertEqual(input_apk.read_bytes(),b'input-apk-fixture');self.assertEqual(p.stat().st_size,dc.SCAN_LIMIT+1)

    def test_apk_in_nonterrain_categories_rejected_without_open(self):
        for category in ('props','names','catalog','catalog-inputs'):
            with self.subTest(category=category):
                p=self.put('base.apk',b'Z'*(dc.SCAN_LIMIT+1),rel='game/0.16.8/'+category)
                real=Path.open
                def guarded(path,*a,**kw):
                    if path.suffix=='.apk':raise AssertionError('nonterrain APK payload opened')
                    return real(path,*a,**kw)
                selection={'names' if category=='catalog-inputs' else category:True}
                with patch.object(Path,'open',guarded):
                    result=self.s.preview(selection)
                self.assertEqual(result['error_code'],'unknown_generated_file',result)
                self.assertTrue(p.exists())

if __name__=='__main__':unittest.main()
