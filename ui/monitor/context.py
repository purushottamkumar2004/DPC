"""Template context available on every page."""
from dpc_ui.ui_config import get_config


# The sidebar (D-098 4). Pages of later stages are shown greyed with what they will bring.
NAV_PAGES = [
    ("overview", "/", "layout-dashboard", "Overview", True, "Overview: the situation at a glance"),
    ("side-LEFT", "/side/LEFT/", "camera", "LEFT", True, "LEFT side: image, detections, functions, auto-ROI, model, camera"),
    ("side-RIGHT", "/side/RIGHT/", "camera", "RIGHT", True, "RIGHT side: image, detections, functions, auto-ROI, model, camera"),
    ("defects", "/defects/", "scan-search", "Defects", True, "Defects: filter, image, CSV"),
    ("system", "/system/programs/", "list-checks", "System", True, "System: programs and logs, health, alert history"),
    ("setup", "/setup/", "sliders-horizontal", "Setup", True, "Setup: the site setup wizard and commissioning"),
]


def site(request):
    from monitor.setup.env import mode
    cfg = get_config()
    setup_mode = mode() == "setup"
    return {
        "site_name": cfg["site"].get("name") or cfg["site"].get("id") or "DPC",
        "refresh_seconds": cfg["ui"].get("refresh_seconds", 2),
        "system_pages": ("programs", "health", "alerts"),
        "setup_mode": setup_mode,
        # setup mode (D-107): only the UI runs, so only Setup is useful; the rest is greyed
        "nav_pages": [{"key": k, "url": u if not setup_mode or k == "setup" else "#", "icon": i, "title": t,
                       "ready": r and (not setup_mode or k == "setup"),
                       "tip": tip if not setup_mode or k == "setup" else f"{t}: available after the setup is applied"}
                      for k, u, i, t, r, tip in NAV_PAGES],
    }
