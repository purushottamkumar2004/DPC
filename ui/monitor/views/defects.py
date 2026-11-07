"""Defects: filter by date, side, type and wheel; one defect with its image; CSV export.
Read-only on the site DB (CONTRACTS 5)."""
import csv
import datetime

from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dpc_ui.ui_config import get_config
from monitor.sources import files
from monitor.sources.sitedb import DefectFilter, SiteDBError, get_sitedb

PER_PAGE = 50
SIDES = ["Left", "Right"]            # defect_side values (CONTRACTS 5)


def _date(text):
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _filter(request):
    return DefectFilter(date_from=_date(request.GET.get("from")), date_to=_date(request.GET.get("to")),
                        side=request.GET.get("side") if request.GET.get("side") in SIDES else "",
                        defect_type=request.GET.get("type", ""), wheel=request.GET.get("wheel", ""))


@require_GET
def page(request):
    flt = _filter(request)
    try:
        page_no = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page_no = 1
    context = {"flt": flt, "sides": SIDES, "active": "defects", "page_no": page_no, "error": "",
               "rows": [], "total": 0, "types": [],
               "query": request.GET.copy()}
    context["query"].pop("page", None)
    context["query"] = context["query"].urlencode()
    try:
        db = get_sitedb()
        context["types"] = db.defect_types()
        rows, total = db.defects(flt, PER_PAGE, (page_no - 1) * PER_PAGE)
        context.update(rows=rows, total=total, pages=max(1, (total + PER_PAGE - 1) // PER_PAGE))
    except SiteDBError as e:
        context["error"] = str(e)
    return render(request, "monitor/defects.html", context)


@require_GET
def detail(request, defect_id):
    try:
        row = get_sitedb().defect(defect_id)
    except SiteDBError as e:
        return render(request, "monitor/defect.html", {"row": None, "error": str(e), "active": "defects"})
    if row is None:
        raise Http404("no such defect")
    return render(request, "monitor/defect.html", {"row": row, "error": "", "active": "defects"})


@require_GET
def image(request, defect_id):
    """The saved defect image; inspection already drew the box on it (defects.py _save_pair)."""
    try:
        row = get_sitedb().defect(defect_id)
    except SiteDBError:
        raise Http404("database not reachable")
    if row is None:
        raise Http404("no such defect")
    jpeg, reason = files.defect_image(row.get("view_defect"), get_config().paths["data"])
    if jpeg is None:
        raise Http404(reason)
    return HttpResponse(jpeg, content_type="image/jpeg")


class _Echo:
    def write(self, value):
        return value


@require_GET
def export_csv(request):
    try:
        rows = get_sitedb().export_rows(_filter(request))
    except SiteDBError as e:
        return HttpResponse(str(e), status=503, content_type="text/plain")
    columns = ["ID", "create_datetime", "defect_side", "defect_type", "wheel_no",
               "xmin", "xmax", "ymin", "ymax", "is_alert_sent", "view_defect"]
    writer = csv.writer(_Echo())
    lines = (writer.writerow([r.get(c) for c in columns]) for r in rows)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    response = StreamingHttpResponse(
        (line for chunk in ([writer.writerow(columns)], lines) for line in chunk), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="defects_{stamp}.csv"'
    return response
