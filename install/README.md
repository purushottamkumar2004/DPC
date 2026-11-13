# install/ : the DPC installer for prepared site machines (D-111, STATUS F95)

Owned by the INSTALLER role (OWNERS.md). Insightzz site machines arrive **prepared**: Ubuntu 22.04, NVIDIA driver, CUDA, torch, detectron2, the camera SDK (pylon or Spinnaker), Firefox, and MySQL server. `install.sh` **checks** those and never installs, upgrades or reconfigures them; it adds only the small DPC pieces. It is the only terminal step: everything after it happens in the UI (D-110).

| File | What it is |
|---|---|
| `install.sh` | The installer. Run as the site user; asks for sudo itself. |
| `kiosk.sh` | Opens the UI full-screen in Firefox; started at every login by `~/.config/autostart/dpc-kiosk.desktop`. |
| `tests/` | `test_install.sh` (whole runs on a fake prepared machine), `test_db_setup.sh` (step 6), `fakes.sh` (fake mysql, pip, apt, systemctl, sudo, Firefox, nvidia-smi). |

## First install
Log in as the site user (every program runs as that user, D-030), then:
```bash
TAG=v0.<n>                                  # the release for this site (SITES.md)
curl -fsSLO "https://raw.githubusercontent.com/purushottamkumar2004/DPC/$TAG/install/install.sh"
bash install.sh --dry-run --tag "$TAG"      # read what it will do
bash install.sh --tag "$TAG"                # asks the company-standard DB password once (hidden)
rm install.sh                               # the installer now lives in ~/INSIGHTZZ/code/install/
```
Never with `sudo` or as root. Then open the setup wizard on the screen (the URL is printed at the end).

## What it does
| # | Step | Does | Never |
|---|---|---|---|
| 0 | Checks | not root, Ubuntu 22.04 (else a note), Python 3.10+, `sudo -v` | - |
| 1 | Prepared programs | **checks**: MySQL server running, NVIDIA driver (`nvidia-smi`), torch + CUDA (GPU visible), detectron2, pypylon or PySpin, OpenCV, Firefox (unless `--no-kiosk`), git, pip | installs, upgrades or changes any of them |
| 2 | MQTT broker | `apt-get install --no-upgrade mosquitto mosquitto-clients` **only when missing**; enabled and started | a full `apt upgrade` |
| 3 | Code | `git clone --branch <tag>` into `~/INSIGHTZZ/code` (detached at the release, D-039) | switch code without `--tag`, or over hand edits |
| 4 | Layout | `scripts/setup_dpc_layout.sh` | move or delete anything |
| 5 | Python libraries | `pip install --user` of **only the missing ones** among paho-mqtt, pymysql, PyYAML, pymodbus (3.x), requests, Django 5.2, waitress, numpy (only if absent) | upgrade a library that is present; numpy/torch/torchvision are pinned to their installed versions |
| 6 | DB defaults | writes `config/site_defaults.yaml`, the wizard's pre-fill (standard password asked once) | log in to MySQL, create a database or a login (D-112) |
| 7 | Boot service | `supervisor/install_service.sh` (systemd `dpc-supervisor`, D-052) | - |
| 8 | Kiosk | `~/.config/autostart/dpc-kiosk.desktop` runs `install/kiosk.sh` (Firefox) at login | install a browser (no Chromium, no Snap store) |
| 9 | Start | `systemctl start dpc-supervisor` (setup mode on a fresh machine: only the UI); waits until the UI answers | restart a Supervisor in normal mode (it prints the command instead) |

At the end a **table** lists every item: `FOUND`, `INSTALLED`, `WRITTEN`, `KEPT`, `SKIPPED`, `PLANNED` (dry-run) or **`MISSING`** with the reason. The install continues past a missing item; the wizard opens anyway. After fixing one: `bash ~/INSIGHTZZ/code/install/install.sh --check-only` (exit 0 = nothing missing, 3 = something missing).

