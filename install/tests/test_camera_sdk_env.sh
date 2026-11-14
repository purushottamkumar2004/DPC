#!/usr/bin/env bash
# Tests for install.sh step 7b (F103): the camera SDK settings of the login shell copied into the
# drop-in dpc-supervisor.service.d/camera-sdk.conf, so the service finds the cameras (Dhar).
# Fake /etc and /opt under DPC_SYSTEM_ROOT, fake sudo and systemctl; no real system file is touched.
#
#   bash install/tests/test_camera_sdk_env.sh

# shellcheck disable=SC2030,SC2031  # each case exports into its own subshell on purpose
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$(dirname "$HERE")/install.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILS=0

# shellcheck source=SCRIPTDIR/fakes.sh
source "$HERE/fakes.sh"

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() {      # check <name> <test command...>
  local name="$1"; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}
has() { grep -qF -- "$2" <<<"$1"; }
lacks() { ! grep -qF -- "$2" <<<"$1"; }
count_of() { if [[ -f "$1" ]]; then grep -c -- "$2" "$1" || true; else echo 0; fi; }
file_lacks() { [[ ! -e "$1" ]] || ! grep -qE -- "$2" "$1"; }   # file_lacks <file> <regex>
row() { grep -qE -- "^ +$2 +$3 +$4" <<<"$1"; }                  # row <output> <STATE> <item> <detail start>

# step7b <fake> [VAR=value ...]: step 7b with the fake machine; prints its output, then the flag
step7b() {
  local fake="$1"; shift
  (
    export FAKE="$fake" PATH="$fake/bin:$PATH" DPC_SYSTEM_ROOT="$fake/root"
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_SH"
    PY="/usr/bin/python3"; DRY_RUN=0
    local assignment
    for assignment in "$@"; do eval "$assignment"; done
    step_camera_sdk_env
    echo "SDK_ENV_CHANGED=$SDK_ENV_CHANGED"
  ) </dev/null 2>&1
}

dropin_of() { echo "$1/root/etc/systemd/system/dpc-supervisor.service.d/camera-sdk.conf"; }

test_spinnaker_written_then_unchanged() {
  local fake="$WORK/f_spin" sys out dropin
  make_fake_machine "$fake"; fake_spinnaker "$fake"
  sys="$fake/root"; dropin="$(dropin_of "$fake")"
  out="$(step7b "$fake")"
  check "Spinnaker: drop-in WRITTEN" row "$out" WRITTEN "SDK settings for service" ""
  check "drop-in has [Service]" grep -qx "\[Service\]" "$dropin"
  check "GENICAM_GENTL64_PATH from profile.d" grep -qxF "Environment=\"GENICAM_GENTL64_PATH=$sys/opt/spinnaker/lib/spinnaker-gentl\"" "$dropin"
  check "SPINNAKER_GENTL64_CTI from profile.d" grep -qxF "Environment=\"SPINNAKER_GENTL64_CTI=$sys/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti\"" "$dropin"
  check "PATH: the unit's PATH plus the Spinnaker bin folder" \
    grep -qxF "Environment=\"PATH=/usr/bin:/usr/local/bin:/usr/bin:/bin:$sys/opt/spinnaker/bin\"" "$dropin"
  check "GenTL folder not listed twice (profile.d + folder)" [ "$(grep -o "spinnaker-gentl" "$dropin" | wc -l)" -eq 2 ]
  check "drop-in mode 644" [ "$(stat -c %a "$dropin")" = 644 ]
  check "daemon-reload after writing" grep -qx "daemon-reload" "$fake/systemctl.log"
  check "first write sets the restart flag" has "$out" "SDK_ENV_CHANGED=1"

  out="$(step7b "$fake")"
  check "second run: unchanged" row "$out" KEPT "SDK settings for service" "unchanged"
  check "second run: no second daemon-reload" [ "$(count_of "$fake/systemctl.log" daemon-reload)" -eq 1 ]
  check "second run: no restart flag" has "$out" "SDK_ENV_CHANGED=0"

  echo 'export SPINNAKER_GENTL64_CTI=/somewhere/else.cti' >>"$sys/etc/profile.d/setup_spinnaker_gentl_64.sh"
  out="$(step7b "$fake")"
  check "changed SDK settings: drop-in rewritten" grep -qF "SPINNAKER_GENTL64_CTI=/somewhere/else.cti" "$dropin"
  check "changed SDK settings: daemon-reload again" [ "$(count_of "$fake/systemctl.log" daemon-reload)" -eq 2 ]
}

test_folder_only() {
  local fake="$WORK/f_dir" sys out dropin
  make_fake_machine "$fake"; fake_spinnaker "$fake"
  sys="$fake/root"; dropin="$(dropin_of "$fake")"
  rm -f "$sys"/etc/profile.d/setup_spinnaker_*.sh        # SDK folder present, no login settings
  out="$(step7b "$fake")"
  check "folder only: GenTL path from /opt/spinnaker" grep -qF "GENICAM_GENTL64_PATH=$sys/opt/spinnaker/lib/spinnaker-gentl\"" "$dropin"
  check "folder only: CTI from /opt/spinnaker" grep -qF "SPINNAKER_GENTL64_CTI=$sys/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti" "$dropin"
  check "folder only: bin on PATH" grep -qF ":$sys/opt/spinnaker/bin\"" "$dropin"
}

