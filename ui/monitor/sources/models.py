"""Read-only view of one side's model package folder (CONTRACTS 12, D-060):
<models_dir>/<name>/<version>/manifest.json, <name>/current -> <version>, <name>/pinned.
Checksums are not verified here (the Supervisor does that before switching); the UI only lists."""
import json
import os


def model_info(models_dir: str, name: str) -> dict:
    root = os.path.join(models_dir or "", name or "")
    info = {"name": name, "folder": root, "current": None, "pinned": None, "installed": [], "error": ""}
    if not models_dir or not name:
        info["error"] = "This side's model folder is not known (no rendered inspection settings)."
        return info
    if not os.path.isdir(root):
        info["error"] = "The model folder does not exist."
        return info
    try:
        link = os.readlink(os.path.join(root, "current"))
        info["current"] = os.path.basename(link.rstrip("/"))
    except OSError:
        pass
    try:
        with open(os.path.join(root, "pinned"), "r", encoding="utf-8") as f:
            info["pinned"] = f.read().strip() or None
    except OSError:
        pass
    for entry in sorted(os.listdir(root), reverse=True):
        manifest = os.path.join(root, entry, "manifest.json")
        if entry in ("current", "pinned") or not os.path.isfile(manifest):
            continue
        trained = ""
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                trained = (json.load(f) or {}).get("trained_at") or ""
        except (OSError, ValueError):
            pass
        info["installed"].append({"version": entry, "trained_at": trained,
                                  "ready": os.path.exists(os.path.join(root, entry, "ready"))})
    return info
