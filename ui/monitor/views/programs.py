"""Programs: every worker with status, since, restarts, heartbeat age and last error, and a
filterable tail of its log."""
import json

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dpc_ui.ui_config import get_config
from monitor import overview
from monitor.secrets import mask
from monitor.sources import files
from monitor.views.common import gather

LOG_LINES = 200


def _program(worker_id):
    state = gather()
    program = overview.program_by_id(state["programs"], worker_id)
    if program is None:
        raise Http404("unknown program")
    return state, program


@require_GET
def page(request):
    state = gather()
    return render(request, "monitor/programs.html", {"programs": state["programs"], "active": "programs"})


@require_GET
def part(request):
    return render(request, "monitor/partials/programs_table.html", {"programs": gather()["programs"]})


@require_GET
def detail(request, worker_id):
    state, program = _program(worker_id)
    raw = mask(json.dumps(program["status"], indent=2, default=str)) if program["status"] else ""
    error = program["last_error"] or {}
    return render(request, "monitor/program.html", {
        "p": program, "raw_status": raw, "active": "programs",
        "error_message": mask(str(error.get("message", ""))),
        "error_traceback": mask("\n".join(error["traceback"]) if isinstance(error.get("traceback"), list)
                                else str(error.get("traceback", ""))),
        "log_text": request.GET.get("q", ""), "log_level": request.GET.get("level", ""),
    })


@require_GET
def log_part(request, worker_id):
    _, program = _program(worker_id)
    lines, error = files.log_tail(program["log_file"], get_config().paths["logs"], LOG_LINES,
                                  text=request.GET.get("q", ""), level=request.GET.get("level", ""))
    return render(request, "monitor/partials/log_tail.html", {"lines": lines, "error": error})
