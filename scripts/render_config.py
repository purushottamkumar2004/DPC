#!/usr/bin/env python3
"""Render <root>/config/rendered/<worker_id>.json from site.yaml (CONTRACTS 13, D-062).

    python3 ~/INSIGHTZZ/code/scripts/render_config.py [site.yaml] [--check]

Prints one line per file: `written|unchanged|removed <worker_id>`.
--check: report what would change, write nothing.
Exit 0 = ok, 1 = site.yaml invalid (nothing written; the problems are printed, never a value).
The Supervisor runs the same code at start and on reload; running it by hand is safe.
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common.config.render import render  # noqa: E402
from common.config.site_config import SiteConfigError  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="render_config.py", description="Render worker configs from site.yaml.")
    parser.add_argument("path", nargs="?", help="site.yaml (default: $DPC_SITE_CONFIG or ~/INSIGHTZZ/config/site.yaml)")
    parser.add_argument("--check", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args(argv)
    try:
        changes = render(args.path, dry_run=args.check)
    except SiteConfigError as exc:
        print(exc)
        print("nothing written")
        return 1
    for change in changes:
        print(f"{change.status} {change.worker_id}")
    if args.check:
        print("(--check: nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
