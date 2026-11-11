"""camera_probe.py: find the cameras on this machine and grab one image, without the capture
program (STATUS S4, D-103 wizard step 3 "cameras").

Functions (for the UI and the setup wizard):
    list_all()                              every driver: Basler (pylon) and FLIR (Spinnaker)
    list_cameras()                          Basler only
    grab_one(serial, out_path, exposure_us=None, timeout_ms=5000, driver="auto")

CLI (stdout = the result; --json for programs):
    python3 camera_probe.py list [--driver all|basler|flir] [--json]
    python3 camera_probe.py grab SERIAL OUT_FILE [--exposure US] [--timeout-ms MS]
                                 [--driver auto|basler|flir] [--json]
Exit codes: 0 ok, 1 failed (SDK missing, open or grab failed), 2 bad request,
            3 camera in use by another program, 4 serial not found.

A camera that another program (normally the running capture) holds is reported as in use;
it is never forced. Stop that program first, e.g. through the Supervisor.
An exposure given here is set in the camera's working memory only (lost at power-off); the
capture program sets its own exposure when it starts, so nothing here changes production.
The FLIR side lives in flir_probe.py with the same functions and result format.
"""
import argparse
import json
import os
import sys

DEFAULT_TIMEOUT_MS = 5000
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


def grab_basler(serial, out_path, exposure_us=None, timeout_ms=DEFAULT_TIMEOUT_MS):
    """One image from the Basler camera with this serial, written to out_path."""
    sdk = _load_pylon()
    if sdk is None:
        return grab_result(SDK_MISSING, "basler", serial, BASLER_SDK_MISSING)
    pylon, genicam = sdk
    factory = pylon.TlFactory.GetInstance()
    info = _find_basler(factory, serial)
    if info is None:
        return grab_result(NOT_FOUND, "basler", serial, f"no Basler camera with serial {serial}")
    model = _info(info, "GetModelName")
    try:
        if not factory.IsDeviceAccessible(info):
            return grab_result(IN_USE, "basler", serial, IN_USE_MESSAGE.format(serial=serial), model=model)
    except Exception:
        pass                                 # cannot tell: opening will show it

    camera = None
    try:
        try:
            camera = pylon.InstantCamera(factory.CreateDevice(info))
            camera.Open()
        except Exception as exc:
            if looks_in_use(exc):
                return grab_result(IN_USE, "basler", serial, IN_USE_MESSAGE.format(serial=serial), model=model)
            return grab_result(FAILED, "basler", serial, f"could not open camera {serial}: {exc}", model=model)

        _set_writable(camera, genicam, ["TriggerMode"], "Off")     # free-running, as the capture runs it
        applied = None
        if exposure_us is not None:
            _set_writable(camera, genicam, ["ExposureAuto"], "Off")
            applied = _set_writable(camera, genicam, ["ExposureTimeAbs", "ExposureTime"], float(exposure_us))
            if applied is None:
                return grab_result(FAILED, "basler", serial, "this camera does not accept an exposure value",
                                   model=model)

        grab = camera.GrabOne(int(timeout_ms))
        try:
            if not grab.GrabSucceeded():
                return grab_result(FAILED, "basler", serial,
                                   f"grab failed: {grab.GetErrorDescription()}", model=model)
            converter = pylon.ImageFormatConverter()
            converter.OutputPixelFormat = pylon.PixelType_BGR8packed
            converter.OutputBitAlignment.Value = pylon.OutputBitAlignment_MsbAligned
            img = converter.Convert(grab).GetArray()
        finally:
            grab.Release()
        path = write_image(img, out_path)
        return grab_result(OK, "basler", serial, None, path=path, model=model,
                           width=int(img.shape[1]), height=int(img.shape[0]),
                           exposure_us=None if applied is None else float(applied))
    except Exception as exc:
        if looks_in_use(exc):
            return grab_result(IN_USE, "basler", serial, IN_USE_MESSAGE.format(serial=serial), model=model)
        return grab_result(FAILED, "basler", serial, f"grab failed: {exc}", model=model)
    finally:
        if camera is not None:
            for step in ("Close", "DestroyDevice"):
                try:
                    getattr(camera, step)()
                except Exception:
                    pass


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


def grab_one(serial, out_path, exposure_us=None, timeout_ms=DEFAULT_TIMEOUT_MS, driver="auto"):
    """One image from the camera with this serial. driver "auto" looks in Basler, then FLIR."""
    serial = str(serial).strip() if serial is not None else ""
    if not serial:
        return grab_result(BAD_REQUEST, None, None, "no serial given")
    if driver not in ("auto", "basler", "flir"):
        return grab_result(BAD_REQUEST, None, serial, f"unknown driver '{driver}'")
    problem = check_output_path(out_path)
    if problem:
        return grab_result(BAD_REQUEST, None, serial, problem)
    try:
        if exposure_us is not None:
            if isinstance(exposure_us, bool) or float(exposure_us) <= 0:
                raise ValueError
            exposure_us = float(exposure_us)
    except (TypeError, ValueError):
        return grab_result(BAD_REQUEST, None, serial, "exposure must be a number above 0 microseconds")
    try:
        if isinstance(timeout_ms, bool) or int(timeout_ms) <= 0:
            raise ValueError
        timeout_ms = int(timeout_ms)
    except (TypeError, ValueError):
        return grab_result(BAD_REQUEST, None, serial, "timeout must be a whole number of ms above 0")

    if driver == "basler":
        return grab_basler(serial, out_path, exposure_us, timeout_ms)
    if driver == "flir":
        return _flir().grab_one(serial, out_path, exposure_us, timeout_ms)

    tried = []
    for name, grab in (("basler", grab_basler), ("flir", lambda *a: _flir().grab_one(*a))):
        result = grab(serial, out_path, exposure_us, timeout_ms)
        if result["status"] not in (NOT_FOUND, SDK_MISSING):
            return result
        tried.append(f"{name}: {result['message']}")
    return grab_result(NOT_FOUND, None, serial, f"camera {serial} not found ({'; '.join(tried)})")


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
    args = parser.parse_args(argv)

    if args.command == "list":
        drivers = ("basler", "flir") if args.driver == "all" else (args.driver,)
        result = list_all(drivers)
        print(json.dumps(result, indent=1)) if args.json else _print_list(result)
        return 0

    result = grab_one(args.serial, args.out_file, args.exposure, args.timeout_ms, args.driver)
    print(json.dumps(result, indent=1)) if args.json else _print_grab(result)
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
