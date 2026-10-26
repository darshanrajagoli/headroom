"""Offline package format regressions, separate from the native aggregate."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
import warnings
import zipfile

from package_local import MANIFEST, safe_name, verify


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='headroom-package-format-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'review.zip'
        self.payload = b'bounded test data'
        self.manifest = dict(version=1, tests=391, files={'README.md': dict(
            bytes=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest())})

    def archive(self, entries=None, manifest=True):
        with zipfile.ZipFile(self.path, 'x') as archive:
            if manifest:
                archive.writestr(MANIFEST, json.dumps(self.manifest))
            for name, data in entries or [('README.md', self.payload)]:
                archive.writestr(name, data)

    def test_valid_archive(self):
        self.archive()
        self.assertTrue(verify(self.path)['verified'])

    def test_unsafe_names(self):
        for name in ('../escape', '/absolute', 'C:/absolute', 'dir\\name',
                     './name', 'dir//name', 'dir/../name', 'NUL', 'con.txt',
                     'dir/file.', 'dir/file ', 'dir/COM1.bin', 'dir/a\x00b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_name(name)

    def test_traversal_member(self):
        self.archive([('../escape', self.payload)])
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_duplicate_member(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            self.archive([('README.md', self.payload), ('README.md', self.payload)])
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_changed_member(self):
        self.archive([('README.md', b'changed')])
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_case_aliased_member(self):
        self.archive([('README.md', self.payload), ('readme.md', self.payload)])
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_extra_member(self):
        self.archive([('README.md', self.payload), ('extra.txt', b'not allowed')])
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_missing_manifest(self):
        self.archive(manifest=False)
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_link_member(self):
        entry = zipfile.ZipInfo('README.md')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.archive([(entry, self.payload)])
        with self.assertRaises(ValueError):
            verify(self.path)


if __name__ == '__main__':
    def hashes():
        return {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ('package_local.py', 'test_package_local.py')}
    before = hashes()
    started = time.perf_counter()
    result = unittest.main(verbosity=2, exit=False).result
    report = dict(schema_version=1, tests=result.testsRun, failures=len(result.failures),
                  errors=len(result.errors), skipped=len(result.skipped),
                  seconds=time.perf_counter() - started, sources_sha256=before,
                  inputs_unchanged=hashes() == before,
                  scope='Offline package format checks, separate from native aggregate.',
                  passed=result.wasSuccessful() and not result.skipped and hashes() == before)
    destination = Path(__file__).resolve().parents[1] / 'evidence' / 'package-format-tests.json'
    destination.write_text(json.dumps(report, indent=2) + '\n')
    raise SystemExit(0 if report['passed'] else 1)
