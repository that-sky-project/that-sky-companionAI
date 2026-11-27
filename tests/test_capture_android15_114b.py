"""Synthetic ADB responses for capture diagnostics; no ADB or simulator is started."""
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from capture import android_compat, device, native_cache


class Android15CaptureTests(unittest.TestCase):
    def test_pidof_multi_result_is_resolved_with_shifted_toybox_columns(self):
        shell = Mock(side_effect=[(0, '128 410\r\n', ''), (0, (
            'Warning: optional feature unavailable\r\n'
            'PID PPID USER NAME S\r\n'
            '128 1 u0_a17 com.netease.sky R\r\n'
            '410 1 u0_a17 com.netease.sky:isolated S\r\n'
            '899 1 u0_a17 bad.com.netease.sky R\r\n'
        ), '')])
        self.assertEqual(android_compat.find_package_pid(shell, 'com.netease.sky'), 128)
        self.assertEqual([call.args[0] for call in shell.call_args_list], ['pidof com.netease.sky', 'ps -A'])

    def test_ps_does_not_select_pid_changed_between_commands(self):
        shell = Mock(side_effect=[(0, '128 410', ''), (0, 'USER PID ARGS\nshell 444 com.netease.sky', '')])
        self.assertIsNone(android_compat.find_package_pid(shell, 'com.netease.sky'))

    def test_threadtime_accepts_year_zone_microseconds_and_uid(self):
        stamp = '2026-11-28 19:20:21.123456 +08:00'
        raw = '\r\n'.join([
            '--------- beginning of main',
            f'{stamp} 10119: 135 117 I Sky: NetVersion: [1, 2, 23536, 0, 0, 3]',
            f'{stamp} 135: 999 119 W Other: logcat: Permission denied and unknown option --pid',
            f'{stamp} 10119: 135 117 I Sky: levelName: CandleSpace levelId: 5',
        ])
        with patch.object(device, 'adb_run', return_value=(0, raw, '')) as run:
            diagnostics = {}
            self.assertEqual(device.current_scene('fake-adb.exe', 'emulator-5554', 135,
                                                  diagnostics=diagnostics), ('CandleSpace', 5))
        self.assertEqual(diagnostics, {})
        self.assertEqual(run.call_args.args[1:], ('logcat', '-d', '-v', 'threadtime', '--pid=135'))
        self.assertEqual(run.call_count, 1)

    def test_explicit_log_buffer_denial_is_not_an_empty_success_or_a_wider_read(self):
        for result in ((1, '', 'logcat: Unable to open log buffer: Permission denied'),
                       (0, 'logcat: read failure: Operation not permitted', '')):
            with self.subTest(result=result), patch.object(device, 'adb_run', return_value=result) as run:
                diagnostics = {}
                self.assertEqual(device.current_scene('fake-adb.exe', 'emulator-5554', 135,
                                                      diagnostics=diagnostics), (None, None))
                self.assertEqual(diagnostics['scene_log_reason'], 'capture_logcat_denied')
                self.assertIn('缓冲区', diagnostics['scene_log_message'])
                self.assertEqual(run.call_count, 1)

    def test_unprivileged_page_size_does_not_guess_from_missing_or_invalid_output(self):
        for response, expected in [((0, '16384\r\n', ''), 16384), ((0, '4096', ''), 4096),
                                   ((127, '', 'getconf: not found'), None), ((0, '12288', ''), None),
                                   ((0, '16384\n4096', ''), None)]:
            with self.subTest(response=response), patch.object(device, 'adb_run', return_value=response) as run:
                self.assertEqual(device.guest_page_size('fake-adb.exe', 'emulator-5554'), expected)
                run.assert_called_once_with('fake-adb.exe', 'shell', 'getconf PAGESIZE',
                                            serial='emulator-5554', timeout=4)

    def test_known_16k_page_guest_stops_before_old_memory_path(self):
        a = SimpleNamespace(native_library=Path('fixture.so'), adb='fake-adb.exe', serial='emulator-5554')
        with patch.object(native_cache, 'sha256_file', return_value=native_cache.NATIVE_SHA256), \
             patch.object(native_cache, 'elf_loads', return_value=[]), \
             patch.object(native_cache.Device, '_run', side_effect=[
                subprocess.CompletedProcess([], 0, b'145\r\n', b''),
                subprocess.CompletedProcess([], 0, b'16384\r\n', b'')]) as run:
            with self.assertRaisesRegex(RuntimeError, '^capture_page_size_unsupported$'):
                native_cache.validated_context(a)
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ['shell', 'pidof com.netease.sky'], ['shell', 'getconf PAGESIZE']])

    def test_explicit_maps_and_mem_denials_end_without_fallback(self):
        instance = native_cache.Device('fake-adb.exe', 'emulator-5554')
        for filename, expected in [('maps', 'capture_maps_denied'), ('mem', 'capture_mem_denied')]:
            output = f"dd: failed to open '/proc/145/{filename}': Permission denied".encode()
            with self.subTest(filename=filename), patch.object(instance, '_run', return_value=
                    subprocess.CompletedProcess([], 0, output, b'')) as run:
                with self.assertRaisesRegex(RuntimeError, f'^{expected}$'):
                    instance.call(['exec-out', f'fixture /proc/145/{filename}'])
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
