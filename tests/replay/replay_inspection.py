#!/usr/bin/env python3
"""Replay saved frames through the OLD algorithm and the NEW inspection program and compare
their defects (STATUS P7 done-when). Runs on the site machine (GPU, Detectron2, MySQL).

    python3 tests/replay/replay_inspection.py --side LEFT --frames <folder of saved frames> \\
        [--config ~/INSIGHTZZ/config/rendered/inspection_left.json] [--old-ref main~0] \\
        [--first <frame name>] [--limit 3000] [--keep]

What it does, all under a scratch folder in /tmp (the live system is not touched, except
that it shares the GPU: stop live inspection for that side first):
  1. two scratch databases `<db>_replay_old` / `<db>_replay_new` from db/schema.sql + fixed_rows.sql
     (dropped and recreated; never the live DB)
  2. OLD: `git show <old-ref>:workers/inspection/{Left|Right}Algorithm.py` with its hardcoded
     /home/ddpc_maihar/INSIGHTZZ/ paths pointed into the scratch folder, and a legacy XML
     pointing at the scratch folders, the scratch DB and the SAME model package as the new run
  3. NEW: workers/inspection/inspection.py with a copy of the rendered JSON pointing at the
     scratch folders and DB (no mqtt: no heartbeat during the replay)
  4. each program: start, wait until it polls an empty INF, copy the frames in, wait until
     all are processed, stop
  5. compare the defects_table rows; write report.json and print a summary

Pass (exit 0): every `Nut_Bolt_Missing_In_Bucket_Clip` of the old run (wheel, box) is in the
new run and the new run has none extra. For RIGHT the old program saved bolt defects as
images only (its insert was commented out, R:843), so the old side is read from its
DEFECTIVE image names (type + wheel). New defect types (WheelMissing, Wheel_Skidding,
Batch_Wheels_Missing) are listed, not compared (the old program never inserted them).
"""

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
WORKER_DIR = os.path.join(REPO, "workers", "inspection")
sys.path.insert(0, REPO)
from common.config.model_package import read_current, verify_package   # noqa: E402

BOLT = "Nut_Bolt_Missing_In_Bucket_Clip"
NEW_TYPES = ("WheelMissing", "Wheel_Skidding", "Batch_Wheels_Missing")
LEGACY_PREFIX = "/home/ddpc_maihar/INSIGHTZZ/"
OLD_FILE = {"LEFT": "LeftAlgorithm.py", "RIGHT": "RightAlgorithm.py"}
OLD_XML = {"LEFT": "leftConfig", "RIGHT": "rightConfig"}
CAM = {"LEFT": "CAM1", "RIGHT": "CAM2"}
START_TIMEOUT_S = 300
IDLE_DONE_S = 15          # INF empty for this long = every frame processed (the move out of INF is last)
BOX_TOLERANCE_PX = 3      # GPU runs can differ by a pixel or two


