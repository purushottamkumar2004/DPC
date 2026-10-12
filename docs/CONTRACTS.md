# Contracts (v3): things that must not drift

v3 changes: per-camera capture workers (1), richer heartbeat (2), Basler config (3), deployed layout and frame lifecycle (4), site.yaml schema (7), secrets location (9).

Status: **[BUILT]** implemented, **[EXISTING]** exists in production code (do not break), **[PROPOSED]** agreed shape, not implemented.
Secrets are never stored here.

## 1. Supervisor `workers.yaml` [BUILT]
```yaml
monitoring: {check_interval_seconds: 5, worker_timeout_seconds: 10, startup_stagger_seconds: 1, status_publish_interval_seconds: 30}
restart_policy: {enabled: true, max_retries: 3, retry_delay_seconds: 5, reset_after_seconds: 300}
mqtt: {enabled: true, host: localhost, port: 1883, username: null, password: null, topic_prefix: dpc}
runtime: {root: runtime}
log_dir: logs
workers:
  - id: <unique>  type: <text>  command: "<string, shlex-split, no shell>"
    enabled: true  working_directory: <path>  runtime_dir: <name>  directories: [..]
    stop_signal: SIGTERM|SIGINT  stop_timeout_seconds: 10  env: {K: v}
    restart: true|false|{enabled,max_retries,retry_delay_seconds,reset_after_seconds}
    health: {type: process|file_activity|heartbeat, path: <file|dir|[list]>, max_age_seconds: 30, startup_grace_seconds: 30}
```
Env passed to workers: `DPC_WORKER_ID`, `DPC_MQTT_HOST`, `DPC_MQTT_PORT`, `PYTHONUNBUFFERED=1`. Worker stdout/stderr: `<log_dir>/workers/<id>.log`.
**[PROPOSED additions]:** `mode: service|oneshot|periodic` (+ `every_seconds`), `restart_cost: normal|high`, `env_file: <path>`, health types `queue_lag {path, max_oldest_age_seconds}` and `db_freshness {table, where, column, max_age_seconds}`.
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
**[PROPOSED]** `poor_view` event (algorithm -> cleaning service): `{worker_id, event:"poor_view", classes_found, side}`; algorithm needs N consecutive bad frames; consumer owns the cooldown and is the only cleaning-register writer; consumers filter on `event`.
**[PROPOSED, append-only]** richer heartbeat for new or modified workers (D-034): `{"worker_id":..,"healthy":true,"pid":..,"version":..,"config_hash":..,"fps":..,"last_frame_age_s":..,"inf_queue_depth":..}`; existing consumers ignore unknown fields. One broker per site, so topics carry no site id. Config is never delivered over MQTT; workers boot from local files.

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
### BASLER_CONFIG1.xml (single-camera Basler FrameCapture) [EXISTING, v3 additions]
Root `BASLER_CAM`, read by position, one camera per file. Located through env `BASLER_CONFIG`, otherwise `BASLER_CONFIG1.xml` in the working directory.
```
0 CODE_PATH, 1 LOG_FILE_PATH (must end with "/": the code appends the file name), 2 LOG_BACKUP_COUNT, 3 CAM_SERIAL_NO,
4 CAM_NAME, 5 EXPOSURE (us), 6 FRAME_RATE, 7 SAVE_LIVE_IMG_PATH (the live/ folder), 8 UI_PATH, 9 INF_PATH, 10 RAW_IMG_PATH,
11 DB_USER, 12 DB_PASS, 13 DB_HOST, 14 DB_NAME, 15 TMP_IMG_PATH (optional; default = sibling "_tmp" folder of the live folder),
16 HEALTH_TABLE (optional), 17 LOG_FILE_NAME (optional), 18 ROTATE_DEG (optional: 0|90|180|270, default 90)   [PROPOSED, D-041]
```
Paths are absolute (XML does not expand `~`). Credentials still live here [EXISTING]; the target is `secrets.env` (D-018). The code also uses a site-specific health table name (see CODE_AUDIT section 6).
### MAIL_SERVICE_CONFIG.xml
`DATABASE_DETAILS`, `PROJECT_DETAILS` (PROJECT_NAME, REPORT dir), `MICROSOFT_AUTH` (tenant/client/secret/sender: **secrets**), `RECEIVER_DETAILS` (RECIPIENT, CC_RECIPIENT as Python list literals). SMTP variant adds `SENDER_DETAILS`.
### deleter_config.xml
Directory groups: raw, inference, repeated, plate, defective (each a list of paths). Settings: `raw_image_retention_hours` 24, `cleanup_interval_seconds` 10, `repeated_defects_threshold_days/items` 20/200, `plate_data_threshold_days` 10, `defective_defects_threshold_days/items` 10/200, `disk_usage_threshold_for_delete` 75, `..._raw_image_delete` 85, `..._for_email` 90, `..._inference_image_delete` 95.

