"""
FrameCapture_Basler.py: single-camera Basler capture for the DPC platform.

One process per camera (D-033). Config is a positional XML file (CONTRACTS section 3,
"BASLER_CONFIG1.xml"), chosen by the camera number on the command line:
    python3 FrameCapture_Basler.py 1    ->  <root>/config/BASLER_CONFIG1.xml Every frame is
handed off atomically to INF, UI and live (D-031).

Robustness (STATUS item 2):
  - the camera is reopened after device-lost, or after a long stretch without a good frame,
    without restarting the process;
  - only a grab retrieved in the current loop iteration is ever published;
  - health table, log file name and rotation come from config (D-038, D-041);
  - each camera setting is set by feature name, with one log line per setting;
  - SIGTERM/SIGINT finish the current frame and stop cleanly (so _tmp is left empty).
"""
import errno
import logging
import os
import re
import shutil
import signal
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from logging import handlers

import cv2
import pymysql
from pypylon import genicam, pylon

# ---------------------------------------------------------------------------
# Timing constants. These are the same on every site, so they are not config.
# ---------------------------------------------------------------------------
RETRIEVE_TIMEOUT_MIN_MS = 5000   # wait at most this long for one frame (or 3 frame periods if longer)
NO_FRAME_REOPEN_S = 15           # no good frame for this long -> treat the camera as lost and reopen it
REOPEN_RETRY_S = 2               # pause between reopen attempts (keeps recovery well under 30 s)
HEALTH_INTERVAL_S = 600          # repeat the same health state at most this often (same as the old code)
LOG_REPEAT_S = 60                # a repeating problem is logged at most once per this period
DB_CONNECT_TIMEOUT_S = 3         # a DB outage must not stall capture for long

# ---------------------------------------------------------------------------
# Defaults for the optional, append-only XML elements (D-041).
# They reproduce the behaviour before D-041, so existing config files keep working.
# NOTE: the default health table name is site-specific; it stays only for backward
# compatibility. Every site should set HEALTH_TABLE explicitly (D-038).
# ---------------------------------------------------------------------------
DEFAULT_HEALTH_TABLE = "tadipatri_health_status_table"
DEFAULT_LOG_FILE_NAME = "FrameCapture_Basler.log"
DEFAULT_ROTATE_DEG = 90

CV2_ROTATION = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}
VALID_ROTATIONS = (0,) + tuple(CV2_ROTATION)

configHashMap = {}
logger = logging.getLogger("FrameCapture_Basler")
_copy_fallback_warned = False
_stop_requested = False


class CONFIG_KEY_NAME:
    CODE_PATH = "CODE_PATH"
    LOG_FILE_PATH = "LOG_FILE_PATH"
    LOG_BACKUP_COUNT = "LOG_BACKUP_COUNT"
    CAM_SERIAL_NO = "CAM_SERIAL_NO"
    CAM_NAME = "CAM_NAME"
    EXPOSURE = "EXPOSURE"
    FRAME_RATE = "FRAME_RATE"
    SAVE_LIVE_IMG_PATH = "SAVE_LIVE_IMG_PATH"
    UI_PATH = "UI_PATH"
    INF_PATH = "INF_PATH"
    RAW_IMG_PATH = "RAW_IMG_PATH"
    DB_USER = "DB_USER"
    DB_PASS = "DB_PASS"
    DB_HOST = "DB_HOST"
    DB_NAME = "DB_NAME"
    TMP_IMG_PATH = "TMP_IMG_PATH"      # position 15, optional (D-031)
    HEALTH_TABLE = "HEALTH_TABLE"      # position 16, optional (D-041)
    LOG_FILE_NAME = "LOG_FILE_NAME"    # position 17, optional (D-041)
    ROTATE_DEG = "ROTATE_DEG"          # position 18, optional (D-041)


# ===========================================================================
# Configuration
# ===========================================================================
def _text(node):
    """XML element text, stripped (None becomes empty string)."""
    return (node.text or "").strip()


def _optional(config_root, position, default):
    """Text of an optional, append-only element. Missing or empty means default.
    The file is read by position, so an empty element can be used as a placeholder
    when only a later element needs a value."""
    if len(config_root) > position:
        value = _text(config_root[position])
        if value:
            return value
    return default


def _validated_table_name(name):
    # A table name cannot be a SQL parameter, so it is checked strictly before use.
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"HEALTH_TABLE '{name}' may only contain letters, digits and '_'")
    return name


