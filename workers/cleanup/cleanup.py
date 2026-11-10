#!/usr/bin/env python3
"""Cleanup worker (STATUS P9): keeps the disk of <root>/data from filling up.

Same rules as the old Maihar delete_script.py (thresholds, ages, item limits, order, mails);
what changed is where settings and folders come from (rendered cleanup.json, CONTRACTS 13 /
4.1), the disk being measured on <root>/data (F17), and the crash behaviour (D-073 D7).
INF and DEFECTIVE deletion were commented out in the old loop; they are switched on here
(developer decision 2026-10-28, P9), and defect RAW copies follow the DEFECTIVE rule.

One pass, every `cleanup.interval_seconds`:
  1. disk % = used / (used + free) of <root>/data (reserved blocks excluded, like `df`)
  2. daily reminder mail  (>= mail %, at disk_report_time, once a day)
  3. threshold alert mail (>= mail %, once until usage drops below again)
  4. if > delete %:
       raw rule   (> raw_delete %): RAW_IMAGES/<CAM>, bolt_data/<SIDE>: frame time older than raw_image_hours
       INF rule   (> inference_delete %): every file in INF/<CAM>
       repeated   REPEATED/<SIDE>: per date folder <= repeated.days old keep the newest repeated.items (recursive)
       plate      PLATE/<SIDE>, MAGNET/<SIDE>: frame time older than plate_days
       defective  DEFECTIVE/<SIDE>, images/RAW/<SIDE>: per date folder <= defective.days old keep the newest defective.items
  5. PLATE cap (D-100, every pass, any disk %): keep the newest plate_max_items per side
  6. one INFO line with the files and MB removed per folder (when step 4 ran or step 5 deleted)

Heartbeat (F68, D-034, common/heartbeat.py): dpc/worker/cleanup/health every 10 s with the last
pass: last_pass_at, disk_percent, deleted_files, deleted_mb. healthy = a pass finished within
3 x interval_seconds (counted from the start until the first pass). Off without an `mqtt` block
in cleanup.json or without paho-mqtt; cleanup itself never depends on the broker.

Errors inside a pass are logged and the pass goes on (as before). A config error or anything
that escapes the loop writes <root>/run/cleanup.last_error.json and exits 1; the Supervisor
restarts the program. SIGTERM / SIGINT: the current pass ends, exit 0.
"""

import datetime
import logging
import os
import shutil
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))          # <root>/code
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cleanup_rules as rules                                # noqa: E402
from cleanup_last_error import write_last_error             # noqa: E402
from cleanup_settings import (ConfigError, WORKER_ID, config_path_from_env,  # noqa: E402
                              load_settings, root_from_code_location)
from common.heartbeat import Heartbeat                       # noqa: E402
from disk_mail import GraphMailer, daily_report, threshold_alert   # noqa: E402
from rotating_log import SizeAndTimeRotatingFileHandler      # noqa: E402

HEALTHY_PASS_INTERVALS = 3      # healthy = a pass finished within 3 x interval_seconds (F68)

logger = logging.getLogger("cleanup")
_stop_requested = False


def _request_stop(signum, frame):
    global _stop_requested
    _stop_requested = True


def disk_used_percent(path, disk_usage=shutil.disk_usage) -> float:
    """Same measure as the old code and dpc-check: used / (used + free)."""
    du = disk_usage(path)
    return du.used / (du.used + du.free) * 100


