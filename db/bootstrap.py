#!/usr/bin/env python3
"""Create a site's database from nothing, or check it again (STATUS S3, D-103 step 2 / Apply).

With MySQL admin credentials it:
  1. creates the site database if it is missing,
  2. creates the program user (one per host, default localhost and 127.0.0.1) and gives it
     ALL PRIVILEGES on that database only, so the programs no longer need root (D-050),
  3. loads db/schema.sql and db/fixed_rows.sql (tables, wheel_counter_table row 1,
     CAMERA_PLC_STATUS row 1),
  4. inserts one health_status row per camera serial (INSERT IGNORE),
  5. logs in as the program user to prove the password and the rights work.

Idempotent: a second run changes nothing and reports every step as "already there".
An existing user keeps its password unless reset_password is set; step 5 then tells you
whether the given password still works.
Passwords are never printed, logged or put in the report (also not inside error texts).

Function:  bootstrap(BootstrapRequest(...)) -> dict  (used by the UI wizard, D-103)
CLI:       python3 db/bootstrap.py --db <name> --user <program user> --serial <s1> --serial <s2> [--json]
           Passwords come from the environment (DPC_DB_ADMIN_PASS, DPC_DB_PASS) or a prompt,
           never from the command line (it would show in `ps` and the shell history).
Exit code: 0 = every step ok, 1 = a step failed (the report says which).
"""

import argparse
import dataclasses
import getpass
import json
import os
import re
import sys
from typing import Callable, List, Optional, Sequence

DB_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ("schema.sql", "fixed_rows.sql")          # run in this order (D-043)

# MySQL limits: database name 64 chars, user name 32 chars. We allow only plain
# identifiers so names never need quoting tricks and cannot inject SQL.
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
USER_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_.:%-]{1,255}$")
FORBIDDEN_PROGRAM_USERS = {"root", "mysql.sys", "mysql.session", "mysql.infoschema", "debian-sys-maint"}

CREATED, ALREADY, UPDATED, OK, FAILED = "created", "already there", "updated", "ok", "failed"


class BootstrapError(ValueError):
    """Wrong input; nothing was changed."""


@dataclasses.dataclass
class BootstrapRequest:
    admin_user: str
    admin_password: str
    db_name: str
    program_user: str
    program_password: str
    serials: Sequence[str]
    host: str = "localhost"
    port: int = 3306
    unix_socket: Optional[str] = None                 # admin over the socket (e.g. root with auth_socket)
    program_hosts: Sequence[str] = ("localhost", "127.0.0.1")
    reset_password: bool = False                       # set the given password on an existing user

    def secrets(self) -> List[str]:
        return [s for s in (self.admin_password, self.program_password) if s]


def validate(req: BootstrapRequest) -> None:
    """Refuse bad input before touching the server."""
    if not NAME_RE.match(req.db_name or ""):
        raise BootstrapError("database name: use 1-64 letters, digits or _")
    if req.db_name.lower() in {"mysql", "information_schema", "performance_schema", "sys"}:
        raise BootstrapError(f"database name {req.db_name!r} is a MySQL system database")
    if not USER_RE.match(req.program_user or ""):
        raise BootstrapError("program user: use 1-32 letters, digits or _")
    if req.program_user.lower() in FORBIDDEN_PROGRAM_USERS or req.program_user == req.admin_user:
        raise BootstrapError(f"program user {req.program_user!r} must be a new, dedicated user, "
                             "not root or the admin user (D-050)")
    if not req.program_password:
        raise BootstrapError("program password is empty")
    if not req.program_hosts or not all(HOST_RE.match(h or "") for h in req.program_hosts):
        raise BootstrapError("program hosts: give at least one host (e.g. localhost)")
    serials = [str(s).strip() for s in req.serials]
    if not serials or any(not s or len(s) > 64 for s in serials):
        raise BootstrapError("camera serials: give at least one, each 1-64 characters")
    if len(set(serials)) != len(serials):
        raise BootstrapError("camera serials: the same serial is given twice")


# --------------------------------------------------------------------------- SQL files
def split_sql(text: str) -> List[str]:
    """Split a .sql file into statements: ';' ends a statement unless it is inside
    quotes or backticks; '--' and '#' comments run to the end of the line."""
    statements, buf, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote != "`" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            buf.append(ch)
        elif text.startswith("--", i) or ch == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def read_script(name: str) -> List[str]:
    with open(os.path.join(DB_DIR, name), encoding="utf-8") as f:
        return split_sql(f.read())


