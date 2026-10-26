# Project status

Owned by MASTER; update after each handoff. Last updated: 2026-10-22 (full clean-up; the history of every finished item is in git and DECISIONS).

## Current state (summary)
1. **Goal:** one codebase for every site; a person edits only `site.yaml`; programs run under the Supervisor and recover by themselves.
2. **Tadipatri** (not in production yet): both Basler captures run under the Supervisor as a boot service, read their rendered JSON (no XML since P6; a `site.yaml` change + reload really changes the camera), recover from cable and network loss, report health to `health_status` and status over MQTT (local mosquitto). Inspection, mail, PLC and cleanup are not running yet.
3. **Config:** `site.yaml` v3, grouped by program, holds the secrets (D-048, D-055); validated by the loader; `dpc-check` passes on Tadipatri (only WARN: `db.user` root, allowed by D-050). Reference copy without secrets: `docs/sites/tadipatri.site.reference.yaml`.
4. **Model:** `dpc_maskrcnn/2026.10.12` packaged on Tadipatri (19 classes); versions are managed by the Supervisor (D-060); weights never in git (D-065).
5. **Next big step:** the renderer (one JSON per program from `site.yaml`, D-062), then capture and inspection read their JSON, then inspection runs under the Supervisor.

## Done
| Item | Result |
|---|---|
| Docs, contracts, decisions, commissioning checklist | D-001 to D-065 |
| Deployed layout script | Layout on Tadipatri |
| Basler capture, robustness, D-041 settings | PR #2; cable-pull and 28-min network outage recovered at Tadipatri |
| DB seed `db/schema.sql` v1 | Reconciled with Tadipatri; `db/test_seed.py` |
| `site.yaml` loader v1 to v3, `--redacted`, chmod 600 check, root = warning | PR #4, #5, #6, #10, #11 |
| Supervisor V2 (modes, queue lag, heartbeat, start order, env bridge) | PR #7 (D-051) |
| Captures under the Supervisor, boot service, MQTT | PR #8 (D-052); checks (a)-(c) passed at Tadipatri |
| `dpc-check` | PR #9; passes at Tadipatri |
| Mail `defect['id']` crash | PR #3 |
| Model package reader, packaging tool, version managed by Supervisor | PR #10, #11 (D-060, D-061); Tadipatri model packaged |
| Tadipatri `site.yaml` v3 | Converted and valid |
| Legacy configs read and mapped (inspection, PLC, UI, mail, cleanup) | D-063, D-064, CODE_AUDIT 7, SITES |

**Install guide:** `docs/INSTALL.md` is the single step-by-step installation for a new site; every handoff carries an `Install impact:` line and MASTER keeps the guide current.

