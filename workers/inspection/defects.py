"""Turning a confirmed finding into a defect, the same way for every defect type (D-073 D9,
docs/reference/inspection_defect_pattern_checkWheelMissing.py):

  1. in-memory repeated check (the repeated-defects list; empty today, the lookup is disabled)
       -> images to REPEATED/<date>/ only
  2. 24 h DB dedupe (D-044) and, where the check asks for it, wheel <= total_wheel_count
       -> new defect: RAW copy to defect_raw/<date>/, boxed image to DEFECTIVE/<date>/,
          then the row in defects_table (images are always saved before the row, CONTRACTS 4.1)
       -> otherwise images to REPEATED/<date>/<wheel>/

File names are exactly those of LeftAlgorithm.py L:714-759 / L:1935-1996.
The SQL text is kept as in LeftAlgorithm.py (f-strings, B14 in INSPECTION.md 11).
"""

import datetime
import logging
import os

import cv2

from helpers import dated_dir, draw_red_box, now_stamp

INSERTED, DUPLICATE, REPEATED = "inserted", "duplicate", "repeated"
DEDUPE_HOURS = 24
DEDUPE_SIZE_PX = 50      # the size condition compares the box with itself (D-044), kept as it is


class DefectStore:
    """defects_table access for one side. `cur`/`conn` are a pymysql cursor/connection."""

    def __init__(self, conn, cur, settings, logger: logging.Logger):
        self.conn, self.cur, self.s, self.log = conn, cur, settings, logger

    # ---- DB --------------------------------------------------------------
    def already_generated(self, wheel_counter, defect_type, xmin, xmax, ymin, ymax) -> bool:
        """L:474-501: same wheel + type + side within 24 h. A DB error counts as "not found",
        as before (the following insert then fails and stops the program)."""
        try:
            since = (datetime.datetime.now() - datetime.timedelta(hours=DEDUPE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
            x_lo, x_hi = (xmax - xmin) - DEDUPE_SIZE_PX, (xmax - xmin) + DEDUPE_SIZE_PX
            y_lo, y_hi = (ymax - ymin) - DEDUPE_SIZE_PX, (ymax - ymin) + DEDUPE_SIZE_PX
            query = f"""
                SELECT * FROM defects_table
                WHERE create_datetime > '{since}'
                AND wheel_no = {wheel_counter}
                AND defect_type = '{defect_type}'
                AND defect_side = '{self.s.defect_side}'
                AND ({xmax} - {xmin}) BETWEEN {x_lo} AND {x_hi}
                AND ({ymax} - {ymin}) BETWEEN {y_lo} AND {y_hi}
            """
            self.cur.execute(query)
            return len(self.cur.fetchall()) > 0
        except Exception as e:
            self.log.error(f"checkIsDefectAlreadyGenerated() Exception is: {e}")
            return False

    def insert(self, defect_type, view_defect, wheel_no, xmin, xmax, ymin, ymax):
        """L:366-376. Raises on a DB error (the program then exits and is restarted, D-073 D7)."""
        query = (f"insert into defects_table(defect_side, defect_type, view_defect, wheel_no, xmin, xmax, ymin, ymax) "
                 f"values('{self.s.defect_side}','{defect_type}','{view_defect}','{wheel_no}', "
                 f"'{xmin}', '{xmax}', '{ymin}', '{ymax}');")
        self.cur.execute(query)
        self.conn.commit()

    def write_live_wheel_count(self, wheel_count):
        """R:2782-2803 (D-073 D3): live wheel number for the UI, row ID 1. Errors are logged,
        not raised, as in RightAlgorithm.py."""
        cur = None
        try:
            cur = self.conn.cursor()
            cur.execute("UPDATE wheel_counter_table SET WHEEL_COUNT = %s WHERE ID = 1", (wheel_count,))
            self.conn.commit()
        except Exception:
            self.log.exception("update_wheel_count failed")
        finally:
            if cur:
                cur.close()

    # ---- the shared steps --------------------------------------------------
    def record(self, defect_type, wheel_counter, box, image, repeated_list, use_wheel_gate=True,
               extra_boxes=()):
        """Steps 1-2 of the module doc. `box` = (xmin, ymin, xmax, ymax). Draws on `image`
        (as the old code did). `extra_boxes` = ((label, (xmin, ymin, xmax, ymax)), ...) drawn
        on the boxed image only. Returns INSERTED, DUPLICATE or REPEATED."""
        xmin, ymin, xmax, ymax = box
        stamp = now_stamp()
        name = f"{defect_type}{wheel_counter}"

        if is_repeated(defect_type, wheel_counter, repeated_list):
            folder = dated_dir(self.s.paths.repeated)
            self._save_pair(image, folder + f"RAW_{stamp}_{name}.jpg", folder + f"{stamp}_{name}.jpg",
                            defect_type, box, extra_boxes)
            return REPEATED

        found = self.already_generated(wheel_counter, defect_type, xmin, xmax, ymin, ymax)
        gate_ok = (not use_wheel_gate) or wheel_counter <= self.s.total_wheel_count
        if not found and gate_ok:
            raw_file = dated_dir(self.s.paths.defect_raw) + f"RAW_{stamp}_{defect_type}_{wheel_counter}.jpg"
            boxed_file = dated_dir(self.s.paths.defective) + f"{stamp}_{name}.jpg"
            self._save_pair(image, raw_file, boxed_file, defect_type, box, extra_boxes)
            self.log.info(f"{defect_type} defect generated on wheel counter {wheel_counter}")
            self.insert(defect_type, boxed_file, wheel_counter, xmin, xmax, ymin, ymax)
            return INSERTED

        folder = os.path.join(dated_dir(self.s.paths.repeated), str(wheel_counter))
        os.makedirs(folder, exist_ok=True)
        self._save_pair(image, os.path.join(folder, f"RAW_{stamp}_{name}.jpg"),
                        os.path.join(folder, f"{stamp}_{name}.jpg"), defect_type, box, extra_boxes)
        return DUPLICATE

    @staticmethod
    def _save_pair(image, raw_file, boxed_file, label, box, extra_boxes):
        cv2.imwrite(raw_file, image)
        xmin, ymin, xmax, ymax = box
        draw_red_box(image, label, xmin, ymin, xmax, ymax)
        for extra_label, (exmin, eymin, exmax, eymax) in extra_boxes:
            draw_red_box(image, extra_label, exmin, eymin, exmax, eymax)
        cv2.imwrite(boxed_file, image)


def is_repeated(defect_type, wheel_counter, repeated_list) -> bool:
    """L:458-472: a row of repeated_defects_table with the same name and wheel number.
    The list is always empty today (the lookup is disabled, L:2340-2341)."""
    for item in repeated_list:
        if item[2] == defect_type and int(item[8]) == wheel_counter:
            return True
    return False
