# Project status

Owned by MASTER; update after each handoff. Last updated: 2026-10-12.

## Current state (summary)
1. Docs are at v3: architecture, contracts, decisions D-001 to D-041, code audit, owners, chat workflow, commissioning checklist. The Project environment has been verified from a fresh chat.
2. Releases: one `main` branch with tags `v0.<n>`. Sites differ only by config (D-039, accepted through D-040). Secrets go in `<root>/config/secrets.env` (D-030).
3. Deployed layout `~/INSIGHTZZ/{code,config,logs,run,data}` is built by `scripts/setup_dpc_layout.sh` and exists on the Tadipatri machine.
4. Basler single-camera FrameCapture with atomic hand-off (D-031) **has been tested on the real Tadipatri cameras and works**: `live` updates, `INF` fills, `_tmp` stays empty, the log is clean.
5. Tadipatri cameras mapped and tuned: CAM1 = LEFT = 24824427 (4 fps, 1500 us), CAM2 = RIGHT = 24874775 (4 fps, 2000 us). No image rotation is wanted, but the code still rotates every frame by 90 until item 2 (D-041).
6. Open Basler findings (CODE_AUDIT 6):
   - P0: root DB login in plaintext.
   - P1: stale grab is republished; camera reopen code is unreachable; health update is commented out; health table name is hardcoded; rotation (90) is hardcoded.
   - P2: old Basler node names.
   None of these showed up in the normal-run test; apart from rotation, they concern failure paths.
7. The two cameras share one log file, so they were tested one at a time and cannot run together yet.
8. Supervisor V1 is built but has only run dummy workers. It is missing `mode` (service/oneshot/periodic), `queue_lag`, the richer heartbeat and a start order (D-019, D-020, D-034, all still Proposed).
9. `site.yaml` (D-033) is the agreed direction but its schema is not in CONTRACTS yet. Workers still read positional XMLs.
10. Legacy algorithms, the FLIR capture, mail, cleanup and PLC scripts still hardcode paths or site values (D-023, D-038). They cannot use the new layout yet.

## Done
- Claude Project environment verified from a fresh chat; housekeeping recorded in D-040.
- Architecture v3, contracts v3, decisions D-001 to D-040, code audit (incl. Basler section), ownership, chat workflow, commissioning checklist.
- Deployed layout script and the Basler single-camera FrameCapture with atomic hand-off.
- Tadipatri machine: layout created, repo at `~/INSIGHTZZ/code`, pushed to GitHub (`purushottamkumar2004/DPC`).
- **Tadipatri first real capture test passed (2026-10-12)** for both Basler cameras. COMMISSIONING step 7 checks passed: `live/IMG_1.jpg` updates every frame, `INF` grows at the configured fps, `_tmp` is empty, the log is clean, the image is correct. `INF` queues were emptied after the test.
- Tadipatri camera mapping and tuning confirmed: CAM1 = LEFT = 24824427 (4 fps, 1500 us), CAM2 = RIGHT = 24874775 (4 fps, 2000 us), no rotation wanted. Recorded in `SITES.md`; Q16 answered.
- Tadipatri camera health working (2026-10-12): both captures started with `BASLER_CONFIG` pointing at `~/INSIGHTZZ/config/`, element 16 `HEALTH_TABLE` = `tadipatri4_health_status_table`; `OK` written at start, every 10 min, and `NOK` at once on a camera loss.

## In progress
- Repo hygiene: the repo is **public** (checked 2026-10-12). Make it Private (GitHub, Settings, General, Danger Zone, Change visibility). A history scan found only `CHANGE_ME` placeholders, no real passwords; tag `v0.1` still to confirm.
- Set CAM2 exposure to 2000 us in `BASLER_CONFIG2.xml` (position 5; done on the machine by you). Rotation switches off with item 2.

## Next (ordered)

**0. You, not a chat (prerequisite).** Rotate the DB `root` password and the MS Graph secret. Create a limited DB user per site. Make the repo Private and confirm `v0.1` exists. Set `RELEASE` for Tadipatri in `SITES.md`.
Done when: no config on the machine uses `root`; the old secrets are revoked; `SITES.md` shows the Tadipatri release tag.

