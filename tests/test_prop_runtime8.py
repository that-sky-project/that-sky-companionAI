"""Release 8 local-only prop assets and independent cleanup; synthetic fixtures."""
from pathlib import Path
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
sys.path[:0]=[str(APP),str(APP/'src'),str(APP/'bot'),str(APP/'launcher')]
import xg_paths
from skyterrain import props
from skyterrain.dynamic_props import PlaceableBinding
import experimental_ground_entry as entry
import room_pose
from seat_tilt_runtime import require_local_prop_assets, SeatTiltControllerMixin
from data_cleanup import CleanupService, MANIFEST
from prop_synthetic_fixtures import write_catalog


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='prop-runtime8-',dir=ROOT/'tests')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.assets=self.root/'game/0.16.8-421629/props'
        self.tracker=room_pose.RoomTracker()
        self.calls=[]
        class Backend:
            def bind_placeables(inner,snapshot,revision,**kw):
                inner.binding=PlaceableBinding(snapshot,revision,**kw)
        self.backend=Backend()

    def bind(self,config=None):
        with patch.object(xg_paths,'PROP_ASSETS',self.assets),patch.object(xg_paths,'GAME_VERSION','0.16.8-421629'):
            entry._bind_placeable_catalog(self.backend,self.tracker,config or {},
                NS(room_generation=1,scene_hash='synthetic-map'),lambda event,**kw:self.calls.append((event,kw)))

    def generate(self):
        return write_catalog(self.assets,count=3)

    def marker(self,**change):
        p=self.assets/'generation.json';doc=json.loads(p.read_text(encoding='utf-8'));doc.update(change)
        p.write_text(json.dumps(doc),encoding='utf-8')

    def test_path_is_current_user_version_and_not_packaged(self):
        self.assertEqual(xg_paths.PROP_ASSETS,xg_paths.GAME_DIR/'props')
        with patch.object(xg_paths,'DATA',self.root):
            self.assertEqual(xg_paths.props_dir('0.16.7-421589'),self.root/'game/0.16.7-421589/props')
            for value in ('../elsewhere','C:/outside','/root','version/../../x','',None):
                if value is None:continue
                with self.assertRaises(ValueError):xg_paths.props_dir(value)

    def test_default_has_no_embedded_geometry_or_observation_identity(self):
        self.assertEqual(props.available_prop_names(allow_observed=True),())
        self.assertEqual(props.known_prop_names(),())
        self.assertEqual(props._DEFAULT_CATALOG['props'],{})
        self.assertNotIn('observed_placement',json.dumps(props._DEFAULT_CATALOG))

    def test_new_tracker_disables_all_prop_geometry_before_binding(self):
        self.assertFalse(self.tracker.props_assets_ready)
        self.assertEqual(self.tracker.placeables.geometry_names,frozenset())

    def test_missing_assets_preserve_static_runtime_and_log_once(self):
        for _ in range(20):self.bind()
        self.assertFalse(self.backend.binding.enabled)
        self.assertFalse(self.tracker.props_assets_ready)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn(str(self.root),json.dumps(self.calls))

    def test_missing_assets_leave_static_surface_and_emit_no_collider_rpc(self):
        self.bind();surface=object()
        self.assertIs(self.backend.binding.surface(surface),surface)
        transport=NS(request=lambda *a,**kw:self.fail('disabled geometry emitted RPC'))
        self.assertTrue(self.backend.binding.sync(transport,1))
        self.assertTrue(self.backend.binding.applied_current(1))

    def test_absent_catalog_never_uses_packaged_or_observed_fallback(self):
        self.bind({'allowObservedProps':True,'propCatalog':str(APP/'assets/props/catalog.json')})
        self.assertFalse(self.backend.binding.enabled)
        self.assertEqual(self.backend.binding.catalog['props'],{})

    def test_valid_generated_assets_enable_without_observed_fallback(self):
        self.generate();self.bind({'allowObservedProps':True})
        self.assertTrue(self.backend.binding.enabled)
        self.assertTrue(self.tracker.props_assets_ready)
        self.assertFalse(self.backend.binding.allow_observed)
        self.assertIn('TableXmas',self.tracker.placeables.geometry_names)

    def test_explicit_current_catalog_is_allowed(self):
        self.generate();self.bind({'propCatalog':str(self.assets/'catalog.json')})
        self.assertTrue(self.backend.binding.enabled)

    def test_other_game_version_catalog_does_not_enable(self):
        other=write_catalog(self.root/'game/0.16.7-421589/props',build=421589,count=3)
        self.bind({'propCatalog':str(other/'catalog.json')})
        self.assertFalse(self.backend.binding.enabled)

    def test_wrong_build_marker_rejected(self):
        self.generate();self.marker(build=421589)
        with self.assertRaisesRegex(ValueError,'build_mismatch'):
            props.load_generated_catalog(self.assets,expected_build=421629)
        self.bind();self.assertFalse(self.tracker.props_assets_ready)

    def test_incomplete_generation_rejected(self):
        self.generate();self.marker(status='building')
        with self.assertRaisesRegex(ValueError,'incomplete'):props.load_generated_catalog(self.assets)

    def test_missing_completion_marker_rejected(self):
        self.generate();(self.assets/'generation.json').unlink()
        with self.assertRaisesRegex(ValueError,'missing'):props.load_generated_catalog(self.assets)

    def test_generated_catalog_junction_is_rejected(self):
        self.generate()
        with patch.object(Path,'is_junction',lambda path:path==self.assets):
            with self.assertRaisesRegex(ValueError,'link_path'):props.load_generated_catalog(self.assets)

    def test_catalog_hash_and_bad_shard_are_rejected(self):
        self.generate();self.marker(catalog_sha256='0'*64)
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):props.load_generated_catalog(self.assets)
        self.generate();(self.assets/'runtime/TableXmas.json').write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'geometry_incomplete'):props.load_generated_catalog(self.assets)

    def test_completion_marker_size_duplicate_and_invalid_build_rejected(self):
        self.generate();marker=self.assets/'generation.json'
        marker.write_bytes(b' '*65537)
        with self.assertRaisesRegex(ValueError,'budget'):props.load_generated_catalog(self.assets)
        marker.write_text('{"schema":1,"schema":2}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'marker_invalid'):props.load_generated_catalog(self.assets)
        self.generate();self.marker(build=True)
        with self.assertRaisesRegex(ValueError,'incomplete'):props.load_generated_catalog(self.assets)

    def test_stale_ready_state_is_removed_when_generation_breaks(self):
        self.generate();self.bind();self.assertTrue(self.tracker.props_assets_ready)
        (self.assets/'generation.json').unlink();self.bind()
        self.assertFalse(self.tracker.props_assets_ready)
        self.assertFalse(self.backend.binding.enabled)
        self.assertEqual(self.tracker.placeables.geometry_names,frozenset())
        self.assertEqual(len(self.calls),2)

    def test_cold_float64_valid_profile_still_rejected_by_world_float32(self):
        self.generate();catalog=props.load_generated_catalog(self.assets)
        self.assertEqual(catalog['load_errors'],[])
        colliders,rejected=props.build_colliders([dict(id='synthetic',name='Float32Thin',
            position=[0,0,0],rotation=[0,0,0,1],scale=1)],catalog=catalog)
        self.assertEqual(colliders,[])
        self.assertEqual(rejected[0]['reason'],'prop_worker_float32_degenerate_triangle')

    def test_hot_queries_use_preloaded_memory_only(self):
        self.generate();catalog=props.load_generated_catalog(self.assets)
        with patch.object(Path,'open',side_effect=AssertionError('hot filesystem access')):
            colliders,rejected=props.build_colliders([dict(id='synthetic',name='TableXmas',
                position=[0,0,0],rotation=[0,0,0,1],scale=1)],catalog=catalog)
        self.assertEqual(len(colliders),1);self.assertEqual(rejected,[])

    def test_no_asset_prop_diagnostics_are_not_repeated(self):
        logs=[];self.tracker.placeable_log=lambda *args,**kwargs:logs.append((args,kwargs))
        for _ in range(50):self.tracker._placeable_notice([{'reason':'prop_no_geometry'},{'reason':'prop_unknown_name'}])
        self.assertEqual(logs,[])


class SeatAvailabilityTests(unittest.TestCase):
    def snapshot(self,ready=False,available=(),position=(0,0,0)):
        return dict(assets_ready=ready,available_names=available,props=[],
            unmodeled_props=[dict(id='synthetic-prop',name='SyntheticSeat',position=position)])

    def test_prop_seat_is_refused_without_assets(self):
        with self.assertRaisesRegex(ValueError,'seat_prop_assets_unavailable'):
            require_local_prop_assets(self.snapshot(),(0,0,0))

    def test_attachment_evidence_refused_even_if_instance_lost(self):
        with self.assertRaises(ValueError):
            require_local_prop_assets(dict(assets_ready=False),(0,0,0),observed={'attachment_kind':2})

    def test_unclassified_far_or_empty_seat_is_not_guessed_static(self):
        for snapshot in (self.snapshot(position=(50,0,0)),dict(assets_ready=False,props=[],unmodeled_props=[])):
            with self.assertRaisesRegex(ValueError,'seat_prop_assets_unavailable'):
                require_local_prop_assets(snapshot,(0,0,0))

    def test_loaded_geometry_allows_only_that_prop_name(self):
        require_local_prop_assets(self.snapshot(True,('SyntheticSeat',)),(0,0,0))
        with self.assertRaises(ValueError):require_local_prop_assets(self.snapshot(True,('DifferentSeat',)),(0,0,0))

    def test_existing_seat_binding_invalidated_when_assets_disabled(self):
        c=NS(tracker=NS(room_generation=1,lock=threading.Lock(),props_assets_ready=False,
                       placeables=NS(geometry_names=set())))
        state=dict(room_generation=1,prop=dict(name='SyntheticSeat'))
        self.assertFalse(SeatTiltControllerMixin._prop_seat_valid(c,state))

    def test_actual_prepare_refuses_missing_classification_then_restores_legacy_static(self):
        c=SeatTiltControllerMixin()
        c.tracker=NS(lock=threading.RLock(),ready=True,room_generation=1,confirmed_map_id=1,
            props_assets_ready=False,placeables=NS(props={},unmodeled_props={},geometry_names=set()))
        c._prop_seat_observation=lambda *a,**kw:dict(attachment_kind=2)
        with self.assertRaisesRegex(ValueError,'seat_prop_assets_unavailable'):
            c._prop_seat_prepare(1,((0.,0.,0.),0.))
        c._prop_seat_observation=lambda *a,**kw:None
        with self.assertRaisesRegex(ValueError,'seat_prop_assets_unavailable'):
            c._prop_seat_prepare(1,((0.,0.,0.),0.))
        c.tracker.props_assets_ready=True
        self.assertIsNone(c._prop_seat_prepare(1,((0.,0.,0.),0.)))

    def test_actual_prepare_unknown_far_rig_cannot_fall_back_to_normal_sit(self):
        c=SeatTiltControllerMixin()
        c.tracker=NS(lock=threading.RLock(),ready=True,room_generation=1,confirmed_map_id=1,
            props_assets_ready=False,placeables=NS(props={},unmodeled_props={
                'fixture':dict(id='fixture',name='SyntheticUnknownRig',position=[0.,0.,0.])},geometry_names=set()))
        c._prop_seat_observation=lambda *a,**kw:None
        with self.assertRaisesRegex(ValueError,'seat_prop_assets_unavailable'):
            c._prop_seat_prepare(7,((10.,0.,0.),0.,'synthetic'))

    def test_type_zero_attachment_is_not_a_verified_static_seat_type(self):
        c=SeatTiltControllerMixin()
        c.tracker=NS(lock=threading.RLock(),props_assets_ready=False)
        c._prop_seat_observation=lambda *a,**kw:dict(attachment_kind=0,seat=dict(seated=1,seat=7))
        with self.assertRaisesRegex(ValueError,'seat_prop_assets_unavailable'):
            c._prop_seat_prepare(7,((0.,0.,0.),0.,'synthetic'))


class PropsCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='prop-cleanup8-',dir=ROOT/'tests')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.online=False;self.busy=False
        self.service=CleanupService(self.root,is_online=lambda:self.online,is_busy=lambda:self.busy)

    def put(self,rel,value=b'synthetic'):
        p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value);return p

    def preview(self,**selection):
        result=self.service.preview(selection);self.assertTrue(result['ok'],result);return result

    def execute(self,**selection):
        p=self.preview(**selection);r=self.service.execute(p['plan_id'],True);self.assertTrue(r['ok'],r);return r

    def test_props_is_independent_and_off_by_default(self):
        self.put('game/0.16.8-421629/terrain/mesh.bin');asset=self.put('game/0.16.8-421629/props/runtime/mesh.json',b'{}')
        p=self.preview(terrain=True)
        self.assertFalse(p['selection']['props'])
        self.assertFalse(any(row['category']=='props' for row in p['items']))
        self.service.execute(p['plan_id'],True);self.assertTrue(asset.exists())

    def test_all_version_props_move_restore_and_selection(self):
        a=self.put('game/0.16.8-421629/props/runtime/a.json',b'{}')
        b=self.put('game/0.16.7-421589/props/catalog.json',b'{}')
        result=self.execute(props=True);self.assertTrue(result['selection']['props'])
        self.assertFalse(a.exists());self.assertFalse(b.exists())
        p=self.service.restore_preview();self.assertTrue(p['ok'],p)
        r=self.service.restore(p['plan_id'],True);self.assertTrue(r['ok'],r)
        self.assertTrue(r['selection']['props']);self.assertEqual(a.read_bytes(),b'{}');self.assertEqual(b.read_bytes(),b'{}')

    def test_props_only_does_not_touch_apk_login_ai_memory_or_other_assets(self):
        paths=['game/0.16.8-421629/apk/base.apk','game/0.16.8-421629/names/names.json',
               'game/0.16.8-421629/terrain/mesh.bin','runtime/runtime-data/accounts.json',
               'runtime/private/ai.json','control/owner_uuid','bot/memory.json','bot/persona.json']
        protected=[self.put(p) for p in paths]
        self.put('game/0.16.8-421629/props/catalog.json',b'{}');self.execute(props=True)
        self.assertTrue(all(p.read_bytes()==b'synthetic' for p in protected))

    def test_props_secret_or_unknown_file_refuses_whole_item(self):
        path=self.put('game/0.16.8-421629/props/catalog.json',b'{"api_key":"synthetic-secret"}')
        self.assertEqual(self.service.preview({'props':True})['error_code'],'sensitive_data')
        path.write_bytes(b'{}');self.put('game/0.16.8-421629/props/base.apk')
        self.assertEqual(self.service.preview({'props':True})['error_code'],'unknown_generated_file')

    def test_props_preview_fingerprint_and_offline_rechecked(self):
        p=self.put('game/0.16.8-421629/props/catalog.json',b'{}');plan=self.preview(props=True)
        p.write_bytes(b'{"changed":1}')
        self.assertEqual(self.service.execute(plan['plan_id'],True)['error_code'],'preview_stale')
        plan=self.preview(props=True);self.online=True
        self.assertEqual(self.service.execute(plan['plan_id'],True)['error_code'],'client_online')
        self.assertTrue(p.exists())

    def test_props_restore_conflict_and_purge(self):
        path=self.put('game/0.16.8-421629/props/catalog.json',b'{}');self.execute(props=True)
        self.put('game/0.16.8-421629/props/new.json',b'{}')
        restore=self.service.restore_preview();self.assertTrue(restore['ok'],restore)
        self.assertEqual(self.service.restore(restore['plan_id'],True)['error_code'],'restore_conflict')
        purge=self.service.purge_preview();self.assertTrue(purge['ok'],purge)
        result=self.service.purge(purge['plan_id'],True);self.assertTrue(result['ok'],result)
        self.assertFalse(path.exists());self.assertTrue((path.parent/'new.json').exists())

    def test_old_four_key_trash_restores_with_props_false(self):
        path=self.put('game/0.16.8-421629/terrain/mesh.bin');result=self.execute(terrain=True)
        manifest=self.root/'trash'/result['batch_id']/MANIFEST
        doc=json.loads(manifest.read_text(encoding='utf-8'));doc['selection'].pop('props')
        manifest.write_text(json.dumps(doc),encoding='utf-8')
        p=self.service.restore_preview();self.assertTrue(p['ok'],p)
        r=self.service.restore(p['plan_id'],True);self.assertTrue(r['ok'],r)
        self.assertFalse(r['selection']['props']);self.assertTrue(path.exists())

    def test_props_whitelist_does_not_accept_nested_escape_or_program_root(self):
        for rel in ('app/assets/props','game/0.16.8-421629/props/../../apk',
                    'game/0.16.8-421629/apk','runtime/private/props'):
            self.assertFalse(self.service._allowed(rel,'props'))


if __name__=='__main__':unittest.main()
