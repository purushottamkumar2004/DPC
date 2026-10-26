"""Inspection for one side of the DPC (D-069): the same program runs twice under the Supervisor,
as `inspection_left` and `inspection_right`, each with its rendered JSON in DPC_WORKER_CONFIG.

Built from LeftAlgorithm.py (the reference); INSPECTION.md describes the behaviour with
references to the old line numbers. Per frame, oldest INF file first:
    inference -> group detections by class -> plate / cycle start -> (skip until the first plate)
    -> wheel counter -> live wheel number (one side, D3) -> bucket clip bolts
    -> copy to RAW_IMAGES -> skidding -> wheel missing -> move INF -> RAW_IMAGES
    -> daily wheel report (D4)

Stopping:
    SIGTERM / SIGINT  finish the current frame, exit 0.
    unexpected error  log it, write <root>/run/<worker_id>.last_error.json, exit 1; the
                      Supervisor restarts the program (D-073 D7). There is no in-process restart.

Run (the Supervisor does this):  DPC_WORKER_CONFIG=<root>/config/rendered/inspection_left.json \
                                  python3 inspection.py
"""

import logging
import os
import shutil
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))      # <root>/code
sys.path.insert(0, REPO_ROOT)                           # for common.config (model packages)

import cv2      # noqa: E402
import pymysql  # noqa: E402

from check_bucket_clip import BucketClipCheck          # noqa: E402
from check_cycle_restart import CycleRestartCheck      # noqa: E402
from check_skidding import SkiddingCheck               # noqa: E402
from check_wheel_missing import WheelMissingCheck      # noqa: E402
from defects import DefectStore                        # noqa: E402
from heartbeat import Heartbeat, LoopStats, code_version   # noqa: E402
from last_error import write_last_error                # noqa: E402
from settings import config_secrets, settings_from_env  # noqa: E402
from tracking import PlateTracker, WheelCounter        # noqa: E402
from wheel_report import WheelReport                   # noqa: E402

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
EMPTY_INF_SLEEP_S = 2           # L:2314
MIN_FILE_AGE_S = 0.03           # L:507 (kept; the atomic hand-off makes it unnecessary, D-031)
DB_RETRY_S = 1                  # L:2521-2525

logger = logging.getLogger("inspection")
_stop_requested = False


# ======================================================================
# Start-up
# ======================================================================
def _request_stop(signum, frame):
    global _stop_requested
    _stop_requested = True


def setup_logger(settings):
    log_file = os.path.join(settings.paths.logs, f"{settings.worker_id}.log")
    handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=settings.log_backup_count)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.setLevel(logging.INFO)               # as before: debug lines are not written
    logger.addHandler(handler)


def create_output_dirs(settings):
    for folder in settings.paths.output_dirs():
        os.makedirs(folder, exist_ok=True)


def connect_db(settings, stats):
    """Retry until the DB answers (as L:2511-2525); a stop request ends the wait."""
    while not _stop_requested:
        try:
            return pymysql.connect(host=settings.db.host, port=settings.db.port, user=settings.db.user,
                                   password=settings.db.password, database=settings.db.name)
        except pymysql.MySQLError as e:
            logger.critical(f"getDatabaseConnection() Exception is : {e}")
            stats.tick()                         # waiting for the DB is not a hung loop
            time.sleep(DB_RETRY_S)
    return None


def purge_inf(inf_dir):
    """L:2299-2304: frames waiting from before this start are deleted, not inspected
    (the wheel counter and plate state they need are gone)."""
    for name in os.listdir(inf_dir):
        if name.lower().endswith(IMAGE_EXTENSIONS):
            path = os.path.join(inf_dir, name)
            try:
                os.remove(path)
                logger.info(f"Deleted: {path}")
            except OSError as e:
                logger.error(f"Error deleting {path}: {e}")


# ======================================================================
# One frame
# ======================================================================
def is_valid_image(path) -> bool:
    """L:503-526: exists, old enough, not empty, decodable."""
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) <= MIN_FILE_AGE_S:
        return False
    try:
        return os.path.getsize(path) > 0 and cv2.imread(path) is not None
    except Exception as e:
        logger.error(f"Error in reading the Image {e}")
        return False


