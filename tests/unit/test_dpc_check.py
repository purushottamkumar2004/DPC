"""Tests for scripts/dpc_check.py (STATUS item 5). No camera, DB or broker needed:
enumeration, pymysql and sockets are replaced by fakes.

Run from the repo root:
    python3 -m unittest tests.unit.test_dpc_check -v
"""

import importlib.util
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from contextlib import redirect_stdout
from unittest import mock

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model_fixture import make_package  # noqa: E402

_spec = importlib.util.spec_from_file_location("dpc_check", os.path.join(REPO_ROOT, "scripts", "dpc_check.py"))
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)

EXAMPLE_YAML = os.path.join(REPO_ROOT, "config", "examples", "site.example.yaml")
DB_PASSWORD = "s3cret-db-pass"
DB_USER = "dpc_app_user"
SERIALS = ["00000001", "00000002"]
DiskUsage = namedtuple("DiskUsage", "total used free")


class FakeMySQLError(Exception):
    pass


def fake_pymysql(error=None):
    """A stand-in pymysql module. `error` = exception raised by connect()."""
    module = types.ModuleType("pymysql")

    def connect(**kwargs):
        if error is not None:
            raise error
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.execute.return_value = 1
        return conn

    module.connect = mock.MagicMock(side_effect=connect)
    return module


