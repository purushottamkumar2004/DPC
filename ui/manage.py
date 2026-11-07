#!/usr/bin/env python3
"""Django management for the DPC UI (migrate, test, create_ui_admin)."""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dpc_ui.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
