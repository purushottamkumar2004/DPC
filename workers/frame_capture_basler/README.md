# frame_capture_basler

Single-camera Basler capture with atomic hand-off (D-031). One process per camera (D-033):
`frame_capture_cam1` (CAM1 = LEFT) and `frame_capture_cam2` (CAM2 = RIGHT). Owner chat: W-frame_capture_basler.

Per frame: write once to `_tmp`, hard-link into `INF` (and `UI` when empty), `os.replace` into `live/IMG_1.jpg`.
See `docs/ARCHITECTURE.md` section 3a and `docs/CONTRACTS.md` sections 4.1 and 13.

## Config
All settings come from the rendered file `<root>/config/rendered/frame_capture_<camn>.json` (CONTRACTS 13, D-062).
The renderer writes it from `site.yaml`; the Supervisor passes its path in the env variable `DPC_WORKER_CONFIG`.
This program reads no XML and no `site.yaml`. To change a camera setting: edit `site.yaml`, then
`python3 supervisor/supervisor.py reload`; the Supervisor restarts only the capture whose file changed.

Keys used: `worker_id`; `paths` {logs, live, tmp, inf, ui}; `log` {backup_count, file_name};
`camera` {id, side, driver (must be `basler`), serial, fps, exposure_us, rotate (0/90/180/270)};
`db` {host, port, name, user, password, health_table}; optional top-level `health_interval_s`
(whole seconds > 0, default 600; the renderer does not write it yet).

A missing env variable, a missing key or a bad value stops the program at start with exit code 2 and a
message naming the key (never a value).

## Run by hand (without the Supervisor)
```
cd ~/INSIGHTZZ/code/workers/frame_capture_basler
DPC_WORKER_CONFIG=~/INSIGHTZZ/config/rendered/frame_capture_cam1.json python3 FrameCapture_Basler.py
```
The file exists after the Supervisor has started once, or after `python3 scripts/render_config.py`.
Stop the Supervisor's copy first: two processes cannot open the same camera.

## Behaviour
- **Camera lost** (device removed, or no good frame for 15 s): the camera is closed and reopened every 2 s
  until it answers, without restarting the process. The log shows `LOST` and `REOPENED` lines.
- Only a frame grabbed in the current loop iteration is published; timeouts and failed grabs publish nothing.
- **Camera settings** are set by feature name before grabbing (older `*Abs` names first, then the newer
  ones), with one log line per setting, e.g. `Camera setting ExposureTimeAbs = 2000.0 (requested 2000.0)`.
- **Health**: an `UPDATE` of the row `ITEM = <serial>` in `db.health_table`: at once when the state changes
  (OK when frames flow, NOK when the camera is lost), otherwise at most every `health_interval_s`.
  Rows are counted as matched, not changed (`CLIENT.FOUND_ROWS`, F7), so "matched no row" really means the
  row is missing. A DB outage is logged and never stops capture. The row must already exist.
- **Heartbeat** (D-034, `common/heartbeat.py`): every 10 s on `dpc/worker/<worker_id>/health` with
  `fps` (frames published in the last 10 s), `last_frame_age_s` (null before the first frame) and
  `camera_connected`; `healthy` = camera connected and a frame younger than 5 s. It needs an `mqtt` block in
  the rendered file and paho-mqtt; without them it is off (one warning) and capture runs as normal.
- **Crash report** (F21, `common/last_error.py`): an unexpected error writes
  `<root>/run/<worker_id>.last_error.json` (type, message, file:line, short traceback, passwords replaced
  by `***`) and exits 1; a config error writes it too and exits 2. The file of an earlier run is removed
  at start.
- Repeating problems are logged at most once per minute, with a repeat count.
- SIGTERM / Ctrl+C: the current frame is finished, the camera is closed, `_tmp` is left empty.

