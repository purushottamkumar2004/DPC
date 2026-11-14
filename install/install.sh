#!/usr/bin/env bash
# install.sh : DPC installer for a PREPARED site machine (D-111, STATUS F95).
#
# Insightzz site machines arrive prepared: Ubuntu 22.04, NVIDIA driver, CUDA, torch, detectron2,
# the camera SDK, Firefox, and MySQL server.
# This script CHECKS those (it never installs, upgrades or reconfigures them) and adds only the
# small DPC pieces. It is the only terminal step; everything after it happens in the UI (D-110).
#
#   0  checks            not root, Ubuntu 22.04, Python 3.10+, sudo
#   1  prepared programs CHECK ONLY: MySQL server running, NVIDIA driver, torch + CUDA, detectron2,
#                        pypylon and PySpin (each with its version; a mixed site needs both), Firefox (for the kiosk), git, pip
#   2  MQTT broker       mosquitto + mosquitto-clients from apt, only when missing
#   3  code              git clone of a release tag into <root>/code
#   4  layout            <root>/{code,config,logs,run,data} via scripts/setup_dpc_layout.sh
#   5  Python libraries  only the missing ones (paho-mqtt, pymysql, PyYAML, pymodbus, requests, Django,
#                        waitress, numpy only if absent); numpy/torch/torchvision are never upgraded
#   6  DB defaults       config/site_defaults.yaml: the company-standard DB login, the wizard's
#                        pre-fill (D-108). No MySQL login: the person creates the database (D-112)
#   7  boot service      supervisor/install_service.sh (systemd unit dpc-supervisor)
#   7b camera SDK env    drop-in dpc-supervisor.service.d/camera-sdk.conf with the Spinnaker/pylon
#                        settings of the login shell (GenTL paths, PATH), so the service finds cameras
#   8  kiosk             autostart entry: Firefox full-screen on the UI at login
#   9  start             starts the Supervisor; on a fresh machine it runs in setup mode (only the UI)
#   then a table: what was found, installed, written, and what is MISSING.
#
# Run as the site user (NOT root, NOT with sudo); it asks for the sudo password when needed.
# Safe to re-run: every step checks first and only does what is missing.
#
# Usage:
#   bash install.sh --tag v0.<n>            first install (tag required: sites run release tags, D-039)
#   bash install.sh                         re-run on an installed machine (code left as it is)
#   bash install.sh --tag v0.<n+1>          update the code of an installed machine to another tag
#   bash install.sh --dry-run --tag v0.<n>  print every step, change nothing
#   bash install.sh --check-only            only the checks (steps 1, 5, 6), nothing installed or written
#
# Options:
#   --tag TAG         release tag to check out (git tag v0.<n>)
#   --ref BRANCH      a branch instead of a tag, ONLY for test machines (prints a warning)
#   --root DIR        deployed root (default ~/INSIGHTZZ, CONTRACTS 4.1)
#   --python PATH     the python3 for the Supervisor and every worker (default: python3 in PATH)
#   --repo URL        git repository (default: the DPC repo on GitHub)
#   --no-kiosk        no kiosk screen (machine without a screen); Firefox is then not checked
#   --reset-defaults  ask the company-standard DB password again, rewrite site_defaults.yaml
#   --dry-run         print every step, change nothing (checks still run: they only read)
#   --check-only      only the checks, then the table
#   -h, --help        this text
#
# The company-standard DB password (D-108) is asked once per machine, hidden, at the start (or set
# DPC_DB_PASSWORD in the environment of that one command). It is never printed. This script never
# logs in to MySQL, never runs "sudo mysql" and never creates or changes a database or a login.
#
# Exit: 0 done (MISSING items are in the table), 1 a step failed, 2 wrong usage,
#       3 (--check-only) something is missing.
# Log: <root>/logs/install_<date>_<time>.log (not in --dry-run / --check-only).

set -euo pipefail

# ---------------------------------------------------------------- fixed values (not site values)
REPO_URL_DEFAULT="https://github.com/purushottamkumar2004/DPC.git"
APT_BROKER_PACKAGES=(mosquitto mosquitto-clients)
SERVICE_NAME="dpc-supervisor"
MYSQL_SERVICE="mysql"
# Python libraries the DPC programs import: "import name|pip requirement". Installed only when the
# import fails; a library that is present is never upgraded (prepared machines stay as delivered).
PY_LIBRARIES=(
  "paho|paho-mqtt>=1.6"
  "pymysql|pymysql"
  "yaml|PyYAML>=5.4"
  "pymodbus|pymodbus>=3,<4"
  "requests|requests"
  "django|Django>=5.2,<5.3"
  "waitress|waitress>=3.0,<4"
  "numpy|numpy"
)
# Reported when missing, never installed: they belong to the prepared GPU stack.
PY_PREPARED_ONLY=("cv2|OpenCV (cv2)")
# Pinned to their installed versions in every pip call: a newer numpy can break torch/detectron2.
PIP_KEEP_AS_IS=(numpy torch torchvision)
MIN_PYTHON="3.10"                  # Django 5.2
UI_WAIT_SECONDS="${DPC_UI_WAIT_SECONDS:-120}"   # first UI start runs the Django migrations
DEFAULT_DB_NAME_SUFFIX="_db"       # D-108 company standards (not secrets)
DEFAULT_DB_USER="dpc_app"

