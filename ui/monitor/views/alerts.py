"""Alerts and errors: what is wrong now, then the stored history (newest first)."""
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from monitor.models import Event
from monitor.views.common import gather

PER_PAGE = 100
LEVELS = {"all": None, "warning": ["warning", "error"], "error": ["error"]}


def _context(request):
    state = gather()
    level = request.GET.get("level", "warning")
    if level not in LEVELS:
        level = "warning"
    events = Event.objects.all()
    if LEVELS[level]:
        events = events.filter(severity__in=LEVELS[level])
    source = request.GET.get("source", "")
    if source:
        events = events.filter(source=source)
    page = Paginator(events, PER_PAGE).get_page(request.GET.get("page"))
    sources = Event.objects.order_by().values_list("source", flat=True).distinct()
    return {"problems": state["problems"], "banner": state["banner"], "page": page, "level": level,
            "source": source, "sources": sorted(set(sources)), "query": request.GET.urlencode(),
            "active": "alerts"}


@require_GET
def page(request):
    return render(request, "monitor/alerts.html", _context(request))


@require_GET
def part(request):
    return render(request, "monitor/partials/alerts_body.html", _context(request))
