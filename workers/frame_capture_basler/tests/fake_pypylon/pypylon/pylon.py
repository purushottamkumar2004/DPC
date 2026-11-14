"""Fake pypylon.pylon for tests. STATE["script"] drives the camera: each RetrieveResult
consumes one event: "frame", "timeout", "failed" (incomplete grab) or "remove" (cable pulled).
When the script is empty the test is over and a stop is requested.

For camera_probe: STATE["devices"] lists the cameras that are connected
({"serial", "model", "ip", "class", "in_use", "open_error", "grab_ok"}); set them with set_devices().
"in_use" = really held by another program: IsDeviceAccessible() False AND Open() fails with
pylon's "controlled by another application" error. "not_accessible" = IsDeviceAccessible() False
but the camera opens fine (free, e.g. reachable for streaming only; F106).
"open_error" makes Open() raise that text.
In a burst, RetrieveResult() gives frames; "fail_at" / "timeout_at" (1-based frame number) make that
frame a failed grab / a timeout."""
import numpy as np

PixelType_BGR8packed = 1
OutputBitAlignment_MsbAligned = 1
GrabStrategy_LatestImageOnly = 1
TimeoutHandling_Return = 1

STATE = {}
BUSY_ERROR = ("Failed to open 'Basler acA1920-40gc#0030534A6217#192.168.1.103:3956'. The device is "
              "controlled by another application. Err: An attempt was made to access an address "
              "location which is currently/momentary not accessible. (0xE1018006)")
FEATURES = {"TriggerMode", "AcquisitionMode", "AcquisitionFrameRateEnable",
            "AcquisitionFrameRateAbs", "ExposureTimeAbs"}     # a mono ace: no BalanceWhiteAuto


def reset(script, available=True):
    STATE.clear()
    STATE.update(available=available, script=list(script), opens=0, frame_id=0, settings={},
                 on_empty=None, devices=[], grabs=0, closed=0)


def set_devices(devices):
    """Replace the connected cameras (camera_probe tests)."""
    if "script" not in STATE:
        reset([])
    STATE["devices"] = [dict(d) for d in devices]
    STATE.update(settings={}, opens=0, closed=0, grabs=0, retrieves=0, strategy=None)


class DeviceInfo:
    def __init__(self, device=None):
        self.device = device or {}
        self.serial = self.device.get("serial")

    def SetSerialNumber(self, serial):
        self.serial = serial

    def GetSerialNumber(self):
        return self.device.get("serial", "")

    def GetModelName(self):
        return self.device.get("model", "")

    def GetDeviceClass(self):
        return self.device.get("class", "BaslerGigE")

    def GetUserDefinedName(self):
        return self.device.get("name", "")

    def IsIpAddressAvailable(self):
        return bool(self.device.get("ip"))

    def GetIpAddress(self):
        if not self.device.get("ip"):
            raise RuntimeError("not a GigE device")
        return self.device["ip"]


class _TransportLayer:
    def EnumerateDevices(self):
        return [DeviceInfo(d) for d in STATE.get("devices", [])]

    def IsDeviceAccessible(self, device_info):
        d = device_info.device
        return not (d.get("in_use") or d.get("not_accessible"))

    def CreateDevice(self, device_info):
        return device_info

    def CreateFirstDevice(self, device_info):
        if not STATE["available"]:
            raise RuntimeError("No device is available or no device contains the provided device info properties.")
        return device_info


class TlFactory:
    @staticmethod
    def GetInstance():
        return _TransportLayer()


class _Node:
    def __init__(self, name):
        self.name = name

    def SetValue(self, value):
        STATE["settings"][self.name] = value

    def GetValue(self):
        return STATE["settings"].get(self.name)


class _Result:
    def __init__(self, kind):
        self.kind = kind
        if kind == "frame":
            STATE["frame_id"] += 1
            self.img = np.zeros((1080, 1920, 3), np.uint8)
            self.img[:64, :64] = (STATE["frame_id"] * 17) % 256     # frames differ after JPEG

    def IsValid(self):
        return self.kind != "timeout"

    def GrabSucceeded(self):
        return self.kind == "frame"

    def GetErrorCode(self):
        return 0xE1000014

    def GetErrorDescription(self):
        return "incomplete"

    def Release(self):
        pass


class InstantCamera:
    def __init__(self, device):
        self.removed = False
        self.device = getattr(device, "device", {}) or {}

    def __getattr__(self, name):
        if name in FEATURES:
            return _Node(name)
        raise AttributeError(name)

    def Open(self):
        if self.device.get("in_use"):
            raise RuntimeError(BUSY_ERROR)
        if self.device.get("open_error"):
            raise RuntimeError(self.device["open_error"])
        STATE["opens"] += 1

    def StartGrabbing(self, strategy):
        STATE["strategy"] = strategy

    def StopGrabbing(self):
        pass

    def Close(self):
        STATE["closed"] = STATE.get("closed", 0) + 1

    def DestroyDevice(self):
        pass

    def IsCameraDeviceRemoved(self):
        return self.removed

    def GrabOne(self, timeout_ms):
        STATE["grabs"] = STATE.get("grabs", 0) + 1
        return _Result("frame" if self.device.get("grab_ok", True) else "failed")

    def RetrieveResult(self, timeout_ms, handling):
        if self.device:                                  # camera_probe burst
            STATE["retrieves"] += 1
            n = STATE["retrieves"]
            if self.device.get("timeout_at") == n:
                return _Result("timeout")
            return _Result("failed" if self.device.get("fail_at") == n else "frame")
        if not STATE["script"]:
            STATE["on_empty"]()
            return _Result("timeout")
        event = STATE["script"].pop(0)
        if event == "remove":
            self.removed = True
            STATE["available"] = False
            raise RuntimeError("device removed")
        return _Result(event)


class _Converted:
    def __init__(self, result):
        self.result = result

    def GetArray(self):
        return self.result.img.copy()


class _Value:
    Value = None


class ImageFormatConverter:
    def __init__(self):
        self.OutputBitAlignment = _Value()

    def Convert(self, result):
        return _Converted(result)
