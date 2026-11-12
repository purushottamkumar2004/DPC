"""Tests for db/bootstrap.py (STATUS S3 and F88, D-103 step 2, D-050, D-107).

Run from the repo root:
    python3 -m unittest tests.unit.test_db_bootstrap -v
The real-server tests run only when DPC_TEST_DB is set to "host:port:admin_user:admin_password"
(the same form as workers/inspection/tests/test_live_counter.py). They create the scratch
database `dpc_s3_test` and the user `dpc_s3_test_user`, and drop both afterwards:
    DPC_TEST_DB=127.0.0.1:3306:root:<password> python3 -m unittest tests.unit.test_db_bootstrap -v
"""

import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from db import bootstrap as bs  # noqa: E402

ADMIN_PW = "Adm1n'pw;--x"
PROG_PW = "Pr0g\"pw%s_y"
SETUP_PW = "Setup-pw_From-CNF-xyz123"        # made up; like install.sh's token_urlsafe password
SERIALS = ("TESTCAM1", "TESTCAM2")


def request(**over):
    values = dict(admin_user="admin", admin_password=ADMIN_PW, db_name="site_db",
                  program_user="dpc_prog", program_password=PROG_PW, serials=SERIALS)
    values.update(over)
    return bs.BootstrapRequest(**values)


# --------------------------------------------------------------------------- fake server
class FakeServer:
    """Just enough of MySQL for the statements bootstrap.py sends."""

    def __init__(self):
        self.state = {"dbs": {}, "users": {}, "grants": {}}
        self.log = []                       # (sql, params) of every statement
        self.fail_on = None                 # regex: raise when a statement matches
        self.connect_calls = []             # keyword arguments of every connect()

    ADMINS = {"admin": ADMIN_PW, "dpc_setup": SETUP_PW}

    def connect(self, user=None, password=None, database=None, read_default_file=None, **kwargs):
        self.connect_calls.append(dict(kwargs, user=user, password=password, read_default_file=read_default_file))
        if read_default_file:          # like pymysql: arguments win, the file fills what is missing
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(read_default_file)
            user = user or cfg.get("client", "user", fallback=None)
            password = password or cfg.get("client", "password", fallback=None)
        if user not in self.ADMINS:
            users = self.state["users"]
            if password not in (users.get((user, kwargs.get("host", "localhost"))), users.get((user, "%"))):
                raise RuntimeError(f"(1045, \"Access denied for user '{user}'\")")
        elif password != self.ADMINS[user]:
            raise RuntimeError(f"(1045, 'Access denied for user {user}')")
        return FakeConnection(self, database)

    def snapshot(self):
        return copy.deepcopy(self.state)


