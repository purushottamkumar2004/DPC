# Cleanup worker (STATUS P9)

Keeps `<root>/data` from filling the disk. Same rules as the old Maihar `delete_script.py`; settings come only from `<root>/config/rendered/cleanup.json` (env `DPC_WORKER_CONFIG`, CONTRACTS 13), folders from the layout (CONTRACTS 4.1), disk measured on `<root>/data` (F17).

Run: `python3 cleanup.py` (working directory `workers/cleanup`), as Supervisor worker `cleanup` (`mode: service`, `restart_cost: normal`).
Tests (repo root, no network, temp folders only): `python3 -m unittest discover -s workers/cleanup/tests -t . -v`

## Folders and rules
| Folder | Rule | Runs when disk % is |
|---|---|---|
| `data/frames/<CAM>/RAW_IMAGES`, `data/bolt_data/<SIDE>` | frame-name time older than `raw_image_hours` | > `raw_delete` |
| `data/frames/<CAM>/INF` | every file | > `inference_delete` |
| `data/images/REPEATED/<SIDE>` | per date folder at most `repeated.days` old: keep the newest `repeated.items` files (recursive, by mtime) | > `delete` |
| `data/images/PLATE/<SIDE>`, `data/images/MAGNET/<SIDE>` | frame-name time older than `plate_days` (MAGNET: time after the first `_`, date subfolders) | > `delete` |
| `data/images/DEFECTIVE/<SIDE>`, `data/images/RAW/<SIDE>` | per date folder at most `defective.days` old: keep the newest `defective.items` files (top level only) | > `delete` |

Order per pass: raw, INF, repeated, plate/magnet, defective/defect RAW. Never touched: `live`, `_tmp`, `UI`, `ui_live`, `state`, `models`, `wheel_status`, `images/TEMP`; every folder must lie inside `<root>/data` (checked at start).
Date folders older than their `days` are never deleted (old behaviour).

Mails (Microsoft Graph, `graph` + `mail` from the JSON): threshold alert once when disk % >= `mail` (re-armed below it); daily reminder at `disk_report_time` (default 09:00) while >= `mail`.

Crash: config error or an error escaping the loop -> `<root>/run/cleanup.last_error.json`, exit 1 (D-073 D7). SIGTERM: exit 0. Log: `<root>/logs/cleanup.log`, 200 MB or midnight rotation, `log.backup_count` days; one `Deletion run:` line per pass with files and MB per folder.

| File | What |
|---|---|
| `cleanup.py` | entry, pass order, mails, summary line, exit codes |
| `cleanup_settings.py` | JSON reader, fallbacks (Tadipatri values, one warning per key), folder safety check |
| `cleanup_rules.py` | the deletion rules |
| `disk_mail.py` | Graph sender and the two mail texts |
| `rotating_log.py` | size-and-midnight log rotation |
| `cleanup_last_error.py` | `last_error.json` writer (copy of inspection's) |

`delete_script.py` and `deleter_config.example.xml` are the old program; deleted after the P9 site check.