class Cleaner:
    """One cleanup pass at a time. Clock, disk measure and mailer are injectable for tests."""

    def __init__(self, settings, mailer, disk_usage=shutil.disk_usage, clock=datetime.datetime.now,
                 log=logger, monotonic=time.monotonic):
        self.s = settings
        self.mailer = mailer
        self.disk_usage = disk_usage
        self.clock = clock
        self.log = log
        self.monotonic = monotonic
        self.threshold_alert_sent = False
        self.last_daily_report_date = None
        # Last finished pass, read by the heartbeat thread: replaced as a whole, never edited.
        self.last_pass = {"last_pass_at": None, "disk_percent": None, "deleted_files": 0, "deleted_mb": 0.0}
        self.last_pass_finished = monotonic()      # the start counts until the first pass ends

    # ---------- heartbeat (F68) ----------
    def heartbeat_fields(self) -> dict:
        return dict(self.last_pass)

    def is_healthy(self) -> bool:
        """A pass finished within HEALTHY_PASS_INTERVALS x interval_seconds."""
        limit = HEALTHY_PASS_INTERVALS * self.s.interval_seconds
        return self.monotonic() - self.last_pass_finished <= limit

    # ---------- mails (old check_and_send_daily_report / check_and_send_threshold_alert) ----------
    def check_daily_report(self, used, now):
        if used < self.s.disk_percent.mail:
            return
        if now.hour == self.s.report_hour and now.minute == self.s.report_minute \
                and self.last_daily_report_date != now.date():
            self.log.info("Sending daily disk usage report")
            self.mailer.send(*daily_report(self.s.site_name, used, self.s.disk_percent.mail, now))
            self.last_daily_report_date = now.date()

    def check_threshold_alert(self, used, now):
        if used >= self.s.disk_percent.mail:
            if not self.threshold_alert_sent:
                self.log.warning(f"Disk usage crossed threshold: {used:.2f}%")
                self.mailer.send(*threshold_alert(self.s.site_name, used, self.s.disk_percent.mail, now))
                self.threshold_alert_sent = True
                self.last_daily_report_date = now.date()
        else:
            if self.threshold_alert_sent:
                self.log.info("Disk usage back to normal. Resetting threshold alert.")
            self.threshold_alert_sent = False

    # ---------- deletion ----------
    def delete(self, used, now) -> dict:
        """Old deletion block, in the old order. Returns {folder: Removed}."""
        s, f, p = self.s, self.s.folders, self.s.disk_percent
        removed = {}

        def note(folder, result):
            removed.setdefault(folder, rules.Removed()).add(result)

        if used > p.raw_delete:
            self.log.info(f"Raw image deletion enabled: disk usage {used:.2f}% > {p.raw_delete}%")
            age = datetime.timedelta(hours=s.raw_image_hours)
            for folder in f["raw_images"] + f["bolt_data"]:
                note(folder, rules.delete_older_than(folder, age, now, self.log))
        if used > p.inference_delete:
            self.log.info(f"INF image deletion enabled: disk usage {used:.2f}% > {p.inference_delete}%")
            for folder in f["inf"]:
                note(folder, rules.delete_all_files(folder, self.log))
        for folder in f["repeated"]:
            note(folder, rules.keep_newest_in_date_folders(folder, s.repeated.days, s.repeated.items,
                                                           now, self.log, recursive=True))
        plate_age = datetime.timedelta(days=s.plate_days)
        for folder in f["plate"]:
            note(folder, rules.delete_older_than(folder, plate_age, now, self.log))
        for folder in f["magnet"]:
            note(folder, rules.delete_older_than(folder, plate_age, now, self.log,
                                                 time_of=rules.prefixed_frame_time, recursive=True))
        for folder in f["defective"] + f["defect_raw"]:
            note(folder, rules.keep_newest_in_date_folders(folder, s.defective.days, s.defective.items,
                                                           now, self.log, recursive=False))
        return removed

    def cap_plate(self) -> dict:
        """D-100: PLATE/<SIDE> keeps at most plate_max_items files, on every pass. {folder: Removed}"""
        return {folder: rules.keep_newest_by_frame_time(folder, self.s.plate_max_items, self.log)
                for folder in self.s.folders["plate"]}

    def summary_line(self, removed: dict) -> str:
        total = rules.Removed()
        parts = []
        for folder, r in removed.items():
            total.add(r)
            if r.files:
                rel = os.path.relpath(folder, self.s.data)
                parts.append(f"{rel} {r.files} files {r.bytes / 1e6:.1f} MB")
        detail = "; ".join(parts) if parts else "nothing to delete"
        return f"Deletion run: {total.files} files {total.bytes / 1e6:.1f} MB removed ({detail})"

    def run_once(self):
        """One pass. Every error inside is logged and the pass ends normally (old behaviour).
        The pass counts as finished for the heartbeat either way: the loop is turning."""
        now, used, total = None, None, rules.Removed()
        try:
            now = self.clock()
            try:
                used = disk_used_percent(self.s.data, self.disk_usage)
            except OSError as e:
                self.log.error(f"Error obtaining disk usage of {self.s.data}: {e}")
                used = 0
            self.log.info(f"Current Disk Usage: {used:.2f}%")
            self.check_daily_report(used, now)
            self.check_threshold_alert(used, now)
            removed = {}
            above = used > self.s.disk_percent.delete
            if above:
                self.log.info(f"Disk usage is {used:.2f}% (> {self.s.disk_percent.delete}%). "
                              f"Initiating deletion process.")
                removed = self.delete(used, now)
            else:
                self.log.info(f"Disk usage is {used:.2f}% (<= {self.s.disk_percent.delete}%). Skipping deletion.")
            capped = self.cap_plate()
            for folder, r in capped.items():
                removed.setdefault(folder, rules.Removed()).add(r)
            for r in removed.values():
                total.add(r)
            if above or any(r.files for r in capped.values()):
                self.log.info(self.summary_line(removed))
        except Exception:
            self.log.exception("Error in main run loop")
        self._record_pass(now, used, total)

    def _record_pass(self, now, used, total):
        self.last_pass = {
            "last_pass_at": (now or self.clock()).isoformat(timespec="seconds"),
            "disk_percent": None if used is None else round(used, 2),
            "deleted_files": total.files,
            "deleted_mb": round(total.bytes / 1e6, 1),
        }
        self.last_pass_finished = self.monotonic()


