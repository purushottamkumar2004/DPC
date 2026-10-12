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
| `schema_version` table | Created by `schema.sql`; each migration inserts its number. Shows which changes a site has. | Read it to see where a site stands |

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
Schema version 1 is written (2026-10-12): `schema_version`, `defects_table`, `repeated_defects_table`, `wheel_counter_table`, `health_status`, `CAMERA_PLC_STATUS`.
Designed fresh from CONTRACTS section 5 and the queries in the code, not copied from an old database.
Tested on MariaDB: all three files run twice without error, and every DB query found in the workers (algorithm insert and dedupe, mail sender select/update, wheel counter update, FLIR and Basler health updates, camera lamps) runs against it.
