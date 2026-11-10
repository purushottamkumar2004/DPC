"""Turns the raw state (MQTT cache, workers file, files, disk) into what the pages show:
the program list, one view per side, the list of current problems and the overall banner.

Pure functions of their inputs (no MQTT, no DB), so they are tested directly.

Banner rule (D-095, UI-1 plan):
  RED    Supervisor offline / MQTT unreachable; a camera or inspection FAILED or STOPPED, or
         restarting/unhealthy for more than 60 s; any other program FAILED; PLC disconnected;
         disk at or above cleanup.disk_percent.mail.
  AMBER  anything restarting or unhealthy; a restart in the last 10 minutes; no status yet;
         status data older than 90 s; inspection queue of 20 or more; disk at or above `delete`;
         auto-ROI drift in the last hour; PLC light not confirmed.
  GREEN  otherwise.
"""
import datetime
import json
import os
from dataclasses import dataclass, field

from monitor.secrets import mask
from monitor.messages import (ERROR, OFF, OK, WARNING, Text, effective_status, plc_text, status_text,
                              worker_label, worker_type)

CORE_TYPES = ("frame_capture", "inspection")      # a side is not inspected without these
SLOW_RESTART_S = 60
RECENT_RESTART_S = 600
STALE_STATUS_S = 90
QUEUE_WARN = 20
STALE_IMAGE_S = 15      # at ~4 fps the live image is replaced 4 times a second; the Supervisor
                        # restarts a capture only after 60 s stale (workers.yaml), the UI warns earlier
RANK = {ERROR: 2, WARNING: 1}
# A process exists in these states, so "since" is its start time (status started_at, F63).
PROCESS_STATES = ("RUNNING", "STARTING", "UNHEALTHY")


@dataclass
class Problem:
    level: str              # error | warning
    sentence: str
    action: str = ""
    link: str = ""          # page with more detail
    details: dict = field(default_factory=dict)


