"""Plain language for every technical state the UI shows (D-095 rule 4).

One table per kind of thing: worker statuses (by worker type), events (by event name) and the
conditions the UI checks itself (PLC, disk, MQTT, Supervisor). Each entry gives one sentence and
one suggested action. Technical details (status value, last_error, traceback, JSON) are only
shown behind "Details".

To add or change a message, edit the tables below; `{label}` is the program's plain name
("LEFT camera", "RIGHT inspection"), other {names} come from the event payload.
"""
from dataclasses import dataclass

from dpc_ui.ui_config import get_config

OK, INFO, WARNING, ERROR, OFF = "ok", "info", "warning", "error", "off"


@dataclass
class Text:
    level: str          # ok | info | warning | error | off
    sentence: str
    action: str = ""


# ----------------------------------------------------------------------------------------------
# Program names
# ----------------------------------------------------------------------------------------------
TYPE_NAMES = {
    "plc": "PLC service",
    "mail": "Defect mail sender",
    "cleanup": "Disk cleanup",
    "ui": "Monitoring screen",
}


def worker_type(worker_id: str, given: str = None) -> str:
    """The worker's type: from its status payload, else from its id (D-033 naming)."""
    if given:
        return given
    if worker_id.startswith("frame_capture"):
        return "frame_capture"
    if worker_id.startswith("inspection"):
        return "inspection"
    return worker_id


def worker_label(worker_id: str, wtype: str = None) -> str:
    """'frame_capture_cam1' -> 'LEFT camera'; 'inspection_right' -> 'RIGHT inspection'."""
    cfg = get_config()
    wtype = worker_type(worker_id, wtype)
    if wtype == "frame_capture":
        for cam in cfg["cameras"]:
            if cam.get("worker") == worker_id:
                return f"{cam['side']} camera"
    if wtype == "inspection":
        for side in cfg["sides"]:
            if side.get("inspection_worker") == worker_id:
                return f"{side['side']} inspection"
    return TYPE_NAMES.get(wtype, worker_id.replace("_", " "))


# ----------------------------------------------------------------------------------------------
# Worker statuses (CONTRACTS 2): STARTING RUNNING UNHEALTHY WAITING_RESTART STOPPED FAILED,
# plus PAUSED (STOPPED with paused_by), IDLE (periodic between runs), NO_STATUS, DISABLED.
# Key: (type, status); a missing type falls back to ("*", status).
# ----------------------------------------------------------------------------------------------
STATUS_TEXT = {
    ("*", "RUNNING"): Text(OK, "{label} is running."),
    ("*", "STARTING"): Text(INFO, "{label} is starting."),
    ("*", "UNHEALTHY"): Text(WARNING, "{label} is not working properly and will be restarted if it does not recover.",
                             "Wait a minute; if it stays like this, open Details."),
    ("*", "WAITING_RESTART"): Text(WARNING, "{label} stopped and is restarted automatically in a few seconds."),
    ("*", "STOPPED"): Text(OFF, "{label} is stopped."),
    ("*", "PAUSED"): Text(INFO, "{label} is paused until {leader} works again."),
    ("*", "IDLE"): Text(OK, "{label} is waiting for its next run."),
    ("*", "FAILED"): Text(ERROR, "{label} failed too often and is no longer restarted automatically.",
                          "Open Details and send them to the developer; restart the program once the cause is fixed."),
    ("*", "NO_STATUS"): Text(WARNING, "No information from {label} yet.",
                             "If this stays for more than a minute, check that the Supervisor is running."),
    ("*", "DISABLED"): Text(OFF, "{label} is switched off in the settings."),

    ("frame_capture", "UNHEALTHY"): Text(WARNING, "{label} is not delivering images. The system keeps retrying.",
                                         "Check the camera cable and power."),
    ("frame_capture", "WAITING_RESTART"): Text(WARNING, "{label} is not delivering images. The system keeps retrying.",
                                               "Check the camera cable and power."),
    ("frame_capture", "FAILED"): Text(ERROR, "{label} is not delivering images and the system has stopped retrying.",
                                      "Check the camera cable, power and the camera network port, then restart the camera program."),
    ("frame_capture", "STOPPED"): Text(ERROR, "{label} is stopped: no images are taken on this side.",
                                       "Start the camera program."),
    ("inspection", "STARTING"): Text(INFO, "{label} is starting (loading the model takes a few minutes)."),
    ("inspection", "UNHEALTHY"): Text(WARNING, "{label} is falling behind or stuck; it is restarted if it does not recover.",
                                      "A restart pauses inspection until the next plate. Wait; if it repeats, open Details."),
    ("inspection", "WAITING_RESTART"): Text(WARNING, "{label} stopped and restarts in a few seconds. Inspection resumes after the next plate."),
    ("inspection", "FAILED"): Text(ERROR, "{label} has stopped: this side is NOT being inspected.",
                                   "Open Details and send them to the developer."),
    ("inspection", "STOPPED"): Text(ERROR, "{label} is stopped: this side is NOT being inspected.",
                                    "Start the inspection program."),
    ("plc", "FAILED"): Text(ERROR, "The PLC service has stopped: the light and cleaning are not controlled.",
                            "Open Details and send them to the developer."),
    ("mail", "STOPPED"): Text(OFF, "Defect mails are not being sent (the mail program is stopped). Defects are still recorded."),
    ("mail", "DISABLED"): Text(OFF, "Defect mails are switched off. Defects are still recorded."),
    ("mail", "FAILED"): Text(ERROR, "The defect mail sender has stopped: no defect mails are sent.",
                             "Check the internet connection, then open Details."),
    ("cleanup", "FAILED"): Text(ERROR, "Disk cleanup has stopped: the disk will fill up.",
                                "Open Details and send them to the developer."),
}


