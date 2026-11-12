"""What each Test / action button runs (the S2-S7 tools and F88-F93, all with --json) and how the
result reads in plain words. Every tool runs as a background job (jobs.py), one at a time.
Secrets go in through env or stdin only, never on the command line.
"""
import datetime
import json
import os
import re

from monitor.setup import env, jobs, vault

BURST_COUNT, BURST_INTERVAL_MS = 20, 250
BROWSE_ROOTS = ("/media", "/mnt")             # + <root>; a USB stick appears under /media/<user>/
MODEL_FILES = (".pth", ".json", ".xml")


def _py(tool, *args):
    return [env.python(), env.tool_path(tool), *[str(a) for a in args]]


# ------------------------------------------------------------------------------- step 2 database
def create_database(answers, serials=()):
    db = answers.get("database") or {}
    cmd = _py("bootstrap", "--db", db.get("name"), "--user", vault.get("db.user") or "", "--root", env.root(), "--json")
    for serial in serials:
        cmd += ["--serial", str(serial)]
    # no admin password: bootstrap uses dpc_setup from config/mysql_setup.cnf (F88)
    jobs.start("database.create", "Creating the database", cmd, env={"DPC_DB_PASS": vault.get("db.password") or ""})


# ------------------------------------------------------------------------------- step 3 cameras
def search_cameras():
    jobs.start("cameras.search", "Searching for cameras", _py("camera_probe", "list", "--json"))


def frames_dir(serial):
    return env.work_dir("frames", re.sub(r"[^\w.-]", "_", str(serial)))


def grab(serial, exposure_us=None):
    out = os.path.join(frames_dir(serial), "grab_" + datetime.datetime.now().strftime("%H%M%S_%f") + ".jpg")
    cmd = _py("camera_probe", "grab", serial, out, "--json") + (["--exposure", exposure_us] if exposure_us else [])
    jobs.start(f"cameras.grab.{serial}", f"Taking an image from camera {serial}", [str(c) for c in cmd])


def burst(serial, exposure_us=None):
    out = os.path.join(frames_dir(serial), "burst_" + datetime.datetime.now().strftime("%H%M%S"))
    cmd = _py("camera_probe", "burst", serial, out, "--count", BURST_COUNT, "--interval-ms", BURST_INTERVAL_MS, "--json")
    if exposure_us:
        cmd += ["--exposure", str(exposure_us)]
    jobs.start(f"cameras.burst.{serial}", f"Recording {BURST_COUNT} frames from camera {serial}", cmd)


def latest_frames(serial):
    """The newest burst folder of this camera, else its grab folder (measure / self-test input)."""
    folder = frames_dir(serial)
    bursts = sorted(d for d in os.listdir(folder) if d.startswith("burst_") and os.path.isdir(os.path.join(folder, d)))
    return os.path.join(folder, bursts[-1]) if bursts else folder


def has_burst(serial):
    return latest_frames(serial) != frames_dir(serial)


def measure(kind, title, serial, model, threshold=0.5):
    cmd = _py("measure", "--frames", latest_frames(serial), "--model", model, "--threshold", threshold,
              "--models-dir", models_dir(), "--count", 40, "--json")
    jobs.start(kind, title, cmd, timeout=1200)


def latest_grab(serial):
    folder = frames_dir(serial)
    grabs = sorted(f for f in os.listdir(folder) if f.startswith("grab_") and f.endswith(".jpg"))
    return os.path.join(folder, grabs[-1]) if grabs else None


# ------------------------------------------------------------------------------- normal mode: pause a capture
_paused = set()


def capture_worker(serial):
    from dpc_ui.ui_config import get_config
    for cam in get_config()["cameras"]:
        if str(cam.get("serial")) == str(serial):
            return cam.get("worker")
    return None


def pause_capture(serial):
    worker = capture_worker(serial)
    if not worker:
        raise ValueError("This camera does not belong to a running capture program.")
    jobs.start(f"cameras.pause.{serial}", f"Pausing {worker} for the test", _py("supervisor", "stop", worker), timeout=120)
    _paused.add(worker)