def log(msg):
    print(f"[replay {datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------------
# Scratch databases
# ----------------------------------------------------------------------
def sql_statements(path):
    with open(path, encoding="utf-8") as f:
        text = re.sub(r"--[^\n]*", "", f.read())
    return [s.strip() for s in text.split(";") if s.strip()]


def create_db(db, name):
    if not re.fullmatch(r"[A-Za-z0-9_]+_replay_(old|new)", name):
        raise SystemExit(f"refusing to (re)create database {name!r}")
    conn = pymysql.connect(host=db["host"], port=db["port"], user=db["user"], passwd=db["password"])
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute(f"USE `{name}`")
        for f in ("schema.sql", "fixed_rows.sql"):
            for stmt in sql_statements(os.path.join(REPO, "db", f)):
                cur.execute(stmt)
    conn.commit()
    conn.close()


def read_defects(db, name):
    conn = pymysql.connect(host=db["host"], port=db["port"], user=db["user"], passwd=db["password"], db=name)
    with conn.cursor() as cur:
        cur.execute("SELECT defect_side, defect_type, wheel_no, xmin, xmax, ymin, ymax, view_defect "
                    "FROM defects_table ORDER BY ID")
        rows = cur.fetchall()
    conn.close()
    return [{"side": r[0], "type": r[1], "wheel": str(r[2]), "box": [r[3], r[4], r[5], r[6]], "image": r[7]}
            for r in rows]


# ----------------------------------------------------------------------
# Running one program
# ----------------------------------------------------------------------
def run_program(cmd, cwd, env, log_file, inf_dir, frames, done_marker):
    """Start, wait for the first idle poll, feed the frames, wait until all are processed, stop."""
    with open(os.path.join(os.path.dirname(log_file), "stdout.txt"), "ab") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        _wait_for(lambda: os.path.exists(log_file) and done_marker in open(log_file, errors="replace").read(),
                  START_TIMEOUT_S, proc, "program start (model load)")
        log(f"started; feeding {len(frames)} frames")
        for src in frames:
            shutil.copy2(src, os.path.join(inf_dir, os.path.basename(src)))
        idle_since = time.time()
        while True:
            if proc.poll() is not None:
                raise SystemExit(f"program exited with code {proc.returncode}; see {log_file}")
            if os.listdir(inf_dir):
                idle_since = time.time()
            if time.time() - idle_since > IDLE_DONE_S:
                break
            time.sleep(2)
        log("all frames processed")
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


def _wait_for(check, timeout, proc, what):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise SystemExit(f"program exited with code {proc.returncode} during {what}")
        if check():
            return
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {what}")


def run_old(args, cfg, package, scratch, frames):
    side = args.side
    base = os.path.join(scratch, "old")
    legacy = os.path.join(base, "legacy") + "/"
    code = subprocess.run(["git", "-C", REPO, "show", f"{args.old_ref}:workers/inspection/{OLD_FILE[side]}"],
                          check=True, capture_output=True, text=True).stdout
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, OLD_FILE[side]), "w") as f:
        f.write(code.replace(LEGACY_PREFIX, legacy))
    folders = {k: os.path.join(base, k) + "/" for k in ("LOGS", "DEFECTIVE", "RAW", "REPEATED")}
    inf = os.path.join(legacy, "CODE", "FRAME_CAPTURE", CAM[side], "INF") + "/"
    for d in list(folders.values()) + [inf, os.path.join(legacy, "CODE", "FRAME_CAPTURE", CAM[side], "RAW_IMAGES"),
                                       os.path.join(legacy, "TEMP_IMG_DIR", "PLATE_DIR", side),
                                       os.path.join(legacy, "MAGNET_NOT_DETECTED_DIR"),
                                       os.path.join(legacy, "WHEEL_STATUS", "IMAGES", side)]:
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(legacy, "bolt_data_left" if side == "LEFT" else "bolt_data"), exist_ok=True)

    tree = ET.parse(os.path.join(WORKER_DIR, f"{OLD_XML[side]}.example.xml"))
    r = tree.getroot()
    values = {(0, 1): folders["LOGS"], (0, 3): folders["DEFECTIVE"], (0, 4): inf, (0, 5): folders["RAW"],
              (0, 6): folders["REPEATED"], (0, 10): cfg["defect_side"], (0, 11): cfg["machine"]["total_wheel_count"],
              (1, 2): cfg["object_sizes"]["bucket_clip_w"], (1, 4): cfg["object_sizes"]["half_bucket_clip_w"],
              (2, 0): cfg["db"]["user"], (2, 1): cfg["db"]["password"], (2, 2): cfg["db"]["host"],
              (2, 3): cfg["db"]["name"] + "_replay_old",
              (3, 0): package.detectron_yaml, (3, 1): os.path.basename(package.weights), (3, 2): cfg["threshold"],
              (3, 3): os.path.dirname(package.weights) + "/", (3, 5): json.dumps(list(package.classes)),
              (3, 6): json.dumps(list(package.classes))}
    for (i, j), v in values.items():
        r[i][j].text = str(v)
    tree.write(os.path.join(base, f"{OLD_XML[side]}.xml"))
    os.chmod(os.path.join(base, f"{OLD_XML[side]}.xml"), 0o600)

    log(f"OLD run: {OLD_FILE[side]} from {args.old_ref}")
    run_program([sys.executable, OLD_FILE[side]], base, dict(os.environ),
                os.path.join(folders["LOGS"], OLD_FILE[side][:-2] + "log"), inf, frames,
                "No files found in directory")
    return folders["DEFECTIVE"]


def run_new(args, cfg, scratch, frames):
    base = os.path.join(scratch, "new")
    new_cfg = json.loads(json.dumps(cfg))
    for key, value in cfg["paths"].items():
        new_cfg["paths"][key] = value.replace(cfg["paths"]["root"], base, 1)
    new_cfg["db"]["name"] = cfg["db"]["name"] + "_replay_new"
    new_cfg.pop("mqtt", None)
    rendered = os.path.join(base, "config", "rendered")
    os.makedirs(rendered, exist_ok=True)
    for folder in new_cfg["paths"].values():
        os.makedirs(folder, exist_ok=True)
    os.makedirs(new_cfg["paths"]["inf"], exist_ok=True)
    path = os.path.join(rendered, f"{cfg['worker_id']}.json")
    with open(path, "w") as f:
        json.dump(new_cfg, f, indent=1)
    os.chmod(path, 0o600)
    env = dict(os.environ, DPC_WORKER_CONFIG=path)
    log("NEW run: workers/inspection/inspection.py")
    run_program([sys.executable, "inspection.py"], WORKER_DIR, env,
                os.path.join(new_cfg["paths"]["logs"], f"{cfg['worker_id']}.log"), new_cfg["paths"]["inf"],
                frames, "No files found in directory")


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------
def old_bolt_events_from_images(defective_dir):
    """RIGHT: '<ts>_Nut_Bolt_Missing_In_Bucket_Clip<wheel>.jpg' in DEFECTIVE/<date>/."""
    events = []
    for dirpath, _, names in os.walk(defective_dir):
        for n in sorted(names):
            m = re.fullmatch(rf"\d{{4}}(?:_\d+){{6}}_{BOLT}(\d+)\.jpg", n)
            if m:
                events.append({"type": BOLT, "wheel": m.group(1), "box": None})
    return events