def status_text(worker_id: str, status: dict) -> Text:
    """Plain text for one worker status payload (or None = no status received)."""
    wtype = worker_type(worker_id, (status or {}).get("type"))
    label = worker_label(worker_id, wtype)
    value = effective_status(status)
    entry = STATUS_TEXT.get((wtype, value)) or STATUS_TEXT.get(("*", value)) or Text(INFO, f"{{label}}: {value}.")
    leader = (status or {}).get("paused_by") or ""
    leader_label = worker_label(leader) if leader else "the program it depends on"
    return Text(entry.level, entry.sentence.format(label=label, leader=leader_label), entry.action)


def effective_status(status: dict) -> str:
    """Status value with the UI's extra states (PAUSED, IDLE, NO_STATUS, DISABLED)."""
    if not status:
        return "NO_STATUS"
    if status.get("_disabled"):
        return "DISABLED"
    value = status.get("status") or "NO_STATUS"
    if value == "STOPPED" and status.get("paused_by"):
        return "PAUSED"
    if value == "STOPPED" and status.get("next_run_at"):
        return "IDLE"
    return value


# ----------------------------------------------------------------------------------------------
# Events (CONTRACTS 2) and the UI's own synthetic events. Key: event name.
# ----------------------------------------------------------------------------------------------
EVENT_TEXT = {
    "worker_started": Text(INFO, "{label} started."),
    "worker_stopped": Text(INFO, "{label} stopped."),
    "worker_failure": Text(ERROR, "{label} stopped because of an error.{error}",
                           "It is restarted automatically. If this repeats, open Details and send them to the developer."),
    "worker_restarted": Text(WARNING, "{label} was restarted.{error}"),
    "worker_failed": Text(ERROR, "{label} failed too often and is no longer restarted automatically.",
                          "Open Details and send them to the developer."),
    "roi_drift": Text(WARNING, "{side} inspection: {ratio} of the '{cls}' detections are outside their learned area. "
                               "The camera may have moved.",
                      "Check the camera position. If the view changed on purpose, relearn the area (Auto-ROI)."),
    "roi_learned": Text(INFO, "{side} inspection learned its areas of interest."),
    "roi_learn_failed": Text(WARNING, "{side} inspection could not learn its areas of interest ({reason}).",
                             "Make sure the conveyor runs a full cycle, then request learning again."),
    "model_switched": Text(INFO, "Model {model} switched to version {to}."),
    "model_rolled_back": Text(WARNING, "Model {model} was rolled back to version {to}."),
    "model_switch_failed": Text(ERROR, "Model {model} could not be switched to {to} ({reason}). The old model keeps running.",
                                "Open Details and send them to the developer."),
    "model_pinned": Text(INFO, "Model {model} is pinned to version {version}."),
    "model_unpinned": Text(INFO, "Model {model} is no longer pinned."),
    "config_reloaded": Text(INFO, "New settings were applied."),
    "config_reload_failed": Text(ERROR, "New settings could not be applied; nothing was changed.",
                                 "Open Details and correct the settings."),
    "config_render_failed": Text(ERROR, "New settings could not be prepared; nothing was changed.",
                                 "Open Details and correct the settings."),
    "light_on_written": Text(INFO, "The conveyor light was switched on."),
    "cleaning_pulse_started": Text(INFO, "Camera cleaning started."),
    "cleaning_pulse_ended": Text(INFO, "Camera cleaning finished."),
    # synthetic (written by the UI itself)
    "supervisor_offline": Text(ERROR, "The Supervisor is not running: programs are not watched or restarted.",
                               "Restart the computer, or ask the developer to start the dpc-supervisor service."),
    "supervisor_online": Text(INFO, "The Supervisor is running."),
    "plc_disconnected": Text(ERROR, "The PLC does not answer: the light and camera cleaning cannot be controlled.",
                             "Check the PLC power and its network cable."),
    "plc_connected": Text(INFO, "The PLC answers again."),
    "disk_ok": Text(INFO, "Disk usage is back to normal ({percent} %)."),
    "disk_delete": Text(WARNING, "The disk is {percent} % full; old images are being deleted.",
                        "No action needed unless it keeps rising."),
    "disk_mail": Text(ERROR, "The disk is {percent} % full and almost out of space.",
                      "Tell the developer; inspection stops when the disk is full."),
    "mqtt_lost": Text(ERROR, "The screen lost the connection to the status service (MQTT); values may be old.",
                      "Check that mosquitto is running (System health)."),
    "mqtt_back": Text(INFO, "The screen is connected to the status service again."),
}

