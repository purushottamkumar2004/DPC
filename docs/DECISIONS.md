# Decisions log (v3)

Format: ID | date | status | decision | why | consequence. Newest at bottom. Never edit history; supersede with a new entry.
Statuses: Accepted, Proposed, Open, Superseded.

## D-001 | 2026-10 | Accepted | Generic Supervisor, not a Camera Manager
Manages any worker via config: id, type, command, working dir, health, restart policy. No domain logic inside.

## D-002 | 2026-10 | Accepted | Existing scripts run unmodified as external processes
V1 launches them with `subprocess.Popen`. Code changes happen gradually, behind adapters.

## D-003 | 2026-10 | Accepted | MQTT for events/status/heartbeat/commands only
Local calls stay Python calls. Broker down => Supervisor keeps working, only status/commands are lost.

## D-004 | 2026-10 | Accepted | Tolerant, time-based failure detection
Failing health must persist `worker_timeout_seconds`; dead process restarts at once; startup grace per worker.

## D-005 | 2026-10 | Accepted | Bounded retries then FAILED; retries earned back after sustained health

## D-006 | 2026-10 | Accepted | Restart waits are non-blocking (WAITING_RESTART), one broken worker never delays others

## D-007 | 2026-10 | Accepted | Own process group per worker; configurable stop signal, then SIGKILL
Check per FrameCapture version whether it releases cameras on SIGTERM or only SIGINT.

## D-008 | 2026-10 | Accepted | Health checks need no change in old scripts (`file_activity`), `heartbeat` for new/modified ones

## D-009 | 2026-10 | Accepted | Commands via MQTT (needs broker)

## D-010 | 2026-10 | Accepted | Supervisor replaces per-worker systemd units, migrated one at a time (never both)

## D-011 | 2026-10 | Accepted | Worker-specific reactions go through events
Example: algorithm publishes `poor_view` after N consecutive bad frames; the cleaning service acts with a cooldown and is the only cleaning-register writer.

## D-012 | 2026-10 | Superseded by D-021 | (was) Merge Left/Right because they are identical
Code review showed they are not identical.

## D-013 | 2026-10 | Proposed | One `site_root`; all paths derived from it

## D-014 | 2026-10 | Proposed | Configuration in DB, edited via Django; Supervisor reads it through ConfigurationManager and gets `reload`

## D-015 | 2026-10 | Proposed | Camera drivers behind one interface (FLIR, Basler, ...)
The deployed FrameCapture already switches FLIR/Basler by config; the quad FLIR rewrite is a second implementation to unify later.

## D-016 | 2026-10 | Proposed | Setup wizard with mandatory self-test (frames, inference, DB insert, PLC read/write, test mail)

## D-017 | 2026-10 | Proposed | Monitoring UI built on MQTT status/events; actions via authenticated API -> MQTT command

## D-018 | 2026-10 | Accepted | No secrets in repo, XML, code or Project knowledge
Found in the real code: MS Graph client secret hardcoded in `delete_script.py` and in the mail XML, DB passwords in all XML. Action: rotate, move to `/etc/dpc/secrets.env` (chmod 600), reference by env var.

## D-019 | 2026-10 | Proposed | Worker `mode`: service | oneshot | periodic
`light.py` and `cleaning_mechanism.py` exit by design. Supervisor must not treat a clean exit of a oneshot as failure. Needed before migrating PLC scripts.

## D-020 | 2026-10 | Proposed | Algorithm health = queue lag; algorithm restart treated as expensive
Health type `queue_lag` (age of oldest file in INF). Algorithms get long grace/timeout, few retries (`restart_cost: high`). Reason: restart deletes INF backlog, resets wheel/plate state, and skips frames until the next plate.

## D-021 | 2026-10 | Proposed | Left/Right merge only after diff review + replay tests
Right is 716 lines longer and has different functions and defaults. Plan: (1) replay harness with saved image sequences and expected defects, (2) diff-driven merge into one codebase with `side` parameter, (3) switch one site, compare defect output on live data in shadow mode.

## D-022 | 2026-10 | Proposed | Config renderer: central site config generates the legacy XMLs
Algorithms/capture read XML by position from cwd. Renderer writes them with exact tag order (golden-file tests), so the UI/wizard can set everything without touching the algorithms. Later the code reads the central config directly.

## D-023 | 2026-10 | Proposed | Remove hardcoded site values from algorithms first (small, safe edits)
`dailyDataDir`, `PLATE_IMG_OUTPUT_DIR`, `bolt_save_dir`, `WHEEL_STAND_REQUIRED_COUNTS`, `expected_total` move to config. This is the minimum for a second site to run the same code.

## D-024 | 2026-10 | Proposed | Single PLC service owns Modbus
One process, one IP/register map from config, periodic light re-assert + verify (today a one-time write; a PLC reboot leaves the light off silently), cleaning on `poor_view` with cooldown, conveyor-status read, publishes health/events.

## D-025 | 2026-10 | Proposed | Persist wheel counter and plate state
So a restart or exception does not blind inspection for up to one revolution. Decide storage (file/DB) and validity window.

## D-026 | 2026-10 | Proposed | Edge-first; cloud layer aggregates sites later
Inference, capture and PLC stay on the local server. Cloud receives status, defect summaries and thumbnails for a central dashboard and fleet management.

## D-027 | 2026-10 | Accepted | PLC IP is per-server config; Maihar = 192.168.2.5
`light.py` already uses it. `cleaning_mechanism.py` (192.168.1.4) is wrong for this server and must read the IP from the site config. Applies to the future PLC service (D-024).

## D-028 | 2026-10 | Proposed | `wheel_no` stays text in DB; code treats it as int at the edges
`defects_table.wheel_no` and `wheel_counter_table.WHEEL_COUNT` are varchar. Do not change column types now (Django/DataSync depend on them); convert in code, and add a rule that wheel numbers are written as plain integers (no padding) so string comparisons stay valid.

