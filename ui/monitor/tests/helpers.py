"""Test helpers: a temporary site root, a config pointing at it, fake MQTT messages and a
fake site DB. No real MQTT broker, MySQL, camera or GPU is needed."""
import datetime
import json
import os
import shutil
import tempfile

from dpc_ui import ui_config

SECRET = "s3cret-db-pass"


def make_root():
    root = tempfile.mkdtemp(prefix="dpc_ui_test_")
    for cam in ("CAM1", "CAM2"):
        os.makedirs(os.path.join(root, "data", "frames", cam, "live"))
    os.makedirs(os.path.join(root, "logs", "workers"))
    os.makedirs(os.path.join(root, "config", "rendered"))
    os.makedirs(os.path.join(root, "data", "images", "DEFECTIVE", "LEFT"))
    os.makedirs(os.path.join(root, "data", "state", "ui"))
    return root


def make_cfg(root, **extra):
    data = {
        "worker_id": "ui", "site": {"id": "testsite", "name": "Test site"},
        "paths": {"root": root}, "log": {"backup_count": 5},
        "ui": {"port": 8080, "refresh_seconds": 2, "event_keep_days": 90, "event_keep_rows": 50000},
        "db": {"host": "localhost", "port": 3306, "name": "testdb", "user": "u", "password": SECRET},
        "mqtt": {"host": "localhost", "port": 1883, "user": None, "password": None, "topic_prefix": "dpc"},
        "cameras": [{"id": "CAM1", "side": "LEFT", "serial": "111", "ip": "192.0.2.10"},
                    {"id": "CAM2", "side": "RIGHT", "serial": "222", "ip": "192.0.2.11"}],
        "sides": [{"side": "LEFT", "camera_id": "CAM1", "inspection_worker": "inspection_left"},
                  {"side": "RIGHT", "camera_id": "CAM2", "inspection_worker": "inspection_right"}],
        "cleanup": {"disk_percent": {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95}},
        "machine": {"total_wheel_count": 190},
    }
    data.update(extra)
    path = os.path.join(root, "config", "rendered", "ui.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return ui_config.load(path)


def write_workers_file(root, mail_enabled=False):
    text = """workers:
  - id: frame_capture_cam1
    type: frame_capture
  - id: frame_capture_cam2
    type: frame_capture
  - id: inspection_left
    type: inspection
  - id: inspection_right
    type: inspection
  - id: plc
    type: plc
  - id: cleanup
    type: cleanup
  - id: mail
    type: mail
    enabled: %s
""" % ("true" if mail_enabled else "false")
    with open(os.path.join(root, "config", "workers.yaml"), "w") as f:
        f.write(text)


def status(worker_id, value="RUNNING", wtype=None, **extra):
    payload = {"worker_id": worker_id, "type": wtype or ("frame_capture" if worker_id.startswith("frame")
                                                         else "inspection" if worker_id.startswith("insp") else worker_id),
               "status": value, "pid": 100, "started_at": "2026-11-06T10:00:00", "restart_count": 0,
               "last_restart_at": None, "exit_code": None,
               "log_file": None, "message": "", "timestamp": "2026-11-06T10:00:00", "mode": "service",
               "restart_cost": "normal", "next_run_at": None, "consecutive_failures": 0, "heartbeat": None,
               "follows": None, "paused_by": None, "config_pending": False, "last_error": None}
    payload.update(extra)
    return f"dpc/worker/{worker_id}/status", payload


def feed_all_running(cache):
    cache.set_connected(True)
    cache.handle("dpc/supervisor/status", {"status": "online", "timestamp": "2026-11-06T10:00:00"})
    for wid in ("frame_capture_cam1", "frame_capture_cam2", "inspection_left", "inspection_right", "plc", "cleanup"):
        cache.handle(*status(wid))
    cache.handle("dpc/plc/status", {"timestamp": "x", "connected": True, "conveyor_status": 1, "light_value": 1,
                                    "light_ok": True, "cleaning_active": False, "last_pulse_at": None,
                                    "auto_cleaning": False})
    cache.handle("dpc/worker/inspection_left/health", {"worker_id": "inspection_left", "healthy": True,
                                                       "fps": 3.9, "inf_queue_depth": 2})


class FakeSiteDB:
    """Stands in for monitor.sources.sitedb.SiteDB."""

    def __init__(self, root, fail=False):
        self.fail = fail
        self.image = os.path.join(root, "data", "images", "DEFECTIVE", "LEFT", "d1.jpg")
        self.rows = [
            {"ID": 7, "defect_side": "Left", "defect_type": "WheelMissing", "view_defect": self.image,
             "wheel_no": "42", "xmin": 10, "xmax": 50, "ymin": 20, "ymax": 60,
             "create_datetime": datetime.datetime(2026, 11, 6, 9, 30), "is_alert_sent": 0},
            {"ID": 8, "defect_side": "Right", "defect_type": "Wheel_Skidding", "view_defect": "/etc/passwd",
             "wheel_no": "43", "xmin": 1, "xmax": 2, "ymin": 3, "ymax": 4,
             "create_datetime": datetime.datetime(2026, 11, 6, 9, 31), "is_alert_sent": 1},
        ]
        self.last_filter = None

    def _check(self):
        if self.fail:
            from monitor.sources.sitedb import SiteDBError
            raise SiteDBError("The site database is not reachable.")

    def ping(self):
        self._check()
        return True

    def live_wheel(self):
        self._check()
        return "117"

    def last_defects(self, count=10):
        self._check()
        return self.rows[:count]

    def defects(self, flt, limit=50, offset=0):
        self._check()
        self.last_filter = flt
        rows = [r for r in self.rows if not flt.side or r["defect_side"] == flt.side]
        return rows[offset:offset + limit], len(rows)

    def defect(self, defect_id):
        self._check()
        return next((r for r in self.rows if r["ID"] == defect_id), None)

    def defect_types(self):
        self._check()
        return sorted({r["defect_type"] for r in self.rows})

    def export_rows(self, flt, max_rows=100000):
        self._check()
        self.last_filter = flt
        return self.rows


def remove(root):
    shutil.rmtree(root, ignore_errors=True)
