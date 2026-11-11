"""Fake PySpin (Spinnaker) for flir_probe tests: only the calls flir_probe makes.
CAMERAS lists the connected cameras: {"serial", "model", "ip" (int), "access" (DeviceAccessStatus),
"init_error", "incomplete"}. LOG records the calls that matter for clean-up order."""
import numpy as np

PixelFormat_BGR8 = 1
HQ_LINEAR = 1
CAMERAS = []
LOG = []
EXPOSURE_RANGE = (10.0, 30000.0)


def setup(cameras):
    CAMERAS[:] = [dict(c) for c in cameras]
    LOG.clear()


class SpinnakerException(Exception):
    pass


class _Entry:
    def __init__(self, name):
        self.name = name

    def GetSymbolic(self):
        return self.name

    def GetValue(self):
        return self.name


class _Node:
    def __init__(self, store, name, value=None, writable=True):
        self.store, self.name, self.writable = store, name, writable
        if value is not None:
            store.setdefault(name, value)

    # enumeration
    def GetCurrentEntry(self):
        return _Entry(self.store.get(self.name))

    def GetEntryByName(self, name):
        return _Entry(name)

    def SetIntValue(self, value):
        self.store[self.name] = value
        LOG.append(f"set {self.name}={value}")

    # string / integer / float
    def GetValue(self):
        return self.store.get(self.name)

    def SetValue(self, value):
        self.store[self.name] = value
        LOG.append(f"set {self.name}={value}")

    def GetMin(self):
        return EXPOSURE_RANGE[0]

    def GetMax(self):
        return EXPOSURE_RANGE[1]


class _NodeMap:
    def __init__(self, store, readable):
        self.store, self.readable = store, readable

    def GetNode(self, name):
        return _Node(self.store, name) if name in self.readable else None


def CStringPtr(node):
    return node


CIntegerPtr = CEnumerationPtr = CFloatPtr = CStringPtr


def IsAvailable(node):
    return node is not None


def IsReadable(node):
    return node is not None


def IsWritable(node):
    return node is not None and node.writable


class _Image:
    def __init__(self, incomplete):
        self.incomplete = incomplete

    def IsIncomplete(self):
        return self.incomplete

    def GetImageStatus(self):
        return 3

    def Release(self):
        LOG.append("image released")


class _Converted:
    def GetNDArray(self):
        return np.full((1080, 1440, 3), 90, np.uint8)


class ImageProcessor:
    def Convert(self, image, pixel_format):
        return _Converted()


class _Camera:
    def __init__(self, info):
        self.info = info
        self.tl_store = {"DeviceSerialNumber": info["serial"], "DeviceModelName": info.get("model"),
                         "GevDeviceIPAddress": info.get("ip", 0),
                         "DeviceAccessStatus": info.get("access", "ReadWrite")}
        self.store = {}

    def GetTLDeviceNodeMap(self):
        return _NodeMap(self.tl_store, set(self.tl_store))

    def GetNodeMap(self):
        return _NodeMap(self.store, {"TriggerMode", "AcquisitionMode", "ExposureAuto", "ExposureTime"})

    def Init(self):
        LOG.append(f"init {self.info['serial']}")
        if self.info.get("init_error"):
            raise SpinnakerException(self.info["init_error"])

    def DeInit(self):
        LOG.append("deinit")

    def BeginAcquisition(self):
        LOG.append("begin")

    def EndAcquisition(self):
        LOG.append("end")

    def GetNextImage(self, timeout_ms):
        return _Image(self.info.get("incomplete", False))


class _CameraList:
    def __init__(self):
        self.items = [_Camera(c) for c in CAMERAS]

    def GetSize(self):
        return len(self.items)

    def GetByIndex(self, index):
        return self.items[index]

    def Clear(self):
        LOG.append("list cleared")


class System:
    @staticmethod
    def GetInstance():
        return System()

    def GetCameras(self):
        return _CameraList()

    def ReleaseInstance(self):
        LOG.append("system released")