class Site(unittest.TestCase):
    """A temp site: layout created, site.yaml (chmod 600) outside git, healthy fakes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            self.data = yaml.safe_load(f)
        self.data["paths"]["root"] = self.root
        self.data["db"]["user"] = DB_USER
        self.data["db"]["password"] = DB_PASSWORD
        os.makedirs(self.root)
        cfg_path = self.write_config()
        self.cfg = dc.load_site_config(cfg_path)
        for d in dc.expected_dirs(self.cfg):
            os.makedirs(d, exist_ok=True)
        self.models_root = os.path.join(self.root, "data", "models")
        make_package(self.models_root, "example_model", "2026.10.1")

        self.pymysql = fake_pymysql()
        self.patches = [
            mock.patch.dict(sys.modules, {"pymysql": self.pymysql}),
            mock.patch.object(dc.socket, "create_connection", return_value=mock.MagicMock()),
            mock.patch.object(dc, "disk_used_percent", return_value=40.0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def write_config(self, mode=0o600):
        path = os.path.join(self.root, "config", "site.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, sort_keys=False)
        os.chmod(path, mode)
        self.path = path
        return path

    def run_all(self, serials=None):
        serials = SERIALS if serials is None else serials
        results = dc.run_checks(self.path, enumerate_serials=lambda: list(serials))
        return {r.name: r for r in results}, dc.exit_code(results)

    def run_cli(self, serials=None):
        serials = SERIALS if serials is None else serials
        out = io.StringIO()
        with mock.patch.object(dc, "enumerate_basler_serials", lambda: list(serials)), redirect_stdout(out):
            code = dc.main([self.path])
        return code, out.getvalue()


# ----------------------------------------------------------------------
class AllGood(Site):
    def test_everything_passes(self):                                # done when (a)
        results, code = self.run_all()
        self.assertEqual(code, dc.EXIT_PASS, results)
        self.assertEqual([r.status for r in results.values()], [dc.PASS] * 8)

    def test_one_line_per_check_and_a_summary(self):
        code, out = self.run_cli()
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 9)
        self.assertEqual([line.split()[1] for line in lines[:8]],
                         ["site.yaml", "git", "layout", "disk", "cameras", "database", "mqtt", "model"])
        self.assertTrue(lines[-1].startswith("RESULT PASS (exit 0)"))

    def test_db_login_uses_site_yaml_values(self):
        self.run_all()
        kwargs = self.pymysql.connect.call_args.kwargs
        self.assertEqual((kwargs["user"], kwargs["password"], kwargs["database"]),
                         (DB_USER, DB_PASSWORD, "example_db"))
        self.assertLessEqual(kwargs["connect_timeout"], 4)


class Failures(Site):                                                # done when (b)
    def test_chmod_644(self):
        self.write_config(mode=0o644)
        results, code = self.run_all()
        self.assertEqual(code, dc.EXIT_FAIL)
        self.assertEqual(results["site.yaml"].status, dc.FAIL)
        self.assertIn("chmod 600", results["site.yaml"].detail)
        self.assertEqual({r.status for name, r in results.items() if name != "site.yaml"}, {dc.SKIP})

    def test_wrong_serial(self):
        results, code = self.run_all(serials=["00000001", "99999999"])
        self.assertEqual(code, dc.EXIT_FAIL)
        detail = results["cameras"].detail
        self.assertIn("CAM2 serial 00000002 not found", detail)
        self.assertIn("also connected, not configured: 99999999", detail)

    def test_unplugged_camera(self):
        results, code = self.run_all(serials=["00000001"])
        self.assertEqual(results["cameras"].status, dc.FAIL)
        self.assertIn("CAM2 serial 00000002 not found (found: 00000001)", results["cameras"].detail)

    def test_no_camera_at_all(self):
        results, _ = self.run_all(serials=[])
        self.assertIn("(found: none)", results["cameras"].detail)

    def test_enumeration_errors(self):
        def broken():
            raise dc.EnumerationError("pypylon is not installed for this Python")
        results = {r.name: r for r in dc.run_checks(self.path, enumerate_serials=broken)}
        self.assertEqual(results["cameras"].status, dc.FAIL)
        self.assertIn("pypylon is not installed", results["cameras"].detail)

    def test_missing_folder(self):
        shutil.rmtree(self.cfg.paths.frame_dir("CAM2", "INF"))
        results, code = self.run_all()
        self.assertEqual(results["layout"].status, dc.FAIL)
        self.assertIn("data/frames/CAM2/INF", results["layout"].detail)
        self.assertIn("setup_dpc_layout.sh", results["layout"].detail)

    def test_frame_folders_on_two_filesystems(self):
        real_stat = os.stat

        def fake_stat(path, *a, **k):
            st = real_stat(path, *a, **k)
            if path.endswith(os.path.join("CAM1", "live")):
                return os.stat_result((st.st_mode, st.st_ino, 999) + tuple(st)[3:])
            return st
        with mock.patch.object(dc.os, "stat", fake_stat):
            results, _ = self.run_all()
        self.assertEqual(results["layout"].status, dc.FAIL)
        self.assertIn("frames/CAM1", results["layout"].detail)

    def test_db_errors_by_code_never_the_message(self):
        cases = {
            1045: "access denied (check db.user / db.password)",
            1049: "unknown database example_db",
            2003: "cannot reach MySQL at localhost:3306",
            1234: "MySQL error 1234",
        }
        for code, expected in cases.items():
            message = f"Access denied for user '{DB_USER}'@'localhost' password {DB_PASSWORD}"
            sys.modules["pymysql"] = fake_pymysql(FakeMySQLError(code, message))
            results, exit_ = self.run_all()
            self.assertEqual(exit_, dc.EXIT_FAIL)
            self.assertIn(expected, results["database"].detail)
            self.assertNotIn(DB_USER, results["database"].detail)
            self.assertNotIn(DB_PASSWORD, results["database"].detail)

    def test_pymysql_missing(self):
        with mock.patch.dict(sys.modules, {"pymysql": None}):
            results, _ = self.run_all()
        self.assertEqual(results["database"].status, dc.FAIL)
        self.assertIn("pymysql is not installed", results["database"].detail)

    def test_disk_thresholds(self):
        for used, status in ((74.9, dc.PASS), (75.0, dc.WARN), (89.9, dc.WARN), (90.0, dc.FAIL)):
            with mock.patch.object(dc, "disk_used_percent", return_value=used):
                results, _ = self.run_all()
            self.assertEqual(results["disk"].status, status, used)

    def test_site_yaml_inside_git(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", self.root], check=True)
        results, code = self.run_all()
        self.assertEqual(results["git"].status, dc.FAIL)
        self.assertEqual(code, dc.EXIT_FAIL)
        with open(os.path.join(self.root, ".gitignore"), "w") as f:
            f.write("config/site.yaml\n")
        results, _ = self.run_all()
        self.assertEqual(results["git"].status, dc.WARN)

    def test_a_crashing_check_does_not_hide_the_others(self):
        with mock.patch.object(dc, "disk_used_percent", side_effect=RuntimeError(DB_PASSWORD)):
            results, code = self.run_all()
        self.assertEqual(results["disk"].detail, "check crashed: RuntimeError")
        self.assertEqual(results["mqtt"].status, dc.PASS)
        self.assertEqual(code, dc.EXIT_FAIL)


class Warnings(Site):
    def test_root_db_user_is_a_warning(self):                       # D-050
        self.data["db"]["user"] = "root"
        self.write_config()
        results, code = self.run_all()
        self.assertEqual(code, dc.EXIT_WARN)
        self.assertEqual(results["site.yaml"].status, dc.WARN)
        self.assertIn("db.user is root", results["site.yaml"].detail)
        self.assertEqual(results["database"].status, dc.PASS)

    def test_mqtt_unreachable_is_only_a_warning(self):              # D-034
        with mock.patch.object(dc.socket, "create_connection", side_effect=ConnectionRefusedError()):
            results, code = self.run_all()
        self.assertEqual(results["mqtt"].status, dc.WARN)
        self.assertEqual(code, dc.EXIT_WARN)
        with mock.patch.object(dc.socket, "create_connection", side_effect=socket.timeout()):
            results, _ = self.run_all()
        self.assertIn("timed out", results["mqtt"].detail)

    def test_flir_camera_is_not_checked(self):
        self.data["capture"]["cameras"][1]["driver"] = "flir"
        self.data["capture"]["cameras"][1]["flir"] = {"interface": "GEV Interface 1", "throughput_bps": 1}
        self.write_config()
        results, code = self.run_all(serials=["00000001"])
        self.assertEqual(results["cameras"].status, dc.WARN)
        self.assertIn("CAM2 (FLIR) not checked", results["cameras"].detail)
        self.assertEqual(code, dc.EXIT_WARN)


class ModelCheck(Site):                                              # 6d (D-060)
    def model_root(self):
        return os.path.join(self.models_root, "example_model")

    def test_running_version_not_pinned(self):
        results, _ = self.run_all()
        self.assertEqual(results["model"].status, dc.PASS)
        self.assertIn("running example_model/2026.10.1", results["model"].detail)
        self.assertIn("not pinned", results["model"].detail)

    def test_pinned_to_the_running_version(self):
        with open(os.path.join(self.model_root(), "pinned"), "w") as f:
            f.write("2026.10.1\n")
        results, code = self.run_all()
        self.assertEqual(results["model"].status, dc.PASS)
        self.assertTrue(results["model"].detail.endswith("; pinned"))
        self.assertEqual(code, dc.EXIT_PASS)

    def test_pinned_to_another_version_is_a_warning(self):
        make_package(self.models_root, "example_model", "2026.11.1")      # moves current
        with open(os.path.join(self.model_root(), "pinned"), "w") as f:
            f.write("2026.10.1")
        results, code = self.run_all()
        self.assertEqual(results["model"].status, dc.WARN)
        self.assertIn("running example_model/2026.11.1", results["model"].detail)
        self.assertIn("pinned to 2026.10.1, which is not the running version", results["model"].detail)
        self.assertEqual(code, dc.EXIT_WARN)

    def test_no_version_installed_is_a_warning(self):
        shutil.rmtree(self.model_root())
        results, code = self.run_all()
        self.assertEqual(results["model"].status, dc.WARN)
        self.assertIn("example_model: no version installed", results["model"].detail)
        self.assertIn("make_model_package.py", results["model"].detail)
        self.assertEqual(code, dc.EXIT_WARN)

    def test_installed_but_no_current_is_a_warning(self):
        os.remove(os.path.join(self.model_root(), "current"))
        results, _ = self.run_all()
        self.assertEqual(results["model"].status, dc.WARN)

    def test_current_to_missing_folder_fails(self):
        os.remove(os.path.join(self.model_root(), "current"))
        os.symlink("9.9.9", os.path.join(self.model_root(), "current"))
        results, code = self.run_all()
        self.assertEqual(results["model"].status, dc.FAIL)
        self.assertIn("current points to a missing version folder", results["model"].detail)

    def test_wrong_checksum_fails(self):
        with open(os.path.join(self.model_root(), "2026.10.1", "model_final.pth"), "ab") as f:
            f.write(b"x")
        results, code = self.run_all()
        self.assertEqual(results["model"].status, dc.FAIL)
        self.assertIn("model_final.pth: checksum does not match", results["model"].detail)
        self.assertEqual(code, dc.EXIT_FAIL)

    def test_bad_pinned_marker_fails(self):
        os.makedirs(os.path.join(self.model_root(), "pinned"))
        results, _ = self.run_all()
        self.assertEqual(results["model"].status, dc.FAIL)
        self.assertIn("pinned", results["model"].detail)


class NeverPrintsASecret(Site):                                      # done when (d)
    def test_no_secret_in_any_output(self):
        outputs = []
        scenarios = [
            lambda: None,
            lambda: sys.modules.__setitem__("pymysql", fake_pymysql(FakeMySQLError(1045, f"pw {DB_PASSWORD}"))),
            lambda: sys.modules.__setitem__("pymysql", fake_pymysql(RuntimeError(DB_PASSWORD))),
        ]
        for scenario in scenarios:
            scenario()
            outputs.append(self.run_cli()[1])
        self.data["db"]["password"] = 12345        # a wrong type must not echo the value either
        self.write_config()
        outputs.append(self.run_cli()[1])
        for out in outputs:
            self.assertNotIn(DB_PASSWORD, out)
            self.assertNotIn(DB_USER, out)
            self.assertNotIn("12345", out)


class SafeOnARunningSite(unittest.TestCase):
    def test_only_enumerates_never_opens(self):
        code = dc._ENUMERATE_CODE
        self.assertIn("EnumerateDevices", code)
        for forbidden in ("CreateDevice", "InstantCamera", "Open", "StartGrabbing"):
            self.assertNotIn(forbidden, code)

    def test_enumeration_has_a_time_limit(self):
        with mock.patch.object(dc.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="x", timeout=12)):
            with self.assertRaises(dc.EnumerationError) as ctx:
                dc.enumerate_basler_serials()
        self.assertIn("more than 12", str(ctx.exception))

    def test_enumeration_parses_serials(self):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="24824427\n24874775\n", stderr="")
        with mock.patch.object(dc.subprocess, "run", return_value=done):
            self.assertEqual(dc.enumerate_basler_serials(), ["24824427", "24874775"])

    def test_worst_case_time_budget(self):                           # done when (c)
        total = dc.CAMERA_ENUM_TIMEOUT_S + dc.DB_TIMEOUT_S * 2 + dc.MQTT_TIMEOUT_S + dc.GIT_TIMEOUT_S * 2
        self.assertLessEqual(total, 30)


if __name__ == "__main__":
    unittest.main()
