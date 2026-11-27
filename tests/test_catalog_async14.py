"""Async catalogue certification with real synthetic files/events; no real user data."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import ast,copy,hmac,http.client,json,os,types
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from pathlib import Path
import sys,tempfile,threading,time,unittest,uuid
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];APP=ROOT/'app'
sys.path[:0]=[str(APP/'escort-v31-server'),str(APP)]
from xg_gamedata import catalog_state as C
from qfsky.version_profile import CURRENT_BUILD,profile_for_build
PROFILE=profile_for_build(CURRENT_BUILD)
# Deterministic fixture identities; the test carries no fixed account identifier.
UUID=str(uuid.uuid5(uuid.NAMESPACE_OID,'offline-catalog-fixture/primary'))
OTHER=str(uuid.uuid5(uuid.NAMESPACE_OID,'offline-catalog-fixture/other'))

def write_json(path,value):
    path=Path(C.xg_paths.long_path(path,force=True))
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.pending')
    tmp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');os.replace(tmp,path)

def wait_idle(timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        with C._LOCK:
            if not C._INFLIGHT:return
        time.sleep(.002)
    raise AssertionError('background verification did not settle')

def settled(data,account='fixture-a',uuid=UUID,timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        row=C.certify(data,account,uuid,PROFILE)
        if row['state']!='checking':return row
        time.sleep(.002)
    raise AssertionError('catalogue remained checking')

class AsyncCatalogTests(unittest.TestCase):
    def setUp(self):
        wait_idle()
        with C._LOCK:C._CACHE.clear()
        self.tmp=tempfile.TemporaryDirectory(prefix='catalog-async14-',dir=ROOT/'tests')
        self.data=Path(self.tmp.name);self.xg=C._paths(self.data);self.events=[]
        self.folder=self.make('fixture-a')
    def tearDown(self):
        for event in self.events:event.set()
        wait_idle()
        with C._LOCK:C._CACHE.clear()
        if os.name=='nt':
            import shutil
            shutil.rmtree(C.xg_paths.long_path(self.tmp.name,force=True))
        self.tmp.cleanup()
    def make(self,account):
        folder=self.xg.ASSET_RELEASE/'asset-catalog-acct'/account
        write_json(folder/'manifest.json',{'apk_build':PROFILE['build'],'native_sha256':PROFILE['native_sha256']})
        return folder
    def slow(self,fail=None):
        started=threading.Event();release=threading.Event();self.events.append(release)
        def verify(request):
            self.assertFalse(C._LOCK._is_owned())
            started.set();self.assertTrue(release.wait(3))
            if fail:raise ValueError(fail)
            return {'maps':115,'variants':1}
        return verify,started,release
    def current_variant(self):
        value=json.loads((APP/'xg_gamedata/spawn_pack_0168.json').read_text(encoding='utf-8'))['variants']['SunsetEnd2']['qianniaocheng']
        from qfsky.map_long0167 import asset_catalog_v32c as policy
        row=copy.deepcopy(json.loads(policy._EXPECTED_VARIANTS_JSON)['507487826']['qianniaocheng']);row['actor_sha256']='a'*64
        manifest={'apk_build':PROFILE['build'],'native_sha256':PROFILE['native_sha256'],'variants':{'507487826':{'qianniaocheng':row}}}
        write_json(self.folder/'manifest.json',manifest)
        placement=Path(C.xg_paths.long_path(self.folder/'SunsetEnd2/variants/qianniaocheng/placement.json',force=True))
        placement.parent.mkdir(parents=True,exist_ok=True)
        placement.write_bytes(policy.canonical_json(value))
        return manifest,value

    def test_slow_verify_returns_checking_under_one_second(self):
        verify,started,release=self.slow()
        with mock.patch.object(C,'_runtime_check',side_effect=verify) as run:
            start=time.perf_counter();row=C.certify(self.data,'fixture-a',UUID,PROFILE);elapsed=time.perf_counter()-start
            self.assertLess(elapsed,1);self.assertFalse(row['certified']);self.assertEqual(row['code'],'catalog_checking')
            self.assertTrue(started.wait(1));self.assertFalse(release.is_set());release.set()
            self.assertTrue(settled(self.data)['certified']);self.assertEqual(run.call_count,1)

    def test_simultaneous_pollers_singleflight(self):
        verify,started,release=self.slow()
        with mock.patch.object(C,'_runtime_check',side_effect=verify) as run:
            with ThreadPoolExecutor(max_workers=12) as pool:
                rows=list(pool.map(lambda _:C.certify(self.data,'fixture-a',UUID,PROFILE),range(24)))
            self.assertTrue(all(r['state']=='checking' and not r['certified'] for r in rows));self.assertEqual(run.call_count,1)
            release.set();self.assertTrue(settled(self.data)['certified'])

    def test_success_cache_has_no_fifteen_second_expiry(self):
        with mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}) as run:
            self.assertTrue(settled(self.data)['certified'])
            with mock.patch.object(C.time,'monotonic',return_value=10**12):
                for _ in range(5):self.assertTrue(C.certify(self.data,'fixture-a',UUID,PROFILE)['certified'])
            self.assertEqual(run.call_count,1)

    def test_failure_cache_does_not_respawn_after_time_passes(self):
        with mock.patch.object(C,'_runtime_check',side_effect=ValueError('catalog_account_mismatch')) as run:
            self.assertEqual(settled(self.data)['code'],'catalog_account_mismatch')
            with mock.patch.object(C.time,'monotonic',return_value=10**12):
                for _ in range(5):self.assertEqual(C.certify(self.data,'fixture-a',UUID,PROFILE)['code'],'catalog_account_mismatch')
            self.assertEqual(run.call_count,1)

    def test_fingerprint_change_revalidates_failure(self):
        with mock.patch.object(C,'_runtime_check',side_effect=ValueError('catalog_self_check')) as run:
            self.assertFalse(settled(self.data)['certified']);self.assertEqual(run.call_count,1)
        (self.folder/'changed.bin').write_bytes(b'new synthetic output')
        with mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}) as run:
            self.assertTrue(settled(self.data)['certified']);self.assertEqual(run.call_count,1)

    def test_inflight_old_fingerprint_cannot_publish_over_new(self):
        verify,started,release=self.slow()
        with mock.patch.object(C,'_runtime_check',side_effect=verify) as run:
            C.certify(self.data,'fixture-a',UUID,PROFILE);self.assertTrue(started.wait(1))
            (self.folder/'changed.bin').write_bytes(b'changed while old job active')
            self.assertEqual(C.certify(self.data,'fixture-a',UUID,PROFILE)['state'],'checking')
            self.assertEqual(run.call_count,1);release.set();wait_idle()
            with C._LOCK:self.assertEqual(len(C._CACHE),0)
        with mock.patch.object(C,'_runtime_check',return_value={'maps':2,'variants':0}) as run:
            self.assertEqual(settled(self.data)['maps'],2);self.assertEqual(run.call_count,1)

    def test_unobserved_midflight_file_change_never_certified(self):
        def mutate(request):
            (self.folder/'changed.bin').write_bytes(b'changed')
            return {'maps':1,'variants':0}
        with mock.patch.object(C,'_runtime_check',side_effect=mutate):
            self.assertFalse(C.certify(self.data,'fixture-a',UUID,PROFILE)['certified']);wait_idle()
        with C._LOCK:rows=list(C._CACHE.values())
        self.assertTrue(rows);self.assertTrue(all(not r['certified'] for r in rows))
        self.assertIn('catalog_changed_during_validation',[r['code'] for r in rows])

    def test_global_slot_cap_no_unbounded_threads(self):
        for index in range(4):self.make('account-'+str(index))
        verify,started,release=self.slow()
        with mock.patch.object(C,'_runtime_check',side_effect=verify) as run:
            for index in range(4):self.assertEqual(C.certify(self.data,'account-'+str(index),UUID,PROFILE)['state'],'checking')
            self.assertEqual(run.call_count,C.MAX_ACTIVE_CHECKS)
            with C._LOCK:self.assertEqual(len(C._INFLIGHT),C.MAX_ACTIVE_CHECKS)
            release.set();wait_idle();self.assertTrue(settled(self.data,'account-3')['certified'])

    def test_cache_capacity_is_bounded_and_eviction_is_rechecked(self):
        with mock.patch.object(C,'MAX_CACHED_RESULTS',3),mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}) as run:
            for index in range(6):
                self.make('cache-'+str(index));self.assertTrue(settled(self.data,'cache-'+str(index))['certified'])
            with C._LOCK:self.assertEqual(len(C._CACHE),3)
            self.assertTrue(settled(self.data,'cache-0')['certified']);self.assertEqual(run.call_count,7)

    def test_thread_start_failure_releases_slot(self):
        with mock.patch.object(C.threading,'Thread',side_effect=RuntimeError('synthetic thread failure')):
            row=C.certify(self.data,'fixture-a',UUID,PROFILE)
        self.assertEqual(row['code'],'catalog_check_start_failed');self.assertFalse(row['certified'])
        with C._LOCK:self.assertEqual(len(C._INFLIGHT),0)
        self.make('other')
        with mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}):self.assertTrue(settled(self.data,'other')['certified'])

    def test_uuid_identity_prevents_reusing_other_account_result(self):
        def check(request):
            if request['uuid']!=UUID:raise ValueError('catalog_account_mismatch')
            return {'maps':1,'variants':0}
        with mock.patch.object(C,'_runtime_check',side_effect=check) as run:
            self.assertTrue(settled(self.data)['certified'])
            self.assertEqual(settled(self.data,uuid=OTHER)['code'],'catalog_account_mismatch')
            self.assertEqual(run.call_count,2)

    def test_cheap_stale_manifest_never_starts_runtime(self):
        manifest,_=self.current_variant();manifest['variants']['507487826']['qianniaocheng']['placement_sha256']='0'*64
        write_json(self.folder/'manifest.json',manifest)
        with mock.patch.object(C,'_runtime_check') as run:
            start=time.perf_counter();row=C.certify(self.data,'fixture-a',UUID,PROFILE)
            self.assertLess(time.perf_counter()-start,1);self.assertEqual(row['code'],C.QIANNIAOCHENG_STALE_CODE)
            self.assertFalse(row['certified']);run.assert_not_called()

    def test_cheap_actual_placement_sha_is_checked(self):
        _,placement=self.current_variant();placement['heading']=5
        write_json(self.folder/'SunsetEnd2/variants/qianniaocheng/placement.json',placement)
        with mock.patch.object(C,'_runtime_check') as run:
            self.assertEqual(C.certify(self.data,'fixture-a',UUID,PROFILE)['code'],C.QIANNIAOCHENG_STALE_CODE);run.assert_not_called()

    def test_matching_placement_does_not_certify_account_or_capture(self):
        self.current_variant()
        for code in ['catalog_account_mismatch','capture_version_stale']:
            with C._LOCK:C._CACHE.clear()
            with mock.patch.object(C,'_runtime_check',side_effect=ValueError(code)):
                row=settled(self.data)
            self.assertFalse(row['certified']);self.assertEqual(row['code'],code)

    def test_missing_policy_is_not_success(self):
        self.current_variant()
        from qfsky.map_long0167 import asset_catalog_v32c as policy
        with mock.patch.object(policy,'_EXPECTED_VARIANTS_JSON','{}'),mock.patch.object(C,'_runtime_check') as run:
            row=C.certify(self.data,'fixture-a',UUID,PROFILE)
        self.assertFalse(row['certified']);self.assertEqual(row['code'],'catalog_variant_policy_invalid');run.assert_not_called()

    def test_missing_account_and_old_build_keep_priority(self):
        with mock.patch.object(C,'_runtime_check') as run:
            self.assertEqual(C.certify(self.data,'','',PROFILE)['code'],'account_required')
            write_json(self.folder/'manifest.json',{'apk_build':0,'native_sha256':'0'*64})
            self.assertEqual(C.certify(self.data,'fixture-a',UUID,PROFILE)['code'],'catalog_version_stale');run.assert_not_called()

    def test_returned_cached_dict_cannot_mutate_certificate(self):
        with mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}):
            row=settled(self.data);row['certified']=False
            self.assertTrue(C.certify(self.data,'fixture-a',UUID,PROFILE)['certified'])

    def test_runtime_and_fingerprint_never_run_inside_metadata_lock(self):
        real=C._fingerprint
        def fingerprint(*args):
            self.assertFalse(C._LOCK._is_owned());return real(*args)
        with mock.patch.object(C,'_fingerprint',side_effect=fingerprint),mock.patch.object(C,'_runtime_check',side_effect=lambda req:(self.assertFalse(C._LOCK._is_owned()) or {'maps':1,'variants':0})):
            self.assertTrue(settled(self.data)['certified'])

    def test_actual_status_collect_returns_checking_while_worker_waits(self):
        from xg_gamedata import status
        verify,started,release=self.slow()
        with mock.patch.object(C,'_runtime_check',side_effect=verify):
            start=time.perf_counter();result=status.collect(self.data,account='fixture-a',account_uuid=UUID,profile=PROFILE)
            self.assertLess(time.perf_counter()-start,1)
            self.assertEqual(next(r for r in result['catalogs'] if r['account']=='fixture-a')['state'],'checking');self.assertFalse(result['done']['catalog'])
            release.set();self.assertTrue(settled(self.data)['certified'])

    def test_realistic_fingerprint_and_slow_verify_timing(self):
        for i in range(115):
            folder=self.folder/('SyntheticMap'+str(i));folder.mkdir()
            for name in ['actor.bin','scene.bin','cache.bin','placement.json','scene-provenance.json']:(folder/name).write_bytes(b'synthetic')
        for i in range(24):
            folder=self.xg.CURRENT_PROFILES/('synthetic-'+str(i));folder.mkdir(parents=True)
            for name in ['record.json','actor.bin','scene.bin','cache.bin']:(folder/name).write_bytes(b'synthetic')
        start=time.perf_counter();fp=C._fingerprint(self.xg,self.folder);fingerprint_seconds=time.perf_counter()-start
        self.assertGreaterEqual(len(fp),674);self.assertLess(fingerprint_seconds,1)
        verify,started,release=self.slow()
        with mock.patch.object(C,'_runtime_check',side_effect=verify):
            start=time.perf_counter();row=C.certify(self.data,'fixture-a',UUID,PROFILE);status_seconds=time.perf_counter()-start
            self.assertLess(status_seconds,1);self.assertEqual(row['state'],'checking')
            release.set();self.assertTrue(settled(self.data)['certified'])
        print('CATALOG_TIMING='+json.dumps({'files':len(fp),'fingerprint_seconds':fingerprint_seconds,'status_seconds':status_seconds}))

    def test_metadata_failures_cached_until_fingerprint_changes(self):
        for raw,code in [(b'{', 'catalog_runtime_invalid'), (b'[]','metadata_shape'),
                         (b'{"apk_build":0}', 'catalog_version_stale')]:
            with self.subTest(code=code):
                (self.folder/'manifest.json').write_bytes(raw)
                with mock.patch.object(C,'_json',wraps=C._json) as read, mock.patch.object(C,'_runtime_check') as run:
                    for _ in range(4):
                        row=C.certify(self.data,'fixture-a',UUID,PROFILE)
                        self.assertEqual(row['code'],code);self.assertFalse(row['certified'])
                    self.assertEqual(read.call_count,1);run.assert_not_called()
        write_json(self.folder/'manifest.json',{'apk_build':PROFILE['build'],'native_sha256':PROFILE['native_sha256']})
        with mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}):
            self.assertTrue(settled(self.data)['certified'])

    def test_manifest_replacement_during_precheck_does_not_poison_new_key(self):
        write_json(self.folder/'manifest.json',{'apk_build':0})
        real=C._json;changed=threading.Event()
        def read_then_replace(path,*args):
            result=real(path,*args)
            if Path(path)==self.folder/'manifest.json' and not changed.is_set():
                write_json(path,{'apk_build':PROFILE['build'],'native_sha256':PROFILE['native_sha256']})
                changed.set()
            return result
        with mock.patch.object(C,'_json',side_effect=read_then_replace),mock.patch.object(C,'_runtime_check') as run:
            row=C.certify(self.data,'fixture-a',UUID,PROFILE)
            self.assertEqual(row['state'],'checking');self.assertFalse(row['certified']);run.assert_not_called()
        with C._LOCK:self.assertEqual(len(C._CACHE),0)
        with mock.patch.object(C,'_runtime_check',return_value={'maps':1,'variants':0}) as run:
            self.assertTrue(settled(self.data)['certified']);self.assertEqual(run.call_count,1)

    def test_raw_placement_bytes_must_match_not_just_parsed_json(self):
        _,placement=self.current_variant()
        # Semantically identical but noncanonical bytes still violate the runtime pin.
        write_json(self.folder/'SunsetEnd2/variants/qianniaocheng/placement.json',placement)
        with mock.patch.object(C,'_runtime_check') as run:
            self.assertEqual(C.certify(self.data,'fixture-a',UUID,PROFILE)['code'],C.QIANNIAOCHENG_STALE_CODE)
            run.assert_not_called()

    def test_long_unicode_manifest_is_read_and_fingerprinted(self):
        folder=self.folder/'含空格 的目录'/('路径段'*30)/('b'*90)
        manifest=folder/'manifest.json'
        self.assertGreater(len(str(manifest)),260)
        write_json(manifest,{'revision':1})
        self.assertEqual(C._json(manifest),{'revision':1})
        before=C._fingerprint(self.xg,folder)
        self.assertIn(str(manifest),{row[0] for row in before})
        write_json(manifest,{'revision':222})
        self.assertNotEqual(C._fingerprint(self.xg,folder),before)

    def test_real_http_status_routes_remain_fast_while_catalogue_checks(self):
        # Execute actual Handler and wizard.catalog_status AST without importing any
        # account/config readers. Only their explicit external dependencies are fixtures.
        wizard_file=APP/'launcher/wizard.py';tree=ast.parse(wizard_file.read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='catalog_status')
        env={'xg_paths':types.SimpleNamespace(DATA=self.data),
             '_reset_doc':lambda:{},'_reset_steps':lambda *_:[],
             '_capture_inventory':lambda _: {'stale':[],'current':['synthetic']},
             '_accounts':lambda:[{'id':'fixture-a','user_uuid':UUID}]}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(wizard_file),'exec'),env)
        def catalog_status():return env['catalog_status']('fixture-a',{},PROFILE)
        api_file=APP/'bot/control_api.py';tree=ast.parse(api_file.read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Handler')
        api_lock=threading.Lock()
        env2={'BaseHTTPRequestHandler':BaseHTTPRequestHandler,'Path':Path,'time':time,'json':json,'hmac':hmac,
              'urlsplit':urlsplit,'parse_qs':parse_qs,'COOKIE':'xg_token','LOCK':api_lock,
              'wizard':types.SimpleNamespace(summary=lambda:{'catalog':catalog_status()}),
              'status':lambda:{'online':False,'fixture':True}}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(api_file),'exec'),env2)
        class Handler(env2['Handler']):
            server_token='synthetic-catalog-http-token'
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(route):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=1)
            start=time.perf_counter()
            try:
                conn.request('GET','/xiaoguang'+route,headers={'Cookie':'xg_token=synthetic-catalog-http-token'})
                response=conn.getresponse();body=json.loads(response.read())
                return response.status,body,time.perf_counter()-start
            finally:conn.close()
        verify,started,release=self.slow()
        def checking(req):
            self.assertTrue(api_lock.acquire(blocking=False),'HTTP operation lock held by verifier')
            api_lock.release();return verify(req)
        try:
            with mock.patch.object(C,'_runtime_check',side_effect=checking) as run:
                code,row,elapsed=request('/api/wizard')
                self.assertEqual(code,200);self.assertEqual(row['catalog']['state'],'checking');self.assertLess(elapsed,1)
                self.assertTrue(started.wait(1));self.assertFalse(release.is_set())
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results=list(pool.map(request,['/api/wizard','/status']*4))
                for code,row,elapsed in results:
                    self.assertEqual(code,200);self.assertLess(elapsed,1)
                    if 'catalog' in row:self.assertEqual(row['catalog']['state'],'checking');self.assertFalse(row['catalog']['certified'])
                    else:self.assertTrue(row['fixture'])
                self.assertFalse(release.is_set());self.assertEqual(run.call_count,1)
                release.set();self.assertTrue(settled(self.data)['certified'])
                code,row,elapsed=request('/api/wizard');self.assertEqual(code,200);self.assertTrue(row['catalog']['certified'])
                print('CATALOG_HTTP_TIMING='+json.dumps({'requests':10,'max_poll_seconds':max(r[2] for r in results)}))
        finally:
            release.set();server.shutdown();server.server_close();thread.join(3)

if __name__=='__main__':unittest.main(verbosity=2)
