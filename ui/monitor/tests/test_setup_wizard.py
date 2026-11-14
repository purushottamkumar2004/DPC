"""S8 setup wizard (D-103, D-107, D-108): step validation, every tool call against a fake tool that
prints a recorded --json result, draft save/resume, Apply ok and refused, commissioning ticks from
fake heartbeats. The fakes record argv, env and stdin so the tests prove secrets never reach a
command line."""
import json
import os
import stat
import sys
import textwrap

from monitor.setup import env as setup_env
from monitor.setup import jobs, tools, vault
from monitor.tests import helpers
from monitor.tests.test_views import ViewTestBase, write_jpeg

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
STANDARD = "Company-Std-Pass-42"


class WizardBase(ViewTestBase):
    def setUp(self):
        super().setUp()
        self.cfg["paths"]["code"] = REPO                       # common.config and the real loader
        vault.clear()
        jobs.wait()
        jobs.reset()
        self.addCleanup(vault.clear)
        self.addCleanup(setup_env.set_tool_override, {})
        self.addCleanup(tools._paused.clear)
        self.fakes = os.path.join(self.root, "fakes")
        os.makedirs(self.fakes)
        self.write_defaults()

    def write_defaults(self, password=STANDARD):
        with open(os.path.join(self.root, "config", "site_defaults.yaml"), "w") as f:
            f.write(f"db:\n  name_suffix: _db\n  user: dpc_app\n  password: {password}\n")

    def fake(self, name, result, code=0, text_before="", writes_image=False):
        """A fake tool: records the call, optionally writes the image argument, prints `result`."""
        script = os.path.join(self.fakes, f"{name}.py")
        calls = os.path.join(self.fakes, f"{name}.calls")
        with open(script, "w") as f:
            f.write(textwrap.dedent(f'''\
                import json, os, sys
                rec = {{"argv": sys.argv[1:], "stdin": "" if sys.stdin.isatty() else sys.stdin.read(),
                        "env": {{k: v for k, v in os.environ.items() if k.startswith(("DPC_",))}}}}
                with open({calls!r}, "a") as f:
                    f.write(json.dumps(rec) + "\\n")
                if {writes_image!r}:
                    out = [a for a in sys.argv if a.endswith(".jpg")]
                    if out:
                        import shutil
                        shutil.copy({self.live!r}, out[0])
                print({text_before!r})
                print(json.dumps({result!r}))
                sys.exit({code})
            '''))
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        overrides = dict(setup_env._tool_override)
        overrides[name] = script
        setup_env.set_tool_override(overrides)
        return calls

    def calls(self, name):
        path = os.path.join(self.fakes, f"{name}.calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def post(self, url, data=None):
        r = self.client.post(url, data or {})
        jobs.wait()
        return r

    def draft(self):
        with open(os.path.join(self.root, "data", "state", "ui", "setup_draft.json")) as f:
            return json.load(f)

    def set_answers(self, answers, step="review"):
        sys.path.insert(0, REPO)
        from common.config.setup_draft import save_draft
        save_draft(self.root, answers, step=step)

    def full_answers(self):
        return {"site": {"id": "baga", "name": "Baga DPC"},
                "database": {"host": "localhost", "port": 3306, "name": "baga_db", "standard_login": True},
                "cameras": [{"id": "CAM1", "side": "LEFT", "driver": "basler", "serial": "40112233", "fps": 4.0,
                             "exposure_us": 1600, "rotate": 0, "motion": "rtl"},
                            {"id": "CAM2", "side": "RIGHT", "driver": "basler", "serial": "40114455", "fps": 4.0,
                             "exposure_us": 1800, "rotate": 0, "motion": "ltr"}],
                "plc": {"type": "modbus_tcp", "ip": "192.168.1.7", "port": 502, "light_register": 4110,
                        "cleaning_register": 4107, "conveyor_status_register": 4099, "light_on_value": 1},
                "machine": {"total_wheel_count": 164, "expected_cycle_total": 164},
                "models": {"LEFT": "dpc_maskrcnn", "RIGHT": "dpc_maskrcnn"},
                "object_sizes": {s: {"wheel_w": 500, "wheel_h": 480, "bucket_clip_w": 90, "chain_w": 60,
                                     "half_bucket_clip_w": 45} for s in ("LEFT", "RIGHT")},
                "mail": None, "transfer": None}


# ------------------------------------------------------------------------------------- draft
class DraftTests(WizardBase):
    def test_resume_where_the_draft_stopped(self):
        self.assertEqual(self.client.get("/setup/").url, "/setup/site/")
        r = self.client.post("/setup/site/", {"name": "Baga DPC", "id": "", "go": "next"})
        self.assertEqual(r.url, "/setup/database/")
        doc = self.draft()
        self.assertEqual((doc["step"], doc["answers"]["site"]), ("database", {"id": "baga_dpc", "name": "Baga DPC"}))
        self.assertEqual(self.client.get("/setup/").url, "/setup/database/")     # reload / restart: resume
        page = self.client.get("/setup/database/").content.decode()
        self.assertIn("step 2 of 10", page)

    def test_idle_job_panel_says_nothing_runs(self):
        html = self.client.get("/part/setup/job/").content.decode()
        self.assertIn("Nothing is running", html)
        self.assertNotIn("did not answer", html)

    def test_back_and_save(self):
        self.client.post("/setup/site/", {"name": "Baga", "go": "next"})
        self.assertEqual(self.client.post("/setup/database/", {"go": "back"}).url, "/setup/site/")
        self.assertEqual(self.client.post("/setup/site/", {"name": "Baga", "go": "save"}).url, "/setup/site/")

    def test_secrets_never_in_the_draft(self):
        self.client.post("/setup/site/", {"name": "Baga", "go": "next"})
        self.client.post("/setup/database/", {"name": "baga_db", "user": "dpc_app", "password_choice": "custom",
                                              "password": "my-own-pass-1", "go": "next"})
        text = open(os.path.join(self.root, "data", "state", "ui", "setup_draft.json")).read()
        self.assertNotIn("my-own-pass-1", text)
        self.assertNotIn("dpc_app", text)                                         # db.user is a secret too
        self.assertIn('"db.password"', text)                                      # only "set", by name
        self.assertEqual(vault.get("db.password"), "my-own-pass-1")


# ------------------------------------------------------------------------------------- steps 1, 2
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        if sql.startswith("USE") and self.conn.use_error:
            raise self.conn.use_error
        self.last = sql

    def fetchone(self):
        return ("x",) if self.conn.exists else None

    def fetchall(self):
        return [(t,) for t in self.conn.tables]


class FakeConn:
    def __init__(self, exists=True, tables=(), use_error=None):
        self.exists, self.tables, self.use_error, self.sql = exists, list(tables), use_error, []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


class SiteAndDatabaseTests(WizardBase):
    """F96 / D-112: the person creates the database and its login; the step tests and seeds only."""
    FORM = {"host": "localhost", "port": "3306", "name": "baga_db", "user": "dpc_app", "password_choice": "standard"}

    def setUp(self):
        super().setUp()
        from monitor.setup import dbcheck
        self.dbcheck = dbcheck
        self.addCleanup(setattr, dbcheck, "connect", dbcheck._connect)
        self.connects = []

    def use_db(self, conn=None, error=None):
        def connect(host, port, user, password, database=None):
            self.connects.append((host, port, user, password))
            if error:
                raise error
            return conn
        self.dbcheck.connect = connect

    def test_site_validation(self):
        r = self.client.post("/setup/site/", {"name": "", "go": "next"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "The site name is needed.", status_code=400)
        r = self.client.post("/setup/site/", {"name": "X", "id": "Bad Id", "go": "next"})
        self.assertContains(r, "lowercase letters, digits, - or _", status_code=400)

    def test_fields_prefilled_from_site_defaults(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        page = self.client.get("/setup/database/").content.decode()
        for value in ('name="host" value="localhost"', 'name="port" value="3306"', 'name="name" value="baga_db"',
                      'name="user" value="dpc_app"'):
            self.assertIn(value, page)
        self.assertIn("Standard password set", page)
        self.assertNotIn(STANDARD, page)                                          # never shown
        self.assertIn("Test connection", page)
        self.assertIn("Prepare tables", page)
        self.assertNotIn("Create database", page)

    def test_root_allowed_with_one_warning(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        r = self.client.post("/setup/database/", dict(self.FORM, user="root", go="next"))
        self.assertEqual(r.url, "/setup/cameras/")                                # allowed
        self.assertEqual(vault.get("db.user"), "root")
        page = self.client.get("/setup/database/").content.decode()
        self.assertIn("The programs will use root: allowed", page)

    def test_without_site_defaults_it_asks(self):
        os.remove(os.path.join(self.root, "config", "site_defaults.yaml"))
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        self.assertIn("no stored company defaults", self.client.get("/setup/database/").content.decode())
        r = self.client.post("/setup/database/", {"host": "bad host", "port": "99999", "name": "", "user": "", "go": "next"})
        for text in ("Type the MySQL host", "Port must be between 1 and 65535", "Type the database name",
                     "Type the MySQL user", "No standard password is stored"):
            self.assertContains(r, text, status_code=400)

    def test_draft_keeps_the_connection_but_no_login(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        self.client.post("/setup/database/", dict(self.FORM, host="192.168.1.20", port="3307", password_choice="custom",
                                                  password="Own-Pass-77", go="next"))
        db = self.draft()["answers"]["database"]
        self.assertEqual((db["host"], db["port"], db["name"], db["standard_login"]), ("192.168.1.20", 3307, "baga_db", False))
        text = open(os.path.join(self.root, "data", "state", "ui", "setup_draft.json")).read()
        self.assertNotIn("Own-Pass-77", text)
        self.assertNotIn("dpc_app", text)

    def test_connection_ok_seeded_or_empty(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        self.use_db(FakeConn(exists=True, tables=[]))
        self.post("/setup/database/do/test/", self.FORM)
        self.assertEqual(self.connects[0], ("localhost", 3306, "dpc_app", STANDARD))
        page = self.client.get("/setup/database/").content.decode()
        self.assertIn("Connected. The database baga_db exists and is empty: press Prepare tables.", page)
        self.use_db(FakeConn(exists=True, tables=["defects_table", "wheel_counter_table", "health_status", "x"]))
        self.post("/setup/database/do/test/", self.FORM)
        self.assertIn("exists and has its tables (4).", self.client.get("/setup/database/").content.decode())

    def test_missing_database_says_create_it_first(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        self.use_db(FakeConn(exists=False))
        self.post("/setup/database/do/test/", self.FORM)
        page = self.client.get("/setup/database/").content.decode()
        self.assertIn("Create the database baga_db first, for example in MySQL Workbench, then press Test connection again",
                      page)
        for hint in ("install.sh", "sudo", "terminal", "mysql -u"):
            self.assertNotIn(hint, page)

    def test_login_refused_unreachable_and_no_rights(self):
        import pymysql
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        cases = ((pymysql.err.OperationalError(1045, "Access denied"), "MySQL refused the login of user dpc_app"),
                 (pymysql.err.OperationalError(2003, "Can't connect"), "No MySQL answers at localhost:3306"))
        for error, sentence in cases:
            self.use_db(error=error)
            self.post("/setup/database/do/test/", self.FORM)
            self.assertIn(sentence, self.client.get("/setup/database/").content.decode())
        self.use_db(FakeConn(exists=True, use_error=pymysql.err.OperationalError(1044, "Access denied for user")))
        self.post("/setup/database/do/test/", self.FORM)
        self.assertIn("User dpc_app may not use the database baga_db", self.client.get("/setup/database/").content.decode())

    def test_prepare_tables_seeds_only_with_the_password_in_env(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        self.fake("bootstrap", {"ok": True, "database": "baga_db", "steps": [
            {"step": "schema", "item": "baga_db", "result": "created", "detail": ""},
            {"step": "fixed_rows", "item": "wheel_counter_table", "result": "already there", "detail": ""}]})
        self.post("/setup/database/do/prepare/", self.FORM)
        call = self.calls("bootstrap")[0]
        self.assertEqual(call["argv"][:2], ["--seed-only", "--json"])
        self.assertEqual(call["argv"][2:10], ["--host", "localhost", "--port", "3306", "--db", "baga_db", "--user", "dpc_app"])
        self.assertNotIn(STANDARD, " ".join(call["argv"]))
        self.assertEqual(call["env"]["DPC_DB_PASS"], STANDARD)
        self.assertNotIn("DPC_DB_ADMIN_PASS", call["env"])                       # no admin login (D-112)
        page = self.client.get("/setup/database/").content.decode()
        self.assertIn("The tables are ready.", page)
        self.assertIn("schema baga_db: created", page)

    def test_prepare_errors_in_plain_words_never_a_terminal_instruction(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        for error, sentence in (("(1049, \"Unknown database 'baga_db'\")", "Create the database baga_db first"),
                                ("cannot connect: run install/install.sh as root", "No MySQL answers at this host and port"),
                                ("schema failed; see sudo mysql output", "Preparing the tables failed. Check the connection")):
            self.fake("bootstrap", {"ok": False, "database": "baga_db", "error": error, "steps": []}, code=1)
            self.post("/setup/database/do/prepare/", self.FORM)
            page = self.client.get("/setup/database/").content.decode()
            self.assertIn(sentence, page)
            self.assertNotIn("install.sh", page)
            self.assertNotIn("sudo", page)

    def test_prepare_without_f97_says_so(self):
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="database")
        script = os.path.join(self.fakes, "old_bootstrap.py")
        with open(script, "w") as f:
            f.write("import sys\nsys.stderr.write('usage: bootstrap.py\\nbootstrap.py: error: unrecognized arguments: "
                    "--seed-only\\n')\nsys.exit(2)\n")
        setup_env.set_tool_override({"bootstrap": script})
        self.post("/setup/database/do/prepare/", self.FORM)
        self.assertIn("needs the newer db/bootstrap.py (F97, --seed-only)", self.client.get("/setup/database/").content.decode())


# ------------------------------------------------------------------------------------- step 3
class CameraTests(WizardBase):
    LIST = {"cameras": [{"driver": "basler", "serial": "40112233", "model": "a2A1920", "ip": "192.168.1.102", "in_use": False},
                        {"driver": "basler", "serial": "40114455", "model": "a2A1920", "ip": "192.168.1.103", "in_use": False}],
            "drivers": [{"driver": "basler", "sdk_installed": True, "cameras": []},
                        {"driver": "flir", "sdk_installed": False, "cameras": []}]}

    def setUp(self):
        super().setUp()
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="cameras")

    def search(self):
        self.fake("camera_probe", self.LIST)
        self.post("/setup/cameras/do/search/")
        return self.client.get("/setup/cameras/").content.decode()

    def test_search_merges_cameras(self):
        page = self.search()
        self.assertIn("2 cameras found (2 Basler).", page)
        self.assertIn("flir: camera software not installed", page)
        self.assertEqual([c["serial"] for c in self.draft()["answers"]["cameras"]], ["40112233", "40114455"])
        self.assertEqual(self.calls("camera_probe")[0]["argv"], ["list", "--json"])

    def test_grab_shows_the_image(self):
        self.search()
        self.fake("camera_probe", {"ok": True, "status": "ok", "serial": "40112233", "path": "x"}, writes_image=True)
        self.post("/setup/cameras/do/grab/", {"serial": "40112233", "exposure_us_40112233": "1700"})
        argv = self.calls("camera_probe")[-1]["argv"]
        self.assertEqual(argv[:2], ["grab", "40112233"])
        self.assertIn("--exposure", argv)
        page = self.client.get("/setup/cameras/").content.decode()
        img = __import__("re").search(r'src="(/setup/frame/40112233/grab_[\w]+\.jpg)"', page).group(1)
        self.assertEqual(self.client.get(img).status_code, 200)
        self.assertEqual(self.client.get("/setup/frame/40112233/..%2F..%2Fsetup_draft.json").status_code, 404)

    def test_assign_sides_and_require_direction(self):
        self.search()
        self.client.post("/setup/cameras/do/assign/", {"serial": "40112233", "side": "LEFT"})
        self.client.post("/setup/cameras/do/assign/", {"serial": "40114455", "side": "LEFT"})   # moves LEFT
        cams = {c["serial"]: c["side"] for c in self.draft()["answers"]["cameras"]}
        self.assertEqual(cams, {"40112233": None, "40114455": "LEFT"})
        r = self.client.post("/setup/cameras/", {"go": "next"})
        self.assertContains(r, "direction of the LEFT camera is not confirmed", status_code=400)
        self.client.post("/setup/cameras/do/motion/", {"serial": "40114455", "motion": "rtl"})
        r = self.client.post("/setup/cameras/", {"go": "next"})
        self.assertEqual(r.url, "/setup/plc/")
        cam = [c for c in self.draft()["answers"]["cameras"] if c.get("side")][0]
        self.assertEqual((cam["id"], cam["motion"]), ("CAM1", "rtl"))

    def test_limits(self):
        self.search()
        self.client.post("/setup/cameras/do/assign/", {"serial": "40112233", "side": "LEFT"})
        r = self.client.post("/setup/cameras/", {"go": "next", "fps_40112233": "45", "exposure_us_40112233": "5",
                                                 "rotate_40112233": "45", "motion_40112233": "rtl"})
        for text in ("Frame rate of 40112233", "Exposure of 40112233", "Rotate must be 0, 90, 180 or 270"):
            self.assertContains(r, text, status_code=400)

    def test_direction_needs_a_model_then_uses_measure(self):
        self.search()
        self.client.post("/setup/cameras/do/assign/", {"serial": "40112233", "side": "LEFT"})
        self.fake("camera_probe", {"ok": True, "status": "ok", "frames": [{"index": i} for i in range(20)]})
        os.makedirs(os.path.join(tools.frames_dir("40112233"), "burst_120000"))
        r = self.client.post("/setup/cameras/do/direction/", {"serial": "40112233"})
        self.assertContains(r, "runs after the Model step")
        os.makedirs(os.path.join(tools.models_dir(), "dpc_maskrcnn", "2026.11.12"))
        os.symlink("2026.11.12", os.path.join(tools.models_dir(), "dpc_maskrcnn", "current"))
        self.fake("measure", {"frames": {"given": 20, "read": 20}, "sizes": {}, "object_sizes": {},
                              "direction": {"value": "rtl", "reason": "9 of 10 steps move left", "consistency": 0.9}})
        self.post("/setup/cameras/do/direction/", {"serial": "40112233", "model": "dpc_maskrcnn"})
        argv = self.calls("measure")[0]["argv"]
        self.assertIn("--model", argv)
        self.assertNotIn("--side", argv)                        # no rendered file in setup mode
        page = self.client.get("/setup/cameras/").content.decode()
        self.assertIn("Detected: <b>right to left</b>", page)
        self.assertIsNone(self.draft()["answers"]["cameras"][0]["motion"])        # the person confirms

    def test_normal_mode_in_use_offers_pause_and_resumes_on_leaving(self):
        with open(self._mkrun(), "w") as f:
            json.dump({"mode": "normal"}, f)
        self.cfg["cameras"][0]["serial"] = "40112233"
        self.search()
        self.fake("camera_probe", {"ok": False, "status": "in_use", "serial": "40112233", "message": "held by another program"})
        self.post("/setup/cameras/do/grab/", {"serial": "40112233"})
        page = self.client.get("/setup/cameras/").content.decode()
        self.assertIn("Pause this camera for the test", page)
        self.assertIn("This site is already running", page)
        self.fake("supervisor", {"ok": True})
        self.post("/setup/cameras/do/pause/", {"serial": "40112233"})
        self.assertEqual(self.calls("supervisor")[0]["argv"], ["stop", "frame_capture_cam1"])
        self.assertIn("Its inspection side is paused too", self.client.get("/setup/cameras/").content.decode())
        self.client.get("/setup/plc/")                                            # leaving the step
        self.assertEqual(self.calls("supervisor")[-1]["argv"], ["start", "frame_capture_cam1"])
        self.assertEqual(tools.paused_workers(), [])

    def _mkrun(self):
        os.makedirs(os.path.join(self.root, "run"), exist_ok=True)
        return os.path.join(self.root, "run", "mode.json")


class MixedFlirTests(WizardBase):
    """F101: a site with one Basler and one FLIR camera (Hirmi)."""
    LIST = {"cameras": [{"driver": "basler", "serial": "40112233", "model": "a2A1920-51gcBAS", "ip": "192.168.1.102", "in_use": False},
                        {"driver": "flir", "serial": "21234567", "model": "Blackfly S BFS-PGE-31S4C", "ip": "192.168.2.10",
                         "in_use": False}],
            "drivers": [{"driver": "basler", "sdk_installed": True, "cameras": []},
                        {"driver": "flir", "sdk_installed": True, "cameras": []}]}

    def setUp(self):
        super().setUp()
        self.set_answers({"site": {"id": "hirmi", "name": "Hirmi DPC"}}, step="cameras")
        self.fake("camera_probe", self.LIST)
        self.post("/setup/cameras/do/search/")
        self.client.get("/setup/cameras/")             # the reload after the job merges the result into the draft

    def cams(self):
        return {c["serial"]: c for c in self.draft()["answers"]["cameras"]}

    def test_both_drivers_listed_flir_with_defaults(self):
        page = self.client.get("/setup/cameras/").content.decode()
        self.assertIn("2 cameras found (1 Basler, 1 FLIR).", page)
        self.assertNotIn("cannot be set up by the wizard", page)            # the F86 refusal is gone
        self.assertIn('name="flir_interface_21234567" value="GEV Interface 1"', page)
        self.assertIn('name="flir_throughput_21234567" value="19662239"', page)
        self.assertNotIn("flir_interface_40112233", page)                   # Basler: no FLIR fields
        cams = self.cams()
        self.assertEqual(cams["21234567"]["flir"], {"interface": "GEV Interface 1", "throughput_bps": 19662239})
        self.assertNotIn("flir", cams["40112233"])

    def test_grab_and_burst_go_through_the_right_driver(self):
        self.fake("camera_probe", {"ok": True, "status": "ok", "driver": "flir", "serial": "21234567"}, writes_image=True)
        self.post("/setup/cameras/do/grab/", {"serial": "21234567"})
        argv = self.calls("camera_probe")[-1]["argv"]
        self.assertEqual(argv[:2], ["grab", "21234567"])
        self.assertEqual(argv[argv.index("--driver") + 1], "flir")
        self.fake("camera_probe", {"ok": True, "status": "ok", "driver": "flir", "frames": [{"index": 0}]})
        self.post("/setup/cameras/do/burst/", {"serial": "21234567"})
        argv = self.calls("camera_probe")[-1]["argv"]
        self.assertEqual((argv[0], argv[argv.index("--driver") + 1]), ("burst", "flir"))
        self.post("/setup/cameras/do/grab/", {"serial": "40112233"})
        argv = self.calls("camera_probe")[-1]["argv"]
        self.assertEqual(argv[argv.index("--driver") + 1], "basler")

    def test_flir_fields_validated_and_saved(self):
        self.client.post("/setup/cameras/do/assign/", {"serial": "40112233", "side": "LEFT"})
        self.client.post("/setup/cameras/do/assign/", {"serial": "21234567", "side": "RIGHT"})
        form = {"go": "next", "motion_40112233": "rtl", "motion_21234567": "rtl",
                "flir_interface_21234567": "", "flir_throughput_21234567": "fast"}
        r = self.client.post("/setup/cameras/", form)
        self.assertContains(r, "Interface of the FLIR camera 21234567 is needed", status_code=400)
        self.assertContains(r, "Throughput of the FLIR camera 21234567 must be a whole number", status_code=400)
        form.update({"flir_interface_21234567": "GEV Interface 2", "flir_throughput_21234567": "12000000"})
        self.assertEqual(self.client.post("/setup/cameras/", form).url, "/setup/plc/")
        cams = self.cams()
        self.assertEqual(cams["21234567"]["flir"], {"interface": "GEV Interface 2", "throughput_bps": 12000000})
        self.assertEqual((cams["40112233"]["id"], cams["21234567"]["id"]), ("CAM1", "CAM2"))
        self.assertNotIn("flir", cams["40112233"])

    def test_mixed_site_passes_the_loader_and_applies(self):
        answers = self.full_answers()
        answers["site"] = {"id": "hirmi", "name": "Hirmi DPC"}
        answers["database"]["name"] = "hirmi_db"
        answers["cameras"][1].update({"driver": "flir", "serial": "21234567",
                                      "flir": {"interface": "GEV Interface 1", "throughput_bps": 19662239}})
        self.set_answers(answers, step="review")
        page = self.client.get("/setup/review/").content.decode()
        self.assertNotIn("The settings are not complete yet", page)
        self.assertIn("FLIR 21234567 (GEV Interface 1, 19662239 B/s)", page)
        os.makedirs(os.path.join(self.root, "run"), exist_ok=True)
        self.cfg["paths"]["run"] = os.path.join(self.root, "run")
        self.fake("bootstrap", {"ok": True, "steps": []})
        self.post("/setup/apply/", {"confirm": "yes"})
        site = open(os.path.join(self.root, "config", "site.yaml")).read()
        self.assertIn("driver: flir", site)
        self.assertIn("GEV Interface 1", site)
        self.assertIn("19662239", site)
        self.assertTrue(os.path.exists(os.path.join(self.root, "run", "setup_finish")))


# ------------------------------------------------------------------------------------- steps 4, 5
class PlcAndMachineTests(WizardBase):
    def setUp(self):
        super().setUp()
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="plc")

    def test_plc_validation_and_read(self):
        r = self.client.post("/setup/plc/", {"ip": "192.168.1.300", "go": "next"})
        self.assertContains(r, "IPv4 address", status_code=400)
        page = self.client.get("/setup/plc/").content.decode()
        self.assertIn('value="4109"', page)                                       # defaults shown
        self.fake("plc", {"ok": True, "status": "ok", "action": "check", "message": "read",
                          "registers": {"light": {"register": 4109, "value": 1}, "conveyor": {"register": 4098, "value": 1}},
                          "light_on_value": 1, "light_is_on": True})
        self.post("/setup/plc/do/read/", {"ip": "192.168.1.7", "port": "502", "light_register": "4109",
                                          "cleaning_register": "4106", "conveyor_status_register": "4098", "light_on_value": "1"})
        argv = self.calls("plc")[0]["argv"]
        self.assertEqual(argv[:2], ["--check", "--json"])
        self.assertIn("192.168.1.7", argv)
        page = self.client.get("/setup/plc/").content.decode()
        self.assertIn("The PLC answered.", page)
        self.assertIn("light: register 4109 = 1", page)

    def test_light_and_pulse_and_unreachable(self):
        form = {"ip": "192.168.1.7", "light_register": "4109", "cleaning_register": "4106", "conveyor_status_register": "4098"}
        self.fake("plc", {"ok": False, "status": "unreachable", "action": "light", "message": "timeout"}, code=3)
        self.post("/setup/plc/do/light/", form)
        self.assertEqual(self.calls("plc")[0]["argv"][:4], ["--light", "off", "--seconds", "5"])
        self.assertIn("does not answer", self.client.get("/setup/plc/").content.decode())
        self.fake("plc", {"ok": True, "status": "ok", "action": "pulse", "message": "pulse sent"})
        self.post("/setup/plc/do/pulse/", form)
        self.assertEqual(self.calls("plc")[-1]["argv"][0], "--pulse")

    def test_machine(self):
        r = self.client.post("/setup/machine/", {"total_wheel_count": "abc", "go": "next"})
        self.assertContains(r, "must be a whole number", status_code=400)
        self.client.post("/setup/machine/", {"total_wheel_count": "164", "expected_cycle_total": "", "go": "next"})
        self.assertEqual(self.draft()["answers"]["machine"], {"total_wheel_count": 164, "expected_cycle_total": 164})


# ------------------------------------------------------------------------------------- steps 6, 7
class ModelAndSizesTests(WizardBase):
    def setUp(self):
        super().setUp()
        answers = self.full_answers()
        answers.pop("models")
        answers.pop("object_sizes")
        self.set_answers(answers, step="model")
        self.usb = os.path.join("/tmp", f"dpc_usb_{os.getpid()}")
        os.makedirs(self.usb, exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.usb, ignore_errors=True))

    def test_class_file_kinds(self):
        coco, idx, xml = (os.path.join(self.usb, n) for n in ("train.json", "classes.json", "leftConfig.xml"))
        json.dump({"categories": [{"id": 1, "name": "wheel"}]}, open(coco, "w"))
        json.dump({"0": "wheel", "1": "bucket_clip"}, open(idx, "w"))
        open(xml, "w").write("<x/>")
        self.assertEqual([tools.classes_option(p) for p in (coco, idx, xml)],
                         ["--classes-from-coco", "--classes-file", "--classes-from-xml"])

    def test_package_and_selftest(self):
        weights = os.path.join(self.usb, "model_final.pth")
        classes = os.path.join(self.usb, "classes.json")
        open(weights, "wb").write(b"x")
        json.dump(["wheel", "bucket_clip"], open(classes, "w"))
        r = self.client.post("/setup/model/do/browse/", {"path": self.usb})
        self.assertNotContains(r, "model_final.pth")                              # /tmp is not an allowed folder
        self.fake("package", {"ok": True, "name": "dpc_maskrcnn", "version": "2026.11.12",
                              "classes": ["wheel", "bucket_clip"], "label_map": {}})
        self.post("/setup/model/do/package/", {"name": "dpc_maskrcnn", "weights": weights, "classes": classes, "side": "both"})
        argv = self.calls("package")[0]["argv"]
        self.assertIn("--json", argv)
        self.assertIn("--classes-file", argv)
        page = self.client.get("/setup/model/").content.decode()
        self.assertEqual(self.draft()["answers"]["packages"]["dpc_maskrcnn"]["version"], "2026.11.12")
        self.assertIn("2026.11.12", page)
        self.fake("selftest", {"model": "dpc_maskrcnn", "version": "2026.11.12", "pass": True,
                               "totals": {"wheel": 12, "bucket_clip": 30}, "frames": [], "reason": ""},
                  text_before="frame 1: wheel 3")
        self.post("/setup/model/do/selftest/", {"name": "dpc_maskrcnn", "serial": "40112233"})
        page = self.client.get("/setup/model/").content.decode()
        self.assertIn("passed", page)
        self.assertIn("finds wheels on the frames (42 detections)", page)

    def test_sizes_propose_p10_with_median(self):
        answers = self.full_answers()
        answers.pop("object_sizes")
        self.set_answers(answers, step="sizes")
        self.fake("measure", {"frames": {"given": 40, "read": 40}, "sizes": {},
                              "object_sizes": {"wheel_w": {"value": 530, "class": "wheel", "boxes": 80, "p10": 498.6, "p90": 560},
                                               "chain_w": None},
                              "direction": {"value": "unknown"}})
        self.post("/setup/sizes/do/measure/", {"side": "LEFT"})
        page = self.client.get("/setup/sizes/").content.decode()
        self.assertIn('name="LEFT_wheel_w" value="499"', page)                   # p10, rounded to whole pixels
        self.assertIn("median 530 · 80 boxes", page)
        r = self.client.post("/setup/sizes/", {"go": "next", "LEFT_wheel_w": "499"})
        self.assertContains(r, "LEFT wheel_h is needed", status_code=400)


# ------------------------------------------------------------------------------------- steps 8, 8b
class MailAndTransferTests(WizardBase):
    def setUp(self):
        super().setUp()
        self.set_answers({"site": {"id": "baga", "name": "Baga"}}, step="mail")

    def test_no_mail_is_allowed(self):
        self.client.post("/setup/mail/", {"enabled": "no", "go": "next"})
        self.assertIsNone(self.draft()["answers"]["mail"])

    def test_mail_validation_and_test_with_secrets_in_env(self):
        r = self.client.post("/setup/mail/", {"enabled": "yes", "sender_address": "x", "recipients": "a@b.com, nope", "go": "next"})
        self.assertContains(r, "not a mail address: nope", status_code=400)
        self.fake("mail", {"ok": True, "status": "ok", "message": "sent"})
        self.post("/setup/mail/do/test/", {"enabled": "yes", "sender": "graph", "sender_address": "dpc@x.com",
                                           "project_name": "Baga", "recipients": "a@x.com", "graph_tenant_id": "T-1",
                                           "graph_client_id": "C-1", "graph_client_secret": "S-1"})
        call = self.calls("mail")[0]
        self.assertEqual(call["argv"][:4], ["--test", "--json", "--sender", "graph"])
        self.assertNotIn("S-1", " ".join(call["argv"]))
        self.assertEqual(call["env"]["DPC_MS_CLIENT_SECRET"], "S-1")
        page = self.client.get("/setup/mail/").content.decode()
        self.assertIn("The test mail was sent.", page)
        self.assertIn("Microsoft 365 login <span class=\"c-ok\">set</span>", page)
        self.assertNotIn("S-1", page)

    def test_transfer_check_login_on_stdin(self):
        r = self.client.post("/setup/transfer/", {"enabled": "yes", "drive_site": "contoso", "go": "next"})
        self.assertContains(r, "host:/sites/name", status_code=400)
        vault.put("graph.tenant_id", "T-1")
        vault.put("graph.client_id", "C-1")
        vault.put("graph.client_secret", "S-1")
        self.fake("transfer", {"ok": True, "steps": [{"step": "token", "status": "ok", "detail": ""},
                                                      {"step": "drive", "status": "ok", "detail": "found"}], "error": None})
        self.post("/setup/transfer/do/test/", {"enabled": "yes", "drive_site": "contoso.sharepoint.com:/sites/DPC"})
        call = self.calls("transfer")[0]
        self.assertIn("--graph-stdin", call["argv"])
        self.assertIn("--no-upload", call["argv"])
        self.assertNotIn("S-1", " ".join(call["argv"]))
        self.assertEqual(json.loads(call["stdin"])["client_secret"], "S-1")
        self.assertIn("SharePoint access works.", self.client.get("/setup/transfer/").content.decode())


# ------------------------------------------------------------------------------------- review + apply
class ReviewAndApplyTests(WizardBase):
    def setUp(self):
        super().setUp()
        self.set_answers(self.full_answers(), step="review")
        self.run = os.path.join(self.root, "run")
        os.makedirs(self.run, exist_ok=True)
        self.cfg["paths"]["run"] = self.run

    def test_review_runs_the_loader(self):
        page = self.client.get("/setup/review/").content.decode()
        self.assertIn("Database password: <span class=\"c-ok\">set</span>", page)  # standard, refilled
        self.assertNotIn("The settings are not complete yet", page)
        self.assertNotIn(STANDARD, page)
        answers = self.full_answers()
        answers["cameras"][0]["exposure_us"] = 1
        self.set_answers(answers)
        page = self.client.get("/setup/review/").content.decode()
        self.assertIn("The settings are not complete yet", page)
        self.assertIn("exposure_us", page)

    def test_apply_ok(self):
        self.fake("bootstrap", {"ok": True, "steps": [{"step": "health", "item": "40112233", "result": "created"}]})
        r = self.post("/setup/apply/", {"confirm": "yes"})
        self.assertEqual(r.status_code, 200)
        argv = self.calls("bootstrap")[0]["argv"]                                 # F96: seed again with the serials
        self.assertEqual(argv[:2], ["--seed-only", "--json"])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--serial"], ["40112233", "40114455"])
        self.assertEqual(self.calls("bootstrap")[0]["env"]["DPC_DB_PASS"], STANDARD)
        site_yaml = os.path.join(self.root, "config", "site.yaml")
        self.assertEqual(oct(os.stat(site_yaml).st_mode & 0o777), "0o600")
        self.assertIn("baga_db", open(site_yaml).read())
        workers = open(os.path.join(self.root, "config", "workers.yaml")).read()
        self.assertRegex(workers, r"(?s)id: mail.*?enabled: false")                # no mail -> disabled
        request = json.load(open(os.path.join(self.run, "setup_finish")))
        self.assertIn("waiting for the Supervisor", self.client.get("/part/setup/apply/").content.decode().replace("Waiting", "waiting"))
        json.dump({"id": request["id"], "ok": True, "mode": "normal", "problems": []},
                  open(os.path.join(self.run, "setup_finish.result.json"), "w"))
        page = self.client.get("/part/setup/apply/").content.decode()
        self.assertIn("The Supervisor accepted the setup", page)
        self.assertIn('fetch("/setup/mode.json"', page)
        self.assertEqual(vault.names(), [])                                       # secrets only in site.yaml now

    def test_apply_refused_by_the_supervisor(self):
        self.fake("bootstrap", {"ok": True, "steps": []})
        self.post("/setup/apply/", {"confirm": "yes"})
        request = json.load(open(os.path.join(self.run, "setup_finish")))
        json.dump({"id": request["id"], "ok": False, "mode": "setup", "problems": ["workers.yaml: no enabled ui worker"]},
                  open(os.path.join(self.run, "setup_finish.result.json"), "w"))
        page = self.client.get("/part/setup/apply/").content.decode()
        self.assertIn("Not applied", page)
        self.assertIn("workers.yaml: no enabled ui worker", page)

    def test_apply_refused_without_secrets(self):
        os.remove(os.path.join(self.root, "config", "site_defaults.yaml"))
        self.post("/setup/apply/", {"confirm": "yes"})
        page = self.client.get("/part/setup/apply/").content.decode()
        self.assertIn("db.user is not set", page)
        self.assertFalse(os.path.exists(os.path.join(self.root, "config", "site.yaml")))

    def test_apply_refused_when_the_database_is_missing(self):
        self.fake("bootstrap", {"ok": False, "error": "(1049, \"Unknown database 'baga_db'\")", "steps": []}, code=1)
        self.post("/setup/apply/", {"confirm": "yes"})
        page = self.client.get("/part/setup/apply/").content.decode()
        self.assertIn("Create the database baga_db first", page)
        self.assertFalse(os.path.exists(os.path.join(self.root, "config", "site.yaml")))

    def test_old_answer_is_not_mistaken_for_this_one(self):
        json.dump({"id": "old", "ok": True}, open(os.path.join(self.run, "setup_finish.result.json"), "w"))
        self.fake("bootstrap", {"ok": True, "steps": []})
        self.post("/setup/apply/", {"confirm": "yes"})
        self.assertIn("aiting for the Supervisor", self.client.get("/part/setup/apply/").content.decode())


# ------------------------------------------------------------------------------------- setup mode, commissioning
class SetupModeTests(WizardBase):
    def test_setup_mode_opens_the_wizard(self):
        os.makedirs(os.path.join(self.root, "run"), exist_ok=True)
        json.dump({"mode": "setup"}, open(os.path.join(self.root, "run", "mode.json"), "w"))
        self.assertEqual(self.client.get("/").url, "/setup/")
        page = self.client.get("/setup/site/").content.decode()
        self.assertIn('aria-label="Overview"', page)
        self.assertIn("Overview: available after the setup is applied", page)
        self.assertNotIn("This site is already running", page)
        self.assertEqual(self.client.get("/setup/mode.json").json(), {"mode": "setup"})


class CommissioningTests(WizardBase):
    def test_ticks_from_heartbeats(self):
        page = self.client.get("/setup/commissioning/").content.decode()
        self.assertIn("LEFT: first plate seen", page)
        self.assertNotIn("✓</span> LEFT: first plate seen", page)
        self.cache.handle("dpc/worker/inspection_left/health", {"worker_id": "inspection_left", "plates_seen": 2,
                                                                "last_cycle_wheels": 190})
        self.cache.handle("dpc/worker/frame_capture_cam1/health", {"worker_id": "frame_capture_cam1", "fps": 3.9,
                                                                   "camera_connected": True})
        page = self.client.get("/part/setup/commissioning/").content.decode()
        self.assertIn("✓</span> LEFT: first plate seen", page)
        self.assertIn("✓</span> LEFT: one revolution counted 190 wheels", page)
        self.assertIn("✓</span> LEFT camera delivers about 4 images per second", page)
        self.assertIn("✓</span> PLC connected and the light on", page)
        self.assertIn("RIGHT: first plate seen", page)
        self.cache.handle("dpc/worker/inspection_left/health", {"worker_id": "inspection_left", "plates_seen": 2,
                                                                "last_cycle_wheels": 188})
        page = self.client.get("/part/setup/commissioning/").content.decode()
        self.assertIn("✓</span> LEFT: one revolution counted 190 wheels", page)   # a tick stays once reached
        self.assertIn("last cycle: 188 wheels", page)

    def test_learn_request_and_report(self):
        os.makedirs(os.path.join(self.root, "run"), exist_ok=True)
        self.cfg["paths"]["run"] = os.path.join(self.root, "run")
        self.client.post("/setup/commissioning/do/learn/", {"worker": "inspection_left"})
        self.assertTrue(os.path.exists(os.path.join(self.root, "run", "inspection_left.roi_learn")))
        self.assertEqual(self.client.post("/setup/commissioning/do/learn/", {"worker": "../../x"}).status_code, 404)
        self.client.post("/setup/commissioning/do/report/")
        reports = [f for f in os.listdir(self.cfg.paths["state"]) if f.startswith("commissioning_2")]
        self.assertEqual(sorted(f.rsplit(".", 1)[1] for f in reports), ["html", "json"])
        name = [f for f in reports if f.endswith(".html")][0][:-5]
        r = self.client.get(f"/setup/report/{name}.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Commissioning report", b"".join(r.streaming_content))

    def test_transfer_progress_tick(self):
        os.makedirs(os.path.join(self.root, "config", "rendered"), exist_ok=True)
        open(os.path.join(self.root, "config", "rendered", "transfer.json"), "w").write("{}")
        self.cache.handle("dpc/worker/transfer/health", {"worker_id": "transfer", "upload_cycle": {
            "state": "uploading", "phase": "uploading", "bytes_done": 50, "bytes_total": 200},
            "model_update": {"state": "idle"}, "last_model_check_at": "2026-11-12T10:00:00+05:30"})
        page = self.client.get("/part/setup/commissioning/").content.decode()
        self.assertIn("✓</span> Model check done", page)
        self.assertIn("uploading · 25 %", page)
