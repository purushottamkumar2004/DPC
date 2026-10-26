"""Bucket clip nut-bolt check, LeftAlgorithm.py L:591-886 (INSPECTION.md 7.1, 7.2).

Full clip: needs 2 `nut_bolt`; a missing bolt must be seen over several consecutive frames
(`tracker.clip_confirm`) before it is a defect. Half clip: needs 1 bolt, no confirmation.
A `high_dusty_bucket_clip` / `half_high_dusty_bucket_clip` near the clip suppresses it
(absence of a detection is not proof of a missing part).

Kept as they are (INSPECTION.md 11): B2 (wrong argument order for the confirmation
centroid), B3 (the half clip is checked only when a half_high_dusty clip is detected),
B10 (every candidate frame copied to bolt_data), B11 (the candidate buffer is cleared only
after an insert), B12 (boxes accumulate on the frame).
"""

import os
import shutil

from defects import INSERTED
from helpers import are_centroids_nearby, frame_time, get_centroid

DEFECT_TYPE = "Nut_Bolt_Missing_In_Bucket_Clip"


def inside_window(window, xmin, xmax, ymin, ymax) -> bool:
    return window.xmin < xmin and xmax < window.xmax and ymin > window.ymin and ymax < window.ymax


def bolts_between(nut_bolts, xmin, xmax):
    """Bolts whose centre x lies between the clip's xmin and xmax (y is not checked)."""
    found = []
    for nb in nut_bolts:
        _, ymin_nb, ymax_nb, xmin_nb, xmax_nb, _ = nb
        cx_nb, _ = get_centroid(xmin_nb, xmax_nb, ymin_nb, ymax_nb)
        if xmin < cx_nb < xmax:
            found.append(nb)
    return found


def confirmed_over_frames(candidates, confirm) -> bool:
    """L:662-677: with `frames` candidates, at least `min_pairs` consecutive pairs are close
    in time and position."""
    if len(candidates) != confirm.frames:
        return False
    detections = sorted(candidates.values(), key=lambda d: d["timestamp"])
    pairs = 0
    for first, second in zip(detections, detections[1:]):
        gap_ms = abs((first["timestamp"] - second["timestamp"]).total_seconds() * 1000)
        c1, c2 = first["centroid"], second["centroid"]
        dist = ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2) ** 0.5
        if gap_ms < confirm.max_gap_ms and dist < confirm.max_dist_px:
            pairs += 1
    return pairs >= confirm.min_pairs


class BucketClipCheck:
    def __init__(self, settings, store, logger):
        self.s, self.store, self.log = settings, store, logger
        self.candidates = {}      # frame name -> {coords, centroid, timestamp} (bucket_clip_dict)

    def run(self, nut_bolts, clips, high_dusty, half_clips, half_high_dusty, image, wheel_counter,
            repeated_list, frame_path):
        for clip in clips:
            self._full_clip(clip, nut_bolts, high_dusty, image, wheel_counter, repeated_list, frame_path)
        for clip in half_clips:
            self._half_clip(clip, nut_bolts, half_high_dusty, image, wheel_counter, repeated_list)

    # ---- full clip (L:598-764) --------------------------------------------
    def _full_clip(self, clip, nut_bolts, high_dusty, image, wheel_counter, repeated_list, frame_path):
        _, ymin, ymax, xmin, xmax, _ = clip
        cx, cy = get_centroid(xmin, xmax, ymin, ymax)
        valid = (xmax - xmin) > self.s.bucket_clip_w and inside_window(self.s.bucket_clip_window,
                                                                         xmin, xmax, ymin, ymax)
        if valid:
            for dusty in high_dusty:
                _, dymin, dymax, dxmin, dxmax, _ = dusty
                dcx, dcy = get_centroid(dxmin, dxmax, dymin, dymax)
                if are_centroids_nearby(cx, cy, dcx, dcy, self.s.tracker.high_dusty_suppress_px):
                    valid = False
                    break
        self.log.info(f"checking for valid bucket clip {valid}")
        if not valid:
            return

        bolts = bolts_between(nut_bolts, xmin, xmax)
        self.log.info(f"Nut Bolts in Bucket Clip :: {len(bolts)}")
        if len(bolts) >= 2:
            return

        shutil.copy2(frame_path, self.s.paths.bolt_data)                 # B10
        try:
            timestamp = frame_time(frame_path)
        except ValueError as e:
            self.log.warning(f"Invalid timestamp format in image name: {frame_path}, error: {e}")
            return
        self.candidates[os.path.basename(frame_path)] = {
            "coords": (xmin, ymin, xmax, ymax),
            "centroid": get_centroid(xmin, ymin, xmax, ymax),              # B2: argument order as L:637
            "timestamp": timestamp,
        }
        keep = self.s.tracker.clip_confirm.frames
        if len(self.candidates) > keep:
            newest = sorted(self.candidates.items(), key=lambda kv: kv[1]["timestamp"], reverse=True)[:keep]
            self.candidates = dict(newest)

        if confirmed_over_frames(self.candidates, self.s.tracker.clip_confirm):
            outcome = self.store.record(DEFECT_TYPE, wheel_counter, (xmin, ymin, xmax, ymax), image,
                                        repeated_list, use_wheel_gate=True)
            if outcome == INSERTED:
                self.candidates.clear()                                     # B11: only after an insert
        self.log.debug(f"bucket_clip_dict {self.candidates}")

    # ---- half clip (L:766-886) --------------------------------------------
    def _half_clip(self, clip, nut_bolts, half_high_dusty, image, wheel_counter, repeated_list):
        _, ymin, ymax, xmin, xmax, _ = clip
        cx, cy = get_centroid(xmin, xmax, ymin, ymax)
        # B3: starts invalid and becomes valid only inside this loop, so without a
        # half_high_dusty_bucket_clip detection the half clip is never checked.
        valid = False
        for dusty in half_high_dusty:
            _, dymin, dymax, dxmin, dxmax, _ = dusty
            dcx, dcy = get_centroid(dxmin, dxmax, dymin, dymax)
            if are_centroids_nearby(cx, cy, dcx, dcy, self.s.tracker.high_dusty_suppress_px):
                valid = False
                break
            valid = True
        if valid:
            valid = (xmax - xmin) > self.s.half_bucket_clip_w and inside_window(
                self.s.half_bucket_clip_window, xmin, xmax, ymin, ymax)
        if not valid:
            return
        bolts = bolts_between(nut_bolts, xmin, xmax)
        self.log.debug(f"Nut Bolts in Half Bucket Clip :: {len(bolts)}")
        if len(bolts) < 1:
            # No frame confirmation and no wheel-count gate for half clips (L:803-860).
            self.store.record(DEFECT_TYPE, wheel_counter, (xmin, ymin, xmax, ymax), image,
                              repeated_list, use_wheel_gate=False)
