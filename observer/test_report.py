"""All fixtures are synthetic and live in disposable private temp directories."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import report

START = 1700000000000  # fixed past epoch; fixtures never depend on host time-of-day
SECRET = 'PRIVATE_PROMPT_42_C:\\Users\\SENSITIVE_ID\\project_secret session_secret <script>alert(1)</script>'


def header():
    return {"type": "start", "version": 1, "interval_ms": 5000,
            "started_unix_ms": START, "deadline_unix_ms": START + 3600000,
            "aggregation": "root_groups", "cpu_scope": "observed_member_deltas"}


def group(pid=987654321, created=111, private=123456, working=234567, cpu=10, members=2):
    return {"pid": pid, "ppid": 987654322, "created_100ns": created,
            "root_pid": pid, "root_created_100ns": created, "name": "claude.exe",
            "private_bytes": private, "working_set_bytes": working,
            "cpu_100ns": cpu, "member_count": members}


def sample(offset=0, groups=None, incomplete=False, collector_cpu=100):
    return {"type": "sample", "ts_ms": START + offset, "host_total_bytes": 16 * report.MIB,
            "host_available_bytes": 8 * report.MIB, "collector_private_bytes": 10001,
            "collector_working_set_bytes": 20002, "collector_cpu_100ns": collector_cpu,
            "processes": [group()] if groups is None else groups,
            "claude_foreground": True, "snapshot_incomplete": incomplete,
            "unknown_sensitive": SECRET}


def user_event(offset=2500, **updates):
    value = {"type": "user", "timestamp": START + offset, "isMeta": False, "isSidechain": False,
             "sessionId": SECRET, "cwd": SECRET,
             "message": {"role": "user", "content": [{"type": "text", "text": SECRET}]}}
    value.update(updates)
    return value


def line(value):
    return (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')


class ParserTests(unittest.TestCase):
    def test_escaped_keys_and_unknown_payload_never_decoded(self):
        raw = (b'{"display":"DO_NOT_DECODE\\\" } [ \\u263a", "pastedContents":{"x":"DO_NOT_DECODE"},'
               b'"sessionId":"DO_NOT_DECODE", "project":"DO_NOT_DECODE",'
               b'"\\u0074imestamp":' + str(START).encode() + b'}\n')
        original = json.loads
        decoded = []
        def track(value, *args, **kwargs):
            decoded.append(value)
            return original(value, *args, **kwargs)
        with mock.patch.object(report.json, 'loads', side_effect=track):
            result = report.event_metadata(raw, 'claude_code_history')
        self.assertEqual(result['ts_ms'], START)
        self.assertFalse(any('DO_NOT_DECODE' in str(x) for x in decoded))

    def test_project_candidates_exclude_tools_meta_sidechain(self):
        cases = [user_event(), user_event(message={"role": "user", "content": SECRET}),
                 user_event(isMeta=True), user_event(isSidechain=True),
                 user_event(type='assistant'), user_event(message={"role": "assistant", "content": SECRET}),
                 user_event(message={"role": "user", "content": [{"type": "tool_result", "content": SECRET}]}),
                 user_event(message={"role": "user", "content": [{"type": "text", "text": SECRET}, {"type": "tool_result"}]}),
                 user_event(message={"role": "user", "content": []}),
                 {"type": "last-prompt", "lastPrompt": SECRET}]
        self.assertEqual([report.event_metadata(line(c), 'project_session') is not None for c in cases],
                         [True, True, False, False, False, False, False, False, False, False])

    def test_timestamp_numeric_ms_and_iso(self):
        self.assertEqual(report.timestamp('2026-10-27T19:00:00+05:30'), report.timestamp('2026-10-27T13:30:00Z'))
        self.assertEqual(report.timestamp(1700000000), 1700000000)
        self.assertIsNone(report.timestamp(True))
        self.assertIsNone(report.timestamp('2026-10-27T13:30:00'))
        self.assertIsNone(report.timestamp('PRIVATE'))
        self.assertIsNone(report.timestamp(float('inf')))

    def test_invalid_json_and_depth_are_rejected(self):
        bad = [b'{"timestamp":1,"timestamp":2}', b'{"timestamp":01}', b'{"timestamp":NaN}',
               b'{"x":"bad\\q","timestamp":1}', b'{"x":"\xff","timestamp":1}',
               b'{"timestamp":1}junk', b'{"x":' + b'[' * 35 + b'0' + b']' * 35 + b'}']
        for raw in bad:
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                report.event_metadata(raw, 'claude_code_history')


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='observer-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        clock = mock.patch.object(report.time, 'time', return_value=(START + 5000) / 1000)
        clock.start()
        self.addCleanup(clock.stop)
        self.data = self.base / 'private'
        self.data.mkdir()
        self.history = self.base / 'synthetic-history.jsonl'
        self.projects = self.base / 'synthetic-projects'
        self.project = self.projects / 'SENSITIVE_PROJECT_PATH'
        self.project.mkdir(parents=True)
        self.session = self.project / 'SENSITIVE_SESSION_ID.jsonl'
        self.write_run([sample(), sample(5000, [group(private=345678, working=456789, cpu=10000010)], collector_cpu=200)])

    def write_run(self, samples, reason='deadline', suffix=b''):
        values = [header(), *samples]
        if reason:
            values.append({"type": "end", "ts_ms": START + 6000 if not samples else max(START + 6000, samples[-1]['ts_ms']), "reason": reason})
        self.raw = b''.join(line(v) for v in values) + suffix
        (self.data / 'samples.jsonl').write_bytes(self.raw)

    def output_text(self):
        return '\n'.join((self.data / name).read_text(encoding='utf-8')
                         for name in ('report.md', 'report.html', 'resume.txt', 'manifest.json'))

    def test_exact_values_privacy_static_html_and_no_raw_overwrite(self):
        self.history.write_bytes(line({"display": SECRET, "pastedContents": {"text": SECRET},
                                       "timestamp": START + 2500, "project": SECRET, "sessionId": SECRET}))
        result = report.generate_report(self.data, self.history, True)
        self.assertEqual(result['metrics']['observed_ms'], 5000)
        self.assertEqual(result['metrics']['peak_private_bytes'], 345678)
        self.assertEqual(result['metrics']['peak_working_set_bytes'], 456789)
        self.assertEqual(result['metrics']['cpu_100ns'], 10000000)
        self.assertEqual(result['metrics']['collector_cpu_100ns'], 100)
        text = self.output_text()
        self.assertIn('123456 / 234567', text)
        self.assertIn('345678 / 456789', text)
        for secret in (SECRET, 'SENSITIVE_ID', 'session_secret', 'project_secret', str(self.base), '987654321', '987654322'):
            self.assertNotIn(secret, text)
        document = (self.data / 'report.html').read_text(encoding='utf-8')
        self.assertIn('<svg', document)
        self.assertNotIn('<script', document)
        self.assertNotIn('<iframe', document)
        self.assertNotIn('src="http', document)
        self.assertEqual((self.data / 'samples.jsonl').read_bytes(), self.raw)
        self.assertIn('not causal', text)
        self.assertIn('Zero events do not prove idle time', text)

    def test_pid_reuse_root_change_and_negative_cpu(self):
        self.write_run([sample(), sample(5000, [group(created=222, cpu=90000000)]),
                        sample(10000, [group(pid=123, created=333, cpu=50)]),
                        sample(15000, [group(pid=123, created=333, cpu=40)]),
                        sample(20000, [group(pid=123, created=333, cpu=80)])])
        m = report.metrics(report.load_run(self.data))
        self.assertEqual(m['cpu_100ns'], 40)
        self.assertEqual(m['cpu_resets'], 1)
        self.assertEqual(m['group_changes'], 2)
        self.assertEqual(m['observed_ms'], 10000)

    def test_gaps_and_partial_empty_are_unknown(self):
        self.write_run([sample(), sample(5000), sample(300000), sample(305000, [], True), sample(310000)])
        run = report.load_run(self.data)
        m = report.metrics(run)
        self.assertEqual(m['gap_ms'], 295000)
        self.assertEqual(m['gap_count'], 1)
        self.assertEqual(m['observed_ms'], 5000)
        self.assertNotIn('870.00,250.00', report.svg_plot(run))
        result = report.generate_report(self.data)
        self.assertEqual(result['partial_samples'], 1)

    def test_no_claude_no_history_and_no_samples(self):
        self.write_run([sample(0, []), sample(5000, [])])
        result = report.generate_report(self.data)
        self.assertEqual(result['metrics']['observed_ms'], 0)
        self.assertEqual(result['metrics']['peak_private_bytes'], 0)
        self.assertEqual(result['event_coverage']['source'], 'none')
        self.write_run([])
        result = report.generate_report(self.data, self.history)
        self.assertIsNone(result['metrics']['peak_private_bytes'])
        self.assertIn('No samples available', (self.data / 'report.html').read_text())

    def test_truncated_native_and_live_refresh_never_infer_deadline(self):
        self.write_run([sample()], reason=None, suffix=b'{"type":"sam')
        result = report.generate_report(self.data, refresh=True)
        self.assertIn('Live / incomplete', result['status'])
        self.assertTrue(result['partial_tail'])
        self.assertEqual(result['sample_count'], 1)
        self.assertNotIn('Completed', self.output_text())

    def test_helpers_before_and_after_end_are_diagnostics_only(self):
        helper = {"type": "helper", "ts_ms": START + 7000, "final": True, "exit_code": 0}
        with (self.data / 'samples.jsonl').open('ab') as handle:
            handle.write(line(helper))
        self.assertEqual(report.generate_report(self.data)['status'], 'Completed: deadline')
        with (self.data / 'samples.jsonl').open('ab') as handle:
            handle.write(b'{"type":"helper","final":')
        result = report.generate_report(self.data)
        self.assertEqual(result['status'], 'Completed: deadline')
        self.assertTrue(result['partial_tail'])
        self.write_run([sample()], reason=None, suffix=line(helper))
        self.assertIn('Incomplete', report.generate_report(self.data)['status'])
        for reason in ('requested', 'limit', 'error'):
            self.write_run([sample()], reason=reason)
            self.assertEqual(report.generate_report(self.data)['status'], 'Stopped: ' + reason)

    def test_native_caps_and_bad_rows(self):
        bad = sample(groups=[group(pid=i + 1) for i in range(17)])
        self.write_run([bad, sample(5000)])
        run = report.load_run(self.data)
        self.assertEqual(len(run.samples), 1)
        self.assertEqual(run.invalid, 1)
        self.write_run([sample()], reason=None)
        with (self.data / 'samples.jsonl').open('ab') as handle:
            handle.write(b'x' * (report.MAX_NATIVE_LINE + 10) + b'\n')
        self.assertEqual(report.load_run(self.data).invalid, 1)
        with mock.patch.object(report, 'MAX_DATA', 30):
            with self.assertRaises(ValueError):
                report.load_run(self.data)

    def test_history_tail_first_fragment_last_fragment_and_range(self):
        rows = [line({"display": SECRET, "timestamp": START - 1}),
                line({"display": SECRET, "timestamp": START + 2500}),
                line({"display": SECRET, "timestamp": START + 5001})]
        self.history.write_bytes(b'bad-leading-fragment' + b''.join(rows) + b'{"timestamp":')
        with mock.patch.object(report, 'MAX_HISTORY', len(b''.join(rows)) + 10):
            result = report.generate_report(self.data, self.history, True)
        self.assertEqual(result['event_records'], 1)
        self.assertTrue(result['event_coverage']['tail_limited'])
        self.assertTrue(result['event_coverage']['partial_tail'])

    def test_correlation_limit_exact_time_and_no_nearby_samples(self):
        samples = [report.sample_record(sample()), report.sample_record(sample(300000))]
        events = [{"ts_ms": START + 150000}, {"ts_ms": START}]
        rows = report.correlate(samples, events)
        self.assertEqual(rows[0][1:], (None, None))
        self.assertIs(rows[1][1], rows[1][2])

    def test_collect_incremental_candidates_and_private_cursors(self):
        self.session.write_bytes(line(user_event()) + line(user_event(type='assistant')))
        self.history.write_bytes(line({"timestamp": START + 2600, "display": SECRET}))
        first = report.collect_events(self.data, self.projects, self.history, START + 5000)
        self.assertEqual(first['written'], 2)
        feed = (self.data / 'events.jsonl').read_text()
        self.assertNotIn('SENSITIVE', feed)
        self.assertNotIn(SECRET, feed)
        self.assertIn('human_prompt_candidate', feed)
        state = json.loads((self.data / '.event-cursors.json').read_text())
        self.assertIn(report.cursor_id(state, self.session), state['cursors'])
        self.assertNotIn('SENSITIVE', json.dumps(state))
        self.assertNotIn(str(self.base), json.dumps(state))
        self.assertEqual(report.collect_events(self.data, self.projects, self.history, START + 5000)['written'], 0)
        with self.session.open('ab') as handle:
            handle.write(line(user_event(3000)))
        self.assertEqual(report.collect_events(self.data, self.projects, self.history, START + 5000)['written'], 1)
        result = report.generate_report(self.data, self.history)
        self.assertEqual(result['event_records'], 3)
        self.assertEqual(result['event_coverage']['source'], 'collected metadata')
        self.assertNotIn('SENSITIVE', self.output_text())

    def test_incomplete_event_retried_without_duplicate(self):
        raw = line(user_event())
        self.session.write_bytes(raw[:-10])
        self.assertEqual(report.collect_events(self.data, self.projects, now_ms=START + 5000)['written'], 0)
        with self.session.open('ab') as handle:
            handle.write(raw[-10:])
        self.assertEqual(report.collect_events(self.data, self.projects, now_ms=START + 5000)['written'], 1)
        self.assertEqual(report.collect_events(self.data, self.projects, now_ms=START + 5000)['written'], 0)

    def test_rotation_and_oversized_record_progress(self):
        self.session.write_bytes(line(user_event()) * 3)
        report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.session.write_bytes(line(user_event(3000)))
        result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(result['coverage']['rotations'], 1)
        self.assertEqual(result['written'], 1)
        with self.session.open('ab') as handle:
            handle.write(b'{"display":"' + b'x' * (report.MAX_EVENT_LINE + 30) + b'"}\n' + line(user_event(3500)))
        result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(result['coverage']['oversized_lines'], 1)
        self.assertEqual(result['written'], 1)

    def test_tail_and_per_file_aggregate_budget_and_no_subagents(self):
        nested = self.project / 'subagents'
        nested.mkdir()
        (nested / 'agent.jsonl').write_bytes(line(user_event()))
        self.session.write_bytes(b'x' * (2 * report.MIB) + b'\n' + line(user_event()))
        result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(result['written'], 1)
        self.assertEqual(result['coverage']['initial_tail'], 1)
        state = json.loads((self.data / '.event-cursors.json').read_text())
        self.assertEqual(len(state['cursors']), 1)
        # Later reads must stop at the configured total budget, retaining progress.
        with self.session.open('ab') as handle:
            handle.write(line(user_event()) * 10)
        with mock.patch.object(report, 'COLLECT_BUDGET', 600):
            result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertGreater(result['coverage']['lagged_files'], 0)
        self.assertLess(result['written'], 10)

    def test_scan_selection_feed_and_state_bounds(self):
        for i in range(4):
            (self.project / f'{i}.jsonl').write_bytes(line(user_event()))
        with mock.patch.object(report, 'MAX_FILES', 2):
            counts = dict.fromkeys(report.GAP_KEYS, 0)
            self.assertEqual(len(report.discover_projects(self.projects, START, counts)), 2)
            self.assertEqual(counts['selection_limit'], 2)
        with mock.patch.object(report, 'MAX_SCAN', 2):
            counts = dict.fromkeys(report.GAP_KEYS, 0)
            report.discover_projects(self.projects, START, counts)
            self.assertEqual(counts['scan_limit'], 1)
        with mock.patch.object(report, 'MAX_HISTORY', 2200):
            result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
            self.assertGreater(result['coverage']['feed_limit'], 0)
            self.assertLessEqual((self.data / 'events.jsonl').stat().st_size, 2200)
        (self.data / '.event-cursors.json').write_bytes(b'x' * 20)
        with mock.patch.object(report, 'MAX_STATE', 10), self.assertRaises(ValueError):
            report.load_state(self.data / '.event-cursors.json', START)

    def test_cli_contract_and_sanitized_failure(self):
        self.session.write_bytes(line(user_event()))
        args = ['--data', str(self.data), '--projects', str(self.projects), '--since-start']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(args + ['--collect-events']), 0)
            self.assertEqual(report.main(args), 0)
        (self.data / 'samples.jsonl').write_bytes(b'{"secret": "SENSITIVE_ID"}\n')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(report.main(args), 1)
        self.assertNotIn('SENSITIVE_ID', output.getvalue())
        self.assertNotIn(str(self.data), output.getvalue())

    def test_git_output_rejected(self):
        (self.base / '.git').mkdir()
        with self.assertRaises(ValueError):
            report.checked_data(self.data)

    def test_hardlink_and_credential_sources_are_not_read(self):
        original = self.project / 'original.jsonl'
        original.write_bytes(line(user_event()))
        linked = self.project / 'linked.jsonl'
        try:
            os.link(original, linked)
        except OSError:
            self.skipTest('hard links unavailable in fixture filesystem')
        with self.assertRaises(ValueError):
            report.safe_open(linked)
        result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(result['written'], 0)
        self.assertGreater(result['coverage']['invalid_records'], 0)
        (self.project / 'credentials.jsonl').write_bytes(line(user_event()))
        result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(result['written'], 0)
        with self.assertRaises(ValueError):
            report.collect_events(self.data, history=self.project / 'credentials.jsonl')

    def test_linked_ancestors_and_symlinks_rejected(self):
        target = self.base / 'target.jsonl'
        target.write_bytes(line(user_event()))
        linked = self.base / 'symlink.jsonl'
        try:
            linked.symlink_to(target)
        except OSError:
            self.skipTest('symlinks require Windows developer mode or privilege')
        with self.assertRaises(ValueError):
            report.safe_open(linked)
        directory_link = self.base / 'linked-dir'
        directory_link.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(ValueError):
            report.checked_data(directory_link)

    def test_preview_throttled_and_final_uses_only_collected_feed(self):
        args = ['--data', str(self.data), '--history', str(self.history), '--since-start']
        self.history.write_bytes(line({"timestamp": START + 2500, "display": SECRET}))
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(report, 'generate_report', wraps=report.generate_report) as generate:
            self.assertEqual(report.main(args + ['--collect-events']), 0)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(report.main(args + ['--collect-events']), 0)
            self.assertEqual(generate.call_count, 1)
            old = report.time.time() - 301
            os.utime(self.data / 'report.html', (old, old))
            self.assertEqual(report.main(args + ['--collect-events']), 0)
            self.assertEqual(generate.call_count, 2)
            with self.history.open('ab') as handle:
                handle.write(line({"timestamp": START + 3000, "display": SECRET}))
            self.assertEqual(report.main(args), 0)
            self.assertEqual(generate.call_count, 3)
        self.assertEqual(json.loads((self.data / 'manifest.json').read_text())['event_records'], 1)

    def test_per_file_bound_and_long_line_discard_cursor(self):
        self.session.write_bytes(line(user_event()))
        report.collect_events(self.data, self.projects, now_ms=START + 5000)
        with self.session.open('ab') as handle:
            handle.write(b'x' * 1100 + b'\n' + line(user_event(3500)))
        with mock.patch.object(report, 'PER_FILE', 300), mock.patch.object(report, 'MAX_EVENT_LINE', 128):
            first = report.collect_events(self.data, self.projects, now_ms=START + 5000)
            self.assertEqual(first['coverage']['oversized_lines'], 1)
            state = json.loads((self.data / '.event-cursors.json').read_text())
            self.assertTrue(state['cursors'][report.cursor_id(state, self.session)]['discarding'])
            for _ in range(4):
                report.collect_events(self.data, self.projects, now_ms=START + 5000)
        # The following candidate itself exceeds mocked 128-byte record size;
        # restoring limits lets a newly appended candidate be observed.
        with self.session.open('ab') as handle:
            handle.write(line(user_event(4000)))
        final = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(final['written'], 1)

    def test_all_partial_hours_are_conservative(self):
        self.write_run([sample(incomplete=True), sample(5000, incomplete=True)])
        result = report.generate_report(self.data)
        self.assertEqual(result['metrics']['observed_ms'], 0)
        self.assertEqual(result['partial_samples'], 2)
        self.assertIn('lower bounds', self.output_text())

    def test_missing_sources_and_untrusted_feed_do_not_leak(self):
        result = report.generate_report(self.data, self.history)
        self.assertEqual(result['event_coverage']['source'], 'unavailable')
        (self.data / 'events.jsonl').write_bytes(line({"kind": SECRET, "ts_ms": START + 2500,
                                                     "provenance": SECRET}))
        result = report.generate_report(self.data)
        self.assertEqual(result['event_records'], 0)
        self.assertEqual(result['event_coverage']['invalid'], 1)
        self.assertNotIn(SECRET, self.output_text())

    def test_deadline_never_opens_sources_or_cursor_state(self):
        original = report.safe_open
        opened = []
        def track(path, *args, **kwargs):
            opened.append(Path(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(report, 'safe_open', side_effect=track):
            result = report.collect_events(self.data, self.projects, self.history, START + 3600000)
        self.assertTrue(result['deadline_reached'])
        self.assertEqual(opened, [self.data / 'samples.jsonl'])
        self.assertFalse((self.data / 'events.jsonl').exists())
        with mock.patch.object(report.time, 'time', return_value=(START + 3600000) / 1000):
            with mock.patch.object(report, 'collect_events', side_effect=AssertionError('final must not collect')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(report.main(['--data', str(self.data), '--history', str(self.history),
                                                  '--projects', str(self.projects)]), 0)

    def test_same_inode_rewrite_regrown_above_cursor_detected(self):
        self.session.write_bytes(line(user_event()) * 3)
        report.collect_events(self.data, self.projects, now_ms=START + 5000)
        identity = self.session.stat().st_ino
        self.session.write_bytes(line(user_event(3000)) * 4)
        self.assertEqual(self.session.stat().st_ino, identity)
        result = report.collect_events(self.data, self.projects, now_ms=START + 5000)
        self.assertEqual(result['coverage']['rotations'], 1)
        self.assertEqual(result['written'], 4)

    def test_helper_failures_do_not_change_sampling_status(self):
        with (self.data / 'samples.jsonl').open('ab') as handle:
            handle.write(line({'type': 'helper', 'final': True, 'exit_code': 70, 'start_failed': True}))
        result = report.generate_report(self.data)
        self.assertEqual(result['helper_failures'], 1)
        self.assertEqual(result['status'], 'Completed: deadline')
        self.assertIn('helper failures: 1', self.output_text())


if __name__ == '__main__':
    unittest.main()
