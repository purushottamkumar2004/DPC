"""
FrameCapture_Basler.py: single-camera Basler capture for the DPC platform.

One process per camera (D-033). Settings come from the rendered worker config
<root>/config/rendered/frame_capture_<camn>.json (CONTRACTS 13, D-062), whose path the
Supervisor passes in the env variable DPC_WORKER_CONFIG. The file is written by the
renderer from site.yaml; this program never reads site.yaml or an XML itself.
A settings change reaches the camera by a Supervisor reload, which restarts this worker.

Every frame is handed off atomically to INF, UI and live (D-031).

Robustness:
  - the camera is reopened after device-lost, or after a long stretch without a good frame,
    without restarting the process;
  - only a grab retrieved in the current loop iteration is ever published;
  - each camera setting is set by feature name, with one log line per setting;
  - SIGTERM/SIGINT finish the current frame and stop cleanly (so _tmp is left empty).
"""
import errno
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime
from logging import handlers

import cv2
import pymysql
from pymysql.constants import CLIENT
from pypylon import genicam, pylon

# ---------------------------------------------------------------------------
# Timing constants. These are the same on every site, so they are not config.
# ---------------------------------------------------------------------------
RETRIEVE_TIMEOUT_MIN_MS = 5000   # wait at most this long for one frame (or 3 frame periods if longer)
NO_FRAME_REOPEN_S = 15           # no good frame for this long -> treat the camera as lost and reopen it
REOPEN_RETRY_S = 2               # pause between reopen attempts (keeps recovery well under 30 s)
LOG_REPEAT_S = 60                # a repeating problem is logged at most once per this period
DB_CONNECT_TIMEOUT_S = 3         # a DB outage must not stall capture for long

CONFIG_ENV = "DPC_WORKER_CONFIG"
DEFAULT_HEALTH_INTERVAL_S = 600  # used when the JSON has no health_interval_s (same rhythm as the old code)

CV2_ROTATION = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}
VALID_ROTATIONS = (0,) + tuple(CV2_ROTATION)

cfg = {}                         # flat settings, filled once by load_worker_config()
logger = logging.getLogger("FrameCapture_Basler")
_copy_fallback_warned = False
_stop_requested = False


# ===========================================================================
# Configuration: the rendered JSON (CONTRACTS 13)
# ===========================================================================
def _require(section, key, where):
    """section[key], or a ValueError naming the missing key (e.g. 'camera.serial')."""
    if not isinstance(section, dict) or key not in section:
        raise ValueError(f"missing key '{where}.{key}'" if where else f"missing key '{key}'")
    return section[key]


def _validated_rotation(value):
    if isinstance(value, bool) or not isinstance(value, int) or value not in VALID_ROTATIONS:
        raise ValueError(f"camera.rotate must be one of {VALID_ROTATIONS}, got {value!r}")
    return value


def _validated_table_name(name):
    # A table name cannot be a SQL parameter, so it is checked strictly before use.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"db.health_table {name!r} may only contain letters, digits and '_'")
    return name


def _validated_health_interval(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"health_interval_s must be a whole number of seconds above 0, got {value!r}")
    return value


def _validated_log_file_name(name):
    if not isinstance(name, str) or not name or os.path.basename(name) != name:
        raise ValueError(f"log.file_name {name!r} must be a file name, not a path")
    return name


