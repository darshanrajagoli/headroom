"""Verify/extract a local bundle, then run bounded batch/live wrapper smoke checks."""
import argparse
import _winapi
from contextlib import ExitStack
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import math
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from compare_fleet import kernel, new_owned_job
from test_fleet import serial_fleet
from package_local import verify


def require(value, message):
    if not value:
        raise RuntimeError(message)


kernel.ResumeThread.argtypes = [w.HANDLE]
kernel.ResumeThread.restype = w.DWORD
kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
kernel.TerminateJobObject.restype = w.BOOL
kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            w.DWORD, ctypes.POINTER(w.DWORD)]
kernel.QueryInformationJobObject.restype = w.BOOL


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (name, w.DWORD) for name in ('faults', 'total', 'active', 'terminated')]


def _drain(group, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        info = _Accounting()
        require(kernel.QueryInformationJobObject(group, 1, ctypes.byref(info),
                ctypes.sizeof(info), None), 'owned smoke group query failed')
        if not info.active:
            return
        require(time.monotonic() < deadline, 'owned smoke group did not drain')
        time.sleep(0.01)


class OwnedProcess:
    """Exact creation handles, owned before execution, with file-backed capture.

    Capture files avoid pipe deadlocks while a wrapper and its descendants exit.
    Only this invocation's three standard handles are inherited by the child.
    """

    def __init__(self, command, cwd):
        self.args = list(map(str, command))
        self.returncode = None
        self.handle = self.group = None
        self.streams = ExitStack()
        thread = None
        assigned = False
        try:
            self.group = new_owned_job()
            null = self.streams.enter_context(open(os.devnull, 'rb', buffering=0))
            self.stdout = self.streams.enter_context(tempfile.TemporaryFile('w+b'))
            self.stderr = self.streams.enter_context(tempfile.TemporaryFile('w+b'))
            handles = [msvcrt.get_osfhandle(stream.fileno())
                       for stream in (null, self.stdout, self.stderr)]
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
            startup.lpAttributeList = {'handle_list': handles}
            try:
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                self.handle, thread, self.pid, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(self.args), None, None, True,
                    4 | subprocess.CREATE_NO_WINDOW, None, str(cwd), startup)
            finally:
                for handle in handles:
                    os.set_handle_inheritable(handle, False)
            require(kernel.AssignProcessToJobObject(self.group, self.handle),
                    'owned smoke group assignment failed')
            assigned = True
            require(kernel.ResumeThread(thread) == 1, 'owned smoke process resume failed')
        except BaseException:
            try:
                if self.handle is not None:
                    if assigned:
                        require(kernel.TerminateJobObject(self.group, 70),
                                'owned smoke group termination failed')
                    else:
                        # Never resumed: terminate only the exact newly created root.
                        _winapi.TerminateProcess(self.handle, 70)
                    require(_winapi.WaitForSingleObject(self.handle, 5000) == 0,
                            'owned smoke process cleanup unconfirmed')
                    if assigned:
                        _drain(self.group)
            finally:
                self._release()
            raise
        finally:
            if thread is not None:
                _winapi.CloseHandle(thread)

    def poll(self):
        if self.returncode is None and _winapi.WaitForSingleObject(self.handle, 0) == 0:
            self.returncode = _winapi.GetExitCodeProcess(self.handle)
        return self.returncode

    def communicate(self, timeout):
        milliseconds = min(0xFFFFFFFE, max(0, math.ceil(timeout * 1000)))
        result = _winapi.WaitForSingleObject(self.handle, milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired(self.args, timeout)
        require(result == 0, 'owned smoke process wait failed')
        self.returncode = _winapi.GetExitCodeProcess(self.handle)
        _drain(self.group, timeout)
        output = []
        for stream in (self.stdout, self.stderr):
            stream.seek(0)
            output.append(stream.read())
        return tuple(output)

    def _release(self):
        if self.group is not None:
            _winapi.CloseHandle(self.group)
            self.group = None
        if self.handle is not None:
            _winapi.CloseHandle(self.handle)
            self.handle = None
        self.streams.close()

    def close(self):
        try:
            require(kernel.TerminateJobObject(self.group, 70),
                    'owned smoke group termination failed')
            require(_winapi.WaitForSingleObject(self.handle, 5000) == 0,
                    'owned smoke process cleanup unconfirmed')
            self.returncode = _winapi.GetExitCodeProcess(self.handle)
            _drain(self.group)
        finally:
            self._release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    proof = verify(args.archive)
    base = ROOT / 'test-output' / ('package-' + uuid.uuid4().hex[:12])
    base.mkdir(parents=True)
    bundle = base / 'bundle'
    bundle.mkdir()
    with zipfile.ZipFile(args.archive) as archive:
        # verify() rejected traversal, duplicate names, links and excess sizes.
        archive.extractall(bundle)
    processes = []

    def spawn(command):
        process = OwnedProcess(command, bundle)
        processes.append(process)
        return process

    def finish(process, timeout=15):
        stdout, stderr = process.communicate(timeout=timeout)
        (base / ('stdout-' + str(process.pid) + '.log')).write_bytes(stdout)
        (base / ('stderr-' + str(process.pid) + '.log')).write_bytes(stderr)
        require(process.returncode == 0, 'packaged command failed (raw logs retained)')
        return stdout

    def offline(*arguments):
        return finish(spawn([sys.executable, *arguments]))

    summary = dict(schema_version=1, passed=False, archive=proof,
                   scope='Extracted files only; fresh batch and live wrapper/CLI runs. No compiler needed.')
    try:
        with serial_fleet():
            batch = base / 'batch'
            finish(spawn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', bundle / 'run.ps1', '-Plan', bundle / 'examples' / 'quickstart.json',
                '-State', batch]))
            batch_status = json.loads(offline(bundle / 'headroom.py', 'status', batch))
            require(batch_status['status'] == 'completed', 'batch did not complete')
            live = base / 'live'
            owner = spawn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', bundle / 'serve.ps1', '-Settings', bundle / 'examples' / 'live-settings.json',
                '-State', live])
            deadline = time.monotonic() + 10
            while not (live / 'session.json').is_file():
                require(owner.poll() is None and time.monotonic() < deadline, 'live owner not ready')
                time.sleep(0.025)
            receipt = json.loads(offline(bundle / 'headroom_live.py', 'submit', live,
                                        bundle / 'examples' / 'live-job.json'))
            require(receipt['status'] == 'accepted', 'packaged request was not accepted')
            seal = json.loads(offline(bundle / 'headroom_live.py', 'seal', live))
            require(seal['status'] == 'seal_requested', 'packaged seal not requested')
            finish(owner)
            live_status = json.loads(offline(bundle / 'headroom.py', 'status', live))
            require(live_status['status'] == 'completed', 'live run did not complete')
            output = json.loads(offline(bundle / 'headroom.py', 'logs', live, 'first-command'))
            require(output['metadata_confirmed'] and not output['error']
                    and 'Command output is now captured by the shared owner.' in output['text'],
                    'packaged log mismatch')
            summary.update(passed=True, batch_status=batch_status['status'],
                           live_status=live_status['status'], accepted=True,
                           log_metadata_confirmed=True,
                           fleet_sha256=hashlib.sha256((bundle / 'build' /
                                                        'headroom-fleet.exe').read_bytes()).hexdigest())
    finally:
        cleanup_errors = []
        for process in reversed(processes):
            try:
                process.close()
            except (OSError, RuntimeError) as error:
                cleanup_errors.append(str(error))
        summary['all_spawned_processes_exited'] = not cleanup_errors and all(
            p.returncode is not None for p in processes)
        if cleanup_errors:
            summary.update(passed=False, cleanup_errors=cleanup_errors)
        (base / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        evidence = ROOT / 'evidence' / 'package-smoke.json'
        evidence.write_text(json.dumps(summary, indent=2) + '\n')
        require(not cleanup_errors, 'owned smoke cleanup unconfirmed: ' + '; '.join(cleanup_errors))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
