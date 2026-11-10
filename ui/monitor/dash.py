"""What the D-098 dashboard shows, built from the UI-1 state (no new data sources).

Layout (docs/ui/dashboard_reference.html): top bar (site, clock, status pill, wheel progress),
two camera cards (image, fps, four compact tiles: camera, inspection, model, auto-ROI), one
device strip (PLC, light, conveyor, disk, GPU, mail), then alerts and last defects.

Every value is a Cell: short text, a level (ok / warning / error / off / neutral), one tooltip
sentence and the page it drills down to. Colour carries meaning only (D-098 2): a value with no
OK/attention/failure meaning is "neutral".
"""
import datetime
from dataclasses import dataclass, field

from monitor import overview
from monitor.messages import ERROR, OFF, OK, WARNING, plc_text
from monitor.models import Event
from monitor.sources import files, system
from monitor.sources.sitedb import SiteDBError, get_sitedb
from monitor.state.samples import get_samples

NEUTRAL = "neutral"
GPU_WARN_C = 75          # attention from this GPU temperature (assumed; no site value yet)
GPU_ERROR_C = 85
ALERT_ROWS = 7
DEFECT_ROWS = 7


@dataclass
class Cell:
    label: str
    text: str
    level: str = NEUTRAL
    tip: str = ""
    link: str = ""
    spark: list = field(default_factory=list)


def _program_cell(label, program, link):
    """Camera / inspection state in a few words, the full sentence in the tooltip."""
    if program is None:
        return Cell(label, "Not configured", OFF, f"No {label.lower()} program is configured for this side.", link)
    words = {"RUNNING": "Running", "STARTING": "Starting", "UNHEALTHY": "Not healthy", "WAITING_RESTART": "Restarting",
             "STOPPED": "Stopped", "PAUSED": "Paused", "IDLE": "Waiting", "FAILED": "Failed", "NO_STATUS": "No status",
             "DISABLED": "Off"}
    level = {"ok": OK, "info": OK, "warning": WARNING, "error": ERROR, "off": OFF}.get(program["text"].level, NEUTRAL)
    tip = program["text"].sentence + (" " + program["text"].action if program["text"].action else "")
    return Cell(label, words.get(program["value"], program["value"].title()), level, tip, link)


def camera_cards(state, cfg):
    samples = get_samples()
    cards = []
    for s in state["sides"]:
        side, cam_id = s["side"], s["camera_id"]
        capture, inspection = s["camera"], s["inspection"]
        fps, age = s["frames"]["fps"], s["frames"]["age_s"]

        camera = _program_cell("Camera", capture, f"/cameras/{side}/")
        if age is not None:
            camera.text += f" · {overview.age_text(age)}"
            camera.tip += f" The newest image is {overview.age_text(age)} old."
            if camera.level == OK and age > overview.STALE_IMAGE_S:
                camera.level = WARNING
        failed = bool(capture and capture["value"] in ("FAILED", "STOPPED", "WAITING_RESTART", "UNHEALTHY"))
        fps_cell = Cell("fps", f"{fps:.1f} fps" if fps is not None else "— fps",
                        ERROR if failed else OK if fps else (WARNING if capture and capture["value"] == "RUNNING" else OFF),
                        "Images per second written by the camera program, measured by the UI from the live image.",
                        f"/cameras/{side}/", samples.values(f"fps:{cam_id}"))

        insp = _program_cell("Inspection", inspection,
                             f"/programs/{inspection['id']}/" if inspection else "/programs/")
        queue = s["queue"]
        if inspection and inspection["value"] == "RUNNING":
            rate = (f"{s['ms_per_image']:.0f} ms/img" if s["ms_per_image"] is not None
                    else f"{s['inspection_fps']:.1f} img/s" if s["inspection_fps"] is not None else "—")
            insp.text = rate + (f" · {queue} waiting" if queue is not None else "")
            if s.get("waiting_for_plate"):
                insp.text = "Waiting for plate" + (f" · {queue} waiting" if queue is not None else "")
                insp.level = WARNING
            insp.tip = (inspection["text"].sentence + " "
                        + ("Processing time per image" if s["ms_per_image"] is not None
                           else "Images inspected per second (ms per image is not published yet)")
                        + (f"; {queue} images are waiting in the queue." if queue is not None else ".")
                        + (" Inspection is paused until the next plate starts a new cycle (after a start or "
                           "restart this takes up to one revolution)." if s.get("waiting_for_plate") else ""))
            if queue is not None and queue >= overview.QUEUE_WARN:
                insp.level = WARNING
            insp.spark = samples.values(f"ms:{side}") or samples.values(f"ips:{side}")
        model = _model_cell(s["model"], f"/programs/{inspection['id']}/" if inspection else "/programs/")
        stale = age is not None and age > overview.STALE_IMAGE_S
        cards.append({"side": side, "camera_id": cam_id, "fps": fps_cell, "camera": camera,
                      "stale_text": f"Last image {overview.age_text(age)} old" if stale else "",
                      "inspection": insp, "model": model, "roi": _roi_cell(s, side),
                      "link": f"/cameras/{side}/"})
    return cards


