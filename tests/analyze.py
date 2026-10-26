"""Run GCC analyzer through object compilation, not just syntax parsing."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FLAGS = ['-std=c11', '-O0', '-Wall', '-Wextra', '-Werror', '-fanalyzer', '-c']


def snapshot():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / 'src').glob('*.[ch]'))}


def main():
    output = ROOT / 'build' / 'analysis'
    output.mkdir(parents=True, exist_ok=True)
    evidence = ROOT / 'evidence'
    evidence.mkdir(exist_ok=True)
    before = snapshot()
    version = subprocess.run(['gcc', '--version'], cwd=ROOT, capture_output=True,
                             text=True, timeout=10, check=True).stdout.splitlines()[0]
    checks, diagnostics = [], []
    for source in sorted((ROOT / 'src').glob('*.c')):
        relative = source.relative_to(ROOT).as_posix()
        command = ['gcc', *FLAGS, relative, '-o',
                   (output / (source.stem + '.o')).relative_to(ROOT).as_posix()]
        result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                text=True, timeout=180)
        diagnostics.append(relative + '\n' + result.stdout + result.stderr)
        checks.append({'command': command, 'returncode': result.returncode})
        print(('PASS ' if result.returncode == 0 else 'FAIL ') + relative, flush=True)
    after = snapshot()
    passed = all(c['returncode'] == 0 for c in checks) and before == after
    report = {'schema_version': 1, 'compiler': version, 'checks': checks,
              'source_sha256': before, 'source_sha256_after': after,
              'inputs_unchanged': before == after, 'passed': passed}
    (evidence / 'static-analysis.log').write_text('\n'.join(diagnostics), encoding='utf-8')
    (evidence / 'static-analysis.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