## D-029 | 2026-10 | Proposed | Live wheel number belongs to both sides, not only Right
Only `RightAlgorithm` updates `wheel_counter_table` (single row ID=1). Decide whether the UI needs per-side live counters (then add a row/column per side) before the Left/Right merge.

## D-030 | 2026-10 | Accepted | One root folder per machine: `~/INSIGHTZZ` (code, config, logs, run, data)
New machine (Tadipatri): one NVMe disk, services run as the login user, no dedicated service user. Created by `scripts/setup_dpc_layout.sh` (no sudo, no users created, `--root` to change). Alternatives considered: FHS split (`/opt`, `/etc`, `/var/lib`, `/var/log`) and a `dpc` service user: rejected for now because there is one disk and one operator, so the split gave no isolation; the paths stay config values, so it can be adopted later. Refines D-013 (`site_root` = `~/INSIGHTZZ`). Secrets path moves from `/etc/dpc/secrets.env` (D-018 text) to `<root>/config/secrets.env`, still chmod 600.

## D-031 | 2026-10 | Accepted | Atomic frame hand-off
Write each frame once into `_tmp`, then `os.link` into `INF` (and into `UI` when empty) and `os.replace` into `live/IMG_1.jpg`. Readers never see partial files and the write volume halves. Needs `_tmp`, `INF`, `UI`, `live` on one filesystem; fallback to a copy only when hard links are impossible. Consequence: hard links share data, so no process may modify these files in place. Built and tested in `workers/frame_capture_basler/`; the FLIR version still copies in place.

## D-032 | 2026-10 | Accepted | Data folder layout under `<root>/data`
`frames/CAMn/{live,_tmp,INF,RAW_IMAGES,UI}`, `ui_live/CAMn` (reserved), `images/{DEFECTIVE,REPEATED,RAW,PLATE}/{LEFT,RIGHT}`, `wheel_status/{LEFT,RIGHT,IMAGES,PDFS}` (subfolders assumed), `bolt_data`, `state`, `models`. Replaces the legacy names (`RAW_IMG_DIR`, `TEMP_IMG_DIR/PLATE_DIR`, ...). The legacy algorithms cannot use it until their paths come from config (D-023).

## D-033 | 2026-10 | Proposed | `site.yaml` is the single source of truth, file-first; one capture process per camera
Validated against a schema on load; secrets separate (`secrets.env`); legacy XMLs and `workers.yaml` are generated by the renderer (refines D-014 and D-022: file first, DB-backed later). Capture runs as one worker per camera (`frame_capture_cam1`, `frame_capture_cam2`), each with its own health and restart. Answers open question Q8.

## D-034 | 2026-10 | Proposed | Config comes from local files at start; MQTT is for supervision only
The system must boot and inspect with the broker down (reinforces D-003). MQTT carries status, health, events and commands. Heartbeat payload is extended append-only with `pid, version, config_hash, fps, last_frame_age_s, inf_queue_depth`. Topic names stay as in CONTRACTS section 2 (one broker per site, no site id in topics). Changing settings later: validated `command` -> versioned file rewrite (previous copy kept) -> restart that instance.

## D-035 | 2026-10 | Proposed | Commissioning flow detects cameras and generates the config
Detect cameras (pypylon, PySpin), assign CAM1/CAM2 from the detected serials, test-open with the chosen settings, write `site.yaml` and `secrets.env`, run the layout script, render configs and `workers.yaml`, enable units. Command-line wizard first, local web page later. Refines D-016.

## D-036 | 2026-10 | Accepted | Repo and chat workflow
One Claude Project, one monorepo, one folder per owner and one chat role per owner (MASTER, SUPERVISOR, W-<worker>, UI, CONFIG, SITE). Contracts first: an interface change goes through MASTER before the affected chats work. The Project's GitHub link is a read-only snapshot refreshed with Sync now; code reaches the repo through the developer's commits or Claude Code. See CHAT_WORKFLOW.md and OWNERS.md.

## D-037 | 2026-10 | Accepted | Basler FrameCapture lives in `workers/frame_capture_basler/`; Left/Right stay in one folder
Positional XML with an optional 16th element `TMP_IMG_PATH`; env `BASLER_CONFIG` selects the file. `workers/inspection/` stays one folder holding both algorithms until the D-021 merge; splitting it into `inspection_left` and `inspection_right` would only create churn.

## D-038 | 2026-10 | Proposed | Site-specific names move out of code
The Basler code updates a table literally named `tadipatri_health_status_table`. Table names, like paths and serials, become config (`db.health_table`). Extends D-023.

