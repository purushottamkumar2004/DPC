"""Stage one revolution, plate to plate, per side, WHILE it happens (D-104).

RAW_IMAGES is deleted after raw_image_hours (and earlier under disk pressure), so frames are
hard-linked into <staging>/<cycle>/<SIDE>/frames/ as they arrive, not collected afterwards.

Per side (independent; the cycle is staged when both sides are closed):
  armed       wait for the NEXT plate after the click (plates_seen grows above the baseline)
  collecting  every SCAN_INTERVAL_S link the new RAW frames with name >= the start plate frame
  closed      plates_seen grew by exactly 1: link up to (not including) the new plate frame and
              remove any frame >= it that was linked early (the heartbeat lags up to 10 s)
A side starts again (folder emptied, armed at the next plate) when:
  - inspection restarted (other pid, or plates_seen went back): frames are missing (INF backlog
    deleted at its start, D-073)
  - plates_seen jumped by 2 or more (a whole revolution passed unseen, e.g. this program was down)
  - the side stayed open longer than MAX_CYCLE_S (conveyor stopped)
When both sides are closed: up to plate_images plate frames per side are added from the PLATE
folder, spread evenly over the folder (many days, many light conditions; neighbouring frames
of one plate pass are near copies) and not already in the cycle; then cycle.json per side.
"""

import datetime
import os
import time
from typing import Callable, Dict, List, Optional

from common.config import model_package as mp
from jobs import set_phase
from transfer_errors import PermanentError
from transfer_files import (dir_size, frame_time_iso, is_frame_name, link_or_copy, remove_tree,
                            save_json)

SCAN_INTERVAL_S = 10
MAX_CYCLE_S = 6 * 3600          # a revolution takes minutes; 6 h open means the conveyor stopped
GB = 1024 ** 3


def frames_between(folder: str, start: str, end: Optional[str], after: Optional[str]) -> List[str]:
    """Frame names in folder with start <= name < end (no end: open) and name > after, sorted."""
    names = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                n = entry.name
                if (is_frame_name(n) and n >= start and (end is None or n < end)
                        and (after is None or n > after)):
                    names.append(n)
    except FileNotFoundError:
        return []
    return sorted(names)


