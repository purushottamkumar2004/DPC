# DPC Platform

Industrial DPC (Drive/Drag Pulley Chain) machine-vision inspection: capture, Detectron2 inspection, defects to MySQL, mail alerts, PLC light/cleaning, disk cleanup, all supervised by one Supervisor process.

```
docs/          architecture, contracts, decisions, code audit, chat workflow, owners, sites (START HERE)
common/        shared code: config loader, MQTT helpers, path helpers            (planned)
supervisor/    generic worker manager (built, tested with dummy workers)
workers/
  frame_capture_basler/   FrameCapture_Basler.py: single camera, atomic hand-off
  frame_capture/          legacy dual-camera FLIR/Basler FrameCapture.py
  inspection/             LeftAlgorithm.py, RightAlgorithm.py
  mail/  cleanup/  plc/
ui/            Django UI / API                                                    (to move in)
config/        schema/, renderer/, examples/ (site.example.yaml)                  (planned)
scripts/       setup_dpc_layout.sh
tests/         unit/, golden/, replay/
secrets.env.example
```

## Machine layout (new machines)
Run `bash scripts/setup_dpc_layout.sh` as your normal user (no sudo). It creates `~/INSIGHTZZ` with `code/ config/ logs/ run/ data/` (see `docs/ARCHITECTURE.md` section 9). Clone this repo into `~/INSIGHTZZ/code/`: the git root is `code/`, so `data/`, `logs/`, `config/` and `run/` sit outside the repo and git never sees them (the `.gitignore` also lists them as a safety net).

## Status
- Worker code under `workers/` other than `frame_capture_basler/` is the production code of the Maihar site, copied as is (only `cleanup/delete_script.py` reads its Microsoft Graph credentials from `DPC_*` environment variables).
- `frame_capture_basler/` is the new single-camera version with atomic hand-off (D-031); the legacy FLIR version still copies in place.
- `*.example.*` files are real configs with secrets and recipients replaced by placeholders; XML tag order is unchanged because the programs read XML **by position**.
- Real configs, `site.yaml` and `secrets.env` stay on the machine and are git-ignored.

## Releases
One `main` branch for all sites; releases are tags (`v0.<n>`). A site pins a tag (`git checkout v0.<n>`), and sites differ by config in `~/INSIGHTZZ/config`, never by branch (D-039). The tag per site is recorded in `docs/SITES.md`.

## Before first use
1. Rotate the Microsoft Graph client secret and the DB passwords (they were in plaintext in the old files and in chats). Use a dedicated DB user, not `root`.
2. Create `~/INSIGHTZZ/config/secrets.env` from `secrets.env.example` (chmod 600).
3. Read `docs/CODE_AUDIT.md` and `docs/CHAT_WORKFLOW.md`.

## Not committed on purpose
Models (`*.pth`), images, logs, real configs, `site.yaml`, secrets.
