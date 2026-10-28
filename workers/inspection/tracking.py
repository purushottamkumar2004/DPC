"""Wheel counting and plate (cycle start) recognition.

Each physical object is counted once across frames by spatial continuity (a watermark of
its edge), not by time. The direction of travel in the image is a per-side setting
(`motion`, D-072): Left frames move right-to-left (`rtl`, L:1775-1818), Right frames
left-to-right (`ltr`, R:1906-1935). The wheel counter is those two functions unchanged; the
plate tracker uses the same rule in the same direction (D-077).
"""

import logging


class WheelCounter:
    """Counts wheels (`wheel` + `half_wheel`) in the travel direction of this camera."""

    def __init__(self, motion: str, new_wheel_jump_px: float, logger: logging.Logger):
        self.motion = motion
        self.jump = new_wheel_jump_px
        self.log = logger
        self.count = 0
        self.watermark = None     # rtl: lowest xmin of the tracked wheel; ltr: highest xmax.
                                  # Never cleared, also not on plate reset (B6, kept)

    def reset_count(self):
        """At a plate: the cycle starts again at 0 (the watermark stays, L:2409/2417)."""
        self.count = 0

    def update(self, wheels, half_wheels) -> int:
        detections = wheels + half_wheels
        if not detections:
            return self.count
        if self.motion == "rtl":
            self._update_rtl(detections)
        else:
            self._update_ltr(detections)
        self.log.info("=====================================================")
        self.log.info(f"current wheel counter is ={self.count}")
        self.log.info("=====================================================")
        return self.count

    def _update_rtl(self, detections):
        # The leftmost wheel (smallest xmin) is the one closest to the exit (L:1789-1810).
        xmin = min(d[3] for d in detections)
        if self.watermark is None:
            self.log.info("NEW WHEEL DETECTED (R->L): Start of tracking session. Incrementing.")
            self.count += 1
            self.watermark = xmin
        elif xmin > self.watermark + self.jump:
            # The previous wheel left on the left side and a new one is now the leftmost.
            self.log.info(f"NEW WHEEL DETECTED (R->L): Current Xmin ({xmin}) > Last Xmin "
                          f"({self.watermark}) + Threshold. Incrementing.")
            self.count += 1
            self.watermark = xmin
        elif xmin < self.watermark:
            self.watermark = xmin           # same wheel moved further left

    def _update_ltr(self, detections):
        # The rightmost wheel (largest xmax) is the one closest to the exit (R:1914-1928).
        xmax = max(d[4] for d in detections)
        if self.watermark is None:
            self.log.info("NEW WHEEL DETECTED: Start of tracking session or after gap. Incrementing counter.")
            self.count += 1
            self.watermark = xmax
        elif xmax < self.watermark - self.jump:
            self.log.info(f"NEW WHEEL DETECTED: Current Xmax ({xmax}) < Last Xmax ({self.watermark}) "
                          f"minus Threshold. Incrementing counter.")
            self.count += 1
            self.watermark = xmax
        elif xmax > self.watermark:
            self.watermark = xmax           # same wheel moved further right


class PlateTracker:
    """Recognises each physical plate once (D-077): the same watermark logic as WheelCounter,
    in the same per-side direction (`motion`), because plate and wheels travel together.
      ltr (Right): rightmost xmax, high watermark; new when xmax drops more than the jump
                   below it (unchanged, L:2208-2243 / R:2598-2634).
      rtl (Left):  leftmost xmin, low watermark; new when xmin rises more than the jump
                   above it. Before D-077 Left used the ltr rule too (B5): one plate moving
                   left was reported again every `jump` px of its travel."""

    def __init__(self, motion: str, new_plate_jump_px: float, logger: logging.Logger):
        self.motion = motion
        self.jump = new_plate_jump_px
        self.log = logger
        self.watermark = None

    def is_new_plate(self, plates, frame_name) -> bool:
        if not plates:
            return False
        if self.motion == "rtl":
            edge = min(p[3] for p in plates)                      # leftmost xmin
            is_jump = self.watermark is not None and edge > self.watermark + self.jump
            moved_on = self.watermark is not None and edge < self.watermark
        else:
            edge = max(p[4] for p in plates)                      # rightmost xmax
            is_jump = self.watermark is not None and edge < self.watermark - self.jump
            moved_on = self.watermark is not None and edge > self.watermark
        if self.watermark is None:
            self.log.info(f"NEW PLATE DETECTED: First encounter. Frame: {frame_name}")
            self.watermark = edge
            return True
        if is_jump:
            self.log.info(f"NEW PLATE DETECTED: edge jump ({edge} vs {self.watermark}). New plate confirmed.")
            self.watermark = edge
            return True
        if moved_on:
            self.watermark = edge                                 # same plate moved further
        return False
