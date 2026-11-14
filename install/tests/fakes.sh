#!/usr/bin/env bash
# fakes.sh : a fake prepared site machine for the installer tests (sourced, not run).
# No sudo, no MySQL, no apt, no pip download, no Firefox: small scripts in $FAKE/bin stand in for
# them and log what they were asked to do. Passwords here are made-up test strings.
#
#   make_fake_machine <dir>    creates <dir>/bin, <dir>/py (a python3 wrapper), <dir>/mods
#   fake_module <dir> <name> [python code]   an importable module in <dir>/mods
#
# State files in <dir>: active_<service>, enabled_<service>, apt_installed,
# gpu; logs: sudo.log, apt.log, pip.log, firefox.log, systemctl.log, mysql.log.

make_fake_machine() {
  local fake="$1"
  mkdir -p "$fake/root/etc/profile.d"     # DPC_SYSTEM_ROOT for the camera SDK settings (F103)
  mkdir -p "$fake/bin" "$fake/mods" "$fake/etc"
  : >"$fake/apt_installed"

  cat >"$fake/bin/sudo" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE/sudo.log"
[[ "$1" == "-v" ]] && exit 0
# tee: a path inside the fake tree is written there, any real system path goes to $FAKE/etc/
if [[ "$1" == "tee" ]]; then
  case "$2" in "$FAKE"/*) cat >"$2" ;; *) cat >"$FAKE/etc/$(basename "$2")" ;; esac
  exit 0
fi
exec "$@"
EOF
  cat >"$fake/bin/apt-get" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE/apt.log"
if [[ "$1" == "install" ]]; then
  for a in "$@"; do [[ "$a" == -* || "$a" == install ]] || echo "$a" >>"$FAKE/apt_installed"; done
fi
EOF
  cat >"$fake/bin/dpkg-query" <<'EOF'
#!/usr/bin/env bash
pkg="${!#}"
grep -qx "$pkg" "$FAKE/apt_installed" && printf 'install ok installed' && exit 0
exit 1
EOF
  cat >"$fake/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE/systemctl.log"
case "$1" in
  is-active)  [[ -e "$FAKE/active_${!#}" ]] ;;
  is-enabled) [[ -e "$FAKE/enabled_${!#}" ]] ;;
  start|restart) touch "$FAKE/active_${!#}" ;;
  enable) for s in "$@"; do [[ "$s" == enable || "$s" == --now ]] && continue
            touch "$FAKE/enabled_$s"; if [[ " $* " == *" --now "* ]]; then touch "$FAKE/active_$s"; fi; done ;;
  *) exit 0 ;;
esac
EOF
  # mysql: present (the "server installed" check looks for the command) but the installer must
  # never call it (D-112); any call is logged and fails, and the tests check the log stays absent.
  cat >"$fake/bin/mysql" <<'EOF'
#!/usr/bin/env bash
echo "mysql $*" >>"$FAKE/mysql.log"
exit 1
EOF
  printf '#!/usr/bin/env bash\nexit 1\n' >"$fake/bin/mysqladmin"
  cat >"$fake/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
[[ -e "$FAKE/gpu" ]] || exit 9
echo "Fake GPU, 535.00"
EOF
  cat >"$fake/bin/firefox" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE/firefox.log"
EOF
  # python3 wrapper: no site-packages (-S), only stdlib + $FAKE/mods; "-m pip" is faked: it logs
  # and makes each requested library importable (requirement name -> import name).
  cat >"$fake/py" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
  [[ "$3" == "--version" ]] && { echo "pip 22.0 (fake)"; exit 0; }
  echo "${*:3}" >>"$FAKE/pip.log"
  [[ "${FAKE_PIP_FAILS:-0}" == 1 ]] && exit 1
  skip=0
  for a in "${@:4}"; do
    if [[ $skip -eq 1 ]]; then skip=0; continue; fi
    [[ "$a" == "-c" ]] && { skip=1; continue; }
    [[ "$a" == -* ]] && continue
    name="${a%%[<>=]*}"
    case "${name,,}" in
      paho-mqtt) mod=paho ;; pyyaml) mod=yaml ;; django) mod=django ;; *) mod="${name,,}" ;;
    esac
    mkdir -p "$FAKE/mods/$mod" && echo "__version__ = 'fake'" >"$FAKE/mods/$mod/__init__.py"
  done
  exit 0
fi
PYTHONPATH="$FAKE/mods" exec python3 -S "$@"
EOF
  chmod +x "$fake/bin/"* "$fake/py"
}

fake_module() {   # fake_module <fake dir> <module> [body]
  mkdir -p "$1/mods/$2"
  printf '%s\n' "${3:-__version__ = 'fake'}" >"$1/mods/$2/__init__.py"
}

# The prepared GPU stack of D-111, as importable fakes, plus a GPU and a running MySQL.
make_prepared_stack() {
  local fake="$1"
  touch "$fake/gpu" "$fake/active_mysql"
  fake_module "$fake" torch "$(printf '%s\n' "__version__ = '2.1.0'" \
    "class version: cuda = '12.1'" "class cuda:" "    @staticmethod" "    def is_available(): return True")"
  fake_module "$fake" detectron2 "__version__ = '0.6'"
  fake_module "$fake" pypylon
  fake_module "$fake" cv2
  fake_module "$fake" numpy "__version__ = '1.24.4'"
}

# fake_spinnaker <fake dir>: Spinnaker as its .deb installs it on Ubuntu: /opt/spinnaker and two
# /etc/profile.d files that set the GenTL variables and PATH only for login shells (Dhar).
fake_spinnaker() {
  local sys="$1/root"
  mkdir -p "$sys/opt/spinnaker/bin" "$sys/opt/spinnaker/lib/spinnaker-gentl" "$sys/etc/profile.d"
  touch "$sys/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti"
  cat >"$sys/etc/profile.d/setup_spinnaker_gentl_64.sh" <<EOF
if [ -z "\$GENICAM_GENTL64_PATH" ]; then
  export GENICAM_GENTL64_PATH=$sys/opt/spinnaker/lib/spinnaker-gentl
else
  export GENICAM_GENTL64_PATH=\$GENICAM_GENTL64_PATH:$sys/opt/spinnaker/lib/spinnaker-gentl
fi
export SPINNAKER_GENTL64_CTI=$sys/opt/spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti
EOF
  echo "export PATH=\$PATH:$sys/opt/spinnaker/bin" >"$sys/etc/profile.d/setup_spinnaker_paths.sh"
}
