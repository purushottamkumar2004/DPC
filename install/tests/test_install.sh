#!/usr/bin/env bash
# Tests for install/install.sh and install/kiosk.sh on a FAKE prepared machine (D-111).
# Run as a normal user (not root). No sudo, no network, no MySQL, no apt, no pip download, no
# Firefox: tests/fakes.sh puts logging fakes in PATH; a local git repository with test tags stands
# in for GitHub. The passwords are made-up test strings.
#
#   bash install/tests/test_install.sh
#
# Covers: shellcheck; --dry-run; a full first run (installs only the missing small pieces, writes
# both DB files after the root check, never "sudo mysql"); a second run (changes nothing);
# root refusing the password (continues, no cnf); an old dpc_setup cnf replaced by root;
# --check-only on a machine without the GPU stack; the code step; the kiosk (Firefox).
# Step 6 details (YAML, --reset-defaults, prompts): tests/test_db_setup.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(dirname "$HERE")"
REPO_DIR="$(dirname "$INSTALL_DIR")"
WORK="$(mktemp -d)"
HTTP_PID=""
cleanup() { if [[ -n "$HTTP_PID" ]]; then kill "$HTTP_PID" 2>/dev/null || true; fi; rm -rf "$WORK"; }
trap cleanup EXIT
FAILS=0
TEST_PASSWORD="made-up-test-password-7"          # not a real password

# shellcheck source=SCRIPTDIR/fakes.sh
source "$HERE/fakes.sh"

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() {      # check <name> <test command...>
  local name="$1"; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}
has() { grep -qF -- "$2" <<<"$1"; }              # has <text> <needle>
lacks() { ! grep -qF -- "$2" <<<"$1"; }
file_lacks() { [[ ! -e "$1" ]] || ! grep -qE -- "$2" "$1"; }   # file_lacks <file> <regex>
mode_of() { stat -c %a "$1"; }
count_lines() { if [[ -f "$1" ]]; then wc -l <"$1"; else echo 0; fi; }
none_exist() { local f; for f in "$@"; do [[ ! -e "$f" ]] || return 1; done; }

# A local "remote": this checkout's HEAD plus the files under test, tagged v0.900 and v0.901.
make_test_remote() {
  local remote="$WORK/remote.git" src="$WORK/src"
  git clone --quiet "$REPO_DIR" "$src"
  cp -r "$INSTALL_DIR/." "$src/install/"
  git -C "$src" -c user.email=t@t -c user.name=t add -A
  git -C "$src" -c user.email=t@t -c user.name=t commit --quiet --allow-empty -m "test release 1"
  git -C "$src" tag v0.900
  git -C "$src" -c user.email=t@t -c user.name=t commit --quiet --allow-empty -m "test release 2"
  git -C "$src" tag v0.901
  git clone --quiet --bare "$src" "$remote"
  echo "$remote"
}

# A UI stand-in: a static HTTP server on a free port; ui.json in each test root points to it.
start_fake_ui() {
  UI_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  python3 -m http.server "$UI_PORT" --bind 127.0.0.1 --directory "$WORK" >/dev/null 2>&1 &
  HTTP_PID=$!
  sleep 1
}

prepare_root() {   # prepare_root <root>: only the rendered ui.json, so the start step finds the fake UI
  mkdir -p "$1/config/rendered"
  echo "{\"ui\": {\"port\": $UI_PORT}}" >"$1/config/rendered/ui.json"
}

# run_installer <fake> <home> <args...>: install.sh with the fake machine first in PATH
run_installer() {
  local fake="$1" home="$2"; shift 2
  FAKE="$fake" HOME="$home" PATH="$fake/bin:$PATH" DPC_UI_WAIT_SECONDS=6 \
    bash "$INSTALL_DIR/install.sh" --python "$fake/py" --repo "$REMOTE" "$@" </dev/null
}

test_shellcheck() {
  if ! command -v shellcheck >/dev/null 2>&1; then echo "SKIP  shellcheck not installed"; return 0; fi
  if shellcheck -x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/kiosk.sh" "$HERE/test_install.sh" \
       "$HERE/test_db_setup.sh" "$HERE/fakes.sh"; then
    pass "shellcheck clean"
  else
    fail "shellcheck"
  fi
}

