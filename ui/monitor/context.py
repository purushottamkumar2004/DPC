"""Template context available on every page."""
from dpc_ui.ui_config import get_config


def site(request):
    cfg = get_config()
    return {
        "site_name": cfg["site"].get("name") or cfg["site"].get("id") or "DPC",
        "refresh_seconds": cfg["ui"].get("refresh_seconds", 2),
    }
