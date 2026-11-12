"""Commissioning checklist after Apply (D-103 phase 3), with live ticks from the heartbeats.

  cameras   capture heartbeat fps within 20 % of the configured fps (about 4)
  plc       dpc/plc/status connected and light_ok
  plate     inspection heartbeat plates_seen >= 1 (F76), per side
  cycle     last_cycle_wheels == machine.total_wheel_count, per side (proves count and direction)
  auto-ROI  learned (roi.json) -> observe -> filter: "Learn now" writes run/<worker>.roi_learn;
            the mode is changed with site_writer (inspection.auto_roi.mode) + supervisor reload --confirm
  mail      mail_worker.py --test --json with the rendered mail.json (when mail is configured)
  transfer  transfer.py --check --json (rendered), then run/transfer.model_update / upload_cycle /
            cancel with progress from the transfer heartbeat (when transfer is configured)
A tick, once reached, is remembered with its time in data/state/ui/commissioning_state.json; the
report (JSON + printable HTML) is saved as data/state/ui/commissioning_<date>.{json,html}.
"""
import datetime
import json
import os

from monitor.setup import env, jobs

STATE_FILE = "commissioning_state.json"
AUTO_ROI_MODES = ("off", "observe", "filter")


def _state_path():
    from dpc_ui.ui_config import get_config
    return os.path.join(get_config().paths["state"], STATE_FILE)


def load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def remember(key, ok, detail=""):
    """Record a tick (first time ok) or a failed test result."""
    state = load_state()
    entry = state.get(key) or {}
    if ok and not entry.get("ok"):
        entry = {"ok": True, "at": datetime.datetime.now().isoformat(timespec="seconds"), "detail": detail}
    elif not ok and not entry.get("ok"):
        entry = {"ok": False, "at": datetime.datetime.now().isoformat(timespec="seconds"), "detail": detail}
    state[key] = entry
    _save_state(state)
    return entry


def _rendered(cfg, worker):
    from monitor.sources.rendered import rendered
    return rendered(cfg, worker)


def checklist(snapshot, cfg):
    """[{key, title, ok, detail, hint}] in D-103 order; live values, remembered ticks kept."""
    heartbeats = snapshot.get("heartbeats") or {}
    plc = (snapshot.get("plc") or {}).get("payload") or {}
    total = cfg["machine"].get("total_wheel_count")
    items = []

    def item(key, title, ok, detail, hint=""):
        if ok:
            entry = remember(key, True, detail)
        else:
            entry = load_state().get(key) or {}
        items.append({"key": key, "title": title, "ok": bool(ok or entry.get("ok")), "live": bool(ok),
                      "detail": detail, "hint": hint, "at": entry.get("at") if entry.get("ok") else None})

    for cam in cfg["cameras"]:
        hb = (heartbeats.get(cam.get("worker")) or {}).get("payload") or {}
        want = (_rendered(cfg, cam.get("worker") or "").get("camera") or {}).get("fps") or 4
        fps = hb.get("fps")
        ok = fps is not None and abs(fps - want) <= 0.2 * want and hb.get("camera_connected", True)
        item(f"camera_{cam['side']}", f"{cam['side']} camera delivers about {want:g} images per second", ok,
             f"{fps:.1f} fps" if fps is not None else "no camera heartbeat yet",
             "Check the cable and the exposure if it stays below.")
    connected, light = plc.get("connected"), plc.get("light_ok")
    item("plc", "PLC connected and the light on", bool(connected and light),
         "connected" if connected else "not connected" if plc else "no PLC status yet",
         "" if connected else "Check the PLC IP and cable (System > Health).")
    for side in cfg["sides"]:
        hb = (heartbeats.get(side["inspection_worker"]) or {}).get("payload") or {}
        plates = hb.get("plates_seen")
        item(f"plate_{side['side']}", f"{side['side']}: first plate seen", bool(plates),
             f"{plates} plate(s) since start" if plates is not None else "waiting for the inspection heartbeat",
             "The conveyor must run; the plate starts the counting.")
        wheels = hb.get("last_cycle_wheels")
        item(f"cycle_{side['side']}", f"{side['side']}: one revolution counted {total} wheels",
             wheels is not None and total is not None and int(wheels) == int(total),
             f"last cycle: {wheels} wheels" if wheels is not None else "no full revolution yet",
             "A different number means a wrong wheel count or direction; check the Machine and Cameras answers.")
    return items


