#!/usr/bin/env python3
"""Test the DB seed (D-043) against a real MySQL 8 server.

What it proves (STATUS item 2b "done when"):
  (a) schema.sql, fixed_rows.sql and a filled site_rows file run twice without error
  (b) every DB statement the repo programs run works against the result
  (c) every table of each db/reference/<site>/ dump is seeded or listed as left out in db/README.md
  (d) every table and column of schema.sql is named in docs/CONTRACTS.md section 5
  (e) the seed files contain no database name, site name or camera serial

Usage (needs a MySQL user that may CREATE/DROP a database):
  pip install pymysql
  DPC_TEST_DB_USER=... DPC_TEST_DB_PASS=... python3 db/test_seed.py [--host localhost] [--keep]
Without DPC_TEST_DB_USER it connects as the current OS user over the local socket.
It creates and drops its own scratch database `dpc_seed_test`; it never touches a site DB.
Exit code 0 = all checks passed, 1 = at least one failed.
"""
import argparse
import glob
import os
import re
import sys

import pymysql
from pymysql.constants import CLIENT

DB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(DB_DIR)
TEST_DB = "dpc_seed_test"
FAKE_SERIALS = ("TESTSERIAL1", "TESTSERIAL2")   # never real serials

failures = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def seed_files():
    """The files a new site runs, in order (site_rows filled with fake serials)."""
    site_rows = read(os.path.join(DB_DIR, "site_rows.example.sql"))
    site_rows = site_rows.replace("<CAM1_SERIAL>", FAKE_SERIALS[0]).replace("<CAM2_SERIAL>", FAKE_SERIALS[1])
    files = [("schema.sql", read(os.path.join(DB_DIR, "schema.sql"))),
             ("fixed_rows.sql", read(os.path.join(DB_DIR, "fixed_rows.sql"))),
             ("site_rows (filled)", site_rows)]
    for path in sorted(glob.glob(os.path.join(DB_DIR, "migrations", "[0-9][0-9][0-9][0-9]_*.sql"))):
        files.append((os.path.basename(path), read(path)))
    return files


def connect(args, db=None):
    user = os.environ.get("DPC_TEST_DB_USER") or os.environ.get("USER")
    kwargs = dict(user=user, password=os.environ.get("DPC_TEST_DB_PASS", ""), database=db,
                  client_flag=CLIENT.MULTI_STATEMENTS, cursorclass=pymysql.cursors.DictCursor,
                  autocommit=True)
    if args.host in ("localhost", "") and not os.environ.get("DPC_TEST_DB_PASS"):
        kwargs["unix_socket"] = args.socket
    else:
        kwargs["host"] = args.host
    return pymysql.connect(**kwargs)


def run_script(conn, sql):
    """Run a whole .sql file the way Workbench does (several statements)."""
    with conn.cursor() as cur:
        cur.execute(sql)
        while cur.nextset():
            pass


# --- (a) ------------------------------------------------------------------
def test_seed_runs_twice(conn):
    for attempt in (1, 2):
        for name, sql in seed_files():
            try:
                run_script(conn, sql)
                check(True, f"(a) run {attempt}: {name}")
            except Exception as e:
                check(False, f"(a) run {attempt}: {name}: {e}")


