"""Cycle restart check, LeftAlgorithm.py L:2172-2206 (INSPECTION.md 6), now a defect (D-073 D9).

At every new plate (the confirmation: the plate tracker reports each physical plate once),
if the revolution that just ended counted more than 0 but fewer than `expected_cycle_total`
wheels, wheels were missed. The defect carries the last wheel number and a zero box, as the
commented-out insert in the old code did. A cycle longer than expected is not reported (as before).
"""

DEFECT_TYPE = "Batch_Wheels_Missing"
NO_BOX = (0, 0, 0, 0)


class CycleRestartCheck:
    def __init__(self, settings, store, logger):
        self.s, self.store, self.log = settings, store, logger

    def run(self, last_wheel_count, image, repeated_list):
        expected = self.s.expected_cycle_total
        if not 0 < last_wheel_count < expected:
            return
        self.log.warning(f"!!! CYCLE RESTART !!! Expected {expected}, but only found {last_wheel_count}. "
                         f"Missing: {expected - last_wheel_count}")
        self.store.record(DEFECT_TYPE, last_wheel_count, NO_BOX, image, repeated_list, use_wheel_gate=True)