def later_items(cfg, mail_result, transfer_result, progress):
    """The ticks after the live ones: auto-ROI filtering, test mail, transfer (when configured)."""
    items = []

    def item(key, title, ok, detail, hint=""):
        entry = remember(key, True, detail) if ok else (load_state().get(key) or {})
        items.append({"key": key, "title": title, "ok": bool(ok or entry.get("ok")), "live": bool(ok), "detail": detail,
                      "hint": hint, "at": entry.get("at") if entry.get("ok") else None})

    rois = roi_states(cfg)
    item("auto_roi", "Auto-ROI learned and filtering on every side",
         bool(rois) and all(r["learned"] and r["mode"] == "filter" for r in rois),
         ", ".join(f"{r['side']}: {'learned' if r['learned'] else 'not learned'}, {r['mode']}" for r in rois),
         "Learn, check in observe mode for a while, then switch to filter.")
    if os.path.exists(os.path.join(cfg.paths["config"], "rendered", "mail.json")):
        item("mail", "Test mail sent", bool(mail_result and mail_result["level"] == "ok"),
             mail_result["sentence"] if mail_result else "not tested yet", "Ask the recipients whether it arrived.")
    if transfer_configured(cfg):
        item("transfer_check", "SharePoint access works", bool(transfer_result and transfer_result["level"] == "ok"),
             transfer_result["sentence"] if transfer_result else "not tested yet")
        for key, title, done_key in (("model_update", "Model check done", "last_model_check_at"),
                                     ("upload_cycle", "One cycle uploaded", "last_upload_at")):
            block = progress.get(key) or {}
            done_at = progress.get(done_key)
            detail = f"last at {done_at}" if done_at else (block.get("state") or "not started")
            if block.get("bytes_total"):
                detail += f" · {100 * (block.get('bytes_done') or 0) // block['bytes_total']} %"
            if block.get("state") == "failed" and block.get("message"):
                detail += f" · {block['message']}"
            item(f"transfer_{key}", title, bool(done_at), detail)
    return items


# ------------------------------------------------------------------------------- auto-ROI
def roi_states(cfg):
    from monitor import level2
    out = []
    for side in cfg["sides"]:
        roi = level2.roi_section(cfg, side["inspection_worker"])
        request = os.path.join(cfg.paths["run"], f"{side['inspection_worker']}.roi_learn")
        out.append({"side": side["side"], "worker": side["inspection_worker"], "learned": roi["has_roi"],
                    "mode": roi["mode"], "learning": roi["learning"] if roi["learning"] != "idle" or not os.path.exists(request)
                    else "requested", "requested": os.path.exists(request)})
    return out


def request_learning(cfg, worker):
    path = os.path.join(cfg.paths["run"], f"{worker}.roi_learn")
    os.makedirs(cfg.paths["run"], exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
    return path


def set_auto_roi_mode(mode):
    """Write inspection.auto_roi.mode with site_writer, then `supervisor.py reload --confirm`
    (both inspections restart and pause until the next plate). Runs as a background job."""
    if mode not in AUTO_ROI_MODES:
        raise ValueError(f"unknown auto-ROI mode {mode!r}")

    def job():
        import subprocess
        import yaml
        env.import_common()
        from common.config.site_writer import write_site
        from dpc_ui.ui_config import get_config
        path = get_config().paths["site_yaml"]
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        inspection = data.setdefault("inspection", {})
        auto_roi = inspection.get("auto_roi") if isinstance(inspection.get("auto_roi"), dict) else {}
        auto_roi["mode"] = mode
        inspection["auto_roi"] = auto_roi
        written = write_site(data, path=path)
        proc = subprocess.run([env.python(), env.tool_path("supervisor"), "reload", "--confirm"],
                              capture_output=True, text=True, timeout=120)
        return {"ok": proc.returncode == 0, "mode": mode, "backup": getattr(written, "backup", None),
                "message": (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]}

    jobs.start("commission.roi_mode", f"Switching auto-ROI to {mode}", func=job)


# ------------------------------------------------------------------------------- mail, transfer
def test_mail(cfg):
    rendered = os.path.join(cfg.paths["config"], "rendered", "mail.json")
    jobs.start("commission.mail", "Sending one test mail",
               [env.python(), env.tool_path("mail"), "--test", "--json"], env={"DPC_WORKER_CONFIG": rendered}, timeout=120)


def transfer_configured(cfg):
    return os.path.exists(os.path.join(cfg.paths["config"], "rendered", "transfer.json"))


def check_transfer(cfg):
    rendered = os.path.join(cfg.paths["config"], "rendered", "transfer.json")
    jobs.start("commission.transfer_check", "Checking the SharePoint permission",
               [env.python(), env.tool_path("transfer"), "--check", "--json"], env={"DPC_WORKER_CONFIG": rendered}, timeout=180)


TRANSFER_REQUESTS = {"model_update": "transfer.model_update", "upload_cycle": "transfer.upload_cycle",
                     "cancel": "transfer.cancel"}


def transfer_request(cfg, action, content=""):
    name = TRANSFER_REQUESTS[action]
    os.makedirs(cfg.paths["run"], exist_ok=True)
    with open(os.path.join(cfg.paths["run"], name), "w", encoding="utf-8") as f:
        f.write(content)


def transfer_progress(snapshot):
    """The transfer heartbeat (D-105): job blocks upload_cycle / model_update {state, phase,
    bytes_done, bytes_total, message} and last_upload_at / last_model_check_at."""
    hb = ((snapshot.get("heartbeats") or {}).get("transfer") or {}).get("payload") or {}
    out = {k: hb.get(k) for k in ("upload_cycle", "model_update") if isinstance(hb.get(k), dict)}
    out.update({k: hb.get(k) for k in ("last_upload_at", "last_model_check_at") if hb.get(k)})
    return out


# ------------------------------------------------------------------------------- report
def save_report(cfg, items, roi, extra):
    stamp = datetime.datetime.now()
    report = {"site": cfg["site"], "saved_at": stamp.isoformat(timespec="seconds"), "checklist": items,
              "auto_roi": roi, "tests": extra}
    folder = cfg.paths["state"]
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, f"commissioning_{stamp.strftime('%Y-%m-%d_%H%M')}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    from django.template.loader import render_to_string
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(render_to_string("monitor/setup/report.html", {"r": report}))
    return os.path.basename(base)
