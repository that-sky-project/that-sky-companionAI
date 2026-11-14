"""Public release data/privacy gates with synthetic failure fixtures only."""
from pathlib import Path
import ast
import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_scan8',ROOT/'tools/scan_release.py')
scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(scanner)
sys.path[:0]=[str(ROOT/'app'),str(ROOT/'app/bot')]
import textcheck

class ReleasePrivacyTests(unittest.TestCase):
    def test_public_tree_has_no_red_lines_or_generated_assets(self):
        report=scanner.inspect_tree(ROOT)
        self.assertTrue(report['ok'],report['findings'])
        self.assertGreater(report['files_scanned'],500)

    def test_apk_and_reference_payloads_absent(self):
        self.assertFalse((ROOT/'app/assets/props').exists())
        self.assertFalse((ROOT/'app/ref/textrules').exists())
        for sub in ('app','tests','tools'):
            self.assertFalse(list((ROOT/sub).rglob('*.apk')))
            self.assertFalse(list((ROOT/sub).rglob('reference-catalog.json')))

    def run_fixture(self,rel,content):
        with tempfile.TemporaryDirectory(prefix='release8-scan-') as tmp:
            p=Path(tmp)/rel;p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(content if isinstance(content,bytes) else content.encode())
            return scanner.inspect_tree(tmp)

    def test_developer_home_path_detected_without_echoing_value(self):
        value='C:'+'\\'+'Us'+'ers'+'\\'+'private-fixture'+'\\'+'code.py'
        report=self.run_fixture('app/example.py',value)
        self.assertEqual(report['findings'][0]['kind'],'developer_home_path')
        self.assertNotIn(value,json.dumps(report))

    def test_private_server_path_detected(self):
        report=self.run_fixture('tools/example.py','/'+'root/'+'clau'+'dework'+'/fixture')
        self.assertIn('private_server_path',[x['kind'] for x in report['findings']])

    def test_known_player_prefix_text_and_binary_detected(self):
        prefix=scanner.PLAYER_PREFIXES[0]
        report=self.run_fixture('tests/example.json',json.dumps({'id':prefix+'0'*24}))
        self.assertIn('player_identifier',[x['kind'] for x in report['findings']])
        report=self.run_fixture('app/renamed.dat',bytes.fromhex(prefix)+b'\x80payload')
        self.assertIn('player_identifier_binary',[x['kind'] for x in report['findings']])

    def test_secret_shapes_detected_without_secret_report_content(self):
        value='s'+'k-'+('syntheticfixture123456789'*2)
        report=self.run_fixture('app/example.py',repr(value))
        self.assertIn('secret_shape',[x['kind'] for x in report['findings']])
        self.assertNotIn(value,json.dumps(report))

    def test_first_party_email_and_unreviewed_github_identity_detected(self):
        report=self.run_fixture('app/example.py','fixture'+'@'+'example.test')
        self.assertIn('email_address',[x['kind'] for x in report['findings']])
        value='https:'+'//github.com/'+'private-fixture-identity'+'/repo'
        report=self.run_fixture('app/example.py',value)
        self.assertIn('unreviewed_github_identity',[x['kind'] for x in report['findings']])

    def test_vendor_public_attribution_does_not_exempt_private_paths(self):
        rel='app/engine/node_modules/fixture/LICENSE'
        report=self.run_fixture(rel,'public-author'+'@'+'example.test')
        self.assertTrue(report['ok'])
        self.assertEqual(report['public_third_party_attribution_occurrences'],1)
        report=self.run_fixture(rel,'/'+'root/'+'clau'+'dework'+'/fixture')
        self.assertFalse(report['ok'])

    def test_mesh_arrays_and_renamed_binary_rejected(self):
        report=self.run_fixture('app/innocent.json',json.dumps({'positions':[0,0,0], 'triangles':[0,0,0]}))
        self.assertIn('embedded_mesh_arrays',[x['kind'] for x in report['findings']])
        report=self.run_fixture('app/renamed.dat',b'\x80not-a-reviewed-library')
        self.assertIn('unreviewed_binary_payload',[x['kind'] for x in report['findings']])

    def test_reference_provenance_is_neutral(self):
        raw=(ROOT/'tests/fixtures_travel6/destination-records-v25.json').read_text(encoding='utf-8')
        self.assertIn('<local-source>/',raw)
        self.assertIsNone(scanner.WINDOWS_USER.search(raw))

class OptionalTextRulesTests(unittest.TestCase):
    def test_missing_rules_are_explicit_and_no_dll_load(self):
        with tempfile.TemporaryDirectory(prefix='release8-rules-') as tmp:
            root=Path(tmp)
            with patch.object(textcheck.xg_paths,'DATA',root),patch.object(textcheck.xg_paths,'GAME_DIR',root/'game/version'),patch.object(textcheck.C,'CDLL') as dll:
                with self.assertRaises(textcheck.RulesUnavailable):textcheck.Engine()
                dll.assert_not_called()

    def test_no_installed_app_rule_fallback(self):
        with tempfile.TemporaryDirectory(prefix='release8-rule-path-') as tmp:
            root=Path(tmp);installed=root/'app/ref/textrules';installed.mkdir(parents=True)
            (installed/'cached_patterns.jsonl').write_text('{}\n',encoding='utf-8')
            with patch.object(textcheck.xg_paths,'DATA',root/'data'),patch.object(textcheck.xg_paths,'GAME_DIR',root/'data/game/current'),patch.object(textcheck.xg_paths,'APP',root/'app'):
                self.assertEqual(textcheck._rules_dir(),root/'data/game/current/text-rules')

    def test_missing_rule_notice_is_once_and_chat_text_preserved(self):
        source=(ROOT/'app/bot/ai_companion.py').read_text(encoding='utf-8')
        node=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name=='pass_review')
        code=ast.Module(body=[node],type_ignores=[])
        logs=[]
        def missing():raise textcheck.RulesUnavailable('synthetic-missing')
        namespace={'textcheck':SimpleNamespace(engine=missing,RulesUnavailable=textcheck.RulesUnavailable),
                   'log':lambda event,**kw:logs.append((event,kw))}
        exec(compile(code,'fixture-pass-review','exec'),namespace)
        owner=SimpleNamespace(REVIEW_GROUPS=('shield',))
        self.assertEqual(namespace['pass_review'](owner,'fixture first'),'fixture first')
        self.assertEqual(namespace['pass_review'](owner,'fixture second'),'fixture second')
        self.assertEqual([x[0] for x in logs],['textcheck_unavailable'])

if __name__=='__main__':unittest.main()
