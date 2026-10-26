# Installing DPC on a new site

Owned by MASTER. **Kept current with every change:** a handoff that adds or changes an install step says so in its `Install impact:` line, and MASTER updates this file in the same commit as STATUS.
Last verified end to end: Tadipatri, 2026-10-25 (layout extended 2026-10-26: re-run `setup_dpc_layout.sh` on existing sites) (captures under the Supervisor; inspection, mail, PLC and cleanup not yet installed).

What a site needs before you start, and what to write down while installing, is the data sheet in `COMMISSIONING.md` section 1. The acceptance checklist is `COMMISSIONING.md` section 3.

**Rules for every step:** run commands **one at a time** and check the result before the next. Never use `sudo` unless the step says so. Real configs live only in `~/INSIGHTZZ/config/` on the machine, never in the code folder or in git.

---

## Overview

| # | Step | Result |
|---|---|---|
| 1 | Machine and system packages | Ubuntu, GPU driver, Basler pylon, MySQL, mosquitto |
| 2 | Python environment | One Python with every package the programs need |
| 3 | Code and folder layout | `~/INSIGHTZZ/{code,config,logs,run,data}` |
| 4 | Database | Site DB from the seed, one health row per camera |
| 5 | `site.yaml` | The site's only config file, validated |
| 6 | Model | Model package in `data/models/<name>/<version>/` |
| 7 | Render | One JSON per program in `config/rendered/` |
| 8 | Supervisor service | Starts on boot, starts and watches the programs |
| 9 | Temporary `INF` cleanup | Needed until inspection runs |
| 10 | Final check | `dpc-check` passes |
| 11 | Verification | Frames, health, recovery, reload |

---

## 1. Machine and system packages

Ubuntu 22.04 (Tadipatri: Python 3.10.12), NVIDIA GPU with its driver and CUDA, Basler cameras on their own Ethernet port.

```bash
sudo apt update
sudo apt install -y git python3-pip python3-venv mysql-server mosquitto mosquitto-clients
sudo systemctl enable --now mysql mosquitto
```
- **Basler pylon:** install the pylon runtime from Basler's website (the cameras must appear in pylon Viewer).
- **Camera network port:** make sure it comes up by itself after a reboot: `nmcli connection show`, then `sudo nmcli connection modify "<camera connection>" connection.autoconnect yes`.
- **mosquitto** (MQTT broker): version 2 only accepts connections from this machine and needs no login, which is what we want.

**Check:** `systemctl is-active mysql mosquitto` prints `active` twice; `nvidia-smi` shows the GPU.

## 2. Python environment

Use **one** Python for the Supervisor and every program, and note its path (`which python3`); step 8 needs it. Tadipatri uses the system `python3`.

```bash
python3 -m pip install pypylon opencv-python-headless numpy pymysql PyYAML "paho-mqtt>=1.6" pymodbus
```
- **PyTorch and Detectron2** (inspection): install PyTorch for the machine's CUDA version (pytorch.org), then Detectron2 0.6 from source. Tadipatri has it in `~/detectron2-0.6`.

**Check:**
```bash
python3 -c "import cv2, pypylon, pymysql, yaml, paho.mqtt, torch, detectron2; print('imports OK', torch.cuda.is_available())"
```
Expected: `imports OK True`.

## 3. Code and folder layout

```bash
git clone https://github.com/purushottamkumar2004/DPC.git /tmp/dpc-bootstrap
bash /tmp/dpc-bootstrap/scripts/setup_dpc_layout.sh          # no sudo; safe to re-run
rmdir ~/INSIGHTZZ/code                                       # empty folder made by the script
git clone https://github.com/purushottamkumar2004/DPC.git ~/INSIGHTZZ/code
rm -rf /tmp/dpc-bootstrap
cd ~/INSIGHTZZ/code && git checkout main                     # later: a release tag, see SITES.md
```
If the repo is private, git asks for a login: user `purushottamkumar2004`, password = a GitHub token with read access.