test_dry_run() {
  local fake="$WORK/f_dry" root="$WORK/dry/INSIGHTZZ" out rc=0
  make_fake_machine "$fake"; make_prepared_stack "$fake"
  out="$(DPC_DB_PASSWORD="$TEST_PASSWORD" run_installer "$fake" "$WORK/h_dry" --dry-run --tag v0.900 --root "$root" 2>&1)" || rc=$?
  check "dry-run exit 0 (was $rc)" [ "$rc" -eq 0 ]
  check "dry-run: step 1 checks" has "$out" "== 1  Prepared programs"
  check "dry-run: mosquitto would be installed" has "$out" "apt-get install -y --no-upgrade mosquitto mosquitto-clients"
  check "dry-run: clone printed" has "$out" "clone --quiet --branch v0.900"
  check "dry-run: pip printed for the missing libraries" has "$out" "pip install"
  check "dry-run: numpy present, not in the pip line" lacks "$(grep 'pip install' <<<"$out")" " numpy"
  check "dry-run: boot service printed" has "$out" "install_service.sh --root $root"
  check "dry-run: kiosk entry printed" has "$out" "dpc-kiosk.desktop"
  check "dry-run: table printed" has "$out" "STATE      ITEM"
  check "dry-run: password never printed" lacks "$out" "$TEST_PASSWORD"
  check "dry-run created nothing" none_exist "$root" "$WORK/h_dry/.config"
  check "dry-run: no apt, pip or sudo call" none_exist "$fake/apt.log" "$fake/pip.log" "$fake/sudo.log"

  rc=0
  out="$(run_installer "$fake" "$WORK/h_dry" --dry-run --tag v9.999 --root "$root" 2>&1)" || rc=$?
  check "unknown tag refused (exit $rc)" [ "$rc" -eq 1 ]
  check "unknown tag lists the existing tags" has "$out" "v0.900"
  rc=0
  bash "$INSTALL_DIR/install.sh" --bogus >/dev/null 2>&1 || rc=$?
  check "wrong option exit 2 (was $rc)" [ "$rc" -eq 2 ]
}

# Fresh prepared machine: GPU stack, MySQL (root = standard password), Firefox; no mosquitto,
# some Python libraries missing.
test_first_and_second_run() {
  local fake="$WORK/f_ok" home="$WORK/h_ok" root="$WORK/ok/INSIGHTZZ" out rc=0 cnf defaults log
  make_fake_machine "$fake"; make_prepared_stack "$fake"
  echo "$TEST_PASSWORD" >"$fake/mysql_root_password"
  fake_module "$fake" yaml; fake_module "$fake" pymysql
  prepare_root "$root"
  cnf="$root/config/mysql_setup.cnf"; defaults="$root/config/site_defaults.yaml"

  out="$(DPC_DB_PASSWORD="$TEST_PASSWORD" run_installer "$fake" "$home" --tag v0.900 --root "$root" 2>&1)" || rc=$?
  check "first run exit 0 (was $rc)" [ "$rc" -eq 0 ]
  check "first run: nothing missing" has "$out" "Nothing missing."
  check "first run: GPU stack FOUND" has "$out" "FOUND      PyTorch + CUDA"
  check "first run: mosquitto INSTALLED" has "$out" "INSTALLED  mosquitto"
  check "first run: apt asked only for mosquitto" grep -q "install -y --no-upgrade mosquitto mosquitto-clients" "$fake/apt.log"
  check "first run: pip asked for the missing libraries" \
    grep -qF "paho-mqtt>=1.6 pymodbus>=3,<4 requests Django>=5.2,<5.3 waitress>=3.0,<4" "$fake/pip.log"
  check "first run: pip not asked for present ones (numpy, PyYAML, pymysql)" file_lacks "$fake/pip.log" "(^| )(numpy|PyYAML|pymysql)"
  check "first run: cnf written, mode 600" [ "$(mode_of "$cnf")" = 600 ]
  check "first run: cnf user=root" grep -qx "user=root" "$cnf"
  check "first run: defaults written, mode 600" [ "$(mode_of "$defaults")" = 600 ]
  check "first run: password not printed" lacks "$out" "$TEST_PASSWORD"
  log="$(cat "$root"/logs/install_*.log)"
  check "first run: password not in the install log" lacks "$log" "$TEST_PASSWORD"
  check "first run: never 'sudo mysql'" file_lacks "$fake/sudo.log" "^mysql"
  check "first run: boot unit written" [ -f "$fake/etc/dpc-supervisor.service" ]
  check "first run: Supervisor started" [ -e "$fake/active_dpc-supervisor" ]
  check "first run: UI found" has "$out" "FOUND      Supervisor + UI"
  check "first run: kiosk entry written" grep -q "install/kiosk.sh" "$home/.config/autostart/dpc-kiosk.desktop"
  check "first run: no Chromium, no snap" file_lacks "$fake/sudo.log" "snap|chromium"

  local apt_before pip_before cnf_before
  apt_before="$(count_lines "$fake/apt.log")"; pip_before="$(count_lines "$fake/pip.log")"; cnf_before="$(cat "$cnf")"
  rc=0
  out="$(run_installer "$fake" "$home" --root "$root" 2>&1)" || rc=$?
  check "second run exit 0, no password asked (was $rc)" [ "$rc" -eq 0 ]
  check "second run: no apt call" [ "$(count_lines "$fake/apt.log")" = "$apt_before" ]
  check "second run: no pip call" [ "$(count_lines "$fake/pip.log")" = "$pip_before" ]
  check "second run: cnf kept" [ "$(cat "$cnf")" = "$cnf_before" ]
  check "second run: table says KEPT" has "$out" "KEPT       mysql_setup.cnf"
  check "second run: code kept" has "$out" "KEPT       code"
  check "second run: password not printed" lacks "$out" "$TEST_PASSWORD"
}

