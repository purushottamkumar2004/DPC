"""Wheel rotation (skidding) check, LeftAlgorithm.py L:1246-1604 (INSPECTION.md 7.5).

A rotating wheel moves its `wheel_magnet` up or down between frames; a sliding (skidding)
wheel carries the magnet sideways only. Over `window_frames` consecutive frames on the same
wheel, a vertical magnet travel below `min_magnet_travel_px` is `Wheel_Skidding`.
The track restarts on a time gap, on another wheel, on a wheel that did not move at all
(conveyor stopped: never a defect), or on a vertical jump of the magnet.

New in P7 (D-073 D9): the finding goes through the shared defect steps (defects.py), so it
gets a DB row and a mail. The last frames are copied as evidence next to a new defect only.
"""

import datetime
import math
import os
import shutil
import time
from collections import deque

import cv2

from defects import INSERTED
from helpers import dated_dir, frame_time_ms

DEFECT_TYPE = "Wheel_Skidding"
EVIDENCE_FRAMES = 6          # frames kept for the evidence folder (LATEST_IMAGE_COUNT, L:46)


def _quadrant(magnet_cx, magnet_cy, ref_x, ref_y) -> str:
    if magnet_cx < ref_x and magnet_cy < ref_y:
        return "top_left"
    if magnet_cx >= ref_x and magnet_cy < ref_y:
        return "top_right"
    if magnet_cx < ref_x and magnet_cy >= ref_y:
        return "bottom_left"
    return "bottom_right"


def _inside(box, x, y) -> bool:
    return box["xmin"] <= x <= box["xmax"] and box["ymin"] <= y <= box["ymax"]


