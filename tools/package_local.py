"""Build/verify an explicit-allowlist local review ZIP; never upload or install."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    'README.md', 'LICENSE', 'SAFETY.md', 'PROTOCOL.md', 'PRIOR_ART.md',
    'RESULTS.md', 'LIVE_GUIDE.md', 'LIVE_INTAKE.md', 'LOCAL_PACKAGE.md', 'AGENT_REPLAY.md',
    'DEMO.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'evidence/PUBLICATION_REVIEW.md',
    'examples/README.md', 'evidence/native-tests.json',
    'evidence/native-tests-pre-path-fix.json',
    'evidence/static-analysis.json', 'evidence/live-benchmark.json',
    'evidence/live-memory-before-layout.json',
    'evidence/LIVE_MEASUREMENT_REVIEW.md', 'evidence/LIVE_REVIEW.md',
    'evidence/OUTPUT_REVIEW.md', 'evidence/FLEET_REVIEW.md', 'evidence/GUARD_REVIEW.md',
    'evidence/compiler-demo.json', 'evidence/COMPILER_DEMO.md',
    'evidence/package-format-tests.json',
    'evidence/agent-cli-replay.json',
    'tools/compiler_demo.py',
    'tools/package_local.py', 'tools/package_smoke.py', 'tools/test_package_local.py',
)
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MANIFEST = 'PACKAGE_MANIFEST.json'


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    require(isinstance(name, str) and name and '\\' not in name and ':' not in name,
            'invalid archive name')
    require(name.isascii() and all(ord(c) >= 32 and c not in '<>"|?*' for c in name),
            'invalid Windows archive characters')
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(x not in ('.', '..', '') for x in name.split('/')),
            'unsafe archive path')
    require(str(path) == name, 'noncanonical archive name')
    reserved = {'con', 'prn', 'aux', 'nul'} | {
        prefix + str(i) for prefix in ('com', 'lpt') for i in range(1, 10)}
    require(all(part == part.rstrip(' .') and part.split('.')[0].lower() not in reserved
                for part in path.parts), 'Windows alias or reserved device name')
    return name


def read_local(name):
    safe_name(name)
    path = ROOT / name
    for entry in (path, *path.parents):
        info = entry.lstat()
        require(not entry.is_symlink() and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'linked or reparse package input')
        if entry == ROOT:
            break
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_FILE,
            'invalid package input')
    data = path.read_bytes()
    require(len(data) == info.st_size and len(data) <= MAX_FILE, 'package input changed size')
    return data


def inputs():
    report_bytes = read_local('evidence/native-tests.json')
    report = json.loads(report_bytes)
    require(report.get('schema_version') == 2 and report.get('passed') is True
            and report.get('inputs_unchanged') is True
            and type(report.get('tests')) is int and report['tests'] >= 391
            and all(report.get(k) == 0 for k in ('failures', 'errors', 'skipped')),
            'a successful frozen full-suite report is required')
    sources, binaries = report['sources_sha256'], report['binaries_sha256']
    require(set(binaries) == {'build/headroom.exe', 'build/headroom-fleet.exe',
                             'build/headroom-policy.dll'}, 'unexpected binary proof')
    root_code = {'build.ps1', 'headroom.py', 'headroom_live.py', 'headroom_agents.py', 'inspect_run.py',
                 'recover.py', 'recover_live.py', 'run.ps1', 'resume.ps1',
                 'serve.ps1', 'resume_live.ps1'}
    tool_code = {'tools/compiler_demo.py', 'tools/agent_replay.py', 'tools/replay_report.py', 'tools/package_local.py',
                 'tools/package_smoke.py', 'tools/test_package_local.py'}
    for name in sources:
        safe_name(name)
        p = PurePosixPath(name)
        require(name in root_code or name in tool_code or (len(p.parts) == 2 and
                ((p.parts[0] == 'src' and p.suffix in ('.c', '.h')) or
                 (p.parts[0] == 'tests' and p.suffix in ('.py', '.c', '.h')) or
                 (p.parts[0] == 'examples' and p.suffix in ('.py', '.json')))),
                'source proof contains a non-allowlisted path')
    require(root_code <= sources.keys(), 'missing runtime source proof')
    names = sorted(set(DOCS) | sources.keys() | binaries.keys())
    payload = {name: read_local(name) for name in names}
    require(sum(map(len, payload.values())) <= MAX_TOTAL, 'package too large')
    for name, expected in {**sources, **binaries}.items():
        require(digest(payload[name]) == expected, 'input differs from full-suite proof: ' + name)
    require(payload['evidence/native-tests.json'] == report_bytes, 'test proof changed')
    return payload, report


def build(output):
    require(output.suffix == '.zip', 'output must be a new .zip file')
    require(output.parent.is_dir() and not output.parent.is_symlink(), 'existing local parent required')
    payload, report = inputs()
    manifest = dict(version=1, kind='local-review-prototype', platform='Windows x86-64',
                    tests=report['tests'], runtime='Python 3.12 + Windows PowerShell',
                    tests_report_sha256=digest(payload['evidence/native-tests.json']),
                    files={name: dict(bytes=len(data), sha256=digest(data))
                           for name, data in payload.items()},
                    boundaries=['No installation, service, upload or real account adapter.',
                                'Commit limits, not host physical-RAM guarantees.',
                                'Checksums detect changes, not publisher authenticity.'])
    payload[MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    # Exclusive creation: never overwrite a user archive. A failed partial file
    # is retained for inspection and must not be described as verified.
    with output.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(payload.items()):
                entry = zipfile.ZipInfo(name, date_time=(2026, 10, 24, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
        stream.flush()
        os.fsync(stream.fileno())
    # Nothing launches until inputs still match after capture and archive proof
    # verifies independently. Keep incomplete output rather than deleting it.
    for name, data in payload.items():
        if name != MANIFEST:
            require(read_local(name) == data, 'input changed while packaging: ' + name)
    return verify(output)


def verify(output):
    require(output.is_file() and not output.is_symlink(), 'regular archive required')
    with zipfile.ZipFile(output) as archive:
        entries = archive.infolist()
        names = [safe_name(e.filename) for e in entries]
        require(len(names) == len({name.casefold() for name in names}) and MANIFEST in names,
                'duplicate, case-aliased or missing manifest')
        require(all(not e.is_dir() and not stat.S_ISLNK(e.external_attr >> 16)
                    and e.file_size <= MAX_FILE for e in entries), 'invalid archive member')
        require(sum(e.file_size for e in entries) <= MAX_TOTAL, 'archive too large')
        manifest = json.loads(archive.read(MANIFEST))
        require(manifest.get('version') == 1 and isinstance(manifest.get('files'), dict),
                'invalid manifest')
        require(set(names) == set(manifest['files']) | {MANIFEST}, 'unexpected archive contents')
        for name, proof in manifest['files'].items():
            data = archive.read(name)
            require(proof == dict(bytes=len(data), sha256=digest(data)), 'member proof mismatch')
    return dict(verified=True, files=len(entries), bytes=output.stat().st_size,
                archive_sha256=digest(output.read_bytes()), tests=manifest['tests'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'verify'))
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        result = build(args.output) if args.action == 'build' else verify(args.output)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps(dict(verified=False, error=str(exc))))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
