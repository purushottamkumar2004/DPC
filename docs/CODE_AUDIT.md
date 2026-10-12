# Code audit: what the real code does today, and what blocks productization

Based on the uploaded `dpc_code_dat` (Maihar site) files. Severity: **P0** fix first, **P1** before multi-site, **P2** improvement.

## 0. Corrections to earlier assumptions
| Earlier assumption | What the code shows |
|---|---|
| Left and Right algorithms are essentially identical | Right is 3243 lines vs Left 2527. Beyond camera/path names they differ in ~1,375 lines: Right-only `saveAndInsertDefect`, `update_wheel_count` (writes `wheel_counter_table`); Left-only `checkCroppedImg`, `extract_time_from_filename`; `checkCycleRestartDefect` default total 65 (Left) vs 188 (Right); `insertDefectData` signature differs. **Merging needs a real diff review plus replay tests, not a rename.** |
| Light and cleaning are services | `light.py` and `cleaning_mechanism.py` are **one-shot scripts**: connect, write, exit. Supervisor V1 treats any exit as a failure and would restart them until FAILED. |
| FrameCapture is the quad FLIR script | The deployed `FrameCapture.py` is an older single-file version: FLIR or Basler by config, one thread for both cameras, DB health updates. The FLIR quad rewrite is a separate newer program. Stop/signal behaviour must be checked per version. |

## 1. Security (P0)
- A **Microsoft Graph client secret and tenant ID are hardcoded** in `delete_script.py` and also in `MAIL_SERVICE_CONFIG.xml`. DB passwords are in plaintext in every XML. Real recipient emails are in the mail XML. **Rotate the secret and DB passwords**, move them to an environment file (chmod 600), and never paste them into the Project or chats again.
- `immediate_mail_sender.py` has SMTP login (Gmail) with `SENDER_PASSWORD` read from XML; also a code bug: `.text.strip` without `()` stores the method, not the string.
- SQL is built with f-strings (`insertDefectData`, `checkIsDefectAlreadyGenerated`, health updates). Inputs are internal, but use parameterized queries when touched.

## 2. Hardcoded site values in code (P0/P1)
- `LeftAlgorithm.py` / `RightAlgorithm.py` hardcode `dailyDataDir` (RAW_IMAGES), `PLATE_IMG_OUTPUT_DIR`, `bolt_save_dir` to `/home/ddpc_maihar/...` (11 and 13 hardcoded path lines) instead of the config. `RAW_IMAGE_DIR_PATH` in the XML is not what is used for the move.
- `WHEEL_STAND_REQUIRED_COUNTS = {34:3, 52:2, 82:1, 169:1, 210:1}` and `expected_total` (65 / 188) are site-specific constants inside code; `TOTAL_WHEEL_COUNT` in XML is 190 (Maihar) / 170 (Bela).
- `leftConfig.xml` `INPUT_IMG_DIR` still points to `/home/dpc_bela/...` (stale copy from another site).
- `FRAME_CAPTURE_CONFIG.xml`: `CAM_2_RAW_IMG_PATH` points to `CAM1/RAW_IMAGES` (looks like a copy-paste mistake).
- PLC IPs hardcoded in `light.py` (192.168.2.5, **the correct one for this server**) and `cleaning_mechanism.py` (192.168.1.4, stale) while the algorithm XML says 192.168.1.5 with DB numbers 12/13 (S7-style) that these Modbus scripts do not use. Three different values for "the PLC".
- `FrameCapture.py` has 5 hardcoded path lines (e.g. Django TMP path).

## 3. Configuration fragility (P1)
All XML is read **by position** (`config_root[0][8]`, `[3][5]` ...). Adding, removing or reordering a tag silently breaks or shifts values. Config is loaded from `os.getcwd()`, so the working directory matters (the Supervisor must set it). `ALL_CLASS_NAMES` uses `ast.literal_eval`.

## 4. Runtime behaviour that matters for the Supervisor (P0 for design)
- **INF folder is a queue**: FrameCapture writes `INF/<ts>.jpg` per frame; the algorithm takes the oldest, then moves it to RAW_IMAGES. Queue lag (age of the oldest file in INF) is the best "algorithm is keeping up" signal.
- On every (re)start of `mainFunction` the algorithm **deletes all images in INF**, resets `wheel_counter`, plate state and histories, and **skips frames until a plate is seen**. Any exception inside `mainFunction` has the same effect (the outer `main()` loop reconnects DB and re-enters). So each restart can blind inspection for up to one revolution. Avoid unnecessary restarts; consider persisting wheel/plate state.
- Frame loop is polling (`sleep(2)` when no files); no per-frame heartbeat or health write exists for the algorithm. FrameCapture does write DB health (`health_status`, `CAMERA_PLC_STATUS`, every 120 s threshold).
- `CAMERA_PLC_STATUS` mapping looks inverted: `update_camera1_health` sets `RIGHT CAMERA`, `update_camera2_health` sets `LEFT CAMERA`, while CAM1 feeds the Left algorithm. **Verify.**
- Defect dedupe: DB lookup (same wheel, type, side, bbox width/height within +-50 px, last 24 h). `repeated_defects_table` lookup is disabled (`NonRepeatedDefectList = []`). Several checks are commented out (chain nut/bolt, wheel bolt missing, bucket clip gap); `checkCycleRestartDefect` saves the image but its DB insert is commented out.
- `delete_script.py` measures disk usage of `os.getcwd()`, i.e. the disk where it is started, not necessarily the image disk. Deletion order depends on thresholds (delete 75 %, raw 85 %, mail 90 %, inference 95 %); it loops every 10 s and sends a daily 9 AM disk mail through MS Graph.
- Two mail senders exist: SMTP (Gmail) and Outlook via MS Graph. Mail dedupe has two layers (batch + DB `is_alert_sent=1` in 24 h).
- PLC scripts: no retry, no connection close, errors swallowed, `readFromPlc` crashes if connect failed. **"Light ON 24x7" currently relies on one write**; if the PLC restarts the light stays off and nobody is told. Register semantics are ambiguous in comments (light register 4109: "0 on / 1 off" in the comment but the script writes 1).

