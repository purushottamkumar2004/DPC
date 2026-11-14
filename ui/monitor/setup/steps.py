"""The wizard's steps (D-103 phase 1), its draft and each step's validation.

Validation uses the same limits as the loader (common/config/site_config.py); the Review step then
runs the loader itself (site_from_draft + write_site(dry_run=True)), so nothing the loader would
refuse reaches Apply. Answers are saved to the draft (common/config/setup_draft.py,
data/state/ui/setup_draft.json) after every step; secrets go to the vault (memory) only.
"""
import ipaddress
import re

from monitor.setup import env, vault

STEPS = [
    ("site", "Site", "The name of this site. Everything else is named after it."),
    ("database", "Database", "Where inspection stores the defects. The database is created here."),
    ("cameras", "Cameras", "Find the two cameras, say which one is LEFT and which RIGHT, and check the image."),
    ("plc", "PLC", "The PLC switches the conveyor light and the camera cleaning."),
    ("machine", "Machine", "How many wheels one revolution of the conveyor has."),
    ("model", "Model", "The trained model that finds wheels, clips and bolts."),
    ("sizes", "Object sizes", "The smallest sizes a wheel, clip or chain may have, measured on this site."),
    ("mail", "Mail", "Optional: who gets a mail when a defect is found."),
    ("transfer", "Data transfer", "Optional: upload training data and fetch new models from SharePoint."),
    ("review", "Review", "Check everything once more, then apply it."),
]
STEP_KEYS = [k for k, _, _ in STEPS]
SIDES = ("LEFT", "RIGHT")
SITE_ID_RE = re.compile(r"^[a-z0-9_-]+$")                 # site_config._SITE_ID_RE
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")         # MySQL identifier without quoting
DB_USER_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROTATIONS = (0, 90, 180, 270)
OBJECT_SIZE_KEYS = ("wheel_w", "wheel_h", "bucket_clip_w", "chain_w", "half_bucket_clip_w")
# FLIR GigE cameras (Spinnaker) need two more settings; defaults from config/examples/site.example.yaml
# and CONTRACTS 13 (the values of the existing FLIR sites)
FLIR_DEFAULTS = {"interface": "GEV Interface 1", "throughput_bps": 19662239}
PLC_DEFAULTS = {"type": "modbus_tcp", "port": 502, "light_register": 4109, "cleaning_register": 4106,
                "conveyor_status_register": 4098, "light_on_value": 1}


# ------------------------------------------------------------------------------- draft
def load():
    """{"answers", "step", "secrets_set"} (empty answers when there is no draft yet)."""
    env.import_common()
    from common.config.setup_draft import DraftError, load_draft
    try:
        doc = load_draft(env.root())
    except DraftError:
        doc = None
    doc = doc or {}
    return {"answers": doc.get("answers") or {}, "step": doc.get("step") or "site",
            "secrets_set": doc.get("secrets_set") or [], "updated_at": doc.get("updated_at")}


def save(answers, step):
    env.import_common()
    from common.config.setup_draft import SECRET_KEYS, save_draft
    names = [k for k in vault.names() if k in SECRET_KEYS]
    save_draft(env.root(), answers, step=step, secrets_set=names)


def progress(step):
    index = STEP_KEYS.index(step) if step in STEP_KEYS else 0
    return {"index": index + 1, "total": len(STEPS), "percent": round(100 * (index + 1) / len(STEPS))}


# ------------------------------------------------------------------------------- helpers
def _int(form, key, errors, label, low=None, high=None, default=None, required=True):
    raw = (form.get(key) or "").strip()
    if raw == "":
        if default is not None:
            return default
        if required:
            errors[key] = f"{label} is needed."
        return None
    try:
        value = int(raw)
    except ValueError:
        errors[key] = f"{label} must be a whole number."
        return None
    if (low is not None and value < low) or (high is not None and value > high):
        errors[key] = f"{label} must be between {low} and {high}."
        return None
    return value


