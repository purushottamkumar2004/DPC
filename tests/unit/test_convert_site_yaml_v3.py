"""Tests for scripts/convert_site_yaml_v3.py (Phase 3 item 6: Tadipatri v2 -> v3).

Run from the repo root:
    python3 -m unittest tests.unit.test_convert_site_yaml_v3 -v
"""

import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)
_spec = importlib.util.spec_from_file_location("convert", os.path.join(REPO_ROOT, "scripts", "convert_site_yaml_v3.py"))
conv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conv)

from common.config import site_config as sc  # noqa: E402

V2_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "site_v2.yaml")
SECRET = "s3cret-db-Qx77"


class Convert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        with open(V2_FIXTURE, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("root: ~/INSIGHTZZ", f"root: {self.dir}/INSIGHTZZ")
        text = text.replace('password: "CHANGE_ME"', f'password: "{SECRET}"', 1)
        self.source = os.path.join(self.dir, "site.yaml")
        with open(self.source, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(self.source, 0o600)
        self.original = text

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = conv.main(list(args))
        return code, out.getvalue()

    def test_converts_and_checks(self):
        code, out = self.run_main(self.source, "--model-version", "2026.10.1")
        self.assertEqual(code, 0, out)
        target = os.path.join(self.dir, "site.v3.yaml")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        cfg = sc.load_site_config(target)
        self.assertEqual((cfg.inspection.model.name, cfg.inspection.model.version), ("example_model", "2026.10.1"))
        self.assertEqual(cfg.db.password, SECRET)
        self.assertIn("cameras -> capture.cameras", out)
        self.assertIn("model.classes -> removed (now in the model package)", out)
        self.assertIn("Check: OK", out)
        self.assertNotIn(SECRET, out)
        with open(self.source, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.original)             # original untouched

    def test_refuses_existing_output_and_non_v2(self):
        self.run_main(self.source, "--model-version", "1")
        code, out = self.run_main(self.source, "--model-version", "1")
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        v3 = os.path.join(self.dir, "site.v3.yaml")
        code, out = self.run_main(v3, "--model-version", "1", "--output", os.path.join(self.dir, "x.yaml"))
        self.assertEqual(code, 1)
        self.assertIn("not a v2 file", out)

    def test_refuses_world_readable_input(self):
        os.chmod(self.source, 0o644)
        code, out = self.run_main(self.source, "--model-version", "1")
        self.assertEqual(code, 1)
        self.assertIn("chmod 600", out)

    def test_reports_remaining_problems(self):
        with open(self.source, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["mystery"] = 1
        with open(self.source, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        code, out = self.run_main(self.source, "--model-version", "1")
        self.assertEqual(code, 1)
        self.assertIn("mystery: unknown key", out)
        self.assertNotIn(SECRET, out)


if __name__ == "__main__":
    unittest.main()
