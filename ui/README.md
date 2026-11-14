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
- Dashboard image: the plain `live/IMG_1.jpg` only, read-only; no overlay, boxes or bands (F66). A frozen
  image gets a red "Last image N s old" badge.
- Camera page per side (`/cameras/<SIDE>/`) with two tabs:
  - **Live**: the plain image.
  - **Detections** (F66): `live_frame.jpg` plus an SVG layer drawn from `live_detections.json`
    (inspection, F65, in `data/state/<inspection worker>/`).
    - Class checkboxes start unticked; the choice is kept per browser.
    - Only ticked classes are drawn, each with its auto-ROI band. Off-path boxes are red: "removed" in
      filter mode, "would be removed" in observe.
    - The view refreshes every 3 s.
    - The image and the boxes are always one pair: the JSON's `frame_mtime_ns` must match the image,
      and the last matched pairs are kept by `seq`.
    - Until F65 writes the pair, the tab says "Detections not available yet".

## Pages (D-101, after the ISA-101 display hierarchy)
Sidebar, 6 entries: Overview, LEFT, RIGHT, Defects, System, Setup (greyed: "Site setup wizard: coming").

| URL | Level | Content |
|---|---|---|
| `/` | 1 Overview | data only, no images, no defect list (`docs/ui/overview_reference.html`); one screen 1920 x 1080 |
| `/side/LEFT/`, `/side/RIGHT/` | 2 side | header with the side's two programs; image (Live / Detections, whole frame); functions; auto-ROI (counts, previews, out-of-bound crops); model; camera |
| `/defects/` | | filter, image, CSV |
| `/system/programs/`, `/system/health/`, `/system/alerts/` | 3-4 System | programs with logs (F63/F64), health with dpc-check, alert history |

Older addresses (`/programs/`, `/alerts/`, `/health/`, `/cameras/<SIDE>/`) still work.

Overview, top to bottom:
- top bar: site, clock, status pill (worst state in plain words), "Cycle: wheel N of T" with a bar, and the
  last plate;
- one card per side: a one-word state, camera, inspection, model and auto-ROI rows, and function chips;
- Services (every program: state, one key value, heartbeat age) and Alerts (scrolls inside its card);
- device strip: PLC, light, conveyor, disk, GPU, database, MQTT.

## Setup (S8, D-103, D-107, D-108)
`/setup/` is a step-by-step wizard; in setup mode (`run/mode.json` mode = setup) the UI opens on it and the
other pages are greyed. In normal mode it warns that applying restarts programs.
Steps: site, database (D-112: the person creates the database and its login, e.g. in MySQL Workbench; the
step pre-fills host, port, `<site.id>_db`, user and "standard password set" from `config/site_defaults.yaml`,
root allowed with a warning; **Test connection** checks the login and that the database exists, **Prepare
tables** runs `db/bootstrap.py --seed-only` (F97); never creates a database or user),
cameras (search lists Basler and FLIR; grab and burst go through camera_probe with the camera's driver, FLIR
via flir_probe; LEFT/RIGHT, exposure/fps/rotate, FLIR interface and throughput_bps with defaults "GEV Interface 1"
and 19662239; record frames, detect direction, confirm), PLC (read,
light off 5 s, cleaning pulse), machine, model (choose files under /media, /mnt or the root; package; self-test),
object sizes (measure: p10 proposed, median beside it), mail (optional, test mail), data transfer (optional,
check), review (the real loader, secrets as set / not set, warnings).
Every button runs its tool with `--json` as a background job, one at a time (`monitor/setup/jobs.py`).
Secrets go in through env or stdin, never argv, and live only in memory until Apply writes site.yaml
(`monitor/setup/vault.py`); the draft holds none.
Apply (one job): `bootstrap.py --seed-only` again with the chosen serials (health rows), then `site_from_draft` and `write_site`, then
`write_workers_file`, then `run/setup_finish {id}`. It waits for `setup_finish.result.json` with that id
and for normal mode, then opens the Overview.
Commissioning (`/setup/commissioning/`): live ticks (camera fps, PLC, first plate, wheels per revolution,
auto-ROI learn/observe/filter, test mail, transfer check/model update/upload), remembered once reached, and
a saved report.

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
| Model version | inspection heartbeat `model {name, version}` (R6); null = "Loading"; missing = "—" |
| ms per image, waiting for plate | inspection heartbeat `ms_per_image`, `waiting_for_plate` (F60) |
| Function chips / side functions | `checks` in the rendered `inspection_<side>.json` (bucket_clip = bolt check, skidding, wheel_missing, cycle_restart) |
| Camera row (overview) | capture heartbeat `fps`, `last_frame_age_s`, `camera_connected` (F61); else measured from `live/IMG_1.jpg` |
| Removed in the last hour | rise of `roi_status.json` outside counts over the UI's own samples (restarts handled) |
| Last plate | the UI notes when the live wheel number starts again (no heartbeat field for it yet) |
| Wheel "no value yet" | `wheel_counter_table` row ID 1 missing or empty; the writer side from `writes_live_wheel_counter` |
| Side page model | `<models_dir>/<name>/` (installed versions, `current`, `pinned`), read-only |
| Side page camera | rendered `frame_capture_<camn>.json` `camera` {serial, exposure_us, fps, rotate}, read-only |
| Auto-ROI images | `roi_preview_*.jpg` listed in `roi_status.json` `roi.previews`, crops listed in `roi_outside.jsonl`; only those names are served |
| Programs "Since" | running: now - status `started_at` (F63); otherwise time in the current state |
| Program log (F64) | the program's own log in `<logs>/`: `log.file_name` from its rendered JSON, else `FrameCapture_<CAM>.log`, `<inspection id>.log`, `cleanup.log`, `mail.log`, `plc.log`, `ui.log`. Tab "Start and crash output" = the Supervisor capture (status `log_file`). Older days = `<name>.YYYY-MM-DD[_HH-MM-SS]` |
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
  so there is one MQTT subscriber and one in-memory state). The UI logs to `<logs>/ui.log`, rotated at
  midnight; its console, which the Supervisor captures, keeps warnings and errors only. It applies its SQLite migrations at start,
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