## camera_probe (setup and checks, STATUS S4)
Lists the cameras on this machine and grabs one image, without the capture program. Used by the UI
setup wizard (D-103 step 3) and by hand:
```
python3 camera_probe.py list [--driver all|basler|flir] [--json]
python3 camera_probe.py grab SERIAL OUT_FILE [--exposure US] [--timeout-ms MS] [--driver auto|basler|flir] [--json]
python3 camera_probe.py burst SERIAL OUT_DIR [--count 20] [--interval-ms 250] [--exposure US] [--timeout-ms MS]
                              [--driver auto|basler|flir] [--json]
```
In Python: `camera_probe.list_all()`, `camera_probe.grab_one(serial, out_path, exposure_us=None, timeout_ms=5000, driver="auto")`,
`camera_probe.burst(serial, out_dir, count=20, interval_ms=250, exposure_us=None, timeout_ms=5000, driver="auto")`.

- `list` gives per camera: driver, serial, model, IP (GigE only), `access` (what the SDK says without opening:
  pylon `accessible` / `not accessible`, Spinnaker's DeviceAccessStatus such as `ReadWrite`, `ReadOnly`), device
  class and user name. `in_use` is always null in a list (F106): only opening the camera tells, so ask `grab`.
- **In use is decided only by opening** (F106): `grab` and `burst` always try `Open()` (pylon) / `Init()`
  (Spinnaker), and say `in_use` only when that fails with a busy / access-denied error, with the SDK's words in
  `message`. Any other open error is `failed` with the SDK's text and the access status (e.g. a camera on another
  subnet than the PC port). The access status alone never refuses: on Dhar it said "not free" for two free FLIR
  cameras. Every grab and burst result carries `access` as information. A missing SDK is reported per driver ("Basler SDK not installed",
  "FLIR SDK not installed"), never an error.
- `grab` writes one image (.jpg, .png or .bmp; written next to the target, then renamed). Status and exit code:
  `ok` 0; `failed` 1 (open or grab failed, the SDK's text in `message`); `sdk_missing` 1; `bad_request` 2;
  `in_use` 3; `not_found` 4.
- A camera held by another program (normally the running capture) is reported `in_use` and never forced:
  stop that program first. The probe sets TriggerMode Off, and with `--exposure` ExposureAuto Off and the
  exposure; these live in the camera's working memory only, and the capture sets its own values when it starts.
- `burst` (F89, for the conveyor direction): COUNT frames, one every INTERVAL ms (0 = as fast as the camera
  delivers; at most 200 frames), into OUT_DIR (created if its parent exists). Files are named by capture time
  like the capture's frames (`2026_11_12_10_15_03_250114.jpg`, CONTRACTS 4.1), so
  `measure.py --frames OUT_DIR --count COUNT` reads exactly this burst: it takes the newest COUNT files by name,
  so pass the same count (or use a fresh folder per burst). The camera keeps grabbing and each tick takes the
  newest frame; the frame-rate limit is lifted for the burst (working memory only; the capture sets its own rate
  on start). Same in-use handling and exit codes as `grab`. The result adds `dir`, `count`, `interval_ms`,
  `saved` and `frames` ([{index, path, t_ms}], t_ms since the first frame). If a frame fails mid-burst the result
  is `failed` with the message naming the frame, and the frames saved before it stay and are listed.
- FLIR (Spinnaker, PySpin) is in `flir_probe.py` with the same functions and results; first real FLIR use is Dhar (Blackfly S BFS-PGE-23S3C, GigE), to be confirmed by F106's site check.
- Try it without hardware: `PYLON_CAMEMU=2 python3 camera_probe.py list` (pylon's emulated cameras).

## Tests
```
python3 -m unittest discover -s workers/frame_capture_basler/tests -t . -v
```
A fake `pypylon` (`tests/fake_pypylon/`) and a fake `PySpin` (`tests/fake_pyspin/`) stand in for the cameras;
one probe test uses the real pylon SDK with its emulated cameras when pypylon is installed (else skipped). One test renders the Tadipatri reference
`site.yaml` with the real renderer and loads both capture files.
