"""Patch 7: actual bundled Node/Rapier + pure-memory Python capsule tests.

All terrain here, including the mathematical box used as a table, is synthetic.
No APK geometry, credentials, network, game process or user data.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
for directory in (APP, APP/'src', APP/'bot'):
    if str(directory) not in sys.path: sys.path.insert(0,str(directory))
import xg_paths
from skyterrain import capsule, props
from skyterrain.ground import GroundSurface
from prop_synthetic_fixtures import inline_catalog


def floor():
    return {'positions':[-20,0,-20,-20,0,20,20,0,20,20,0,-20], 'indices':[0,1,2,0,2,3]}


def wall(x=0):
    return {'positions':[x,-5,-5,x,5,-5,x,5,5,x,-5,5], 'indices':[0,1,2,0,2,3]}


def combine(*parts):
    positions=[]; indices=[]
    for part in parts:
        offset=len(positions)//3
        positions.extend(part['positions']); indices.extend(i+offset for i in part['indices'])
    return {'positions':positions, 'indices':indices}


def terrain(mesh,offset=(0,0,0)):
    positions=[mesh['positions'][i:i+3] for i in range(0,len(mesh['positions']),3)]
    triangles=[mesh['indices'][i:i+3] for i in range(0,len(mesh['indices']),3)]
    surface=GroundSurface(positions,triangles)
    return types.SimpleNamespace(surface=surface,offset=offset,
        hits=lambda x,z:[(index,y+offset[1],ny) for index,y,ny in surface.hits(x-offset[0],z-offset[2])])


class RealNodeOverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='overlap7-node-',dir=ROOT)
        cls.addClassCleanup(cls.temp.cleanup)
        catalog=inline_catalog()
        colliders,rejected=props.build_colliders([{'id':'synthetic-table','name':'TableXmas',
            'position':[10,0,10],'rotation':[0,0,0,1],'scale':1}],catalog=catalog)
        if rejected: raise AssertionError(rejected)
        cls.cross_cases=[{'mesh':combine(floor(),wall()),'feet':[.1,0,0]},
                         {'mesh':floor(),'feet':[0,-.03,0]},
                         {'mesh':combine(floor(),wall()),'feet':[2,0,0]},
                         {'mesh':wall(),'feet':[-.12,1,0]}]
        input_file=Path(cls.temp.name)/'input.json'
        input_file.write_text(json.dumps({'table':colliders[0],'probes':cls.cross_cases}),encoding='utf-8')
        result=subprocess.run([str(ROOT/'runtime/node/node.exe'),str(ROOT/'tests/overlap_terrain7_scenarios.mjs'),
             (APP/'engine').as_uri()+'/',cls.temp.name,str(input_file)],
             capture_output=True,text=True,encoding='utf-8',timeout=180,
             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise AssertionError(result.stderr)
        cls.result=json.loads(result.stdout)

    def node(self,name):
        value=self.result[name]
        self.assertTrue(value['ok'],value.get('error'))
        return value

    def test_no_props_90_frames_exactly_match_before7(self):
        value=self.node('no_props_golden')
        before=json.loads((ROOT/'tests/fixtures_overlap7/no_props_motor_before7.json').read_text(encoding='utf-8'))
        self.assertEqual(value['frames'],before['frames'])
        self.assertEqual(value['audit'],before['audit'])
        self.assertEqual(len(value['frames']),90)

    def test_real_capsule_dimensions_raw_contacts_and_clear(self):
        value=self.node('capsule_clear_and_raw')
        self.assertEqual(value['raw']['coordinateFrame'],'world_feet')
        self.assertFalse(value['raw']['collisionClearanceVerified'])
        self.assertTrue(value['clear']['collisionClearanceVerified'])

    def test_real_monotone_bounded_exit_cannot_cross_thin_wall(self):
        value=self.node('bounded_monotone_exit')
        self.assertGreater(value['samples'],0)
        self.assertGreaterEqual(value['probe']['adjustedStart'][0],.25)
        self.assertLessEqual(value['probe']['deltaLength'],.5)
        self.assertTrue(value['probe']['proof']['clearedFacesCannotReenter'])

    def test_real_deep_overlap_and_new_obstacle_reject(self):
        value=self.node('new_obstacle_and_deep_reject')
        self.assertIsNone(value['corridor']['adjustedStart'])
        self.assertIsNone(value['deep']['adjustedStart'])

    def test_real_candidate_and_contact_limits(self):
        value=self.node('candidate_and_contact_budgets')
        self.assertEqual(value['candidate']['reason'],'initial_contact_candidate_budget')
        self.assertEqual(value['contactCount'],65)

    def test_real_shallow_floor_overlap_exits_upward(self):
        value=self.node('shallow_floor_and_ceiling')['foot']
        self.assertGreater(value['deltaY'],0)
        self.assertLessEqual(value['deltaLength'],.03)

    def test_real_shallow_head_overlap_exits_sideways_not_deeper(self):
        value=self.node('shallow_floor_and_ceiling')['head']
        self.assertLessEqual(value['deltaLength'],.5)
        self.assertGreater(math.hypot(value['delta'][0],value['delta'][2]),.1)

    def test_real_continuous_speed_bounded_prefix_then_ground_motor(self):
        value=self.node('continuous_prefix')
        self.assertEqual(value['frames'][0]['position'],[.1,0,0])
        self.assertTrue(value['frames'][-1]['arrived'])
        self.assertTrue(value['audit']['initialContactExit'])

    def test_real_prefix_cancel_and_clock_gap_preserve_committed_position(self):
        value=self.node('prefix_cancel_clock_gap')
        self.assertEqual(value['gap']['reason'],'clock_gap')
        self.assertTrue(value['gap']['blocked'])

    def test_real_worker_probe_does_not_evict_lru_or_consume_tokens(self):
        value=self.node('worker_probe_isolation_and_props')
        self.assertEqual(value['framesCompared'],70)
        self.assertIn('capsule_probe_resolve',value['invalid'])
        self.assertEqual(value['load'],'ready')

    def test_real_worker_capsule_uses_merged_props_and_retains_invalidation(self):
        value=self.node('worker_probe_isolation_and_props')
        self.assertEqual(value['tableTriangles'],12)
        self.assertTrue(value['changed']['motionInvalidated'])
        self.assertGreater(len(value['blocked']['capsule']['contacts']),0)
        self.assertEqual(value['clear']['capsule']['contacts'],[])

    def test_python_capsule_agrees_with_actual_rapier_contact_depth(self):
        value=self.node('cross_language_contacts')
        for case,actual in zip(self.cross_cases,value['probes']):
            with self.subTest(feet=case['feet']):
                expected=capsule.CapsuleProbe(terrain(case['mesh'])).contacts(case['feet'])
                py={hit['triangle_id']:hit['depth'] for hit in expected}
                node={hit['id']:-hit['distance'] for hit in actual['contacts']}
                self.assertEqual(set(py),set(node))
                for face in py:self.assertAlmostEqual(py[face],node[face],delta=3e-5)


class PythonCapsuleTests(unittest.TestCase):
    def test_full_capsule_rejects_side_wall_even_with_support(self):
        view=terrain(combine(floor(),wall())); probe=capsule.CapsuleProbe(view)
        self.assertTrue(view.hits(.1,0))
        self.assertFalse(probe.standable((.1,0,0)))
        self.assertTrue(probe.standable((2,0,0)))

    def test_actor_offset_is_applied_exactly_once(self):
        mesh=combine(floor(),wall())
        base=capsule.CapsuleProbe(terrain(mesh)).contacts((.1,0,0))
        shifted=capsule.CapsuleProbe(terrain(mesh,(10,.5,20))).contacts((10.1,.5,20))
        self.assertEqual([r['triangle_id'] for r in base],[r['triangle_id'] for r in shifted])
        for a,b in zip(base,shifted):
            self.assertAlmostEqual(a['depth'],b['depth'])
            for k,d in enumerate((10,.5,20)):self.assertAlmostEqual(b['point'][k]-a['point'][k],d)

    def test_python_sweep_detects_thin_wall_not_just_endpoints(self):
        probe=capsule.CapsuleProbe(terrain(wall()))
        self.assertEqual(probe.intersecting((-1,0,0)),set())
        self.assertEqual(probe.intersecting((1,0,0)),set())
        self.assertTrue(probe.sweep((-1,0,0),(1,0,0)))

    def test_python_resolve_clear_or_bounded_exit(self):
        probe=capsule.CapsuleProbe(terrain(combine(floor(),wall())))
        clear=probe.resolve((2,0,0)); self.assertEqual(clear['distance'],0)
        resolved=probe.resolve((.1,0,0)); self.assertIsNotNone(resolved)
        self.assertLessEqual(resolved['distance'],.5)
        self.assertEqual(probe.intersecting(resolved['point']),set())
        self.assertFalse(probe.sweep((.1,0,0),resolved['point'])-set(resolved['initial_faces']))

    def test_python_resolve_candidate_is_not_permission_actual_proof_rejects(self):
        from stuck_recovery import validate_static_takeoff
        view=terrain(combine(floor(),wall(-.1),wall(.1)))
        probe=capsule.CapsuleProbe(view)
        # resolve enumerates candidates, not permission to execute a route.
        # Its candidate may pass through an initial face; the real mandatory
        # takeoff proof must reject that inward trajectory before any action.
        candidate=probe.resolve((0,0,0))
        self.assertIsNotNone(candidate)
        self.assertAlmostEqual(candidate['point'][0],.35)
        phases=[{'phase':'climb','mode':'linear','duration':.35,
                 'from':(0,0,0),'to':candidate['point']}]
        proof,reason=validate_static_takeoff(view,phases)
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_deeper_penetration')

    def test_input_and_sweep_distance_budgets(self):
        probe=capsule.CapsuleProbe(terrain(floor()))
        for point in ((math.nan,0,0),(math.inf,0,0),(1e6+1,0,0),(0,0)):
            with self.subTest(point=point),self.assertRaises(ValueError):probe.contacts(point)
        for step in (0,.001,.026,math.nan):
            with self.subTest(step=step),self.assertRaises(ValueError):probe.sweep((0,0,0),(1,0,0),step=step)
        with self.assertRaises(ValueError):probe.sweep((0,0,0),(257,0,0))
        for limit in (-.1,.51,math.inf):
            with self.subTest(limit=limit),self.assertRaises(ValueError):probe.resolve((0,0,0),max_distance=limit)

    def test_memory_only_cache_is_bounded_and_geometry_local(self):
        probe=capsule.CapsuleProbe(terrain(combine(floor(),wall())))
        with mock.patch.object(Path,'open',side_effect=AssertionError('hot capsule disk read')):
            for i in range(180):probe.contacts((i*.01,0,0))
        self.assertEqual(len(probe._cache),128)
        other=capsule.CapsuleProbe(terrain(floor()))
        self.assertTrue(probe.intersecting((.1,0,0)))
        self.assertEqual(other.intersecting((.1,0,0)),set())

    def test_no_triangle_surface_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'capsule_triangle_geometry_required'):
            capsule.CapsuleProbe(types.SimpleNamespace())


if __name__=='__main__':unittest.main(verbosity=2)
