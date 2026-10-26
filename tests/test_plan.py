"""Bounded offline compiler/evidence tests; never launch fleet workloads."""

import contextlib
import copy
import ctypes
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom


def decode_plan(data):
    """Independent cursor decoder checks exact framing, not native execution."""
    offset = 0

    def unpack(format):
        nonlocal offset
        result = struct.unpack_from(format, data, offset)
        offset += struct.calcsize(format)
        return result

    def string(wide):
        nonlocal offset
        length, = unpack('<I')
        size = length * (2 if wide else 1)
        value = data[offset:offset + size]
        if len(value) != size:
            raise AssertionError('short string')
        offset += size
        return value.decode('utf-16le' if wide else 'ascii')

    header = unpack('<8sIQQQIII')
    jobs = []
    for _ in range(header[1]):
        identifier = string(False)
        fields = unpack('<IIIQQ')
        jobs.append((identifier, fields, *(string(True) for _ in range(4))))
    if offset != len(data):
        raise AssertionError('trailing bytes')
    return header, jobs


def event(name, *, task='', attempt=0, priority=None, code=0, elapsed=0, **extra):
    return {'version': 1, 'run_id': 'offline-test-run', 'event': name,
            'elapsed_ms': elapsed, 'task': task,
            'priority': (50 if task else 0) if priority is None else priority,
            'attempt': attempt, 'total_commit_bytes': 0,
            'available_bytes': 100 * headroom.MIB, 'code': code, **extra}


