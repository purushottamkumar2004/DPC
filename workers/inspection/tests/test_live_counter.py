"""F75: the live wheel number is written with an upsert (row ID 1 created when missing).

The SQL test always runs (fake cursor). The real-DB test runs against MySQL / MariaDB when
DPC_TEST_DB is set to "host:port:user:password" (a scratch database `dpc_f75_test` is created
and dropped); otherwise it is skipped.

Run from the repo root:
    python3 -m unittest discover -s workers/inspection/tests -t . -v
    DPC_TEST_DB=127.0.0.1:3306:root:<password> python3 -m unittest workers.inspection.tests.test_live_counter -v
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tests_support import FakeConn, FakeCursor, load_reference_settings   # noqa: E402

from defects import DefectStore  # noqa: E402

LOG = logging.getLogger("live_counter_test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False
SCRATCH_DB = "dpc_f75_test"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        _, cls.right = load_reference_settings(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)


class SqlTest(Base):
    def test_upsert_with_parameters(self):
        cur = FakeCursor()
        DefectStore(FakeConn(cur), cur, self.right, LOG).write_live_wheel_count(57)
        sql, params = cur.calls[-1]
        self.assertEqual(sql, "INSERT INTO wheel_counter_table (ID, WHEEL_COUNT) VALUES (1, %s) "
                              "ON DUPLICATE KEY UPDATE WHEEL_COUNT = VALUES(WHEEL_COUNT)")
        self.assertEqual(params, ("57",))


@unittest.skipUnless(os.environ.get("DPC_TEST_DB"), "set DPC_TEST_DB=host:port:user:password for the real-DB test")
class RealDbTest(Base):
    def setUp(self):
        import pymysql
        host, port, user, password = os.environ["DPC_TEST_DB"].split(":", 3)
        self.server = dict(host=host, port=int(port), user=user, password=password)
        with pymysql.connect(**self.server) as conn, conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH_DB}`")
            cur.execute(f"CREATE DATABASE `{SCRATCH_DB}`")
            cur.execute(f"USE `{SCRATCH_DB}`")
            with open(os.path.join(HERE, "..", "..", "..", "db", "schema.sql")) as f:
                schema = f.read()
            start = schema.index("CREATE TABLE IF NOT EXISTS wheel_counter_table")
            cur.execute(schema[start:schema.index(";", start)])        # the real table definition
        self.conn = pymysql.connect(database=SCRATCH_DB, **self.server)
        self.store = DefectStore(self.conn, self.conn.cursor(), self.right, LOG)

    def tearDown(self):
        import pymysql
        self.conn.close()
        with pymysql.connect(**self.server) as conn, conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{SCRATCH_DB}`")

    def rows(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT ID, WHEEL_COUNT FROM wheel_counter_table ORDER BY ID")
            return cur.fetchall()

    def test_empty_table_gets_row_1(self):
        self.assertEqual(self.rows(), ())
        self.store.write_live_wheel_count(12)
        self.assertEqual(self.rows(), ((1, "12"),))

    def test_existing_row_updated_never_duplicated(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO wheel_counter_table (ID, WHEEL_COUNT) VALUES (1, '0')")   # the site seed
        self.conn.commit()
        for n in (1, 2, 190, 0):
            self.store.write_live_wheel_count(n)
            self.assertEqual(self.rows(), ((1, str(n)),))


if __name__ == "__main__":
    unittest.main()