def group_by_class(detections, label):
    """Detections by the name the checks use (label mapping applied). Each detection keeps the
    model's own class name at index 5, as before (L:2345-2352)."""
    by_class = {}
    for det in detections:
        by_class.setdefault(label(det[5]), []).append(det)
    return by_class


class Inspection:
    """All per-run state: lost on a restart (INSPECTION.md 9)."""

    def __init__(self, settings, detector, store, stats):
        self.s, self.detector, self.store, self.stats = settings, detector, store, stats
        self.wheels = WheelCounter(settings.motion, settings.tracker.new_wheel_jump_px, logger)
        self.plates = PlateTracker(settings.tracker.new_plate_jump_px, logger)
        self.bucket_clip = BucketClipCheck(settings, store, logger)
        self.skidding = SkiddingCheck(settings, store, logger)
        self.wheel_missing = WheelMissingCheck(settings, store, logger)
        self.cycle_restart = CycleRestartCheck(settings, store, logger)
        self.wheel_report = WheelReport(settings, logger)
        self.plate_found = False                # no inspection before the first plate
        self.wheel_counter = 0
        self.live_counter_written = 0           # R:2884 starts at 0
        self.wheel_present = []                 # WHEEL_PRESENT_LIST: grows forever (B9, kept)

    def process(self, frame_path):
        image = cv2.imread(frame_path)
        if image is None:
            logger.error(f"Failed to load image: {frame_path}. Skipping...")
            return
        clean = image.copy()                     # skidding gets a frame without boxes
        by_class = group_by_class(self.detector.detect(image), self.detector.label)
        repeated_list = []                       # repeated-defects lookup disabled (L:2340-2341)

        for det in by_class.get("wheel_stand", []):
            logger.info(f"STAND FOUND → stand_count {det[5]} = 0")
        for _ in by_class.get("wheel", []) + by_class.get("half_wheel", []):
            self.wheel_present.insert(0, True)
        for det in by_class.get("wheel", []):    # D4: uses the previous frame's wheel number
            self.wheel_report.update(det, self.wheel_counter, frame_path)

        wheels, half_wheels = by_class.get("wheel", []), by_class.get("half_wheel", [])
        plates = by_class.get("plate", []) + by_class.get("plate_area", [])
        if plates:
            wheels, half_wheels = self._on_plate(plates, frame_path, image, repeated_list, wheels, half_wheels)
        if not self.plate_found:
            logger.info("Plate not detected yet.. skipping frame")
            self._move_to_raw(frame_path)
            return

        self.wheel_counter = self.wheels.update(wheels, half_wheels)
        logger.info(f"Current Wheel Counter (Tracking): {self.wheel_counter}")
        if self.s.writes_live_wheel_counter and self.wheel_counter != self.live_counter_written:
            self.store.write_live_wheel_count(self.wheel_counter)
            self.live_counter_written = self.wheel_counter

        self.bucket_clip.run(by_class.get("nut_bolt", []), by_class.get("bucket_clip", []),
                             by_class.get("high_dusty_bucket_clip", []), by_class.get("half_bucket_clip", []),
                             by_class.get("half_high_dusty_bucket_clip", []), image, self.wheel_counter,
                             repeated_list, frame_path)
        # The current frame must be in RAW_IMAGES before the skidding evidence copy (L:2447).
        shutil.copy2(frame_path, os.path.join(self.s.paths.raw, os.path.basename(frame_path)))
        self.skidding.run(wheels, by_class.get("wheel_magnet", []), by_class.get("wheel_bolt", []),
                          clean, self.wheel_counter, repeated_list, frame_path)
        self.wheel_missing.run(wheels, half_wheels, image, self.wheel_counter, repeated_list)
        self._move_to_raw(frame_path)
        self.wheel_report.run_daily()

    def _on_plate(self, plates, frame_path, image, repeated_list, wheels, half_wheels):
        """L:2389-2420: copy the frame to PLATE; at a new plate report a short cycle and
        start counting again from 0, ignoring this frame's wheels."""
        try:
            shutil.copy2(frame_path, os.path.join(self.s.paths.plate, os.path.basename(frame_path)))
            logger.info(f"image file {frame_path} copied to {self.s.paths.plate}")
        except OSError as e:
            logger.error(f"Failed to copy image {frame_path} to {self.s.paths.plate} | {e}")
        if not self.plates.is_new_plate(plates, os.path.basename(frame_path)):
            return wheels, half_wheels
        # a copy: the cycle-restart box must not end up on this frame's other defect images
        self.cycle_restart.run(self.wheels.count, image.copy(), repeated_list)
        if not self.plate_found:
            self.plate_found = True
            logger.info("Plate Found: Resetting state and clearing wheel lists for this frame.")
        self.wheel_counter = 0
        self.wheels.reset_count()
        logger.info(f"Wheel Counter reset to 0 on wheel counter: {self.wheel_counter}")
        return [], []

    def _move_to_raw(self, frame_path):
        destination = os.path.join(self.s.paths.raw, os.path.basename(frame_path))
        try:
            shutil.move(frame_path, destination)
        except OSError as e:
            logger.error(f"Failed to move image {frame_path} to {self.s.paths.raw} | {e}")


