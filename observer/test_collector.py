"""Finite native integration tests; only disposable sources and owned helpers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
BINARY = ROOT / 'build' / 'claude-observer.exe'


@unittest.skipUnless(os.name == 'nt' and BINARY.exists(), 'build Windows observer first')
class NativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='observer-native-synthetic-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.data = self.base / 'private'
        self.data.mkdir()
        self.projects = self.base / 'projects'
        self.projects.mkdir()
        self.history = self.base / 'history.jsonl'
        self.history.write_bytes(b'')

    def command(self, seconds=3, report=None, data=None):
        return [str(BINARY), '--output', str(data or self.data),
                '--deadline-ms', str(int(time.time() * 1000) + seconds * 1000),
                '--python', sys.executable, '--report', str(report or ROOT / 'report.py'),
                '--history', str(self.history), '--projects', str(self.projects),
                '--interval-ms', '200', '--metadata-ms', '1000']

    def run_native(self, args=None):
        return subprocess.run(args or self.command(), capture_output=True, timeout=15)

    def test_deadline_and_final_report_confirmed_separately(self):
        result = self.run_native()
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(row) for row in (self.data / 'samples.jsonl').read_bytes().splitlines()]
        samples = [row for row in rows if row['type'] == 'sample']
        self.assertGreaterEqual(len(samples), 3)
        end = next(row for row in rows if row['type'] == 'end')
        self.assertEqual(end['reason'], 'deadline')
        self.assertLess(end['ts_ms'] - rows[0]['deadline_unix_ms'], 2000)
        completion = json.loads((self.data / 'completion.json').read_bytes())
        self.assertTrue(completion['sampling_end_written'])
        self.assertTrue(completion['report_generated'])
        self.assertEqual(completion['report_exit_code'], 0)
        self.assertEqual(json.loads((self.data / 'manifest.json').read_bytes())['status'], 'Completed: deadline')

    def test_stop_request_only_stops_observer(self):
        process = subprocess.Popen(self.command(seconds=10), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ready = time.monotonic() + 3
            while not (self.data / 'samples.jsonl').exists() and time.monotonic() < ready:
                time.sleep(.05)
            (self.data / 'stop.request').touch()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            completion = json.loads((self.data / 'completion.json').read_bytes())
            self.assertEqual(completion['sampling_reason'], 'requested')
            self.assertTrue(completion['report_generated'])
        finally:
            if process.poll() is None:
                process.kill()  # This test-created observer only, never Claude.
                process.communicate()

    def test_failed_report_is_failure_even_when_sampling_finishes(self):
        result = self.run_native(self.command(report=self.base / 'missing-report.py'))
        self.assertEqual(result.returncode, 70)
        completion = json.loads((self.data / 'completion.json').read_bytes())
        self.assertEqual(completion['sampling_reason'], 'deadline')
        self.assertFalse(completion['report_generated'])
        self.assertNotEqual(completion['report_exit_code'], 0)

    def test_observer_and_helpers_never_counted_as_claude(self):
        # Synthetic executable alias exercises exclusion even when the observer
        # itself would otherwise be selected as a Claude root.
        alias = self.base / 'claude.exe'
        shutil.copyfile(BINARY, alias)
        args = self.command()
        args[0] = str(alias)
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0)
            rows = [json.loads(row) for row in (self.data / 'samples.jsonl').read_bytes().splitlines()]
            for row in rows:
                if row['type'] == 'sample':
                    self.assertNotIn(process.pid, [group['root_pid'] for group in row['processes']])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_existing_samples_never_overwritten(self):
        target = self.data / 'samples.jsonl'
        target.write_bytes(b'SYNTHETIC_SENTINEL')
        self.assertEqual(self.run_native().returncode, 73)
        self.assertEqual(target.read_bytes(), b'SYNTHETIC_SENTINEL')

    def test_existing_hardlinked_helper_log_never_appended(self):
        target = self.base / 'sentinel'
        target.write_bytes(b'SYNTHETIC_SENTINEL')
        os.link(target, self.data / 'helper.log')
        self.assertEqual(self.run_native().returncode, 73)
        self.assertEqual(target.read_bytes(), b'SYNTHETIC_SENTINEL')

    def test_linked_ancestor_rejected_before_output(self):
        linked = self.base / 'linked'
        try:
            linked.symlink_to(self.data, target_is_directory=True)
        except OSError:
            self.skipTest('Windows symlink privilege unavailable')
        self.assertEqual(self.run_native(self.command(data=linked)).returncode, 73)
        self.assertFalse((self.data / 'samples.jsonl').exists())

    def test_numeric_arguments_reject_signs_and_overflow(self):
        for value in ('-1', '+500', '18446744073709551616', '1.5'):
            args = self.command()
            args[args.index('--interval-ms') + 1] = value
            self.assertEqual(self.run_native(args).returncode, 64)
        self.assertFalse((self.data / 'samples.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