# ---------------------------------------------------------------- options and state
ROOT="$HOME/INSIGHTZZ"
TAG=""
REF=""
REPO_URL="$REPO_URL_DEFAULT"
PY=""
DRY_RUN=0
CHECK_ONLY=0
NO_KIOSK=0
RESET_DEFAULTS=0
DB_PASSWORD=""                     # company-standard DB password: only in memory, never printed
CODE_CHANGED=0                     # step 3 moved the checkout to another commit
NOTES=()                           # things the person must know, printed after the table
REPORT=()                          # "STATE|item|detail" rows of the final table
MISSING_COUNT=0

# ---------------------------------------------------------------- output helpers
say()  { echo "$*"; }
step() { echo; echo "== $* =="; }
note() { echo "  NOTE: $*"; NOTES+=("$*"); }
fail() { echo "ERROR: $*" >&2; exit 1; }

# report <STATE> <item> <detail>: one row of the final table, also shown right away.
# STATE: FOUND, INSTALLED, WRITTEN, KEPT, MISSING, WARN, SKIPPED, PLANNED (dry-run)
# WARN does not count as missing for the exit code: it is something to check for this site.
report() {
  REPORT+=("$1|$2|$3")
  [[ "$1" == MISSING ]] && MISSING_COUNT=$((MISSING_COUNT + 1))
  printf '  %-9s %-26s %s\n' "$1" "$2" "$3"
}

# run <command...>: run it, or only print it in --dry-run
run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run]$(printf ' %q' "$@")"; else "$@"; fi
}

# run_sudo <command...>: the same with sudo (the password is asked once, in preflight)
run_sudo() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] sudo$(printf ' %q' "$@")"; else sudo "$@"; fi
}

usage() { sed -n '2,55p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tag)            TAG="${2:?--tag needs a value}"; shift 2 ;;
      --ref)            REF="${2:?--ref needs a value}"; shift 2 ;;
      --root)           ROOT="${2:?--root needs a value}"; shift 2 ;;
      --python)         PY="${2:?--python needs a value}"; shift 2 ;;
      --repo)           REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
      --no-kiosk)       NO_KIOSK=1; shift ;;
      --reset-defaults) RESET_DEFAULTS=1; shift ;;
      --dry-run)        DRY_RUN=1; shift ;;
      --check-only)     CHECK_ONLY=1; shift ;;
      -h|--help)        usage; exit 0 ;;
      *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
  done
  if [[ -n "$TAG" && -n "$REF" ]]; then
    echo "Use --tag or --ref, not both." >&2; exit 2
  fi
  ROOT="${ROOT%/}"
  CODE="$ROOT/code"
}

# ---------------------------------------------------------------- 0 preflight
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
    say "  sudo is needed for mosquitto (only if missing) and the boot service; enter your password if asked."
    sudo -v || fail "sudo is not available for $(id -un); ask the machine's administrator"
  fi
}

start_log() {
  [[ $DRY_RUN -eq 0 && $CHECK_ONLY -eq 0 ]] || return 0
  mkdir -p "$ROOT/logs"
  LOG_FILE="$ROOT/logs/install_$(date +%Y%m%d_%H%M%S).log"
  exec > >(tee -a "$LOG_FILE") 2>&1
  say "Log: $LOG_FILE (passwords are never written to it)"
}

# ---------------------------------------------------------------- 1 prepared programs (check only)
python_can_import() {
  "$PY" -c "import $1" >/dev/null 2>&1
}

check_mysql_server() {
  if ! command -v mysql >/dev/null 2>&1; then
    report MISSING "MySQL client/server" "no 'mysql' command: the machine should come with MySQL server installed (D-111)"
  elif systemctl is-active --quiet "$MYSQL_SERVICE" 2>/dev/null || mysqladmin ping >/dev/null 2>&1; then
    report FOUND "MySQL server" "running"
  else
    report MISSING "MySQL server" "installed but not running: sudo systemctl start $MYSQL_SERVICE"
  fi
}

check_nvidia_driver() {
  local driver
  if command -v nvidia-smi >/dev/null 2>&1 \
     && driver="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)" \
     && [[ -n "$driver" ]]; then
    report FOUND "NVIDIA driver" "$driver"
  else
    report MISSING "NVIDIA driver" "nvidia-smi does not answer (part of the prepared machine)"
  fi
}

