# DPC Supervisor V2

A generic manager for independent worker processes (cameras, algorithms, PLC,
sync, reports, mail...). It only knows **command, health, restart policy**.
All business logic stays inside the workers.

## Architecture

```
                    supervisor.py
                         |
     +-------------------+--------------------+
     |                   |                    |
ConfigurationManager  ProcessManager       Monitor ----> RecoveryManager
 (workers.yaml)       (Popen, stop,        (every 5 s,    (retry, delay,
                       status)              health checks) FAILED)
     |                   |                    |
RuntimeManager           v                    |
 (create folders)   Worker processes <--------+
                    FrameCapture / LeftAlgorithm / PLC / ...
                         |
     MQTT: status, events, heartbeats, commands
```

Local calls stay local (`process_manager.restart_worker(w)`); MQTT is only used
for status, events, heartbeats and external commands.

## Structure

```
supervisor/
  supervisor.py              main + command line
  config/workers.yaml        THE place where workers are added
  core/config.py             ConfigurationManager
  core/process_manager.py    start / stop / restart / is_running / get_status
  core/runtime_manager.py    creates worker folders
  core/monitor.py            periodic checks, tolerant failure detection
  core/recovery_manager.py   retry policy, FAILED state
  communication/mqtt_client.py
  health/base_health_check.py   HealthCheck interface
  health/basic_health_check.py  process / file_activity / heartbeat / queue_lag
  health/heartbeat.py           heartbeat payloads (old and extended, D-034)
  core/site_env.py              env bridge: DPC_* variables from site.yaml (CONTRACTS 9)
  workers/dummy_worker.py    test worker
  logs/                      supervisor.log + logs/workers/<id>.log (worker stdout/stderr)
```

## Run

```bash
pip install -r requirements.txt
sudo apt install mosquitto        # optional - supervisor works without a broker

python3 supervisor.py check-config          # also checks site.yaml if a worker has env_from_site
python3 supervisor.py check-config --site-config ~/INSIGHTZZ/config/site.yaml
python3 supervisor.py run         # Ctrl+C stops the supervisor and all workers

# from another terminal (needs the broker):
python3 supervisor.py status
python3 supervisor.py restart left_algorithm
python3 supervisor.py stop    left_algorithm
python3 supervisor.py start   left_algorithm
```

For production use `dpc-supervisor.service` (systemd). Workers are stopped when
the supervisor stops.

## Adding a new worker

```yaml
- id: new_algorithm
  type: algorithm
  command: python3 NewAlgorithm.py
  working_directory: /path/to/code
```

## Health checks

| type | meaning | needs change in the script? |
|---|---|---|
| `process` | process is alive | no |
| `file_activity` | file/folder modified within `max_age_seconds` | no |
| `heartbeat` | worker publishes `{"healthy": true}` to `dpc/worker/<id>/health` | yes (later) |

New check type: subclass `HealthCheck`, add it to `build_health_check()`.

## MQTT messages

```
dpc/worker/left_algorithm/status   (retained, supervisor -> all)
{"worker_id":"left_algorithm","type":"algorithm","status":"RUNNING","pid":18231,
 "started_at":"2026-10-06T14:32:10","restart_count":0,"exit_code":null,
 "log_file":"logs/workers/left_algorithm.log","message":"","timestamp":"2026-10-06T14:35:00"}

dpc/worker/left_algorithm/event    (supervisor -> all)
{"worker_id":"left_algorithm","event":"worker_failure","reason":"unhealthy for 10s: ...","timestamp":"..."}
{"worker_id":"left_algorithm","event":"worker_restarted","retry":1,"pid":18302,"timestamp":"..."}
{"worker_id":"left_algorithm","event":"worker_failed","reason":"... (gave up after 3 retries)","timestamp":"..."}

dpc/worker/inspection_left/health  (worker -> supervisor, only for health.type: heartbeat / require_heartbeat)
{"worker_id":"inspection_left","healthy":true}

dpc/worker/left_algorithm/command  (anyone -> supervisor)
{"command":"restart","reason":"no_frames"}      # start | stop | restart | status
```

## Failure / recovery flow (real output of the test run)

```
14:33:21 [hanger] health check failed: heartbeat.txt not updated ... (will re-check)
14:33:26 [hanger] still failing (5s/10s)
14:33:31 [hanger] failure: unhealthy for 10s  -> stopping pid=92 with SIGTERM
14:33:31 [hanger] restart 1/3 in 5s            (status WAITING_RESTART)
14:33:36 [hanger] restarting (attempt 1)       pid=94
14:33:41 [hanger] healthy -> RUNNING

crashed process:  exit code 1 -> restart 1/3 -> 2/3 -> 3/3 -> FAILED (event worker_failed)
```

A worker that stays healthy for `reset_after_seconds` (default 5 min) gets its
retry counter back, so one crash per day never ends in FAILED.

## Things to know before connecting the real code

* **FrameCapture.py only cleans up on Ctrl+C** (`KeyboardInterrupt`), so use
  `stop_signal: SIGINT` for it (already set in the example).
