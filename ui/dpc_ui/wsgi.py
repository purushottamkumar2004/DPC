"""WSGI entry used by run_ui.py (waitress)."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dpc_ui.settings")
application = get_wsgi_application()