## D-039 | 2026-10 | Proposed | One `main` branch with tagged releases; sites differ by config, never by branch
Every site runs a tagged release of `main` (`git fetch && git checkout v0.<n>`; rollback = an older tag). Site settings live in `<root>/config` (`site.yaml`, `secrets.env`, rendered files), optionally mirrored in a separate private repo `dpc-sites` with one folder per site and no secrets. A site that needs a code fix before the next release gets a short-lived `hotfix/<site>-<what>` branch that is merged to `main` quickly and released as a new tag, so the fix reaches every site. Rejected: one long-lived code branch per site (every fix applied N times, drift, merge conflicts in config files, real configs and secrets ending up in git history, and the Project's GitHub link reading the wrong code). Consequence: `docs/SITES.md` records the release tag per site.

## D-040 | 2026-10 | Accepted | Housekeeping after the Project verification
Found when the Project knowledge was checked from a fresh chat. History above is not edited; this entry supersedes:
- **D-039** (one `main`, tagged releases, no site branches) is **Accepted**; its "Proposed" label is superseded by this entry.
- The secrets path in **D-018** (`/etc/dpc/secrets.env`) is superseded by **D-030**: secrets live in `<root>/config/secrets.env` (chmod 600, never committed).
- The backlog order in `CODE_AUDIT.md` section 5 is superseded by `STATUS.md` ("Next"), with section 6 holding the later additions. The audit findings themselves stay valid.
- Rule: `STATUS.md` is the single current backlog and state; the audit, decisions and contracts hold the history and the agreements.

## D-041 | 2026-10 | Proposed | Basler XML: optional append-only elements for health table, log name and rotation
`BASLER_CONFIG*.xml` gains optional positions 16 `HEALTH_TABLE` (default `tadipatri_health_status_table`), 17 `LOG_FILE_NAME` (default today's shared name), 18 `ROTATE_DEG` (0 | 90 | 180 | 270, default 90 = today's behaviour; 0 = no rotation, no warning). Defaults reproduce current behaviour so existing files keep working. Matches `cameras[].rotate` in `site.yaml`; the renderer will write these elements later. Reason: rotation, health table and log name are site values hardcoded in code (D-023, D-038). Tadipatri sets `ROTATE_DEG` 0 on both cameras.

## D-042 | 2026-10 | Accepted | Secrets may be shared in chats; Claude works on the repo directly
The developer wants Claude to read and write the repo without manual downloads and uploads. A GitHub fine-grained token (DPC repo only, Contents read/write) is given in the chat or in a hand-added Project knowledge file. Claude clones, edits, commits and pushes: docs straight to `main`, code to a `feat/` or `hotfix/` branch that the developer tests and merges. Supersedes the "nothing secret in a chat, rotate if pasted" rule of `CLAUDE_PROJECT_SETUP.md` section E and instruction 4. Unchanged: no secret ever goes into code, docs, git history or memory (D-018, D-030); production secrets stay in `<root>/config/secrets.env`. Accepted risk: anyone who can read the chat history or the Project can use the token until it expires.

## D-043 | 2026-10 | Accepted | Database seed lives in `db/`, maintained with every DB change
The full DB structure for a new site is kept as SQL in `db/`: `schema.sql` (all tables, `CREATE TABLE IF NOT EXISTS`, no database name, no data), `fixed_rows.sql` (rows programs only `UPDATE`: `wheel_counter_table` ID 1, `CAMERA_PLC_STATUS` id 1), `site_rows.example.sql` (one health row per camera serial), numbered append-only `migrations/` for existing sites, and a `schema_version` table. Run by hand in MySQL Workbench today; `scripts/dpc_db_init.py` (CONFIG) runs the same files later for the installer. `db/` is owned by MASTER because it is the DB contract; worker chats request changes in their handoff. Every table or column change updates `schema.sql`, adds a migration and updates CONTRACTS section 5 in the same commit. Django tables stay with `manage.py migrate`. v1 is built from `mysqldump --no-data` of Maihar and Tadipatri, not from guesses. Reason: 35+ sites need an identical, repeatable DB; today the structure exists only on the running machines.

## D-044 | 2026-10 | Accepted | Keep the dedupe size check and the FLIR camera-lamp mapping as they are
Found while testing the DB seed (D-043): (1) the dedupe query in the algorithms and mail senders computes the bbox size from the new defect's own values, so the +-50 px size condition is always true and dedupe is effectively wheel + type + side within 24 h; (2) the FLIR capture writes CAM1 into `CAMERA_PLC_STATUS.RIGHT CAMERA` and CAM2 into `LEFT CAMERA`. Developer decision: no change to either. Chats must not raise these again or "fix" them unless the developer asks.

## D-045 | 2026-10 | Accepted | DB work moves to its own chat role, DB
The developer runs database work in a separate chat instead of MASTER. Role **DB** owns `db/` (seed SQL, migrations, `reference/` dumps) and `docs/CONTRACTS.md` section 5, so a table change, its migration and its contract text land in one commit (D-043). Supersedes the "`db/` owned by MASTER" part of D-043; everything else in D-043 stands. DB pushes directly per D-042. MASTER still owns the rest of `docs/` and records DB decisions in DECISIONS from the DB chat's handoff.

## D-046 | 2026-10 | Proposed | New sites do not get the legacy `LOGIN` table
From the DB chat handoff (2026-10-12). The legacy `LOGIN` table (plain-text `PASSWORD`) is left out of `db/schema.sql`; UI login on new sites uses Django auth (`auth_user`, created by `manage.py migrate`). Existing sites keep their table. Accept once the UI work confirms nothing new reads `LOGIN`.

## D-047 | 2026-10 | Accepted | D-033 accepted; `site.yaml` schema v1 fixed in CONTRACTS section 7
D-033 (`site.yaml` single source of truth, file first, one capture process per camera) moves to **Accepted**: the DB becomes the master copy only when the UI edits config, which needs its own decision then. Schema v1 is written in CONTRACTS section 7 so the CONFIG chat builds the loader against it. Choices made: unknown keys are errors; no per-key env overrides in v1 (only `DPC_SITE_CONFIG` for the file location); `cameras[].rotate` has no default because the model depends on orientation; `machine.expected_cycle_total` and `wheel_stand_required_counts` are optional until W-inspection reads them (D-023); `plc.legacy_db_numbers` only feeds the legacy XML; new secret key `DPC_SMTP_PASSWORD`, `DPC_SITE_NAME` retired (section 9). Changing a key later follows CONTRACTS section 8 and bumps `schema_version`.

## D-048 | 2026-10 | Proposed (accepted in D-049) | Secrets move into `site.yaml`; `secrets.env` retired
Would supersede the `secrets.env` part of D-018, D-030 and D-047, and CONTRACTS section 9. `site.yaml` `schema_version` 2 adds `db.user`, `db.password`, `mqtt.user`/`mqtt.password` (both or neither), `mail.sender_address`, `mail.disk_report_to`, `mail.graph.{tenant_id, client_id, client_secret}`, `mail.smtp.password`. `site.yaml` must be chmod 600 and owned by the service user; the loader never prints secret values; new CLI flag `--redacted` prints a shareable copy. Legacy workers reading `DPC_*` env vars: the Supervisor exports them from `site.yaml` (SUPERVISOR task) or the worker reads `site.yaml` (W-cleanup task). Risk accepted: any copy of `site.yaml` (chat, `dpc-sites` mirror, UI) holds secrets unless redacted.
MASTER review (2026-10-13): not implemented yet (loader is v1). Only `workers/cleanup/delete_script.py` reads `DPC_*` env vars today (verified), so one of the two legacy options must be chosen, not both. If accepted: Q17's `dpc-sites` mirror may hold only `--redacted` copies, and the later "DB becomes master" step (D-047) must store these fields encrypted or outside the shared config tables.

## D-049 | 2026-10 | Accepted | D-019, D-020, D-034 and D-048 accepted; Q1 answered
Developer decision 2026-10-13. **D-019** (worker `mode` service | oneshot | periodic), **D-020** (algorithm health = queue lag; `restart_cost: high`), **D-034** (config from local files, MQTT for supervision only, extended heartbeat) and **D-048** (secrets in `site.yaml`, schema v2, `secrets.env` retired) are Accepted. D-048 legacy route: the **Supervisor exports `DPC_*` env vars from `site.yaml`** for workers with `env_from_site: true` (CONTRACTS section 9); `delete_script.py` stays unchanged. **Q1:** an algorithm is healthy when its queue lag is under the threshold **and** its heartbeat is fresh. CONTRACTS sections 7 and 9 updated.

## D-050 | 2026-10 | Accepted | The DB user may be `root`; it is a warning, not an error
Developer decision 2026-10-13: sites may keep using MySQL `root` for the programs. Supersedes the "never `root`" rule of D-030 and CONTRACTS section 7/9. The loader accepts `db.user: root` and prints one warning line ("db.user is root; a dedicated user with limited rights is recommended (D-030)"); `--redacted` and every other check are unchanged; `dpc-check` reports it as a warning (exit 2), not a failure. A dedicated user stays the recommendation, not a requirement. Accepted risk: a leaked `site.yaml` gives full access to every database on that MySQL server, not only the site's own.

## D-051 | 2026-10 | Accepted | Supervisor V2 runtime rules
From the SUPERVISOR handoff (PR #7, 2026-10-18). (1) A periodic worker never goes FAILED: a failed run is a `worker_failure` event and increments `consecutive_failures`; the next scheduled run is the retry (a PLC down for minutes must not stop the light re-assert for good). (2) `start_after` readiness: a service is RUNNING, a oneshot/periodic has finished one run with exit 0; after `start_after_timeout_seconds` or as soon as a dependency is FAILED the worker starts anyway with a warning and `start_after_unmet`; applies only at Supervisor start, a start/restart command overrules it. (3) `site.yaml` is re-read at every start of an `env_from_site` worker (after a password change only that worker is restarted); a bad `site.yaml` fails only that worker; values are never logged or published, unexpected errors are reported by exception type only. (4) `require_heartbeat` defaults to false until the algorithms publish heartbeats; the full Q1 rule (queue lag AND heartbeat) is switched on per worker when W-inspection adds them.

## D-052 | 2026-10 | Accepted | How the Supervisor runs on a site
From the SUPERVISOR handoff (PR #8, 2026-10-20). The site's workers file is `<root>/config/workers.yaml`, hand-written from `supervisor/config/workers.basler-2cam.yaml` until the renderer exists (D-022). The Supervisor starts on boot as a systemd **system** unit `dpc-supervisor.service` with `User=<site user>`, rendered from the template `supervisor/dpc-supervisor.service` by `supervisor/install_service.sh` (run as the site user, from the Python that has pypylon; it runs `check-config` first); `KillMode=mixed`, `TimeoutStopSec=90`, `StartLimitIntervalSec=0`, ordered after `mysql.service`. Today the Basler XMLs live in `<root>/config/`; when the renderer exists it writes them to `config/rendered/` and the workers file changes one line per camera.

## D-053 | 2026-10 | Accepted (superseded by D-062) | Inspection reads `site.yaml` directly
Developer decision 2026-10-20. `LeftAlgorithm`/`RightAlgorithm` load their settings through `load_site_config` (folders from the layout, CONTRACTS 4.1; model, thresholds, machine values from `site.yaml`), not from `leftConfig.xml`/`rightConfig.xml`. No XML is rendered for inspection. Business logic is unchanged (wheel counting, plate, bolt, chain, dedupe; D-044 stays). Reason: Tadipatri is not in production, and runtime-learned values (D-054) and model updates (D-056) do not fit a positional XML that the renderer would overwrite. Maihar gets this version only after it has a `site.yaml`.

## D-054 | 2026-10 | Accepted | Learned values live in their own files, never in `site.yaml`
Values a program learns at runtime (first user: auto-ROI) are written to `<root>/data/state/<program>/<topic>.json`. Rules: exactly one program writes each file, others only read; atomic write (temp file + rename); each file records what it was learned from (camera serial, rotation, model name and version, number of images, time) and is ignored and relearned if any of these changed; a value fixed in `site.yaml` always wins over a learned one; deleting the file resets it; an unreadable file is logged and relearned, never a crash. No program ever edits `site.yaml`. Auto-ROI starts in observe mode (logs what it would remove, removes nothing); filtering is switched on per site later, because removing detections outside a learned ROI can hide a real defect.

## D-055 | 2026-10 | Accepted | `site.yaml` v3 is grouped by program
Shared sections at the top (`site`, `paths`, `db`, `mqtt`, `graph`); then one section per program holding everything it needs including its log settings (`capture`, `inspection`, `plc`, `mail`, `cleanup`, `transfer`), plus `models`. Log files still go to `<root>/logs/`. The Microsoft Graph login moves to one shared top-level `graph` section read by mail and transfer. Layout in CONTRACTS section 7.1. Only Tadipatri has a v2 file to convert; the loader names every moved key when it sees a v2 file.

## D-056 | 2026-10 | Accepted | Models are versioned packages; the Supervisor switches them
A model version is the folder `<root>/data/models/<name>/<version>/` with weights, Detectron config, class list, label mapping (e.g. `dusty_chain_base` -> `bucket_clip`) and a `manifest.json` (version, file checksums, classes, training date). `site.yaml` names the model and version (`inspection.model`); class list and label mapping leave the site config and the code and travel with the model. A `current` pointer per model name selects the running version. **The switch is a Supervisor function:** test the new version on saved images, move `current`, restart inspection once at a quiet moment (`restart_cost: high`), keep the previous version for one-step rollback. Learned values record the model version (D-054), so they are relearned after a switch. `models.auto_update`: `off` | `download` (fetch and verify, a person approves the switch) | `install` (switch automatically).

## D-057 | 2026-10 | Accepted | One transfer service for OneDrive (upload and model download)
A periodic worker under the Supervisor moves data to and from OneDrive through Microsoft Graph. **Upload** (automation case A7): weekly, images from both sides in chunks, resumable uploads; local data is not deleted or marked by the transfer (disk-based cleanup handles local space as today). **Model download** (D-056): checks the OneDrive folder `DPC/models/<name>/<version>/` for a newer version of the model named in `site.yaml`; a version counts as complete only when its `manifest.json` is present (uploaded last); downloads into `data/models/<name>/<version>/`, verifies every checksum, deletes and reports a failed version, marks a good one ready. It never switches models (D-056). This answers the model-hosting question: OneDrive, subject to Graph file permissions (admin consent in the Microsoft 365 tenant).

## D-058 | 2026-10 | Proposed (accepted in D-059) | Which model version runs: `site.yaml` version vs the `current` pointer
From the CONFIG handoff (PR #10). The Supervisor moves `current` (D-056) and no program edits `site.yaml` (D-054), so the two can disagree. Built today: `current` wins, `site.yaml` is used only when `current` is unset; `dpc-check` warns on a mismatch. MASTER proposal: `inspection.model.version` is either a fixed version (**pinned**: that version runs, the Supervisor does not switch away from it) or `auto` (**managed**: `current` decides, the Supervisor switches it on update); `models.auto_update: install` requires `auto`. This follows the D-054 rule that a value fixed in `site.yaml` wins.

## D-059 | 2026-10 | Accepted (superseded by D-060) | Model version: pinned or `auto`
Developer decision 2026-10-21 (option B of D-058). `inspection.model.version` is either a version string (**pinned**: exactly that version runs; the Supervisor never switches away from it; `current` is ignored for running) or `auto` (**managed**: `current` decides; the Supervisor switches it on update). `models.auto_update: install` requires `version: auto` (loader error otherwise); `download` works with both (a pinned site downloads and verifies, a person changes `site.yaml` to switch). `dpc-check` line `model`: pinned and the version is not installed = FAIL; `auto` and `current` unset = WARN. Supersedes the "current wins" behaviour built in PR #10.

## D-060 | 2026-10 | Accepted | Model versions are managed by the Supervisor, not in `site.yaml`
Developer decision 2026-10-21; supersedes D-059 and the model parts of D-055/D-056. **`site.yaml`** names only which model a site uses: `inspection.model: {name: <name>}`; inspection loads `<root>/data/models/<name>/current`. No version and no `models` section in `site.yaml`. **Supervisor config** (`workers.yaml`, section `models`): `auto_update: off | download | install`, `check_interval_hours`. **Supervisor commands:** `model status` (running, installed and pinned versions), `model switch <version>` (verify, test on saved images, move `current`, restart inspection once), `model rollback`, `model pin <version>` / `model unpin`. **State** in `<root>/data/models/<name>/`: `current` pointer and `pinned` marker, written only by the Supervisor (one-writer rule, D-054). A pinned version is never switched away from, also with `auto_update: install`, until `unpin`. Every switch, pin, unpin and rollback is an MQTT event. The OneDrive models folder stays in the transfer service's own `site.yaml` section (`transfer.models_folder`). Learned values keep recording the running version (D-054). Reason: the version changes while the site runs; it is operational state, not a site fact a person sets once.

## D-061 | 2026-10 | Accepted | First-install exception to the Supervisor's one-writer rule for `current`
From the CONFIG handoff (PR #11). `scripts/make_model_package.py` may set `current` only when a model has no `current` and no `pinned` yet (very first install on a site, before any Supervisor state exists). In every other case only the Supervisor writes `current` and `pinned` (D-060). The packaging tool writes `ready` after its own verification, like the transfer service.

## D-062 | 2026-10 | Accepted | One rendered JSON config per worker, generated from `site.yaml`
Developer decision 2026-10-22; supersedes D-053, the XML output of D-022, and the per-camera XML of D-041 once the capture is migrated. **`site.yaml` is the only file a person edits.** The **renderer** (CONFIG, `scripts/render_config.py`) validates `site.yaml` with the loader and writes **one JSON file per worker instance** to `<root>/config/rendered/<worker_id>.json` (chmod 600, atomic write, a `_generated` header with the `site.yaml` sha256 and time; never edited by hand, overwritten at the next render). **One format for every worker:** JSON with named keys; no positional XML is rendered. Each file holds only that worker's settings and only the secrets it uses. Workers read their file from env `DPC_WORKER_CONFIG`, which the Supervisor sets for every worker that has a rendered file. **Reload:** the Supervisor renders at every start and on `supervisor.py reload`; a `site.yaml` error writes nothing; the renderer reports which files changed and the Supervisor restarts only those workers (`restart_cost: high` workers at a quiet moment or on confirmation). Learned values (D-054) and the model `current` pointer (D-060) stay outside config. **Migration:** each worker switches from its hand-written XML to its rendered JSON when it is migrated (capture and inspection first); workers not yet migrated keep their current XML untouched until then. The JSON key set per worker is fixed in CONTRACTS section 13 when that worker is migrated.

## D-063 | 2026-10 | Accepted | What inspection settings live where (from the Tadipatri legacy XMLs)
Developer and MASTER, 2026-10-22. **Once per site** in `site.yaml`: `inspection.machine.total_wheel_count`, `inspection.threshold` (belongs to the shared model), `inspection.model.name`, `plc` (one PLC per site; `legacy_db_numbers` kept for the S7 DB numbers 12/13 until the PLC migration), `db`. **Per side** (`inspection.sides.<SIDE>`): object sizes in pixels (equal today, but they depend on each camera's distance, lens and angle). **Never in `site.yaml`, derived by the renderer from `paths.root` and the fixed layout (CONTRACTS 4.1):** every folder (`CODE_PATH`, logs, INF per camera, RAW/DEFECTIVE/REPEATED per side, TEMP, `start.txt`). **Derived:** the legacy `DEFECT_SIDE` value = `Left` / `Right` from the camera's `side` (Tadipatri's old right config had `RIGHT`; new right-side rows in `defects_table` will be `Right`, readers must accept it). **From the model package:** weights, Detectron config, classes, label map. **Dropped:** `DETECT_LABEL_NAMES` (same 19 classes as the model, filters nothing) and `INPUT_IMG_DIR` (another site's leftover path; W-inspection confirms in item 7 whether the code uses it, and if so it gets a fixed layout folder). Wheel-stand counts and the expected cycle total move from the code to `inspection.machine` in item 7.

## D-064 | 2026-10 | Accepted | Two `site.yaml` keys from the legacy PLC and UI configs
Append-only additions to v3 (CONTRACTS 7.1): `cameras[].ip` (optional; GigE camera address, used by the UI status and `dpc-check`), and `plc.light_on_value` (0 or 1, default 1; Tadipatri's light register is inverted, 0 = ON). Without the second key, a program writing 1 to switch the light on would switch it off. Legacy UI paths (live images, reports, daily defects) are derived from the layout when the UI is migrated, never typed (D-063).

## D-065 | 2026-10 | Accepted | Model metadata in git, weights never
Developer decision 2026-10-22 (middle way to D-057). For every packaged model version, the **small files** are kept in the repo at `models/<name>/<version>/`: `manifest.json` (incl. the weights sha256), `classes.json` and the self-contained Detectron `.yaml`. The **weights (`.pth`) are never in git** (GitHub refuses files over 100 MB, the repo is public, and git history keeps every version forever); they come from OneDrive (D-057) or a manual copy, and are verified against the sha256 in the repo's manifest. Packaging an already-known version then needs only the weights file: `make_model_package.py --from-repo models/<name>/<version> --weights <.pth>` (STATUS 6h). `.gitignore` blocks `*.pth`, `*.pt`, `*.pkl`, `*.onnx` everywhere.

## D-066 | 2026-10 | Accepted | Housekeeping: status of older "Proposed" entries (2026-10-22)
History above is not edited; this entry records where each older proposal stands. **Resolved by later decisions:** D-013 (one root, derived paths) by D-047/D-063; D-014 (config in DB first) by D-047 (file first) and D-062; D-019, D-020, D-034 by D-049; D-022 (renderer writes XML) by D-062 (renderer writes JSON); D-023 (path edits in the algorithms) by D-062/D-063; D-033 by D-047; D-038 (site names out of code) by D-062/D-063; D-039 by D-040; D-041 (Basler XML elements) built, then retired by D-062 when the capture moves to JSON (P6); D-048 by D-049; D-058 by D-059 and D-060. **Still proposals, decided when their work starts:** D-015 (camera driver interface, with the FLIR capture), D-016 (wizard self-test), D-017 (monitoring UI on MQTT), D-021 (Left/Right merge after replay tests), D-024 (single PLC service), D-025 (persist wheel counter and plate state), D-026 (edge-first), D-028 (`wheel_no` stays text), D-029 (live wheel number for both sides), D-035 (commissioning flow; `dpc-check` part built), D-046 (no `LOGIN` table on new sites).
**Questions closed:** Q2 (Tadipatri light 0 = ON, D-064; cleaning by script), Q3 (Tadipatri PLC 192.168.1.5, Maihar 192.168.2.5), Q4 (camera-lamp mapping kept, D-044), Q5 (schemas reconciled, D-043/D-045), Q9 (Graph is standard, SMTP supported), Q15 (new sites `health_status`; Tadipatri renamed to it), Q17 (reference copies without secrets or personal addresses in `docs/sites/`; real files only on the machines).

## D-067 | 2026-10 | Accepted | Inspection folders in the layout (from the renderer handoff, PR #12)
- `paths.raw` (images inspection has processed) = `data/frames/<CAM>/RAW_IMAGES`: the move out of `INF` is a rename on the same filesystem (D-031), so it stays next to `INF`, not under `data/images/RAW/<SIDE>`.
- `paths.temp` = `data/images/TEMP/<SIDE>`; added to the layout (CONTRACTS 4.1) and to `scripts/setup_dpc_layout.sh` (STATUS P4b).
- `paths.start_file` = `data/state/inspection_<side>/start.txt`, one per side (the legacy code kept both sides' `start.txt` in `LOGS/`). W-inspection confirms in P7 whether the two sides really share it; if they do, this becomes one shared file.
- `make_model_package --export-to-repo` copies the manifest unchanged, including `packaged_from` (local source paths, not secret); kept as is.

## D-068 | 2026-10 | Accepted | Captures keep the retry limit; each inspection side follows its camera
Developer decision 2026-10-24 (STATUS D1 = option b). (1) Captures keep `max_retries` (about 20-25 min without a camera, then FAILED; a person restarts it). (2) **Inspection follows its camera:** `inspection_left` belongs to the camera with side LEFT (`frame_capture_cam1`), `inspection_right` to RIGHT (`frame_capture_cam2`). When the camera's capture stops (unhealthy, restarting or FAILED), the Supervisor stops that side's inspection. When the capture is healthy again (frames flowing), the Supervisor starts that side's inspection once. Inspection is not restarted in a loop while the camera is down, and the other side is not touched. The SUPERVISOR chat designs the mechanism (for example a `follows: <worker_id>` key in `workers.yaml`, append-only in CONTRACTS 1) in P5.

## D-069 | 2026-10 | Accepted | Left is the reference; one inspection program for both sides
Developer decision 2026-10-24. Work is done on `LeftAlgorithm.py`; the right side becomes exactly the same code. Target: one inspection program run twice by the Supervisor, once with `inspection_left.json` and once with `inspection_right.json` (side, camera, folders and sizes come from the rendered JSON, D-062). `RightAlgorithm.py` is about 700 lines longer than Left: before Right is replaced, P7a lists every Right-only behaviour (for example the live wheel-number write, D-029) and the developer decides per item: keep it in the shared code (switched by side or by a config key), or drop it. Settles the direction of D-021.

## D-070 | 2026-10 | Accepted | Supervisor reload and follow rules (from the P5 handoff, PR #13)
(1) A reload reaches the running Supervisor by SIGHUP plus request/result files in `<root>/run/`, not over MQTT: it works with the broker down, and only the site user can trigger it. (2) A "quiet moment" for high-cost workers is an optional daily `monitoring.reload_quiet_window`; without one, such a worker waits for `reload --confirm` or its next natural restart. A better signal (for example right after a plate, from the inspection heartbeat) can replace it in P7. (3) `follows` pauses only on the change RUNNING -> down; STARTING changes nothing; retries during one outage count as one outage; planned restarts pause nothing; a follower never times out at start. (4) A worker whose rendered file is removed keeps running with its old settings (a warning is logged); reload never starts a stopped worker. (Proposed as "D-069" in the handoff; renumbered because D-069 was already taken.)

## D-071 | 2026-10 | Accepted | Basler capture health writing (from the P6 handoff, PR #15)
The capture only UPDATEs its existing health row (never inserts), immediately on a state change (OK <-> NOK) and otherwise every `health_interval_s` (default 600 s). Rows are counted as matched (`CLIENT.FOUND_ROWS`), so repeating the same value no longer logs a false "matched no row" warning (F7). Supersedes the health part of D-041 together with the XML.

## D-072 | 2026-10 | Accepted | Inspection config and layout additions (from the P7a analysis, `workers/inspection/INSPECTION.md`)
MASTER decisions on contracts only; no business-logic change. (1) `INPUT_IMG_DIR` stays dropped (never used, L:108 / R:112). (2) `paths.start_file` is removed from `inspection_<side>.json` (no code reads `start.txt`); the start-file line of D-067 is withdrawn. (3) New `paths.defect_raw` = `data/images/RAW/<SIDE>` (raw copies of defect frames), next to `paths.raw` = `data/frames/<CAM>/RAW_IMAGES` (frames moved out of `INF`). (4) New layout folders `data/images/MAGNET/<SIDE>` (magnet diagnostics) and `data/bolt_data/<SIDE>`; cleanup must cover `bolt_data` (B10). (5) New optional per-side keys under `inspection.sides.<SIDE>`: `motion` (`rtl` | `ltr`), `bucket_clip_window` and `half_bucket_clip_window` ({xmin, xmax, ymin, ymax}), and a `tracker` block (new-wheel jump, new-plate jump, clip confirmation, skidding, wheel-missing values); defaults = each side's current values (D-073 D5), so nothing changes until a value is set. (6) Withdrawn by D-073 D8: the wheel-stand mapping is a learned value in `data/state/`, not a `site.yaml` key. CONFIG implements (STATUS P7-prep).

## D-073 | 2026-10 | Accepted | Inspection behaviour decisions for the shared program (P7-dec, answered one by one)
- **D6 (R6), 2026-10-25:** both sides insert their defects into `defects_table` (Left's behaviour); Right's commented-out full-clip insert is not kept.
- **Rule (2026-10-25): every feature runs on both sides.** A feature runs on one side only when it is a true exception, switched by an explicit config key (example: the live wheel number, D3). Right-only and Left-only features found in INSPECTION.md become both-side features in the shared program.
- **D4 (R2, R15), 2026-10-25:** keep the best image per wheel and the daily copy (today Right only, 17:58, `WHEEL_STATUS/IMAGES/RIGHT/wheel.txt` and per-wheel copies), on **both sides**. Time in config (`inspection.wheel_report.daily_time`, default "17:58"); folders derived from the layout (`data/wheel_status/IMAGES/<SIDE>`, Q13); the crash when a best image was already deleted is fixed.
- **D5 (R5, R9), 2026-10-25:** tuning values are per side in `inspection.sides.<SIDE>.tracker`, each side keeping exactly today's value: clip confirmation window LEFT 300 ms / 200 px, RIGHT 500 ms / 500 px; skidding gap LEFT 500 ms, RIGHT 1000 ms. The same applies to the other tracker values and the clip windows (D-072): the rendered default for each side is that side's current value, not Left's.
- **D7, 2026-10-25:** no in-process restart. On an unexpected error the inspection program logs it and **exits with a non-zero code**; the Supervisor restarts it under its inspection rules (`restart_cost: high`, D-020, D-051). **The cause of every restart must be visible:** before exiting, the program writes `<root>/run/<worker_id>.last_error.json` (time, exception type, message, file:line, short traceback; no secret values); the Supervisor reads it when the process exits and puts it into the `worker_failure` / `worker_restarted` event and the worker status (`last_error`), and into its own log. This applies to every worker, not only inspection (CONTRACTS 2, STATUS F21).
- **D8, 2026-10-25:** (1) The expected cycle total is the same on both sides and equals the wheel count: 190 at Tadipatri for now (site-level `inspection.machine.expected_cycle_total`, default = `total_wheel_count`); the in-code 65 (Left) and 188 (Right) are dropped. (2) **The wheel-stand mapping is a learned value, not config** (D-054): inspection learns which wheel numbers carry how many stands and writes it to `<root>/data/state/inspection_<side>/wheel_stands.json` (one writer, `learned_from` incl. camera serial, model version and revolutions seen; deleted = learned again). The hardcoded maps (Left {34:3, 52:2, 82:1, 169:1, 210:1}, Right {35:2, ...}) are not carried over; `wheel_stand_required_counts` is withdrawn from D-072 item 6. The learning and the check are a new feature (STATUS P10b), built after P7; the check stays off until then, as today.
- **D9 (Q21), 2026-10-25:** wheel skidding, wheel missing and cycle restart become **defects** (DB row in `defects_table`, shown in the UI, mailed; the mail sender already sends every unsent row whatever its type), on both sides. All three follow the developer's reference pattern `docs/reference/inspection_defect_pattern_checkWheelMissing.py`: confirm over several frames (cancel when the belt has stopped), in-memory repeated check, 24 h DB dedupe (D-044) and the wheel-count gate, then RAW + boxed image and `insertDefectData`; repeated ones go to REPEATED only. Defect type names as today: `WheelMissing`, `Wheel_Skidding`, and a cycle-restart name chosen in P7. Frame thresholds and the fixed bbox move to per-side config (D-072 `tracker`, D-073 D5). **Note for P7:** in the pattern, `is_reported` is never reset, so each wheel number is reported at most once per process run (B7); developer decision 2026-10-25: **keep it as it is** (no reset per cycle).
- **D3 (R1, D-029), 2026-10-25:** the live wheel number (`wheel_counter_table`, row ID 1, read by the UI) is written by exactly one side, chosen in config: new key `inspection.live_wheel_counter_side: LEFT | RIGHT` (Tadipatri: RIGHT, as today); the other side counts but does not write it. Settles D-029.

## D-074 | 2026-10 | Accepted | `~/INSIGHTZZ/old/` is not a code reference
Developer decision 2026-10-25. The previous setup copied to `~/INSIGHTZZ/old/` on Tadipatri is not compared or used as a reference. The repo `main` is the only source for how a worker behaves; INSPECTION.md describes the repo copies only (section 15). The old folder is still useful for data (configs read in D-063/D-064, the original model weights) until it is deleted.

## D-075 | 2026-10 | Accepted | P7-prep choices (CONFIG handoff 2026-10-26)
Accepted as built: `live_wheel_counter_side` defaults to RIGHT when a RIGHT camera exists, else the only side; `tracker` exposes `skidding.required_ratio` (0.7) and `high_dusty_suppress_px`; `OFFSET_PX_X` (10) is not exposed (unused on both sides); Right's half-clip x window 100..1850 is taken from R:894-895. `data/wheel_status/IMAGES/<SIDE>` is added to the layout (CONTRACTS 4.1); `setup_dpc_layout.sh` and the `dpc-check` layout check will include it (STATUS F23), until then W-inspection creates it when missing.

## Open questions
- Q21: Should skidding, wheel-missing and cycle-restart findings create DB rows and mails? Today they only save images, so nobody is alerted (INSPECTION.md 8.1). (STATUS D9)
- Q20 (answered 2026-10-22): Tadipatri PLC IP is 192.168.1.5; the 192.168.1.4 in the legacy PLC test script is wrong.
- Q18: Who in the Microsoft 365 tenant grants admin consent for Graph file permissions (D-057), and is the target a OneDrive or a SharePoint document library? (Not blocking: the developer settles it while testing item 9.)
- Q19: What does the existing DataSync do today (the `is_data_sync` columns)? Absorb it into the transfer service (D-057) or keep it separate?
- Q1 (answered, D-049): queue lag plus heartbeat.
- Q2: Light register 4109: does 1 mean ON or OFF? Is cleaning ever triggered by the algorithm today, or by an operator/script?
- Q3 (partly answered): PLC IP for the Maihar server is 192.168.2.5. Still open: should `cleaning_mechanism.py` (192.168.1.4) and the XML (192.168.1.5) simply be corrected? Are S7 DB numbers 12/13 used anywhere?
- Q4: `CAMERA_PLC_STATUS`: is LEFT/RIGHT CAMERA mapping inverted vs CAM1/CAM2?
- Q5 (mostly answered): `defects_table` and `wheel_counter_table` schemas received (see CONTRACTS section 5). Still needed: `health_status` and `CAMERA_PLC_STATUS` schemas, and indexes on `defects_table` (the 24 h dedupe query filters by create_datetime, wheel_no, defect_type, defect_side).
- Q6: Why are the chain/wheel-bolt/gap checks commented out, and is that intended for all sites?
- Q7 (answered for Tadipatri): services run as the login user (D-030). Still open for Maihar: do the existing services run as different Linux users or with EnvironmentFile settings?
- Q8 (answered): FrameCapture splits into one process per camera (D-033).
- Q9: Which mail sender is standard (SMTP/Gmail or Graph) for new sites?
- Q10: Is a dedicated Linux/GPU test machine available for the replay harness?
- Q11: Who empties `UI/` (the mailbox), and what reads `ui_live/CAMn/TMP.jpg` or `live/IMG_1.jpg`: the Django UI for both?
- Q12: Does any consumer (algorithm, UI, DataSync) modify files in `INF`, `UI` or `live` in place? Hard links make that unsafe (D-031).
- Q13: What are the real `WHEEL_STATUS` subfolders? The layout script assumes LEFT, RIGHT, IMAGES, PDFS.
- Q14: Are old defect records imported into the new DB? `view_defect` holds absolute image paths, so a layout change breaks old images unless the rows are rewritten or the old paths are kept.
- Q15: Which health table does each site use (`health_status` or `tadipatri_health_status_table`)? Needed for D-038. **New sites: answered by D-043, standard `health_status`; existing sites keep their table via `HEALTH_TABLE`.**
- Q16 (answered): Tadipatri CAM1 = LEFT = 24824427 (exposure 1500 us), CAM2 = RIGHT = 24874775 (exposure 2000 us), both 4 fps, no rotation. See SITES.md.
- Q17: Keep site configs only on each machine, or also mirror them (no secrets) in a private `dpc-sites` repo, one folder per site (D-039)?
