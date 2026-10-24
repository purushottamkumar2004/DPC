# Project status

Owned by MASTER; update after each handoff. Last updated: 2026-10-22 (full clean-up; the history of every finished item is in git and DECISIONS).

## Current state (summary)
1. **Goal:** one codebase for every site; a person edits only `site.yaml`; programs run under the Supervisor and recover by themselves.
2. **Tadipatri** (not in production yet): both Basler captures run under the Supervisor as a boot service, recover from cable and network loss, report health to `health_status` and status over MQTT (local mosquitto). Inspection, mail, PLC and cleanup are not running yet.
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

## Pending, in order (do one at a time)
| # | Owner | What | Done when |
|---|---|---|---|
| P1 (DONE 2026-10-24: service active, FAILED = 0, both captures started by the Supervisor and delivering frames, `INF` cron installed) | You (Tadipatri) | Check the service-run basics: `systemctl status dpc-supervisor` active; `grep -c FAILED ~/INSIGHTZZ/logs/supervisor.log` = 0; the `INF` cron entry exists (`crontab -l`) so the disk does not fill | All three confirmed |
| P2 (DONE 2026-10-24: real values filled from the reference file, developer confirmed) | You (Tadipatri) | Fill the real `site.yaml` from `docs/sites/tadipatri.site.reference.yaml` (DB password, Graph values, mail lists), chmod 600, run the checker | Checker `OK` (root warning only) |
| P3 (DONE 2026-10-24: `models/dpc_maskrcnn/2026.10.12/` on main; both small files match the manifest sha256) | You + MASTER | Copy the model's small files (`manifest.json`, `classes.json`, `.yaml`) into the repo at `models/dpc_maskrcnn/2026.10.12/` (D-065) | Files on `main`; no `.pth` in git |
| P4 (DONE, PR #12, 197 tests pass; Tadipatri reference rendered: 4 written, then 4 unchanged) | CONFIG | One task: **6e** renderer (`site.yaml` -> `config/rendered/<worker_id>.json`, CONTRACTS 13, D-062, D-063), **6g** keys `cameras[].ip` and `plc.light_on_value` (D-064), **6h** `make_model_package.py --from-repo` / `--export-to-repo` (D-065) | Tadipatri: 4 JSON files rendered; second run `unchanged`; tests pass |
| P4b | CONFIG (small) | D-067: add `data/images/TEMP/LEFT` and `RIGHT` to the layout (CONTRACTS 4.1), `scripts/setup_dpc_layout.sh` and the `dpc-check` layout check | `setup_dpc_layout.sh` creates them; `dpc-check` layout counts them |
| P4c (DONE 2026-10-24: renderer on the real site.yaml, 4 written then 4 unchanged; `--export-to-repo` worked; machine back on `main`, clean) | You (Tadipatri) | `git pull`, then try the renderer once: `python3 scripts/render_config.py ~/INSIGHTZZ/config/site.yaml` (writes 4 files to `~/INSIGHTZZ/config/rendered/`, nothing reads them yet); optional: add `ip:` to both cameras and `light_on_value: 0` under `plc` in `site.yaml` | 4 `written`; a second run says 4 `unchanged` |
| P5 | SUPERVISOR | **6f** `reload` and render at start, `DPC_WORKER_CONFIG` per worker, restart only changed workers; plus **F10** `run_timeout_seconds`, **F11** MQTT login from `site.yaml`, **F15** retry rule decided in D1 | Edit CAM2 exposure, `reload`: only CAM2 restarts |
| P6 | W-frame_capture_basler | **7b** capture reads its rendered JSON (no XML); carry over the deprecation fix and `health_interval_s` from branch `feat/frame_capture_basler-robustness` (then delete it); **F7** health "matched no row" uses `CLIENT.FOUND_ROWS` | Both Tadipatri captures run from JSON; `BASLER_CONFIG*.xml` no longer read |
| P7 | W-inspection | **7** Left/Right read their rendered JSON and the model package; folders from the layout; heartbeat; wheel-stand counts and cycle total into `site.yaml`; check whether `INPUT_IMG_DIR` is used; no business-logic change | Replay of saved images gives the same detections and defects as the old code; runs under the Supervisor (`restart_cost: high`, `queue_lag`); `INF` drains, cron removed |
| P8 | DB | **F13** CONTRACTS 5 and `db/README.md` name `health_status` for Tadipatri; **F8** reviewed upgrade script for `tadipatri4` to schema v1 (after a full backup) | Docs fixed; script reviewed by you |
| P9 | W-cleanup | Migrate cleanup: rendered JSON, folders from the layout, disk measured on `<root>/data` (**F17**) | Cleanup runs under the Supervisor on Tadipatri |
| P10 | You (Tadipatri) | **F14** find anything still using the old table name: `grep -rn tadipatri4_health_status_table ~/INSIGHTZZ --include=*.py --include=*.xml` | Nothing found outside `old/` and backups |
| P11 | SUPERVISOR | **8** model management: `model status / switch / rollback / pin / unpin`, `workers.yaml` `models` policy, events (D-056, D-060) | Switch to a test version and back without manual steps |
| P12 | new W-transfer | **9** OneDrive transfer service: weekly chunked upload, model download + verify + ready (D-057); Q18 settled while testing | Upload resumes after a cut; bad checksum rejected |
| P13 | W-inspection | **10** Auto-ROI in observe mode (D-054) | Learned file per side; nothing removed |
| Later | various | Mail and PLC services migrated (PLC service D-024, light/dust/sunlight cases A8-A10), FLIR capture on JSON, UI monitoring, installer + wizard | - |

## Decisions waiting for you
| # | Question | Options |
|---|---|---|
| D1 (F15) | A capture with `max_retries: 30` ends FAILED after about 20-25 min without the camera and then needs a manual start. Should cheap-to-restart workers (captures) retry forever? | (a) retry forever for captures; (b) keep the limit |
| D2 | The GitHub repo is public (code, docs, site reference values, model metadata). | (a) make it private; (b) keep public |

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
- Nothing drains `INF` except the temporary cron job until P7 (inspection) runs: check P1.
- The camera settings exist twice (XML and `site.yaml`) until P6; only the XML is used by the captures today.
- The only copies of the model weights are on the Tadipatri machine (original folder in `~/INSIGHTZZ/old/` and the package): keep a backup until OneDrive (P12).
- Inspection geometry and object sizes must match unrotated 1920x1080 frames (rotation is 0 at Tadipatri): verify in P7's replay.
