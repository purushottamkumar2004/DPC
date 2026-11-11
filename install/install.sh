#!/usr/bin/env bash
# install.sh : DPC installer, phase 0 of D-103 (STATUS S7).
#
# Turns a fresh Ubuntu 22.04 machine WITH internet into a DPC machine that shows the setup wizard:
#   1  system packages   git, python3-pip, mysql-server, mosquitto(+clients), curl; MySQL and mosquitto on
#   2  browser           Chromium for the kiosk screen (snap on Ubuntu 22.04)
#   3  code              git clone of a release tag into <root>/code
#   4  layout            <root>/{code,config,logs,run,data} via scripts/setup_dpc_layout.sh
#   5  Python packages   every worker's packages + ui/requirements.txt, for the same python3 the service uses
#   6  manual items      checks NVIDIA driver, torch + CUDA, detectron2, camera SDK (pypylon / PySpin)
#   7  boot service      supervisor/install_service.sh (systemd unit dpc-supervisor)
#   8  kiosk             autostart entry that opens the UI full-screen at login
#   9  start             starts the Supervisor; on a fresh machine it runs in setup mode (S1: only the UI)
#
# Run as the site user (NOT root, NOT with sudo); it asks for the sudo password when a step needs it.
# Safe to re-run: every step checks first and only does what is missing.
#
# Usage:
#   bash install.sh --tag v0.<n>            first install (tag required: sites run release tags, D-039)
#   bash install.sh                         re-run on an installed machine (code left as it is)
#   bash install.sh --tag v0.<n+1>          update the code of an installed machine to another tag
#   bash install.sh --dry-run --tag v0.<n>  print every step, change nothing
#   bash install.sh --check-only            only the checks of step 6 (after installing a manual item)
#
# Options:
#   --tag TAG      release tag to check out (git tag v0.<n>)
#   --ref BRANCH   a branch instead of a tag, ONLY for test machines (prints a warning)
#   --root DIR     deployed root (default ~/INSIGHTZZ, CONTRACTS 4.1)
#   --python PATH  the python3 for the Supervisor and every worker (default: python3 in PATH)
#   --repo URL     git repository (default: the DPC repo on GitHub)
#   --no-kiosk     skip the browser and the kiosk autostart (machine without a screen)
#   --dry-run      print every step, change nothing (checks still run: they only read)
#   --check-only   run only the manual-item checks
#   -h, --help     this text
#
# Exit: 0 done (manual items still missing are listed at the end), 1 a step failed, 2 wrong usage,
#       3 (--check-only) a manual item is missing.
# Log: <root>/logs/install_<date>_<time>.log (not in --dry-run / --check-only).

set -euo pipefail

# ---------------------------------------------------------------- fixed values (not site values)
REPO_URL_DEFAULT="https://github.com/purushottamkumar2004/DPC.git"
APT_PACKAGES=(git python3-pip mysql-server mosquitto mosquitto-clients curl)
SYSTEM_SERVICES=(mysql mosquitto)
SERVICE_NAME="dpc-supervisor"
# Packages the workers import (capture, inspection helpers, plc, mail, cleanup, transfer, supervisor).
# opencv-python-headless is added only when no cv2 is importable yet (an existing opencv-python
# and a second OpenCV package in the same Python break each other).
PIP_PACKAGES=(requests "paho-mqtt>=1.6" numpy pymysql "PyYAML>=5.4" "pymodbus>=3,<4")
PIP_OPENCV="opencv-python-headless"
# Never upgraded by this script when already installed: a newer numpy can break a torch/detectron2
# that was built against the installed one (they are installed by hand, step 6).
PIP_KEEP_AS_IS=(numpy torch torchvision)
MIN_PYTHON="3.10"                  # Django 5.2 (ui/requirements.txt)
UI_WAIT_SECONDS=120                # first UI start runs the Django migrations