def _model_cell(model, link):
    """R6 (F65): the inspection heartbeat's model {name, version}; null while the model loads."""
    if model == "absent" or (model is not None and not isinstance(model, dict)):
        return Cell("Model", "—", NEUTRAL, "This inspection does not publish its model yet.", link)
    if model is None:
        return Cell("Model", "Loading", NEUTRAL, "Inspection is loading its model.", link)
    name, version = model.get("name") or "?", model.get("version") or "?"
    return Cell("Model", f"{name} {version}", NEUTRAL, f"Model {name}, version {version} (from inspection).", link)


def _roi_cell(s, side):
    roi = s["roi"]
    link = f"/cameras/{side}/"
    if roi is None:
        return Cell("Auto-ROI", "—", OFF, "No auto-ROI status from this side's inspection.", link)
    mode = (roi.get("mode") or "?").lower()
    removed = roi.get("outside", 0)
    if mode == "off":
        return Cell("Auto-ROI", "Off", OFF, "Auto-ROI is switched off: all detections are used.", link)
    if not roi.get("learned"):
        return Cell("Auto-ROI", f"{mode.title()} · not learned", WARNING,
                    "The areas of interest are not learned yet, so nothing is filtered.", link)
    verb = "removed" if mode == "filter" else "outside"
    level = WARNING if roi.get("drifting") else OK
    tip = (f"Mode {mode}: since inspection started, {removed} detections were outside their learned area"
           + (" and were removed." if mode == "filter" else " (observe: kept, only counted).")
           + (f" Drift on {', '.join(roi['drifting'])}: the camera may have moved." if roi.get("drifting") else ""))
    return Cell("Auto-ROI", f"{mode.title()} · {removed} {verb}", level, tip, link)


def device_strip(state):
    samples = get_samples()
    plc, expected = state["plc"], state["plc_expected"]
    cells = []
    if not expected:
        cells += [Cell("PLC", "None", OFF, "No PLC service on this site.", "/health/"),
                  Cell("Light", "—", OFF, "No PLC service on this site.", "/health/"),
                  Cell("Conveyor", "—", OFF, "No PLC service on this site.", "/health/")]
    elif not plc:
        cells += [Cell("PLC", "No status", WARNING, plc_text(None).sentence, "/health/"),
                  Cell("Light", "—", NEUTRAL, "Unknown until the PLC service reports.", "/health/"),
                  Cell("Conveyor", "—", NEUTRAL, "Unknown until the PLC service reports.", "/health/")]
    else:
        text = plc_text(plc)
        connected = bool(plc.get("connected"))
        cells.append(Cell("PLC", "Connected" if connected else "Disconnected", OK if connected else ERROR,
                          text.sentence + (" " + text.action if text.action else ""), "/health/"))
        light_ok = plc.get("light_ok")
        cells.append(Cell("Light", "On" if light_ok else ("Not confirmed" if light_ok is False else "—"),
                          OK if light_ok else (WARNING if light_ok is False else NEUTRAL),
                          "The conveyor light; the PLC service switches it on and checks it every minute.", "/health/"))
        value = plc.get("conveyor_status")
        cells.append(Cell("Conveyor", f"value {value}" if value is not None else "—", NEUTRAL,
                          "Raw conveyor status register from the PLC; its meaning is not confirmed yet.", "/health/"))
    disk = state["disk"]
    if disk.get("ok"):
        level = ERROR if disk["level"] == "mail" else WARNING if disk["level"] == "delete" else NEUTRAL
        thr = disk["thresholds"]
        cells.append(Cell("Disk", f"{disk['percent']:.0f} %", level,
                          f"Data disk {disk['percent']:.0f} % used, {disk['free_gb']:.0f} GB free; the cleanup deletes "
                          f"old images above {thr.get('delete')} %, alarm at {thr.get('mail')} %.", "/health/",
                          samples.values("disk")))
    else:
        cells.append(Cell("Disk", "—", WARNING, f"Disk usage cannot be read: {disk.get('error')}", "/health/"))
    gpu = system.gpu()
    if gpu.get("ok") and gpu.get("gpus"):
        g = gpu["gpus"][0]
        temp = g.get("temperature_c")
        level = NEUTRAL if temp is None else ERROR if temp >= GPU_ERROR_C else WARNING if temp >= GPU_WARN_C else OK
        cells.append(Cell("GPU", f"{temp:.0f} °C" if temp is not None else "—", level,
                          f"GPU temperature; load {g.get('load_percent') or 0:.0f} %, "
                          f"attention from {GPU_WARN_C} °C.", "/health/", samples.values("gpu_temp")))
    else:
        cells.append(Cell("GPU", "—", WARNING, gpu.get("error") or "No GPU reading.", "/health/"))
    mail = overview.program_by_id(state["programs"], "mail")
    if mail is None:
        cells.append(Cell("Mail", "None", OFF, "No defect mail program on this site.", "/programs/"))
    else:
        cell = _program_cell("Mail", mail, "/programs/mail/")
        if cell.level == OFF:
            cell.text = "Off"
        cells.append(cell)
    return cells