## 4a. Database findings (from the exported schemas)
- `defects_table.wheel_no` is `varchar(45)` while the code builds SQL as `wheel_no = {int}`; works through MySQL conversion but prevents proper indexing/ordering. Keep the type (other services use it), convert in code.
- `defects_table` carries columns written by other services: `is_data_sync`, `is_image_sync` (DataSync), `review_status` default PENDING, `reviewed_by/at`, `defect_checkbox` (review workflow in the UI). **Any change to the table must be checked against DataSync and Django.**
- The 24 h dedupe query filters on `create_datetime, wheel_no, defect_type, defect_side`; check there is a composite index, since it runs on every defect candidate.
- `wheel_counter_table` is a single row (`ID=1`, `WHEEL_COUNT varchar(45)`), updated by `RightAlgorithm` only (call in the main loop). Left never updates it.
- PLC: this server's PLC is `192.168.2.5` (confirmed). `cleaning_mechanism.py` points to `192.168.1.4`, so **cleaning currently talks to a different address than the light** and may be failing silently (errors are swallowed). Verify on the machine.

## 4b. Observability gaps (P1)
Logs are per script with different rotation; no common run id; no metrics (FPS, queue lag, inference time, GPU use); no central view.

## 5. Productization backlog (ordered)
> **Superseded (D-040):** the current order is in `docs/STATUS.md` ("Next"); later additions are in section 6. The items below remain a useful list of work, not the current priority order.
1. Rotate secrets; introduce env-file secrets. (P0)
2. Add Supervisor `mode: oneshot | service | periodic` and `queue_lag` / `db_freshness` health types; migrate delete script, mail, FrameCapture first (low risk), algorithms last.
3. Replace hardcoded paths in algorithms with config values (small, safe edit). Move wheel-stand counts and expected totals to config.
4. Central **site config** + a **renderer that writes the legacy XML files** (exact tag order, golden-file tests). Old code keeps working, UI can edit config.
5. PLC service: one process owning Modbus, periodic light re-assert/verify, cleaning on `poor_view` event with cooldown, conveyor-status read; one IP per site in config.
6. Persist wheel counter/plate state so a restart does not cost a revolution.
7. Replay test harness (saved image sequences with expected defects) before any Left/Right merge.
8. Merge Left/Right into one codebase with `side` parameter, guided by the diff above.
9. Setup wizard + self-test, then monitoring UI on MQTT status/events.
10. Central cloud dashboard aggregating many sites (later).

## 6. Basler FrameCapture review (Tadipatri) and v3 notes
Reviewed: the single-camera Basler `FrameCapture` (now `workers/frame_capture_basler/FrameCapture_Basler.py`). Severity as above.

**Fixed in the new version (D-031):** frames were written in place and copied into INF and UI (partial files visible, two writes per frame); config path depended on the working directory (now `BASLER_CONFIG` env override); paths now come from config with a staging folder.

**Still open:**
| Sev | Finding | Fix |
|---|---|---|
| P0 | `BASLER_CONFIG1.xml` holds a plaintext DB login for the `root` MySQL user (the password was also pasted into a chat). | Rotate; dedicated DB user with limited rights; `secrets.env` (D-018). |
| P1 | `updateHealthStatus` updates a table literally named `tadipatri_health_status_table`. | Table name from config (D-038). |
| P1 | If `IsGrabbing()` is false the loop reuses the previous `grabResult` and republishes the same frame. | Only publish a grab retrieved in this iteration. |
| P1 | The camera restart (`self.initCam()` after `while True`) is unreachable. A lost camera ends in 5 s retrieve timeouts forever, with no recovery and no health signal. | Device-lost handling with reopen; health via heartbeat or `file_activity` on `live/IMG_1.jpg`. |
| P1 | The health update is commented out, so camera health never reaches the DB or the Supervisor. | Heartbeat (CONTRACTS section 2 proposal) once the Supervisor supports it. |
| P2 | `AcquisitionFrameRateAbs` and `ExposureTimeAbs` are older Basler node names; newer cameras use `ExposureTime` and `AcquisitionFrameRate`. A failure is caught by the outer handler, so the camera may run with partial settings. | Verify on the camera model; set nodes by feature name with a log line per setting. |
| P2 | `CAM_SERIAL_NO` is reused for the serial, the device info and the camera object. | Rename for readability when touched. |

**FLIR `FrameCapture.py` (Maihar) still has:** in-place writes (non-atomic hand-off), 5 hardcoded path lines including a path inside the Django UI code folder (`TMP.jpg`), and a possible timer bug in the incomplete-frame branch (CAM1 resets `last_health_update` inside the `if`, CAM2 resets `last_health_update_2` outside it; confirm against the full code).

**Backlog additions (v3), in order:** (a) config loader and `site.yaml` schema (CONFIG); (b) Supervisor `mode`, `queue_lag`, heartbeat extension; (c) `frame_capture_basler` fixes from the table above, then a FLIR per-camera version with the same hand-off; (d) renderer and `workers.yaml` generation; (e) UI reads `ui_live/` and the status topics; (f) commissioning wizard and golden image.
