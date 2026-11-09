# DPC local UI (`ui/`, worker `ui`)

The monitoring and (from UI-2) control screen of a DPC site (D-095). A Django application on
**127.0.0.1 only**, opened in a full-screen browser on the same machine. It uses no internet, no CDN
and no web fonts: htmx and every style are bundled in `static/`.
**Stage UI-1 + UI-1b (this version): read-only monitoring, D-098 style.** No login (D-096).

## Look (UI-1b, D-098)
- Dark theme by default with the D-098 colours; a light toggle (sidebar, bottom) is stored per browser.
  Colour carries meaning only: green OK, amber attention, red failure, grey off.
- Dashboard = `docs/ui/dashboard_reference.html`, on one 1920 x 1080 screen with no scrolling:
  - top bar: site, clock, status pill, "Wheel N of 190";
  - two camera cards, each with the live image in the camera's own aspect ratio (no black bars) and four
    tiles: camera, inspection, model, auto-ROI;
  - device strip: PLC, light, conveyor, disk, GPU, mail;
  - alerts and last defects side by side.
- Slim icon sidebar (Lucide 0.460.0, ISC, bundled in `templates/monitor/icons.svg`). Pages of later stages
  are greyed.
- Interaction:
  - every tile, alert and defect links to its detail page; every value has a one-sentence tooltip;
  - one poll per `refresh_seconds` returns small out-of-band fragments, so values change in place and the
    images never reload;
  - new alerts slide in highlighted;
  - sparklines (camera fps, inspection rate, GPU temperature, disk) are plain inline SVG from the UI's own
    samples (`monitor/state/samples.py`, every 10 s, last hour, kept in memory only).
- Live overlay (F60): if `data/state/inspection_<side>/live_overlay.jpg` is younger than 10 s, it is shown
  instead of `live/IMG_1.jpg`, with an "overlay" badge. A frozen image gets a red "Last image N s old"
  badge.
- Camera page per side (`/cameras/<SIDE>/`): large image, programs, rates, auto-ROI per class.

## Pages
| URL | Page | Refresh |
|---|---|---|
| `/` | Dashboard (D-098 reference layout) | 2 s, one poll (images 1 s) |
| `/cameras/<SIDE>/` | Camera page: large image, programs, rates, auto-ROI per class | 2 s |
| `/alerts/` | Alerts and errors: what is wrong **now**, then the stored history, newest first; technical details behind "Details" | 2 s |
| `/programs/`, `/programs/<id>/` | Every program: state, since, restarts, heartbeat age, last error; last 200 log lines with text and level filters | 2 s / 5 s |
| `/health/` | Disk, GPU, CPU/RAM, database, MQTT, Supervisor, PLC, cameras; "Run dpc-check" | 2 s |
| `/defects/` | Filter by date, side, type, wheel; defect image; CSV export | on request |

## Where every value comes from
| Shown | Source |
|---|---|
| Program states, restarts, last error | retained `dpc/worker/<id>/status` (Supervisor, CONTRACTS 2) |
| Programs switched off | `<root>/config/workers.yaml` `enabled: false` (read-only; a disabled worker publishes nothing) |
| Inspection img/s, images waiting | heartbeat `dpc/worker/inspection_<side>/health` (`fps`, `inf_queue_depth`) |
| ms per image | heartbeat `ms_per_image` when inspection publishes it (not yet: shown "—") |
| Camera fps, image age | the UI stats `data/frames/<CAM>/live/IMG_1.jpg` 10 times a second (no capture heartbeat yet) |
| Live image | `data/frames/<CAM>/live/IMG_1.jpg`, opened read-only, **never deleted or modified** (D-031, F45) |
| Supervisor online/offline | retained `dpc/supervisor/status` |
| PLC connected, light, conveyor | retained `dpc/plc/status` |
| Auto-ROI mode, drift, removed count | `data/state/inspection_<side>/roi_status.json` (sum of `classes.*.outside`) and `roi_drift` events |
| Model version | not published yet: shown "—" (request: inspection heartbeat `model {name, version}`) |
| Conveyor | `dpc/plc/status.conveyor_status`, shown as the raw value (its meaning is not documented) |
| Disk | used / (used + free) of `paths.data`, graded by `cleanup.disk_percent` (same as cleanup and dpc-check) |
| GPU | `nvidia-smi --query-gpu=...` (cached 5 s) |
| CPU, RAM | `/proc` |
| Camera reachability | `ping -c1 -W1 <camera ip>` every 15 s |
| Live wheel, defects | site MySQL `wheel_counter_table` ID 1 and `defects_table` (SELECT only, base columns only) |
| Alert history | every `.../event` message plus changes the UI notices itself, stored in SQLite |

## Data the UI owns
`<root>/data/state/ui/`: `ui.sqlite3` (events, last known states, Django users/sessions), `secret_key`
(generated at first start, chmod 600). Events are kept `ui.event_keep_days` (90) and at most
`ui.event_keep_rows` (50 000). The UI never writes the site MySQL DB and never writes outside this folder.

## Settings
Only from `DPC_WORKER_CONFIG` = `<root>/config/rendered/ui.json` (F58). Missing file or key = a safe
default plus one WARNING per key at start. Keys: `worker_id`, `site` {id, name}, `paths` {root, code,
config, site_yaml, workers_file, logs, run, data, frames, state}, `log`, `ui` {port, refresh_seconds,
event_keep_days, event_keep_rows}, `db` {host, port, name, user, password}, `mqtt` {host, port, user,
password, topic_prefix}, `cameras` [{id, side, serial, ip}], `sides` [{side, camera_id,
inspection_worker}], `cleanup.disk_percent` {delete, raw_delete, mail, inference_delete},
`machine.total_wheel_count`. Secrets are never shown; DB and MQTT passwords are also masked in any
log line, traceback or payload the UI displays.

## Plain language
`monitor/messages.py` holds the tables: worker states by type (`STATUS_TEXT`), events (`EVENT_TEXT`)
and the PLC. Each entry is one sentence plus one action. Add or change messages there; `{label}`
becomes the program's plain name ("LEFT camera"). The overall banner rule is in `monitor/overview.py`.

## Run
- **Production:** the Supervisor runs `python3 run_ui.py` (waitress, one process with 8 threads,
  so there is one MQTT subscriber and one in-memory state). It applies its SQLite migrations at start,
  publishes its heartbeat on `dpc/worker/ui/health` every 10 s and stops on SIGTERM.
- **Development:** `DPC_WORKER_CONFIG=<ui.json> python3 run_ui.py`, then open http://127.0.0.1:8080.
- **Tests:** `cd ui && python3 manage.py test monitor` (no broker, MySQL, camera or GPU needed).
- **Accounts (prepared for UI-3/UI-5, not needed now):** `python3 manage.py create_ui_admin --username <name>`.

## Layout
```
run_ui.py            entry point (Supervisor worker `ui`)
dpc_ui/              Django project: settings (SQLite only), urls, ui_config.py (ui.json loader)
monitor/state/       MQTT subscriber, state cache, event store, background checks
monitor/sources/     site DB (read-only), system readings, files (live image, logs), workers.yaml, dpc-check
monitor/messages.py  plain-language tables      monitor/overview.py  programs, sides, problems, banner
monitor/views/       one module per page        monitor/templates/   pages and HTMX partials
static/monitor/      ui.css, ui.js, vendor/htmx-2.0.4.min.js (sha256 e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447)
```
