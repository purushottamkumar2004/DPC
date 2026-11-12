"""flir_probe.py: the FLIR (Spinnaker, PySpin) side of camera_probe.py (STATUS S4).

Same functions and result format as the Basler side:
    list_cameras()                                        -> list result (driver "flir")
    grab_one(serial, out_path, exposure_us=None, timeout_ms=5000) -> grab result
    burst(serial, out_dir, count, interval_ms, exposure_us=None, timeout_ms=5000) -> burst result
Use it through camera_probe.py (list_all / grab_one / the CLI), which also covers Basler.
Without the Spinnaker SDK both report "FLIR SDK not installed" instead of failing.

Not yet tried on a real FLIR camera (no FLIR site so far): built from the Spinnaker API and
tested with a fake PySpin. The first FLIR site should run `camera_probe.py list` and `grab` once.
"""
import os
import time

import camera_probe as probe

FLIR_SDK_MISSING = "FLIR SDK not installed (Spinnaker + PySpin)"

# DeviceAccessStatus (GenTL), read without opening the camera.
_FREE_STATES = ("ReadWrite",)
_IN_USE_STATES = ("ReadOnly", "NoAccess", "Busy", "OpenReadWrite", "OpenReadOnly")


def _load_pyspin():
    try:
        import PySpin
    except ImportError:
        return None
    return PySpin


def _read(PySpin, nodemap, name, kind):
    """Value of a readable node, or None. kind: "string", "integer" or "enum"."""
    try:
        node = nodemap.GetNode(name)
        ptr = {"string": PySpin.CStringPtr, "integer": PySpin.CIntegerPtr,
               "enum": PySpin.CEnumerationPtr}[kind](node)
        if not PySpin.IsAvailable(ptr) or not PySpin.IsReadable(ptr):
            return None
        if kind == "enum":
            return ptr.GetCurrentEntry().GetSymbolic()
        return ptr.GetValue()
    except Exception:
        return None


def _ip_text(value):
    """GevDeviceIPAddress is an integer; 0 or missing means not GigE."""
    if not value:
        return None
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _describe(PySpin, camera):
    """(serial, model, ip, in_use) from the transport-layer node map, without opening the camera."""
    tl = camera.GetTLDeviceNodeMap()
    access = _read(PySpin, tl, "DeviceAccessStatus", "enum")
    in_use = True if access in _IN_USE_STATES else False if access in _FREE_STATES else None
    return (_read(PySpin, tl, "DeviceSerialNumber", "string"),
            _read(PySpin, tl, "DeviceModelName", "string"),
            _ip_text(_read(PySpin, tl, "GevDeviceIPAddress", "integer")),
            in_use)


class _System:
    """Spinnaker's system and camera list, released in the order the SDK requires
    (every camera reference first, then the list, then the system)."""

    def __init__(self, PySpin):
        self.system = PySpin.System.GetInstance()
        self.cameras = self.system.GetCameras()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.cameras.Clear()
        finally:
            self.system.ReleaseInstance()
        return False


def list_cameras():
    PySpin = _load_pyspin()
    if PySpin is None:
        return probe.list_result("flir", False, message=FLIR_SDK_MISSING)
    try:
        found = []
        with _System(PySpin) as spin:
            for index in range(spin.cameras.GetSize()):
                camera = spin.cameras.GetByIndex(index)
                serial, model, ip, in_use = _describe(PySpin, camera)
                del camera
                found.append(probe.camera_entry("flir", serial, model, ip, in_use))
        return probe.list_result("flir", True, found)
    except Exception as exc:
        return probe.list_result("flir", True, message=f"listing FLIR cameras failed: {exc}")


def _set_enum(PySpin, nodemap, name, entry_name):
    node = PySpin.CEnumerationPtr(nodemap.GetNode(name))
    if not PySpin.IsAvailable(node) or not PySpin.IsWritable(node):
        return False
    entry = node.GetEntryByName(entry_name)
    if not PySpin.IsAvailable(entry) or not PySpin.IsReadable(entry):
        return False
    node.SetIntValue(entry.GetValue())
    return True


def _set_exposure(PySpin, nodemap, exposure_us):
    """Manual exposure, clamped to what the camera allows. Returns the value set, or None."""
    _set_enum(PySpin, nodemap, "ExposureAuto", "Off")
    node = PySpin.CFloatPtr(nodemap.GetNode("ExposureTime"))
    if not PySpin.IsAvailable(node) or not PySpin.IsWritable(node):
        return None
    node.SetValue(min(max(float(exposure_us), node.GetMin()), node.GetMax()))
    return node.GetValue()


def _to_bgr(PySpin, image):
    """BGR array; Spinnaker 3 converts through ImageProcessor, Spinnaker 2 on the image."""
    if hasattr(PySpin, "ImageProcessor"):
        converted = PySpin.ImageProcessor().Convert(image, PySpin.PixelFormat_BGR8)
    else:
        converted = image.Convert(PySpin.PixelFormat_BGR8, PySpin.HQ_LINEAR)
    return converted.GetNDArray()


def _find(PySpin, cameras, serial):
    for index in range(cameras.GetSize()):
        camera = cameras.GetByIndex(index)
        found_serial, model, _, in_use = _describe(PySpin, camera)
        if found_serial == str(serial):
            return camera, model, in_use
        del camera
    return None, None, None


