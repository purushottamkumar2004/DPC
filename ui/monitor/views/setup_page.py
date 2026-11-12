"""Setup page (D-103): the wizard, Apply and the commissioning checklist.

  /setup/                     -> the step the draft stopped at (resume after a reload)
  /setup/<step>/              GET show, POST validate + save (Back / Next)
  /setup/<step>/do/<action>/  POST one button (a background job, or an instant change)
  /part/setup/job/            the job panel (polled while a job runs)
  /setup/apply/  /part/setup/apply/  /setup/mode.json
  /setup/commissioning/       checklist; /setup/commissioning/do/<action>/; /setup/report/<name>.html
"""
import os

from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from monitor.setup import apply as apply_mod
from monitor.setup import commissioning, env, jobs, steps, tools, vault
from monitor.views.common import gather

STEP_TEMPLATES = {k: f"monitor/setup/step_{k}.html" for k in steps.STEP_KEYS}


class Blank(str):
    """An empty, falsy value whose every lookup is Blank again: a missing answer in a template."""

    def __getitem__(self, key):
        return Blank()


class SafeDict(dict):
    def __missing__(self, key):
        if key in ("items", "keys", "values", "get"):    # let Django fall back to the dict method
            raise KeyError(key)
        return Blank()


def safe(value):
    """The draft for the templates: missing keys and None become Blank, so a filter argument such as
    `default:answers.site.name` resolves on a fresh draft (Django refuses unresolvable arguments)."""
    if isinstance(value, dict):
        return SafeDict({k: safe(v) for k, v in value.items()})
    if isinstance(value, list):
        return [safe(v) for v in value]
    return Blank() if value is None else value


def _base(step, draft, **extra):
    context = {"active": "setup", "step": step, "steps": steps.STEPS, "progress": steps.progress(step),
               "answers": safe(draft["answers"]), "draft": draft, "mode": env.mode(), "job": jobs.snapshot(),
               "title": dict((k, t) for k, t, _ in steps.STEPS)[step],
               "explain": dict((k, e) for k, _, e in steps.STEPS)[step]}
    context.update(extra)
    return context


def _leave_cameras(step):
    """Captures paused for a camera test start again when the person leaves the cameras step."""
    if step != "cameras" and tools.paused_workers():
        tools.resume_captures()


# ------------------------------------------------------------------------------- results into the draft
def _merge_results(step, answers):
    """Finished tool results that change answers (camera search, direction, packaging)."""
    changed = False
    if step in ("cameras", "model"):
        found = jobs.last("cameras.search")
        if found and isinstance(found.get("result"), dict):
            known = {c["serial"] for c in answers.get("cameras") or []}
            for cam in found["result"].get("cameras") or []:
                if str(cam.get("serial")) not in known:
                    answers.setdefault("cameras", []).append(
                        {"serial": str(cam["serial"]), "driver": cam.get("driver") or "basler", "ip": cam.get("ip"),
                         "model": cam.get("model"), "side": None, "fps": 4.0, "exposure_us": 1500, "rotate": 0,
                         "motion": None})
                    changed = True
        for cam in answers.get("cameras") or []:
            job = jobs.last(f"cameras.direction.{cam['serial']}")
            result = (job or {}).get("result") or {}
            detected = (result.get("direction") or {}) if isinstance(result, dict) else {}
            if detected and cam.get("detected", {}).get("at") != job.get("finished_at"):
                cam["detected"] = {"value": detected.get("value"), "consistency": detected.get("consistency"),
                                   "reason": detected.get("reason"), "at": job.get("finished_at")}
                changed = True
    if step == "model":
        packages = answers.setdefault("packages", {})
        for name in set((answers.get("models") or {}).values()) | set(packages):
            job = jobs.last(f"model.package.{name}")
            result = (job or {}).get("result") or {}
            if isinstance(result, dict) and result.get("ok") and packages.get(name, {}).get("version") != result.get("version"):
                packages[name] = {"version": result.get("version"), "selftest": None}
                changed = True
            test = jobs.last(f"model.selftest.{name}")
            t = (test or {}).get("result") or {}
            if isinstance(t, dict) and name in packages and t.get("version") == packages[name].get("version") \
                    and packages[name].get("selftest") != bool(t.get("pass")):
                packages[name]["selftest"] = bool(t.get("pass"))
                changed = True
    if step == "sizes":
        proposals = answers.setdefault("size_proposals", {})
        for side in steps.SIDES:
            job = jobs.last(f"sizes.measure.{side}")
            result = (job or {}).get("result") or {}
            if isinstance(result, dict) and result.get("object_sizes") and proposals.get(side, {}).get("_at") != job.get("finished_at"):
                proposals[side] = {k: v for k, v in result["object_sizes"].items() if v}
                proposals[side]["_at"] = job.get("finished_at")
                changed = True
    return changed


