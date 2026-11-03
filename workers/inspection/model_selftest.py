#!/usr/bin/env python3
"""Model self-test before a model switch (STATUS F47, for P11; D-056, D-060, D-086).

    python3 workers/inspection/model_selftest.py --model <name> --version <v> --frames <folder> [--count 5]

Loads the package `<models_dir>/<name>/<version>/` (every file's sha256 checked by
common/config/model_package.py), runs it on the newest N `.jpg` frames in the folder, and prints
per frame the number of detections per class (the names the checks use, after the package's label
map and the legacy mapping, as inspection does).

Exit 0: the package loads and at least one `wheel` is found in the frames.
Exit 1: anything else; the last line says why.

Read-only by design: it never writes to the DB, never moves, deletes or changes a frame, and never
touches `current` or `pinned` (the Supervisor switches, D-060). It uses the GPU while it runs, so the
Supervisor calls it at a quiet moment, like the switch itself.

`<models_dir>` defaults to `<root>/data/models`, with <root> taken from where this file lives
(`<root>/code/workers/inspection/`, CONTRACTS 4.1); `--models-dir` overrides it.
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))      # <root>/code
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, HERE)

import cv2  # noqa: E402

from common.config.model_package import ModelPackageError, verify_package   # noqa: E402

REQUIRED_CLASS = "wheel"
DEFAULT_COUNT = 5
DEFAULT_THRESHOLD = 0.5      # inspection drops scores <= 0.5 anyway (model.SCORE_FILTER)

logger = logging.getLogger("model_selftest")


def default_models_dir() -> str:
    return os.path.join(os.path.dirname(REPO_ROOT), "data", "models")


def newest_frames(folder: str, count: int):
    """The newest `count` .jpg files by name (frame names are capture times, CONTRACTS 4.1),
    oldest first. Only lists the folder."""
    names = sorted(n for n in os.listdir(folder)
                   if n.lower().endswith(".jpg") and os.path.isfile(os.path.join(folder, n)))
    return [os.path.join(folder, n) for n in names[-count:]] if count > 0 else []


def _real_detector(package, threshold):
    from model import Detector                       # Detectron2 / GPU only here
    return Detector(package, threshold, logger)


def run_selftest(models_dir, name, version, frames_dir, count=DEFAULT_COUNT, threshold=DEFAULT_THRESHOLD,
                 make_detector=None, out=print):
    """Returns (exit_code, summary dict). Prints one line per frame and a final PASS/FAIL line."""
    make_detector = make_detector or _real_detector      # looked up at call time (tests replace it)
    summary = {"model": name, "version": version, "frames": [], "totals": {}, "pass": False, "reason": ""}

    def fail(reason):
        summary["reason"] = reason
        out(f"SELFTEST FAIL: {reason}")
        return 1, summary

    version_dir = os.path.join(models_dir, name, version)
    try:
        package = verify_package(version_dir, name=name, version=version)
    except ModelPackageError as e:
        return fail(f"package {name} {version} is not usable: " + "; ".join(e.problems))
    out(f"package {package.name} {package.version} verified ({len(package.classes)} classes, "
        f"trained {package.trained_at}) in {package.dir}")

    if not os.path.isdir(frames_dir):
        return fail(f"frames folder not found: {frames_dir}")
    frames = newest_frames(frames_dir, count)
    if not frames:
        return fail(f"no .jpg frames in {frames_dir}")

    try:
        detector = make_detector(package, threshold)
    except Exception as e:                            # Detectron2 missing, CUDA, bad weights ...
        return fail(f"model does not load: {type(e).__name__}: {e}")

    totals, read = Counter(), 0
    for path in frames:
        frame = os.path.basename(path)
        image = cv2.imread(path)                      # read-only
        if image is None:
            out(f"{frame}: unreadable, skipped")
            summary["frames"].append({"frame": frame, "error": "unreadable"})
            continue
        try:
            detections = detector.detect(image)
        except Exception as e:
            return fail(f"inference failed on {frame}: {type(e).__name__}: {e}")
        read += 1
        counts = Counter(detector.label(d[5]) for d in detections)
        totals.update(counts)
        text = ", ".join(f"{cls}={n}" for cls, n in sorted(counts.items())) or "no detections"
        out(f"{frame}: {text}")
        summary["frames"].append({"frame": frame, "counts": dict(sorted(counts.items()))})
    summary["totals"] = dict(sorted(totals.items()))

    if read == 0:
        return fail(f"none of the {len(frames)} frames could be read")
    if totals[REQUIRED_CLASS] == 0:
        return fail(f"no '{REQUIRED_CLASS}' found in {read} frame(s); totals: {summary['totals'] or 'nothing'}")
    summary["pass"] = True
    out(f"SELFTEST PASS: {package.name} {package.version}, {read} frame(s), "
        f"{totals[REQUIRED_CLASS]} '{REQUIRED_CLASS}' detection(s)")
    return 0, summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="model name (package folder under the models folder)")
    ap.add_argument("--version", required=True, help="version folder to test (not necessarily 'current')")
    ap.add_argument("--frames", required=True, help="folder with saved frames, e.g. data/frames/CAM1/RAW_IMAGES")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"newest N frames (default {DEFAULT_COUNT})")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"score threshold (default {DEFAULT_THRESHOLD}; inspection uses its rendered value)")
    ap.add_argument("--models-dir", default=default_models_dir(), help="default <root>/data/models")
    ap.add_argument("--json", action="store_true", help="also print the summary as one JSON line at the end")
    args = ap.parse_args(argv)
    if args.count < 1:
        ap.error("--count must be at least 1")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    code, summary = run_selftest(args.models_dir, args.model, args.version, args.frames,
                                 args.count, args.threshold)
    if args.json:
        print(json.dumps(summary))
    return code


if __name__ == "__main__":
    sys.exit(main())
