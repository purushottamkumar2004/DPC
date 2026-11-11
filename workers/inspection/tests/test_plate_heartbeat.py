"""F76: heartbeat plate fields, set in _on_plate() at a NEW plate (D-105: transfer uses them for
cycle boundaries; the UI shows "last plate N min ago").

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
"""
import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tests_support import FakeConn, FakeCursor, det, frame_name, load_reference_settings   # noqa: E402

import heartbeat as HB          # noqa: E402
import inspection as I          # noqa: E402
from defects import DefectStore  # noqa: E402

I.logger.addHandler(logging.NullHandler())
I.logger.propagate = False
FIELDS = ["plates_seen", "last_plate_frame", "last_plate_at", "last_cycle_wheels"]


class PayloadTest(unittest.TestCase):
    def test_defaults_and_append_only_order(self):
        p = HB.heartbeat_payload("inspection_left", HB.LoopStats(), "v", "c")
        keys = list(p)
        self.assertEqual(keys[keys.index("model") + 1:], FIELDS)               # appended after R6
        self.assertEqual([p[k] for k in FIELDS], [0, None, None, None])
        json.dumps(p)


class ScriptedDetector:
    def __init__(self, frames):
        self.frames = list(frames)

    def detect(self, image):
        return self.frames.pop(0)

    label = staticmethod(lambda n: n)


class OnPlateTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.left, _ = load_reference_settings(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_new_plates_only_cycle_wheels_and_time(self):
        """Left (rtl): a plate crossing over 10 frames with a wheel in view = one new plate (D-077);
        three more wheels; the next plate ends a cycle of 4 wheels."""
        frames = []
        for k in range(10):
            x = 1200 - 60 * k
            frames.append([det("plate", x, 0, x + 300, 100), det("wheel", x + 100, 100, x + 600, 600)])
        for _ in range(3):
            frames += [[det("wheel", x, 100, x + 500, 600)] for x in range(1400, 700, -100)]
        frames.append([det("plate", 1200, 0, 1500, 100)])
        stats = HB.LoopStats()
        cur = FakeCursor()
        insp = I.Inspection(self.left, ScriptedDetector(frames), DefectStore(FakeConn(cur), cur, self.left, I.logger),
                            stats)
        names, seen = [], []
        for i in range(len(frames)):
            names.append(frame_name(i % 60, (i // 60) * 10))
            path = os.path.join(self.left.paths.inf, names[-1])
            cv2.imwrite(path, np.zeros((8, 8, 3), np.uint8))
            before = dict(stats.plate)
            insp.process(path)
            seen.append(dict(stats.plate) if stats.plate != before else None)

        changes = [(i, s) for i, s in enumerate(seen) if s]
        self.assertEqual([i for i, _ in changes], [0, len(frames) - 1])        # first plate frame, next plate
        first, second = changes[0][1], changes[1][1]
        self.assertEqual((first["plates_seen"], first["last_plate_frame"], first["last_cycle_wheels"]),
                         (1, names[0], None))                                  # no complete cycle before it
        self.assertEqual((second["plates_seen"], second["last_plate_frame"], second["last_cycle_wheels"]),
                         (2, names[-1], 4))
        at = datetime.datetime.fromisoformat(second["last_plate_at"])
        self.assertIsNotNone(at.tzinfo)
        self.assertLess(abs((datetime.datetime.now(datetime.timezone.utc) - at).total_seconds()), 60)
        p = HB.heartbeat_payload(self.left.worker_id, stats, "v", "c")
        self.assertEqual({k: p[k] for k in FIELDS}, second)


if __name__ == "__main__":
    unittest.main()
