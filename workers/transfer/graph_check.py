"""`transfer.py --check`: prove the Microsoft Graph permission works (README, Q18), without a job.

Two ways to give the settings:

  rendered (a configured site):   DPC_WORKER_CONFIG=<root>/config/rendered/transfer.json \\
                                  python3 transfer.py --check [--json]
  direct (setup wizard step 8b, F91: before Apply there is no site.yaml and no rendered file):
      python3 transfer.py --check --json --site-id tadipatri \\
          --drive-site contoso.sharepoint.com:/sites/DPC   (or --drive-user dpc@contoso.com)
          [--models-folder DPC/models/<site.id>] [--upload-folder DPC/data/<site.id> | --no-upload]
          [--graph-stdin]
    The Graph login is NEVER a command-line argument (visible to every user in `ps`):
      --graph-stdin   one JSON object on stdin: {"tenant_id": .., "client_id": .., "client_secret": ..}
                      (a {"graph": {...}} wrapper as in site.yaml is accepted too)
      otherwise       env DPC_GRAPH_TENANT_ID, DPC_GRAPH_CLIENT_ID, DPC_GRAPH_CLIENT_SECRET
    Folder defaults as in site.yaml: DPC/models/<site.id> and DPC/data/<site.id>.

Steps, each ok | fail | skipped (a step after a failed token or drive is skipped):
  token          the login works (tenant, client id, secret)
  drive          the SharePoint site / OneDrive is found and readable
  models_folder  the site's models folder can be listed (missing is OK on a new site: nothing
                 uploaded yet; "exists" says which)
  upload_write   a small file is written, read back and deleted in <upload folder>/_check/
                 (skipped with --no-upload). Missing parent folders are created and left, as the
                 upload would create them anyway.

--json prints exactly one JSON object (F90 style) and nothing else:
  {"ok", "mode": "rendered"|"direct", "drive", "models_folder", "upload_folder",
   "steps": [{"step", "status", "detail"}], "error"}
"error" is set when the check could not run at all (bad arguments, missing login); then "steps"
is empty. Exit code 0 = every step that ran passed, 1 = otherwise. No secret is ever printed.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from graph_client import GraphClient
from transfer_errors import PermanentError, TemporaryError
from transfer_settings import (DEFAULT_MODELS_FOLDER, DEFAULT_REMOTE_FOLDER, ConfigError, build_settings,
                               parse_drive, read_raw_config, remote_path, secrets_in)

GRAPH_ENV = {"tenant_id": "DPC_GRAPH_TENANT_ID", "client_id": "DPC_GRAPH_CLIENT_ID",
             "client_secret": "DPC_GRAPH_CLIENT_SECRET"}
SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")            # the site_config rule for names
CHECK_FILE_TEXT = b"DPC transfer permission check\n"


@dataclass
class CheckTarget:
    mode: str                         # rendered | direct
    graph: Dict[str, str]
    drive: Dict[str, str]
    models_folder: str
    upload_folder: Optional[str]      # None = upload off: no write test

    def secrets(self) -> List[str]:
        return [v for v in self.graph.values() if v]


# ====================================================================== the check

def run_check(target: CheckTarget, graph=None) -> dict:
    """Every step in order; a later step is skipped when token or drive failed."""
    graph = graph or GraphClient(target.graph, target.drive)
    steps = []

    def add(step, status, detail=""):
        steps.append({"step": step, "status": status, "detail": _scrub(str(detail), target.secrets())[:300]})
        return status == "ok"

    def attempt(step, action):
        try:
            return add(step, "ok", action() or "")
        except (TemporaryError, PermanentError) as exc:
            return add(step, "fail", exc)

    reachable = attempt("token", lambda: graph.token() and "login accepted") and \
        attempt("drive", lambda: f"found ({graph.drive_base().rsplit('/v1.0', 1)[-1]})")
    if not reachable:
        for step in ("drive", "models_folder", "upload_write"):
            if not any(s["step"] == step for s in steps):
                add(step, "skipped", "needs the token and the drive")
    else:
        exists = {}

        def read_models():
            items = graph.list_children(target.models_folder)
            exists["value"] = items is not None
            if items is None:
                return f"{target.models_folder} does not exist yet (fine on a new site: no model uploaded)"
            return f"{target.models_folder}: {len(items)} item(s)"

        attempt("models_folder", read_models)
        if exists:
            steps[-1]["exists"] = exists["value"]
        if target.upload_folder is None:
            add("upload_write", "skipped", "upload is off")
        else:
            attempt("upload_write", lambda: _write_read_delete(graph, target.upload_folder))
    ok = all(s["status"] != "fail" for s in steps)
    return {"ok": ok, "mode": target.mode, "drive": target.drive, "models_folder": target.models_folder,
            "upload_folder": target.upload_folder, "steps": steps, "error": None}


def _write_read_delete(graph, upload_folder: str) -> str:
    folder = f"{upload_folder}/_check"
    path = f"{folder}/check.txt"
    graph.ensure_folder(folder)
    url, _ = graph.create_upload_session(path)
    outcome, item = graph.upload_fragment(url, CHECK_FILE_TEXT, 0, len(CHECK_FILE_TEXT))
    if outcome != "done" or (item or {}).get("size") != len(CHECK_FILE_TEXT):
        raise PermanentError(f"the test upload ended with {outcome}")
    if graph.download_range(path, 0, len(CHECK_FILE_TEXT) - 1) != CHECK_FILE_TEXT:
        raise PermanentError("the test file read back differs from what was written")
    graph.delete_item(path)
    graph.delete_item(folder)
    return f"{path} written, read back and deleted"


def _scrub(text: str, secrets: List[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# ====================================================================== settings for the check

def target_from_rendered(raw: dict) -> CheckTarget:
    s = build_settings(raw)
    return CheckTarget("rendered", s.graph, s.drive, s.models_folder,
                       s.upload.remote_folder if s.upload else None)


def graph_login(from_stdin: bool, stdin, environ) -> Dict[str, str]:
    """tenant_id / client_id / client_secret from stdin JSON or the environment."""
    if from_stdin:
        try:
            data = json.loads(stdin.read() or "null")
        except ValueError:
            raise ConfigError("--graph-stdin: stdin is not one JSON object")
        if isinstance(data, dict) and isinstance(data.get("graph"), dict):
            data = data["graph"]
        if not isinstance(data, dict):
            raise ConfigError('--graph-stdin: expected {"tenant_id": .., "client_id": .., "client_secret": ..}')
        login = {k: data.get(k) for k in GRAPH_ENV}
        missing = [k for k, v in login.items() if not isinstance(v, str) or not v.strip()]
        if missing:
            raise ConfigError("--graph-stdin: missing " + ", ".join(missing))
    else:
        login = {k: environ.get(name, "") for k, name in GRAPH_ENV.items()}
        missing = [GRAPH_ENV[k] for k, v in login.items() if not v.strip()]
        if missing:
            raise ConfigError("Graph login missing: set " + ", ".join(missing) + " (or use --graph-stdin)")
    return {k: v.strip() for k, v in login.items()}


def target_from_args(args, stdin, environ) -> CheckTarget:
    """Direct mode (F91): everything from the command line, the login from stdin or env."""
    drive = parse_drive({"site": args.drive_site} if args.drive_site else {"user": args.drive_user})
    site_id = args.site_id
    if site_id is not None and not SITE_ID.match(site_id):
        raise ConfigError(f"--site-id {site_id!r}: letters, digits, '.', '_' and '-' only")
    needs_site = (not args.models_folder) or (not args.no_upload and not args.upload_folder)
    if needs_site and not site_id:
        raise ConfigError("--site-id is needed for the default folders (or give --models-folder and "
                          "--upload-folder / --no-upload)")
    models = remote_path(args.models_folder or DEFAULT_MODELS_FOLDER, site_id or "", "--models-folder")
    upload = None if args.no_upload else remote_path(args.upload_folder or DEFAULT_REMOTE_FOLDER, site_id or "",
                                                     "--upload-folder")
    return CheckTarget("direct", graph_login(args.graph_stdin, stdin, environ), drive, models, upload)


# ====================================================================== command line

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="transfer.py --check", description="Test the Microsoft Graph permission "
                                "(README, Q18). Without --drive-* the rendered transfer.json is used.")
    p.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="print only one JSON result (setup wizard, F91)")
    drive = p.add_mutually_exclusive_group()
    drive.add_argument("--drive-site", help="SharePoint site, e.g. contoso.sharepoint.com:/sites/DPC")
    drive.add_argument("--drive-user", help="a user's OneDrive, e.g. dpc@contoso.com")
    p.add_argument("--site-id", help="site.id, for the default folders DPC/models/<site.id> and DPC/data/<site.id>")
    p.add_argument("--models-folder", help="default DPC/models/<site.id>")
    p.add_argument("--upload-folder", help="default DPC/data/<site.id>")
    p.add_argument("--no-upload", action="store_true", help="upload off: skip the write test")
    p.add_argument("--graph-stdin", action="store_true",
                   help='read {"tenant_id", "client_id", "client_secret"} as JSON from stdin')
    return p


def print_text(result: dict, out: Callable = print) -> None:
    if result["error"]:
        out(f"FAIL  {result['error']}")
        return
    for step in result["steps"]:
        out(f"{step['status'].upper():<6}{step['step']}" + (f": {step['detail']}" if step["detail"] else ""))


def check_main(argv, stdin=None, environ=None, graph_factory=None, out: Callable = print) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    environ = environ if environ is not None else os.environ
    args = parser().parse_args(argv)                    # a usage error exits 2 (argparse)
    direct = bool(args.drive_site or args.drive_user)
    result = {"ok": False, "mode": "direct" if direct else "rendered", "drive": None, "models_folder": None,
              "upload_folder": None, "steps": [], "error": None}
    secrets: List[str] = []
    try:
        if direct:
            target = target_from_args(args, stdin, environ)
        else:
            if any((args.site_id, args.models_folder, args.upload_folder, args.no_upload, args.graph_stdin)):
                raise ConfigError("--site-id, --models-folder, --upload-folder, --no-upload and --graph-stdin "
                                  "need --drive-site or --drive-user")
            raw = read_raw_config()
            secrets = secrets_in(raw)
            if not raw:
                raise ConfigError("no settings: set DPC_WORKER_CONFIG to the rendered transfer.json, or give "
                                  "--drive-site / --drive-user (setup wizard)")
            target = target_from_rendered(raw)
        secrets = target.secrets()
        graph = graph_factory(target.graph, target.drive) if graph_factory else None
        result = run_check(target, graph)
    except ConfigError as exc:
        result["error"] = _scrub(str(exc), secrets)
    except Exception as exc:                            # still one answer, never a traceback with a secret
        result["error"] = _scrub(f"{type(exc).__name__}: {exc}", secrets)[:500]
    if args.json:
        out(json.dumps(result))
    else:
        print_text(result, out)
    return 0 if result["ok"] else 1