def top_bar(state, wheel, wheel_error, total):
    banner = state["banner"]
    if banner["level"] == OK:
        pill = Cell("status", "All systems running", OK, banner["headline"] + " All programs and devices are OK.",
                    "/alerts/")
    else:
        more = f" (+{banner['count'] - 1})" if banner["count"] > 1 else ""
        pill = Cell("status", banner["headline"] + more, banner["level"],
                    banner["headline"] + " Click for all current problems.", "/alerts/")
    try:
        number = int(str(wheel).strip())
    except (TypeError, ValueError):
        number = None
    if wheel_error:
        progress = Cell("wheel", "Wheel —", NEUTRAL, wheel_error, "/defects/")
    elif number is not None and total:
        progress = Cell("wheel", f"Wheel {number} of {total}", NEUTRAL,
                        "Wheel counted since the last plate (live wheel number of this revolution).", "/defects/")
    else:
        progress = Cell("wheel", f"Wheel {wheel if wheel is not None else '—'}", NEUTRAL,
                        "Wheel counted since the last plate.", "/defects/")
    percent = max(0, min(100, round(100 * number / total))) if number is not None and total else 0
    return {"pill": pill, "wheel": progress, "wheel_percent": percent}


def recent_alerts(state):
    """Current problems first (marked 'now'), then the newest stored warnings and errors."""
    rows = []
    for p in state["problems"][:ALERT_ROWS]:
        rows.append({"id": None, "level": p.level, "text": p.sentence, "tip": p.action or p.sentence,
                     "when": "now", "link": p.link or "/alerts/", "icon": "circle-x" if p.level == ERROR else "triangle-alert"})
    active = {r["text"] for r in rows}
    if len(rows) < ALERT_ROWS:
        since = datetime.datetime.now() - datetime.timedelta(days=7)
        for e in Event.objects.filter(severity__in=["warning", "error"], received_at__gte=since)[:ALERT_ROWS * 2]:
            if len(rows) >= ALERT_ROWS:
                break
            if e.message in active:          # already shown as "now"
                continue
            icon = "circle-x" if e.severity == "error" else "rotate-ccw" if "restart" in e.name else "triangle-alert"
            rows.append({"id": e.id, "level": e.severity, "text": e.message, "tip": e.action or e.message,
                         "when": e.received_at.strftime("%H:%M") if e.received_at.date() == datetime.date.today()
                         else e.received_at.strftime("%d %b"), "link": f"/alerts/#e{e.id}", "icon": icon})
    return rows


def recent_defects():
    try:
        return get_sitedb().last_defects(DEFECT_ROWS), ""
    except SiteDBError as e:
        return [], str(e)