# ---------------------------------------------------------------- options and state
ROOT="$HOME/INSIGHTZZ"
TAG=""
REF=""
REPO_URL="$REPO_URL_DEFAULT"
PY=""
DRY_RUN=0
CHECK_ONLY=0
NO_KIOSK=0
CODE_CHANGED=0                     # step 3 moved the checkout to another commit
CHECK_LINES=()                     # results of step 6, printed in the summary
MANUAL_MISSING=0
NOTES=()                           # other things the person must know, printed in the summary

# ---------------------------------------------------------------- output helpers
say()  { echo "$*"; }
step() { echo; echo "== $* =="; }
note() { echo "  NOTE: $*"; NOTES+=("$*"); }
fail() { echo "ERROR: $*" >&2; exit 1; }

# run <command...>: run it, or only print it in --dry-run
run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run]$(printf ' %q' "$@")"; else "$@"; fi
}

# run_sudo <command...>: the same with sudo (the password is asked once, in preflight)
run_sudo() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] sudo$(printf ' %q' "$@")"; else sudo "$@"; fi
}

usage() { sed -n '2,39p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tag)        TAG="${2:?--tag needs a value}"; shift 2 ;;
      --ref)        REF="${2:?--ref needs a value}"; shift 2 ;;
      --root)       ROOT="${2:?--root needs a value}"; shift 2 ;;
      --python)     PY="${2:?--python needs a value}"; shift 2 ;;
      --repo)       REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
      --no-kiosk)   NO_KIOSK=1; shift ;;
      --dry-run)    DRY_RUN=1; shift ;;
      --check-only) CHECK_ONLY=1; shift ;;
      -h|--help)    usage; exit 0 ;;
      *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
  done
  if [[ -n "$TAG" && -n "$REF" ]]; then
    echo "Use --tag or --ref, not both." >&2; exit 2
  fi
  ROOT="${ROOT%/}"
  CODE="$ROOT/code"
}

# ---------------------------------------------------------------- preflight
preflight() {
  step "0  Checks before installing"
  if [[ $EUID -eq 0 ]]; then
    fail "do not run this as root or with sudo: the files and the service must belong to the site user.
       Run it as that user:  bash $0 ...   (it asks for the sudo password itself)"
  fi
  case "$ROOT$PY" in
    *[[:space:]]*) fail "spaces in the root folder or the Python path are not supported: $ROOT $PY" ;;
  esac
  say "  user $(id -un), root $ROOT"

  local version_id=""
  if [[ -r /etc/os-release ]]; then
    version_id="$(. /etc/os-release && echo "${ID:-}-${VERSION_ID:-}")"
  fi
  if [[ "$version_id" == "ubuntu-22.04" ]]; then
    say "  Ubuntu 22.04: OK"
  else
    note "this installer is written for Ubuntu 22.04; this machine is '${version_id:-unknown}' (continuing)"
  fi

  PY="${PY:-$(command -v python3 || true)}"
  [[ -n "$PY" && -x "$PY" ]] || fail "no python3 found; pass --python /path/to/python3"
  if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info >= tuple(map(int, '$MIN_PYTHON'.split('.'))) else 1)"; then
    fail "$PY is Python $("$PY" -c 'import sys; print(sys.version.split()[0])'); $MIN_PYTHON or newer is needed"
  fi
  say "  Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

  if [[ $DRY_RUN -eq 0 && $CHECK_ONLY -eq 0 ]]; then
    say "  sudo is needed for system packages and the boot service; enter your password if asked."
    sudo -v || fail "sudo is not available for $(id -un); ask the machine's administrator"
  fi
}

start_log() {
  [[ $DRY_RUN -eq 0 && $CHECK_ONLY -eq 0 ]] || return 0
  mkdir -p "$ROOT/logs"
  LOG_FILE="$ROOT/logs/install_$(date +%Y%m%d_%H%M%S).log"
  exec > >(tee -a "$LOG_FILE") 2>&1
  say "Log: $LOG_FILE"
}

# ---------------------------------------------------------------- 1 system packages
is_apt_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

