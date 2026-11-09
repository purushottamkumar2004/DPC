"""Writes events and state changes to the UI's SQLite DB (models.Event, models.KnownState).

Called from the MQTT thread and the watcher thread. Keeps the last known value per key in
memory as well, so the status messages repeated every 30 s cost no database access.
"""
import datetime
import json
import logging
import threading

from django.db import close_old_connections

from monitor.messages import EVENT_SEVERITY, event_text
from monitor.models import Event, KnownState
from monitor.secrets import mask

log = logging.getLogger("dpc.ui.events")


class EventStore:
    def __init__(self, now=datetime.datetime.now):
        self._now = now
        self._known = {}          # key -> (value, changed_at datetime)
        self._lock = threading.Lock()

    def record(self, topic, source, name, payload) -> Event:
        text = event_text(source, name, payload)
        event = Event.objects.create(
            received_at=self._now(), topic=topic[:200], source=str(source)[:100], name=str(name)[:100],
            severity=EVENT_SEVERITY.get(text.level, "info"),
            message=mask(text.sentence)[:500], action=mask(text.action)[:300],
            payload=mask(json.dumps(payload, default=str, indent=1))[:20000])
        close_old_connections()
        return event

    def transition(self, key, value):
        """(changed, previous value or None, since epoch seconds) for `key` now having `value`."""
        with self._lock:
            known = self._known.get(key)
            if known is None:
                row = KnownState.objects.filter(key=key).first()
                known = (row.value, row.changed_at) if row else None
            if known is not None and known[0] == value:
                self._known[key] = known
                return False, known[0], known[1].timestamp()
            now = self._now()
            KnownState.objects.update_or_create(key=key, defaults={"value": value[:120], "changed_at": now})
            self._known[key] = (value, now)
            close_old_connections()
            return True, (known[0] if known else None), now.timestamp()

    def prune(self, keep_days: int, keep_rows: int) -> int:
        """Delete events older than keep_days and beyond the newest keep_rows."""
        cutoff = self._now() - datetime.timedelta(days=keep_days)
        deleted, _ = Event.objects.filter(received_at__lt=cutoff).delete()
        # ids grow with time: everything at or below the (keep_rows+1)-th newest id goes
        beyond = list(Event.objects.order_by("-id").values_list("id", flat=True)[keep_rows:keep_rows + 1])
        if beyond:
            extra, _ = Event.objects.filter(id__lte=beyond[0]).delete()
            deleted += extra
        close_old_connections()
        return deleted
