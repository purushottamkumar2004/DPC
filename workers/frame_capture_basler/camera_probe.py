"""camera_probe.py: find the cameras on this machine and grab one image, without the capture
program (STATUS S4, D-103 wizard step 3 "cameras").

Functions (for the UI and the setup wizard):
    list_all()                              every driver: Basler (pylon) and FLIR (Spinnaker)
    list_cameras()                          Basler only
    grab_one(serial, out_path, exposure_us=None, timeout_ms=5000, driver="auto")
    burst(serial, out_dir, count=20, interval_ms=250, exposure_us=None, timeout_ms=5000, driver="auto")

CLI (stdout = the result; --json for programs):
    python3 camera_probe.py list [--driver all|basler|flir] [--json]
    python3 camera_probe.py grab SERIAL OUT_FILE [--exposure US] [--timeout-ms MS]
                                 [--driver auto|basler|flir] [--json]
    python3 camera_probe.py burst SERIAL OUT_DIR [--count 20] [--interval-ms 250] [--exposure US]
                                  [--timeout-ms MS] [--driver auto|basler|flir] [--json]

A burst writes COUNT frames close in time into OUT_DIR (created if missing), named by capture time
like the capture's frames (CONTRACTS 4.1), so `measure.py --frames OUT_DIR --count COUNT` reads it
for the conveyor direction (F89). If a frame fails mid-burst, the frames already saved stay and are
listed in the result (status failed).
Exit codes: 0 ok, 1 failed (SDK missing, open or grab failed), 2 bad request,
            3 camera in use by another program, 4 serial not found.

A camera that another program (normally the running capture) holds is reported as in use;
it is never forced. Stop that program first, e.g. through the Supervisor.
An exposure given here is set in the camera's working memory only (lost at power-off); the
capture program sets its own exposure (and frame rate, which a burst unlocks) when it starts,
so nothing here changes production.
The FLIR side lives in flir_probe.py with the same functions and result format.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

DEFAULT_TIMEOUT_MS = 5000
DEFAULT_BURST_COUNT = 20
DEFAULT_BURST_INTERVAL_MS = 250
MAX_BURST_COUNT = 200            # a burst is for a short look, not for recording
MAX_BURST_INTERVAL_MS = 60000
FRAME_NAME_FORMAT = "%Y_%m_%d_%H_%M_%S_%f"   # the capture's frame names (CONTRACTS 4.1)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp")

# Result status values (CLI exit code in brackets).
OK = "ok"                    # (0)
FAILED = "failed"            # (1) open or grab failed for another reason; see message
SDK_MISSING = "sdk_missing"  # (1)
BAD_REQUEST = "bad_request"  # (2)
IN_USE = "in_use"            # (3)
NOT_FOUND = "not_found"      # (4)
EXIT_CODES = {OK: 0, FAILED: 1, SDK_MISSING: 1, BAD_REQUEST: 2, IN_USE: 3, NOT_FOUND: 4}

BASLER_SDK_MISSING = "Basler SDK not installed (pylon + pypylon)"
IN_USE_MESSAGE = ("camera {serial} is in use by another program (normally the running capture); "
                  "stop that program first, e.g. through the Supervisor")

# Words that SDK errors use when another program holds the camera
# (pylon: "controlled by another application", 0xE1018006; Spinnaker: "Access Denied", -1005).
_IN_USE_HINTS = ("controlled by another application", "0xe1018006", "access denied", "-1005",
                 "exclusive", "already opened", "already open", "busy", "in use")


# ===========================================================================
# Shared helpers (also used by flir_probe.py)
# ===========================================================================
def looks_in_use(error_text):
    """True when an SDK error says another program holds the camera."""
    text = str(error_text).lower()
    return any(hint in text for hint in _IN_USE_HINTS)


def list_result(driver, sdk_installed, cameras=(), message=None):
    return {"driver": driver, "sdk_installed": sdk_installed, "message": message, "cameras": list(cameras)}


def camera_entry(driver, serial, model, ip, in_use, device_class=None, name=None):
    """One camera in a list result. in_use: True / False, or None when the SDK cannot tell."""
    return {"driver": driver, "serial": str(serial), "model": model, "ip": ip,
            "in_use": in_use, "device_class": device_class, "name": name}


def grab_result(status, driver=None, serial=None, message=None, **fields):
    result = {"ok": status == OK, "status": status, "driver": driver,
              "serial": None if serial is None else str(serial), "message": message,
              "path": None, "model": None, "width": None, "height": None, "exposure_us": None}
    result.update(fields)
    return result


def check_output_path(out_path):
    """None when the file can be written, else the reason (checked before the camera is touched)."""
    if not out_path:
        return "no output file given"
    if os.path.splitext(out_path)[1].lower() not in IMAGE_EXTENSIONS:
        return f"output file must end in one of {', '.join(IMAGE_EXTENSIONS)}"
    folder = os.path.dirname(os.path.abspath(out_path))
    if not os.path.isdir(folder):
        return f"folder {folder} does not exist"
    return None


def burst_result(status, driver=None, serial=None, message=None, frames=(), **fields):
    """A grab result plus the burst's folder and frames ([{index, path, t_ms}], oldest first)."""
    result = grab_result(status, driver, serial, message, **fields)
    result["frames"] = list(frames)
    result["saved"] = len(result["frames"])
    return result


