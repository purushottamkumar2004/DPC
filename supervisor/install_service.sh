#!/usr/bin/env bash
# Install the DPC Supervisor as a systemd service that starts on boot.
#
#   ./install_service.sh [--root DIR] [--python PATH] [--dry-run]
#
#   --root     deployed root (default ~/INSIGHTZZ, CONTRACTS 4.1)
#   --python   the Python that runs the Supervisor and the workers
#              (default: the python3 found in your PATH now - run this from the
#               environment that has pypylon installed)
#   --dry-run  print the filled unit and stop; nothing is written
#
# Run it as the site user (not root); it asks for sudo only to write the unit.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/dpc-supervisor.service"
UNIT=/etc/systemd/system/dpc-supervisor.service
ROOT="$HOME/INSIGHTZZ"
PYTHON="$(command -v python3 || true)"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    --python)  PYTHON="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }

USER_NAME="$(id -un)"
[ "$(id -u)" -ne 0 ] || fail "run as the site user, not root (the service runs as that user)"
ROOT="$(cd "$ROOT" 2>/dev/null && pwd)" || fail "root folder not found (scripts/setup_dpc_layout.sh creates it)"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || fail "no python3 found; pass --python /path/to/python3"
PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"
[ -f "$ROOT/code/supervisor/supervisor.py" ] || fail "$ROOT/code/supervisor/supervisor.py missing (code checkout)"
[ -f "$ROOT/config/workers.yaml" ] || fail "$ROOT/config/workers.yaml missing (copy supervisor/config/workers.basler-2cam.yaml)"
for value in "$USER_NAME" "$ROOT" "$PYTHON"; do
  case "$value" in *[[:space:]\|\&]*) fail "spaces or | & are not supported in: $value" ;; esac
done

# the unit must be able to start: config valid and the same Python can import the Supervisor
(cd "$ROOT/code/supervisor" && "$PYTHON" supervisor.py check-config -c "$ROOT/config/workers.yaml") >&2 \
  || fail "check-config failed; fix the workers file first"

RENDERED="$(sed -e "s|@USER@|$USER_NAME|g" -e "s|@ROOT@|$ROOT|g" \
                -e "s|@PYTHON_DIR@|$(dirname "$PYTHON")|g" -e "s|@PYTHON@|$PYTHON|g" "$TEMPLATE")"
if grep -q "@[A-Z_]*@" <<<"$RENDERED"; then fail "template placeholder left unfilled"; fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "$RENDERED"
  exit 0
fi

echo "Installing $UNIT (user $USER_NAME, root $ROOT, python $PYTHON)"
echo "$RENDERED" | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable dpc-supervisor
echo "Installed and enabled. Start now with:  sudo systemctl start dpc-supervisor"
echo "Status: systemctl status dpc-supervisor   Logs: journalctl -u dpc-supervisor -f   and $ROOT/logs/"