def _validated_rotation(text):
    try:
        angle = int(text)
    except ValueError:
        raise ValueError(f"ROTATE_DEG must be one of {VALID_ROTATIONS}, got '{text}'")
    if angle not in VALID_ROTATIONS:
        raise ValueError(f"ROTATE_DEG must be one of {VALID_ROTATIONS}, got {angle}")
    return angle


def _validated_log_file_name(name):
    if os.path.basename(name) != name:
        raise ValueError(f"LOG_FILE_NAME '{name}' must be a file name, not a path")
    return name


def deployed_config_dir():
    """<root>/config, found from where this script sits in the deployed layout
    (<root>/code/workers/frame_capture_basler/FrameCapture_Basler.py, CONTRACTS 4.1).
    No home folder or user name is hardcoded."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    return os.path.join(root, "config")


def config_file_path():
    """Which camera config to read, first match wins:
    1. command-line argument: a camera number ("1" -> <root>/config/BASLER_CONFIG1.xml)
       or a path to an XML file;
    2. env BASLER_CONFIG (kept so existing start commands keep working);
    3. BASLER_CONFIG1.xml in the working directory (the old behaviour)."""
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        if arg.isdigit():
            return os.path.join(deployed_config_dir(), f"BASLER_CONFIG{arg}.xml")
        return os.path.abspath(os.path.expanduser(arg))
    if os.environ.get("BASLER_CONFIG"):
        return os.environ["BASLER_CONFIG"]
    return os.path.join(os.getcwd(), "BASLER_CONFIG1.xml")


def loadConfiguration():
    """Read the positional XML into configHashMap. Raises on any problem: a capture
    process with a broken config must stop with a clear message, not run half-configured."""
    config_root = ET.parse(config_file_path()).getroot()

    configHashMap[CONFIG_KEY_NAME.CODE_PATH] = _text(config_root[0])
    configHashMap[CONFIG_KEY_NAME.LOG_FILE_PATH] = _text(config_root[1])
    configHashMap[CONFIG_KEY_NAME.LOG_BACKUP_COUNT] = int(_text(config_root[2]))
    configHashMap[CONFIG_KEY_NAME.CAM_SERIAL_NO] = _text(config_root[3])
    configHashMap[CONFIG_KEY_NAME.CAM_NAME] = _text(config_root[4])
    configHashMap[CONFIG_KEY_NAME.EXPOSURE] = int(_text(config_root[5]))
    configHashMap[CONFIG_KEY_NAME.FRAME_RATE] = float(_text(config_root[6]))
    configHashMap[CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH] = _text(config_root[7])
    configHashMap[CONFIG_KEY_NAME.UI_PATH] = _text(config_root[8])
    configHashMap[CONFIG_KEY_NAME.INF_PATH] = _text(config_root[9])
    configHashMap[CONFIG_KEY_NAME.RAW_IMG_PATH] = _text(config_root[10])
    configHashMap[CONFIG_KEY_NAME.DB_USER] = _text(config_root[11])
    configHashMap[CONFIG_KEY_NAME.DB_PASS] = _text(config_root[12])
    configHashMap[CONFIG_KEY_NAME.DB_HOST] = _text(config_root[13])
    configHashMap[CONFIG_KEY_NAME.DB_NAME] = _text(config_root[14])

    # 15: staging folder. Default: a "_tmp" folder next to the live folder (.../CAMn/live -> .../CAMn/_tmp).
    live_dir = configHashMap[CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH].rstrip("/")
    configHashMap[CONFIG_KEY_NAME.TMP_IMG_PATH] = _optional(
        config_root, 15, os.path.join(os.path.dirname(live_dir), "_tmp"))

    # 16 to 18: D-041.
    configHashMap[CONFIG_KEY_NAME.HEALTH_TABLE] = _validated_table_name(
        _optional(config_root, 16, DEFAULT_HEALTH_TABLE))
    configHashMap[CONFIG_KEY_NAME.LOG_FILE_NAME] = _validated_log_file_name(
        _optional(config_root, 17, DEFAULT_LOG_FILE_NAME))
    configHashMap[CONFIG_KEY_NAME.ROTATE_DEG] = _validated_rotation(
        _optional(config_root, 18, str(DEFAULT_ROTATE_DEG)))

    if not configHashMap[CONFIG_KEY_NAME.CAM_SERIAL_NO]:
        raise ValueError("CAM_SERIAL_NO (position 3) is empty")
    if configHashMap[CONFIG_KEY_NAME.FRAME_RATE] <= 0:
        raise ValueError("FRAME_RATE (position 6) must be greater than 0")


def initializeLogger():
    """One log file per camera: LOG_FILE_PATH + LOG_FILE_NAME, rotated at midnight."""
    log_file = os.path.join(configHashMap[CONFIG_KEY_NAME.LOG_FILE_PATH],
                            configHashMap[CONFIG_KEY_NAME.LOG_FILE_NAME])
    logger.setLevel(logging.DEBUG)
    file_handler = handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=configHashMap[CONFIG_KEY_NAME.LOG_BACKUP_COUNT])
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)

    # Startup summary: what this process will actually do. Never log the DB password.
    logger.info(
        f"Started pid={os.getpid()} config={config_file_path()} "
        f"camera='{configHashMap[CONFIG_KEY_NAME.CAM_NAME]}' "
        f"serial={configHashMap[CONFIG_KEY_NAME.CAM_SERIAL_NO]} "
        f"fps={configHashMap[CONFIG_KEY_NAME.FRAME_RATE]} "
        f"exposure_us={configHashMap[CONFIG_KEY_NAME.EXPOSURE]} "
        f"rotate_deg={configHashMap[CONFIG_KEY_NAME.ROTATE_DEG]} "
        f"health_table={configHashMap[CONFIG_KEY_NAME.HEALTH_TABLE]} log={log_file}")


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
        "tmp":  configHashMap.get(CONFIG_KEY_NAME.TMP_IMG_PATH),
        "live": configHashMap.get(CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH),
        "inf":  configHashMap.get(CONFIG_KEY_NAME.INF_PATH),
        "ui":   configHashMap.get(CONFIG_KEY_NAME.UI_PATH),
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
    tmp_dir  = configHashMap.get(CONFIG_KEY_NAME.TMP_IMG_PATH)
    live_dir = configHashMap.get(CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH)
    inf_dir  = configHashMap.get(CONFIG_KEY_NAME.INF_PATH)
    ui_dir   = configHashMap.get(CONFIG_KEY_NAME.UI_PATH)

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
    table = configHashMap[CONFIG_KEY_NAME.HEALTH_TABLE]   # validated at config load
    item = configHashMap[CONFIG_KEY_NAME.CAM_SERIAL_NO]
    connection = None
    try:
        connection = pymysql.connect(
            host=configHashMap[CONFIG_KEY_NAME.DB_HOST],
            user=configHashMap[CONFIG_KEY_NAME.DB_USER],
            passwd=configHashMap[CONFIG_KEY_NAME.DB_PASS],
            db=configHashMap[CONFIG_KEY_NAME.DB_NAME],
            connect_timeout=DB_CONNECT_TIMEOUT_S)
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
    at most every HEALTH_INTERVAL_S (the old code's 600 s rhythm)."""

    def __init__(self):
        self._last_state = None
        self._last_write = None

    def report(self, state):
        now = time.monotonic()
        unchanged = state == self._last_state
        recent = self._last_write is not None and now - self._last_write < HEALTH_INTERVAL_S
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
    fps = configHashMap[CONFIG_KEY_NAME.FRAME_RATE]
    exposure = float(configHashMap[CONFIG_KEY_NAME.EXPOSURE])
    set_feature(camera, ["TriggerMode"], "Off", required=False)
    set_feature(camera, ["AcquisitionMode"], "Continuous", required=False)
    set_feature(camera, ["AcquisitionFrameRateEnable"], True, required=True)
    set_feature(camera, ["AcquisitionFrameRateAbs", "AcquisitionFrameRate"], fps, required=True)
    set_feature(camera, ["ExposureTimeAbs", "ExposureTime"], exposure, required=True)
    set_feature(camera, ["BalanceWhiteAuto"], "Continuous", required=False)   # colour models only


def make_converter():
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned
    return converter


def retrieve_timeout_ms():
    frame_period_ms = 1000.0 / configHashMap[CONFIG_KEY_NAME.FRAME_RATE]
    return max(RETRIEVE_TIMEOUT_MIN_MS, int(3 * frame_period_ms))


class BaslerCam:
    """Opens the camera, grabs until it is lost, reopens it. Runs until a stop is requested."""

    STOPPED = "stopped"
    LOST = "lost"

    def __init__(self):
        self.serial = configHashMap[CONFIG_KEY_NAME.CAM_SERIAL_NO]
        self.rotate_deg = configHashMap[CONFIG_KEY_NAME.ROTATE_DEG]
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
    try:
        loadConfiguration()
        initializeLogger()
    except Exception as e:
        # Exit non-zero so the Supervisor sees a failure; the message names the problem.
        print(f"FrameCapture_Basler: cannot start: {e}", file=sys.stderr)
        sys.exit(2)

    install_signal_handlers()
    prepare_directories()     # create folders, clear stale _tmp, check filesystem
    BaslerCam().run()


if __name__ == "__main__":
    main()
