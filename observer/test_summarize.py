"""Synthetic multi-segment offline summaries; no installed agent data is used."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import report
import summarize
from test_report import START, SECRET, group, header, line, sample


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='observer-summary-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / 'output'
        self.output.mkdir()

    def segment(self, name, shift=0, private=123456, collector_start=100, collector_end=200, partial=False):
        data = self.base / name
        data.mkdir()
        start = header()
        start['started_unix_ms'] += shift
        start['deadline_unix_ms'] += shift
        a = sample(0, [group(private=private)], collector_cpu=collector_start)
        b = sample(5000, [group(private=private, cpu=10010)], incomplete=partial, collector_cpu=collector_end)
        a['ts_ms'] += shift
        b['ts_ms'] += shift
        end = {'type': 'end', 'ts_ms': START + shift + 6000, 'reason': 'requested'}
        (data / 'samples.jsonl').write_bytes(b''.join(line(value) for value in (start, a, b, end)))
        completed = {'version': 1, 'sampling_reason': 'requested', 'sampling_end_written': True,
                     'report_generated': True, 'report_exit_code': 0, 'ended_unix_ms': end['ts_ms'] + 100}
        (data / 'completion.json').write_bytes(line(completed))
        (data / 'events.jsonl').write_bytes(line({'kind': 'human_prompt_candidate',
                                                 'provenance': 'project_session', 'ts_ms': START + shift + 2500}))
        return data

    def test_combined_metrics_preserve_restart_boundaries_and_privacy(self):
        first = self.segment('SENSITIVE_FIRST')
        second = self.segment('SENSITIVE_SECOND', shift=10000, private=900000, collector_start=800, collector_end=1000)
        result = summarize.summarize([second, first], self.output)
        self.assertEqual(result['samples'], 4)
        self.assertEqual(result['metrics']['observed_ms'], 10000)
        self.assertEqual(result['metrics']['collector_cpu_100ns'], 300)
        self.assertEqual(result['metrics']['cpu_100ns'], 20000)
        self.assertEqual(result['metrics']['peak_private_bytes'], 900000)
        self.assertEqual(result['metrics']['boundary_unmeasured_ms'], 5000)
        self.assertEqual(result['metrics']['sample_span_ms'], 15000)
        self.assertEqual(result['event_records_by_kind']['human_prompt_candidate'], 2)
        text = '\n'.join(path.read_text(encoding='utf-8') for path in self.output.iterdir())
        for secret in ('SENSITIVE', SECRET, str(self.base), '987654321', '987654322'):
            self.assertNotIn(secret, text)
        self.assertNotIn('<script', text)
        self.assertNotIn('src="http', text)
        self.assertIn('not physical RAM', text)
        self.assertIn('did not control Claude', text)

    def test_partial_snapshot_does_not_become_observed_hours(self):
        result = summarize.summarize([self.segment('partial', partial=True)], self.output)
        self.assertEqual(result['partial_samples'], 1)
        self.assertEqual(result['metrics']['observed_ms'], 0)

    def test_duplicate_segment_is_refused(self):
        data = self.segment('one')
        with self.assertRaises(ValueError):
            summarize.summarize([data, data], self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_overlapping_segments_are_refused_without_output(self):
        a, b = self.segment('one'), self.segment('two', shift=4000)
        with self.assertRaises(ValueError):
            summarize.summarize([a, b], self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_or_missing_final_report_is_refused(self):
        data = self.segment('one')
        completed = json.loads((data / 'completion.json').read_bytes())
        completed['report_generated'] = False
        (data / 'completion.json').write_bytes(line(completed))
        with self.assertRaises(ValueError):
            summarize.summarize([data], self.output)
        (data / 'completion.json').unlink()
        with self.assertRaises(OSError):
            summarize.summarize([data], self.output)

    def test_sampling_error_or_limit_never_becomes_success_summary(self):
        for reason in ('error', 'limit'):
            with self.subTest(reason=reason):
                data = self.segment(reason)
                rows = [json.loads(row) for row in (data / 'samples.jsonl').read_bytes().splitlines()]
                rows[-1]['reason'] = reason
                (data / 'samples.jsonl').write_bytes(b''.join(line(row) for row in rows))
                completed = json.loads((data / 'completion.json').read_bytes())
                completed['sampling_reason'] = reason
                (data / 'completion.json').write_bytes(line(completed))
                with self.assertRaises(ValueError):
                    summarize.summarize([data], self.output)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_output_directory_is_never_reused(self):
        sentinel = self.output / 'summary.md'
        sentinel.write_text(SECRET)
        with self.assertRaises(ValueError):
            summarize.summarize([self.segment('one')], self.output)
        self.assertEqual(sentinel.read_text(), SECRET)

    def test_git_output_and_source_are_refused(self):
        data = self.segment('one')
        (self.output / '.git').mkdir()
        with self.assertRaises(ValueError):
            summarize.summarize([data], self.output)
        (self.output / '.git').rmdir()
        (data / '.git').mkdir()
        with self.assertRaises(ValueError):
            summarize.summarize([data], self.output)

    def test_bounds_and_cli_errors_are_sanitized(self):
        data = self.segment('SENSITIVE_PATH')
        with mock.patch.object(summarize, 'MAX_RUNS', 0), self.assertRaises(ValueError):
            summarize.summarize([data], self.output)
        with mock.patch.object(report, 'MAX_SAMPLES', 1), self.assertRaises(ValueError):
            summarize.summarize([data], self.output)
        (data / 'samples.jsonl').write_bytes(line({'sensitive': SECRET}))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(summarize.main(['--runs', str(data), '--output', str(self.output)]), 1)
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertNotIn(str(self.base), stderr.getvalue())

    def test_only_owned_metadata_is_opened_never_original_histories(self):
        data = self.segment('one')
        original = report.safe_open
        def guarded(path, *args, **kwargs):
            self.assertIn(Path(path).name, ('samples.jsonl', 'completion.json', 'events.jsonl'))
            return original(path, *args, **kwargs)
        with mock.patch.object(report, 'safe_open', side_effect=guarded):
            summarize.summarize([data], self.output)


if __name__ == '__main__':
    unittest.main()