def spread_sample(names: List[str], count: int) -> List[str]:
    """count names spread evenly over the sorted list (all of them when there are fewer)."""
    if count <= 0:
        return []
    if len(names) <= count:
        return list(names)
    return [names[(i * len(names)) // count] for i in range(count)]


def model_of_side(models_in_use: Dict[str, List[str]], side: str) -> Optional[str]:
    for name, sides in models_in_use.items():
        if side in sides:
            return name
    return None


class CycleCapture:
    def __init__(self, settings, plates, store, log, publish: Callable, clock=time.time):
        self.s = settings
        self.plates = plates
        self.store = store
        self.log = log
        self.publish = publish
        self.clock = clock
        self.sides = {side.side: side for side in settings.sides}
        self._next_scan: Dict[str, float] = {}

    # ---------------------------------------------------------------- job setup
    def start_job(self, job: dict) -> None:
        job["cycle"] = None
        job["sides"] = {side: self._armed(None) for side in self.sides}
        job["staging_other_bytes"] = dir_size(self.s.staging)       # earlier cycles still waiting
        job["restarts"] = 0

    @staticmethod
    def _armed(baseline: Optional[dict]) -> dict:
        return {"phase": "armed", "baseline": baseline, "start_frame": None, "end_frame": None,
                "start_pid": None, "start_plates_seen": None, "started_at": None, "last_linked": None,
                "frames": 0, "bytes": 0, "missing": 0, "plate_frames": 0, "cycle_wheels": None}

    def rearm_open_sides(self, job: dict) -> None:
        """After a failure was clicked again: sides that were not closed start at the next plate."""
        for side, st in job.get("sides", {}).items():
            if st["phase"] != "closed":
                self._restart_side(job, side, None, "resumed by a new click")

    def cycle_dir(self, job: dict) -> str:
        return os.path.join(self.s.staging, job["cycle"])

    # ---------------------------------------------------------------- step
    def step(self, job: dict) -> bool:
        """One look at every side. Returns True when something was linked or changed."""
        now = self.clock()
        changed = False
        for side in self.sides:
            changed |= self._step_side(job, side, now)
        all_armed = all(st["phase"] == "armed" for st in job["sides"].values())
        job["phase"] = job["state"] = "waiting_plate" if all_armed else "staging"
        if all(st["phase"] == "closed" for st in job["sides"].values()):
            self._finish(job)
            return True
        job["message"] = self._progress_text(job)
        return changed

    def _progress_text(self, job: dict) -> str:
        waiting = [s for s, st in job["sides"].items() if st["phase"] == "armed"]
        if not waiting:
            return "collecting frames: " + ", ".join(f"{s} {st['frames']}" for s, st in job["sides"].items())
        lacking = [s for s in waiting if (self.plates.snapshot(s) or {}).get("no_plate_signal")
                   or self.plates.snapshot(s) is None]
        if lacking:
            return (f"waiting for the plate signal of {', '.join(lacking)} "
                    f"(inspection heartbeat with plates_seen, F76)")
        return f"waiting for the next plate on {', '.join(waiting)}"

    def _step_side(self, job: dict, side: str, now: float) -> bool:
        st = job["sides"][side]
        if st["phase"] == "closed":
            return False
        snap = self.plates.snapshot(side)
        if not snap or snap.get("no_plate_signal"):
            if st["phase"] == "collecting":
                self._collect(job, side, None, now)
                return self._check_age(job, side, now)
            return False
        pid, seen, frame = snap.get("pid"), snap["plates_seen"], snap.get("last_plate_frame")

        if st["phase"] == "armed":
            base = st["baseline"]
            if base is None or base["pid"] != pid or seen < base["plates_seen"]:
                st["baseline"] = {"pid": pid, "plates_seen": seen}      # wait for the next plate
                return base is None
            if seen > base["plates_seen"] and frame and is_frame_name(frame):
                self._start_side(job, side, pid, seen, frame, now)
                return True
            return False

        # collecting
        if pid != st["start_pid"] or seen < st["start_plates_seen"]:
            self._restart_side(job, side, {"pid": pid, "plates_seen": seen}, "inspection restarted (frames missing)")
            return True
        if seen >= st["start_plates_seen"] + 2:
            self._restart_side(job, side, {"pid": pid, "plates_seen": seen}, "a whole revolution passed unseen")
            return True
        if seen == st["start_plates_seen"] + 1 and frame and is_frame_name(frame) and frame > st["start_frame"]:
            self._collect(job, side, frame, now, force=True)
            self._close_side(job, side, frame, snap.get("last_cycle_wheels"))
            return True
        linked = self._collect(job, side, None, now)
        return self._check_age(job, side, now) or linked

    # ---------------------------------------------------------------- side transitions
    def _start_side(self, job, side, pid, seen, frame, now):
        if job["cycle"] is None:
            job["cycle"] = self._new_cycle_name(frame)
        st = job["sides"][side]
        st.update(phase="collecting", start_frame=frame, start_pid=pid, start_plates_seen=seen, started_at=now)
        os.makedirs(os.path.join(self.cycle_dir(job), side, "frames"), exist_ok=True)
        self._next_scan[side] = 0
        self.log.info(f"{job['cycle']} {side}: started at plate frame {frame}")

    def _restart_side(self, job, side, baseline, why):
        if job.get("cycle"):
            remove_tree(os.path.join(self.cycle_dir(job), side))
        job["sides"][side] = self._armed(baseline)
        job["restarts"] = int(job.get("restarts", 0)) + 1
        job["last_restart"] = f"{side}: {why}"
        self.log.warning(f"{job.get('cycle')} {side}: {why}; starting again at the next plate")

    def _check_age(self, job, side, now) -> bool:
        st = job["sides"][side]
        if st["started_at"] and now - st["started_at"] > MAX_CYCLE_S:
            self._restart_side(job, side, None, f"no closing plate within {MAX_CYCLE_S // 3600} h")
            return True
        return False

    def _close_side(self, job, side, end_frame, cycle_wheels):
        st = job["sides"][side]
        frames_dir = os.path.join(self.cycle_dir(job), side, "frames")
        for name in os.listdir(frames_dir):              # linked early, belong to the next cycle
            if name >= end_frame:
                path = os.path.join(frames_dir, name)
                st["bytes"] -= os.path.getsize(path)
                st["frames"] -= 1
                os.remove(path)
        st.update(phase="closed", end_frame=end_frame,
                  cycle_wheels=cycle_wheels if isinstance(cycle_wheels, int) else None)
        self.log.info(f"{job['cycle']} {side}: closed at plate frame {end_frame}, {st['frames']} frames"
                      + (f", {st['missing']} deleted before they could be linked" if st["missing"] else ""))

    # ---------------------------------------------------------------- linking
    def _collect(self, job, side, end: Optional[str], now: float, force: bool = False) -> bool:
        if not force and now < self._next_scan.get(side, 0):
            return False
        self._next_scan[side] = now + SCAN_INTERVAL_S
        st = job["sides"][side]
        source = self.sides[side].raw_images
        target = os.path.join(self.cycle_dir(job), side, "frames")
        os.makedirs(target, exist_ok=True)
        names = frames_between(source, st["start_frame"], end, st["last_linked"])
        for name in names:
            if link_or_copy(os.path.join(source, name), os.path.join(target, name)):
                st["frames"] += 1
                st["bytes"] += os.path.getsize(os.path.join(target, name))
            else:
                st["missing"] += 1
            st["last_linked"] = name
        if names:
            self._check_cap(job)
        return bool(names)

    def _check_cap(self, job):
        used = job.get("staging_other_bytes", 0) + sum(st["bytes"] for st in job["sides"].values())
        cap = self.s.upload.staging_max_gb * GB
        if used > cap:
            remove_tree(self.cycle_dir(job))
            for side in job["sides"]:
                job["sides"][side] = self._armed(None)
            job["cycle"] = None
            job["phase"] = "waiting_plate"
            raise PermanentError(f"staging full: {used / GB:.1f} GB > staging_max_gb "
                                 f"{self.s.upload.staging_max_gb:g}; partial cycle deleted")

    # ---------------------------------------------------------------- naming
    def _new_cycle_name(self, start_frame: str) -> str:
        """<YYYY-MM-DD>_cycle<N>, N counted per date in counters.json (D-104)."""
        date = frame_time_iso(start_frame)[:10]
        counters = self.store.load_other("counters", {})
        n = int(counters.get(date, 0)) + 1
        while os.path.exists(os.path.join(self.s.staging, f"{date}_cycle{n}")):
            n += 1
        counters[date] = n
        self.store.save_other("counters", counters)
        return f"{date}_cycle{n}"

    # ---------------------------------------------------------------- finish
    def _finish(self, job):
        for side in self.sides:
            self._add_plate_frames(job, side)
            save_json(os.path.join(self.cycle_dir(job), side, "cycle.json"), self._cycle_info(job, side))
        set_phase(job, "packing", "packing chunks")
        frames = {s: st["frames"] for s, st in job["sides"].items()}
        plates = {s: st["plate_frames"] for s, st in job["sides"].items()}
        total = sum(st["bytes"] for st in job["sides"].values())
        self.log.info(f"{job['cycle']} staged: frames {frames}, plate frames {plates}, {total / GB:.2f} GB")
        self.publish("cycle_staged", {"cycle": job["cycle"], "frames": frames, "plate_frames": plates,
                                      "bytes": total})

    def _add_plate_frames(self, job, side):
        count = self.s.upload.plate_images
        st = job["sides"][side]
        source = self.sides[side].plate
        target = os.path.join(self.cycle_dir(job), side, "PLATE")
        try:
            names = sorted(n for n in os.listdir(source) if is_frame_name(n)
                           and not (st["start_frame"] <= n < st["end_frame"]))
        except FileNotFoundError:
            names = []
        os.makedirs(target, exist_ok=True)
        linked = 0
        for name in spread_sample(names, count):
            if link_or_copy(os.path.join(source, name), os.path.join(target, name)):
                linked += 1
                st["bytes"] += os.path.getsize(os.path.join(target, name))
        st["plate_frames"] = linked

    def _cycle_info(self, job, side) -> dict:
        st = job["sides"][side]
        cam = self.sides[side]
        model = model_of_side(self.s.models_in_use, side)
        try:
            version = mp.read_current(os.path.join(self.s.models, model)) if model else None
        except mp.ModelPackageError as exc:          # information only: never stop the upload for it
            self.log.warning(f"cycle.json {side}: model version unknown ({exc})")
            version = None
        return {"site": self.s.site_id, "site_name": self.s.site_name, "cycle": job["cycle"], "side": side,
                "camera": {"id": cam.camera_id, "serial": cam.serial, "fps": cam.fps,
                           "exposure_us": cam.exposure_us, "rotate": cam.rotate},
                "frames": st["frames"], "frames_missing": st["missing"],
                "start_frame": st["start_frame"], "end_frame": st["end_frame"],
                "start_time": frame_time_iso(st["start_frame"]), "end_time": frame_time_iso(st["end_frame"]),
                "plate_frames": st["plate_frames"], "total_wheel_count": self.s.total_wheel_count,
                "cycle_wheels": st["cycle_wheels"], "model": {"name": model, "version": version},
                "created_at": datetime.datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")}
