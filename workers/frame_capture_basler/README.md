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
- Repeating problems are logged at most once per minute, with a repeat count.
- SIGTERM / Ctrl+C: the current frame is finished, the camera is closed, `_tmp` is left empty.

## Tests
```
python3 -m unittest discover -s workers/frame_capture_basler/tests -t . -v
```
A fake `pypylon` (`tests/fake_pypylon/`) stands in for the camera. One test renders the Tadipatri reference
`site.yaml` with the real renderer and loads both capture files.