def _float(form, key, errors, label, low, high, low_exclusive=False):
    raw = (form.get(key) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        errors[key] = f"{label} must be a number."
        return None
    if value < low or value > high or (low_exclusive and value == low):
        errors[key] = f"{label} must be {'above' if low_exclusive else 'at least'} {low:g} and at most {high:g}."
        return None
    return value


def _emails(text, errors, key, label, required):
    items = [x.strip() for x in re.split(r"[,;\s]+", text or "") if x.strip()]
    bad = [x for x in items if not EMAIL_RE.match(x)]
    if bad:
        errors[key] = f"{label}: not a mail address: {', '.join(bad)}."
    elif required and not items:
        errors[key] = f"{label}: at least one address is needed."
    return items


def derive_site_id(name):
    text = re.sub(r"[^a-z0-9_-]+", "_", (name or "").strip().lower())
    return re.sub(r"_+", "_", text).strip("_-")[:40]


# ------------------------------------------------------------------------------- validators
def validate_site(form, answers):
    errors = {}
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "The site name is needed."
    elif len(name) > 80:
        errors["name"] = "Use at most 80 characters."
    site_id = (form.get("id") or "").strip() or derive_site_id(name)
    if not site_id:
        errors.setdefault("id", "The site id is needed (lowercase letters, digits, - or _).")
    elif not SITE_ID_RE.match(site_id):
        errors["id"] = "Use lowercase letters, digits, - or _ only."
    return {"site": {"id": site_id, "name": name}}, errors


HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def database_defaults(answers):
    """Pre-fill (D-108, D-112): host localhost, port 3306, <site.id><name_suffix>, the user and the
    standard password from site_defaults.yaml. Empty values when the installer wrote no defaults."""
    d = env.site_defaults()
    site_id = (answers.get("site") or {}).get("id", "")
    return {"host": "localhost", "port": 3306,
            "name": (site_id + d["name_suffix"]) if site_id and d.get("name_suffix") else "",
            "user": d.get("user", ""), "has_standard": bool(d.get("password")), "defaults": bool(d)}


def validate_database(form, answers):
    """The connection the person created themselves (D-112): the wizard never creates a database or a
    user. root is allowed (the template shows the D-050 warning)."""
    errors = {}
    defaults = database_defaults(answers)
    host = (form.get("host") or "").strip() or defaults["host"]
    if not HOST_RE.match(host):
        errors["host"] = "Type the MySQL host, e.g. localhost or 192.168.1.20."
    port = _int(form, "port", errors, "Port", 1, 65535, default=defaults["port"])
    name = (form.get("name") or "").strip() or defaults["name"]
    if not DB_NAME_RE.match(name or ""):
        errors["name"] = "Type the database name: letters, digits and _ only (at most 64)."
    user = (form.get("user") or "").strip() or vault.get("db.user") or defaults["user"]
    if not DB_USER_RE.match(user or ""):
        errors["user"] = "Type the MySQL user: letters, digits and _ only."
    standard = form.get("password_choice", "standard") == "standard" and defaults["has_standard"]
    password = form.get("password") or ""
    if not standard and not password and not vault.get("db.password"):
        errors["password"] = ("Type the password of this user." if defaults["has_standard"] else
                              "No standard password is stored on this machine (site_defaults.yaml); type the password.")
    if not errors:
        vault.put("db.user", user)
        if standard:
            vault.put("db.password", env.site_defaults().get("password"))
        elif password:
            vault.put("db.password", password)
    part = {"host": host, "port": port, "name": name, "standard_login": bool(standard)}
    return {"database": part}, errors


def connection(answers):
    """(host, port, database, user, password) for the tools; password from the vault (memory)."""
    refill_standard(answers)
    db = answers.get("database") or {}
    return (db.get("host") or "localhost", int(db.get("port") or 3306), db.get("name") or "",
            vault.get("db.user") or "", vault.get("db.password") or "")


def validate_cameras(form, answers):
    """The cameras step builds answers["cameras"] with its buttons; Next checks them."""
    errors = {}
    cameras = list(answers.get("cameras") or [])
    sides = [c.get("side") for c in cameras]
    if not cameras:
        errors["cameras"] = "Search for the cameras and choose LEFT and RIGHT."
    if sorted(s for s in sides if s) != sorted(set(s for s in sides if s)):
        errors["cameras"] = "Each side can have only one camera."
    for cam in cameras:
        serial = cam.get("serial")
        for key, label, low, high in (("fps", "Frame rate", 0, 30), ("exposure_us", "Exposure", 10, 1000000)):
            value = form.get(f"{key}_{serial}", cam.get(key))
            try:
                value = float(value) if key == "fps" else int(float(value))
                if value <= low and key == "fps" or value < low or value > high:
                    raise ValueError
                cam[key] = value
            except (TypeError, ValueError):
                errors[f"{key}_{serial}"] = f"{label} of {serial} must be {'above' if key == 'fps' else 'at least'} {low} and at most {high}."
        rotate = form.get(f"rotate_{serial}", cam.get("rotate", 0))
        try:
            cam["rotate"] = int(rotate)
            if cam["rotate"] not in ROTATIONS:
                raise ValueError
        except (TypeError, ValueError):
            errors[f"rotate_{serial}"] = "Rotate must be 0, 90, 180 or 270."
        motion = form.get(f"motion_{serial}", cam.get("motion"))
        if motion in ("rtl", "ltr"):
            cam["motion"] = motion
        if cam.get("side") and cam.get("motion") not in ("rtl", "ltr"):
            errors[f"motion_{serial}"] = (f"The direction of the {cam['side']} camera is not confirmed yet "
                                          "(detect it, or choose it).")
        if cam.get("driver") == "flir":
            current = cam.get("flir") or dict(FLIR_DEFAULTS)
            interface = (form.get(f"flir_interface_{serial}", current.get("interface")) or "").strip()
            if not interface:
                errors[f"flir_interface_{serial}"] = f"Interface of the FLIR camera {serial} is needed (default: GEV Interface 1)."
            raw = form.get(f"flir_throughput_{serial}", current.get("throughput_bps"))
            try:
                throughput = int(str(raw).strip())
                if throughput < 1:
                    raise ValueError
            except (TypeError, ValueError):
                throughput = None
                errors[f"flir_throughput_{serial}"] = (f"Throughput of the FLIR camera {serial} must be a whole number of "
                                                       "bytes per second (default 19662239).")
            cam["flir"] = {"interface": interface, "throughput_bps": throughput}
        else:
            cam.pop("flir", None)                      # the loader allows a flir block only for driver flir
    if not errors and len([c for c in cameras if c.get("side")]) == 0:
        errors["cameras"] = "Choose which camera is LEFT and which RIGHT."
    chosen = sorted([c for c in cameras if c.get("side")], key=lambda c: SIDES.index(c["side"]))
    for i, cam in enumerate(chosen):
        cam["id"] = f"CAM{i + 1}"                       # CAM1 = LEFT, CAM2 = RIGHT (as Tadipatri)
    return {"cameras": chosen if not errors else cameras}, errors


def validate_plc(form, answers):
    errors = {}
    ip = (form.get("ip") or "").strip()
    try:
        if ipaddress.ip_address(ip).version != 4:
            raise ValueError
    except ValueError:
        errors["ip"] = "Type the PLC's IPv4 address, e.g. 192.168.1.5."
    part = {"type": "modbus_tcp", "ip": ip,
            "port": _int(form, "port", errors, "Port", 1, 65535, default=502),
            "light_register": _int(form, "light_register", errors, "Light register", 0, 65535),
            "cleaning_register": _int(form, "cleaning_register", errors, "Cleaning register", 0, 65535),
            "conveyor_status_register": _int(form, "conveyor_status_register", errors, "Conveyor status register", 0, 65535),
            "light_on_value": _int(form, "light_on_value", errors, "Light on value", 0, 1, default=1)}
    return {"plc": part}, errors


def validate_machine(form, answers):
    errors = {}
    total = _int(form, "total_wheel_count", errors, "Wheels per revolution", 1, 10000)
    expected = _int(form, "expected_cycle_total", errors, "Expected cycle total", 1, 10000,
                    default=total if total else None)
    return {"machine": {"total_wheel_count": total, "expected_cycle_total": expected}}, errors


def validate_model(form, answers):
    errors = {}
    sides = [c["side"] for c in answers.get("cameras") or [] if c.get("side")] or list(SIDES)
    models = dict(answers.get("models") or {})
    if form.get("model_mode") == "one" and form.get("model_name"):
        models = {s: form["model_name"].strip() for s in sides}
    for side in sides:
        name = (form.get(f"model_{side}") or models.get(side) or "").strip()
        if not re.match(r"^[A-Za-z0-9_.-]+$", name or ""):
            errors[f"model_{side}"] = f"Package a model for {side} first (or name an installed one)."
        else:
            models[side] = name
    return {"models": models}, errors


def validate_sizes(form, answers):
    errors = {}
    sides = [c["side"] for c in answers.get("cameras") or [] if c.get("side")] or list(SIDES)
    sizes = {}
    for side in sides:
        sizes[side] = {}
        for key in OBJECT_SIZE_KEYS:
            value = _int(form, f"{side}_{key}", errors, f"{side} {key}", 1, 5000)
            sizes[side][key] = value if value is not None else (answers.get("object_sizes") or {}).get(side, {}).get(key)
    return {"object_sizes": sizes}, errors


def _graph_keys(form):
    for key in ("tenant_id", "client_id", "client_secret"):
        if form.get(f"graph_{key}"):
            vault.put(f"graph.{key}", form[f"graph_{key}"].strip())


def validate_mail(form, answers):
    errors = {}
    if form.get("enabled") != "yes":
        return {"mail": None}, errors
    sender = form.get("sender", "graph")
    if sender not in ("graph", "smtp"):
        errors["sender"] = "Choose Microsoft 365 (Graph) or SMTP."
    address = (form.get("sender_address") or "").strip()
    if not EMAIL_RE.match(address):
        errors["sender_address"] = "Type the sender's mail address."
    project = (form.get("project_name") or "").strip()
    if not project:
        errors["project_name"] = "Type the name shown in the mail subject."
    recipients = _emails(form.get("recipients"), errors, "recipients", "Recipients", True)
    cc = _emails(form.get("cc"), errors, "cc", "Cc", False)
    part = {"sender": sender, "sender_address": address, "project_name": project, "recipients": recipients, "cc": cc}
    if sender == "graph":
        _graph_keys(form)
    else:
        part["smtp"] = {"host": (form.get("smtp_host") or "smtp.gmail.com").strip(),
                        "port": _int(form, "smtp_port", errors, "SMTP port", 1, 65535, default=587)}
        if form.get("smtp_password"):
            vault.put("mail.smtp.password", form["smtp_password"])
    return {"mail": part}, errors


def validate_transfer(form, answers):
    errors = {}
    if form.get("enabled") != "yes":
        return {"transfer": None}, errors
    site = (form.get("drive_site") or "").strip()
    user = (form.get("drive_user") or "").strip()
    if bool(site) == bool(user):
        errors["drive_site"] = "Give either the SharePoint site (host:/sites/name) or a user's OneDrive address."
    elif site and ":/" not in site:
        errors["drive_site"] = "Write it as host:/sites/name, e.g. contoso.sharepoint.com:/sites/DPC."
    elif user and not EMAIL_RE.match(user):
        errors["drive_site"] = "The OneDrive user must be a mail address."
    _graph_keys(form)
    drive = {"site": site} if site else {"user": user}
    return {"transfer": {"drive": drive, "upload": form.get("upload") == "on"}}, errors


VALIDATORS = {"site": validate_site, "database": validate_database, "cameras": validate_cameras,
              "plc": validate_plc, "machine": validate_machine, "model": validate_model, "sizes": validate_sizes,
              "mail": validate_mail, "transfer": validate_transfer}


def apply_step(step, form, answers):
    """Validate one step; returns (answers updated with this step, errors)."""
    if step not in VALIDATORS:
        return answers, {}
    part, errors = VALIDATORS[step](form, answers)
    updated = dict(answers)
    updated.update(part)
    return updated, errors


# ------------------------------------------------------------------------------- review
def secrets_status(answers):
    """[(key, plain name, set?)] for the secrets Apply needs."""
    env.import_common()
    from common.config.setup_draft import secrets_needed
    names = {"db.user": "Database program user", "db.password": "Database password",
             "graph.tenant_id": "Microsoft 365 tenant id", "graph.client_id": "Microsoft 365 app (client) id",
             "graph.client_secret": "Microsoft 365 app secret", "mail.smtp.password": "SMTP password",
             "mqtt.user": "MQTT user", "mqtt.password": "MQTT password"}
    refill_standard(answers)
    return [(k, names.get(k, k), bool(vault.get(k))) for k in secrets_needed(answers)]


def refill_standard(answers):
    """After a UI restart the vault is empty: the D-108 standard login comes back from
    site_defaults.yaml when the database step chose it."""
    if (answers.get("database") or {}).get("standard_login"):
        d = env.site_defaults()
        if d.get("password") and not vault.get("db.password"):
            vault.put("db.password", d["password"])
        if d.get("user") and not vault.get("db.user"):
            vault.put("db.user", d["user"])


def warnings(answers):
    """Plain warnings for values left at a default that rarely fits a new site."""
    out = []
    plc = answers.get("plc") or {}
    if plc and all(plc.get(k) == PLC_DEFAULTS[k] for k in ("light_register", "cleaning_register", "conveyor_status_register")):
        out.append("The PLC registers are the Tadipatri defaults; check them against this site's PLC program.")
    machine = answers.get("machine") or {}
    if machine.get("total_wheel_count") in (156, 170, 190):
        out.append(f"{machine['total_wheel_count']} wheels per revolution is the value of another site; count this conveyor.")
    for cam in answers.get("cameras") or []:
        if cam.get("exposure_us") in (1500, 2000):
            out.append(f"{cam.get('side')} camera exposure is a default ({cam['exposure_us']} µs); check the image.")
    if not answers.get("mail"):
        out.append("No mail: nobody is told about defects by mail (they are still recorded).")
    return out


def loader_check(answers):
    """The real loader on the finished draft (nothing written): [] or the problems in its words."""
    env.import_common()
    from common.config.setup_draft import DraftError, site_from_draft
    from common.config.site_config import SiteConfigError
    from common.config.site_writer import write_site
    refill_standard(answers)
    secrets = vault.all_values()
    try:
        data = site_from_draft({"answers": answers}, secrets, root=env.root())
        write_site(data, path=_site_yaml(), dry_run=True)
        return []
    except DraftError as e:
        return list(e.problems)
    except SiteConfigError as e:
        return [str(p) for p in (getattr(e, "problems", None) or [str(e)])]


def _site_yaml():
    from dpc_ui.ui_config import get_config
    return get_config().paths["site_yaml"]
