import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]


class AccountOperationsTests(unittest.TestCase):
    def test_account_backend_offline(self):
        result=subprocess.run([sys.executable,'-X','utf8','-B',str(ROOT/'tests/account_ops_scenarios.py')],capture_output=True,text=True,encoding='utf-8',timeout=45)
        self.assertEqual(result.returncode,0,result.stderr[-4000:])
        self.assertGreaterEqual(json.loads(result.stdout)['passed'],14)

    def test_account_interface_offline(self):
        node=os.environ.get('XG_TEST_NODE') or shutil.which('node')
        self.assertTrue(node)
        args=[node,str(ROOT/'tests/account_ops_scenarios.cjs')]
        if os.environ.get('XG_ACCOUNT_TEST_OUTPUT'):args.append(os.environ['XG_ACCOUNT_TEST_OUTPUT'])
        result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr[-4000:])
        self.assertGreaterEqual(json.loads(result.stdout)['passed'],8)
