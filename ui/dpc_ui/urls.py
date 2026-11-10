"""URL map (D-101 structure). Static files are served by Django itself from ui/static/: the UI
runs on 127.0.0.1 for one kiosk browser, so no web server or CDN is needed (D-095).

  /                      Overview (level 1)
  /side/<SIDE>/          LEFT / RIGHT page (level 2), tabs Live and Detections
  /defects/              Defects
  /system/programs/      System > Programs (logs), /system/health/, /system/alerts/
Older addresses (/programs/, /alerts/, /health/, /cameras/<SIDE>/) keep working.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import path, re_path
from django.views.static import serve

from monitor.views import alerts, cameras, dashboard, defects, health, overview_page, programs, side_page

urlpatterns = [
    path("", overview_page.page, name="overview"),
    path("part/overview/", overview_page.poll, name="part-overview"),
    path("part/banner/", dashboard.banner, name="part-banner"),
    path("live/<str:camera_id>.jpg", dashboard.live_image, name="live-image"),

    path("side/<str:side>/", side_page.page, name="side"),
    path("part/side/<str:side>/page/", side_page.part, name="part-side-page"),
    re_path(r"^side/(?P<side>[A-Za-z]+)/roi/(?P<name>[\w./-]+)$", side_page.roi_file, name="side-roi-file"),
    path("cameras/<str:side>/", lambda request, side: redirect(f"/side/{side.upper()}/" + (
        "?tab=detections" if request.GET.get("tab") == "detections" else "")), name="camera"),
    path("part/cameras/<str:side>/", cameras.part, name="part-camera"),
    path("cameras/<str:side>/detections.json", cameras.detections, name="camera-detections"),
    path("cameras/<str:side>/frame/<int:seq>.jpg", cameras.frame, name="camera-frame"),

    path("system/", lambda request: redirect("/system/programs/"), name="system"),
    path("system/programs/", programs.page, name="programs"),
    path("system/programs/<str:worker_id>/", programs.detail, name="program"),
    path("system/health/", health.page, name="health"),
    path("system/alerts/", alerts.page, name="alerts"),
    path("programs/", programs.page),
    path("programs/<str:worker_id>/", programs.detail),
    path("health/", health.page),
    path("alerts/", alerts.page),
    path("part/alerts/", alerts.part, name="part-alerts"),
    path("part/programs/", programs.part, name="part-programs"),
    path("part/programs/<str:worker_id>/log/", programs.log_part, name="part-program-log"),
    path("part/health/", health.part, name="part-health"),
    path("health/dpc-check/", health.dpc_check_start, name="dpc-check-start"),
    path("part/dpc-check/", health.dpc_check_part, name="part-dpc-check"),

    path("defects/", defects.page, name="defects"),
    path("defects/export.csv", defects.export_csv, name="defects-csv"),
    path("defects/<int:defect_id>/", defects.detail, name="defect"),
    path("defects/<int:defect_id>/image.jpg", defects.image, name="defect-image"),

    re_path(r"^static/(?P<path>.*)$", serve, {"document_root": settings.STATIC_DIR}),
]
