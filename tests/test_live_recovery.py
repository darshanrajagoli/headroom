"""Small offline live-session fixtures; no workload or native process launches."""

import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import headroom
import headroom_live
import recover
import recover_live
import test_recovery as batch


class LiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = batch.RecoveryTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.state, self.output = self.fx.state, self.fx.output
        self.settings = {k: v for k, v in self.fx.plan.items() if k != 'jobs'}
        self.settings_path = self.fx.base / 'settings.json'
        batch.save(self.settings_path, self.settings)
        batch.save(self.state / 'plan.hrp', headroom_live.compile_session(self.settings))
        self.owner = {'version': 2, 'mode': 'live', 'run_id': self.fx.run,
                      'job_name': 'Local\\Headroom-' + self.fx.run}
        batch.save(self.state / 'owner.json', self.owner)
        self.session = {'version': 1, 'run_id': self.fx.run, 'max_jobs': 64,
                        'settings': self.settings}
        batch.save(self.state / 'session.json', self.session)
        for name in ('inbox', 'processed', 'receipts'):
            (self.state / name).mkdir()
        self.fx.fleet_records = []
        self.fx.fleet('prepared')
        self.fx.fleet('listening')

    def request(self, task='task', *, queue=True, receipt=True, request=None, job=None):
        request = request or str(uuid.uuid4())
        job = job or self.fx.job(task)
        if task not in {j['id'] for j in self.fx.plan['jobs']}:
            self.fx.plan['jobs'].append(job)
        plan = {**self.settings, 'jobs': [job]}
        folder = self.state / 'processed' / request
        folder.mkdir()
        batch.save(folder / 'target',
                   f'headroom-submit-v1\n{self.fx.run}\n{request}\n'.encode('ascii'))
        compiled = headroom.compile_plan(plan)
        batch.save(folder / 'plan.hrp', compiled)
        batch.save(folder / 'validated.hrp', compiled)
        batch.save(folder / 'plan.json', plan)
        if queue:
            self.fx.fleet('queued', task)
        if receipt:
            ordinal = sum(row['event'] == 'queued' for row in self.fx.fleet_records)
            self.save_receipt(request, task=task, ordinal=ordinal)
        return folder

    def save_receipt(self, request, **changes):
        receipt = {'version': 1, 'run_id': self.fx.run, 'request_id': request,
                   'task': 'task', 'status': 'accepted', 'code': 0, 'ordinal': 1}
        receipt.update(changes)
        batch.save(self.state / 'receipts' / f'{request}.json', receipt)

    def run_recovery(self, safety=None):
        return recover_live.prepare_live_recovery(self.settings_path, self.state, self.output,
                                                 safety=safety or self.fx.safety)

    def refused(self, reason=None, *, reserved=False):
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused', report)
        self.assertFalse(self.output.exists())
        if not reserved:
            self.assertFalse(Path(str(self.output) + '.json').exists())
        self.assertEqual((self.state / 'recovery-reserved.json').exists(), reserved)
        if reason:
            self.assertIn(reason, str(report))
        return report

    def seal(self):
        batch.save(self.state / 'sealed.json', {'version': 1, 'run_id': self.fx.run})
        self.fx.fleet('sealed')

    def test_queued_original_prepares_ordinary_batch_and_reserves(self):
        self.request()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(self.fx.plan))
        self.assertEqual(report['eligible'][0]['mode'], 'original')
        self.assertEqual(report['reservation']['run_id'], self.fx.run)
        companion = Path(str(self.output) + '.json')
        self.assertEqual(report['plan_json'], str(companion))
        self.assertEqual(report['reservation']['plan_json_path'], str(companion))
        saved = headroom.strict_json_loads(companion.read_bytes())
        self.assertEqual(headroom.compile_plan(saved), self.output.read_bytes())
        self.assertEqual(companion.read_bytes(), (json.dumps(saved, ensure_ascii=True,
            sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii'))
        self.assertFalse(self.fx.safety.held)
        self.assertGreaterEqual(self.fx.safety.checks, 3)

    def test_queue_before_receipt_crash_is_original_work(self):
        self.request(receipt=False)
        self.assertEqual(self.run_recovery()['status'], 'prepared')

    def test_saved_working_directory_survives_different_recovery_cwd(self):
        self.request(job=self.fx.job('task', argv=[str(self.fx.exe), 'worker.py']))
        caller = self.fx.base / 'caller'
        caller.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(caller)
            report = self.run_recovery()
        finally:
            os.chdir(previous)
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(report['working_directory'], str(self.fx.base))

    def test_missing_or_corrupt_working_directory_refuses_before_reservation(self):
        self.request()
        path = self.state / 'working-directory.hrc'
        path.unlink()
        self.refused('working-directory.hrc')
        for payload in (b'HRCWD000', b'HRCWD001x', b'HRCWD001\x00\xd8',
                        b'HRCWD001' + str(self.fx.base).encode('utf-16le') + b'\0\0'):
            with self.subTest(payload=payload[:20]):
                batch.save(path, payload)
                self.refused()

    def test_working_directory_metadata_is_rechecked_before_reservation(self):
        self.request()
        classify = recover._classify
        def changed(*args):
            result = classify(*args)
            batch.save(self.state / 'working-directory.hrc',
                       b'HRCWD001' + str(Path.cwd()).encode('utf-16le'))
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('changed')

    def test_started_without_receipt_refuses_even_exact_checkpoint(self):
        self.request(receipt=False)
        self.fx.start()
        self.fx.checkpoint()
        self.refused('requires accepted admission receipt')

    def test_dynamic_queue_after_completed_job_preserves_acceptance_order(self):
        self.request('task')
        self.fx.start()
        self.fx.complete()
        self.request('later', job=self.fx.job('later', priority=31))
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual([j['id'] for j in report['skipped']], ['task'])
        self.assertEqual([j['id'] for j in report['eligible']], ['later'])
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(
            {**self.settings, 'jobs': [self.fx.job('later')]}))

    def test_dynamic_queue_while_earlier_job_checkpointed_preserves_fifo(self):
        self.request()
        self.fx.start()
        self.request('later')
        self.fx.checkpoint()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        expected = [self.fx.job('task'), self.fx.job('later')]
        expected[0].update(argv=expected[0]['resume_argv'], max_resumes=1)
        self.assertEqual(self.output.read_bytes(), headroom.compile_plan(
            {**self.settings, 'jobs': expected}))

    def test_ack_bytes_before_ack_events_can_resume(self):
        self.request()
        self.fx.start()
        self.fx.checkpoint(acknowledged=False, stopped=False)
        self.assertEqual(self.run_recovery()['status'], 'prepared')

    def test_existing_resume_reservation_not_consumed_twice(self):
        self.request()
        self.fx.start()
        self.fx.checkpoint()
        self.fx.fleet('resume_queued', 'task', 1, 1)
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(report['eligible'][0]['max_resumes'], 1)

    def test_exhausted_resume_budget_refuses(self):
        self.request(job=self.fx.job('task', max_resumes=0))
        self.fx.start()
        self.fx.checkpoint()
        self.refused('exhausted')

    def test_started_without_checkpoint_refuses(self):
        self.request()
        self.fx.start()
        self.refused('no exact checkpoint ack')

    def test_normal_failure_blocks_all_work(self):
        self.request()
        self.fx.start()
        self.fx.complete(code=1)
        self.request('later')
        self.refused('normal failure')

    def test_launch_failed_never_replayed(self):
        self.request()
        self.fx.fleet('launch_failed', 'task', 0, 70)
        self.refused('launch_failed')

    def test_completed_zero_no_work(self):
        self.request()
        self.fx.start()
        self.fx.complete()
        self.seal()
        self.fx.fleet('finished')
        self.assertEqual(self.run_recovery()['status'], 'no_work')
        self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_empty_sealed_session_no_work(self):
        self.seal()
        self.fx.fleet('finished')
        report = self.run_recovery()
        self.assertEqual(report['status'], 'no_work')
        self.assertIsNone(report['plan_json'])
        self.assertFalse(Path(str(self.output) + '.json').exists())

    def test_pending_and_unqueued_processed_are_retained_and_never_run(self):
        pending = self.state / 'inbox' / (str(uuid.uuid4()) + '.ready')
        pending.mkdir()
        batch.save(pending / 'plan.json', b'incomplete unaccepted input')
        folder = self.request('unaccepted', queue=False, receipt=False)
        before = {p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        self.assertEqual(self.run_recovery()['status'], 'no_work')
        self.assertEqual(before, {p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()})
        self.assertTrue(folder.exists())

    def test_malformed_rejected_input_does_not_run_or_block_peer(self):
        self.request()
        request = str(uuid.uuid4())
        folder = self.state / 'processed' / request
        folder.mkdir()
        batch.save(folder / 'plan.json', b'{')
        self.save_receipt(request, task='', status='rejected', code=64, ordinal=0)
        self.fx.fleet('request_rejected', code=64)
        self.assertEqual(self.run_recovery()['status'], 'prepared')
        self.assertEqual((folder / 'plan.json').read_bytes(), b'{')

    def test_all_rejection_codes_are_bound_root_diagnostics(self):
        for code in (64, 73, 75):
            self.fx.fleet('request_rejected', code=code)
        self.assertEqual(self.run_recovery()['status'], 'no_work')

    def test_invalid_rejection_code_refuses(self):
        self.fx.fleet('request_rejected', code=0)
        self.refused('rejection diagnostic')

    def test_queue_after_seal_refuses(self):
        self.seal()
        self.request()
        self.refused('admission window')

    def test_missing_listening_refuses_admission(self):
        self.fx.fleet_records.pop()
        self.request()
        self.refused('admission window')

    def test_duplicate_listening_refuses(self):
        self.fx.fleet('listening')
        self.refused('listening diagnostic')

    def test_duplicate_sealed_refuses(self):
        self.seal()
        self.fx.fleet('sealed')
        self.refused('sealed diagnostic')

    def test_task_bound_live_diagnostic_refuses(self):
        self.request()
        self.fx.fleet('request_rejected', 'task', code=64)
        self.refused()

    def test_sealed_diagnostic_requires_metadata(self):
        self.fx.fleet('sealed')
        self.refused('without durable metadata')

    def test_invalid_sealed_metadata_refuses(self):
        self.seal()
        batch.save(self.state / 'sealed.json', {'version': 1, 'run_id': str(uuid.uuid4())})
        self.refused('sealed metadata')

    def test_terminal_failure_refuses(self):
        self.request()
        self.fx.fleet('deadline', code=124)
        self.refused('terminal fleet policy')

    def test_unknown_event_refuses(self):
        self.fx.fleet('new_future_event')
        self.refused('unknown fleet event')

    def test_partial_journal_refuses(self):
        with (self.state / 'fleet.jsonl').open('ab') as stream:
            stream.write(b'{')
        self.refused('truncated')

    def test_queue_order_is_not_receipt_or_directory_order(self):
        self.request('task', request='ffffffff-ffff-ffff-ffff-ffffffffffff')
        self.request('later', request='00000000-0000-0000-0000-000000000000')
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual([j['id'] for j in report['eligible']], ['task', 'later'])

    def test_lifecycle_before_own_queue_refuses(self):
        self.request('task')
        self.fx.fleet('started', 'later', 1, 101, priority=31)
        self.request('later')
        self.refused('before durable queue')

    def test_duplicate_queue_refuses(self):
        self.request()
        self.fx.fleet('queued', 'task')
        self.refused('duplicate queue')

    def test_queue_missing_processed_proof_refuses(self):
        self.fx.fleet('queued', 'task')
        self.refused('missing or ambiguous')

    def test_ambiguous_processed_proof_refuses(self):
        self.request()
        self.request(queue=False, receipt=False)
        self.refused('ambiguous')

    def test_changed_queue_priority_refuses(self):
        self.request()
        self.fx.fleet_records[-1]['priority'] = 2
        self.fx.flush_fleet()
        self.refused('priority')

    def test_bad_target_identity_refuses(self):
        folder = self.request()
        batch.save(folder / 'target',
                   f'headroom-submit-v1\n{uuid.uuid4()}\n{folder.name}\n'.encode())
        self.refused('target identity')

    def test_changed_validated_bytes_refuses(self):
        folder = self.request()
        batch.save(folder / 'validated.hrp', b'different')
        self.refused('validated bytes differ')

    def test_json_does_not_match_validated_bytes_refuses(self):
        folder = self.request()
        plan = {**self.settings, 'jobs': [self.fx.job('task', argv=[str(self.fx.exe), 'changed'])]}
        batch.save(folder / 'plan.json', plan)
        self.refused('differs from validated bytes')

    def test_canonical_plan_requires_full_matching_root(self):
        folder = self.request()
        plan = {**self.settings, 'jobs': [self.fx.job('task')]}
        plan.pop('reserve_mib')
        batch.save(folder / 'plan.json', plan)
        self.refused('root settings mismatch')

    def test_two_job_plan_refuses(self):
        folder = self.request()
        plan = {**self.settings, 'jobs': [self.fx.job('task'), self.fx.job('other')]}
        batch.save(folder / 'plan.json', plan)
        for name in ('plan.hrp', 'validated.hrp'):
            batch.save(folder / name, headroom.compile_plan(plan))
        self.refused('one-job')

    def test_partial_validated_request_refuses(self):
        folder = self.request()
        (folder / 'plan.json').unlink()
        self.refused('partial or unknown')

    def test_duplicate_json_key_refuses(self):
        folder = self.request()
        batch.save(folder / 'plan.json', b'{"jobs":[],"jobs":[]}')
        self.refused('duplicate JSON key')

    def test_accepted_receipt_requires_queue(self):
        folder = self.request(queue=False, receipt=False)
        self.save_receipt(folder.name)
        self.refused('without durable queue')

    def test_accepted_receipt_requires_validated_bytes(self):
        folder = self.request()
        (folder / 'validated.hrp').unlink()
        self.refused('without validated plan')

    def test_receipt_ordinal_must_match_queue_order(self):
        folder = self.request()
        self.save_receipt(folder.name, ordinal=2)
        self.refused('conflicting admission receipt')

    def test_rejected_receipt_conflicts_with_queued_request(self):
        folder = self.request()
        self.save_receipt(folder.name, status='rejected', code=73, ordinal=0)
        self.refused('conflicting admission receipt')

    def test_receipt_schema_identity_and_types_strict(self):
        folder = self.request()
        changes = [{'run_id': str(uuid.uuid4())}, {'request_id': str(uuid.uuid4())},
                   {'version': True}, {'code': False}, {'ordinal': True},
                   {'ordinal': 65}, {'status': 'pending'}, {'task': ''},
                   {'extra': 1}, {'task': 'con'}]
        for change in changes:
            with self.subTest(change=change):
                self.save_receipt(folder.name, **change)
                self.refused()

    def test_receipt_size_bound(self):
        folder = self.request()
        batch.save(self.state / 'receipts' / f'{folder.name}.json', b' ' * 4097)
        self.refused('oversized')

    def test_owner_version_mode_identity_and_job_name_strict(self):
        for change in ({'version': 1}, {'version': True}, {'mode': 'batch'},
                       {'run_id': 'invalid'}, {'job_name': 'Local\\other'}, {'extra': 1}):
            with self.subTest(change=change):
                batch.save(self.state / 'owner.json', {**self.owner, **change})
                self.refused()

    def test_session_schema_identity_settings_and_capacity_strict(self):
        for change in ({'version': True}, {'run_id': str(uuid.uuid4())},
                       {'max_jobs': True}, {'max_jobs': 63}, {'extra': 1},
                       {'settings': {**self.settings, 'timeout_ms': 1000}},
                       {'settings': {'budget_mib': 64, 'hard_mib': 128}}):
            with self.subTest(change=change):
                batch.save(self.state / 'session.json', {**self.session, **change})
                self.refused()

    def test_caller_settings_header_and_types_strict(self):
        for change in ({'jobs': []}, {'budget_mib': True}, {'hard_mib': 64},
                       {'timeout_ms': 1}, {'reserve_mib': -1}):
            with self.subTest(change=change):
                batch.save(self.settings_path, {**self.settings, **change})
                self.refused()

    def test_batch_recovery_still_refuses_v2_owner(self):
        # Make the plan match a batch input so owner-version validation is reached.
        batch.save(self.state / 'plan.hrp', headroom.compile_plan(self.fx.plan))
        report = recover.prepare_recovery(self.fx.input, self.state, self.output,
                                          safety=self.fx.safety)
        self.assertEqual(report['status'], 'refused')
        self.assertIn('owner metadata', report['error'])

    def test_batch_ledger_default_still_requires_all_queues_before_launch(self):
        self.request()
        self.fx.start()
        self.request('later')
        with self.assertRaises(recover.RecoveryError):
            recover._fleet(recover._Files(), self.state, self.fx.plan, self.fx.run)

    def test_busy_owner_and_active_job_refuse(self):
        self.request()
        for safety in (batch.FakeSafety(busy=True), batch.FakeSafety(active=True)):
            with self.subTest(safety=safety):
                self.assertEqual(self.run_recovery(safety)['status'], 'refused')
                self.assertFalse(self.output.exists())

    def test_live_job_rechecked_before_reservation(self):
        self.request()
        original = self.fx.safety.inactive_job
        from contextlib import contextmanager
        @contextmanager
        def adapter(name):
            with original(name) as check:
                def recheck():
                    self.fx.safety.active = True
                    check()
                yield recheck
        with mock.patch.object(self.fx.safety, 'inactive_job', adapter):
            self.refused('active processes')

    def test_unjournaled_attempt_directory_refuses(self):
        self.request()
        (self.state / 'task').mkdir()
        self.refused('unjournaled')

    def test_ack_nonce_mismatch_refuses(self):
        self.request()
        self.fx.start()
        self.fx.checkpoint()
        batch.save(self.state / 'task' / 'attempt-1' / 'stop.ack', b'wrong nonce')
        self.refused('nonmatching exact')

    def test_unknown_attempt_event_refuses(self):
        self.request()
        self.fx.start()
        self.fx.checkpoint(stopped=False)
        self.fx.attempt('task', 1, 'surprise')
        self.refused('unknown per-attempt')

    def test_snapshot_mutation_before_classification_refuses(self):
        self.request()
        original = recover._classify
        def changed(*args):
            result = original(*args)
            self.fx.fleet('sample')
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('changed')

    def test_missing_receipt_appearing_during_preparation_refuses(self):
        folder = self.request(receipt=False)
        original = recover._classify
        def changed(*args):
            result = original(*args)
            self.save_receipt(folder.name)
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('changed')

    def stable_directory_signature(self, info):
        if recover.stat.S_ISDIR(info.st_mode):
            return info.st_dev, info.st_ino, info.st_mode
        return self.original_signature(info)

    def test_missing_receipt_with_stable_directory_metadata_refuses(self):
        self.original_signature = recover._signature
        with mock.patch.object(recover, '_signature', side_effect=self.stable_directory_signature):
            self.test_missing_receipt_appearing_during_preparation_refuses()

    def test_hardlinked_validated_evidence_refuses(self):
        folder = self.request()
        os.link(folder / 'validated.hrp', self.fx.base / 'alias')
        self.refused('singly linked')

    def test_processed_symlink_refuses_when_supported(self):
        folder = self.request()
        original = folder / 'target'
        saved = self.fx.base / 'saved-target'
        original.rename(saved)
        try:
            original.symlink_to(saved)
        except OSError as error:
            self.skipTest(str(error))
        self.refused('reparse')

    def test_executable_resolution_drift_refuses(self):
        self.request()
        original = headroom.compile_plan
        calls = 0
        def changed(plan):
            nonlocal calls
            calls += 1
            payload = original(plan)
            return payload + b'drift' if calls == 3 else payload
        with mock.patch.object(headroom, 'compile_plan', side_effect=changed):
            self.refused('resolution changed')

    def test_publication_failure_keeps_reservation(self):
        self.request()
        with mock.patch.object(recover, '_publish_new', side_effect=OSError('disk full')):
            report = self.refused('disk full', reserved=True)
        self.assertEqual(report['plan_json'], str(self.output) + '.json')
        self.assertFalse(Path(report['plan_json']).exists())
        self.refused('duplicate recovery', reserved=True)

    def test_reservation_flush_failure_keeps_marker(self):
        self.request()
        with mock.patch.object(recover.os, 'fsync', side_effect=OSError('flush failed')):
            self.refused('flush failed', reserved=True)

    def test_second_preparation_refuses(self):
        self.request()
        self.assertEqual(self.run_recovery()['status'], 'prepared')
        self.output = self.fx.base / 'second.hrp'
        self.refused('duplicate recovery', reserved=True)

    def test_publication_uses_existing_lease_and_reservation(self):
        self.request()
        original = recover._publish_new
        publications = []
        def checked(path, body):
            self.assertTrue(self.fx.safety.held)
            self.assertTrue((self.state / 'recovery-reserved.json').is_file())
            if path == self.output:
                saved = headroom.strict_json_loads(Path(str(path) + '.json').read_bytes())
                self.assertEqual(headroom.compile_plan(saved), body)
            publications.append(path)
            return original(path, body)
        with mock.patch.object(recover, '_publish_new', side_effect=checked):
            self.assertEqual(self.run_recovery()['status'], 'prepared')
        self.assertEqual(publications, [Path(str(self.output) + '.json'), self.output])

    def test_output_inside_old_state_refuses(self):
        self.output = self.state / 'new.hrp'
        self.refused('outside old state')

    def test_output_not_overwritten(self):
        batch.save(self.output, b'keep')
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused')
        self.assertEqual(self.output.read_bytes(), b'keep')

    def test_actual_client_canonical_payload_is_recoverable(self):
        request = str(uuid.uuid4())
        payloads = headroom_live._payloads(self.session, self.fx.job('task'), request)
        folder = self.state / 'processed' / request
        folder.mkdir()
        for name, body in payloads.items():
            batch.save(folder / name, body)
        batch.save(folder / 'validated.hrp', payloads['plan.hrp'])
        self.fx.fleet('queued', 'task')
        self.save_receipt(request)
        self.assertEqual(self.run_recovery()['status'], 'prepared')
        self.assertEqual(self.output.read_bytes(), payloads['plan.hrp'])

    def test_caller_may_omit_default_settings(self):
        settings = {'budget_mib': 64, 'hard_mib': 128}
        batch.save(self.settings_path, settings)
        batch.save(self.state / 'plan.hrp', headroom_live.compile_session(settings))
        self.session['settings'] = {**recover_live.DEFAULTS, **settings}
        batch.save(self.state / 'session.json', self.session)
        self.assertEqual(self.run_recovery()['status'], 'no_work')

    def test_noncanonical_executable_spelling_refuses(self):
        folder = self.request()
        plan = {**self.settings, 'jobs': [self.fx.job('task')]}
        executable = self.fx.exe
        plan['jobs'][0]['argv'][0] = str(executable.parent) + '/./' + executable.name
        batch.save(folder / 'plan.json', plan)
        self.refused('noncanonical executable')

    def test_validated_file_size_bound(self):
        folder = self.request()
        batch.save(folder / 'validated.hrp', b' ' * (512 * 1024 + 1))
        self.refused('oversized')

    def test_processed_request_capacity_128_and_129(self):
        for _ in range(128):
            (self.state / 'processed' / str(uuid.uuid4())).mkdir()
        self.assertEqual(self.run_recovery()['status'], 'no_work')
        (self.state / 'processed' / str(uuid.uuid4())).mkdir()
        self.refused('processed request capacity')

    def test_all_64_accepted_jobs_and_no_65th(self):
        for index in range(64):
            self.request(f'job-{index}')
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        self.assertEqual(len(report['eligible']), 64)
        # Check admission discovery directly without disturbing the reservation.
        self.fx.fleet('queued', 'overflow', priority=31)
        with self.assertRaisesRegex(recover.RecoveryError, 'capacity exceeded'):
            recover_live._queues(recover._Files(), self.state, self.fx.run)

    def test_processed_evidence_mutation_refuses(self):
        folder = self.request()
        original = recover._classify
        def changed(*args):
            result = original(*args)
            batch.save(folder / 'validated.hrp', b'changed evidence')
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('changed')

    def test_output_parent_mutation_refuses(self):
        self.request()
        original = recover._classify
        def changed(*args):
            result = original(*args)
            batch.save(self.fx.base / 'new-neighbor', b'changed parent')
            return result
        with mock.patch.object(recover, '_classify', side_effect=changed):
            self.refused('output parent changed')

    def test_output_parent_with_stable_directory_metadata_refuses(self):
        self.original_signature = recover._signature
        with mock.patch.object(recover, '_signature', side_effect=self.stable_directory_signature):
            self.test_output_parent_mutation_refuses()

    def test_duplicate_attempt_nonce_refuses(self):
        self.request()
        self.fx.start()
        self.fx.checkpoint()
        self.fx.fleet('resume_queued', 'task', 1, 1)
        self.fx.nonces['task', 2] = self.fx.nonces['task', 1]
        self.fx.start(number=2)
        self.fx.checkpoint(number=2)
        self.refused('nonce reused')

    def test_live_diagnostic_identity_fields_are_strict(self):
        self.fx.fleet_records[-1]['priority'] = 1
        self.fx.flush_fleet()
        self.refused('fleet event identity')

    def test_live_ledger_still_enforces_original_queue_order(self):
        self.request()
        self.request('later')
        plan = {**self.settings, 'jobs': list(reversed(self.fx.plan['jobs']))}
        with self.assertRaisesRegex(recover.RecoveryError, 'queue order changed'):
            recover._fleet(recover._Files(), self.state, plan, self.fx.run, live=True)

    def test_batch_ledger_does_not_recognize_live_diagnostics(self):
        self.request()
        with self.assertRaisesRegex(recover.RecoveryError, 'unknown fleet event: listening'):
            recover._fleet(recover._Files(), self.state, self.fx.plan, self.fx.run)

    def test_output_publication_race_keeps_marker_and_other_bytes(self):
        self.request()
        original = recover._publish_new
        def changed(path, body):
            if path == self.output:
                batch.save(path, b'other writer')
            return original(path, body)
        with mock.patch.object(recover, '_publish_new', side_effect=changed):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused')
        self.assertTrue((self.state / 'recovery-reserved.json').exists())
        self.assertEqual(self.output.read_bytes(), b'other writer')
        self.assertTrue(Path(str(self.output) + '.json').is_file())

    def test_existing_companion_refuses_without_overwriting_or_reserving(self):
        self.request()
        companion = Path(str(self.output) + '.json')
        batch.save(companion, b'keep companion')
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused')
        self.assertIn('plan JSON output already exists', report['error'])
        self.assertEqual(companion.read_bytes(), b'keep companion')
        self.assertFalse(self.output.exists())
        self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_companion_cannot_replace_caller_settings(self):
        self.request()
        self.output = self.fx.base / 'settings'
        before = self.settings_path.read_bytes()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'refused')
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.state / 'recovery-reserved.json').exists())

    def test_binary_failure_keeps_companion_and_reservation_and_refuses_retry(self):
        self.request()
        original = recover._publish_new
        def changed(path, body):
            if path == self.output:
                raise OSError('binary publication failed')
            return original(path, body)
        with mock.patch.object(recover, '_publish_new', side_effect=changed):
            report = self.refused('binary publication failed', reserved=True)
        companion = Path(report['plan_json'])
        saved = companion.read_bytes()
        self.assertEqual(headroom.compile_plan(headroom.strict_json_loads(saved)),
                         headroom.compile_plan(self.fx.plan))
        self.output = self.fx.base / 'retry.hrp'
        self.refused('duplicate recovery', reserved=True)
        self.assertEqual(companion.read_bytes(), saved)

    def test_companion_publication_race_preserves_other_bytes_and_marker(self):
        self.request()
        original = recover._publish_new
        companion = Path(str(self.output) + '.json')
        def changed(path, body):
            self.assertEqual(path, companion)
            batch.save(path, b'other companion writer')
            return original(path, body)
        with mock.patch.object(recover, '_publish_new', side_effect=changed):
            self.refused(reserved=True)
        self.assertEqual(companion.read_bytes(), b'other companion writer')
        self.output = self.fx.base / 'retry.hrp'
        self.refused('duplicate recovery', reserved=True)

    def test_failure_after_both_publications_keeps_both_and_refuses(self):
        self.request()
        original = recover._publish_new
        def changed(path, body):
            original(path, body)
            if path == self.output:
                raise OSError('publication interrupted')
        with mock.patch.object(recover, '_publish_new', side_effect=changed):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused')
        self.assertIsNone(report['output'])
        companion = Path(report['plan_json'])
        self.assertEqual(headroom.compile_plan(headroom.strict_json_loads(companion.read_bytes())),
                         self.output.read_bytes())
        self.assertTrue((self.state / 'recovery-reserved.json').exists())

    def test_published_companion_mutation_prevents_binary_publication(self):
        self.request()
        original = recover._publish_new
        companion = Path(str(self.output) + '.json')
        def changed(path, body):
            self.assertEqual(path, companion)
            original(path, body)
            batch.save(path, body + b' ')
        with mock.patch.object(recover, '_publish_new', side_effect=changed):
            self.refused('published batch JSON', reserved=True)
        self.assertTrue(companion.exists())

    def test_companion_mutation_during_binary_publication_refuses_and_retains_both(self):
        self.request()
        original = recover._publish_new
        companion = Path(str(self.output) + '.json')
        def changed(path, body):
            original(path, body)
            if path == self.output:
                batch.save(companion, companion.read_bytes() + b' ')
        with mock.patch.object(recover, '_publish_new', side_effect=changed):
            report = self.run_recovery()
        self.assertEqual(report['status'], 'refused')
        self.assertIn('evidence changed', report['error'])
        self.assertTrue(companion.exists())
        self.assertTrue(self.output.exists())
        self.assertTrue((self.state / 'recovery-reserved.json').exists())

    def test_companion_recompile_drift_after_publication_keeps_marker(self):
        self.request()
        original_compile = headroom.compile_plan
        original_publish = recover._publish_new
        published = False
        def compile_changed(plan):
            payload = original_compile(plan)
            return payload + b'drift' if published else payload
        def publish_changed(path, body):
            nonlocal published
            original_publish(path, body)
            published = True
        with mock.patch.object(headroom, 'compile_plan', side_effect=compile_changed), \
             mock.patch.object(recover, '_publish_new', side_effect=publish_changed):
            self.refused('published batch JSON', reserved=True)
        self.assertTrue(Path(str(self.output) + '.json').exists())

    def test_recovered_companion_is_valid_input_for_later_batch_recovery(self):
        self.request()
        self.fx.start()
        self.fx.checkpoint()
        report = self.run_recovery()
        self.assertEqual(report['status'], 'prepared', report)
        companion = Path(report['plan_json'])
        recovered = headroom.strict_json_loads(companion.read_bytes())
        self.assertEqual(recovered['jobs'][0]['argv'], self.fx.job('task')['resume_argv'])
        self.assertEqual(recovered['jobs'][0]['max_resumes'], 1)
        second = batch.RecoveryTests()
        second.setUp()
        self.addCleanup(second.doCleanups)
        second.plan = recovered
        second.reset_state()
        later = recover.prepare_recovery(companion, second.state, second.output,
                                         safety=second.safety)
        self.assertEqual(later['status'], 'prepared', later)
        self.assertEqual(second.output.read_bytes(), self.output.read_bytes())

    def test_cli_reports_both_prepared_output_paths(self):
        self.request()
        with mock.patch.object(recover, 'WindowsSafety', return_value=self.fx.safety), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = recover_live.main([str(self.settings_path), str(self.state), str(self.output)])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'prepared')
        self.assertEqual(report['output'], str(self.output))
        self.assertEqual(report['plan_json'], str(self.output) + '.json')

    def test_cli_uses_real_safety_by_default_and_reports_refusal(self):
        with mock.patch.object(recover, 'WindowsSafety', return_value=batch.FakeSafety(busy=True)), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = recover_live.main([str(self.settings_path), str(self.state), str(self.output)])
        self.assertEqual(code, 73)
        self.assertEqual(json.loads(output.getvalue())['status'], 'refused')


if __name__ == '__main__':
    unittest.main()
