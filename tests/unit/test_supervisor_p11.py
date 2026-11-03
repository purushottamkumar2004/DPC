"""Tests for STATUS P11: model management per model name (D-056, D-060, D-061, D-086).

Run from the repo root:  python3 -m unittest tests.unit.test_supervisor_p11 -v

Fake packages in the real format (checked by the real common.config.model_package.verify_package)
and a fake self-test (records its arguments, passes or fails on demand) stand in for the
Detectron model; everything else is real: site.yaml, the renderer's side -> worker mapping,
`current`/`pinned` written with model_package's own functions, the reload channel and a running
`supervisor.py run` for the end-to-end tests.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import unittest

import yaml

from tests.unit.test_supervisor_p5 import SECRETS, SiteYamlTest                      # noqa: F401 (sys.path)
from tests.unit.test_supervisor_v2 import DUMMY, SUPERVISOR_DIR, dummy, supervisor_main
from core.config import ConfigError                                                 # noqa: E402
from core.models import ModelCommandError, ModelManager                              # noqa: E402
from core.process_manager import WorkerStatus                                       # noqa: E402
from core.site_env import SiteEnv                                                   # noqa: E402
from common.config import model_package as mp                                       # noqa: E402

FAKE_SELFTEST = '''
import json, os, sys
args = sys.argv[1:]
log = {log!r}
with open(log, "a") as f:
    f.write(json.dumps(args) + "\\n")
version = args[args.index("--version") + 1]
if os.path.exists({fail_flag!r} + "_" + version):
    print("frame 1: nothing")
    print("SELFTEST FAIL: no 'wheel' found in 5 frame(s); totals: nothing")
    print(json.dumps({{"pass": False, "reason": "no 'wheel' found in 5 frame(s); totals: nothing"}}))
    sys.exit(1)
print("SELFTEST PASS: wheel found")
print(json.dumps({{"pass": True, "reason": "", "totals": {{"wheel": 7}}}}))
'''


class ModelTest(SiteYamlTest):
    MODEL = "dpc_model"

    def setUp(self):
        super().setUp()
        self.root = self.path("INSIGHTZZ")
        self.selftest_log = self.path("selftest_calls.jsonl")
        self.fail_flag = self.path("selftest_fail")
        self.selftest = self.path("fake_selftest.py")
        with open(self.selftest, "w") as f:
            f.write(FAKE_SELFTEST.format(log=self.selftest_log, fail_flag=self.fail_flag))
        for cam in ("CAM1", "CAM2"):
            raw = os.path.join(self.root, "data", "frames", cam, "RAW_IMAGES")
            os.makedirs(raw)
            open(os.path.join(raw, "2026_11_04_10_00_00_000001.jpg"), "w").close()

    # ---- site.yaml, packages, manager ----
    def site(self, right_model=None):
        def edit(data):
            data["inspection"]["model"] = {"name": self.MODEL}
            if right_model:
                data["inspection"]["sides"]["RIGHT"]["model"] = {"name": right_model}
        return self.write_site_yaml(edit=edit)

    def package(self, version, name=None, broken=False, ready=True):
        name = name or self.MODEL
        folder = os.path.join(self.root, "data", "models", name, version)
        os.makedirs(folder)
        files = {"model_final.pth": f"weights of {name} {version}".encode(),
                 "config.yaml": b"MODEL: {}\n",
                 "classes.json": json.dumps({"classes": ["wheel", "bucket_clip"], "label_map": {}}).encode()}
        for file_name, content in files.items():
            with open(os.path.join(folder, file_name), "wb") as f:
                f.write(content)
        manifest = {"name": name, "version": version, "trained_at": "2026-11-01",
                    "files": {n: hashlib.sha256(c).hexdigest() for n, c in files.items()}}
        with open(os.path.join(folder, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        if broken:                                         # corrupt copy: checksum no longer matches
            with open(os.path.join(folder, "model_final.pth"), "ab") as f:
                f.write(b"half a download")
        if ready:
            open(os.path.join(folder, "ready"), "w").close()
        return folder

    def model_root(self, name=None):
        return os.path.join(self.root, "data", "models", name or self.MODEL)

    def manager(self, site_path):
        return ModelManager(SiteEnv(site_path).load(), self.path("run"), self.selftest, sys.executable)

    def install(self, *versions, current=None, name=None):
        for v in versions:
            self.package(v, name=name)
        if current:
            mp.set_current(self.model_root(name), current)

    def selftest_calls(self):
        try:
            with open(self.selftest_log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


# ----------------------------------------------------------------------
class Status(ModelTest):
    def test_status_per_model_with_sides_versions_and_markers(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        self.package("v3", ready=False)
        rows = self.manager(site).status()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["model"], row["sides"], row["workers"]),
                         (self.MODEL, ["LEFT", "RIGHT"], ["inspection_left", "inspection_right"]))
        self.assertEqual((row["current"], row["previous"], row["pinned"]), ("v1", None, None))
        self.assertEqual(row["installed"], [{"version": "v1", "ready": True}, {"version": "v2", "ready": True},
                                            {"version": "v3", "ready": False}])

    def test_per_side_model_shows_two_models(self):
        site = self.site(right_model="dpc_model_right")
        self.install("v1", current="v1")
        self.install("r1", current="r1", name="dpc_model_right")
        rows = {r["model"]: r for r in self.manager(site).status()}
        self.assertEqual(rows[self.MODEL]["workers"], ["inspection_left"])
        self.assertEqual(rows["dpc_model_right"]["workers"], ["inspection_right"])
        self.assertEqual(rows["dpc_model_right"]["current"], "r1")

    def test_broken_marker_is_reported_not_raised(self):
        site = self.site()
        self.install("v1", current="v1")
        with open(os.path.join(self.model_root(), "pinned"), "w") as f:
            f.write("../etc\n")
        row = self.manager(site).status()[0]
        self.assertIsNone(row["pinned"])
        self.assertTrue(any("pinned" in p for p in row["problems"]))


# ----------------------------------------------------------------------
class Switch(ModelTest):
    def test_switch_verifies_selftests_moves_current_and_records_previous(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        result = self.manager(site).switch(self.MODEL, "v2")
        self.assertEqual((result["from"], result["to"], result["workers"]),
                         ("v1", "v2", ["inspection_left", "inspection_right"]))
        self.assertEqual(mp.read_current(self.model_root()), "v2")
        self.assertTrue(os.path.islink(os.path.join(self.model_root(), "current")), "set_current symlink")
        self.assertEqual(self.manager(site).status()[0]["previous"], "v1")
        self.assertTrue(os.path.isdir(os.path.join(self.model_root(), "v1")), "old version kept on disk")
        call = self.selftest_calls()[0]
        self.assertEqual(call[call.index("--model") + 1], self.MODEL)
        self.assertEqual(call[call.index("--version") + 1], "v2")
        self.assertEqual(call[call.index("--frames") + 1],
                         os.path.join(self.root, "data", "frames", "CAM1", "RAW_IMAGES"))
        self.assertEqual(call[call.index("--threshold") + 1], "0.5")
        self.assertEqual(call[call.index("--models-dir") + 1], os.path.join(self.root, "data", "models"))
        self.assertIn("--json", call)

    def test_broken_package_is_refused_before_the_selftest_and_changes_nothing(self):
        site = self.site()
        self.install("v1", current="v1")
        self.package("v2", broken=True)
        with self.assertRaises(ModelCommandError) as ctx:
            self.manager(site).switch(self.MODEL, "v2")
        self.assertIn("checksum does not match", str(ctx.exception))
        self.assertEqual(mp.read_current(self.model_root()), "v1")
        self.assertEqual(self.selftest_calls(), [], "no GPU work for a broken package")
        self.assertFalse(os.path.exists(os.path.join(self.model_root(), "previous")))

    def test_failing_selftest_refuses_with_its_reason(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        open(self.fail_flag + "_v2", "w").close()
        with self.assertRaises(ModelCommandError) as ctx:
            self.manager(site).switch(self.MODEL, "v2")
        self.assertIn("self-test failed", str(ctx.exception))
        self.assertIn("no 'wheel' found", str(ctx.exception))
        self.assertEqual(mp.read_current(self.model_root()), "v1")

    def test_selftest_uses_a_side_that_has_frames(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        cam1 = os.path.join(self.root, "data", "frames", "CAM1", "RAW_IMAGES")
        for n in os.listdir(cam1):
            os.remove(os.path.join(cam1, n))
        self.manager(site).switch(self.MODEL, "v2")
        call = self.selftest_calls()[0]
        self.assertEqual(call[call.index("--frames") + 1], os.path.join(self.root, "data", "frames", "CAM2", "RAW_IMAGES"))

    def test_refusals(self):
        site = self.site()
        self.install("v1", current="v1")
        m = self.manager(site)
        for args, text in (((self.MODEL, "v1"), "already the current"),
                           (("other_model", "v1"), "not used on this site"),
                           ((self.MODEL, "../v1"), "not a version name"),
                           ((self.MODEL, "current"), "not a version name"),
                           ((self.MODEL, "v9"), "version folder not found")):
            with self.assertRaises(ModelCommandError) as ctx:
                m.switch(*args)
            self.assertIn(text, str(ctx.exception))

    def test_first_install_without_current(self):
        site = self.site()
        self.install("v1")
        result = self.manager(site).switch(self.MODEL, "v1")
        self.assertEqual((result["from"], result["to"]), (None, "v1"))
        self.assertFalse(os.path.exists(os.path.join(self.model_root(), "previous")))

    def test_per_side_model_switch_names_only_its_side(self):
        site = self.site(right_model="dpc_model_right")
        self.install("v1", current="v1")
        self.install("r1", "r2", current="r1", name="dpc_model_right")
        result = self.manager(site).switch("dpc_model_right", "r2")
        self.assertEqual(result["workers"], ["inspection_right"])
        call = self.selftest_calls()[0]
        self.assertEqual(call[call.index("--frames") + 1], os.path.join(self.root, "data", "frames", "CAM2", "RAW_IMAGES"))
        self.assertEqual(mp.read_current(self.model_root()), "v1", "the other model is untouched")

    def test_one_model_command_at_a_time(self):
        site = self.site()
        m = self.manager(site)
        with m.lock():
            with self.assertRaises(ModelCommandError) as ctx:
                with self.manager(site).lock():
                    pass
        self.assertIn("another model command", str(ctx.exception))


# ----------------------------------------------------------------------
class RollbackAndPin(ModelTest):
    def test_switch_and_back(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        self.manager(site).switch(self.MODEL, "v2")
        result = self.manager(site).rollback(self.MODEL)
        self.assertEqual((result["from"], result["to"]), ("v2", "v1"))
        self.assertEqual(mp.read_current(self.model_root()), "v1")
        self.assertEqual(self.manager(site).status()[0]["previous"], "v2", "rollback can be undone")
        self.assertEqual(len(self.selftest_calls()), 1, "rollback runs no self-test")
        for v in ("v1", "v2"):
            self.assertTrue(os.path.isdir(os.path.join(self.model_root(), v)))

    def test_rollback_needs_a_valid_previous(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        with self.assertRaises(ModelCommandError) as ctx:
            self.manager(site).rollback(self.MODEL)
        self.assertIn("no previous version", str(ctx.exception))
        self.manager(site).switch(self.MODEL, "v2")
        with open(os.path.join(self.model_root(), "v1", "model_final.pth"), "ab") as f:
            f.write(b"x")                                  # previous got corrupted meanwhile
        with self.assertRaises(ModelCommandError) as ctx:
            self.manager(site).rollback(self.MODEL)
        self.assertIn("checksum", str(ctx.exception))
        self.assertEqual(mp.read_current(self.model_root()), "v2")

    def test_pin_format_and_pinned_blocks_switch_and_rollback(self):
        site = self.site()
        self.install("v1", "v2", "v3", current="v1")
        self.manager(site).switch(self.MODEL, "v2")
        result = self.manager(site).pin(self.MODEL, "v2")
        self.assertEqual((result["version"], result["current"]), ("v2", "v2"))
        self.assertEqual(mp.read_pinned(self.model_root()), "v2", "format read by read_pinned()")
        for call in (lambda m: m.switch(self.MODEL, "v3"), lambda m: m.rollback(self.MODEL)):
            with self.assertRaises(ModelCommandError) as ctx:
                call(self.manager(site))
            self.assertIn("pinned to v2", str(ctx.exception))
        self.assertFalse(self.manager(site).may_switch_automatically(self.MODEL, "install"))
        self.assertEqual(self.manager(site).unpin(self.MODEL)["version"], "v2")
        self.assertIsNone(mp.read_pinned(self.model_root()))
        self.assertTrue(self.manager(site).may_switch_automatically(self.MODEL, "install"))
        self.assertFalse(self.manager(site).may_switch_automatically(self.MODEL, "off"))
        self.manager(site).switch(self.MODEL, "v3")

    def test_pin_refusals(self):
        site = self.site()
        self.install("v1", current="v1")
        with self.assertRaises(ModelCommandError):
            self.manager(site).pin(self.MODEL, "v7")
        with self.assertRaises(ModelCommandError) as ctx:
            self.manager(site).unpin(self.MODEL)
        self.assertIn("not pinned", str(ctx.exception))


# ----------------------------------------------------------------------
class ModelsSection(ModelTest):
    def test_models_section(self):
        cfg = self.config([dummy("a", "")])
        self.assertEqual((cfg.settings.models.auto_update, cfg.settings.models.check_interval_hours), ("off", 6.0))
        cfg = self.config([dummy("a", "")], models={"auto_update": "download", "check_interval_hours": 12})
        self.assertEqual(cfg.settings.models.auto_update, "download")
        path = self.path("bare_off.yaml")                  # `auto_update: off` unquoted, as people write it
        with open(path, "w") as f:
            f.write(f"models:\n  auto_update: off\nlog_dir: {self.path('logs')}\nworkers: []\n")
        bare = supervisor_main.ConfigurationManager(path, SUPERVISOR_DIR)
        bare.load()
        self.assertEqual(bare.settings.models.auto_update, "off")
        for bad in ({"auto_update": "yes"}, {"auto_update": True}, {"check_interval_hours": 0}, {"interval": 3}):
            with self.assertRaises(ConfigError):
                self.config([dummy("a", "")], models=bad)


# ----------------------------------------------------------------------
class SupervisorRestart(ModelTest):
    """The running Supervisor's side: restart only the given workers, high cost waits."""

    def two_sides(self, monitoring=None):
        workers = [dummy(w, "", restart_cost="high") for w in ("inspection_left", "inspection_right")]
        self.build(workers, monitoring)
        self.sup.start()
        self.run_until(lambda: all(w.status == WorkerStatus.RUNNING for w in self.sup.workers.values()),
                       message="not running")
        return {w.id: w.pid for w in self.sup.workers.values()}

    def test_confirm_restarts_only_the_named_side(self):
        pids = self.two_sides()
        report = self.sup.restart_for_new_config(["inspection_right"], True, "model dpc_model_right r2")
        self.assertEqual(report["restarted"], ["inspection_right"])
        self.assertEqual(self.worker("inspection_left").pid, pids["inspection_left"])
        self.assertNotEqual(self.worker("inspection_right").pid, pids["inspection_right"])

    def test_without_confirm_high_cost_waits_and_reload_confirm_restarts_it(self):
        pids = self.two_sides()
        report = self.sup.restart_for_new_config(["inspection_left", "inspection_right"], False, "model dpc_model v2")
        self.assertEqual(report["pending"], ["inspection_left", "inspection_right"])
        self.assertEqual({w.id: w.pid for w in self.sup.workers.values()}, pids)
        self.assertIn("reload --confirm", self.worker("inspection_left").message)
        self.assertTrue(self.worker("inspection_left").config_pending)


