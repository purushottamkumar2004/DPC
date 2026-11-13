#!/usr/bin/env bash
# Tests for install.sh step 6 (database login, D-108/D-110/D-111) with a fake MySQL.
# Made-up passwords only; no MySQL, no sudo.
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
make_fake_machine "$FAKE"
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
    step_database_login
  )
}

yaml_password() {
  python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["db"]["password"], end="")' "$1"
}

test_yaml_and_reset() {
  local root="$WORK/a/INSIGHTZZ" file out tricky="made-up 'quoted': value #x"
  file="$root/config/site_defaults.yaml"
  echo "$tricky" >"$FAKE/mysql_root_password"
  out="$(DPC_DB_PASSWORD="$tricky" step6 "$root" 2>&1)"
  check "defaults mode 600" [ "$(mode_of "$file")" = 600 ]
  check "password never printed" lacks "$out" "made-up 'quoted'"
  if python3 -c "import yaml" 2>/dev/null; then
    check "YAML password round-trips exactly" [ "$(yaml_password "$file")" = "$tricky" ]
  else
    echo "SKIP  PyYAML not installed"
  fi
  check "cnf written for root (tricky password accepted)" grep -qx "user=root" "$root/config/mysql_setup.cnf"

  local before; before="$(cat "$file")"
  out="$(DPC_DB_PASSWORD="another made-up" step6 "$root" 2>&1)"
  check "re-run keeps site_defaults.yaml" [ "$(cat "$file")" = "$before" ]

  echo "another made-up" >"$FAKE/mysql_root_password"
  out="$(DPC_DB_PASSWORD="another made-up" step6 "$root" RESET_DEFAULTS=1 2>&1)"
  check "--reset-defaults rewrites site_defaults.yaml" grep -q "another made-up" "$file"
  check "--reset-defaults rewrites the cnf" grep -qx 'password="another made-up"' "$root/config/mysql_setup.cnf"
  check "--reset-defaults keeps mode 600" [ "$(mode_of "$file")" = 600 ]
}

test_unwritable_password() {
  local root="$WORK/f/INSIGHTZZ" out
  out="$(DPC_DB_PASSWORD='made"up' step6 "$root" 2>&1)"
  check 'password with a double quote: no cnf, clear reason' grep -q "option file cannot hold" <<<"$out"
  check 'password with a double quote: site_defaults.yaml still written' [ -f "$root/config/site_defaults.yaml" ]
}

test_password_from_existing_defaults() {
  local root="$WORK/b/INSIGHTZZ" out
  echo "kept made-up" >"$FAKE/mysql_root_password"
  DPC_DB_PASSWORD="kept made-up" step6 "$root" >/dev/null 2>&1
  rm "$root/config/mysql_setup.cnf"
  out="$(step6 "$root" </dev/null 2>&1)"     # no env, no terminal: the kept file has the password
  check "cnf rebuilt from the kept site_defaults.yaml, no prompt" grep -qx "user=root" "$root/config/mysql_setup.cnf"
}

test_no_terminal_refused() {
  local rc=0 out
  out="$( (unset DPC_DB_PASSWORD; step6 "$WORK/c/INSIGHTZZ") </dev/null 2>&1)" || rc=$?
  check "no terminal and no DPC_DB_PASSWORD: refused (exit $rc)" [ "$rc" -eq 1 ]
  check "refusal names DPC_DB_PASSWORD" grep -q "DPC_DB_PASSWORD" <<<"$out"
}

test_mysql_down() {
  local root="$WORK/d/INSIGHTZZ" out rc=0
  touch "$FAKE/mysql_down"
  out="$(DPC_DB_PASSWORD="made-up" step6 "$root" 2>&1)" || rc=$?
  rm -f "$FAKE/mysql_down"
  check "MySQL down: step continues (exit $rc)" [ "$rc" -eq 0 ]
  check "MySQL down: reason shown" grep -q "Can't connect" <<<"$out"
  check "MySQL down: no cnf" [ ! -e "$root/config/mysql_setup.cnf" ]
}

test_dry_run_writes_nothing() {
  local root="$WORK/e/INSIGHTZZ"
  echo "made-up" >"$FAKE/mysql_root_password"
  DPC_DB_PASSWORD="made-up" step6 "$root" DRY_RUN=1 >/dev/null 2>&1
  check "dry-run: no site_defaults.yaml" [ ! -e "$root/config/site_defaults.yaml" ]
  check "dry-run: no cnf" [ ! -e "$root/config/mysql_setup.cnf" ]
  check "dry-run: no temporary file left" [ -z "$(find "$root/config" -name '.*')" ]
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user." >&2; exit 2; fi
test_yaml_and_reset
test_password_from_existing_defaults
test_unwritable_password
test_no_terminal_refused
test_mysql_down
test_dry_run_writes_nothing
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