## Pending, in order (do one at a time)
| # | Owner | What | Done when |
|---|---|---|---|
| P1 (DONE 2026-10-24: service active, FAILED = 0, both captures started by the Supervisor and delivering frames, `INF` cron installed) | You (Tadipatri) | Check the service-run basics: `systemctl status dpc-supervisor` active; `grep -c FAILED ~/INSIGHTZZ/logs/supervisor.log` = 0; the `INF` cron entry exists (`crontab -l`) so the disk does not fill | All three confirmed |
| P2 (DONE 2026-10-24: real values filled from the reference file, developer confirmed) | You (Tadipatri) | Fill the real `site.yaml` from `docs/sites/tadipatri.site.reference.yaml` (DB password, Graph values, mail lists), chmod 600, run the checker | Checker `OK` (root warning only) |
| P3 (DONE 2026-10-24: `models/dpc_maskrcnn/2026.10.12/` on main; both small files match the manifest sha256) | You + MASTER | Copy the model's small files (`manifest.json`, `classes.json`, `.yaml`) into the repo at `models/dpc_maskrcnn/2026.10.12/` (D-065) | Files on `main`; no `.pth` in git |
| P4 (DONE, PR #12, 197 tests pass; Tadipatri reference rendered: 4 written, then 4 unchanged) | CONFIG | One task: **6e** renderer (`site.yaml` -> `config/rendered/<worker_id>.json`, CONTRACTS 13, D-062, D-063), **6g** keys `cameras[].ip` and `plc.light_on_value` (D-064), **6h** `make_model_package.py --from-repo` / `--export-to-repo` (D-065) | Tadipatri: 4 JSON files rendered; second run `unchanged`; tests pass |
| P4b | CONFIG (small) | D-067: add `data/images/TEMP/LEFT` and `RIGHT` to the layout (CONTRACTS 4.1), `scripts/setup_dpc_layout.sh` and the `dpc-check` layout check | `setup_dpc_layout.sh` creates them; `dpc-check` layout counts them |
| P4c (DONE 2026-10-24: renderer on the real site.yaml, 4 written then 4 unchanged; `--export-to-repo` worked; machine back on `main`, clean) | You (Tadipatri) | `git pull`, then try the renderer once: `python3 scripts/render_config.py ~/INSIGHTZZ/config/site.yaml` (writes 4 files to `~/INSIGHTZZ/config/rendered/`, nothing reads them yet); optional: add `ip:` to both cameras and `light_on_value: 0` under `plc` in `site.yaml` | 4 `written`; a second run says 4 `unchanged` |
| P5 (code DONE, PR #13, 228 tests pass on main; F10, F11, D-068 included) | SUPERVISOR | **6f** `reload` and render at start, `DPC_WORKER_CONFIG` per worker, restart only changed workers; plus **F10** `run_timeout_seconds`, **F11** MQTT login from `site.yaml`, **D-068** captures keep the retry limit; inspection follows its camera (stop when the capture stops, start once when it is healthy again) | Edit CAM2 exposure, `reload`: only CAM2 restarts |
| P5b (DONE 2026-10-25: service reinstalled; reload after a CAM2 exposure edit: `1 written, 3 unchanged`, restarted only `frame_capture_cam2`) | You (Tadipatri) | Site check of P5: `git pull`; re-run `supervisor/install_service.sh` (adds `ExecReload`); restart the service; captures still run; edit CAM2 `exposure_us` in `site.yaml` and run `supervisor.py reload`: only `frame_capture_cam2` is reported as changed (captures do not use their JSON until P6, so nothing else changes) | Service active after reinstall; reload reports exactly one change |
| P6 (DONE 2026-10-25, PR #15; at Tadipatri CAM2 exposure 2100 -> 2200 + reload gave `ExposureTimeAbs = 2200.0`, both health rows OK; F7 done; old robustness branch carried over and deleted) | W-frame_capture_basler | **7b** capture reads its rendered JSON (no XML); carry over the deprecation fix and `health_interval_s` from branch `feat/frame_capture_basler-robustness` (then delete it); **F7** health "matched no row" uses `CLIENT.FOUND_ROWS` | Both Tadipatri captures run from JSON; `BASLER_CONFIG*.xml` no longer read |
| P4d | CONFIG (small) | Optional `site.yaml` key `capture.health_interval_s` (seconds, default 600), rendered into each `frame_capture_<camn>.json` (CONTRACTS 13); add to the example and the Tadipatri reference | Changing it + reload changes the capture's health interval |
| F20 | SUPERVISOR (small, with F19) | After P6: drop the two `env: BASLER_CONFIG` blocks and the "settings live in the XMLs" header in `supervisor/config/workers.basler-2cam.yaml` (and its test line 55); remove the XML mentions in `workers.yaml` (line 86), `supervisor/README.md` (line 153) and the `core/config.py` docstring; same change in Tadipatri's `~/INSIGHTZZ/config/workers.yaml` | No XML mentioned for the Basler captures; tests pass |
| P7a (DONE 2026-10-25: `workers/inspection/INSPECTION.md`, PR #16; the old-folder comparison dropped, D-074) | W-inspection | **Understand before changing (D-069):** read-only, in-depth analysis of `LeftAlgorithm.py` (2,527 lines), the reference; for `RightAlgorithm.py` (3,243 lines) only the list of Right-only behaviour and differences, each needing a keep/drop decision; write `workers/inspection/INSPECTION.md` (the full logic: lifecycle, detection, wheel counting, plate/cycle, bolt/clip/chain/stand checks, defects and dedupe, DB/files/PLC, in-memory state, every hardcoded site value, Left vs Right differences, open questions answered); compare the repo copy with the code that ran at Tadipatri (`~/INSIGHTZZ/old/CODE/ALGORITHM/`). **No code change** | `INSPECTION.md` merged; MASTER links it from ARCHITECTURE; every statement has a file and line reference or is marked assumed |
| P7-dec (DONE 2026-10-25, D-073; `is_reported` stays as it is, no reset per cycle) | You | Decisions for the shared inspection program (INSPECTION.md sections 4, 12, 14): D3 to D9 | All answered and recorded |
| P7-prep | CONFIG | D-072: drop `paths.start_file`; add `paths.defect_raw`; layout folders `data/images/MAGNET/<SIDE>` and `data/bolt_data/<SIDE>` (CONTRACTS 4.1, `setup_dpc_layout.sh`, `dpc-check`); optional per-side keys `motion`, `bucket_clip_window`, `half_bucket_clip_window`, `tracker` (no `wheel_stand_required_counts`, D-073 D8), rendered into `inspection_<side>.json`; site key `inspection.live_wheel_counter_side` (D-073 D3), rendered as `writes_live_wheel_counter: true|false` per side; `inspection.wheel_report.daily_time` (default "17:58") and `paths.wheel_status_images` = `data/wheel_status/IMAGES/<SIDE>` (D-073 D4) | Tests pass; Tadipatri renders with today's values as defaults |
| P7 | W-inspection | **7** Left/Right read their rendered JSON and the model package; folders from the layout; heartbeat; wheel-stand counts and cycle total into `site.yaml`; check whether `INPUT_IMG_DIR` is used; no business-logic change | Replay of saved images gives the same detections and defects as the old code; runs under the Supervisor (`restart_cost: high`, `queue_lag`); `INF` drains, cron removed |
| P8 | DB | **F13** CONTRACTS 5 and `db/README.md` name `health_status` for Tadipatri, and `db/README.md` (line 64) no longer describes the retired Basler XML element 16; **F8** reviewed upgrade script for `tadipatri4` to schema v1 (after a full backup) | Docs fixed; script reviewed by you |
| P9 | W-cleanup | Migrate cleanup: rendered JSON, folders from the layout, disk measured on `<root>/data` (**F17**) | Cleanup runs under the Supervisor on Tadipatri |
| P10 | You (Tadipatri) | **F14** find anything still using the old table name: `grep -rn tadipatri4_health_status_table ~/INSIGHTZZ --include=*.py --include=*.xml` | Nothing found outside `old/` and backups |
| P11 | SUPERVISOR | **8** model management: `model status / switch / rollback / pin / unpin`, `workers.yaml` `models` policy, events (D-056, D-060) | Switch to a test version and back without manual steps |
| P12 | new W-transfer | **9** OneDrive transfer service: weekly chunked upload, model download + verify + ready (D-057); Q18 settled while testing | Upload resumes after a cut; bad checksum rejected |
| P13 | W-inspection | **10** Auto-ROI in observe mode (D-054) | Learned file per side; nothing removed |
| F19 | SUPERVISOR (small) | Without `-c`, `supervisor.py reload` (and the other commands) use the example config in the code folder and look for the pid in `code/supervisor/runtime/`; on a site they should find `<root>/config/workers.yaml` and `<root>/run/` by themselves (e.g. default from `DPC_SITE_CONFIG`/`paths.root`, or the installed unit) | `python3 supervisor.py reload` works on Tadipatri without `-c` |
| F21 | SUPERVISOR + every worker | D-073 D7: a worker that dies writes `<root>/run/<worker_id>.last_error.json` (time, exception type, message, file:line, short traceback, no secrets); the Supervisor adds it as `last_error` to `worker_failure` / `worker_restarted`, to the worker status and to its log; append-only in CONTRACTS 2. Inspection does it in P7; captures with their next change | A forced error in a dummy worker shows its type and line in the MQTT event and in `supervisor.log` |
| F22 | W-inspection | P7 is built with fallbacks while P7-prep is not on `main` (MASTER, 2026-10-25): every new key missing from `inspection_<side>.json` uses a built-in per-side default equal to today's value (names exactly as in D-072/D-073/P7-prep), all in one place, with one WARNING per key at start. Remove the fallbacks once P7-prep is merged and Tadipatri renders the keys | No fallback WARNING on Tadipatri; fallback table deleted |
| F18 | SUPERVISOR (later) | `reload` does not re-read `workers.yaml`; a reload command over MQTT (`dpc/supervisor/command`) for the UI | When the UI work starts |
| P10b | W-inspection | Learned wheel-stand mapping (D-073 D8, D-054): learn per side which wheel numbers carry how many `wheel_stand`s over the first revolutions after a plate, write `data/state/inspection_<side>/wheel_stands.json`, then check each revolution against it; start in observe mode like auto-ROI (log what would be a defect, insert nothing) | Learned file written per side; observe log shows mismatches; nothing inserted |
| Later | various | Mail and PLC services migrated (PLC service D-024, light/dust/sunlight cases A8-A10), FLIR capture on JSON, UI monitoring, installer + wizard | - |

## Decisions waiting for you
| # | Question | Options |
|---|---|---|
| D1 (F15) | DECIDED 2026-10-24 (D-068): keep the capture retry limit; each inspection side follows its camera | - |
| D2 | The GitHub repo is public (code, docs, site reference values, model metadata). | (a) make it private; (b) keep public |

### Inspection decisions (from INSPECTION.md, P7-dec)
| # | Question | Options |
|---|---|---|
| D3 (R1, D-029) | DECIDED (D-073): one side writes it, `inspection.live_wheel_counter_side` (Tadipatri RIGHT) | - |
| D4 (R2) | DECIDED (D-073): keep, on both sides; time in config; crash fixed. Rule: every feature on both sides, single-side only by explicit config (D3) | - |
| D5 (R5, R9) | DECIDED (D-073): per side, each side keeps today's values | - |
| D6 (R6) | DECIDED (D-073): both sides insert their defects | - |
| D7 | DECIDED (D-073): exit and let the Supervisor restart; every restart carries its cause (`last_error`) | - |
| D8 | DECIDED (D-073): expected cycle total = 190 on both sides (= wheel count); wheel-stand mapping becomes a learned value (P10b) | - |
| D9 (Q21) | DECIDED (D-073): all three become defects (DB + mail), on both sides, following `docs/reference/inspection_defect_pattern_checkWheelMissing.py` | - |

## Automation cases (from the 2026-10-13 review)
| # | Case | What the system will do | Covered by |
|---|---|---|---|
| A1 | New camera inserted | Detect the serial, offer it in the wizard, write `site.yaml` | `dpc-check` (done), wizard (later) |
| A2 | Camera not connected | Reopen by itself, `NOK` health, alert | Reopen + NOK done; alert after the mail migration |
| A3 | Camera below the configured fps | Measure real fps, report, warn | Heartbeat `fps` (D-034) with P6 |
| A4 | Camera parameters changed | Read settings back and re-apply config values | P6 or later capture task |
| A5 | Inference delay | Detect a growing `INF` backlog | `queue_lag` (built), active with P7 |
| A6 | Model deployment | Fetch by version with checksum, switch, roll back | P11, P12 |
| A7 | Weekly data upload | Chunk images from both sides, upload weekly | P12 |
| A8 | Light off | Detect a dark view, switch the light on via PLC (`light_on_value`, D-064) | PLC service (later) |
| A9 | Sunlight on the view | Mask that region | Auto-ROI groundwork P13, then a config key |
| A10 | Dust on the view | Detect a dirty view, trigger cleaning | PLC service (later) |
| A11 | Other wrong-view causes | To be defined | - |
| A12 | Code error | Restart, MQTT event, mail alert | Restart done; alert after the mail migration |

## Open questions (details in DECISIONS)
Q6 (commented-out chain checks), Q7 (Maihar service users), Q10 (test machine for replay), Q11 (who reads `UI/` and `live/`), Q12 (in-place edits of hard-linked frames), Q13 (`WHEEL_STATUS` subfolders), Q14 (old defect records), Q18 (Graph admin consent, during P12), Q19 (existing DataSync).

## Known risks
- **Inspection findings (INSPECTION.md; nothing changed yet, bugs B1-B19 stay unless you ask):** Left inserts only one defect type (full-clip bolt missing); Right inserts none (insert commented out; its half-clip insert would crash); the half-clip check never runs with the Tadipatri model (needs `half_high_dusty_bucket_clip`, not among the 19 classes); chain checks detect nothing with this model (needs new code); `WHEEL_PRESENT_LIST` grows forever; wheel-missing goes quiet after the first revolution; Left's plate tracker and wheel counter assume opposite directions (to confirm by replay).
- Nothing drains `INF` except the temporary cron job until P7 (inspection) runs: check P1.
- The camera settings exist twice (XML and `site.yaml`) until P6; only the XML is used by the captures today.
- The only copies of the model weights are on the Tadipatri machine (original folder in `~/INSIGHTZZ/old/` and the package): keep a backup until OneDrive (P12).
- Inspection geometry and object sizes must match unrotated 1920x1080 frames (rotation is 0 at Tadipatri): verify in P7's replay.
