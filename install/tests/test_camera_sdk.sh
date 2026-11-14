#!/usr/bin/env bash
# Tests for the camera SDK check in install.sh step 1 (F102): pypylon (Basler) and PySpin (FLIR)
# on separate lines with their versions; a mixed site needs both. Fake SDKs only (no camera, no SDK).
#
#   bash install/tests/test_camera_sdk.sh
#
# Cases: both SDKs; only pypylon before the wizard (WARN + mixed-site note, not missing); only
# pypylon with a Basler + FLIR site.yaml (PySpin MISSING); only pypylon with a Basler-only site.yaml
# (PySpin SKIPPED); no SDK at all (both MISSING).

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
has() { grep -qE -- "$2" <<<"$1"; }              # has <text> <regex>
lacks() { ! grep -qE -- "$2" <<<"$1"; }

# A fake PySpin like the real wheel: no __version__, the version only in its package metadata.
fake_pyspin() {
  fake_module "$1" PySpin "# fake PySpin: no __version__"
  mkdir -p "$1/mods/spinnaker_python-4.0.0.116.dist-info"
  printf 'Metadata-Version: 2.1\nName: spinnaker-python\nVersion: 4.0.0.116\n' \
    >"$1/mods/spinnaker_python-4.0.0.116.dist-info/METADATA"
}

# site_yaml <root> <driver...>: a minimal site.yaml with one camera per driver
site_yaml() {
  local root="$1" driver n=1; shift
  mkdir -p "$root/config"
  { echo "capture:"; echo "  cameras:"
    for driver in "$@"; do
      printf '    - id: CAM%s\n      driver: %s   # basler | flir\n' "$n" "$driver"; n=$((n + 1))
    done
  } >"$root/config/site.yaml"
}

# sdk_check <fake> <root>: runs only the camera SDK check; prints its rows, notes and the missing count
sdk_check() {
  (
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_SH"
    ROOT="$2"; PY="$1/py"; export FAKE="$1"
    check_camera_sdk
    echo "MISSING_COUNT=$MISSING_COUNT"
  )
}

test_both_sdks() {
  local fake="$WORK/f_both" out
  make_fake_machine "$fake"
  fake_module "$fake" pypylon "__version__ = '4.1.0'"
  fake_pyspin "$fake"
  out="$(sdk_check "$fake" "$WORK/r_both")"
  check "both: pypylon on its own line with its version" has "$out" "FOUND +pypylon \(Basler\) +4\.1\.0"
  check "both: PySpin on its own line, version from metadata" has "$out" "FOUND +PySpin \(FLIR\) +4\.0\.0\.116"
  check "both: no warning" lacks "$out" "WARN|NOTE"
  check "both: nothing missing" has "$out" "MISSING_COUNT=0"
}

test_one_sdk_before_wizard() {
  local fake="$WORK/f_one" out
  make_fake_machine "$fake"
  fake_module "$fake" pypylon "__version__ = '4.1.0'"
  out="$(sdk_check "$fake" "$WORK/r_one")"
  check "one, no site.yaml: pypylon FOUND" has "$out" "FOUND +pypylon \(Basler\)"
  check "one, no site.yaml: PySpin WARN" has "$out" "WARN +PySpin \(FLIR\)"
  check "one, no site.yaml: note says a mixed site needs both" has "$out" "mixed site .* needs BOTH camera SDKs"
  check "one, no site.yaml: note names the lacking SDK" has "$out" "lacks: FLIR Spinnaker"
  check "one, no site.yaml: WARN is not missing" has "$out" "MISSING_COUNT=0"
}

test_mixed_site_lacks_one() {
  local fake="$WORK/f_mix" root="$WORK/r_mix" out
  make_fake_machine "$fake"
  fake_module "$fake" pypylon "__version__ = '4.1.0'"
  site_yaml "$root" basler flir
  out="$(sdk_check "$fake" "$root")"
  check "mixed site.yaml without PySpin: PySpin MISSING" has "$out" "MISSING +PySpin \(FLIR\) +site.yaml has a flir camera"
  check "mixed site.yaml without PySpin: counted" has "$out" "MISSING_COUNT=1"
}

test_basler_only_site() {
  local fake="$WORK/f_bas" root="$WORK/r_bas" out
  make_fake_machine "$fake"
  fake_module "$fake" pypylon "__version__ = '4.1.0'"
  site_yaml "$root" basler basler
  out="$(sdk_check "$fake" "$root")"
  check "Basler-only site.yaml: PySpin SKIPPED" has "$out" "SKIPPED +PySpin \(FLIR\) +not needed"
  check "Basler-only site.yaml: no mixed-site note" lacks "$out" "NOTE"
  check "Basler-only site.yaml: nothing missing" has "$out" "MISSING_COUNT=0"
}

test_no_sdk() {
  local fake="$WORK/f_none" out
  make_fake_machine "$fake"
  out="$(sdk_check "$fake" "$WORK/r_none")"
  check "no SDK: pypylon MISSING" has "$out" "MISSING +pypylon \(Basler\)"
  check "no SDK: PySpin MISSING" has "$out" "MISSING +PySpin \(FLIR\)"
  check "no SDK: both counted" has "$out" "MISSING_COUNT=2"
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user." >&2; exit 2; fi
test_both_sdks
test_one_sdk_before_wizard
test_mixed_site_lacks_one
test_basler_only_site
test_no_sdk
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
