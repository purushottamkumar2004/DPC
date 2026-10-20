"""Tests for scripts/make_model_package.py (STATUS 6b).

Run from the repo root:
    python3 -m unittest tests.unit.test_make_model_package -v
"""

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)
_spec = importlib.util.spec_from_file_location("make_pkg", os.path.join(REPO_ROOT, "scripts", "make_model_package.py"))
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

from common.config import model_package as mp  # noqa: E402

# Same shape as workers/inspection/leftConfig.example.xml (<ALL_CLASS_NAMES> deep in the tree).
LEGACY_XML = """<DDPC><A/><B/><C/><D><x/><x/><x/><x/><x/>
<ALL_CLASS_NAMES>["bucket_clip", "nut_bolt", "dusty_chain_base", "wheel", "plate", "dusty_wheel_bolt"]</ALL_CLASS_NAMES>
</D></DDPC>"""
XML_CLASSES = ["bucket_clip", "nut_bolt", "dusty_chain_base", "wheel", "plate", "dusty_wheel_bolt"]


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Package(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.src = os.path.join(self.dir, "legacy_model")
        os.makedirs(self.src)
        self.weights = os.path.join(self.src, "model_final.pth")
        self.yaml = os.path.join(self.src, "mask_rcnn_R_50_FPN_3x.yaml")
        self.xml = os.path.join(self.src, "leftConfig.xml")
        for path, data in ((self.weights, b"w" * 5000), (self.yaml, b"MODEL:\n  MASK_ON: True\n"),
                           (self.xml, LEGACY_XML.encode())):
            with open(path, "wb") as f:
                f.write(data)
        self.models = os.path.join(self.dir, "INSIGHTZZ", "data", "models")
        self.before = {p: (digest(p), os.stat(p).st_mtime_ns) for p in (self.weights, self.yaml, self.xml)}

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *extra, version="2026.10.1"):
        args = ["--name", "dpc_maskrcnn", "--version", version, "--weights", self.weights,
                "--detectron-yaml", self.yaml, "--models-dir", self.models] + list(extra)
        out = io.StringIO()
        with redirect_stdout(out):
            code = mk.main(args)
        return code, out.getvalue()

    def assertSourcesUntouched(self):
        for path, before in self.before.items():
            self.assertEqual((digest(path), os.stat(path).st_mtime_ns), before, f"{path} changed")

    # ---- 6b done when: the present model is packaged and dpc-check's model line can PASS ----
    def test_packages_from_legacy_xml_with_legacy_label_map(self):
        code, out = self.run_tool("--classes-from-xml", self.xml, "--legacy-label-map", "--trained-at", "2026-09-30")
        self.assertEqual(code, 0, out)
        folder = os.path.join(self.models, "dpc_maskrcnn", "2026.10.1")
        package = mp.verify_package(folder, name="dpc_maskrcnn", version="2026.10.1")
        self.assertEqual(list(package.classes), XML_CLASSES)                  # order kept exactly
        self.assertEqual(package.label_map, {"dusty_chain_base": "bucket_clip", "dusty_wheel_bolt": "wheel_bolt"})
        self.assertEqual(os.path.basename(package.weights), "model_final.pth")
        self.assertEqual(os.path.basename(package.detectron_yaml), "mask_rcnn_R_50_FPN_3x.yaml")
        self.assertTrue(package.ready)
        self.assertEqual(mp.read_current(os.path.join(self.models, "dpc_maskrcnn")), "2026.10.1")
        with open(os.path.join(folder, "manifest.json")) as f:
            self.assertEqual(json.load(f)["trained_at"], "2026-09-30")
        self.assertIn("skipped (no such class in this model): dusty_chain_base_nut_bolt", out)
        self.assertIn("current -> 2026.10.1", out)
        self.assertSourcesUntouched()

    def test_classes_flag_and_explicit_label_map(self):
        code, out = self.run_tool("--classes", "wheel, plate,bucket_clip,dusty_chain_base",
                                  "--label-map", "dusty_chain_base=bucket_clip")
        self.assertEqual(code, 0, out)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1"))
        self.assertEqual(package.classes, ("wheel", "plate", "bucket_clip", "dusty_chain_base"))
        self.assertEqual(package.label_map, {"dusty_chain_base": "bucket_clip"})

    def test_classes_file(self):
        path = os.path.join(self.dir, "classes.list.json")
        with open(path, "w") as f:
            json.dump(["wheel", "plate"], f)
        code, out = self.run_tool("--classes-file", path)
        self.assertEqual(code, 0, out)

    def test_trained_at_defaults_to_weights_date(self):
        os.utime(self.weights, (1767225600, 1767225600))          # 2026-01-01 UTC
        self.before[self.weights] = (digest(self.weights), os.stat(self.weights).st_mtime_ns)
        self.run_tool("--classes", "wheel")
        with open(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1", "manifest.json")) as f:
            self.assertIn(json.load(f)["trained_at"], ("2025-12-31", "2026-01-01"))   # local time zone
        self.assertSourcesUntouched()

    # ---- refusals: nothing is written ----
    def assertNothingWritten(self):
        self.assertFalse(os.path.exists(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1")))

    def test_bad_inputs(self):
        cases = [
            (["--classes", "wheel,wheel"], "a name appears more than once"),
            (["--classes", "wheel", "--label-map", "chain=x"], "label map: 'chain' is not one of the classes"),
            (["--classes", "wheel", "--label-map", "broken"], "is not model_class=label"),
            (["--classes", "wheel", "--classes-from-xml", self.xml], "give exactly one of"),
            ([], "give exactly one of"),
        ]
        for extra, message in cases:
            code, out = self.run_tool(*extra)
            self.assertEqual(code, 1, extra)
            self.assertIn(message, out)
            self.assertNothingWritten()

    def test_bad_files_and_names(self):
        code, out = self.run_tool("--classes", "wheel", version="../x")
        self.assertIn("--version: '../x' must be a folder name", out)
        self.weights = self.yaml
        code, out = self.run_tool("--classes", "wheel")
        self.assertIn("--weights:", out)
        self.assertEqual(code, 1)
        self.assertNothingWritten()

    def test_xml_without_class_list(self):
        with open(self.xml, "w") as f:
            f.write("<DDPC><A/></DDPC>")
        self.before[self.xml] = (digest(self.xml), os.stat(self.xml).st_mtime_ns)
        code, out = self.run_tool("--classes-from-xml", self.xml)
        self.assertEqual(code, 1)
        self.assertIn("no <ALL_CLASS_NAMES>", out)

    def test_existing_version_is_never_overwritten(self):
        self.run_tool("--classes", "wheel")
        code, out = self.run_tool("--classes", "wheel,plate")
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1"))
        self.assertEqual(package.classes, ("wheel",))

    def test_no_temp_folder_left_after_a_failure(self):
        os.makedirs(os.path.join(self.models, "dpc_maskrcnn"))
        original = mk.mp.sha256_of
        mk.mp.sha256_of = lambda path: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                self.run_tool("--classes", "wheel")
        finally:
            mk.mp.sha256_of = original
        self.assertEqual(os.listdir(os.path.join(self.models, "dpc_maskrcnn")), [])

    # ---- current: first install only (D-060: afterwards the Supervisor owns it) ----
    def test_second_version_does_not_move_current(self):
        self.run_tool("--classes", "wheel")
        code, out = self.run_tool("--classes", "wheel", version="2026.11.1")
        self.assertEqual(code, 0)
        self.assertEqual(mp.read_current(os.path.join(self.models, "dpc_maskrcnn")), "2026.10.1")
        self.assertIn("current not changed: it is already set (2026.10.1)", out)
        self.assertIn("model switch 2026.11.1", out)

    def test_no_current_flag(self):
        code, out = self.run_tool("--classes", "wheel", "--no-current")
        self.assertEqual(code, 0)
        self.assertIsNone(mp.read_current(os.path.join(self.models, "dpc_maskrcnn")))

    def test_base_config_warning(self):
        with open(self.yaml, "w") as f:
            f.write("_BASE_: ../Base-RCNN-FPN.yaml\nMODEL: {}\n")
        self.before[self.yaml] = (digest(self.yaml), os.stat(self.yaml).st_mtime_ns)
        code, out = self.run_tool("--classes", "wheel")
        self.assertEqual(code, 0)
        self.assertIn("WARNING mask_rcnn_R_50_FPN_3x.yaml uses _BASE_", out)


if __name__ == "__main__":
    unittest.main()