def load_worker_config(path):
    """Read the rendered JSON and return the flat settings this program uses.
    Raises ValueError (or OSError / json errors) on any problem: a capture with a broken
    config must stop with a clear message, not run half-configured."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("the file does not hold a JSON object")

    paths = _require(data, "paths", "")
    log_cfg = _require(data, "log", "")
    camera = _require(data, "camera", "")
    db = _require(data, "db", "")

    driver = _require(camera, "driver", "camera")
    if driver != "basler":
        raise ValueError(f"camera.driver is '{driver}': this program handles Basler cameras only")
    fps = float(_require(camera, "fps", "camera"))
    if fps <= 0:
        raise ValueError(f"camera.fps must be greater than 0, got {fps}")
    serial = str(_require(camera, "serial", "camera")).strip()
    if not serial:
        raise ValueError("camera.serial is empty")

    return {
        "config_path": path,
        "worker_id": _require(data, "worker_id", ""),
        # folders (paths.raw_images is not used by capture; inspection moves frames there)
        "logs_dir": _require(paths, "logs", "paths"),
        "live_dir": _require(paths, "live", "paths"),
        "tmp_dir": _require(paths, "tmp", "paths"),
        "inf_dir": _require(paths, "inf", "paths"),
        "ui_dir": _require(paths, "ui", "paths"),
        # log
        "log_backup_count": int(_require(log_cfg, "backup_count", "log")),
        "log_file_name": _validated_log_file_name(_require(log_cfg, "file_name", "log")),
        # camera
        "camera_id": _require(camera, "id", "camera"),
        "side": _require(camera, "side", "camera"),
        "serial": serial,
        "fps": fps,
        "exposure_us": float(_require(camera, "exposure_us", "camera")),
        "rotate_deg": _validated_rotation(_require(camera, "rotate", "camera")),
        # database (health only)
        "db_host": _require(db, "host", "db"),
        "db_port": int(db.get("port", 3306)),
        "db_name": _require(db, "name", "db"),
        "db_user": _require(db, "user", "db"),
        "db_password": _require(db, "password", "db"),
        "health_table": _validated_table_name(_require(db, "health_table", "db")),
        # optional; not rendered yet (see README)
        "health_interval_s": _validated_health_interval(
            data.get("health_interval_s", DEFAULT_HEALTH_INTERVAL_S)),
    }


def initializeLogger():
    """One log file per camera: paths.logs + log.file_name, rotated at midnight."""
    log_file = os.path.join(cfg["logs_dir"], cfg["log_file_name"])
    logger.setLevel(logging.DEBUG)
    file_handler = handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=cfg["log_backup_count"])
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)

    # Startup summary: what this process will actually do. Never log the DB password.
    logger.info(
        f"Started pid={os.getpid()} worker={cfg['worker_id']} config={cfg['config_path']} "
        f"camera={cfg['camera_id']}/{cfg['side']} serial={cfg['serial']} fps={cfg['fps']} "
        f"exposure_us={cfg['exposure_us']} rotate_deg={cfg['rotate_deg']} "
        f"health_table={cfg['health_table']} health_interval_s={cfg['health_interval_s']} log={log_file}")


# ===========================================================================
# Small helpers: throttled logging and stop handling
# ===========================================================================
_last_logged = {}   # key -> (monotonic time of last log, repeats suppressed since)


def log_throttled(key, level, message):
    """Log a repeating problem at most once per LOG_REPEAT_S, with a count of the
    repeats in between. Prevents a dead camera or a full disk from flooding the log."""
    now = time.monotonic()
    last, suppressed = _last_logged.get(key, (None, 0))
    if last is None or now - last >= LOG_REPEAT_S:
        if suppressed:
            message += f" (repeated {suppressed} more times since the last log line)"
        logger.log(level, message)
        _last_logged[key] = (now, 0)
    else:
        _last_logged[key] = (last, suppressed + 1)


def _request_stop(signum, frame):
    # Only set a flag: the loop finishes the frame it is publishing, then stops.
    global _stop_requested
    _stop_requested = True


def install_signal_handlers():
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


def sleep_unless_stopped(seconds):
    """Sleep in short steps so a stop request is noticed quickly."""
    deadline = time.monotonic() + seconds
    while not _stop_requested and time.monotonic() < deadline:
        time.sleep(0.2)


# ===========================================================================
# Atomic frame hand-off (D-031). Unchanged from the tested version.
#
# Write the JPEG ONCE into the staging folder (_tmp, which no other process reads),
# then publish it with hard links / rename. Both are instant and atomic, so a reader
# only ever sees complete files.
# Requirement: _tmp, live, INF and UI must be on the SAME filesystem.
# ===========================================================================
def _link_or_copy(src, dst):
    """Hard-link src to dst (atomic, no data copied). Falls back to a plain copy
    only if hard links are impossible (e.g. different filesystem)."""
    global _copy_fallback_warned
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        # Only fall back when hard links are impossible; real errors (missing folder,
        # permissions, disk full) must surface, not be hidden by a copy.
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK):
            raise
        if not _copy_fallback_warned:
            logger.critical(f"os.link failed ({e}); falling back to non-atomic copy. "
                            f"Check that _tmp, INF and UI are on the same filesystem.")
            _copy_fallback_warned = True
        shutil.copy2(src, dst)


def prepare_directories():
    """Create the folders, clear stale staging files, warn if not on one filesystem."""
    dirs = {
        "tmp":  cfg["tmp_dir"],
        "live": cfg["live_dir"],
        "inf":  cfg["inf_dir"],
        "ui":   cfg["ui_dir"],
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    # Leftovers from a crash mid-write. Only this process writes to _tmp.
    for f in os.listdir(dirs["tmp"]):
        try:
            os.remove(os.path.join(dirs["tmp"], f))
        except OSError:
            pass

    tmp_dev = os.stat(dirs["tmp"]).st_dev
    for name in ("live", "inf", "ui"):
        if os.stat(dirs[name]).st_dev != tmp_dev:
            logger.critical(f"'{name}' folder is on a different filesystem than _tmp: "
                            f"atomic hand-off will NOT work.")


def publish_frame(img, stamp):
    """Write one frame once, then publish it to INF (queue), UI (mailbox) and live."""
    tmp_dir  = cfg["tmp_dir"]
    live_dir = cfg["live_dir"]
    inf_dir  = cfg["inf_dir"]
    ui_dir   = cfg["ui_dir"]

    name = f"{stamp}.jpg"
    tmp_path = os.path.join(tmp_dir, name)

    # The only real data write. Nobody reads _tmp, so a partial file is never seen.
    if not cv2.imwrite(tmp_path, img):
        raise IOError(f"cv2.imwrite failed for {tmp_path}")

    try:
        # 1) Inference queue: complete file appears under its final name at once.
        _link_or_copy(tmp_path, os.path.join(inf_dir, name))

        # 2) UI mailbox: only when the UI has taken the previous frame (folder empty).
        if len(os.listdir(ui_dir)) == 0:
            try:
                _link_or_copy(tmp_path, os.path.join(ui_dir, "IMG_1.jpg"))
            except FileExistsError:
                pass

        # 3) Live image LAST: atomic swap. This moves the staging name away, so it
        #    must come after the links above.
        os.replace(tmp_path, os.path.join(live_dir, "IMG_1.jpg"))
    except Exception:
        # Do not leave a stray file in _tmp (INF already holds a complete copy if step 1 ran).
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise

    return name


def rotate_frame(img, angle):
    """Rotate by the configured angle. 0 returns the frame untouched.
    The angle was validated when the config was loaded, so there is no per-frame check."""
    if angle == 0:
        return img
    return cv2.rotate(img, CV2_ROTATION[angle])


# ===========================================================================
# Health status in the DB (table name from config, D-038)
# ===========================================================================
def update_health_status(health, is_data_sync):
    """UPDATE the row of this camera (ITEM = serial). Never raises: a DB problem
    must not stop capture. Only updates; it does not create the row."""
    table = cfg["health_table"]          # validated at config load
    item = cfg["serial"]
    connection = None
    try:
        connection = pymysql.connect(
            host=cfg["db_host"], port=cfg["db_port"],
            user=cfg["db_user"], passwd=cfg["db_password"], db=cfg["db_name"],
            connect_timeout=DB_CONNECT_TIMEOUT_S,
            # F7: report rows MATCHED, not rows changed. Without this flag an UPDATE that
            # writes the same values returns 0 and looks like "no row for this camera".
            client_flag=CLIENT.FOUND_ROWS)
        with connection.cursor() as cursor:
            rows = cursor.execute(
                f"UPDATE `{table}` SET HEALTH = %s, IS_DATA_SYNC = %s, CREATE_DATETIME = NOW() "
                f"WHERE ITEM = %s",
                (health, int(is_data_sync), item))
        connection.commit()
        if rows == 0:
            log_throttled("health_no_row", logging.WARNING,
                          f"Health update matched no row: table {table} has no ITEM = '{item}'")
        else:
            logger.info(f"Health {health} written to {table} for ITEM {item}")
    except Exception as e:
        log_throttled("health_db_error", logging.ERROR, f"Health update to {table} failed: {e}")
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class HealthReporter:
    """Writes OK/NOK at once when the state changes, and repeats the same state
    at most every health_interval_s seconds (JSON key, default 600)."""

    def __init__(self):
        self.interval_s = cfg["health_interval_s"]
        self._last_state = None
        self._last_write = None

    def report(self, state):
        now = time.monotonic()
        unchanged = state == self._last_state
        recent = self._last_write is not None and now - self._last_write < self.interval_s
        if unchanged and recent:
            return
        self._last_state = state
        self._last_write = now
        update_health_status(state, 0)


# ===========================================================================
# Camera handling
# ===========================================================================
def open_camera(serial):
    """Find the camera by serial and open it. Raises if it is not reachable."""
    device_info = pylon.DeviceInfo()
    device_info.SetSerialNumber(serial)
    device = pylon.TlFactory.GetInstance().CreateFirstDevice(device_info)
    camera = pylon.InstantCamera(device)
    try:
        camera.Open()
    except Exception:
        close_camera(camera)
        raise
    return camera


def close_camera(camera):
    """Stop and release the camera. Safe on a half-open or removed camera."""
    if camera is None:
        return
    for label, step in (("StopGrabbing", camera.StopGrabbing),
                        ("Close", camera.Close),
                        ("DestroyDevice", camera.DestroyDevice)):
        try:
            step()
        except Exception as e:
            logger.debug(f"close_camera {label}: {e}")


def set_feature(camera, names, value, required):
    """Set the first available, writable feature in `names`. One log line either way.
    Several names cover older and newer Basler models (e.g. ExposureTimeAbs vs ExposureTime)."""
    for name in names:
        try:
            node = getattr(camera, name)
        except Exception:
            continue                      # this camera model has no feature with this name
        if not genicam.IsWritable(node):
            continue
        try:
            node.SetValue(value)
            logger.info(f"Camera setting {name} = {node.GetValue()} (requested {value})")
            return True
        except Exception as e:
            logger.error(f"Camera setting {name} = {value} FAILED: {e}")
            return False
    level = logging.ERROR if required else logging.WARNING
    logger.log(level, f"Camera setting {' / '.join(names)} not available or not writable; "
                      f"camera keeps its own value")
    return False


def apply_camera_settings(camera):
    """Same settings as before, now set before grabbing starts (AcquisitionMode cannot
    change while grabbing). The old *Abs names are tried first because they are what
    the tested cameras accepted; the newer names cover other models."""
    fps = cfg["fps"]
    exposure = cfg["exposure_us"]
    set_feature(camera, ["TriggerMode"], "Off", required=False)
    set_feature(camera, ["AcquisitionMode"], "Continuous", required=False)
    set_feature(camera, ["AcquisitionFrameRateEnable"], True, required=True)
    set_feature(camera, ["AcquisitionFrameRateAbs", "AcquisitionFrameRate"], fps, required=True)
    set_feature(camera, ["ExposureTimeAbs", "ExposureTime"], exposure, required=True)
    set_feature(camera, ["BalanceWhiteAuto"], "Continuous", required=False)   # colour models only


def make_converter():
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment.Value = pylon.OutputBitAlignment_MsbAligned   # newer pypylon syntax
    return converter


def retrieve_timeout_ms():
    frame_period_ms = 1000.0 / cfg["fps"]
    return max(RETRIEVE_TIMEOUT_MIN_MS, int(3 * frame_period_ms))


class BaslerCam:
    """Opens the camera, grabs until it is lost, reopens it. Runs until a stop is requested."""

    STOPPED = "stopped"
    LOST = "lost"

    def __init__(self):
        self.serial = cfg["serial"]
        self.rotate_deg = cfg["rotate_deg"]
        self.health = HealthReporter()
        self.frame_count = 0

    def run(self):
        failed_attempts = 0
        was_lost = False
        while not _stop_requested:
            camera = None
            outcome = self.LOST
            try:
                camera = open_camera(self.serial)
                apply_camera_settings(camera)
                camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
                if was_lost:
                    logger.critical(f"Camera {self.serial} REOPENED after {failed_attempts} "
                                    f"failed attempt(s); grabbing again")
                else:
                    logger.info(f"Camera {self.serial} opened; grabbing")
                failed_attempts = 0
                outcome = self.grab_until_lost(camera)
            except Exception as e:
                failed_attempts += 1
                log_throttled("open_failed", logging.ERROR,
                              f"Camera {self.serial} open attempt {failed_attempts} failed: {e}")
            finally:
                close_camera(camera)

            if outcome == self.LOST and not _stop_requested:
                was_lost = True
                self.health.report("NOK")
                sleep_unless_stopped(REOPEN_RETRY_S)

        logger.info(f"Camera {self.serial} stopped on request after {self.frame_count} frames")

    def grab_until_lost(self, camera):
        """Publish frames until the camera is lost (returns LOST) or a stop is requested (STOPPED)."""
        converter = make_converter()
        timeout_ms = retrieve_timeout_ms()
        last_good_frame = time.monotonic()

        while not _stop_requested:
            if camera.IsCameraDeviceRemoved():
                logger.critical(f"Camera {self.serial} LOST: device removed")
                return self.LOST
            if time.monotonic() - last_good_frame > NO_FRAME_REOPEN_S:
                logger.critical(f"Camera {self.serial} LOST: no good frame for "
                                f"{NO_FRAME_REOPEN_S} s; reopening")
                return self.LOST

            grab_result = None   # fresh every iteration: an older grab is never republished
            try:
                grab_result = camera.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
                if not grab_result.IsValid():
                    log_throttled("grab_timeout", logging.WARNING,
                                  f"No frame from camera {self.serial} within {timeout_ms} ms")
                    continue
                if not grab_result.GrabSucceeded():
                    log_throttled("grab_failed", logging.WARNING,
                                  f"Grab failed on camera {self.serial}: "
                                  f"0x{grab_result.GetErrorCode():08X} {grab_result.GetErrorDescription()}")
                    continue
                # Convert() copies the pixels, so the grab can be released right after.
                img = converter.Convert(grab_result).GetArray()
                last_good_frame = time.monotonic()
            except Exception as e:
                if camera.IsCameraDeviceRemoved():
                    logger.critical(f"Camera {self.serial} LOST during grab: {e}")
                    return self.LOST
                log_throttled("grab_error", logging.ERROR,
                              f"Grab error on camera {self.serial}: {traceback.format_exc()}")
                continue
            finally:
                if grab_result is not None:
                    try:
                        grab_result.Release()
                    except Exception:
                        pass

            self.publish(img)

        return self.STOPPED

    def publish(self, img):
        """Rotate, hand off, record health. A disk or folder problem is logged but is
        not a camera problem, so it never triggers a reopen."""
        start = time.monotonic()
        try:
            img = rotate_frame(img, self.rotate_deg)
            name = publish_frame(img, datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f"))
        except Exception:
            log_throttled("publish_error", logging.ERROR,
                          f"Publishing a frame failed: {traceback.format_exc()}")
            return
        self.frame_count += 1
        logger.info(f"Frame {name} published in {int((time.monotonic() - start) * 1000)} ms")
        self.health.report("OK")


def main():
    path = os.environ.get(CONFIG_ENV, "").strip()
    if not path:
        print(f"FrameCapture_Basler: cannot start: env {CONFIG_ENV} is not set. The Supervisor sets it; "
              f"to run by hand: {CONFIG_ENV}=<root>/config/rendered/frame_capture_cam1.json "
              f"python3 FrameCapture_Basler.py", file=sys.stderr)
        sys.exit(2)
    try:
        cfg.update(load_worker_config(path))
        initializeLogger()
    except Exception as e:
        # Exit non-zero so the Supervisor sees a failure; the message names the problem
        # (never a value: the file holds the DB password).
        print(f"FrameCapture_Basler: cannot start: {path}: {e}", file=sys.stderr)
        sys.exit(2)

    install_signal_handlers()
    prepare_directories()     # create folders, clear stale _tmp, check filesystem
    BaslerCam().run()


if __name__ == "__main__":
    main()
