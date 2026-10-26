"""Small stdlib file fixtures; no native processes or memory-pressure loads."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / 'examples' / 'checkpointed_hash.py'
spec = importlib.util.spec_from_file_location('checkpointed_hash', SCRIPT)
hashapp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hashapp)


class CheckpointedHashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='headroom-hash-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.files = [self.base / 'a.bin', self.base / 'b.bin']
        self.files[0].write_bytes(b'alpha' * 1000)
        self.files[1].write_bytes(b'beta' * 2000)
        self.state = self.base / 'state'
        self.control = self.base / 'control'
        self.control.mkdir()
        self.request = f'headroom-stop-v1\n{uuid.uuid4()}\n'.encode()

    def stop_after_first(self):
        def request(control):
            checkpoint = self.state / 'checkpoint.json'
            if checkpoint.exists() and len(json.loads(checkpoint.read_bytes())['completed']) == 1:
                (self.control / 'stop.request').write_bytes(self.request)
                return self.request
            return None
        with mock.patch.object(hashapp, 'stop_request', side_effect=request):
            self.assertEqual(hashapp.run(self.files, self.state, control=self.control,
                                         chunk_bytes=1024), 'checkpointed')

    def test_normal_deterministic_checksums_and_input_order(self):
        self.assertEqual(hashapp.run(self.files, self.state), 'completed')
        other = self.base / 'other'
        hashapp.run(list(reversed(self.files)), other, chunk_bytes=1024)
        self.assertEqual((self.state / 'manifest.json').read_bytes(),
                         (other / 'manifest.json').read_bytes())
        manifest = json.loads((self.state / 'manifest.json').read_bytes())
        self.assertEqual([row['sha256'] for row in manifest['files']],
                         [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.files])

    def test_resume_identical_without_reading_completed_content(self):
        normal = self.base / 'normal'
        hashapp.run(self.files, normal)
        self.stop_after_first()
        original = Path.open
        def opened(path, mode='r', *args, **kwargs):
            if path == self.files[0] and mode == 'rb':
                raise AssertionError('completed input content was read again')
            return original(path, mode, *args, **kwargs)
        with mock.patch.object(Path, 'open', opened):
            resumed = self.base / 'resumed'
            self.assertEqual(hashapp.run(self.files, resumed, resume=self.state), 'completed')
        self.assertEqual((normal / 'manifest.json').read_bytes(), (resumed / 'manifest.json').read_bytes())
        self.assertFalse((self.state / 'manifest.json').exists())

    def test_exact_ack_follows_flushed_checkpoint_publication(self):
        (self.control / 'stop.request').write_bytes(self.request)
        atomic = hashapp.atomic
        def checked(path, data, **kwargs):
            if path.name == 'stop.ack':
                checkpoint = json.loads((self.state / 'checkpoint.json').read_bytes())
                self.assertEqual(checkpoint['stop_nonce'], self.request.split(b'\n')[1].decode())
                self.assertEqual(data, self.request)
            return atomic(path, data, **kwargs)
        with mock.patch.object(hashapp, 'atomic', side_effect=checked), \
             mock.patch.object(hashapp.os, 'fsync', wraps=os.fsync) as sync:
            self.assertEqual(hashapp.run(self.files, self.state, control=self.control), 'checkpointed')
        self.assertGreaterEqual(sync.call_count, 3)
        self.assertEqual((self.control / 'stop.ack').read_bytes(), self.request)

    def test_changed_completed_input_refused(self):
        self.stop_after_first()
        self.files[0].write_bytes(b'changed completed file')
        output = self.base / 'next'
        with self.assertRaisesRegex(hashapp.Refusal, 'metadata changed'):
            hashapp.run(self.files, output, resume=self.state)
        self.assertFalse(output.exists())

    def test_changed_pending_input_refused(self):
        self.stop_after_first()
        self.files[1].write_bytes(b'changed pending file')
        with self.assertRaisesRegex(hashapp.Refusal, 'metadata changed'):
            hashapp.run(self.files, self.base / 'next', resume=self.state)

    def test_mid_file_stop_restarts_only_unfinished_file(self):
        calls = 0
        def request(control):
            nonlocal calls
            calls += 1
            if calls >= 3:  # One chunk was hashed; no file has completed.
                return self.request
            return None
        with mock.patch.object(hashapp, 'stop_request', side_effect=request):
            self.assertEqual(hashapp.run(self.files, self.state, control=self.control,
                                         chunk_bytes=1024), 'checkpointed')
        checkpoint = json.loads((self.state / 'checkpoint.json').read_bytes())
        self.assertEqual(checkpoint['completed'], [])
        next_state = self.base / 'next'
        hashapp.run(self.files, next_state, resume=self.state)
        normal = self.base / 'normal'
        hashapp.run(self.files, normal)
        self.assertEqual((normal / 'manifest.json').read_bytes(),
                         (next_state / 'manifest.json').read_bytes())

    def test_changed_request_and_existing_ack_never_overwritten(self):
        first, second = self.request, f'headroom-stop-v1\n{uuid.uuid4()}\n'.encode()
        with mock.patch.object(hashapp, 'stop_request', side_effect=[first, second]):
            with self.assertRaisesRegex(hashapp.Refusal, 'request changed'):
                hashapp.run(self.files, self.state, control=self.control)
        self.assertFalse((self.control / 'stop.ack').exists())
        (self.control / 'stop.request').write_bytes(first)
        (self.control / 'stop.ack').write_bytes(b'keep existing ack')
        with self.assertRaisesRegex(hashapp.Refusal, 'already exists'):
            hashapp.run(self.files, self.base / 'other', control=self.control)
        self.assertEqual((self.control / 'stop.ack').read_bytes(), b'keep existing ack')

    def test_empty_file_and_completed_checkpoint_resume(self):
        self.files[0].write_bytes(b'')
        hashapp.run(self.files, self.state)
        other = self.base / 'other'
        hashapp.run(self.files, other, resume=self.state)
        self.assertEqual((self.state / 'manifest.json').read_bytes(), (other / 'manifest.json').read_bytes())
        manifest = json.loads((other / 'manifest.json').read_bytes())
        self.assertEqual(manifest['files'][0]['sha256'], hashlib.sha256(b'').hexdigest())

    def test_changed_input_set_refused(self):
        self.stop_after_first()
        with self.assertRaisesRegex(hashapp.Refusal, 'input set'):
            hashapp.run(self.files[:1], self.base / 'next', resume=self.state)

    def test_transient_windows_request_access_error_retries_exact_read(self):
        (self.control / 'stop.request').write_bytes(self.request)
        locked = PermissionError(13, 'synthetic CRT sharing error')
        with mock.patch.object(hashapp.os, 'name', 'nt'), \
             mock.patch.object(hashapp, 'read_small', side_effect=[locked, self.request]), \
             mock.patch.object(hashapp.time, 'sleep') as sleep:
            self.assertEqual(hashapp.stop_request(self.control), self.request)
            sleep.assert_called_once_with(0.01)
        self.assertFalse((self.control / 'stop.ack').exists())

    def test_permanent_request_access_failure_never_acknowledges(self):
        (self.control / 'stop.request').write_bytes(self.request)
        locked = PermissionError(13, 'synthetic CRT sharing error')
        with mock.patch.object(hashapp.os, 'name', 'nt'), \
             mock.patch.object(hashapp, 'read_small', side_effect=locked), \
             mock.patch.object(hashapp.time, 'monotonic', side_effect=[0, 0.1, 0.21]), \
             mock.patch.object(hashapp.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError):
                hashapp.stop_request(self.control)
            sleep.assert_called_once()
        self.assertFalse((self.control / 'stop.ack').exists())

    def test_malformed_or_corrupt_checkpoints_refused(self):
        self.stop_after_first()
        path = self.state / 'checkpoint.json'
        original = path.read_bytes()
        for data in (b'{', b'{}', b'{"version":1,"version":1}', original[:-8],
                     original.replace(b'"version":1', b'"version":true')):
            with self.subTest(data=data[:30]):
                path.write_bytes(data)
                with self.assertRaises((ValueError, OSError)):
                    hashapp.run(self.files, self.base / 'next', resume=self.state)
                self.assertFalse((self.base / 'next').exists())

    def test_invalid_progress_even_with_recomputed_integrity_refused(self):
        self.stop_after_first()
        path = self.state / 'checkpoint.json'
        source = json.loads(path.read_bytes())
        for completed in (['wrong'], ['0' * 64] * 3, 'not-a-list', [True]):
            value = hashapp.checkpoint(source['inputs'], completed)
            path.write_bytes(hashapp.encode(value))
            with self.assertRaisesRegex(hashapp.Refusal, 'progress|checksum'):
                hashapp.run(self.files, self.base / 'next', resume=self.state)

    def test_malformed_requests_never_ack(self):
        for index, body in enumerate((b'', b'headroom-stop-v1\nwrong\n', self.request[:-1],
                                      self.request + b'extra', self.request.replace(b'\n', b'\r\n'))):
            with self.subTest(body=body):
                (self.control / 'stop.request').write_bytes(body)
                with self.assertRaisesRegex(hashapp.Refusal, 'malformed'):
                    hashapp.run(self.files, self.base / f'bad-{index}', control=self.control)
                self.assertFalse((self.control / 'stop.ack').exists())

    def test_failed_checkpoint_flush_cannot_ack(self):
        (self.control / 'stop.request').write_bytes(self.request)
        with mock.patch.object(hashapp.os, 'fsync', side_effect=OSError('flush failed')):
            with self.assertRaises(OSError):
                hashapp.run(self.files, self.state, control=self.control)
        self.assertFalse((self.control / 'stop.ack').exists())

    def test_duplicate_input_hardlink_and_existing_state_refused(self):
        with self.assertRaisesRegex(hashapp.Refusal, 'duplicate'):
            hashapp.run([self.files[0], self.files[0]], self.state)
        alias = self.base / 'alias'
        os.link(self.files[0], alias)
        with self.assertRaisesRegex(hashapp.Refusal, 'aliased'):
            hashapp.run([self.files[0], alias], self.state)
        with self.assertRaisesRegex(hashapp.Refusal, 'must be new'):
            hashapp.run(self.files, self.files[0])
        self.assertFalse(self.state.exists())

    def test_symlink_input_refused_if_supported(self):
        link = self.base / 'link'
        try:
            link.symlink_to(self.files[0])
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaisesRegex(hashapp.Refusal, 'symlink/reparse'):
            hashapp.run([link], self.state)

    def test_input_mutation_while_hashing_refuses_manifest(self):
        original = hashapp.stop_request
        calls = 0
        def changed(control):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.files[0].write_bytes(b'changed')
            return original(control)
        with mock.patch.object(hashapp, 'stop_request', side_effect=changed):
            with self.assertRaisesRegex(hashapp.Refusal, 'changed'):
                hashapp.run(self.files, self.state, chunk_bytes=1024)
        self.assertFalse((self.state / 'manifest.json').exists())

    def test_cli_and_side_effects_are_confined(self):
        before = {p: p.read_bytes() for p in self.files}
        env = {k: v for k, v in os.environ.items() if k != 'HEADROOM_CONTROL_DIR'}
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state),
                                 '--', *map(str, self.files)], env=env,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'completed')
        self.assertEqual(before, {p: p.read_bytes() for p in self.files})
        self.assertEqual({p.name for p in self.state.iterdir()}, {'checkpoint.json', 'manifest.json'})
        bad = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.base / 'bad'),
                              '--chunk-bytes', '0', str(self.files[0])], env=env,
                             capture_output=True, timeout=10)
        self.assertEqual(bad.returncode, 73)
        self.assertFalse((self.base / 'bad').exists())


if __name__ == '__main__':
    unittest.main()
