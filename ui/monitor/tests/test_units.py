"""Unit tests without Django views: config, messages, cache, overview, files, system, sitedb."""
import datetime
import os
import types
import unittest

from dpc_ui import ui_config
from monitor import messages, overview
from monitor.sources import files, system
from monitor.sources.sitedb import DefectFilter, SiteDB, SiteDBError
from monitor.state.cache import StateCache
from monitor.tests import helpers


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root()
        self.addCleanup(helpers.remove, self.root)
        self.addCleanup(ui_config.set_config, ui_config.get_config())

    def test_missing_file_gives_safe_defaults_and_one_warning(self):
        with self.assertLogs("dpc.ui.config", "WARNING") as logs:
            cfg = ui_config.load(os.path.join(self.root, "nope.json"))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(cfg["ui"]["port"], 8080)
        self.assertEqual(cfg["db"]["name"], "")
        self.assertTrue(cfg.paths["state"].endswith(os.path.join("data", "state", "ui")))

    def test_partial_file_warns_once_per_missing_key_and_derives_paths(self):
        path = os.path.join(self.root, "ui.json")
        with open(path, "w") as f:
            f.write('{"paths": {"root": "%s"}, "ui": {"port": 9000}}' % self.root)
        with self.assertLogs("dpc.ui.config", "WARNING") as logs:
            cfg = ui_config.load(path)
        self.assertEqual(cfg["ui"]["port"], 9000)
        self.assertEqual(cfg["ui"]["refresh_seconds"], 2)
        self.assertEqual(cfg.paths["frames"], os.path.join(self.root, "data", "frames"))
        self.assertEqual(cfg["sides"][1], {"side": "RIGHT", "camera_id": "CAM2", "inspection_worker": "inspection_right"})
        joined = "\n".join(logs.output)
        self.assertIn("'ui.refresh_seconds'", joined)
        self.assertIn("'db'", joined)
        self.assertEqual(len(logs.output), len(set(logs.output)))

    def test_secret_values(self):
        cfg = helpers.make_cfg(self.root)
        self.assertEqual(cfg.secret_values(), [helpers.SECRET])


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root()
        self.addCleanup(helpers.remove, self.root)
        self.addCleanup(ui_config.set_config, ui_config.get_config())
        ui_config.set_config(helpers.make_cfg(self.root))

    def test_labels(self):
        self.assertEqual(messages.worker_label("frame_capture_cam1"), "LEFT camera")
        self.assertEqual(messages.worker_label("inspection_right"), "RIGHT inspection")
        self.assertEqual(messages.worker_label("plc"), "PLC service")

    def test_capture_failed_is_plain_with_action(self):
        _, payload = helpers.status("frame_capture_cam1", "FAILED")
        text = messages.status_text("frame_capture_cam1", payload)
        self.assertEqual(text.level, "error")
        self.assertIn("LEFT camera is not delivering images", text.sentence)
        self.assertIn("cable", text.action)

    def test_capture_retrying_matches_requested_wording(self):
        _, payload = helpers.status("frame_capture_cam1", "WAITING_RESTART")
        text = messages.status_text("frame_capture_cam1", payload)
        self.assertEqual(text.sentence, "LEFT camera is not delivering images. The system keeps retrying.")
        self.assertEqual(text.action, "Check the camera cable and power.")

    def test_paused_and_idle(self):
        _, payload = helpers.status("inspection_left", "STOPPED", paused_by="frame_capture_cam1")
        self.assertIn("paused until LEFT camera", messages.status_text("inspection_left", payload).sentence)
        _, payload = helpers.status("cleanup", "STOPPED", next_run_at="2026-11-06T11:00:00")
        self.assertEqual(messages.effective_status(payload), "IDLE")

    def test_events(self):
        text = messages.event_text("inspection_left", "roi_drift", {"side": "LEFT", "class": "wheel", "ratio": 0.31})
        self.assertIn("31 %", text.sentence)
        self.assertEqual(text.level, "warning")
        text = messages.event_text("inspection_left", "worker_failure",
                                   {"worker_id": "inspection_left", "last_error": {"message": "CUDA out of memory"}})
        self.assertIn("LEFT inspection stopped because of an error. Reason: CUDA out of memory", text.sentence)
        self.assertEqual(messages.event_text("x", "something_new", {}).level, "info")