def frame_path(out_dir, previous=None):
    """<out_dir>/<capture time>.jpg, always later than `previous` (a datetime), so names sort in
    capture order even if the clock gives the same microsecond twice. Returns (path, time)."""
    stamp = datetime.now()
    if previous is not None and stamp <= previous:
        stamp = previous + timedelta(microseconds=1)
    return os.path.join(out_dir, stamp.strftime(FRAME_NAME_FORMAT) + ".jpg"), stamp


def wait_until(deadline):
    """Sleep until time.monotonic() reaches deadline (returns at once if it already has)."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def write_image(img, out_path):
    """Write next to the target, then rename: a reader (the UI) never sees half a file."""
    import cv2
    out_path = os.path.abspath(out_path)
    root, ext = os.path.splitext(out_path)
    tmp = f"{root}.tmp-{os.getpid()}{ext}"
    try:
        if not cv2.imwrite(tmp, img):
            raise IOError(f"could not write {out_path}")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out_path


# ===========================================================================
# Basler (pylon)
# ===========================================================================
def _load_pylon():
    """(pylon, genicam) or None when the SDK is not installed. Imported here, not at the top,
    so this file also runs (and lists FLIR cameras) on a machine without pylon."""
    try:
        from pypylon import genicam, pylon
    except ImportError:
        return None
    return pylon, genicam


def _info(device_info, getter):
    try:
        value = getattr(device_info, getter)()
        return value if value not in ("", None) else None
    except Exception:
        return None


def _basler_ip(device_info):
    try:
        if device_info.IsIpAddressAvailable():
            return device_info.GetIpAddress() or None
    except Exception:
        pass
    return None


def list_cameras():
    """Every Basler camera pylon can see, with in_use = another program holds it."""
    sdk = _load_pylon()
    if sdk is None:
        return list_result("basler", False, message=BASLER_SDK_MISSING)
    pylon, _ = sdk
    try:
        factory = pylon.TlFactory.GetInstance()
        cameras = []
        for info in factory.EnumerateDevices():
            try:
                in_use = not factory.IsDeviceAccessible(info)
            except Exception:
                in_use = None
            cameras.append(camera_entry("basler", _info(info, "GetSerialNumber"), _info(info, "GetModelName"),
                                        _basler_ip(info), in_use, _info(info, "GetDeviceClass"),
                                        _info(info, "GetUserDefinedName")))
        return list_result("basler", True, cameras)
    except Exception as exc:
        return list_result("basler", True, message=f"listing Basler cameras failed: {exc}")


def _set_writable(camera, genicam, names, value):
    """Set the first writable feature in names; returns the value the camera reports, or None."""
    for name in names:
        try:
            node = getattr(camera, name)
        except Exception:
            continue
        if not genicam.IsWritable(node):
            continue
        node.SetValue(value)
        return node.GetValue()
    return None


def _find_basler(factory, serial):
    for info in factory.EnumerateDevices():
        if _info(info, "GetSerialNumber") == str(serial):
            return info
    return None


def _basler_settings(camera, genicam, exposure_us, unlock_frame_rate=False):
    """Free-running; optionally manual exposure and no frame-rate limit (a burst wants a fresh
    frame at every tick). Returns (exposure the camera reports or None, error text or None)."""
    _set_writable(camera, genicam, ["TriggerMode"], "Off")         # free-running, as the capture runs it
    if unlock_frame_rate:
        _set_writable(camera, genicam, ["AcquisitionFrameRateEnable"], False)
    if exposure_us is None:
        return None, None
    _set_writable(camera, genicam, ["ExposureAuto"], "Off")
    applied = _set_writable(camera, genicam, ["ExposureTimeAbs", "ExposureTime"], float(exposure_us))
    if applied is None:
        return None, "this camera does not accept an exposure value"
    return float(applied), None


def _with_basler(serial, make_result, work):
    """Find, check and open the Basler camera, run work(pylon, genicam, camera, model), close it.
    Shared by grab and burst so both say "in use" the same way. make_result(status, message, **fields)
    builds the operation's result for every failure before work runs."""
    sdk = _load_pylon()
    if sdk is None:
        return make_result(SDK_MISSING, BASLER_SDK_MISSING)
    pylon, genicam = sdk
    factory = pylon.TlFactory.GetInstance()
    info = _find_basler(factory, serial)
    if info is None:
        return make_result(NOT_FOUND, f"no Basler camera with serial {serial}")
    model = _info(info, "GetModelName")
    in_use = make_result(IN_USE, IN_USE_MESSAGE.format(serial=serial), model=model)
    try:
        if not factory.IsDeviceAccessible(info):
            return in_use
    except Exception:
        pass                                 # cannot tell: opening will show it

    camera = None
    try:
        try:
            camera = pylon.InstantCamera(factory.CreateDevice(info))
            camera.Open()
        except Exception as exc:
            if looks_in_use(exc):
                return in_use
            return make_result(FAILED, f"could not open camera {serial}: {exc}", model=model)
        return work(pylon, genicam, camera, model)
    except Exception as exc:
        if looks_in_use(exc):
            return in_use
        return make_result(FAILED, f"camera {serial}: {exc}", model=model)
    finally:
        if camera is not None:
            for step in ("StopGrabbing", "Close", "DestroyDevice"):
                try:
                    getattr(camera, step)()
                except Exception:
                    pass


