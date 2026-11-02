"""The UI's PLC lamp: CAMERA_PLC_STATUS.PLC ('True'/'False', row id 1, CONTRACTS 5).

Nobody wrote this column before (the old PLC scripts had no DB); 'True' = the PLC answers.
Written when reachability changes and otherwise every LAMP_INTERVAL_S (the captures refresh
their own lamps about every 120 s). A DB problem is logged once and never stops the service.
"""

import time
from typing import Callable, Optional

LAMP_INTERVAL_S = 120
SQL = "UPDATE CAMERA_PLC_STATUS SET PLC = %s WHERE id = 1"


def _pymysql_connect(db):
    import pymysql
    return pymysql.connect(host=db.host, port=db.port, user=db.user, password=db.password,
                           database=db.name, connect_timeout=5, autocommit=True)


class PlcLamp:
    def __init__(self, db_settings, log, connect: Callable = _pymysql_connect,
                 clock: Callable = time.monotonic):
        self.db = db_settings
        self.log = log
        self.connect = connect
        self.clock = clock
        self.last_value: Optional[bool] = None
        self.next_write = 0.0
        self.failing = False

    def update(self, connected: bool):
        if self.db is None:
            return
        now = self.clock()
        if now < self.next_write and (self.failing or connected == self.last_value):
            return                                           # nothing new, or wait after a DB error
        try:
            conn = self.connect(self.db)
            try:
                with conn.cursor() as cur:
                    cur.execute(SQL, ("True" if connected else "False",))
            finally:
                conn.close()
        except Exception as e:
            if not self.failing:
                self.log.warning(f"CAMERA_PLC_STATUS.PLC not written: {e}")
            self.failing = True
            self.next_write = now + LAMP_INTERVAL_S          # retry later, not every tick
            return
        if self.failing:
            self.log.info("CAMERA_PLC_STATUS.PLC written again")
        self.failing = False
        self.last_value = connected
        self.next_write = now + LAMP_INTERVAL_S
