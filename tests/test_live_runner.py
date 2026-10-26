"""Actual wrapper/live-owner/crash/recovery checks, bounded and test-owned."""
import ctypes
import json
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
import headroom_live as live
from compare_fleet import kernel, new_owned_job
from test_fleet import read_events, serial_fleet, until


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='headroom-live-runner-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.settings = dict(budget_mib=64, hard_mib=200, reserve_mib=0,
                             sustain_ms=100, grace_ms=2500, timeout_ms=15000)
        self.settings_path = self.base / 'settings.json'
        self.settings_path.write_text(json.dumps(self.settings))
        self.processes = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process, group in reversed(self.processes):
            kernel.CloseHandle(group)  # Our outer job contains wrapper + descendants.
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def spawn(self, command, *, cwd=None):
        group = new_owned_job()
        process = subprocess.Popen(list(map(str, command)), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, creationflags=subprocess.CREATE_NO_WINDOW)
        if not kernel.AssignProcessToJobObject(group, int(process._handle)):
            error = ctypes.get_last_error()
            process.kill()
            process.communicate(timeout=5)
            kernel.CloseHandle(group)
            self.fail('test-owned outer job assignment failed: ' + str(error))
        self.processes.append((process, group))
        return process

    def start(self, *, wrapper=False, cwd=None):
        state = self.base / 'original'
        if wrapper:
            command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ROOT / 'serve.ps1', '-Settings', self.settings_path, '-State', state]
        else:
            compiled = self.base / 'session.hrs'
            compiled.write_bytes(live.compile_session(self.settings))
            command = [ROOT / 'build' / 'headroom-fleet.exe', '--live-plan', compiled, '--state', state]
        process = self.spawn(command, cwd=cwd)
        until(lambda: (state / 'session.json').is_file())
        return process, state

    def resume(self, state, destination, *, relative=False, cwd=None):
        inherited = cwd
        if relative:
            inherited = self.base / 'inherited directory'
            inherited.mkdir(exist_ok=True)
            def quote(value):
                return "'" + str(value).replace("'", "''") + "'"
            command = (f'Set-Location -LiteralPath {quote(self.base)}; '
                'if ([IO.Directory]::GetCurrentDirectory() -eq (Get-Location).ProviderPath) '
                '{ throw "Regression requires different process and PowerShell directories" }; '
                f'& {quote(ROOT / "resume_live.ps1")} '
                f'-Settings {quote(self.settings_path.relative_to(self.base))} '
                f'-FromState {quote(state.relative_to(self.base))} '
                f'-State {quote(destination.relative_to(self.base))}; exit $LASTEXITCODE')
            invocation = ['-Command', command]
        else:
            invocation = ['-File', ROOT / 'resume_live.ps1', '-Settings', self.settings_path,
                '-FromState', state, '-State', destination]
        process = self.spawn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            *invocation], cwd=inherited)
        stdout, stderr = process.communicate(timeout=20)
        return process.returncode, stdout, stderr

    def test_foreground_serve_wrapper_submit_logs_and_seal(self):
        with serial_fleet():
            process, state = self.start(wrapper=True)
            receipt = live.submit(state, dict(id='wrapper', claim_mib=32, hard_mib=64,
                argv=[sys.executable, '-c', "print('real wrapped command')"]))
            self.assertEqual(receipt['status'], 'accepted')
            self.assertEqual(live.seal(state)['status'], 'seal_requested')
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(headroom.read_status(state)['status'], 'completed')
            log = headroom.read_log(state, 'wrapper')
            self.assertIsNone(log['error'], log)
            self.assertEqual(log['text'].strip(), 'real wrapped command')
            self.assertTrue(log['metadata_confirmed'])

    def test_recovery_non_filesystem_paths_refused_before_preparation(self):
        state = self.base / 'original'
        state.mkdir()
        destination = self.base / 'recovered'
        for index in range(3):
            with self.subTest(argument=('-Settings', '-FromState', '-State')[index]):
                values = [self.settings_path, state, destination]
                values[index] = 'Env:PATH'
                process = self.spawn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', ROOT / 'resume_live.ps1', '-Settings', values[0],
                    '-FromState', values[1], '-State', values[2]])
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 73, stdout + stderr)
                self.assertIn(b'FileSystem provider', stderr)
                self.assertFalse(destination.exists())
                self.assertFalse(Path(str(destination) + '.recovery.hrp').exists())
                self.assertFalse(Path(str(destination) + '.recovery.hrp.json').exists())
                self.assertEqual(list(state.iterdir()), [])

    def test_crashed_live_owner_checkpoint_recovers_once_as_recoverable_batch(self):
        self.check_checkpoint_recovery()

    def test_relative_checkpoint_recovery_after_set_location(self):
        self.check_checkpoint_recovery(relative=True)

    def test_cross_cwd_checkpoint_recovery_runs_original_relative_recipe(self):
        self.check_checkpoint_recovery(cross_cwd=True)

    def check_checkpoint_recovery(self, *, relative=False, cross_cwd=False):
        with serial_fleet():
            original_cwd = self.base / 'original cwd'
            caller_cwd = self.base / 'caller cwd'
            original_cwd.mkdir()
            caller_cwd.mkdir()
            process, state = self.start(cwd=original_cwd if cross_cwd else None)
            output = self.base / 'progress'
            output.mkdir()
            worker = ROOT / 'tests' / 'recovery_worker.py'
            resume_argv = [sys.executable, str(worker), str(output), '--resume']
            if cross_cwd:
                (original_cwd / 'resume-worker.py').write_text(
                    "from pathlib import Path\nimport runpy\n"
                    "Path('cwd-proof').write_text(str(Path.cwd()))\n"
                    f"runpy.run_path({str(worker)!r}, run_name='__main__')\n", encoding='utf-8')
                (caller_cwd / 'resume-worker.py').write_text(
                    "from pathlib import Path\nPath('wrong-work').write_text('wrong cwd')\n", encoding='utf-8')
                resume_argv = [sys.executable, 'resume-worker.py', str(output), '--resume']
            receipt = live.submit(state, dict(id='saved', priority=20, claim_mib=24,
                hard_mib=160, allow_stop=True, max_resumes=1,
                argv=[sys.executable, str(worker), str(output)],
                resume_argv=resume_argv))
            self.assertEqual(receipt['status'], 'accepted')
            until(lambda: any(row['event'] == 'checkpoint_acknowledged'
                             for row in read_events(state, partial=True)))
            saved = json.loads((output / 'checkpoint.json').read_bytes())['progress']
            process.kill()  # Only the pinned process created in this test.
            process.communicate(timeout=5)
            recovered = self.base / 'recovered'
            code, stdout, stderr = self.resume(state, recovered, relative=relative,
                                               cwd=caller_cwd if cross_cwd else None)
            self.assertEqual(code, 0, stdout + stderr)
            self.assertEqual(headroom.read_status(recovered)['status'], 'completed')
            self.assertEqual(json.loads((output / 'resumed.json').read_bytes())['started_from'], saved)
            if cross_cwd:
                self.assertEqual((original_cwd / 'cwd-proof').read_text(), str(original_cwd))
                self.assertFalse((caller_cwd / 'wrong-work').exists())
            companion = Path(str(recovered) + '.recovery.hrp.json')
            plan = headroom.strict_json_loads(companion.read_bytes())
            self.assertEqual(headroom.compile_plan(plan), (recovered / 'plan.hrp').read_bytes())
            self.assertTrue((state / 'recovery-launch-intent.json').exists())
            intent = json.loads((state / 'recovery-launch-intent.json').read_bytes())
            reservation = json.loads((state / 'recovery-reserved.json').read_bytes())
            expected_plan = str(recovered) + '.recovery.hrp'
            self.assertEqual(intent['new_state'], str(recovered))
            self.assertEqual(intent['prepared_plan'], expected_plan)
            self.assertEqual(intent['plan_json'], str(companion))
            self.assertEqual(reservation['output_path'], expected_plan)
            self.assertEqual(reservation['plan_json_path'], str(companion))
            code, _, _ = self.resume(state, self.base / 'duplicate', relative=relative)
            self.assertEqual(code, 73)
            self.assertFalse((self.base / 'duplicate').exists())
            # Recovered work is an ordinary batch; its companion supports the
            # existing batch recovery interface instead of losing provenance.
            check = subprocess.run([sys.executable, str(ROOT / 'recover.py'), str(companion),
                str(recovered), str(self.base / 'not-needed.hrp')], capture_output=True, timeout=8)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertEqual(json.loads(check.stdout)['status'], 'no_work')
            self.assertNotEqual(headroom.read_status(state)['status'], 'completed')

    def test_crash_without_supported_checkpoint_refuses_no_command_replay(self):
        with serial_fleet():
            process, state = self.start()
            output = self.base / 'unsupported'
            output.mkdir()
            receipt = live.submit(state, dict(id='unsupported', claim_mib=32, hard_mib=160,
                argv=[sys.executable, str(ROOT / 'tests' / 'worker.py'), 'stubborn', str(output)]))
            self.assertEqual(receipt['status'], 'accepted')
            until(lambda: (output / 'stubborn-pid.json').exists())
            process.kill()
            process.communicate(timeout=5)
            destination = self.base / 'refused'
            code, _, _ = self.resume(state, destination)
            self.assertEqual(code, 73)
            self.assertFalse(destination.exists())
            self.assertFalse(Path(str(destination) + '.recovery.hrp').exists())


if __name__ == '__main__':
    unittest.main()
