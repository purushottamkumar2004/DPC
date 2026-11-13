"""Wizard step 2 "Test connection" (D-112, F96): connect with the login the person entered and
check that the database exists. Never creates a database or a user, needs no admin login.

Result (plain words for the page):
  {"ok", "status": ok | db_missing | login_refused | unreachable | no_rights | error,
   "sentence", "tables": n or None, "seeded": bool}
"""
import socket

MISSING = "Create the database {db} first, for example in MySQL Workbench, then press Test connection again."
MYSQL_ACCESS_DENIED, MYSQL_DB_ACCESS_DENIED, MYSQL_UNKNOWN_DB = 1045, 1044, 1049
MYSQL_CANNOT_CONNECT, MYSQL_UNKNOWN_HOST = 2003, 2005
SEEDED_TABLES = ("defects_table", "wheel_counter_table", "health_status")


def _connect(host, port, user, password, database=None):
    import pymysql
    return pymysql.connect(host=host, port=int(port), user=user, password=password, database=database,
                           connect_timeout=5, read_timeout=10, autocommit=True)


connect = _connect                      # tests replace it


def check(host, port, database, user, password):
    try:
        conn = connect(host, port, user, password)
    except Exception as e:                                             # pymysql.err.OperationalError
        code = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
        if code == MYSQL_ACCESS_DENIED:
            return {"ok": False, "status": "login_refused", "tables": None, "seeded": False,
                    "sentence": f"MySQL refused the login of user {user}: check the user and the password."}
        if code in (MYSQL_CANNOT_CONNECT, MYSQL_UNKNOWN_HOST) or isinstance(e, (socket.error, ConnectionError)):
            return {"ok": False, "status": "unreachable", "tables": None, "seeded": False,
                    "sentence": f"No MySQL answers at {host}:{port}. Check the host and the port, and that MySQL is running."}
        return {"ok": False, "status": "error", "tables": None, "seeded": False,
                "sentence": f"The connection failed: {str(e)[:200]}"}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (database,))
            if cur.fetchone() is None:
                return {"ok": False, "status": "db_missing", "tables": None, "seeded": False,
                        "sentence": MISSING.format(db=database)}
            try:
                cur.execute(f"USE `{database}`")
            except Exception as e:
                code = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
                if code in (MYSQL_DB_ACCESS_DENIED, MYSQL_ACCESS_DENIED):
                    return {"ok": False, "status": "no_rights", "tables": None, "seeded": False,
                            "sentence": f"User {user} may not use the database {database}. Give it the rights to "
                                        f"this database (for example in MySQL Workbench), then test again."}
                raise
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (database,))
            tables = {row[0] for row in cur.fetchall()}
    except Exception as e:
        return {"ok": False, "status": "error", "tables": None, "seeded": False,
                "sentence": f"The check failed: {str(e)[:200]}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    seeded = all(t in tables for t in SEEDED_TABLES)
    sentence = (f"Connected. The database {database} exists" +
                (f" and has its tables ({len(tables)})." if seeded else
                 (f" with {len(tables)} tables, but not the inspection tables yet: press Prepare tables." if tables
                  else " and is empty: press Prepare tables.")))
    return {"ok": True, "status": "ok", "tables": len(tables), "seeded": seeded, "sentence": sentence}