def _camera_by_serial(answers, serial):
    for cam in answers.get("cameras") or []:
        if cam["serial"] == str(serial):
            return cam
    raise Http404("unknown camera")


# ------------------------------------------------------------------------------- pages
@require_GET
def start(request):
    draft = steps.load()
    return redirect(f"/setup/{draft['step'] if draft['step'] in steps.STEP_KEYS else 'site'}/")


def step_page(request, step):
    if step not in steps.STEP_KEYS:
        raise Http404("unknown step")
    _leave_cameras(step)
    draft = steps.load()
    answers = draft["answers"]
    if request.method == "POST":
        answers, errors = steps.apply_step(step, request.POST, answers)
        direction = request.POST.get("go", "next")
        if errors and direction == "next":
            return render(request, STEP_TEMPLATES[step], _base(step, draft, errors=errors, form=request.POST,
                                                              **_step_extra(step, answers)), status=400)
        index = steps.STEP_KEYS.index(step)
        target = steps.STEP_KEYS[max(0, index - 1)] if direction == "back" else \
            steps.STEP_KEYS[min(len(steps.STEP_KEYS) - 1, index + 1)] if direction == "next" else step
        steps.save(answers, target)
        return redirect(f"/setup/{target}/")
    if _merge_results(step, answers):
        steps.save(answers, step)
        draft = steps.load()
    return render(request, STEP_TEMPLATES[step], _base(step, draft, errors={}, form={}, **_step_extra(step, answers)))


def _step_extra(step, answers):
    extra = {}
    if step == "database":
        extra["db_defaults"] = steps.database_defaults(answers)
        extra["user_value"] = vault.get("db.user") or extra["db_defaults"]["user"]
        extra["result"] = tools.plain("database.create", jobs.last("database.create"))
    elif step == "cameras":
        extra["cams"] = [dict(c, grab=os.path.basename(tools.latest_grab(c["serial"]) or ""),
                              burst=tools.has_burst(c["serial"]),
                              result=tools.plain("cameras.grab", jobs.last(f"cameras.grab.{c['serial']}")),
                              burst_result=tools.plain("cameras.burst", jobs.last(f"cameras.burst.{c['serial']}")),
                              direction=tools.plain("cameras.direction", jobs.last(f"cameras.direction.{c['serial']}")))
                         for c in answers.get("cameras") or []]
        extra["search"] = tools.plain("cameras.search", jobs.last("cameras.search"))
        extra["paused"] = tools.paused_workers()
        extra["model_ready"] = _any_model(answers)
    elif step == "plc":
        extra["plc"] = safe(dict(steps.PLC_DEFAULTS, **(answers.get("plc") or {})))
        extra["results"] = {a: tools.plain(f"plc.{a}", jobs.last(f"plc.{a}")) for a in ("read", "light", "pulse")}
    elif step == "model":
        extra["sides"] = [c for c in answers.get("cameras") or [] if c.get("side")]
        extra["browse"] = tools.browse(None)
        extra["packages"] = answers.get("packages") or {}
        extra["results"] = {name: {"package": tools.plain("model.package", jobs.last(f"model.package.{name}")),
                                   "selftest": tools.plain("model.selftest", jobs.last(f"model.selftest.{name}"))}
                            for name in set((answers.get("models") or {}).values()) | set(answers.get("packages") or {})}
    elif step == "sizes":
        extra["sides"] = [c for c in answers.get("cameras") or [] if c.get("side")]
        extra["keys"] = steps.OBJECT_SIZE_KEYS
        extra["results"] = {s: tools.plain("sizes.measure", jobs.last(f"sizes.measure.{s}")) for s in steps.SIDES}
        extra["size_rows"] = {}
        for cam in extra["sides"]:
            side = cam["side"]
            proposal = (answers.get("size_proposals") or {}).get(side) or {}
            saved = (answers.get("object_sizes") or {}).get(side) or {}
            rows = []
            for key in steps.OBJECT_SIZE_KEYS:
                p = proposal.get(key) if isinstance(proposal.get(key), dict) else {}
                rows.append({"key": key, "name": f"{side}_{key}", "median": p.get("value"), "boxes": p.get("boxes"),
                             "value": saved.get(key) if saved.get(key) and not p else
                             (int(round(p["p10"])) if p.get("p10") is not None else saved.get(key))})
            extra["size_rows"][side] = rows
    elif step == "mail":
        extra["graph_set"] = all(vault.get(f"graph.{k}") for k in ("tenant_id", "client_id", "client_secret"))
        extra["smtp_set"] = bool(vault.get("mail.smtp.password"))
        extra["result"] = tools.plain("mail.test", jobs.last("mail.test"))
    elif step == "transfer":
        extra["graph_set"] = all(vault.get(f"graph.{k}") for k in ("tenant_id", "client_id", "client_secret"))
        extra["result"] = tools.plain("transfer.test", jobs.last("transfer.test"))
    elif step == "review":
        extra["secrets"] = steps.secrets_status(answers)
        extra["warnings"] = steps.warnings(answers)
        extra["problems"] = steps.loader_check(answers)
        extra["apply"] = jobs.last("apply")
    return extra