def parse_iso(text):
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def age_text(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


# ------------------------------------------------------------------------------- programs
def programs(snapshot: dict, configured: list) -> list:
    """One entry per program: those in workers.yaml (file order), then any other status seen."""
    now = snapshot["now"]
    statuses, heartbeats = snapshot["statuses"], snapshot["heartbeats"]
    entries, seen = [], set()
    listed = configured or [{"id": wid, "type": "", "enabled": True, "mode": ""} for wid in sorted(statuses)]
    for w in listed + [{"id": wid, "type": "", "enabled": True, "mode": ""} for wid in sorted(statuses)]:
        if w["id"] in seen:
            continue
        seen.add(w["id"])
        cached = statuses.get(w["id"])
        payload = dict(cached["payload"]) if cached else None
        if not w["enabled"] and (payload is None or payload.get("status") == "STOPPED"):
            payload = dict(payload or {}, _disabled=True)
        wtype = worker_type(w["id"], (payload or {}).get("type") or w.get("type"))
        hb = heartbeats.get(w["id"])
        last_error = (payload or {}).get("last_error")
        entries.append({
            "id": w["id"], "type": wtype, "label": worker_label(w["id"], wtype), "enabled": w["enabled"],
            "status": payload, "value": effective_status(payload), "text": status_text(w["id"], payload),
            # F63: "since" = now - started_at (local ISO, no zone, as the Supervisor writes it) while the
            # process runs; otherwise how long the current state has lasted. The UI's own state-change
            # record misses restarts that happen between two status messages, so it is not used for
            # a running process.
            "since_s": _since(payload, cached, now),
            "state_for_s": (now - cached["since"]) if cached and cached.get("since") else None,
            "status_age_s": (now - cached["received_at"]) if cached else None,
            "restarts": (payload or {}).get("restart_count"),
            "last_restart_at": parse_iso((payload or {}).get("last_restart_at")),
            "heartbeat": hb["payload"] if hb else None,
            "heartbeat_age_s": (now - hb["received_at"]) if hb else None,
            "last_error": last_error if isinstance(last_error, dict) else None,
            "log_file": (payload or {}).get("log_file"),
            "pid": (payload or {}).get("pid"),
            "message": mask(str((payload or {}).get("message") or ""))[:200],
        })
    return entries


def _since(payload, cached, now):
    if payload and payload.get("status") in PROCESS_STATES:
        started = parse_iso(payload.get("started_at"))
        return max(0.0, now - started) if started else None
    return (now - cached["since"]) if cached and cached.get("since") else None


def program_by_id(progs, worker_id):
    for p in progs:
        if p["id"] == worker_id:
            return p
    return None


# ------------------------------------------------------------------------------- sides
def read_roi_status(data_dir: str, side: str):
    path = os.path.join(data_dir, "state", f"inspection_{side.lower()}", "roi_status.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    drifting = sorted(c for c, e in (data.get("classes") or {}).items() if isinstance(e, dict) and e.get("drift"))
    outside = sum(int(e.get("outside") or 0) for e in (data.get("classes") or {}).values() if isinstance(e, dict))
    return {"mode": data.get("mode"), "learned": data.get("roi") is not None, "outside": outside,
            "learning": (data.get("learning") or {}).get("state"), "drifting": drifting}


def sides(cfg, progs, meter=None) -> list:
    """Per side: its camera program, inspection program, frame reading and auto-ROI state."""
    result = []
    for side in cfg["sides"]:
        cam = next((c for c in cfg["cameras"] if c["id"] == side["camera_id"]), {"id": side["camera_id"]})
        capture = program_by_id(progs, cam.get("worker", ""))
        inspection = program_by_id(progs, side["inspection_worker"])
        hb = (inspection or {}).get("heartbeat") or {}
        result.append({
            "side": side["side"], "camera_id": cam["id"], "camera": capture, "inspection": inspection,
            "frames": meter.reading(cam["id"]) if meter else {"fps": None, "age_s": None},
            "inspection_fps": hb.get("fps"), "queue": hb.get("inf_queue_depth"),
            "last_frame_age_s": hb.get("last_frame_age_s"), "ms_per_image": hb.get("ms_per_image"),
            "waiting_for_plate": hb.get("waiting_for_plate"),
            "model": hb.get("model", "absent"),          # R6/F65: {name, version}; null while loading
            "roi": read_roi_status(cfg.paths["data"], side["side"]),
        })
    return result


# ------------------------------------------------------------------------------- problems
def _program_problem(p, now):
    value, text = p["value"], p["text"]
    if value in ("DISABLED", "RUNNING", "IDLE", "STARTING") or not p["enabled"]:
        if value == "RUNNING" and p["last_restart_at"] and now - p["last_restart_at"] < RECENT_RESTART_S:
            when = datetime.datetime.fromtimestamp(p["last_restart_at"]).strftime("%H:%M")
            return Problem(WARNING, f"{p['label']} was restarted at {when}.",
                           "Nothing to do if it stays running.", f"/programs/{p['id']}/")
        return None
    core = p["type"] in CORE_TYPES
    level = text.level if text.level in (ERROR, WARNING) else WARNING
    if value in ("WAITING_RESTART", "UNHEALTHY") and core and (p["state_for_s"] or 0) > SLOW_RESTART_S:
        level = ERROR
    if value == "STOPPED" and not core and p["type"] == "mail":
        return None                        # mail stopped on purpose is shown on its tile, not as a problem
    if value == "STOPPED" and not core:
        level = WARNING
    if value == "PAUSED":
        level = WARNING
    return Problem(level, text.sentence, text.action, f"/programs/{p['id']}/")


def problems(snapshot, progs, side_views, disk, plc_expected=True, drift_events=0) -> list:
    now = snapshot["now"]
    found = []
    if not snapshot["connected"]:
        found.append(Problem(ERROR, "The screen has no connection to the status service (MQTT); "
                                    "everything shown may be old.", "Check that mosquitto is running.", "/health/"))
    sup = snapshot.get("supervisor")
    sup_state = ((sup or {}).get("payload") or {}).get("status")
    if snapshot["connected"] and sup_state != "online":
        found.append(Problem(ERROR, "The Supervisor is not running: programs are not watched or restarted.",
                             "Restart the computer, or ask the developer to start the dpc-supervisor service.",
                             "/programs/"))
    elif sup and now - sup["received_at"] > STALE_STATUS_S:
        found.append(Problem(WARNING, "No news from the Supervisor for "
                             f"{age_text(now - sup['received_at'])}; the status may be old.", "", "/programs/"))
    for p in progs:
        problem = _program_problem(p, now)
        if problem:
            found.append(problem)
    for s in side_views:
        camera = s["camera"] or {}
        age = (s["frames"] or {}).get("age_s")
        if camera.get("value") == "RUNNING" and age is not None and age > STALE_IMAGE_S:
            found.append(Problem(WARNING, f"{s['side']} camera: the newest image is {age_text(age)} old.",
                                 "The system restarts the camera program if this lasts a minute. "
                                 "If it repeats, check the camera cable and power.", "/health/"))
        if s["queue"] is not None and s["queue"] >= QUEUE_WARN:
            found.append(Problem(WARNING, f"{s['side']} inspection is behind: {s['queue']} images are waiting.",
                                 "It normally catches up; if the number keeps growing, check the GPU on System health.",
                                 "/health/"))
        roi = s["roi"] or {}
        if roi.get("drifting"):
            found.append(Problem(WARNING, f"{s['side']} inspection: detections of {', '.join(roi['drifting'])} are often "
                                          "outside their learned area. The camera may have moved.",
                                 "Check the camera position.", "/alerts/"))
    if drift_events and not any("learned area" in p.sentence for p in found):
        found.append(Problem(WARNING, "Auto-ROI reported detections outside their learned area in the last hour.",
                             "Check the camera position.", "/alerts/"))
    plc = (snapshot.get("plc") or {}).get("payload")
    if plc_expected:
        text = plc_text(plc)
        if text.level in (ERROR, WARNING):
            found.append(Problem(text.level, text.sentence, text.action, "/health/"))
    if disk.get("level") == "mail":
        found.append(Problem(ERROR, f"The disk is {disk['percent']} % full and almost out of space.",
                             "Tell the developer; inspection stops when the disk is full.", "/health/"))
    elif disk.get("level") == "delete":
        found.append(Problem(WARNING, f"The disk is {disk['percent']} % full; old images are being deleted.",
                             "No action needed unless it keeps rising.", "/health/"))
    found.sort(key=lambda p: -RANK.get(p.level, 0))
    return found


def banner(found: list) -> dict:
    if not found:
        return {"level": OK, "headline": "Everything is working.", "count": 0}
    worst = found[0]
    if worst.level == ERROR:
        return {"level": ERROR, "headline": worst.sentence, "count": len(found)}
    return {"level": WARNING, "headline": worst.sentence, "count": len(found)}


def level_of(text: Text) -> str:
    return text.level if text else OFF
