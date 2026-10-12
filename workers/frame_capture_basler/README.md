# frame_capture_basler

Single-camera Basler FrameCapture with atomic hand-off (D-031). One process per camera (D-033). Owner chat: W-frame_capture_basler.

Per frame: write once to `_tmp`, hard-link into `INF` (and `UI` when empty), `os.replace` into `live/IMG_1.jpg`. See `docs/ARCHITECTURE.md` section 3a and `docs/CONTRACTS.md` sections 3 and 4.1.

## Run
Give the camera number; the file is read from `<root>/config/` (found from where the script sits):
```
python3 FrameCapture_Basler.py 1     # <root>/config/BASLER_CONFIG1.xml
python3 FrameCapture_Basler.py 2     # <root>/config/BASLER_CONFIG2.xml
```
Lookup order: argument (camera number or a path to an XML file), then env `BASLER_CONFIG`, then `BASLER_CONFIG1.xml` in the working directory.
Copy `BASLER_CONFIG1.example.xml`, fill in the serial and DB login, keep the real file out of git (chmod 600).
A config error (bad value, missing element) stops the process at once with exit code 2 and a message on stderr.

## Optional config elements (append-only, read by position): 16-18 from D-041, 19 added later
| Pos | Element | Default (absent or empty) | Notes |
|---|---|---|---|
| 16 | `HEALTH_TABLE` | `tadipatri_health_status_table` | Letters, digits, `_` only. Set it on every site. |
| 17 | `LOG_FILE_NAME` | `FrameCapture_Basler.log` | Required in practice when two cameras run: give each its own name. |
| 18 | `ROTATE_DEG` | `90` | `0`, `90`, `180`, `270` clockwise. `0` = no rotation. Any other value stops the process at start. |
| 19 | `HEALTH_INTERVAL_S` | `600` | Seconds between repeated health writes while the state is unchanged. Whole number > 0. |

Elements are read by position: to set a later one, the earlier optional elements must be present (they may be empty).

## Behaviour
- **Camera lost** (device removed, or no good frame for 15 s): the camera is closed and reopened every 2 s until it answers, without restarting the process. The log shows `LOST` and `REOPENED` lines.
- Only a frame grabbed in the current loop iteration is published; timeouts and failed grabs publish nothing.
- **Health**: an `UPDATE` of the row `ITEM = <serial>` in `HEALTH_TABLE`: at once when the state changes (OK after frames flow, NOK when the camera is lost), otherwise at most every `HEALTH_INTERVAL_S` seconds (default 600). A DB outage is logged and never stops capture. The row must already exist.
- **Camera settings** are set by feature name before grabbing (older `*Abs` names first, then the newer ones), with one log line per setting.
- Repeating problems are logged at most once per minute, with a repeat count.
- SIGTERM / Ctrl+C: the current frame is finished, the camera is closed, `_tmp` is left empty.

Open issues: `docs/CODE_AUDIT.md` section 6.
