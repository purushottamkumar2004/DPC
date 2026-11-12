# install/ : the DPC installer (D-103 phase 0, STATUS S7)

Owned by the INSTALLER role (OWNERS.md). One command turns a fresh Ubuntu 22.04 machine with internet into a DPC machine that shows the setup wizard. Everything a site differs by (cameras, PLC, wheel count, DB, model, mail) is entered later in the wizard, never here.

| File | What it is |
|---|---|
| `install.sh` | The installer. Run as the site user; asks for sudo itself. |
| `kiosk.sh` | Opens the UI full-screen; started at every login by `~/.config/autostart/dpc-kiosk.desktop`. |
| `tests/test_install.sh` | Shellcheck, dry-run, re-run and kiosk tests (no network, no sudo). |

## First install
Before: Ubuntu 22.04 installed, the site user created (it is the user every program runs as, D-030), the machine on the internet. Log in as that user, then:
```bash
TAG=v0.<n>                                  # the release for this site (SITES.md)
curl -fsSLO "https://raw.githubusercontent.com/purushottamkumar2004/DPC/$TAG/install/install.sh"
bash install.sh --dry-run --tag "$TAG"      # read what it will do
bash install.sh --tag "$TAG"
rm install.sh                               # the installer now lives in ~/INSIGHTZZ/code/install/
```
Never with `sudo` or as root: the files and the service must belong to the site user.

## What it does
| # | Step | Details | Re-run |
|---|---|---|---|
| 0 | Checks | not root, Ubuntu 22.04 (other versions: a note), Python 3.10 or newer, `sudo -v` | - |
| 1 | System packages | `git python3-pip mysql-server mosquitto mosquitto-clients curl`; MySQL and mosquitto enabled and started | installs only what is missing |
| 2 | Browser | Chromium (on 22.04 only as a snap: `snap install chromium`) | skipped when present |
| 3 | Code | `git clone --branch <tag>` into `~/INSIGHTZZ/code` (detached at the release, D-039) | see "Updating" |
| 4 | Layout | `scripts/setup_dpc_layout.sh` (code, config, logs, run, data incl. `data/transfer`) | creates only missing folders |
| 5 | MySQL admin account | `dpc_setup@localhost` with a random password, created through `sudo mysql` (root via auth_socket); login in `config/mysql_setup.cnf` (D-107) | kept while it logs in; a broken login gets a new password |
| 6 | Company DB defaults | `config/site_defaults.yaml`: `db.name_suffix _db`, `db.user dpc_app`, `db.password` = the company-standard password (D-108) | kept; `--reset-defaults` asks again |
| 7 | Python packages | `requests paho-mqtt numpy pymysql PyYAML pymodbus` (3.x), `opencv-python-headless` (only when no `cv2` imports yet), `supervisor/requirements.txt`, `ui/requirements.txt` (Django, waitress); `--user` for the system Python | pip skips what is installed; installed `numpy`, `torch`, `torchvision` are never upgraded (constraints file) |
| 8 | Manual items | checks only, see below | - |
| 9 | Boot service | `supervisor/install_service.sh` (systemd `dpc-supervisor`, D-052) | rewrites the same unit |
| 10 | Kiosk | `~/.config/autostart/dpc-kiosk.desktop` runs `install/kiosk.sh` at login | written only when different |
| 11 | Start | `systemctl start dpc-supervisor`: with no `site.yaml` it runs in **setup mode**, only the UI (S1); waits until the UI answers | see "Updating" |

The installer itself was cloned with the code, so step 3 must come before step 4 (the layout script is in the repo); `setup_dpc_layout.sh` keeps the existing `code/` checkout.

The UI address is `http://127.0.0.1:<ui.port>/`: the port comes from `config/rendered/ui.json` (rendered from `site.yaml`), else `8080`. `bash ~/INSIGHTZZ/code/install/kiosk.sh --url` prints it.

Log of each run: `~/INSIGHTZZ/logs/install_<date>_<time>.log`.

## MySQL admin account and company DB defaults (D-107, D-108)
Both files are in `~/INSIGHTZZ/config/` (folder mode 700), mode 600, owned by the site user, outside `code/`, so never in git. The installer never prints the passwords.

| File | Content | Read by |
|---|---|---|
| `mysql_setup.cnf` | `[client]` `user=dpc_setup`, `password=<random, 32 characters>` | the wizard's database step through `db/bootstrap.py` (creates the site DB and the program user) |
| `site_defaults.yaml` | `db: {name_suffix: '_db', user: 'dpc_app', password: '<company standard>'}` | the wizard's database step (pre-filled; can be changed per site) |

**The company-standard DB password** is asked once per machine, at the start of the run (typed twice, not shown). Without a terminal, give it for that one command: `DPC_DB_PASSWORD='...' bash install.sh --tag v0.<n>` (put a space before the command so bash does not keep it in the history). To change it: `bash ~/INSIGHTZZ/code/install/install.sh --reset-defaults`.

