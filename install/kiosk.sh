#!/usr/bin/env bash
# kiosk.sh : open the DPC UI full-screen on this machine (D-095: local web UI, kiosk at login).
# Started at login by ~/.config/autostart/dpc-kiosk.desktop, which install/install.sh writes.
#
#   bash kiosk.sh          wait for the UI, open it in Chromium kiosk mode, reopen it if it closes
#   bash kiosk.sh --once   open it once (no reopening; for a test)
#   bash kiosk.sh --url    print the UI address and stop
#
# Root: $DPC_ROOT or ~/INSIGHTZZ. Port: ui.port from <root>/config/rendered/ui.json (rendered from
# site.yaml), else 8080, the UI's default, so a port change in site.yaml needs no re-install.
# Leave kiosk mode: Alt+F4 closes the browser, then stop the reopening with  pkill -f install/kiosk.sh

set -euo pipefail

ROOT="${DPC_ROOT:-$HOME/INSIGHTZZ}"
DEFAULT_PORT=8080            # ui.port default (common/config/site_config.py, ui/dpc_ui/ui_config.py)
WAIT_SECONDS=180             # after boot the Supervisor and the UI need some time
REOPEN_DELAY_SECONDS=5

ui_port() {
  local file="$ROOT/config/rendered/ui.json" port=""
  if [[ -r "$file" ]] && command -v python3 >/dev/null 2>&1; then
    port="$(python3 -c 'import json, sys; print(int(json.load(open(sys.argv[1]))["ui"]["port"]))' "$file" 2>/dev/null || true)"
  fi
  [[ "$port" =~ ^[0-9]+$ ]] || port="$DEFAULT_PORT"
  echo "$port"
}

ui_url() { echo "http://127.0.0.1:$(ui_port)/"; }   # the UI listens on 127.0.0.1 only (D-095)

find_browser() {
  local name
  for name in chromium chromium-browser; do
    if command -v "$name" >/dev/null 2>&1; then echo "$name"; return 0; fi
  done
  return 1
}

# A 24x7 status screen must not blank or lock (GNOME; ignored where these keys do not exist).
keep_screen_on() {
  command -v gsettings >/dev/null 2>&1 || return 0
  gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
  gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
}

wait_for_ui() {
  local url="$1" waited=0
  while ! curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; do
    # Open anyway after a while: the page then shows the browser's error and a reload works later.
    [[ $waited -ge $WAIT_SECONDS ]] && return 0
    sleep 3
    waited=$((waited + 3))
  done
}

open_browser() {
  local browser="$1" url="$2"
  # --disable-session-crashed-bubble: no "restore pages?" box after a power cut
  "$browser" --kiosk --no-first-run --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble --disable-translate --password-store=basic "$url" || true
}

main() {
  local once=0 browser url
  case "${1:-}" in
    --url)  ui_url; return 0 ;;
    --once) once=1 ;;
    "")     ;;
    *) echo "usage: kiosk.sh [--once | --url]" >&2; return 2 ;;
  esac
  browser="$(find_browser)" || { echo "kiosk.sh: Chromium is not installed (run install/install.sh)" >&2; return 1; }
  keep_screen_on
  while true; do
    url="$(ui_url)"            # read again each time: the port may have changed in site.yaml
    wait_for_ui "$url"
    open_browser "$browser" "$url"
    [[ $once -eq 1 ]] && return 0
    sleep "$REOPEN_DELAY_SECONDS"
  done
}

main "$@"
