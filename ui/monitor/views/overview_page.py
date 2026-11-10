"""Level 1 Overview (D-101): one full render, then one poll returning small out-of-band fragments."""
from django.shortcuts import render
from django.views.decorators.http import require_GET

from monitor import level1
from monitor.views.common import gather


def _context(oob=False):
    state = gather()
    cfg = state["cfg"]
    return {"oob": oob, "active": "overview", "pill": level1.pill(state), "cycle": level1.cycle(state, cfg),
            "sides": level1.side_cards(state, cfg), "services": level1.services(state),
            "alerts": level1.alerts(state), "strip": level1.strip(state)}


@require_GET
def page(request):
    return render(request, "monitor/overview.html", _context())


@require_GET
def poll(request):
    return render(request, "monitor/partials/ov_oob.html", _context(oob=True))