step_system_packages() {
  step "1  System packages"
  local missing=() pkg service
  for pkg in "${APT_PACKAGES[@]}"; do
    is_apt_installed "$pkg" || missing+=("$pkg")
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    say "  all installed: ${APT_PACKAGES[*]}"
  else
    say "  installing: ${missing[*]}"
    run_sudo apt-get update
    run_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
  # MySQL and the MQTT broker run on every boot (ARCHITECTURE 7); mosquitto 2 listens on this
  # machine only and needs no login by default, which is what the site uses (INSTALL step 1).
  for service in "${SYSTEM_SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$service" 2>/dev/null && systemctl is-active --quiet "$service" 2>/dev/null; then
      say "  $service: enabled and running"
    else
      run_sudo systemctl enable --now "$service"
    fi
  done
}

# ---------------------------------------------------------------- 2 browser
browser_command() {
  local name
  for name in chromium chromium-browser; do
    if command -v "$name" >/dev/null 2>&1; then echo "$name"; return 0; fi
  done
  return 1
}

step_browser() {
  step "2  Browser for the kiosk screen"
  if [[ $NO_KIOSK -eq 1 ]]; then say "  skipped (--no-kiosk)"; return 0; fi
  local found
  if found="$(browser_command)"; then
    say "  $found: installed"
    return 0
  fi
  # On Ubuntu 22.04 Chromium exists only as a snap ("apt install chromium-browser" installs the snap too).
  if command -v snap >/dev/null 2>&1; then
    run_sudo snap install chromium
  else
    run_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y chromium-browser
  fi
}

# ---------------------------------------------------------------- 3 code
remote_has_tag() {
  git ls-remote --exit-code --tags "$REPO_URL" "refs/tags/$1" >/dev/null 2>&1
}

list_remote_tags() {
  git ls-remote --tags --refs "$REPO_URL" 2>/dev/null | sed 's|.*refs/tags/||' | tr '\n' ' '
}

check_target_exists() {
  if [[ -n "$TAG" ]]; then
    remote_has_tag "$TAG" || fail "tag '$TAG' not found in $REPO_URL. Tags there: $(list_remote_tags)"
  elif [[ -n "$REF" ]]; then
    git ls-remote --exit-code --heads "$REPO_URL" "$REF" >/dev/null 2>&1 \
      || fail "branch '$REF' not found in $REPO_URL"
    note "--ref $REF: a branch, not a release tag. Only for test machines; sites run tags (D-039)."
  fi
}

step_code() {
  step "3  Code ($CODE)"
  if [[ -n "$TAG$REF" ]] && ! command -v git >/dev/null 2>&1 && [[ $DRY_RUN -eq 1 ]]; then
    say "  (git is not installed yet: the tag is checked when the real run clones)"
  elif [[ -n "$TAG$REF" ]]; then
    check_target_exists
  fi

  if [[ ! -d "$CODE/.git" ]]; then
    clone_code
  else
    update_code
  fi
}

clone_code() {
  if [[ -z "$TAG$REF" ]]; then
    fail "no code at $CODE yet: give the release to install, for example  --tag v0.1"
  fi
  if [[ -d "$CODE" ]] && [[ -n "$(ls -A "$CODE" 2>/dev/null)" ]]; then
    fail "$CODE exists, is not empty and is not a git checkout. Move it away first (nothing was changed)."
  fi
  run mkdir -p "$ROOT"
  # A tag given to --branch is checked out detached: the site runs exactly that release.
  run git -c advice.detachedHead=false clone --quiet --branch "${TAG:-$REF}" "$REPO_URL" "$CODE"
  CODE_CHANGED=1
  [[ $DRY_RUN -eq 1 ]] || say "  cloned ${TAG:-$REF}"
}

