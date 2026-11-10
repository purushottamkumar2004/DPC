"""Camera page per side (drill-down from the dashboard camera card, D-098 5): large live image,
camera and inspection programs, rates with sparklines, auto-ROI per class. Existing sources only."""
import json
import os

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from monitor import dash
from monitor.sources.detections import get_reader
from monitor.views.common import gather


def _roi_classes(data_dir, side):
    """Per class: inside, outside, recent outside ratio, drift (from roi_status.json)."""
    path = os.path.join(data_dir, "state", f"inspection_{side.lower()}", "roi_status.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rows = []
    for cls, entry in sorted((data.get("classes") or {}).items()):
        if isinstance(entry, dict):
            rows.append({"cls": cls, "inside": entry.get("inside", 0), "outside": entry.get("outside", 0),
                         "ratio": entry.get("recent_outside_ratio"), "drift": bool(entry.get("drift"))})
    return rows


def _context(side):
    state = gather()
    cards = dash.camera_cards(state, state["cfg"])
    card = next((c for c in cards if c["side"] == side.upper()), None)
    if card is None:
        raise Http404("unknown side")
    view = next(s for s in state["sides"] if s["side"] == card["side"])
    return {"cam": card, "view": view, "sides": [c["side"] for c in cards], "active": "cameras",
            "roi_classes": _roi_classes(state["cfg"].paths["data"], card["side"]), "banner": state["banner"]}


def _detections_folder(side):
    """<data>/state/<inspection worker of this side>/ (F65 writes the pair there)."""
    state = gather()
    for s in state["cfg"]["sides"]:
        if s["side"] == side.upper():
            return os.path.join(state["cfg"].paths["data"], "state", s["inspection_worker"])
    raise Http404("unknown side")


@require_GET
def page(request, side):
    context = _context(side)
    context["tab"] = "detections" if request.GET.get("tab") == "detections" else "live"
    if context["tab"] == "detections":
        context["det"] = get_reader().latest(_detections_folder(side))
    return render(request, "monitor/camera.html", context)


@require_GET
def detections(request, side):
    """The latest matched pair as JSON for the Detections tab (F66); its image: frame/<seq>.jpg."""
    data = get_reader().latest(_detections_folder(side))
    if data.get("available"):
        data["image"] = f"/cameras/{side.upper()}/frame/{data['seq']}.jpg"
    response = JsonResponse(data)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def frame(request, side, seq):
    jpeg = get_reader().image(_detections_folder(side), int(seq))
    if jpeg is None:
        raise Http404("pair no longer kept")
    response = HttpResponse(jpeg, content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=60"       # a seq never changes
    return response


@require_GET
def part(request, side):
    return render(request, "monitor/partials/camera_body.html", _context(side))
