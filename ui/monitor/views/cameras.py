"""Camera page per side (drill-down from the dashboard camera card, D-098 5): large live image,
camera and inspection programs, rates with sparklines, auto-ROI per class. Existing sources only."""
import json
import os

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from monitor import dash
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


@require_GET
def page(request, side):
    return render(request, "monitor/camera.html", _context(side))


@require_GET
def part(request, side):
    return render(request, "monitor/partials/camera_body.html", _context(side))
