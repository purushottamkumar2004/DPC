# DPC Inspection System: Architecture (v3, based on the real code)

v3 changes: deployed folder layout (section 9), atomic frame hand-off (3a), commissioning flow (6a), site.yaml as source of truth, repo and chat structure (section 10). Decisions D-030 to D-038 in DECISIONS.md.

Legend: **[BUILT]** implemented and tested, **[EXISTING]** current production code, **[PLANNED]** designed only.

## 1. System in one picture
```
                   CLOUD (later)  central dashboard for many sites           [PLANNED]
                        ^   status/defects pushed from each site
 ---------------------- | ------------------------------------ SITE (edge server, 24x7) ----
                        |
   Setup wizard + Monitoring UI  <->  Django API  <->  MySQL (site DB)       [EXISTING Django, PLANNED wizard]
                                             |
                                             v   site config -> renderer -> legacy XML files   [PLANNED]
                                     SUPERVISOR  [BUILT V1]
            +-----------------+----------+----------+----------------+
            v                 v          v          v                v
       frame_capture   left_algo  right_algo   mail sender     delete_script   plc services   reports/sync
       (FLIR/Basler)   (CAM1)     (CAM2)       (SMTP|Graph)    (cleanup)       light/cleaning
            |               ^           ^             ^
            v               |           |             |
   CAM1/INF  CAM2/INF  (image queues) --+             +-- polls defects_table
   IMG_1.jpg live image                  |
                                         v
          RAW_IMAGES, PLATE_DIR, DEFECTIVE_IMG_DIR, defects_table (MySQL)
                       MQTT (Mosquitto): supervisor status/events, worker heartbeats, commands
```

## 2. Principles
1. Supervisor manages, workers work. No domain logic in the Supervisor.
2. Site-specific = configuration, never code.
3. Workers are independent processes; they communicate by files (image queues), MySQL, and MQTT. They never import each other.
4. Local calls stay local; MQTT only for status, events, heartbeats, commands.
5. Tolerant monitoring (5-10 s is fine) because **algorithm restarts are expensive** (see 5.2).
6. Preserve existing business logic. Old code keeps running unmodified; change it gradually behind adapters.
7. No secrets in code, repo, XML or Project knowledge.
8. Config is read from local files at start. The system must boot and inspect without MQTT (D-034).
9. Frame hand-off is atomic: a reader never sees a half-written file, and nobody modifies hand-off files in place (D-031).

## 3. Real data flow (from the code)
1. **FrameCapture** grabs frames per camera (config selects FLIR/Spinnaker or Basler/Pylon; cameras identified by serial). Per frame it writes the live `IMG_1.jpg`, copies it to the UI folder (when empty) and to `INF/<YYYY_MM_DD_HH_MM_SS_ffffff>.jpg`. It updates DB health (`health_status`, `CAMERA_PLC_STATUS`) about every 120 s.
   **New (D-031, D-033):** `workers/frame_capture_basler/` is a single-camera version that hands frames off atomically (section 3a) and reads its own config file; the plan is one capture process per camera.
2. **Left/RightAlgorithm** (separate programs): on start deletes the INF backlog, then loops: take oldest INF image, validate, run Detectron2, map labels (`changeLabelName`), group by class.
   - plate seen -> copy image to `TEMP_IMG_DIR/PLATE_DIR/<SIDE>`; new physical plate (x-movement) -> cycle check, reset wheel counter. **No plate yet -> frame skipped.**
   - wheel tracking (`checkWheelCounter`, spatial), bucket clip / nut_bolt check, wheel rotation (magnet/bolt), wheel missing (uses wheel_stand counts), others currently commented out.
   - defect -> save image under `DEFECTIVE_IMG_DIR/<SIDE>/<date>/`, dedupe in DB (24 h), `INSERT defects_table`.
   - processed image is moved INF -> `RAW_IMAGES`.