## Database: site_defaults.yaml only (D-108, D-112)
The person creates the site database and its login (for example in MySQL Workbench) and enters the connection in the wizard's database step, which only **seeds** that database (tables, fixed rows, health rows). The installer never logs in to MySQL; it checks only that the MySQL server runs (step 1) and writes the wizard's pre-fill:

| File | Content | Read by |
|---|---|---|
| `~/INSIGHTZZ/config/site_defaults.yaml` (mode 600, outside `code/` and git) | `db: {name_suffix: '_db', user: 'dpc_app', password: '<standard>'}` | the wizard's database step: pre-fills `<site.id>_db`, `dpc_app` and the password |

The password is asked once per machine at the start (typed twice, hidden), or given for that one command: ` DPC_DB_PASSWORD='...' bash install.sh --tag v0.<n>` (the leading space keeps it out of the bash history). It is never printed or logged. Re-runs keep the file and ask nothing; `--reset-defaults` asks again and rewrites it.

A `config/mysql_setup.cnf` from an older installer (v0.2 or the F95 build) is no longer read by anything. The installer leaves it in place and prints a note; delete it when you no longer need the MySQL login it holds.

## Re-running and updating
`bash ~/INSIGHTZZ/code/install/install.sh` is safe at any time: every step checks first, nothing present is reinstalled, the code is not switched without `--tag`.
```bash
bash ~/INSIGHTZZ/code/install/install.sh --dry-run --tag v0.<n+1>
bash ~/INSIGHTZZ/code/install/install.sh --tag v0.<n+1>
```
It refuses when files inside `code/` were changed by hand. It adds any library the new release needs. In setup mode it restarts the Supervisor itself; in normal mode it does **not** (inspection would pause until the next plate) and prints `sudo systemctl restart dpc-supervisor` for a quiet moment. Rollback = the same with the older tag. Record the tag in `SITES.md`.

## Options
`--tag TAG`, `--ref BRANCH` (test machines only), `--root DIR` (default `~/INSIGHTZZ`), `--python PATH` (default `python3`; a venv is allowed), `--repo URL`, `--no-kiosk` (no screen; Firefox not checked), `--reset-defaults`, `--dry-run`, `--check-only`. Exit codes: 0 done, 1 a step failed, 2 wrong usage, 3 `--check-only` found something missing.

## Kiosk (Firefox)
`kiosk.sh` waits for the UI (up to 3 min after boot), turns off GNOME screen blanking and lock, and opens `firefox --kiosk --no-remote --profile <root>/data/state/kiosk/firefox <url>`. The own profile keeps the person's Firefox untouched and switches off the "restore session" page after a power cut, welcome tabs and update prompts. It reopens Firefox if it is closed; to leave: Alt+F4, then `pkill -f install/kiosk.sh`. The port comes from `config/rendered/ui.json`, else 8080 (`kiosk.sh --url` prints the address). For the screen to come back after a power cut, turn on automatic login (Settings > Users).

## Tests
```bash
bash install/tests/test_install.sh      # as a normal user; needs git, python3; shellcheck optional
bash install/tests/test_db_setup.sh
```
No sudo, network, MySQL, apt, pip download or Firefox needed: `tests/fakes.sh` provides logging fakes and a python3 wrapper whose `-m pip` only records the request. Covered: dry-run changes nothing; a first run on a prepared machine installs only mosquitto and the missing libraries, writes `site_defaults.yaml`, never calls MySQL (the fake `mysql` is a tripwire), never prints the password (output and log); a second run changes nothing and asks nothing; `--no-kiosk` without Firefox; an old `mysql_setup.cnf` left untouched with a note; `--check-only` on a bare machine names every missing item and writes nothing; YAML round-trip (quotes, `#`, `\`), `--reset-defaults`, no terminal without `DPC_DB_PASSWORD`; the code step; the Firefox kiosk.

## Limits
Ubuntu 22.04 (24.04 refuses `pip install --user` into the system Python: use `--python` with a venv); internet needed for git, pip and apt (mosquitto only).
