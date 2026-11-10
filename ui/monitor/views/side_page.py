"""Level 2 side pages /side/LEFT/ and /side/RIGHT/ (D-101): one template for both sides."""
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from monitor import level2
from monitor.sources.detections import get_reader
from monitor.views.common import gather


def _context(side):
    state = gather()
    page = level2.side_page(state, state["cfg"], side.upper())
    if page is None:
        raise Http404("unknown side")
    return {"s": page, "active": f"side-{page['side']}",
            "cam": {"side": page["side"], "camera_id": page["camera_id"], "stale_text": page["stale_text"]}}


@require_GET
def page(request, side):
    context = _context(side)
    context["tab"] = "detections" if request.GET.get("tab") == "detections" else "live"
    if context["tab"] == "detections":
        context["det"] = get_reader().latest(level2.state_folder(gather()["cfg"], context["s"]["inspection_worker"]))
    return render(request, "monitor/side.html", context)


@require_GET
def part(request, side):
    """The parts that change while the page is open: header, functions, auto-ROI counts."""
    return render(request, "monitor/partials/side_oob.html", dict(_context(side), oob=True))


@require_GET
def roi_file(request, side, name):
    """A pair preview or an out-of-bound crop of this side (only names inspection lists)."""
    context = _context(side)
    path = level2.roi_file_allowed(gather()["cfg"], context["s"]["inspection_worker"], name)
    if path is None:
        raise Http404("not an auto-ROI image of this side")
    return FileResponse(open(path, "rb"), content_type="image/jpeg")