def paused_workers():
    return sorted(_paused)


def resume_captures():
    """Start every capture this wizard paused (on leaving the step, or the button). Runs inline:
    `supervisor.py start` only writes a request and returns."""
    import subprocess
    started = []
    for worker in sorted(_paused):
        try:
            subprocess.run(_py("supervisor", "start", worker), capture_output=True, text=True, timeout=60)
            started.append(worker)
        except (OSError, subprocess.TimeoutExpired):
            continue
    _paused.difference_update(started)
    return started


# ------------------------------------------------------------------------------- step 4 PLC
def plc_test(action, plc):
    args = {"read": ["--check"], "light": ["--light", "off", "--seconds", "5"], "pulse": ["--pulse"]}[action]
    cmd = _py("plc", *args, "--json", "--type", plc.get("type", "modbus_tcp"), "--ip", plc.get("ip", ""),
              "--port", plc.get("port", 502), "--light-register", plc.get("light_register", ""),
              "--light-on-value", plc.get("light_on_value", 1), "--cleaning-register", plc.get("cleaning_register", ""),
              "--conveyor-status-register", plc.get("conveyor_status_register", ""))
    titles = {"read": "Reading the PLC", "light": "Switching the light off for 5 s", "pulse": "Sending one cleaning pulse"}
    jobs.start(f"plc.{action}", titles[action], cmd, timeout=60)


# ------------------------------------------------------------------------------- step 6 model
def models_dir():
    from dpc_ui.ui_config import get_config
    return os.path.join(get_config().paths["data"], "models")


def browse(path):
    """Folders and model files under the allowed roots: {"path", "parent", "dirs", "files"} or None."""
    roots = [os.path.realpath(r) for r in BROWSE_ROOTS + (env.root(),)]
    real = os.path.realpath(path or BROWSE_ROOTS[0])
    if not any(real == r or real.startswith(r + os.sep) for r in roots) or not os.path.isdir(real):
        return {"path": None, "roots": [r for r in roots if os.path.isdir(r)], "dirs": [], "files": []}
    dirs, files = [], []
    for name in sorted(os.listdir(real)):
        full = os.path.join(real, name)
        if name.startswith("."):
            continue
        if os.path.isdir(full):
            dirs.append(name)
        elif name.lower().endswith(MODEL_FILES):
            files.append({"name": name, "path": full, "size_mb": round(os.path.getsize(full) / 1e6, 1)})
    parent = os.path.dirname(real)
    return {"path": real, "parent": parent if any(parent == r or parent.startswith(r + os.sep) for r in roots) else None,
            "roots": [r for r in roots if os.path.isdir(r)], "dirs": dirs, "files": files}


def classes_option(path):
    """make_model_package option for the class file: COCO dataset, legacy XML or a list / index map."""
    if path.lower().endswith(".xml"):
        return "--classes-from-xml"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "categories" in data:
            return "--classes-from-coco"
    except (OSError, ValueError):
        pass
    return "--classes-file"                       # list, classes.json or index map {"0": name} (D-089)


def next_version(name):
    base = datetime.date.today().strftime("%Y.%m.%d")
    version, n = base, 1
    while os.path.exists(os.path.join(models_dir(), name, version)):
        n += 1
        version = f"{base}.{n}"
    return version


def package(name, weights, classes):
    version = next_version(name)
    cmd = _py("package", "--name", name, "--version", version, "--weights", weights, classes_option(classes), classes,
              "--models-dir", models_dir(), "--json")
    jobs.start(f"model.package.{name}", f"Packaging {name} {version}", cmd, timeout=900)


def selftest(name, version, serial):
    cmd = _py("selftest", "--model", name, "--version", version, "--frames", latest_frames(serial), "--count", 5,
              "--models-dir", models_dir(), "--json")
    jobs.start(f"model.selftest.{name}", f"Testing {name} on the grabbed frames", cmd, timeout=900)


def installed_version(name):
    try:
        return os.path.basename(os.readlink(os.path.join(models_dir(), name, "current")))
    except OSError:
        return None


