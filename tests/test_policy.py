import ctypes as c
from pathlib import Path
import random
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Job(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ('state', 'priority', 'allow_stop', 'valid')] + [
        (name, c.c_uint64) for name in ('claim', 'commit', 'working_set')]


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dll = c.CDLL(str(ROOT / 'build' / 'headroom-policy.dll'))
        cls.dll.hr_next.argtypes = [c.POINTER(Job), c.c_size_t] + [c.c_uint64] * 3 + [c.c_int]
        cls.dll.hr_next.restype = c.c_int
        cls.dll.hr_victim.argtypes = [c.POINTER(Job), c.c_size_t] + [c.c_uint64] * 4 + [c.c_int]
        cls.dll.hr_victim.restype = c.c_int

    def next(self, jobs, budget=100, available=1000, reserve=0, valid=1):
        array = (Job * len(jobs))(*jobs)
        return self.dll.hr_next(array, len(jobs), budget, available, reserve, valid)

    def victim(self, jobs, total=160, budget=100, available=1000, reserve=0, valid=1):
        array = (Job * len(jobs))(*jobs)
        return self.dll.hr_victim(array, len(jobs), total, budget, available, reserve, valid)

    def test_priority_and_fifo(self):
        jobs = [Job(0, 10, 0, 1, 40, 0, 0), Job(0, 90, 0, 1, 40, 0, 0), Job(0, 90, 0, 1, 40, 0, 0)]
        self.assertEqual(self.next(jobs), 1)

    def test_no_backfill_past_blocked_head(self):
        jobs = [Job(1, 50, 0, 1, 60, 60, 60), Job(0, 90, 0, 1, 60, 0, 0), Job(0, 10, 0, 1, 20, 0, 0)]
        self.assertEqual(self.next(jobs), -1)

    def test_actual_growth_charges_more_than_claim(self):
        jobs = [Job(1, 10, 1, 1, 20, 80, 80), Job(0, 90, 0, 1, 30, 0, 0)]
        self.assertEqual(self.next(jobs), -1)

    def test_unspent_claims_also_reserve_physical_headroom(self):
        jobs = [Job(1, 10, 0, 1, 40, 10, 10), Job(0, 50, 0, 1, 40, 0, 0)]
        self.assertEqual(self.next(jobs, available=150, reserve=100), -1)
        self.assertEqual(self.next(jobs, available=170, reserve=100), 1)

    def test_unknown_data_and_stopping_block_admission(self):
        queued = Job(0, 50, 0, 1, 40, 0, 0)
        self.assertEqual(self.next([queued], valid=0), -1)
        self.assertEqual(self.next([Job(1, 10, 0, 0, 20, 0, 0), queued]), -1)
        self.assertEqual(self.next([Job(2, 10, 1, 1, 20, 40, 40), queued]), -1)

    def test_protected_or_honest_jobs_never_selected(self):
        self.assertEqual(self.victim([Job(1, 0, 0, 1, 20, 150, 150)]), -1)
        self.assertEqual(self.victim([Job(1, 0, 1, 1, 150, 150, 150)]), -1)

    def test_no_stop_on_healthy_fleet_even_if_job_is_overclaim(self):
        self.assertEqual(self.victim([Job(1, 0, 1, 1, 20, 80, 80)], total=80), -1)

    def test_lowest_priority_then_largest_excess(self):
        jobs = [Job(1, 90, 1, 1, 20, 150, 150), Job(1, 10, 1, 1, 20, 90, 90), Job(1, 10, 1, 1, 20, 110, 110)]
        self.assertEqual(self.victim(jobs), 2)

    def test_no_futile_stop_or_second_pending_stop(self):
        small = Job(1, 0, 1, 1, 20, 50, 50)
        self.assertEqual(self.victim([small]), -1)
        self.assertEqual(self.victim([Job(1, 0, 1, 1, 20, 90, 90), Job(2, 0, 1, 1, 20, 90, 90)]), -1)

    def test_physical_deficit_uses_working_set_not_commit(self):
        job = Job(1, 0, 1, 1, 20, 100, 40)
        self.assertEqual(self.victim([job], total=90, available=50, reserve=100), -1)
        job.working_set = 60
        self.assertEqual(self.victim([job], total=90, available=50, reserve=100), 0)

    def test_overflow_cannot_admit_or_authorize_unknown_data(self):
        self.assertEqual(self.next([Job(1, 0, 0, 1, 1, 2**64 - 1, 1), Job(0, 0, 0, 1, 1, 0, 0)]), -1)
        self.assertEqual(self.victim([Job(1, 0, 1, 0, 1, 2**64 - 1, 1)]), -1)

    def test_generated_cases_never_select_protected_or_within_claim(self):
        randomizer = random.Random(1051)
        for _ in range(1000):
            jobs = [Job(randomizer.choice([0, 1, 2, 3]), randomizer.randrange(101),
                        randomizer.randrange(2), randomizer.randrange(2),
                        randomizer.randrange(1, 100), randomizer.randrange(200), randomizer.randrange(200))
                    for _ in range(8)]
            victim = self.victim(jobs)
            if victim >= 0:
                job = jobs[victim]
                self.assertEqual(job.state, 1)
                self.assertTrue(job.allow_stop and job.valid)
                self.assertGreater(job.commit, job.claim)
                self.assertGreaterEqual(job.commit, 60)
