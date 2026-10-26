import ctypes
from ctypes import wintypes as w
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom
from test_fleet import serial_fleet


def until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError('bounded recovery fixture timed out')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='headroom-runner-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def shell(self, script, *args, cwd=None):
        return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(ROOT / script), *map(str, args)], capture_output=True,
            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW, cwd=cwd)

    def shell_from_location(self, script, *args):
        inherited = self.base / 'inherited directory'
        inherited.mkdir(exist_ok=True)
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        command = (f'Set-Location -LiteralPath {quote(self.base)}; '
            'if ([IO.Directory]::GetCurrentDirectory() -eq (Get-Location).ProviderPath) '
            '{ throw "Regression requires different process and PowerShell directories" }; '
            f'& {quote(ROOT / script)} ' + ' '.join(
                str(value) if index % 2 == 0 else quote(value)
                for index, value in enumerate(args)) + '; exit $LASTEXITCODE')
        return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-Command', command], cwd=inherited, capture_output=True,
            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)

    def plan(self, jobs, budget=64):
        document = {'budget_mib': budget, 'hard_mib': 200, 'reserve_mib': 0,
                    'sustain_ms': 100, 'grace_ms': 2500, 'timeout_ms': 10000, 'jobs': jobs}
        path = self.base / 'plan.json'
        path.write_text(json.dumps(document))
        return path, document

    def test_one_command_runner_preserves_success_and_state(self):
        with serial_fleet():
            path, _ = self.plan([{'id': 'simple', 'claim_mib': 32, 'hard_mib': 64,
                'argv': [sys.executable, '-c', 'pass']}])
            state = self.base / 'run'
            result = self.shell('run.ps1', '-Plan', path, '-State', state)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual(headroom.read_status(state)['status'], 'completed')
            self.assertTrue((state / 'plan.hrp').exists())

    def test_invalid_plan_does_not_create_state(self):
        path = self.base / 'bad.json'
        path.write_text('{"budget_mib":true}')
        state = self.base / 'never'
        result = self.shell('run.ps1', '-Plan', path, '-State', state)
        self.assertEqual(result.returncode, 64)
        self.assertFalse(state.exists())

    def test_recovery_non_filesystem_paths_refused_before_preparation(self):
        path, _ = self.plan([{'id': 'simple', 'claim_mib': 32, 'hard_mib': 64,
            'argv': [sys.executable, '-c', 'pass']}])
        state = self.base / 'original'
        state.mkdir()
        destination = self.base / 'recovered'
        for index in range(3):
            with self.subTest(argument=('-Plan', '-FromState', '-State')[index]):
                values = [path, state, destination]
                values[index] = 'Env:PATH'
                result = self.shell('resume.ps1', '-Plan', values[0],
                    '-FromState', values[1], '-State', values[2])
                self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                self.assertIn(b'FileSystem provider', result.stderr)
                self.assertFalse(destination.exists())
                self.assertFalse(Path(str(destination) + '.recovery.hrp').exists())
                self.assertEqual(list(state.iterdir()), [])

    def test_real_hash_application_checkpoints_under_native_pressure(self):
        with serial_fleet():
            small, large = self.base / 'a-small.bin', self.base / 'b-large.bin'
            small.write_bytes(b'completed file boundary')
            # Bounded disk fixture, streamed rather than retained in memory.
            with large.open('wb') as stream:
                chunk = b'h' * (1024 * 1024)
                for _ in range(64):
                    stream.write(chunk)
            app = ROOT / 'examples' / 'checkpointed_hash.py'
            output = self.base / 'application'
            path, _ = self.plan([{'id': 'hash', 'claim_mib': 1, 'hard_mib': 64,
                'allow_stop': True, 'argv': [sys.executable, str(app), '--state', str(output),
                '--chunk-bytes', '1024', '--', str(small), str(large)]}], budget=1)
            state = self.base / 'native'
            result = self.shell('run.ps1', '-Plan', path, '-State', state)
            self.assertEqual(result.returncode, 1, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
            events = [json.loads(line) for line in (state / 'fleet.jsonl').read_text().splitlines()]
            self.assertTrue(any(row['event'] == 'checkpoint_acknowledged' for row in events))
            self.assertTrue(any(row['event'] == 'stopped' for row in events))
            self.assertFalse(any(row['event'] == 'completed' for row in events))
            self.assertFalse((output / 'manifest.json').exists())
            control = state / 'hash' / 'attempt-1'
            self.assertEqual((control / 'stop.request').read_bytes(), (control / 'stop.ack').read_bytes())
            checkpoint = json.loads((output / 'checkpoint.json').read_bytes())
            self.assertEqual(len(checkpoint['completed']), 1)
            resumed = self.base / 'application-resumed'
            env = {key: value for key, value in os.environ.items() if key != 'HEADROOM_CONTROL_DIR'}
            completed = subprocess.run([sys.executable, str(app), '--state', str(resumed),
                '--resume', str(output), '--', str(small), str(large)], env=env,
                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            manifest = json.loads((resumed / 'manifest.json').read_bytes())
            expected = []
            for input_file in (small, large):
                with input_file.open('rb') as stream:
                    expected.append(hashlib.file_digest(stream, 'sha256').hexdigest())
            self.assertEqual([row['sha256'] for row in manifest['files']], expected)

    def test_crashed_coordinator_checkpoint_resumes_once(self):
        self.check_checkpoint_recovery()

    def test_relative_checkpoint_recovery_after_set_location(self):
        self.check_checkpoint_recovery(relative=True)

    def test_cross_cwd_checkpoint_recovery_runs_original_relative_recipe(self):
        self.check_checkpoint_recovery(cross_cwd=True)

    def check_checkpoint_recovery(self, *, relative=False, cross_cwd=False):
        with serial_fleet():
            output = self.base / 'progress'
            output.mkdir()
            worker = ROOT / 'tests' / 'recovery_worker.py'
            original_cwd = self.base / 'original cwd'
            caller_cwd = self.base / 'caller cwd'
            original_cwd.mkdir()
            caller_cwd.mkdir()
            resume_argv = [sys.executable, str(worker), str(output), '--resume']
            if cross_cwd:
                (original_cwd / 'resume-worker.py').write_text(
                    "from pathlib import Path\nimport runpy\n"
                    "Path('cwd-proof').write_text(str(Path.cwd()))\n"
                    f"runpy.run_path({str(worker)!r}, run_name='__main__')\n", encoding='utf-8')
                (caller_cwd / 'resume-worker.py').write_text(
                    "from pathlib import Path\nPath('wrong-work').write_text('wrong cwd')\n", encoding='utf-8')
                resume_argv = [sys.executable, 'resume-worker.py', str(output), '--resume']
            path, plan = self.plan([{'id': 'recoverable', 'priority': 20,
                'claim_mib': 24, 'hard_mib': 160, 'allow_stop': True,
                'max_resumes': 1, 'argv': [sys.executable, str(worker), str(output)],
                'resume_argv': resume_argv}])
            compiled = self.base / 'first.hrp'
            compiled.write_bytes(headroom.compile_plan(plan))
            state = self.base / 'original'
            process = subprocess.Popen([str(ROOT / 'build' / 'headroom-fleet.exe'),
                '--plan', str(compiled), '--state', str(state)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=original_cwd if cross_cwd else None)
            try:
                def acknowledged():
                    if not (state / 'fleet.jsonl').exists():
                        return False
                    return any('"checkpoint_acknowledged"' in line
                               for line in (state / 'fleet.jsonl').read_text().splitlines())
                until(acknowledged)
                saved = json.loads((output / 'checkpoint.json').read_text())['progress']
                process.kill()
                process.communicate(timeout=5)
                # Test only the owned child, using its stable process handle.
                pid = json.loads((output / 'ready.json').read_text())['pid']
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
                kernel.OpenProcess.restype = w.HANDLE
                kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
                kernel.CloseHandle.argtypes = [w.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, pid)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                    finally:
                        kernel.CloseHandle(handle)
                new_state = self.base / 'recovered'
                shell = self.shell_from_location if relative else self.shell
                argument = (lambda p: p.relative_to(self.base)) if relative else str
                result = shell('resume.ps1', '-Plan', argument(path),
                    '-FromState', argument(state), '-State', argument(new_state),
                    **({'cwd': caller_cwd} if cross_cwd else {}))
                self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
                self.assertEqual(headroom.read_status(new_state)['status'], 'completed')
                self.assertEqual(json.loads((output / 'resumed.json').read_text())['started_from'], saved)
                if cross_cwd:
                    self.assertEqual((original_cwd / 'cwd-proof').read_text(), str(original_cwd))
                    self.assertFalse((caller_cwd / 'wrong-work').exists())
                self.assertTrue((state / 'recovery-launch-intent.json').is_file())
                intent = json.loads((state / 'recovery-launch-intent.json').read_bytes())
                reservation = json.loads((state / 'recovery-reserved.json').read_bytes())
                expected_plan = str(new_state) + '.recovery.hrp'
                self.assertEqual(intent['new_state'], str(new_state))
                self.assertEqual(intent['prepared_plan'], expected_plan)
                self.assertEqual(reservation['output_path'], expected_plan)
                second_state = self.base / 'duplicate'
                duplicate = shell('resume.ps1', '-Plan', argument(path),
                    '-FromState', argument(state), '-State', argument(second_state))
                self.assertEqual(duplicate.returncode, 73)
                self.assertFalse(second_state.exists())
                self.assertNotEqual(headroom.read_status(state)['status'], 'completed')
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