def _with_flir(serial, make_result, work):
    """Find the FLIR camera, check it is free, Init it, run work(PySpin, camera, nodemap, model),
    then EndAcquisition (if started) and DeInit. Shared by grab and burst so both say "in use"
    the same way. make_result(status, message, **fields) builds the operation's result."""
    PySpin = _load_pyspin()
    if PySpin is None:
        return make_result(probe.SDK_MISSING, FLIR_SDK_MISSING)
    in_use_message = probe.IN_USE_MESSAGE.format(serial=serial)
    try:
        with _System(PySpin) as spin:
            camera, model, in_use = _find(PySpin, spin.cameras, serial)
            if camera is None:
                return make_result(probe.NOT_FOUND, f"no FLIR camera with serial {serial}")
            try:
                if in_use:
                    return make_result(probe.IN_USE, in_use_message, model=model)
                try:
                    camera.Init()
                except Exception as exc:
                    if probe.looks_in_use(exc):
                        return make_result(probe.IN_USE, in_use_message, model=model)
                    return make_result(probe.FAILED, f"could not open camera {serial}: {exc}", model=model)
                state = {"acquiring": False}
                try:
                    return work(PySpin, camera, camera.GetNodeMap(), model, state)
                finally:
                    if state["acquiring"]:
                        try:
                            camera.EndAcquisition()
                        except Exception:
                            pass
                    try:
                        camera.DeInit()
                    except Exception:
                        pass
            finally:
                del camera
    except Exception as exc:
        if probe.looks_in_use(exc):
            return make_result(probe.IN_USE, in_use_message)
        return make_result(probe.FAILED, f"camera {serial}: {exc}")


def _settings(PySpin, nodemap, acquisition_mode, exposure_us):
    """Free-running in the given mode, manual exposure if asked. Returns (exposure or None, problem)."""
    _set_enum(PySpin, nodemap, "TriggerMode", "Off")
    _set_enum(PySpin, nodemap, "AcquisitionMode", acquisition_mode)
    if exposure_us is None:
        return None, None
    applied = _set_exposure(PySpin, nodemap, exposure_us)
    if applied is None:
        return None, "this camera does not accept an exposure value"
    return float(applied), None


def _image_array(PySpin, image):
    """(BGR array, None) or (None, problem) for an incomplete image. Releases the image."""
    try:
        if image.IsIncomplete():
            return None, f"incomplete image (status {image.GetImageStatus()})"
        return _to_bgr(PySpin, image), None
    finally:
        image.Release()


def grab_one(serial, out_path, exposure_us=None, timeout_ms=probe.DEFAULT_TIMEOUT_MS):
    """One image from the FLIR camera with this serial, written to out_path."""
    def result(status, message, **fields):
        return probe.grab_result(status, "flir", serial, message, **fields)

    def work(PySpin, camera, nodemap, model, state):
        applied, problem = _settings(PySpin, nodemap, "SingleFrame", exposure_us)
        if problem:
            return result(probe.FAILED, problem, model=model)
        camera.BeginAcquisition()
        state["acquiring"] = True
        img, problem = _image_array(PySpin, camera.GetNextImage(int(timeout_ms)))
        if problem:
            return result(probe.FAILED, f"grab failed: {problem}", model=model)
        path = probe.write_image(img, out_path)
        return result(probe.OK, None, path=path, model=model, width=int(img.shape[1]),
                      height=int(img.shape[0]), exposure_us=applied)

    return _with_flir(serial, result, work)


def burst(serial, out_dir, count, interval_ms, exposure_us=None, timeout_ms=probe.DEFAULT_TIMEOUT_MS):
    """count frames, one every interval_ms, from the FLIR camera with this serial into out_dir."""
    frames = []
    base = {"path": None, "dir": os.path.abspath(out_dir), "count": count, "interval_ms": interval_ms}

    def result(status, message, **fields):
        return probe.burst_result(status, "flir", serial, message, frames, **{**base, **fields})

    def work(PySpin, camera, nodemap, model, state):
        applied, problem = _settings(PySpin, nodemap, "Continuous", exposure_us)
        if problem:
            return result(probe.FAILED, problem, model=model)
        # each GetNextImage should give the newest frame, not one waiting in the buffer
        _set_enum(PySpin, camera.GetTLStreamNodeMap(), "StreamBufferHandlingMode", "NewestOnly")
        camera.BeginAcquisition()
        state["acquiring"] = True
        start, previous, size = time.monotonic(), None, (None, None)
        for index in range(count):
            probe.wait_until(start + index * interval_ms / 1000.0)
            img, problem = _image_array(PySpin, camera.GetNextImage(int(timeout_ms)))
            if problem:
                return result(probe.FAILED, f"frame {index + 1} of {count}: {problem}", model=model,
                              exposure_us=applied, width=size[0], height=size[1])
            taken_ms = round((time.monotonic() - start) * 1000)
            path, previous = probe.frame_path(out_dir, previous)
            frames.append({"index": index, "path": probe.write_image(img, path), "t_ms": taken_ms})
            size = (int(img.shape[1]), int(img.shape[0]))
        return result(probe.OK, None, model=model, exposure_us=applied, width=size[0], height=size[1])

    return _with_flir(serial, result, work)