EVENT_SEVERITY = {OK: "info", INFO: "info", OFF: "info", WARNING: "warning", ERROR: "error"}


def _ratio(value):
    try:
        return f"{float(value) * 100:.0f} %"
    except (TypeError, ValueError):
        return "many"


def event_text(source: str, name: str, payload: dict) -> Text:
    """Plain text for one event. Unknown events get a neutral sentence (never an error)."""
    payload = payload or {}
    worker_id = payload.get("worker_id") or source
    label = worker_label(worker_id)
    last_error = payload.get("last_error") or {}
    error = f" Reason: {last_error.get('message')[:160]}" if isinstance(last_error, dict) and last_error.get("message") else ""
    if name == "worker_stopped" and payload.get("reason") == "completed":
        return Text(INFO, f"{label} finished its run.")
    if name == "status_changed":
        return status_text(worker_id, payload)
    entry = EVENT_TEXT.get(name)
    if entry is None:
        return Text(INFO, f"{label}: {name.replace('_', ' ')}.")
    values = {
        "label": label, "error": error,
        "side": payload.get("side") or label.split(" ")[0],
        "cls": payload.get("class", "?"), "ratio": _ratio(payload.get("ratio")),
        "reason": payload.get("reason") or payload.get("result") or "unknown reason",
        "model": payload.get("model", "?"), "to": payload.get("to", "?"),
        "version": payload.get("version", "?"), "percent": payload.get("percent", "?"),
    }
    try:
        sentence = entry.sentence.format(**values)
    except (KeyError, IndexError, ValueError):
        sentence = entry.sentence
    return Text(entry.level, sentence, entry.action)


def plc_text(plc: dict) -> Text:
    """The PLC tile: connected, light, conveyor."""
    if not plc:
        return Text(WARNING, "No information from the PLC service yet.")
    if not plc.get("connected"):
        return EVENT_TEXT["plc_disconnected"]
    if plc.get("light_ok") is False:
        return Text(WARNING, "The PLC answers but the conveyor light is not on.",
                    "The PLC service rewrites it within a minute; check the light if it stays off.")
    return Text(OK, "The PLC answers; the light is on.")