# ------------------------------------------------------------------------------- step 8 mail, 8b transfer
def test_mail(mail):
    cmd = _py("mail", "--test", "--json", "--sender", mail.get("sender", "graph"), "--from", mail.get("sender_address", ""),
              "--to", ",".join(mail.get("recipients") or []), "--project-name", mail.get("project_name", ""))
    if mail.get("cc"):
        cmd += ["--cc", ",".join(mail["cc"])]
    if mail.get("sender") == "smtp":
        smtp = mail.get("smtp") or {}
        cmd += ["--smtp-host", smtp.get("host", ""), "--smtp-port", str(smtp.get("port", 587))]
    secret_env = {"DPC_MS_TENANT_ID": vault.get("graph.tenant_id") or "", "DPC_MS_CLIENT_ID": vault.get("graph.client_id") or "",
                  "DPC_MS_CLIENT_SECRET": vault.get("graph.client_secret") or "",
                  "DPC_SMTP_PASSWORD": vault.get("mail.smtp.password") or ""}
    jobs.start("mail.test", "Sending one test mail", [str(c) for c in cmd], env=secret_env, timeout=120)


def test_transfer(site_id, transfer):
    drive = transfer.get("drive") or {}
    cmd = _py("transfer", "--check", "--json", "--site-id", site_id, "--graph-stdin")
    cmd += ["--drive-site", drive["site"]] if drive.get("site") else ["--drive-user", drive.get("user", "")]
    if not transfer.get("upload"):
        cmd.append("--no-upload")
    login = json.dumps({k: vault.get(f"graph.{k}") or "" for k in ("tenant_id", "client_id", "client_secret")})
    jobs.start("transfer.test", "Checking the SharePoint permission", cmd, stdin=login, timeout=180)


# ------------------------------------------------------------------------------- plain words
def plain(kind, job):
    """{"level": ok|warning|error, "sentence", "lines": [...]} for a finished job, or None."""
    if not job:
        return None
    r, code = job.get("result"), job.get("exit_code")
    if r is None:
        return {"level": "error", "sentence": f"{job.get('title', 'The test')} did not answer: {job.get('error') or 'no result'}",
                "lines": []}
    base = kind.split(".")[0] + "." + kind.split(".")[1] if "." in kind else kind
    describe = DESCRIBE.get(base)
    out = describe(r, code) if describe else {"level": "ok" if r.get("ok") else "error",
                                              "sentence": r.get("message") or ("Done." if r.get("ok") else "Failed."), "lines": []}
    return out


def _bootstrap(r, code):
    words = {"failed": "FAILED"}                 # the other results are plain already: created, already there, ...
    lines = [f"{s.get('step')} {s.get('item') or ''}: {words.get(s.get('result'), s.get('result'))}" for s in r.get("steps") or []]
    if r.get("error"):
        return {"level": "error", "sentence": r["error"], "lines": lines}
    return {"level": "ok" if r.get("ok") else "error",
            "sentence": "The database is ready." if r.get("ok") else "A database step failed (see below).", "lines": lines}


def _probe_list(r, code):
    cams = r.get("cameras") or []
    lines = [f"{d.get('driver')}: camera software not installed" for d in r.get("drivers") or [] if d.get("sdk_installed") is False]
    if not cams:
        return {"level": "error", "sentence": "No camera found. Check the network cables and that the cameras have power.",
                "lines": lines}
    return {"level": "ok", "sentence": f"{len(cams)} camera{'s' if len(cams) != 1 else ''} found.", "lines": lines}


def _probe_grab(r, code):
    words = {"ok": "Image taken.", "in_use": "The camera is in use by the capture program; pause it for the test first.",
             "not_found": "This camera was not found any more (cable or power?).", "failed": "Taking the image failed."}
    level = "ok" if r.get("ok") else ("warning" if r.get("status") == "in_use" else "error")
    sentence = words.get(r.get("status"), r.get("message") or "")
    if r.get("frames") is not None and r.get("ok"):
        sentence = f"{len(r['frames'])} frames recorded."
    return {"level": level, "sentence": sentence, "lines": [r["message"]] if r.get("message") and not r.get("ok") else []}


