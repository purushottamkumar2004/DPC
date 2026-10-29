"""Tests for common/last_error.py (STATUS F31, D-079 format).

Run from the repo root:
    python3 -m unittest tests.unit.test_last_error -v
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common import last_error as le  # noqa: E402

SECRET = "s3cret-db-Zx91"


def failing_worker_step(password):
    """Stands for a worker function: raises with a secret inside the message."""
    raise RuntimeError(f"login failed for password {password}")


def raise_in_library():
    """A failure deep inside a library (json) called from our code."""
    return json.loads("{not json")


class Record(unittest.TestCase):
    def capture(self, func, *args):
        try:
            func(*args)
        except Exception as exc:
            return exc
        self.fail("no exception")

    def test_d079_fields_and_secret_scrubbed(self):
        record = le.error_record(self.capture(failing_worker_step, SECRET), secrets=[SECRET, "", None])
        self.assertEqual(set(record), {"at", "type", "message", "where", "traceback"})
        self.assertEqual(record["type"], "RuntimeError")
        self.assertEqual(record["message"], "login failed for password ***")
        self.assertRegex(record["where"], r"^test_last_error\.py:\d+ in failing_worker_step$")
        self.assertRegex(record["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertLessEqual(len(record["traceback"]), le.TRACEBACK_LINES)

    def test_where_is_our_code_not_the_library(self):
        record = le.error_record(self.capture(raise_in_library))
        self.assertRegex(record["where"], r"^test_last_error\.py:\d+ in raise_in_library$")
        self.assertEqual(record["type"], "JSONDecodeError")

    def test_own_code_dir_override(self):
        exc = self.capture(raise_in_library)
        record = le.error_record(exc, own_code_dir="/nowhere")      # nothing is "ours": the last frame
        self.assertNotIn("test_last_error.py", record["where"])

    def test_longer_secret_is_replaced_whole(self):
        exc = RuntimeError("abc-LONGER and abc")
        self.assertEqual(le.error_record(exc, secrets=["abc", "abc-LONGER"])["message"], "*** and ***")

    def test_caps(self):
        exc = self.capture(failing_worker_step, "x" * 2000)
        record = le.error_record(exc)
        self.assertLessEqual(len(record["message"]), le.MESSAGE_CHARS)
        self.assertTrue(all(len(line) <= le.LINE_CHARS for line in record["traceback"]))

    def test_exception_without_traceback(self):
        record = le.error_record(ValueError("never raised"))
        self.assertEqual(record["where"], "unknown")


class Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_is_private_and_atomic(self):
        try:
            failing_worker_step(SECRET)
        except RuntimeError as exc:
            path = le.write_last_error(self.root, "cleanup", exc, secrets=[SECRET])
        self.assertEqual(path, os.path.join(self.root, "run", "cleanup.last_error.json"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            record = json.load(f)
        self.assertEqual(record["message"], "login failed for password ***")
        self.assertEqual(os.listdir(os.path.join(self.root, "run")), ["cleanup.last_error.json"])   # no temp left

    def test_never_raises(self):
        with mock.patch.object(le.os, "open", side_effect=PermissionError("no")):
            path = le.write_last_error(self.root, "inspection_left", RuntimeError("x"))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.listdir(os.path.join(self.root, "run")), [])

    def test_clear_at_start(self):
        path = le.write_last_error(self.root, "inspection_right", RuntimeError("old run"))
        self.assertTrue(os.path.exists(path))
        le.clear_last_error(self.root, "inspection_right")
        self.assertFalse(os.path.exists(path))
        le.clear_last_error(self.root, "inspection_right")          # nothing there: no error

    def test_same_format_as_the_worker_copies(self):
        """The two copies it replaces (read-only import) produce the same keys and `where` format."""
        for folder, module in (("inspection", "last_error"), ("cleanup", "cleanup_last_error")):
            sys.path.insert(0, os.path.join(REPO_ROOT, "workers", folder))
            try:
                copy = __import__(module)
            finally:
                sys.path.pop(0)
            try:
                failing_worker_step(SECRET)
            except RuntimeError as exc:
                old = copy.error_record(exc, [SECRET])
                new = le.error_record(exc, [SECRET], own_code_dir=os.path.dirname(os.path.abspath(__file__)))
            self.assertEqual(set(old), set(new))
            self.assertEqual((old["type"], old["message"]), (new["type"], new["message"]))
            sys.modules.pop(module, None)


if __name__ == "__main__":
    unittest.main()
