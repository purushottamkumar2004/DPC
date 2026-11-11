"""Shared test helpers: settings, a fake Microsoft Graph, fake plate signals, frame files.
Nothing here touches the network or a real drive."""

import datetime
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(WORKER_DIR))
for p in (REPO_ROOT, WORKER_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from graph_client import UPLOAD_FRAGMENT_BYTES          # noqa: E402
from quickxor import QuickXorHash                       # noqa: E402
from transfer_errors import TemporaryError              # noqa: E402
from transfer_settings import build_settings            # noqa: E402

LOG = logging.getLogger("transfer-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

GRAPH_SECRET = "graph-secret-for-test"


def raw_config(root, **transfer_overrides):
    transfer = {"drive": {"site": "contoso.sharepoint.com:/sites/DPC"}, "models_folder": "DPC/models/<site.id>",
                "upload": {"chunk_mb": 1, "remote_folder": "DPC/data/<site.id>", "staging_max_gb": 1.0,
                           "max_mbps": None, "plate_images": 5}}
    transfer.update(transfer_overrides)
    return {
        "worker_id": "transfer", "site": {"id": "tadipatri", "name": "Tadipatri DPC"},
        "paths": {"root": root, "logs": f"{root}/logs", "run": f"{root}/run", "state": f"{root}/data/state/transfer",
                  "staging": f"{root}/data/transfer/staging", "downloads": f"{root}/data/transfer/downloads",
                  "models": f"{root}/data/models"},
        "log": {"backup_count": 2},
        "cameras": [{"id": "CAM1", "side": "LEFT", "serial": "111", "fps": 4.0, "exposure_us": 1500, "rotate": 0,
                     "raw_images": f"{root}/data/frames/CAM1/RAW_IMAGES"},
                    {"id": "CAM2", "side": "RIGHT", "serial": "222", "fps": 4.0, "exposure_us": 2000, "rotate": 0,
                     "raw_images": f"{root}/data/frames/CAM2/RAW_IMAGES"}],
        "sides": [{"side": "LEFT", "camera_id": "CAM1", "inspection_worker": "inspection_left",
                   "plate": f"{root}/data/images/PLATE/LEFT"},
                  {"side": "RIGHT", "camera_id": "CAM2", "inspection_worker": "inspection_right",
                   "plate": f"{root}/data/images/PLATE/RIGHT"}],
        "models_in_use": {"dpc_maskrcnn": ["LEFT", "RIGHT"]},
        "machine": {"total_wheel_count": 190},
        "graph": {"tenant_id": "tenant", "client_id": "client", "client_secret": GRAPH_SECRET},
        "mqtt": None,
        "transfer": transfer}


def make_settings(root, **transfer_overrides):
    s = build_settings(raw_config(root, **transfer_overrides))
    for folder in (s.run, s.state, s.staging, s.downloads, s.models):
        os.makedirs(folder, exist_ok=True)
    for side in s.sides:
        os.makedirs(side.raw_images, exist_ok=True)
        os.makedirs(side.plate, exist_ok=True)
    return s


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, name, payload=None):
        self.items.append((name, payload or {}))
        return True

    def names(self):
        return [n for n, _ in self.items]

    def last(self, name):
        return next(p for n, p in reversed(self.items) if n == name)


class FakePlates:
    """Inspection heartbeats with the F76 fields, set by the test."""

    def __init__(self):
        self.state = {}

    def set(self, side, pid, plates_seen, frame, wheels=None):
        self.state[side] = {"pid": pid, "plates_seen": plates_seen, "last_plate_frame": frame,
                            "last_cycle_wheels": wheels}

    def snapshot(self, side):
        value = self.state.get(side)
        return dict(value) if value else None


class FrameMaker:
    """Frame files with real capture names, 4 per second, in time order."""

    def __init__(self, start=datetime.datetime(2026, 11, 10, 9, 0, 0)):
        self.t = start

    def next_name(self):
        name = self.t.strftime("%Y_%m_%d_%H_%M_%S_%f") + ".jpg"
        self.t += datetime.timedelta(milliseconds=250)
        return name

    def write(self, folder, count=1, size=200_000, name=None):
        names = []
        for _ in range(count):
            n = name or self.next_name()
            with open(os.path.join(folder, n), "wb") as f:
                f.write(os.urandom(size))
            names.append(n)
        return names


def qx(data: bytes) -> str:
    h = QuickXorHash()
    h.update(data)
    return h.base64()


class FakeGraph:
    """An in-memory drive with the GraphClient methods the transfer uses, plus fault switches:
       cut_at_fragment   raise TemporaryError (network) when that fragment number is sent
       corrupt_uploads   that many completed uploads are stored with one byte changed
       expire_next       the next upload_status / fragment finds the session gone
       cut_download_at   raise TemporaryError on that download_range call number
    """

    def __init__(self, clock=None):
        self.files = {}                      # path -> bytes
        self.meta = {}                       # path -> {"eTag", "lastModifiedDateTime"}
        self.folders = set()
        self.sessions = {}
        self.fragments_sent = 0
        self.bytes_sent = 0
        self.cut_at_fragment = None
        self.corrupt_uploads = 0
        self.expire_next = False
        self.download_calls = []
        self.cut_download_at = None
        self.listed = []
        self.deleted = []
        self.clock = clock or Clock()
        self._etag = 0

    # -- helpers for tests
    def put_file(self, path, data, modified_ago_s=3600):
        self.files[path] = data
        self._etag += 1
        when = datetime.datetime.fromtimestamp(self.clock() - modified_ago_s, datetime.timezone.utc)
        self.meta[path] = {"eTag": f"etag-{self._etag}", "lastModifiedDateTime": when.isoformat().replace("+00:00", "Z")}
        parts = path.split("/")
        for i in range(1, len(parts)):
            self.folders.add("/".join(parts[:i]))

    def item(self, path, data=None):
        data = self.files[path] if data is None else data
        meta = self.meta.get(path, {})
        return {"name": path.rsplit("/", 1)[-1], "size": len(data), "eTag": meta.get("eTag"),
                "lastModifiedDateTime": meta.get("lastModifiedDateTime"),
                "file": {"hashes": {"quickXorHash": qx(data)}}}

    # -- GraphClient interface
    def get_item(self, path):
        if path in self.files:
            return self.item(path)
        if path in self.folders:
            return {"name": path.rsplit("/", 1)[-1], "folder": {}}
        return None

    def list_children(self, path):
        self.listed.append(path)
        if path not in self.folders:
            return None
        prefix = path + "/"
        items = [self.item(p) for p in self.files if p.startswith(prefix) and "/" not in p[len(prefix):]]
        items += [{"name": f[len(prefix):], "folder": {}} for f in self.folders
                  if f.startswith(prefix) and "/" not in f[len(prefix):]]
        return items

    def ensure_folder(self, path):
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            self.folders.add("/".join(parts[:i]))

    def delete_item(self, path):
        self.deleted.append(path)
        self.files.pop(path, None)

    def create_upload_session(self, path):
        url = f"https://upload.example/{len(self.sessions) + 1}"
        self.sessions[url] = {"path": path, "data": bytearray()}
        return url, "2026-11-20T00:00:00Z"

    def upload_status(self, url):
        if self.expire_next:
            self.expire_next = False
            self.sessions.pop(url, None)
        if url not in self.sessions:
            return None
        return len(self.sessions[url]["data"])

    def upload_fragment(self, url, data, start, total):
        self.fragments_sent += 1
        if self.cut_at_fragment is not None and self.fragments_sent == self.cut_at_fragment:
            self.cut_at_fragment = None
            raise TemporaryError("upload fragment: ConnectionError (network)")
        if self.expire_next:
            self.expire_next = False
            self.sessions.pop(url, None)
        session = self.sessions.get(url)
        if session is None:
            return "expired", None
        have = len(session["data"])
        if start < have:
            return "resync", None
        assert start == have, f"gap: sent {start}, server has {have}"
        if start + len(data) < total:
            assert len(data) % (320 * 1024) == 0, "Graph rule: fragments in multiples of 320 KiB"
        session["data"] += data
        self.bytes_sent += len(data)
        if len(session["data"]) < total:
            return "more", len(session["data"])
        stored = bytes(session["data"])
        if self.corrupt_uploads:
            self.corrupt_uploads -= 1
            stored = bytes([stored[0] ^ 0xFF]) + stored[1:]
        self.put_file(session["path"], stored, modified_ago_s=0)
        del self.sessions[url]
        return "done", self.item(session["path"])

    def cancel_upload(self, url):
        self.sessions.pop(url, None)

    def download_range(self, path, start, end):
        self.download_calls.append((path, start, end))
        if self.cut_download_at is not None and len(self.download_calls) == self.cut_download_at:
            self.cut_download_at = None
            raise TemporaryError("download: Timeout (network)")
        return self.files[path][start:end + 1]


def temp_root(test):
    folder = tempfile.mkdtemp(prefix="dpc-transfer-test-")
    test.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
    return folder


assert UPLOAD_FRAGMENT_BYTES % (320 * 1024) == 0
