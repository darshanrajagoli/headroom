"""Native Windows integration tests; memory allocations stay deliberately small."""
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'build' / 'headroom.exe'
WORKER = ROOT / 'tests' / 'worker.py'
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel.OpenProcess.restype = w.HANDLE
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel.WaitForSingleObject.restype = w.DWORD
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.CloseHandle.restype = w.BOOL


def alive(pid):
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError('bounded fixture wait expired')


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='headroom-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / 'state'
        self.output = self.base / 'output'
        self.output.mkdir()

    def launch(self, mode, *, allow=True, soft=40, hard=160, grace=1000,
               timeout=6000, args=(), state=None, script=None, creationflags=0):
        command = [str(GUARD), '--state', str(state or self.state),
                   '--soft-mib', str(soft), '--hard-mib', str(hard),
                   '--grace-ms', str(grace), '--timeout-ms', str(timeout)]
        if allow:
            command.append('--allow-stop')
        command += ['--', sys.executable]
        command += (['-c', script, str(self.output), *args] if script is not None
                    else [str(WORKER), mode, str(self.output), *args])
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=creationflags, startupinfo=startup)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=8)

        self.addCleanup(cleanup)
        return process

    def finish(self, process):
        stdout, stderr = process.communicate(timeout=12)
        events = [json.loads(line) for line in (self.state / 'events.jsonl').read_text().splitlines()]
        return process.returncode, events, stderr.decode(errors='replace')

    def names(self, events):
        return [event['event'] for event in events]

    def test_normal_exit(self):
        code, events, error = self.finish(self.launch('normal'))
        self.assertEqual(code, 0, error)
        self.assertEqual(self.names(events)[-1], 'completed')
        self.assertEqual(events[-1]['active_processes'], 0)

    def test_nonzero_exit_is_preserved(self):
        code, events, error = self.finish(self.launch('normal', args=['17']))
        self.assertEqual(code, 17, error)
        self.assertEqual(events[-1]['code'], 17)

    def test_child_exit_70_is_not_a_guardian_error(self):
        code, events, error = self.finish(self.launch('normal', args=['70']))
        self.assertEqual(code, 70)
        self.assertEqual(error, '')
        self.assertNotIn('guardian_error', self.names(events))

    def test_checkpoint_handler_zero_is_stopped_not_completed(self):
        code, events, error = self.finish(self.launch('cooperative'))
        self.assertEqual(code, 125, error)
        names = self.names(events)
        self.assertIn('soft_limit_observed', names)
        self.assertLess(names.index('checkpoint_intent'), names.index('checkpoint_acknowledged'))
        self.assertEqual(names[-1], 'stopped')
        self.assertEqual(events[-1]['code'], 0)
        self.assertTrue((self.output / 'checkpoint.json').is_file())
        self.assertNotIn('terminate_intent', names)
        expected = f"headroom-stop-v1\n{events[0]['run_id']}\n".encode('ascii')
        self.assertEqual((self.state / 'stop.request').read_bytes(), expected)
        self.assertEqual((self.state / 'stop.ack').read_bytes(), expected)
        self.assertFalse((self.state / 'stop.request.tmp').exists())

    def test_observe_only_does_not_request_stop(self):
        code, events, error = self.finish(self.launch('observe', allow=False))
        self.assertEqual(code, 0, error)
        self.assertIn('soft_limit_observed', self.names(events))
        self.assertNotIn('checkpoint_intent', self.names(events))
        self.assertFalse((self.state / 'stop.request').exists())

    def test_repeated_fast_checkpoint_exits_collect_ack_before_terminal(self):
        for attempt in range(6):
            with self.subTest(attempt=attempt):
                self.state = self.base / ('fast-exit-' + str(attempt))
                code, events, error = self.finish(self.launch('cooperative'))
                names = self.names(events)
                self.assertEqual(code, 125, error)
                self.assertEqual(events[-1]['code'], 0)
                self.assertIn('checkpoint_acknowledged', names)
                self.assertLess(names.index('checkpoint_acknowledged'), names.index('stopped'))
                self.assertNotIn('terminate_intent', names)
                self.assertEqual((self.state / 'stop.request').read_bytes(),
                                 (self.state / 'stop.ack').read_bytes())

    def test_fixture_explicitly_resumes_its_saved_progress(self):
        code, events, error = self.finish(self.launch('cooperative'))
        self.assertEqual(code, 125, error)
        checkpoint = json.loads((self.output / 'checkpoint.json').read_text())
        self.assertGreater(checkpoint['progress'], 0)
        self.state = self.base / 'resumed-state'
        code, events, error = self.finish(self.launch('resume'))
        self.assertEqual(code, 0, error)
        resumed = json.loads((self.output / 'resumed.json').read_text())
        self.assertEqual(resumed['started_from'], checkpoint['progress'])
        self.assertEqual(resumed['completed'], 20)

    def test_no_executable_fails_without_launching_anything(self):
        process = subprocess.run([str(GUARD), '--state', str(self.state),
            '--soft-mib', '40', '--hard-mib', '80', '--',
            'headroom-no-such-program-9ac09f42.exe'], capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 70)
        events = [json.loads(line) for line in (self.state / 'events.jsonl').read_text().splitlines()]
        self.assertIn('guardian_error', self.names(events))
        self.assertNotIn('contained', self.names(events))

    def test_restrictive_outer_job_launch_failure_is_contained(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [('per_process_time', ctypes.c_longlong),
                        ('per_job_time', ctypes.c_longlong), ('flags', w.DWORD),
                        ('minimum_ws', ctypes.c_size_t), ('maximum_ws', ctypes.c_size_t),
                        ('active_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class Limits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', ctypes.c_ulonglong * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

        class Startup(ctypes.Structure):
            _fields_ = ([('cb', w.DWORD), ('reserved', w.LPWSTR),
                         ('desktop', w.LPWSTR), ('title', w.LPWSTR)]
                        + [(name, w.DWORD) for name in ('x', 'y', 'xsize', 'ysize',
                            'xchars', 'ychars', 'fill', 'flags')]
                        + [('show', w.WORD), ('reserved_size', w.WORD),
                           ('reserved_bytes', ctypes.c_void_p), ('stdin', w.HANDLE),
                           ('stdout', w.HANDLE), ('stderr', w.HANDLE)])

        class Process(ctypes.Structure):
            _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE),
                        ('pid', w.DWORD), ('tid', w.DWORD)]

        class Accounting(ctypes.Structure):
            _fields_ = [('times', ctypes.c_longlong * 4), ('faults', w.DWORD),
                        ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]

        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, w.DWORD]
        kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
            ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        kernel.ResumeThread.argtypes = [w.HANDLE]
        kernel.ResumeThread.restype = w.DWORD
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
            ctypes.POINTER(Startup), ctypes.POINTER(Process)]
        job = kernel.CreateJobObjectW(None, None)
        self.assertTrue(job, ctypes.get_last_error())
        info = Process()

        def cleanup():
            # This outer job owns only the suspended guardian and its fixtures.
            if info.process:
                kernel.TerminateJobObject(job, 99)
                kernel.WaitForSingleObject(info.process, 5000)
                kernel.CloseHandle(info.process)
            if info.thread:
                kernel.CloseHandle(info.thread)
            kernel.CloseHandle(job)

        self.addCleanup(cleanup)
        limits = Limits()
        limits.basic.flags = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS
        limits.basic.active_limit = 1  # The guardian alone fills the outer job.
        self.assertTrue(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits),
                                                       ctypes.sizeof(limits)), ctypes.get_last_error())
        command = [str(GUARD), '--state', str(self.state), '--soft-mib', '40',
                   '--hard-mib', '80', '--timeout-ms', '2000', '--',
                   sys.executable, str(WORKER), 'normal', str(self.output)]
        startup = Startup(cb=ctypes.sizeof(Startup))
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        self.assertTrue(kernel.CreateProcessW(str(GUARD), line, None, None, False,
            4 | subprocess.CREATE_NO_WINDOW, None, None, ctypes.byref(startup), ctypes.byref(info)),
            ctypes.get_last_error())
        self.assertTrue(kernel.AssignProcessToJobObject(job, info.process), ctypes.get_last_error())
        self.assertEqual(kernel.ResumeThread(info.thread), 1)
        self.assertEqual(kernel.WaitForSingleObject(info.process, 5000), 0)
        code = w.DWORD()
        self.assertTrue(kernel.GetExitCodeProcess(info.process, ctypes.byref(code)))
        self.assertEqual(code.value, 70)
        accounting = Accounting()

        def empty():
            self.assertTrue(kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting),
                                                            ctypes.sizeof(accounting), None))
            return accounting.active == 0

        until(empty, timeout=2)
        self.assertFalse((self.output / 'normal-pid.json').exists())
        events = [json.loads(line) for line in (self.state / 'events.jsonl').read_text().splitlines()]
        self.assertNotIn('contained', self.names(events))
        self.assertEqual(events[-1]['code'], 1816)  # ERROR_NOT_ENOUGH_QUOTA

    def test_stubborn_job_gets_grace_then_group_stop(self):
        code, events, error = self.finish(self.launch('stubborn', grace=600))
        self.assertEqual(code, 125, error)
        intent = next(e for e in events if e['event'] == 'checkpoint_intent')
        killed = next(e for e in events if e['event'] == 'terminate_intent')
        self.assertGreaterEqual(killed['elapsed_ms'] - intent['elapsed_ms'], 600)
        self.assertNotIn('checkpoint_acknowledged', self.names(events))

    def test_wrong_run_ack_is_not_accepted(self):
        code, events, error = self.finish(self.launch('forged-ack'))
        self.assertEqual(code, 125, error)
        self.assertIn('terminate_intent', self.names(events))
        self.assertNotIn('checkpoint_acknowledged', self.names(events))

    def test_child_is_in_same_group_and_terminated(self):
        process = self.launch('spawn')
        until(lambda: (self.output / 'child-pid.json').exists())
        child = json.loads((self.output / 'child-pid.json').read_text())
        code, events, error = self.finish(process)
        self.assertEqual(code, 125, error)
        until(lambda: not alive(child))
        self.assertEqual(events[-1]['active_processes'], 0)
        self.assertGreaterEqual(max(e['active_processes'] for e in events), 2)

    def test_leader_exit_does_not_detach_child(self):
        process = self.launch('leader-exits')
        until(lambda: (self.output / 'child-pid.json').exists())
        child = json.loads((self.output / 'child-pid.json').read_text())
        code, events, error = self.finish(process)
        self.assertEqual(code, 125, error)
        until(lambda: not alive(child))
        self.assertEqual(self.names(events)[-1], 'stopped')

    def test_crashed_guardian_does_not_leave_owned_children(self):
        process = self.launch('spawn', soft=128, hard=200)
        until(lambda: (self.output / 'child-pid.json').exists())
        child = json.loads((self.output / 'child-pid.json').read_text())
        parent = json.loads((self.output / 'spawn-pid.json').read_text())
        process.kill()
        process.communicate(timeout=8)
        until(lambda: not alive(parent) and not alive(child))

    def test_breakaway_is_refused(self):
        code, events, error = self.finish(self.launch('breakaway'))
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads((self.output / 'breakaway.json').read_text())['refused'])

    def test_hard_limit_denies_allocation_without_calling_it_checkpointed(self):
        code, events, error = self.finish(self.launch('allocation', allow=False, soft=24, hard=48))
        self.assertEqual(code, 71, error)
        self.assertTrue((self.output / 'allocation-denied.json').exists())
        self.assertNotIn('checkpoint_acknowledged', self.names(events))
        self.assertFalse((self.state / 'stop.request').exists())

    def test_timeout_is_not_success(self):
        code, events, error = self.finish(self.launch('stubborn', soft=128, hard=200, timeout=700))
        self.assertEqual(code, 124, error)
        self.assertEqual(self.names(events)[-1], 'timeout')

    def test_state_must_be_fresh(self):
        self.state.mkdir()
        marker = self.state / 'keep.txt'
        marker.write_text('keep')
        process = self.launch('normal')
        process.communicate(timeout=8)
        self.assertEqual(process.returncode, 73)
        self.assertEqual(marker.read_text(), 'keep')
        self.assertFalse((self.output / 'normal-pid.json').exists())

    def test_arguments_and_unicode_paths_round_trip(self):
        self.state = self.base / 'नमस्ते state'
        arguments = ['', 'hello world', 'quoted"value', 'trailing\\',
                     'backslash\\"quote', 'नमस्ते', 'C:\\a path\\',
                     '\t', 'line\nbreak', '& | ^ %PATH% $(ignored)']
        arguments += ['prefix' + '\\' * n + suffix
                      for n in range(9) for suffix in ('', '"', '"quoted"', ' tail\\')]
        code, events, error = self.finish(self.launch('argv', args=arguments))
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads((self.output / 'argv.json').read_text()), arguments)

    def test_expanded_command_line_overflow_has_precise_error(self):
        # The caller can pass an unquoted slash-only argument under the Windows
        # limit; quoting its trailing slashes doubles the child's command line.
        code, events, error = self.finish(self.launch('argv', args=['\\' * 16500]))
        self.assertEqual(code, 70, error)
        self.assertNotIn('contained', self.names(events))
        self.assertEqual(events[-1]['event'], 'guardian_error')
        self.assertEqual(events[-1]['code'], 122)  # ERROR_INSUFFICIENT_BUFFER
        self.assertFalse((self.output / 'argv-pid.json').exists())

    def test_full_windows_exit_codes_are_preserved(self):
        script = 'import ctypes,sys; ctypes.windll.kernel32.ExitProcess(int(sys.argv[2]))'
        for value in (259, 0x80000000, 0xFFFFFFFF):
            with self.subTest(code=value):
                self.state = self.base / f'exit-{value}'
                code, events, error = self.finish(self.launch(
                    'inline', script=script, args=[str(value)]))
                self.assertEqual(code, value, error)
                self.assertEqual(events[-1]['event'], 'completed')
                self.assertEqual(events[-1]['code'], value)

    def test_ack_does_not_extend_grace_for_live_descendants(self):
        script = r'''
import json, os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
(Path(sys.argv[1]) / 'child-pid.json').write_text(json.dumps(child.pid))
control = Path(os.environ['HEADROOM_CONTROL_DIR'])
payload = bytearray(48 * 1024 * 1024)
deadline = time.monotonic() + 3
while not (control / 'stop.request').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('no request')
    time.sleep(.005)
(control / 'stop.ack').write_bytes((control / 'stop.request').read_bytes())
'''
        code, events, error = self.finish(self.launch('inline', script=script, grace=500))
        self.assertEqual(code, 125, error)
        self.assertIn('checkpoint_acknowledged', self.names(events))
        intent = next(e for e in events if e['event'] == 'checkpoint_intent')
        terminate = next(e for e in events if e['event'] == 'terminate_intent')
        self.assertGreaterEqual(terminate['elapsed_ms'] - intent['elapsed_ms'], 500)
        self.assertLess(terminate['elapsed_ms'] - intent['elapsed_ms'], 1400)
        self.assertEqual(events[-1]['active_processes'], 0)
        self.assertEqual(events[-1]['code'], 0)
        child = json.loads((self.output / 'child-pid.json').read_text())
        until(lambda: not alive(child))

    def test_large_partial_and_directory_acks_are_refused(self):
        script = r'''
import os, sys, time
from pathlib import Path
control = Path(os.environ['HEADROOM_CONTROL_DIR'])
payload = bytearray(48 * 1024 * 1024)
deadline = time.monotonic() + 3
while not (control / 'stop.request').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('no request')
    time.sleep(.005)
body = (control / 'stop.request').read_bytes()
ack = control / 'stop.ack'
if sys.argv[2] == 'directory':
    ack.mkdir()
elif sys.argv[2] == 'large':
    ack.write_bytes(body + b'x' * (2 * 1024 * 1024))
else:
    ack.write_bytes(body[:-1])
time.sleep(3)
'''
        for kind in ('directory', 'large', 'partial'):
            with self.subTest(kind=kind):
                self.state = self.base / kind
                code, events, error = self.finish(self.launch(
                    'inline', script=script, args=[kind], grace=300))
                self.assertEqual(code, 125, error)
                self.assertNotIn('checkpoint_acknowledged', self.names(events))
                self.assertEqual(events[-1]['active_processes'], 0)

    def test_hard_limit_is_aggregate_across_children(self):
        script = r"""
import json, subprocess, sys
from pathlib import Path
child = r'''
import json, sys, time
from pathlib import Path
try:
    payload = bytearray(40 * 1024 * 1024)
    denied = False
except MemoryError:
    denied = True
Path(sys.argv[1]).write_text(json.dumps(denied))
time.sleep(.5)
'''
output = Path(sys.argv[1])
children = [subprocess.Popen([sys.executable, '-c', child, str(output / f'cap-{i}.json')])
            for i in range(2)]
for process in children:
    assert process.wait(timeout=4) == 0
assert any(json.loads((output / f'cap-{i}.json').read_text()) for i in range(2))
"""
        code, events, error = self.finish(self.launch(
            'inline', script=script, allow=False, soft=24, hard=80))
        self.assertEqual(code, 0, error)
        self.assertNotIn('checkpoint_intent', self.names(events))
        self.assertFalse((self.state / 'stop.request').exists())

    def test_interrupt_confirms_whole_group_exit(self):
        process = self.launch('spawn', soft=128, hard=200,
                              creationflags=subprocess.CREATE_NEW_CONSOLE
                              | subprocess.CREATE_NEW_PROCESS_GROUP)
        until(lambda: (self.output / 'child-pid.json').exists())
        child = json.loads((self.output / 'child-pid.json').read_text())
        parent = json.loads((self.output / 'spawn-pid.json').read_text())
        sender = r'''
import ctypes, sys
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.FreeConsole()
assert k.AttachConsole(int(sys.argv[1])), ctypes.get_last_error()
assert k.GenerateConsoleCtrlEvent(1, int(sys.argv[1])), ctypes.get_last_error()
k.FreeConsole()
'''
        sent = subprocess.run([sys.executable, '-c', sender, str(process.pid)],
            capture_output=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(sent.returncode, 0, sent.stderr.decode(errors='replace'))
        code, events, error = self.finish(process)
        self.assertEqual(code, 130, error)
        self.assertEqual(events[-1]['event'], 'interrupted')
        self.assertEqual(events[-1]['active_processes'], 0)
        self.assertFalse(alive(parent))
        self.assertFalse(alive(child))

    def test_request_collision_fails_closed_and_removes_own_temporary(self):
        script = r'''
import os, time
from pathlib import Path
control = Path(os.environ['HEADROOM_CONTROL_DIR'])
(control / 'stop.request').write_bytes(b'existing request must survive')
payload = bytearray(48 * 1024 * 1024)
time.sleep(4)
'''
        code, events, error = self.finish(self.launch('inline', script=script))
        self.assertEqual(code, 70, error)
        self.assertEqual(events[-1]['event'], 'guardian_error')
        self.assertEqual(events[-1]['code'], 183)  # ERROR_ALREADY_EXISTS
        self.assertEqual((self.state / 'stop.request').read_bytes(),
                         b'existing request must survive')
        self.assertFalse((self.state / 'stop.request.tmp').exists())

    def test_preexisting_request_temporary_is_preserved(self):
        script = r'''
import os, time
from pathlib import Path
control = Path(os.environ['HEADROOM_CONTROL_DIR'])
(control / 'stop.request.tmp').write_bytes(b'preexisting temporary')
payload = bytearray(48 * 1024 * 1024)
time.sleep(4)
'''
        code, events, error = self.finish(self.launch('inline', script=script))
        self.assertEqual(code, 70, error)
        self.assertEqual(events[-1]['code'], 80)  # ERROR_FILE_EXISTS
        self.assertFalse((self.state / 'stop.request').exists())
        self.assertEqual((self.state / 'stop.request.tmp').read_bytes(), b'preexisting temporary')

    def test_ack_oplock_cannot_block_grace_or_deadline(self):
        # An exclusive file oplock makes a second synchronous open wait for
        # its owner. Only this owned fixture holds it, for at most four seconds.
        script = r'''
import ctypes, json, os, sys, time
from ctypes import wintypes as w
from pathlib import Path
class OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', w.DWORD), ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                         w.DWORD, w.DWORD, w.HANDLE]
k.CreateFileW.restype = w.HANDLE
k.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
k.CreateEventW.restype = w.HANDLE
k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
    ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
k.CloseHandle.argtypes = [w.HANDLE]
control = Path(os.environ['HEADROOM_CONTROL_DIR'])
payload = bytearray(48 * 1024 * 1024)
deadline = time.monotonic() + 3
while not (control / 'stop.request').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('no request')
    time.sleep(.005)
ack = control / 'stop.ack'
ack.write_bytes((control / 'stop.request').read_bytes())
handle = k.CreateFileW(str(ack), 0x80000000, 7, None, 3, 0x40000000, None)
assert handle != w.HANDLE(-1).value, ctypes.get_last_error()
overlapped = OVERLAPPED(hEvent=k.CreateEventW(None, True, False, None))
got = w.DWORD()
ok = k.DeviceIoControl(handle, 0x00090000, None, 0, None, 0,
                       ctypes.byref(got), ctypes.byref(overlapped))
assert not ok and ctypes.get_last_error() == 997, ctypes.get_last_error()
(Path(sys.argv[1]) / 'oplock-ready.json').write_text(json.dumps(True))
time.sleep(4)
k.CloseHandle(handle)
'''
        code, events, error = self.finish(self.launch(
            'inline', script=script, grace=300, timeout=2000))
        self.assertTrue((self.output / 'oplock-ready.json').is_file(), error)
        self.assertEqual(code, 125, error)
        intent = next(e for e in events if e['event'] == 'checkpoint_intent')
        terminate = next(e for e in events if e['event'] == 'terminate_intent')
        self.assertGreaterEqual(terminate['elapsed_ms'] - intent['elapsed_ms'], 300)
        self.assertLess(terminate['elapsed_ms'] - intent['elapsed_ms'], 1200)

    def test_bad_limits_never_launch(self):
        process = self.launch('normal', soft=64, hard=32)
        process.communicate(timeout=8)
        self.assertEqual(process.returncode, 64)
        self.assertFalse(self.state.exists())

    def test_unrelated_process_is_untouched(self):
        outsider = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        self.addCleanup(lambda: outsider.poll() is None and outsider.kill())
        code, events, error = self.finish(self.launch('stubborn'))
        self.assertEqual(code, 125, error)
        self.assertIsNone(outsider.poll())
        outsider.terminate()
        outsider.wait(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
