"""Read-only access to the site MySQL DB (CONTRACTS 5), with pymysql, not the Django ORM.

Only SELECTs on `defects_table` and `wheel_counter_table`, and only the base columns every
site has (Tadipatri has no review columns). A short-lived connection per call with short
timeouts: if MySQL is slow or down the page shows "database not reachable" and never hangs.
All values go in as query parameters.
"""
import datetime
import logging
import threading
import time

log = logging.getLogger("dpc.ui.sitedb")

DEFECT_COLUMNS = ("ID", "defect_side", "defect_type", "view_defect", "wheel_no",
                  "xmin", "xmax", "ymin", "ymax", "create_datetime", "is_alert_sent")
CACHE_S = 2.0


class SiteDBError(Exception):
    """The site database could not be read (plain-language text in str())."""


class DefectFilter:
    """Filters of the Defects page; empty values mean 'any'."""

    def __init__(self, date_from=None, date_to=None, side="", defect_type="", wheel=""):
        self.date_from, self.date_to = date_from, date_to
        self.side, self.defect_type, self.wheel = side or "", defect_type or "", (wheel or "").strip()

    def where(self):
        clauses, params = [], []
        if self.date_from:
            clauses.append("create_datetime >= %s")
            params.append(datetime.datetime.combine(self.date_from, datetime.time.min))
        if self.date_to:
            clauses.append("create_datetime < %s")
            params.append(datetime.datetime.combine(self.date_to + datetime.timedelta(days=1), datetime.time.min))
        if self.side:
            clauses.append("defect_side = %s")
            params.append(self.side)
        if self.defect_type:
            clauses.append("defect_type = %s")
            params.append(self.defect_type)
        if self.wheel:
            clauses.append("wheel_no = %s")     # wheel_no is text (CONTRACTS 5)
            params.append(self.wheel)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


class SiteDB:
    def __init__(self, db_cfg: dict):
        self.cfg = db_cfg
        self._cache = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("name"))

    def _connect(self):
        if not self.configured:
            raise SiteDBError("The site database is not configured (ui.json has no db.name).")
        try:
            import pymysql
            import pymysql.cursors
        except ImportError:
            raise SiteDBError("pymysql is not installed.")
        try:
            return pymysql.connect(host=self.cfg.get("host") or "localhost", port=int(self.cfg.get("port") or 3306),
                                   user=self.cfg.get("user") or "", password=self.cfg.get("password") or "",
                                   database=self.cfg["name"], connect_timeout=3, read_timeout=10,
                                   cursorclass=pymysql.cursors.DictCursor, autocommit=True)
        except Exception as e:
            log.warning("site DB not reachable: %s", e.__class__.__name__)
            raise SiteDBError("The site database is not reachable.") from None

    def _query(self, sql, params=()):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except SiteDBError:
            raise
        except Exception as e:
            log.warning("site DB query failed: %s", e)
            raise SiteDBError("The site database answered with an error.") from None
        finally:
            conn.close()

    def _cached(self, key, fn):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_S:
                return hit[1]
        value = fn()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    # ---------------------------------------------------------------- queries
    def ping(self) -> bool:
        self._query("SELECT 1 AS ok")
        return True

    def live_wheel(self):
        """wheel_counter_table row ID 1 (text), written by the side with writes_live_wheel_counter."""
        rows = self._cached("wheel", lambda: self._query("SELECT WHEEL_COUNT FROM wheel_counter_table WHERE ID = 1"))
        return rows[0]["WHEEL_COUNT"] if rows else None

    def last_defects(self, count=10):
        sql = f"SELECT {', '.join(DEFECT_COLUMNS)} FROM defects_table ORDER BY ID DESC LIMIT %s"
        return self._cached(("last", count), lambda: self._query(sql, (int(count),)))

    def defects(self, flt: DefectFilter, limit=50, offset=0):
        where, params = flt.where()
        total = self._query(f"SELECT COUNT(*) AS n FROM defects_table{where}", params)[0]["n"]
        rows = self._query(f"SELECT {', '.join(DEFECT_COLUMNS)} FROM defects_table{where} "
                           f"ORDER BY create_datetime DESC, ID DESC LIMIT %s OFFSET %s",
                           list(params) + [int(limit), int(offset)])
        return rows, total

    def defect(self, defect_id: int):
        rows = self._query(f"SELECT {', '.join(DEFECT_COLUMNS)} FROM defects_table WHERE ID = %s", (int(defect_id),))
        return rows[0] if rows else None

    def defect_types(self):
        rows = self._cached("types", lambda: self._query(
            "SELECT DISTINCT defect_type FROM defects_table ORDER BY defect_type"))
        return [r["defect_type"] for r in rows]

    def export_rows(self, flt: DefectFilter, max_rows=100000):
        where, params = flt.where()
        return self._query(f"SELECT {', '.join(DEFECT_COLUMNS)} FROM defects_table{where} "
                           f"ORDER BY create_datetime DESC, ID DESC LIMIT %s", list(params) + [int(max_rows)])


_db = None


def get_sitedb() -> SiteDB:
    global _db
    if _db is None:
        from dpc_ui.ui_config import get_config
        _db = SiteDB(get_config()["db"])
    return _db


def set_sitedb(db):
    global _db
    _db = db