### 1. `site.yaml` schema and loader: DONE (merged PR #4, `cc191e1`; no handoff received yet)
| | |
|---|---|
| Owner chat | CONFIG |
| Files | `common/config/site_config.py` *(proposed)*, `config/examples/site.example.yaml`, `config/examples/secrets.example.env`, `tests/unit/test_site_config.py` |
| Contract impact | None new: build against CONTRACTS section 7 (schema v1, D-047) and section 9 (secret keys). Update `config/examples/site.example.yaml` to v1 (it predates it: e.g. `rotate`, `object_sizes`, `model` missing). |
| Done when | (a) The example file loads into a typed object. (b) A missing required key, an unknown key or a wrong type fails with a message that names the key. (c) `~` and `paths.root` are expanded. (d) Secrets come only from `secrets.env`, and the loader refuses that file if it is not chmod 600. (e) Unit tests pass. (f) The examples contain no real secret or serial. |

### 2. Basler capture robustness: BUILT (PR #2), in use at Tadipatri; cable-pull test (b) still to run
| | |
|---|---|
| Owner chat | W-frame_capture_basler |
| Files | `workers/frame_capture_basler/FrameCapture_Basler.py`, `BASLER_CONFIG1.example.xml`, `README.md` |
| Scope | Reopen the camera after device-lost. Publish only a grab retrieved in the current iteration. Health table name from config (D-038). One log file per camera. Rotation from config (`ROTATE_DEG`; 0 = none, with no per-frame warning). Set each camera setting by feature name, with one log line per setting. |
| Contract impact | **Yes, small.** The Basler XML gets optional, append-only elements 16 `HEALTH_TABLE`, 17 `LOG_FILE_NAME` and 18 `ROTATE_DEG` (D-041); the defaults reproduce today's behaviour. The heartbeat is out of scope here; health comes from `file_activity` on `live/IMG_1.jpg` until item 3 is done. |
| Done when | Tested on the Tadipatri cameras: (a) both cameras run at the same time with separate logs; (b) after the cable is pulled and replugged, frames resume within 30 s with no manual restart, and the log shows the lost and reopened events; (c) no two consecutive `INF` files are byte-identical during a 10-minute run; (d) health rows go to the table named in config; (e) the `_tmp` folder is empty after a clean stop; (f) the normal-run checks from the 2026-10-12 test still pass (regression); (g) with `ROTATE_DEG` 0 the saved frames are 1920x1080 and the log has no rotation warnings, and with the element absent frames are still rotated 90 (backward compatible). |

### 2b. Database seed (D-043): DONE 2026-10-12 (DB chat)
`db/schema.sql` v1 reconciled with the Tadipatri export: every Tadipatri table is seeded or listed under "Left out" in `db/README.md` with the reason; `CAMERA_PLC_STATUS` gained `PLC`; CONTRACTS section 5 rewritten (writer and reader per table). `db/test_seed.py` passed twice on MySQL 8.0.46: seed runs twice, 16 worker DB statements, all 26 Tadipatri tables accounted for, CONTRACTS 5 names every seeded column, no site values. Reference export: `db/reference/tadipatri/schema_2026-10-12.sql`. `scripts/dpc_db_init.py` stays with CONFIG after item 1.

### 3. Supervisor V2: modes, `queue_lag`, heartbeat, start order
| | |
|---|---|
| Owner chat | SUPERVISOR |
| Files | `supervisor/core/{config,process_manager,monitor,recovery_manager}.py`, `supervisor/health/queue_lag_health_check.py` *(proposed)*, `supervisor/config/workers.yaml` (example), `tests/unit/` for the supervisor |
| Contract impact | **Yes.** New `workers.yaml` keys: `mode`, `interval_s` (periodic), `restart_cost`, `start_after`, and a `queue_lag` health block (folder, max age). The heartbeat payload is extended append-only (D-034). Needs D-019, D-020 and D-034 Accepted, and Q1 answered. |
| Done when | Tested with dummy workers: (a) a oneshot that exits 0 ends as STOPPED and is not restarted, while a non-zero exit counts as a failure; (b) a periodic worker runs every `interval_s`; (c) `queue_lag` goes UNHEALTHY when the oldest `INF` file is older than the threshold and recovers when the folder drains; (d) workers start in `start_after` order; (e) both the old and the extended heartbeat are accepted; (f) a worker with `restart_cost: high` uses the longer timeout. |

