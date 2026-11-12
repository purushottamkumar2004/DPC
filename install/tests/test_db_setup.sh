#!/usr/bin/env bash
# Tests for install.sh steps 5 (MySQL admin account, D-107) and 6 (company DB defaults, D-108).
# No MySQL, no sudo, no real password: fake mysql commands stand in for the server, and the
# passwords below are made-up test strings.
#
#   bash install/tests/test_db_setup.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$(dirname "$HERE")/install.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILS=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() {      # check <name> <test command...>
  local name="$1"; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}
mode_of() { stat -c %a "$1"; }
not_in() { ! grep -qF -- "$2" <<<"$1"; }       # not_in <text> <needle>
no_temp_files() { [[ -z "$(find "$1" -name '.mysql_setup.*' -o -name '.site_defaults.*')" ]]; }

# Fake MySQL. The "server" keeps dpc_setup's password in $WORK/server_password.
#   fake_root   = "sudo mysql": reads the SQL on stdin, logs it, stores the password (or fails)
#   fake_client = "mysql --defaults-file=X -e ...": logs in when X holds the server's password
make_fakes() {
  cat >"$WORK/fake_root" <<'FAKE'
#!/usr/bin/env bash
sql="$(cat)"
echo "called" >>"$FAKE_DIR/root_calls"
[[ "${FAKE_ROOT_FAILS:-0}" == 1 ]] && exit 1
sed -n "s/^ALTER USER 'dpc_setup'@'localhost' IDENTIFIED BY '\(.*\)';$/\1/p" <<<"$sql" >"$FAKE_DIR/server_password"
FAKE
  cat >"$WORK/fake_client" <<'FAKE'
#!/usr/bin/env bash
file="${1#--defaults-file=}"
[[ -r "$file" && -r "$FAKE_DIR/server_password" ]] || exit 1
grep -qx "user=dpc_setup" "$file" || exit 1
[[ "$(sed -n 's/^password=//p' "$file")" == "$(cat "$FAKE_DIR/server_password")" ]]
FAKE
  chmod +x "$WORK/fake_root" "$WORK/fake_client"
}

# load <root>: the installer's functions in this (sub)shell, with the fakes in place
load() {
  # shellcheck source=SCRIPTDIR/../install.sh
  source "$INSTALL_SH"
  ROOT="$1"; CODE="$ROOT/code"; PY="$(command -v python3)"; DRY_RUN=0
  MYSQL_CLIENT=("$WORK/fake_client"); MYSQL_AS_ROOT=("$WORK/fake_root")
  mkdir -p "$ROOT/config" && chmod 700 "$ROOT/config"
}

test_mysql_admin() {
  local root="$WORK/m/INSIGHTZZ" cnf out first second
  cnf="$root/config/mysql_setup.cnf"
  export FAKE_DIR="$WORK"
  out="$( (load "$root"; step_mysql_admin) 2>&1 )"
  check "first run: cnf written" [ -f "$cnf" ]
  check "cnf mode 600" [ "$(mode_of "$cnf")" = 600 ]
  check "cnf has [client] and user=dpc_setup" grep -qx "user=dpc_setup" "$cnf"
  check "cnf starts with [client]" [ "$(head -1 "$cnf")" = "[client]" ]
  first="$(sed -n 's/^password=//p' "$cnf")"
  check "random password of 32 characters" [ "${#first}" -eq 32 ]
  check "password never printed" not_in "$out" "$first"
  check "SQL creates dpc_setup@localhost" grep -q "called" "$WORK/root_calls"
  check "no temporary file left" no_temp_files "$root/config"

  out="$( (load "$root"; step_mysql_admin) 2>&1 )"
  second="$(sed -n 's/^password=//p' "$cnf")"
  check "second run: working account kept" [ "$first" = "$second" ]
  check "second run: no root login" [ "$(wc -l <"$WORK/root_calls")" -eq 1 ]
  check "second run says kept" grep -q "kept" <<<"$out"

  echo "changed-on-the-server" >"$WORK/server_password"
  out="$( (load "$root"; step_mysql_admin) 2>&1 )"
  second="$(sed -n 's/^password=//p' "$cnf")"
  check "broken login: new password set" [ "$first" != "$second" ]
  check "broken login: new password works" "$WORK/fake_client" --defaults-file="$cnf" -e "SELECT 1"
  check "broken login: still mode 600" [ "$(mode_of "$cnf")" = 600 ]

  local root2="$WORK/m2/INSIGHTZZ" rc=0
  out="$( (load "$root2"; FAKE_ROOT_FAILS=1; export FAKE_ROOT_FAILS; step_mysql_admin) 2>&1 )" || rc=$?
  check "root login fails: step continues (exit 0)" [ "$rc" -eq 0 ]
  check "root login fails: no cnf written" [ ! -e "$root2/config/mysql_setup.cnf" ]
  check "root login fails: a note explains it" grep -q "was not created" <<<"$out"
  check "root login fails: no temporary file left" no_temp_files "$root2/config"
}

