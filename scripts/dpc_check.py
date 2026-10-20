#!/usr/bin/env python3
"""dpc-check: preflight for one DPC site (STATUS item 5, D-035).

Run it as the service user, from the code checkout, any time (also while the captures run):
    python3 ~/INSIGHTZZ/code/scripts/dpc_check.py [path/to/site.yaml]

Prints one line per check, then one summary line:
    PASS  site.yaml     valid (schema 2)
    FAIL  cameras       CAM2 serial 24874775 not found (found: 24824427)
    ...
    RESULT FAIL (exit 1) in 3.2 s

Exit codes: 0 = all pass, 1 = at least one FAIL, 2 = no FAIL but at least one WARN.

Safe on a running site:
- Cameras are only ENUMERATED with pypylon (the same call the capture uses to find its camera).
  A camera is never opened: the running captures hold them, and opening one would stop a capture.
- The DB check logs in and runs SELECT 1; nothing is written.
- MQTT is a TCP connect only (a warning, never a failure: D-034).
- Nothing is created, changed or deleted.
It never prints a secret: config problems come from the loader (which never shows secret values),
and DB errors are reported by MySQL error code only (MySQL's own message names the user).
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

# Make `common` importable when run as a plain script from <root>/code/scripts.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common.config.model_package import ModelPackageError, read_current, verify_package  # noqa: E402
from common.config.site_config import (  # noqa: E402
    FRAME_SUBDIRS, IMAGE_KINDS, SiteConfig, SiteConfigError, default_config_path, load_site_config,
)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
EXIT_PASS, EXIT_FAIL, EXIT_WARN = 0, 1, 2

# Time limits keep the whole run under 30 s even when everything times out.
CAMERA_ENUM_TIMEOUT_S = 12
DB_TIMEOUT_S = 4
MQTT_TIMEOUT_S = 3
GIT_TIMEOUT_S = 2

# Folders under data/ that are not per camera or per side (CONTRACTS 4.1). wheel_status is left
# out on purpose: its subfolders are still "assumed" (Q13).
DATA_TOP_DIRS = ("bolt_data", "state", "models")
# The frame hand-off uses hard links and renames, so these must share one filesystem (D-031).
SAME_FS_SUBDIRS = ("_tmp", "INF", "UI", "live", "RAW_IMAGES")

# MySQL error codes -> reason. MySQL's own text is never printed: it contains the user name.
DB_ERROR_REASONS = {
    1044: "access denied to database {db} (check the user's grants)",
    1045: "access denied (check db.user / db.password)",
    1049: "unknown database {db} (check db.name)",
    2003: "cannot reach MySQL at {host}:{port} (is it running? check db.host / db.port)",
    2005: "unknown host {host} (check db.host)",
    2013: "connection lost during login (MySQL at {host}:{port})",
}


@dataclass
class Result:
    status: str     # PASS | WARN | FAIL | SKIP
    name: str
    detail: str


# ======================================================================
# Checks. Each takes the loaded config and returns one Result.
# ======================================================================

def check_site_yaml(path: str):
    """Valid v2 file, chmod 600 and owned by this user (both checked by the loader)."""
    try:
        cfg = load_site_config(path)
    except SiteConfigError as exc:
        return None, Result(FAIL, "site.yaml", f"{len(exc.problems)} problem(s): " + "; ".join(exc.problems))
    if cfg.warnings:
        return cfg, Result(WARN, "site.yaml", f"valid (schema {cfg.schema_version}); " + "; ".join(cfg.warnings))
    return cfg, Result(PASS, "site.yaml", f"valid (schema {cfg.schema_version}), chmod 600, owned by you")


def _git(args: List[str], cwd: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_outside_git(cfg: SiteConfig) -> Result:
    """site.yaml holds the secrets: it must never be committable (D-048)."""
    folder = os.path.dirname(cfg.source_path)
    inside = _git(["rev-parse", "--is-inside-work-tree"], folder)
    if inside is None:
        return Result(WARN, "git", "git not available; could not check that site.yaml is outside git")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return Result(PASS, "git", "site.yaml is outside any git checkout")
    ignored = _git(["check-ignore", "-q", cfg.source_path], folder)
    if ignored is not None and ignored.returncode == 0:
        return Result(WARN, "git", "site.yaml is inside a git checkout (ignored); keep it in <root>/config")
    return Result(FAIL, "git", "site.yaml is inside a git checkout and NOT ignored; move it to <root>/config")


def expected_dirs(cfg: SiteConfig) -> List[str]:
    """Folders of the deployed layout (CONTRACTS 4.1) for the configured cameras and sides."""
    p = cfg.paths
    dirs = [p.code, p.config, p.rendered, p.logs, p.run]
    for cam in cfg.cameras:
        dirs += [p.frame_dir(cam.id, sub) for sub in FRAME_SUBDIRS]
        dirs += [p.image_dir(kind, cam.side) for kind in IMAGE_KINDS]
    dirs += [os.path.join(p.data, d) for d in DATA_TOP_DIRS]
    return dirs


def check_layout(cfg: SiteConfig) -> Result:
    missing = [d for d in expected_dirs(cfg) if not os.path.isdir(d)]
    if missing:
        shown = ", ".join(os.path.relpath(d, cfg.paths.root) for d in missing[:3])
        more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
        return Result(FAIL, "layout", f"{len(missing)} folder(s) missing: {shown}{more} "
                                      f"(run scripts/setup_dpc_layout.sh)")
    for cam in cfg.cameras:
        devices = {os.stat(cfg.paths.frame_dir(cam.id, sub)).st_dev for sub in SAME_FS_SUBDIRS}
        if len(devices) > 1:
            return Result(FAIL, "layout", f"frames/{cam.id} folders are on different filesystems; "
                                          f"hard links and renames need one (D-031)")
    return Result(PASS, "layout", f"all {len(expected_dirs(cfg))} folders exist; frame folders share one filesystem")


def disk_used_percent(path: str) -> float:
    """Same formula as the cleanup (delete_script.py): used / (used + free), like `df`."""
    du = shutil.disk_usage(path)
    return du.used / (du.used + du.free) * 100


def check_disk(cfg: SiteConfig) -> Result:
    """Below the cleanup threshold: PASS. Cleanup range: WARN (cleanup should be deleting).
    At or above the mail threshold: FAIL (cleanup is not keeping up)."""
    limits = cfg.cleanup.disk_percent
    if not os.path.isdir(cfg.paths.data):
        return Result(FAIL, "disk", "data folder missing, cannot measure (see layout)")
    used = disk_used_percent(cfg.paths.data)
    text = f"data disk {used:.1f}% used"
    if used >= limits["mail"]:
        return Result(FAIL, "disk", f"{text}, at or above the mail threshold {limits['mail']}%")
    if used >= limits["delete"]:
        return Result(WARN, "disk", f"{text}, above the cleanup threshold {limits['delete']}%; "
                                    f"check that cleanup is running")
    return Result(PASS, "disk", f"{text}, below the cleanup threshold {limits['delete']}%")


# Runs in a child process: a hung GigE discovery cannot hang dpc-check, and a pypylon crash
# cannot take it down. EnumerateDevices only lists cameras; it never opens one.
_ENUMERATE_CODE = (
    "from pypylon import pylon\n"
    "for d in pylon.TlFactory.GetInstance().EnumerateDevices():\n"
    "    print(d.GetSerialNumber())\n"
)


class EnumerationError(Exception):
    pass


def enumerate_basler_serials(timeout_s: float = CAMERA_ENUM_TIMEOUT_S) -> List[str]:
    try:
        done = subprocess.run([sys.executable, "-c", _ENUMERATE_CODE],
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise EnumerationError(f"pypylon device enumeration took more than {timeout_s} s") from None
    if done.returncode != 0:
        if "No module named" in done.stderr:
            raise EnumerationError("pypylon is not installed for this Python")
        last = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise EnumerationError(f"pypylon device enumeration failed: {last[:120]}")
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def check_cameras(cfg: SiteConfig, enumerate_serials: Callable[[], List[str]] = enumerate_basler_serials) -> Result:
    basler = [c for c in cfg.cameras if c.driver == "basler"]
    flir = [c.id for c in cfg.cameras if c.driver == "flir"]
    flir_note = f"; {', '.join(flir)} (FLIR) not checked: only Basler enumeration is built" if flir else ""
    if not basler:
        return Result(WARN, "cameras", "no Basler camera configured" + flir_note)
    try:
        found = enumerate_serials()
    except EnumerationError as exc:
        return Result(FAIL, "cameras", str(exc))
    missing = [f"{c.id} serial {c.serial}" for c in basler if c.serial not in found]
    extra = sorted(set(found) - {c.serial for c in cfg.cameras})
    extra_note = f"; also connected, not configured: {', '.join(extra)}" if extra else ""
    if missing:
        seen = ", ".join(found) if found else "none"
        return Result(FAIL, "cameras", f"{'; '.join(missing)} not found (found: {seen}){extra_note}{flir_note}")
    ok = ", ".join(f"{c.id}={c.serial}" for c in basler)
    return Result(WARN if flir else PASS, "cameras", f"detected {ok}{extra_note}{flir_note}")


def _db_reason(exc: Exception, cfg: SiteConfig) -> str:
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    template = DB_ERROR_REASONS.get(code, "MySQL error {code}" if code else "login failed ({kind})")
    return template.format(db=cfg.db.name, host=cfg.db.host, port=cfg.db.port, code=code,
                           kind=type(exc).__name__)


def check_db(cfg: SiteConfig) -> Result:
    """Log in with the site.yaml credentials and run SELECT 1. Writes nothing."""
    try:
        import pymysql
    except ImportError:
        return Result(FAIL, "database", "pymysql is not installed for this Python")
    try:
        conn = pymysql.connect(host=cfg.db.host, port=cfg.db.port, user=cfg.db.user,
                               password=cfg.db.password, database=cfg.db.name,
                               connect_timeout=DB_TIMEOUT_S, read_timeout=DB_TIMEOUT_S)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        finally:
            conn.close()
    except Exception as exc:          # every driver error: report the code, never the message
        return Result(FAIL, "database", _db_reason(exc, cfg))
    return Result(PASS, "database", f"login ok, database {cfg.db.name} on {cfg.db.host}:{cfg.db.port}")


def check_mqtt(cfg: SiteConfig) -> Result:
    """TCP connect to the broker only. A warning, never a failure (D-034)."""
    where = f"{cfg.mqtt.host}:{cfg.mqtt.port}"
    try:
        with socket.create_connection((cfg.mqtt.host, cfg.mqtt.port), timeout=MQTT_TIMEOUT_S):
            pass
    except OSError as exc:
        reason = "timed out" if isinstance(exc, socket.timeout) else type(exc).__name__
        return Result(WARN, "mqtt", f"broker {where} not reachable ({reason}); status events will be lost")
    return Result(PASS, "mqtt", f"broker {where} reachable (TCP; login not tested)")


def check_model(cfg: SiteConfig) -> Result:
    """The model package inspection would run (CONTRACTS 12): `current` if set, else the version
    in site.yaml. Not installed = WARN (captures can run without it); a bad package = FAIL."""
    model = cfg.inspection.model
    try:
        current = read_current(model.root)
    except ModelPackageError as exc:
        return Result(FAIL, "model", f"{model.name}/current: {exc.problems[0]}")
    version = current or model.version
    label = f"{model.name}/{version}"
    if not os.path.isdir(os.path.join(model.root, version)):
        return Result(WARN, "model", f"{label} not installed in data/models; inspection cannot start yet")
    try:
        package = verify_package(os.path.join(model.root, version), name=model.name)
    except ModelPackageError as exc:
        return Result(FAIL, "model", f"{label}: " + "; ".join(exc.problems))
    notes = []
    if current is None:
        notes.append("no current pointer yet")
    elif current != model.version:
        notes.append(f"current is {current}, site.yaml names {model.version}")
    if not package.ready:
        notes.append("no ready marker")
    detail = f"{label} verified ({len(package.classes)} classes, all checksums match)"
    if current is not None and current != model.version:
        return Result(WARN, "model", f"{detail}; " + "; ".join(notes))
    return Result(PASS, "model", detail + (f"; {'; '.join(notes)}" if notes else ""))


# ======================================================================
# Run
# ======================================================================

def run_checks(path: str, enumerate_serials: Optional[Callable[[], List[str]]] = None) -> List[Result]:
    """site.yaml first: every other check needs its values (root, serials, DB login)."""
    enumerate_serials = enumerate_serials or enumerate_basler_serials
    cfg, first = check_site_yaml(path)
    results = [first]
    names = ["git", "layout", "disk", "cameras", "database", "mqtt", "model"]
    if cfg is None:
        return results + [Result(SKIP, name, "site.yaml is not valid") for name in names]
    checks = [check_outside_git, check_layout, check_disk,
              lambda c: check_cameras(c, enumerate_serials), check_db, check_mqtt, check_model]
    for name, check in zip(names, checks):
        try:
            results.append(check(cfg))
        except Exception as exc:    # a bug in one check must not hide the others; never print a value
            results.append(Result(FAIL, name, f"check crashed: {type(exc).__name__}"))
    return results


def exit_code(results: List[Result]) -> int:
    statuses = {r.status for r in results}
    if FAIL in statuses or SKIP in statuses:
        return EXIT_FAIL
    return EXIT_WARN if WARN in statuses else EXIT_PASS


def format_result(result: Result) -> str:
    return f"{result.status:<5} {result.name:<10} {result.detail}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dpc_check.py", description="Preflight checks for one DPC site.")
    parser.add_argument("path", nargs="?", help="site.yaml (default: $DPC_SITE_CONFIG or ~/INSIGHTZZ/config/site.yaml)")
    args = parser.parse_args(argv)
    path = os.path.abspath(os.path.expanduser(args.path)) if args.path else default_config_path()

    started = time.monotonic()
    results = run_checks(path)
    for result in results:
        print(format_result(result), flush=True)
    code = exit_code(results)
    word = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_WARN: "PASS with warnings"}[code]
    print(f"RESULT {word} (exit {code}) in {time.monotonic() - started:.1f} s")
    return code


if __name__ == "__main__":
    sys.exit(main())
