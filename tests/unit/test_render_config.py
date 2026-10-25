"""Tests for the renderer: common/config/render.py and scripts/render_config.py (STATUS P4 / 6e,
CONTRACTS 13, D-062, D-063). Reference values: docs/sites/tadipatri.site.reference.yaml.

Run from the repo root:
    python3 -m unittest tests.unit.test_render_config -v
"""

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common.config import render as rd  # noqa: E402
from common.config.site_config import SiteConfigError  # noqa: E402

_spec = importlib.util.spec_from_file_location("render_cli", os.path.join(REPO_ROOT, "scripts", "render_config.py"))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

REFERENCE = os.path.join(REPO_ROOT, "docs", "sites", "tadipatri.site.reference.yaml")
DB_PASSWORD = "s3cret-db-Rt55"
GRAPH_SECRET = "s3cret-graph-Uy21"
WORKERS = ["frame_capture_cam1", "frame_capture_cam2", "inspection_left", "inspection_right"]


class Site(unittest.TestCase):
    """The Tadipatri reference file with the 6g keys switched on and test secrets filled in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        with open(REFERENCE, encoding="utf-8") as f:
            text = f.read()
        text = (text.replace("      # ip: 192.168.1.102", "      ip: 192.168.1.102")
                    .replace("      # ip: 192.168.1.103", "      ip: 192.168.1.103")
                    .replace("  # light_on_value: 0", "  light_on_value: 0"))
        self.data = yaml.safe_load(text)
        self.data["paths"]["root"] = self.root
        self.data["db"]["password"] = DB_PASSWORD
        self.data["graph"] = {"tenant_id": "t", "client_id": "c", "client_secret": GRAPH_SECRET}
        self.path = os.path.join(self._tmp.name, "site.yaml")
        self.write()
        self.rendered = os.path.join(self.root, "config", "rendered")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, sort_keys=False)
        os.chmod(self.path, mode)

    def render(self, **kwargs):
        return {c.worker_id: c.status for c in rd.render(self.path, **kwargs)}

    def load(self, worker_id):
        with open(os.path.join(self.rendered, f"{worker_id}.json"), encoding="utf-8") as f:
            return json.load(f)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main([self.path] + list(args))
        return code, out.getvalue()


class Tadipatri(Site):                                               # P4 done when
    def test_four_files_then_unchanged(self):
        self.assertEqual(self.render(), {w: "written" for w in WORKERS})
        self.assertEqual(sorted(os.listdir(self.rendered)), [f"{w}.json" for w in WORKERS])
        self.assertEqual(self.render(), {w: "unchanged" for w in WORKERS})

    def test_cli_lines(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"written {w}" for w in WORKERS])
        code, out = self.run_cli()
        self.assertEqual(out.splitlines(), [f"unchanged {w}" for w in WORKERS])

    def test_files_are_private_and_have_the_header(self):
        self.render()
        with open(self.path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        for worker in WORKERS:
            path = os.path.join(self.rendered, f"{worker}.json")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            header = self.load(worker)["_generated"]
            self.assertEqual((header["from"], header["sha256"], header["renderer"]), ("site.yaml", sha, 1))
            self.assertIn("T", header["at"])
        self.assertEqual([n for n in os.listdir(self.rendered) if n.startswith(".")], [])   # no temp files

    def test_capture_file(self):
        self.render()
        cam2 = self.load("frame_capture_cam2")
        self.assertEqual(cam2["worker_id"], "frame_capture_cam2")
        self.assertEqual(cam2["camera"], {"id": "CAM2", "side": "RIGHT", "driver": "basler", "serial": "24874775",
                                          "fps": 4.0, "exposure_us": 2000, "rotate": 0, "ip": "192.168.1.103"})
        frames = os.path.join(self.root, "data", "frames", "CAM2")
        self.assertEqual(cam2["paths"], {"root": self.root, "logs": os.path.join(self.root, "logs"),
                                         "live": f"{frames}/live", "tmp": f"{frames}/_tmp", "inf": f"{frames}/INF",
                                         "ui": f"{frames}/UI", "raw_images": f"{frames}/RAW_IMAGES"})
        self.assertEqual(cam2["log"], {"backup_count": 5, "file_name": "FrameCapture_CAM2.log"})
        self.assertEqual(cam2["db"]["health_table"], "health_status")
        self.assertEqual(cam2["db"]["password"], DB_PASSWORD)

    def test_inspection_files(self):                                 # D-063
        self.render()
        left, right = self.load("inspection_left"), self.load("inspection_right")
        self.assertEqual((left["side"], left["defect_side"], left["camera_id"]), ("LEFT", "Left", "CAM1"))
        self.assertEqual((right["side"], right["defect_side"], right["camera_id"]), ("RIGHT", "Right", "CAM2"))
        for key in ("threshold", "machine", "model", "auto_roi", "db", "plc"):    # site-wide: same in both
            self.assertEqual(left[key], right[key], key)
        self.assertEqual(left["model"], {"name": "dpc_maskrcnn",
                                         "models_dir": os.path.join(self.root, "data", "models", "dpc_maskrcnn")})
        self.assertEqual(left["machine"], {"total_wheel_count": 190, "expected_cycle_total": 190})
        self.assertEqual(left["plc"], {"ip": "192.168.1.5", "legacy_db_numbers": {"write": 12, "read": 13}})
        self.assertEqual(left["object_sizes"]["chain_w"], 880)
        data = os.path.join(self.root, "data")
        self.assertEqual(right["paths"]["inf"], f"{data}/frames/CAM2/INF")
        self.assertEqual(right["paths"]["raw"], f"{data}/frames/CAM2/RAW_IMAGES")
        self.assertEqual(right["paths"]["defective"], f"{data}/images/DEFECTIVE/RIGHT")
        self.assertEqual(right["paths"]["plate"], f"{data}/images/PLATE/RIGHT")
        self.assertEqual(right["paths"]["defect_raw"], f"{data}/images/RAW/RIGHT")                # D-072
        self.assertEqual(right["paths"]["temp"], f"{data}/images/TEMP/RIGHT")
        self.assertEqual(right["paths"]["magnet"], f"{data}/images/MAGNET/RIGHT")
        self.assertEqual(right["paths"]["bolt_data"], f"{data}/bolt_data/RIGHT")
        self.assertEqual(right["paths"]["wheel_status_images"], f"{data}/wheel_status/IMAGES/RIGHT")   # D-073 D4
        self.assertNotIn("start_file", right["paths"])                                          # D-072 (2)
        for doc in (left, right):                                    # no model files or classes (D-060)
            text = json.dumps(doc)
            for word in ("classes", "weights", ".pth", "detectron", "version"):
                self.assertNotIn(word, text)

    def test_only_needed_secrets(self):
        self.render()
        for worker in WORKERS:
            text = json.dumps(self.load(worker))
            self.assertNotIn(GRAPH_SECRET, text)                     # no worker here uses Graph
            for section in ("graph", "mail", "cleanup", "transfer", "mqtt"):
                self.assertNotIn(f'"{section}"', text)

    def test_object_sizes_per_side(self):
        self.data["inspection"]["sides"]["RIGHT"]["object_sizes"]["wheel_w"] = 600
        self.write()
        self.render()
        self.assertEqual(self.load("inspection_left")["object_sizes"]["wheel_w"], 530)
        self.assertEqual(self.load("inspection_right")["object_sizes"]["wheel_w"], 600)


class Changes(Site):
    def test_only_the_workers_whose_settings_changed(self):
        self.render()
        self.data["mail"]["project_name"] = "Other"                  # nobody rendered reads mail
        self.write()
        self.assertEqual(self.render(), {w: "unchanged" for w in WORKERS})
        self.data["inspection"]["threshold"] = 0.6
        self.write()
        self.assertEqual(self.render(), {"frame_capture_cam1": "unchanged", "frame_capture_cam2": "unchanged",
                                         "inspection_left": "written", "inspection_right": "written"})
        self.data["capture"]["cameras"][0]["exposure_us"] = 1800
        self.write()
        self.assertEqual(self.render()["frame_capture_cam1"], "written")

    def test_db_password_change_rewrites_every_file(self):
        self.render()
        self.data["db"]["password"] = "s3cret-new"
        self.write()
        self.assertEqual(self.render(), {w: "written" for w in WORKERS})

    def test_removed_camera_removes_its_files(self):
        self.render()
        del self.data["capture"]["cameras"][1]
        del self.data["inspection"]["sides"]["RIGHT"]
        self.write()
        changes = self.render()
        self.assertEqual(changes["frame_capture_cam2"], "removed")
        self.assertEqual(changes["inspection_right"], "removed")
        self.assertEqual(sorted(os.listdir(self.rendered)), ["frame_capture_cam1.json", "inspection_left.json"])

    def test_foreign_files_are_never_removed(self):
        self.render()
        for name, content in (("notes.json", '{"mine": 1}'), ("broken.json", "{"), ("readme.txt", "x")):
            with open(os.path.join(self.rendered, name), "w") as f:
                f.write(content)
        self.render()
        self.assertTrue(all(os.path.exists(os.path.join(self.rendered, n))
                            for n in ("notes.json", "broken.json", "readme.txt")))

    def test_hand_edit_is_overwritten(self):
        self.render()
        path = os.path.join(self.rendered, "inspection_left.json")
        doc = self.load("inspection_left")
        doc["threshold"] = 0.9
        with open(path, "w") as f:
            json.dump(doc, f)
        self.assertEqual(self.render()["inspection_left"], "written")
        self.assertEqual(self.load("inspection_left")["threshold"], 0.5)

    def test_check_writes_nothing(self):
        code, out = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("written inspection_left", out)
        self.assertIn("nothing written", out)
        self.assertFalse(os.path.exists(self.rendered))


class Errors(Site):
    def test_invalid_site_yaml_writes_nothing(self):
        self.render()
        before = {n: open(os.path.join(self.rendered, n)).read() for n in os.listdir(self.rendered)}
        self.data["inspection"]["threshold"] = 0.7
        self.data["plc"]["ip"] = "not-an-ip"
        self.write()
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("plc.ip: must be an IPv4 address", out)
        self.assertIn("nothing written", out)
        self.assertNotIn(DB_PASSWORD, out)
        after = {n: open(os.path.join(self.rendered, n)).read() for n in os.listdir(self.rendered)}
        self.assertEqual(before, after)

    def test_world_readable_site_yaml(self):
        self.write(mode=0o644)
        with self.assertRaises(SiteConfigError):
            rd.render(self.path)
        self.assertFalse(os.path.exists(self.rendered))

    def test_flir_camera_gets_its_block(self):
        cam = self.data["capture"]["cameras"][1]
        cam["driver"] = "flir"
        cam["flir"] = {"interface": "GEV Interface 1", "throughput_bps": 19662239}
        self.write()
        self.render()
        self.assertEqual(self.load("frame_capture_cam2")["camera"]["flir"],
                         {"interface": "GEV Interface 1", "throughput_bps": 19662239})
        self.assertNotIn("flir", self.load("frame_capture_cam1")["camera"])

    def test_expected_cycle_total(self):
        self.data["inspection"]["machine"]["expected_cycle_total"] = 188
        self.write()
        self.render()
        self.assertEqual(self.load("inspection_left")["machine"], {"total_wheel_count": 190, "expected_cycle_total": 188})


class P7Prep(Site):                                                  # D-072, D-073: today's values
    def test_live_wheel_counter_written_by_one_side(self):
        self.render()
        self.assertEqual((self.load("inspection_left")["writes_live_wheel_counter"],
                          self.load("inspection_right")["writes_live_wheel_counter"]), (False, True))
        self.data["inspection"]["live_wheel_counter_side"] = "LEFT"
        self.write()
        self.render()
        self.assertEqual((self.load("inspection_left")["writes_live_wheel_counter"],
                          self.load("inspection_right")["writes_live_wheel_counter"]), (True, False))

    def test_each_side_gets_its_own_tuning(self):                    # P7-prep done when
        self.render()
        left, right = self.load("inspection_left"), self.load("inspection_right")
        self.assertEqual((left["motion"], right["motion"]), ("rtl", "ltr"))
        self.assertEqual(left["bucket_clip_window"], {"xmin": 50, "xmax": 1990, "ymin": 750, "ymax": 1085})
        self.assertEqual(right["bucket_clip_window"], {"xmin": 10, "xmax": 1270, "ymin": 550, "ymax": 780})
        self.assertEqual(right["half_bucket_clip_window"], {"xmin": 100, "xmax": 1850, "ymin": 480, "ymax": 730})
        self.assertEqual(left["tracker"]["clip_confirm"], {"frames": 5, "min_pairs": 3, "max_gap_ms": 300, "max_dist_px": 200})
        self.assertEqual(right["tracker"]["clip_confirm"], {"frames": 5, "min_pairs": 3, "max_gap_ms": 500, "max_dist_px": 500})
        self.assertEqual((left["tracker"]["skidding"]["time_gap_reset_ms"],
                          right["tracker"]["skidding"]["time_gap_reset_ms"]), (500, 1000))
        self.assertEqual(right["tracker"]["wheel_missing"],
                         {"min_frames": 9, "belt_stop_frames": 20,
                          "bbox": {"xmin": 668, "xmax": 1474, "ymin": 508, "ymax": 1278}})
        self.assertEqual(left["wheel_report"], {"daily_time": "17:58"})

    def test_a_right_tuning_change_rewrites_only_right(self):
        self.render()
        self.data["inspection"]["sides"]["RIGHT"]["tracker"] = {"skidding": {"time_gap_reset_ms": 800}}
        self.write()
        self.assertEqual(self.render(), {"frame_capture_cam1": "unchanged", "frame_capture_cam2": "unchanged",
                                         "inspection_left": "unchanged", "inspection_right": "written"})

    def test_capture_health_interval(self):                          # P4d
        self.render()
        self.assertEqual(self.load("frame_capture_cam1")["health_interval_s"], 600)
        self.data["capture"]["health_interval_s"] = 120
        self.write()
        changes = self.render()
        self.assertEqual((changes["frame_capture_cam1"], changes["inspection_left"]), ("written", "unchanged"))
        self.assertEqual(self.load("frame_capture_cam2")["health_interval_s"], 120)


if __name__ == "__main__":
    unittest.main()
