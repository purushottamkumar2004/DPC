"""Level 2 side page (D-101): everything about one side, read-only in UI-1c.

  header     side, one-word state, its two programs (camera, inspection) with status, heartbeat age
  image      tabs Live (live/IMG_1.jpg) and Detections (F66), always the whole frame
  functions  the four checks from inspection_<side>.json (switches come in UI-2)
  auto-ROI   roi_status.json (mode, learned at, classes, counts, drift, previews) + roi.json present?
             + the last entries of roi_outside.jsonl with their crops (CONTRACTS 11)
  model      heartbeat model + the side's package folder (installed, current, pinned)
  camera     frame_capture_<camn>.json: serial, exposure, fps, rotate (editing comes in UI-3)
"""
import json
import os
import re

from monitor import level1, overview
from monitor.sources import rendered
from monitor.sources.models import model_info
from monitor.state.samples import get_samples

OUTSIDE_ROWS = 20
PREVIEW_NAME = re.compile(r"^roi_preview[\w.-]*\.jpg$")
CROP_NAME = re.compile(r"^roi_outside/[\w.-]+\.jpg$")


def state_folder(cfg, inspection_worker):
    return os.path.join(cfg.paths["data"], "state", inspection_worker)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def outside_entries(folder, count=OUTSIDE_ROWS):
    """The newest lines of roi_outside.jsonl (inspection keeps at most outside_keep), newest first."""
    path = os.path.join(folder, "roi_outside.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            crop = item.get("crop")
            item["crop_ok"] = bool(crop and CROP_NAME.match(crop) and os.path.isfile(os.path.join(folder, crop)))
            entries.append(item)
        if len(entries) >= count:
            break
    return entries


def roi_section(cfg, inspection_worker):
    folder = state_folder(cfg, inspection_worker)
    status = _read_json(os.path.join(folder, "roi_status.json")) or {}
    roi = status.get("roi") if isinstance(status.get("roi"), dict) else None
    previews = [p for p in (roi or {}).get("previews") or [] if isinstance(p, str) and PREVIEW_NAME.match(p)
                and os.path.isfile(os.path.join(folder, p))]
    classes = []
    for cls, entry in sorted((status.get("classes") or {}).items()):
        if isinstance(entry, dict):
            classes.append({"cls": cls, "inside": entry.get("inside", 0), "outside": entry.get("outside", 0),
                            "ratio": entry.get("recent_outside_ratio"), "drift": bool(entry.get("drift"))})
    return {"has_status": bool(status), "has_roi": os.path.isfile(os.path.join(folder, "roi.json")),
            "mode": (status.get("mode") or "—"),
            "learned_at": str((roi or {}).get("learned_at") or "").replace("T", " ")[:19] or None,
            "learned": (roi or {}).get("learned_classes") or [], "not_learned": (roi or {}).get("not_learned") or [],
            "learning": (status.get("learning") or {}).get("state") or "idle", "classes": classes,
            "previews": previews, "outside": outside_entries(folder),
            "outside_total": (status.get("outside_list") or {}).get("entries"),
            "not_listed": (status.get("outside_list") or {}).get("not_listed_rate_limit")}


def roi_file_allowed(cfg, inspection_worker, name):
    """Path of a preview or crop the page may show, or None (only listed names, inside the folder)."""
    folder = state_folder(cfg, inspection_worker)
    if not (PREVIEW_NAME.match(name) or CROP_NAME.match(name)):
        return None
    path = os.path.realpath(os.path.join(folder, name))
    if not path.startswith(os.path.realpath(folder) + os.sep) or not os.path.isfile(path):
        return None
    return path


def side_page(state, cfg, side_name):
    view = next((s for s in state["sides"] if s["side"] == side_name), None)
    if view is None:
        return None
    side_cfg = next(s for s in cfg["sides"] if s["side"] == side_name)
    cam_cfg = next((c for c in cfg["cameras"] if c["id"] == side_cfg["camera_id"]), {})
    word, level = level1.side_state(view)
    samples = get_samples()
    programs = []
    for program, spark in ((view["camera"], samples.values(f"fps:{side_cfg['camera_id']}")),
                           (view["inspection"], samples.values(f"ms:{side_name}") or samples.values(f"ips:{side_name}"))):
        if program:
            programs.append({"p": program, "spark": spark})
    cam_hb = (view["camera"] or {}).get("heartbeat") or {}
    fps = cam_hb.get("fps") if cam_hb.get("fps") is not None else view["frames"]["fps"]
    name, models_dir = rendered.model_folder(cfg, side_cfg["inspection_worker"])
    return {"side": side_name, "state": word, "level": level, "view": view, "programs": programs,
            "camera_id": side_cfg["camera_id"], "inspection_worker": side_cfg["inspection_worker"],
            "rows": [level1.camera_row(view), level1.inspection_row(view, fps), level1.model_row(view),
                     level1.roi_row(view)],
            "checks": rendered.checks(cfg, side_cfg["inspection_worker"]),
            "roi": roi_section(cfg, side_cfg["inspection_worker"]),
            "model": model_info(models_dir, name), "model_running": level1.model_row(view),
            "camera": rendered.camera_settings(cfg, cam_cfg.get("worker", "")),
            "capture_worker": cam_cfg.get("worker"), "stale_text": (
                f"Last image {overview.age_text(view['frames']['age_s'])} old"
                if view["frames"]["age_s"] is not None and view["frames"]["age_s"] > overview.STALE_IMAGE_S else "")}