def _same(a, b):
    if a["wheel"] != b["wheel"]:
        return False
    if a["box"] is None or b["box"] is None:          # RIGHT old side: images carry no box
        return True
    return all(abs(x - y) <= BOX_TOLERANCE_PX for x, y in zip(a["box"], b["box"]))


def compare(old_rows, new_rows):
    """Match every old bolt defect to one new bolt defect (same wheel, box within tolerance)."""
    old_bolts = [e for e in old_rows if e["type"] == BOLT]
    unmatched_new = [e for e in new_rows if e["type"] == BOLT]
    missing = []
    for old in old_bolts:
        match = next((n for n in unmatched_new if _same(old, n)), None)
        if match is None:
            missing.append({"wheel": old["wheel"], "box": old["box"]})
        else:
            unmatched_new.remove(match)
    extra = [{"wheel": e["wheel"], "box": e["box"]} for e in unmatched_new]
    counts = {t: sum(1 for e in new_rows if e["type"] == t) for t in NEW_TYPES}
    return {"old_bolt_defects": len(old_bolts), "new_bolt_defects": sum(1 for e in new_rows if e["type"] == BOLT),
            "bolt_missing_in_new": missing, "bolt_extra_in_new": extra, "new_types": counts,
            "pass": not missing and not extra}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--side", required=True, choices=("LEFT", "RIGHT"))
    ap.add_argument("--frames", required=True, help="folder with saved frames (YYYY_MM_DD_HH_MM_SS_ffffff.jpg)")
    ap.add_argument("--config", help="rendered inspection_<side>.json (default ~/INSIGHTZZ/config/rendered/...)")
    ap.add_argument("--old-ref", default="HEAD", help="git ref holding the old algorithm (default HEAD)")
    ap.add_argument("--first", help="first frame name to use (default: the oldest)")
    ap.add_argument("--limit", type=int, default=3000, help="number of frames (default 3000, about 12 min at 4 fps)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder and databases")
    args = ap.parse_args()

    config = args.config or os.path.expanduser(f"~/INSIGHTZZ/config/rendered/inspection_{args.side.lower()}.json")
    with open(config) as f:
        cfg = json.load(f)
    model_root = cfg["model"]["models_dir"]
    package = verify_package(os.path.join(model_root, read_current(model_root)), name=cfg["model"]["name"])
    frames = sorted(os.path.join(args.frames, n) for n in os.listdir(args.frames) if n.endswith(".jpg"))
    if args.first:
        frames = [p for p in frames if os.path.basename(p) >= args.first]
    frames = frames[:args.limit]
    if not frames:
        raise SystemExit("no frames selected")
    scratch = f"/tmp/dpc_replay_{args.side.lower()}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    os.makedirs(scratch)
    log(f"{len(frames)} frames {os.path.basename(frames[0])} .. {os.path.basename(frames[-1])}; "
        f"model {package.name} {package.version}; scratch {scratch}")

    db = {"host": cfg["db"]["host"], "port": cfg["db"]["port"], "user": cfg["db"]["user"],
          "password": cfg["db"]["password"]}
    old_db, new_db = cfg["db"]["name"] + "_replay_old", cfg["db"]["name"] + "_replay_new"
    create_db(db, old_db)
    create_db(db, new_db)

    old_defective = run_old(args, cfg, package, scratch, frames)
    run_new(args, cfg, scratch, frames)

    old_rows = read_defects(db, old_db)
    if args.side == "RIGHT":
        old_rows = old_bolt_events_from_images(old_defective)
    new_rows = read_defects(db, new_db)
    result = compare(old_rows, new_rows)
    result.update({"side": args.side, "frames": len(frames), "first": os.path.basename(frames[0]),
                   "last": os.path.basename(frames[-1]), "model": f"{package.name} {package.version}",
                   "old_ref": args.old_ref, "scratch": scratch})
    report = os.path.join(scratch, "report.json")
    with open(report, "w") as f:
        json.dump({"result": result, "old_rows": old_rows, "new_rows": new_rows}, f, indent=1, default=str)

    print(json.dumps(result, indent=1))
    print(f"{'PASS' if result['pass'] else 'FAIL'}: bolt defects old {result['old_bolt_defects']} / "
          f"new {result['new_bolt_defects']}; new types {result['new_types']}; report {report}")
    if not args.keep:
        conn = pymysql.connect(host=db["host"], port=db["port"], user=db["user"], passwd=db["password"])
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{old_db}`")
            cur.execute(f"DROP DATABASE IF EXISTS `{new_db}`")
        conn.close()
        log("scratch databases dropped (images and report kept in the scratch folder)")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
