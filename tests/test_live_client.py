"""Small offline fixtures only; never start native owners or workloads."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import headroom
import headroom_live as live


class LiveClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        for name in ('inbox', 'processed', 'receipts'):
            (self.state / name).mkdir()
        self.run = str(uuid.uuid4())
        self.request = str(uuid.uuid4())
        self.settings = dict(budget_mib=512, hard_mib=1024)
        self.session = dict(version=1, run_id=self.run, max_jobs=64,
                            settings={**live.DEFAULTS, **self.settings})
        self.write_session()
        self.exe = self.root / 'fixture.exe'
        self.exe.write_bytes(b'offline fixture, never executed')
        self.job = dict(id='task-one', claim_mib=64, hard_mib=128,
                        argv=[str(self.exe), 'a b'])

    def write_session(self):
        (self.state / 'session.json').write_text(json.dumps(self.session), encoding='utf-8')

    def receipt(self, **changes):
        value = dict(version=1, run_id=self.run, request_id=self.request,
                     task='task-one', status='accepted', code=0, ordinal=1)
        value.update(changes)
        (self.state / 'receipts' / f'{self.request}.json').write_text(
            json.dumps(value), encoding='utf-8')
        return value

    def send(self, **kwargs):
        return live.submit(self.state, self.job, self.request, wait_seconds=0, **kwargs)

    def test_session_header_and_defaults(self):
        data = live.compile_session(self.settings)
        self.assertEqual(len(data), headroom.HEADER.size)
        self.assertEqual(headroom.HEADER.unpack(data),
                         (b'HRFLEET1', 0, 512 * headroom.MIB, 1024 * headroom.MIB,
                          256 * headroom.MIB, 500, 2000, 60000))

    def test_settings_reject_unknown_missing_and_all_bounds(self):
        cases = [dict(jobs=[]), dict(budget_mib=True), dict(budget_mib=0),
                 dict(hard_mib=512), dict(reserve_mib=-1), dict(sustain_ms=99),
                 dict(grace_ms=60001), dict(timeout_ms=86400001)]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(live.LiveError):
                live.compile_session({**self.settings, **change})
        with self.assertRaises(live.LiveError):
            live.compile_session({})

    def test_submit_three_exact_files_and_canonical_json(self):
        original = copy.deepcopy(self.job)
        result = self.send()
        folder = self.state / 'inbox' / f'{self.request}.ready'
        self.assertEqual({p.name for p in folder.iterdir()}, {'target', 'plan.hrp', 'plan.json'})
        self.assertEqual((folder / 'target').read_bytes(),
                         f'headroom-submit-v1\n{self.run}\n{self.request}\n'.encode())
        plan = json.loads((folder / 'plan.json').read_bytes())
        self.assertEqual(plan['jobs'][0]['argv'][0], str(self.exe.resolve()))
        self.assertEqual((folder / 'plan.hrp').read_bytes(), headroom.compile_plan(plan))
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['run_id'], self.run)
        self.assertEqual(result['request_id'], self.request)
        self.assertEqual(self.job, original)

    def test_relative_and_resume_executable_canonicalization(self):
        self.job.update(allow_stop=True, max_resumes=1, resume_argv=[str(self.exe)])
        relative = os.path.relpath(self.exe, Path.cwd())
        self.job['argv'][0] = relative
        self.send()
        plan = json.loads((self.state / 'inbox' / f'{self.request}.ready' / 'plan.json').read_bytes())
        for key in ('argv', 'resume_argv'):
            self.assertEqual(plan['jobs'][0][key][0], str(self.exe.resolve()))

    def test_recompile_mismatch_refused_before_publication(self):
        compiler = headroom.compile_plan
        calls = []
        def changing(plan):
            calls.append(1)
            return compiler(plan) + (b'x' if len(calls) == 2 else b'')
        with patch.object(headroom, 'compile_plan', side_effect=changing):
            with self.assertRaises(live.LiveError):
                self.send()
        self.assertEqual(list((self.state / 'inbox').iterdir()), [])

    def test_idempotent_inbox_and_processed_no_mutation(self):
        self.send()
        inbox = self.state / 'inbox' / f'{self.request}.ready'
        before = {p.name: p.read_bytes() for p in inbox.iterdir()}
        self.send()
        processed = self.state / 'processed' / self.request
        inbox.rename(processed)
        (processed / 'validated.hrp').write_bytes(b'native evidence')
        self.send()
        self.assertFalse(inbox.exists())
        self.assertEqual({name: (processed / name).read_bytes() for name in before}, before)

    def test_duplicate_uuid_different_each_payload_refused(self):
        for name in ('target', 'plan.hrp', 'plan.json'):
            with self.subTest(name=name):
                request = str(uuid.uuid4())
                live.submit(self.state, self.job, request, 0)
                file = self.state / 'inbox' / f'{request}.ready' / name
                file.write_bytes(b'different')
                with self.assertRaises(live.LiveError):
                    live.submit(self.state, self.job, request, 0)
                self.assertEqual(file.read_bytes(), b'different')

    def test_receipt_missing_and_unknown_status_never_success(self):
        self.assertEqual(live.read_receipt(self.state, self.request)['status'], 'pending')
        self.receipt(status='completed')
        with self.assertRaises(live.LiveError):
            live.read_receipt(self.state, self.request)

    def test_accepted_and_rejected_receipts(self):
        accepted = self.receipt()
        self.assertEqual(self.send(), accepted)
        for code in (64, 73, 75):
            rejected = self.receipt(status='rejected', code=code, ordinal=0, task='')
            self.assertEqual(live.read_receipt(self.state, self.request), rejected)

    def test_receipt_schema_identity_and_semantics_strict(self):
        for changes in (dict(extra=1), dict(version=True), dict(run_id=str(uuid.uuid4())),
                        dict(request_id=str(uuid.uuid4())), dict(task='con'),
                        dict(task=''), dict(code=True), dict(code=73), dict(ordinal=0),
                        dict(ordinal=65), dict(status='rejected', ordinal=0, code=0)):
            with self.subTest(changes=changes):
                self.receipt(**changes)
                with self.assertRaises(live.LiveError):
                    live.read_receipt(self.state, self.request)

    def test_invalid_session_exact_schema_and_settings(self):
        valid = copy.deepcopy(self.session)
        for changes in (dict(version=2), dict(max_jobs=True), dict(max_jobs=63),
                        dict(extra=1), dict(run_id=self.run.upper()),
                        dict(settings=self.settings)):
            self.session = {**valid, **changes}
            self.write_session()
            with self.subTest(changes=changes), self.assertRaises(live.LiveError):
                self.send()

    def test_invalid_ids_waits_and_job_publish_nothing(self):
        for identity in ('../bad', self.request.upper(), self.request.replace('-', ''), 5):
            with self.subTest(identity=identity), self.assertRaises(live.LiveError):
                live.submit(self.state, self.job, identity, 0)
        for seconds in (-1, 31, float('nan'), float('inf'), True, '0'):
            with self.subTest(seconds=seconds), self.assertRaises(live.LiveError):
                live.submit(self.state, self.job, self.request, seconds)
        self.job['claim_mib'] = 513
        with self.assertRaises(live.LiveError):
            self.send()
        self.assertEqual(list((self.state / 'inbox').iterdir()), [])

    def test_dot_relative_state_allowed_traversal_remote_alias_refused(self):
        # Use a cwd containing the fixture so the allowed example has no '..'.
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(live.read_receipt('.' + os.sep + 'state', self.request)['status'], 'pending')
        finally:
            os.chdir(previous)
        for state in (str(self.state / '..' / 'state'), r'\\server\share',
                      str(self.state) + '.', str(self.state / 'nul')):
            with self.subTest(state=state), self.assertRaises(live.LiveError):
                live.read_receipt(state, self.request)

    def test_size_caps_and_duplicate_json_keys(self):
        file = self.state / 'receipts' / f'{self.request}.json'
        file.write_bytes(b' ' * 4097)
        with self.assertRaises(live.LiveError):
            live.read_receipt(self.state, self.request)
        file.write_bytes(b'{"version":1,"version":1}')
        with self.assertRaises(live.LiveError):
            live.read_receipt(self.state, self.request)
        with patch.object(live, 'MAX_PLAN_BYTES', 1), self.assertRaises(live.LiveError):
            self.send()
        with patch.object(live, 'MAX_JSON_BYTES', 1), self.assertRaises(live.LiveError):
            self.send()

    def test_reparse_state_rejected(self):
        actual = Path.lstat
        def reparse(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            if path == self.state:
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, 'lstat', reparse), self.assertRaises(live.LiveError):
            self.send()

    def test_failed_publication_cleans_only_own_staging(self):
        peer = self.state / 'inbox' / '.peer'
        peer.mkdir()
        (peer / 'keep').write_bytes(b'peer')
        with patch.object(live.os, 'rename', side_effect=FileExistsError('race')):
            with self.assertRaises(live.LiveError):
                self.send()
        self.assertEqual(list((self.state / 'inbox').iterdir()), [peer])
        self.assertEqual((peer / 'keep').read_bytes(), b'peer')

    def test_seal_exact_idempotent_and_mismatch_refused(self):
        result = live.seal(self.state)
        self.assertEqual(result['status'], 'seal_requested')
        self.assertEqual(live.seal(self.state), result)
        file = self.state / 'seal.request'
        self.assertEqual(file.read_bytes(), f'headroom-seal-v1\n{self.run}\n'.encode())
        file.write_bytes(b'other')
        with self.assertRaises(live.LiveError):
            live.seal(self.state)
        self.assertEqual(file.read_bytes(), b'other')

    def test_concurrent_same_uuid_refused_without_second_publication(self):
        entered, proceed = threading.Event(), threading.Event()
        existing = live._existing
        def blocked(*args):
            entered.set()
            if not proceed.wait(2):
                raise AssertionError('test publisher did not resume')
            return existing(*args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(live, '_existing', side_effect=blocked):
                first = pool.submit(self.send)
                try:
                    self.assertTrue(entered.wait(2))
                    other = {**self.job, 'priority': 1}
                    with self.assertRaises(live.LiveError):
                        live.submit(self.state, other, self.request, 0)
                finally:
                    proceed.set()
                self.assertEqual(first.result(timeout=2)['status'], 'pending')
        self.assertEqual(len(list((self.state / 'inbox').iterdir())), 1)

    def test_wait_observes_receipt_and_is_bounded(self):
        pending = live._pending(self.session, self.request)
        accepted = self.receipt()
        with patch.object(live, '_receipt', side_effect=[pending, accepted]), \
                patch.object(live.time, 'sleep') as sleep:
            result = live.submit(self.state, self.job, self.request, 0.2)
        self.assertEqual(result, accepted)
        self.assertLessEqual(sleep.call_args.args[0], 0.1)
        with patch.object(live, '_receipt', return_value=pending), \
                patch.object(live.time, 'monotonic', side_effect=[0, 31]):
            self.assertEqual(live.submit(self.state, self.job, self.request, 30), pending)

    def test_cli_init_no_overwrite_and_receipt_exit_codes(self):
        source = self.root / 'settings.json'
        source.write_text(json.dumps(self.settings), encoding='utf-8')
        output = self.root / 'settings.hrs'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(live.main(['init', str(source), str(output)]), 0)
            self.assertEqual(live.main(['init', str(source), str(output)]), 73)
            args = ['receipt', str(self.state), self.request]
            self.assertEqual(live.main(args), 75)
            self.receipt()
            self.assertEqual(live.main(args), 0)
            self.receipt(status='rejected', code=75, ordinal=0)
            self.assertEqual(live.main(args), 73)
            self.assertEqual(live.main(['receipt', str(self.state), 'bad']), 64)
        self.assertEqual(output.read_bytes(), live.compile_session(self.settings))


if __name__ == '__main__':
    unittest.main()
