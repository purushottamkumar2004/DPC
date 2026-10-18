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

dpc/worker/plc_service/health      (worker -> supervisor, only for health.type: heartbeat)
{"worker_id":"plc_service","healthy":true}

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
