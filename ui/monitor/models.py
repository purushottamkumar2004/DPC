"""UI-owned data in SQLite (D-095): stored events, so the alert page survives a UI restart.

Never in the site MySQL DB. Heartbeats are not stored (every few seconds, only the latest
matters, kept in memory by the state cache).
"""
from django.db import models


class Event(models.Model):
    """One MQTT event, or a change the UI noticed itself (a worker status changing, the PLC
    disconnecting, the disk crossing a threshold). Newest first on the alert page."""
    SEVERITIES = [("info", "info"), ("warning", "warning"), ("error", "error")]

    received_at = models.DateTimeField(db_index=True)
    topic = models.CharField(max_length=200)
    source = models.CharField(max_length=100, db_index=True)   # worker id, "supervisor", "plc", "disk"
    name = models.CharField(max_length=100)                     # event name, e.g. worker_failure
    severity = models.CharField(max_length=10, choices=SEVERITIES, db_index=True)
    message = models.CharField(max_length=500)                  # plain-language sentence
    action = models.CharField(max_length=300, blank=True)       # what a person should do
    payload = models.TextField(blank=True)                      # the original JSON (details)

    class Meta:
        ordering = ["-received_at", "-id"]
        indexes = [models.Index(fields=["severity", "received_at"])]

    def __str__(self):
        return f"{self.received_at} {self.source} {self.name}"


class KnownState(models.Model):
    """Last state the UI recorded per subject (worker status, supervisor, PLC, disk level), so
    the retained messages replayed after a reconnect or a UI restart do not create duplicates."""
    key = models.CharField(max_length=120, primary_key=True)
    value = models.CharField(max_length=120)
    changed_at = models.DateTimeField()