## 4. Filesystem contract
### 4.1 Deployed layout [BUILT script, new machines] (D-030, D-032)
Root `<root>` = `~/INSIGHTZZ` (config key `paths.root`), created by `scripts/setup_dpc_layout.sh`:
```
code/   config/{site.yaml, secrets.env, rendered/}   logs/   run/
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

## 7. Site config [PROPOSED] (`<root>/config/site.yaml`, later DB rows) (D-033)
Single source of truth, validated against a schema when loaded (missing serial, fps out of range or duplicate camera id fail with a clear message before anything starts). `schema_version` allows migration. Layers: code defaults < `site.yaml` < env/CLI overrides. Secrets are never in this file.
```yaml
schema_version: 1
site: {id: <str>, name: <str>}
paths: {root: /home/<user>/INSIGHTZZ}            # code/ config/ logs/ run/ data/ derive from this
mqtt: {host: localhost, port: 1883}              # user/password from secrets.env
db: {host: localhost, name: <..>, health_table: health_status}   # user/password from secrets.env
cameras:
  - {id: CAM1, side: LEFT, driver: basler|flir, serial: "<..>", fps: <n>, exposure_us: <us>, rotate: 0|90|180|270,
     interface: "<flir only>", throughput_bps: <flir only>}
  - {id: CAM2, side: RIGHT, driver: ..., serial: "<..>", ...}
machine: {total_wheel_count: <n>, expected_cycle_total: <n>, wheel_stand_required_counts: {34: 3}, object_sizes: {LEFT: {wheel_w,wheel_h,bucket_clip_w,chain_w,half_bucket_clip_w}, RIGHT: {..}}}
model: {dir: <path>, detectron_yaml: <path>, weights: model_final.pth, class_json: <path>, threshold: 0.5, classes: [..]}
plc: {ip: <..>, port: 502, light_register: <..>, cleaning_register: <..>, conveyor_status_register: <..>, cleaning_pulse_seconds: 10}
mail: {sender: smtp|graph, project_name: <..>, recipients: [..], cc: [..]}
retention: {raw_hours: 24, thresholds: {delete: 75, raw: 85, mail: 90, inference: 95}, ...}
```
Renderer output = the legacy XML files above plus `workers.yaml` (one capture worker per camera), golden-file tested. `docs/SITES.md` records the values per site.

## 8. Compatibility rules
Topics, status fields, event names, XML tag order and DB columns are append-only; renaming/removing needs a DECISIONS entry plus a migration note. New optional config keys must not break existing files.

## 9. Secrets [PROPOSED] (D-018, D-030)
`<root>/config/secrets.env` (chmod 600, owner = the user running the services). Keys today: `DPC_DB_USER`, `DPC_DB_PASS`, `DPC_MS_TENANT_ID`, `DPC_MS_CLIENT_ID`, `DPC_MS_CLIENT_SECRET`, `DPC_MAIL_SENDER`, `DPC_DISK_REPORT_TO`, `DPC_SITE_NAME`; MQTT credentials when auth is enabled. Never committed (`secrets.env.example` is). Loaded by systemd `EnvironmentFile=` or by the Supervisor's `env_file` option (proposed in section 1). Use a dedicated DB user with limited rights instead of `root`.