### 4. First real workers under the Supervisor (Tadipatri)
| | |
|---|---|
| Owner chat | SUPERVISOR (worker entries); SITE-tadipatri records the result in `SITES.md` |
| Files | Site `workers.yaml` in `<root>/config/` (hand-written until the renderer exists), with an example in `supervisor/config/` |
| Contract impact | Worker ids `frame_capture_cam1` and `frame_capture_cam2` (D-033). Decide where the site `workers.yaml` lives (see gaps). No DB change. |
| Done when | (a) The Supervisor starts both captures. (b) `kill -9` on one capture leads to a restart within 10 s and a `worker_restarted` event on MQTT. (c) A pulled cable produces UNHEALTHY and then recovery, without exhausting `max_retries`. (d) A 24 h unattended run with both cameras has no FAILED state and the `INF` folders are drained (manually, until the algorithms run). |

### 5. `dpc-check` preflight
| | |
|---|---|
| Owner chat | CONFIG |
| Files | `scripts/dpc_check.py` *(proposed)*, `tests/unit/test_dpc_check.py` |
| Checks, in order | layout exists and the frame folders share one filesystem; `site.yaml` is valid (item 1 loader); `secrets.env` is chmod 600 and outside git; free disk is above the cleanup threshold; each configured serial is detected (pypylon); DB login works with a non-`root` user; MQTT broker is reachable (a warning only, per D-034) |
| Contract impact | **Small.** Exit codes 0 = pass, 1 = fail, 2 = pass with warnings, plus one result line per check. Add to CONTRACTS as the commissioning interface (D-035). |
| Done when | (a) On Tadipatri all checks pass without sudo. (b) Each check can be made to fail on purpose (unplug a camera, `chmod 644 secrets.env`, a wrong serial, `root` as DB user) and then fails with a one-line reason. (c) It runs in under 30 s. (d) It never prints a secret. |