class FakeConnection:
    def __init__(self, server, database):
        self.server, self.database = server, database

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.server = conn, conn.server
        self._rows, self.rowcount = [], 0

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def _db(self):
        return self.server.state["dbs"][self.conn.database]

    def execute(self, sql, params=None):
        st, s = self.server.state, " ".join(sql.split())
        self.server.log.append((s, params))
        if self.server.fail_on and re.search(self.server.fail_on, s):
            raise RuntimeError(f"near '{s} {params}'")     # like MySQL: echoes the statement
        self._rows, self.rowcount = [], 0
        m = None
        if s.startswith("SELECT SCHEMA_NAME"):
            self._rows = [(params[0],)] if params[0] in st["dbs"] else []
        elif (m := re.match(r"CREATE DATABASE IF NOT EXISTS `(\w+)`", s)):
            st["dbs"].setdefault(m.group(1), {"tables": set(), "rows": {}})
        elif s.startswith("SELECT 1 FROM mysql.user"):
            self._rows = [(1,)] if tuple(params) in st["users"] else []
        elif (m := re.match(r"(CREATE|ALTER) USER '(\w+)'@'([^']+)' IDENTIFIED BY %s", s)):
            st["users"][(m.group(2), m.group(3).replace("%%", "%"))] = params[0]
        elif (m := re.match(r"SHOW GRANTS FOR '(\w+)'@'([^']+)'", s)):
            self._rows = [(g,) for g in st["grants"].get((m.group(1), m.group(2)), ["GRANT USAGE ON *.*"])]
        elif (m := re.match(r"GRANT ALL PRIVILEGES ON (`[^`]+`)\.\* TO '(\w+)'@'([^']+)'", s)):
            st["grants"].setdefault((m.group(2), m.group(3)), ["GRANT USAGE ON *.*"]).append(
                f"GRANT ALL PRIVILEGES ON {m.group(1)}.* TO `{m.group(2)}`@`{m.group(3)}`")
        elif (m := re.match(r"USE `(\w+)`", s)):
            self.conn.database = m.group(1)
        elif s == "SHOW TABLES":
            self._rows = [(t,) for t in sorted(self._db()["tables"])]
        elif (m := re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", s)):
            self._db()["tables"].add(m.group(1))
        elif (m := re.match(r"INSERT IGNORE INTO (\w+)", s)):
            key = (m.group(1), params[0] if params else "seed")
            if key not in self._db()["rows"]:
                self._db()["rows"][key] = True
                self.rowcount = 1
        elif s.startswith("SELECT MAX(version)"):
            self._rows = [(1,)]
        elif s.startswith("SELECT COUNT(*) FROM wheel_counter_table"):
            self._rows = [(1,)]
        else:
            raise AssertionError(f"unexpected statement: {s}")


def results(report, step=None):
    return [(s["step"], s["item"], s["result"]) for s in report["steps"] if step in (None, s["step"])]


# --------------------------------------------------------------------------- fake tests
class FakeServerTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()

    def run_bs(self, **over):
        return bs.bootstrap(request(**over), connect=self.server.connect)

    def test_empty_server_everything_created(self):
        report = self.run_bs()
        self.assertTrue(report["ok"], report)
        self.assertEqual(results(report, "database"), [("database", "site_db", bs.CREATED)])
        self.assertEqual([r for _, _, r in results(report, "user")], [bs.CREATED, bs.CREATED])
        self.assertEqual({i for _, i, _ in results(report, "user")}, {"dpc_prog@localhost", "dpc_prog@127.0.0.1"})
        self.assertEqual({r for _, _, r in results(report, "table")}, {bs.CREATED})
        self.assertEqual({i for _, i, _ in results(report, "table")}, set(bs.read_schema_tables()))
        self.assertIn(("fixed row", "wheel_counter_table row 1", bs.CREATED), results(report))
        self.assertIn(("fixed row", "CAMERA_PLC_STATUS row 1", bs.CREATED), results(report))
        self.assertEqual(results(report, "health row"),
                         [("health row", s, bs.CREATED) for s in SERIALS])
        self.assertEqual(results(report, "login check")[0][2], bs.OK)

    def test_second_run_changes_nothing(self):
        self.run_bs()
        before = self.server.snapshot()
        report = self.run_bs()
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.server.snapshot(), before)
        changed = [s for s in report["steps"] if s["result"] in (bs.CREATED, bs.UPDATED)]
        self.assertEqual(changed, [])
        for stmt, _ in self.server.log[len(self.server.log) // 2:]:
            self.assertNotRegex(stmt, r"^(CREATE (USER|DATABASE)|GRANT|ALTER USER)")

    def test_new_camera_on_second_run_adds_only_its_row(self):
        self.run_bs()
        report = self.run_bs(serials=SERIALS + ("TESTCAM3",))
        self.assertEqual(results(report, "health row")[-1], ("health row", "TESTCAM3", bs.CREATED))
        self.assertEqual([r for _, _, r in results(report, "health row")[:2]], [bs.ALREADY, bs.ALREADY])

    def test_grant_is_for_this_database_only(self):
        self.run_bs()
        grants = [s for s, _ in self.server.log if s.startswith("GRANT")]
        self.assertEqual(len(grants), 2)
        for g in grants:   # '_' is a wildcard in GRANT, so it must be escaped
            self.assertIn(r"ON `site\_db`.*", g)
            self.assertNotIn("*.*", g)
            self.assertNotIn("WITH GRANT OPTION", g)

    def test_password_is_sent_as_a_parameter_never_in_the_sql_text(self):
        self.run_bs()
        for stmt, params in self.server.log:
            self.assertNotIn(PROG_PW, stmt)
            self.assertNotIn(ADMIN_PW, stmt)
        self.assertIn((PROG_PW,), [p for s, p in self.server.log if s.startswith("CREATE USER")])

    def test_no_password_in_report_even_when_a_statement_fails(self):
        self.server.fail_on = r"^CREATE USER"
        report = self.run_bs()
        self.assertFalse(report["ok"])
        text = json.dumps(report)
        self.assertNotIn(PROG_PW, text)
        self.assertNotIn(ADMIN_PW, text)
        self.assertIn("***", text)                       # the echoed password was redacted
        self.assertEqual(results(report, "login check"), [])   # stops after the failure

    def test_wrong_admin_password_reports_failure(self):
        report = bs.bootstrap(request(admin_password="wrong"), connect=self.server.connect)
        self.assertFalse(report["ok"])
        self.assertEqual(report["steps"][0]["result"], bs.FAILED)
        self.assertEqual(self.server.state["dbs"], {})

    def test_existing_user_keeps_password_and_login_check_says_so(self):
        self.run_bs()
        report = self.run_bs(program_password="another-pw")
        self.assertEqual(self.server.state["users"][("dpc_prog", "localhost")], PROG_PW)
        self.assertFalse(report["ok"])
        check = [s for s in report["steps"] if s["step"] == "login check"][0]
        self.assertEqual(check["result"], bs.FAILED)
        self.assertIn("--reset-password", check["detail"])
        self.assertNotIn("another-pw", json.dumps(report))

    def test_reset_password_sets_it_on_existing_user(self):
        self.run_bs()
        report = self.run_bs(program_password="another-pw", reset_password=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual([r for _, _, r in results(report, "user")], [bs.UPDATED, bs.UPDATED])
        self.assertEqual(self.server.state["users"][("dpc_prog", "127.0.0.1")], "another-pw")

    def test_percent_host_works_with_parameters(self):
        report = self.run_bs(program_hosts=("%",))
        self.assertTrue(report["ok"], report)
        self.assertIn(("dpc_prog", "%"), self.server.state["users"])

    def test_bad_input_is_refused_before_connecting(self):
        connect = mock.Mock()
        cases = [dict(program_user="root"), dict(program_user="admin"), dict(db_name="site-db"),
                 dict(db_name="mysql"), dict(db_name="x; DROP DATABASE y"), dict(program_user="a'b"),
                 dict(serials=()), dict(serials=("A", "A")), dict(serials=("",)), dict(program_password=""),
                 dict(program_hosts=("bad host",)), dict(program_user="x" * 33)]
        for over in cases:
            with self.subTest(**{k: str(v) for k, v in over.items()}):
                with self.assertRaises(bs.BootstrapError):
                    bs.bootstrap(request(**over), connect=connect)
        connect.assert_not_called()


class SqlSplitTests(unittest.TestCase):
    def test_split_respects_quotes_and_comments(self):
        text = "-- a; comment\nSELECT 'a;b', \"c;d\", `e;f`; # x;y\nINSERT INTO t VALUES ('it\\'s;'); SELECT 1"
        self.assertEqual(bs.split_sql(text),
                         ["SELECT 'a;b', \"c;d\", `e;f`", "INSERT INTO t VALUES ('it\\'s;')", "SELECT 1"])

    def test_schema_tables_are_the_seeded_tables(self):
        self.assertEqual(bs.read_schema_tables(), ["schema_version", "defects_table", "repeated_defects_table",
                                                   "wheel_counter_table", "health_status", "CAMERA_PLC_STATUS"])

    def test_every_script_statement_is_complete(self):
        for name in bs.SCRIPTS:
            for stmt in bs.read_script(name):
                self.assertNotIn("--", stmt.split("'")[0])
                self.assertRegex(stmt, r"^(CREATE|INSERT)")


def write_cnf(folder, text=None):
    """<folder>/config/mysql_setup.cnf as install.sh writes it (mode 600)."""
    os.makedirs(os.path.join(folder, "config"), exist_ok=True)
    path = os.path.join(folder, "config", "mysql_setup.cnf")
    with open(path, "w") as f:
        f.write(text if text is not None else f"[client]\nuser=dpc_setup\npassword={SETUP_PW}\n")
    os.chmod(path, 0o600)
    return path


class OptionFileTests(unittest.TestCase):
    """F88: no admin password -> the dpc_setup login from <root>/config/mysql_setup.cnf."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.server = FakeServer()

    def run_bs(self, **over):
        values = dict(admin_user=None, admin_password=None, root=self.root)
        values.update(over)
        return bs.bootstrap(request(**values), connect=self.server.connect)

    def test_admin_login_comes_from_the_option_file(self):
        cnf = write_cnf(self.root)
        report = self.run_bs()
        self.assertTrue(report["ok"], report)
        admin_call = self.server.connect_calls[0]
        self.assertEqual(admin_call["read_default_file"], cnf)
        self.assertIsNone(admin_call["user"])        # pymysql would let an argument win over the file
        self.assertIsNone(admin_call["password"])
        self.assertEqual(report["admin"], f"dpc_setup ({cnf})")
        login = self.server.connect_calls[-1]          # the login check uses the program user, no file
        self.assertEqual((login["user"], login["read_default_file"]), ("dpc_prog", None))

    def test_second_run_with_the_option_file_changes_nothing(self):
        write_cnf(self.root)
        self.run_bs()
        before = self.server.snapshot()
        report = self.run_bs()
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.server.snapshot(), before)
        self.assertEqual([s for s in report["steps"] if s["result"] in (bs.CREATED, bs.UPDATED)], [])

    def test_empty_string_password_also_means_option_file(self):
        write_cnf(self.root)
        self.assertTrue(self.run_bs(admin_password="", admin_user="")["ok"])
        self.assertTrue(self.server.connect_calls[0]["read_default_file"])

    def test_missing_file_tells_to_run_the_installer(self):
        connect = mock.Mock()
        with self.assertRaises(bs.BootstrapError) as ctx:
            bs.bootstrap(request(admin_user=None, admin_password=None, root=self.root), connect=connect)
        self.assertIn("run install/install.sh (it creates dpc_setup)", str(ctx.exception))
        self.assertIn(os.path.join(self.root, "config", "mysql_setup.cnf"), str(ctx.exception))
        connect.assert_not_called()

    def test_incomplete_or_broken_file_is_refused(self):
        for text in ("[client]\nuser=dpc_setup\n", "[client]\npassword=x\n", "[mysql]\nuser=a\npassword=b\n",
                     "user=dpc_setup\npassword=x\n", ""):
            with self.subTest(text=text):
                write_cnf(self.root, text)
                with self.assertRaises(bs.BootstrapError) as ctx:
                    self.run_bs()
                self.assertIn("install/install.sh", str(ctx.exception))
                self.assertNotIn("password=x", str(ctx.exception))

    def test_explicit_option_file_path(self):
        other = os.path.join(self.root, "elsewhere")
        cnf = write_cnf(other)
        report = self.run_bs(admin_option_file=cnf)
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.server.connect_calls[0]["read_default_file"], cnf)

    def test_given_admin_password_or_socket_does_not_use_the_file(self):
        write_cnf(self.root)
        self.assertTrue(self.run_bs(admin_user="admin", admin_password=ADMIN_PW)["ok"])
        self.assertEqual((self.server.connect_calls[0]["read_default_file"], self.server.connect_calls[0]["user"]),
                         (None, "admin"))
        self.server.connect_calls.clear()
        req = request(admin_user="admin", admin_password="", unix_socket="/x.sock", root=self.root)
        connect = mock.Mock(side_effect=RuntimeError("socket not there"))
        bs.bootstrap(req, connect=connect)
        self.assertNotIn("read_default_file", connect.call_args.kwargs)
        self.assertEqual(connect.call_args.kwargs["unix_socket"], "/x.sock")

    def test_dpc_setup_is_never_the_program_user(self):
        write_cnf(self.root)
        for user in ("dpc_setup", "DPC_SETUP"):
            with self.subTest(user=user), self.assertRaises(bs.BootstrapError):
                self.run_bs(program_user=user)
            with self.subTest(user=user, admin="password"), self.assertRaises(bs.BootstrapError):
                self.run_bs(program_user=user, admin_user="admin", admin_password=ADMIN_PW)
        self.assertIn("dpc_setup", bs.FORBIDDEN_PROGRAM_USERS)

    def test_option_file_password_is_redacted_from_errors(self):
        write_cnf(self.root)
        with mock.patch.object(FakeCursor, "execute", autospec=True,
                               side_effect=lambda cur, sql, params=None: (_ for _ in ()).throw(
                                   RuntimeError(f"denied, login was {SETUP_PW}"))):
            report = self.run_bs()
        self.assertFalse(report["ok"])
        self.assertNotIn(SETUP_PW, json.dumps(report))
        self.assertIn("***", json.dumps(report))


class CliTests(unittest.TestCase):
    def run_cli(self, argv, env):
        out = io.StringIO()
        fake = FakeServer()
        module = types.SimpleNamespace(connect=fake.connect)
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(sys.modules, {"pymysql": module}), \
                mock.patch.object(sys.stdin, "isatty", return_value=False), contextlib.redirect_stdout(out):
            code = bs.main(argv)
        return code, out.getvalue()

    BASE = ["--admin-user", "admin", "--db", "site_db", "--user", "dpc_prog",
            "--serial", "TESTCAM1", "--serial", "TESTCAM2"]
    ENV = {"DPC_DB_ADMIN_PASS": ADMIN_PW, "DPC_DB_PASS": PROG_PW}

    def test_json_report_and_exit_code(self):
        code, out = self.run_cli(self.BASE + ["--json"], self.ENV)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertNotIn(PROG_PW, out)
        self.assertNotIn(ADMIN_PW, out)

    def test_text_report(self):
        code, out = self.run_cli(self.BASE, self.ENV)
        self.assertEqual(code, 0)
        self.assertIn("CREATED", out)
        self.assertTrue(out.strip().endswith("OK"))

    def test_missing_password_is_an_error_not_a_prompt(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DPC_DB_PASS", None)
            code, out = self.run_cli(self.BASE + ["--json"], {"DPC_DB_ADMIN_PASS": ADMIN_PW})
        self.assertEqual(code, 1)
        self.assertIn("DPC_DB_PASS", json.loads(out)["error"])

    def test_no_admin_password_uses_mysql_setup_cnf_under_root(self):
        with tempfile.TemporaryDirectory() as root:
            write_cnf(root)
            argv = [a for a in self.BASE if a not in ("--admin-user", "admin")]
            env = {"DPC_DB_PASS": PROG_PW}
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DPC_DB_ADMIN_PASS", None)
                code, out = self.run_cli(argv + ["--root", root, "--json"], env)
            self.assertEqual(code, 0, out)
            self.assertTrue(json.loads(out)["admin"].startswith("dpc_setup ("))
            self.assertNotIn(SETUP_PW, out)

    def test_no_admin_password_and_no_cnf_says_run_the_installer(self):
        with tempfile.TemporaryDirectory() as root:
            argv = [a for a in self.BASE if a not in ("--admin-user", "admin")]
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DPC_DB_ADMIN_PASS", None)
                code, out = self.run_cli(argv + ["--root", root, "--json"], {"DPC_DB_PASS": PROG_PW})
            self.assertEqual(code, 1)
            self.assertIn("run install/install.sh (it creates dpc_setup)", json.loads(out)["error"])

    def test_explicit_admin_user_without_password_is_an_error_not_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            write_cnf(root)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DPC_DB_ADMIN_PASS", None)
                code, out = self.run_cli(self.BASE + ["--root", root, "--json"], {"DPC_DB_PASS": PROG_PW})
            self.assertEqual(code, 1)
            self.assertIn("DPC_DB_ADMIN_PASS", json.loads(out)["error"])

    def test_root_as_program_user_is_refused(self):
        argv = [a if a != "dpc_prog" else "root" for a in self.BASE]
        code, out = self.run_cli(argv + ["--json"], self.ENV)
        self.assertEqual(code, 1)
        self.assertIn("root", json.loads(out)["error"])


# --------------------------------------------------------------------------- real server
TEST_DB, TEST_USER = "dpc_s3_test", "dpc_s3_test_user"
LOOKALIKE_DB = "dpcXs3Xtest"     # matches dpc_s3_test if '_' were not escaped in GRANT


@unittest.skipUnless(os.environ.get("DPC_TEST_DB"), "set DPC_TEST_DB=host:port:user:password for the real-DB test")
class RealServerTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        self.pymysql = pymysql
        host, port, user, password = os.environ["DPC_TEST_DB"].split(":", 3)
        self.req = bs.BootstrapRequest(admin_user=user, admin_password=password, db_name=TEST_DB,
                                       program_user=TEST_USER, program_password=PROG_PW,
                                       serials=SERIALS, host=host, port=int(port))
        self.admin = dict(host=host, port=int(port), user=user, password=password, autocommit=True)
        self._cleanup()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        conn = self.pymysql.connect(**self.admin)
        with conn.cursor() as cur:
            for db in (TEST_DB, LOOKALIKE_DB):
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
            for host in self.req.program_hosts:
                cur.execute(f"DROP USER IF EXISTS '{TEST_USER}'@'{host}'")
        conn.close()

    def program_conn(self, database=TEST_DB):
        return self.pymysql.connect(host=self.req.host, port=self.req.port, user=TEST_USER,
                                    password=PROG_PW, database=database, autocommit=True)

    def state(self):
        conn = self.pymysql.connect(database=TEST_DB, **self.admin)
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = sorted(r[0] for r in cur.fetchall())
            cur.execute("SELECT ID, WHEEL_COUNT FROM wheel_counter_table")
            wheel = cur.fetchall()
            cur.execute("SELECT ITEM, HEALTH FROM health_status ORDER BY ITEM")
            health = cur.fetchall()
            grants = []
            for host in self.req.program_hosts:
                cur.execute(f"SHOW GRANTS FOR '{TEST_USER}'@'{host}'")
                grants += [r[0] for r in cur.fetchall()]
        conn.close()
        return tables, wheel, health, grants

    def test_empty_then_second_run_without_changes(self):
        first = bs.bootstrap(self.req)
        self.assertTrue(first["ok"], first)
        self.assertIn(("database", TEST_DB, bs.CREATED), results(first))
        tables, wheel, health, grants = self.state()
        self.assertEqual(set(tables), set(bs.read_schema_tables()))
        self.assertEqual(wheel, ((1, "0"),))
        self.assertEqual(health, tuple((s, "NOK") for s in sorted(SERIALS)))

        before = self.state()
        second = bs.bootstrap(self.req)
        self.assertTrue(second["ok"], second)
        self.assertEqual([s for s in second["steps"] if s["result"] in (bs.CREATED, bs.UPDATED)], [])
        self.assertEqual(self.state(), before)
        self.assertNotIn(PROG_PW, json.dumps(first) + json.dumps(second))

    def test_program_user_can_run_worker_statements_on_its_database_only(self):
        self.assertTrue(bs.bootstrap(self.req)["ok"])
        conn = self.program_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO wheel_counter_table (ID, WHEEL_COUNT) VALUES (1, %s) "
                        "ON DUPLICATE KEY UPDATE WHEEL_COUNT = VALUES(WHEEL_COUNT)", ("42",))
            cur.execute("insert into defects_table(defect_side, defect_type, view_defect, wheel_no, "
                        "xmin, xmax, ymin, ymax) values('Right','bolt_missing','/x.jpg','12','1','2','3','4')")
            cur.execute("UPDATE health_status SET HEALTH = %s WHERE ITEM = %s", ("OK", SERIALS[0]))
            self.assertEqual(cur.rowcount, 1)
        conn.close()

        admin = self.pymysql.connect(**self.admin)
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{LOOKALIKE_DB}`")
        admin.close()
        for db in (LOOKALIKE_DB, "mysql"):
            with self.subTest(db=db), self.assertRaises(self.pymysql.err.OperationalError):
                self.program_conn(database=db)
        conn = self.program_conn()
        with conn.cursor() as cur, self.assertRaises(self.pymysql.err.OperationalError):
            cur.execute("CREATE DATABASE dpc_s3_test_other")
        conn.close()

    def test_admin_from_option_file_on_a_real_server(self):
        """F88: the admin login read by pymysql from a [client] option file (as mysql_setup.cnf)."""
        with tempfile.TemporaryDirectory() as root:
            write_cnf(root, f"[client]\nuser={self.admin['user']}\npassword={self.admin['password']}\n")
            req = dataclass_replace(self.req, admin_user=None, admin_password=None, root=root)
            first = bs.bootstrap(req)
            self.assertTrue(first["ok"], first)
            self.assertIn(("database", TEST_DB, bs.CREATED), results(first))
            before = self.state()
            second = bs.bootstrap(req)
            self.assertTrue(second["ok"], second)
            self.assertEqual([s for s in second["steps"] if s["result"] in (bs.CREATED, bs.UPDATED)], [])
            self.assertEqual(self.state(), before)
            self.assertNotIn(self.admin["password"], json.dumps(first) + json.dumps(second))

    def test_installer_style_admin_at_localhost(self):
        """As install.sh step 5: an admin account '<name>'@'localhost' with ALL ON *.* WITH GRANT
        OPTION, its login only in the option file; pymysql connects over TCP to localhost."""
        setup_user, setup_pw = "dpc_s3_test_setup", "Setup_t3st-pw"
        admin = self.pymysql.connect(**self.admin)
        with admin.cursor() as cur:
            cur.execute(f"DROP USER IF EXISTS '{setup_user}'@'localhost'")
            cur.execute(f"CREATE USER '{setup_user}'@'localhost' IDENTIFIED BY %s", (setup_pw,))
            cur.execute(f"GRANT ALL PRIVILEGES ON *.* TO '{setup_user}'@'localhost' WITH GRANT OPTION")
        admin.close()
        self.addCleanup(self._drop_user, setup_user)
        with tempfile.TemporaryDirectory() as root:
            write_cnf(root, f"[client]\nuser={setup_user}\npassword={setup_pw}\n")
            req = dataclass_replace(self.req, admin_user=None, admin_password=None, root=root, host="localhost")
            report = bs.bootstrap(req)
        self.assertTrue(report["ok"], report)
        self.assertNotIn(setup_pw, json.dumps(report))

    def _drop_user(self, user):
        conn = self.pymysql.connect(**self.admin)
        with conn.cursor() as cur:
            cur.execute(f"DROP USER IF EXISTS '{user}'@'localhost'")
        conn.close()

    def test_missing_row_and_camera_are_added_on_rerun(self):
        self.assertTrue(bs.bootstrap(self.req)["ok"])
        admin = self.pymysql.connect(database=TEST_DB, **self.admin)
        with admin.cursor() as cur:      # like Tadipatri: tables there, row 1 missing (D-101 finding)
            cur.execute("DELETE FROM wheel_counter_table")
        admin.close()
        req = dataclass_replace(self.req, serials=SERIALS + ("TESTCAM3",))
        report = bs.bootstrap(req)
        self.assertTrue(report["ok"], report)
        self.assertIn(("fixed row", "wheel_counter_table row 1", bs.CREATED), results(report))
        self.assertIn(("health row", "TESTCAM3", bs.CREATED), results(report))
        self.assertEqual(self.state()[1], ((1, "0"),))


def dataclass_replace(obj, **changes):
    import dataclasses
    return dataclasses.replace(obj, **changes)


if __name__ == "__main__":
    unittest.main()
