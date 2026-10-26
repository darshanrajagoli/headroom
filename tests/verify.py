"""Run isolated integration checks and retain machine-readable local evidence."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    """Bind evidence to all implementation, fixture and build inputs."""
    sources = list((ROOT / 'src').glob('*.[ch]'))
    sources += list((ROOT / 'tests').glob('*.py'))
    sources += list((ROOT / 'tests').glob('*.[ch]'))
    # New offline tests import tool implementations; bind those exact bytes too.
    sources += list((ROOT / 'tools').glob('*.py'))
    sources += list(ROOT.glob('*.py')) + list(ROOT.glob('*.ps1'))
    sources += list((ROOT / 'examples').glob('*.py')) + list((ROOT / 'examples').glob('*.json'))
    binaries = [ROOT / 'build' / name for name in
                ('headroom.exe', 'headroom-fleet.exe', 'headroom-policy.dll')]
    def hashes(paths):
        return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(set(paths))}
    return {'sources': hashes(sources), 'binaries': hashes(binaries)}


before = snapshot()
# An explicit invocation of this serial verifier authorizes its bounded owned
# live fixtures; this variable exists only inside this verification process.
os.environ['HEADROOM_LIVE_NATIVE_GO'] = '1'
sys.path.insert(0, str(ROOT / 'tests'))
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
buffer = io.StringIO()
start = time.perf_counter()
result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
after = snapshot()
evidence = ROOT / 'evidence'
evidence.mkdir(exist_ok=True)
(evidence / 'native-tests.log').write_text(buffer.getvalue(), encoding='utf-8')
summary = {'schema_version': 2, 'os': platform.platform(), 'tests': result.testsRun,
           'failures': len(result.failures), 'errors': len(result.errors),
           'skipped': len(result.skipped), 'inputs_unchanged': before == after,
           'seconds': time.perf_counter() - start,
           'binary_sha256': before['binaries']['build/headroom.exe'],
           'sources_sha256': before['sources'],
           'binaries_sha256': before['binaries'],
           'snapshot_after': after,
           'passed': result.wasSuccessful() and not result.skipped and before == after}
(evidence / 'native-tests.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
print(buffer.getvalue(), end='')
print(json.dumps(summary, indent=2))
raise SystemExit(0 if summary['passed'] else 1)