def _measure(r, code):
    if code not in (0, None) and r.get("error"):
        return {"level": "error", "sentence": f"Measuring failed: {r['error']}", "lines": []}
    d = r.get("direction") or {}
    words = {"rtl": "right to left", "ltr": "left to right", "unknown": "unknown"}
    lines = [f"{k}: {v}" for k, v in d.items() if k not in ("value",)]
    return {"level": "ok" if d.get("value") in ("rtl", "ltr") else "warning",
            "sentence": f"Direction: {words.get(d.get('value'), d.get('value'))}. "
                        f"Sizes measured on {(r.get('frames') or {}).get('read', '?')} frames.", "lines": lines}


def _plc(r, code):
    words = {"ok": "The PLC answered.", "unreachable": "The PLC does not answer (IP, cable, power?).",
             "bad_request": "The PLC settings are incomplete.", "failed": "The PLC refused the request.",
             "restore_failed": "The light could not be switched back. Check it by hand!"}
    lines = [f"{name}: register {item.get('register')} = {item.get('value')}" for name, item in (r.get("registers") or {}).items()]
    lines += [f"wrote {w.get('register')} = {w.get('value')} ({w.get('reason')}): {'ok' if w.get('written') else 'FAILED'}"
              for w in r.get("writes") or []]
    if "light_is_on" in r:
        lines.append(f"light is on: {'yes' if r['light_is_on'] else 'no'}")
    level = "ok" if r.get("ok") else ("warning" if r.get("status") == "unreachable" else "error")
    return {"level": level, "sentence": words.get(r.get("status"), r.get("message") or ""), "lines": lines}


def _package(r, code):
    if r.get("ok"):
        return {"level": "ok", "sentence": f"Packaged {r.get('name')} version {r.get('version')} "
                                           f"({len(r.get('classes') or [])} classes).", "lines": []}
    return {"level": "error", "sentence": f"Packaging failed: {r.get('error') or r.get('message') or 'see the tool'}",
            "lines": []}


def _selftest(r, code):
    totals = r.get("totals") or {}
    lines = [f"{cls}: {n}" for cls, n in sorted(totals.items(), key=lambda x: -x[1])]
    if r.get("pass"):
        return {"level": "ok", "sentence": f"The model loads and finds wheels on the frames ({sum(totals.values())} detections).",
                "lines": lines}
    return {"level": "error", "sentence": f"Self-test failed: {r.get('reason') or 'no wheel found'}", "lines": lines}


def _mail(r, code):
    words = {"ok": "The test mail was sent. Ask the recipients whether it arrived.",
             "failed": "The mail service refused the mail.", "bad_request": "The mail settings are incomplete.",
             "unreachable": "The mail service cannot be reached (internet?)."}
    return {"level": "ok" if r.get("ok") else "error", "sentence": words.get(r.get("status"), r.get("message") or ""),
            "lines": [r["message"]] if r.get("message") and not r.get("ok") else []}


def _transfer(r, code):
    lines = [f"{s.get('step') or s.get('name')}: {s.get('result') or s.get('status')}"
             + (f" ({s.get('detail')})" if s.get("detail") else "") for s in r.get("steps") or []]
    if r.get("error"):
        return {"level": "error", "sentence": f"The check could not run: {r['error']}", "lines": lines}
    return {"level": "ok" if r.get("ok") else "error",
            "sentence": "SharePoint access works." if r.get("ok") else "SharePoint access does not work yet (see below).",
            "lines": lines}


DESCRIBE = {"database.create": _bootstrap, "cameras.search": _probe_list, "cameras.grab": _probe_grab,
            "cameras.burst": _probe_grab, "cameras.direction": _measure, "sizes.measure": _measure,
            "plc.read": _plc, "plc.light": _plc, "plc.pulse": _plc, "model.package": _package,
            "model.selftest": _selftest, "mail.test": _mail, "transfer.test": _transfer}
