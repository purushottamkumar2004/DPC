"""Tests for common/config/model_package.py (STATUS Phase 3 item 6, CONTRACTS 12, D-056).

Run from the repo root:
    python3 -m unittest tests.unit.test_model_package -v
"""

import json
import os
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.config import model_package as mp  # noqa: E402
from common.config import site_config as sc  # noqa: E402
from model_fixture import CLASSES, LABEL_MAP, make_package, sha256_bytes  # noqa: E402

NAME, VERSION = "dpc_maskrcnn", "2026.10.1"


class _Models(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self, folder, **kwargs):
        with self.assertRaises(mp.ModelPackageError) as ctx:
            mp.verify_package(folder, **kwargs)
        return ctx.exception.problems

    def rewrite_manifest(self, folder, **changes):
        path = os.path.join(folder, "manifest.json")
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest.update(changes)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)


class Verify(_Models):
    def test_good_package(self):
        folder = make_package(self.models, NAME, VERSION)
        package = mp.verify_package(folder, name=NAME, version=VERSION)
        self.assertEqual((package.name, package.version, package.trained_at), (NAME, VERSION, "2026-10-01"))
        self.assertEqual(package.weights, os.path.join(folder, "model_final.pth"))
        self.assertEqual(package.detectron_yaml, os.path.join(folder, "mask_rcnn.yaml"))
        self.assertEqual(package.classes, tuple(CLASSES))
        self.assertEqual(package.label_map, LABEL_MAP)
        self.assertTrue(package.ready)

    def test_wrong_checksum_is_rejected(self):                       # item 6 done when
        folder = make_package(self.models, NAME, VERSION)
        with open(os.path.join(folder, "model_final.pth"), "ab") as f:
            f.write(b"one more byte")
        self.assertEqual(self.problems(folder),
                         ["model_final.pth: checksum does not match the manifest (corrupt or incomplete copy)"])

    def test_missing_file(self):
        folder = make_package(self.models, NAME, VERSION)
        os.remove(os.path.join(folder, "mask_rcnn.yaml"))
        self.assertEqual(self.problems(folder), ["mask_rcnn.yaml: missing"])

    def test_missing_manifest(self):                                 # incomplete download (D-057)
        folder = make_package(self.models, NAME, VERSION)
        os.remove(os.path.join(folder, "manifest.json"))
        self.assertEqual(self.problems(folder), ["manifest.json: missing"])

    def test_not_ready_is_reported_not_refused(self):
        folder = make_package(self.models, NAME, VERSION, ready=False)
        self.assertFalse(mp.verify_package(folder).ready)

    def test_name_and_version_must_match_folder_and_site_yaml(self):
        folder = make_package(self.models, NAME, VERSION, manifest_overrides={"version": "2026.9.9"})
        self.assertIn("manifest.json: version is '2026.9.9' but the folder says '2026.10.1'", self.problems(folder))
        folder = make_package(self.models, NAME, "2026.10.2")
        self.assertIn("manifest.json: name is 'dpc_maskrcnn' but site.yaml says 'other'",
                      self.problems(folder, name="other"))

    def test_manifest_structure(self):
        folder = make_package(self.models, NAME, VERSION)
        self.rewrite_manifest(folder, files={"../evil.pth": "0" * 64, "a.yaml": "xyz", "b.yaml": "1" * 64})
        problems = self.problems(folder)
        self.assertIn("manifest.json: files: '../evil.pth' is not a file inside the version folder", problems)
        self.assertIn("manifest.json: files: a.yaml: not a sha256 (64 hex characters)", problems)
        self.assertIn("manifest.json: files must list classes.json", problems)
        self.assertIn("manifest.json: files must list exactly one Detectron config file (.yaml/.yml), found 2",
                      problems)

    def test_manifest_not_json(self):
        folder = make_package(self.models, NAME, VERSION)
        with open(os.path.join(folder, "manifest.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.problems(folder), ["manifest.json: not valid JSON (JSONDecodeError)"])

    def test_uppercase_checksum_is_accepted(self):
        folder = make_package(self.models, NAME, VERSION)
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.rewrite_manifest(folder, files={k: v.upper() for k, v in manifest["files"].items()})
        mp.verify_package(folder)

    def test_classes_json_rules(self):
        folder = make_package(self.models, NAME, VERSION,
                              classes_doc={"classes": ["wheel", "wheel"], "label_map": {"chain": "x"}, "x": 1})
        problems = self.problems(folder)
        self.assertIn("classes.json: classes contains a name more than once", problems)
        self.assertIn("classes.json: label_map key 'chain' is not in classes", problems)
        self.assertIn("classes.json: unknown key(s) x", problems)

    def test_manifest_classes_must_match(self):
        folder = make_package(self.models, NAME, VERSION, manifest_overrides={"classes": ["wheel"]})
        self.assertEqual(self.problems(folder), ["manifest.json: classes differ from classes.json"])

    def test_extra_manifest_keys_are_allowed(self):
        folder = make_package(self.models, NAME, VERSION, manifest_overrides={"notes": "retrained", "classes": CLASSES})
        self.assertEqual(mp.read_manifest(folder).extra["notes"], "retrained")

    def test_sha256_of_streams(self):
        path = os.path.join(self.models, "big.bin")
        data = os.urandom(3 * 1024 * 1024 + 7)
        with open(path, "wb") as f:
            f.write(data)
        self.assertEqual(mp.sha256_of(path), sha256_bytes(data))


class CurrentPointer(_Models):
    def test_symlink_and_plain_file(self):
        make_package(self.models, NAME, VERSION)
        root = os.path.join(self.models, NAME)
        self.assertEqual(mp.read_current(root), VERSION)
        os.remove(os.path.join(root, "current"))
        self.assertIsNone(mp.read_current(root))
        with open(os.path.join(root, "current"), "w") as f:
            f.write(VERSION + "\n")
        self.assertEqual(mp.read_current(root), VERSION)

    def test_set_current_is_atomic_and_checked(self):
        make_package(self.models, NAME, VERSION)
        make_package(self.models, NAME, "2026.11.1", set_current=False)
        root = os.path.join(self.models, NAME)
        mp.set_current(root, "2026.11.1")
        self.assertEqual(mp.read_current(root), "2026.11.1")
        self.assertEqual(os.readlink(os.path.join(root, "current")), "2026.11.1")      # relative target
        self.assertEqual([n for n in os.listdir(root) if n.startswith(".")], [])       # no temp left
        with self.assertRaises(mp.ModelPackageError):
            mp.set_current(root, "9.9.9")                 # no such folder
        with self.assertRaises(mp.ModelPackageError):
            mp.set_current(root, "../x")
        self.assertEqual(mp.read_current(root), "2026.11.1")

    def test_bad_pointer(self):
        root = os.path.join(self.models, NAME)
        os.makedirs(os.path.join(root, "current"))
        with self.assertRaises(mp.ModelPackageError):
            mp.read_current(root)


class FromSiteConfig(_Models):
    def config(self):
        root = os.path.join(self.models, "INSIGHTZZ")
        with open(os.path.join(REPO_ROOT, "config", "examples", "site.example.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["paths"]["root"] = root
        data["inspection"]["model"] = {"name": NAME, "version": VERSION}
        path = os.path.join(self.models, "site.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        os.chmod(path, 0o600)
        cfg = sc.load_site_config(path)
        return cfg, os.path.join(root, "data", "models")

    def test_running_version_prefers_current(self):
        cfg, models = self.config()
        make_package(models, NAME, VERSION, set_current=False)
        self.assertEqual(mp.running_version(cfg), VERSION)
        self.assertEqual(mp.load_model_package(cfg).version, VERSION)
        make_package(models, NAME, "2026.11.1")
        self.assertEqual(mp.running_version(cfg), "2026.11.1")
        self.assertEqual(mp.load_model_package(cfg).version, "2026.11.1")

    def test_missing_package(self):
        cfg, _ = self.config()
        with self.assertRaises(mp.ModelPackageError) as ctx:
            mp.load_model_package(cfg)
        self.assertEqual(ctx.exception.problems, ["version folder not found"])


if __name__ == "__main__":
    unittest.main()
