"""Template context available on every page."""
from dpc_ui.ui_config import get_config


# The sidebar (D-098 4). Pages of later stages are shown greyed with what they will bring.
NAV_PAGES = [
    ("dashboard", "/", "layout-dashboard", "Dashboard", True, "Dashboard: everything on one screen"),
    ("alerts", "/alerts/", "bell", "Alerts and errors", True, "Alerts and errors"),
    ("defects", "/defects/", "scan-search", "Defects", True, "Defects: filter, image, CSV"),
    ("cameras", "/cameras/LEFT/", "camera", "Cameras", True, "Cameras: live image and status per side"),
    ("settings", "#", "sliders-horizontal", "Inspection settings", False, "Inspection settings (comes with UI-3)"),
    ("programs", "/programs/", "list-checks", "Programs", True, "Programs: state, restarts, logs"),
    ("models", "#", "box", "Models", False, "Models (comes with UI-4)"),
    ("roi", "#", "scan", "Auto-ROI", False, "Auto-ROI (comes with UI-4)"),
    ("health", "/health/", "activity", "System health", True, "System health: disk, GPU, database, devices"),
]


def site(request):
    cfg = get_config()
    return {
        "site_name": cfg["site"].get("name") or cfg["site"].get("id") or "DPC",
        "refresh_seconds": cfg["ui"].get("refresh_seconds", 2),
        "nav_pages": [{"key": k, "url": u, "icon": i, "title": t, "ready": r, "tip": tip}
                      for k, u, i, t, r, tip in NAV_PAGES],
    }
