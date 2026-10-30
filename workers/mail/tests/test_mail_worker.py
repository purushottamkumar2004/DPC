"""Tests for workers/mail/mail_worker.py.

Pure tests (settings, mail content, images) need nothing. The poll tests use a REAL MySQL /
MariaDB test database built from db/schema.sql and a fake Microsoft Graph (requests.post is
replaced), so no mail is sent and no network is used. The test DB `dpc_mail_test` is
created and dropped by the tests; give the server with env DPC_TEST_DB_HOST, _PORT, _USER,
_PASSWORD (default localhost, 3306, root, empty). Without a server the DB tests are skipped.

Run from the repo root:
    python3 -m unittest discover -s workers/mail/tests -t . -v
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(WORKER_DIR))
sys.path.insert(0, WORKER_DIR)

import pymysql                        # noqa: E402
from pymysql.constants import CLIENT  # noqa: E402

import mail_worker as M               # noqa: E402

TEST_DB = "dpc_mail_test"
FAKE_SECRET = "s3cr3t-graph-value"
DB_PASSWORD_SECRET = "db-pass-for-test"

log = logging.getLogger("mail-test")
log.addHandler(logging.NullHandler())
log.propagate = False


def db_server():
    return dict(host=os.environ.get("DPC_TEST_DB_HOST", "localhost"),
                port=int(os.environ.get("DPC_TEST_DB_PORT", "3306")),
                user=os.environ.get("DPC_TEST_DB_USER", "root"),
                password=os.environ.get("DPC_TEST_DB_PASSWORD", ""))


# A fixed, complete DB block: settings and crash tests never depend on the DB environment
# (STATUS F38). Only PollTests pass the real test server (db=real_db_block()).
FIXED_DB = {"host": "localhost", "port": 3306, "name": TEST_DB, "user": "dpc_test",
            "password": "fixed-db-password-for-tests"}


def real_db_block():
    server = db_server()
    return {"host": server["host"], "port": server["port"], "name": TEST_DB,
            "user": server["user"], "password": server["password"]}


def mail_json(root, db=None, **mail_overrides):
    """A rendered mail.json as proposed for CONTRACTS 13."""
    data = {
        "_generated": {"from": "site.yaml", "sha256": "0" * 64, "at": "2026-10-30T10:00:00", "renderer": 1},
        "worker_id": "mail",
        "site": {"id": "testsite", "name": "Test DPC"},
        "paths": {"root": root, "logs": os.path.join(root, "logs"), "state": os.path.join(root, "data", "state")},
        "log": {"backup_count": 3},
        "db": dict(db or FIXED_DB),
        "mail": {"sender": "graph", "sender_address": "reports@example.com", "project_name": "Test_dpc",
                 "to": ["a@example.com", "b@example.com"], "cc": ["c@example.com"], "smtp": None,
                 "report_url": "https://example.com/report", "max_alert_age_hours": 24,
                 "flood": {"max_per_window": 8, "window_hours": 5, "internal_to": ["internal@example.com"]}},
        "graph": {"tenant_id": "tenant-x", "client_id": "client-x", "client_secret": FAKE_SECRET},
    }
    data["mail"].update(mail_overrides)
    return data


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeGraph:
    """Replaces requests.post: token -> 200, sendMail -> send_status (202 = accepted)."""

    def __init__(self, send_status=202):
        self.send_status = send_status
        self.mails = []

    def post(self, url, **kwargs):
        if "oauth2" in url:
            return FakeResponse(200, {"access_token": "fake-token"})
        self.mails.append(kwargs["json"]["message"])
        return FakeResponse(self.send_status, {"error": "fake failure"} if self.send_status != 202 else {})


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_full_file_has_no_warnings(self):
        s = M.build_settings(mail_json("/tmp/r"))
        self.assertEqual(s.warnings, [])
        self.assertEqual(s.flood.max_per_window, 8)
        self.assertEqual(s.to, ["a@example.com", "b@example.com"])
        self.assertIn(FAKE_SECRET, s.secrets())

    def test_secret_recipient_and_db_name_are_never_defaulted(self):
        raw = mail_json("/tmp/r")
        del raw["db"]["password"], raw["db"]["name"], raw["mail"]["to"], raw["graph"]["client_secret"]
        with self.assertRaises(M.ConfigError) as ctx:
            M.build_settings(raw)
        for key in ("db.password", "db.name", "mail.to", "graph.client_secret"):
            self.assertIn(key, str(ctx.exception))

    def test_safe_keys_get_a_default_with_one_warning_each(self):
        raw = mail_json("/tmp/r")
        for key in ("project_name", "report_url", "max_alert_age_hours", "flood", "cc"):
            del raw["mail"][key]
        del raw["log"], raw["db"]["host"]
        s = M.build_settings(raw)
        self.assertEqual(s.project_name, "Test DPC")            # from site.name
        self.assertEqual(s.max_alert_age_hours, 24.0)
        self.assertIsNone(s.flood)
        self.assertIsNone(s.report_url)
        self.assertEqual((s.backup_count, s.db_host, s.cc), (5, "localhost", []))
        self.assertEqual(len(s.warnings), 7)

    def test_explicit_null_switches_off_without_warning(self):
        s = M.build_settings(mail_json("/tmp/r", flood=None, max_alert_age_hours=None, report_url=None))
        self.assertEqual((s.flood, s.max_alert_age_hours, s.report_url, s.warnings), (None, None, None, []))

    def test_wrong_type_names_the_key(self):
        with self.assertRaises(M.ConfigError) as ctx:
            M.build_settings(mail_json("/tmp/r", to="a@example.com"))
        self.assertIn("mail.to", str(ctx.exception))

    def test_smtp_needs_its_block_not_graph(self):
        raw = mail_json("/tmp/r", sender="smtp", smtp={"host": "smtp.example.com", "port": 587, "password": "p"})
        raw["graph"] = None
        s = M.build_settings(raw)
        self.assertEqual((s.smtp_host, s.smtp_port), ("smtp.example.com", 587))
        raw["mail"]["smtp"] = None
        with self.assertRaises(M.ConfigError):
            M.build_settings(raw)


# ---------------------------------------------------------------------------------------------
# Mail content and images
# ---------------------------------------------------------------------------------------------
class ContentTests(unittest.TestCase):
    rows = [{"ID": 1, "defect_type": "WheelMissing", "defect_side": "Left", "wheel_no": "12",
             "create_datetime": datetime.datetime(2026, 10, 30, 9, 0, 0)},
            {"ID": 2, "defect_type": "Wheel_Skidding", "defect_side": "Right", "wheel_no": "40",
             "create_datetime": datetime.datetime(2026, 10, 30, 9, 5, 0)}]

    def test_subjects_as_before(self):
        self.assertEqual(M.build_subject("Baga DPC", 2, False), "!! CRITICAL ALERT: 2 New Defects Detected in Baga DPC !!")
        self.assertEqual(M.build_subject("Baga DPC", 2, True),
                         "!! FALSE-ALARM WATCH (Restricted) !!: 2 New Defects Detected in Baga DPC !!")

    def test_body_window_rows_link_and_notice(self):
        flood = M.Flood(8, 5.0, ["i@example.com"])
        body = M.build_body("Baga DPC", self.rows, "https://example.com/r", flood, restricted=True)
        self.assertIn("Report from: 2026-10-30 09:00:00 to 2026-10-30 09:05:00", body)
        self.assertIn("Total Number of Defects: 2", body)
        self.assertIn("<td>Wheel_Skidding</td><td>Right</td><td>40</td>", body)
        self.assertIn("More than 8 defects have been sent in the last 5 hours", body)
        self.assertIn("href='https://example.com/r'", body)
        plain = M.build_body("Baga DPC", self.rows, None, flood, restricted=False)
        self.assertNotIn("NOTE:", plain)
        self.assertNotIn("Click here to view the report", plain)

    def test_image_limits(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        rows = []
        for i in range(12):
            path = os.path.join(tmp, f"img{i}.jpg")
            with open(path, "wb") as f:
                f.write(b"x" * (400 if i == 1 else 100))
            rows.append({"view_defect": path})
        rows.insert(0, {"view_defect": os.path.join(tmp, "gone.jpg")})       # missing: skipped
        images = M.collect_images(rows, max_bytes=1000, log=log)
        names = [n for n, _ in images]
        self.assertNotIn("gone.jpg", names)
        self.assertLessEqual(sum(len(d) for _, d in images), 1000)
        self.assertEqual(names[:2], ["img0.jpg", "img1.jpg"])
        many = M.collect_images(rows, max_bytes=10 ** 6, log=log)
        self.assertEqual(len(many), M.MAX_ATTACH_FILES)


# ---------------------------------------------------------------------------------------------
# Polls against a real test DB with a fake Graph
# ---------------------------------------------------------------------------------------------
def _server_available():
    try:
        pymysql.connect(connect_timeout=3, **db_server()).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_server_available(), "no MySQL/MariaDB test server (DPC_TEST_DB_*)")
class PollTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        admin = pymysql.connect(client_flag=CLIENT.MULTI_STATEMENTS, autocommit=True, **db_server())
        with admin.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS {TEST_DB}; CREATE DATABASE {TEST_DB}; USE {TEST_DB};")
            with open(os.path.join(REPO_ROOT, "db", "schema.sql"), encoding="utf-8") as f:
                c.execute(f.read())
            while c.nextset():
                pass
        admin.close()
        self.addCleanup(self._drop_db)
        self.s = M.build_settings(mail_json(self.root, db=real_db_block()))
        self.conn = M.connect_db(self.s)
        self.cur = self.conn.cursor()
        self.addCleanup(self.conn.close)
        self.graph = FakeGraph()
        self._real_post = M.requests.post
        M.requests.post = self.graph.post
        self.addCleanup(setattr, M.requests, "post", self._real_post)
        self.image_dir = os.path.join(self.root, "data", "images", "DEFECTIVE", "LEFT")
        os.makedirs(self.image_dir)

    def _drop_db(self):
        admin = pymysql.connect(autocommit=True, **db_server())
        with admin.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        admin.close()

    def insert(self, defect_type="WheelMissing", side="Left", wheel="12", status=0, age_hours=0.0, box=(10, 60, 20, 80)):
        path = os.path.join(self.image_dir, f"{defect_type}_{side}_{wheel}_{status}_{age_hours}.jpg")
        with open(path, "wb") as f:
            f.write(b"jpegdata")
        created = datetime.datetime.now() - datetime.timedelta(hours=age_hours)
        self.cur.execute("INSERT INTO defects_table (defect_side, defect_type, view_defect, wheel_no, xmin, xmax, "
                         "ymin, ymax, is_alert_sent, create_datetime) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (side, defect_type, path, wheel, box[0], box[1], box[2], box[3], status,
                          created.strftime('%Y-%m-%d %H:%M:%S')))
        return self.cur.lastrowid

    def status(self, row_id):
        self.cur.execute("SELECT is_alert_sent FROM defects_table WHERE ID = %s", (row_id,))
        return self.cur.fetchone()["is_alert_sent"]

    def test_rows_picked_deduped_mailed_once_and_marked(self):
        first = self.insert("WheelMissing", "Left", "12")
        twin = self.insert("WheelMissing", "Left", "12")                   # same poll: duplicate
        right = self.insert("WheelMissing", "Right", "12")                 # other side: its own line
        skid = self.insert("Wheel_Skidding", "Right", "40")
        batch = self.insert("Batch_Wheels_Missing", "Left", "189", box=(0, 0, 0, 0))
        M.poll_once(self.s, self.cur, log)
        self.assertEqual([self.status(i) for i in (first, twin, right, skid, batch)], [1, 2, 1, 1, 1])
        self.assertEqual(len(self.graph.mails), 1)
        mail = self.graph.mails[0]
        self.assertEqual(mail["subject"], "!! CRITICAL ALERT: 4 New Defects Detected in Test_dpc !!")
        self.assertEqual([r["emailAddress"]["address"] for r in mail["toRecipients"]], self.s.to)
        self.assertEqual([r["emailAddress"]["address"] for r in mail["ccRecipients"]], self.s.cc)
        self.assertEqual(len(mail["attachments"]), 4)
        for name in ("Wheel_Skidding", "Batch_Wheels_Missing", "WheelMissing"):
            self.assertIn(f"<td>{name}</td>", mail["body"]["content"])
        M.poll_once(self.s, self.cur, log)                                  # nothing left
        self.assertEqual(len(self.graph.mails), 1)

    def test_already_sent_twin_within_24h_makes_a_duplicate(self):
        self.insert("WheelMissing", "Left", "12", status=1, age_hours=3)
        new = self.insert("WheelMissing", "Left", "12")
        other_wheel = self.insert("WheelMissing", "Left", "13")
        M.poll_once(self.s, self.cur, log)
        self.assertEqual((self.status(new), self.status(other_wheel)), (2, 1))
        self.assertIn("1 New Defects", self.graph.mails[0]["subject"])

    def test_send_failure_leaves_rows_unsent_then_retry_sends(self):
        row = self.insert()
        self.graph.send_status = 500
        M.poll_once(self.s, self.cur, log)
        self.assertEqual(self.status(row), 0)
        self.assertFalse(os.path.exists(M.journal_path(self.s)))
        self.graph.send_status = 202
        M.poll_once(self.s, self.cur, log)
        self.assertEqual(self.status(row), 1)
        self.assertEqual(len(self.graph.mails), 2)          # the failed try + the accepted one

    def test_backlog_older_than_max_age_is_ignored_not_mailed(self):
        old = self.insert("WheelMissing", "Left", "5", age_hours=30)
        fresh = self.insert("WheelMissing", "Left", "6", age_hours=1)
        M.poll_once(self.s, self.cur, log)
        self.assertEqual((self.status(old), self.status(fresh)), (2, 1))
        self.assertIn("1 New Defects", self.graph.mails[0]["subject"])

    def test_flood_breaker_restricts_to_internal(self):
        for wheel in range(7):                                  # 7 sent in the last 5 h
            self.insert("WheelMissing", "Left", str(100 + wheel), status=1, age_hours=1)
        new = [self.insert("WheelMissing", "Right", str(w)) for w in (1, 2)]   # 7 + 2 > 8
        M.poll_once(self.s, self.cur, log)
        mail = self.graph.mails[0]
        self.assertTrue(mail["subject"].startswith("!! FALSE-ALARM WATCH (Restricted) !!"))
        self.assertEqual([r["emailAddress"]["address"] for r in mail["toRecipients"]], ["internal@example.com"])
        self.assertEqual(mail["ccRecipients"], [])
        self.assertEqual([self.status(i) for i in new], [1, 1])

    def test_under_flood_limit_goes_to_clients(self):
        for wheel in range(6):
            self.insert("WheelMissing", "Left", str(100 + wheel), status=1, age_hours=1)
        self.insert("WheelMissing", "Right", "1")
        self.insert("WheelMissing", "Right", "2")                                   # 6 + 2 = 8, not > 8
        M.poll_once(self.s, self.cur, log)
        self.assertTrue(self.graph.mails[0]["subject"].startswith("!! CRITICAL ALERT"))

    def test_journal_replay_marks_sent_without_mailing(self):
        row = self.insert()
        M.write_journal(self.s, [row])                  # mailed, then crashed before the UPDATE
        M.replay_journal(self.s, self.cur, log)
        self.assertEqual(self.status(row), 1)
        self.assertFalse(os.path.exists(M.journal_path(self.s)))
        M.poll_once(self.s, self.cur, log)
        self.assertEqual(self.graph.mails, [])


# ---------------------------------------------------------------------------------------------
# Crash behaviour (whole program in a subprocess)
# ---------------------------------------------------------------------------------------------
class CrashTests(unittest.TestCase):
    def run_worker(self, raw):
        root = raw["paths"]["root"]
        path = os.path.join(root, "mail.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        env = dict(os.environ, DPC_WORKER_CONFIG=path)
        proc = subprocess.run([sys.executable, os.path.join(WORKER_DIR, "mail_worker.py")], env=env,
                              capture_output=True, text=True, timeout=60)
        record = os.path.join(root, "run", "mail.last_error.json")
        return proc, record

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_db_error_writes_last_error_without_secrets_and_exits_1(self):
        raw = mail_json(self.root)
        raw["db"].update(host="127.0.0.1", port=1, password=DB_PASSWORD_SECRET)        # nothing listens
        proc, record = self.run_worker(raw)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        text = read_text(record)
        self.assertIn("OperationalError", text)
        for secret in (DB_PASSWORD_SECRET, FAKE_SECRET):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, read_text(os.path.join(self.root, "logs", "mail.log")))

    def test_missing_secret_is_a_config_error_naming_the_key(self):
        raw = mail_json(self.root)
        del raw["graph"]["client_secret"]
        proc, record = self.run_worker(raw)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("graph.client_secret", json.loads(read_text(record))["message"])

    def test_stale_last_error_is_cleared_at_start(self):
        os.makedirs(os.path.join(self.root, "run"))
        stale = os.path.join(self.root, "run", "mail.last_error.json")
        with open(stale, "w") as f:
            f.write('{"type": "Old"}')
        raw = mail_json(self.root)
        del raw["db"]["name"]
        self.run_worker(raw)
        self.assertNotEqual(json.loads(read_text(stale))["type"], "Old")


if __name__ == "__main__":
    unittest.main()
