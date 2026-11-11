"""The setup wizard's draft (S2, D-103 phase 1) and the step from a finished draft to site.yaml.

File: <root>/data/state/ui/setup_draft.json (mode 600, written atomically), so the wizard can be
left and resumed. It holds the answers so far and NO secret: passwords, the DB and MQTT user
names and the Graph keys are typed at Apply only and go straight into site.yaml. The draft may
record which secrets were already given, by name only ("secrets_set": ["db.password", ...]), so
the wizard can show them as set / not set.

    {
      "format": 1, "updated_at": "2026-11-12T10:05:00", "step": "machine",
      "secrets_set": ["db.password"],
      "answers": {
        "site": {"id": "baga", "name": "Baga DPC"},
        "database": {"host": "localhost", "port": 3306, "name": "baga_dpc"},
        "mqtt": {"host": "localhost", "port": 1883},                      # optional
        "cameras": [{"id": "CAM1", "side": "LEFT", "driver": "basler", "serial": "40112233",
                     "ip": "192.168.1.102", "fps": 4.0, "exposure_us": 1500, "rotate": 0,
                     "motion": "rtl"}, ...],                            # motion from direction detection
        "plc": {"ip": "192.168.1.5", "light_register": 4109, "cleaning_register": 4106,
                "conveyor_status_register": 4098},                     # optional keys as site.yaml
        "machine": {"total_wheel_count": 156},                          # any count; expected = count
        "models": {"LEFT": "dpc_maskrcnn_left", "RIGHT": "dpc_maskrcnn_right"},   # one name = one model
        "threshold": 0.5,                                               # optional
        "object_sizes": {"LEFT": {"wheel_w": 530, ...}, "RIGHT": {...}},
        "mail": null | {"sender": "graph", "sender_address": "...", "project_name": "...",
                        "recipients": [...], "cc": [...]},              # optional (step 8)
        "transfer": null | {"drive": {"site": "x.sharepoint.com:/sites/DPC"}, "upload": true}  # optional (8b)
      }
    }

    from common.config.setup_draft import load_draft, save_draft, site_from_draft
    save_draft(root, answers, step="cameras")
    data = site_from_draft(load_draft(root), secrets={"db.user": ..., "db.password": ...}, root=root)
    # then common.config.site_writer.write_site(data) validates and writes it
"""

import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from common.config.site_config import _SECRET_NAME_RE

DRAFT_FORMAT = 1
SIDES = ("LEFT", "RIGHT")
# Secrets given at Apply (dotted site.yaml keys). Which are needed depends on the answers.
SECRET_KEYS = ("db.user", "db.password", "mqtt.user", "mqtt.password",
               "graph.tenant_id", "graph.client_id", "graph.client_secret", "mail.smtp.password")
# Answer keys that are never stored even though their name does not look secret.
NEVER_IN_DRAFT = {("database", "user"), ("mqtt", "user")}


class DraftError(Exception):
    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__("setup draft: " + "; ".join(self.problems))


def draft_path(root: str) -> str:
    return os.path.join(os.path.expanduser(root), "data", "state", "ui", "setup_draft.json")


# ======================================================================
# Saving and loading
# ======================================================================

