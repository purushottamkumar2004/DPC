# Migrations

`schema.sql` is version 1. Changes start at `0002_<what>.sql`, one file per change, never edited after it is merged.
Each file: the `ALTER`/`CREATE` statements, then `INSERT INTO schema_version (version, description) VALUES (<n>, '<what>');`.
A fresh site runs `schema.sql`, `fixed_rows.sql`, its `site_rows` file and then every migration in number order; an existing site runs only the migrations it does not have yet (`SELECT MAX(version) FROM schema_version;`).
