"""Wheel missing check: the developer's reference pattern
(docs/reference/inspection_defect_pattern_checkWheelMissing.py = LeftAlgorithm.py L:1919-2092
with the insert enabled), INSPECTION.md 7.6, D-073 D9.

Per wheel number (slot), frames without any `wheel`/`half_wheel` are counted and buffered.
When a wheel is seen again in the slot, or the counter moves on to the next slot, a gap of
`min_frames`..`belt_stop_frames` frames is `WheelMissing`. More than `belt_stop_frames`
missing frames means the belt stopped: the slot is cancelled.

`is_reported` is never reset (developer decision 2026-10-25, D-073 D9 note; B7): a slot
reported once is not reported again in this process run, also not in the next revolution.
"""

DEFECT_TYPE = "WheelMissing"


class WheelMissingCheck:
    def __init__(self, settings, store, logger):
        self.s, self.store, self.log = settings, store, logger
        self.p = settings.tracker.wheel_missing
        self.history = {}           # wheel number -> slot state (WHEEL_MISSING_HISTORY)

    def run(self, wheels, half_wheels, image, wheel_counter, repeated_list):
        is_new_slot = wheel_counter not in self.history
        if is_new_slot:
            self.history[wheel_counter] = {"missing_count": 0, "is_reported": False,
                                           "suspect_images": [], "belt_stopped": False}
            # The gap counted under the previous slot is evaluated now, or it would never be.
            previous = self.history.get(wheel_counter - 1)
            if previous is not None:
                if self._is_valid_gap(previous):
                    self._report(wheel_counter - 1, repeated_list)
                self._reset_gap(previous)

        slot = self.history[wheel_counter]
        if wheels or half_wheels:
            if self._is_valid_gap(slot):
                self._report(wheel_counter, repeated_list)
            self._reset_gap(slot)                      # is_reported intentionally stays
            return

        if slot["is_reported"] or slot["belt_stopped"]:
            return
        slot["missing_count"] += 1
        if slot["missing_count"] <= self.p.belt_stop_frames:
            slot["suspect_images"].append(image.copy())
        else:
            self.log.info(f"checkWheelMissing() belt stop detected for wheel_counter={wheel_counter}, "
                          f"cancelling defect and clearing buffer")
            slot["belt_stopped"] = True
            slot["suspect_images"].clear()

    def _is_valid_gap(self, slot) -> bool:
        return (self.p.min_frames <= slot["missing_count"] <= self.p.belt_stop_frames
                and not slot["belt_stopped"] and not slot["is_reported"]
                and len(slot["suspect_images"]) > 0)

    @staticmethod
    def _reset_gap(slot):
        slot["missing_count"] = 0
        slot["belt_stopped"] = False
        slot["suspect_images"].clear()

    def _report(self, wheel_counter, repeated_list):
        """Errors while saving are logged and do not stop inspection (as in the reference)."""
        slot = self.history[wheel_counter]
        slot["is_reported"] = True
        b = self.p.bbox
        try:
            self.store.record(DEFECT_TYPE, wheel_counter, (b.xmin, b.ymin, b.xmax, b.ymax),
                              slot["suspect_images"][-1], repeated_list, use_wheel_gate=True)
        except Exception:
            self.log.exception(f"checkWheelMissing() failed saving defect for slot {wheel_counter}")