def _any_model(answers):
    names = set((answers.get("models") or {}).values()) | set(answers.get("packages") or {})
    return sorted(n for n in names if tools.installed_version(n))


# ------------------------------------------------------------------------------- buttons
@require_POST
def action(request, step, name):
    draft = steps.load()
    answers = draft["answers"]
    p = request.POST
    try:
        if step == "database" and name == "create":
            answers, errors = steps.apply_step("database", p, answers)
            if errors:
                return _panel(request, errors=errors)
            steps.save(answers, "database")
            serials = [c["serial"] for c in answers.get("cameras") or [] if c.get("side")]
            tools.create_database(answers, serials)
        elif step == "cameras":
            serial = p.get("serial", "")
            if name == "search":
                tools.search_cameras()
            elif name in ("grab", "burst"):
                cam = _camera_by_serial(answers, serial)
                exposure = p.get("exposure_us") or cam.get("exposure_us")
                cam["exposure_us"] = int(float(exposure)) if exposure else cam.get("exposure_us")
                steps.save(answers, "cameras")
                (tools.grab if name == "grab" else tools.burst)(serial, cam["exposure_us"])
            elif name == "assign":
                side = p.get("side")
                for cam in answers.get("cameras") or []:
                    if cam["serial"] == serial:
                        cam["side"] = side if side in steps.SIDES else None
                    elif cam.get("side") == side:
                        cam["side"] = None                    # one camera per side
                steps.save(answers, "cameras")
                return redirect("/setup/cameras/")
            elif name == "motion":
                cam = _camera_by_serial(answers, serial)
                cam["motion"] = p.get("motion") if p.get("motion") in ("rtl", "ltr") else None
                steps.save(answers, "cameras")
                return redirect(request.POST.get("back") or "/setup/cameras/")
            elif name == "direction":
                cam = _camera_by_serial(answers, serial)
                model = p.get("model") or next(iter(_any_model(answers)), None)
                if not model:
                    return _panel(request, errors={"direction": "The direction is detected with the model: it runs "
                                                                "after the Model step (the recorded frames are kept)."})
                tools.measure(f"cameras.direction.{serial}", f"Detecting the direction of camera {serial}", serial, model)
            elif name == "pause":
                tools.pause_capture(serial)
            elif name == "resume":
                tools.resume_captures()
                return redirect("/setup/cameras/")
            else:
                raise Http404("unknown action")
        elif step == "plc":
            answers, errors = steps.apply_step("plc", p, answers)
            if errors:
                return _panel(request, errors=errors)
            steps.save(answers, "plc")
            tools.plc_test(name, answers["plc"])
        elif step == "model":
            if name == "package":
                model = (p.get("name") or "").strip()
                weights, classes = p.get("weights", ""), p.get("classes", "")
                if not model or not weights.endswith(".pth") or not classes:
                    return _panel(request, errors={"package": "Choose the .pth weights, the class file and a model name."})
                sides = steps.SIDES if p.get("side") == "both" else [p.get("side")]
                answers.setdefault("models", {}).update({s: model for s in sides if s in steps.SIDES})
                steps.save(answers, "model")
                tools.package(model, weights, classes)
            elif name == "selftest":
                model = p.get("name", "")
                version = (answers.get("packages") or {}).get(model, {}).get("version") or tools.installed_version(model)
                if not version:
                    return _panel(request, errors={"selftest": "Package the model first."})
                tools.selftest(model, version, p.get("serial", ""))
            elif name == "browse":
                return render(request, "monitor/setup/browse.html", {"b": tools.browse(p.get("path")),
                                                                     "target": p.get("target", "")})
            else:
                raise Http404("unknown action")
        elif step == "sizes" and name == "measure":
            side = p.get("side")
            cam = next((c for c in answers.get("cameras") or [] if c.get("side") == side), None)
            model = (answers.get("models") or {}).get(side)
            if not cam or not model:
                return _panel(request, errors={"measure": "Finish the Cameras and Model steps first."})
            tools.measure(f"sizes.measure.{side}", f"Measuring object sizes on {side}", cam["serial"], model)
        elif step == "mail" and name == "test":
            answers, errors = steps.apply_step("mail", p, answers)
            if errors or not answers.get("mail"):
                return _panel(request, errors=errors or {"enabled": "Switch mail on to test it."})
            steps.save(answers, "mail")
            tools.test_mail(answers["mail"])
        elif step == "transfer" and name == "test":
            answers, errors = steps.apply_step("transfer", p, answers)
            if errors or not answers.get("transfer"):
                return _panel(request, errors=errors or {"enabled": "Switch the transfer on to test it."})
            steps.save(answers, "transfer")
            tools.test_transfer(answers["site"]["id"], answers["transfer"])
        else:
            raise Http404("unknown action")
    except jobs.Busy as busy:
        return _panel(request, errors={"busy": f"Please wait: {busy} is still running."})
    except ValueError as e:
        return _panel(request, errors={"action": str(e)})
    return _panel(request)


