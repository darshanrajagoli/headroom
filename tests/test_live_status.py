import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import headroom


def row(event, task='', attempt=0, code=0):
    return dict(version=1, run_id='fixed-live-test', event=event, elapsed_ms=0,
                task=task, priority=50 if task else 0, attempt=attempt,
                total_commit_bytes=0, available_bytes=0, code=code)


class LiveStatusTests(unittest.TestCase):
    def check(self, rows, expected):
        with tempfile.TemporaryDirectory(prefix='headroom-live-status-') as directory:
            (Path(directory) / 'fleet.jsonl').write_text(
                ''.join(json.dumps(r) + '\n' for r in rows))
            report = headroom.read_status(directory)
            self.assertEqual(report['status'], expected, report)
            self.assertFalse(report['process_liveness_verified'])
            return report

    def test_empty_open_session_does_not_claim_liveness_or_completion(self):
        self.check([row('prepared'), row('listening')], 'running_or_interrupted')

    def test_empty_sealed_session_is_no_work(self):
        report = self.check([row('prepared'), row('listening'), row('sealed'), row('finished')], 'no_work')
        self.assertEqual(report['jobs'], [])
        self.assertTrue(report['evidence_complete'])

    def test_live_success_needs_seal_even_after_all_current_jobs_finish(self):
        rows = [row('prepared'), row('listening'), row('queued', 'one'),
                row('started', 'one', 1, 100), row('completed', 'one', 1)]
        self.check(rows, 'running_or_interrupted')
        self.check(rows + [row('finished')], 'invalid_or_incomplete_evidence')
        self.check(rows + [row('sealed'), row('finished')], 'completed')

    def test_later_queue_while_first_runs_is_valid(self):
        self.check([row('prepared'), row('listening'), row('queued', 'one'),
                    row('started', 'one', 1, 100), row('queued', 'two'),
                    row('completed', 'one', 1), row('sealed'),
                    row('started', 'two', 1, 200), row('completed', 'two', 1),
                    row('finished')], 'completed')

    def test_no_queue_or_second_seal_after_seal(self):
        prefix = [row('prepared'), row('listening'), row('sealed')]
        self.check(prefix + [row('queued', 'late')], 'invalid_or_incomplete_evidence')
        self.check(prefix + [row('sealed')], 'invalid_or_incomplete_evidence')

    def test_batch_cannot_claim_seal_or_open_after_queue(self):
        self.check([row('prepared'), row('sealed')], 'invalid_or_incomplete_evidence')
        self.check([row('prepared'), row('queued', 'one'), row('listening')],
                   'invalid_or_incomplete_evidence')

    def test_rejection_does_not_complete_or_fail_unaccepted_work(self):
        self.check([row('prepared'), row('listening'), row('request_rejected', code=64),
                    row('sealed'), row('finished')], 'no_work')


if __name__ == '__main__':
    unittest.main()