# yaml_value <file> <key under db>: the parsed value (PyYAML when present, else skipped)
yaml_value() {
  python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["db"][sys.argv[2]], end="")' "$1" "$2"
}

test_site_defaults() {
  local root="$WORK/d/INSIGHTZZ" file out rc
  local test_password="made-up test value with 'quotes' and: colon #hash"
  file="$root/config/site_defaults.yaml"

  out="$( (load "$root"; DPC_DB_PASSWORD="$test_password"; ask_db_password; step_site_defaults) 2>&1 )"
  check "defaults written" [ -f "$file" ]
  check "defaults mode 600" [ "$(mode_of "$file")" = 600 ]
  check "password never printed" not_in "$out" "made-up test value"
  check "no temporary file left" no_temp_files "$root/config"
  if python3 -c "import yaml" 2>/dev/null; then
    check "yaml db.password round-trips exactly" [ "$(yaml_value "$file" password)" = "$test_password" ]
    check "yaml db.user dpc_app" [ "$(yaml_value "$file" user)" = dpc_app ]
    check "yaml db.name_suffix _db" [ "$(yaml_value "$file" name_suffix)" = _db ]
  else
    echo "SKIP  PyYAML not installed: YAML content not parsed"
  fi

  local before; before="$(cat "$file")"
  out="$( (load "$root"; DPC_DB_PASSWORD="another made-up value"; ask_db_password; step_site_defaults) 2>&1 )"
  check "re-run keeps the file" [ "$(cat "$file")" = "$before" ]
  check "re-run says kept" grep -q "kept" <<<"$out"

  out="$( (load "$root"; RESET_DEFAULTS=1; DPC_DB_PASSWORD="another made-up value"; ask_db_password; step_site_defaults) 2>&1 )"
  check "--reset-defaults rewrites the file" grep -q "another made-up value" "$file"
  check "--reset-defaults keeps mode 600" [ "$(mode_of "$file")" = 600 ]
  check "--reset-defaults does not print it" not_in "$out" "another made-up value"

  rc=0
  out="$( (load "$WORK/e/INSIGHTZZ"; unset DPC_DB_PASSWORD; ask_db_password) </dev/null 2>&1 )" || rc=$?
  check "no terminal and no DPC_DB_PASSWORD: refused (exit 1)" [ "$rc" -eq 1 ]
  check "refusal names DPC_DB_PASSWORD" grep -q "DPC_DB_PASSWORD" <<<"$out"
}

test_dry_run_db() {
  local root="$WORK/dry/INSIGHTZZ" out
  mkdir -p "$root/config"
  export FAKE_DIR="$WORK/dryfake"; mkdir -p "$FAKE_DIR"
  out="$( (load "$root"; DRY_RUN=1; DPC_DB_PASSWORD="made-up"; ask_db_password; step_mysql_admin; step_site_defaults) 2>&1 )"
  check "dry-run: no cnf" [ ! -e "$root/config/mysql_setup.cnf" ]
  check "dry-run: no defaults file" [ ! -e "$root/config/site_defaults.yaml" ]
  check "dry-run: no root login" [ ! -e "$FAKE_DIR/root_calls" ]
  check "dry-run prints both steps" grep -q "site_defaults.yaml (mode 600)" <<<"$out"
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user." >&2; exit 2; fi
make_fakes
test_mysql_admin
test_site_defaults
test_dry_run_db
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