@csrf_exempt                    # navigator.sendBeacon on leaving the page; localhost only (D-095)
@require_POST
def resume_cameras(request):
    return JsonResponse({"started": tools.resume_captures()})


def _panel(request, errors=None):
    snap = jobs.snapshot()
    kind = snap.get("kind") or ""
    plain = None if snap.get("running") or not kind else tools.plain(kind, snap)    # no job yet: nothing to say
    return render(request, "monitor/setup/job_panel.html", {"job": snap, "errors": errors or {}, "plain": plain})


@require_GET
def job_part(request):
    return _panel(request)


@require_GET
def frame(request, serial, name):
    folder = tools.frames_dir(serial)
    path = os.path.realpath(os.path.join(folder, name))
    if not path.startswith(os.path.realpath(folder) + os.sep) or not path.endswith(".jpg") or not os.path.isfile(path):
        raise Http404("no such image")
    return FileResponse(open(path, "rb"), content_type="image/jpeg")


# ------------------------------------------------------------------------------- apply
@require_POST
def apply_start(request):
    if request.POST.get("confirm") != "yes":
        return redirect("/setup/review/")
    answers = steps.load()["answers"]
    for key in ("db.user", "db.password", "graph.tenant_id", "graph.client_id", "graph.client_secret", "mail.smtp.password"):
        if request.POST.get(key):
            vault.put(key, request.POST[key])

    def job():
        try:
            return apply_mod.run(answers)
        except apply_mod.ApplyRefused as e:
            return {"refused": e.problems}
    try:
        jobs.start("apply", "Applying the setup", func=job, timeout=900)
    except jobs.Busy as busy:
        return render(request, "monitor/setup/apply_panel.html", {"busy": str(busy)})
    return render(request, "monitor/setup/apply_panel.html", {"state": "running"})


