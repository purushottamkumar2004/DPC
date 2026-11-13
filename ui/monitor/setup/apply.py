"""Apply (D-103 phase 2, D-107): one background job, then the Supervisor decides.

  1. secrets: the D-108 standard login is refilled from site_defaults.yaml; any other missing
     secret refuses Apply (the review shows which, "not set")
  2. db/bootstrap.py --seed-only again with the chosen camera serials (D-112, F97: seeds the existing
     database the person created; idempotent; adds the health rows; never creates a DB or a user)
  3. site_from_draft -> site_writer.write_site (loader, backup, atomic, mode 600)
  4. workers_file.write_workers_file (mail disabled when the draft has no mail)
  5. <root>/run/setup_finish = {"id": ...}; the Supervisor answers in setup_finish.result.json
     ok -> it removes the marker and re-execs in normal mode (every program starts, the UI too)
     refused -> it stays in setup mode; the page shows the problems
"""
import json
import os
import subprocess
import uuid

from monitor.setup import env, steps, vault

REQUEST, RESULT = "setup_finish", "setup_finish.result.json"


class ApplyRefused(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def run(answers):
    """The Apply job. Returns {"id", "backup", "changes", "bootstrap"} or raises ApplyRefused."""
    env.import_common()
    from common.config.setup_draft import DraftError, secrets_needed, site_from_draft
    from common.config.site_config import SiteConfigError, load_site_config
    from common.config.site_writer import write_site
    from common.config.workers_file import write_workers_file
    from dpc_ui.ui_config import get_config

    steps.refill_standard(answers)
    missing = [k for k in secrets_needed(answers) if not vault.get(k)]
    if missing:
        raise ApplyRefused([f"{k} is not set: enter it again (it is kept only until the screen restarts)" for k in missing])

    from monitor.setup import tools
    from monitor.setup.jobs import parse_output
    serials = [c["serial"] for c in answers.get("cameras") or [] if c.get("side")]
    cmd, secret_env = tools.seed_command(answers, serials)
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=300,
                          env=dict(os.environ, **secret_env))
    boot = parse_output(proc.stdout) or {}
    if proc.returncode != 0 or not boot.get("ok"):
        error = boot.get("error") or (proc.stderr or "").strip().splitlines()[-1:] or ["a step failed"]
        error = error if isinstance(error, str) else error[0]
        if "--seed-only" in error or "unrecognized arguments" in error:
            raise ApplyRefused(["database: Apply needs the newer db/bootstrap.py (F97, --seed-only); update the programs"])
        raise ApplyRefused([f"database: {tools.seed_error_sentence(error, answers['database'].get('name'))}"] +
                           [f"database {s.get('step')} {s.get('item')}: {s.get('result')}" for s in boot.get("steps") or []
                            if s.get("result") == "failed"])

    site_yaml = get_config().paths["site_yaml"]
    try:
        data = site_from_draft({"answers": answers}, vault.all_values(), root=env.root())
        written = write_site(data, path=site_yaml)
        cfg = load_site_config(site_yaml)
        write_workers_file(cfg, mail_enabled=bool(answers.get("mail")))
    except DraftError as e:
        raise ApplyRefused(e.problems)
    except SiteConfigError as e:
        raise ApplyRefused(e.problems)

    request_id = uuid.uuid4().hex
    os.makedirs(env.run_dir(), exist_ok=True)
    try:
        os.remove(os.path.join(env.run_dir(), RESULT))         # an old answer must not be read as this one
    except OSError:
        pass
    tmp = os.path.join(env.run_dir(), REQUEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": request_id}, f)
    os.replace(tmp, os.path.join(env.run_dir(), REQUEST))
    return {"id": request_id, "backup": getattr(written, "backup", None),
            "changes": len(getattr(written, "changes", []) or []), "bootstrap": boot.get("steps") or []}


def finish_result(request_id):
    """The Supervisor's answer for this request: {"ok", "mode", "problems"} or None (not yet)."""
    try:
        with open(os.path.join(env.run_dir(), RESULT), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") == request_id else None