# ======================================================================
# Main loop
# ======================================================================
def oldest_inf_file(inf_dir):
    files = sorted(n for n in os.listdir(inf_dir) if os.path.isfile(os.path.join(inf_dir, n)))
    return (os.path.join(inf_dir, files[0]) if files else None), len(files)


def run(inspection, settings, stats):
    inf_dir = settings.paths.inf
    while not _stop_requested:
        frame, depth = oldest_inf_file(inf_dir)
        stats.tick(depth)
        if frame is None:
            logger.info("No files found in directory, waiting...")
            time.sleep(EMPTY_INF_SLEEP_S)
            continue
        if not is_valid_image(frame):
            try:
                os.remove(frame)
                logger.warning(f"Removed invalid image: {frame}")
            except OSError as e:
                logger.warning(f"Failed to remove invalid image: {frame} | {e}")
            continue
        inspection.process(frame)
        stats.frame_done()


def _where_to_report(settings):
    """(<root>, <worker_id>) for last_error.json. When the config itself could not be read,
    both come from its path <root>/config/rendered/<worker_id>.json (CONTRACTS 13)."""
    if settings is not None:
        return settings.paths.root, settings.worker_id
    path = os.environ.get("DPC_WORKER_CONFIG", "")
    rendered = os.path.dirname(os.path.abspath(path)) if path else ""
    if os.path.basename(rendered) == "rendered" and os.path.basename(os.path.dirname(rendered)) == "config":
        return os.path.dirname(os.path.dirname(rendered)), os.path.splitext(os.path.basename(path))[0]
    return None, None


def main() -> int:
    settings = None
    heartbeat = None
    try:
        settings = settings_from_env()
        create_output_dirs(settings)
        setup_logger(settings)
        stale = os.path.join(settings.paths.root, "run", f"{settings.worker_id}.last_error.json")
        if os.path.exists(stale):
            os.remove(stale)      # the file always describes the last exit of this program
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        version = code_version(REPO_ROOT)
        logger.info(f"Started pid={os.getpid()} version={version} {settings.summary()}")

        stats = LoopStats()
        heartbeat = Heartbeat(settings, stats, version, logger)
        heartbeat.start()

        conn = connect_db(settings, stats)
        if conn is None:
            return 0
        from model import Detector, load_package          # Detectron2 / GPU from here on
        stats.tick()
        detector = Detector(load_package(settings.models_dir, settings.model_name), settings.threshold, logger)
        store = DefectStore(conn, conn.cursor(), settings, logger)
        purge_inf(settings.paths.inf)
        run(Inspection(settings, detector, store, stats), settings, stats)
        logger.info("Stop requested: finished the current frame, exiting")
        return 0
    except Exception as exc:
        secrets = config_secrets(settings)
        if logger.handlers:
            logger.error("Unexpected error, exiting (the Supervisor restarts inspection)", exc_info=True)
        root, worker_id = _where_to_report(settings)
        if root:
            path = write_last_error(root, worker_id, exc, secrets)
            print(f"inspection stopped: {type(exc).__name__}: details in {path}", file=sys.stderr)
        else:
            print(f"inspection could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if heartbeat is not None:
            heartbeat.stop()


if __name__ == "__main__":
    sys.exit(main())
