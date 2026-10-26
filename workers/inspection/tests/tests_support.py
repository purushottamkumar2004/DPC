"""Shared helpers for the inspection tests: reference settings from the real renderer,
detections, frame names, a fake DB cursor/connection."""
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(WORKER_DIR))
for p in (WORKER_DIR, REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import settings as S                     # noqa: E402
from common.config.render import render  # noqa: E402

REFERENCE_SITE = os.path.join(REPO_ROOT, "docs", "sites", "tadipatri.site.reference.yaml")


def render_reference(root):
    """Render the Tadipatri reference site.yaml into <root>; returns {worker_id: json path}."""
    with open(REFERENCE_SITE) as f:
        site = yaml.safe_load(f)
    site["paths"]["root"] = root
    site_path = os.path.join(root, "site.yaml")
    with open(site_path, "w") as f:
        yaml.safe_dump(site, f)
    os.chmod(site_path, 0o600)
    render(site_path)
    rendered = os.path.join(root, "config", "rendered")
    return {n[:-5]: os.path.join(rendered, n) for n in os.listdir(rendered)}


def load_reference_settings(root):
    """(left, right) Settings of the reference site, with their output folders and INF created."""
    files = render_reference(root)
    sides = S.load_settings(files["inspection_left"]), S.load_settings(files["inspection_right"])
    for s in sides:
        for folder in s.paths.output_dirs() + (s.paths.inf,):
            os.makedirs(folder, exist_ok=True)
    return sides


def det(cls, xmin, ymin, xmax, ymax, score=0.9):
    """A detection as the model returns it: [score, ymin, ymax, xmin, xmax, class_name]."""
    return [score, ymin, ymax, xmin, xmax, cls]


def frame_name(second, ms=0):
    return f"2026_10_26_10_00_{second:02d}_{ms * 1000:06d}.jpg"


class FakeCursor:
    def __init__(self, found=False):
        self.found, self.queries = found, []

    def execute(self, query, params=None):
        self.queries.append(query.strip())

    def fetchall(self):
        return [("row",)] if self.found else []

    def close(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self.c, self.commits = cursor, 0

    def commit(self):
        self.commits += 1

    def cursor(self):
        return self.c