**Check:** `ls ~/INSIGHTZZ` shows `code config data logs run`; `git -C ~/INSIGHTZZ/code log --oneline -1` shows the expected commit.

## 4. Database

**New site** (empty MySQL): create the database and the tables from the seed (`db/README.md`):
```bash
sudo mysql -e "CREATE DATABASE <db_name>;"
sudo mysql <db_name> < ~/INSIGHTZZ/code/db/schema.sql
sudo mysql <db_name> < ~/INSIGHTZZ/code/db/fixed_rows.sql
```
One health row per camera serial (the captures only UPDATE it):
```bash
sudo mysql <db_name> -e "INSERT IGNORE INTO health_status (ITEM, HEALTH) VALUES ('<CAM1 serial>','NOK'), ('<CAM2 serial>','NOK');"
```
- **DB user:** a dedicated user with rights on this database only is recommended; `root` is allowed and only gives a warning (D-050).
- **Existing site** (old DB kept, like Tadipatri): do not run the seed; check that the health table has one row per serial, and use its name as `db.health_table` in step 5.

**Check:** `sudo mysql <db_name> -e "SELECT ITEM, HEALTH FROM health_status;"` lists both serials.

## 5. `site.yaml`

The only file a person edits (`CONTRACTS.md` section 7.1). Start from the example, or from a similar site's reference file in `docs/sites/`:
```bash
cp ~/INSIGHTZZ/code/config/examples/site.example.yaml ~/INSIGHTZZ/config/site.yaml
chmod 600 ~/INSIGHTZZ/config/site.yaml
```
Edit it with any text editor (`gedit` or `nano`), never by pasting a long block into the terminal. Fill in: `site`, `paths.root`, `db` (name, health table, user, password), `graph` (Microsoft login), `capture.cameras` (serial, side, fps, exposure, rotate, ip), `inspection` (model name, threshold, wheel count, object sizes per side), `plc` (ip, registers, `light_on_value`), `mail` (project name, recipients), `cleanup`. Leave `transfer` out until the transfer service exists.

**Find the camera serials:**
```bash
python3 -c "from pypylon import pylon; print([d.GetSerialNumber() for d in pylon.TlFactory.GetInstance().EnumerateDevices()])"
```
CAM1 is the LEFT side, CAM2 the RIGHT (`CONTRACTS.md` 4.1). If unsure which camera is which, look at each camera's `live/IMG_1.jpg` after step 11.

**Check:**
```bash
cd ~/INSIGHTZZ/code && python3 -m common.config.site_config ~/INSIGHTZZ/config/site.yaml; echo "exit $?"
```
Expected: `OK` and `exit 0` (with `root` as DB user, one warning line). To share the file safely: add `--redacted`.

## 6. Model

The model's small files are in the repo (`models/<name>/<version>/`, D-065); the weights file (`.pth`) is copied to the machine separately (USB, `scp`, later OneDrive).

