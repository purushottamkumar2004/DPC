"""What remains of the UI-1b dashboard views after D-101: the status pill fragment used in every
page's top bar, and the plain live image (read-only, D-031; no overlay, F66)."""
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dpc_ui.ui_config import get_config
from monitor import level1
from monitor.sources import files
from monitor.views.common import gather


@require_GET
def banner(request):
    return render(request, "monitor/partials/ov_pill.html", {"pill": level1.pill(gather())})


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
