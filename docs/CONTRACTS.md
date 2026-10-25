# Contracts (v3): things that must not drift

v3 changes: per-camera capture workers (1), richer heartbeat (2), Basler config (3), deployed layout and frame lifecycle (4), site.yaml schema (7), secrets location (9).

Status: **[BUILT]** implemented, **[EXISTING]** exists in production code (do not break), **[PROPOSED]** agreed shape, not implemented.
Secrets are never stored here.

## 1. Supervisor `workers.yaml` [BUILT, V2 merged PR #7]
```yaml
monitoring: {check_interval_seconds: 5, worker_timeout_seconds: 10, startup_stagger_seconds: 1, status_publish_interval_seconds: 30,
             high_cost_timeout_seconds: 120, start_after_timeout_seconds: 120}
restart_policy: {enabled: true, max_retries: 3, retry_delay_seconds: 5, reset_after_seconds: 300}
mqtt: {enabled: true, host: localhost, port: 1883, username: null, password: null, topic_prefix: dpc}
runtime: {root: runtime}
log_dir: logs
workers:
  - id: <unique>  type: <text>  command: "<string, shlex-split, no shell>"
    enabled: true  working_directory: <path>  runtime_dir: <name>  directories: [..]
    stop_signal: SIGTERM|SIGINT  stop_timeout_seconds: 10  env: {K: v}
    restart: true|false|{enabled,max_retries,retry_delay_seconds,reset_after_seconds}
    health: {type: process|file_activity|heartbeat|queue_lag, path: <file|dir|[list]>, max_age_seconds: 30, startup_grace_seconds: 30,
             max_oldest_age_seconds: <s, required for queue_lag>, require_heartbeat: false}
    mode: service|oneshot|periodic      # default service (D-019)
    interval_s: <s>                     # periodic only: start to start, no overlap
    restart_cost: normal|high           # high = must fail high_cost_timeout_seconds before a restart (D-020)
    start_after: [<worker id>, ..]      # at Supervisor start only: waits until each is ready (service RUNNING; oneshot/periodic one run exit 0), at most start_after_timeout_seconds or until one is FAILED, then starts anyway (D-051)
    env_from_site: false                # true = DPC_* variables exported from site.yaml (section 9)
    follows: <worker id>                # service -> service only (D-068): also orders the start; paused when the followed worker goes RUNNING -> WAITING_RESTART|UNHEALTHY|FAILED|STOPPED, started once when it is RUNNING again; operator start/stop wins; never times out [BUILT P5]
    run_timeout_seconds: <s>            # oneshot/periodic only: a longer run is stopped and counts as a failed run [BUILT P5]
```
**P5 additions [BUILT, PR #13] (D-070):** `monitoring.reload_quiet_window: "HH:MM-HH:MM"` (optional, may cross midnight). The `mqtt` block holds `enabled`, `host`, `port`, `topic_prefix` only; a non-empty `username`/`password` there is a config error (`null` accepted): the login comes from `site.yaml` `mqtt.user`/`mqtt.password` (F11). Every worker that has `config/rendered/<id>.json` gets `DPC_WORKER_CONFIG` in its environment. The Supervisor renders at every start. CLI `supervisor.py reload [--confirm]` (also `systemctl reload dpc-supervisor`, SIGHUP): exit 0 ok, 1 `site.yaml` error (nothing changed), 3 Supervisor not reachable. Files in `<root>/run/`: `supervisor.pid`, `reload_request.json`, `reload_result.json` (chmod 600, no values). `check-config` also validates `site.yaml` when it exists (exit 2 on error). A `workers.yaml` change still needs a Supervisor restart.
**Location and start (D-052):** site file `<root>/config/workers.yaml`, run as `supervisor.py run -c <root>/config/workers.yaml` by `dpc-supervisor.service`. In `env` values only a leading `~/` (or exactly `~`) is expanded; nothing else is [BUILT].
**V2 semantics (built):** `oneshot` exit 0 = STOPPED, not restarted; exit != 0 follows the retry policy. `periodic` is STOPPED between runs (`next_run_at` in its status); a failed run is a `worker_failure` event and the next run is the retry, never FAILED. `queue_lag` = the oldest file in `path` (the INF folder) is younger than `max_oldest_age_seconds`; with `require_heartbeat: true` a fresh heartbeat (`max_age_seconds`) is also needed (D-049, Q1). A `start_after` naming a disabled worker is ignored; loops and unknown ids are config errors. Site config for the env bridge: `--site-config`, else `$DPC_SITE_CONFIG`, else the default (section 7); `python3 supervisor.py check-config` also validates it.
Env passed to workers: `DPC_WORKER_ID`, `DPC_MQTT_HOST`, `DPC_MQTT_PORT`, `PYTHONUNBUFFERED=1`. Worker stdout/stderr: `<log_dir>/workers/<id>.log`.
**[PROPOSED, not built]:** health type `db_freshness {table, where, column, max_age_seconds}`. (`env_file` was dropped: secrets come from `site.yaml`, D-048.)
**[PROPOSED]** one capture worker per camera: `id: frame_capture_cam1` / `frame_capture_cam2`, each started with its own config file (env `BASLER_CONFIG`, later `--camera CAM1` reading `site.yaml`). Until a heartbeat exists, `file_activity` on `data/frames/CAMn/live/IMG_1.jpg` is a valid health check: with the atomic hand-off its mtime changes on every frame.

## 2. MQTT [BUILT]
| Topic | Direction | Retained |
|---|---|---|
| `dpc/worker/<id>/status` | supervisor -> all | yes |
| `dpc/worker/<id>/event` | supervisor/worker -> all | no |
| `dpc/worker/<id>/health` | worker -> supervisor (heartbeat) | no |
| `dpc/worker/<id>/command` | anyone -> supervisor | no |
| `dpc/supervisor/status` | online/offline (last-will) | yes |

Statuses: STARTING, RUNNING, UNHEALTHY, WAITING_RESTART, STOPPED, FAILED. Events: `worker_started|stopped|failure|restarted|failed`. Commands: `start|stop|restart|status`. Heartbeat: `{"worker_id":..,"healthy":true}`. All JSON, ISO-8601 timestamps.
**[ACCEPTED, not built, D-073 D7 / F21]** a worker that exits on an error first writes `<root>/run/<worker_id>.last_error.json` {at, type, message, where (file:line), traceback (last lines)}, no secret values; the Supervisor adds it as `last_error` to `worker_failure` and `worker_restarted` and to the worker status.
**[BUILT P5, append-only, D-070]** topic `dpc/supervisor/event`: `config_reloaded` {changes, restarted, pending, not_running, not_supervised, removed}, `config_reload_failed` {error}, `config_render_failed` {error}. Worker status adds `follows`, `paused_by`, `config_pending`; `worker_started` may carry `reason`.
**[BUILT, V2, append-only, D-051]** status payload adds `mode`, `restart_cost`, `next_run_at`, `consecutive_failures`, `heartbeat` (only the known extended fields, type-checked, text cut to 64 chars). No new status value: a periodic worker between runs is STOPPED with `next_run_at` set (UI shows it as idle). Events add `worker_stopped` with `reason: "completed"` (oneshot/periodic exit 0) and `start_after_unmet` on `worker_started`. Heartbeat: the old and the extended payload are both accepted, unknown fields ignored; `{"healthy": false}` fails the check at once (the failure timeout still applies before a restart).
**[PROPOSED]** `poor_view` event (algorithm -> cleaning service): `{worker_id, event:"poor_view", classes_found, side}`; algorithm needs N consecutive bad frames; consumer owns the cooldown and is the only cleaning-register writer; consumers filter on `event`.
**[ACCEPTED, consumer built in V2]** richer heartbeat for new or modified workers (D-034): `{"worker_id":..,"healthy":true,"pid":..,"version":..,"config_hash":..,"fps":..,"last_frame_age_s":..,"inf_queue_depth":..}`; existing consumers ignore unknown fields. One broker per site, so topics carry no site id. Config is never delivered over MQTT; workers boot from local files.

## 3. Legacy program configs [EXISTING] (read **by position**, from `os.getcwd()`)
**Order of tags must never change.** The renderer must reproduce it exactly.
### leftConfig.xml / rightConfig.xml (root `DDPC`)
```
[0] SYSTEM_PATH:    0 CODE_PATH, 1 LOG_FILE_PATH, 2 LOG_BACKUP_COUNT, 3 DEFECTIVE_IMAGE_PATH, 4 INF_IMAGE_DIR_PATH,
                    5 RAW_IMAGE_DIR_PATH, 6 REPEATED_IMAGE_DIR_PATH, 7 FRAME_START_INFO_FILE_PATH, 8 INPUT_IMG_DIR,
                    9 TEMP_RAW_IMAGE_DIR_PATH, 10 DEFECT_SIDE (Left|Right), 11 TOTAL_WHEEL_COUNT
[1] OBJECT_DETAILS: 0 WHEEL_WIDTH, 1 WHEEL_HEIGHT, 2 BUCKET_CLIP_WIDTH, 3 CHAIN_WIDTH, 4 HALF_BUCKET_CLIP_WIDTH
[2] DATABASE_DETAILS: 0 DB_USER, 1 DB_PASS, 2 DB_HOST, 3 DB_NAME
[3] MODEL:          0 CONFIG_YAML_FL_PATH, 1 MODEL (weights file), 2 DETECTTHRESH, 3 MASK_MODEL_PATH, 4 JSON_PATH,
                    5 ALL_CLASS_NAMES (python list literal), 6 DETECT_LABEL_NAMES (python list literal)
[4] PLC_DETAILS:    0 PLC_IP_ADDRESS, 1 PLC_WRITE_DB_NUMBER, 2 PLC_READ_DB_NUMBER
```
Right differs from Left only in side/paths/object sizes/PLC text (values are site data).
### FRAME_CAPTURE_CONFIG.xml
`CODE_DETAILS` (CODE_PATH, LOG_FILE_PATH, LOG_BACKUP_COUNT, then per camera SAVE_LIVE_IMG_PATH, UI_PATH, INF_PATH, RAW_IMG_PATH), `DB_CONFIG`, `CAM_DETAILS` (CAMn_NAME FLIR|BASLER, CAMn_SERIAL_NO, CAMn_INTERFACE, CAM_n_EXPOSURE, CAM_n_FRAME_RATE, CAM_n_DEVICE_THROUGHPUT_VALUE). Indices are positional here too.
### BASLER_CONFIG1.xml (single-camera Basler FrameCapture) [RETIRED 2026-10-25 by P6, PR #15: the Basler capture reads its rendered JSON (section 13); kept here as history]
Root `BASLER_CAM`, read by position, one camera per file. Located through env `BASLER_CONFIG`, otherwise `BASLER_CONFIG1.xml` in the working directory.
```
0 CODE_PATH, 1 LOG_FILE_PATH (must end with "/": the code appends the file name), 2 LOG_BACKUP_COUNT, 3 CAM_SERIAL_NO,
4 CAM_NAME, 5 EXPOSURE (us), 6 FRAME_RATE, 7 SAVE_LIVE_IMG_PATH (the live/ folder), 8 UI_PATH, 9 INF_PATH, 10 RAW_IMG_PATH,
11 DB_USER, 12 DB_PASS, 13 DB_HOST, 14 DB_NAME, 15 TMP_IMG_PATH (optional; default = sibling "_tmp" folder of the live folder),
16 HEALTH_TABLE (optional), 17 LOG_FILE_NAME (optional), 18 ROTATE_DEG (optional: 0|90|180|270, default 90)   [PROPOSED, D-041]
```
Paths are absolute (XML does not expand `~`). Credentials still live here [EXISTING]; the target is `site.yaml` (D-048), with the renderer writing these XMLs. The code also uses a site-specific health table name (see CODE_AUDIT section 6).
### MAIL_SERVICE_CONFIG.xml
`DATABASE_DETAILS`, `PROJECT_DETAILS` (PROJECT_NAME, REPORT dir), `MICROSOFT_AUTH` (tenant/client/secret/sender: **secrets**), `RECEIVER_DETAILS` (RECIPIENT, CC_RECIPIENT as Python list literals). SMTP variant adds `SENDER_DETAILS`.
### deleter_config.xml
Directory groups: raw, inference, repeated, plate, defective (each a list of paths). Settings: `raw_image_retention_hours` 24, `cleanup_interval_seconds` 10, `repeated_defects_threshold_days/items` 20/200, `plate_data_threshold_days` 10, `defective_defects_threshold_days/items` 10/200, `disk_usage_threshold_for_delete` 75, `..._raw_image_delete` 85, `..._for_email` 90, `..._inference_image_delete` 95.

## 4. Filesystem contract
### 4.1 Deployed layout [BUILT script, new machines] (D-030, D-032)
Root `<root>` = `~/INSIGHTZZ` (config key `paths.root`), created by `scripts/setup_dpc_layout.sh`:
```
code/   config/{site.yaml, rendered/}   logs/   run/
data/frames/CAM1|CAM2/{live, _tmp, INF, RAW_IMAGES, UI}      data/ui_live/CAM1|CAM2 (reserved)
data/images/{DEFECTIVE, REPEATED, RAW, PLATE}/{LEFT, RIGHT}
data/wheel_status/{LEFT, RIGHT, IMAGES, PDFS} (assumed)      data/bolt_data   data/state   data/models
```
**Frame lifecycle rules (D-031)** [BUILT in `frame_capture_basler`]:
- File name `YYYY_MM_DD_HH_MM_SS_ffffff.jpg` (microseconds, sorts in time order). Do not change.
- Publish order per frame: write `_tmp/<ts>.jpg`, link into `INF/`, link into `UI/IMG_1.jpg` only if `UI/` is empty, then `os.replace` into `live/IMG_1.jpg`.
- Only FrameCapture writes `_tmp` and `live` and creates files in `INF` and `UI`.
- The algorithm consumes the **oldest** `INF` file and moves it (a rename on the same filesystem) to `RAW_IMAGES`; defect images are saved before the DB row.
- `UI/` is a mailbox: its consumer deletes the file after reading; FrameCapture refills it only when empty.
- Readers open these files read-only and never modify them in place (hard links share data). Rename or delete is safe.
- `_tmp`, `INF`, `UI`, `live` (and `ui_live`) must be on one filesystem; the layout script checks this.
- CAM1 -> LEFT, CAM2 -> RIGHT.

### 4.2 Legacy layout [EXISTING, Maihar] (do not break until each program is config-driven, D-023)
Root `<site_root>` (e.g. `/home/<user>/INSIGHTZZ`):
```
CODE/FRAME_CAPTURE/CAM1|CAM2/{IMG_1.jpg (live, rewritten each frame), UI/, INF/ (work queue), RAW_IMAGES/}
CODE/{ALGORITHM, MailModule, PLC, DataSync, UP_TIME_REPORT, WHEEL_PDF_GENERATOR, ShellScript, LOGS}
RAW_IMG_DIR/{LEFT,RIGHT}   REPEATED_IMG_DIR/{LEFT,RIGHT}   DEFECTIVE_IMG_DIR/{LEFT,RIGHT}/<date>/
TEMP_IMG_DIR/PLATE_DIR/{LEFT,RIGHT}   WHEEL_STATUS/{LEFT,RIGHT,IMAGES,PDFs}   MODELS/<model>/   bolt_data, bolt_data_left
```
Rules: INF file name `YYYY_MM_DD_HH_MM_SS_ffffff.jpg`; algorithm consumes the **oldest** INF file and moves it to RAW_IMAGES; defect images are saved before the DB row. CAM1 -> LEFT, CAM2 -> RIGHT.
**[PROPOSED]** every path derived from one `site_root`.

## 5. Database [EXISTING; seed reconciled with the live Tadipatri DB 2026-10-12] (D-043, D-045)
The executable source of truth is `db/schema.sql` + `db/fixed_rows.sql` + `db/site_rows.example.sql` + `db/migrations/` (D-043). This section says what each table and column means and who writes and reads it. Both change in the same commit; `db/test_seed.py` checks that every seeded table and column is named here. One DB per site (name in config); new sites get exactly the tables below, nothing else. Tables found on live sites but **not** seeded, and per-site differences, are listed in `db/README.md` ("Left out", "Site notes").

Rules for all tables: column names, types and defaults are append-only (section 8). Collation `utf8mb4_unicode_ci`. Table names are case-sensitive on Linux (`HEALTH_STATUS` is not `health_status`); column names are not.

- `schema_version`: `version` int PK, `description` varchar(255) NOT NULL, `applied_at` datetime default CURRENT_TIMESTAMP. Written by `schema.sql` (version 1) and by each migration; read by people and later `scripts/dpc_db_init.py`.
- `defects_table`: one row per confirmed defect.
  `ID` int PK auto_increment; `defect_side` varchar(45) NOT NULL (Left|Right); `defect_type` varchar(45) NOT NULL; `view_defect` varchar(545) NOT NULL (defect image path);
  `wheel_no` varchar(45) NOT NULL (**text, not int**; code compares it with integers, MySQL converts, D-028); `xmin,xmax,ymin,ymax` int NULL;
  `create_datetime` datetime default CURRENT_TIMESTAMP; `update_datetime` datetime NULL, on update CURRENT_TIMESTAMP;
  `is_alert_sent` int NOT NULL default 0 (0 pending, 1 sent, 2 ignored);
  `is_data_sync`, `is_image_sync` tinyint NOT NULL default 0 (DataSync; not in the repo);
  `defect_checkbox` varchar(255) default 'Select To Park Defect'; `reviewed_by` varchar(100) NULL, `reviewed_at` datetime NULL, `review_status` varchar(20) NOT NULL default 'PENDING' (review workflow, Django UI; Maihar has these columns, Tadipatri does not);
  `no_of_days` int default 0.
  Indexes: `is_alert_sent`; (`defect_side`, `defect_type`, `create_datetime`); `create_datetime`.
  Writer: LeftAlgorithm / RightAlgorithm `INSERT` of `defect_side, defect_type, view_defect, wheel_no, xmin..ymax` only; everything else is a default. Mail senders `UPDATE is_alert_sent`. DataSync and the UI update their own columns.
  Readers: the algorithms (24 h dedupe), both mail senders (`SELECT * ... is_alert_sent = 0` with a DictCursor, so rows are read **by column name**: the key is `ID`, not `id`), DataSync, the UI.
- `repeated_defects_table` (plural, as the code queries it): `ID` int PK auto; `defect_table_id` int NOT NULL (`defects_table.ID`, indexed); `defect_name` varchar(255) NOT NULL; `image_path` varchar(500) NULL; `xmin,xmax,ymin,ymax` int NOT NULL; `wheel_no` int NULL; `expiry_datetime` datetime NULL; `create_datetime` datetime NULL; `no_of_days` int default 0. Writer: the UI (assumed; not in the repo). Reader: the algorithms (`select *`; the lookup is currently disabled in code). Tadipatri has the singular `repeated_defect_table` instead.
- `wheel_counter_table`: `ID` int PK auto; `WHEEL_COUNT` varchar(45) NULL. **One row, `ID = 1`, holds the live wheel number as text.** Row created by `fixed_rows.sql`. Writer: only `RightAlgorithm.update_wheel_count()` (`UPDATE ... WHERE ID = 1`); `LeftAlgorithm` does not write it (D-029). Reader: the UI.
- `health_status`: one row per camera. `ID` int PK auto; `ITEM` varchar(64) NOT NULL, unique (camera serial); `HEALTH` varchar(45) NULL (Basler `OK`|`NOK`, FLIR its own text); `IS_DATA_SYNC` tinyint NOT NULL default 0; `CREATE_DATETIME` datetime default CURRENT_TIMESTAMP; `UPDATE_DATETIME` datetime default CURRENT_TIMESTAMP on update CURRENT_TIMESTAMP.
  Writers only `UPDATE` an existing row, so each camera's row comes from the site's filled `site_rows` file: FLIR capture `UPDATE health_status SET health, update_datetime WHERE item`; Basler capture `UPDATE <HEALTH_TABLE> SET HEALTH, IS_DATA_SYNC, CREATE_DATETIME WHERE ITEM` (table from Basler XML element 16, D-041). Readers: DataSync, the UI.
  **New sites set `HEALTH_TABLE` = `health_status`.** Live sites keep their table: Maihar `health_status` (`item`, `health`, `update_datetime`), Tadipatri **`tadipatri4_health_status_table`** (same columns as above, no unique key). The Basler code default `tadipatri_health_status_table` (D-041) exists on no site, so element 16 must always be set.
- `CAMERA_PLC_STATUS`: **one row, `id = 1`** (created by `fixed_rows.sql`), status lamps for the UI, text 'True'/'False'. `id` int PK; `` `RIGHT CAMERA` ``, `` `LEFT CAMERA` ``, `PLC` varchar(45) default 'False'; `UPDATE_DATETIME` datetime, on update CURRENT_TIMESTAMP.
  Writer: the FLIR capture, CAM1 into `RIGHT CAMERA` and CAM2 into `LEFT CAMERA`, kept as is (D-044). `PLC` has no writer in the repo yet (future PLC service, D-024); it is seeded because the live table and its UI have it. Reader: the UI. The Basler capture does not write this table.

Dedupe rule: same `wheel_no` + `defect_type` + `defect_side` within 24 h with bbox width/height within +-50 px. In practice the size check compares the new defect with itself, so only wheel + type + side + 24 h decide; kept as is (D-044).
Affected rows: MySQL reports **changed** rows, so an `UPDATE` that writes identical values returns 0 even though the row exists.

## 6. PLC [EXISTING]
Modbus TCP (pymodbus), port 502, timeout 3 s. Registers: light **4109** (script writes 1; comment says 0 on/1 off: confirm), cleaning **4106** (1 for 10 s then 0), legacy cleaning 4097 / 4197, read conveyor status **4098**. **PLC IP for this server (Maihar): `192.168.2.5`** (confirmed by you; matches `light.py`). `cleaning_mechanism.py` (192.168.1.4) and the algorithm XML `PLC_IP_ADDRESS` (192.168.1.5) disagree with it and are treated as stale until proven otherwise. The XML DB numbers 12/13 are not used by the Modbus scripts. **One PLC IP per site must be a single config value** (`plc.ip` in site.yaml).

## 7. Site config v2 [ACCEPTED; loader built for v1, v2 next] (`<root>/config/site.yaml`) (D-033, D-047, D-048)
One file per site; the only file a person edits to set up a site. File first; the DB becomes the master copy only when the UI starts editing config (later decision). Since schema v2 the file also holds the site's secrets (D-048): it must be chmod 600 and owned by the user running the services; share it only as a `--redacted` copy. Every program gets its settings from this file, directly (new code) or through the renderer (legacy XMLs, section 3).

**Location:** env `DPC_SITE_CONFIG`, else `~/INSIGHTZZ/config/site.yaml`.
**Loader** (CONFIG, `common/config/site_config.py`): `load_site_config(path=None) -> SiteConfig`. It reads the file, applies the defaults below, validates everything and raises `SiteConfigError` that lists **every** problem with its key path (e.g. `cameras[1].serial: missing`). `python -m common.config.site_config [path]` prints `OK` or the problems; exit 0 or 1. `--redacted` prints the whole file with every secret replaced by `***` (safe to share). The loader refuses a file that is not chmod 600 and never prints or logs a secret value.
**Rules:** unknown keys are errors (catches typos); no per-key env overrides in v1; relative paths are relative to `paths.root`; `~` is expanded; numbers are checked against the ranges below; serials are strings (quote them).

```yaml
schema_version: 2                 # required, must be 2 (v1 files: add the secret keys below and bump)

site:
  id: tadipatri                   # required; lowercase letters, digits, - or _; used in mail subjects and MQTT
  name: "Tadipatri DPC"           # required; free text

paths:
  root: ~/INSIGHTZZ               # required; code/ config/ logs/ run/ data/ derive from it (section 4.1)

logging:
  backup_count: 5                 # default 5; days of rotated logs kept (LOG_BACKUP_COUNT)

db:
  host: localhost                 # default localhost
  port: 3306                      # default 3306
  name: tadipatri4                # required
  health_table: health_status     # default health_status (D-043); existing sites set their own table
  user: dpc_app                   # required; root allowed with a warning (D-050), dedicated user recommended
  password: "<secret>"           # required; secret

mqtt:
  host: localhost                 # default localhost
  port: 1883                      # default 1883
  user: ""                        # optional; give both user and password, or neither
  password: ""                    # optional; secret

cameras:                          # required; 1 or 2 entries
  - id: CAM1                      # required; CAM1 | CAM2; unique
    side: LEFT                    # required; LEFT | RIGHT; unique; CAM1 = LEFT, CAM2 = RIGHT (section 4.1)
    driver: basler                # required; basler | flir
    serial: "24824427"            # required; string; unique
    fps: 4.0                      # required; > 0 and <= 30
    exposure_us: 1500             # required; integer 10 .. 1000000
    rotate: 0                     # required; 0 | 90 | 180 | 270 (no default: the model depends on orientation)
    flir:                         # required when driver = flir, not allowed otherwise
      interface: "GEV Interface 1"
      throughput_bps: 19662239

machine:
  total_wheel_count: 190          # required; integer > 0 (TOTAL_WHEEL_COUNT)
  expected_cycle_total: 188       # optional; read once W-inspection moves it out of code (D-023)
  wheel_stand_required_counts: {34: 3}   # optional, default {}; wheel number -> required wheel_stand count (same, D-023)
  object_sizes:                   # required; one entry per configured side; pixels, integers > 0
    LEFT:  {wheel_w: 530, wheel_h: 495, bucket_clip_w: 300, chain_w: 880, half_bucket_clip_w: 150}
    RIGHT: {wheel_w: 530, wheel_h: 495, bucket_clip_w: 300, chain_w: 880, half_bucket_clip_w: 150}

model:
  dir: data/models/<model_name>   # required; folder with weights and class json (MASK_MODEL_PATH)
  weights: model_final.pth        # default model_final.pth; file inside dir (MODEL)
  class_json: <file>.json         # required; file inside dir (JSON_PATH)
  detectron_yaml: <path>          # required; Detectron2 config yaml (CONFIG_YAML_FL_PATH)
  threshold: 0.5                  # required; > 0 and < 1 (DETECTTHRESH)
  classes: [wheel, half_wheel, plate, bucket_clip, nut_bolt]   # required; non-empty, unique (ALL_CLASS_NAMES)
  detect_labels: [...]            # default = classes; each must be in classes (DETECT_LABEL_NAMES)

plc:
  type: modbus_tcp                # default modbus_tcp (only value in v1)
  ip: 192.168.2.5                 # required; IPv4; ONE PLC address per site (section 6)
  port: 502                       # default 502
  timeout_s: 3                    # default 3
  light_register: 4109            # required
  cleaning_register: 4106         # required
  conveyor_status_register: 4098  # required
  cleaning_pulse_seconds: 10      # default 10
  legacy_db_numbers: {write: 12, read: 13}   # optional; only written into the legacy algorithm XML, not used by Modbus

mail:
  sender: graph                   # required; graph | smtp
  project_name: "Tadipatri_dpc"   # required; used in subjects (PROJECT_NAME)
  report_dir: data/reports        # default data/reports (REPORT)
  recipients: ["a@example.com"]   # required; at least one address
  cc: []                          # default []
  sender_address: "dpc@example.com"     # required; the mailbox mails are sent from
  disk_report_to: ["a@example.com"]     # default = recipients; daily disk report
  graph:                          # required when sender = graph, not allowed otherwise
    tenant_id: "<secret>"
    client_id: "<secret>"
    client_secret: "<secret>"
  smtp:                           # required when sender = smtp, not allowed otherwise
    host: smtp.gmail.com          # default = today's hardcoded value
    port: 587                     # default 587
    password: "<secret>"          # required

retention:                        # all optional; defaults = today's deleter_config values
  raw_image_hours: 24
  cleanup_interval_seconds: 10
  repeated: {days: 20, items: 200}
  plate_days: 10
  defective: {days: 10, items: 200}
  disk_percent: {delete: 75, raw_delete: 85, mail: 90, inference_delete: 95}   # each 1..99, strictly increasing in this order
```

**Cross-field checks:** camera `id`, `side` and `serial` unique; `object_sizes` has exactly the sides of the configured cameras; `flir` block present if and only if `driver: flir`; `smtp` block only with `sender: smtp`; `detect_labels` subset of `classes`; `disk_percent` increasing.

**Derived values (computed, never written in the file):** frame folders `<root>/data/frames/<CAMn>/{live,_tmp,INF,RAW_IMAGES,UI}`, images `<root>/data/images/{DEFECTIVE,REPEATED,RAW,PLATE}/<SIDE>`, logs `<root>/logs/`, per-camera log name `FrameCapture_<CAMn>.log`, legacy `DEFECT_SIDE` = `Left|Right` from `side`.

**Renderer contract (later, D-022):** writes `<root>/config/rendered/` from this file (rendered files that carry secrets are chmod 600), keeping the XML tag order of section 3 exactly:
| Rendered file | Filled from |
|---|---|
| `BASLER_CONFIG<n>.xml` (one per basler camera) | `paths` (derived), `logging.backup_count`, `cameras[]` (serial, exposure_us, fps, rotate), `db` + DB secrets, `db.health_table` |
| `FRAME_CAPTURE_CONFIG.xml` (flir) | the same plus `cameras[].flir` |
| `leftConfig.xml` / `rightConfig.xml` | derived paths for that side, `machine.total_wheel_count`, `machine.object_sizes.<SIDE>`, `db`, `model.*`, `plc.ip`, `plc.legacy_db_numbers` |
| `MAIL_SERVICE_CONFIG.xml` | `db`, `mail.*`, mail secrets |
| `deleter_config.xml` | derived image folders, `retention.*` |
| `workers.yaml` | one `frame_capture_<camn>` worker per camera plus the other workers (D-033) |

## 7.1 Site config v3 [BUILT, PR #10; the loader accepts only schema 3] (D-055; replaces the v2 layout when CONFIG builds loader v3)
Same rules as v2 (chmod 600, `--redacted`, unknown keys are errors, every problem listed with its key). Grouped by program:
```yaml
schema_version: 3
site:   {id: tadipatri, name: "Tadipatri DPC"}
paths:  {root: ~/INSIGHTZZ}
db:     {host: localhost, port: 3306, name: tadipatri4, user: root, password: "<secret>", health_table: health_status}
mqtt:   {host: localhost, port: 1883, user: "", password: ""}
graph:  {tenant_id: "<secret>", client_id: "<secret>", client_secret: "<secret>"}   # shared by mail and transfer

capture:
  log: {backup_count: 5}
  cameras:
    - {id: CAM1, side: LEFT,  driver: basler, serial: "24824427", fps: 4.0, exposure_us: 1500, rotate: 0}
    - {id: CAM2, side: RIGHT, driver: basler, serial: "24874775", fps: 4.0, exposure_us: 2000, rotate: 0}

inspection:
  log: {backup_count: 5}
  model: {name: dpc_maskrcnn}                          # which model only; the version is managed by the Supervisor (D-060)
  threshold: 0.5
  machine: {total_wheel_count: 190, expected_cycle_total: 188, wheel_stand_required_counts: {34: 3}}
  sides:
    LEFT:  {object_sizes: {wheel_w: 530, wheel_h: 495, bucket_clip_w: 300, chain_w: 880, half_bucket_clip_w: 150}}
    RIGHT: {object_sizes: {wheel_w: 530, wheel_h: 495, bucket_clip_w: 300, chain_w: 880, half_bucket_clip_w: 150}}
  auto_roi: {mode: observe, learn_images: 30}          # off | observe | filter (D-054)

plc:      {type: modbus_tcp, ip: 192.168.2.5, port: 502, light_register: 4109, cleaning_register: 4106, conveyor_status_register: 4098, cleaning_pulse_seconds: 10, log: {backup_count: 5}}
mail:     {sender: graph, sender_address: "dpc@example.com", project_name: "Tadipatri_dpc", recipients: ["a@example.com"], cc: [], disk_report_to: [], smtp: null, log: {backup_count: 5}}
cleanup:  {raw_image_hours: 24, interval_seconds: 10, repeated: {days: 20, items: 200}, plate_days: 10, defective: {days: 10, items: 200}, disk_percent: {delete: 75, raw_delete: 85, mail: 90, inference_delete: 95}, log: {backup_count: 5}}
transfer: {upload: {weekday: sunday, chunk_mb: 500, remote_folder: "DPC/data/<site.id>"}, models_folder: "DPC/models", log: {backup_count: 5}}
```
Values above are examples. `mail.smtp` is a block (host, port, password) only when `sender: smtp`. Which keys each program reads is fixed when that program is migrated; new keys are append-only (section 8).
**Built details (PR #10):** optional keys kept from v2: `plc.timeout_s`, `plc.legacy_db_numbers`, `mail.report_dir` (defaults as v2). `model.detect_labels` is dropped (append a key if inspection needs it). `graph` is required when `mail.sender: graph` or a `transfer` section exists. `transfer` is optional (absent = no transfer service); `transfer.upload` optional (absent = download only); defaults `weekday: sunday`, `chunk_mb: 500` (upload batch size, not the Graph request size), `remote_folder: "DPC/data/<site.id>"`. (D-064, built PR #12: optional `cameras[].ip` (IPv4, unique) and `plc.light_on_value` (0|1, default 1). D-060, built PR #11: `inspection.model` = `{name}` only; `inspection.model.version` and a `models` section are errors with a message pointing to the Supervisor; `transfer.models_folder` default `"DPC/models"`.) `mail.disk_report_to` empty or absent = `recipients`. `auto_roi` defaults: `mode: observe`, `learn_images: 30`. A v2 file is refused with one line per moved key; `scripts/convert_site_yaml_v3.py` converts it.

## 11. Learned values [ACCEPTED, not built] (D-054)
`<root>/data/state/<program>/<topic>.json`, one writer each, atomic write, JSON with `schema_version`, `learned_from` {camera serial, rotate, model name, model version, images, time} and the values. Ignored and relearned when `learned_from` no longer matches; `site.yaml` fixed values win; delete to reset; unreadable = relearn, never crash.

## 12. Model packages and switch [reader BUILT PR #10; switch and download not built] (D-056, D-057)
`<root>/data/models/<name>/<version>/` = weights, Detectron yaml, `classes.json` (class list + label mapping), `manifest.json` {name, version, trained_at, files: {file: sha256}}; `<root>/data/models/<name>/current` points to the running version. OneDrive source `DPC/models/<name>/<version>/`, `manifest.json` uploaded last. Transfer service: download, verify, mark ready (`ready` file in the version folder); never switches. Supervisor: switch = test on saved images, move `current`, restart inspection once, keep previous for rollback; publishes `model_switched` / `model_switch_failed` events.
**Formats (built, PR #10):** `classes.json` = `{"classes": [...], "label_map": {...}}`. `manifest.json` lists `classes.json`, exactly one `.pth` (weights) and one `.yaml`/`.yml` (Detectron config), each with its sha256; extra keys allowed; a `classes` key in the manifest, if present, must equal `classes.json`. `current` is a relative symlink to the version folder (a plain file holding the version name is also read); `set_current()` switches it atomically. API in `common/config/model_package.py`: `load_model_package(cfg)`, `verify_package()`, `set_current()`. Which version runs (D-060): `current`, written only by the Supervisor; a `pinned` marker in `data/models/<name>/` stops automatic switching.
**Supervisor model management (D-060, not built):** `workers.yaml` section `models: {auto_update: off|download|install, check_interval_hours: 6}`; commands `model status | switch <version> | rollback | pin <version> | unpin`; state files `data/models/<name>/current` and `pinned`, Supervisor is the only writer; events `model_switched`, `model_switch_failed`, `model_rolled_back`, `model_pinned`, `model_unpinned`.
**Built (PR #11):** `pinned` is a file in `data/models/<name>/` holding the pinned version name (`read_pinned()`); the Supervisor writes it in that format. `running_version()` returns the `current` version or none. Manifest may carry `packaged_at` and `packaged_from` (extra keys allowed). `scripts/make_model_package.py` packages an existing model, verifies it and writes `ready`; it sets `current` **only on first install** (no `current` and no `pinned` yet), afterwards it prints "switch with the Supervisor (model switch <v>)" (D-061).


## 8. Compatibility rules
Topics, status fields, event names, XML tag order and DB columns are append-only; renaming/removing needs a DECISIONS entry plus a migration note. New optional config keys must not break existing files.

## 9. Secrets [ACCEPTED] (D-048; supersedes the `secrets.env` part of D-018, D-030, D-047)
Secrets live in `site.yaml` (section 7: `db.user`, `db.password`, `mqtt.user`, `mqtt.password`, `mail.graph.*`, `mail.smtp.password`; addresses `mail.sender_address`, `mail.disk_report_to`). `secrets.env` is retired. Never committed: the repo holds only `config/examples/site.example.yaml` with placeholders. A `dpc-sites` mirror (Q17), chats and the UI get `--redacted` copies only.
**Legacy env bridge (Supervisor):** programs that still read env vars get them from the Supervisor, which loads `site.yaml` and exports, per worker that asks for it (`env_from_site: true` in `workers.yaml`): `DPC_DB_USER`, `DPC_DB_PASS`, `DPC_MS_TENANT_ID`, `DPC_MS_CLIENT_ID`, `DPC_MS_CLIENT_SECRET`, `DPC_MAIL_SENDER`, `DPC_SMTP_PASSWORD`, `DPC_DISK_REPORT_TO` (comma-separated), `DPC_MQTT_USER`, `DPC_MQTT_PASS`, `DPC_SITE_NAME` (= `site.name`). Today only `workers/cleanup/delete_script.py` needs it. A dedicated DB user with limited rights is recommended; `root` is allowed (D-050).

## 10. Commissioning check `dpc-check` [BUILT, PR #9] (D-035)
**Command:** `python3 <root>/code/scripts/dpc_check.py [site.yaml]` (default site.yaml as in section 7). Read-only: never writes, never opens a camera (enumeration only).
**Output:** one line per check, `<PASS|WARN|FAIL|SKIP> <name> <reason>`, in this order: `site.yaml`, `git`, `layout`, `disk`, `cameras`, `database`, `mqtt`, `cameras` also pings each camera that has an `ip` (no answer = WARN, never FAIL; 1 s timeout, PR #12); `model` (PR #11: shows the running version and pinned / not pinned; no `current` = WARN; pinned differs from `current` = WARN; dangling `current`, bad package or bad `pinned` marker = FAIL); then `RESULT ... (exit N) in T s`. No secret value is ever printed.
**Exit:** 0 = all pass; 1 = any FAIL or SKIP; 2 = warnings only.
**Grading:** `db.user` root = WARN (D-050); MQTT broker unreachable = WARN (D-034); disk used/(used+free) below `retention.disk_percent.delete` = PASS, between `delete` and `mail` = WARN, at or above `mail` = FAIL (same measure as cleanup). The layout check does not check `data/wheel_status` yet (Q13).

## 13. Rendered worker config [renderer BUILT, PR #12; the Basler captures read it since P6, PR #15] (D-062)
`<root>/config/rendered/<worker_id>.json`, chmod 600, written atomically by the renderer from `site.yaml`; read by the worker from env `DPC_WORKER_CONFIG` (set by the Supervisor). Common shape:
```json
{"_generated": {"from": "site.yaml", "sha256": "<hex>", "at": "2026-10-22T10:00:00", "renderer": 1},
 "worker_id": "frame_capture_cam1",
 "paths": {"root": "/home/<user>/INSIGHTZZ", "logs": "...", "...": "..."},
 "log": {"backup_count": 5},
 "...worker keys...": {}}
```
Per-worker keys **as built (PR #12)**:
- `frame_capture_<camn>`: `worker_id`, `paths` {root, logs, live, tmp, inf, ui, raw_images}, `log` {backup_count, file_name}, `camera` {id, side, driver, serial, fps, exposure_us, rotate, ip[, flir]}, `db` {host, port, name, user, password, health_table}, optional top-level `health_interval_s` (default 600; not rendered yet, P4d). Read by the Basler capture since P6 (PR #15); `BASLER_CONFIGn.xml` retired.
- `inspection_<side>`: `worker_id`, `side`, `defect_side` (`Left`/`Right`), `camera_id`, `paths` {root, logs, inf, raw, defective, repeated, plate, temp, start_file}, `log` {backup_count}, `model` {name, models_dir} (version from `current`, D-060), `threshold`, `machine` {total_wheel_count[, expected_cycle_total][, wheel_stand_required_counts (string keys)]}, `object_sizes`, `auto_roi` {mode, learn_images}, `db` {...}, `plc` {ip[, legacy_db_numbers]}. Folders per D-067.
- No PLC worker is rendered yet, so `plc.light_on_value` is in no file until the PLC service exists.
**Renderer semantics (built):** "changed" means the settings changed (the `_generated` header is ignored for the comparison); `_generated.sha256` is the `site.yaml` the settings were last written from; only files carrying a `_generated.renderer` key are ever removed; `--check` is a dry run (exit 0). API: `common.config.render.render()`, called by the Supervisor at every start and on reload [BUILT, PR #13].
Renderer CLI: `python3 scripts/render_config.py [site.yaml] [--check]`: prints one line per file `written|unchanged|removed <worker_id>`; exit 0 ok, 1 `site.yaml` invalid (nothing written).

