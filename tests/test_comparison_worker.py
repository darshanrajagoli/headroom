"""Regression for the retained Windows request-read measurement failure."""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import comparison_worker as worker


def sharing_error():
    error = PermissionError('synthetic Windows sharing violation')
    error.winerror = 32
    return error


class RequestReadTests(unittest.TestCase):
    def test_transient_sharing_failure_then_exact_bytes(self):
        with mock.patch.object(worker.os, 'name', 'nt'), \
             mock.patch.object(Path, 'read_bytes', side_effect=[sharing_error(), b'exact-packet\n']), \
             mock.patch.object(worker.time, 'sleep') as sleep:
            self.assertEqual(worker.read_stop_request(Path('unused')), b'exact-packet\n')
            sleep.assert_called_once_with(0.01)

    def test_permanent_permission_failure_is_bounded_and_not_hidden(self):
        with mock.patch.object(worker.os, 'name', 'nt'), \
             mock.patch.object(Path, 'read_bytes', side_effect=sharing_error()), \
             mock.patch.object(worker.time, 'monotonic', side_effect=[0, 0.1, 0.21]), \
             mock.patch.object(worker.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError):
                worker.read_stop_request(Path('unused'))
            sleep.assert_called_once()

    def test_unclassified_and_non_permission_failures_are_not_retried(self):
        for error in (PermissionError('not classified'), FileNotFoundError('gone'), OSError('other')):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(Path, 'read_bytes', side_effect=error), \
                 mock.patch.object(worker.time, 'sleep') as sleep:
                with self.assertRaises(type(error)):
                    worker.read_stop_request(Path('unused'))
                sleep.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'requires Windows sharing semantics')
    def test_real_exclusive_handle_releases_within_retry_window(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                                      w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        with tempfile.TemporaryDirectory(prefix='headroom-sharing-') as directory:
            path = Path(directory) / 'stop.request'
            expected = b'headroom-stop-v1\nsynthetic-fixture\n'
            path.write_bytes(expected)
            handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            results = []
            def read():
                try:
                    results.append(worker.read_stop_request(path))
                except BaseException as error:
                    results.append(error)
            reader = threading.Thread(target=read)
            try:
                # Establish the actual pre-fix failure while our own handle
                # holds the file exclusively. No unrelated process is involved.
                with self.assertRaises(PermissionError):
                    path.read_bytes()
                reader.start()
                time.sleep(0.05)
                self.assertTrue(kernel.CloseHandle(handle))
                handle = None
                reader.join(timeout=1)
                self.assertFalse(reader.is_alive())
                self.assertEqual(results, [expected])
            finally:
                if handle is not None:
                    kernel.CloseHandle(handle)
                if reader.ident is not None:
                    reader.join(timeout=1)


if __name__ == '__main__':
    unittest.main()
