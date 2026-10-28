"""Log file handler of the old delete_script.py (SizeAndTimeRotatingFileHandler), unchanged:
rotate at 200 MB or at midnight, keep `retention_days` days of rotated files
(`log.backup_count` from cleanup.json; it was a fixed 5)."""

import datetime
import os
import time
from logging.handlers import BaseRotatingHandler

MAX_BYTES = 200 * 1024 * 1024


class SizeAndTimeRotatingFileHandler(BaseRotatingHandler):

    def __init__(self, filename, max_bytes=MAX_BYTES, retention_days=5, encoding="utf-8"):
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self.rollover_reason = None
        super().__init__(filename, mode="a", encoding=encoding, delay=False)
        self.next_rollover = self._next_midnight()

    @staticmethod
    def _next_midnight():
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def shouldRollover(self, record):
        self.rollover_reason = None
        if self.max_bytes > 0 and os.path.exists(self.baseFilename) \
                and os.path.getsize(self.baseFilename) >= self.max_bytes:
            self.rollover_reason = "size"
            return True
        if time.time() >= self.next_rollover:
            self.rollover_reason = "midnight"
            return True
        return False

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.rollover_reason == "midnight":
            stamp = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        if os.path.exists(self.baseFilename):
            os.rename(self.baseFilename, f"{self.baseFilename}.{stamp}")
        self._delete_old_logs()
        self.next_rollover = self._next_midnight()
        self.stream = self._open()

    def _delete_old_logs(self):
        cutoff = datetime.date.today() - datetime.timedelta(days=self.retention_days - 1)
        directory, name = os.path.split(self.baseFilename)
        for file in os.listdir(directory):
            if not file.startswith(name + "."):
                continue
            path = os.path.join(directory, file)
            try:
                file_date = datetime.datetime.strptime(file[len(name) + 1:len(name) + 11], "%Y-%m-%d").date()
                if os.path.isfile(path) and file_date < cutoff:
                    os.remove(path)
            except (ValueError, OSError):
                continue