# ----------------------------------------------------------------------
class CommandLine(ModelTest):
    """`supervisor.py model ...` as a person runs it; with and without a running Supervisor."""

    def setUp(self):
        super().setUp()
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(30)
        super().tearDown()

    def workers_file(self, right_model=False):
        starts = self.path("starts.txt")
        data = {"mqtt": {"enabled": False}, "runtime": {"root": os.path.join(self.root, "run")},
                "log_dir": os.path.join(self.root, "logs"),
                "monitoring": {"check_interval_seconds": 0.5, "startup_stagger_seconds": 0},
                "workers": [dict(dummy(w, f"--log-start {starts}"), restart_cost="high")
                            for w in ("inspection_left", "inspection_right")]}
        path = self.path("workers.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(data, f)
        return path

    def model(self, *args, site):
        cmd = [sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), "model", *args,
               "-c", self.path("workers.yaml"), "--site-config", site, "--selftest-script", self.selftest]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def starts(self):
        try:
            with open(self.path("starts.txt")) as f:
                return [line.split()[0] for line in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def run_supervisor(self, site):
        self.proc = subprocess.Popen([sys.executable, os.path.join(SUPERVISOR_DIR, "supervisor.py"), "run",
                                      "-c", self.path("workers.yaml"), "--site-config", site],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline and len(self.starts()) < 2:
            time.sleep(0.1)
        time.sleep(1.0)                                   # both RUNNING, pid file written

    def test_status_text_and_json(self):
        site = self.site()
        self.workers_file()
        self.install("v1", "v2", current="v1")
        out = self.model("status", site=site)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("auto_update: off", out.stdout)
        self.assertIn("current: v1", out.stdout)
        data = json.loads(self.model("status", "--json", site=site).stdout)
        self.assertEqual(data["models"][0]["current"], "v1")

    def test_switch_and_back_with_a_running_supervisor_only_its_sides_restart(self):
        site = self.site(right_model="dpc_model_right")
        self.workers_file()
        self.install("v1", current="v1")
        self.install("r1", "r2", current="r1", name="dpc_model_right")
        self.run_supervisor(site)
        self.assertEqual(sorted(self.starts()), ["inspection_left", "inspection_right"])

        out = self.model("switch", "dpc_model_right", "r2", "--confirm", site=site)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("current is now r2 (was r1)", out.stdout)
        self.assertIn("restarted: inspection_right", out.stdout)
        time.sleep(1.0)
        self.assertEqual(self.starts().count("inspection_right"), 2)
        self.assertEqual(self.starts().count("inspection_left"), 1, "D-086: only that side restarts")

        out = self.model("rollback", "dpc_model_right", "--confirm", site=site)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("current is now r1 (was r2)", out.stdout)
        self.assertEqual(mp.read_current(self.model_root("dpc_model_right")), "r1")
        time.sleep(1.0)
        self.assertEqual(self.starts().count("inspection_right"), 3)
        self.assertEqual(self.starts().count("inspection_left"), 1)

    def test_broken_package_is_refused_before_any_restart(self):
        site = self.site()
        self.workers_file()
        self.install("v1", current="v1")
        self.package("v2", broken=True)
        self.run_supervisor(site)
        out = self.model("switch", self.MODEL, "v2", "--confirm", site=site)
        self.assertEqual(out.returncode, 1)
        self.assertIn("Refused, nothing changed", out.stdout)
        self.assertIn("checksum does not match", out.stdout)
        time.sleep(1.0)
        self.assertEqual(sorted(self.starts()), ["inspection_left", "inspection_right"], "nothing restarted")
        self.assertEqual(mp.read_current(self.model_root()), "v1")

    def test_without_a_running_supervisor_current_moves_and_it_says_so(self):
        site = self.site()
        self.workers_file()
        self.install("v1", "v2", current="v1")
        out = self.model("switch", self.MODEL, "v2", site=site)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Supervisor not reached", out.stdout)
        self.assertEqual(mp.read_current(self.model_root()), "v2")

    def test_pin_and_unpin_from_the_command_line(self):
        site = self.site()
        self.workers_file()
        self.install("v1", "v2", current="v1")
        out = self.model("pin", self.MODEL, "v2", site=site)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("pinned to v2", out.stdout)
        self.assertIn("never switched automatically", out.stdout)
        self.assertEqual(self.model("unpin", self.MODEL, site=site).returncode, 0)
        self.assertEqual(self.model("unpin", self.MODEL, site=site).returncode, 1)

    def test_wrong_number_of_arguments(self):
        site = self.site()
        self.workers_file()
        out = self.model("switch", self.MODEL, site=site)
        self.assertEqual(out.returncode, 2)
        self.assertIn("needs 2 argument", out.stderr)

    def test_nothing_secret_in_the_output(self):
        site = self.site()
        self.workers_file()
        self.install("v1", "v2", current="v1")
        text = ""
        for args in (("status",), ("switch", self.MODEL, "v2"), ("rollback", self.MODEL), ("pin", self.MODEL, "v1")):
            out = self.model(*args, site=site)
            text += out.stdout + out.stderr
        for value in SECRETS.values():
            self.assertNotIn(value, text)


# ----------------------------------------------------------------------
class Events(ModelTest):
    """The five events, on dpc/supervisor/event (published by the command, best effort)."""

    def capture(self):
        published = []

        class FakeClient:
            def __init__(self, *a, **k): pass
            def start(self): pass
            def wait_connected(self, t): return True
            def topic(self, *parts): return "/".join(["dpc", *parts])
            def publish(self, topic, payload, retain=False): published.append((topic, payload))
            def stop(self): pass
        return published, FakeClient

    def run_cli(self, *args, site):
        published, fake = self.capture()
        path = self.path("workers.yaml")
        with open(path, "w") as f:
            yaml.safe_dump({"mqtt": {"enabled": True}, "runtime": {"root": os.path.join(self.root, "run")},
                            "log_dir": os.path.join(self.root, "logs"), "workers": [dummy("inspection_left", "")]}, f)
        original = supervisor_main._cli_mqtt
        supervisor_main._cli_mqtt = lambda config, site_env: fake()
        try:
            code = supervisor_main.cmd_model([*args, "-c", path, "--site-config", site,
                                              "--selftest-script", self.selftest])
        finally:
            supervisor_main._cli_mqtt = original
        return code, published

    def test_all_five_events(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        self.package("v3", broken=True)
        seen = []
        for args in (("switch", self.MODEL, "v2"), ("rollback", self.MODEL), ("switch", self.MODEL, "v3"),
                     ("pin", self.MODEL, "v1"), ("unpin", self.MODEL)):
            code, published = self.run_cli(*args, site=site)
            topic, payload = published[-1]
            self.assertEqual(topic, "dpc/supervisor/event")
            seen.append((payload["event"], code))
        self.assertEqual(seen, [("model_switched", 0), ("model_rolled_back", 0), ("model_switch_failed", 1),
                                ("model_pinned", 0), ("model_unpinned", 0)])

    def test_switched_event_payload(self):
        site = self.site()
        self.install("v1", "v2", current="v1")
        _, published = self.run_cli("switch", self.MODEL, "v2", site=site)
        payload = published[-1][1]
        self.assertEqual((payload["model"], payload["from"], payload["to"]), (self.MODEL, "v1", "v2"))
        self.assertEqual(payload["sides"], ["LEFT", "RIGHT"])
        self.assertIn("timestamp", payload)
        self.assertNotIn("selftest", payload)


if __name__ == "__main__":
    unittest.main()
