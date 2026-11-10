"""Level 1 overview (D-101, ISA-101): the situation at a glance, data only (no images, no defect
list). Layout: docs/ui/overview_reference.html. Built from the UI-1 state; no new sources.

  top bar   site, clock, status pill (worst state, plain words), cycle "wheel N of T", last plate
  sides     per side: one-word state, camera, inspection, model, auto-ROI, function chips
  services  every program: plain name, state word, heartbeat age, one key value
  alerts    newest first, scrolls inside its card
  strip     PLC, light, conveyor, disk, GPU, database, MQTT
"""
import datetime

from monitor import overview
from monitor.dash import Cell, NEUTRAL, device_strip
from monitor.messages import ERROR, OFF, OK, WARNING
from monitor.models import Event
from monitor.sources import rendered
from monitor.sources.sitedb import SiteDBError, get_sitedb
from monitor.state.samples import get_plate_clock, get_samples

ALERT_ROWS = 60


def _ago(seconds):
    return overview.age_text(seconds)


# ------------------------------------------------------------------------------- top bar
def cycle(state, cfg):
    total = cfg["machine"].get("total_wheel_count")
    writer = rendered.wheel_writer_side(cfg)
    who = f"the {writer} inspection" if writer else "inspection"
    try:
        wheel = get_sitedb().live_wheel()
        db_error = ""
    except SiteDBError as e:
        wheel, db_error = None, str(e)
    try:
        number = int(str(wheel).strip())
    except (TypeError, ValueError):
        number = None
    if db_error:
        wheel_cell = Cell("Cycle", "Cycle: wheel —", NEUTRAL, db_error, "/defects/")
    elif number is None:
        wheel_cell = Cell("Cycle", "Cycle: wheel no value yet", NEUTRAL,
                          f"The live wheel number is written by {who} into wheel_counter_table (row ID 1); "
                          "there is no value yet.", "/defects/")
    else:
        wheel_cell = Cell("Cycle", f"Cycle: wheel {number}" + (f" of {total}" if total else ""), NEUTRAL,
                          f"Wheels counted since the last plate, written by {who}.", "/defects/")
    waiting = [s["side"] for s in state["sides"] if s.get("waiting_for_plate")]
    clock = get_plate_clock()
    if waiting:
        plate = Cell("plate", "waiting for plate", WARNING,
                     f"{' and '.join(waiting)} inspection waits for the plate that starts a new cycle "
                     "(after a start or restart this takes up to one revolution).")
    elif clock.last_plate_at:
        ago = state["now"] - clock.last_plate_at
        plate = Cell("plate", f"last plate {_ago(ago)} ago", NEUTRAL,
                     "Time since the wheel number last started again from the beginning (a new cycle).")
    else:
        plate = Cell("plate", "last plate not seen yet", NEUTRAL,
                     "The screen notes the plate when the wheel number starts again; none since it started.")
    percent = max(0, min(100, round(100 * number / total))) if number is not None and total else 0
    return {"wheel": wheel_cell, "plate": plate, "percent": percent}


def pill(state):
    banner = state["banner"]
    if banner["level"] == OK:
        return Cell("status", "All systems running", OK, "Every program and device is working.", "/system/alerts/")
    more = f" (+{banner['count'] - 1})" if banner["count"] > 1 else ""
    return Cell("status", banner["headline"] + more, banner["level"],
                banner["headline"] + " Click for the alert history.", "/system/alerts/")


# ------------------------------------------------------------------------------- side cards
def side_state(view):
    """One word for the side (D-101): Inspecting, Waiting for plate, Starting, Restarting, Stopped, Failed."""
    camera, inspection = view["camera"] or {}, view["inspection"] or {}
    cam_value, insp_value = camera.get("value"), inspection.get("value")
    if "FAILED" in (cam_value, insp_value):
        return "Failed", ERROR
    if insp_value in ("WAITING_RESTART", "UNHEALTHY") or cam_value in ("WAITING_RESTART", "UNHEALTHY"):
        return "Restarting", WARNING
    if insp_value == "STARTING":
        return "Starting", WARNING
    if insp_value == "RUNNING":
        if view.get("waiting_for_plate"):
            return "Waiting for plate", WARNING
        return "Inspecting", OK
    return "Stopped", (OFF if insp_value == "DISABLED" else ERROR)