# --- (b) ------------------------------------------------------------------
def test_program_queries(conn):
    """Each statement copied in shape from the program that runs it."""
    cur = conn.cursor()

    def run(what, sql, params=None, rows=None, keys=()):
        try:
            n = cur.execute(sql, params)
            data = cur.fetchall() if cur.description else []
            ok = rows is None or n == rows
            missing = [k for k in keys if data and k not in data[0]]
            check(ok and not missing, f"(b) {what}" + (f" (rows {n}, expected {rows})" if not ok else "")
                  + (f" (missing keys {missing})" if missing else ""))
            return data
        except Exception as e:
            check(False, f"(b) {what}: {e}")
            return []

    # inspection: LeftAlgorithm / RightAlgorithm insertDefectData (values quoted as text, as in the code)
    run("algorithm INSERT defects_table",
        "insert into defects_table(defect_side, defect_type, view_defect, wheel_no, xmin, xmax, ymin, ymax) "
        "values('Right','bolt_missing','/x/1.jpg','12', '10', '60', '20', '90')", rows=1)
    # inspection: 24 h dedupe (wheel_no compared as an integer, D-028)
    run("algorithm dedupe SELECT",
        "SELECT * FROM defects_table WHERE create_datetime > NOW() - INTERVAL 1 DAY AND wheel_no = 12 "
        "AND defect_type = 'bolt_missing' AND defect_side = 'Right' "
        "AND (60 - 10) BETWEEN 0 AND 100 AND (90 - 20) BETWEEN 20 AND 120", rows=1)
    run("algorithm SELECT repeated_defects_table", "select * from repeated_defects_table order by ID desc;")
    # inspection: RightAlgorithm.update_wheel_count
    run("RightAlgorithm UPDATE wheel_counter_table", "UPDATE wheel_counter_table SET WHEEL_COUNT = %s WHERE ID = 1",
        ("57",), rows=1)
    # mail senders (DictCursor)
    unsent = run("mail SELECT unsent", "SELECT * FROM defects_table WHERE is_alert_sent = 0;", rows=1,
                 keys=("ID", "defect_type", "defect_side", "wheel_no", "create_datetime", "xmin", "xmax", "ymin", "ymax"))
    if unsent and "id" not in unsent[0]:
        print("NOTE immediate_mail_sender reads defect['id'] from SELECT *; the column is ID, so that key does "
              "not exist (KeyError). Mail worker issue, not a schema issue (see handoff).")
    for col in ("id", "ID"):   # immediate_mail_sender uses id, outlook sender uses ID
        run(f"mail dedupe SELECT {col}",
            f"SELECT {col} FROM defects_table WHERE create_datetime > NOW() - INTERVAL 1 DAY AND wheel_no = 12 "
            "AND defect_type = 'bolt_missing' AND defect_side = 'Right' AND is_alert_sent = 1 "
            "AND (60 - 10) BETWEEN 0 AND 100 AND (90 - 20) BETWEEN 20 AND 120 LIMIT 1;")
    run("mail mark sent (id IN)", "UPDATE defects_table SET is_alert_sent = 1 WHERE id IN (%s);", (1,), rows=1)
    run("mail mark ignored (ID IN)", "UPDATE defects_table SET is_alert_sent = 2 WHERE ID IN (%s);", (1,), rows=1)
    # FLIR capture: health and lamps
    run("FLIR UPDATE health_status", f"UPDATE health_status SET health = 'True', update_datetime = NOW() "
        f"WHERE item = '{FAKE_SERIALS[0]}'", rows=1)
    run("FLIR UPDATE CAMERA_PLC_STATUS RIGHT", "UPDATE CAMERA_PLC_STATUS SET `RIGHT CAMERA` = %s WHERE id = 1",
        ("True",), rows=1)
    run("FLIR UPDATE CAMERA_PLC_STATUS LEFT", "UPDATE CAMERA_PLC_STATUS SET `LEFT CAMERA` = %s WHERE id = 1",
        ("True",), rows=1)
    run("UI read CAMERA_PLC_STATUS lamps", "SELECT `RIGHT CAMERA`, `LEFT CAMERA`, PLC FROM CAMERA_PLC_STATUS WHERE id = 1",
        rows=1)
    # Basler capture: health (table name from HEALTH_TABLE = health_status)
    run("Basler UPDATE health_status", "UPDATE `health_status` SET HEALTH = %s, IS_DATA_SYNC = %s, "
        "CREATE_DATETIME = NOW() WHERE ITEM = %s", ("OK", 0, FAKE_SERIALS[1]), rows=1)
    # (MySQL counts CHANGED rows: an UPDATE that writes the same values returns 0)
    run("schema_version is 1", "SELECT MAX(version) AS v FROM schema_version HAVING v >= 1", rows=1)
    cur.close()


