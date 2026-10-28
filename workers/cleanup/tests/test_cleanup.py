"""Tests for the cleanup worker (STATUS P9). Fake folder tree in a temp dir, fake disk
percentages, fake clock and a fake mailer: nothing outside the temp dir is touched and no
mail or network call is made.

Run from the repo root:
    python3 -m unittest discover -s workers/cleanup/tests -t . -v
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
sys.path.insert(0, WORKER_DIR)

import cleanup as C                     # noqa: E402
import cleanup_rules as R               # noqa: E402
import cleanup_settings as S            # noqa: E402

NOW = datetime.datetime(2026, 10, 28, 12, 0, 0)
FAKE_SECRET = "s3cr3t-value-for-test"
DiskUsage = namedtuple("DiskUsage", "total used free")

log = logging.getLogger("cleanup-test")
log.addHandler(logging.NullHandler())
log.propagate = False


def frame_name(when):
    return when.strftime(R.FRAME_TIME_FORMAT) + ".jpg"


def touch(path, size=10, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def full_json(root, **cleanup_overrides):
    cams = [{"id": "CAM1", "side": "LEFT"}, {"id": "CAM2", "side": "RIGHT"}]
    cleanup = {"raw_image_hours": 20, "interval_seconds": 10, "repeated": {"days": 20, "items": 3},
               "plate_days": 10, "defective": {"days": 10, "items": 2},
               "disk_percent": {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95},
               "disk_report_time": "09:00"}
    cleanup.update(cleanup_overrides)
    return {
        "_generated": {"from": "site.yaml", "sha256": "0" * 64, "at": "2026-10-28T10:00:00", "renderer": 1},
        "worker_id": "cleanup",
        "site": {"id": "tadipatri", "name": "Tadipatri DPC"},
        "cameras": cams,
        "paths": dict({"root": root, "logs": os.path.join(root, "logs"), "data": os.path.join(root, "data")},
                      **S.default_folders(root, cams)),
        "log": {"backup_count": 5},
        "cleanup": cleanup,
        "mail": {"sender_address": "sender@example.com", "to": ["to@example.com"], "cc": []},
        "graph": {"tenant_id": "tenant", "client_id": "client", "client_secret": FAKE_SECRET},
    }


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append(subject)
        return True


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dpc_cleanup_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.data = os.path.join(self.root, "data")
        for folders in S.default_folders(self.root, S.DEFAULT_CAMERAS).values():
            for folder in folders:
                os.makedirs(folder)
        for cam in ("CAM1", "CAM2"):
            for name in ("live", "UI", "_tmp"):
                os.makedirs(os.path.join(self.data, "frames", cam, name))

    def write_json(self, data):
        folder = os.path.join(self.root, "config", "rendered")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "cleanup.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def settings(self, **overrides):
        return S.load_settings(self.write_json(full_json(self.root, **overrides)))

    def cleaner(self, settings, percent):
        used = int(percent * 10)
        disk = lambda path: (self.measured.append(path), DiskUsage(1000, used, 1000 - used))[1]
        self.measured = []
        self.mailer = FakeMailer()
        return C.Cleaner(settings, self.mailer, disk_usage=disk, clock=lambda: self.now, log=log)

    now = NOW

    def folder(self, group, index=0):
        return S.default_folders(self.root, S.DEFAULT_CAMERAS)[group][index]


# ------------------------------------------------------------------ settings
class SettingsTests(TempRoot):
    def test_full_json_has_no_warnings(self):
        s = self.settings()
        self.assertEqual(s.warnings, [])
        self.assertEqual(s.raw_image_hours, 20)
        self.assertEqual((s.report_hour, s.report_minute), (9, 0))
        self.assertTrue(s.mail.enabled)
        self.assertEqual(s.folders["bolt_data"][1], os.path.join(self.data, "bolt_data", "RIGHT"))

    def test_missing_keys_use_tadipatri_defaults_with_one_warning_each(self):
        data = full_json(self.root)
        del data["cleanup"]["raw_image_hours"], data["cleanup"]["disk_percent"]["mail"], data["paths"]["magnet"]
        s = S.load_settings(self.write_json(data))
        self.assertEqual(s.raw_image_hours, 20)
        self.assertEqual(s.disk_percent.mail, 90)
        self.assertEqual(s.folders["magnet"][0], os.path.join(self.data, "images", "MAGNET", "LEFT"))
        self.assertEqual(len(s.warnings), 3)
        self.assertTrue(any("'cleanup.raw_image_hours'" in w for w in s.warnings))

    def test_no_env_means_code_location_root_and_mail_off(self):
        s = S.load_settings(None)
        self.assertEqual(s.root, S.root_from_code_location())
        self.assertFalse(s.mail.enabled)
        self.assertEqual(s.disk_percent.inference_delete, 95)

    def test_wrong_type_names_the_key(self):
        data = full_json(self.root)
        data["cleanup"]["plate_days"] = "ten"
        with self.assertRaisesRegex(S.ConfigError, "cleanup.plate_days"):
            S.load_settings(self.write_json(data))

    def test_folder_outside_data_is_refused(self):
        data = full_json(self.root)
        data["paths"]["plate"] = ["/tmp"]
        with self.assertRaisesRegex(S.ConfigError, "not inside"):
            S.load_settings(self.write_json(data))

    def test_capture_folders_are_refused(self):
        for bad in ("live", "UI", "_tmp"):
            data = full_json(self.root)
            data["paths"]["inf"] = [os.path.join(self.data, "frames", "CAM1", bad)]
            with self.assertRaisesRegex(S.ConfigError, "capture folder"):
                S.load_settings(self.write_json(data))

    def test_secret_never_in_warnings(self):
        data = full_json(self.root)
        data["mail"]["to"] = []
        s = S.load_settings(self.write_json(data))
        self.assertFalse(s.mail.enabled)
        self.assertNotIn(FAKE_SECRET, " ".join(s.warnings))


# ------------------------------------------------------------------ rules through a pass
class DeletionTests(TempRoot):
    def frames(self, folder, hours_ago_list):
        return [touch(os.path.join(folder, frame_name(NOW - datetime.timedelta(hours=h))))
                for h in hours_ago_list]

    def test_disk_measured_on_data(self):
        cleaner = self.cleaner(self.settings(), 50)
        cleaner.run_once()
        self.assertEqual(self.measured, [os.path.join(self.root, "data")])

    def test_below_delete_threshold_nothing_is_deleted(self):
        old = self.frames(self.folder("raw_images"), [100]) + self.frames(self.folder("plate"), [24 * 30])
        self.cleaner(self.settings(), 75).run_once()          # strict ">" as before
        self.assertTrue(all(os.path.exists(p) for p in old))

    def test_raw_rule_only_above_raw_delete(self):
        raw = self.folder("raw_images", 1)
        bolt = self.folder("bolt_data", 0)
        old_raw, young_raw = self.frames(raw, [21, 19])
        old_bolt, = self.frames(bolt, [30])
        odd = touch(os.path.join(raw, "not_a_frame.jpg"))
        other = touch(os.path.join(raw, "notes.txt"))
        self.cleaner(self.settings(), 80).run_once()
        self.assertTrue(os.path.exists(old_raw))
        self.cleaner(self.settings(), 86).run_once()
        self.assertFalse(os.path.exists(old_raw))
        self.assertFalse(os.path.exists(old_bolt))
        for kept in (young_raw, odd, other):
            self.assertTrue(os.path.exists(kept))

    def test_inf_rule_only_above_inference_delete(self):
        inf = self.folder("inf")
        files = self.frames(inf, [0, 1])
        live = touch(os.path.join(self.data, "frames", "CAM1", "live", "IMG_1.jpg"))
        ui = touch(os.path.join(self.data, "frames", "CAM1", "UI", "IMG_1.jpg"))
        self.cleaner(self.settings(), 95).run_once()
        self.assertTrue(all(os.path.exists(p) for p in files))
        self.cleaner(self.settings(), 96).run_once()
        self.assertFalse(any(os.path.exists(p) for p in files))
        self.assertTrue(os.path.exists(live) and os.path.exists(ui))

    def test_plate_and_magnet_older_than_plate_days(self):
        plate = self.folder("plate")
        old, young = self.frames(plate, [24 * 11, 24 * 9])
        magnet_day = os.path.join(self.folder("magnet", 1), "20261015")
        old_mag = touch(os.path.join(magnet_day, "RIGHT12_" + frame_name(NOW - datetime.timedelta(days=13))))
        young_mag = touch(os.path.join(magnet_day, "RIGHT13_" + frame_name(NOW - datetime.timedelta(days=2))))
        self.cleaner(self.settings(), 76).run_once()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(young))
        self.assertFalse(os.path.exists(old_mag))
        self.assertTrue(os.path.exists(young_mag))

    def test_repeated_keeps_newest_items_in_recent_date_folders_only(self):
        repeated = self.folder("repeated")
        recent = [touch(os.path.join(repeated, "20261027", "12", f"{i}.jpg"), mtime=1000 + i) for i in range(5)]
        too_old = [touch(os.path.join(repeated, "20260901", "5", f"{i}.jpg"), mtime=1000 + i) for i in range(5)]
        not_a_date = touch(os.path.join(repeated, "misc", "a.jpg"))
        self.cleaner(self.settings(), 76).run_once()
        self.assertEqual([os.path.exists(p) for p in recent], [False, False, True, True, True])
        self.assertTrue(all(os.path.exists(p) for p in too_old))         # old rule: never touched
        self.assertTrue(os.path.exists(not_a_date))

    def test_defective_and_defect_raw_top_level_only(self):
        day = os.path.join(self.folder("defective", 1), "20261027")
        tops = [touch(os.path.join(day, f"{i}.jpg"), mtime=1000 + i) for i in range(4)]
        evidence = [touch(os.path.join(day, "10_11_12", f"{i}.jpg"), mtime=900) for i in range(3)]
        raw_day = os.path.join(self.folder("defect_raw", 1), "20261027")
        raws = [touch(os.path.join(raw_day, f"RAW_{i}.jpg"), mtime=1000 + i) for i in range(3)]
        self.cleaner(self.settings(), 76).run_once()
        self.assertEqual([os.path.exists(p) for p in tops], [False, False, True, True])
        self.assertTrue(all(os.path.exists(p) for p in evidence))
        self.assertEqual([os.path.exists(p) for p in raws], [False, True, True])

    def test_summary_line_counts_files_and_mb(self):
        self.frames(self.folder("plate"), [24 * 11, 24 * 12])
        cleaner = self.cleaner(self.settings(), 76)
        line = cleaner.summary_line(cleaner.delete(76, NOW))
        self.assertIn("Deletion run: 2 files", line)
        self.assertIn(os.path.join("images", "PLATE", "LEFT") + " 2 files", line)

    def test_nothing_outside_the_temp_root_is_touched(self):
        s = self.settings()
        for folders in s.folders.values():
            for folder in folders:
                self.assertTrue(os.path.realpath(folder).startswith(os.path.realpath(self.root)))


# ------------------------------------------------------------------ mails
class MailTests(TempRoot):
    def at(self, cleaner, when, percent):
        self.now = when
        used = int(percent * 10)
        cleaner.disk_usage = lambda path: DiskUsage(1000, used, 1000 - used)
        cleaner.run_once()

    def test_threshold_alert_once_then_rearmed(self):
        cleaner = self.cleaner(self.settings(), 0)
        day = datetime.datetime(2026, 10, 28, 14, 0)
        self.at(cleaner, day, 91)
        self.at(cleaner, day + datetime.timedelta(minutes=1), 92)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertTrue(self.mailer.sent[0].startswith("⚠️ Disk Usage Alert - Tadipatri DPC"))
        self.at(cleaner, day + datetime.timedelta(minutes=2), 89)
        self.at(cleaner, day + datetime.timedelta(minutes=3), 90)       # ">=" as before
        self.assertEqual(len(self.mailer.sent), 2)

    def test_daily_reminder_at_report_time_once_per_day(self):
        cleaner = self.cleaner(self.settings(), 0)
        cleaner.threshold_alert_sent = True                             # alert went out yesterday
        cleaner.last_daily_report_date = datetime.date(2026, 10, 27)
        nine = datetime.datetime(2026, 10, 28, 9, 0, 5)
        self.at(cleaner, nine - datetime.timedelta(minutes=1), 91)
        self.at(cleaner, nine, 91)
        self.at(cleaner, nine + datetime.timedelta(seconds=10), 91)
        self.assertEqual(self.mailer.sent, ["Remainder -  Disk Usage Alert - Tadipatri DPC"])

    def test_no_reminder_below_mail_threshold_or_on_alert_day(self):
        cleaner = self.cleaner(self.settings(), 0)
        nine = datetime.datetime(2026, 10, 28, 9, 0)
        self.at(cleaner, nine, 89)
        self.assertEqual(self.mailer.sent, [])
        self.at(cleaner, nine, 95)               # reminder check runs first, then the alert: both, as before
        self.assertEqual(len(self.mailer.sent), 2)


# ------------------------------------------------------------------ crash behaviour
class LastErrorTests(TempRoot):
    def run_main(self, config_path):
        env = dict(os.environ, DPC_WORKER_CONFIG=config_path)
        return subprocess.run([sys.executable, os.path.join(WORKER_DIR, "cleanup.py")],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_config_error_writes_last_error_and_exits_1(self):
        data = full_json(self.root)
        data["paths"]["plate"] = ["/etc"]
        result = self.run_main(self.write_json(data))
        self.assertEqual(result.returncode, 1)
        path = os.path.join(self.root, "run", "cleanup.last_error.json")
        with open(path) as f:
            record = json.load(f)
        self.assertEqual(record["type"], "ConfigError")
        self.assertIn(" in ", record["where"])
        self.assertTrue(set(record) >= {"at", "type", "message", "where", "traceback"})
        self.assertNotIn(FAKE_SECRET, json.dumps(record))

    def test_unreadable_json_still_reports(self):
        path = self.write_json({})
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(self.run_main(path).returncode, 1)
        self.assertTrue(os.path.exists(os.path.join(self.root, "run", "cleanup.last_error.json")))

    def test_sigterm_exits_0_without_last_error(self):
        data = full_json(self.root)
        del data["graph"]                       # no mail attempt, whatever the real disk shows
        env = dict(os.environ, DPC_WORKER_CONFIG=self.write_json(data))
        proc = subprocess.Popen([sys.executable, os.path.join(WORKER_DIR, "cleanup.py")], env=env)
        log_file = os.path.join(self.root, "logs", "cleanup.log")
        for _ in range(100):
            if os.path.exists(log_file) and "Current Disk Usage" in open(log_file).read():
                break
            import time; time.sleep(0.1)
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "run", "cleanup.last_error.json")))
        with open(log_file) as f:
            text = f.read()
        self.assertIn("Started: data", text)
        self.assertIn("disk mail is OFF", text)


if __name__ == "__main__":
    unittest.main()