def job_event(name, *, attempt=1, **kwargs):
    return event(name, task='job', attempt=attempt, **kwargs)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='headroom-plan-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.executable = self.base / 'program with spaces.exe'
        # Only existence is required by the compiler. This is never executed.
        self.executable.write_bytes(b'offline fixture, not executable code')
        self.plan = {'budget_mib': 128, 'hard_mib': 256,
                     'jobs': [{'id': 'job', 'claim_mib': 48, 'hard_mib': 160,
                               'argv': [str(self.executable), 'argument']}]}

    def invalid(self, mutate):
        plan = copy.deepcopy(self.plan)
        mutate(plan)
        with self.assertRaises(ValueError):
            headroom.compile_plan(plan)

    def cli(self, source, output=None):
        source_path = self.base / 'input.json'
        source_path.write_bytes(source if isinstance(source, bytes) else source.encode('utf-8'))
        destination = output or self.base / 'output.hrp'
        with contextlib.redirect_stderr(io.StringIO()):
            code = headroom.main(['compile', str(source_path), str(destination)])
        return code, destination

    def test_binary_header_defaults_and_exact_framing(self):
        payload = headroom.compile_plan(self.plan)
        self.assertIs(type(payload), bytes)
        header, jobs = decode_plan(payload)
        self.assertEqual(header, (b'HRFLEET1', 1, 128 * 2**20, 256 * 2**20,
                                  256 * 2**20, 500, 2000, 60000))
        self.assertEqual(jobs, [('job', (50, 0, 0, 48 * 2**20, 160 * 2**20),
                                 str(self.executable.resolve()),
                                 f'"{self.executable.resolve()}" argument', '', '')])
        self.assertEqual(payload[-8:], b'\0' * 8)

    def test_original_fifo_order_and_resume_recipe(self):
        first = self.plan['jobs'][0]
        first.update(priority=0, allow_stop=True, max_resumes=3,
                     resume_argv=[str(self.executable), '--resume'])
        second = copy.deepcopy(first)
        second.update(id='z' * 40, priority=100, max_resumes=0,
                      resume_argv=[], allow_stop=False)
        self.plan['jobs'].append(second)
        _, jobs = decode_plan(headroom.compile_plan(self.plan))
        self.assertEqual([j[0] for j in jobs], ['job', 'z' * 40])
        self.assertEqual(jobs[0][1][:3], (0, 1, 3))
        self.assertEqual(jobs[0][4:], (str(self.executable.resolve()),
                                     f'"{self.executable.resolve()}" --resume'))
        self.assertEqual(jobs[1][1][:3], (100, 0, 0))

    def test_all_documented_boundaries(self):
        self.plan.update(budget_mib=1, hard_mib=headroom.MAX_MIB,
                         reserve_mib=0, sustain_ms=100, grace_ms=1, timeout_ms=1)
        self.plan['jobs'][0].update(claim_mib=1, hard_mib=1, id='0')
        decode_plan(headroom.compile_plan(self.plan))
        self.plan.update(reserve_mib=headroom.MAX_MIB, sustain_ms=60000,
                         grace_ms=60000, timeout_ms=86400000)
        self.plan['jobs'][0]['hard_mib'] = headroom.MAX_MIB
        decode_plan(headroom.compile_plan(self.plan))

    def test_root_numeric_validation_and_relationships(self):
        invalids = {'budget_mib': [0, -1, True, 1.0, '128', headroom.MAX_MIB + 1],
                    'hard_mib': [0, 127, 128, False, 256.0, headroom.MAX_MIB + 1],
                    'reserve_mib': [-1, True, 0.0, headroom.MAX_MIB + 1],
                    'sustain_ms': [99, 60001, True, 500.0],
                    'grace_ms': [0, 60001, False, 2000.0],
                    'timeout_ms': [0, 86400001, True, 60000.0]}
        for key, values in invalids.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.invalid(lambda p: p.update({key: value}))

    def test_job_numeric_validation(self):
        invalids = {'priority': [-1, 101, True, 50.0],
                    'claim_mib': [0, -1, 129, True, 48.0],
                    'hard_mib': [0, 47, 257, True, 160.0],
                    'max_resumes': [-1, 4, True, 0.0],
                    'allow_stop': [0, 1, 'true', None]}
        for key, values in invalids.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.invalid(lambda p: p['jobs'][0].update({key: value}))

    def test_required_and_unknown_fields_and_object_types(self):
        for key in ('budget_mib', 'hard_mib', 'jobs'):
            with self.subTest(missing=key):
                self.invalid(lambda p: p.pop(key))
        for key in ('id', 'claim_mib', 'hard_mib', 'argv'):
            with self.subTest(missing_job=key):
                self.invalid(lambda p: p['jobs'][0].pop(key))
        self.invalid(lambda p: p.update(typo=3))
        self.invalid(lambda p: p['jobs'][0].update(typo=3))
        self.invalid(lambda p: p.update({1: 'nonstring key'}))
        for value in (None, [], 42, 'plan'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                headroom.compile_plan(value)
        self.invalid(lambda p: p.update(jobs=[None]))

    def test_ids_counts_and_duplicates(self):
        for identifier in ('', 'a' * 41, '-a', 'A', 'a_b', 'a\n', 'é', True, 'a\0'):
            with self.subTest(identifier=identifier):
                self.invalid(lambda p: p['jobs'][0].update(id=identifier))
        self.invalid(lambda p: p['jobs'].append(copy.deepcopy(p['jobs'][0])))
        for value in ([], None, {}, tuple(self.plan['jobs'])):
            with self.subTest(jobs=value):
                self.invalid(lambda p: p.update(jobs=value))
        jobs = [dict(self.plan['jobs'][0], id=f'job-{i}') for i in range(65)]
        self.plan['jobs'] = jobs[:64]
        header, decoded = decode_plan(headroom.compile_plan(self.plan))
        self.assertEqual((header[1], len(decoded)), (64, 64))
        self.plan['jobs'] = jobs
        with self.assertRaises(ValueError):
            headroom.compile_plan(self.plan)

    def test_windows_device_ids_are_refused_but_similar_ids_are_valid(self):
        reserved = ['con', 'prn', 'aux', 'nul'] + [
            f'{prefix}{number}' for prefix in ('com', 'lpt') for number in range(1, 10)]
        for identifier in reserved:
            with self.subTest(identifier=identifier):
                self.invalid(lambda p: p['jobs'][0].update(id=identifier))
        for identifier in ('console', 'con-1', 'com0', 'com10', 'lpt0', 'lpt10', 'null'):
            with self.subTest(valid_id=identifier):
                self.plan['jobs'][0]['id'] = identifier
                self.assertEqual(decode_plan(headroom.compile_plan(self.plan))[1][0][0], identifier)

    def test_resume_requires_opt_in_and_recipe(self):
        self.invalid(lambda p: p['jobs'][0].update(max_resumes=1))
        self.invalid(lambda p: p['jobs'][0].update(max_resumes=1, allow_stop=True))
        self.invalid(lambda p: p['jobs'][0].update(max_resumes=1,
                     resume_argv=[str(self.executable)]))
        self.plan['jobs'][0].update(resume_argv=[str(self.executable)])
        # A recipe may be supplied without enabling automatic resumes.
        self.assertTrue(decode_plan(headroom.compile_plan(self.plan))[1][0][4])

    def test_argument_types_nuls_and_unpaired_surrogates(self):
        for key in ('argv', 'resume_argv'):
            for value in (None, '', (), [4], [str(self.executable), None],
                          [str(self.executable), 'x\0y'],
                          [str(self.executable), '\ud800'], ['']):
                with self.subTest(key=key, value=repr(value)):
                    self.invalid(lambda p: p['jobs'][0].update({key: value}))
        self.invalid(lambda p: p['jobs'][0].update(argv=[]))

    def test_unicode_and_windows_crt_quoting(self):
        args = ['', 'two words', 'a\tb', 'say"hi', 'trail \\',
                'slash\\"quote', 'plain\\', '雪😀', '&', '%TEMP%', '|']
        self.plan['jobs'][0]['argv'] = [str(self.executable), *args]
        _, jobs = decode_plan(headroom.compile_plan(self.plan))
        command = jobs[0][3]
        expected_tail = (' "" "two words" "a\tb" say\\"hi '
                         '"trail \\\\" slash\\\\\\"quote plain\\ 雪😀 & %TEMP% |')
        self.assertEqual(command, f'"{self.executable.resolve()}"' + expected_tail)
        if os.name == 'nt':
            shell = ctypes.WinDLL('shell32', use_last_error=True)
            shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            count = ctypes.c_int()
            result = shell.CommandLineToArgvW(command, ctypes.byref(count))
            self.assertTrue(result)
            try:
                self.assertEqual([result[i] for i in range(count.value)],
                                 [str(self.executable.resolve()), *args])
            finally:
                kernel.LocalFree(result)

    def test_command_limit_counts_utf16_units_after_quoting(self):
        executable = str(self.executable.resolve())
        # Quoted executable + separator leave exactly this many units.
        available = 32766 - (len(executable.encode('utf-16le')) // 2 + 3)
        self.plan['jobs'][0]['argv'] = [executable, 'x' * available]
        command = decode_plan(headroom.compile_plan(self.plan))[1][0][3]
        self.assertEqual(len(command.encode('utf-16le')) // 2, 32766)
        self.invalid(lambda p: p['jobs'][0]['argv'].__setitem__(1, 'x' * (available + 1)))
        self.invalid(lambda p: p['jobs'][0]['argv'].__setitem__(1, '😀' * (available // 2 + 1)))
        # Every embedded quote adds an escaping backslash.
        self.invalid(lambda p: p['jobs'][0]['argv'].__setitem__(1, '"' * available))
        self.invalid(lambda p: p['jobs'][0].update(argv=[executable, *([''] * 16384)]))

    def test_executable_unit_limit(self):
        with patch.object(headroom, '_executable', return_value='x' * 4095 + '.exe'):
            with self.assertRaises(ValueError):
                headroom.compile_plan(self.plan)
        with patch.object(headroom, '_executable', return_value='x' * 4091 + '.exe'):
            self.assertEqual(len(decode_plan(headroom.compile_plan(self.plan))[1][0][2]), 4095)

    def test_relative_cwd_and_path_resolution_without_shell_or_launch(self):
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(self.base)
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('must not execute')):
            self.plan['jobs'][0]['argv'] = [self.executable.stem]
            self.assertEqual(decode_plan(headroom.compile_plan(self.plan))[1][0][2],
                             str(self.executable.resolve()))
            path_exe = self.base / 'bin' / 'path-tool.exe'
            path_exe.parent.mkdir()
            path_exe.write_bytes(b'offline')
            with patch.dict(os.environ, {'PATH': str(path_exe.parent), 'PATHEXT': '.BAT;.EXE'}):
                self.plan['jobs'][0]['argv'] = ['path-tool']
                self.assertEqual(decode_plan(headroom.compile_plan(self.plan))[1][0][2],
                                 str(path_exe.resolve()))
            shell_file = self.base / 'shell.bat'
            shell_file.write_bytes(b'not run')
            self.invalid(lambda p: p['jobs'][0].update(argv=[str(shell_file)]))
            self.invalid(lambda p: p['jobs'][0].update(argv=[str(self.base)]))
            self.invalid(lambda p: p['jobs'][0].update(argv=['nonexistent-offline-fixture']))

    def test_strict_json_duplicate_keys_and_nonstandard_numbers(self):
        source = json.dumps(self.plan)
        invalids = [source.replace('"budget_mib": 128', '"budget_mib": 128, "budget_mib": 128'),
                    source.replace('"id": "job"', '"id": "job", "id": "job"'),
                    source.replace('"budget_mib": 128', '"budget_mib": 128, "budget_\\u006dib": 128'),
                    source.replace('128', 'NaN', 1), source.replace('128', 'Infinity', 1),
                    source.replace('128', '-Infinity', 1), source.replace('128', '128.0', 1),
                    source + '{}', source + '\0', b'\xff', source.encode('utf-16'),
                    b'\xef\xbb\xbf' + source.encode('utf-8'), b'[' * 2000]
        for invalid in invalids:
            with self.subTest(source=repr(invalid)[:120]):
                code, output = self.cli(invalid)
                self.assertEqual(code, 64)
                self.assertFalse(output.exists())

    def test_public_strict_loader(self):
        for source in ('{"name":"雪😀","count":1}',
                       '{"name":"雪😀","count":1}'.encode('utf-8')):
            self.assertEqual(headroom.strict_json_loads(source), {'name': '雪😀', 'count': 1})
        for source in ('{"a":1,"a":2}', '{"x":{"a":1,"a":2}}',
                       '{"x":NaN}', '{"x":Infinity}', '{} {}', '[[[', b'\xff'):
            with self.subTest(source=source), self.assertRaises((ValueError, UnicodeError)):
                headroom.strict_json_loads(source)

    def test_compile_cli_atomic_output_and_no_overwrite(self):
        source = json.dumps(self.plan)
        code, output = self.cli(source)
        self.assertEqual(code, 0)
        original = output.read_bytes()
        self.assertEqual(original, headroom.compile_plan(self.plan))
        code, _ = self.cli(source, output)
        self.assertEqual(code, 73)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(list(self.base.glob('.headroom-*.tmp')), [])
        directory = self.base / 'existing-directory'
        directory.mkdir()
        code, _ = self.cli(source, directory)
        self.assertNotEqual(code, 0)
        self.assertTrue(directory.is_dir())

    def test_atomic_publication_race_has_exactly_one_winner(self):
        path = self.base / 'race.hrp'
        payloads = [bytes([number]) * 4096 for number in range(4)]

        def publish(payload):
            try:
                headroom._write_new(path, payload)
                return payload
            except FileExistsError:
                return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(publish, payloads))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(path.read_bytes(), winners[0])
        self.assertEqual(list(self.base.glob('.headroom-*.tmp')), [])

    def test_write_failure_cleans_temp_and_never_publishes(self):
        target = self.base / 'not-created.hrp'
        with patch.object(os, 'fsync', side_effect=OSError('injected disk failure')):
            with self.assertRaises(OSError):
                headroom._write_new(target, b'complete payload')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.base.glob('.headroom-*.tmp')), [])

    def test_dangling_output_symlink_is_never_replaced(self):
        target = self.base / 'missing-target'
        output = self.base / 'dangling.hrp'
        try:
            output.symlink_to(target)
        except OSError as failure:
            self.skipTest(f'OS does not allow fixture symlinks: {failure}')
        with self.assertRaises(FileExistsError):
            headroom._write_new(output, b'cannot replace link')
        self.assertTrue(output.is_symlink())
        self.assertFalse(target.exists())
        self.assertEqual(list(self.base.glob('.headroom-*.tmp')), [])

    def test_binary_size_cap(self):
        with patch.object(headroom, 'MAX_PLAN_BYTES', 16):
            with self.assertRaises(ValueError):
                headroom.compile_plan(self.plan)

    def test_cli_rejects_oversized_json_before_parsing(self):
        source = self.base / 'oversized.json'
        # Set the logical length without constructing a large fixture buffer.
        with source.open('wb') as stream:
            stream.truncate(headroom.MAX_PLAN_BYTES + 1)
        output = self.base / 'must-not-exist.hrp'
        errors = io.StringIO()
        with patch.object(headroom, 'strict_json_loads') as loader:
            with contextlib.redirect_stderr(errors):
                code = headroom.main(['compile', str(source), str(output)])
        self.assertEqual(code, 64)
        self.assertIn('JSON input exceeds 16 MiB', errors.getvalue())
        loader.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(list(self.base.glob('.headroom-*.tmp')), [])
        self.assertEqual(source.stat().st_size, headroom.MAX_PLAN_BYTES + 1)

    def test_module_entrypoint_compile_and_status(self):
        source = self.base / 'cli.json'
        source.write_text(json.dumps(self.plan), encoding='utf-8')
        output = self.base / 'cli.hrp'
        result = subprocess.run([sys.executable, str(ROOT / 'headroom.py'),
                                 'compile', str(source), str(output)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        decode_plan(output.read_bytes())
        result = subprocess.run([sys.executable, str(ROOT / 'headroom.py'),
                                 'status', str(self.base / 'missing')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
        self.assertFalse(report['process_liveness_verified'])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='headroom-status-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.success = [event('prepared'), job_event('queued', attempt=0),
                        job_event('started'), job_event('completed'), event('finished')]

    def inspect(self, records, tail=b''):
        path = self.base / 'fleet.jsonl'
        path.write_bytes(b''.join(json.dumps(r).encode('utf-8') + b'\n' for r in records) + tail)
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('read only')):
            report = headroom.read_status(self.base)
        self.assertFalse(report['process_liveness_verified'])
        return report

    def test_success_requires_complete_job_and_fleet_evidence(self):
        report = self.inspect(self.success)
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['evidence_complete'])
        self.assertEqual(report['fleet_exit_code'], 0)
        self.assertEqual(report['jobs'][0]['status'], 'completed')
        self.assertEqual(report['jobs'][0]['attempts'][0]['root_exit_code'], 0)

    def test_unfinished_and_missing_job_outcomes_are_not_success(self):
        for records in (self.success[:1], self.success[:3], self.success[:4],
                        self.success[:3] + [event('finished')],
                        [event('prepared'), event('finished')]):
            with self.subTest(records=records):
                report = self.inspect(records)
                self.assertNotEqual(report['status'], 'completed')
                self.assertFalse(report['evidence_complete'])
                self.assertTrue(all(j['status'] == 'unconfirmed' for j in report['jobs']))

    def test_nonzero_completed_event_is_failed_attempt(self):
        records = copy.deepcopy(self.success)
        records[3]['code'] = 70
        records[-1]['code'] = 1
        report = self.inspect(records)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['jobs'][0]['status'], 'failed')
        self.assertEqual(report['jobs'][0]['attempts'][0]['root_exit_code'], 70)
        records[-1]['code'] = 0
        self.assertEqual(self.inspect(records)['status'], 'invalid_or_incomplete_evidence')

    def test_contradictory_or_unknown_finished_code_is_invalid(self):
        for code in (1, 2, 64, 73):
            records = copy.deepcopy(self.success)
            records[-1]['code'] = code
            report = self.inspect(records)
            self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
            self.assertFalse(report['evidence_valid'])
            self.assertIsNotNone(report['error'])

    def test_launch_failure_after_queue_or_start_is_terminal_failure(self):
        for prefix in (self.success[:2], self.success[:3]):
            with self.subTest(started=len(prefix) == 3):
                records = prefix + [job_event('launch_failed', code=70)]
                unfinished = self.inspect(records)
                self.assertTrue(unfinished['evidence_valid'])
                self.assertEqual(unfinished['jobs'][0]['observed_outcome'], 'failed')
                self.assertEqual(unfinished['jobs'][0]['status'], 'unconfirmed')
                report = self.inspect(records + [event('finished', code=1)])
                self.assertEqual(report['status'], 'failed', report)
                self.assertTrue(report['evidence_complete'])
                attempt = report['jobs'][0]['attempts'][0]
                self.assertEqual(attempt['status'], 'failed')
                self.assertTrue(attempt['launch_failed'])
                self.assertEqual(attempt['logical_exit_code'], 70)
                self.assertIsNone(attempt['root_exit_code'])
                self.assertEqual(self.inspect(records + [event('finished')])['status'],
                                 'invalid_or_incomplete_evidence')

    def test_launch_failure_before_first_attempt_directory_is_failed(self):
        records = self.success[:2] + [job_event('launch_failed', attempt=0, code=70),
                                     event('finished', code=1)]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'failed', report)
        attempt = report['jobs'][0]['attempts'][0]
        self.assertEqual(attempt['attempt'], 0)
        self.assertEqual(attempt['logical_exit_code'], 70)
        self.assertTrue(attempt['launch_failed'])

    def test_launch_failure_does_not_change_successful_protected_peer(self):
        records = self.success[:2] + [event('queued', task='protected', priority=100),
                   event('started', task='protected', attempt=1, priority=100),
                   job_event('launch_failed', code=70),
                   event('completed', task='protected', attempt=1, priority=100),
                   event('finished', code=1)]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual({j['id']: j['status'] for j in report['jobs']},
                         {'job': 'failed', 'protected': 'completed'})

    def test_launch_failure_cannot_replace_completed_or_stopped_outcome(self):
        prefixes = [self.success[:4], self.success[:3] + [job_event('completed', code=17)],
                    self.success[:3] + [job_event('pressure_intent'), job_event('stopped')]]
        for prefix in prefixes:
            report = self.inspect(prefix + [job_event('launch_failed', code=70)])
            self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
            self.assertFalse(report['evidence_valid'])

    def test_launch_failed_attempt_cannot_resume_start_or_complete(self):
        for prefix in (self.success[:2], self.success[:3]):
            records = prefix + [job_event('launch_failed', code=70)]
            for suffix in (job_event('resume_queued'), job_event('started'),
                           job_event('completed'), job_event('stopped'),
                           job_event('launch_failed', code=70)):
                with self.subTest(started=len(prefix) == 3, suffix=suffix['event']):
                    report = self.inspect(records + [suffix])
                    self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
                    self.assertFalse(report['evidence_valid'])
                    self.assertEqual(report['jobs'][0]['observed_outcome'], 'failed')

    def test_resume_launch_failure_keeps_consumed_attempt_history(self):
        records = self.success[:3] + [job_event('pressure_intent'),
                   job_event('checkpoint_acknowledged'), job_event('stopped'),
                   job_event('resume_queued'), job_event('launch_failed', attempt=2, code=70),
                   event('finished', code=1)]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'failed', report)
        attempts = report['jobs'][0]['attempts']
        self.assertEqual([a['attempt'] for a in attempts], [1, 2])
        self.assertEqual([a['logical_exit_code'] for a in attempts], [125, 70])
        self.assertTrue(attempts[1]['launch_failed'])

    def test_launch_failure_requires_valid_task_attempt_and_code(self):
        for prefix, failure in ((self.success[:1], job_event('launch_failed', code=70)),
                                (self.success[:2], job_event('launch_failed', attempt=2, code=70)),
                                (self.success[:3], job_event('launch_failed', attempt=0, code=70)),
                                (self.success[:2], job_event('launch_failed', code=0)),
                                (self.success[:2], event('launch_failed', code=70)),
                                (self.success[:3] + [job_event('pressure_intent')],
                                 job_event('launch_failed', code=70))):
            report = self.inspect(prefix + [failure])
            self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
            self.assertFalse(report['evidence_valid'])

    def test_stopped_zero_root_exit_remains_stopped(self):
        records = self.success[:3] + [job_event('pressure_intent'),
                   job_event('stopped', code=0), event('finished', code=1)]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['jobs'][0]['status'], 'stopped')
        self.assertEqual(report['jobs'][0]['attempts'][0]['root_exit_code'], 0)
        self.assertEqual(report['jobs'][0]['attempts'][0]['logical_exit_code'], 125)
        # A later zero root exit cannot undo durable stop intent.
        records.insert(-1, job_event('completed'))
        self.assertEqual(self.inspect(records)['jobs'][0]['status'], 'stopped')
        records[-1]['code'] = 0
        self.assertEqual(self.inspect(records)['status'], 'invalid_or_incomplete_evidence')

    def test_pressure_intent_prevents_completed_zero_becoming_success(self):
        records = self.success[:3] + [job_event('pressure_intent'),
                                     job_event('completed'), event('finished')]
        self.assertEqual(self.inspect(records)['status'], 'invalid_or_incomplete_evidence')

    def test_multiple_logical_jobs_all_need_success(self):
        records = self.success[:2] + [event('queued', task='second', priority=100)]
        records += self.success[2:-1] + [event('finished')]
        self.assertEqual(self.inspect(records)['status'], 'invalid_or_incomplete_evidence')
        records[-1:-1] = [event('started', task='second', attempt=1, priority=100),
                          event('completed', task='second', attempt=1, priority=100)]
        self.assertEqual(self.inspect(records)['status'], 'completed')

    def test_malformed_or_truncated_tail_revokes_success(self):
        for tail in (b'{', b'\n', b'broken\n', b'\xff\n', b' ' * (headroom.MAX_EVENT_BYTES + 1),
                     json.dumps(event('sample')).encode()):
            with self.subTest(tail=repr(tail)[:80]):
                report = self.inspect(self.success, tail)
                self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
                self.assertFalse(report['evidence_complete'])
                self.assertEqual(report['jobs'][0]['status'], 'unconfirmed')
                self.assertIsNone(report['fleet_exit_code'])

    def test_mixed_runs_backward_time_and_post_terminal_record(self):
        mixed = copy.deepcopy(self.success)
        mixed[3]['run_id'] = 'different-run'
        backwards = copy.deepcopy(self.success)
        backwards[2]['elapsed_ms'] = 10
        for records in (mixed, backwards, self.success + [event('sample')],
                        self.success[:1] + self.success,
                        self.success[1:]):
            with self.subTest(records=records):
                self.assertEqual(self.inspect(records)['status'], 'invalid_or_incomplete_evidence')

    def test_all_required_fields_are_strict_and_duplicate_keys_rejected(self):
        for key in event('prepared'):
            record = event('prepared')
            record.pop(key)
            with self.subTest(missing=key):
                self.assertEqual(self.inspect([record])['status'], 'invalid_or_incomplete_evidence')
        for key in ('version', 'code', 'elapsed_ms', 'priority', 'attempt',
                    'total_commit_bytes', 'available_bytes'):
            for value in (True, 0.0, -1):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.inspect([event('prepared', **{key: value})])['status'],
                                     'invalid_or_incomplete_evidence')
        for tail in (b'{"version":1,"version":1}\n', b'{"version":NaN}\n', b'[]\n'):
            self.assertEqual(self.inspect([], tail)['status'], 'invalid_or_incomplete_evidence')

    def test_unknown_diagnostics_never_imply_completion(self):
        records = self.success[:3] + [job_event('future_success_like_event')]
        self.assertEqual(self.inspect(records)['status'], 'running_or_interrupted')
        records += [event('finished')]
        self.assertEqual(self.inspect(records)['status'], 'invalid_or_incomplete_evidence')
        records = self.success[:3] + [event('measurement_unavailable'),
                                     job_event('future_diagnostic')] + self.success[3:]
        self.assertEqual(self.inspect(records)['status'], 'completed')

    def test_pressure_diagnostics_do_not_complete_tasks(self):
        records = self.success[:3] + [event('pressure_unresolved'),
                   job_event('pressure_intent'), job_event('pressure_revoked')]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'running_or_interrupted')
        attempt = report['jobs'][0]['attempts'][0]
        self.assertEqual(attempt['status'], 'running_or_interrupted')
        self.assertFalse(attempt['stop_intent'])
        self.assertEqual(attempt['revoked_stop_intents'], 1)
        self.assertEqual(self.inspect(records + [event('finished')])['status'],
                         'invalid_or_incomplete_evidence')
        # Completion can follow a revoked, unpublished stop request.
        report = self.inspect(records + self.success[3:])
        self.assertEqual(report['status'], 'completed', report)

    def test_revocation_cannot_undo_acknowledged_or_completed_stop(self):
        records = self.success[:3] + [job_event('pressure_intent'),
                                     job_event('checkpoint_acknowledged')]
        for suffix in ([job_event('pressure_revoked')],
                       [job_event('stopped'), job_event('pressure_revoked')]):
            self.assertEqual(self.inspect(records + suffix)['status'],
                             'invalid_or_incomplete_evidence')
        self.assertEqual(self.inspect(self.success[:3] + [job_event('pressure_revoked')])['status'],
                         'invalid_or_incomplete_evidence')

    def test_invalid_attempt_order_and_task_identity(self):
        for record in (job_event('completed'), job_event('stopped'),
                       job_event('checkpoint_acknowledged'),
                       job_event('started', attempt=2),
                       job_event('started', priority=51)):
            with self.subTest(record=record):
                self.assertEqual(self.inspect(self.success[:2] + [record])['status'],
                                 'invalid_or_incomplete_evidence')
        for record in (job_event('started'), job_event('queued')):
            self.assertEqual(self.inspect(self.success[:3] + [record])['status'],
                             'invalid_or_incomplete_evidence')

    def test_empty_and_missing_evidence(self):
        self.assertEqual(self.inspect([])['status'], 'invalid_or_incomplete_evidence')
        self.assertEqual(headroom.read_status(self.base / 'missing')['status'],
                         'invalid_or_incomplete_evidence')

    def test_acknowledged_stop_resume_keeps_attempt_history(self):
        records = self.success[:3] + [job_event('pressure_intent'),
                   job_event('checkpoint_acknowledged'), job_event('terminated', code=125),
                   job_event('stopped', code=125), job_event('resume_queued', code=0),
                   job_event('started', attempt=2), job_event('completed', attempt=2),
                   event('finished')]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual(report['jobs'][0]['status'], 'completed')
        attempts = report['jobs'][0]['attempts']
        self.assertEqual([a['attempt'] for a in attempts], [1, 2])
        self.assertEqual([a['status'] for a in attempts], ['stopped', 'completed'])
        self.assertEqual([a['logical_exit_code'] for a in attempts], [125, 0])
        self.assertEqual([a['root_exit_code'] for a in attempts], [125, 0])
        self.assertTrue(attempts[0]['checkpoint_acknowledged'])

    def test_requeue_does_not_count_as_started_or_success(self):
        records = self.success[:3] + [job_event('pressure_intent'),
                   job_event('checkpoint_acknowledged'), job_event('stopped'),
                   job_event('resume_queued')]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'running_or_interrupted')
        self.assertEqual(report['jobs'][0]['attempts'][-1]['status'], 'queued')
        self.assertEqual(self.inspect(records + [event('finished')])['status'],
                         'invalid_or_incomplete_evidence')

    def test_resume_requires_stop_ack_and_sequential_attempts(self):
        prefixes = [self.success[:3], self.success[:4],
                    self.success[:3] + [job_event('pressure_intent'), job_event('stopped')]]
        for prefix in prefixes:
            self.assertEqual(self.inspect(prefix + [job_event('resume_queued')])['status'],
                             'invalid_or_incomplete_evidence')
        records = self.success[:3]
        for number in range(1, 5):
            records += [job_event('pressure_intent', attempt=number),
                        job_event('checkpoint_acknowledged', attempt=number),
                        job_event('stopped', attempt=number)]
            if number < 4:
                records += [job_event('resume_queued', attempt=number),
                            job_event('started', attempt=number + 1)]
        self.assertEqual(len(self.inspect(records)['jobs'][0]['attempts']), 4)
        self.assertEqual(self.inspect(records + [job_event('resume_queued', attempt=4)])['status'],
                         'invalid_or_incomplete_evidence')

    def test_deadline_interrupt_and_internal_error_never_imply_success(self):
        for name, code, expected in (('deadline', 124, 'deadline'),
                                     ('interrupt', 130, 'interrupted'),
                                     ('internal_error', 70, 'internal_error')):
            records = self.success[:3] + [event(name, code=code)]
            report = self.inspect(records)
            self.assertEqual(report['status'], expected)
            self.assertFalse(report['evidence_complete'])
            self.assertEqual(report['jobs'][0]['status'], 'unconfirmed')
            self.assertEqual(self.inspect(records + [event('finished')])['status'],
                             'invalid_or_incomplete_evidence')

    def test_change_during_read_revokes_final_success(self):
        self.inspect(self.success)
        path = self.base / 'fleet.jsonl'
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
        changed = SimpleNamespace(st_dev=before.st_dev, st_ino=before.st_ino,
                                  st_size=before.st_size + 1,
                                  st_mtime_ns=before.st_mtime_ns,
                                  st_ctime_ns=before.st_ctime_ns)
        with patch.object(os, 'fstat', side_effect=[before, changed]):
            report = headroom.read_status(self.base)
        self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')
        self.assertIn('changed', report['error'])

    def test_replaced_path_is_not_confused_with_open_handle(self):
        self.inspect(self.success)
        before = (self.base / 'fleet.jsonl').stat()
        replaced = SimpleNamespace(st_dev=before.st_dev, st_ino=before.st_ino + 1,
                                   st_size=before.st_size,
                                   st_mtime_ns=before.st_mtime_ns,
                                   st_ctime_ns=before.st_ctime_ns)
        with patch.object(Path, 'stat', return_value=replaced):
            report = headroom.read_status(self.base)
        self.assertEqual(report['status'], 'invalid_or_incomplete_evidence')

    def test_many_sample_lines_stream_without_history_growth(self):
        records = self.success[:3] + [event('sample') for _ in range(2000)] + self.success[3:]
        report = self.inspect(records)
        self.assertEqual(report['status'], 'completed', report)
        self.assertEqual(report['records_read'], 2005)
        self.assertEqual(len(report['jobs']), 1)
        self.assertEqual(len(report['jobs'][0]['attempts']), 1)


if __name__ == '__main__':
    unittest.main()
