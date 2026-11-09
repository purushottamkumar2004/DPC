# DPC local UI (`ui/`, worker `ui`)

The monitoring and (from UI-2) control screen of a DPC site (D-095). A Django application on
**127.0.0.1 only**, opened in a full-screen browser on the same machine. It uses no internet, no CDN
and no web fonts: htmx and every style are bundled in `static/`.
**Stage UI-1 (this version): read-only monitoring.** No login (developer, 2026-11-06; proposed D-095 amendment).

## Pages
| URL | Page | Refresh |
|---|---|---|
| `/` | Dashboard: overall banner, per side the live image, camera and inspection state; tiles for wheel, PLC, disk, GPU, mail, cleanup; last 10 defects | 2 s (images 1 s) |
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
| Auto-ROI mode, drift | `data/state/inspection_<side>/roi_status.json` and `roi_drift` events |
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
