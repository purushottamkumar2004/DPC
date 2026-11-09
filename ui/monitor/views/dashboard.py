"""Dashboard (D-095): banner, both sides with live image, tiles, live wheel, last defects."""
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dpc_ui.ui_config import get_config
from monitor import overview
from monitor.messages import plc_text
from monitor.sources import files, system
from monitor.sources.sitedb import SiteDBError, get_sitedb
from monitor.views.common import gather


def _side_context(state, side_name):
    for s in state["sides"]:
        if s["side"] == side_name:
            return s
    raise Http404("unknown side")


def _tiles_context(state):
    progs = state["programs"]
    wheel, wheel_error = None, ""
    try:
        wheel = get_sitedb().live_wheel()
    except SiteDBError as e:
        wheel_error = str(e)
    return {
        "plc": state["plc"], "plc_text": plc_text(state["plc"]), "plc_expected": state["plc_expected"],
        "disk": state["disk"], "gpu": system.gpu(),
        "mail": overview.program_by_id(progs, "mail"), "cleanup": overview.program_by_id(progs, "cleanup"),
        "wheel": wheel, "wheel_error": wheel_error,
        "total_wheels": state["cfg"]["machine"].get("total_wheel_count"),
    }


def _recent_context():
    try:
        return {"defects": get_sitedb().last_defects(10), "defects_error": ""}
    except SiteDBError as e:
        return {"defects": [], "defects_error": str(e)}


@require_GET
def page(request):
    state = gather()
    context = {"state": state, "banner": state["banner"], "sides": state["sides"], "active": "dashboard"}
    context.update(_tiles_context(state))
    context.update(_recent_context())
    return render(request, "monitor/dashboard.html", context)


@require_GET
def banner(request):
    state = gather()
    return render(request, "monitor/partials/banner.html", {"banner": state["banner"]})


@require_GET
def side(request, side):
    state = gather()
    return render(request, "monitor/partials/side_status.html", {"s": _side_context(state, side.upper())})


@require_GET
def tiles(request):
    return render(request, "monitor/partials/tiles.html", _tiles_context(gather()))


@require_GET
def recent_defects(request):
    return render(request, "monitor/partials/recent_defects.html", _recent_context())


@require_GET
def live_image(request, camera_id):
    """The current frame of one configured camera, downscaled (read-only, D-031)."""
    cfg = get_config()
    if camera_id not in {c["id"] for c in cfg["cameras"]}:
        raise Http404("unknown camera")
    try:
        width = max(160, min(1920, int(request.GET.get("w", files.LIVE_WIDTH))))
    except ValueError:
        width = files.LIVE_WIDTH
    jpeg = files.live_image(cfg.paths["frames"], camera_id, width)
    if jpeg is None:
        raise Http404("no live image")
    response = HttpResponse(jpeg, content_type="image/jpeg")
    response["Cache-Control"] = "no-store"
    return response
