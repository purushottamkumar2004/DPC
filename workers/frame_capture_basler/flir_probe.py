"""flir_probe.py: the FLIR (Spinnaker, PySpin) side of camera_probe.py (STATUS S4).

Same functions and result format as the Basler side:
    list_cameras()                                        -> list result (driver "flir")
    grab_one(serial, out_path, exposure_us=None, timeout_ms=5000) -> grab result
Use it through camera_probe.py (list_all / grab_one / the CLI), which also covers Basler.
Without the Spinnaker SDK both report "FLIR SDK not installed" instead of failing.

Not yet tried on a real FLIR camera (no FLIR site so far): built from the Spinnaker API and
tested with a fake PySpin. The first FLIR site should run `camera_probe.py list` and `grab` once.
"""
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


def grab_one(serial, out_path, exposure_us=None, timeout_ms=probe.DEFAULT_TIMEOUT_MS):
    """One image from the FLIR camera with this serial, written to out_path."""
    PySpin = _load_pyspin()
    if PySpin is None:
        return probe.grab_result(probe.SDK_MISSING, "flir", serial, FLIR_SDK_MISSING)
    in_use_message = probe.IN_USE_MESSAGE.format(serial=serial)
    try:
        with _System(PySpin) as spin:
            camera, model, in_use = _find(PySpin, spin.cameras, serial)
            if camera is None:
                return probe.grab_result(probe.NOT_FOUND, "flir", serial, f"no FLIR camera with serial {serial}")
            if in_use:
                del camera
                return probe.grab_result(probe.IN_USE, "flir", serial, in_use_message, model=model)
            try:
                return _grab_from(PySpin, camera, serial, model, out_path, exposure_us, timeout_ms)
            finally:
                del camera
    except Exception as exc:
        if probe.looks_in_use(exc):
            return probe.grab_result(probe.IN_USE, "flir", serial, in_use_message)
        return probe.grab_result(probe.FAILED, "flir", serial, f"grab failed: {exc}")


def _grab_from(PySpin, camera, serial, model, out_path, exposure_us, timeout_ms):
    try:
        camera.Init()
    except Exception as exc:
        if probe.looks_in_use(exc):
            return probe.grab_result(probe.IN_USE, "flir", serial,
                                     probe.IN_USE_MESSAGE.format(serial=serial), model=model)
        return probe.grab_result(probe.FAILED, "flir", serial, f"could not open camera {serial}: {exc}",
                                 model=model)
    acquiring = False
    try:
        nodemap = camera.GetNodeMap()
        _set_enum(PySpin, nodemap, "TriggerMode", "Off")
        _set_enum(PySpin, nodemap, "AcquisitionMode", "SingleFrame")
        applied = None
        if exposure_us is not None:
            applied = _set_exposure(PySpin, nodemap, exposure_us)
            if applied is None:
                return probe.grab_result(probe.FAILED, "flir", serial,
                                         "this camera does not accept an exposure value", model=model)
        camera.BeginAcquisition()
        acquiring = True
        image = camera.GetNextImage(int(timeout_ms))
        try:
            if image.IsIncomplete():
                return probe.grab_result(probe.FAILED, "flir", serial,
                                         f"grab failed: incomplete image (status {image.GetImageStatus()})",
                                         model=model)
            img = _to_bgr(PySpin, image)
        finally:
            image.Release()
        path = probe.write_image(img, out_path)
        return probe.grab_result(probe.OK, "flir", serial, None, path=path, model=model,
                                 width=int(img.shape[1]), height=int(img.shape[0]),
                                 exposure_us=None if applied is None else float(applied))
    finally:
        if acquiring:
            try:
                camera.EndAcquisition()
            except Exception:
                pass
        try:
            camera.DeInit()
        except Exception:
            pass
