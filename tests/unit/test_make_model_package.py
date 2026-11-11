"""Tests for scripts/make_model_package.py (STATUS 6b, F48, F49).

Run from the repo root:
    python3 -m unittest tests.unit.test_make_model_package -v
"""

import hashlib
import importlib.util
import io
import json
import os
import shutil
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

import yaml  # noqa: E402

# A self-contained Detectron config in miniature: ROI_HEADS.NUM_CLASSES is the key the tool sets,
# RETINANET.NUM_CLASSES must stay as it is.
TEST_CONFIG = (b"MODEL:\n  MASK_ON: true\n  RETINANET:\n    NUM_CLASSES: 80\n"
               b"  ROI_HEADS:\n    NAME: StandardROIHeads\n    NUM_CLASSES: 3\n    SCORE_THRESH_TEST: 0.5\n"
               b"OUTPUT_DIR: ./output\n")


def num_classes_of(path):
    with open(path, encoding="utf-8") as f:
        model = yaml.safe_load(f)["MODEL"]
    return model["ROI_HEADS"]["NUM_CLASSES"], model.get("RETINANET", {}).get("NUM_CLASSES")

# Same shape as workers/inspection/leftConfig.example.xml (<ALL_CLASS_NAMES> deep in the tree).
LEGACY_XML = """<DDPC><A/><B/><C/><D><x/><x/><x/><x/><x/>
<ALL_CLASS_NAMES>["bucket_clip", "nut_bolt", "dusty_chain_base", "wheel", "plate", "dusty_wheel_bolt"]</ALL_CLASS_NAMES>
</D></DDPC>"""
XML_CLASSES = ["bucket_clip", "nut_bolt", "dusty_chain_base", "wheel", "plate", "dusty_wheel_bolt"]


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.src = os.path.join(self.dir, "legacy_model")
        os.makedirs(self.src)
        self.weights = os.path.join(self.src, "model_final.pth")
        self.yaml = os.path.join(self.src, "mask_rcnn_R_50_FPN_3x.yaml")
        self.xml = os.path.join(self.src, "leftConfig.xml")
        for path, data in ((self.weights, b"w" * 5000), (self.yaml, TEST_CONFIG),
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


class Package(_Base):
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
        self.assertIn("supervisor.py model switch dpc_maskrcnn 2026.11.1", out)

    def test_no_current_flag(self):
        code, out = self.run_tool("--classes", "wheel", "--no-current")
        self.assertEqual(code, 0)
        self.assertIsNone(mp.read_current(os.path.join(self.models, "dpc_maskrcnn")))

    def test_base_config_warning(self):
        with open(self.yaml, "w") as f:
            f.write("_BASE_: ../Base-RCNN-FPN.yaml\nMODEL:\n  ROI_HEADS:\n    NUM_CLASSES: 3\n")
        self.before[self.yaml] = (digest(self.yaml), os.stat(self.yaml).st_mtime_ns)
        code, out = self.run_tool("--classes", "wheel")
        self.assertEqual(code, 0)
        self.assertIn("WARNING mask_rcnn_R_50_FPN_3x.yaml uses _BASE_", out)


class Repo(_Base):                                                 # D-065 history; --export retired (D-088)
    def setUp(self):
        super().setUp()
        self.repo_models = os.path.join(self.dir, "repo", "models")

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = mk.main(list(args))
        return code, out.getvalue()

    def repo_version(self):
        """A version kept in git the D-065 way (manifest, classes.json, yaml; never the weights)."""
        code, out = self.run_tool("--classes-from-xml", self.xml, "--legacy-label-map")
        self.assertEqual(code, 0, out)
        installed = os.path.join(self.models, "dpc_maskrcnn", "2026.10.1")
        repo_dir = os.path.join(self.repo_models, "dpc_maskrcnn", "2026.10.1")
        os.makedirs(repo_dir)
        for name in os.listdir(installed):
            if not name.endswith(".pth") and name != "ready":
                shutil.copyfile(os.path.join(installed, name), os.path.join(repo_dir, name))
        return repo_dir

    def test_export_to_repo_is_retired(self):                        # D-088
        code, out = self.run_tool("--classes", "wheel", "--export-to-repo")
        self.assertEqual(code, 1)
        self.assertIn("--export-to-repo is retired (D-088)", out)
        self.assertFalse(os.path.exists(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1")))

    def test_the_tadipatri_version_in_git_still_installs(self):
        """models/dpc_maskrcnn/2026.10.12 stays as history; its manifest still verifies against its files."""
        repo_dir = os.path.join(REPO_ROOT, "models", "dpc_maskrcnn", "2026.10.12")
        manifest = mp.read_manifest(repo_dir)
        for name, sha in manifest.files.items():
            if not name.endswith(".pth"):
                self.assertEqual(digest(os.path.join(repo_dir, name)), sha, name)

    def test_from_repo_installs_with_only_the_weights(self):
        repo_dir = self.repo_version()
        other_site = os.path.join(self.dir, "SITE2", "data", "models")
        code, out = self.run_cli("--from-repo", repo_dir, "--weights", self.weights, "--models-dir", other_site)
        self.assertEqual(code, 0, out)
        installed = os.path.join(other_site, "dpc_maskrcnn", "2026.10.1")
        package = mp.verify_package(installed, name="dpc_maskrcnn", version="2026.10.1")
        self.assertEqual(list(package.classes), XML_CLASSES)
        self.assertTrue(package.ready)
        self.assertEqual(mp.read_current(os.path.join(other_site, "dpc_maskrcnn")), "2026.10.1")
        with open(os.path.join(installed, "manifest.json")) as a, open(os.path.join(repo_dir, "manifest.json")) as b:
            self.assertEqual(a.read(), b.read())                      # the repo manifest, as it is
        self.assertSourcesUntouched()

    def test_from_repo_refuses_other_weights(self):
        repo_dir = self.repo_version()
        wrong = os.path.join(self.dir, "other.pth")
        with open(wrong, "wb") as f:
            f.write(b"different weights")
        other_site = os.path.join(self.dir, "SITE2", "data", "models")
        code, out = self.run_cli("--from-repo", repo_dir, "--weights", wrong, "--models-dir", other_site)
        self.assertEqual(code, 1)
        self.assertIn("sha256 differs from the repo manifest", out)
        self.assertFalse(os.path.exists(os.path.join(other_site, "dpc_maskrcnn", "2026.10.1")))

    def test_from_repo_refuses_a_changed_repo_file(self):
        repo_dir = self.repo_version()
        with open(os.path.join(repo_dir, "classes.json"), "a") as f:
            f.write(" ")
        code, out = self.run_cli("--from-repo", repo_dir, "--weights", self.weights,
                                 "--models-dir", os.path.join(self.dir, "SITE2"))
        self.assertEqual(code, 1)
        self.assertIn("classes.json in the repo does not match its manifest sha256", out)

    def test_from_repo_takes_no_other_inputs(self):
        repo_dir = self.repo_version()
        code, out = self.run_cli("--from-repo", repo_dir, "--weights", self.weights, "--classes", "wheel",
                                 "--version", "9")
        self.assertEqual(code, 1)
        self.assertIn("--from-repo takes only --weights", out)
        code, out = self.run_cli("--from-repo", repo_dir)
        self.assertIn("--from-repo needs --weights", out)

    def test_from_repo_folder_must_match_manifest(self):
        repo_dir = self.repo_version()
        moved = os.path.join(self.repo_models, "dpc_maskrcnn", "9.9.9")
        os.rename(repo_dir, moved)
        code, out = self.run_cli("--from-repo", moved, "--weights", self.weights,
                                 "--models-dir", os.path.join(self.dir, "SITE2"))
        self.assertEqual(code, 1)
        self.assertIn("manifest says dpc_maskrcnn/2026.10.1, the folder is dpc_maskrcnn/9.9.9", out)


class CocoAndSharedConfig(_Base):                                    # F49, D-088
    def write_coco(self, categories):
        path = os.path.join(self.src, "train.json")
        with open(path, "w") as f:
            json.dump({"images": [], "annotations": [], "categories": categories}, f)
        self.before[path] = (digest(path), os.stat(path).st_mtime_ns)
        return path

    def run_default_config(self, *extra, version="2026.11.10"):
        """No --detectron-yaml: the shared config in git is used."""
        args = ["--name", "dpc_maskrcnn", "--version", version, "--weights", self.weights,
                "--models-dir", self.models] + list(extra)
        out = io.StringIO()
        with redirect_stdout(out):
            code = mk.main(args)
        return code, out.getvalue()

    def test_shuffled_ids_give_the_right_order(self):                # F49 done when
        coco = self.write_coco([{"id": 3, "name": "plate"}, {"id": 1, "name": "wheel"},
                                {"id": 7, "name": "nut_bolt"}, {"id": 2, "name": "bucket_clip"}])
        code, out = self.run_tool("--classes-from-coco", coco)
        self.assertEqual(code, 0, out)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1"))
        self.assertEqual(package.classes, ("wheel", "bucket_clip", "plate", "nut_bolt"))
        self.assertSourcesUntouched()

    def test_package_yaml_has_num_classes_equal_to_the_class_count(self):   # F49 done when
        coco = self.write_coco([{"id": i, "name": f"class_{i}"} for i in range(1, 8)])
        self.run_tool("--classes-from-coco", coco)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1"))
        self.assertEqual(num_classes_of(package.detectron_yaml), (7, 80))   # RETINANET untouched
        self.assertEqual(num_classes_of(self.yaml), (3, 80))                # the source is not changed
        self.assertSourcesUntouched()

    def test_default_config_is_the_shared_one(self):                 # F49 done when
        coco = self.write_coco([{"id": 1, "name": "wheel"}, {"id": 2, "name": "plate"}])
        code, out = self.run_default_config("--classes-from-coco", coco)
        self.assertEqual(code, 0, out)
        self.assertIn("mask_rcnn_R101_FPN_3x.yaml with NUM_CLASSES 2 (shared, D-088)", out)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.11.10"))
        self.assertEqual(os.path.basename(package.detectron_yaml), "mask_rcnn_R101_FPN_3x.yaml")
        self.assertEqual(num_classes_of(package.detectron_yaml), (2, 80))
        with open(mk.SHARED_DETECTRON_YAML, encoding="utf-8") as a, open(package.detectron_yaml, encoding="utf-8") as b:
            shared, packaged = a.read().splitlines(), b.read().splitlines()
        diff = [(x, y) for x, y in zip(shared, packaged) if x != y]
        self.assertEqual(len(shared), len(packaged))
        self.assertEqual(len(diff), 1)                               # only the NUM_CLASSES line
        self.assertEqual(diff[0][1].strip(), "NUM_CLASSES: 2")

    def test_shared_config_in_git(self):
        self.assertEqual(num_classes_of(mk.SHARED_DETECTRON_YAML), (0, 80))
        with open(mk.SHARED_DETECTRON_YAML, encoding="utf-8") as f:
            self.assertNotIn("_BASE_", yaml.safe_load(f))            # self-contained: no base file

    def test_label_map_as_today(self):
        coco = self.write_coco([{"id": 1, "name": "wheel"}, {"id": 2, "name": "dusty_chain_base"}])
        code, out = self.run_tool("--classes-from-coco", coco, "--legacy-label-map", "--label-map", "wheel=wheel")
        self.assertEqual(code, 0, out)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1"))
        self.assertEqual(package.label_map, {"dusty_chain_base": "bucket_clip", "wheel": "wheel"})

    def test_bad_coco_files_are_refused(self):
        cases = [
            ([{"id": 1, "name": "wheel"}, {"id": 2, "name": "wheel"}], "name 'wheel' appears more than once"),
            ([{"id": 1, "name": "wheel"}, {"id": 1, "name": "plate"}], "id 1 appears more than once"),
            ([{"id": 1, "name": "wheel"}, {"id": 2}], "categories[1]: name missing"),
            ([{"id": 1, "name": ""}], "categories[0]: name missing"),
            ([{"id": "1", "name": "wheel"}], "categories[0]: id must be an integer"),
            ([], "has no `categories` list"),
        ]
        for categories, message in cases:
            code, out = self.run_tool("--classes-from-coco", self.write_coco(categories))
            self.assertEqual(code, 1, categories)
            self.assertIn(message, out)
            self.assertFalse(os.path.exists(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1")))
        code, out = self.run_tool("--classes-from-coco", os.path.join(self.src, "missing.json"))
        self.assertIn("cannot read", out)

    def test_config_without_roi_heads_is_refused(self):
        with open(self.yaml, "wb") as f:
            f.write(b"_BASE_: Base-RCNN-FPN.yaml\nMODEL:\n  MASK_ON: true\n")
        self.before[self.yaml] = (digest(self.yaml), os.stat(self.yaml).st_mtime_ns)
        code, out = self.run_tool("--classes", "wheel")
        self.assertEqual(code, 1)
        self.assertIn("no MODEL: ROI_HEADS: NUM_CLASSES: line to set", out)

    def test_one_class_source_only(self):
        coco = self.write_coco([{"id": 1, "name": "wheel"}])
        code, out = self.run_tool("--classes-from-coco", coco, "--classes", "wheel")
        self.assertEqual(code, 1)
        self.assertIn("give exactly one of --classes-from-coco", out)

    def test_switch_hint_names_the_model(self):                      # F48
        self.run_tool("--classes", "wheel")
        code, out = self.run_tool("--classes", "wheel", version="2026.11.1")
        self.assertEqual(code, 0)
        self.assertIn("supervisor.py model switch dpc_maskrcnn 2026.11.1", out)


class IndexMapF50(_Base):                                            # F50, D-089
    def write_json(self, data, name="classes_map.json"):
        path = os.path.join(self.src, name)
        with open(path, "w") as f:
            json.dump(data, f)
        self.before[path] = (digest(path), os.stat(path).st_mtime_ns)
        return path

    def test_index_map_in_classes_from_coco(self):
        path = self.write_json({"2": "plate", "0": "wheel", "1": "bucket_clip"})
        code, out = self.run_tool("--classes-from-coco", path)
        self.assertEqual(code, 0, out)
        package = mp.verify_package(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1"))
        self.assertEqual(package.classes, ("wheel", "bucket_clip", "plate"))
        self.assertEqual(num_classes_of(package.detectron_yaml)[0], 3)
        self.assertSourcesUntouched()

    def test_index_map_in_classes_file(self):
        code, out = self.run_tool("--classes-file", self.write_json({"0": "wheel", "1": "plate"}))
        self.assertEqual(code, 0, out)

    def test_bad_index_maps_are_refused(self):
        cases = [({"0": "wheel", "2": "plate"}, "without gaps; missing [1]"),
                 ({"0": "wheel", "x": "plate"}, "key 'x' is not a class index"),
                 ({"0": "wheel", "1": "wheel"}, "name 'wheel' appears more than once"),
                 ({"0": "wheel", "1": ""}, "index 1: name missing")]
        for data, message in cases:
            code, out = self.run_tool("--classes-from-coco", self.write_json(data))
            self.assertEqual(code, 1, data)
            self.assertIn(message, out)
            self.assertFalse(os.path.exists(os.path.join(self.models, "dpc_maskrcnn", "2026.10.1")))


if __name__ == "__main__":
    unittest.main()