test_root_refuses() {
  local fake="$WORK/f_no" root="$WORK/no/INSIGHTZZ" out rc=0
  make_fake_machine "$fake"; make_prepared_stack "$fake"
  echo "the-other-made-up-root-password" >"$fake/mysql_root_password"
  prepare_root "$root"
  out="$(DPC_DB_PASSWORD="$TEST_PASSWORD" run_installer "$fake" "$WORK/h_no" --tag v0.900 --root "$root" --no-kiosk 2>&1)" || rc=$?
  check "root refuses: install continues (exit $rc)" [ "$rc" -eq 0 ]
  check "root refuses: no cnf" [ ! -e "$root/config/mysql_setup.cnf" ]
  check "root refuses: site_defaults still written" [ -f "$root/config/site_defaults.yaml" ]
  check "root refuses: MISSING row with MySQL's reason" has "$out" "Access denied for user 'root'"
  check "root refuses: says the wizard asks" has "$out" "The wizard's database step asks"
  check "root refuses: no temporary file left" [ -z "$(find "$root/config" -name '.mysql_setup*')" ]
  check "root refuses: password not printed" lacks "$out" "$TEST_PASSWORD"
  check "--no-kiosk: kiosk SKIPPED, Firefox not checked" has "$out" "SKIPPED    kiosk"
}

test_old_dpc_setup_cnf_replaced() {
  local fake="$WORK/f_up" root="$WORK/up/INSIGHTZZ"
  make_fake_machine "$fake"; make_prepared_stack "$fake"
  echo "$TEST_PASSWORD" >"$fake/mysql_root_password"
  prepare_root "$root"
  printf '[client]\nuser=dpc_setup\npassword=old-made-up\n' >"$root/config/mysql_setup.cnf"
  DPC_DB_PASSWORD="$TEST_PASSWORD" run_installer "$fake" "$WORK/h_up" --tag v0.900 --root "$root" --no-kiosk >/dev/null 2>&1 || true
  check "old dpc_setup cnf replaced by root" grep -qx "user=root" "$root/config/mysql_setup.cnf"
}

test_check_only_missing() {
  local fake="$WORK/f_bare" root="$WORK/bare/INSIGHTZZ" out rc=0 item
  make_fake_machine "$fake"                    # no GPU, no torch, no camera SDK, MySQL stopped
  rm -f "$fake/bin/firefox"
  out="$(FAKE="$fake" PATH="$fake/bin:/usr/bin:/bin" bash "$INSTALL_DIR/install.sh" --check-only \
          --python "$fake/py" --root "$root" </dev/null 2>&1)" || rc=$?
  check "check-only with missing items: exit 3 (was $rc)" [ "$rc" -eq 3 ]
  for item in "NVIDIA driver" "PyTorch + CUDA" "Detectron2" "camera SDK" "MySQL server" "Firefox (kiosk)" "OpenCV (cv2)"; do
    check "check-only names MISSING $item" has "$out" "MISSING    $item"
  done
  check "check-only: nothing installed (no apt, pip)" none_exist "$fake/apt.log" "$fake/pip.log"
  check "check-only: nothing written" [ ! -e "$root" ]
}