def _secret_keys_in(node: Any, path=()) -> List[str]:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = path + (str(key),)
            if _SECRET_NAME_RE.search(str(key)) or here in NEVER_IN_DRAFT:
                found.append(".".join(here))
            found += _secret_keys_in(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += _secret_keys_in(item, path + (str(i),))
    return found


def save_draft(root: str, answers: Dict[str, Any], step: str = "", secrets_set: Optional[List[str]] = None) -> str:
    """Write the draft atomically (mode 600). Refuses an answer that is a secret."""
    secret = _secret_keys_in(answers)
    if secret:
        raise DraftError([f"{key}: a secret is never stored in the draft; it is entered at Apply" for key in secret])
    unknown = [k for k in (secrets_set or []) if k not in SECRET_KEYS]
    if unknown:
        raise DraftError([f"secrets_set: unknown secret name {k!r}" for k in unknown])
    path = draft_path(root)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    doc = {"format": DRAFT_FORMAT, "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "step": step, "secrets_set": sorted(set(secrets_set or [])), "answers": answers}
    temp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(temp, path)
    return path


def load_draft(root: str) -> Optional[Dict[str, Any]]:
    """The draft, or None when there is none yet. A broken file raises DraftError."""
    path = draft_path(root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise DraftError([f"{path}: not readable ({type(exc).__name__})"]) from None
    if not isinstance(doc, dict) or doc.get("format") != DRAFT_FORMAT or not isinstance(doc.get("answers"), dict):
        raise DraftError([f"{path}: not a format-{DRAFT_FORMAT} draft"])
    return doc


# ======================================================================
# Draft -> site.yaml dict
# ======================================================================

def secrets_needed(answers: Dict[str, Any]) -> List[str]:
    """The secrets Apply must ask for, from the answers."""
    needed = ["db.user", "db.password"]
    mail, transfer = answers.get("mail"), answers.get("transfer")
    if (isinstance(mail, dict) and mail.get("sender", "graph") == "graph") or isinstance(transfer, dict):
        needed += ["graph.tenant_id", "graph.client_id", "graph.client_secret"]
    if isinstance(mail, dict) and mail.get("sender") == "smtp":
        needed.append("mail.smtp.password")
    return needed


def missing_answers(answers: Dict[str, Any]) -> List[str]:
    """What the wizard still has to ask (empty = complete). Values are checked by the loader later."""
    missing = []
    for key in ("site", "database", "cameras", "plc", "machine", "models", "object_sizes"):
        if not answers.get(key):
            missing.append(key)
    site = answers.get("site") or {}
    missing += [f"site.{k}" for k in ("id", "name") if site and not site.get(k)]
    if answers.get("database") and not answers["database"].get("name"):
        missing.append("database.name")
    if answers.get("machine") and not answers["machine"].get("total_wheel_count"):
        missing.append("machine.total_wheel_count")
    mail = answers.get("mail")
    if isinstance(mail, dict):
        missing += [f"mail.{k}" for k in ("sender_address", "project_name", "recipients") if not mail.get(k)]
    transfer = answers.get("transfer")
    if isinstance(transfer, dict) and not transfer.get("drive"):
        missing.append("transfer.drive")
    for i, cam in enumerate(answers.get("cameras") or []):
        for key in ("id", "side", "driver", "serial", "fps", "exposure_us", "rotate", "motion"):
            if cam.get(key) in (None, ""):
                missing.append(f"cameras[{i}].{key}")
        side = cam.get("side")
        if side and not (answers.get("models") or {}).get(side):
            missing.append(f"models.{side}")
        if side and not (answers.get("object_sizes") or {}).get(side):
            missing.append(f"object_sizes.{side}")
    return sorted(set(missing), key=missing.index)


def _side_models(answers: Dict[str, Any], sides: List[str]) -> Tuple[str, Dict[str, str]]:
    """(site model, {side: own model}) from {side: name}: one name = one model for the site."""
    names = {side: answers["models"][side] for side in sides}
    site_model = names[sides[0]]
    return site_model, {side: name for side, name in names.items() if name != site_model}


def _camera(cam: Dict[str, Any]) -> Dict[str, Any]:
    entry = {k: cam[k] for k in ("id", "side", "driver", "serial", "fps", "exposure_us", "rotate")}
    for optional in ("ip", "flir"):
        if cam.get(optional):
            entry[optional] = cam[optional]
    return entry


def site_from_draft(draft: Dict[str, Any], secrets: Dict[str, str], root: Optional[str] = None) -> Dict[str, Any]:
    """A complete site.yaml dict (schema 3) from a finished draft and the secrets typed at Apply.
    Raises DraftError listing missing answers and secrets. Validation of the values is the
    loader's job (site_writer.write_site)."""
    answers = draft.get("answers", draft)                      # a draft doc or just the answers
    problems = missing_answers(answers)
    problems += [f"secret {k} not given" for k in secrets_needed(answers) if not secrets.get(k)]
    if problems:
        raise DraftError(problems)

    cameras = answers["cameras"]
    sides = [cam["side"] for cam in cameras]
    site_model, own_models = _side_models(answers, sides)
    machine = dict(answers["machine"])
    machine.setdefault("expected_cycle_total", machine["total_wheel_count"])   # D-073 D8

    data: Dict[str, Any] = {
        "schema_version": 3,
        "site": {"id": answers["site"]["id"], "name": answers["site"]["name"]},
        "paths": {"root": root or answers.get("root") or "~/INSIGHTZZ"},
        "db": {"host": answers["database"].get("host", "localhost"), "port": answers["database"].get("port", 3306),
               "name": answers["database"]["name"],
               "health_table": answers["database"].get("health_table", "health_status"),
               "user": secrets["db.user"], "password": secrets["db.password"]},
        "mqtt": {"host": (answers.get("mqtt") or {}).get("host", "localhost"),
                 "port": (answers.get("mqtt") or {}).get("port", 1883)},
    }
    if secrets.get("mqtt.user"):
        data["mqtt"].update(user=secrets["mqtt.user"], password=secrets.get("mqtt.password", ""))
    if "graph.tenant_id" in secrets_needed(answers):
        data["graph"] = {k: secrets[f"graph.{k}"] for k in ("tenant_id", "client_id", "client_secret")}

    data["capture"] = {"cameras": [_camera(cam) for cam in cameras]}

    side_blocks = {}
    for cam in cameras:
        block = {"object_sizes": dict(answers["object_sizes"][cam["side"]]), "motion": cam["motion"]}
        if cam["side"] in own_models:
            block["model"] = {"name": own_models[cam["side"]]}               # D-086
        side_blocks[cam["side"]] = block
    data["inspection"] = {"model": {"name": site_model}, "threshold": answers.get("threshold", 0.5),
                          "machine": machine, "sides": side_blocks}

    data["plc"] = dict(answers["plc"])

    mail = answers.get("mail")
    if isinstance(mail, dict):                                    # optional (D-103 step 8)
        block = {"sender": mail.get("sender", "graph"), "sender_address": mail["sender_address"],
                 "project_name": mail["project_name"], "recipients": list(mail["recipients"]),
                 "cc": list(mail.get("cc", []))}
        if block["sender"] == "smtp":
            smtp = mail.get("smtp") or {}
            block["smtp"] = {"host": smtp.get("host", "smtp.gmail.com"), "port": smtp.get("port", 587),
                             "password": secrets["mail.smtp.password"]}
        data["mail"] = block

    transfer = answers.get("transfer")
    if isinstance(transfer, dict):                                # optional (D-103 step 8b, D-105)
        block = {"drive": dict(transfer["drive"])}
        if transfer.get("upload"):
            block["upload"] = {}                                  # all defaults
        data["transfer"] = block
    return data
