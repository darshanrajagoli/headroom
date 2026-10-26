"""Offline log snapshots only: bounded data, not task completion or authority."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='headroom-log-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'
        self.folder = self.state / 'task' / 'attempt-1'
        self.folder.mkdir(parents=True)

    def capture(self, stdout=b'hello\n', stderr=b'', discarded=0):
        (self.folder / 'stdout.log').write_bytes(stdout)
        (self.folder / 'stderr.log').write_bytes(stderr)
        value = {'version': 1, 'limit_bytes': 1048576,
                 'stdout': {'retained_bytes': len(stdout), 'discarded_bytes': discarded, 'eof': True},
                 'stderr': {'retained_bytes': len(stderr), 'discarded_bytes': 0, 'eof': True}}
        self.metadata(value)
        return value

    def metadata(self, value):
        (self.folder / 'output.json').write_text(json.dumps(value), encoding='utf-8')

    def read(self, **kwargs):
        return headroom.read_log(self.state, 'task', **kwargs)

    def refused(self, result):
        self.assertIsNotNone(result['error'])
        self.assertEqual(result['text'], '')
        self.assertFalse(result['metadata_confirmed'])
        self.assertFalse(result['process_liveness_verified'])

    def test_exact_streams_and_empty_capture(self):
        self.capture(b'hello\n', b'error\n')
        for stream, expected in [('stdout', 'hello\n'), ('stderr', 'error\n')]:
            result = self.read(stream=stream)
            self.assertIsNone(result['error'], result)
            self.assertEqual(result['text'], expected)
            self.assertTrue(result['metadata_confirmed'])
            self.assertFalse(result['capture_truncated'])
            self.assertNotIn('status', result)
        self.capture(b'')
        self.assertEqual(self.read()['returned_bytes'], 0)

    def test_tail_is_bounded_by_raw_bytes(self):
        self.capture(b'a' * 80000)
        result = self.read(tail_bytes=65536)
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['returned_bytes'], 65536)
        self.assertTrue(result['tail_truncated'])
        self.assertEqual(self.read(tail_bytes=0)['text'], '')

    def test_unicode_invalid_utf8_and_control_text_remain_data(self):
        body = 'नमस्ते 🌱'.encode() + b'\xff\x00\x1b[31m\n'
        self.capture(body)
        result = self.read()
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['text'], body.decode('utf-8', errors='replace'))
        self.assertEqual(result['returned_bytes'], len(body))

    def test_missing_metadata_is_unknown_not_success(self):
        (self.folder / 'stdout.log').write_bytes(b'partial')
        result = self.read()
        self.assertIsNone(result['error'], result)
        self.assertFalse(result['metadata_confirmed'])
        self.assertIsNone(result['discarded_bytes'])
        self.assertIsNone(result['capture_truncated'])
        self.assertIsNone(result['eof'])
        self.assertFalse(result['process_liveness_verified'])

    def test_capture_truncation_counts_are_distinct_from_tail(self):
        self.capture(b'x' * 1048576, discarded=1234)
        result = self.read(tail_bytes=16)
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['discarded_bytes'], 1234)
        self.assertTrue(result['capture_truncated'])
        self.assertTrue(result['tail_truncated'])
        self.assertEqual(result['text'], 'x' * 16)

    def test_invalid_task_attempt_stream_and_tail_refused(self):
        self.capture()
        for task in ('../task', 'con', 'task/other', '', 'UPPER', True, None):
            with self.subTest(task=task):
                self.refused(headroom.read_log(self.state, task))
        for key, values in {'attempt': (0, 5, True, 1.0),
                            'stream': ('stdin', '../stdout', None, []),
                            'tail_bytes': (-1, 65537, True, 1.0)}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.refused(self.read(**{key: value}))

    def test_leading_dot_local_path_and_cli_work(self):
        self.capture(b'visible\x1b[31m\n')
        relative = '.\\state' if os.name == 'nt' else './state'
        result = subprocess.run([sys.executable, str(ROOT / 'headroom.py'), 'logs',
            relative, 'task', '--tail-bytes', '32'], cwd=self.state.parent,
            capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['text'], 'visible\x1b[31m\n')
        self.assertNotIn(b'\x1b', result.stdout)

    def test_traversal_and_device_state_paths_refused(self):
        self.capture()
        for path in (str(self.state / '..' / 'state'), '\\\\server\\share',
                     str(self.state) + ':stream', str(self.state) + ' '):
            with self.subTest(path=path):
                self.refused(headroom.read_log(path, 'task'))

    def test_missing_selected_log_refused(self):
        self.capture()
        (self.folder / 'stdout.log').unlink()
        self.refused(self.read())

    def test_oversized_log_and_metadata_refused(self):
        self.capture(b'x' * 1048577)
        self.refused(self.read())
        self.capture()
        (self.folder / 'output.json').write_bytes(b' ' * 4097)
        self.refused(self.read())

    def test_malformed_metadata_and_duplicate_keys_refused(self):
        self.capture()
        for value in (b'{', b'[]', b'{"version":1,"version":1}', b'NaN'):
            with self.subTest(value=value):
                (self.folder / 'output.json').write_bytes(value)
                self.refused(self.read())

    def test_metadata_schema_sizes_and_counters_checked(self):
        original = self.capture()
        mutations = [('version', True), ('limit_bytes', 1), ('extra', 0)]
        for key, value in mutations:
            bad = copy.deepcopy(original)
            bad[key] = value
            self.metadata(bad)
            self.refused(self.read())
        for stream in ('stdout', 'stderr'):
            for key, values in {'retained_bytes': (-1, True, 999),
                                'discarded_bytes': (-1, True, 1, 2**64),
                                'eof': (False, 1, None)}.items():
                for value in values:
                    with self.subTest(stream=stream, key=key, value=value):
                        bad = copy.deepcopy(original)
                        bad[stream][key] = value
                        self.metadata(bad)
                        self.refused(self.read())

    def test_metadata_requires_both_streams(self):
        self.capture()
        (self.folder / 'stderr.log').unlink()
        self.refused(self.read())

    def test_hardlinked_or_symlinked_evidence_refused(self):
        self.capture()
        target = self.folder / 'stdout.log'
        alias = self.state.parent / 'alias'
        os.link(target, alias)
        self.refused(self.read())
        alias.unlink()
        target.unlink()
        target.symlink_to(self.folder / 'stderr.log')
        self.refused(self.read())

    def test_changed_snapshot_refused_without_returning_text(self):
        self.capture()
        original = headroom._log_open
        def changed(stack, path, *args, **kwargs):
            snapshot = original(stack, path, *args, **kwargs)
            if path.name == 'stderr.log':
                with (self.folder / 'stdout.log').open('ab') as stream:
                    stream.write(b'changed')
            return snapshot
        with mock.patch.object(headroom, '_log_open', side_effect=changed):
            self.refused(self.read())

    def test_reader_never_starts_processes_or_modifies_files(self):
        self.capture()
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        with mock.patch.object(headroom.subprocess, 'Popen', side_effect=AssertionError('not allowed')):
            self.assertIsNone(self.read()['error'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.folder.iterdir()})


if __name__ == '__main__':
    unittest.main()