### Follow-ups from handoffs (small, any order)
| # | Owner chat | What | Done when |
|---|---|---|---|
| 6 (DONE, PR #3) | W-mail | `workers/mail/immediate_mail_sender.py` line ~329 uses `defect['id']` on a DictCursor row whose key is `ID`: KeyError the first time a duplicate defect is found (the outlook sender already uses `ID`). Verified in code. | A duplicate defect is marked ignored (2) without an exception; send path unchanged |
| 7 | W-frame_capture_basler | Health "matched no row" warning can be false: MySQL returns changed rows, not matched rows, when the value is unchanged. Connect with `client_flag=CLIENT.FOUND_ROWS`. | Repeating the same health value on an existing row logs no warning; a missing row still warns |
| 8 | DB | Bring the Tadipatri DB to schema version 1 with a reviewed upgrade script, run only after a full backup. | Script reviewed by you; after it, `db/test_seed.py` checks pass against `tadipatri4`; backup restorable |

### Automation cases (from the 2026-10-13 review; owner = chat that builds it)
| # | Case | What the system will do | Owner chat | Covered by |
|---|---|---|---|---|
| A1 | New camera inserted | Detect the serial, offer it in the wizard, write `site.yaml` | CONFIG | `dpc-check` (item 5), wizard (later, D-035) |
| A2 | Camera not connected | Reopen by itself, `NOK` health, alert | W-frame_capture_basler, SUPERVISOR | Reopen + NOK done (item 2); alert after item 3 |
| A3 | Camera below the configured fps | Measure real fps, report it, warn below a threshold | W-frame_capture_basler, SUPERVISOR | Heartbeat `fps` (D-034, item 3) |
| A4 | Camera parameters changed | Read settings back periodically and re-apply config values | W-frame_capture_basler | New |
| A5 | Inference delay | Detect a growing `INF` backlog | SUPERVISOR | `queue_lag` (D-020, item 3) |
| A6 | Model deployment | Fetch a model by version with checksum, switch, roll back | CONFIG | Installer; waits for the model-hosting decision |
| A7 | Weekly data upload | Chunk images from both sides, upload weekly | new worker (W-data_upload) | New; needs a target (where to upload) |
| A8 | Light off | Detect a dark view, switch the light on via PLC | W-plc | PLC service (D-024) |
| A9 | Sunlight on the view | Mask that image region, set per camera in config | W-inspection, CONFIG | New; needs a `site.yaml` key (contract change) |
| A10 | Dust on the view | Detect a dirty view, trigger the cleaning pulse | W-plc, W-inspection | PLC service (D-024) |
| A11 | Other wrong-view causes | To be defined | - | - |
| A12 | Code error | Restart the failed program, event over MQTT, mail alert | SUPERVISOR | Restart done (V1); alert after item 3 |

### Later (unchanged intent)
- `install.sh`: layout, clone at a tag, venv, dependencies, model fetch, then the wizard.
- Renderer for the legacy XMLs and `workers.yaml` (D-022).
- FLIR per-camera capture with the same hand-off.
- D-023 path edits in the algorithms.
- PLC service (D-024).
- Replay harness (D-021).
- UI monitoring page.

## Decision and contract gaps (blocking the items above)
| Gap | Blocks | Needed |
|---|---|---|
| D-048 Proposed (secrets into `site.yaml`, schema v2) | CONFIG v2 loader, SUPERVISOR env export, W-cleanup | Accept or reject; if accepted pick one legacy route (Supervisor export or `delete_script.py` reads `site.yaml`); then CONTRACTS 7 and 9 updated |
| D-019, D-020, D-034 Proposed; Q1 open | 3 (Supervisor V2) | Accept; Q1 = queue lag plus heartbeat |
| Which extra tables Django and DataSync need (Django's own are left to `manage.py migrate`) | 2b (later) | Add to `schema.sql` through a migration when the UI/DataSync work starts |
| D-038 Proposed; Q15 open (health table per site) | 2 | Accept D-038; answer Q15 for Maihar and Tadipatri |
| D-041 Proposed (Basler XML elements 16 to 18) | 2 | Accept D-041 and add the elements to CONTRACTS section 3 |
| `health_status` schema unknown (Q5) | 2 (DB health part) | Export the schema from Maihar and Tadipatri |
| D-019, D-020, D-034 Proposed; Q1 open | 3 | Accept all three; answer Q1 (proposed: queue lag plus heartbeat) |
| No `start_after` / `restart_cost` keys in the `workers.yaml` contract | 3, 4 | Add to CONTRACTS together with item 3 |
| Where the site `workers.yaml` lives | 4 | Proposed: `<root>/config/workers.yaml` until the renderer exists, then `rendered/` |
| How the Supervisor itself is started on boot (systemd user unit? assumed) | 4 | Decide; note it in COMMISSIONING |
| `dpc-check` exit code and output format | 5 | Add to CONTRACTS (D-035 refinement) |
| Secrets rotation and a dedicated DB user (item 0) | 2, 4, 5 (anything touching the DB) | Owner: you |
| Tadipatri model and object sizes must match unrotated 1920x1080 frames | Tadipatri inspection (not items 1 to 5) | Confirm before the algorithms run on this site |

## Waiting for a decision (not blocking the next 5)
- Where the global model is hosted and how installs fetch it (checksum).
- How a new machine gets the private repo (read-only deploy key, or a release bundle).
- Q11 to Q14 and Q17 in `DECISIONS.md`.

## Known risks
- Root DB login and a plaintext password in the old Basler config (rotate; use a dedicated DB user).
- The capture test covered normal running only. A lost camera still ends in timeouts forever, with no health signal (CODE_AUDIT 6, P1). Fixed by item 2.
- Frames are rotated 90 in code while the site wants none. The algorithms' geometry (left-to-right motion, plate x-movement, object sizes) and the model must match whatever orientation is finally used.
- Legacy algorithms and the FLIR FrameCapture hardcode paths (D-023); they cannot use the new layout yet.
- The cameras share one log file; run them one at a time until item 2 is done.
- The Supervisor has never run a real worker; item 4 is the first proof.