check_torch() {
  local out version cuda available
  if ! out="$("$PY" -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())' 2>/dev/null)"; then
    report MISSING "PyTorch + CUDA" "import torch fails in $PY (part of the prepared machine)"
    return 0
  fi
  read -r version cuda available <<<"$out"
  if [[ "$available" == "True" ]]; then
    report FOUND "PyTorch + CUDA" "torch $version, CUDA $cuda, GPU visible"
  else
    report MISSING "PyTorch + CUDA" "torch $version sees no GPU (CUDA $cuda): driver/CUDA/torch do not match"
  fi
}

check_detectron2() {
  local version
  if version="$("$PY" -c 'import detectron2; print(detectron2.__version__)' 2>/dev/null)"; then
    report FOUND "Detectron2" "$version"
  else
    report MISSING "Detectron2" "import detectron2 fails in $PY (part of the prepared machine)"
  fi
}

# Camera SDKs: each camera's driver needs its own SDK, so a mixed site (one Basler + one FLIR camera)
# needs both. Each SDK gets its own line with its version. On a configured site the drivers come from
# site.yaml (capture.cameras[].driver); before the wizard has run they are not known yet.
# "driver|import name|pip distribution name|what to install"
CAMERA_SDKS=(
  "basler|pypylon|pypylon|Basler pylon SDK + pypylon"
  "flir|PySpin|spinnaker-python|FLIR Spinnaker SDK + its PySpin wheel"
)

# site_camera_drivers: the camera drivers in site.yaml (basler / flir, one per line); empty when
# there is no site.yaml yet. A plain text read: PyYAML may not be installed yet at step 1.
site_camera_drivers() {
  local file="$ROOT/config/site.yaml"
  [[ -r "$file" ]] || return 0
  sed -n -E 's/#.*//; s/^[[:space:]]*driver:[[:space:]]*["'"'"']?([a-z]+).*/\1/p' "$file" | sort -u
}

# sdk_version <import name> <distribution name>: version from the package metadata, else the
# module's __version__, else "version unknown". Never opens a camera (no PySpin.System instance).
sdk_version() {
  "$PY" - "$1" "$2" 2>/dev/null <<'EOF' || echo "version unknown"
import importlib, sys
from importlib import metadata
module, dist = sys.argv[1], sys.argv[2]
try:
    print(metadata.version(dist))
except metadata.PackageNotFoundError:
    print(getattr(importlib.import_module(module), "__version__", "version unknown"))
EOF
}