@require_GET
def apply_part(request):
    snap = jobs.snapshot()
    if snap.get("kind") == "apply" and snap.get("running"):
        return render(request, "monitor/setup/apply_panel.html", {"state": "running", "job": snap})
    last = jobs.last("apply") or {}
    result = last.get("result") or {}
    if last.get("error"):
        return render(request, "monitor/setup/apply_panel.html", {"state": "refused", "problems": [last["error"]]})
    if result.get("refused"):
        return render(request, "monitor/setup/apply_panel.html", {"state": "refused", "problems": result["refused"]})
    answer = apply_mod.finish_result(result.get("id")) if result.get("id") else None
    if answer is None:
        return render(request, "monitor/setup/apply_panel.html", {"state": "waiting", "result": result})
    if answer.get("ok"):
        vault.clear()                                        # the secrets now live in site.yaml only
        return render(request, "monitor/setup/apply_panel.html", {"state": "ok", "result": result})
    return render(request, "monitor/setup/apply_panel.html", {"state": "refused", "problems": answer.get("problems") or []})


@require_GET
def mode_json(request):
    return JsonResponse({"mode": env.mode()})


# ------------------------------------------------------------------------------- commissioning
def _commission_context():
    state = gather()
    cfg = state["cfg"]
    items = commissioning.checklist(state["snapshot"], cfg)
    items += commissioning.later_items(cfg, tools.plain("mail.test", jobs.last("commission.mail")),
                                       tools.plain("transfer.test", jobs.last("commission.transfer_check")),
                                       commissioning.transfer_progress(state["snapshot"]))
    return {"active": "setup", "items": items,
            "roi": commissioning.roi_states(cfg), "transfer": commissioning.transfer_configured(cfg),
            "progress_blocks": commissioning.transfer_progress(state["snapshot"]),
            "mail_configured": os.path.exists(os.path.join(cfg.paths["config"], "rendered", "mail.json")),
            "tests": {"mail": tools.plain("mail.test", jobs.last("commission.mail")),
                      "transfer": tools.plain("transfer.test", jobs.last("commission.transfer_check"))},
            "roi_job": jobs.last("commission.roi_mode"), "job": jobs.snapshot(), "mode": env.mode(),
            "reports": sorted((f[:-5] for f in os.listdir(cfg.paths["state"]) if f.startswith("commissioning_2")
                               and f.endswith(".html")), reverse=True) if os.path.isdir(cfg.paths["state"]) else []}


@require_GET
def commissioning_page(request):
    return render(request, "monitor/setup/commissioning.html", _commission_context())


@require_GET
def commissioning_part(request):
    return render(request, "monitor/setup/commissioning_body.html", _commission_context())


@require_POST
def commissioning_action(request, name):
    cfg = gather()["cfg"]
    try:
        if name == "learn":
            worker = request.POST.get("worker", "")
            if worker not in [s["inspection_worker"] for s in cfg["sides"]]:
                raise Http404("unknown inspection")
            commissioning.request_learning(cfg, worker)
        elif name == "roi_mode":
            commissioning.set_auto_roi_mode(request.POST.get("mode", ""))
        elif name == "mail":
            commissioning.test_mail(cfg)
        elif name == "transfer_check":
            commissioning.check_transfer(cfg)
        elif name in ("model_update", "upload_cycle", "cancel"):
            commissioning.transfer_request(cfg, name, request.POST.get("what", ""))
        elif name == "report":
            context = _commission_context()
            saved = commissioning.save_report(cfg, context["items"], context["roi"],
                                              {k: v for k, v in context["tests"].items() if v})
            commissioning.remember("report", True, saved)
        else:
            raise Http404("unknown action")
    except (jobs.Busy, ValueError) as e:
        commissioning.remember(f"error_{name}", False, str(e))
    return redirect("/setup/commissioning/")


@require_GET
def report(request, name):
    cfg = gather()["cfg"]
    if not name.startswith("commissioning_2") or "/" in name:
        raise Http404("no such report")
    path = os.path.join(cfg.paths["state"], name + ".html")
    if not os.path.isfile(path):
        raise Http404("no such report")
    return FileResponse(open(path, "rb"), content_type="text/html; charset=utf-8")