class FakeRecorder:
    def __init__(self):
        self.events, self.known = [], {}

    def record(self, topic, source, name, payload):
        self.events.append((source, name))

    def transition(self, key, value):
        previous = self.known.get(key)
        self.known[key] = value
        return previous != value, previous, 1000.0


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root()
        self.addCleanup(helpers.remove, self.root)
        self.addCleanup(ui_config.set_config, ui_config.get_config())
        ui_config.set_config(helpers.make_cfg(self.root))
        self.rec = FakeRecorder()
        self.cache = StateCache(recorder=self.rec, clock=lambda: 2000.0)

    def test_statuses_heartbeats_plc(self):
        helpers.feed_all_running(self.cache)
        snap = self.cache.snapshot()
        self.assertEqual(snap["statuses"]["inspection_left"]["payload"]["status"], "RUNNING")
        self.assertEqual(snap["heartbeats"]["inspection_left"]["payload"]["fps"], 3.9)
        self.assertTrue(snap["plc"]["payload"]["connected"])
        self.assertEqual(snap["supervisor"]["payload"]["status"], "online")
        self.assertEqual(self.rec.events, [])          # first RUNNING / online / connected: quiet

    def test_transitions_recorded_once(self):
        helpers.feed_all_running(self.cache)
        for _ in range(3):                              # the same status repeated every 30 s
            self.cache.handle(*helpers.status("frame_capture_cam1", "FAILED"))
        self.cache.handle("dpc/plc/status", {"connected": False})
        self.cache.handle("dpc/supervisor/status", {"status": "offline"})
        self.assertEqual(self.rec.events, [("frame_capture_cam1", "status_changed"), ("plc", "plc_disconnected"),
                                           ("supervisor", "supervisor_offline")])

    def test_events_and_ignored_topics(self):
        self.cache.handle("dpc/worker/inspection_left/event", {"event": "roi_drift", "side": "LEFT"})
        self.cache.handle("dpc/supervisor/event", {"event": "config_reloaded"})
        self.cache.handle("dpc/plc/command", {"action": "clean_pulse"})
        self.cache.handle("other/worker/x/status", {"status": "FAILED"})
        self.cache.handle("dpc/supervisor/status", "online")      # plain text payload
        self.assertEqual(self.rec.events, [("inspection_left", "roi_drift"), ("supervisor", "config_reloaded")])
        self.assertEqual(self.cache.snapshot()["statuses"], {})

    def test_recorder_error_never_raises(self):
        class Broken(FakeRecorder):
            def record(self, *a):
                raise RuntimeError("disk full")

            def transition(self, *a):
                raise RuntimeError("disk full")
        cache = StateCache(recorder=Broken())
        with self.assertLogs("dpc.ui.cache", "ERROR"):
            cache.handle("dpc/worker/plc/event", {"event": "light_on_written"})
            cache.handle(*helpers.status("plc", "FAILED"))
        self.assertEqual(cache.snapshot()["statuses"]["plc"]["payload"]["status"], "FAILED")


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root()
        self.addCleanup(helpers.remove, self.root)
        self.addCleanup(ui_config.set_config, ui_config.get_config())
        self.cfg = helpers.make_cfg(self.root)
        ui_config.set_config(self.cfg)
        helpers.write_workers_file(self.root)
        self.now = [5000.0]
        self.cache = StateCache(recorder=FakeRecorder(), clock=lambda: self.now[0])
        helpers.feed_all_running(self.cache)
        self.disk_ok = {"ok": True, "level": "ok", "percent": 40.0}

    def _state(self, disk=None):
        from monitor.sources.workers_file import configured_workers
        snap = self.cache.snapshot()
        progs = overview.programs(snap, configured_workers(self.cfg.paths["workers_file"]))
        sides = overview.sides(self.cfg, progs)
        found = overview.problems(snap, progs, sides, disk or self.disk_ok)
        return progs, found, overview.banner(found)

    def test_all_running_is_green_and_disabled_mail_is_off(self):
        progs, found, banner = self._state()
        self.assertEqual(banner["level"], "ok", [p.sentence for p in found])
        mail = overview.program_by_id(progs, "mail")
        self.assertEqual(mail["value"], "DISABLED")
        self.assertEqual(mail["text"].level, "off")

    def test_capture_failed_is_red(self):
        self.cache.handle(*helpers.status("frame_capture_cam2", "FAILED"))
        _, found, banner = self._state()
        self.assertEqual(banner["level"], "error")
        self.assertIn("RIGHT camera", banner["headline"])

    def test_restarting_capture_turns_red_after_60_s(self):
        self.cache._recorder = None            # 'since' = when received
        self.cache.handle(*helpers.status("frame_capture_cam1", "WAITING_RESTART"))
        self.assertEqual(self._state()[2]["level"], "warning")
        self.now[0] += 61
        self.cache.handle("dpc/supervisor/status", {"status": "online"})
        self.assertEqual(self._state()[2]["level"], "error")

    def test_recent_restart_queue_disk_plc_amber_and_red(self):
        recent = datetime.datetime.fromtimestamp(self.now[0] - 120).isoformat()
        self.cache.handle(*helpers.status("plc", "RUNNING", last_restart_at=recent))
        _, found, banner = self._state()
        self.assertEqual(banner["level"], "warning")
        self.assertIn("was restarted at", banner["headline"])
        _, _, banner = self._state(disk={"ok": True, "level": "mail", "percent": 92.0})
        self.assertEqual(banner["level"], "error")
        self.cache.handle("dpc/plc/status", {"connected": False})
        self.assertIn("PLC does not answer", self._state()[2]["headline"])

    def test_frozen_image_while_running_is_amber(self):
        from monitor.sources.workers_file import configured_workers
        snap = self.cache.snapshot()
        progs = overview.programs(snap, configured_workers(self.cfg.paths["workers_file"]))

        class Meter:
            def reading(self, cam):
                return {"fps": 0.0, "age_s": 46.0 if cam == "CAM1" else 0.2}
        found = overview.problems(snap, progs, overview.sides(self.cfg, progs, Meter()), self.disk_ok)
        self.assertEqual([p.sentence for p in found], ["LEFT camera: the newest image is 46 s old."])

    def test_supervisor_offline_and_mqtt_lost(self):
        self.cache.handle("dpc/supervisor/status", {"status": "offline"})
        self.assertIn("Supervisor is not running", self._state()[2]["headline"])
        self.cache.set_connected(False)
        self.assertIn("no connection to the status service", self._state()[2]["headline"])

    def test_mail_stopped_is_not_a_problem(self):
        helpers.write_workers_file(self.root, mail_enabled=True)
        os.utime(self.cfg.paths["workers_file"], (1, 1))
        self.cache.handle(*helpers.status("mail", "STOPPED"))
        self.assertEqual(self._state()[2]["level"], "ok")


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root()
        self.addCleanup(helpers.remove, self.root)
        self.addCleanup(ui_config.set_config, ui_config.get_config())
        ui_config.set_config(helpers.make_cfg(self.root))

    def test_frame_meter_counts_new_frames(self):
        now, key = [100.0], [1]

        def stat(path):
            return types.SimpleNamespace(st_ino=key[0], st_mtime_ns=int(now[0] * 1e9), st_mtime=now[0])
        meter = files.FrameMeter("/x", ["CAM1"], clock=lambda: now[0], stat=stat)
        for _ in range(40):                    # 4 new files per second for 10 s
            meter.sample()
            now[0] += 0.25
            key[0] += 1
        reading = meter.reading("CAM1")
        self.assertAlmostEqual(reading["fps"], 4.0, delta=0.2)
        self.assertLess(reading["age_s"], 1)

    def test_log_tail_filters_masks_and_stays_in_logs(self):
        log = os.path.join(self.root, "logs", "workers", "plc.log")
        with open(log, "w") as f:
            f.write("".join(f"INFO line {i}\n" for i in range(300)))
            f.write(f"ERROR could not log in with {helpers.SECRET}\n")
        lines, err = files.log_tail(log, os.path.join(self.root, "logs"), 200)
        self.assertEqual((len(lines), err), (200, ""))
        lines, _ = files.log_tail(log, os.path.join(self.root, "logs"), level="ERROR")
        self.assertEqual(lines, ["ERROR could not log in with ********"])
        lines, _ = files.log_tail(log, os.path.join(self.root, "logs"), text="LINE 29")
        self.assertEqual(len(lines), 11)
        lines, err = files.log_tail("/etc/hostname", os.path.join(self.root, "logs"))
        self.assertEqual(lines, [])
        self.assertIn("outside", err)

    def test_inside(self):
        self.assertTrue(files.inside(os.path.join(self.root, "data", "x.jpg"), os.path.join(self.root, "data")))
        self.assertFalse(files.inside(os.path.join(self.root, "data", "..", "x.jpg"), os.path.join(self.root, "data")))


