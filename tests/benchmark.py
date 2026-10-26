"""Short healthy-host guardian-only measurements, not a minimum-RAM claim."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'build' / 'headroom.exe'
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)


class Memory(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('PageFaultCount', w.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
            'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
            'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]


kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel.OpenProcess.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
kernel.GetProcessTimes.restype = w.BOOL
psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Memory), w.DWORD]
psapi.GetProcessMemoryInfo.restype = w.BOOL


def snapshot(handle):
    memory = Memory()
    memory.cb = ctypes.sizeof(memory)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    times = [w.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    cpu = sum((times[i].dwHighDateTime << 32) | times[i].dwLowDateTime for i in (2, 3)) / 10_000_000
    return {'working_set_bytes': memory.WorkingSetSize,
            'private_commit_bytes': memory.PrivateUsage, 'cpu_seconds': cpu}


def run():
    trials = []
    for trial in range(3):
        with tempfile.TemporaryDirectory(prefix='headroom-bench-') as folder:
            base = Path(folder)
            output = base / 'output'
            output.mkdir()
            command = [str(EXE), '--state', str(base / 'state'), '--soft-mib', '40',
                       '--hard-mib', '64', '--timeout-ms', '12000', '--', sys.executable,
                       str(ROOT / 'tests' / 'worker.py'), 'idle', str(output)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            handle = kernel.OpenProcess(0x0400 | 0x0010, False, process.pid)
            if not handle:
                process.kill()
                process.communicate()
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                time.sleep(0.5)
                start = time.perf_counter()
                first = snapshot(handle)
                samples = []
                for _ in range(6):
                    time.sleep(1)
                    if process.poll() is not None:
                        raise RuntimeError('guardian exited before the measurement ended')
                    samples.append(snapshot(handle))
                seconds = time.perf_counter() - start
                trials.append({'trial': trial + 1, 'seconds': seconds, 'samples': samples,
                    'cpu_percent_one_core': 100 * (samples[-1]['cpu_seconds'] - first['cpu_seconds']) / seconds,
                    'max_working_set_bytes': max(s['working_set_bytes'] for s in samples),
                    'max_private_commit_bytes': max(s['private_commit_bytes'] for s in samples)})
            finally:
                kernel.CloseHandle(handle)
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=8)
    evidence = {'schema_version': 1, 'os': platform.platform(),
                'binary_sha256': hashlib.sha256(EXE.read_bytes()).hexdigest(),
                'scope': 'one guardian, one idle synthetic Python child excluded; three six-second samples; no pressure induced',
                'trials': trials}
    destination = ROOT / 'evidence' / 'native-benchmark.json'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: evidence[k] for k in ('binary_sha256', 'scope')}))
    for trial in trials:
        print(json.dumps({k: v for k, v in trial.items() if k != 'samples'}))


if __name__ == '__main__':
    run()
