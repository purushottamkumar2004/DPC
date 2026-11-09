from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_pragmas(sender, connection, **kwargs):
    """WAL lets request threads read while the MQTT thread writes events."""
    if connection.vendor == "sqlite":
        with connection.cursor() as cur:
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")


class MonitorConfig(AppConfig):
    name = "monitor"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # No threads are started here (ready() also runs for manage.py and tests);
        # run_ui.py starts the MQTT cache and the watchers.
        connection_created.connect(_sqlite_pragmas)
