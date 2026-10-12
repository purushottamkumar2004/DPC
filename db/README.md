# Database seed (D-043)

Goal: one set of SQL files that builds the **whole DPC database structure** for a new site.
Today you run them by hand in MySQL Workbench; later `scripts/dpc_db_init.py` runs the same files automatically (installer, D-035).

## Files
| File | What it does | When to run |
|---|---|---|
| `schema.sql` | Every table the DPC programs use, as `CREATE TABLE IF NOT EXISTS`. No `USE`, no database name, no site data, no passwords. | New site: once, on an empty schema |
| `fixed_rows.sql` | Rows the programs expect to already exist, because they only `UPDATE` them: `wheel_counter_table` ID 1, `CAMERA_PLC_STATUS` id 1. `INSERT IGNORE`, safe to run twice. | New site: after `schema.sql` |
| `site_rows.example.sql` | Template for site-specific rows: one health row per camera serial. Copy, fill the serials, run. Never commit the filled copy. | New site: after `fixed_rows.sql` |
| `migrations/NNNN_<what>.sql` (from `0002`) | One file per change, numbered, append-only. Brings an **existing** site DB up to the current `schema.sql`. | Existing sites, in number order |
| `reference/<site>/schema_<date>.sql` | Structure-only export of a live site DB, kept as a record. Never run on a new site. | Read when reconciling `schema.sql` |
| `schema_version` table | Created by `schema.sql`; each migration inserts its number. Shows which changes a site has. | Read it to see where a site stands |
| `test_seed.py` | Builds a scratch DB `dpc_seed_test` from the files above (twice), runs every DB statement the repo programs use, and checks the reference coverage, CONTRACTS section 5 and "no site values". Never touches a site DB. | After every change in `db/` |

## New site in MySQL Workbench
1. Create an empty schema with the site's DB name and select it (double-click it so it is bold).
2. Open and run `schema.sql`, then `fixed_rows.sql`.
3. Copy `site_rows.example.sql`, put the site's camera serials in, run it.
4. Create the site's DB user with rights on this schema only (see `CLAUDE_PROJECT_SETUP.md` / `secrets.env`), never `root`.

## Not in here
- Django's own tables (`auth_*`, `django_*` and the UI app tables): created by `python manage.py migrate`.
- Data. The seed creates structure and the fixed rows only.

## Keeping it up to date (the rule)
Any change that adds or changes a table or column is a **contract change**:
1. The worker chat that needs it writes the request in its handoff (it does not edit `db/`).
2. MASTER, in one commit: updates `schema.sql`, adds the next `migrations/NNNN_<what>.sql`, updates `CONTRACTS.md` section 5, adds a DECISIONS entry if a column is renamed or removed (CONTRACTS section 8: columns are append-only).
3. `schema.sql` must always equal "the oldest schema plus every migration". A fresh site and an upgraded site end up identical.

## Status
Schema version 1, reconciled with the live Tadipatri database (2026-10-12): `schema_version`, `defects_table`, `repeated_defects_table`, `wheel_counter_table`, `health_status`, `CAMERA_PLC_STATUS` (now with the `PLC` lamp column).
Designed from CONTRACTS section 5 and the queries in the code, then checked table by table against `reference/tadipatri/`.
Tested on MySQL 8.0 with `test_seed.py`: all files run twice without error and every DB statement in the workers runs (algorithm insert and dedupe, repeated-defect lookup, wheel counter update, both mail senders' select/dedupe/update, FLIR and Basler health updates, camera lamps).

Run the test (needs `pip install pymysql` and a MySQL user allowed to create and drop the scratch database):
```bash
DPC_TEST_DB_USER=<user> DPC_TEST_DB_PASS=<password> python3 db/test_seed.py
```

## Left out
Tables found in a live site database that the seed does **not** create. `test_seed.py` fails if a table in `reference/` is neither in `schema.sql` nor listed here.
Rule: a table is seeded only when a program in this repo reads or writes it. When its program moves into the repo, the table is added to `schema.sql` through a migration, together with CONTRACTS section 5.

| Table (Tadipatri) | Why it is left out | Comes in when |
|---|---|---|
| `auth_*`, `django_*` | Django's own tables; `python manage.py migrate` creates them. | The UI is installed |
| `repeated_defect_table` | Old singular name. The code queries `repeated_defects_table` (plural), which the seed creates. The algorithms' lookup is disabled today, so nothing reads either table. | Never under this name; see site notes |
| `tadipatri4_health_status_table` | Tadipatri's own health table. New sites use `health_status` (same columns plus a unique `ITEM`). | Never; Tadipatri keeps it (site notes) |
| `HEALTH_STATUS` | Old table (`DEVICE_NAME`, `STATUS`), different from `health_status` (`ITEM`, `HEALTH`). No writer or reader in the repo. Names are case-sensitive on Linux, so the two can exist side by side. | Its program is found and moved into the repo |
| `LOGIN` | Old UI login with a **plain-text** `PASSWORD` column. Django's `auth_user` (hashed passwords) replaces it. New sites do not get it. | Never (proposed decision, see STATUS) |
| `PROCESS_UPTIME_REPORT`, `UPTIME_TABLE` | Written by the uptime report, which is not in the repo. | The uptime report moves into the repo |
| `system_health_status` | Disk and process status; writer not in the repo (likely the old health/uptime script). | Its writer moves into the repo |
| `cycle_time_table`, `dpc_status_table` | Cycle time and running status (single row); writer not in the repo. | Its writer moves into the repo |
| `live_images` | Image list for the old UI; writer not in the repo. | The UI or its writer moves into the repo |
| `mail_status`, `shutdown_status` | Mail and shift-shutdown bookkeeping; writer not in the repo (another mail or report script). | Its writer moves into the repo |

## Site notes
Differences between a live site database and the seed. The seed is for new sites; these sites keep their database as it is.

**Tadipatri (`reference/tadipatri/schema_2026-10-12.sql`)**
- Health table is `tadipatri4_health_status_table`. Both `BASLER_CONFIG*.xml` must set element 16 `HEALTH_TABLE` to that name; the code default (`tadipatri_health_status_table`, D-041) does not exist there. The table has one row per camera only if someone inserted it; check with `SELECT ITEM FROM tadipatri4_health_status_table;` and insert a row per serial if missing (the capture only updates).
- `repeated_defect_table` (singular) instead of `repeated_defects_table`. Harmless while the lookup is disabled; before it is enabled at Tadipatri, create the plural table from `schema.sql`.
- `defects_table` has no review columns (`reviewed_by`, `reviewed_at`, `review_status`) and no indexes. The workers do not need them; the UI review page will.
- `CAMERA_PLC_STATUS.id` has no auto-increment and there is no `UPDATE_DATETIME`; nothing in the repo depends on either.
- Collation `utf8mb4_0900_ai_ci` (MySQL 8 default); the seed uses `utf8mb4_unicode_ci` so it also runs on MariaDB.
- No `schema_version` table: Tadipatri is "before version 1". Bringing it to version 1 is a separate, reviewed script, run with a backup, when the UI or DataSync needs it.