test_pylon_and_environment_file() {
  local fake="$WORK/f_pyl" sys dropin
  make_fake_machine "$fake"
  sys="$fake/root"; dropin="$(dropin_of "$fake")"
  mkdir -p "$sys/opt/pylon/bin" "$sys/opt/pylon/lib/gentlproducer/gtl"
  printf 'PATH="/usr/local/sbin:/usr/bin"\nPYLON_CAMEMU="2"\nLANG="en_US.UTF-8"\n' >"$sys/etc/environment"
  step7b "$fake" >/dev/null
  check "pylon: PYLON_ROOT" grep -qxF "Environment=\"PYLON_ROOT=$sys/opt/pylon\"" "$dropin"
  check "pylon: GenTL producer folder" grep -qF "GENICAM_GENTL64_PATH=$sys/opt/pylon/lib/gentlproducer/gtl\"" "$dropin"
  check "pylon: bin on PATH" grep -qF ":$sys/opt/pylon/bin\"" "$dropin"
  check "/etc/environment: SDK variable taken, quotes removed" grep -qxF 'Environment="PYLON_CAMEMU=2"' "$dropin"
  check "/etc/environment: other variables left out" file_lacks "$dropin" "LANG="
}

test_no_sdk() {
  local fake="$WORK/f_none" out
  make_fake_machine "$fake"
  echo 'export EDITOR=vim' >"$fake/root/etc/profile.d/editor.sh"
  out="$(step7b "$fake")"
  check "no SDK: reported" row "$out" SKIPPED "SDK settings for service" "no SDK"
  check "no SDK: no drop-in" [ ! -e "$(dropin_of "$fake")" ]
  check "no SDK: no daemon-reload" [ "$(count_of "$fake/systemctl.log" daemon-reload)" -eq 0 ]
}

test_hostile_profile_file() {
  local fake="$WORK/f_exit" dropin
  make_fake_machine "$fake"; fake_spinnaker "$fake"
  dropin="$(dropin_of "$fake")"
  # An SDK file that ends with exit, and one that reads stdin: neither may lose the settings or hang.
  printf 'export SPINNAKER_EXTRA=1\nexit 3\n' >"$fake/root/etc/profile.d/zz_spinnaker_exit.sh"
  printf 'read -r x; export PYLON_X=1\n' >"$fake/root/etc/profile.d/zz_pylon_read.sh"
  step7b "$fake" >/dev/null
  check "SDK file with exit: settings still collected" grep -qF "SPINNAKER_GENTL64_CTI=" "$dropin"
  check "SDK file with exit: its own variable kept" grep -qF "SPINNAKER_EXTRA=1" "$dropin"
}

test_dry_run() {
  local fake="$WORK/f_dry" out
  make_fake_machine "$fake"; fake_spinnaker "$fake"
  out="$(step7b "$fake" DRY_RUN=1)"
  check "dry-run: PLANNED" row "$out" PLANNED "SDK settings for service" ""
  check "dry-run: content printed" has "$out" "Environment=\"SPINNAKER_GENTL64_CTI="
  check "dry-run: no drop-in" [ ! -e "$(dropin_of "$fake")" ]
  check "dry-run: no sudo" [ ! -e "$fake/sudo.log" ]
}

test_quoting() {
  local out
  out="$(
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_SH"
    systemd_quote 'A=x "y" 50% \z'
  )"
  check "systemd quoting: \" \\ and % escaped" [ "$out" = '"A=x \"y\" 50%% \\z"' ]
}

test_restart_rules() {
  local fake="$WORK/f_rs" root="$WORK/rs/INSIGHTZZ" out
  make_fake_machine "$fake"
  touch "$fake/active_dpc-supervisor"
  mkdir -p "$root/run"
  run_start() {   # run_start <mode>: step_start with only SDK_ENV_CHANGED=1
    echo "{\"mode\": \"$1\"}" >"$root/run/mode.json"
    (
      export FAKE="$fake" PATH="$fake/bin:$PATH"
      # shellcheck source=SCRIPTDIR/../install.sh
      source "$INSTALL_SH"
      ROOT="$root"; CODE="$root/code"; PY="/usr/bin/python3"; DRY_RUN=0
      SDK_ENV_CHANGED=1; CODE_CHANGED=0; UI_WAIT_SECONDS=0
      step_start
      printf '%s\n' "${NOTES[@]}"
    ) </dev/null 2>&1
  }
  out="$(run_start setup)"
  check "new SDK settings in setup mode: Supervisor restarted" grep -q "^restart dpc-supervisor" "$fake/systemctl.log"
  : >"$fake/systemctl.log"
  out="$(run_start normal)"
  check "new SDK settings in normal mode: no restart" file_lacks "$fake/systemctl.log" "^restart"
  check "new SDK settings in normal mode: note with the command" has "$out" "sudo systemctl restart dpc-supervisor"
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user." >&2; exit 2; fi
test_spinnaker_written_then_unchanged
test_folder_only
test_pylon_and_environment_file
test_no_sdk
test_hostile_profile_file
test_dry_run
test_quoting
test_restart_rules
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
