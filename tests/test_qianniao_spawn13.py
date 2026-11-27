"""Independent terrainfix.19 landing pins/privacy regressions; no live account data."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import re
import struct
import unittest

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
VERSION='1.0.5-desktop-ui.10.2-terrainfix.19'
SCENE='SunsetEnd2'
VARIANT='qianniaocheng'
MAP_ID=507487826
GEO=[311.25495800042734,123.33380126953125,-761.5790023206526]
WIRE=[311.25495800042734,123.83380126953125,-761.5790023206526]
ANNOTATION_SHA='41b507525fda0ef7066001d98bd54aad944185e946b2098d588e90c32506b9c3'
PLACEMENT_SHA='353ba870afe64b424caf3b6508d050fefdaa3fede8032a9ed6be9e6b0dd8f73a'
MAIN_TABLE_SHA={
 'spawn_pack.json':'93a3e4abb66f4444f3df9aa80423d60432258e1e5540b9d2e438811349ded299',
 'spawn_pack_0168.json':'31a8a454caea5f426ab60a30eb318fd2937faf113a6f1c3e5ffb7df92c82c891',
}
# Exact negative-regression markers. Only this one assignment is exempt from
# legacy-reference scanning; all other test lines remain covered.
_LEGACY_LITERALS=(
 '276.0450015730899','84.99315440566033','85.49315440566033','-739.3020689763309',
 '276.0450134277344','85.49315643310547','-739.3020629882812',
 '1cb24031f4d5132f5fe29913feadafda743527de5e628e13c76621705a6e1473',
 'db3539593747d63bac3aba5199f9092c549791aad51a55412ebeddf5bc018d73',
)

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def read_json(path):return json.loads(path.read_text(encoding='utf-8'))
def source_value(path,name):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    return ast.literal_eval(next(n.value for n in tree.body if isinstance(n,ast.Assign)
        and any(isinstance(k,ast.Name) and k.id==name for k in n.targets)))

def legacy_hits(relative,text):
    allowed=set()
    # The generation suite retains exactly two old hashes for rejection cases.
    permitted={'tests/test_qianniao_spawn13.py':{'_LEGACY_LITERALS'},
               'tests/test_spawn_generation13.py':{'OLD_PLACEMENT_SHA','OLD_ANNOTATION_SHA'}}
    if relative in permitted:
        for n in ast.parse(text).body:
            if isinstance(n,ast.Assign) and any(isinstance(k,ast.Name) and k.id in permitted[relative] for k in n.targets):
                allowed.update(range(n.lineno,n.end_lineno+1))
    return [(relative,no,token) for no,line in enumerate(text.splitlines(),1) for token in _LEGACY_LITERALS if token in line and no not in allowed]

class QianniaoSpawnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs={name:read_json(APP/'xg_gamedata'/name) for name in MAIN_TABLE_SHA}
        cls.current=cls.packs['spawn_pack_0168.json']['variants'][SCENE][VARIANT]

    def test_current_full_placement_matches_supplied_canonical_pin(self):
        self.assertEqual(digest(self.current),PLACEMENT_SHA)
        self.assertEqual(set(self.current),{'classification','feet_position','heading','map_id','native_default_spawn_reproduced','provenance','scene','wire_position','wire_position_float32'})

    def test_annotation_canonical_sha_uses_runtime_ascii_serialization(self):
        p=self.current['provenance'];self.assertEqual(digest(p['annotation']),ANNOTATION_SHA)
        self.assertEqual(p['annotation_sha256'],ANNOTATION_SHA)
        runtime=APP/'escort-v31-server/qfsky/map_long0167/asset_catalog_v32c.py'
        tree=ast.parse(runtime.read_text(encoding='utf-8'));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='canonical_json')
        ns={'json':json};exec(compile(ast.Module(body=[node],type_ignores=[]),str(runtime),'exec'),ns)
        self.assertEqual(ns['canonical_json'](p['annotation']),canonical(p['annotation']))

    def test_geometry_and_wire_offset_and_float32_exact(self):
        v=self.current;p=v['provenance'];ann=p['annotation']
        self.assertEqual(v['feet_position'],GEO);self.assertEqual(v['wire_position'],WIRE)
        self.assertEqual(ann['geometry_position'],GEO);self.assertEqual(ann['feet_position'],WIRE)
        self.assertEqual(ann['offset'],[0,0.5,0]);self.assertEqual(p['wire_origin_offset'],0.5)
        self.assertEqual(v['wire_position_float32'],list(struct.unpack('<3f',struct.pack('<3f',*WIRE))))
        self.assertEqual(v['map_id'],MAP_ID);self.assertEqual(v['scene'],SCENE)

    def test_static_failed_support_and_user_override_remain_truthful(self):
        p=self.current['provenance'];check=p['engineering_check']
        self.assertIs(check['ok'],False);self.assertEqual(check['reasons'],['footprint_missing_up_face_within_0.25'])
        self.assertIsNone(check['support']);self.assertEqual(len(check['support_samples']),5)
        self.assertTrue(all(row['hit'] is False for row in check['support_samples']))
        self.assertIs(p['user_override'],True)
        self.assertIs(p['annotation']['checks']['ok'],True)
        self.assertIs(p['annotation']['checks']['support'],True)

    def test_native_success_is_not_fabricated(self):
        v=self.current;p=v['provenance']
        self.assertEqual(v['classification'],'engineering_trial')
        self.assertIs(v['native_default_spawn_reproduced'],False)
        for key in ['native_collision_verified','native_executed','native_spawn_selector_reproduced']:
            self.assertIs(p[key],False)
        self.assertEqual(p['annotation']['source'],'desktop-annotation')
        self.assertGreaterEqual(p['portal_clearance']['minimum_body_aabb_separation'],0.5)

    def test_every_original_main_placement_is_unchanged(self):
        for name,pack in self.packs.items():
            with self.subTest(name=name):self.assertEqual(digest(pack['placements']),MAIN_TABLE_SHA[name])
        for pack in self.packs.values():
            self.assertEqual(pack['placements'][SCENE]['feet_position'],[8.189192771911621,137.4515838623047,-138.56072998046875])
            self.assertNotEqual(pack['placements'][SCENE]['feet_position'],GEO)

    def test_legacy_pack_retires_only_obsolete_qianniao_variant(self):
        legacy=self.packs['spawn_pack.json']
        self.assertNotIn(VARIANT,(legacy.get('variants') or {}).get(SCENE) or {})
        self.assertEqual(digest(legacy['placements']),MAIN_TABLE_SHA['spawn_pack.json'])

    def test_spawn_config_uses_variant_not_a_duplicate_coordinate(self):
        config=read_json(APP/'config/spawn_points.json')
        row=next(r for r in config['destinations'] if r['id']=='landmark:qianniaocheng')
        self.assertEqual(row['map_id'],MAP_ID);self.assertEqual(row['scene'],SCENE)
        self.assertEqual(row['variant'],VARIANT);self.assertIsNone(row['position'])

    def test_name_registry_matches_new_annotation_without_arrival_claim(self):
        data=source_value(APP/'xg_gamedata/name_data.py','DATA')
        row=next(r for r in data['DEST_LANDMARKS'] if r['id']=='landmark:qianniaocheng')
        self.assertEqual(row['sourcePosition'],WIRE);self.assertEqual(row['spawnVariant'],VARIANT)
        self.assertEqual(row['verification'],'unverified_landmark');self.assertIsNone(row['arrival'])
        p=next(p for p in row['provenance'] if p['kind']=='user_annotation_spawn_variant')
        self.assertEqual(p['annotation_sha256'],ANNOTATION_SHA)
        self.assertEqual(p['updated_at'],self.current['provenance']['annotation']['updated_at'])

    def test_current_travel_fixture_is_not_an_old_point_negative_fixture(self):
        registry=read_json(ROOT/'tests/fixtures_travel6/destination-records-v25.json')
        row=next(r for r in registry['records'] if r['id']=='landmark:qianniaocheng')
        self.assertEqual(row['sourcePosition'],WIRE)
        p=next(p for p in row['provenance'] if p['kind']=='user_annotation_spawn_variant')
        self.assertEqual(p['annotation_sha256'],ANNOTATION_SHA)

    def test_stale_name_source_position_cannot_become_executable_destination(self):
        import copy
        from dataclasses import dataclass
        row=copy.deepcopy(next(r for r in source_value(APP/'xg_gamedata/name_data.py','DATA')['DEST_LANDMARKS'] if r['id']=='landmark:qianniaocheng'))
        row['sourcePosition']=[float(_LEGACY_LITERALS[0]),float(_LEGACY_LITERALS[2]),float(_LEGACY_LITERALS[3])]
        source=(APP/'bot/destination_resolver_v25.py').read_text(encoding='utf-8');tree=ast.parse(source)
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DestinationResolver')
        ns={'copy':copy,'SCHEMA':'sky-destination-registry-v25','_key':lambda s:s,'RUNTIME_REQUIREMENTS':[],'Path':Path,'json':json,'hashlib':hashlib}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'destination_resolver_v25.py','exec'),ns)
        resolved=ns['DestinationResolver']({'schema':ns['SCHEMA'],'records':[row]}).resolve('千鸟城')
        self.assertEqual(resolved['status'],'unverified_landmark')
        self.assertIsNone(resolved['destination']['arrival'])
        travel=ast.parse((APP/'bot/travel_chat_v25.py').read_text(encoding='utf-8'))
        classes=[n for n in travel.body if isinstance(n,ast.ClassDef) and n.name in ('DestinationRef','RegistryResolver')]
        config=read_json(APP/'config/spawn_points.json')
        ns2={'dataclass':dataclass,'re':re,'Path':Path,'_read_json':lambda *a:config,'_loose':lambda s:s}
        exec(compile(ast.Module(body=classes,type_ignores=[]),'travel_chat_v25.py','exec'),ns2)
        ref=ns2['RegistryResolver']('synthetic-config')('千鸟城')
        self.assertTrue(ref.valid());self.assertIsNone(ref.position);self.assertEqual(ref.variant,VARIANT)
        receiver=ast.parse((APP/'bot/leader_travel_runtime.py').read_text(encoding='utf-8'))
        accept=next(n for n in ast.walk(receiver) if isinstance(n,ast.FunctionDef) and n.name=='accept')
        call=next(n for n in ast.walk(accept) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='Destination')
        actual=eval(compile(ast.Expression(body=call),'leader_travel_runtime.py::Destination','eval'),
            {'dest':resolved['destination'],'cmd':{'map_id':MAP_ID,'variant':VARIANT},'target':None,
             'Destination':lambda ident,map_id,position,**kwargs:{'id':ident,'map_id':map_id,'position':position,**kwargs}})
        self.assertIsNone(actual['position']);self.assertEqual(actual['variant'],VARIANT)
        self.assertEqual(actual['arrival'],'scene_spawn')
        self.assertNotIn('sourcePosition',[n.value for n in ast.walk(accept) if isinstance(n,ast.Constant) and isinstance(n.value,str)])

    def test_placement_contains_no_account_ids_secrets_or_machine_paths(self):
        p=self.current
        banned_keys={'uuid','account_uuid','user_uuid','owner_uuid','session','token','password','authorization','cookie','device_id'}
        machine=re.compile(r'(?:[A-Za-z]:[\\/]|/(?:root|home|Users)/|https?://|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')
        def walk(value):
            if isinstance(value,dict):
                self.assertFalse(banned_keys.intersection(k.casefold() for k in value))
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
            elif isinstance(value,str):self.assertIsNone(machine.search(value))
        walk(p)
        self.assertEqual(p['provenance']['source_member'],'assets/Data/Levels/SunsetEnd2/Objects.level.bin')
        self.assertNotIn('positions',p);self.assertNotIn('triangles',p)

    def test_no_legacy_point_or_annotation_in_managed_production_and_tests(self):
        findings=[]
        folders=[APP/name for name in ['bot','src','xg_gamedata','escort-v31-server','web','engine']]+[ROOT/'tests']
        files={APP/'config/spawn_points.json'}
        for folder in folders:
            files.update(p for p in folder.rglob('*') if p.is_file() and p.suffix in {'.py','.json','.mjs','.js','.html','.css'})
        for p in sorted(files):
            findings.extend(legacy_hits(p.relative_to(ROOT).as_posix(),p.read_bytes().decode('utf-8',errors='replace')))
        self.assertEqual(findings,[])

    def test_negative_scan_exception_does_not_skip_other_tests_or_lines(self):
        token=_LEGACY_LITERALS[-1]
        self.assertTrue(legacy_hits('tests/unrelated.py','value='+repr(token)))
        self.assertTrue(legacy_hits('app/bot/current.py','value='+repr(token)))
        own='_LEGACY_LITERALS=('+repr(token)+',)\nother='+repr(token)+'\n'
        self.assertEqual([x[1] for x in legacy_hits('tests/test_qianniao_spawn13.py',own)],[2])
        generation='OLD_ANNOTATION_SHA='+repr(token)+'\ncurrent='+repr(token)+'\n'
        self.assertEqual([x[1] for x in legacy_hits('tests/test_spawn_generation13.py',generation)],[2])

    def test_current_client_version13(self):
        doc=read_json(APP/'config/experimental-hop-trial.runtime.json')
        self.assertEqual(doc['clientRelease'],VERSION)
        self.assertIn(VERSION,(APP/'web/desktop.html').read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main(verbosity=2)