* It starts **all cameras in one process**. The example health check watches every
  camera's `IMG_1.jpg`, so one stuck camera is detected. Its own per-camera
  reconnect logic keeps working untouched.
* `QUAD_FLIR_CONFIG.xml` contains DB passwords in plain text. The supervisor never
  reads it; keep it out of git.
* Verify the log-file paths used for the algorithm health checks on each site.

## V2: modes, start order, restart cost, env bridge (STATUS item 3)

| key | values | meaning |
|---|---|---|
| `mode` | `service` (default) | runs forever; any exit is a failure |
| | `oneshot` | runs once; exit 0 = STOPPED, not restarted; exit != 0 = failure (retry policy, then FAILED) |
| | `periodic` + `interval_s` | runs every `interval_s` s (start to start, no overlap); STOPPED between runs with `next_run_at` in the status; a failed run is a `worker_failure` event and the next run is the retry (never FAILED) |
| `restart_cost` | `normal` / `high` | `high` must fail `monitoring.high_cost_timeout_seconds` (default 120) instead of `worker_timeout_seconds`. A dead process is still restarted at once. |
| `start_after` | `[ids]` | started only when those are ready (service RUNNING, oneshot/periodic finished with exit 0). After `monitoring.start_after_timeout_seconds`, or when a dependency is FAILED, it starts anyway with a warning and `start_after_unmet` in the `worker_started` event. Disabled ids are ignored; loops and unknown ids are config errors. Only applies at supervisor start; a `start` command overrules it. |
| `env_from_site` | `true` / `false` | exports `DPC_DB_USER`, `DPC_DB_PASS`, `DPC_MS_*`, `DPC_MAIL_SENDER`, `DPC_SMTP_PASSWORD`, `DPC_DISK_REPORT_TO`, `DPC_MQTT_USER/PASS`, `DPC_SITE_NAME` from site.yaml. Read at every start of the worker (rotate a password, then `restart <id>`). Values are never logged or published; a bad site.yaml fails that worker only. `env:` in workers.yaml wins. |
| `health.type: queue_lag` | `path`, `max_oldest_age_seconds`, `require_heartbeat`, `max_age_seconds` | oldest `*.jpg` in the folder(s) must be younger than the limit; empty folder = healthy. With `require_heartbeat: true` a fresh healthy heartbeat is needed too (D-049). |

Heartbeat: `{"healthy": true}` and the extended payload (`pid, version, config_hash, fps, last_frame_age_s, inf_queue_depth`) are both accepted; the known fields are type-checked and shown under `heartbeat` in the worker status. `{"healthy": false}` makes the check fail at once (the timeout still applies).

Tests: `python3 -m unittest tests.unit.test_supervisor_v2 -v` from the repo root (about 30 s).

## Running on a site (STATUS item 4)

1. Workers file: copy `config/workers.basler-2cam.yaml` to `~/INSIGHTZZ/config/workers.yaml`
   (two Basler captures and two inspection sides; every setting comes from `site.yaml` through the
   rendered `config/rendered/<id>.json`). From then on `supervisor.py` finds it without `-c`.
   `env:` values that start with `~/` are expanded, like every path in the file.
2. Check: `python3 supervisor.py check-config`
3. Boot service: `./install_service.sh --dry-run` shows the filled unit, `./install_service.sh` installs and
   enables it. Run it as the site user, from the Python environment that has pypylon (`--python` to choose).
   `dpc-supervisor.service` is a template (`@USER@`, `@ROOT@`, `@PYTHON@`, `@PYTHON_DIR@`); do not copy it by hand.
4. Logs: `journalctl -u dpc-supervisor -f`, `~/INSIGHTZZ/logs/supervisor.log`, `~/INSIGHTZZ/logs/workers/<id>.log`.

## P5: rendered configs, reload, follows, run timeout, MQTT login

* **Rendered configs (6f, D-062).** At start the Supervisor runs the renderer (`site.yaml` ->
  `<root>/config/rendered/<id>.json`) and gives every worker that has a file `DPC_WORKER_CONFIG`.
  `supervisor.py reload` (or `systemctl reload dpc-supervisor`) renders again and restarts only the
  workers whose file was written. `restart_cost: high` workers wait for `monitoring.reload_quiet_window`
  ("HH:MM-HH:MM") or `reload --confirm`; until then their status shows `config_pending`. A `site.yaml`
  error changes nothing: the old files stay and the error is printed, logged and published
  (`dpc/supervisor/event`). The command talks to the running Supervisor through SIGHUP and
  `<run>/reload_request.json` / `reload_result.json`, so it works without MQTT.
* **`follows: <worker id>` (D-068).** Start only when that worker is RUNNING; paused (STOPPED, `paused_by`)
  when it goes from RUNNING to WAITING_RESTART / UNHEALTHY / FAILED / STOPPED; started once when it is
  RUNNING again. Planned restarts and the retries of one outage pause nothing extra. Operator start/stop wins.