update_code() {
  local current target
  current="$(git -C "$CODE" rev-parse HEAD)"
  say "  checkout present at $(git -C "$CODE" describe --tags --always 2>/dev/null || echo "$current")"
  if [[ -z "$TAG$REF" ]]; then
    say "  no --tag given: code left as it is"
    return 0
  fi
  # Same rule as INSTALL.md "Updating an installed site": nobody edits files inside code/.
  if [[ -n "$(git -C "$CODE" status --porcelain --untracked-files=no)" ]]; then
    fail "files inside $CODE were changed by hand (git status shows them). Nothing was switched.
       Look at them with:  git -C $CODE status   and undo them before changing the release."
  fi
  run git -C "$CODE" fetch --quiet --tags origin
  if [[ -n "$TAG" ]]; then
    target="refs/tags/$TAG"
  else
    run git -C "$CODE" fetch --quiet origin "$REF"
    target="origin/$REF"
  fi
  if [[ $DRY_RUN -eq 0 ]] && [[ "$(git -C "$CODE" rev-parse "$target^{commit}")" == "$current" ]]; then
    say "  already at ${TAG:-$REF}: unchanged"
    return 0
  fi
  run git -C "$CODE" -c advice.detachedHead=false checkout --quiet --detach "$target"
  CODE_CHANGED=1
  [[ $DRY_RUN -eq 1 ]] || say "  switched to ${TAG:-$REF}"
}

# ---------------------------------------------------------------- 4 layout
step_layout() {
  step "4  Folder layout ($ROOT)"
  # The layout script only creates what is missing; it never moves or deletes anything.
  local args=(--root "$ROOT")
  [[ $DRY_RUN -eq 1 ]] && args+=(--dry-run)
  if [[ -f "$CODE/scripts/setup_dpc_layout.sh" ]]; then
    bash "$CODE/scripts/setup_dpc_layout.sh" "${args[@]}"
  else
    run bash "$CODE/scripts/setup_dpc_layout.sh" "${args[@]}"
  fi
}

# ---------------------------------------------------------------- 5 Python packages
python_in_venv() {
  "$PY" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'
}

python_can_import() {
  "$PY" -c "import $1" >/dev/null 2>&1
}

# A pip constraints file that pins the installed numpy/torch/torchvision to their current versions.
write_keep_constraints() {
  "$PY" - "${PIP_KEEP_AS_IS[@]}" <<'EOF'
import sys
from importlib import metadata
for name in sys.argv[1:]:
    try:
        print(f"{name}=={metadata.version(name)}")
    except metadata.PackageNotFoundError:
        pass
EOF
}

step_python_packages() {
  step "5  Python packages (for $PY)"
  local packages=("${PIP_PACKAGES[@]}") pip_args=(--disable-pip-version-check) constraints
  if python_can_import cv2; then
    say "  OpenCV already importable: $PIP_OPENCV not added"
  else
    packages+=("$PIP_OPENCV")
  fi
  # System Python: install for this user (as on Tadipatri). A venv installs into the venv.
  python_in_venv || pip_args+=(--user)

  constraints="$(mktemp)"
  write_keep_constraints >"$constraints"
  if [[ -s "$constraints" ]]; then
    say "  kept at their installed versions: $(paste -sd ' ' "$constraints")"
    pip_args+=(-c "$constraints")
  fi
  # Without --upgrade pip leaves every package that is already installed alone: re-runs change nothing.
  run "$PY" -m pip install "${pip_args[@]}" "${packages[@]}" \
      -r "$CODE/supervisor/requirements.txt" -r "$CODE/ui/requirements.txt"
  rm -f "$constraints"
}

# ---------------------------------------------------------------- 6 manual items (checks only)
check_ok()      { CHECK_LINES+=("  OK       $1"); }
check_missing() { CHECK_LINES+=("  MISSING  $1"$'\n'"           -> $2"); MANUAL_MISSING=1; }
check_warn()    { CHECK_LINES+=("  WARN     $1"$'\n'"           -> $2"); }

check_nvidia_driver() {
  local driver
  if command -v nvidia-smi >/dev/null 2>&1 && driver="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)" && [[ -n "$driver" ]]; then
    check_ok "NVIDIA driver: $driver"
  else
    check_missing "NVIDIA driver (nvidia-smi does not answer)" \
      "install it:  sudo ubuntu-drivers install   then reboot; check with  nvidia-smi"
  fi
}

