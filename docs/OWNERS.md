# Folder ownership

One owner per folder. A chat changes only the folder it owns. Anything that crosses folders (MQTT topics, config keys, DB columns, file paths) is a contract: MASTER changes `docs/CONTRACTS.md` first.

| Folder | Owner chat | Others may |
|---|---|---|
| `docs/` (including `COMMISSIONING.md`, `STATUS.md`, `CLAUDE_PROJECT_SETUP.md`) | MASTER | read; SITE chats edit only their entry in `SITES.md` |
| `db/` (`schema.sql`, `fixed_rows.sql`, `migrations/`) | MASTER (DB contract, D-043) | read; worker chats request DB changes in their handoff |
| `supervisor/` | SUPERVISOR | read |
| `common/` | SUPERVISOR (MQTT helpers), CONFIG (config loader, path helpers) | import, never edit |
| `workers/frame_capture_basler/` | W-frame_capture_basler | read |
| `workers/frame_capture/` (legacy FLIR dual-camera) | W-frame_capture | read |
| `workers/inspection/` (Left + Right) | W-inspection (one task per file, never both at once) | read |
| `workers/mail/` | W-mail | read |
| `workers/cleanup/` | W-cleanup | read |
| `workers/plc/` | W-plc | read |
| `ui/` | UI | read; DB, files and MQTT as per contracts |
| `config/` | CONFIG | read |
| `scripts/` | CONFIG | run |
| `tests/unit`, `tests/golden` | the owner of the code under test | read |
| `tests/replay` | W-inspection | read |

Rules
1. One task per chat; one branch per task.
2. If you need a change in another folder, write it down as a request in the handoff; do not edit it.
3. A contract change needs a DECISIONS entry (append-only, never edit history).
4. Hand-off files (`INF`, `UI`, `live`) are read-only for everyone except FrameCapture (D-031).
