"""Offline launch failure checks and bounded, serial wrapper-child regressions."""
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import package_smoke as smoke
from test_fleet import serial_fleet


class LaunchUnitTests(unittest.TestCase):
    def setUp(self):
        self.group, self.process, self.thread = 0x100000001, 0x100000002, 0x100000003
        self.calls = []
        def create(*args):
            self.calls.append(('create', args))
            return self.process, self.thread, 123, 124
        self.create = self.enterContext(mock.patch.object(smoke._winapi, 'CreateProcess', side_effect=create))
        self.enterContext(mock.patch.object(smoke, 'new_owned_job', return_value=self.group))
        self.assign = self.enterContext(mock.patch.object(smoke.kernel, 'AssignProcessToJobObject',
            side_effect=lambda *args: self.calls.append(('assign', args)) or True))
        self.resume = self.enterContext(mock.patch.object(smoke.kernel, 'ResumeThread',
            side_effect=lambda handle: self.calls.append(('resume', handle)) or 1))
        self.close = self.enterContext(mock.patch.object(smoke._winapi, 'CloseHandle'))
        self.terminate = self.enterContext(mock.patch.object(smoke._winapi, 'TerminateProcess'))
        self.terminate_job = self.enterContext(mock.patch.object(smoke.kernel, 'TerminateJobObject', return_value=True))
        self.wait = self.enterContext(mock.patch.object(smoke._winapi, 'WaitForSingleObject', return_value=0))
        self.enterContext(mock.patch.object(smoke._winapi, 'GetExitCodeProcess', return_value=259))
        self.drain = self.enterContext(mock.patch.object(smoke, '_drain'))

    def test_suspended_assignment_precedes_execution_and_only_stdio_inherits(self):
        process = smoke.OwnedProcess([sys.executable, '-c', 'pass'], ROOT)
        try:
            self.assertEqual([name for name, _ in self.calls], ['create', 'assign', 'resume'])
            args = self.create.call_args.args
            self.assertTrue(args[5] & 4)
            self.assertEqual(args[7], str(ROOT))
            startup = args[8]
            self.assertEqual(startup.lpAttributeList['handle_list'],
                             [startup.hStdInput, startup.hStdOutput, startup.hStdError])
            self.assertNotIn(self.group, startup.lpAttributeList['handle_list'])
            self.assign.assert_called_once_with(self.group, self.process)
            self.resume.assert_called_once_with(self.thread)
            self.assertEqual(process.communicate(timeout=1), (b'', b''))
            self.assertEqual(process.returncode, 259)
        finally:
            process.close()
        self.assertTrue(process.stdout.closed and process.stderr.closed)
        self.assertCountEqual([call.args[0] for call in self.close.call_args_list],
                              [self.group, self.process, self.thread])

    def test_assignment_failure_terminates_only_exact_unresumed_root(self):
        self.assign.side_effect = None
        self.assign.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'assignment failed'):
            smoke.OwnedProcess([sys.executable], ROOT)
        self.resume.assert_not_called()
        self.terminate.assert_called_once_with(self.process, 70)
        self.terminate_job.assert_not_called()
        self.wait.assert_called_once_with(self.process, 5000)
        self.assertCountEqual([call.args[0] for call in self.close.call_args_list],
                              [self.group, self.process, self.thread])

    def test_resume_failure_drains_assigned_tree_and_releases_handles(self):
        self.resume.side_effect = None
        self.resume.return_value = 0xFFFFFFFF
        with self.assertRaisesRegex(RuntimeError, 'resume failed'):
            smoke.OwnedProcess([sys.executable], ROOT)
        self.terminate_job.assert_called_once_with(self.group, 70)
        self.terminate.assert_not_called()
        self.drain.assert_called_once_with(self.group)
        self.assertCountEqual([call.args[0] for call in self.close.call_args_list],
                              [self.group, self.process, self.thread])

    def test_creation_failure_releases_group_without_signalling_any_process(self):
        self.create.side_effect = OSError('injected create failure')
        with self.assertRaisesRegex(OSError, 'injected create failure'):
            smoke.OwnedProcess([sys.executable], ROOT)
        self.assign.assert_not_called()
        self.resume.assert_not_called()
        self.terminate.assert_not_called()
        self.terminate_job.assert_not_called()
        self.close.assert_called_once_with(self.group)

    def test_timed_out_wait_is_not_success(self):
        process = smoke.OwnedProcess([sys.executable], ROOT)
        self.wait.return_value = 258
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=0)
            self.assertIsNone(process.returncode)
        finally:
            self.wait.return_value = 0
            process.close()


def wrapper(marker):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        Path(marker).write_text(json.dumps({'pid': child.pid}), encoding='ascii')
        child.wait(timeout=12)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


class LaunchProcessTests(unittest.TestCase):
    def test_delayed_assignment_escape_and_suspended_launch_containment(self):
        # The legacy ordering is reproduced INSIDE a separately owned envelope.
        # Its intentionally unassigned child can never escape fixture cleanup.
        with serial_fleet(), tempfile.TemporaryDirectory(prefix='headroom-package-launch-') as folder:
            base = Path(folder)
            for legacy in (True, False):
                marker = base / ('legacy.json' if legacy else 'fixed.json')
                process = smoke.OwnedProcess([sys.executable, str(Path(__file__)), '--wrapper', marker], base)
                inner = child = None
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            pid = json.loads(marker.read_text())['pid']
                            break
                        except (FileNotFoundError, ValueError):
                            self.assertIsNone(process.poll())
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.01)
                    child = smoke.kernel.OpenProcess(0x100000 | 0x0400, False, pid)
                    self.assertTrue(child)
                    if legacy:
                        inner = smoke.new_owned_job()
                        self.assertTrue(smoke.kernel.AssignProcessToJobObject(inner, process.handle))
                        group = inner
                    else:
                        group = process.group
                    member = w.BOOL()
                    self.assertTrue(smoke.kernel.IsProcessInJob(child, group, ctypes.byref(member)))
                    self.assertEqual(bool(member.value), not legacy)
                    if legacy:
                        self.assertTrue(smoke.kernel.TerminateJobObject(inner, 70))
                        self.assertEqual(smoke._winapi.WaitForSingleObject(process.handle, 5000), 0)
                        self.assertEqual(smoke._winapi.WaitForSingleObject(child, 0), 258)
                    process.close()
                    self.assertEqual(smoke._winapi.WaitForSingleObject(child, 5000), 0)
                finally:
                    if process.handle is not None:
                        process.close()
                    if inner is not None:
                        smoke._winapi.CloseHandle(inner)
                    if child is not None:
                        smoke._winapi.CloseHandle(child)

    def test_capture_and_real_exit_259(self):
        with serial_fleet():
            process = smoke.OwnedProcess([sys.executable, '-c',
                "import sys; print('stdout'); print('stderr', file=sys.stderr); sys.exit(259)"], ROOT)
            try:
                self.assertEqual(process.communicate(timeout=5), (b'stdout\r\n', b'stderr\r\n'))
                self.assertEqual(process.poll(), 259)
            finally:
                process.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--wrapper':
        wrapper(sys.argv[2])
    else:
        unittest.main()
