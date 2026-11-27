"""T114 discovery and conflict regression tests; never run real MuMu/ADB."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_adb_discovery12 import OfflineBase
from capture import device
from capture.mumu_info import MuMuInstance, parse_instances
import xg_adb

_UNINSTALL_ROOTS = device._registry_uninstall_roots


def instance(*, host="127.0.0.1", port=19601, running=True, root=True, debug=True):
    return {"adb_host_ip": host, "adb_port": port, "is_android_started": running,
            "system": {"root": root}, "adb_debug": {"enabled": debug}}


class ManagerFormatTests(unittest.TestCase):
    def test_json_keyed_array_wrapped_and_prefixed(self):
        rows = [instance(host="192.168.3.42", port=16384), instance(port=19601, running=False)]
        expected = (MuMuInstance(("192.168.3.42:5555", "192.168.3.42:16384", "127.0.0.1:16384"), True, True, True),
                    MuMuInstance(("127.0.0.1:19601",), False, True, True))
        for doc in ({"0": rows[0], "1": rows[1]}, {"result": {"instances": rows}}, rows):
            with self.subTest(doc=doc):
                self.assertEqual(parse_instances("MuMu Manager output\r\n" + json.dumps(doc)), expected)

    def test_flat_text_bridge_mode_is_not_the_debug_switch(self):
        text = '[VM 0]\nindex=0\nadb_host_ip: "192.168.3.42"\nadb_port = 16384\n' \
               'adb_debug.mode=0\nroot.enabled=关闭\nis_android_started=已启动\n'
        rows = parse_instances(text)
        self.assertEqual(rows, (MuMuInstance(("192.168.3.42:5555", "192.168.3.42:16384", "127.0.0.1:16384"),
                                             True, False, None),))

    def test_tabulated_old_cli_and_untrusted_endpoints(self):
        text = "index\tadb_host_ip\tadb_port\tvm_state\tadb_debug_enabled\n0\t8.8.8.8\t7555\tstopped\tfalse\n"
        self.assertEqual(parse_instances(text), (MuMuInstance(("127.0.0.1:7555",), False, None, False),))
        self.assertEqual(parse_instances('{"0":{"adb_host_ip":"malicious.test", "adb_port":70000}}'), ())

    def test_single_vm_without_index_may_nest_network_and_settings(self):
        text = json.dumps({"network": {"guest_ip": "10.1.2.3", "adb_port": 19808},
                           "system": {"root": True}, "adb_debug": {"enabled": False}})
        self.assertEqual(parse_instances(text), (MuMuInstance(("10.1.2.3:5555", "127.0.0.1:19808"),
                                                              None, True, False),))

    def test_camel_case_fields_and_numeric_state_only_rows(self):
        doc = {"0":{"status":"stopped"}, "1":{"adbHostIp":"10.1.2.5", "adbPort":18912,
                                                "isAndroidStarted":True, "rootEnabled":False}}
        self.assertEqual(parse_instances(json.dumps(doc)), (
            MuMuInstance((), False, None, None),
            MuMuInstance(("10.1.2.5:5555", "10.1.2.5:18912", "127.0.0.1:18912"), True, False, None),
        ))


class InstallationLayoutTests(OfflineBase):
    def test_manager_on_path_discovers_custom_adb_root(self):
        manager = self.put('定制 空格/nx_main/MuMuManager.exe')
        adb = self.install('定制 空格', '12.0')
        self.mock('shutil.which', side_effect=lambda name: str(manager) if name == 'MuMuManager.exe' else None)
        self.mock('_probe_adb_devices', return_value=['emulator-5454'])
        self.assertEqual(device.find_adb(), (adb, self.root/'定制 空格'))

    def test_bundled_adb_is_fallback_after_mu_mu(self):
        adb = self.install('MuMuPlayerX', '12.0')
        bundled = self.put('app/bin/adb.exe')
        self.mock('_bundled_adb_paths', return_value=[bundled])
        self.assertEqual(self.select([self.root/'MuMuPlayerX'], ready=[adb,bundled]), (adb,self.root/'MuMuPlayerX'))
        self.assertEqual(self.calls[0][0],adb)

    def test_vendor_probe_budget_reserves_existing_adb_path(self):
        for i in range(12):
            self.install('MuMuPlayer', str(i))
        existing = self.put('AndroidStudio/platform-tools/adb.exe')
        adb, root = self.select([self.root/'MuMuPlayer'], ready=[existing], processes=[existing])
        self.assertEqual((adb,root), (existing,None))
        self.assertEqual(len(self.calls),device.MAX_ADB_PROBES)

    @unittest.skipUnless(os.name == 'nt', 'Windows uninstall registry shape')
    def test_uninstall_registry_6_x_and_custom_drive_from_display_icon(self):
        close = []
        known = self.root/'中文 自定义安装/Nemu 6'
        class Key:
            def __init__(self, name): self.name = name
            def __enter__(self): return self
            def __exit__(self,*args): close.append(self.name)
        def enum(parent, index):
            if index > 1: raise OSError('end')
            return ('MuMu 6', 'unrelated')[index]
        def query(key, name):
            if name == 'DisplayName': return key.name, 1
            if key.name == 'MuMu 6' and name == 'DisplayIcon': return f'"{known}/NemuPlayer.exe",0', 1
            raise OSError('missing')
        mock = types.SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, EnumKey=enum,
                                     OpenKey=lambda parent,name: Key(name), QueryValueEx=query)
        with patch.dict(sys.modules, {'winreg':mock}):
            roots = _UNINSTALL_ROOTS()
        self.assertEqual(roots, [known])
        self.assertEqual(len(close), 12)


class LiveDiscoveryTests(OfflineBase):
    def setup_flow(self, manager_docs=None, devices=(), game=None, *, activities=None, packages=None, manager_rc=0):
        adb = self.put("中文 安装目录/nx_device/12.0/shell/adb.exe")
        manager = self.put("中文 安装目录/nx_main/MuMuManager.exe") if manager_docs is not None else None
        pending = iter(manager_docs or [])
        ready = dict(devices)
        game = game or {}
        activities, packages = activities or {}, packages or {}
        def manager_run(argv, **kwargs):
            self.assertEqual(argv, [str(manager), 'info', '-v', 'all'])
            self.assertEqual(kwargs['cwd'], str(manager.parent))
            self.calls.append(("manager", argv))
            output = next(pending)
            return subprocess.CompletedProcess(argv, manager_rc, json.dumps(output).encode("utf-8"), b"")
        if manager:
            self.exec_guard.side_effect = manager_run
        def run(path, *args, serial=None, timeout=20):
            self.calls.append((args[0], serial, args[1:] or None))
            if args == ("devices",):
                return 0, "List of devices attached\n" + "\n".join(f"{key}\t{value}" for key, value in ready.items()), ""
            if args[0] == "connect":
                endpoint = args[1]
                if endpoint in game:
                    ready[endpoint] = "device"
                    return 0, "connected", ""
                return 1, "", "refused"
            if args[0] == "shell":
                cmd = args[1]
                if cmd == "pidof com.netease.sky":
                    return (0, str(game[serial]), "") if serial in game else (1, "", "")
                if cmd == "pm list packages com.netease.sky":
                    return 0, packages.get(serial, "package:com.netease.sky"), ""
                if cmd == "dumpsys activity activities":
                    return 0, activities.get(serial, ""), ""
                if cmd == "dumpsys package com.netease.sky":
                    return 0, "versionCode=421629 targetSdk=30", ""
                if cmd == "getconf PAGESIZE":
                    return 0, "4096\n", ""
                if cmd in ("ps -A", "ps", "id"):
                    return 1, "", ""
            raise AssertionError(args)
        self.mock("adb_run", side_effect=run)
        return adb

    def test_bridge_ip_reloaded_on_each_check_before_persisted_stale_ip(self):
        serials = ["192.168.3.42:5555", "192.168.3.43:5555"]
        self.cfg.return_value = {"serial": serials[0]}
        docs = [{"0": instance(host=serial.rsplit(":", 1)[0], port=16384, root=False)} for serial in serials]
        adb = self.setup_flow(docs, game={serials[0]: 123, serials[1]: 124})
        for num, serial in enumerate(serials):
            with self.subTest(serial=serial):
                diag = {}
                self.assertEqual(device.find_game(adb, diagnostics=diag), {"serial":serial, "pid":123+num})
                self.assertIs(diag["root_enabled"], False)
        commands = [entry[0] for entry in self.calls]
        self.assertEqual(commands.count("manager"), 2)
        self.assertEqual([c[2][0] for c in self.calls if c[0] == "connect"], serials)

    def test_network_nat_port_changes_and_stopped_instance_is_ignored(self):
        dead, current = "127.0.0.1:16384", "127.0.0.1:19204"
        adb = self.setup_flow([{"0":instance(port=16384,running=False),"1":instance(port=19204)}],
                              devices=[(dead,"device")],game={dead:123,current:555})
        self.assertEqual(device.find_game(adb), {"serial":current,"pid":555})
        self.assertFalse(any(call[0] == "shell" and call[1] == dead for call in self.calls))

    def test_failed_manager_uses_legacy_local_port(self):
        serial = "127.0.0.1:16384"
        adb = self.setup_flow(["legacy CLI rejects info"], manager_rc=2, game={serial:44})
        self.assertEqual(device.find_game(adb), {"serial":serial,"pid":44})

    def test_known_mumu_not_started_has_no_connect(self):
        adb = self.setup_flow([{"0":instance(running=False)}])
        diag = {}
        self.assertIsNone(device.find_game(adb, diagnostics=diag))
        self.assertEqual(diag['reason'], 'mumu_not_started')
        self.assertNotIn('connect', [call[0] for call in self.calls])

    def test_known_adb_debug_disabled_has_no_connect(self):
        adb = self.setup_flow([{"0":instance(debug=False)}])
        diag = {}
        self.assertIsNone(device.find_game(adb, diagnostics=diag))
        self.assertEqual(diag['reason'], 'mumu_adb_disabled')
        self.assertNotIn('connect', [call[0] for call in self.calls])

    def test_public_and_usb_not_probed_even_if_explicit(self):
        devices = [('52.1.2.3:5555','device'), ('R5USB','device')]
        adb = self.setup_flow(devices=devices, game={serial:4 for serial, _ in devices})
        diag = {}
        with patch.dict(os.environ, {'XG_ADB_SERIAL': devices[0][0]}):
            self.assertIsNone(device.find_game(adb, diagnostics=diag))
        self.assertEqual(diag['reason'], 'unsupported_device_scope')
        self.assertFalse(any(call[0] == 'shell' or (call[0] == 'connect' and not call[2][0].startswith('127.'))
                             for call in self.calls))

    def test_offline_unauthorized_distinction(self):
        for status in ('offline','unauthorized'):
            with self.subTest(status=status):
                adb = self.setup_flow(devices=[('127.0.0.1:20092',status)])
                diag = {}
                self.assertIsNone(device.find_game(adb, diagnostics=diag))
                self.assertEqual(diag['reason'], f'mumu_adb_{status}')

    def test_channel_build_is_identified_without_probing_channel_process(self):
        serial = 'emulator-5554'
        adb = self.setup_flow(devices=[(serial,'device')], packages={serial:'package:com.netease.sky.bilibili'})
        diag = {}
        self.assertIsNone(device.find_game(adb, diagnostics=diag))
        self.assertEqual(diag['reason'], 'game_channel_unsupported')
        self.assertEqual(diag['package'], 'com.netease.sky.bilibili')
        self.assertFalse(any('pidof com.netease.sky.bilibili' in (c[2] or ()) for c in self.calls))

    def test_game_not_running_and_not_installed(self):
        for packages, expected in [('package:com.netease.sky','game_not_running'),('', 'game_not_installed')]:
            with self.subTest(packages=packages):
                serial = 'emulator-5554'
                adb = self.setup_flow(devices=[(serial,'device')], packages={serial:packages})
                diag = {}
                self.assertIsNone(device.find_game(adb, diagnostics=diag))
                self.assertEqual(diag['reason'], expected)

    def test_game_background_uses_next_foreground_instance_then_explains_if_none(self):
        one, two = 'emulator-5554','emulator-5556'
        activities = {one:'  mResumedActivity: ActivityRecord{abcdef u0 com.android.launcher/.Home t4}',
                      two:'  topResumedActivity=ActivityRecord{abcdef u0 com.netease.sky/.Game t4}'}
        adb = self.setup_flow(devices=[(one,'device'),(two,'device')], game={one:44,two:55}, activities=activities)
        self.assertEqual(device.find_game(adb), {'serial':two,'pid':55})
        adb = self.setup_flow(devices=[(one,'device')],game={one:44},activities=activities)
        diag = {}
        self.assertIsNone(device.find_game(adb, diagnostics=diag))
        self.assertEqual(diag['reason'], 'game_in_background')

    def test_invalid_adb_path_and_version_failure_explain_different_causes(self):
        self.assertEqual(device.check_device(self.root/'bad.exe')['reason'], 'adb_path_invalid')
        serial = 'emulator-5554'
        adb = self.setup_flow(devices=[(serial,'device')],game={serial:42})
        with patch.object(device, 'game_version', return_value='421589'), patch.object(device, 'ensure_root') as root:
            result = device.check_device(adb)
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'], 'game_version_unsupported')
            self.assertIn('0.16.7', result['message'])
            root.assert_not_called()

    def test_root_metadata_disables_root_probe_and_reports_its_own_cause(self):
        adb = self.setup_flow([{"0":instance(root=False)}],game={'127.0.0.1:19601':42})
        result = device.check_device(adb)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'root_disabled')
        self.assertIn('Root', result['message'])
        self.assertFalse(any(c[0]=='root' or (c[0]=='shell' and c[2]==('id',)) for c in self.calls))

    def test_transport_failure_during_root_never_becomes_root_hint(self):
        serial = 'emulator-5554'
        adb = self.setup_flow(devices=[(serial,'device')],game={serial:42})
        original = device.adb_run.side_effect
        def unplugged(path,*args,**kw):
            if args==('shell','id'):
                return 1, '', 'device offline'
            return original(path,*args,**kw)
        device.adb_run.side_effect = unplugged
        result = device.check_device(adb)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'adb_disconnected')
        self.assertNotIn('Root', result['message'])


class AdbServerGuardTests(unittest.TestCase):
    def test_guard_uses_only_configured_loopback_socket(self):
        self.assertEqual(xg_adb._endpoint({}), ('127.0.0.1',5037))
        self.assertEqual(xg_adb._endpoint({'ANDROID_ADB_SERVER_PORT':'21112'}), ('127.0.0.1',21112))
        self.assertEqual(xg_adb._endpoint({'ADB_SERVER_SOCKET':'tcp:[::1]:9900'}), ('::1',9900))
        self.assertIsNone(xg_adb._endpoint({'ADB_SERVER_SOCKET':'tcp:192.168.1.2:5037'}))
        self.assertIsNone(xg_adb._endpoint({'ANDROID_ADB_SERVER_ADDRESS':'other.example'}))

    def test_fragmented_host_version_reads_are_bounded(self):
        class Connection:
            def __init__(self): self.response = bytearray(b'OKAY00040029')
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def sendall(self,request): self.request = request
            def recv(self,count):
                result = bytes(self.response[:1]); del self.response[:1]; return result
        connection = Connection()
        with patch.object(xg_adb.socket,'create_connection', return_value=connection) as connect:
            self.assertEqual(xg_adb._server_protocol(('127.0.0.1',5037)),41)
            self.assertEqual(connection.request,b'000chost:version')
            connect.assert_called_once_with(('127.0.0.1',5037), timeout=0.2)

    def test_incompatible_or_unknown_server_never_runs_the_device_command(self):
        for server, client in [(41,39),(xg_adb._UNKNOWN,41)]:
            with self.subTest(server=server), patch.object(xg_adb.os,'name','nt'), \
                 patch.object(xg_adb,'_server_protocol',return_value=server), \
                 patch.object(xg_adb,'_client_protocol',return_value=client), \
                 patch.object(xg_adb.xg_exec,'run') as run:
                result = xg_adb.run(['vendor-adb.exe'], ['devices'], env={})
                self.assertEqual(result.stderr,xg_adb.SERVER_CONFLICT.encode())
                run.assert_not_called()

    def test_compatible_and_absent_server_both_allow_client(self):
        for server in (41,None):
            with self.subTest(server=server), patch.object(xg_adb.os,'name','nt'), \
                 patch.object(xg_adb,'_server_protocol',return_value=server), \
                 patch.object(xg_adb,'_client_protocol',return_value=41), \
                 patch.object(xg_adb.xg_exec,'run',return_value=Mock(returncode=0)) as run:
                result = xg_adb.run(['vendor-adb.exe'], ['devices'], env={})
                self.assertEqual(result.returncode,0)
                run.assert_called_once_with(['vendor-adb.exe','devices'], env={})


if __name__ == '__main__':
    unittest.main()
