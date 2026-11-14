"""Local APK-generation contracts. All unit fixtures are mathematical, never APK-derived."""
from pathlib import Path
import hashlib,json,os,sys,tempfile,unittest,zipfile
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'app'),str(ROOT/'app/src'),str(ROOT/'tests')]
from xg_gamedata import props as gen
from xg_gamedata.prop_build import extract,reviewed_geometry,rules,budget,budget_geometry_verify
from prop_synthetic_fixtures import profile

class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gen8-',dir=ROOT/'tests');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.apk=self.root/'input.apk';self.apk.write_bytes(b'not-an-apk')
        self.output=self.root/'game/props'
    def fake_build(self,apk,scratch,progress):
        shape=profile();shape['vertex_count']=8;shape['triangle_count']=12
        rel='all/runtime/SyntheticBox.json'
        digest=gen._write(scratch/rel,dict(schema='skyterrain-props-v1',coordinate_space='sky_native',props={'SyntheticBox':shape}))
        index=dict(schema='skyterrain-props-index-v1',coordinate_space='sky_native',counts={},props={
            'SyntheticBox':dict(status='ready',runtime_eligible=True,reasons=[],shard=rel,sha256=digest,
                byte_length=(scratch/rel).stat().st_size,vertex_count=8,triangle_count=12,source_kind='render_mesh_candidate'),
            'SyntheticUnknown':dict(status='no_geometry',runtime_eligible=False,reasons=['synthetic'],shard=None)})
        gen._write(scratch/'all/raw/not-for-publication.json',{'positions':[123,456,789]})
        return index,dict(sha256='a'*64,bytes=10)
    def make(self):
        with patch.object(gen,'_build',self.fake_build):return gen.generate(self.apk,self.output)
    def change_marker(self,**changes):
        p=self.output/'generation.json';m=json.loads(p.read_text(encoding='utf8'));m.update(changes);gen._write(p,m)
    def snapshot(self):return {p.relative_to(self.output).as_posix():p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
    def test_public_api_installed_and_dependencies_import(self):
        import xg_gamedata.prop_build.coverage_cleanup,xg_gamedata.prop_build.planar_simplification
        import xg_gamedata.prop_build.surface_simplification,xg_gamedata.prop_build.simplify_collision
        self.assertTrue(callable(gen.generate));self.assertTrue(callable(gen.validate_generated))
        self.assertEqual(sys.version_info[:2],(3,12))
    def test_rules_contain_no_geometry_arrays_or_private_inputs(self):
        text=json.dumps(rules.POLICY)
        for key in ('positions','triangles','bounds_min','source_positions','observed_placement'):
            self.assertNotIn('"'+key+'"',text)
        self.assertEqual(len(rules.BUDGET_RECIPES),14)
        for p in Path(extract.__file__).parent.glob('*.py'):
            self.assertNotIn('inputs/8-prop-extract',p.read_text(encoding='utf8'))
    def test_synthetic_full_pipeline_publishes_only_runtime(self):
        events=[]
        with patch.object(gen,'_build',self.fake_build):result=gen.generate(self.apk,self.output,progress=events.append)
        self.assertTrue(result['ok']);self.assertEqual(result['ready'],1);self.assertEqual(result['known'],2)
        self.assertEqual(set(self.snapshot()),{'catalog.json','generation.json','all/runtime/SyntheticBox.json'})
        self.assertEqual(events[-1]['stage'],'complete')
        self.assertFalse(list(self.output.parent.glob('.props-build-*')))
        self.assertFalse((self.output.parent/'props.generation.lock').exists())
    def test_validate_actual_runtime_loader(self):
        self.make();v=gen.validate_generated(self.output,gen.BUILD)
        self.assertTrue(v['ready'],v);self.assertEqual((v['count'],v['known']),(1,2))
    def test_missing_is_not_ready(self):self.assertFalse(gen.validate_generated(self.output)['ready'])
    def test_wrong_build_not_ready(self):
        self.make();self.assertFalse(gen.validate_generated(self.output,gen.BUILD-1)['ready'])
    def test_incomplete_marker_not_ready(self):
        self.make();self.change_marker(status='building');self.assertFalse(gen.validate_generated(self.output)['ready'])
    def test_catalog_hash_corruption_not_ready(self):
        self.make();p=self.output/'catalog.json';p.write_bytes(p.read_bytes()+b' ')
        self.assertFalse(gen.validate_generated(self.output)['ready'])
    def test_missing_shard_not_ready(self):
        self.make();(self.output/'all/runtime/SyntheticBox.json').unlink();self.assertFalse(gen.validate_generated(self.output)['ready'])
    def test_corrupt_shard_not_ready(self):
        self.make();(self.output/'all/runtime/SyntheticBox.json').write_bytes(b'{}');self.assertFalse(gen.validate_generated(self.output)['ready'])
    def test_count_mismatch_not_ready(self):
        self.make();self.change_marker(counts=dict(ready=174,known=522));v=gen.validate_generated(self.output)
        self.assertFalse(v['ready']);self.assertEqual(v['code'],'props_generated_counts_mismatch')
    def test_unknown_apk_preserves_previous(self):
        self.make();before=self.snapshot()
        with self.assertRaisesRegex(gen.PropGenerationError,'当前 APK'):gen.generate(self.apk,self.output)
        self.assertEqual(self.snapshot(),before)
        self.assertFalse(list(self.output.parent.glob('.props-build-*')))
    def test_build_exception_preserves_previous(self):
        self.make();before=self.snapshot()
        with patch.object(gen,'_build',side_effect=ValueError('synthetic failure')):
            with self.assertRaisesRegex(gen.PropGenerationError,'synthetic failure'):gen.generate(self.apk,self.output)
        self.assertEqual(self.snapshot(),before)
    def test_failed_validation_preserves_previous(self):
        self.make();before=self.snapshot()
        with patch.object(gen,'_build',self.fake_build),patch.object(gen,'validate_generated',return_value={'ready':False,'code':'injected-invalid'}):
            with self.assertRaisesRegex(gen.PropGenerationError,'injected-invalid'):gen.generate(self.apk,self.output)
        self.assertEqual(self.snapshot(),before)
    def test_promotion_failure_rolls_back_previous(self):
        self.make();before=self.snapshot();real_replace=gen.os.replace
        def fail(source,dest):
            if Path(source).name=='publish' and Path(dest)==self.output:raise OSError('promotion rejected')
            return real_replace(source,dest)
        with patch.object(gen,'_build',self.fake_build),patch.object(gen.os,'replace',side_effect=fail):
            with self.assertRaisesRegex(gen.PropGenerationError,'promotion rejected'):gen.generate(self.apk,self.output)
        self.assertEqual(self.snapshot(),before)
    def test_progress_failure_preserves_previous(self):
        self.make();before=self.snapshot()
        with patch.object(gen,'_build',self.fake_build):
            with self.assertRaisesRegex(gen.PropGenerationError,'callback'):
                gen.generate(self.apk,self.output,progress=lambda event:(_ for _ in ()).throw(ValueError('callback')))
        self.assertEqual(self.snapshot(),before)
    def test_unmanaged_directory_never_overwritten(self):
        self.output.mkdir(parents=True);(self.output/'personal.txt').write_bytes(b'preserve')
        with self.assertRaisesRegex(gen.PropGenerationError,'unmanaged'):gen.generate(self.apk,self.output)
        self.assertEqual((self.output/'personal.txt').read_bytes(),b'preserve')
    def test_managed_output_with_unrelated_file_not_replaced(self):
        self.make();note=self.output/'personal.txt';note.write_bytes(b'preserve')
        with self.assertRaisesRegex(gen.PropGenerationError,'unmanaged'):gen.generate(self.apk,self.output)
        self.assertEqual(note.read_bytes(),b'preserve')
    def test_directory_changed_while_building_not_replaced(self):
        self.make();before=self.snapshot()
        def build(apk,scratch,progress):
            result=self.fake_build(apk,scratch,progress)
            p=self.output/'generation.json';p.write_bytes(p.read_bytes()+b' ')
            return result
        with patch.object(gen,'_build',build):
            with self.assertRaisesRegex(gen.PropGenerationError,'output_changed'):gen.generate(self.apk,self.output)
        self.assertEqual((self.output/'generation.json').read_bytes(),before['generation.json']+b' ')
    def test_new_unmanaged_target_created_while_building_is_preserved(self):
        def build(apk,scratch,progress):
            result=self.fake_build(apk,scratch,progress)
            self.output.mkdir();(self.output/'personal.txt').write_bytes(b'keep')
            return result
        with patch.object(gen,'_build',build):
            with self.assertRaisesRegex(gen.PropGenerationError,'unmanaged'):gen.generate(self.apk,self.output)
        self.assertEqual((self.output/'personal.txt').read_bytes(),b'keep')
    def test_unknown_runtime_json_preserved(self):
        self.make();note=self.output/'all/runtime/Personal.json';note.write_bytes(b'not generated')
        with self.assertRaisesRegex(gen.PropGenerationError,'unmanaged'):gen.generate(self.apk,self.output)
        self.assertEqual(note.read_bytes(),b'not generated')
    def test_cleanup_failure_releases_lock_and_reports_warning(self):
        with patch.object(gen,'_build',self.fake_build),patch.object(gen,'_remove_tree',side_effect=OSError('locked staging')):
            result=gen.generate(self.apk,self.output)
        self.assertTrue(result['ok']);self.assertTrue(any('清理失败' in item for item in result['warnings']))
        self.assertFalse((self.output.parent/'props.generation.lock').exists())
        self.assertTrue(gen.validate_generated(self.output)['ready'])
    def test_old_backup_cleanup_failure_reports_committed_success(self):
        self.make();real_remove=gen._remove_tree
        def remove(path,parent):
            if '.previous-' in Path(path).name:raise OSError('old backup locked')
            return real_remove(path,parent)
        with patch.object(gen,'_build',self.fake_build),patch.object(gen,'_remove_tree',side_effect=remove):result=gen.generate(self.apk,self.output)
        self.assertTrue(result['ok']);self.assertTrue(any('备份清理失败' in item for item in result['warnings']))
        self.assertTrue(gen.validate_generated(self.output)['ready'])
        self.assertFalse((self.output.parent/'props.generation.lock').exists())
    def test_busy_lock_does_not_get_removed(self):
        self.output.parent.mkdir(parents=True);lock=self.output.parent/'props.generation.lock';lock.write_bytes(b'unknown owner')
        with self.assertRaisesRegex(gen.PropGenerationError,'busy'):gen.generate(self.apk,self.output)
        self.assertEqual(lock.read_bytes(),b'unknown owner')
    def test_stale_lock_recovers_without_signaling_pid(self):
        import psutil
        self.output.parent.mkdir(parents=True)
        stale=self.output.parent/'.props-build-stale';stale.mkdir();(stale/'scratch.json').write_bytes(b'private staging')
        lock=self.output.parent/'props.generation.lock'
        lock.write_text(json.dumps(dict(pid=543210,create_time=1.,workspace=stale.name)),encoding='utf8')
        real=psutil.Process
        def process(pid=None):
            if pid==543210:raise psutil.NoSuchProcess(pid)
            return real() if pid is None else real(pid)
        with patch.object(psutil,'Process',side_effect=process),patch.object(gen,'_build',self.fake_build):gen.generate(self.apk,self.output)
        self.assertFalse(stale.exists());self.assertFalse(lock.exists());self.assertTrue(gen.validate_generated(self.output)['ready'])
    def test_pid_reuse_releases_stale_lock(self):
        import psutil
        self.output.parent.mkdir(parents=True);lock=self.output.parent/'props.generation.lock'
        lock.write_text(json.dumps(dict(pid=543210,create_time=1.)),encoding='utf8')
        real=psutil.Process
        def process(pid=None):return SimpleNamespace(create_time=lambda:2.) if pid==543210 else real()
        with patch.object(psutil,'Process',side_effect=process),patch.object(gen,'_build',self.fake_build):gen.generate(self.apk,self.output)
        self.assertTrue(gen.validate_generated(self.output)['ready'])
    def test_access_denied_lock_owner_stays_busy(self):
        import psutil
        self.output.parent.mkdir(parents=True);lock=self.output.parent/'props.generation.lock'
        lock.write_text(json.dumps(dict(pid=543210,create_time=1.)),encoding='utf8')
        real=psutil.Process
        def process(pid=None):
            if pid==543210:raise psutil.AccessDenied(pid)
            return real()
        with patch.object(psutil,'Process',side_effect=process):
            with self.assertRaisesRegex(gen.PropGenerationError,'busy'):gen.generate(self.apk,self.output)
        self.assertTrue(lock.exists())
    def test_output_cannot_contain_source(self):
        with self.assertRaisesRegex(gen.PropGenerationError,'contains_input'):gen.generate(self.apk,self.root)
    def test_link_path_rejected_without_following(self):
        with patch.object(Path,'is_symlink',return_value=True):
            with self.assertRaisesRegex(gen.PropGenerationError,'link_rejected'):gen._local(self.output)
    def test_cleanup_scope_rejects_parent(self):
        with self.assertRaisesRegex(gen.PropGenerationError,'cleanup_scope'):gen._remove_tree(self.root,self.root)
    def test_local_default_uses_xg_paths(self):
        import xg_paths
        with patch.object(xg_paths,'PROP_ASSETS',self.output),patch.object(gen,'_build',self.fake_build):result=gen.generate(self.apk)
        self.assertEqual(Path(result['catalog']),self.output/'catalog.json')
    def test_apk_member_budget(self):
        zpath=self.root/'small.zip'
        with zipfile.ZipFile(zpath,'w') as z:z.writestr('empty',b'');z.writestr('valid',b'123')
        with zipfile.ZipFile(zpath) as z:
            self.assertEqual(extract.member(z,'valid'),b'123')
            with self.assertRaisesRegex(ValueError,'member_budget'):extract.member(z,'empty')
    def test_xyz_fallback_and_limits(self):
        np.testing.assert_array_equal(reviewed_geometry.scale_xyz({'scale':[2,0,3]}),[2,2,3])
        for bad in [[],[-1],[21],[float('nan')]]:
            with self.assertRaises(ValueError):reviewed_geometry.scale_xyz({'scale':bad})
    def test_definition_conflicts_not_guessed(self):
        group=[dict(definition=dict(mesh='Box',scale=[1],name='A')),dict(definition=dict(mesh='Box',scale=[1],name='B'))]
        self.assertFalse(extract.definition_equivalence(group)['geometry_conflict'])
        group[1]['definition']['scale']=[2];self.assertTrue(extract.definition_equivalence(group)['geometry_conflict'])
    def test_clean_geometry_preserves_triangle_winding(self):
        mesh=SimpleNamespace(animated=False,positions=np.array([[0,0,0],[0,0,1],[1,0,0]],dtype=np.float32),triangles=np.array([[0,1,2]]))
        p,t=extract.clean_geometry(mesh,2)
        np.testing.assert_array_equal(p[t],mesh.positions[mesh.triangles]*2)
    def test_animated_mesh_rejected(self):
        with self.assertRaisesRegex(ValueError,'animated'):extract.clean_geometry(SimpleNamespace(animated=True),1)
    def test_budget_synthetic_geometry_independently_verified(self):
        shape=profile();p,t,a=budget.reduce_geometry(shape['positions'],shape['triangles'],target=2048,max_error=.005)
        proof=budget_geometry_verify.verify_budget_geometry(a,shape['positions'],shape['triangles'])
        self.assertTrue(proof['verified']);self.assertLessEqual(proof['error'],.02);self.assertEqual(len(t),12)
    def test_budget_source_tamper_rejected(self):
        shape=profile();p,t,a=budget.reduce_geometry(shape['positions'],shape['triangles'],target=2048,max_error=.005)
        a['source_positions'][0]+=1
        with self.assertRaisesRegex(ValueError,'budget_source_changed'):
            budget_geometry_verify.verify_budget_geometry(a,shape['positions'],shape['triangles'])
    def test_budget_error_tamper_rejected(self):
        shape=profile();p,t,a=budget.reduce_geometry(shape['positions'],shape['triangles'],target=2048,max_error=.005)
        a['certified_combined_local_error']=.1
        with self.assertRaisesRegex(ValueError,'budget_error_changed'):
            budget_geometry_verify.verify_budget_geometry(a,shape['positions'],shape['triangles'])

if __name__=='__main__':unittest.main()