`dpc_setup` has all rights on this MySQL (it creates databases and users); the site programs never use it. **When `sudo mysql` cannot log in** (a MySQL whose root has a password, for example an older site), the installer says so and continues; create the account by hand, then re-run the installer (it keeps a working file):
```bash
mysql -u root -p      # then, with a password of your choice:
#   CREATE USER 'dpc_setup'@'localhost' IDENTIFIED BY '<password>';
#   GRANT ALL PRIVILEGES ON *.* TO 'dpc_setup'@'localhost' WITH GRANT OPTION;
printf '[client]\nuser=dpc_setup\npassword=%s\n' '<password>' > ~/INSIGHTZZ/config/mysql_setup.cnf
chmod 600 ~/INSIGHTZZ/config/mysql_setup.cnf
```

## What stays manual (checked, not installed)
Step 8 checks these and prints `OK` / `MISSING` with what to do. The wizard opens anyway; its camera and model steps need them.

| Item | Check | How to install |
|---|---|---|
| NVIDIA driver | `nvidia-smi` answers | `sudo ubuntu-drivers install`, reboot |
| PyTorch with CUDA | `import torch`, `torch.cuda.is_available()` | the build for this driver from pytorch.org, **with the same Python** the installer used |
| Detectron2 | `import detectron2` | 0.6 from source after PyTorch (`docs/INSTALL.md` step 2) |
| Camera SDK | `import pypylon` or `import PySpin` | Basler: `python3 -m pip install --user pypylon` (or pylon from baslerweb.com); FLIR: Spinnaker SDK + its PySpin wheel |
| Automatic login (WARN only) | `/etc/gdm3/custom.conf` | Settings > Users > Automatic Login, so the screen comes back after a power cut |

Also manual: cabling, the camera network port coming up on boot (`docs/INSTALL.md` step 1), model training.

After installing one of them: `bash ~/INSIGHTZZ/code/install/install.sh --check-only` (exit 0 = all present, 3 = something missing).

## Re-running
`bash ~/INSIGHTZZ/code/install/install.sh` is safe at any time: every step checks first, nothing is moved or deleted, the code is not switched without `--tag`, and a running Supervisor is not restarted unless the code changed while it is in setup mode. Use it to repair a half-finished install (for example after a lost internet connection).

## Updating a site later
```bash
bash ~/INSIGHTZZ/code/install/install.sh --dry-run --tag v0.<n+1>
bash ~/INSIGHTZZ/code/install/install.sh --tag v0.<n+1>
```
It refuses when files inside `code/` were changed by hand (`git status` shows them). It installs any new package of the new release. In **setup mode** it restarts the Supervisor itself (only the UI runs). In **normal mode** it does **not** restart: restarting stops inspection until the next plate and loses the wheel counter, so it prints the command and the person chooses the moment:
```bash
sudo systemctl restart dpc-supervisor
python3 ~/INSIGHTZZ/code/scripts/dpc_check.py
```
Rollback = the same with the older tag. Read the handoffs between the two tags for `Install impact:` lines. Record the tag in `SITES.md`.

## Options
`--tag TAG`, `--ref BRANCH` (test machines only, never a site), `--root DIR` (default `~/INSIGHTZZ`), `--python PATH` (default `python3` in PATH; a venv is allowed), `--repo URL`, `--no-kiosk` (no screen), `--reset-defaults` (ask the company-standard DB password again), `--dry-run`, `--check-only`. Exit codes: 0 done, 1 a step failed, 2 wrong usage, 3 `--check-only` found a missing item.

## Kiosk
`kiosk.sh` waits for the UI (up to 3 min after boot), turns off GNOME screen blanking and lock, opens Chromium `--kiosk`, and reopens it if it is closed. To leave it: Alt+F4, then `pkill -f install/kiosk.sh`. It is started again at the next login. Machines without a screen: install with `--no-kiosk`.

## Tests
```bash
bash install/tests/test_install.sh          # as a normal user; needs git, optional shellcheck
bash install/tests/test_db_setup.sh         # steps 5 and 6 with a fake MySQL; made-up passwords only
```
`test_db_setup.sh` covers: cnf written with mode 600 and a 32-character password, never printed, kept on the second run, renewed when its login fails, nothing written when `sudo mysql` fails; `site_defaults.yaml` mode 600 and exact YAML round-trip (quotes, colon, `#`), kept on re-run, rewritten with `--reset-defaults`, refused without terminal and without `DPC_DB_PASSWORD`; `--dry-run` writes nothing.
Covers: shellcheck clean; `--dry-run` prints every step and creates nothing; an unknown tag is refused with the list of tags; the code step clones, is unchanged on the second run, switches tags, refuses hand-edited code and a non-empty non-git `code/`; the kiosk entry is written once; `kiosk.sh --url` reads the port from `ui.json`. The sudo steps (apt, snap, systemd) are only checked by `--dry-run` here and on a real machine.

## Limits (v1)
Ubuntu 22.04 only (24.04 refuses `pip install --user` into the system Python: use `--python` with a venv there); internet needed (no offline bundle, D-103); the GPU stack, the camera SDK and automatic login stay manual.
