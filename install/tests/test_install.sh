#!/usr/bin/env bash
# Tests for install/install.sh and install/kiosk.sh. Run as a normal user (not root), no sudo,
# no network: a local git repository with a test tag stands in for GitHub.
#
#   bash install/tests/test_install.sh
#
# 1 shellcheck (when installed); 2 --dry-run prints every step and creates nothing;
# 3 the code step clones, is unchanged the second time, switches tags, refuses hand-edited code;
# 4 the kiosk autostart entry is written once and unchanged the second time; 5 kiosk.sh --url.
# Steps 5 and 6 (MySQL admin account, DB defaults): tests/test_db_setup.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(dirname "$HERE")"
REPO_DIR="$(dirname "$INSTALL_DIR")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILS=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() {      # check <name> <test command...>
  local name="$1"; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}
expect_in() {   # expect_in <text> <needle> <name>
  if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (missing: $2)"; fi
}

# A local "remote": a copy of this checkout's HEAD with two test tags (no network needed).
make_test_remote() {
  local remote="$WORK/remote.git" src="$WORK/src"
  git clone --quiet "$REPO_DIR" "$src"
  cp -r "$INSTALL_DIR/." "$src/install/"      # the files under test, committed or not
  git -C "$src" -c user.email=t@t -c user.name=t add -A
  git -C "$src" -c user.email=t@t -c user.name=t commit --quiet --allow-empty -m "test release 1"
  git -C "$src" tag v0.900
  git -C "$src" -c user.email=t@t -c user.name=t commit --quiet --allow-empty -m "test release 2"
  git -C "$src" tag v0.901
  git clone --quiet --bare "$src" "$remote"
  echo "$remote"
}

test_shellcheck() {
  if ! command -v shellcheck >/dev/null 2>&1; then echo "SKIP  shellcheck not installed"; return 0; fi
  if shellcheck -x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/kiosk.sh" "$HERE/test_install.sh" "$HERE/test_db_setup.sh"; then
    pass "shellcheck clean"
  else
    fail "shellcheck"
  fi
}

test_dry_run() {
  local remote="$1" root="$WORK/dry/INSIGHTZZ" out rc=0
  out="$(HOME="$WORK/dryhome" bash "$INSTALL_DIR/install.sh" --dry-run --tag v0.900 \
          --repo "$remote" --root "$root" 2>&1)" || rc=$?
  check "dry-run exit 0 (was $rc)" [ "$rc" -eq 0 ] || echo "$out"
  expect_in "$out" "== 1  System packages ==" "dry-run step 1"
  expect_in "$out" "== 3  Code" "dry-run step 3"
  expect_in "$out" "clone --quiet --branch v0.900" "dry-run prints the clone"
  expect_in "$out" "setup_dpc_layout.sh --root $root" "dry-run prints the layout"
  expect_in "$out" "== 5  MySQL admin account (dpc_setup)" "dry-run step 5"
  expect_in "$out" "db.password <hidden>" "dry-run step 6 hides the password"
  expect_in "$out" "pip install" "dry-run prints the pip install"
  expect_in "$out" "== 8  Items installed by hand" "dry-run runs the checks"
  expect_in "$out" "install_service.sh --root $root" "dry-run prints the boot service"
  expect_in "$out" "dpc-kiosk.desktop" "dry-run prints the kiosk entry"
  expect_in "$out" "systemctl start dpc-supervisor" "dry-run prints the start"
  if [[ ! -e "$root" && ! -e "$WORK/dryhome/.config" ]]; then pass "dry-run created nothing"; else fail "dry-run created files"; fi

  rc=0
  out="$(bash "$INSTALL_DIR/install.sh" --dry-run --tag v9.999 --repo "$remote" --root "$root" 2>&1)" || rc=$?
  check "unknown tag refused (exit $rc)" [ "$rc" -eq 1 ]
  expect_in "$out" "v0.900" "unknown tag lists the existing tags"

  rc=0
  bash "$INSTALL_DIR/install.sh" --bogus >/dev/null 2>&1 || rc=$?
  check "wrong option exit 2 (was $rc)" [ "$rc" -eq 2 ]
}