class SystemTests(unittest.TestCase):
    def test_disk_level(self):
        t = {"delete": 75, "mail": 90}
        self.assertEqual([system.disk_level(p, t) for p in (10, 75, 89.9, 90)], ["ok", "delete", "delete", "mail"])

    def test_parse_gpu(self):
        g = system.parse_gpu_line("NVIDIA RTX A4000, 63, 1800, 78, 5000, 16376")
        self.assertEqual((g["temperature_c"], g["clock_mhz"], g["load_percent"]), (63.0, 1800.0, 78.0))

    def test_gpu_missing_driver(self):
        def runner(*a, **k):
            raise FileNotFoundError()
        system._gpu_cache.update(at=0.0, value=None)
        result = system.gpu(now=1.0, runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("nvidia-smi", result["error"])
        system._gpu_cache.update(at=0.0, value=None)

    def test_cpu_ram_from_proc(self):
        import tempfile
        proc = tempfile.mkdtemp()
        with open(f"{proc}/stat", "w") as f:
            f.write("cpu 100 0 100 800 0 0 0 0 0 0\n")
        with open(f"{proc}/loadavg", "w") as f:
            f.write("0.50 0.40 0.30 1/100 1\n")
        with open(f"{proc}/meminfo", "w") as f:
            f.write("MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\n")
        result = system.cpu_ram(proc)
        self.assertEqual(result["ram_used_percent"], 75.0)
        self.assertEqual(result["load"], [0.5, 0.4, 0.3])


class SiteDBTests(unittest.TestCase):
    def test_filter_sql_is_parameterised(self):
        flt = DefectFilter(date_from=datetime.date(2026, 11, 1), date_to=datetime.date(2026, 11, 6),
                           side="Left", defect_type="WheelMissing", wheel=" 42 ")
        where, params = flt.where()
        self.assertEqual(where.count("%s"), 5)
        self.assertNotIn("42", where)
        self.assertEqual(params[-1], "42")
        self.assertEqual(params[1], datetime.datetime(2026, 11, 7))

    def test_unconfigured_db_is_a_plain_error(self):
        with self.assertRaises(SiteDBError) as ctx:
            SiteDB({"name": ""}).ping()
        self.assertIn("not configured", str(ctx.exception))

    def test_unreachable_db_is_a_plain_error(self):
        db = SiteDB({"name": "x", "host": "127.0.0.1", "port": 1, "user": "u", "password": "p"})
        with self.assertRaises(SiteDBError) as ctx:
            db.live_wheel()
        self.assertIn("not reachable", str(ctx.exception))


class EntryPointTests(unittest.TestCase):
    def test_listens_on_localhost_only(self):
        import run_ui
        self.assertEqual(run_ui.LISTEN_HOST, "127.0.0.1")
