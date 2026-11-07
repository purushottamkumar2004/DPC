"""Django settings for the DPC local UI (D-095).

* Everything comes from ui.json (dpc_ui/ui_config.py); nothing site-specific is written here.
* Django's own database is SQLite in <root>/data/state/ui/ui.sqlite3 (events, users, sessions).
  The site MySQL DB is read through monitor/sources/sitedb.py, never through the ORM, so Django
  never migrates or writes the site database.
* Listens on 127.0.0.1 only (run_ui.py); no login in UI-1/UI-2 (developer, 2026-11-06,
  proposed D-095 amendment). django.contrib.auth stays installed for UI-3/UI-5.
"""
import logging
import os
import secrets
from pathlib import Path

from dpc_ui.ui_config import get_config

BASE_DIR = Path(__file__).resolve().parent.parent
CFG = get_config()
STATE_DIR = CFG.paths["state"]

log = logging.getLogger("dpc.ui.settings")


def _secret_key() -> str:
    """Generated on first start into <state>/secret_key (chmod 600); never in the repo."""
    key_file = os.path.join(STATE_DIR, "secret_key")
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_urlsafe(50)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except FileExistsError:            # another process wrote it first: use that one
        with open(key_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:               # tests / no state folder: a key for this run only
        log.warning("Could not store the UI secret key in %s (%s); using a temporary key", key_file, e)
    return key


SECRET_KEY = _secret_key()
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.auth",          # prepared for UI-3/UI-5 roles (D-046); not used for access now
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "monitor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "dpc_ui.urls"
WSGI_APPLICATION = "dpc_ui.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.messages.context_processors.messages",
        "monitor.context.site",
    ]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.path.join(STATE_DIR, "ui.sqlite3"),
        "OPTIONS": {"timeout": 20},     # the MQTT thread and request threads share the file
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"



def _machine_time_zone() -> str:
    """The machine's own zone name (site DB datetimes are naive local time); never hardcoded."""
    if os.environ.get("TZ", "").strip(":"):
        return os.environ["TZ"].strip(":")
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    return "UTC"


USE_TZ = False
TIME_ZONE = _machine_time_zone()
USE_I18N = False
LANGUAGE_CODE = "en"

STATIC_URL = "/static/"
STATIC_DIR = BASE_DIR / "static"     # served by dpc_ui/urls.py: no CDN, no collectstatic

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.request": {"level": "ERROR"}},
}
