#!/usr/bin/env python3
"""Measure tool for the setup wizard (STATUS S6; D-103 steps 3 and 7, D-077, D-086).

    python3 workers/inspection/measure.py --frames <folder> (--side LEFT | --model <name>) \\
        [--version <v>] [--count 40] [--threshold 0.5] [--models-dir <dir>] [--json]

Runs a model package on the newest N `.jpg` frames of a folder (live RAW_IMAGES, or the frames the
wizard grabbed) and returns:
  1. sizes: per class the median box width and height (plus the 10th / 90th percentiles and the
     number of boxes), and a proposal for `object_sizes` (wheel_w, wheel_h, bucket_clip_w, chain_w,
     half_bucket_clip_w) taken from those medians;
  2. direction: the conveyor's movement in this camera's image, from the wheel's x positions over
     consecutive frames: `rtl` (x decreases), `ltr` (x increases) or `unknown`, with the evidence.
     D-077: the direction is never assumed; the wizard shows it and a person confirms it.

Model: `--side` takes the model name and threshold from `<root>/config/rendered/inspection_<side>.json`
(per-side models, D-086); `--model` names it directly. Without `--version`, the version `current`
points to. Every file's sha256 is checked (common/config/model_package.py).

Read-only by design: no DB access, no file is moved, deleted or written; `current` is not touched.
Exit 0 when the measurement ran (the direction may still be `unknown`), 1 with the reason otherwise.
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))      # <root>/code
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, HERE)

import cv2           # noqa: E402
import numpy as np   # noqa: E402

from common.config.model_package import ModelPackageError, read_current, verify_package   # noqa: E402
from helpers import frame_time                                                         # noqa: E402
from model_selftest import default_models_dir, newest_frames                           # noqa: E402

DEFAULT_COUNT = 40              # about 10 s at 4 fps: several wheels pass
DEFAULT_THRESHOLD = 0.5
MIN_STEP_PX = 3                 # a smaller x step between frames = not moving (belt stopped)
MAX_MATCH_FRACTION = 0.5        # the same wheel moved less than half a wheel width between frames
MAX_FRAME_GAP_S = 2.0           # frames further apart are not consecutive
MIN_MOVING_PAIRS = 5            # fewer moving steps: direction unknown
MIN_CONSISTENCY = 0.8           # share of moving steps that must agree
# object_sizes key -> (class, dimension); for chain_w the first class the model has
SIZE_KEYS = {"wheel_w": (("wheel",), "w"), "wheel_h": (("wheel",), "h"),
             "bucket_clip_w": (("bucket_clip",), "w"), "half_bucket_clip_w": (("half_bucket_clip",), "w"),
             "chain_w": (("chain", "bottom_chain", "top_chain"), "w")}

logger = logging.getLogger("measure")


# ----------------------------------------------------------------------
# Sizes
# ----------------------------------------------------------------------
def class_sizes(boxes_by_class):
    """{class: {boxes, w_median, h_median, w_p10, w_p90, h_p10, h_p90}} from (w, h) lists."""
    out = {}
    for cls in sorted(boxes_by_class):
        wh = np.array(boxes_by_class[cls], float)
        out[cls] = {"boxes": int(len(wh)),
                    "w_median": int(round(np.median(wh[:, 0]))), "h_median": int(round(np.median(wh[:, 1]))),
                    "w_p10": int(round(np.percentile(wh[:, 0], 10))), "w_p90": int(round(np.percentile(wh[:, 0], 90))),
                    "h_p10": int(round(np.percentile(wh[:, 1], 10))), "h_p90": int(round(np.percentile(wh[:, 1], 90)))}
    return out


def propose_object_sizes(sizes):
    """object_sizes proposal from the medians: {key: {value, class, boxes}} or None per key."""
    proposal = {}
    for key, (classes, dim) in SIZE_KEYS.items():
        cls = next((c for c in classes if c in sizes), None)
        proposal[key] = None if cls is None else {
            "value": sizes[cls][f"{dim}_median"], "class": cls, "boxes": sizes[cls]["boxes"],
            "p10": sizes[cls][f"{dim}_p10"], "p90": sizes[cls][f"{dim}_p90"]}
    return proposal


# ----------------------------------------------------------------------
# Direction
# ----------------------------------------------------------------------
def wheel_steps(frames):
    """frames: [(time or None, [(cx, width), ...]) ...] in order. For each pair of consecutive frames,
    each wheel is matched to the nearest wheel of the next frame (within half a wheel width);
    returns the x steps and the number of pairs used."""
    steps, pairs = [], 0
    for (t1, w1), (t2, w2) in zip(frames, frames[1:]):
        if not w1 or not w2:
            continue
        if t1 is not None and t2 is not None and not 0 < (t2 - t1).total_seconds() <= MAX_FRAME_GAP_S:
            continue
        pairs += 1
        for cx, width in w1:
            nearest = min(w2, key=lambda w: abs(w[0] - cx))
            if abs(nearest[0] - cx) <= MAX_MATCH_FRACTION * width:
                steps.append(nearest[0] - cx)
    return steps, pairs


def direction_from(frames):
    steps, pairs = wheel_steps(frames)
    moving = [s for s in steps if abs(s) >= MIN_STEP_PX]
    evidence = {"frames_with_wheel": sum(1 for _, w in frames if w), "frame_pairs": pairs,
                "wheel_steps": len(steps), "moving_steps": len(moving),
                "mean_x_step": round(float(np.mean(moving)), 1) if moving else None,
                "median_x_step": round(float(np.median(moving)), 1) if moving else None,
                "consistency": None}
    if len(moving) < MIN_MOVING_PAIRS:
        reason = ("no consecutive frames with a wheel" if not steps else
                  f"only {len(moving)} moving wheel steps (min {MIN_MOVING_PAIRS}): conveyor stopped or too few frames")
        return "unknown", evidence, reason
    median = float(np.median(moving))
    agree = sum(1 for s in moving if (s < 0) == (median < 0)) / len(moving)
    evidence["consistency"] = round(agree, 2)
    if agree < MIN_CONSISTENCY:
        return "unknown", evidence, f"only {agree:.0%} of the moving steps agree (min {MIN_CONSISTENCY:.0%})"
    direction = "rtl" if median < 0 else "ltr"
    return direction, evidence, f"{len(moving)} moving steps, {agree:.0%} in the same direction"


# ----------------------------------------------------------------------
def measure_frames(frame_paths, detector, out=lambda text: None):
    """Runs the detector on the frames (oldest first). Returns {frames, sizes, object_sizes, direction}."""
    sizes = defaultdict(list)
    track, read = [], 0
    for path in frame_paths:
        image = cv2.imread(path)                       # read-only
        if image is None:
            out(f"{os.path.basename(path)}: unreadable, skipped")
            track.append((None, []))                   # breaks the consecutive pair
            continue
        read += 1
        wheels = []
        for det in detector.detect(image):
            cls = detector.label(det[5])
            _, ymin, ymax, xmin, xmax, _ = det
            sizes[cls].append((xmax - xmin, ymax - ymin))
            if cls == "wheel":                          # full wheels only: half wheels are cut at the edge
                wheels.append(((xmin + xmax) / 2.0, xmax - xmin))
        try:
            t = frame_time(path)
        except ValueError:
            t = None
        track.append((t, wheels))
    per_class = class_sizes(sizes)
    direction, evidence, reason = direction_from(track)
    return {"frames": {"given": len(frame_paths), "read": read},
            "sizes": per_class, "object_sizes": propose_object_sizes(per_class),
            "direction": {"value": direction, "reason": reason, **evidence}}


def _real_detector(package, threshold):
    from model import Detector                         # Detectron2 / GPU only here
    return Detector(package, threshold, logger)


def run(frames_dir, count, models_dir, model_name, version=None, threshold=DEFAULT_THRESHOLD,
        make_detector=None, out=print):
    """Returns (exit_code, result). result has `error` on failure."""
    make_detector = make_detector or _real_detector
    if not os.path.isdir(frames_dir):
        return 1, {"error": f"frames folder not found: {frames_dir}"}
    frames = newest_frames(frames_dir, count)
    if not frames:
        return 1, {"error": f"no .jpg frames in {frames_dir}"}
    try:
        version = version or read_current(os.path.join(models_dir, model_name))
        if not version:
            return 1, {"error": f"model {model_name}: no 'current' version and no --version given"}
        package = verify_package(os.path.join(models_dir, model_name, version), name=model_name, version=version)
    except ModelPackageError as e:
        return 1, {"error": f"package {model_name} {version} is not usable: " + "; ".join(e.problems)}
    try:
        detector = make_detector(package, threshold)
    except Exception as e:
        return 1, {"error": f"model does not load: {type(e).__name__}: {e}"}
    try:
        result = measure_frames(frames, detector, out)
    except Exception as e:
        return 1, {"error": f"inference failed: {type(e).__name__}: {e}"}
    if result["frames"]["read"] == 0:
        return 1, {"error": f"none of the {len(frames)} frames could be read"}
    result["model"] = {"name": package.name, "version": package.version}
    result["frame_names"] = {"first": os.path.basename(frames[0]), "last": os.path.basename(frames[-1])}
    return 0, result


def side_settings(root, side):
    """(model name, threshold) of a side from its rendered inspection JSON."""
    path = os.path.join(root, "config", "rendered", f"inspection_{side.lower()}.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["model"]["name"], float(data.get("threshold", DEFAULT_THRESHOLD))


def print_text(result):
    d = result["direction"]
    print(f"model {result['model']['name']} {result['model']['version']}, frames read {result['frames']['read']} "
          f"of {result['frames']['given']} ({result['frame_names']['first']} .. {result['frame_names']['last']})")
    print(f"direction: {d['value']}  ({d['reason']}; mean x step {d['mean_x_step']}, "
          f"consistency {d['consistency']}, frames with wheel {d['frames_with_wheel']})")
    print("object_sizes proposal (medians):")
    for key, p in result["object_sizes"].items():
        print(f"  {key}: " + ("no boxes" if p is None else
                              f"{p['value']}  (class {p['class']}, {p['boxes']} boxes, p10 {p['p10']}, p90 {p['p90']})"))
    print("sizes per class (median w x h, boxes):")
    for cls, s in result["sizes"].items():
        print(f"  {cls}: {s['w_median']} x {s['h_median']}  ({s['boxes']})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", required=True, help="folder with frames, e.g. data/frames/CAM1/RAW_IMAGES")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--side", choices=("LEFT", "RIGHT"), help="model and threshold from inspection_<side>.json")
    who.add_argument("--model", help="model package name")
    ap.add_argument("--version", help="version to use (default: the one 'current' points to)")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"newest N frames (default {DEFAULT_COUNT})")
    ap.add_argument("--threshold", type=float, help=f"score threshold (default: the side's, else {DEFAULT_THRESHOLD})")
    ap.add_argument("--models-dir", default=default_models_dir(), help="default <root>/data/models")
    ap.add_argument("--json", action="store_true", help="print the result as JSON only")
    args = ap.parse_args(argv)
    if args.count < 2:
        ap.error("--count must be at least 2 (the direction needs consecutive frames)")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    name, threshold = args.model, args.threshold
    if args.side:
        root = os.path.dirname(REPO_ROOT)
        try:
            name, side_threshold = side_settings(root, args.side)
        except (OSError, KeyError, ValueError) as e:
            result = {"error": f"rendered config of {args.side} not readable: {e}"}
            print(json.dumps(result) if args.json else f"MEASURE FAIL: {result['error']}")
            return 1
        threshold = threshold if threshold is not None else side_threshold
    threshold = DEFAULT_THRESHOLD if threshold is None else threshold

    code, result = run(args.frames, args.count, args.models_dir, name, args.version, threshold,
                       out=(lambda t: None) if args.json else print)
    if args.json:
        print(json.dumps(result))
    elif code:
        print(f"MEASURE FAIL: {result['error']}")
    else:
        print_text(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