def _bgr_converter(pylon):
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment.Value = pylon.OutputBitAlignment_MsbAligned
    return converter


def grab_basler(serial, out_path, exposure_us=None, timeout_ms=DEFAULT_TIMEOUT_MS):
    """One image from the Basler camera with this serial, written to out_path."""
    def result(status, message, **fields):
        return grab_result(status, "basler", serial, message, **fields)

    def work(pylon, genicam, camera, model):
        applied, problem = _basler_settings(camera, genicam, exposure_us)
        if problem:
            return result(FAILED, problem, model=model)
        grab = camera.GrabOne(int(timeout_ms))
        try:
            if not grab.GrabSucceeded():
                return result(FAILED, f"grab failed: {grab.GetErrorDescription()}", model=model)
            img = _bgr_converter(pylon).Convert(grab).GetArray()
        finally:
            grab.Release()
        path = write_image(img, out_path)
        return result(OK, None, path=path, model=model, width=int(img.shape[1]), height=int(img.shape[0]),
                      exposure_us=applied)

    return _with_basler(serial, result, work)


def burst_basler(serial, out_dir, count, interval_ms, exposure_us=None, timeout_ms=DEFAULT_TIMEOUT_MS):
    """count frames, one every interval_ms, from the Basler camera with this serial into out_dir."""
    frames = []
    base = {"path": None, "dir": os.path.abspath(out_dir), "count": count, "interval_ms": interval_ms}

    def result(status, message, **fields):
        return burst_result(status, "basler", serial, message, frames, **{**base, **fields})

    def work(pylon, genicam, camera, model):
        applied, problem = _basler_settings(camera, genicam, exposure_us, unlock_frame_rate=True)
        if problem:
            return result(FAILED, problem, model=model)
        converter = _bgr_converter(pylon)
        camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)   # each tick takes the newest frame
        start, previous, size = time.monotonic(), None, (None, None)
        for index in range(count):
            wait_until(start + index * interval_ms / 1000.0)
            grab = camera.RetrieveResult(int(timeout_ms), pylon.TimeoutHandling_Return)
            if not grab.IsValid():
                return result(FAILED, f"frame {index + 1} of {count}: no frame within {timeout_ms} ms",
                              model=model, exposure_us=applied, width=size[0], height=size[1])
            try:
                if not grab.GrabSucceeded():
                    return result(FAILED, f"frame {index + 1} of {count}: {grab.GetErrorDescription()}",
                                  model=model, exposure_us=applied, width=size[0], height=size[1])
                img = converter.Convert(grab).GetArray()
            finally:
                grab.Release()
            taken_ms = round((time.monotonic() - start) * 1000)
            path, previous = frame_path(out_dir, previous)
            frames.append({"index": index, "path": write_image(img, path), "t_ms": taken_ms})
            size = (int(img.shape[1]), int(img.shape[0]))
        return result(OK, None, model=model, exposure_us=applied, width=size[0], height=size[1])

    return _with_basler(serial, result, work)


