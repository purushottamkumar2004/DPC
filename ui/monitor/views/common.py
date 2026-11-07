"""Shared view helpers: one place that gathers the whole state for a page render."""
import datetime

from dpc_ui.ui_config import get_config
from monitor import overview
from monitor.models import Event
from monitor.sources import files, system
from monitor.sources.workers_file import configured_workers
from monitor.state import get_cache


def gather():
    """Everything the dashboard, alerts and programs pages need, read once per request."""
    cfg = get_config()
    snapshot = get_cache().snapshot()
    configured = configured_workers(cfg.paths["workers_file"])
    progs = overview.programs(snapshot, configured)
    side_views = overview.sides(cfg, progs, files.get_meter())
    disk = system.disk(cfg.paths["data"], cfg["cleanup"]["disk_percent"])
    plc_prog = overview.program_by_id(progs, "plc")
    plc_expected = bool(plc_prog and plc_prog["enabled"]) or snapshot.get("plc") is not None
    hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
    try:
        drift_events = Event.objects.filter(name="roi_drift", received_at__gte=hour_ago).count()
    except Exception:
        drift_events = 0
    found = overview.problems(snapshot, progs, side_views, disk, plc_expected=plc_expected,
                              drift_events=drift_events)
    return {"cfg": cfg, "snapshot": snapshot, "programs": progs, "sides": side_views, "disk": disk,
            "problems": found, "banner": overview.banner(found), "now": snapshot["now"],
            "plc": (snapshot.get("plc") or {}).get("payload"), "plc_expected": plc_expected}
