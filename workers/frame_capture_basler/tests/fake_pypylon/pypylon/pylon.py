"""Fake pypylon.pylon for tests. STATE["script"] drives the camera: each RetrieveResult
consumes one event: "frame", "timeout", "failed" (incomplete grab) or "remove" (cable pulled).
When the script is empty the test is over and a stop is requested."""
import numpy as np

PixelType_BGR8packed = 1
OutputBitAlignment_MsbAligned = 1
GrabStrategy_LatestImageOnly = 1
TimeoutHandling_Return = 1

STATE = {}
FEATURES = {"TriggerMode", "AcquisitionMode", "AcquisitionFrameRateEnable",
            "AcquisitionFrameRateAbs", "ExposureTimeAbs"}     # a mono ace: no BalanceWhiteAuto


def reset(script, available=True):
    STATE.clear()
    STATE.update(available=available, script=list(script), opens=0, frame_id=0, settings={},
                 on_empty=None)


class DeviceInfo:
    def SetSerialNumber(self, serial):
        self.serial = serial


class _TransportLayer:
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

    def __getattr__(self, name):
        if name in FEATURES:
            return _Node(name)
        raise AttributeError(name)

    def Open(self):
        STATE["opens"] += 1

    def StartGrabbing(self, strategy):
        pass

    def StopGrabbing(self):
        pass

    def Close(self):
        pass

    def DestroyDevice(self):
        pass

    def IsCameraDeviceRemoved(self):
        return self.removed

    def RetrieveResult(self, timeout_ms, handling):
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