# ===========================================================================
# All drivers
# ===========================================================================
def _flir():
    """The FLIR module (same folder); its functions report a missing SDK themselves."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import flir_probe
    return flir_probe


def list_all(drivers=("basler", "flir")):
    """{"cameras": every camera of every driver, "drivers": one list result per driver}."""
    per_driver = []
    if "basler" in drivers:
        per_driver.append(list_cameras())
    if "flir" in drivers:
        per_driver.append(_flir().list_cameras())
    return {"cameras": [cam for result in per_driver for cam in result["cameras"]], "drivers": per_driver}


def _check_request(serial, exposure_us, timeout_ms, driver):
    """(serial, exposure_us, timeout_ms, None) cleaned up, or (..., problem text) for a bad request."""
    serial = str(serial).strip() if serial is not None else ""
    if not serial:
        return serial, exposure_us, timeout_ms, "no serial given"
    if driver not in ("auto", "basler", "flir"):
        return serial, exposure_us, timeout_ms, f"unknown driver '{driver}'"
    try:
        if exposure_us is not None:
            if isinstance(exposure_us, bool) or float(exposure_us) <= 0:
                raise ValueError
            exposure_us = float(exposure_us)
    except (TypeError, ValueError):
        return serial, exposure_us, timeout_ms, "exposure must be a number above 0 microseconds"
    try:
        if isinstance(timeout_ms, bool) or int(timeout_ms) <= 0:
            raise ValueError
        timeout_ms = int(timeout_ms)
    except (TypeError, ValueError):
        return serial, exposure_us, timeout_ms, "timeout must be a whole number of ms above 0"
    return serial, exposure_us, timeout_ms, None


def _by_driver(driver, serial, basler_call, flir_call, not_found_result):
    """Run on the named driver, or with "auto" try Basler, then FLIR."""
    if driver == "basler":
        return basler_call()
    if driver == "flir":
        return flir_call()
    tried = []
    for name, call in (("basler", basler_call), ("flir", flir_call)):
        result = call()
        if result["status"] not in (NOT_FOUND, SDK_MISSING):
            return result
        tried.append(f"{name}: {result['message']}")
    return not_found_result(f"camera {serial} not found ({'; '.join(tried)})")


def grab_one(serial, out_path, exposure_us=None, timeout_ms=DEFAULT_TIMEOUT_MS, driver="auto"):
    """One image from the camera with this serial. driver "auto" looks in Basler, then FLIR."""
    serial, exposure_us, timeout_ms, problem = _check_request(serial, exposure_us, timeout_ms, driver)
    problem = problem or check_output_path(out_path)
    if problem:
        return grab_result(BAD_REQUEST, None, serial or None, problem)
    return _by_driver(driver, serial,
                      lambda: grab_basler(serial, out_path, exposure_us, timeout_ms),
                      lambda: _flir().grab_one(serial, out_path, exposure_us, timeout_ms),
                      lambda message: grab_result(NOT_FOUND, None, serial, message))


def _check_burst(out_dir, count, interval_ms):
    """(count, interval_ms, None) or (..., problem). Creates out_dir when its parent exists."""
    try:
        if isinstance(count, bool) or not 1 <= int(count) <= MAX_BURST_COUNT:
            raise ValueError
        count = int(count)
    except (TypeError, ValueError):
        return count, interval_ms, f"count must be a whole number from 1 to {MAX_BURST_COUNT}"
    try:
        if isinstance(interval_ms, bool) or not 0 <= int(interval_ms) <= MAX_BURST_INTERVAL_MS:
            raise ValueError
        interval_ms = int(interval_ms)
    except (TypeError, ValueError):
        return count, interval_ms, f"interval must be a whole number of ms from 0 to {MAX_BURST_INTERVAL_MS}"
    if not out_dir:
        return count, interval_ms, "no output folder given"
    out_dir = os.path.abspath(out_dir)
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        return count, interval_ms, f"{out_dir} exists and is not a folder"
    if not os.path.isdir(os.path.dirname(out_dir)):
        return count, interval_ms, f"folder {os.path.dirname(out_dir)} does not exist"
    os.makedirs(out_dir, exist_ok=True)
    return count, interval_ms, None


def burst(serial, out_dir, count=DEFAULT_BURST_COUNT, interval_ms=DEFAULT_BURST_INTERVAL_MS,
          exposure_us=None, timeout_ms=DEFAULT_TIMEOUT_MS, driver="auto"):
    """count frames, one every interval_ms, into out_dir, named by capture time (for measure.py)."""
    serial, exposure_us, timeout_ms, problem = _check_request(serial, exposure_us, timeout_ms, driver)
    if not problem:
        count, interval_ms, problem = _check_burst(out_dir, count, interval_ms)
    if problem:
        return burst_result(BAD_REQUEST, None, serial or None, problem)
    return _by_driver(driver, serial,
                      lambda: burst_basler(serial, out_dir, count, interval_ms, exposure_us, timeout_ms),
                      lambda: _flir().burst(serial, out_dir, count, interval_ms, exposure_us, timeout_ms),
                      lambda message: burst_result(NOT_FOUND, None, serial, message))


# ===========================================================================
# CLI
# ===========================================================================
def _print_list(result):
    for driver in result["drivers"]:
        if not driver["sdk_installed"] or driver["message"]:
            print(f"{driver['driver']}: {driver['message']}")
        elif not driver["cameras"]:
            print(f"{driver['driver']}: no cameras found")
    for cam in result["cameras"]:
        state = {True: "IN USE", False: "free", None: "unknown"}[cam["in_use"]]
        print(f"{cam['driver']:7} serial {cam['serial']:12} {cam['model'] or '?':24} "
              f"ip {cam['ip'] or '-':15} {state}")


def _print_grab(result):
    if result["ok"]:
        exposure = "" if result["exposure_us"] is None else f", exposure {result['exposure_us']:g} us"
        print(f"saved {result['path']} ({result['width']}x{result['height']}{exposure}) "
              f"from {result['driver']} camera {result['serial']}")
    else:
        print(f"{result['status']}: {result['message']}")


def _print_burst(result):
    if result["ok"]:
        span = result["frames"][-1]["t_ms"] / 1000.0 if result["frames"] else 0.0
        print(f"saved {result['saved']} frames to {result['dir']} over {span:.1f} s "
              f"({result['width']}x{result['height']}) from {result['driver']} camera {result['serial']}")
    else:
        kept = f" ({result['saved']} frames saved before that)" if result.get("saved") else ""
        print(f"{result['status']}: {result['message']}{kept}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="List cameras or grab one image, without the capture program.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="list connected cameras")
    p_list.add_argument("--driver", choices=("all", "basler", "flir"), default="all")
    p_list.add_argument("--json", action="store_true", help="print JSON")
    p_grab = sub.add_parser("grab", help="grab one image into a file")
    p_grab.add_argument("serial")
    p_grab.add_argument("out_file", help="image file to write (.jpg, .png, .bmp)")
    p_grab.add_argument("--exposure", type=float, default=None, help="exposure in microseconds")
    p_grab.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    p_grab.add_argument("--driver", choices=("auto", "basler", "flir"), default="auto")
    p_grab.add_argument("--json", action="store_true", help="print JSON")
    p_burst = sub.add_parser("burst", help="grab frames close in time into a folder (for measure.py)")
    p_burst.add_argument("serial")
    p_burst.add_argument("out_dir", help="folder for the frames (created if missing)")
    p_burst.add_argument("--count", type=int, default=DEFAULT_BURST_COUNT)
    p_burst.add_argument("--interval-ms", type=int, default=DEFAULT_BURST_INTERVAL_MS)
    p_burst.add_argument("--exposure", type=float, default=None, help="exposure in microseconds")
    p_burst.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    p_burst.add_argument("--driver", choices=("auto", "basler", "flir"), default="auto")
    p_burst.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    if args.command == "list":
        drivers = ("basler", "flir") if args.driver == "all" else (args.driver,)
        result = list_all(drivers)
        print(json.dumps(result, indent=1)) if args.json else _print_list(result)
        return 0

    if args.command == "burst":
        result = burst(args.serial, args.out_dir, args.count, args.interval_ms, args.exposure,
                       args.timeout_ms, args.driver)
        print(json.dumps(result, indent=1)) if args.json else _print_burst(result)
        return EXIT_CODES[result["status"]]

    result = grab_one(args.serial, args.out_file, args.exposure, args.timeout_ms, args.driver)
    print(json.dumps(result, indent=1)) if args.json else _print_grab(result)
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