def setup_logger(settings):
    os.makedirs(settings.logs, exist_ok=True)
    handler = SizeAndTimeRotatingFileHandler(os.path.join(settings.logs, f"{settings.worker_id}.log"),
                                             retention_days=settings.backup_count)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _sleep(seconds):
    end = time.monotonic() + seconds
    while not _stop_requested and time.monotonic() < end:
        time.sleep(min(0.5, end - time.monotonic()))


def _where_to_report(config_path):
    """(root, worker_id) for last_error.json even when the settings cannot be read:
    <root>/config/rendered/<worker_id>.json, else the code location."""
    if config_path:
        rendered = os.path.dirname(os.path.abspath(config_path))
        if os.path.basename(rendered) == "rendered":
            return (os.path.dirname(os.path.dirname(rendered)),
                    os.path.splitext(os.path.basename(config_path))[0])
    return root_from_code_location(), WORKER_ID


def main() -> int:
    config_path = config_path_from_env()
    root, worker_id = _where_to_report(config_path)
    secrets = ()
    heartbeat = None
    try:
        stale = os.path.join(root, "run", f"{worker_id}.last_error.json")
        if os.path.exists(stale):
            os.remove(stale)          # the file always describes the last exit of this program
        settings = load_settings(config_path)
        root, worker_id, secrets = settings.root, settings.worker_id, settings.secrets()
        setup_logger(settings)
        for warning in settings.warnings:
            logger.warning(warning)
        logger.info(f"Started: data {settings.data}, interval {settings.interval_seconds} s, "
                    f"disk % delete/raw/mail/inf {settings.disk_percent.delete}/{settings.disk_percent.raw_delete}/"
                    f"{settings.disk_percent.mail}/{settings.disk_percent.inference_delete}, "
                    f"mail {'on' if settings.mail.enabled else 'OFF'}")
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        cleaner = Cleaner(settings, GraphMailer(settings.mail, logger))
        heartbeat = Heartbeat(worker_id, mqtt=settings.mqtt, extra=cleaner.heartbeat_fields,
                              healthy=cleaner.is_healthy, logger=logger)
        heartbeat.start()
        while not _stop_requested:
            cleaner.run_once()
            _sleep(settings.interval_seconds)
        logger.info("Stop requested: exiting.")
        heartbeat.stop()
        return 0
    except Exception as e:
        if isinstance(e, ConfigError):
            message = f"Config error: {e}"
        else:
            message = "Unexpected error"
        logger.exception(message) if logger.handlers else print(f"{message}: {e}", file=sys.stderr)
        path = write_last_error(root, worker_id, e, secrets)
        print(f"cleanup: {type(e).__name__}, details in {path}", file=sys.stderr)
        if heartbeat is not None:
            heartbeat.stop()
        return 1


if __name__ == "__main__":
    sys.exit(main())
