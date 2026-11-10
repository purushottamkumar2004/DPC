"""URL map. Static files are served from ui/static/ by Django itself: the UI runs on
127.0.0.1 for one kiosk browser, so no separate web server or CDN is needed (D-095)."""
from django.conf import settings
from django.urls import path, re_path
from django.views.static import serve

from monitor.views import alerts, cameras, dashboard, defects, health, programs

urlpatterns = [
    path("", dashboard.page, name="dashboard"),
    path("part/dash/", dashboard.poll, name="part-dash"),
    path("part/banner/", dashboard.banner, name="part-banner"),
    path("part/side/<str:side>/", dashboard.side, name="part-side"),
    path("part/tiles/", dashboard.tiles, name="part-tiles"),
    path("part/recent-defects/", dashboard.recent_defects, name="part-recent-defects"),
    path("live/<str:camera_id>.jpg", dashboard.live_image, name="live-image"),

    path("cameras/<str:side>/", cameras.page, name="camera"),
    path("part/cameras/<str:side>/", cameras.part, name="part-camera"),
    path("cameras/<str:side>/detections.json", cameras.detections, name="camera-detections"),
    path("cameras/<str:side>/frame/<int:seq>.jpg", cameras.frame, name="camera-frame"),

    path("alerts/", alerts.page, name="alerts"),
    path("part/alerts/", alerts.part, name="part-alerts"),

    path("programs/", programs.page, name="programs"),
    path("part/programs/", programs.part, name="part-programs"),
    path("programs/<str:worker_id>/", programs.detail, name="program"),
    path("part/programs/<str:worker_id>/log/", programs.log_part, name="part-program-log"),

    path("health/", health.page, name="health"),
    path("part/health/", health.part, name="part-health"),
    path("health/dpc-check/", health.dpc_check_start, name="dpc-check-start"),
    path("part/dpc-check/", health.dpc_check_part, name="part-dpc-check"),

    path("defects/", defects.page, name="defects"),
    path("defects/export.csv", defects.export_csv, name="defects-csv"),
    path("defects/<int:defect_id>/", defects.detail, name="defect"),
    path("defects/<int:defect_id>/image.jpg", defects.image, name="defect-image"),

    re_path(r"^static/(?P<path>.*)$", serve, {"document_root": settings.STATIC_DIR}),
]
