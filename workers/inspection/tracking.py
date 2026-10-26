"""Wheel counting and plate (cycle start) recognition.

Each physical object is counted once across frames by spatial continuity (a watermark of
its edge), not by time. The direction of travel in the image is a per-side setting
(`motion`, D-072): Left frames move right-to-left (`rtl`, L:1775-1818), Right frames
left-to-right (`ltr`, R:1906-1935). Logic and log texts are those two functions unchanged.
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
    """L:2208-2243, the same on both sides: a plate is "new" the first time, or when the
    rightmost plate xmax drops by more than the jump below the watermark. This assumes
    left-to-right travel also on the Left camera (B5 in INSPECTION.md, kept as it is)."""

    def __init__(self, new_plate_jump_px: float, logger: logging.Logger):
        self.jump = new_plate_jump_px
        self.log = logger
        self.watermark = None

    def is_new_plate(self, plates, frame_name) -> bool:
        if not plates:
            return False
        xmax = max(p[4] for p in plates)
        if self.watermark is None:
            self.log.info(f"NEW PLATE DETECTED: First encounter. Frame: {frame_name}")
            self.watermark = xmax
            return True
        if xmax < self.watermark - self.jump:
            self.log.info(f"NEW PLATE DETECTED: Xmax jump ({xmax} < {self.watermark}). New vehicle confirmed.")
            self.watermark = xmax
            return True
        if xmax > self.watermark:
            self.watermark = xmax
        return False