class SkiddingCheck:
    def __init__(self, settings, store, logger):
        self.s, self.store, self.log = settings, store, logger
        self.p = settings.tracker.skidding
        self.track = {}                                     # magnet_track_state
        self.latest_frames = deque(maxlen=EVIDENCE_FRAMES)  # latest_image_name_list

    def run(self, wheels, magnets, wheel_bolts, image, wheel_counter, repeated_list, frame_path):
        """`wheels` = `wheel` detections only (half wheels are not used, L:2450-2456)."""
        if not magnets:
            self._save_if_magnet_missing(wheels, image, wheel_counter)
            return

        now_ms = frame_time_ms(frame_path, int(time.time() * 1000))
        frame_name = os.path.basename(frame_path)
        self.log.info(f"+++++++++++++++++++++++++++ checkWheelRotation logic start for current_filename {frame_path}+++++++++++++++++++++++++++")
        if frame_name not in self.latest_frames:
            self.latest_frames.append(frame_name)

        for magnet in self._largest_magnet_per_wheel(magnets, wheels):
            if self._track_magnet(magnet, wheels, wheel_bolts, image, wheel_counter, repeated_list, now_ms):
                return     # a defect was handled: the track is cleared, stop for this frame

    # ------------------------------------------------------------------
    def _save_if_magnet_missing(self, wheels, image, wheel_counter):
        """L:1268-1301: a big wheel without a magnet is saved for diagnosis (not a defect)."""
        for wheel in wheels:
            _, _, _, wxmin, wxmax, wheel_class = wheel
            self.log.info(f"Detected {wheel_class} | Width = {wxmax - wxmin}")
            if wheel_class == "wheel" and (wxmax - wxmin) > self.p.magnet_check_min_wheel_w:
                folder = os.path.join(self.s.paths.magnet, datetime.datetime.now().strftime("%Y%m%d"))
                os.makedirs(folder, exist_ok=True)
                name = f"{self.s.side}{wheel_counter}_{datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg"
                cv2.imwrite(os.path.join(folder, name), image)
                self.log.info(f"Magnet not detected. Image saved at: {os.path.join(folder, name)}")
                return

    @staticmethod
    def _largest_magnet_per_wheel(magnets, wheels):
        """L:1329-1350: with several magnets keep the largest one inside each wheel."""
        if len(magnets) <= 1:
            return magnets
        best = {}
        for magnet in magnets:
            _, mymin, mymax, mxmin, mxmax, _ = magnet
            cx, cy = (mxmin + mxmax) / 2.0, (mymin + mymax) / 2.0
            area = (mxmax - mxmin) * (mymax - mymin)
            for wheel in wheels:
                _, wymin, wymax, wxmin, wxmax, _ = wheel
                if wxmin <= cx <= wxmax and wymin <= cy <= wymax:
                    key = (wxmin, wymin, wxmax, wymax)
                    if key not in best or area > best[key][0]:
                        best[key] = (area, magnet)
                    break
        return [item[1] for item in best.values()]

    def _track_magnet(self, magnet, wheels, wheel_bolts, image, wheel_counter, repeated_list, now_ms) -> bool:
        """One magnet, L:1352-1599. Returns True when a defect was handled."""
        p = self.p
        _, mymin, mymax, mxmin, mxmax, _ = magnet
        mcx, mcy = (mxmin + mxmax) / 2.0, (mymin + mymax) / 2.0
        magnet_h = max(1.0, mymax - mymin)

        wheel = None
        for item in wheels:
            _, wymin, wymax, wxmin, wxmax, wclass = item
            if wxmin <= mcx <= wxmax and wymin <= mcy <= wymax:
                wheel = {"xmin": wxmin, "ymin": wymin, "xmax": wxmax, "ymax": wymax, "class_name": wclass}
                break
        if not wheel:
            self.log.info("matched_wheel None due to this magnet_track_state clear  ")
            self.track.clear()
            return False

        # Reference point: a wheel_bolt inside the wheel, else the wheel centre shifted down.
        ref = None
        for bolt in wheel_bolts:
            _, bymin, bymax, bxmin, bxmax, _ = bolt
            bx, by = (bxmin + bxmax) / 2.0, (bymin + bymax) / 2.0
            if _inside(wheel, bx, by):
                ref = (bx, by)
                break
        if ref is None:
            self.log.info("Bolt missing → fallback to wheel center")
            ref = ((wheel["xmin"] + wheel["xmax"]) / 2.0, (wheel["ymin"] + wheel["ymax"]) / 2.0 + p.offset_px)
        side = _quadrant(mcx, mcy, ref[0], ref[1])

        last = self.track.get("last")
        current = {"ts_ms": now_ms, "centroid_y": mcy, "side": side, "wheel": wheel, "history": []}
        cy_history = (last.get("cy_history", []).copy() if last else []) + [mcy]
        current["cy_history"] = cy_history[-p.window_frames:]

        if not last:                                                     # first frame
            current["history"], current["cy_history"] = [side], [mcy]
            self.track["last"] = current
            return False
        if now_ms - last.get("ts_ms", now_ms) >= p.time_gap_reset_ms:    # time gap
            current["history"] = [side]
            self.track["last"] = current
            self.log.info(f"Time gap reset  image_name skip === {now_ms}")
            return False
        last_wheel = last.get("wheel")
        if last_wheel:                                                   # same wheel, and moving
            last_cx = (last_wheel["xmin"] + last_wheel["xmax"]) / 2.0
            curr_cx = (wheel["xmin"] + wheel["xmax"]) / 2.0
            tol_x = max(5.0, max(1.0, last_wheel["xmax"] - last_wheel["xmin"]) * p.same_wheel_scale)
            if abs(curr_cx - last_cx) > tol_x or curr_cx - last_cx == 0:
                current["history"] = [side]
                self.track["last"] = current
                self.log.info(f"Same wheel check skip: moved {curr_cx - last_cx} (tolerance {tol_x})")
                return False
        if abs(mcy - last["centroid_y"]) > max(p.min_vertical_px, magnet_h * p.vertical_factor):
            current["history"], current["cy_history"] = [side], [mcy]   # vertical jump
            self.track["last"] = current
            self.log.info("Vertical continuity reset")
            return False

        history = (last["history"] + [side])[-p.window_frames:]
        current["history"] = history
        if len(history) < p.window_frames:
            self.track["last"] = current
            return False
        if ("top_left" in history and "bottom_right" in history) or \
           ("top_right" in history and "bottom_left" in history):
            self.track.clear()                                            # diagonal conflict
            return False

        # The vote is logged only; the decision uses the vertical travel (L:1496-1518).
        required = int(math.ceil(p.window_frames * p.required_ratio))
        most = max(set(history), key=history.count)
        valid_wheel = wheel["xmin"] >= p.wheel_xmin and wheel["xmax"] <= p.wheel_xmax
        self.log.info(f" ==history.count=============={history.count(most)} required_count {required} and {valid_wheel}")
        cy_hist = current["cy_history"]
        if len(cy_hist) >= p.window_frames and valid_wheel:
            travel = max(cy_hist) - min(cy_hist)
            self.log.info(f"CY History: {cy_hist}, Range: {travel}")
            if travel < p.min_magnet_travel_px:
                self._report(wheel, (mxmin, mymin, mxmax, mymax), image, wheel_counter, repeated_list)
                self.track.clear()
                return True
        self.track["last"] = current
        return False

    def _report(self, wheel, magnet_box, image, wheel_counter, repeated_list):
        self.log.info(f"Wheel Skidding detected - magnet Y movement < {self.p.min_magnet_travel_px} pixels")
        box = (wheel["xmin"], wheel["ymin"], wheel["xmax"], wheel["ymax"])
        outcome = self.store.record(DEFECT_TYPE, wheel_counter, box, image, repeated_list,
                                    use_wheel_gate=True, extra_boxes=(("Magnet", magnet_box),))
        if outcome == INSERTED:
            self._copy_evidence()
        self.latest_frames.clear()

    def _copy_evidence(self):
        """L:1558-1585: the last frames (already moved to RAW_IMAGES) next to the defect."""
        folder = os.path.join(dated_dir(self.s.paths.defective),
                              datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f"))
        os.makedirs(folder, exist_ok=True)
        self.log.info(f"{DEFECT_TYPE} detected → copying {len(self.latest_frames)} images")
        for name in self.latest_frames:
            src = os.path.join(self.s.paths.raw, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(folder, name))
            else:
                self.log.warning(f"Image not found: {src}")
