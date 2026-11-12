"""Where the setup wizard finds things (CONTRACTS 4.1, D-103, D-107, D-108).

  code root   <root>/code (ui.json paths.code); the wizard imports common.config from it and runs
              the tools below with the same Python as the UI
  mode        <root>/run/mode.json {mode: setup | normal, reason, since} written by the Supervisor
  defaults    <root>/config/site_defaults.yaml (install.sh, D-108): db.name_suffix, db.user,
              db.password (the company-standard password; never shown, never in the draft)
  work folder <root>/data/state/ui/setup/ (grabbed frames, bursts); only this folder is served
"""
import json
import os
import sys

from dpc_ui.ui_config import get_config

TOOLS = {
    "bootstrap": ("db", "bootstrap.py"),
    "camera_probe": ("workers", "frame_capture_basler", "camera_probe.py"),
    "plc": ("workers", "plc", "plc_worker.py"),
    "measure": ("workers", "inspection", "measure.py"),
    "selftest": ("workers", "inspection", "model_selftest.py"),
    "package": ("scripts", "make_model_package.py"),
    "mail": ("workers", "mail", "mail_worker.py"),
    "transfer": ("workers", "transfer", "transfer.py"),
    "supervisor": ("supervisor", "supervisor.py"),
}
_tool_override = {}          # tests: name -> fake script


def root():
    return get_config().paths["root"]


def code_root():
    return get_config().paths["code"]


def tool_path(name):
    return _tool_override.get(name) or os.path.join(code_root(), *TOOLS[name])


def set_tool_override(mapping):
    """Tests point tools at fake scripts that print recorded --json results."""
    _tool_override.clear()
    _tool_override.update(mapping or {})


def python():
    return sys.executable


def work_dir(*parts):
    path = os.path.join(get_config().paths["state"], "setup", *parts)
    os.makedirs(path, exist_ok=True)
    return path


def run_dir():
    return get_config().paths["run"]


def mode():
    """'setup' or 'normal' (normal when the file is missing: a site set up before S1)."""
    try:
        with open(os.path.join(run_dir(), "mode.json"), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("mode") or "normal"
    except (OSError, ValueError):
        return "normal"


def import_common():
    """Make `common.config` importable from the code root (read-only use)."""
    code = code_root()
    if code not in sys.path:
        sys.path.insert(0, code)


def site_defaults():
    """D-108 defaults: {"name_suffix", "user", "password"}; {} when the installer wrote none."""
    path = os.path.join(get_config().paths["config"], "site_defaults.yaml")
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, ImportError, ValueError):
        return {}
    except Exception:                       # broken YAML: treat as missing, the step asks
        return {}
    db = data.get("db") if isinstance(data.get("db"), dict) else {}
    return {k: db.get(k) for k in ("name_suffix", "user", "password") if db.get(k) not in (None, "")}