* **`run_timeout_seconds` (F10)**, oneshot/periodic only: a longer run is stopped and counts as a failed run.
* **MQTT login (F11):** from `site.yaml` `mqtt.user` / `mqtt.password`; `workers.yaml` keeps
  `enabled, host, port, topic_prefix` (a login there is a config error).

## Without -c, and why a worker stopped (F19, F21)

* **Finding the files (F19).** Without `-c`, every command (`run`, `check-config`, `reload`, `status`,
  `start`, `stop`, `restart`) uses `<root>/config/workers.yaml`, the folder that holds `site.yaml`
  (`--site-config`, then `$DPC_SITE_CONFIG`, then `~/INSIGHTZZ/config/`). The pid and reload files are
  in that file's `runtime.root` (`<root>/run`). A site folder without `workers.yaml` is an error; the
  example with dummy workers is used only where `<root>/config` does not exist (development).
* **last_error (F21, D-073 D7).** A worker that exits on an error writes
  `<root>/run/<id>.last_error.json` `{at, type, message, where, traceback}`. When the process ends the
  Supervisor reads it (only if written during that run), keeps the five fields (cut short), replaces
  the `site.yaml` secret values by `***`, and adds it as `last_error` to `worker_failure`, the
  `worker_restarted` that follows, and the worker status; `supervisor.log` gets
  `[id] last error: ValueError: ... (file.py:123)` plus the traceback lines.
  `runtime.root` must be `<root>/run` for the file to be found (it is in `workers.basler-2cam.yaml`).

## dpc/supervisor/status: only the running Supervisor writes it (F28)

The Supervisor's own MQTT client (`announce_presence=True`) sets the last-will "offline", publishes
"online" (retained) on every connect and again every `status_publish_interval_seconds`, and "offline"
when it stops. CLI clients (`status`, `start`, `stop`, `restart`) have no last-will and never publish
this topic, so `supervisor.py status` no longer marks a running Supervisor offline, and a CLI call
while it is down does not mark it online. A stale "offline" is corrected within one status interval.

## Models (P11; D-056, D-060, D-061, D-086)

Per model **name** (a site may run its own model per side). State in `<root>/data/models/<name>/`:
`current` (symlink to the running version), `pinned` and `previous` (one line: a version name); only the
Supervisor's `model` commands write them (first install: `scripts/make_model_package.py`, D-061).

```
python3 supervisor.py model status [--json]                  sides, current, previous, pinned, installed (ready or not)
python3 supervisor.py model switch <name> <version> [--confirm]
python3 supervisor.py model rollback <name> [--confirm]       back to `previous`; both versions stay on disk
python3 supervisor.py model pin <name> <version>              never switched automatically until unpin
python3 supervisor.py model unpin <name>
```

`switch` runs in the command's own process, so the Supervisor keeps watching the workers meanwhile:
1. refuses (and changes nothing) when the model is not used on this site, the version name is invalid,
   the model is pinned to another version, or the version is already current;
2. `verify_package` (manifest, every sha256, classes): a broken package is refused **before** any GPU
   work or restart;
3. the self-test (`workers/inspection/model_selftest.py --json`, F47) with the inspection workers' Python,
   on the RAW_IMAGES of the first side of that model that has frames, with `inspection.threshold`;
   exit 1 = refused, with its reason;
4. `previous` := old current, `current` := new (atomic);
5. asks the running Supervisor (reload channel, works without MQTT) to restart **only the inspection sides
   that use this model**, once: now with `--confirm` or inside `monitoring.reload_quiet_window`, else they
   are `config_pending` until then (`supervisor.py reload --confirm` restarts them). Without a running
   Supervisor the sides load the new version when they start.

`rollback` = the same without the self-test (that version ran here before). A pinned model refuses
`switch` and `rollback` to another version. One model command at a time (`<root>/run/model.lock`).
Events on `dpc/supervisor/event`: `model_switched`, `model_rolled_back` {model, from, to, sides, workers,
restarted, pending, not_running}, `model_switch_failed` {model, action, to, reason}, `model_pinned`
{model, version, previous_pin, current}, `model_unpinned` {model, version}. Published best effort: the
change is done even when the broker is down.
`workers.yaml` `models: {auto_update: off|download|install, check_interval_hours: 6}`: only `off` is acted
on today; the transfer service (P12) uses the others, never for a pinned model.

## For the UI (F59, D-095, D-096)

* `python3 supervisor.py status --json` prints `{"supervisor": <dpc/supervisor/status or null>, "workers":
  {<id>: <status, same keys as the MQTT status>}}`; `model status --json` prints `{"auto_update", "models"}`.
  In `--json` mode an error is one object `{"error": "..."}` (stdout stays pure JSON; remarks go to stderr).
  Exit 0 = read; 1 = broker or Supervisor not reached; 2 = configuration error. The human table is unchanged.
* Workers with `enabled: false` get a retained status `DISABLED` ("disabled in workers.yaml"), sent with the
  periodic "online" once MQTT is connected, so the UI does not need to read workers.yaml.
* The `ui` worker (two-camera file) gets `DPC_WORKER_CONFIG=<root>/config/rendered/ui.json` like any worker.