**A version already in the repo** (normal case):
```bash
cd ~/INSIGHTZZ/code
python3 scripts/make_model_package.py --from-repo models/dpc_maskrcnn/2026.10.12 --weights <path to the .pth>
```
A wrong `.pth` is refused (its checksum does not match the repo's manifest).

**A brand-new model** (not in the repo yet): follow `COMMISSIONING.md` section 2c (flatten the Detectron config, package, then `--export-to-repo` and commit the small files).

The first install sets `current` to that version (D-061). Later switches are done by the Supervisor (D-060).

**Check:** `ls ~/INSIGHTZZ/data/models/<name>/` shows the version folder and `current`.

## 7. Render the program configs

```bash
cd ~/INSIGHTZZ/code && python3 scripts/render_config.py ~/INSIGHTZZ/config/site.yaml
```
**Check:** one `written` line per program (two-camera site: `frame_capture_cam1`, `frame_capture_cam2`, `inspection_left`, `inspection_right`); running it again prints `unchanged` for all. The Supervisor also renders at every start (step 8), so this step is a check.

## 8. Supervisor service

```bash
cp ~/INSIGHTZZ/code/supervisor/config/workers.basler-2cam.yaml ~/INSIGHTZZ/config/workers.yaml
cd ~/INSIGHTZZ/code/supervisor
python3 supervisor.py check-config -c ~/INSIGHTZZ/config/workers.yaml
```
Check: no errors; the start order lists the captures. Then install the boot service, with the Python from step 2:
```bash
./install_service.sh --python "$(which python3)" --dry-run
./install_service.sh --python "$(which python3)"
sudo systemctl start dpc-supervisor
systemctl status dpc-supervisor | head -5
```
**Check:** `active (running)`; `pgrep -af FrameCapture_Basler` shows one process per camera.
- **Re-run `install_service.sh`** whenever a handoff says the service file changed (it did in P5: `ExecReload`).
- **Only one Supervisor at a time:** never start `supervisor.py run` by hand while the service runs (two copies fight over the cameras).

## 9. Temporary `INF` cleanup (until inspection runs)

Nothing consumes the `INF` folders before inspection runs (about 690,000 images a day with two cameras at 4 fps). Install this cron job:
```bash
echo "*/5 * * * * find /home/$USER/INSIGHTZZ/data/frames/CAM*/INF -name '*.jpg' -mmin +5 -delete" > /tmp/dpc_cron
crontab /tmp/dpc_cron
crontab -l
```
**Check:** `crontab -l` shows the one line. **Remove it** (`crontab -r`) once inspection runs under the Supervisor (STATUS P7).

## 10. Final check

```bash
cd ~/INSIGHTZZ/code && python3 scripts/dpc_check.py; echo "exit $?"
```
Expected: every line `PASS` (allowed `WARN`: `root` DB user, MQTT login not tested, a camera IP that does not answer ping); `exit 0` or `exit 2`. Any `FAIL` must be fixed first.

## 11. Verification

```bash
ls -l --time-style=+%T ~/INSIGHTZZ/data/frames/CAM*/live/IMG_1.jpg; date +%T
mysql -u <db_user> -p <db_name> -e "SELECT ITEM, HEALTH, CREATE_DATETIME FROM <health_table>;"
mosquitto_sub -h localhost -t 'dpc/#' -v          # Ctrl+C after a status message per worker
```
- **Frames:** both image times within a few seconds of `date`.
- **Health:** `OK` for every serial.
- **Recovery:** `kill -9` one capture: restarted within 10 s; pull a camera cable for 2 min: `NOK`, then `OK` with frames resuming.
- **Reload:** change one camera's `exposure_us` in `site.yaml`, run `sudo systemctl reload dpc-supervisor`: only that camera restarts (`~/INSIGHTZZ/run/reload_result.json`); put the value back and reload again.

Then fill the site's entry in `SITES.md` and add a reference copy without secrets to `docs/sites/<site>.site.reference.yaml`.

---

## Updating an installed site
```bash
cd ~/INSIGHTZZ/code && git status --short          # must show no changed tracked files
git pull origin main                               # later: git fetch --tags && git checkout v0.<n>
python3 scripts/dpc_check.py
sudo systemctl restart dpc-supervisor              # or reload, if only site.yaml changed
```
Read the handoffs since the last update for an `Install impact:` line (for example "re-run install_service.sh").

## Changes to a running site
- **A setting:** edit `site.yaml`, then `sudo systemctl reload dpc-supervisor` (only the affected programs restart).
- **`workers.yaml`:** needs `sudo systemctl restart dpc-supervisor` (reload does not re-read it).
- **Never** edit files in `config/rendered/` (overwritten at the next render) or anything inside `code/`.

## Not covered yet (added here when built)
Inspection under the Supervisor (P7), cleanup (P9), mail and PLC services, model updates over OneDrive (P11, P12), the installer script and setup wizard.