test_code_step() {
  local remote="$1" out rc
  (
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_DIR/install.sh"
    ROOT="$WORK/real/INSIGHTZZ"; CODE="$ROOT/code"; REPO_URL="$remote"; DRY_RUN=0

    TAG="v0.900"; CODE_CHANGED=0; step_code >/dev/null
    [[ "$(git -C "$CODE" describe --tags)" == "v0.900" && $CODE_CHANGED -eq 1 ]] \
      && echo "PASS  first run clones v0.900" || echo "FAIL  first run clone"

    TAG="v0.900"; CODE_CHANGED=0; step_code >/dev/null
    [[ $CODE_CHANGED -eq 0 ]] && echo "PASS  second run with the same tag: unchanged" || echo "FAIL  second run changed code"

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
  bash "$INSTALL_DIR/install.sh" --dry-run --tag v0.900 --repo "$remote" --root "$WORK/notgit/INSIGHTZZ" >/dev/null 2>&1 || rc=$?
  check "non-empty code folder that is not a checkout: refused (exit $rc)" [ "$rc" -eq 1 ]
  check "non-empty code folder left untouched" [ -f "$WORK/notgit/INSIGHTZZ/code/something" ]
}

test_kiosk_entry() {
  (
    # shellcheck source=SCRIPTDIR/../install.sh
    source "$INSTALL_DIR/install.sh"
    HOME="$WORK/khome"; ROOT="$WORK/real/INSIGHTZZ"; CODE="$ROOT/code"; DRY_RUN=0; NO_KIOSK=0
    first="$(step_kiosk)"; second="$(step_kiosk)"
    grep -q "written" <<<"$first" && echo "PASS  kiosk entry written" || echo "FAIL  kiosk entry: $first"
    grep -q "unchanged" <<<"$second" && echo "PASS  kiosk entry unchanged on re-run" || echo "FAIL  kiosk re-run: $second"
    grep -q "^Exec=/bin/bash $CODE/install/kiosk.sh$" "$HOME/.config/autostart/dpc-kiosk.desktop" \
      && echo "PASS  kiosk entry runs kiosk.sh from the checkout" || echo "FAIL  kiosk Exec line"
  ) | tee "$WORK/kiosk.out"
  FAILS=$((FAILS + $(grep -c '^FAIL' "$WORK/kiosk.out" || true)))
}

test_kiosk_url() {
  local root="$WORK/k/INSIGHTZZ" url
  url="$(DPC_ROOT="$root" bash "$INSTALL_DIR/kiosk.sh" --url)"
  check "kiosk url default 8080 ($url)" [ "$url" = "http://127.0.0.1:8080/" ]
  mkdir -p "$root/config/rendered"
  echo '{"ui": {"port": 8099}}' >"$root/config/rendered/ui.json"
  url="$(DPC_ROOT="$root" bash "$INSTALL_DIR/kiosk.sh" --url)"
  check "kiosk url from rendered ui.json ($url)" [ "$url" = "http://127.0.0.1:8099/" ]
  echo 'not json' >"$root/config/rendered/ui.json"
  url="$(DPC_ROOT="$root" bash "$INSTALL_DIR/kiosk.sh" --url)"
  check "kiosk url falls back on a broken ui.json ($url)" [ "$url" = "http://127.0.0.1:8080/" ]
}

if [[ $EUID -eq 0 ]]; then echo "Run the tests as a normal user (install.sh refuses root)." >&2; exit 2; fi
test_shellcheck
remote="$(make_test_remote)"
test_dry_run "$remote"
test_code_step "$remote"
test_kiosk_entry
test_kiosk_url
echo
if [[ $FAILS -eq 0 ]]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
