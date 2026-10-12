# Project status

Owned by MASTER; update after each handoff. Last updated: 2026-10-11.

## Done
- Architecture v3, contracts v3, decisions D-001 to D-039, code audit (incl. Basler section), ownership, chat workflow, commissioning checklist.
- Deployed layout script (`~/INSIGHTZZ`: code, config, logs, run, data) and the Basler single-camera FrameCapture with atomic hand-off (tested with stubbed camera libraries; not yet on the real camera).
- Tadipatri machine: layout created, repo at `~/INSIGHTZZ/code`, pushed to GitHub (`purushottamkumar2004/DPC`). Two Basler cameras detected: serials 24824427 and 24874775.

## In progress
- Tadipatri first real capture test (CAM1, then CAM2): config files, run, check `live`, `INF`, `_tmp`, log. Side of each camera (LEFT/RIGHT) still to confirm.
- Repo hygiene to confirm: repository set to **Private**; tag `v0.1` visible on GitHub; no credentials in history.

## Next (suggested order)
1. CONFIG chat: `site.yaml` schema and loader, with tests.
2. W-frame_capture_basler: reopen the camera when lost, publish only fresh grabs, health table name and log name per camera to config, heartbeat.
3. SUPERVISOR chat: `mode`, `queue_lag`, heartbeat extension, start-after rule.
4. CONFIG chat: `dpc-check` preflight (cameras, disk and layout first), then the wizard.
5. `install.sh` (layout, clone at a tag, venv, deps, UI, model download, then wizard).
6. UI chat: monitoring page, then configuration page.

## Waiting for a decision
- Wizard writes a file first (`site.yaml`, D-033) or the DB first (the install-flow sketch)? Proposed: file first, DB master when the UI edits config; workers always boot from rendered local files (D-034).
- Where the global model is hosted and how installs fetch it (checksum).
- How a new machine gets the private repo (read-only deploy key, or a release bundle).
- Open questions Q11 to Q17 in `DECISIONS.md`.

## Known risks
- Root DB login and a plaintext password in the old Basler config (rotate; dedicated DB user).
- Legacy algorithms and the FLIR FrameCapture still hardcode paths (D-023); they cannot use the new layout yet.
- Two cameras currently share one log file; run one at a time until fixed.
