"""Offline recovery safety cases, plus small Windows lease/native checks.

No tests signal a process or an old Job Object. The optional native case uses
an already-built coordinator and one short sleeping interpreter, then waits for
normal exit. All other cases use synthetic state and a query/lease adapter.
"""

from contextlib import contextmanager
import copy
import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom
import recover


def save(path, payload):
    path.write_bytes(payload if isinstance(payload, bytes) else
                     json.dumps(payload).encode('utf-8'))


class FakeSafety:
    def __init__(self, busy=False, active=False):
        self.busy, self.active, self.held, self.checks = busy, active, False, 0
        self.name = None

    @contextmanager
    def lease(self, path):
        if self.busy:
            raise recover.RecoveryError('LIVE/BUSY owner lease')
        self.held = True
        try:
            yield
        finally:
            self.held = False

    @contextmanager
    def inactive_job(self, name):
        self.name = name
        def check():
            if not self.held:
                raise AssertionError('owner lease released prematurely')
            self.checks += 1
            if self.active:
                raise recover.RecoveryError('outer job still has active processes')
        check()
        yield check


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='headroom-recovery-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / 'old'
        self.state.mkdir()
        self.input = self.base / 'plan.json'
        self.output = self.base / 'recovery.hrp'
        self.run = str(uuid.uuid4())
        self.safety = FakeSafety()
        # Existing .exe required by the compiler; it is never executed in the
        # synthetic cases. Windows uses its ordinary installed interpreter.
        self.exe = Path(sys.executable)
        if self.exe.suffix.lower() != '.exe':
            self.exe = self.base / 'fixture.exe'
            save(self.exe, b'not executed')
        self.plan = {'budget_mib': 64, 'hard_mib': 128, 'reserve_mib': 0,
                     'sustain_ms': 500, 'grace_ms': 300, 'timeout_ms': 5000,
                     'jobs': [self.job('task')]}
        self.fleet_records = []
        self.attempt_records = {}
        self.nonces = {}
        self.reset_state()

    def job(self, name, **changes):
        job = {'id': name, 'priority': 31, 'claim_mib': 16, 'hard_mib': 96,
               'allow_stop': True, 'argv': [str(self.exe), '-c', 'pass'],
               'max_resumes': 2, 'resume_argv': [str(self.exe), '-c', 'pass', 'resume']}
        job.update(changes)
        return job

    def reset_state(self):
        save(self.input, self.plan)
        save(self.state / 'plan.hrp', headroom.compile_plan(self.plan))
        save(self.state / 'owner.lock', b'')
        save(self.state / 'working-directory.hrc',
             b'HRCWD001' + str(self.base).encode('utf-16le'))
        save(self.state / 'owner.json', {'version': 1, 'run_id': self.run,
                                       'job_name': 'Local\\Headroom-' + self.run})
        self.fleet_records = []
        self.fleet('prepared')
        for job in self.plan['jobs']:
            self.fleet('queued', job['id'], 0)

    def fleet(self, event, task='', number=0, code=0, **changes):
        record = {'version': 1, 'run_id': self.run, 'event': event,
                  'elapsed_ms': len(self.fleet_records), 'task': task,
                  'priority': next((j['priority'] for j in self.plan['jobs']
                                    if j['id'] == task), 0), 'attempt': number,
                  'total_commit_bytes': 0, 'available_bytes': 1024**3, 'code': code}
        record.update(changes)
        self.fleet_records.append(record)
        self.flush_fleet()

    def flush_fleet(self):
        save(self.state / 'fleet.jsonl', b''.join(
            (json.dumps(record) + '\n').encode() for record in self.fleet_records))

    def attempt(self, task, number, event, code=0, active=1, **changes):
        key = (task, number)
        directory = self.state / task / f'attempt-{number}'
        directory.mkdir(parents=True, exist_ok=True)
        nonce = self.nonces.setdefault(key, str(uuid.uuid4()))
        records = self.attempt_records.setdefault(key, [])
        record = {'version': 1, 'run_id': nonce, 'event': event,
                  'elapsed_ms': len(records), 'job_commit_bytes': 0,
                  'active_processes': active, 'code': code}
        record.update(changes)
        records.append(record)
        save(directory / 'events.jsonl', b''.join(
            (json.dumps(row) + '\n').encode() for row in records))
        return directory

    def start(self, task='task', number=1):
        self.attempt(task, number, 'prepared', active=0)
        self.attempt(task, number, 'contained', code=101)
        self.fleet('started', task, number, code=101)

    def checkpoint(self, task='task', number=1, *, acknowledged=True, stopped=True):
        self.attempt(task, number, 'checkpoint_intent', code=125)
        self.fleet('pressure_intent', task, number, code=125)
        directory = self.state / task / f'attempt-{number}'
        data = f'headroom-stop-v1\n{self.nonces[task, number]}\n'.encode()
        save(directory / 'stop.request', data)
        save(directory / 'stop.ack', data)
        if acknowledged:
            self.attempt(task, number, 'checkpoint_acknowledged')
            self.fleet('checkpoint_acknowledged', task, number)
        if stopped:
            self.attempt(task, number, 'stopped', active=0)
            self.fleet('stopped', task, number)

    def complete(self, task='task', number=1, code=0):
        self.attempt(task, number, 'completed', active=0, code=code)
        self.fleet('completed', task, number, code=code)

    def run_recovery(self, **kwargs):
        return recover.prepare_recovery(self.input, self.state, self.output,
                                        safety=kwargs.pop('safety', self.safety), **kwargs)

    def refused(self, contains=None):
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.state / 'recovery-reserved.json').exists())
        if contains:
            reasons = str(report['error']) + str(report['blocked'])
            self.assertIn(contains, reasons)
        return report

    def test_never_started_queued_original_plan(self):
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(self.plan))
        reservation = json.loads((self.state / 'recovery-reserved.json').read_bytes())
        self.assertEqual(reservation['run_id'], self.run)
        self.assertEqual(reservation['output_path'], str(self.output))
        self.assertEqual(str(uuid.UUID(reservation['recovery_id'])), reservation['recovery_id'])
        self.assertEqual(report['eligible'][0]['mode'], 'original')
        self.assertFalse(self.safety.held)

    def test_saved_working_directory_survives_different_recovery_cwd(self):
        self.plan['jobs'][0]['argv'] = [str(self.exe), 'worker.py']
        self.reset_state()
        caller = self.base / 'caller'
        caller.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(caller)
            report = self.run_recovery()
        finally:
            os.chdir(previous)
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(report['working_directory'], str(self.base))

    def test_legacy_missing_working_directory_refuses_before_reservation(self):
        (self.state / 'working-directory.hrc').unlink()
        self.refused('working-directory.hrc')

    def test_corrupt_working_directory_metadata_refuses_before_reservation(self):
        valid = str(self.base).encode('utf-16le')
        for data in (b'', b'HRCWD000' + valid, b'HRCWD001', b'HRCWD001x',
                     b'HRCWD001\x00\xd8', b'HRCWD001' + valid + b'\0\0',
                     b'HRCWD001' + b'x' * 8192):
            with self.subTest(data=data[:20]):
                save(self.state / 'working-directory.hrc', data)
                self.refused()

    def test_nonlocal_noncanonical_or_missing_working_directory_refuses(self):
        for value in ('relative', 'C:relative', '\\\\server\\share',
                      str(self.base) + '\\..\\' + self.base.name,
                      str(self.base) + '\\.', str(self.base / 'missing'), str(self.input)):
            with self.subTest(value=value):
                save(self.state / 'working-directory.hrc', b'HRCWD001' + value.encode('utf-16le'))
                self.refused()

    def test_working_directory_metadata_is_rechecked_before_reservation(self):
        classify = recover._classify
        def changed(*args):
            result = classify(*args)
            save(self.state / 'working-directory.hrc', b'HRCWD001' + str(ROOT).encode('utf-16le'))
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('changed')

    def test_working_directory_identity_is_rechecked_before_reservation(self):
        directory = self.base / 'launch'
        directory.mkdir()
        save(self.state / 'working-directory.hrc', b'HRCWD001' + str(directory).encode('utf-16le'))
        classify = recover._classify
        def changed(*args):
            result = classify(*args)
            directory.rename(self.base / 'old-launch')
            directory.mkdir()
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('working directory changed')

    def test_hardlinked_working_directory_metadata_refuses(self):
        os.link(self.state / 'working-directory.hrc', self.base / 'cwd-alias')
        self.refused('singly linked')

    def test_started_without_checkpoint_blocked(self):
        self.start()
        self.refused('no exact checkpoint ack')

    def test_stop_resume_preserves_settings_and_consumes_one(self):
        self.start()
        self.checkpoint()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        expected = copy.deepcopy(self.plan)
        expected['jobs'][0]['argv'] = expected['jobs'][0]['resume_argv']
        expected['jobs'][0]['max_resumes'] = 1
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(expected))
        self.assertEqual(self.plan['jobs'][0]['max_resumes'], 2)

    def test_crash_after_ack_bytes_before_ack_events(self):
        self.start()
        self.checkpoint(acknowledged=False, stopped=False)
        self.assertEqual(self.run_recovery()['status'], 'prepared')

    def test_completed_zero_is_skipped_without_refunding_consumed_budget(self):
        self.plan['jobs'].append(self.job('waiting'))
        self.reset_state()
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, code=1)
        self.start(number=2)
        self.complete(number=2)
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual([j['id'] for j in report['skipped']], ['task'])
        expected = {**self.plan, 'jobs': [self.plan['jobs'][1]]}
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(expected))

    def test_all_completed_no_work_with_and_without_successful_finish(self):
        self.start()
        self.complete()
        for finished in (False, True):
            with self.subTest(finished=finished):
                if finished:
                    self.fleet('finished')
                report = self.run_recovery()
                self.assertEqual(report['status'], 'no_work', report)
                self.assertFalse(self.output.exists())
                self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_normal_failure_never_replayed(self):
        self.start()
        self.complete(code=259)
        self.refused('normal failure')

    def test_launch_failed_after_queue_is_blocked_not_eligible(self):
        for number in (0, 1):
            with self.subTest(attempt=number):
                self.reset_state()
                self.fleet('launch_failed', 'task', number, 70)
                report = self.refused('launch_failed (code 70)')
                self.assertEqual(report['eligible'], [])
                self.assertEqual(report['skipped'], [])
                self.assertEqual([j['id'] for j in report['blocked']], ['task'])

    def test_launch_failed_after_started_blocks_without_requiring_attempt_tail(self):
        self.start()
        # Runtime start failure can leave only a prepared/contained prefix.
        self.attempt('task', 1, 'guardian_error', code=5, active=0)
        self.fleet('launch_failed', 'task', 1, 70)
        report = self.refused('launch_failed (code 70)')
        self.assertEqual(report['eligible'], [])
        self.assertEqual([j['id'] for j in report['blocked']], ['task'])

    def test_launch_failed_does_not_change_protected_peer_outcome(self):
        self.plan['jobs'] += [self.job('protected', allow_stop=False,
                                       max_resumes=0, resume_argv=[]), self.job('waiting')]
        self.reset_state()
        self.start('protected')
        self.fleet('launch_failed', 'task', 1, 70)
        self.complete('protected')
        report = self.refused('launch_failed (code 70)')
        self.assertEqual([j['id'] for j in report['blocked']], ['task'])
        self.assertEqual([j['id'] for j in report['skipped']], ['protected'])
        self.assertEqual([j['id'] for j in report['eligible']], ['waiting'])

    def test_launch_failed_reserved_resume_is_never_replayed(self):
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, 1)
        prefix = copy.deepcopy(self.fleet_records)
        for number in (1, 2):
            with self.subTest(attempt=number):
                self.fleet_records = copy.deepcopy(prefix)
                self.fleet('launch_failed', 'task', number, 70)
                report = self.refused('launch_failed (code 70)')
                self.assertEqual(report['eligible'], [])

    def test_launch_failed_outcome_is_immutable(self):
        self.fleet('launch_failed', 'task', 1, 70)
        prefix = copy.deepcopy(self.fleet_records)
        for event, number, code in (('started', 1, 101), ('queued', 0, 0),
                                    ('completed', 1, 0), ('resume_queued', 1, 1),
                                    ('launch_failed', 1, 70)):
            with self.subTest(event=event):
                self.fleet_records = copy.deepcopy(prefix)
                self.fleet(event, 'task', number, code)
                self.refused('immutable launch_failed')

    def test_launch_failed_cannot_replace_completed_outcome(self):
        self.start()
        self.complete()
        self.fleet('launch_failed', 'task', 1, 70)
        self.refused('immutable attempt outcome')

    def test_launch_failed_cannot_replace_stopped_outcome(self):
        self.start()
        self.checkpoint()
        self.fleet('launch_failed', 'task', 1, 70)
        self.refused('immutable attempt outcome')

    def test_launch_failed_invalid_attempt_or_code_refused(self):
        prefix = copy.deepcopy(self.fleet_records)
        for number, code in ((2, 70), (0, 0), (1, 1), (1, True)):
            with self.subTest(attempt=number, code=code):
                self.fleet_records = copy.deepcopy(prefix)
                self.fleet('launch_failed', 'task', number, code)
                self.refused()

    def test_stopped_zero_cannot_be_success(self):
        self.start()
        self.checkpoint()
        self.fleet('completed', 'task', 1)
        self.refused('immutable')

    def test_any_blocked_job_prevents_partial_publication(self):
        self.plan['jobs'].append(self.job('unsafe'))
        self.reset_state()
        self.start('unsafe')
        report = self.refused()
        self.assertEqual([j['id'] for j in report['eligible']], ['task'])
        self.assertEqual([j['id'] for j in report['blocked']], ['unsafe'])

    def test_pending_resume_reservation_already_consumed(self):
        self.plan['jobs'][0]['max_resumes'] = 1
        self.reset_state()
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, code=0)
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(report['eligible'][0]['max_resumes'], 0)

    def test_started_reserved_resume_needs_a_new_ack_and_budget(self):
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, code=1)
        self.start(number=2)
        self.refused('no exact checkpoint ack')
        self.checkpoint(number=2)
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(report['eligible'][0]['max_resumes'], 0)

    def test_exhausted_allowance_refuses_even_exact_checkpoint(self):
        self.plan['jobs'][0]['max_resumes'] = 0
        self.reset_state()
        self.start()
        self.checkpoint()
        self.refused('allowance exhausted')

    def test_protected_or_missing_resume_recipe_refused(self):
        for change in ({'allow_stop': False, 'max_resumes': 0},
                       {'resume_argv': [], 'max_resumes': 0}):
            with self.subTest(change=change):
                # New cases use isolated directories to avoid stale evidence.
                with tempfile.TemporaryDirectory() as base:
                    original = self.plan
                    self.plan = copy.deepcopy(original)
                    self.plan['jobs'][0].update(change)
                    saved_state, self.state = self.state, Path(base)
                    try:
                        self.attempt_records, self.nonces = {}, {}
                        self.reset_state()
                        self.start()
                        self.refused('recipe')
                    finally:
                        self.state, self.plan = saved_state, original

    def test_fleet_terminal_policy_refuses(self):
        for event, code in (('deadline', 124), ('interrupt', 130),
                            ('internal_error', 5), ('finished', 1), ('finished', 0)):
            with self.subTest(event=event, code=code):
                self.reset_state()
                self.fleet(event, code=code)
                self.refused('terminal fleet policy')

    def test_live_owner_and_active_outer_job_refuse(self):
        for option in ('busy', 'active'):
            with self.subTest(option=option):
                report = self.run_recovery(safety=FakeSafety(**{option: True}))
                self.assertEqual(report['status'], 'refused', report)
                self.assertFalse(self.output.exists())

    def test_liveness_rechecked_before_reservation(self):
        original = self.safety.inactive_job
        @contextmanager
        def job(name):
            with original(name) as check:
                def later():
                    self.safety.active = True
                    check()
                yield later
        self.safety.inactive_job = job
        self.refused('active processes')

    def test_missing_mixed_or_invalid_owner(self):
        cases = [b'{', b'{"version":true}',
                 {'version': 1, 'run_id': 'not-a-uuid', 'job_name': 'Local\\Headroom-x'},
                 {'version': 1, 'run_id': self.run, 'job_name': 'Local\\Other'},
                 {'version': 1, 'run_id': str(uuid.uuid4()), 'job_name': 'Local\\Headroom-' + self.run}]
        for owner in cases:
            with self.subTest(owner=owner):
                save(self.state / 'owner.json', owner)
                self.refused()

    def test_compiled_plan_must_match_exactly(self):
        original = (self.state / 'plan.hrp').read_bytes()
        for data in (original + b'\0', original[:-1], bytes([original[0] ^ 1]) + original[1:]):
            with self.subTest(length=len(data)):
                save(self.state / 'plan.hrp', data)
                self.refused('exactly match')

    def test_json_input_duplicate_keys_and_invalid_values(self):
        for data in (b'{"jobs":[],"jobs":[]}', b'{"budget_mib":NaN}', b'[]', b'{}', b'\xff'):
            with self.subTest(data=data):
                save(self.input, data)
                self.refused()

    def test_malformed_truncated_empty_and_oversized_fleet(self):
        original = (self.state / 'fleet.jsonl').read_bytes()
        for data in (b'', b'{}\n', b'{\n', original[:-1], original + b'{}',
                     original + b' ' * (recover.MAX_LINE + 1) + b'\n',
                     original + b'{"version":1,"version":1}\n'):
            with self.subTest(length=len(data)):
                save(self.state / 'fleet.jsonl', data)
                self.refused()

    def test_fleet_field_types_identity_time_and_unknown_event(self):
        original = copy.deepcopy(self.fleet_records)
        for field, value in (('run_id', str(uuid.uuid4())), ('version', True),
                             ('attempt', True), ('priority', True), ('elapsed_ms', -1),
                             ('total_commit_bytes', 2**64), ('available_bytes', -1),
                             ('code', 2**32), ('task', 'unknown'), ('task', []),
                             ('event', 'future-replay'), ('event', []), ('extra', 1)):
            with self.subTest(field=field, value=value):
                self.fleet_records = copy.deepcopy(original)
                self.fleet_records[-1][field] = value
                self.flush_fleet()
                self.refused()

    def test_duplicate_queue_missing_queue_and_backward_time(self):
        self.fleet('queued', 'task')
        self.refused('queue')
        self.reset_state()
        self.fleet_records.pop()
        self.flush_fleet()
        self.refused('queue')
        self.reset_state()
        self.fleet_records[-1]['elapsed_ms'] = -1
        self.flush_fleet()
        self.refused('elapsed_ms')

    def test_task_event_transitions_refuse(self):
        original = copy.deepcopy(self.fleet_records)
        for event, number, code in (('completed', 1, 0), ('checkpoint_acknowledged', 1, 0),
                                    ('pressure_intent', 1, 125), ('terminated', 1, 125),
                                    ('resume_queued', 1, 1), ('started', 2, 101)):
            with self.subTest(event=event):
                self.fleet_records = copy.deepcopy(original)
                self.fleet(event, 'task', number, code)
                self.refused()

    def test_resume_reservation_wrong_number_count_or_duplicate(self):
        self.start()
        self.checkpoint()
        original = copy.deepcopy(self.fleet_records)
        for number, code in ((2, 1), (1, 2), (1, 0), (1, True)):
            with self.subTest(number=number, code=code):
                self.fleet_records = copy.deepcopy(original)
                self.fleet('resume_queued', 'task', number, code)
                self.refused()
        self.fleet_records = original
        self.fleet('resume_queued', 'task', 1, 1)
        self.fleet('resume_queued', 'task', 1, 0)
        self.refused()

    def test_extra_attempt_directory_blocks_queued_or_pending_resume(self):
        directory = self.state / 'task' / 'attempt-1'
        directory.mkdir(parents=True)
        self.refused('unjournaled')
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, 1)
        (self.state / 'task' / 'attempt-2').mkdir()
        self.refused('unjournaled')

    def test_attempt_file_missing_truncated_or_mixed(self):
        self.start()
        self.checkpoint()
        path = self.state / 'task' / 'attempt-1' / 'events.jsonl'
        original = path.read_bytes()
        for data in (b'', b'{}\n', original[:-1], original + b'{'):
            with self.subTest(data_length=len(data)):
                save(path, data)
                self.refused()
        save(path, original.replace(self.nonces['task', 1].encode(), str(uuid.uuid4()).encode(), 1))
        self.refused('mixed')

    def test_attempt_ack_cannot_use_another_attempt_nonce(self):
        self.start()
        self.checkpoint()
        path = self.state / 'task' / 'attempt-1' / 'stop.ack'
        original = path.read_bytes()
        for data in (b'', original[:-1], original + b'x',
                     b'headroom-stop-v1\n' + str(uuid.uuid4()).encode() + b'\n',
                     original.replace(b'\n', b'\r\n')):
            with self.subTest(data=data):
                save(path, data)
                self.refused('exact attempt')

    def test_request_must_match_ack_and_journal(self):
        self.start()
        self.checkpoint()
        directory = self.state / 'task' / 'attempt-1'
        bad = b'headroom-stop-v1\n' + str(uuid.uuid4()).encode() + b'\n'
        save(directory / 'stop.request', bad)
        save(directory / 'stop.ack', bad)
        self.refused('exact attempt')

    def test_missing_ack_refused_even_if_journal_claims_it(self):
        self.start()
        self.checkpoint()
        (self.state / 'task' / 'attempt-1' / 'stop.ack').unlink()
        self.refused()

    def test_attempt_fleet_outcome_or_containment_mismatch(self):
        self.start()
        self.complete()
        rows = self.attempt_records['task', 1]
        for index, field, value in ((1, 'code', 102), (2, 'code', 1),
                                    (2, 'active_processes', 1), (2, 'event', 'stopped')):
            with self.subTest(field=field, value=value):
                original = rows[index][field]
                rows[index][field] = value
                save(self.state / 'task' / 'attempt-1' / 'events.jsonl', b''.join(
                    (json.dumps(row) + '\n').encode() for row in rows))
                self.refused()
                rows[index][field] = original

    def test_maximum_three_resumes_cannot_gain_a_fourth(self):
        self.plan['jobs'][0]['max_resumes'] = 3
        self.reset_state()
        for number in range(1, 5):
            self.start(number=number)
            self.checkpoint(number=number)
            if number < 4:
                self.fleet('resume_queued', 'task', number, 3 - number)
        self.refused('allowance exhausted')
        self.fleet('resume_queued', 'task', 4, 0)
        self.refused('bounded allowance')

    def test_last_reserved_resume_keeps_zero_future_allowance(self):
        self.plan['jobs'][0]['max_resumes'] = 3
        self.reset_state()
        for number in range(1, 4):
            self.start(number=number)
            self.checkpoint(number=number)
            self.fleet('resume_queued', 'task', number, 3 - number)
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(report['eligible'][0]['max_resumes'], 0)

    def test_nonce_reuse_across_attempts_is_not_a_fresh_checkpoint(self):
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, 1)
        self.nonces['task', 2] = self.nonces['task', 1]
        self.start(number=2)
        self.checkpoint(number=2)
        self.refused('nonce reused')

    def test_attempt_nonce_must_differ_from_fleet_identity(self):
        self.nonces['task', 1] = self.run
        self.start()
        self.checkpoint()
        self.refused('nonce reused')

    def test_ack_for_previous_attempt_does_not_resume_current_attempt(self):
        self.start()
        self.checkpoint()
        self.fleet('resume_queued', 'task', 1, 1)
        self.start(number=2)
        self.checkpoint(number=2)
        save(self.state / 'task' / 'attempt-2' / 'stop.ack',
             (self.state / 'task' / 'attempt-1' / 'stop.ack').read_bytes())
        self.refused('exact attempt')

    def test_attempt_boolean_fields_and_unknown_events_refused(self):
        self.start()
        self.checkpoint()
        path = self.state / 'task' / 'attempt-1' / 'events.jsonl'
        original = copy.deepcopy(self.attempt_records['task', 1])
        for field, value in (('version', True), ('active_processes', True),
                             ('job_commit_bytes', -1), ('elapsed_ms', 0),
                             ('code', True), ('event', 'future'), ('extra', 1)):
            with self.subTest(field=field):
                rows = copy.deepcopy(original)
                rows[-1][field] = value
                save(path, b''.join((json.dumps(row) + '\n').encode() for row in rows))
                self.refused()

    def test_fleet_ack_requires_durable_attempt_ack(self):
        self.start()
        self.checkpoint(acknowledged=False, stopped=False)
        self.fleet('checkpoint_acknowledged', 'task', 1)
        self.refused('ack missing')

    def test_later_fleet_outcome_cannot_skip_ack_event(self):
        self.start()
        self.checkpoint(acknowledged=False, stopped=False)
        self.attempt('task', 1, 'checkpoint_acknowledged')
        self.attempt('task', 1, 'stopped', active=0)
        self.fleet('stopped', 'task', 1)
        self.refused('skipped a durable attempt acknowledgement')

    def test_termination_intent_prefix_can_resume_after_exact_ack(self):
        self.start()
        self.checkpoint(stopped=False)
        self.attempt('task', 1, 'terminate_intent', code=125)
        self.fleet('terminated', 'task', 1, 125)
        self.assertEqual(self.run_recovery()['status'], 'prepared')

    def test_fleet_termination_requires_attempt_intent(self):
        self.start()
        self.checkpoint(stopped=False)
        self.fleet('terminated', 'task', 1, 125)
        self.refused('termination missing')

    def test_stop_outcome_cannot_skip_termination_event(self):
        self.start()
        self.checkpoint(stopped=False)
        self.attempt('task', 1, 'terminate_intent', code=125)
        self.attempt('task', 1, 'stopped', active=0)
        self.fleet('stopped', 'task', 1)
        self.refused('skipped a durable attempt termination')

    def test_two_simultaneous_pressure_intents_are_contradictory(self):
        self.plan['jobs'].append(self.job('second'))
        self.reset_state()
        self.start()
        self.start('second')
        self.checkpoint(stopped=False)
        self.fleet('pressure_intent', 'second', 1, 125)
        self.refused('overlapping')

    def test_missing_and_duplicate_started_event_refused(self):
        self.start()
        self.fleet('started', 'task', 1, 101)
        self.refused('duplicate')

    def test_streams_diagnostics_without_retaining_journal(self):
        # The native file can grow far beyond the compiled-plan size. Generate
        # a small stream and spy on reads so an unbounded read is detectable.
        row = copy.deepcopy(self.fleet_records[-1])
        row.update(event='sample', task='', priority=0, attempt=0, elapsed_ms=10)
        with (self.state / 'fleet.jsonl').open('ab') as stream:
            for _ in range(1024):
                stream.write((json.dumps(row) + '\n').encode())
        original = recover._Files.open
        @contextmanager
        def bounded(files, path):
            with original(files, path) as stream:
                if Path(path).name == 'fleet.jsonl':
                    wrapped = mock.Mock(wraps=stream)
                    wrapped.read.side_effect = AssertionError('journal read without streaming')
                    yield wrapped
                else:
                    yield stream
        with mock.patch.object(recover._Files, 'open', bounded):
            self.assertEqual(self.run_recovery()['status'], 'prepared')

    def test_mixed_completed_resume_and_queued_keep_original_fifo_and_caps(self):
        self.plan['jobs'] += [self.job('done', priority=90),
                              self.job('wait', priority=2)]
        self.reset_state()
        self.start('done')
        self.complete('done')
        self.start()
        self.checkpoint()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        expected = copy.deepcopy(self.plan)
        expected['jobs'] = [expected['jobs'][0], expected['jobs'][2]]
        expected['jobs'][0]['argv'] = expected['jobs'][0]['resume_argv']
        expected['jobs'][0]['max_resumes'] = 1
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(expected))
        self.assertEqual([j['id'] for j in report['eligible']], ['task', 'wait'])

    def test_all_64_durable_queues_supported(self):
        self.plan['jobs'] = [self.job(f'task-{number}') for number in range(64)]
        self.reset_state()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(len(report['eligible']), 64)

    @unittest.skipUnless(os.name == 'nt', 'Windows path aliases')
    def test_device_alternate_stream_and_trailing_dot_outputs_refused(self):
        for name in ('NUL.hrp', 'output.hrp:stream', 'output.', 'output '):
            with self.subTest(name=name):
                self.output = self.base / name
                report = self.run_recovery()
                self.assertEqual(report['status'], 'refused', report)
                self.assertIn('path refused', report['error'])
                self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_completed_attempt_cannot_mutate_or_resume(self):
        self.start()
        self.complete()
        for event, code in (('started', 101), ('pressure_intent', 125),
                            ('resume_queued', 1), ('completed', 1)):
            with self.subTest(event=event):
                original = copy.deepcopy(self.fleet_records)
                self.fleet(event, 'task', 1, code)
                self.refused()
                self.fleet_records = original

    def test_per_attempt_completed_without_fleet_commit_never_replayed(self):
        self.start()
        self.attempt('task', 1, 'completed', active=0)
        self.refused('not resumable')

    def test_duplicate_recovery_refused_even_with_different_output(self):
        self.assertEqual(self.run_recovery()['status'], 'prepared')
        self.output = self.base / 'other.hrp'
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertIn('duplicate recovery', report['error'])
        self.assertFalse(self.output.exists())

    def test_existing_corrupt_reservation_always_refuses(self):
        for data in (b'', b'{', b'{}'):
            with self.subTest(data=data):
                save(self.state / 'recovery-reserved.json', data)
                report = self.run_recovery()
                self.assertEqual(report['status'], 'refused', report)
                self.assertEqual((self.state / 'recovery-reserved.json').read_bytes(), data)
                self.assertFalse(self.output.exists())

    def test_publication_failure_retains_durable_reservation(self):
        with mock.patch.object(recover, '_publish_new', side_effect=OSError('disk failure')):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertIsNotNone(report['reservation'])
        marker = self.state / 'recovery-reserved.json'
        self.assertTrue(marker.exists())
        self.assertFalse(self.output.exists())
        again = self.run_recovery()
        self.assertIn('duplicate recovery', again['error'])

    def test_reservation_flush_failure_retains_marker_and_no_output(self):
        with mock.patch.object(recover.os, 'fsync', side_effect=OSError('flush failed')):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertTrue((self.state / 'recovery-reserved.json').exists())
        self.assertFalse(self.output.exists())

    def test_existing_output_is_not_overwritten_or_reserved(self):
        save(self.output, b'keep')
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertEqual(self.output.read_bytes(), b'keep')
        self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_output_must_be_outside_old_state_and_not_input(self):
        for output in (self.input, self.state / 'new.hrp', self.state / 'task' / 'new.hrp'):
            with self.subTest(output=output):
                self.output = output
                report = self.run_recovery()
                self.assertEqual(report['status'], 'refused', report)
                self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_output_race_retains_reservation_and_existing_bytes(self):
        original = recover._publish_new
        def racing(path, payload):
            save(path, b'other writer')
            original(path, payload)
        with mock.patch.object(recover, '_publish_new', side_effect=racing):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertEqual(self.output.read_bytes(), b'other writer')
        self.assertTrue((self.state / 'recovery-reserved.json').exists())

    def test_exclusive_reservation_race(self):
        original = recover._reserve
        def racing(*args):
            save(self.state / 'recovery-reserved.json', b'another reservation')
            return original(*args)
        with mock.patch.object(recover, '_reserve', side_effect=racing):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertEqual((self.state / 'recovery-reserved.json').read_bytes(), b'another reservation')
        self.assertFalse(self.output.exists())

    def test_lease_held_while_reserving_and_publishing(self):
        reserve, publish = recover._reserve, recover._publish_new
        def reserve_checked(*args):
            self.assertTrue(self.safety.held)
            return reserve(*args)
        def publish_checked(*args):
            self.assertTrue(self.safety.held)
            self.assertTrue((self.state / 'recovery-reserved.json').exists())
            return publish(*args)
        with mock.patch.object(recover, '_reserve', side_effect=reserve_checked), \
             mock.patch.object(recover, '_publish_new', side_effect=publish_checked):
            self.assertEqual(self.run_recovery()['status'], 'prepared')

    def test_plan_revalidated_before_reservation(self):
        compile_plan = headroom.compile_plan
        calls = []
        def compile_checked(plan):
            calls.append(copy.deepcopy(plan))
            if len(calls) == 2:
                raise headroom.PlanError('changed executable resolution')
            return compile_plan(plan)
        with mock.patch.object(headroom, 'compile_plan', side_effect=compile_checked):
            self.refused('changed executable')
        self.assertEqual(len(calls), 2)

    def test_original_executable_resolution_drift_refused(self):
        compile_plan = headroom.compile_plan
        calls = 0
        def drift(plan):
            nonlocal calls
            calls += 1
            result = compile_plan(plan)
            return result + b'drift' if calls == 3 else result
        with mock.patch.object(headroom, 'compile_plan', side_effect=drift):
            self.refused('resolution changed')

    def test_output_parent_mutation_refused_before_reservation(self):
        original = recover._classify
        def changed(*args):
            result = original(*args)
            save(self.base / 'other-output', b'new file')
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('output parent changed')

    def test_evidence_mutation_during_classification_refused(self):
        original = recover._classify
        def changed(*args):
            result = original(*args)
            self.fleet('sample')
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('changed')

    def test_hard_linked_evidence_refused(self):
        try:
            os.link(self.state / 'owner.json', self.base / 'owner-alias')
        except OSError as error:
            self.skipTest(str(error))
        self.refused('singly linked')

    def test_symlinked_evidence_refused_if_supported(self):
        path = self.state / 'plan.hrp'
        destination = self.base / 'saved-plan'
        path.rename(destination)
        try:
            path.symlink_to(destination)
        except OSError as error:
            self.skipTest(str(error))
        self.refused('reparse')

    def test_cli_emits_report_and_refusal_exit_code(self):
        with mock.patch.object(recover, 'WindowsSafety', return_value=FakeSafety(busy=True)), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = recover.main([str(self.input), str(self.state), str(self.output)])
        self.assertEqual(code, 73)
        self.assertEqual(json.loads(output.getvalue())['status'], 'refused')


