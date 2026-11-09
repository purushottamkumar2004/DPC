"""System health: disk, GPU, CPU/RAM, MySQL, MQTT, Supervisor, PLC, cameras, dpc-check."""
import time

from django.http import HttpResponseNotAllowed
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dpc_ui.ui_config import get_config
from monitor.sources import files, system
from monitor.sources.dpc_check import get_runner
from monitor.sources.sitedb import SiteDBError, get_sitedb
from monitor.state.samples import get_samples
from monitor.views.common import gather

_mysql_cache = {"at": 0.0, "value": None}
MYSQL_CACHE_S = 5


def _mysql():
    now = time.time()
    if _mysql_cache["value"] is None or now - _mysql_cache["at"] > MYSQL_CACHE_S:
        try:
            get_sitedb().ping()
            value = {"ok": True, "error": ""}
        except SiteDBError as e:
            value = {"ok": False, "error": str(e)}
        _mysql_cache.update(at=now, value=value)
    return _mysql_cache["value"]


def _context():
    state = gather()
    cfg = get_config()
    snap = state["snapshot"]
    meter = files.get_meter()
    cameras = []
    for cam in cfg["cameras"]:
        cameras.append({"id": cam["id"], "side": cam["side"], "serial": cam.get("serial"), "ip": cam.get("ip"),
                        "ping": system.ping_result(cam["ip"]) if cam.get("ip") else None,
                        "frames": meter.reading(cam["id"]) if meter else {"fps": None, "age_s": None}})
    sup = snap.get("supervisor") or {}
    return {
        "disk": state["disk"], "gpu": system.gpu(), "cpu": system.cpu_ram(), "mysql": _mysql(),
        "db_name": cfg["db"].get("name"),
        "mqtt": {"connected": snap["connected"],
                 "last_message_age_s": (snap["now"] - snap["last_message_at"]) if snap["last_message_at"] else None,
                 "host": cfg["mqtt"].get("host"), "port": cfg["mqtt"].get("port")},
        "supervisor": {"state": (sup.get("payload") or {}).get("status"),
                       "age_s": (snap["now"] - sup["received_at"]) if sup else None},
        "plc": state["plc"], "plc_expected": state["plc_expected"], "cameras": cameras,
        "now": snap["now"], "disk_spark": get_samples().values("disk"), "gpu_spark": get_samples().values("gpu_temp"),
    }


@require_GET
def page(request):
    context = _context()
    context.update(active="health", check=get_runner().snapshot())
    return render(request, "monitor/health.html", context)


@require_GET
def part(request):
    return render(request, "monitor/partials/health_body.html", _context())


def dpc_check_start(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    get_runner().start()
    return render(request, "monitor/partials/dpc_check.html", {"check": get_runner().snapshot()})


@require_GET
def dpc_check_part(request):
    return render(request, "monitor/partials/dpc_check.html", {"check": get_runner().snapshot()})
