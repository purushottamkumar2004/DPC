"""Best image per wheel and the daily copy, RightAlgorithm.py R:2826-2835, R:2957-3015,
R:3163-3193; on both sides since P7 (D-073 D4).

For every `wheel` detection the frame becomes that wheel number's best image when the wheel
centre is inside the centre band, or when the previous best image no longer exists. Once a
day, during the minute of `wheel_report.daily_time`, the list is written to
`<wheel_status_images>/wheel.txt` and each best image is copied to
`<wheel_status_images>/<YYYY-MM-DD>/wheel_<n>_<frame>.jpg` (assumed input of the wheel PDF).

Fixed in P7 (D4): a best image that no longer exists (deleted by cleanup) or was never set is
skipped with a warning; before, `shutil.copy` raised and inspection restarted.
"""

import datetime
import os
import shutil

from helpers import frame_time

# Centre band for the best image. Right's values (R:2960, R:2981), used on both sides.
# NOT in the rendered config yet: requested as a per-side key in the P7 handoff.
CENTER_BAND_X = (400, 850)


class WheelReport:
    def __init__(self, settings, logger):
        self.s, self.log = settings, logger
        # Right starts with an empty entry for wheel 1 (R:2829-2836); kept so wheel.txt has the
        # same lines, but its empty path is now skipped instead of crashing the copy.
        self.best = {1: {"xDiff": 0, "center_x": 0, "image_path": "", "timestamp": datetime.datetime.now()}}
        self.last_run_date = None

    def update(self, wheel_detection, wheel_counter, frame_path):
        """Called for every `wheel` detection, before this frame's counter update (R:2957-3015:
        the number used is the one from the previous frame)."""
        _, _, _, xmin, xmax, _ = wheel_detection
        center_x = (xmin + xmax) / 2
        final_path = os.path.join(self.s.paths.raw, os.path.basename(frame_path))
        previous = self.best.get(wheel_counter)
        if previous is None:
            update = True
        else:
            update = not os.path.exists(previous["image_path"]) or CENTER_BAND_X[0] < center_x < CENTER_BAND_X[1]
        if update and 0 < wheel_counter <= self.s.total_wheel_count:
            self.best[wheel_counter] = {"xDiff": xmax - xmin, "center_x": center_x,
                                        "image_path": final_path, "timestamp": frame_time(final_path)}

    def run_daily(self, now=None):
        """R:3163-3193: once per day, during the configured minute."""
        now = now or datetime.datetime.now()
        due = (now.hour, now.minute) == (self.s.daily_report_time.hour, self.s.daily_report_time.minute)
        if not due or self.last_run_date == now.date():
            return
        folder = self.s.paths.wheel_status_images
        day_folder = os.path.join(folder, now.strftime("%Y-%m-%d"))
        os.makedirs(day_folder, exist_ok=True)
        copied = skipped = 0
        with open(os.path.join(folder, "wheel.txt"), "w") as f:
            for wheel_no in sorted(self.best):
                data = self.best[wheel_no]
                f.write(f"{data['image_path']}, {wheel_no}, xDiff={data['xDiff']}\n")
                if not data["image_path"] or not os.path.isfile(data["image_path"]):
                    skipped += 1
                    continue
                shutil.copy(data["image_path"],
                            os.path.join(day_folder, f"wheel_{wheel_no}_" + os.path.basename(data["image_path"])))
                copied += 1
        if skipped:
            self.log.warning(f"Daily wheel report: {skipped} best image(s) no longer exist, skipped")
        self.log.info(f"Daily wheel report written: {copied} image(s) in {day_folder}")
        self.last_run_date = now.date()
