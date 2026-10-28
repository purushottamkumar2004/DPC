"""The deletion rules of the old Maihar delete_script.py, unchanged in logic (STATUS P9).

Each rule works on ONE folder, deletes whole files only, logs every deleted file at DEBUG and
every problem at ERROR, never raises (one bad file or folder must not stop the pass), and
returns a Removed (files, bytes) so the pass can log one summary line.

Old function -> rule here:
  delete_old_files            -> delete_older_than (frame-name time, hours)
  delete_plate_data           -> delete_older_than (frame-name time, days)
  delete_extra_files          -> delete_all_files (INF)
  delete_repeated_defects_dirs-> keep_newest_in_date_folders (recursive)
  delete_defective_defects_dirs-> keep_newest_in_date_folders (top level only)
"""

import datetime
import os
from dataclasses import dataclass

FRAME_TIME_FORMAT = "%Y_%m_%d_%H_%M_%S_%f"      # CONTRACTS 4.1 frame names
DATE_FOLDER_FORMAT = "%Y%m%d"


@dataclass
class Removed:
    files: int = 0
    bytes: int = 0

    def add(self, other: "Removed"):
        self.files += other.files
        self.bytes += other.bytes


def _remove(path: str, removed: Removed, log) -> None:
    try:
        size = os.path.getsize(path)
        os.remove(path)
    except OSError as e:
        log.error(f"Error deleting '{path}': {e}")
        return
    removed.files += 1
    removed.bytes += size
    log.debug(f"Deleted {path}")


def frame_time(filename: str) -> datetime.datetime:
    """Time in a frame name `YYYY_MM_DD_HH_MM_SS_ffffff.jpg` (old rule)."""
    return datetime.datetime.strptime(os.path.splitext(filename)[0], FRAME_TIME_FORMAT)


def prefixed_frame_time(filename: str) -> datetime.datetime:
    """Time in a MAGNET name `<SIDE><wheel>_YYYY_MM_DD_HH_MM_SS_ffffff.jpg` (check_skidding.py L:75)."""
    base = os.path.splitext(filename)[0]
    return datetime.datetime.strptime(base.split("_", 1)[1], FRAME_TIME_FORMAT)


def delete_older_than(folder, max_age: datetime.timedelta, now, log,
                      time_of=frame_time, recursive=False) -> Removed:
    """Old delete_old_files / delete_plate_data: delete every .jpg whose name-time is older
    than max_age. A name that does not parse is logged and kept."""
    removed = Removed()
    if not os.path.isdir(folder):
        log.error(f"Directory '{folder}' does not exist.")
        return removed
    try:
        if recursive:
            entries = [(d, n) for d, _, names in os.walk(folder) for n in names]
        else:
            entries = [(folder, n) for n in os.listdir(folder)]
    except OSError as e:
        log.error(f"Error iterating directory '{folder}': {e}")
        return removed
    for directory, name in entries:
        if not name.lower().endswith(".jpg"):
            continue
        try:
            age = now - time_of(name)
        except (ValueError, IndexError) as e:
            log.error(f"Error processing file '{name}' in '{directory}': {e}")
            continue
        if age > max_age:
            _remove(os.path.join(directory, name), removed, log)
    log.debug(f"Finished processing directory: {folder}")
    return removed


def delete_all_files(folder, log) -> Removed:
    """Old delete_extra_files (INF): delete every file in the folder."""
    removed = Removed()
    if not os.path.isdir(folder):
        log.error(f"Extra directory '{folder}' does not exist.")
        return removed
    try:
        names = os.listdir(folder)
    except OSError as e:
        log.error(f"Error iterating extra directory '{folder}': {e}")
        return removed
    for name in names:
        path = os.path.join(folder, name)
        if os.path.isfile(path):          # whole files only, never a folder
            _remove(path, removed, log)
    return removed


def keep_newest_in_date_folders(parent, days: int, items: int, now, log, recursive: bool) -> Removed:
    """Old delete_repeated_defects_dirs (recursive=True) / delete_defective_defects_dirs
    (recursive=False). Only date folders `YYYYMMDD` that are at most `days` old are looked at
    (older ones are never touched, as before); in each, when it holds more than `items` files,
    the oldest by mtime are deleted until `items` remain."""
    removed = Removed()
    if not os.path.isdir(parent):
        log.error(f"Parent directory '{parent}' does not exist.")
        return removed
    try:
        subdirs = [os.path.join(parent, d) for d in os.listdir(parent)
                   if os.path.isdir(os.path.join(parent, d))]
    except OSError as e:
        log.error(f"Error processing parent directory '{parent}': {e}")
        return removed
    for subdir in subdirs:
        try:
            folder_date = datetime.datetime.strptime(os.path.basename(subdir), DATE_FOLDER_FORMAT)
        except ValueError as e:
            log.error(f"Error processing subdirectory '{subdir}': {e}")
            continue
        if now - folder_date > datetime.timedelta(days=days):
            continue
        try:
            if recursive:
                files = [os.path.join(d, n) for d, _, names in os.walk(subdir) for n in names]
            else:
                files = [os.path.join(subdir, n) for n in os.listdir(subdir)
                         if os.path.isfile(os.path.join(subdir, n))]
            log.debug(f"Checking data inside : {subdir}")
            if len(files) <= items:
                continue
            files.sort(key=os.path.getmtime)
        except OSError as e:
            log.error(f"Error processing subdirectory '{subdir}': {e}")
            continue
        for path in files[:len(files) - items]:
            _remove(path, removed, log)
    return removed
