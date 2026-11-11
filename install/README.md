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
| 5 | Python packages | `requests paho-mqtt numpy pymysql PyYAML pymodbus` (3.x), `opencv-python-headless` (only when no `cv2` imports yet), `supervisor/requirements.txt`, `ui/requirements.txt` (Django, waitress); `--user` for the system Python | pip skips what is installed; installed `numpy`, `torch`, `torchvision` are never upgraded (constraints file) |
| 6 | Manual items | checks only, see below | - |
| 7 | Boot service | `supervisor/install_service.sh` (systemd `dpc-supervisor`, D-052) | rewrites the same unit |
| 8 | Kiosk | `~/.config/autostart/dpc-kiosk.desktop` runs `install/kiosk.sh` at login | written only when different |
| 9 | Start | `systemctl start dpc-supervisor`: with no `site.yaml` it runs in **setup mode**, only the UI (S1); waits until the UI answers | see "Updating" |

The installer itself was cloned with the code, so step 3 must come before step 4 (the layout script is in the repo); `setup_dpc_layout.sh` keeps the existing `code/` checkout.

The UI address is `http://127.0.0.1:<ui.port>/`: the port comes from `config/rendered/ui.json` (rendered from `site.yaml`), else `8080`. `bash ~/INSIGHTZZ/code/install/kiosk.sh --url` prints it.

Log of each run: `~/INSIGHTZZ/logs/install_<date>_<time>.log`.

## What stays manual (checked, not installed)
Step 6 checks these and prints `OK` / `MISSING` with what to do. The wizard opens anyway; its camera and model steps need them.

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
`--tag TAG`, `--ref BRANCH` (test machines only, never a site), `--root DIR` (default `~/INSIGHTZZ`), `--python PATH` (default `python3` in PATH; a venv is allowed), `--repo URL`, `--no-kiosk` (no screen), `--dry-run`, `--check-only`. Exit codes: 0 done, 1 a step failed, 2 wrong usage, 3 `--check-only` found a missing item.

## Kiosk
`kiosk.sh` waits for the UI (up to 3 min after boot), turns off GNOME screen blanking and lock, opens Chromium `--kiosk`, and reopens it if it is closed. To leave it: Alt+F4, then `pkill -f install/kiosk.sh`. It is started again at the next login. Machines without a screen: install with `--no-kiosk`.

## Tests
```bash
bash install/tests/test_install.sh          # as a normal user; needs git, optional shellcheck
```
Covers: shellcheck clean; `--dry-run` prints every step and creates nothing; an unknown tag is refused with the list of tags; the code step clones, is unchanged on the second run, switches tags, refuses hand-edited code and a non-empty non-git `code/`; the kiosk entry is written once; `kiosk.sh --url` reads the port from `ui.json`. The sudo steps (apt, snap, systemd) are only checked by `--dry-run` here and on a real machine.

## Limits (v1)
Ubuntu 22.04 only (24.04 refuses `pip install --user` into the system Python: use `--python` with a venv there); internet needed (no offline bundle, D-103); the GPU stack, the camera SDK and automatic login stay manual.