# --------------------------------------------------------------------------- report
class Report:
    def __init__(self, secrets: Sequence[str]):
        self.steps = []
        self._secrets = [s for s in secrets if s]

    def redact(self, text) -> str:
        text = str(text)
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def add(self, step: str, item: str, result: str, detail: str = "") -> None:
        self.steps.append({"step": step, "item": item, "result": result, "detail": self.redact(detail)})

    @property
    def ok(self) -> bool:
        return all(s["result"] != FAILED for s in self.steps)


# --------------------------------------------------------------------------- steps
def _connect(connect, req: BootstrapRequest, user: str, password: str, database=None, as_admin=True):
    kwargs = dict(user=user, password=password, port=int(req.port), autocommit=True, connect_timeout=10)
    if database:
        kwargs["database"] = database
    if as_admin and req.unix_socket:
        kwargs["unix_socket"] = req.unix_socket
    else:
        kwargs["host"] = req.host
    return connect(**kwargs)


def _grant_db_pattern(db_name: str) -> str:
    """In GRANT, '_' and '%' in a database name are wildcards; escape them so the
    user gets this one database, not every database whose name matches."""
    return db_name.replace("_", r"\_").replace("%", r"\%")


def step_database(cur, req, report):
    cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (req.db_name,))
    if cur.fetchone():
        report.add("database", req.db_name, ALREADY)
        return
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{req.db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    report.add("database", req.db_name, CREATED)


def step_user(cur, req, report, host):
    account = f"'{req.program_user}'@'{host}'"
    account_p = account.replace("%", "%%")    # pymysql formats with %, so a '%' host must be doubled
    cur.execute("SELECT 1 FROM mysql.user WHERE User = %s AND Host = %s", (req.program_user, host))
    if not cur.fetchone():
        cur.execute(f"CREATE USER {account_p} IDENTIFIED BY %s", (req.program_password,))
        report.add("user", f"{req.program_user}@{host}", CREATED)
    elif req.reset_password:
        cur.execute(f"ALTER USER {account_p} IDENTIFIED BY %s", (req.program_password,))
        report.add("user", f"{req.program_user}@{host}", UPDATED, "password set")
    else:
        report.add("user", f"{req.program_user}@{host}", ALREADY, "password not changed")

    pattern = _grant_db_pattern(req.db_name)
    cur.execute(f"SHOW GRANTS FOR {account}")
    grants = " ".join(str(list(row.values())[0] if isinstance(row, dict) else row[0]) for row in cur.fetchall())
    if f"ALL PRIVILEGES ON `{pattern}`.*" in grants:
        report.add("grant", f"{req.program_user}@{host} on {req.db_name}", ALREADY)
    else:
        cur.execute(f"GRANT ALL PRIVILEGES ON `{pattern}`.* TO {account}")
        report.add("grant", f"{req.program_user}@{host} on {req.db_name}", CREATED)


def _tables(cur) -> set:
    cur.execute("SHOW TABLES")
    return {str(list(r.values())[0] if isinstance(r, dict) else r[0]) for r in cur.fetchall()}


def step_scripts(cur, req, report):
    before = _tables(cur)
    for name in SCRIPTS:
        for stmt in read_script(name):
            cur.execute(stmt)
            m = re.match(r"INSERT\s+IGNORE\s+INTO\s+`?(\w+)`?", stmt, re.I)
            if m:   # fixed rows: rowcount 1 = inserted, 0 = the row was there
                report.add("fixed row", f"{m.group(1)} row 1", CREATED if cur.rowcount else ALREADY)
    after = _tables(cur)
    for name in read_schema_tables():
        report.add("table", name, ALREADY if name in before else (CREATED if name in after else FAILED))
    cur.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    version = (list(row.values())[0] if isinstance(row, dict) else row[0]) if row else None
    report.add("schema version", str(version), OK if version else FAILED)


def read_schema_tables() -> List[str]:
    tables = []
    for stmt in read_script("schema.sql"):
        m = re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?(\w+)`?", stmt, re.I)
        if m:
            tables.append(m.group(1))
    return tables


def step_health_rows(cur, req, report):
    for serial in req.serials:
        serial = str(serial).strip()
        cur.execute("INSERT IGNORE INTO health_status (ITEM, HEALTH) VALUES (%s, 'NOK')", (serial,))
        report.add("health row", serial, CREATED if cur.rowcount else ALREADY)


def step_login_check(connect, req, report):
    """Log in as the program user (TCP to the given host, as the programs do) and read a table."""
    try:
        conn = _connect(connect, req, req.program_user, req.program_password, req.db_name, as_admin=False)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM wheel_counter_table")
            cur.fetchone()
        finally:
            conn.close()
        report.add("login check", f"{req.program_user}@{req.host}", OK)
    except Exception as e:   # report, do not raise: everything before this worked
        hint = "" if req.reset_password else " (an existing user keeps its old password; rerun with --reset-password)"
        report.add("login check", f"{req.program_user}@{req.host}", FAILED, f"{e}{hint}")


# --------------------------------------------------------------------------- main function
def bootstrap(req: BootstrapRequest, connect: Optional[Callable] = None) -> dict:
    """Run all steps. Returns {"ok": bool, "database": ..., "steps": [...]} with no password in it.
    Raises BootstrapError for bad input (before anything is changed)."""
    validate(req)
    if connect is None:
        import pymysql
        connect = pymysql.connect
    report = Report(req.secrets())
    try:
        admin = _connect(connect, req, req.admin_user, req.admin_password)
        try:
            cur = admin.cursor()
            step_database(cur, req, report)
            for host in req.program_hosts:
                step_user(cur, req, report, host)
            cur.execute(f"USE `{req.db_name}`")
            step_scripts(cur, req, report)
            step_health_rows(cur, req, report)
        finally:
            admin.close()
    except Exception as e:
        report.add("error", type(e).__name__, FAILED, str(e))
    else:
        step_login_check(connect, req, report)
    return {"ok": report.ok, "database": req.db_name, "program_user": req.program_user, "steps": report.steps}


# --------------------------------------------------------------------------- CLI
def _password(env_name: str, prompt: str, allow_empty=False) -> str:
    value = os.environ.get(env_name)
    if value is None and sys.stdin.isatty():
        value = getpass.getpass(prompt)
    if not value and not allow_empty:
        raise BootstrapError(f"no password: set {env_name} or run in a terminal to be asked")
    return value or ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create or check a site's DPC database (S3, D-103).")
    ap.add_argument("--host", default="localhost", help="MySQL host (default localhost)")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--socket", help="connect the admin over this unix socket (root with auth_socket)")
    ap.add_argument("--admin-user", default="root")
    ap.add_argument("--admin-password-env", default="DPC_DB_ADMIN_PASS",
                    help="environment variable with the admin password (default DPC_DB_ADMIN_PASS)")
    ap.add_argument("--db", required=True, help="site database name")
    ap.add_argument("--user", required=True, help="program user to create (not root)")
    ap.add_argument("--password-env", default="DPC_DB_PASS",
                    help="environment variable with the program user's password (default DPC_DB_PASS)")
    ap.add_argument("--serial", action="append", default=[], required=True, help="camera serial (repeat per camera)")
    ap.add_argument("--program-host", action="append", help="host part of the program user (repeat; "
                    "default localhost and 127.0.0.1)")
    ap.add_argument("--reset-password", action="store_true", help="set the password on an existing program user")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    try:
        req = BootstrapRequest(
            admin_user=args.admin_user,
            admin_password=_password(args.admin_password_env, "MySQL admin password: ", allow_empty=bool(args.socket)),
            db_name=args.db, program_user=args.user,
            program_password=_password(args.password_env, f"Password for {args.user}: "),
            serials=args.serial, host=args.host, port=args.port, unix_socket=args.socket,
            program_hosts=tuple(args.program_host or ("localhost", "127.0.0.1")),
            reset_password=args.reset_password)
        result = bootstrap(req)
    except BootstrapError as e:
        result = {"ok": False, "error": str(e), "steps": []}

    if args.json:
        print(json.dumps(result, indent=2))
    elif "error" in result:
        print(f"ERROR {result['error']}")
    else:
        for s in result["steps"]:
            detail = f" ({s['detail']})" if s["detail"] else ""
            print(f"{s['result'].upper():14} {s['step']:15} {s['item']}{detail}")
        print("OK" if result["ok"] else "FAILED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