test_code_step() {
  local out rc
  (
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_DIR/install.sh"
    ROOT="$WORK/real/INSIGHTZZ"; CODE="$ROOT/code"; REPO_URL="$REMOTE"; DRY_RUN=0

    TAG="v0.900"; CODE_CHANGED=0; step_code >/dev/null
    [[ "$(git -C "$CODE" describe --tags)" == "v0.900" && $CODE_CHANGED -eq 1 ]] \
      && echo "PASS  first run clones v0.900" || echo "FAIL  first run clone"
    TAG="v0.900"; CODE_CHANGED=0; step_code >/dev/null
    [[ $CODE_CHANGED -eq 0 ]] && echo "PASS  same tag again: unchanged" || echo "FAIL  same tag changed code"
    TAG=""; CODE_CHANGED=0; step_code >/dev/null
    [[ $CODE_CHANGED -eq 0 ]] && echo "PASS  re-run without --tag leaves code alone" || echo "FAIL  re-run without tag"
    TAG="v0.901"; CODE_CHANGED=0; step_code >/dev/null
    [[ "$(git -C "$CODE" describe --tags)" == "v0.901" && $CODE_CHANGED -eq 1 ]] \
      && echo "PASS  --tag v0.901 switches the release" || echo "FAIL  tag switch"
    echo "# hand edit" >>"$CODE/README.md"
    TAG="v0.900"; rc=0
    out="$( (step_code) 2>&1)" || rc=$?
    [[ $rc -ne 0 && "$(git -C "$CODE" describe --tags)" == "v0.901" ]] \
      && echo "PASS  hand-edited code: switch refused, code untouched" || echo "FAIL  hand-edited code: $out"
  ) | tee "$WORK/code.out"
  FAILS=$((FAILS + $(grep -c '^FAIL' "$WORK/code.out" || true)))

  rc=0
  mkdir -p "$WORK/notgit/INSIGHTZZ/code" && touch "$WORK/notgit/INSIGHTZZ/code/something"
  bash "$INSTALL_DIR/install.sh" --dry-run --tag v0.900 --repo "$REMOTE" --root "$WORK/notgit/INSIGHTZZ" >/dev/null 2>&1 </dev/null || rc=$?
  check "non-empty non-git code folder: refused (exit $rc)" [ "$rc" -eq 1 ]
  check "non-empty non-git code folder: untouched" [ -f "$WORK/notgit/INSIGHTZZ/code/something" ]
}

test_kiosk() {
  local fake="$WORK/f_k" root="$WORK/k/INSIGHTZZ" url rc=0
  make_fake_machine "$fake"
  url="$(DPC_ROOT="$root" bash "$INSTALL_DIR/kiosk.sh" --url)"
  check "kiosk url default 8080 ($url)" [ "$url" = "http://127.0.0.1:8080/" ]
  prepare_root "$root"
  url="$(DPC_ROOT="$root" bash "$INSTALL_DIR/kiosk.sh" --url)"
  check "kiosk url from rendered ui.json ($url)" [ "$url" = "http://127.0.0.1:$UI_PORT/" ]
  FAKE="$fake" PATH="$fake/bin:$PATH" DPC_ROOT="$root" DPC_KIOSK_WAIT_SECONDS=3 HOME="$WORK/h_k" \
    bash "$INSTALL_DIR/kiosk.sh" --once
  check "kiosk opens Firefox in kiosk mode on the UI" \
    grep -qxF -- "--kiosk --no-remote --profile $root/data/state/kiosk/firefox http://127.0.0.1:$UI_PORT/" "$fake/firefox.log"
  check "kiosk profile: no session restore after a power cut" \
    grep -q 'browser.sessionstore.resume_from_crash", false' "$root/data/state/kiosk/firefox/user.js"
  rm -f "$fake/bin/firefox"
  FAKE="$fake" PATH="$fake/bin:/usr/bin:/bin" DPC_ROOT="$root" bash "$INSTALL_DIR/kiosk.sh" --once 2>/dev/null || rc=$?
  check "kiosk without Firefox: exit 1, nothing installed (was $rc)" [ "$rc" -eq 1 ]
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user (install.sh refuses root)." >&2; exit 2; fi
test_shellcheck
REMOTE="$(make_test_remote)"
start_fake_ui
test_dry_run
test_first_and_second_run
test_root_refuses
test_old_dpc_setup_cnf_replaced
test_check_only_missing
test_code_step
test_kiosk
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
