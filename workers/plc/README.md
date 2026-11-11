# PLC service (STATUS "PLC service", D-024, automation cases A8 and A10)

The one program on a site that talks to the PLC (Modbus TCP). It replaces the one-shot `light.py` and `cleaning_mechanism.py` with the same writes and the same pulse. Settings come only from `<root>/config/rendered/plc.json` (env `DPC_WORKER_CONFIG`, CONTRACTS 13).

Run: `python3 plc_worker.py` (working directory `workers/plc`), as Supervisor worker `plc` (`mode: service`, `restart_cost: normal`).
Tests (repo root; a fake Modbus client, plus pymodbus's own local server when pymodbus is installed): `python3 -m unittest discover -s workers/plc/tests -t . -v`

## What it does
| Case | Behaviour | Old program |
|---|---|---|
| Light (A8) | Writes `light_on_value` (1 = ON at Tadipatri) to `light_register` at start and after every reconnect; reads it every `light_check_seconds` (60) and rewrites only when it differs | `light.py`: one write of 1 at start |
| Cleaning (A10) | On a request: 1 to `cleaning_register`, wait `cleaning_pulse_seconds` (10), 0. The 0 is retried until written; a pulse cut by a crash is closed at the next start (`data/state/plc/pulse_open.json`); SIGTERM ends a running pulse at once | `cleaning_mechanism.py`: 1, sleep 10 s, 0, run by hand |
| Conveyor status | Reads `conveyor_status_register` every `status_interval_seconds` (10); raw value in the MQTT status; a change is logged | declared, never read |
| PLC lamp | `CAMERA_PLC_STATUS.PLC` = 'True'/'False' (PLC answers), on change and every 120 s; only with a `db` block | nobody wrote it |

Cleaning requests: `dpc/plc/command` `{"action":"clean_pulse","source":..,"reason":..}` (manual; also `python3 plc_worker.py --clean-pulse --reason "..."`), always accepted when no pulse runs and the PLC answers. `poor_view` events on `dpc/worker/+/event` are used only with `plc.auto_cleaning: true` (default false) and after `cleaning_cooldown_seconds` (600) since the last pulse.

Safety: only `light_register` and `cleaning_register` can be written (any other register raises and stops the program); `light_register == cleaning_register` is a config error. An unreachable PLC is not an error: it is logged once (then every 10 min), retried with backoff (1, 2, 5, 10, 30 s), and shown as `connected: false`.

MQTT: `dpc/plc/status` (retained, every status interval), heartbeat on `dpc/worker/plc/health` (healthy while the program runs, even with the PLC down), events `light_on_written`, `cleaning_pulse_started`, `cleaning_pulse_ended` on `dpc/worker/plc/event`. Broker down: the service runs on.

## Test functions (STATUS S5, wizard step 4, `plc_test.py`)
They talk to the PLC directly (not through the service). Add `--json` for the UI.
| Command | Does | Writes |
|---|---|---|
| `python3 plc_worker.py --check` | reads the light, cleaning and conveyor registers that are given; `light_is_on` | nothing |
| `python3 plc_worker.py --light on\|off --seconds N` (default 5, max 600) | switches the light, holds N s, then writes `light_on_value` again | the light register only |
| `python3 plc_worker.py --pulse` | one pulse: 1, `cleaning_pulse_seconds` (max 120), 0 | the cleaning register only |

Settings: `plc.json` (env `DPC_WORKER_CONFIG`) when present, every value overridable, so the wizard can test before `site.yaml` exists: `--type --ip --port --timeout-s --light-register --light-on-value --cleaning-register --conveyor-status-register --cleaning-pulse-seconds`. There are no built-in IP or register values for the tests; a missing one is a bad request.
Example: `python3 plc_worker.py --light off --seconds 5 --ip 192.168.1.5 --light-register 4109 --json`

Result JSON: `ok`, `status`, `action`, `message`, `plc {type, ip, port}`, `settings_from`; check adds `registers {light|cleaning|conveyor_status: {register, value}}`, `light_on_value`, `light_is_on`; light and pulse add `writes [{register, value, reason, written, read_back}]`, `held_full_time`, `restored` (light) / `ended` (pulse), `duration_s`.
Exit codes: 0 ok, 1 failed or restore_failed, 2 bad_request, 3 unreachable.
The light is always put back to `light_on_value` and cleaning back to 0, also on SIGTERM / Ctrl-C during the wait (retried for 15 s); if that fails the status is `restore_failed` and the message says the light may be OFF / cleaning may be ON. Test writes are logged to `<root>/logs/plc_test.log` when the logs folder exists.
If the PLC service is running at the same time, it puts the light back within `light_check_seconds`, and a test pulse ends any running service pulse; use the tests in setup mode (D-103) or with the service stopped.
`--clean-pulse` is different: it asks the RUNNING service for a pulse over MQTT.

Crash: config error (including a missing `plc.json`) or an error escaping the loop -> `<root>/run/plc.last_error.json`, exit 1 (D-073 D7, D-079). SIGTERM/SIGINT: exit 0. Log: `<root>/logs/plc.log`, midnight rotation, `log.backup_count` files; every write at INFO with register, value and reason.

Missing keys inside `plc.json` get the Tadipatri values (`plc_settings.DEFAULTS`) with one warning each. A missing file stops the program, so another site's PLC is never written with Tadipatri registers.

| File | What |
|---|---|
| `plc_worker.py` | entry, loop, CLI (`--check`, `--light`, `--pulse`, `--clean-pulse`), exit codes |
| `plc_test.py` | the wizard test functions (S5) |
| `plc_settings.py` | JSON reader, defaults, validation |
| `modbus_link.py` | the one Modbus client: reconnect, backoff, write gate |
| `plc_service.py` | light, pulse, conveyor status (tick based) |
| `plc_mqtt.py` | status, heartbeat, events, commands |
| `plc_db.py` | `CAMERA_PLC_STATUS.PLC` lamp |

`light.py` and `cleaning_mechanism.py` are the old programs; they are deleted after the site check.
