"""Generated-data cleanup, synthetic fixtures only; never reads live profiles."""
from pathlib import Path
import hashlib
import json
import os
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]/'app'
# The desktop runner also exposes a top-level launcher.py. Import the service
# like the real control API, from app/launcher, rather than that shadowed name.
sys.path[:0]=[str(APP/'launcher'),str(APP)]
from data_cleanup import CleanupService,CleanupError,RELEASE,MIXED,MANIFEST,INDEX,_Spans
from qfsky.join0167_profile import account_binding


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cleanup-fixture-',dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.now=1000000000.
        self.online=False;self.busy=False
        self.service=CleanupService(self.root,is_online=lambda:self.online,is_busy=lambda:self.busy,clock=lambda:self.now)

    def put(self,rel,value=b'asset-fixture'):
        p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode())
        return p

    def terrain(self,version='0.16.8-421629'):
        return self.put('game/'+version+'/terrain/positions.f32')

    def preview(self,**selection):
        value=self.service.preview(selection)
        self.assertTrue(value['ok'],value)
        return value

    def execute(self,**selection):
        p=self.preview(**selection)
        result=self.service.execute(p['plan_id'],True)
        self.assertTrue(result['ok'],result)
        return result

    def restore(self):
        p=self.service.restore_preview();self.assertTrue(p['ok'],p)
        result=self.service.restore(p['plan_id'],True);self.assertTrue(result['ok'],result)
        return result

    def test_selection_defaults_captures_off(self):
        self.terrain();p=self.preview(terrain=True)
        self.assertEqual(p['selection'],{'terrain':True,'catalog':False,'names':False,'captures':False,'props':False})
        self.assertEqual(p['total_bytes'],len(b'asset-fixture'))

    def test_invalid_selection_rejected(self):
        for value in ({'../secrets':True},{'terrain':'yes'},{'terrain':1},[],{}):
            self.assertFalse(self.service.preview(value)['ok'])

    def test_root_must_be_absolute_and_scoped(self):
        for root in ('relative',Path(self.root.anchor)):
            with self.assertRaises(CleanupError):CleanupService(root,is_online=lambda:False,is_busy=lambda:False)

    def test_traversal_and_ads_rejected(self):
        for rel in ('../escape','/absolute','game\\bad','game/a:ads'):
            with self.assertRaises(CleanupError):self.service._path(rel)

    def test_junction_attribute_rejected(self):
        p=self.terrain();real=Path.lstat
        def fake(path,*args,**kwargs):
            value=real(path,*args,**kwargs)
            if path==p.parent:
                return SimpleNamespace(st_mode=value.st_mode,st_file_attributes=0x400,st_nlink=1)
            return value
        with patch.object(Path,'lstat',fake):
            self.assertEqual(self.service.preview({'terrain':True})['error_code'],'unsafe_link')

    def test_symlink_mode_rejected(self):
        p=self.terrain();real=Path.lstat
        def fake(path,*args,**kwargs):
            value=real(path,*args,**kwargs)
            if path==p.parent:return SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0,st_nlink=1)
            return value
        with patch.object(Path,'lstat',fake):
            self.assertEqual(self.service.preview({'terrain':True})['error_code'],'unsafe_link')

    def test_hardlinks_rejected(self):
        p=self.terrain();link=self.root/'other.bin'
        os.link(p,link)
        self.assertEqual(self.service.preview({'terrain':True})['error_code'],'unsafe_link')

    def test_secret_filename_refuses_entire_item(self):
        p=self.terrain();self.put('game/0.16.8-421629/terrain/secrets.json',{})
        self.assertEqual(self.service.preview({'terrain':True})['error_code'],'sensitive_data')
        self.assertTrue(p.exists())

    def test_unknown_file_refuses_entire_item(self):
        self.terrain();self.put('game/0.16.8-421629/terrain/unrelated.txt',b'not-generated')
        self.assertEqual(self.service.preview({'terrain':True})['error_code'],'unknown_generated_file')

    def test_embedded_secret_refuses_entire_item(self):
        self.put('game/0.16.8-421629/terrain/manifest.json',{'metadata':{'api_key':'fixture-key'}})
        result=self.service.preview({'terrain':True})
        self.assertEqual(result['error_code'],'sensitive_data')
        self.assertNotIn('fixture-key',json.dumps(result))

    def test_private_key_blob_rejected(self):
        self.put('game/0.16.8-421629/terrain/actor.bin',b'-----BEGIN '+b'RSA PRIVATE '+b'KEY-----fixture')
        self.assertEqual(self.service.preview({'terrain':True})['error_code'],'sensitive_data')

    def test_protected_roots_and_program_assets_untouched(self):
        protected=[self.put(p,{'token':'fixture'}) for p in ('config.json','runtime/runtime-data/accounts.json',
            'bot/memory.json','control/owner_uuid.json','app/assets/props/catalog.json','runtime/private/ai_secrets.json')]
        before={p:p.read_bytes() for p in protected};self.terrain()
        self.execute(terrain=True,catalog=True,names=True)
        self.assertEqual(before,{p:p.read_bytes() for p in protected})

    def test_all_game_versions_and_release_paths(self):
        self.terrain('0.16.7');self.terrain('0.16.8')
        self.put(RELEASE+'/assets/positions.f32');self.put(RELEASE+'/terrain/nav.bin')
        self.put(RELEASE+'/multimap-catalog.json',{})
        p=self.preview(terrain=True);self.assertEqual(len(p['items']),5)

    def test_names_and_catalog_inputs_only_selected(self):
        a=self.put('game/0.16.8/names/emotes.json',{});b=self.put('game/0.16.8/catalog-inputs/scene.bin')
        c=self.put('game/0.16.8/apk/base.apk',b'apk-fixture')
        self.execute(names=True);self.assertFalse(a.exists());self.assertFalse(b.exists());self.assertTrue(c.exists())

    def test_move_preserves_structure_and_selection(self):
        p=self.terrain();before=p.read_bytes();result=self.execute(terrain=True)
        moved=self.root/result['trash_path']/p.relative_to(self.root)
        self.assertFalse(p.exists());self.assertEqual(moved.read_bytes(),before)
        self.assertTrue(result['selection']['terrain'])

    def test_confirmation_required(self):
        p=self.terrain();preview=self.preview(terrain=True)
        for confirm in (False,'true',1,None):
            self.assertEqual(self.service.execute(preview['plan_id'],confirm)['error_code'],'confirmation_required')
        self.assertTrue(p.exists())

    def test_online_rechecked_at_execution(self):
        p=self.terrain();preview=self.preview(terrain=True);self.online=True
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'client_online')
        self.assertTrue(p.exists())

    def test_busy_rechecked_at_execution(self):
        p=self.terrain();preview=self.preview(terrain=True);self.busy=True
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'task_running')
        self.assertTrue(p.exists())

    def test_unknown_runtime_state_fails_closed(self):
        self.terrain();preview=self.preview(terrain=True)
        self.service.is_online=lambda:(_ for _ in ()).throw(RuntimeError('fixture'))
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'state_unknown')

    def test_expired_preview(self):
        self.terrain();preview=self.preview(terrain=True);self.now+=121
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'preview_expired')

    def test_size_mtime_and_content_stale(self):
        p=self.terrain();preview=self.preview(terrain=True)
        old=p.stat();p.write_bytes(b'other-fixture');os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'preview_stale')

    def test_new_subtree_file_invalidates_preview(self):
        self.terrain();preview=self.preview(terrain=True);self.put('game/0.16.8-421629/terrain/extra.bin')
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'preview_stale')

    def test_restore_roundtrip_after_service_restart(self):
        p=self.terrain();before=p.read_bytes();self.execute(terrain=True)
        self.service=CleanupService(self.root,is_online=lambda:False,is_busy=lambda:False,clock=lambda:self.now)
        result=self.restore();self.assertEqual(p.read_bytes(),before);self.assertTrue(result['selection']['terrain'])

    def test_restore_does_not_overwrite_new_data(self):
        p=self.terrain();self.execute(terrain=True);self.put(str(p.relative_to(self.root)),b'new-generated')
        preview=self.service.restore_preview();result=self.service.restore(preview['plan_id'],True)
        self.assertEqual(result['error_code'],'restore_conflict');self.assertEqual(p.read_bytes(),b'new-generated')

    def test_restore_requires_offline_and_confirmation(self):
        self.terrain();self.execute(terrain=True);p=self.service.restore_preview()
        self.assertEqual(self.service.restore(p['plan_id'])['error_code'],'confirmation_required')
        self.online=True;self.assertEqual(self.service.restore(p['plan_id'],True)['error_code'],'client_online')

    def test_trash_mutation_refuses_restore(self):
        p=self.terrain();result=self.execute(terrain=True)
        (self.root/result['trash_path']/p.relative_to(self.root)).write_bytes(b'changed')
        self.assertEqual(self.service.restore_preview()['error_code'],'trash_changed')

    def test_extra_trash_file_refuses_purge(self):
        self.terrain();result=self.execute(terrain=True)
        self.put(result['trash_path']+'/secrets.json',{'secret':'fixture'})
        self.assertEqual(self.service.purge_preview()['error_code'],'untrusted_trash')

    def test_purge_only_owned_batches_preserves_unknown_trash(self):
        unknown=self.put('trash/unrelated-user-folder/keep.txt',b'keep');self.terrain();result=self.execute(terrain=True)
        p=self.service.purge_preview();self.assertTrue(p['ok'],p)
        result=self.service.purge(p['plan_id'],True);self.assertTrue(result['ok'],result)
        self.assertTrue(unknown.exists());self.assertFalse((self.root/'trash'/result['purged'][0]).exists())

    def test_purge_requires_confirm_and_offline(self):
        self.terrain();self.execute(terrain=True);p=self.service.purge_preview()
        self.assertEqual(self.service.purge(p['plan_id'])['error_code'],'confirmation_required')
        self.busy=True;self.assertEqual(self.service.purge(p['plan_id'],True)['error_code'],'task_running')

    def test_purge_preview_rechecks_manifest(self):
        self.terrain();result=self.execute(terrain=True);p=self.service.purge_preview()
        manifest=self.root/result['trash_path']/MANIFEST
        value=json.loads(manifest.read_bytes());value['items'][0]['path']='../config.json';manifest.write_text(json.dumps(value))
        self.assertEqual(self.service.purge(p['plan_id'],True)['error_code'],'untrusted_trash')

    def test_partial_move_failure_rolls_back(self):
        a=self.terrain('0.16.7');b=self.terrain('0.16.8');preview=self.preview(terrain=True)
        original=os.rename;calls=[]
        def flaky(source,target):
            calls.append(str(source))
            if len(calls)==2:raise PermissionError('synthetic second rename failure')
            return original(source,target)
        with patch('data_cleanup.os.rename',side_effect=flaky):
            result=self.service.execute(preview['plan_id'],True)
        self.assertEqual(result['error_code'],'operation_rolled_back',result)
        self.assertIn('已完整回滚',result['message'])
        self.assertTrue(a.exists());self.assertTrue(b.exists())

    def test_incomplete_rollback_reports_bound_selection_and_recovery(self):
        self.terrain('0.16.7');self.terrain('0.16.8');preview=self.preview(terrain=True)
        original=os.rename;calls=[]
        def fail_move_and_rollback(source,target):
            calls.append(str(source))
            if len(calls)>=2:raise PermissionError('synthetic move and rollback interruption')
            return original(source,target)
        with patch('data_cleanup.os.rename',side_effect=fail_move_and_rollback):
            result=self.service.execute(preview['plan_id'],True)
        self.assertEqual(result['error_code'],'rollback_incomplete',result)
        self.assertEqual(result['selection'],{'terrain':True,'catalog':False,'names':False,'captures':False,'props':False})
        self.assertIn('回滚未完成',result['message']);self.assertIn('回收区',result['message'])

    def profiles(self):
        value={'token':'retain-secret-fixture','account':{'password':'retain-password-fixture'},
            'owner_uuid':'retain-owner','persona':'retain-persona','memory':['retain-chat'],
            'captures':{'old':{'record_dir':'fixture-record'}},
            'maps':{'3526133726':{'scene':'CandleSpace','current_capture':{'record_dir':'fixture-record'},
                               'captures':[{'record_dir':'older'}],'other':{'custom':'retain'}},
                    '10':{'scene':'Other','current_capture':{'record_dir':'record2'}}}}
        return self.put(MIXED['profiles'],value),value

    def test_captures_default_off_and_raw_capture_files_preserved(self):
        profiles,value=self.profiles();raw=profiles.read_bytes();self.terrain()
        capture=self.put('captures/raw.json',{'secret':'raw-fixture'})
        private=self.put('runtime/private/current_profiles/fixture/record.json',{'token':'private-fixture'})
        self.execute(terrain=True);self.assertEqual(profiles.read_bytes(),raw)
        self.execute(captures=True);self.assertTrue(capture.exists());self.assertTrue(private.exists())

    def test_mixed_profiles_only_selected_members_removed(self):
        path,before=self.profiles();self.execute(captures=True);after=json.loads(path.read_bytes())
        expected=json.loads(json.dumps(before));expected.pop('captures')
        for row in expected['maps'].values():row.pop('current_capture',None);row.pop('captures',None)
        self.assertEqual(after,expected)

    def test_no_surrounding_secrets_copied_to_trash_or_preview(self):
        path,before=self.profiles();preview=self.preview(captures=True)
        result=self.service.execute(preview['plan_id'],True);self.assertTrue(result['ok'],result)
        combined=json.dumps(preview).encode()+b''.join(p.read_bytes() for p in (self.root/'trash').rglob('*') if p.is_file())
        for value in (b'retain-secret-fixture',b'retain-password-fixture',b'retain-owner',b'retain-persona',b'retain-chat'):
            self.assertNotIn(value,combined)

    def test_restore_mixed_preserves_later_unselected_changes(self):
        path,before=self.profiles();self.execute(captures=True)
        doc=json.loads(path.read_bytes());doc['token']='new-login-token-fixture';doc['maps']['10']['new']='new-other'
        path.write_text(json.dumps(doc),encoding='utf-8');self.restore()
        expected=before;expected['token']='new-login-token-fixture';expected['maps']['10']['new']='new-other'
        self.assertEqual(json.loads(path.read_bytes()),expected)

    def test_new_capture_registration_refuses_restore(self):
        path,_=self.profiles();self.execute(captures=True)
        doc=json.loads(path.read_bytes());doc['maps']['10']['current_capture']={'record_dir':'new-record'}
        path.write_text(json.dumps(doc));preview=self.service.restore_preview()
        result=self.service.restore(preview['plan_id'],True)
        self.assertEqual(result['error_code'],'restore_conflict')
        self.assertEqual(json.loads(path.read_bytes())['maps']['10']['current_capture'],{'record_dir':'new-record'})

    def test_sensitive_field_inside_selected_registration_rejected(self):
        self.put(MIXED['profiles'],{'maps':{'1':{'current_capture':{'session3':'fixture-secret'}}}})
        self.assertEqual(self.service.preview({'captures':True})['error_code'],'sensitive_registration')

    def test_json_duplicate_keys_rejected(self):
        self.put(MIXED['profiles'],b'{"maps":{},"maps":{}}')
        self.assertEqual(self.service.preview({'captures':True})['error_code'],'ambiguous_json')

    def test_mixed_preview_stale_even_when_only_unselected_field_changes(self):
        p,before=self.profiles();preview=self.preview(captures=True);before['token']='changed';p.write_text(json.dumps(before))
        self.assertEqual(self.service.execute(preview['plan_id'],True)['error_code'],'preview_stale')

    def test_catalog_registry_matches_selected_manifest_binding_only(self):
        uid='11111111-1111-4111-8111-111111111111';binding=account_binding(uid)
        self.put(RELEASE+'/asset-catalog-acct/fixture/manifest.json',{'source_account_binding_hash':binding})
        sources=self.put(MIXED['sources'],{uid:{'record_dir':'fixture'},'metadata':{'token':'retain-secret'}})
        pins=self.put(MIXED['pins'],{binding:{'source_actor_sha256':'a'*64},'metadata':{'description':'retain'}})
        preview=self.preview(catalog=True)
        self.assertNotIn(uid,json.dumps(preview));self.assertNotIn(binding,json.dumps(preview))
        result=self.service.execute(preview['plan_id'],True);self.assertTrue(result['ok'],result)
        self.assertEqual(json.loads(sources.read_bytes()),{'metadata':{'token':'retain-secret'}})
        self.assertEqual(json.loads(pins.read_bytes()),{'metadata':{'description':'retain'}})
        self.restore();self.assertIn(uid,json.loads(sources.read_bytes()));self.assertIn(binding,json.loads(pins.read_bytes()))

    def test_unmatched_catalog_metadata_never_cleared(self):
        p=self.put(MIXED['sources'],{'unmatched':{'secret':'retain'}});raw=p.read_bytes()
        self.execute(catalog=True);self.assertEqual(p.read_bytes(),raw)

    def test_valid_orphan_catalog_registrations_cleared_and_restored(self):
        uid='11111111-1111-4111-8111-111111111111';binding=account_binding(uid)
        source={uid:{'record_dir':'fixture','netversion_u16':[1,2,3,4,5,6]}}
        pin={binding:{'source_actor_sha256':'a'*64,'source_metadata_sha256':'b'*64,'entries_sha256':'c'*64,'variants':{}}}
        s=self.put(MIXED['sources'],source);p=self.put(MIXED['pins'],pin)
        self.execute(catalog=True)
        self.assertEqual(json.loads(s.read_bytes()),{});self.assertEqual(json.loads(p.read_bytes()),{})
        self.restore();self.assertEqual(json.loads(s.read_bytes()),source);self.assertEqual(json.loads(p.read_bytes()),pin)

    def test_unknown_orphan_registration_is_not_guessed(self):
        self.put(MIXED['pins'],{'a'*64:{'unexpected':'retain'}})
        result=self.service.preview({'catalog':True})
        self.assertEqual(result['error_code'],'unknown_catalog_registration')

    def test_staging_backup_and_atomic_pending_are_eligible(self):
        a=self.put(RELEASE+'/asset-catalog-acct/.t01234567/manifest.json.pending',{})
        b=self.put(RELEASE+'/asset-catalog-acct/.b89abcdef/scene/actor.bin')
        self.execute(catalog=True);self.assertFalse(a.exists());self.assertFalse(b.exists())
        self.restore();self.assertTrue(a.exists());self.assertTrue(b.exists())

    def test_unknown_dot_directory_is_rejected(self):
        self.put(RELEASE+'/asset-catalog-acct/.unknown/actor.bin')
        self.assertEqual(self.service.preview({'catalog':True})['error_code'],'unknown_generated_path')

    def test_purge_failure_can_resume_and_cannot_claim_full_restore(self):
        self.terrain();self.put('game/0.16.8-421629/terrain/second.bin');self.execute(terrain=True)
        preview=self.service.purge_preview();real=Path.unlink;calls=[]
        def flaky(path,*args,**kwargs):
            if path.name in ('positions.f32','second.bin'):
                calls.append(path.name)
                if len(calls)==2:raise PermissionError('synthetic purge interruption')
            return real(path,*args,**kwargs)
        with patch.object(Path,'unlink',flaky):
            result=self.service.purge(preview['plan_id'],True)
        self.assertEqual(result['error_code'],'purge_partial',result)
        self.assertEqual(self.service.restore_preview()['error_code'],'permanently_deleted_partial')
        self.service=CleanupService(self.root,is_online=lambda:False,is_busy=lambda:False,clock=lambda:self.now)
        preview=self.service.purge_preview();self.assertTrue(preview['ok'],preview)
        result=self.service.purge(preview['plan_id'],True);self.assertTrue(result['ok'],result)

    def test_manifest_extra_sensitive_field_not_purged(self):
        self.terrain();result=self.execute(terrain=True)
        p=self.root/result['trash_path']/MANIFEST;doc=json.loads(p.read_bytes());doc['APIKey']='injected-fixture';p.write_text(json.dumps(doc))
        self.assertEqual(self.service.purge_preview()['error_code'],'untrusted_trash');self.assertTrue(p.exists())

    def test_index_extra_sensitive_field_not_purged(self):
        self.terrain();self.execute(terrain=True)
        p=self.root/'trash'/INDEX;doc=json.loads(p.read_bytes());doc['secret']='injected-fixture';p.write_text(json.dumps(doc))
        self.assertEqual(self.service.purge_preview()['error_code'],'untrusted_trash')

    def test_restore_partial_journal_resumes_without_overwrite(self):
        path,before=self.profiles();self.terrain();result=self.execute(terrain=True,captures=True)
        preview=self.service.restore_preview();real=self.service._save_batch;count=[]
        def flaky(doc):
            if any(i['state']=='restored' for i in doc['items']):
                count.append(1)
                if len(count)==1:raise PermissionError('synthetic restore journal failure')
            return real(doc)
        with patch.object(self.service,'_save_batch',side_effect=flaky):
            failed=self.service.restore(preview['plan_id'],True)
        self.assertFalse(failed['ok'])
        self.restore();self.assertEqual(json.loads(path.read_bytes()),before)

    def test_embedded_password_in_binary_refused(self):
        self.put('game/0.16.8/terrain/actor.bin',b'{"password":"synthetic"}')
        self.assertEqual(self.service.preview({'terrain':True})['error_code'],'sensitive_data')

    def test_nested_span_edits_preserve_untouched_tokens(self):
        raw=b'{ "token" : "keep\\u0021", "captures":{"a":1}, "maps": { "1": { "current_capture":{}, "keep" : [1,  2] } } }'
        result=_Spans(raw).remove([['captures'],['maps','1','current_capture']])
        self.assertIn(b'"token" : "keep\\u0021"',result);self.assertIn(b'"keep" : [1,  2]',result)
        self.assertEqual(json.loads(result),{'token':'keep!','maps':{'1':{'keep':[1,2]}}})

    def test_empty_restore_and_purge_are_explicit(self):
        self.assertEqual(self.service.restore_preview()['error_code'],'nothing_to_restore')
        self.assertEqual(self.service.purge_preview()['error_code'],'nothing_to_purge')

    def test_all_preview_items_match_strict_shared_ui_contract(self):
        self.terrain();self.profiles();first=self.preview(terrain=True,captures=True)
        self.assertTrue(self.service.execute(first['plan_id'],True)['ok'])
        for preview in (first,self.service.restore_preview(),self.service.purge_preview()):
            self.assertTrue(preview['ok'],preview)
            self.assertEqual(preview['plan_id'],preview['preview_id'])
            self.assertIsInstance(preview['expires_at'],(int,float))
            self.assertGreaterEqual(preview['total_bytes'],0)
            self.assertGreaterEqual(preview['total_files'],0)
            for item in preview['items']:
                self.assertTrue(all(isinstance(item[key],str) and item[key] for key in ('path','action','category')))
                self.assertIs(type(item['files']),int);self.assertGreaterEqual(item['files'],0)
                self.assertIs(type(item['bytes']),int);self.assertGreaterEqual(item['bytes'],0)


if __name__=='__main__':unittest.main()
