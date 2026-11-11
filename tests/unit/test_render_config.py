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
WORKERS = ["frame_capture_cam1", "frame_capture_cam2", "inspection_left", "inspection_right", "cleanup", "mail",
           "plc", "ui"]
UI_DIR = os.path.join(REPO_ROOT, "ui")
PLC_WORKER = os.path.join(REPO_ROOT, "workers", "plc")
MAIL_WORKER = os.path.join(REPO_ROOT, "workers", "mail")
CLEANUP_SETTINGS = os.path.join(REPO_ROOT, "workers", "cleanup")


class Site(unittest.TestCase):
    """The Tadipatri reference file with the 6g keys switched on and test secrets filled in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        with open(REFERENCE, encoding="utf-8") as f:
            text = f.read()
        # The reference already has the camera ips and light_on_value 1 (D-084).
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
        self.assertEqual(sorted(os.listdir(self.rendered)), sorted(f"{w}.json" for w in WORKERS))
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
            self.assertEqual(set(doc["model"]), {"name", "models_dir"})
            keys = set()

            def collect(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        keys.add(key)
                        collect(value)
            collect(doc)
            # key names, not words: auto_roi.preview_classes_per_image (F57) is a setting, not a class list
            self.assertFalse(keys & {"classes", "label_map", "weights", "detectron_yaml", "version"}, keys)
            text = json.dumps(doc)
            for word in (".pth", "detectron"):
                self.assertNotIn(word, text)

    def test_only_needed_secrets(self):
        self.render()
        for worker in WORKERS:
            text = json.dumps(self.load(worker))
            if worker in ("cleanup", "mail"):
                continue                                             # they mail via Graph (F29, F36: checked below)
            self.assertNotIn(GRAPH_SECRET, text)
            if worker == "ui":
                # the ui shows the disk thresholds: cleanup.disk_percent only (F58); its "mail" is a threshold
                doc = self.load(worker)
                self.assertFalse({"graph", "mail", "transfer"} & set(doc))
                self.assertEqual(set(doc["cleanup"]), {"disk_percent"})
                continue
            for section in ("graph", "mail", "cleanup", "transfer"):
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
        self.data["mail"]["report_dir"] = "data/reports2"            # no rendered file holds report_dir
        self.write()
        self.assertEqual(self.render(), {w: "unchanged" for w in WORKERS})
        self.data["mail"]["project_name"] = "Other"                  # only the mail program reads it (F36)
        self.write()
        self.assertEqual({w for w, s in self.render().items() if s == "written"}, {"mail"})
        self.data["inspection"]["threshold"] = 0.6
        self.write()
        self.assertEqual(self.render(), {"frame_capture_cam1": "unchanged", "frame_capture_cam2": "unchanged",
                                         "inspection_left": "written", "inspection_right": "written",
                                         "cleanup": "unchanged", "mail": "unchanged", "plc": "unchanged",
                                         "ui": "unchanged"})
        self.data["capture"]["cameras"][0]["exposure_us"] = 1800
        self.write()
        self.assertEqual(self.render()["frame_capture_cam1"], "written")

    def test_db_password_change_rewrites_every_file(self):
        self.render()
        self.data["db"]["password"] = "s3cret-new"
        self.write()
        expected = {w: "written" for w in WORKERS}
        expected["cleanup"] = "unchanged"                            # cleanup has no DB login (mail has)
        self.assertEqual(self.render(), expected)

    def test_removed_camera_removes_its_files(self):
        self.render()
        del self.data["capture"]["cameras"][1]
        del self.data["inspection"]["sides"]["RIGHT"]
        self.write()
        changes = self.render()
        self.assertEqual(changes["frame_capture_cam2"], "removed")
        self.assertEqual(changes["inspection_right"], "removed")
        self.assertEqual(sorted(os.listdir(self.rendered)),
                         ["cleanup.json", "frame_capture_cam1.json", "inspection_left.json", "mail.json", "plc.json",
                          "ui.json"])

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
                                         "inspection_left": "unchanged", "inspection_right": "written",
                                         "cleanup": "unchanged", "mail": "unchanged", "plc": "unchanged",
                                         "ui": "unchanged"})

    def test_capture_health_interval(self):                          # P4d
        self.render()
        self.assertEqual(self.load("frame_capture_cam1")["health_interval_s"], 600)
        self.data["capture"]["health_interval_s"] = 120
        self.write()
        changes = self.render()
        self.assertEqual((changes["frame_capture_cam1"], changes["inspection_left"]), ("written", "unchanged"))
        self.assertEqual(self.load("frame_capture_cam2")["health_interval_s"], 120)


class F25(Site):                                                     # D-076: mqtt block and best-image band
    def test_mqtt_block_without_login(self):                         # done when: Tadipatri renders both keys
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            self.assertEqual(self.load(worker)["mqtt"], {"host": "localhost", "port": 1883, "user": None, "password": None})

    def test_mqtt_block_with_login(self):
        self.data["mqtt"].update(user="dpc_broker", password="s3cret-mqtt-Kp3")
        self.write()
        self.render()
        self.assertEqual(self.load("inspection_right")["mqtt"],
                         {"host": "localhost", "port": 1883, "user": "dpc_broker", "password": "s3cret-mqtt-Kp3"})
        self.assertEqual(self.load("frame_capture_cam1")["mqtt"]["user"], "dpc_broker")   # every program (F70)

    def test_best_image_band_default_and_per_side(self):
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            self.assertEqual(self.load(worker)["tracker"]["best_image_band_x"], {"min": 400, "max": 850})
        self.data["inspection"]["sides"]["LEFT"]["tracker"] = {"best_image_band_x": {"max": 900}}
        self.write()
        changes = self.render()
        self.assertEqual((changes["inspection_left"], changes["inspection_right"]), ("written", "unchanged"))
        self.assertEqual(self.load("inspection_left")["tracker"]["best_image_band_x"], {"min": 400, "max": 900})

    def test_bad_band(self):
        self.data["inspection"]["sides"]["RIGHT"]["tracker"] = {"best_image_band_x": {"min": 900, "max": 850}}
        self.write()
        with self.assertRaises(SiteConfigError) as ctx:
            rd.render(self.path)
        self.assertIn("inspection.sides.RIGHT.tracker.best_image_band_x: needs min < max", ctx.exception.problems)

    def test_inspection_program_reads_the_rendered_file(self):
        """The shared inspection program (workers/inspection/settings.py) accepts what is rendered,
        heartbeat included (read-only use of the worker's parser)."""
        sys.path.insert(0, os.path.join(REPO_ROOT, "workers", "inspection"))
        try:
            import settings as inspection_settings
        finally:
            sys.path.pop(0)
        self.data["mqtt"].update(user="dpc_broker", password="s3cret-mqtt-Kp3")
        self.write()
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            loaded = inspection_settings.load_settings(os.path.join(self.rendered, f"{worker}.json"))
            self.assertEqual((loaded.mqtt.host, loaded.mqtt.port, loaded.mqtt.user), ("localhost", 1883, "dpc_broker"))


class CleanupFile(Site):                                             # F29, D-082
    KEYS = {"worker_id", "site", "cameras", "paths", "log", "cleanup", "mail", "graph", "mqtt"}

    def cleanup_settings(self):
        """workers/cleanup/cleanup_settings.py, used read-only to prove the file is what it reads."""
        sys.path.insert(0, CLEANUP_SETTINGS)
        try:
            import cleanup_settings
        finally:
            sys.path.pop(0)
        return cleanup_settings

    def test_tadipatri_renders_cleanup_json_with_the_exact_keys(self):
        self.assertEqual(self.render()["cleanup"], "written")
        path = os.path.join(self.rendered, "cleanup.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = self.load("cleanup")
        self.assertEqual(set(doc) - {"_generated"}, self.KEYS)
        self.assertEqual(doc["worker_id"], "cleanup")
        self.assertEqual(doc["site"], {"id": "tadipatri", "name": self.data["site"]["name"]})
        self.assertEqual(doc["cameras"], [{"id": "CAM1", "side": "LEFT"}, {"id": "CAM2", "side": "RIGHT"}])
        data = os.path.join(self.root, "data")
        self.assertEqual(doc["paths"], {
            "root": self.root, "logs": os.path.join(self.root, "logs"), "data": data,
            "raw_images": [f"{data}/frames/CAM1/RAW_IMAGES", f"{data}/frames/CAM2/RAW_IMAGES"],
            "inf": [f"{data}/frames/CAM1/INF", f"{data}/frames/CAM2/INF"],
            "repeated": [f"{data}/images/REPEATED/LEFT", f"{data}/images/REPEATED/RIGHT"],
            "plate": [f"{data}/images/PLATE/LEFT", f"{data}/images/PLATE/RIGHT"],
            "defective": [f"{data}/images/DEFECTIVE/LEFT", f"{data}/images/DEFECTIVE/RIGHT"],
            "defect_raw": [f"{data}/images/RAW/LEFT", f"{data}/images/RAW/RIGHT"],
            "magnet": [f"{data}/images/MAGNET/LEFT", f"{data}/images/MAGNET/RIGHT"],
            "bolt_data": [f"{data}/bolt_data/LEFT", f"{data}/bolt_data/RIGHT"]})
        self.assertEqual(doc["log"], {"backup_count": 5})
        self.assertEqual(doc["cleanup"], {
            "raw_image_hours": 20, "interval_seconds": 10, "repeated": {"days": 20, "items": 200},
            "plate_days": 10, "defective": {"days": 10, "items": 200},
            "disk_percent": {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95},
            "disk_report_time": "09:00"})
        # disk_report_to is [] at Tadipatri -> the recipients; cc empty on purpose
        self.assertEqual(doc["mail"], {"sender_address": "insightzz.reports@insightzz.com",
                                       "to": ["redacted@example.com"], "cc": []})
        self.assertEqual(doc["graph"], {"tenant_id": "t", "client_id": "c", "client_secret": GRAPH_SECRET})

    def test_cleanup_program_loads_it_with_zero_warnings(self):     # F29 done when
        self.render()
        settings = self.cleanup_settings().load_settings(os.path.join(self.rendered, "cleanup.json"))
        self.assertEqual(settings.warnings, [])
        self.assertTrue(settings.mail.enabled)
        self.assertEqual((settings.report_hour, settings.report_minute), (9, 0))
        self.assertEqual(settings.raw_image_hours, 20)
        self.assertEqual(settings.folders["bolt_data"][1], os.path.join(self.root, "data", "bolt_data", "RIGHT"))

    def test_disk_report_to_and_time(self):
        self.data["mail"]["disk_report_to"] = ["ops@example.com"]
        self.data["cleanup"]["disk_report_time"] = "07:30"
        self.write()
        self.render()
        doc = self.load("cleanup")
        self.assertEqual(doc["mail"]["to"], ["ops@example.com"])
        self.assertEqual(doc["cleanup"]["disk_report_time"], "07:30")
        settings = self.cleanup_settings().load_settings(os.path.join(self.rendered, "cleanup.json"))
        self.assertEqual((settings.report_hour, settings.report_minute, settings.warnings), (7, 30, []))

    def test_smtp_site_gets_no_graph(self):                          # D-082 item 7
        self.data["mail"]["sender"] = "smtp"
        self.data["mail"]["smtp"] = {"password": "s3cret-smtp-Wq1"}
        self.write()
        self.render()
        doc = self.load("cleanup")
        self.assertIsNone(doc["graph"])
        self.assertNotIn("s3cret-smtp", json.dumps(doc))
        settings = self.cleanup_settings().load_settings(os.path.join(self.rendered, "cleanup.json"))
        self.assertFalse(settings.mail.enabled)

    def test_one_camera_site(self):
        del self.data["capture"]["cameras"][1]
        del self.data["inspection"]["sides"]["RIGHT"]
        self.data["inspection"].pop("live_wheel_counter_side", None)
        self.write()
        self.render()
        doc = self.load("cleanup")
        self.assertEqual(doc["cameras"], [{"id": "CAM1", "side": "LEFT"}])
        self.assertTrue(all(len(v) == 1 for k, v in doc["paths"].items() if isinstance(v, list)))

    def test_only_cleanup_changes_when_cleanup_values_change(self):
        self.render()
        self.data["cleanup"]["plate_days"] = 7
        self.write()
        changes = self.render()
        self.assertEqual(changes["cleanup"], "written")
        self.assertEqual({w for w, s in changes.items() if s == "written"}, {"cleanup"})


class MailFile(Site):                                                # F36, D-083
    KEYS = {"worker_id", "site", "paths", "log", "db", "mail", "graph", "mqtt"}
    MAIL_KEYS = {"sender", "sender_address", "project_name", "to", "cc", "smtp", "report_url",
                 "max_alert_age_hours", "flood"}

    def mail_worker(self):
        """workers/mail/mail_worker.py, used read-only to prove the file is what it reads."""
        sys.path.insert(0, MAIL_WORKER)
        try:
            import mail_worker
        finally:
            sys.path.pop(0)
        return mail_worker

    def load_worker_settings(self):
        with open(os.path.join(self.rendered, "mail.json"), encoding="utf-8") as f:
            return self.mail_worker().build_settings(json.load(f))

    def with_handoff_values(self):
        """The Tadipatri reference with the F36 values the handoff adds to it."""
        self.data["mail"]["report_url"] = "http://192.168.1.10:8000/defects"
        self.data["mail"]["flood"] = {"max_per_window": 8, "window_hours": 5, "internal_to": ["support@example.com"]}
        self.write()

    def test_tadipatri_renders_mail_json(self):                      # F36 done when
        self.with_handoff_values()
        self.assertEqual(self.render()["mail"], "written")
        path = os.path.join(self.rendered, "mail.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = self.load("mail")
        self.assertEqual(set(doc) - {"_generated"}, self.KEYS)
        self.assertEqual(set(doc["mail"]), self.MAIL_KEYS)
        self.assertEqual(doc["worker_id"], "mail")
        self.assertEqual(doc["site"], {"id": "tadipatri", "name": self.data["site"]["name"]})
        self.assertEqual(doc["paths"], {"root": self.root, "logs": os.path.join(self.root, "logs"),
                                        "state": os.path.join(self.root, "data", "state")})
        self.assertEqual(doc["db"], {"host": "localhost", "port": 3306, "name": self.data["db"]["name"],
                                     "user": self.data["db"]["user"], "password": DB_PASSWORD})
        mail = doc["mail"]
        self.assertEqual((mail["sender"], mail["sender_address"], mail["project_name"]),
                         ("graph", "insightzz.reports@insightzz.com", "Tadipatri_dpc"))
        self.assertEqual((mail["to"], mail["cc"]), (["redacted@example.com"], ["redacted@example.com"]))
        self.assertEqual((mail["smtp"], mail["report_url"], mail["max_alert_age_hours"]),
                         (None, "http://192.168.1.10:8000/defects", 24.0))
        self.assertEqual(mail["flood"], {"max_per_window": 8, "window_hours": 5.0,
                                         "internal_to": ["support@example.com"]})
        self.assertEqual(doc["graph"], {"tenant_id": "t", "client_id": "c", "client_secret": GRAPH_SECRET})

    def test_mail_worker_loads_it_with_no_warning(self):             # F36 done when
        self.with_handoff_values()
        self.render()
        s = self.load_worker_settings()
        self.assertEqual(s.warnings, [])
        self.assertEqual((s.sender, s.report_url, s.max_alert_age_hours),
                         ("graph", "http://192.168.1.10:8000/defects", 24.0))
        self.assertEqual((s.flood.max_per_window, s.flood.window_hours), (8, 5.0))
        self.assertEqual(s.graph_client_secret, GRAPH_SECRET)

    def test_reference_as_it_is_today_also_loads_without_warning(self):
        """Without report_url and flood (written as null = off), still no warning."""
        self.render()
        doc = self.load("mail")
        self.assertEqual((doc["mail"]["report_url"], doc["mail"]["flood"]), (None, None))
        s = self.load_worker_settings()
        self.assertEqual((s.warnings, s.report_url, s.flood), ([], None, None))

    def test_max_age_off(self):
        self.data["mail"]["max_alert_age_hours"] = None
        self.write()
        self.render()
        self.assertIsNone(self.load("mail")["mail"]["max_alert_age_hours"])
        s = self.load_worker_settings()
        self.assertEqual((s.max_alert_age_hours, s.warnings), (None, []))

    def test_smtp_site(self):
        self.data["mail"]["sender"] = "smtp"
        self.data["mail"]["smtp"] = {"host": "smtp.example.com", "port": 587, "password": "s3cret-smtp-Mn2"}
        self.write()
        self.render()
        doc = self.load("mail")
        self.assertIsNone(doc["graph"])
        self.assertEqual(doc["mail"]["smtp"], {"host": "smtp.example.com", "port": 587, "password": "s3cret-smtp-Mn2"})
        self.assertNotIn(GRAPH_SECRET, json.dumps(doc))
        s = self.load_worker_settings()
        self.assertEqual((s.sender, s.smtp_host, s.smtp_password, s.warnings),
                         ("smtp", "smtp.example.com", "s3cret-smtp-Mn2", []))

    def test_only_mail_changes_when_a_mail_value_changes(self):
        self.render()
        self.data["mail"]["max_alert_age_hours"] = 12
        self.write()
        changes = self.render()
        self.assertEqual({w for w, s in changes.items() if s == "written"}, {"mail"})


class PlcFile(Site):                                                 # F39, D-084
    KEYS = {"worker_id", "paths", "log", "plc", "mqtt", "db"}
    PLC_KEYS = {"type", "ip", "port", "timeout_s", "light_register", "light_on_value", "cleaning_register",
                "conveyor_status_register", "cleaning_pulse_seconds", "light_check_seconds",
                "status_interval_seconds", "cleaning_cooldown_seconds", "auto_cleaning"}

    def plc_settings(self):
        """workers/plc/plc_settings.py, used read-only to prove the file is what it reads."""
        sys.path.insert(0, PLC_WORKER)
        try:
            import plc_settings
        finally:
            sys.path.pop(0)
        return plc_settings

    def load_worker_settings(self):
        with open(os.path.join(self.rendered, "plc.json"), encoding="utf-8") as f:
            return self.plc_settings().build_settings(json.load(f))

    def test_tadipatri_renders_plc_json(self):                       # F39 done when
        self.assertEqual(self.render()["plc"], "written")
        path = os.path.join(self.rendered, "plc.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = self.load("plc")
        self.assertEqual(set(doc) - {"_generated"}, self.KEYS)
        self.assertEqual(set(doc["plc"]), self.PLC_KEYS)                # no legacy_db_numbers
        self.assertEqual(doc["worker_id"], "plc")
        self.assertEqual(doc["paths"], {"root": self.root, "logs": os.path.join(self.root, "logs"),
                                        "state": os.path.join(self.root, "data", "state")})
        self.assertEqual(doc["plc"], {
            "type": "modbus_tcp", "ip": "192.168.1.5", "port": 502, "timeout_s": 3.0,
            "light_register": 4109, "light_on_value": 1,                # 1 = ON at Tadipatri (D-084)
            "cleaning_register": 4106, "conveyor_status_register": 4098, "cleaning_pulse_seconds": 10.0,
            "light_check_seconds": 60.0, "status_interval_seconds": 10.0,
            "cleaning_cooldown_seconds": 600.0, "auto_cleaning": False})
        self.assertEqual(doc["mqtt"], {"host": "localhost", "port": 1883, "user": None, "password": None})
        self.assertEqual(doc["db"], {"host": "localhost", "port": 3306, "name": self.data["db"]["name"],
                                     "user": self.data["db"]["user"], "password": DB_PASSWORD})
        self.assertNotIn(GRAPH_SECRET, json.dumps(doc))

    def test_plc_worker_loads_it_with_no_warning(self):              # F39 done when
        self.render()
        s = self.load_worker_settings()
        self.assertEqual(s.warnings, [])
        self.assertEqual((s.ip, s.light_register, s.light_on_value, s.cleaning_register, s.auto_cleaning),
                         ("192.168.1.5", 4109, 1, 4106, False))
        self.assertEqual((s.light_check_seconds, s.status_interval_seconds, s.cleaning_cooldown_seconds),
                         (60.0, 10.0, 600.0))
        self.assertIsNotNone(s.mqtt)
        self.assertEqual(s.db.password, DB_PASSWORD)

    def test_new_keys_set_in_site_yaml(self):
        self.data["plc"].update(light_check_seconds=30, status_interval_seconds=5,
                                cleaning_cooldown_seconds=0, auto_cleaning=True)
        self.write()
        changes = self.render()
        self.assertEqual({w for w, s in changes.items() if s == "written"}, set(WORKERS))
        self.render()
        self.data["plc"]["light_check_seconds"] = 45
        self.write()
        self.assertEqual({w for w, s in self.render().items() if s == "written"}, {"plc"})
        s = self.load_worker_settings()
        self.assertEqual((s.light_check_seconds, s.status_interval_seconds, s.cleaning_cooldown_seconds,
                          s.auto_cleaning, s.warnings), (45.0, 5.0, 0.0, True, []))

    def test_mqtt_login_reaches_the_plc(self):
        self.data["mqtt"].update(user="dpc_broker", password="s3cret-mqtt-Pl4")
        self.write()
        self.render()
        s = self.load_worker_settings()
        self.assertEqual((s.mqtt.user, s.mqtt.password), ("dpc_broker", "s3cret-mqtt-Pl4"))


class ChecksRender(Site):                                            # F41, D-085
    def test_default_all_true_on_both_sides(self):
        self.data["inspection"].pop("checks", None)
        self.write()
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            self.assertEqual(self.load(worker)["checks"], {"bucket_clip": True, "skidding": True,
                                                           "wheel_missing": True, "cycle_restart": True})

    def test_tadipatri_wheel_missing_off_on_both_sides(self):        # F41 done when
        self.data["inspection"]["checks"] = {"wheel_missing": False}   # the value proposed for the reference
        self.write()
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            self.assertEqual(self.load(worker)["checks"], {"bucket_clip": True, "skidding": True,
                                                           "wheel_missing": False, "cycle_restart": True})

    def test_per_side_value_wins_and_only_that_side_changes(self):
        self.data["inspection"]["checks"] = {"wheel_missing": False}
        self.write()
        self.render()
        self.data["inspection"]["sides"]["RIGHT"]["checks"] = {"wheel_missing": True}
        self.write()
        changes = self.render()
        self.assertEqual({w for w, s in changes.items() if s == "written"}, {"inspection_right"})
        self.assertFalse(self.load("inspection_left")["checks"]["wheel_missing"])
        self.assertTrue(self.load("inspection_right")["checks"]["wheel_missing"])

    def test_inspection_program_still_reads_the_file(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "workers", "inspection"))
        try:
            import settings as inspection_settings
        finally:
            sys.path.pop(0)
        self.data["inspection"]["checks"] = {"wheel_missing": False}
        self.write()
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            inspection_settings.load_settings(os.path.join(self.rendered, f"{worker}.json"))


class PerSideModelRender(Site):                                      # F46, D-086
    def test_per_side_model_renders_into_only_that_side(self):     # F46 done when
        self.render()
        self.data["inspection"]["sides"]["RIGHT"]["model"] = {"name": "dpc_maskrcnn_right"}
        self.write()
        changes = self.render()
        self.assertEqual({w for w, s in changes.items() if s == "written"}, {"inspection_right"})
        models = os.path.join(self.root, "data", "models")
        self.assertEqual(self.load("inspection_left")["model"],
                         {"name": "dpc_maskrcnn", "models_dir": os.path.join(models, "dpc_maskrcnn")})
        self.assertEqual(self.load("inspection_right")["model"],
                         {"name": "dpc_maskrcnn_right", "models_dir": os.path.join(models, "dpc_maskrcnn_right")})


class AutoRoiRender(Site):                                           # F53, D-093
    def inspection_settings(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "workers", "inspection"))
        try:
            import settings as inspection_settings
        finally:
            sys.path.pop(0)
        return inspection_settings

    def test_every_key_is_rendered_and_learn_images_is_gone(self):
        self.render()
        roi = self.load("inspection_left")["auto_roi"]
        self.assertEqual(set(roi), {"mode"} | set(self.inspection_settings().AUTO_ROI_KEYS))
        self.assertNotIn("learn_images", roi)                        # the reference still has it: dropped
        self.assertEqual((roi["mode"], roi["margin_px"], roi["bottom_percentile"], roi["preview_classes_per_image"]),
                         ("observe", 100, 99.0, 2))                  # F57
        self.assertEqual(roi, self.load("inspection_right")["auto_roi"])   # site level, both sides

    def test_inspection_starts_without_the_defaults_warning(self):   # F53 done when
        self.data["inspection"]["auto_roi"] = {"mode": "filter", "margin_px": 120}
        self.write()
        self.render()
        for worker in ("inspection_left", "inspection_right"):
            loaded = self.inspection_settings().load_settings(os.path.join(self.rendered, f"{worker}.json"))
            self.assertEqual(loaded.auto_roi.defaulted, ())          # nothing taken from the program's defaults
            self.assertEqual((loaded.auto_roi.mode, loaded.auto_roi.margin_px), ("filter", 120))

    def test_a_roi_value_change_rewrites_both_inspection_files_only(self):
        self.render()
        self.data["inspection"]["auto_roi"] = {"mode": "observe", "drift_ratio": 0.3}
        self.write()
        self.assertEqual({w for w, s in self.render().items() if s == "written"},
                         {"inspection_left", "inspection_right"})


class UiFile(Site):                                                  # F58, D-095, D-096
    KEYS = {"worker_id", "site", "paths", "log", "ui", "db", "mqtt", "cameras", "sides", "cleanup", "machine"}

    def ui_loader(self):
        """ui/dpc_ui/ui_config.py, used read-only to prove the file is what the UI reads."""
        sys.path.insert(0, UI_DIR)
        try:
            from dpc_ui import ui_config
        finally:
            sys.path.pop(0)
        return ui_config

    def test_tadipatri_renders_ui_json_with_the_exact_key_set(self):   # F58 done when
        self.assertEqual(self.render()["ui"], "written")
        path = os.path.join(self.rendered, "ui.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = self.load("ui")
        self.assertEqual(set(doc) - {"_generated"}, self.KEYS)
        r, data = self.root, os.path.join(self.root, "data")
        self.assertEqual(doc["paths"], {
            "root": r, "code": f"{r}/code", "config": f"{r}/config", "site_yaml": self.path,
            "workers_file": f"{r}/config/workers.yaml", "logs": f"{r}/logs", "run": f"{r}/run", "data": data,
            "frames": f"{data}/frames", "state": f"{data}/state/ui"})
        self.assertEqual(doc["ui"], {"port": 8080, "refresh_seconds": 2.0, "event_keep_days": 90,
                                     "event_keep_rows": 50000})
        self.assertEqual(doc["log"], {"backup_count": 5})
        self.assertEqual(doc["db"], {"host": "localhost", "port": 3306, "name": self.data["db"]["name"],
                                     "user": self.data["db"]["user"], "password": DB_PASSWORD})
        self.assertEqual(doc["mqtt"], {"host": "localhost", "port": 1883, "user": None, "password": None,
                                       "topic_prefix": "dpc"})
        self.assertEqual(doc["cameras"], [
            {"id": "CAM1", "side": "LEFT", "serial": "24824427", "ip": "192.168.1.102"},
            {"id": "CAM2", "side": "RIGHT", "serial": "24874775", "ip": "192.168.1.103"}])
        self.assertEqual(doc["sides"], [
            {"side": "LEFT", "camera_id": "CAM1", "inspection_worker": "inspection_left"},
            {"side": "RIGHT", "camera_id": "CAM2", "inspection_worker": "inspection_right"}])
        self.assertEqual(doc["cleanup"], {"disk_percent": {"delete": 75, "raw_delete": 85, "mail": 90,
                                                           "inference_delete": 95}})
        self.assertEqual(doc["machine"], {"total_wheel_count": 190})
        self.assertNotIn(GRAPH_SECRET, json.dumps(doc))

    def test_ui_loader_reads_it_with_no_missing_key(self):           # F58 done when
        self.render()
        with self.assertNoLogs("dpc.ui.config", level="WARNING"):
            cfg = self.ui_loader().load(os.path.join(self.rendered, "ui.json"))
        self.assertEqual(cfg["_missing"], [])
        self.assertEqual(cfg.paths["state"], os.path.join(self.root, "data", "state", "ui"))
        self.assertEqual(cfg.camera_for_side("RIGHT")["serial"], "24874775")
        self.assertEqual(cfg.secret_values(), [DB_PASSWORD])

    def test_ui_block_values(self):
        self.data["ui"] = {"port": 8090, "refresh_seconds": 1, "event_keep_days": 30, "event_keep_rows": 1000,
                           "log": {"backup_count": 7}}
        self.write()
        self.render()
        doc = self.load("ui")
        self.assertEqual(doc["ui"], {"port": 8090, "refresh_seconds": 1.0, "event_keep_days": 30,
                                     "event_keep_rows": 1000})
        self.assertEqual(doc["log"], {"backup_count": 7})

    def test_a_ui_value_change_rewrites_only_ui(self):
        self.render()
        self.data["ui"] = {"port": 8081}
        self.write()
        self.assertEqual({w for w, s in self.render().items() if s == "written"}, {"ui"})

    def test_one_camera_site(self):
        del self.data["capture"]["cameras"][1]
        del self.data["inspection"]["sides"]["RIGHT"]
        self.data["inspection"].pop("live_wheel_counter_side", None)
        self.write()
        self.render()
        doc = self.load("ui")
        self.assertEqual([c["id"] for c in doc["cameras"]], ["CAM1"])
        self.assertEqual(doc["sides"], [{"side": "LEFT", "camera_id": "CAM1", "inspection_worker": "inspection_left"}])


class MqttEverywhereF70(Site):                                       # F70, D-099
    NO_LOGIN = {"host": "localhost", "port": 1883, "user": None, "password": None}

    def test_every_rendered_worker_file_has_the_mqtt_block(self):   # F70 done when
        self.render()
        for worker in WORKERS:
            mqtt = self.load(worker)["mqtt"]
            self.assertEqual({k: mqtt[k] for k in ("host", "port", "user", "password")}, self.NO_LOGIN, worker)

    def test_login_reaches_capture_cleanup_and_mail(self):
        self.data["mqtt"].update(user="dpc_broker", password="s3cret-mqtt-F70")
        self.write()
        self.render()
        login = {"host": "localhost", "port": 1883, "user": "dpc_broker", "password": "s3cret-mqtt-F70"}
        for worker in ("frame_capture_cam1", "frame_capture_cam2", "cleanup", "mail"):
            self.assertEqual(self.load(worker)["mqtt"], login, worker)

    def test_the_programs_read_it(self):
        """Read-only use of each program's own settings reader: the heartbeat finds its login."""
        self.data["mqtt"].update(user="dpc_broker", password="s3cret-mqtt-F70")
        self.write()
        self.render()
        sys.path.insert(0, CLEANUP_SETTINGS)
        try:
            import cleanup_settings
        finally:
            sys.path.pop(0)
        cleanup = cleanup_settings.load_settings(os.path.join(self.rendered, "cleanup.json"))
        self.assertEqual((cleanup.mqtt["user"], cleanup.warnings), ("dpc_broker", []))
        sys.path.insert(0, MAIL_WORKER)
        try:
            import mail_worker
        finally:
            sys.path.pop(0)
        with open(os.path.join(self.rendered, "mail.json"), encoding="utf-8") as f:
            mail = mail_worker.build_settings(json.load(f))
        self.assertEqual((mail.mqtt["user"], mail.warnings), ("dpc_broker", []))

    def test_a_broker_change_rewrites_every_file(self):
        self.render()
        self.data["mqtt"]["port"] = 1884
        self.write()
        self.assertEqual({w for w, s in self.render().items() if s == "written"}, set(WORKERS))


class TransferFile(Site):                                            # F81, D-104, D-105
    TRANSFER = {"drive": {"site": "contoso.sharepoint.com:/sites/DPC"},
                "upload": {"chunk_mb": 500, "staging_max_gb": 20, "max_mbps": 20, "plate_images": 100}}

    def transfer_settings(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "workers", "transfer"))
        try:
            import transfer_settings
        finally:
            sys.path.pop(0)
        return transfer_settings

    def with_transfer(self, **changes):
        self.data["transfer"] = dict(self.TRANSFER, **changes)
        self.write()

    def test_not_rendered_without_a_transfer_section(self):
        self.data.pop("transfer", None)
        self.write()
        self.assertNotIn("transfer", self.render())
        self.with_transfer()
        self.assertEqual(self.render()["transfer"], "written")
        self.data.pop("transfer")
        self.write()
        self.assertEqual(self.render()["transfer"], "removed")
        self.assertFalse(os.path.exists(os.path.join(self.rendered, "transfer.json")))

    def test_shape(self):                                            # F81 done when
        self.with_transfer()
        self.render()
        path = os.path.join(self.rendered, "transfer.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = self.load("transfer")
        self.assertEqual(set(doc) - {"_generated"}, {"worker_id", "site", "log", "paths", "cameras", "sides",
                                                     "models_in_use", "machine", "graph", "mqtt", "transfer"})
        r, data = self.root, os.path.join(self.root, "data")
        self.assertEqual(doc["paths"], {"root": r, "logs": f"{r}/logs", "run": f"{r}/run",
                                        "state": f"{data}/state/transfer", "staging": f"{data}/transfer/staging",
                                        "downloads": f"{data}/transfer/downloads", "models": f"{data}/models"})
        self.assertEqual(doc["cameras"][1], {"id": "CAM2", "side": "RIGHT", "serial": "24874775", "fps": 4.0,
                                             "exposure_us": 2000, "rotate": 0,
                                             "raw_images": f"{data}/frames/CAM2/RAW_IMAGES"})
        self.assertEqual(doc["sides"][0], {"side": "LEFT", "camera_id": "CAM1", "inspection_worker": "inspection_left",
                                           "plate": f"{data}/images/PLATE/LEFT"})
        self.assertEqual(doc["models_in_use"], {"dpc_maskrcnn": ["LEFT", "RIGHT"]})
        self.assertEqual(doc["graph"]["client_secret"], GRAPH_SECRET)
        self.assertEqual(doc["mqtt"], {"host": "localhost", "port": 1883, "user": None, "password": None})
        self.assertEqual(doc["transfer"], {
            "drive": {"site": "contoso.sharepoint.com:/sites/DPC"}, "models_folder": "DPC/models/tadipatri",
            "upload": {"chunk_mb": 500, "remote_folder": "DPC/data/tadipatri", "staging_max_gb": 20.0,
                       "max_mbps": 20.0, "plate_images": 100}})

    def test_transfer_starts_without_warnings(self):                 # F81 done when
        self.with_transfer()
        self.render()
        ts = self.transfer_settings()
        settings = ts.build_settings(ts.read_raw_config(os.path.join(self.rendered, "transfer.json")))
        self.assertEqual(settings.warnings, [])
        self.assertEqual((settings.models_folder, settings.upload.remote_folder, settings.drive),
                         ("DPC/models/tadipatri", "DPC/data/tadipatri", {"site": "contoso.sharepoint.com:/sites/DPC"}))
        self.assertEqual([s.side for s in settings.sides], ["LEFT", "RIGHT"])

    def test_download_only_and_unlimited(self):
        self.with_transfer(upload=None)
        self.render()
        self.assertIsNone(self.load("transfer")["transfer"]["upload"])
        self.with_transfer(upload={"max_mbps": None})
        self.render()
        self.assertIsNone(self.load("transfer")["transfer"]["upload"]["max_mbps"])
        ts = self.transfer_settings()
        settings = ts.build_settings(ts.read_raw_config(os.path.join(self.rendered, "transfer.json")))
        self.assertEqual((settings.upload.max_mbps, settings.warnings), (None, []))

    def test_per_side_models_in_use(self):
        self.with_transfer()
        self.data["inspection"]["sides"]["RIGHT"]["model"] = {"name": "dpc_maskrcnn_right"}
        self.write()
        self.render()
        self.assertEqual(self.load("transfer")["models_in_use"], {"dpc_maskrcnn": ["LEFT"], "dpc_maskrcnn_right": ["RIGHT"]})


class NoMailSection(Site):                                           # S2: mail is optional (D-103 step 8)
    def test_no_mail_json_and_cleanup_disk_mail_off(self):
        self.render()
        self.data.pop("mail")
        self.write()
        changes = self.render()
        self.assertEqual(changes["mail"], "removed")
        self.assertFalse(os.path.exists(os.path.join(self.rendered, "mail.json")))
        doc = self.load("cleanup")
        self.assertIsNone(doc["mail"])
        self.assertIsNone(doc["graph"])
        sys.path.insert(0, CLEANUP_SETTINGS)
        try:
            import cleanup_settings
        finally:
            sys.path.pop(0)
        settings = cleanup_settings.load_settings(os.path.join(self.rendered, "cleanup.json"))
        self.assertFalse(settings.mail.enabled)
        self.assertEqual(settings.warnings, ["cleanup.json: 'graph' or 'mail' missing: disk mail is OFF"])


if __name__ == "__main__":
    unittest.main()