3. **Mail sender** polls `defects_table WHERE is_alert_sent=0`, drops duplicates (batch + DB), sends one batched email (SMTP/Gmail or Microsoft Graph), sets `is_alert_sent` 1 (sent) or 2 (ignored).
4. **delete_script** loops every 10 s: disk usage -> delete old RAW, INF, REPEATED, PLATE, DEFECTIVE by thresholds and retention; daily disk mail and threshold alerts.
5. **PLC scripts** (Modbus TCP): `light.py` writes light register; `cleaning_mechanism.py` pulses cleaning register 1 for 10 s then 0. Both are one-shot.
6. **Django UI/API** reads DB/files for status, defects, images, config.

## 3a. Frame hand-off (atomic) [BUILT in `frame_capture_basler`, PLANNED for the FLIR version] (D-031)
Per frame, FrameCapture writes the JPEG **once** into a staging folder that nobody else reads, then publishes it with hard links and a rename (both instant and atomic):
1. `cv2.imwrite` -> `frames/CAMn/_tmp/<ts>.jpg`
2. `os.link` -> `frames/CAMn/INF/<ts>.jpg` (the algorithm's work queue)
3. `os.link` -> `frames/CAMn/UI/IMG_1.jpg`, only when `UI/` is empty (a mailbox: the consumer empties it)
4. `os.replace` -> `frames/CAMn/live/IMG_1.jpg` (always last: it moves the staging name away)

Rules: `_tmp`, `INF`, `UI` and `live` must be on one filesystem; hard links share data, so no process may modify these files in place (read, move or delete only); if hard links are impossible FrameCapture falls back to a copy and logs a critical warning once; stale `_tmp` files are cleared at start. The old way (in-place `imwrite`, then `copy2` into INF and UI) let readers open partial JPEGs and wrote every frame twice.

## 4. Supervisor [BUILT, V1]
Folder `supervisor/`: `supervisor.py`, `core/{config,process_manager,runtime_manager,monitor,recovery_manager}.py`, `health/{base,basic}_health_check.py`, `communication/mqtt_client.py`, `workers/dummy_worker.py`, `config/workers.yaml`.
- Worker states: STARTING, RUNNING, UNHEALTHY, WAITING_RESTART, STOPPED, FAILED.
- Health types today: `process`, `file_activity` (file/folder mtime), `heartbeat` (MQTT).
- Failure rule: dead process -> restart at once; failing health must persist `worker_timeout_seconds`; `max_retries` then FAILED; retries are earned back after `reset_after_seconds` of health.
- CLI: `run | check-config | status | start|stop|restart <id>`; commands travel over MQTT.
- Tested with dummy workers only; not yet connected to real workers.

### V1 gaps the real code exposed [PLANNED fixes]
| Gap | Why it matters | Fix |
|---|---|---|
| Only long-running services supported | `light.py` and `cleaning_mechanism.py` exit by design; V1 would restart them until FAILED | worker `mode: service / oneshot / periodic` |
| No "queue lag" health | best signal that an algorithm is stuck: oldest file in INF too old | health type `queue_lag` |
| No DB-based health | FrameCapture already writes `health_status` | health type `db_freshness` |
| Restart cost not modelled | algorithm restart loses wheel/plate state, blind until next plate | per-worker `restart_cost: high` => longer timeouts; later state persistence |
| No config reload / DB config | needed for UI-driven setup | ConfigurationManager from DB + `reload` |
| `EnvironmentFile`, per-worker user | systemd units may use them | `env_file:` option; check users |

## 5. Operational behaviour to design around
### 5.1 Startup order
mosquitto -> MySQL -> light (oneshot) -> frame_capture -> mail/delete -> algorithms (so INF is fresh) -> UI.
### 5.2 Algorithm restart cost
In-memory wheel counter, plate state, histories; INF backlog deleted at start; frames skipped until the next plate (up to one revolution at ~4 FPS). Supervisor policy for algorithms: long `startup_grace_seconds` (Detectron2 load), generous `worker_timeout_seconds`, health from queue lag, few retries.
### 5.3 Disk
Cleanup is threshold-driven (75/85/90/95 %); `delete_script` measures the disk of its working directory, so run it from the data disk or fix to use configured paths.
On single-disk edge machines (e.g. Tadipatri: one NVMe) the thresholds apply to the whole disk, so OS, DB and images share the headroom and cleanup is the only protection against a full disk. Raw images are expected to fill the disk up to the raw-delete threshold; that is normal. Pass the data root to the usage check explicitly instead of relying on `os.getcwd()`.

## 6. Product layer [PLANNED]
```
dpc-platform/                     one monorepo on GitHub (supersedes the earlier dpc-product sketch)
  docs/        ARCHITECTURE CONTRACTS DECISIONS CODE_AUDIT PROJECT_INSTRUCTIONS CHAT_WORKFLOW OWNERS SITES
  common/      shared code: config loader, MQTT helpers, path helpers            [PLANNED]
  supervisor/  generic worker manager                                            [BUILT V1]
  workers/     frame_capture/        legacy dual-camera FLIR/Basler              [EXISTING]
               frame_capture_basler/ single camera, atomic hand-off              [BUILT]
               inspection/           Left + Right in one folder until D-021      [EXISTING]
               mail/  cleanup/  plc/                                             [EXISTING]
  ui/          Django UI / API                                                   [EXISTING, to move in]
  config/      schema/  renderer/  examples/ (site.example.yaml)                 [PLANNED]
  scripts/     setup_dpc_layout.sh, install helpers
  tests/       unit/  golden/ (renderer output)  replay/ (image sequences)
```
- **Site config** (one source of truth: file-first `site.yaml`, validated; DB-backed later, D-014/D-033) -> **renderer** writes the legacy XML files and `workers.yaml` in exact tag order, so existing programs run unchanged. Secrets are part of `site.yaml` (chmod 600, D-048).
- **Setup wizard**: site name -> model upload (validate, GPU test-load, class list) -> cameras (discover, assign LEFT/RIGHT) -> PLC (test) -> mail/retention -> Apply -> **self-test** (frames arrive, inference runs, DB insert, PLC read/write, test mail).
- **Monitoring UI**: Django subscribes to MQTT status/events; actions via authenticated API -> MQTT command.
- **Installer**: preflight (GPU driver, CUDA, Detectron2 version), pinned deps, systemd units for mosquitto, supervisor, Django.
- **Cloud layer (later)**: sites push status/defect summaries to a central dashboard; inference stays on the edge GPU.

### 6a. Commissioning flow (D-033, D-035) [PLANNED]
A golden OS image holds the OS, the code, the services and an empty `site.yaml`. On first boot the commissioning step:
1. enumerates connected cameras (pypylon, PySpin) and shows their serials; the installer picks CAM1/CAM2, side, fps and exposure from the detected list (no typing of serials);
2. test-opens each camera with those settings and fails loudly if it cannot;
3. writes `site.yaml` (chmod 600, holds the secrets, D-048), runs `scripts/setup_dpc_layout.sh`, runs the renderer (legacy XMLs) and generates `workers.yaml` (one `frame_capture_<cam>` worker per camera);
4. enables mosquitto, MySQL and `dpc-supervisor` (systemd); the Supervisor starts the workers.

Start with a command-line wizard, a local web page later. Changing a setting = edit `site.yaml` (or the wizard), then restart that instance. Later a validated `command` over MQTT rewrites the file (versioned, previous copy kept) and restarts the instance; the file stays the truth.

## 7. Deployment
systemd runs only: mosquitto, MySQL, dpc-supervisor, Django. The Supervisor runs all workers. Migrate one worker at a time: disable its old unit first, never both.
On edge machines everything lives under one root, `~/INSIGHTZZ` (section 9), and all services run as the login user (no dedicated service user, D-030). `run/` is a normal folder, so pid files can survive a reboot: the Supervisor must clear stale ones at start. MySQL data stays where the OS puts it.
Releases (D-039): one `main` branch, tagged `v0.<n>`; every site runs a tag, so sites differ by config (`<root>/config`), never by code. Update = check out a newer tag, rollback = an older one.

## 8. Known limitations
Workers share the Supervisor's Linux user; stopping the Supervisor stops all workers; MQTT unauthenticated by default (set user/password/ACL in production); algorithm restarts are costly; GPU stack (CUDA/Detectron2) is the main install risk. Without a dedicated service user (D-030) a faulty worker can touch the login user's files: accepted trade-off on single-purpose machines.

## 9. Deployed folder layout (D-030, D-032)
Created by `scripts/setup_dpc_layout.sh`: run as the normal user, no sudo; `--root` changes the root; safe to re-run; it also checks that the frame folders share one filesystem. Default root `~/INSIGHTZZ`:
```
INSIGHTZZ/
  code/      git checkout of the platform
  config/    site.yaml (chmod 600, holds the secrets), rendered/ (generated legacy XMLs and workers.yaml)
  logs/      all logs
  run/       pid files, sockets
  data/
    frames/CAM1|CAM2/{live, _tmp, INF, RAW_IMAGES, UI}
    ui_live/CAM1|CAM2          reserved: live image for the Django UI (replaces TMP.jpg inside the UI code folder)
    images/{DEFECTIVE, REPEATED, RAW, PLATE}/{LEFT, RIGHT}
    wheel_status/{LEFT, RIGHT, IMAGES, PDFS}   (subfolders assumed: confirm against the real tree)
    bolt_data/  state/  models/
```
Why one root: everything visible in one place, one value (`paths.root`) in config, no dependence on a service account; the frame folders must share a filesystem anyway for atomic hand-off. If a second disk is added later, mount it at `<root>/data`.

| Legacy (Maihar, under `/home/<user>/INSIGHTZZ/`) | New (`<root>/`) |
|---|---|
| `CODE/ALGORITHM`, `PLC`, `MailModule`, `supervisor` | `code/` |
| `CODE/LOGS` | `logs/` |
| `CODE/FRAME_CAPTURE/CAMn/{INF,UI,RAW_IMAGES}` | `data/frames/CAMn/{INF,UI,RAW_IMAGES}` |
| `CODE/FRAME_CAPTURE/CAMn/IMG_1.jpg` | `data/frames/CAMn/live/IMG_1.jpg` |
| `RAW_IMG_DIR/{LEFT,RIGHT}` | `data/images/RAW/{LEFT,RIGHT}` |
| `REPEATED_IMG_DIR`, `DEFECTIVE_IMG_DIR` | `data/images/REPEATED`, `data/images/DEFECTIVE` |
| `TEMP_IMG_DIR/PLATE_DIR/{LEFT,RIGHT}` | `data/images/PLATE/{LEFT,RIGHT}` |
| `WHEEL_STATUS`, `bolt_data*`, `MODELS` | `data/wheel_status`, `data/bolt_data`, `data/models` |
| `DJANGO_UI/.../CAMn/TMP.jpg` (inside UI code) | `data/ui_live/CAMn/TMP.jpg` (planned) |
| config XMLs, passwords | `config/` (`site.yaml`, `rendered/`) |

The algorithms still hardcode legacy paths (D-023): they cannot use this layout until their paths come from config. New machines get the new layout first through the config-driven workers.

## 10. Repository and chat structure (D-036)
One monorepo, one folder per owner, and one Claude chat role per owner (MASTER, SUPERVISOR, W-<worker>, UI, CONFIG, SITE). Chats share nothing except the repo docs: CONTRACTS.md is the glue, OWNERS.md says who may change what, CHAT_WORKFLOW.md has the loop and the openers. Branching: one `main`, short-lived `feat/` and `hotfix/` branches, tagged releases, no long-lived site branches (D-039).