check_camera_sdk() {
  local drivers entry driver module dist label item found_count=0 lacking=()
  drivers="$(site_camera_drivers)"
  for entry in "${CAMERA_SDKS[@]}"; do
    IFS='|' read -r driver module dist label <<<"$entry"
    if python_can_import "$module"; then found_count=$((found_count + 1)); fi
  done
  for entry in "${CAMERA_SDKS[@]}"; do
    IFS='|' read -r driver module dist label <<<"$entry"
    item="$module (${label%% *})"
    if python_can_import "$module"; then
      report FOUND "$item" "$(sdk_version "$module" "$dist")"
    elif grep -qx "$driver" <<<"$drivers"; then
      report MISSING "$item" "site.yaml has a $driver camera: install the $label for $PY"
    elif [[ -n "$drivers" ]]; then
      report SKIPPED "$item" "not needed: no $driver camera in site.yaml"
    elif [[ $found_count -eq 0 ]]; then
      report MISSING "$item" "no camera SDK imports in $PY; each camera needs the SDK of its driver"
    else
      lacking+=("$label")
      report WARN "$item" "not found: needed only if this site has a $driver camera"
    fi
  done
  if [[ ${#lacking[@]} -gt 0 ]]; then
    note "a mixed site (one Basler + one FLIR camera) needs BOTH camera SDKs; this machine lacks: ${lacking[*]}.
        If this site has such a camera, install it before the wizard's camera step."
  fi
}

check_prepared_python_only() {
  local entry module label
  for entry in "${PY_PREPARED_ONLY[@]}"; do
    module="${entry%%|*}"; label="${entry#*|}"
    if python_can_import "$module"; then
      report FOUND "$label" "importable"
    else
      report MISSING "$label" "import $module fails in $PY (part of the prepared machine, not installed here)"
    fi
  done
}

check_firefox() {
  [[ $NO_KIOSK -eq 1 ]] && return 0
  local path
  if path="$(command -v firefox 2>/dev/null)"; then
    report FOUND "Firefox (kiosk)" "$path"
  else
    report MISSING "Firefox (kiosk)" "no 'firefox' command; the kiosk cannot open (or install with --no-kiosk)"
  fi
}

check_tools() {
  if command -v git >/dev/null 2>&1; then
    report FOUND "git" "$(git --version | awk '{print $3}')"
  else
    report MISSING "git" "needed to fetch the code: sudo apt install git"
  fi
  if "$PY" -m pip --version >/dev/null 2>&1; then
    report FOUND "pip" "for $PY"
  else
    report MISSING "pip" "needed to add Python libraries: sudo apt install python3-pip"
  fi
}

step_prepared_checks() {
  step "1  Prepared programs (checked, never installed or changed)"
  check_mysql_server
  check_nvidia_driver
  check_torch
  check_detectron2
  check_camera_sdk
  check_prepared_python_only
  check_firefox
  check_tools
}

# ---------------------------------------------------------------- 2 MQTT broker (install only when missing)
is_apt_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

step_broker() {
  step "2  MQTT broker (mosquitto)"
  local missing=() pkg
  for pkg in "${APT_BROKER_PACKAGES[@]}"; do
    is_apt_installed "$pkg" || missing+=("$pkg")
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    report FOUND "mosquitto" "installed"
  else
    # Only these two packages, never a full upgrade: the rest of the machine stays as delivered.
    run_sudo apt-get update
    run_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-upgrade "${missing[@]}"
    report "$([[ $DRY_RUN -eq 1 ]] && echo PLANNED || echo INSTALLED)" "mosquitto" "apt: ${missing[*]}"
  fi
  # The broker runs on every boot (ARCHITECTURE 7); mosquitto 2 listens on this machine only.
  if systemctl is-enabled --quiet mosquitto 2>/dev/null && systemctl is-active --quiet mosquitto 2>/dev/null; then
    say "  mosquitto: enabled and running"
  else
    run_sudo systemctl enable --now mosquitto
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
  report "$([[ $DRY_RUN -eq 1 ]] && echo PLANNED || echo INSTALLED)" "code" "${TAG:-$REF} cloned into $CODE"
}

update_code() {
  local current target
  current="$(git -C "$CODE" rev-parse HEAD)"
  say "  checkout present at $(git -C "$CODE" describe --tags --always 2>/dev/null || echo "$current")"
  if [[ -z "$TAG$REF" ]]; then
    report KEPT "code" "$(git -C "$CODE" describe --tags --always 2>/dev/null) (no --tag given)"
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
    report KEPT "code" "${TAG:-$REF} (unchanged)"
    return 0
  fi
  run git -C "$CODE" -c advice.detachedHead=false checkout --quiet --detach "$target"
  CODE_CHANGED=1
  report "$([[ $DRY_RUN -eq 1 ]] && echo PLANNED || echo INSTALLED)" "code" "switched to ${TAG:-$REF}"
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

# ---------------------------------------------------------------- 5 Python libraries (only the missing ones)
python_in_venv() {
  "$PY" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'
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

installed_version() {   # installed_version <import name>: version string or "present"
  "$PY" -c 'import sys, importlib; m = importlib.import_module(sys.argv[1]); print(getattr(m, "__version__", "present"))' "$1" 2>/dev/null \
    || echo "present"
}

step_python_libraries() {
  step "5  Python libraries (for $PY)"
  local entry module requirement missing=() pip_args=(--disable-pip-version-check) constraints
  for entry in "${PY_LIBRARIES[@]}"; do
    module="${entry%%|*}"; requirement="${entry#*|}"
    if python_can_import "$module"; then
      report FOUND "$requirement" "$(installed_version "$module")"
    else
      missing+=("$requirement")
    fi
  done
  [[ ${#missing[@]} -eq 0 ]] && return 0
  if [[ $CHECK_ONLY -eq 1 ]]; then
    for requirement in "${missing[@]}"; do report MISSING "$requirement" "would be installed by install.sh"; done
    return 0
  fi
  # System Python: install for this user (as on Tadipatri). A venv installs into the venv.
  python_in_venv || pip_args+=(--user)
  constraints="$(mktemp)"
  write_keep_constraints >"$constraints"
  [[ -s "$constraints" ]] && pip_args+=(-c "$constraints")
  # Only the missing requirements are named, without --upgrade: nothing present is touched.
  if run "$PY" -m pip install "${pip_args[@]}" "${missing[@]}"; then
    for requirement in "${missing[@]}"; do
      report "$([[ $DRY_RUN -eq 1 ]] && echo PLANNED || echo INSTALLED)" "$requirement" "pip"
    done
  else
    for requirement in "${missing[@]}"; do report MISSING "$requirement" "pip install failed (see above)"; done
  fi
  rm -f "$constraints"
}

# ---------------------------------------------------------------- 6 company DB defaults (D-108, D-112)
# The person creates the site database and its login (for example in MySQL Workbench); the wizard
# only seeds it (D-112). The installer never logs in to MySQL: it only keeps the company-standard
# login as the wizard's pre-fill.
#   site_defaults.yaml  db.name_suffix, db.user, db.password; <root>/config, mode 600, outside code/ and git
site_defaults_file() { echo "$ROOT/config/site_defaults.yaml"; }

# The password is needed only when site_defaults.yaml must be (re)written.
need_db_password() {
  [[ $RESET_DEFAULTS -eq 1 || ! -f "$(site_defaults_file)" ]]
}

# Asked right after the first checks, so nobody waits for a prompt in the middle of the install.
ask_db_password() {
  need_db_password || return 0
  if [[ $DRY_RUN -eq 1 || $CHECK_ONLY -eq 1 ]]; then
    [[ $DRY_RUN -eq 1 ]] && say "  [dry-run] the company-standard DB password would be asked here (or taken from DPC_DB_PASSWORD)"
    DB_PASSWORD="${DPC_DB_PASSWORD:-}"
    return 0
  fi
  if [[ -n "${DPC_DB_PASSWORD:-}" ]]; then
    DB_PASSWORD="$DPC_DB_PASSWORD"
    say "  company-standard DB password: taken from DPC_DB_PASSWORD"
    return 0
  fi
  [[ -t 0 ]] || fail "the company-standard DB password is needed once: run this in a terminal, or set DPC_DB_PASSWORD"
  local first second
  while true; do
    read -r -s -p "Company-standard DB password (not shown): " first; echo
    read -r -s -p "The same password again: " second; echo
    if [[ -z "$first" ]]; then echo "  empty: please enter it again"; continue; fi
    if [[ "$first" != "$second" ]]; then echo "  the two entries differ: please enter it again"; continue; fi
    break
  done
  DB_PASSWORD="$first"
}

yaml_single_quoted() {   # any text as a YAML single-quoted scalar (a ' inside is written '')
  local q="'"
  printf "'%s'" "${1//$q/$q$q}"
}

site_defaults_yaml() {
  cat <<YAML
# Company-standard settings for new sites (D-108), written by install/install.sh.
# Holds a secret: mode 600; never copy it into code/, git or a chat. Change: install.sh --reset-defaults
db:
  name_suffix: '$DEFAULT_DB_NAME_SUFFIX'
  user: '$DEFAULT_DB_USER'
  password: $(yaml_single_quoted "$DB_PASSWORD")
YAML
}

# write_secret_file <path> <content via stdin>: mode 600 from the first byte, then moved into place
write_secret_file() {
  local path="$1" tmp
  tmp="$(umask 077 && mktemp "$(dirname "$path")/.$(basename "$path").XXXXXX")"
  cat >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$path"
}

write_site_defaults() {
  local file
  file="$(site_defaults_file)"
  if ! need_db_password; then
    [[ $DRY_RUN -eq 1 || $CHECK_ONLY -eq 1 ]] || chmod 600 "$file"
    report KEPT "site_defaults.yaml" "present (install.sh --reset-defaults changes it)"
    return 0
  fi
  if [[ $CHECK_ONLY -eq 1 ]]; then
    report MISSING "site_defaults.yaml" "not written yet: run install.sh"
  elif [[ $DRY_RUN -eq 1 ]]; then
    report PLANNED "site_defaults.yaml" "mode 600: db.name_suffix $DEFAULT_DB_NAME_SUFFIX, db.user $DEFAULT_DB_USER, db.password <hidden>"
  else
    site_defaults_yaml | write_secret_file "$file"
    report WRITTEN "site_defaults.yaml" "mode 600 (password not shown)"
  fi
}

step_site_defaults() {
  step "6  Company DB defaults (the wizard's pre-fill)"
  if [[ ! -d "$ROOT/config" ]]; then
    report "$([[ $DRY_RUN -eq 1 ]] && echo PLANNED || echo MISSING)" "site_defaults.yaml" "$ROOT/config does not exist yet (written by a real run)"
    return 0
  fi
  write_site_defaults
  DB_PASSWORD=""
  # Written by F85/F95 installers; nothing reads it since D-112. Left in place (it holds a login
  # the person may still want); deleting it is their choice.
  if [[ -f "$ROOT/config/mysql_setup.cnf" ]]; then
    note "config/mysql_setup.cnf is no longer used (D-112: the wizard only seeds a database you created).
        It holds a MySQL login; delete it when you no longer need it:  rm $ROOT/config/mysql_setup.cnf"
  fi
}

# ---------------------------------------------------------------- 7 boot service
step_service() {
  step "7  Boot service ($SERVICE_NAME)"
  # install_service.sh writes the unit through sudo, runs check-config on a set-up site, and on a
  # fresh machine says that the Supervisor will start in setup mode (S1).
  run bash "$CODE/supervisor/install_service.sh" --root "$ROOT" --python "$PY"
  report "$([[ $DRY_RUN -eq 1 ]] && echo PLANNED || echo WRITTEN)" "boot service" "$SERVICE_NAME enabled"
}

# ---------------------------------------------------------------- 7b camera SDK settings for the service (F103)
# Spinnaker (and pylon) put their GenTL settings in the LOGIN shell (/etc/profile.d, /etc/environment).
# A systemd service never reads those, so everything the Supervisor starts (the wizard's camera
# search, the FLIR capture) saw 0 cameras while a terminal saw them (Dhar, 2026-11-14). This step
# copies the SDK settings the machine defines into a drop-in of the boot service. It never changes
# the SDK or its files. The locations are variables so the tests can point them at fake files.
# DPC_SYSTEM_ROOT is only for the tests (a fake /etc and /opt); empty on a machine.
SYSTEM_ROOT="${DPC_SYSTEM_ROOT:-}"
PROFILE_D_DIR="$SYSTEM_ROOT/etc/profile.d"
ETC_ENVIRONMENT="$SYSTEM_ROOT/etc/environment"
SDK_SPINNAKER_DIR="$SYSTEM_ROOT/opt/spinnaker"
SDK_PYLON_GLOB="$SYSTEM_ROOT/opt/pylon*"
SERVICE_DROPIN_DIR="$SYSTEM_ROOT/etc/systemd/system/$SERVICE_NAME.service.d"
SERVICE_DROPIN_NAME="camera-sdk.conf"
# The unit's own PATH (supervisor/dpc-supervisor.service, after the Python folder): the drop-in's
# PATH replaces it, so it starts with the same entries.
UNIT_BASE_PATH="/usr/local/bin:/usr/bin:/bin"
# Variables taken from the login settings: the GenTL search paths and everything the SDKs name.
SDK_VAR_REGEX='^(GENICAM_GENTL(32|64)_PATH|SPINNAKER_[A-Z0-9_]*|PYLON_[A-Z0-9_]*|FLIR_[A-Z0-9_]*)$'
# Only profile.d files that mention an SDK are read (other files are not the installer's business).
SDK_FILE_REGEX='GENICAM|GENTL|SPINNAKER|[Ss]pinnaker|PYLON|[Pp]ylon'
SDK_ENV_CHANGED=0                  # the drop-in was (re)written: a running service needs a restart

# sdk_login_env: NAME=value lines as a login shell would have them, from /etc/environment and the
# SDK files in /etc/profile.d, read in a clean environment (nothing from this terminal leaks in).
sdk_login_env() {
  local files=() f
  for f in "$PROFILE_D_DIR"/*.sh; do
    [[ -f "$f" ]] && grep -qE "$SDK_FILE_REGEX" "$f" 2>/dev/null && files+=("$f")
  done
  # shellcheck disable=SC2016  # the script runs in the clean shell, not here
  timeout 20 env -i HOME="$HOME" PATH="$UNIT_BASE_PATH" bash --noprofile --norc -c '
    trap env EXIT                  # printed even if an SDK file calls exit
    environment_file="$1"; shift
    # /etc/environment holds plain NAME=value lines (no shell code): read, do not source.
    if [[ -r "$environment_file" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        q=$(printf "\047"); value="${BASH_REMATCH[2]}"; value="${value#[\"$q]}"; value="${value%[\"$q]}"
        export "${BASH_REMATCH[1]}=$value"
      done <"$environment_file"
    fi
    for f in "$@"; do . "$f" >/dev/null 2>&1 </dev/null || true; done' _ "$ETC_ENVIRONMENT" "${files[@]}" 2>/dev/null || true
}

# path_list_add <list> <entry>: list with entry appended once (":"-separated)
path_list_add() {
  local list="$1" entry="$2"
  [[ -z "$entry" ]] && { echo "$list"; return 0; }
  case ":$list:" in *":$entry:"*) echo "$list" ;; *) echo "${list:+$list:}$entry" ;; esac
}

# sdk_entries <list>: the entries of a ":"-list that belong to a camera SDK
sdk_entries() {
  tr ':' '\n' <<<"$1" | grep -iE 'spinnaker|pylon|flir|genicam|gentl' | paste -sd ':' - || true
}

# camera_sdk_settings: prints the settings for the service, "NAME=value" per line (PATH last), or
# nothing when no camera SDK is installed. Login settings first; the SDK folders fill the gaps.
camera_sdk_settings() {
  local login name value gentl="" bins="" libs="" pylon_dir spin_cti spin_gentl
  declare -A vars=()
  login="$(sdk_login_env)"
  while IFS= read -r line; do
    name="${line%%=*}"; value="${line#*=}"
    if [[ "$name" =~ $SDK_VAR_REGEX ]]; then vars["$name"]="$value"; fi
    [[ "$name" == PATH ]] && bins="$(sdk_entries "$value")"
    [[ "$name" == LD_LIBRARY_PATH ]] && libs="$(sdk_entries "$value")"
  done <<<"$login"
  gentl="${vars[GENICAM_GENTL64_PATH]:-}"

  # Spinnaker folder (the .deb installs to /opt/spinnaker)
  if [[ -d "$SDK_SPINNAKER_DIR" ]]; then
    spin_gentl="$SDK_SPINNAKER_DIR/lib/spinnaker-gentl"
    spin_cti="$spin_gentl/Spinnaker_GenTL.cti"
    if [[ -d "$spin_gentl" ]]; then gentl="$(path_list_add "$gentl" "$spin_gentl")"; fi
    if [[ -f "$spin_cti" && -z "${vars[SPINNAKER_GENTL64_CTI]:-}" ]]; then vars[SPINNAKER_GENTL64_CTI]="$spin_cti"; fi
    if [[ -d "$SDK_SPINNAKER_DIR/bin" ]]; then bins="$(path_list_add "$bins" "$SDK_SPINNAKER_DIR/bin")"; fi
  fi
  # pylon folder(s) (/opt/pylon, /opt/pylon5, ...); pypylon brings its own runtime, these are for
  # the pylon tools and any GenTL consumer
  # shellcheck disable=SC2086  # the glob must expand
  for pylon_dir in $SDK_PYLON_GLOB; do
    [[ -d "$pylon_dir" ]] || continue
    if [[ -z "${vars[PYLON_ROOT]:-}" ]]; then vars[PYLON_ROOT]="$pylon_dir"; fi
    if [[ -d "$pylon_dir/lib/gentlproducer/gtl" ]]; then gentl="$(path_list_add "$gentl" "$pylon_dir/lib/gentlproducer/gtl")"; fi
    if [[ -d "$pylon_dir/bin" ]]; then bins="$(path_list_add "$bins" "$pylon_dir/bin")"; fi
  done

  [[ -n "$gentl" ]] && vars[GENICAM_GENTL64_PATH]="$gentl"
  [[ ${#vars[@]} -eq 0 && -z "$bins" ]] && return 0
  for name in $(printf '%s\n' "${!vars[@]}" | sort); do echo "$name=${vars[$name]}"; done
  [[ -n "$libs" ]] && echo "LD_LIBRARY_PATH=$libs"
  # The unit's PATH (same Python first), then the SDK tools
  local path_value entry
  path_value="$(dirname "$PY"):$UNIT_BASE_PATH"
  while IFS= read -r entry; do path_value="$(path_list_add "$path_value" "$entry")"; done < <(tr ':' '\n' <<<"$bins")
  echo "PATH=$path_value"
}

# systemd_quote <NAME=value>: one Environment= value in double quotes (\ " escaped, % as %%)
systemd_quote() {
  local text="$1"
  text="${text//\\/\\\\}"; text="${text//\"/\\\"}"; text="${text//%/%%}"
  printf '"%s"' "$text"
}

camera_sdk_dropin() {   # camera_sdk_dropin <settings>: the drop-in file content
  local line
  echo "# Camera SDK settings for $SERVICE_NAME, written by install/install.sh (F103)."
  echo "# Copied from this machine's login settings (/etc/profile.d, /etc/environment) and the SDK"
  echo "# folders, because a service does not read them. Rewritten by every install.sh run; do not edit."
  echo "[Service]"
  while IFS= read -r line; do [[ -n "$line" ]] && echo "Environment=$(systemd_quote "$line")"; done <<<"$1"
}

step_camera_sdk_env() {
  step "7b Camera SDK settings for the service"
  local settings content target current
  target="$SERVICE_DROPIN_DIR/$SERVICE_DROPIN_NAME"
  settings="$(camera_sdk_settings)"
  if [[ -z "$settings" ]]; then
    report SKIPPED "SDK settings for service" "no SDK: no Spinnaker or pylon folder or login settings found"
    return 0
  fi
  say "  settings: $(cut -d= -f1 <<<"$settings" | paste -sd ' ' -)"
  content="$(camera_sdk_dropin "$settings")"
  current="$(cat "$target" 2>/dev/null || true)"
  if [[ "$current" == "$content" ]]; then
    report KEPT "SDK settings for service" "unchanged: $target"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  [dry-run] write $target:"
    while IFS= read -r line; do echo "      $line"; done <<<"$content"
    report PLANNED "SDK settings for service" "$target, then daemon-reload"
    return 0
  fi
  run_sudo mkdir -p "$SERVICE_DROPIN_DIR"
  printf '%s\n' "$content" | sudo tee "$target" >/dev/null
  run_sudo chmod 644 "$target"
  run_sudo systemctl daemon-reload
  SDK_ENV_CHANGED=1
  report WRITTEN "SDK settings for service" "$target (daemon-reload done)"
}

# ---------------------------------------------------------------- 8 kiosk autostart (Firefox, D-111)
kiosk_desktop_entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=DPC screen
Comment=Opens the DPC UI full-screen in Firefox at login (written by install/install.sh)
Exec=/bin/bash $CODE/install/kiosk.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
}

# write_if_changed <path> <content>: prints unchanged/written; re-runs change nothing
write_if_changed() {
  local path="$1" content="$2" line
  if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
    echo unchanged
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  [dry-run] write $path:" >&2
    while IFS= read -r line; do echo "      $line" >&2; done <<<"$content"
    echo planned
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" >"$path"
  echo written
}

step_kiosk() {
  step "8  Kiosk screen (Firefox at login)"
  if [[ $NO_KIOSK -eq 1 ]]; then report SKIPPED "kiosk" "--no-kiosk"; return 0; fi
  local entry="$HOME/.config/autostart/dpc-kiosk.desktop" result
  result="$(write_if_changed "$entry" "$(kiosk_desktop_entry)")"
  case "$result" in
    unchanged) report KEPT "kiosk autostart" "$entry" ;;
    planned)   report PLANNED "kiosk autostart" "$entry" ;;
    *)         report WRITTEN "kiosk autostart" "$entry" ;;
  esac
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

url_answers() {   # no curl needed: the Python that runs the UI checks it
  "$PY" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3)' "$1" >/dev/null 2>&1
}

step_start() {
  step "9  Start the Supervisor"
  if ! systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    # Fresh machine: no site.yaml yet, so the Supervisor starts in setup mode (only the UI).
    run_sudo systemctl start "$SERVICE_NAME"
  elif [[ $CODE_CHANGED -eq 1 || $SDK_ENV_CHANGED -eq 1 ]]; then
    # New code or new camera SDK settings take effect only after a restart.
    if [[ "$(current_mode)" == "setup" ]]; then
      run_sudo systemctl restart "$SERVICE_NAME"      # setup mode runs only the UI: costs nothing
    else
      # Normal mode: a restart restarts inspection (wheel counter lost, pause until the next plate),
      # so the person chooses the moment.
      note "the code or the camera SDK settings changed while the site runs: the programs keep the old ones until
        sudo systemctl restart $SERVICE_NAME
        (inspection pauses until the next plate; choose a quiet moment)"
    fi
  else
    say "  running, code unchanged: left as it is"
  fi
  [[ $DRY_RUN -eq 1 ]] && { report PLANNED "Supervisor" "start (setup mode on a fresh machine)"; return 0; }
  wait_for_ui
}

wait_for_ui() {
  local url waited=0
  url="$(ui_url)"
  say "  waiting for the UI at $url (first start can take a minute) ..."
  while ! url_answers "$url"; do
    if [[ $waited -ge $UI_WAIT_SECONDS ]]; then
      report MISSING "UI" "no answer at $url after ${UI_WAIT_SECONDS} s: journalctl -u $SERVICE_NAME -n 50"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  report FOUND "Supervisor + UI" "running, mode $(current_mode), $url"
}

# ---------------------------------------------------------------- final table
print_table() {
  local row state item detail
  echo
  echo "================================================================"
  if [[ $DRY_RUN -eq 1 ]]; then echo "Dry run: nothing was changed."; fi
  printf '%-9s  %-26s  %s\n' "STATE" "ITEM" "DETAIL"
  printf '%-9s  %-26s  %s\n' "---------" "--------------------------" "------"
  for row in "${REPORT[@]}"; do
    IFS='|' read -r state item detail <<<"$row"
    printf '%-9s  %-26s  %s\n' "$state" "$item" "$detail"
  done
  if [[ ${#NOTES[@]} -gt 0 ]]; then
    echo
    echo "Notes:"
    printf '  - %s\n' "${NOTES[@]}"
  fi
  echo
  if [[ $MISSING_COUNT -gt 0 ]]; then
    echo "$MISSING_COUNT item(s) MISSING. Prepared items are not installed by this script; after fixing them:"
    echo "      bash $CODE/install/install.sh --check-only"
  else
    echo "Nothing missing."
  fi
}

print_next() {
  echo
  echo "Next: open the setup wizard on this machine's screen:"
  echo "      $(ui_url)"
  if [[ $NO_KIOSK -eq 0 ]]; then
    echo "The kiosk opens it in Firefox at every login. To open it now (from the desktop):"
    echo "      bash $CODE/install/kiosk.sh &"
  fi
  echo "================================================================"
}

# ---------------------------------------------------------------- main
main() {
  parse_args "$@"
  if [[ $CHECK_ONLY -eq 1 ]]; then
    preflight
    ask_db_password
    step_prepared_checks
    step_python_libraries
    step_site_defaults
    print_table
    [[ $MISSING_COUNT -eq 0 ]] || return 3
    return 0
  fi
  [[ $DRY_RUN -eq 1 ]] && echo "DRY RUN: every step is printed, nothing is changed."
  preflight
  ask_db_password
  start_log
  step_prepared_checks
  step_broker
  step_code
  step_layout
  step_python_libraries
  step_site_defaults
  step_service
  step_camera_sdk_env
  step_kiosk
  step_start
  print_table
  print_next
}

# The whole script is read before main runs, so checking out another tag (which rewrites this
# file) during a re-run cannot break the running copy. Sourcing it (tests) only defines functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; exit $?; fi