check_torch() {
  local out
  if ! out="$("$PY" -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())' 2>/dev/null)"; then
    check_missing "PyTorch (import torch fails in $PY)" \
      "install PyTorch for this machine's CUDA from pytorch.org, with this same Python: $PY -m pip install ..."
    return 0
  fi
  local version cuda available
  read -r version cuda available <<<"$out"
  if [[ "$available" == "True" ]]; then
    check_ok "PyTorch $version, CUDA $cuda, GPU visible"
  else
    check_missing "PyTorch $version installed but it sees no GPU (CUDA $cuda)" \
      "torch, CUDA and the NVIDIA driver do not match: reinstall the torch build for this driver (pytorch.org)"
  fi
}

check_detectron2() {
  local version
  if version="$("$PY" -c 'import detectron2; print(detectron2.__version__)' 2>/dev/null)"; then
    check_ok "Detectron2 $version"
  else
    check_missing "Detectron2 (import detectron2 fails in $PY)" \
      "build Detectron2 0.6 from source with this same Python after PyTorch (docs/INSTALL.md step 2)"
  fi
}

check_camera_sdk() {
  local found=()
  python_can_import pypylon && found+=("Basler pypylon")
  python_can_import PySpin && found+=("FLIR PySpin")
  if [[ ${#found[@]} -gt 0 ]]; then
    check_ok "camera SDK: ${found[*]}"
  else
    check_missing "camera SDK (neither pypylon nor PySpin imports in $PY)" \
      "Basler: $PY -m pip install --user pypylon (or the pylon software from baslerweb.com); FLIR: Spinnaker SDK and its PySpin wheel for this Python (flir.com)"
  fi
}

check_autologin() {
  # The kiosk opens at login; after a power cut nobody logs in unless automatic login is on.
  if grep -Eqs '^[[:space:]]*AutomaticLoginEnable[[:space:]]*=[[:space:]]*(true|True)' /etc/gdm3/custom.conf; then
    check_ok "automatic login (the screen opens by itself after a power cut)"
  else
    check_warn "automatic login is off: after a reboot the screen stays at the login page" \
      "Settings > Users > Automatic Login for $(id -un) (the programs run anyway; only the screen waits)"
  fi
}

step_checks() {
  step "6  Items installed by hand (checked, not installed)"
  CHECK_LINES=()
  MANUAL_MISSING=0
  check_nvidia_driver
  check_torch
  check_detectron2
  check_camera_sdk
  [[ $NO_KIOSK -eq 1 ]] || check_autologin
  printf '%s\n' "${CHECK_LINES[@]}"
}

# ---------------------------------------------------------------- 7 boot service
step_service() {
  step "7  Boot service ($SERVICE_NAME)"
  # install_service.sh writes the unit only through sudo, runs check-config on a set-up site,
  # and on a fresh machine says that the Supervisor will start in setup mode (S1).
  local args=(--root "$ROOT" --python "$PY")
  run bash "$CODE/supervisor/install_service.sh" "${args[@]}"
}

# ---------------------------------------------------------------- 8 kiosk autostart
kiosk_desktop_entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=DPC screen
Comment=Opens the DPC UI full-screen at login (written by install/install.sh)
Exec=/bin/bash $CODE/install/kiosk.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
}

# write_if_changed <path> <content>: writes only when the content differs (re-runs change nothing)
write_if_changed() {
  local path="$1" content="$2"
  if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
    say "  $path: unchanged"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  [dry-run] write $path:"
    local line
    while IFS= read -r line; do echo "      $line"; done <<<"$content"
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" >"$path"
  say "  written $path"
}

step_kiosk() {
  step "8  Kiosk screen (autostart at login)"
  if [[ $NO_KIOSK -eq 1 ]]; then say "  skipped (--no-kiosk)"; return 0; fi
  write_if_changed "$HOME/.config/autostart/dpc-kiosk.desktop" "$(kiosk_desktop_entry)"
}

# ---------------------------------------------------------------- 9 start the Supervisor
current_mode() {
  local mode_file="$ROOT/run/mode.json"
  [[ -r "$mode_file" ]] || { echo "unknown"; return 0; }
  "$PY" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("mode", "unknown"))' "$mode_file" 2>/dev/null \
    || echo "unknown"
}

ui_url() {
  if [[ -f "$CODE/install/kiosk.sh" ]]; then
    DPC_ROOT="$ROOT" bash "$CODE/install/kiosk.sh" --url
  else
    echo "http://127.0.0.1:8080/"
  fi
}

step_start() {
  step "9  Start the Supervisor"
  if ! systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    # Fresh machine: no site.yaml yet, so the Supervisor starts in setup mode (only the UI).
    run_sudo systemctl start "$SERVICE_NAME"
  elif [[ $CODE_CHANGED -eq 1 ]]; then
    if [[ "$(current_mode)" == "setup" ]]; then
      # Setup mode runs only the UI: a restart costs nothing.
      run_sudo systemctl restart "$SERVICE_NAME"
    else
      # Normal mode: a restart restarts inspection (wheel counter lost, pause until the next plate),
      # so the person chooses the moment.
      note "the code changed while the site runs: the programs keep the old code until
        sudo systemctl restart $SERVICE_NAME
        (inspection pauses until the next plate; choose a quiet moment)"
    fi
  else
    say "  running, code unchanged: left as it is"
  fi
  [[ $DRY_RUN -eq 0 ]] && wait_for_ui
  return 0
}

wait_for_ui() {
  local url waited=0
  url="$(ui_url)"
  say "  waiting for the UI at $url (first start can take a minute) ..."
  while ! curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; do
    if [[ $waited -ge $UI_WAIT_SECONDS ]]; then
      note "the UI did not answer within ${UI_WAIT_SECONDS} s. Look at:  journalctl -u $SERVICE_NAME -n 50   and $ROOT/logs/"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  say "  UI answers: OK (mode: $(current_mode))"
}

# ---------------------------------------------------------------- summary
summary() {
  echo
  echo "================================================================"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "Dry run finished: nothing was changed."
  else
    echo "DPC installation finished."
  fi
  echo
  echo "Items installed by hand:"
  printf '%s\n' "${CHECK_LINES[@]}"
  if [[ ${#NOTES[@]} -gt 0 ]]; then
    echo
    echo "Notes:"
    printf '  - %s\n' "${NOTES[@]}"
  fi
  echo
  if [[ $MANUAL_MISSING -eq 1 ]]; then
    echo "Install the MISSING items, then check again:  bash $CODE/install/install.sh --check-only"
    echo "The setup wizard opens anyway; its camera and model steps need those items."
  fi
  echo "Next: open the setup wizard on this machine's screen:"
  echo "      $(ui_url)"
  echo "and follow the Setup page (site name, database, cameras, PLC, machine, model, sizes, mail)."
  if [[ $NO_KIOSK -eq 0 ]]; then
    echo "The kiosk screen opens it by itself at every login. To open it now (from the desktop):"
    echo "      bash $CODE/install/kiosk.sh &"
  fi
  echo "================================================================"
}

# ---------------------------------------------------------------- main
main() {
  parse_args "$@"
  if [[ $CHECK_ONLY -eq 1 ]]; then
    preflight
    step_checks
    if [[ $MANUAL_MISSING -eq 0 ]]; then echo "All manual items present."; return 0; fi
    echo "Some manual items are MISSING (see above)."
    return 3
  fi
  [[ $DRY_RUN -eq 1 ]] && echo "DRY RUN: every step is printed, nothing is changed."
  preflight
  start_log
  step_system_packages
  step_browser
  step_code
  step_layout
  step_python_packages
  step_checks
  step_service
  step_kiosk
  step_start
  summary
}

# The whole script is read before main runs, so checking out another tag (which rewrites this
# file) during a re-run cannot break the running copy. Sourcing it (tests) only defines functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; exit $?; fi
