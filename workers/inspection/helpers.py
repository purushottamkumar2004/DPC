"""Small helpers shared by the checks, copied from LeftAlgorithm.py with the same behaviour.

A detection is the list the model returns for one object:
    [score, ymin, ymax, xmin, xmax, class_name]   (class_name as the model named it)
"""

import datetime
import math
import os

import cv2

FRAME_TIME_FORMAT = "%Y_%m_%d_%H_%M_%S_%f"     # frame file names (CONTRACTS 4.1, D-031)


def now_stamp() -> str:
    """Time stamp used in every saved image name (same format as frame names)."""
    return datetime.datetime.now().strftime(FRAME_TIME_FORMAT)


def get_centroid(xmin, xmax, ymin, ymax):
    """Centre of a box. NOTE the argument order (x, x, y, y), as in L:307. The clip check
    calls it once with (xmin, ymin, xmax, ymax): bug B2, kept as it is (INSPECTION.md 11)."""
    return int((xmin + xmax) / 2.0), int((ymin + ymax) / 2.0)


def are_centroids_nearby(cx1, cy1, cx2, cy2, threshold):
    return math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2) <= threshold


def draw_red_box(frame, label, xmin, ymin, xmax, ymax):
    """Label and red box drawn ON the given image (callers rely on that, L:330-336)."""
    cv2.putText(frame, label, (xmin, ymin - 10), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 255), 2, cv2.LINE_AA)
    cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 0, 255), 2)


def dated_dir(parent: str) -> str:
    """<parent>/<YYYYMMDD>/ (with the trailing slash, as L:405-412), created when missing."""
    path = os.path.join(parent, datetime.datetime.now().strftime("%Y%m%d") + "/")
    os.makedirs(path, exist_ok=True)
    return path


def frame_time(path: str) -> datetime.datetime:
    """Capture time from a frame file name. Raises ValueError for another name format.
    A 'RAW_' prefix is allowed (L:643)."""
    base = os.path.basename(path).replace("RAW_", "").replace(".jpg", "")
    return datetime.datetime.strptime(base[:26], FRAME_TIME_FORMAT)


def frame_time_ms(path: str, fallback_ms: int) -> int:
    """Capture time in ms for the skidding tracker; the fallback when the name does not parse
    (L:1304-1316 used the wall clock as fallback)."""
    try:
        base = os.path.splitext(os.path.basename(path))[0]
        parts = base.split("_")
        if len(parts) >= 7:
            dt = datetime.datetime.strptime("_".join(parts[:7]), FRAME_TIME_FORMAT)
            return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    return fallback_ms
