"""Dashboard (D-098, docs/ui/dashboard_reference.html).

The full page renders once; then one request every refresh_seconds (/part/dash/) returns every
small fragment marked hx-swap-oob, so values change in place without reloading the page or the
camera images. The older fragment URLs stay (same fragments, one each).
"""
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dpc_ui.ui_config import get_config
from monitor import dash
from monitor.sources import files
from monitor.sources.sitedb import SiteDBError, get_sitedb
from monitor.views.common import gather


def _context(oob=False):
    state = gather()
    cfg = state["cfg"]
    wheel, wheel_error = None, ""
    try:
        wheel = get_sitedb().live_wheel()
    except SiteDBError as e:
        wheel_error = str(e)
    defects, defects_error = dash.recent_defects()
    return {"state": state, "oob": oob, "active": "dashboard",
            "top": dash.top_bar(state, wheel, wheel_error, cfg["machine"].get("total_wheel_count")),
            "cams": dash.camera_cards(state, cfg), "strip": dash.device_strip(state),
            "alerts": dash.recent_alerts(state), "defects": defects, "defects_error": defects_error}


@require_GET
def page(request):
    return render(request, "monitor/dashboard.html", _context())


@require_GET
def poll(request):
    """Every fragment of the dashboard, each swapped into its own element (hx-swap-oob)."""
    return render(request, "monitor/partials/dash_oob.html", _context(oob=True))


@require_GET
def banner(request):
    return render(request, "monitor/partials/banner.html", _context())


@require_GET
def side(request, side):
    context = _context()
    for cam in context["cams"]:
        if cam["side"] == side.upper():
            context["cam"] = cam
            return render(request, "monitor/partials/side_status.html", context)
    raise Http404("unknown side")


@require_GET
def tiles(request):
    return render(request, "monitor/partials/tiles.html", _context())


@require_GET
def recent_defects(request):
    return render(request, "monitor/partials/recent_defects.html", _context())


@require_GET
def live_image(request, camera_id):
    """The current frame of one configured camera, downscaled (read-only, D-031). Plain image only:
    no overlay, boxes or bands (F66)."""
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