def camera_row(view):
    camera = view["camera"]
    hb = (camera or {}).get("heartbeat") or {}
    fps, age = (hb.get("fps"), hb.get("last_frame_age_s")) if hb.get("fps") is not None else \
        (view["frames"]["fps"], view["frames"]["age_s"])
    source = "the camera's heartbeat" if hb.get("fps") is not None else "the live image (no camera heartbeat)"
    if camera is None or fps is None:
        return Cell("Camera", "—", OFF, "No camera information yet.")
    level = OK
    if camera["value"] != "RUNNING" or (age is not None and age > overview.STALE_IMAGE_S):
        level = ERROR if camera["value"] in ("FAILED", "STOPPED") else WARNING
    if hb.get("camera_connected") is False:
        level = ERROR
    return Cell("Camera", f"{fps:.1f} fps · frame {_ago(age)}", level,
                f"{camera['text'].sentence} Frames per second and age of the newest frame, from {source}.")


def inspection_row(view, camera_fps):
    insp = view["inspection"]
    if insp is None or insp["value"] != "RUNNING":
        return Cell("Inspection", insp["text"].sentence if insp else "Not configured",
                    ERROR if insp and insp["value"] in ("FAILED", "STOPPED") else WARNING if insp else OFF,
                    insp["text"].sentence if insp else "")
    ms, queue = view["ms_per_image"], view["queue"]
    rate = f"{ms:.0f} ms/img" if ms is not None else (f"{view['inspection_fps']:.1f} img/s"
                                                       if view["inspection_fps"] is not None else "—")
    budget = 1000.0 / camera_fps if camera_fps else None
    slow = (queue is not None and queue >= overview.QUEUE_WARN) or (ms is not None and budget and ms > budget)
    return Cell("Inspection", rate + (f" · queue {queue}" if queue is not None else ""), WARNING if slow else OK,
                "Time per image and images waiting in INF." +
                (f" At {camera_fps:.1f} fps there are {budget:.0f} ms per image." if budget else ""))


def model_row(view):
    model = view.get("model", "absent")
    if model == "absent" or (model is not None and not isinstance(model, dict)):
        return Cell("Model", "—", NEUTRAL, "This inspection does not publish its model yet.")
    if model is None:
        return Cell("Model", "loading", NEUTRAL, "Inspection is loading its model.")
    return Cell("Model", f"{model.get('name', '?')} {model.get('version', '?')}", NEUTRAL,
                "Running model name and version, from the inspection heartbeat.")


def roi_row(view):
    roi = view["roi"]
    if roi is None:
        return Cell("Auto-ROI", "—", OFF, "No auto-ROI status from this side's inspection.")
    mode = (roi.get("mode") or "?").lower()
    if mode == "off":
        return Cell("Auto-ROI", "Off", OFF, "Auto-ROI is switched off: every detection is used.")
    if not roi.get("learned"):
        return Cell("Auto-ROI", f"{mode.title()} · no region learned yet", WARNING,
                    "Nothing is filtered until the regions are learned.")
    rise = get_samples().increase(f"roi_out:{view['side']}")
    verb = "removed" if mode == "filter" else "outside"
    count = f"{rise:.0f}" if rise is not None else "—"
    tip = (f"Detections outside their learned region in the last hour ({'removed' if mode == 'filter' else 'kept, observe mode'})"
           + ("; counted by this screen since it started" if rise is None else "") + "."
           + (f" Drift on {', '.join(roi['drifting'])}: the camera may have moved." if roi.get("drifting") else ""))
    return Cell("Auto-ROI", f"{mode.title()} · {count} {verb} in the last hour",
                WARNING if roi.get("drifting") else OK, tip)


def side_cards(state, cfg):
    cards = []
    for view in state["sides"]:
        word, level = side_state(view)
        cam = camera_row(view)
        cam_hb = ((view["camera"] or {}).get("heartbeat") or {})
        fps = cam_hb.get("fps") if cam_hb.get("fps") is not None else view["frames"]["fps"]
        side_cfg = next(s for s in cfg["sides"] if s["side"] == view["side"])
        cards.append({"side": view["side"], "state": word, "level": level, "link": f"/side/{view['side']}/",
                      "rows": [cam, inspection_row(view, fps), model_row(view), roi_row(view)],
                      "checks": rendered.checks(cfg, side_cfg["inspection_worker"])})
    return cards


# ------------------------------------------------------------------------------- services
SERVICE_NAMES = {"frame_capture": "Camera {side}", "inspection": "Inspection {side}", "cleanup": "Cleanup",
                 "plc": "PLC service", "mail": "Mail sender", "ui": "Monitoring screen"}