# --- (c) (d) (e): static checks on the files -------------------------------
def table_names(sql):
    return re.findall(r"^CREATE TABLE (?:IF NOT EXISTS )?`?(\w+)`?", sql, re.M)


def schema_columns(sql):
    """{table: [columns]} from schema.sql (column lines start with a name and a type)."""
    result = {}
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", sql, re.S):
        cols = []
        for line in body.splitlines():
            m = re.match(r"\s*(`[^`]+`|\w+)\s+(INT|VARCHAR|DATETIME|TINYINT)", line, re.I)
            if m:
                cols.append(m.group(1).strip("`"))
        result[table] = cols
    return result


def test_reference_covered():
    seeded = set(table_names(read(os.path.join(DB_DIR, "schema.sql"))))
    readme = read(os.path.join(DB_DIR, "README.md"))
    left_out = readme.split("## Left out", 1)[1].split("\n## ", 1)[0] if "## Left out" in readme else ""
    for ref in sorted(glob.glob(os.path.join(DB_DIR, "reference", "*", "schema_*.sql"))):
        site = os.path.basename(os.path.dirname(ref))
        for t in table_names(read(ref)):
            django = t.startswith(("auth_", "django_")) and "`auth_*`" in left_out
            check(t in seeded or f"`{t}`" in left_out or django,
                  f"(c) {site}.{t} is seeded or listed under 'Left out' in db/README.md")


def test_contract_matches():
    contracts = read(os.path.join(REPO, "docs", "CONTRACTS.md"))
    section5 = contracts.split("## 5.", 1)[1].split("\n## 6.", 1)[0]
    for table, cols in schema_columns(read(os.path.join(DB_DIR, "schema.sql"))).items():
        check(f"`{table}`" in section5, f"(d) CONTRACTS 5 names table {table}")
        missing = [c for c in cols if c.lower() not in section5.lower()]
        check(not missing, f"(d) CONTRACTS 5 names every column of {table}" + (f" (missing {missing})" if missing else ""))


def test_no_site_values():
    paths = [os.path.join(DB_DIR, n) for n in ("schema.sql", "fixed_rows.sql", "site_rows.example.sql")]
    paths += glob.glob(os.path.join(DB_DIR, "migrations", "*.sql"))
    for p in paths:
        text = read(p)
        bad = re.findall(r"\b\d{8}\b|\bUSE\s+\w+|tadipatri4?\b(?!_)|maihar|mahiar|IDENTIFIED BY", text, re.I)
        # site names may appear in comments that explain a live difference, never in statements
        statements = "\n".join(l.split("--")[0] for l in text.splitlines())
        bad_stmt = re.findall(r"\b\d{8}\b|\bUSE\s+\w+|tadipatri|maihar|mahiar|IDENTIFIED BY", statements, re.I)
        check(not bad_stmt, f"(e) {os.path.relpath(p, REPO)} has no site value in a statement"
              + (f" (found {bad_stmt})" if bad_stmt else ""))
        if bad and not bad_stmt:
            print(f"     note: {os.path.basename(p)} names a site only in comments: {sorted(set(bad))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--socket", default="/var/run/mysqld/mysqld.sock")
    ap.add_argument("--keep", action="store_true", help="keep the scratch database for inspection")
    args = ap.parse_args()

    server = connect(args)
    with server.cursor() as cur:
        cur.execute("SELECT VERSION() AS v, @@lower_case_table_names AS l")
        info = cur.fetchone()
        print(f"server {info['v']}, lower_case_table_names={info['l']}")
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    try:
        conn = connect(args, TEST_DB)
        test_seed_runs_twice(conn)
        test_program_queries(conn)
        conn.close()
    finally:
        if not args.keep:
            with server.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        server.close()
    test_reference_covered()
    test_contract_matches()
    test_no_site_values()

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
