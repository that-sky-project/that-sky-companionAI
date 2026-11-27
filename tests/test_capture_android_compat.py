"""Synthetic ADB output only: no simulator, host ADB, or network access."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from capture import android_compat, device, device_scope, errors, native_cache, pause


class SerialScopeTests(unittest.TestCase):
    def test_allowed_serials_including_lan_and_loopback(self):
        for serial in ('127.0.0.1:16384', '127.1.2.3:1', 'localhost:7555', '[::1]:5555',
                       'emulator-5554', '10.0.0.1:5555', '192.168.3.42:5555',
                       '172.16.1.2:5555', '172.31.1.2:65535'):
            with self.subTest(serial=serial):
                self.assertTrue(device_scope.is_supported_emulator_serial(serial))
                self.assertEqual(pause._pid(type('Fake', (), {'serial': serial, 'pid': 123})()), 123)

    def test_usb_public_link_local_and_malformed_are_rejected_before_commands(self):
        for serial in ('R5USB123', '8.8.8.8:5555', '172.15.1.2:5555', '172.32.1.2:5555',
                       '169.254.1.1:5555', '100.64.0.1:5555', '192.0.0.1:5555', 'mumu.home:5555',
                       '[fd00::1]:5555', '192.168.1.4:0', '127.0.0.1:65536',
                       'emulator-00000', 'emulator-５５５４', 'localhost:5555\n', None):
            with self.subTest(serial=serial):
                self.assertFalse(device_scope.is_supported_emulator_serial(serial))
                with self.assertRaisesRegex(ValueError, '^capture_device_scope$'):
                    native_cache.Device('mock-adb.exe', serial)
                with self.assertRaisesRegex(ValueError, '^current_profile_pause_device_scope$'):
                    pause._pid(type('Fake', (), {'serial': serial, 'pid': 123})())
                with patch.object(device, 'adb_run') as run:
                    self.assertFalse(device.ensure_root('mock-adb.exe', serial))
                    self.assertIsNone(device.game_pid('mock-adb.exe', serial))
                    self.assertEqual(device.current_scene('mock-adb.exe', serial, 123), (None, None))
                    run.assert_not_called()

    def test_scope_not_misdiagnosed_as_root(self):
        error = ValueError('current_profile_pause_device_scope')
        self.assertEqual(errors.error_code(error), 'current_profile_pause_device_scope')
        self.assertIn('桥接内网 IP', errors.explain(error))
        self.assertNotIn('Root', errors.explain(error))
        self.assertIn('Root', errors.explain(RuntimeError('capture_root_unavailable')))

    def test_loopback_test_does_not_classify_bridged_or_public_as_localhost(self):
        for value in ('127.8.9.10:5555', 'Localhost:5555', '[::1]:5555'):
            self.assertTrue(device_scope.is_loopback_emulator_tcp_serial(value))
        for value in ('emulator-5554', '192.168.3.42:5555', '8.8.8.8:5555', 'localhost:0'):
            self.assertFalse(device_scope.is_loopback_emulator_tcp_serial(value))

    def test_bridged_pause_still_uses_existing_identity_and_rescue_checks(self):
        data = b'fixture'
        identity = b'com.netease.sky\x00\n135 (fixture command) S ' + b'0 ' * 18 + b'442\n'
        commands = ['dd if=/proc/135/mem bs=1 skip=1 count=1 2>/dev/null'] * 20
        emu = Mock(serial='192.168.3.42:5555', pid=135)
        emu.call.side_effect = [identity, pause.HEADER + data + pause.TRAILER, b'']
        self.assertEqual(pause.read_paused_pair(emu, commands, len(data)), data)
        self.assertEqual(emu.call.call_count, 3)
        self.assertIn('cmdline', emu.call.call_args_list[0].args[0][-1])
        self.assertIn('kill -STOP 135', emu.call.call_args_list[1].args[0][-1])
        self.assertIn('kill -CONT 135', emu.call.call_args_list[2].args[0][-1])
        emu.reset_mock()
        emu.serial = '8.8.8.8:5555'
        with self.assertRaisesRegex(ValueError, 'current_profile_pause_device_scope'):
            pause.read_paused_pair(emu, commands, len(data))
        emu.call.assert_not_called()


class AndroidShellTests(unittest.TestCase):
    def test_pidof_modern_and_legacy_ps_exact_process(self):
        with patch.object(device, 'adb_run', return_value=(0, ' 345 ', '')) as run:
            self.assertEqual(device.game_pid('mock-adb.exe', '192.168.3.42:5555'), 345)
            self.assertEqual(run.call_count, 1)
        replies = [(127, '', 'pidof: not found'), (1, '', 'ps: bad option -A'),
                   (0, 'USER PID PPID VSIZE RSS WCHAN PC NAME\n'
                       'u0_a12 678 1 0 0 SyS_epoll_wait 0 S com.netease.sky\n'
                       'u0_a12 679 1 0 0 SyS_epoll_wait 0 S com.netease.sky:remote\n'
                       'u0_a13 680 1 0 0 SyS_epoll_wait 0 S bad.com.netease.sky\n', '')]
        with patch.object(device, 'adb_run', side_effect=replies) as run:
            self.assertEqual(device.game_pid('mock-adb.exe', 'emulator-5554'), 678)
            self.assertEqual([call.args[2] for call in run.call_args_list],
                             ['pidof com.netease.sky', 'ps -A', 'ps'])

    def test_ambiguous_pid_or_invalid_package_never_picks_unrelated_process(self):
        rows = 'PID USER ARGS\n12 shell com.netease.sky --instrumented\n15 app com.netease.sky\n'
        with patch.object(device, 'adb_run', side_effect=[(0, '12 15', ''), (0, rows, '')]):
            self.assertIsNone(device.game_pid('mock-adb.exe', 'emulator-5554'))
        with patch.object(device, 'adb_run') as run:
            self.assertIsNone(device.game_pid('mock-adb.exe', 'emulator-5554', package='x; id'))
            run.assert_not_called()

    def test_offline_does_not_query_ps(self):
        with patch.object(device, 'adb_run', return_value=(1, '', 'adb.exe: device offline')) as run:
            self.assertIsNone(device.game_pid('mock-adb.exe', 'emulator-5554'))
            self.assertEqual(run.call_count, 1)

    def test_native_logcat_pid_supported_and_legacy_fallback(self):
        prefix = '11-28 00:00:00.123'
        target = f'{prefix} 135 117 I Sky: '
        other_process = f'{prefix} 777 117 I Unrelated: unknown option from a different app'
        stdout = '\n'.join((target + 'NetVersion: [1, 2, 23536, 0, 0, 3]',
                            other_process, target + 'levelName: CandleSpace levelId: 5'))
        with patch.object(device, 'adb_run', return_value=(0, stdout, '')) as run:
            self.assertEqual(device.current_scene('mock-adb.exe', 'emulator-5554', 135), ('CandleSpace', 5))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[-1], '--pid=135')
        for err in ((1, '', 'logcat: unrecognized option `--pid=135`'),
                    (0, 'logcat: Unknown option --pid', '')):
            with self.subTest(err=err), patch.object(device, 'adb_run', side_effect=[err, (0, stdout, '')]) as run:
                self.assertEqual(device.current_scene('mock-adb.exe', 'emulator-5554', 135), ('CandleSpace', 5))
                self.assertEqual(run.call_args.args[1:], ('logcat', '-d', '-t', '4096', '-v', 'threadtime'))

    def test_logcat_offline_not_misdiagnosed_as_incompatible(self):
        with patch.object(device, 'adb_run', return_value=(1, '', 'device offline')) as run:
            self.assertEqual(device.current_scene('mock-adb.exe', 'emulator-5554', 135), (None, None))
            self.assertEqual(run.call_count, 1)


class RootCompatibilityTests(unittest.TestCase):
    def test_enabled_modes_confirm_actual_uid_zero_without_adb_restart(self):
        for successful in ('id', "su -c 'id'", "su 0 sh -c 'id'", "su 0 -c 'id'"):
            with self.subTest(form=successful):
                def reply(adb, mode, command, **kwargs):
                    self.assertEqual(mode, 'shell')
                    return (0, 'uid=0(root) gid=0(root)', '') if command == successful else (0, 'uid=2000(shell)', '')
                with patch.object(device, 'adb_run', side_effect=reply) as run:
                    self.assertTrue(device.ensure_root('mock-adb.exe', 'emulator-5554'))
                    self.assertEqual(run.call_args.args[-1], successful)

    def test_manager_disabled_root_bypasses_all_adb_commands(self):
        with patch.object(device, 'adb_run') as run:
            info = {}
            self.assertFalse(device.ensure_root('mock-adb.exe', 'emulator-5554', root_enabled=False,
                                                diagnostics=info))
            run.assert_not_called()
            self.assertEqual(info['reason'], 'root_disabled')
            self.assertIn('未开启', info['message'])

    def test_adb_root_normal_restart_reconnects_only_tcp(self):
        for serial in ('192.168.3.42:5555', 'emulator-5554'):
            with self.subTest(serial=serial), patch.object(device, 'adb_run') as run, \
                    patch.object(device, '_connect', return_value=True) as connect, patch.object(device.time, 'sleep'):
                run.side_effect = [*(4 * [(0, 'uid=2000(shell)', '')]), (0, 'restarting adbd as root', ''),
                                   (0, '', ''), (0, 'uid=0(root) gid=0(root)', '')]
                self.assertTrue(device.ensure_root('mock-adb.exe', serial))
                self.assertEqual([call.args[1] for call in run.call_args_list][-3:],
                                 ['root', 'wait-for-device', 'shell'])
                self.assertEqual(connect.call_count, int(':' in serial))

    def test_adb_root_denied_is_not_retried(self):
        replies = [*(3 * [(0, 'error: unexpected uid=0', '')]), (0, 'uid=2000(shell)', ''),
                   (0, '', 'adbd cannot run as root in production builds')]
        with patch.object(device, 'adb_run', side_effect=replies) as run, patch.object(device, '_connect') as connect:
            self.assertFalse(device.ensure_root('mock-adb.exe', '192.168.3.42:5555'))
            self.assertEqual(run.call_args.args, ('mock-adb.exe', 'root'))
            connect.assert_not_called()

    def test_android15_user_build_distinguishes_adbd_refusal_from_disabled_root(self):
        for root_response in ((0, 'adbd cannot run as root in production builds', ''),
                              (1, '', 'adbd cannot run as root in user builds')):
            with self.subTest(root_response=root_response):
                def fake_adb(_adb, *args, **kwargs):
                    return root_response if args == ('root',) else (0, 'uid=2000(shell)', '')
                info = {}
                with patch.object(device, 'adb_run', side_effect=fake_adb) as run, \
                        patch.object(device, '_connect') as connect:
                    self.assertFalse(device.ensure_root('mock-adb.exe', 'emulator-5554',
                                                        root_enabled=True, diagnostics=info))
                self.assertEqual(info['reason'], 'root_user_build')
                self.assertIn('adb root 不可用', info['message'])
                self.assertEqual([call.args[1] for call in run.call_args_list],
                                 ['shell', 'shell', 'shell', 'shell', 'root'])
                connect.assert_not_called()

    def test_transport_errors_do_not_issue_adb_root_or_show_root_disabled(self):
        cases = ((1, 'adb.exe: device offline', 'adb_disconnected'),
                 (1, 'xg_adb_server_version_conflict', 'adb_server_conflict'),
                 (-1, 'Command timed out after 5 seconds', 'adb_timeout'),
                 (-1, '[WinError 2] The system cannot find the file specified', 'adb_missing'),
                 (-1, '[WinError 5] Access is denied', 'adb_permission'),
                 (-1, '[WinError 225] Blocked by antivirus', 'adb_permission'))
        for code, error, reason in cases:
            with self.subTest(reason=reason), patch.object(device, 'adb_run', return_value=(code, '', error)) as run:
                info = {}
                self.assertFalse(device.ensure_root('mock-adb.exe', 'emulator-5554', diagnostics=info))
                self.assertEqual(run.call_count, 1)
                self.assertEqual(info['reason'], reason)
                self.assertNotIn('Root', info['message'])


class NativeReadCompatibilityTests(unittest.TestCase):
    @staticmethod
    def output(code=0, out=b'', err=b''):
        return subprocess.CompletedProcess([], code, out, err)

    def test_reuses_verified_shell_without_retrying_a_privileged_command(self):
        d = native_cache.Device('mock-adb.exe', '192.168.3.42:5555')
        with patch.object(d, '_run', side_effect=[self.output(out=b'uid=2000(shell)'), self.output(1),
                          self.output(out=b'uid=0(root)'), self.output(out=b'ok'), self.output(1)]) as run:
            self.assertEqual(d.call(['exec-out', "su -c 'true'"]), b'ok')
            with self.assertRaisesRegex(RuntimeError, '^adb operation failed$'):
                d.call(['exec-out', "su -c 'true'"])
            self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['id', "su -c 'id'",
                             "su 0 sh -c 'id'", "su 0 sh -c 'true'", "su 0 sh -c 'true'"])

    def test_maps_read_denied_and_process_gone_are_separate(self):
        d = native_cache.Device('mock-adb.exe', 'emulator-5554')
        replies = [(b'Permission denied', 'capture_maps_denied'),
                   (b'No such file or directory', 'capture_game_exited')]
        for output, error in replies:
            with self.subTest(error=error), patch.object(d, '_run', side_effect=[
                    self.output(out=b'uid=0(root)'), self.output(0, b'cat: /proc/145/maps: ' + output)]):
                d._root_prefix = None
                with self.assertRaisesRegex(RuntimeError, '^' + error + '$'):
                    d.call(['shell', "su -c 'cat /proc/145/maps'"])

    def test_parse_maps_whitespace_and_anonymous_mappings(self):
        rows = native_cache.parse_maps('aa12-bb13\tr-xp  \t0010 08:01 441   /data/app/新版/libBootloader.so\n'
                                       'cdef-d000 rw-p 0000 00:00 0\nnot a mapping\n')
        self.assertEqual([(row['lo'], row['path']) for row in rows],
                         [(0xaa12, '/data/app/新版/libBootloader.so'), (0xcdef, '')])

    def test_native_adb_errors_and_missing_scene_are_specific(self):
        d = native_cache.Device('mock-adb.exe', 'emulator-5554')
        for output, code in ((b'error: device offline', 'capture_adb_disconnected'),
                             (b'xg_adb_server_version_conflict', 'capture_adb_conflict')):
            with self.subTest(code=code), patch.object(d, '_run', return_value=self.output(1, err=output)):
                with self.assertRaisesRegex(RuntimeError, '^' + code + '$'):
                    d.call(['exec-out', "su -c 'true'"])
        with self.assertRaisesRegex(ValueError, '^capture_scene_log_missing$'):
            native_cache.latest_scene('', pid=145, scene='CandleSpace', revision='V76318')


if __name__ == '__main__':
    unittest.main()