@unittest.skipUnless(os.name == 'nt', 'real Windows kernel checks')
class LiveRecoveryTests(unittest.TestCase):
    def test_real_exclusive_owner_lease_and_missing_lock(self):
        safety = recover.WindowsSafety()
        with tempfile.TemporaryDirectory(prefix='headroom-lease-') as base:
            path = Path(base) / 'owner.lock'
            save(path, b'')
            with safety.lease(path):
                with self.assertRaisesRegex(recover.RecoveryError, 'LIVE/BUSY'):
                    with safety.lease(path):
                        self.fail('two exclusive owners')
            with safety.lease(path):
                pass
            with self.assertRaisesRegex(recover.RecoveryError, 'unavailable|No such file'):
                with safety.lease(Path(base) / 'missing.lock'):
                    self.fail('missing lock accepted')

    def test_real_absent_named_job(self):
        safety = recover.WindowsSafety()
        with safety.inactive_job('Local\\Headroom-' + str(uuid.uuid4())) as recheck:
            recheck()

    def test_unknown_open_job_error_is_not_absence(self):
        safety = recover.WindowsSafety()
        for code in (5, 6, 32, 87, 0):
            with self.subTest(code=code), \
                 mock.patch.object(safety.kernel, 'OpenJobObjectW', return_value=None), \
                 mock.patch.object(recover.ctypes, 'get_last_error', return_value=code):
                with self.assertRaisesRegex(recover.RecoveryError, 'liveness unknown'):
                    with safety.inactive_job('Local\\Headroom-' + str(uuid.uuid4())):
                        self.fail('query error treated as absence')

    def test_real_empty_named_job_and_query_failure(self):
        safety = recover.WindowsSafety()
        kernel = safety.kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, safety.w.LPCWSTR]
        kernel.CreateJobObjectW.restype = safety.w.HANDLE
        name = 'Local\\Headroom-' + str(uuid.uuid4())
        handle = kernel.CreateJobObjectW(None, name)
        self.assertTrue(handle)
        try:
            with safety.inactive_job(name) as recheck:
                recheck()
            with mock.patch.object(kernel, 'QueryInformationJobObject', return_value=False):
                with self.assertRaisesRegex(recover.RecoveryError, 'query failed'):
                    with safety.inactive_job(name):
                        self.fail('failed job query accepted')
        finally:
            kernel.CloseHandle(handle)

    def test_live_native_owner_refuses_recovery_and_active_job_query(self):
        binary = ROOT / 'build' / 'headroom-fleet.exe'
        if not binary.is_file():
            self.skipTest('native executable unavailable; never rebuild another worker binary')
        with tempfile.TemporaryDirectory(prefix='headroom-live-recovery-') as base:
            base = Path(base)
            plan = {'budget_mib': 64, 'hard_mib': 128, 'reserve_mib': 0,
                    'timeout_ms': 4000,
                    'jobs': [{'id': 'hold', 'claim_mib': 24, 'hard_mib': 96,
                              'argv': [sys.executable, '-c', 'import time; time.sleep(2)']}]}
            source, compiled, state = base / 'plan.json', base / 'plan.hrp', base / 'state'
            save(source, plan)
            save(compiled, headroom.compile_plan(plan))
            # No memory fixture, process signals, or binary rebuilds. Native
            # containment owns the child; this test waits for normal completion.
            process = subprocess.Popen([str(binary), '--plan', str(compiled), '--state', str(state)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    journal = state / 'fleet.jsonl'
                    if journal.exists() and b'"event":"started"' in journal.read_bytes():
                        break
                    if process.poll() is not None:
                        self.fail('native build does not support owner contract or launch failed')
                    time.sleep(0.02)
                else:
                    self.fail('native launch did not reach started')
                self.assertTrue((state / 'owner.lock').is_file())
                self.assertEqual((state / 'plan.hrp').read_bytes(), compiled.read_bytes())
                owner = json.loads((state / 'owner.json').read_bytes())
                safety = recover.WindowsSafety()
                with self.assertRaisesRegex(recover.RecoveryError, 'active processes'):
                    with safety.inactive_job(owner['job_name']):
                        self.fail('live old job considered inactive')
                output = base / 'recovery.hrp'
                report = recover.prepare_recovery(source, state, output)
                self.assertEqual(report['status'], 'refused', report)
                self.assertIn('LIVE/BUSY', report['error'])
                self.assertFalse(output.exists())
                self.assertFalse((state / 'recovery-reserved.json').exists())
            finally:
                # A timeout is a failed check, never permission to signal it.
                process.wait(timeout=10)
            self.assertEqual(process.returncode, 0)
            after = recover.prepare_recovery(source, state, base / 'after.hrp')
            self.assertEqual(after['status'], 'no_work', after)


if __name__ == '__main__':
    unittest.main()
