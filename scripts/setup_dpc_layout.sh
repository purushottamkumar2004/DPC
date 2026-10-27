#!/usr/bin/env bash
# setup_dpc_layout.sh : create the DPC folder layout under ONE root folder.
#
# - Run it as your normal user. Do NOT use sudo.
# - No users or groups are created; everything is owned by you.
# - Safe to re-run: only creates missing folders, never moves or deletes anything.
#
# Usage:
#   bash setup_dpc_layout.sh                 # creates ~/INSIGHTZZ
#   bash setup_dpc_layout.sh --dry-run       # show what would be created
#   bash setup_dpc_layout.sh --root /path    # use a different root folder
#
# Layout (everything under the root):
#   code/      git checkout of the platform
#   config/    site.yaml (holds secrets: chmod 600), rendered/ (generated legacy XMLs)
#   logs/      all logs
#   run/       pid files / sockets (may contain stale files after a reboot)
#   data/      frames, images, wheel_status, bolt_data, state, models

set -euo pipefail

ROOT="${DPC_ROOT:-$HOME/INSIGHTZZ}"
DRY_RUN=0

CAMERAS=(CAM1 CAM2)               # CAM1 = LEFT, CAM2 = RIGHT
SIDES=(LEFT RIGHT)
FRAME_SUBDIRS=(live _tmp INF RAW_IMAGES UI)
IMAGE_KINDS=(DEFECTIVE REPEATED RAW PLATE MAGNET TEMP)   # MAGNET: magnet diagnostics (D-072), TEMP (D-067)
WHEEL_STATUS_SUBDIRS=(LEFT RIGHT IMAGES IMAGES/LEFT IMAGES/RIGHT PDFS)   # IMAGES/<SIDE>: best image per wheel (D-075)
DATA_TOP=(bolt_data state models)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)    ROOT="${2:?--root needs a value}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -eq 0 ]]; then
  echo "ERROR: do not run this with sudo or as root: the folders would be owned by root." >&2
  echo "       Run it as your normal user:  bash $0" >&2
  exit 1
fi

run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi
}

# mkd <mode> <path>
mkd() { run mkdir -p "$2"; run chmod "$1" "$2"; }

DATA="$ROOT/data"

mkd 0755 "$ROOT"
mkd 0755 "$ROOT/code"
mkd 0700 "$ROOT/config"            # secrets live here: owner only
mkd 0700 "$ROOT/config/rendered"
mkd 0755 "$ROOT/logs"
mkd 0755 "$ROOT/run"
mkd 0755 "$DATA"

for cam in "${CAMERAS[@]}"; do
  for sub in "${FRAME_SUBDIRS[@]}"; do
    mkd 0755 "$DATA/frames/$cam/$sub"
  done
  mkd 0755 "$DATA/ui_live/$cam"
done

for kind in "${IMAGE_KINDS[@]}"; do
  for side in "${SIDES[@]}"; do
    mkd 0755 "$DATA/images/$kind/$side"
  done
done

for sub in "${WHEEL_STATUS_SUBDIRS[@]}"; do
  mkd 0755 "$DATA/wheel_status/$sub"
done

for d in "${DATA_TOP[@]}"; do
  mkd 0755 "$DATA/$d"
done

for side in "${SIDES[@]}"; do
  mkd 0755 "$DATA/bolt_data/$side"     # bolt candidate frames per side (D-072); cleanup covers it (B10)
done

# _tmp, INF, live, UI and ui_live must share one filesystem so link/rename is atomic.
if [[ $DRY_RUN -eq 0 ]]; then
  echo
  echo "Checking atomic hand-off folders share one filesystem..."
  ok=1
  for cam in "${CAMERAS[@]}"; do
    ref="$(stat -c %d "$DATA/frames/$cam/_tmp")"
    for d in "frames/$cam/INF" "frames/$cam/live" "frames/$cam/UI" "frames/$cam/RAW_IMAGES" "ui_live/$cam"; do
      dev="$(stat -c %d "$DATA/$d")"
      if [[ "$dev" != "$ref" ]]; then
        echo "  PROBLEM: $d is on a different filesystem than frames/$cam/_tmp" >&2
        ok=0
      fi
    done
  done
  if [[ $ok -eq 1 ]]; then echo "  OK"; else echo "  Fix the mounts before running FrameCapture." >&2; fi
fi

cat <<EOF

Done. Root: $ROOT

  code    $ROOT/code
  config  $ROOT/config      (put site.yaml here, chmod 600, never commit it)
  logs    $ROOT/logs
  run     $ROOT/run
  data    $DATA

Nothing was moved or deleted. Point the worker configs at these paths.
EOF
