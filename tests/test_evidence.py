import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inspect_run import inspect


def event(name, code=0, elapsed=0, active=0, identity='unique-test-run'):
    return {'version': 1, 'run_id': identity, 'event': name, 'code': code,
            'elapsed_ms': elapsed, 'active_processes': active, 'job_commit_bytes': 0}


class EvidenceTests(unittest.TestCase):
    def check(self, records, expected, tail=b''):
        with tempfile.TemporaryDirectory(prefix='headroom-evidence-') as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_bytes(b''.join(json.dumps(r).encode() + b'\n' for r in records) + tail)
            result = inspect(folder)
            self.assertEqual(result['status'], expected)
            self.assertFalse(result['process_liveness_verified'])

    def test_success_requires_final_evidence(self):
        self.check([event('prepared'), event('completed')], 'completed')

    def test_heartbeat_is_not_success_or_proof_of_liveness(self):
        self.check([event('prepared'), event('heartbeat', active=1)], 'running_or_interrupted')

    def test_truncated_tail_is_not_success(self):
        self.check([event('prepared')], 'invalid_or_incomplete_evidence', b'{"event":')

    def test_extra_bytes_after_success_invalidate_it(self):
        self.check([event('completed')], 'invalid_or_incomplete_evidence', b'broken')

    def test_stopped_zero_code_is_not_success(self):
        self.check([event('stopped')], 'stopped')

    def test_interrupted_cleanup_is_terminal_and_not_success(self):
        self.check([event('prepared'), event('interrupted', code=130)], 'interrupted')

    def test_completion_cannot_override_interrupted_terminal(self):
        self.check([event('interrupted', code=130), event('completed')], 'invalid_or_incomplete_evidence')

    def test_child_failure_remains_failure(self):
        self.check([event('completed', code=70)], 'failed')

    def test_mixed_runs_are_not_accepted(self):
        self.check([event('prepared'), event('completed', identity='other')], 'invalid_or_incomplete_evidence')

    def test_time_cannot_go_backward(self):
        self.check([event('prepared', elapsed=2), event('completed', elapsed=1)], 'invalid_or_incomplete_evidence')

    def test_live_children_cannot_be_reported_complete(self):
        self.check([event('completed', active=1)], 'running_or_interrupted')

    def test_unknown_schema_and_empty_records_are_refused(self):
        self.check([], 'invalid_or_incomplete_evidence')
        self.check([{'version': 2}], 'invalid_or_incomplete_evidence')

    def test_large_line_is_bounded(self):
        self.check([], 'invalid_or_incomplete_evidence', b' ' * 4097 + b'\n')
