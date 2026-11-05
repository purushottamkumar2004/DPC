#!/usr/bin/env python3
"""Learn the auto region of interest from SAVED frames, outside the running inspection (P13).

    python3 workers/inspection/roi_offline.py --side LEFT --frames <folder> --out <folder>
        [--config <rendered inspection_<side>.json>] [--version <model version>] [--whole] [--limit N]

Same calculation as the live learning (auto_roi.py): frames in order (names = capture time),
moving-wheel frames only, from the first plate to the next plate (`--whole`: every frame), then
roi.json + roi_preview.jpg are written to --out. Use it to check a preview before asking the live
program to learn, or to look at a past cycle.

Read-only for the site: frames are only read; nothing is written outside --out; the live
`data/state/inspection_<side>/` and the DB are never touched. It runs the model on the GPU, next
to live inspection: use it at a quiet time. To use its result live, a person may copy roi.json to
`data/state/inspection_<side>/` (inspection reads it at its next start); the normal way is the
request file (INSPECTION.md 13).
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, HERE)

import cv2  # noqa: E402

from auto_roi import (MotionJudge, PREVIEW_FILE, ROI_FILE, SCHEMA_VERSION, atomic_write_jpg,   # noqa: E402
                      atomic_write_json, box_of, draw_preview, learn_all, now_iso)
from settings import load_settings                                                            # noqa: E402
from tracking import PlateTracker                                                             # noqa: E402

logger = logging.getLogger("roi_offline")


def _real_detector(settings, version):
    from common.config.model_package import verify_package
    from model import Detector, load_package
    if version:
        package = verify_package(os.path.join(settings.models_dir, settings.model_name, version),
                                 name=settings.model_name, version=version)
    else:
        package = load_package(settings.models_dir, settings.model_name)
    return Detector(package, settings.threshold, logger), package


def learn_from_frames(settings, frame_paths, whole=False, detector=None, package_info=("", ""), out=print):
    """Returns (roi data or None, message)."""
    cfg = settings.auto_roi
    judge = MotionJudge(settings.motion, cfg.min_wheel_step_px, settings.tracker.new_wheel_jump_px)
    plates = PlateTracker(settings.motion, settings.tracker.new_plate_jump_px, logger)
    boxes, state = defaultdict(list), "active" if whole else "waiting"
    seen = used = 0
    size, preview = None, None
    for path in frame_paths:
        image = cv2.imread(path)
        if image is None:
            out(f"{os.path.basename(path)}: unreadable, skipped")
            continue
        by_class = defaultdict(list)
        for d in detector.detect(image):
            by_class[detector.label(d[5])].append(d)
        moving = judge.moved(by_class.get("wheel", []) + by_class.get("half_wheel", []))
        if state == "active":
            seen += 1
            if moving:
                used += 1
                size = (image.shape[1], image.shape[0])
                if preview is None or used % 500 == 0:
                    preview = image
                for cls, dets in by_class.items():
                    boxes[cls].extend((used, box_of(d)) for d in dets)
        if plates.is_new_plate(by_class.get("plate", []) + by_class.get("plate_area", []), os.path.basename(path)) \
                and not whole:
            if state == "waiting":
                state = "active"
                out(f"cycle starts at {os.path.basename(path)}")
            elif state == "active":
                out(f"cycle ends at {os.path.basename(path)}")
                state = "done"
                break
    if not whole and state != "done":
        return None, ("no complete cycle (two plates) in the frames" if state == "active"
                      else "no plate in the frames"), None
    if used == 0:
        return None, "no frame with a moving wheel", None
    data = {"schema_version": SCHEMA_VERSION, "side": settings.side,
            "learned_from": {"camera_id": settings.camera_id,
                             "model": {"name": package_info[0], "version": package_info[1]},
                             "frames_seen": seen, "frames_used": used, "frame_size": list(size),
                             "source": "roi_offline", "time": now_iso()},
            "settings": {k: getattr(cfg, k) for k in ("margin_px", "static_px", "static_share", "min_per_point",
                                                      "grid_steps", "top_percentile", "bottom_percentile",
                                                      "mad_factor", "min_wheel_step_px")},
            "classes": learn_all(boxes, used, size[0], cfg)}
    return data, f"{used} moving frames of {seen}", preview


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--side", required=True, choices=("LEFT", "RIGHT"))
    ap.add_argument("--frames", required=True, help="folder with saved frames (YYYY_MM_DD_HH_MM_SS_ffffff.jpg)")
    ap.add_argument("--out", required=True, help="folder for roi.json and roi_preview.jpg (created)")
    ap.add_argument("--config", help="rendered inspection_<side>.json (default <root>/config/rendered/...)")
    ap.add_argument("--version", help="model version to use (default: the one 'current' points to)")
    ap.add_argument("--whole", action="store_true", help="learn from every frame, not plate to plate")
    ap.add_argument("--limit", type=int, default=0, help="use at most N frames (0 = all)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    root = os.path.dirname(REPO_ROOT)
    config = args.config or os.path.join(root, "config", "rendered", f"inspection_{args.side.lower()}.json")
    settings = load_settings(config)
    frames = sorted(os.path.join(args.frames, n) for n in os.listdir(args.frames) if n.lower().endswith(".jpg"))
    if args.limit:
        frames = frames[:args.limit]
    if not frames:
        print(f"FAIL: no .jpg frames in {args.frames}")
        return 1
    detector, package = _real_detector(settings, args.version)
    data, message, preview = learn_from_frames(settings, frames, args.whole, detector, (package.name, package.version))
    if data is None:
        print(f"FAIL: {message}")
        return 1
    os.makedirs(args.out, exist_ok=True)
    atomic_write_json(os.path.join(args.out, ROI_FILE), data)
    atomic_write_jpg(os.path.join(args.out, PREVIEW_FILE), draw_preview(preview, data))
    for cls, entry in data["classes"].items():
        if entry.get("learned"):
            print(f"{cls}: learned, x {entry['x_range']}, {len(entry['points'])} points, boxes {entry['boxes']} "
                  f"(static dropped {entry['dropped_static']}, off-line dropped {entry['dropped_outliers']})")
        else:
            print(f"{cls}: not learned ({entry['reason']})")
    print(f"OK: {message}; wrote {os.path.join(args.out, ROI_FILE)} and {PREVIEW_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