STATE_WORDS = {"RUNNING": "Running", "STARTING": "Starting", "UNHEALTHY": "Not healthy", "WAITING_RESTART": "Restarting",
               "STOPPED": "Stopped", "PAUSED": "Paused", "IDLE": "Waiting", "FAILED": "Failed", "NO_STATUS": "No status",
               "DISABLED": "Switched off"}


def _key_value(p, plc):
    hb, t = p["heartbeat"] or {}, p["type"]
    if p["value"] == "DISABLED":
        return ""
    if t == "frame_capture" and hb.get("fps") is not None:
        return f"{hb['fps']:.1f} fps"
    if t == "inspection" and hb.get("ms_per_image") is not None:
        return f"{hb['ms_per_image']:.0f} ms/img"
    if t == "cleanup":
        parts = []
        last = overview.parse_iso(hb.get("last_pass_at"))
        if last:
            parts.append(f"last pass {_ago(max(0, datetime.datetime.now().timestamp() - last))} ago")
        if hb.get("disk_percent") is not None:
            parts.append(f"disk {hb['disk_percent']:.0f} %")
        return " · ".join(parts)
    if t == "plc" and plc is not None:
        return "connected" if plc.get("connected") else "not connected"
    if t == "mail" and hb.get("pending_rows") is not None:
        return f"{hb['pending_rows']} pending"
    return ""


def services(state):
    rows, running, off = [], 0, 0
    sides = {c.get("worker"): c["side"] for c in state["cfg"]["cameras"]}
    sides.update({s["inspection_worker"]: s["side"] for s in state["cfg"]["sides"]})
    for p in state["programs"]:
        name = SERVICE_NAMES.get(p["type"], p["label"]).format(side=sides.get(p["id"], ""))
        word = STATE_WORDS.get(p["value"], p["value"].title())
        level = {"ok": OK, "info": OK, "warning": WARNING, "error": ERROR, "off": OFF}.get(p["text"].level, NEUTRAL)
        running += p["value"] in ("RUNNING", "IDLE")
        off += p["value"] in ("DISABLED", "STOPPED")
        parts = [word]
        key = _key_value(p, state["plc"])
        if key:
            parts.append(key)
        if p["heartbeat_age_s"] is not None and p["value"] not in ("DISABLED",):
            parts.append(f"heartbeat {_ago(p['heartbeat_age_s'])}")
        rows.append(Cell(name, " · ".join(parts), level, p["text"].sentence + (" " + p["text"].action if p["text"].action else ""),
                         f"/system/programs/{p['id']}/"))
    return {"rows": rows, "summary": f"{running} running · {off} off" + (
        f" · {len(rows) - running - off} other" if len(rows) - running - off else "")}


# ------------------------------------------------------------------------------- alerts
def alerts(state):
    """Current problems ('now'), then stored events newest first (warnings, errors and restarts)."""
    rows = [{"id": None, "level": p.level, "text": p.sentence, "tip": p.action or p.sentence, "when": "now",
             "link": "/system/alerts/", "icon": "circle-x" if p.level == ERROR else "triangle-alert"}
            for p in state["problems"]]
    active = {r["text"] for r in rows}
    since = datetime.datetime.now() - datetime.timedelta(days=7)
    for e in Event.objects.filter(severity__in=["warning", "error"], received_at__gte=since)[:ALERT_ROWS]:
        if e.message in active:
            continue
        today = e.received_at.date() == datetime.date.today()
        rows.append({"id": e.id, "level": e.severity, "text": e.message, "tip": e.action or e.message,
                     "when": e.received_at.strftime("%H:%M") if today else e.received_at.strftime("%d %b %H:%M"),
                     "link": f"/system/alerts/#e{e.id}",
                     "icon": "circle-x" if e.severity == "error" else "rotate-ccw" if "restart" in e.name else "triangle-alert"})
    return rows[:ALERT_ROWS]


# ------------------------------------------------------------------------------- device strip
def strip(state):
    """PLC, light, conveyor, disk, GPU (from UI-1b) + database and MQTT."""
    cells = [c for c in device_strip(state) if c.label != "Mail"]
    try:
        get_sitedb().ping()
        db = Cell("Database", "OK", OK, "The site database answers.", "/system/health/")
    except SiteDBError as e:
        db = Cell("Database", "Not reachable", ERROR, str(e), "/system/health/")
    snap = state["snapshot"]
    mqtt = Cell("MQTT", "OK" if snap["connected"] else "Not connected", OK if snap["connected"] else ERROR,
                "The status service that carries every program's state to this screen.", "/system/health/")
    for c in cells:
        c.spark = []                    # sparklines live on the side pages, not on level 1 (D-101)
    return cells + [db, mqtt]
