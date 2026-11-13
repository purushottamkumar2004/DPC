#!/usr/bin/env bash
# kiosk.sh : open the DPC UI full-screen in Firefox on this machine (D-095, D-111).
# Started at login by ~/.config/autostart/dpc-kiosk.desktop, which install/install.sh writes.
# Uses the Firefox already on the prepared machine (snap or apt); never installs a browser.
#
#   bash kiosk.sh          wait for the UI, open it in Firefox kiosk mode, reopen it if it closes
#   bash kiosk.sh --once   open it once (no reopening; for a test)
#   bash kiosk.sh --url    print the UI address and stop
#
# Root: $DPC_ROOT or ~/INSIGHTZZ. Port: ui.port from <root>/config/rendered/ui.json (rendered from
# site.yaml), else 8080, the UI's default, so a port change in site.yaml needs no re-install.
# Leave kiosk mode: Alt+F4 closes Firefox, then stop the reopening with  pkill -f install/kiosk.sh

set -euo pipefail

ROOT="${DPC_ROOT:-$HOME/INSIGHTZZ}"
DEFAULT_PORT=8080            # ui.port default (common/config/site_config.py, ui/dpc_ui/ui_config.py)
WAIT_SECONDS="${DPC_KIOSK_WAIT_SECONDS:-180}"   # after boot the Supervisor and the UI need some time
REOPEN_DELAY_SECONDS=5
FIREFOX="${DPC_KIOSK_FIREFOX:-firefox}"      # the tests put a fake here
# A profile of its own: the person's normal Firefox is not touched, and the kiosk can run beside it
# (--no-remote). Under the home folder and not hidden, so the snap Firefox may use it.
PROFILE_DIR="$ROOT/data/state/kiosk/firefox"

ui_port() {
  local file="$ROOT/config/rendered/ui.json" port=""
  if [[ -r "$file" ]] && command -v python3 >/dev/null 2>&1; then
    port="$(python3 -c 'import json, sys; print(int(json.load(open(sys.argv[1]))["ui"]["port"]))' "$file" 2>/dev/null || true)"
  fi
  [[ "$port" =~ ^[0-9]+$ ]] || port="$DEFAULT_PORT"
  echo "$port"
}

ui_url() { echo "http://127.0.0.1:$(ui_port)/"; }   # the UI listens on 127.0.0.1 only (D-095)

url_answers() {
  python3 -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3)' "$1" >/dev/null 2>&1
}

# Settings for a status screen: no "restore session?" page after a power cut, no welcome or
# what's-new tabs, no update prompts on top of the UI. Rewritten at every start.
write_profile_prefs() {
  mkdir -p "$PROFILE_DIR"
  cat >"$PROFILE_DIR/user.js" <<'EOF'
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("startup.homepage_welcome_url", "");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("app.update.auto", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.translations.automaticallyPopup", false);
EOF
}

# A 24x7 status screen must not blank or lock (GNOME; ignored where these keys do not exist).
keep_screen_on() {
  command -v gsettings >/dev/null 2>&1 || return 0
  gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
  gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
}

wait_for_ui() {
  local url="$1" waited=0
  while ! url_answers "$url"; do
    # Open anyway after a while: the page then shows Firefox's error and a reload works later.
    [[ $waited -ge $WAIT_SECONDS ]] && return 0
    sleep 3
    waited=$((waited + 3))
  done
}

open_firefox() {
  "$FIREFOX" --kiosk --no-remote --profile "$PROFILE_DIR" "$1" || true
}

main() {
  local once=0 url
  case "${1:-}" in
    --url)  ui_url; return 0 ;;
    --once) once=1 ;;
    "")     ;;
    *) echo "usage: kiosk.sh [--once | --url]" >&2; return 2 ;;
  esac
  command -v "$FIREFOX" >/dev/null 2>&1 \
    || { echo "kiosk.sh: Firefox is not on this machine (it comes with the prepared machine, D-111)" >&2; return 1; }
  keep_screen_on
  write_profile_prefs
  while true; do
    url="$(ui_url)"            # read again each time: the port may have changed in site.yaml
    wait_for_ui "$url"
    open_firefox "$url"
    [[ $once -eq 1 ]] && return 0
    sleep "$REOPEN_DELAY_SECONDS"
  done
}

main "$@"
