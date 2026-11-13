#!/usr/bin/env bash
# Tests for install.sh step 6 (company DB defaults, D-108, D-112): site_defaults.yaml only;
# the installer never logs in to MySQL. Made-up passwords only; no MySQL, no sudo.
#
#   bash install/tests/test_db_setup.sh

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
mode_of() { stat -c %a "$1"; }
lacks() { ! grep -qF -- "$2" <<<"$1"; }

export FAKE="$WORK/fake"
make_fake_machine "$FAKE"          # a fake mysql in PATH logs any call: there must be none
export PATH="$FAKE/bin:$PATH"

# step6 <root> [VAR=value ...]: runs step 6 in a subshell with the installer's functions
step6() {
  local root="$1"; shift
  (
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_SH"
    ROOT="$root"; CODE="$ROOT/code"; PY="$(command -v python3)"; DRY_RUN=0
    mkdir -p "$ROOT/config" && chmod 700 "$ROOT/config"
    local assignment
    for assignment in "$@"; do eval "$assignment"; done
    ask_db_password
    step_site_defaults
  )
}

yaml_value() {   # yaml_value <file> <key under db>
  python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["db"][sys.argv[2]], end="")' "$1" "$2"
}

test_write_keep_reset() {
  local root="$WORK/a/INSIGHTZZ" file out tricky="made-up 'quoted': value #x \"dq\" \\bs"
  file="$root/config/site_defaults.yaml"
  out="$(DPC_DB_PASSWORD="$tricky" step6 "$root" 2>&1)"
  check "written, mode 600" [ "$(mode_of "$file")" = 600 ]
  check "password never printed" lacks "$out" "made-up 'quoted'"
  check "no temporary file left" [ -z "$(find "$root/config" -name '.site_defaults*')" ]
  if python3 -c "import yaml" 2>/dev/null; then
    check "YAML password round-trips exactly (quotes, #, \\)" [ "$(yaml_value "$file" password)" = "$tricky" ]
    check "YAML db.user dpc_app" [ "$(yaml_value "$file" user)" = dpc_app ]
    check "YAML db.name_suffix _db" [ "$(yaml_value "$file" name_suffix)" = _db ]
  else
    echo "SKIP  PyYAML not installed"
  fi

  local before; before="$(cat "$file")"
  out="$(DPC_DB_PASSWORD="another made-up" step6 "$root" 2>&1)"
  check "re-run keeps the file" [ "$(cat "$file")" = "$before" ]
  check "re-run says KEPT" grep -q "KEPT" <<<"$out"

  out="$(DPC_DB_PASSWORD="another made-up" step6 "$root" RESET_DEFAULTS=1 2>&1)"
  check "--reset-defaults rewrites it" grep -q "another made-up" "$file"
  check "--reset-defaults keeps mode 600" [ "$(mode_of "$file")" = 600 ]
  check "--reset-defaults does not print it" lacks "$out" "another made-up"
}

test_no_terminal_refused() {
  local rc=0 out
  out="$( (unset DPC_DB_PASSWORD; step6 "$WORK/c/INSIGHTZZ") </dev/null 2>&1)" || rc=$?
  check "no terminal and no DPC_DB_PASSWORD: refused (exit $rc)" [ "$rc" -eq 1 ]
  check "refusal names DPC_DB_PASSWORD" grep -q "DPC_DB_PASSWORD" <<<"$out"
}

test_dry_run_writes_nothing() {
  local root="$WORK/e/INSIGHTZZ"
  DPC_DB_PASSWORD="made-up" step6 "$root" DRY_RUN=1 >/dev/null 2>&1
  check "dry-run: no site_defaults.yaml" [ ! -e "$root/config/site_defaults.yaml" ]
  check "dry-run: no temporary file left" [ -z "$(find "$root/config" -name '.*')" ]
}

test_never_mysql() {
  check "no MySQL call in any of these runs (D-112)" [ ! -e "$FAKE/mysql.log" ]
  check "no mysql_setup.cnf written anywhere" [ -z "$(find "$WORK" -name mysql_setup.cnf)" ]
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user." >&2; exit 2; fi
test_write_keep_reset
test_no_terminal_refused
test_dry_run_writes_nothing
test_never_mysql
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
