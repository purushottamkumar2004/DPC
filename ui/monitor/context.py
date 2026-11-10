"""Template context available on every page."""
from dpc_ui.ui_config import get_config


# The sidebar (D-098 4). Pages of later stages are shown greyed with what they will bring.
NAV_PAGES = [
    ("overview", "/", "layout-dashboard", "Overview", True, "Overview: the situation at a glance"),
    ("side-LEFT", "/side/LEFT/", "camera", "LEFT", True, "LEFT side: image, detections, functions, auto-ROI, model, camera"),
    ("side-RIGHT", "/side/RIGHT/", "camera", "RIGHT", True, "RIGHT side: image, detections, functions, auto-ROI, model, camera"),
    ("defects", "/defects/", "scan-search", "Defects", True, "Defects: filter, image, CSV"),
    ("system", "/system/programs/", "list-checks", "System", True, "System: programs and logs, health, alert history"),
    ("setup", "#", "sliders-horizontal", "Setup", False, "Site setup wizard: coming"),
]


def site(request):
    cfg = get_config()
    return {
        "site_name": cfg["site"].get("name") or cfg["site"].get("id") or "DPC",
        "refresh_seconds": cfg["ui"].get("refresh_seconds", 2),
        "system_pages": ("programs", "health", "alerts"),
        "nav_pages": [{"key": k, "url": u, "icon": i, "title": t, "ready": r, "tip": tip}
                      for k, u, i, t, r, tip in NAV_PAGES],
    }
