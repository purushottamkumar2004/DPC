"""Tests for common/config/site_config.py (STATUS item 1).

Run from the repo root (only PyYAML needed):
    python3 -m unittest tests.unit.test_site_config -v
"""

import copy
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common.config import site_config as sc  # noqa: E402

EXAMPLE_YAML = os.path.join(REPO_ROOT, "config", "examples", "site.example.yaml")
EXAMPLE_SECRETS = os.path.join(REPO_ROOT, "config", "examples", "secrets.example.env")


def example_data():
    with open(EXAMPLE_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


class _TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = os.path.join(self.tmp, "INSIGHTZZ")
        os.makedirs(os.path.join(self.root, "config"))

    def tearDown(self):
        self._tmp.cleanup()

    def write_yaml(self, data, name="site.yaml"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        return path

    def config_data(self):
        """The example, with paths.root pointing into the temp dir."""
        data = example_data()
        data["paths"]["root"] = self.root
        return data

    def problems_for(self, data):
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(self.write_yaml(data))
        return ctx.exception.problems

    def assertProblem(self, problems, pattern):
        self.assertTrue(any(re.search(pattern, p) for p in problems),
                        f"no problem matches {pattern!r} in {problems}")


# ----------------------------------------------------------------------
class ExampleAndDefaults(_TempDirTest):
    def test_example_file_loads_into_typed_object(self):          # done when (a)
        cfg = sc.load_site_config(EXAMPLE_YAML)
        self.assertIsInstance(cfg, sc.SiteConfig)
        self.assertEqual(cfg.schema_version, 1)
        self.assertEqual([c.id for c in cfg.cameras], ["CAM1", "CAM2"])
        cam1 = cfg.camera("CAM1")
        self.assertIsInstance(cam1.serial, str)
        self.assertIsInstance(cam1.fps, float)
        self.assertEqual(cam1.side, "LEFT")
        self.assertEqual(cfg.camera_for_side("RIGHT").id, "CAM2")
        self.assertEqual(cam1.defect_side, "Left")
        self.assertEqual(cam1.log_file_name, "FrameCapture_CAM1.log")
        self.assertIsInstance(cfg.machine.object_sizes["LEFT"], sc.ObjectSizes)

    def test_defaults_are_applied(self):
        data = self.config_data()
        for key in ("logging", "mqtt", "retention"):
            del data[key]
        for key in ("host", "port", "health_table"):
            del data["db"][key]
        for key in ("type", "port", "timeout_s", "cleaning_pulse_seconds"):
            del data["plc"][key]
        for key in ("weights",):
            del data["model"][key]
        for key in ("report_dir", "cc"):
            del data["mail"][key]
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.logging.backup_count, 5)
        self.assertEqual((cfg.db.host, cfg.db.port, cfg.db.health_table), ("localhost", 3306, "health_status"))
        self.assertEqual((cfg.mqtt.host, cfg.mqtt.port), ("localhost", 1883))
        self.assertEqual((cfg.plc.type, cfg.plc.port, cfg.plc.timeout_s, cfg.plc.cleaning_pulse_seconds),
                         ("modbus_tcp", 502, 3.0, 10.0))
        self.assertTrue(cfg.model.weights.endswith(os.path.join("example_model", "model_final.pth")))
        self.assertEqual(cfg.model.detect_labels, cfg.model.classes)
        self.assertEqual(cfg.mail.report_dir, os.path.join(self.root, "data", "reports"))
        self.assertEqual(cfg.mail.cc, ())
        self.assertEqual(cfg.retention.disk_percent,
                         {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95})
        self.assertEqual((cfg.retention.repeated.days, cfg.retention.repeated.items), (20, 200))
        self.assertEqual(cfg.machine.wheel_stand_required_counts, {})
        self.assertIsNone(cfg.machine.expected_cycle_total)

    def test_smtp_defaults_when_sender_is_smtp(self):
        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual((cfg.mail.smtp.host, cfg.mail.smtp.port), ("smtp.gmail.com", 587))


class PathHandling(_TempDirTest):                                    # done when (c)
    def test_tilde_root_is_expanded(self):
        data = self.config_data()
        data["paths"]["root"] = "~/INSIGHTZZ"
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.paths.root, self.root)

    def test_relative_paths_are_relative_to_root(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        model_dir = os.path.join(self.root, "data", "models", "example_model")
        self.assertEqual(cfg.model.dir, model_dir)
        self.assertEqual(cfg.model.class_json, os.path.join(model_dir, "classes.json"))
        self.assertEqual(cfg.model.detectron_yaml, os.path.join(model_dir, "mask_rcnn.yaml"))

    def test_absolute_paths_are_kept(self):
        data = self.config_data()
        data["model"]["detectron_yaml"] = "/opt/detectron/cfg.yaml"
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.model.detectron_yaml, "/opt/detectron/cfg.yaml")

    def test_derived_layout(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        p = cfg.paths
        self.assertEqual(p.frame_dir("CAM1", "INF"), os.path.join(self.root, "data", "frames", "CAM1", "INF"))
        self.assertEqual(p.image_dir("DEFECTIVE", "RIGHT"),
                         os.path.join(self.root, "data", "images", "DEFECTIVE", "RIGHT"))
        self.assertEqual(p.secrets_file, os.path.join(self.root, "config", "secrets.env"))
        self.assertEqual(p.logs, os.path.join(self.root, "logs"))
        with self.assertRaises(ValueError):
            p.frame_dir("CAM3", "INF")

    def test_relative_root_is_an_error(self):
        data = self.config_data()
        data["paths"]["root"] = "INSIGHTZZ"
        self.assertProblem(self.problems_for(data), r"^paths\.root: must be an absolute path")

    def test_location_from_env_var(self):
        path = self.write_yaml(self.config_data(), "other.yaml")
        with mock.patch.dict(os.environ, {sc.CONFIG_ENV_VAR: path}):
            self.assertEqual(sc.load_site_config().source_path, path)


class Problems(_TempDirTest):                                        # done when (b)
    def test_missing_required_key_is_named(self):
        data = self.config_data()
        del data["cameras"][1]["serial"]
        self.assertEqual(self.problems_for(data), ["cameras[1].serial: missing"])

    def test_missing_section_is_reported_once(self):
        data = self.config_data()
        del data["site"]
        self.assertEqual(self.problems_for(data), ["site: missing"])

    def test_unknown_key_is_named_with_suggestion(self):
        data = self.config_data()
        data["plc"]["light_regster"] = data["plc"].pop("light_register")
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^plc\.light_regster: unknown key \(did you mean 'light_register'\?\)")
        self.assertProblem(problems, r"^plc\.light_register: missing")

    def test_unknown_top_level_key(self):
        data = self.config_data()
        data["camera"] = []
        self.assertProblem(self.problems_for(data), r"^camera: unknown key")

    def test_wrong_types_are_named(self):
        data = self.config_data()
        data["cameras"][0]["serial"] = 24824427          # unquoted serial
        data["cameras"][0]["exposure_us"] = "1500"
        data["cameras"][0]["fps"] = True
        data["db"]["port"] = 3306.5
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^cameras\[0\]\.serial: must be a string \(quote it\)")
        self.assertProblem(problems, r"^cameras\[0\]\.exposure_us: must be an integer")
        self.assertProblem(problems, r"^cameras\[0\]\.fps: must be a number")
        self.assertProblem(problems, r"^db\.port: must be an integer")

    def test_every_problem_is_listed_at_once(self):
        data = self.config_data()
        data["schema_version"] = 2
        data["site"]["id"] = "Bad Site"
        data["cameras"][0]["rotate"] = 45
        data["model"]["threshold"] = 1.0
        data["plc"]["ip"] = "192.168.1"
        data["mail"]["recipients"] = ["not-an-address"]
        problems = self.problems_for(data)
        self.assertEqual(len(problems), 6, problems)
        for pattern in (r"^schema_version: must be 1", r"^site\.id: use lowercase", r"^cameras\[0\]\.rotate",
                        r"^model\.threshold: must be < 1", r"^plc\.ip: must be an IPv4",
                        r"^mail\.recipients\[0\]: must be an email"):
            self.assertProblem(problems, pattern)

    def test_ranges(self):
        data = self.config_data()
        data["cameras"][0]["fps"] = 0
        data["cameras"][1]["exposure_us"] = 5
        data["machine"]["total_wheel_count"] = 0
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^cameras\[0\]\.fps: must be > 0")
        self.assertProblem(problems, r"^cameras\[1\]\.exposure_us: must be between 10 and 1000000")
        self.assertProblem(problems, r"^machine\.total_wheel_count")

    def test_rotate_has_no_default(self):
        data = self.config_data()
        del data["cameras"][0]["rotate"]
        self.assertEqual(self.problems_for(data), ["cameras[0].rotate: missing"])

    def test_duplicate_yaml_key_is_an_error(self):
        path = os.path.join(self.tmp, "dup.yaml")
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            text = f.read().replace("  name: example_db", "  name: example_db\n  name: other_db")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(path)
        self.assertIn("duplicate key 'name'", ctx.exception.problems[0])

    def test_missing_file(self):
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(os.path.join(self.tmp, "nope.yaml"))
        self.assertEqual(ctx.exception.problems, ["file not found"])

    def test_sql_names_are_plain_identifiers(self):
        data = self.config_data()
        data["db"]["health_table"] = "health; DROP TABLE x"
        self.assertProblem(self.problems_for(data), r"^db\.health_table: use only letters")


class CrossFieldChecks(_TempDirTest):
    def test_duplicate_camera_values(self):
        data = self.config_data()
        data["cameras"][1]["serial"] = data["cameras"][0]["serial"]
        self.assertProblem(self.problems_for(data), r"^cameras: serial '00000001' is used by more than one")

    def test_cam1_must_be_left(self):
        data = self.config_data()
        data["cameras"][0]["side"] = "RIGHT"
        self.assertProblem(self.problems_for(data), r"^cameras\[0\]\.side: CAM1 must be LEFT")

    def test_flir_block_required_for_flir(self):
        data = self.config_data()
        data["cameras"][1]["driver"] = "flir"
        self.assertEqual(self.problems_for(data), ["cameras[1].flir: missing"])

    def test_flir_block_loads(self):
        data = self.config_data()
        data["cameras"][1]["driver"] = "flir"
        data["cameras"][1]["flir"] = {"interface": "GEV Interface 1", "throughput_bps": 19662239}
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.camera("CAM2").flir.throughput_bps, 19662239)
        self.assertIsNone(cfg.camera("CAM1").flir)

    def test_flir_block_not_allowed_for_basler(self):
        data = self.config_data()
        data["cameras"][0]["flir"] = {"interface": "x", "throughput_bps": 1}
        self.assertEqual(self.problems_for(data), ["cameras[0].flir: only allowed when driver is flir"])

    def test_object_sizes_must_match_camera_sides(self):
        data = self.config_data()
        del data["cameras"][1]                      # only LEFT left
        self.assertEqual(self.problems_for(data),
                         ["machine.object_sizes.RIGHT: no camera is configured for this side"])
        data = self.config_data()
        del data["machine"]["object_sizes"]["RIGHT"]
        self.assertEqual(self.problems_for(data), ["machine.object_sizes.RIGHT: missing"])

    def test_single_camera_site_loads(self):
        data = self.config_data()
        del data["cameras"][1]
        del data["machine"]["object_sizes"]["RIGHT"]
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(len(cfg.cameras), 1)

    def test_too_many_cameras(self):
        data = self.config_data()
        data["cameras"].append(copy.deepcopy(data["cameras"][0]))
        self.assertProblem(self.problems_for(data), r"^cameras: must be a list with 1 or 2")

    def test_smtp_block_only_with_smtp(self):
        data = self.config_data()
        data["mail"]["smtp"] = {"host": "smtp.example.com", "port": 587}
        self.assertEqual(self.problems_for(data), ["mail.smtp: only allowed when sender is smtp"])

    def test_detect_labels_subset_of_classes(self):
        data = self.config_data()
        data["model"]["detect_labels"] = ["wheel", "chain"]
        self.assertEqual(self.problems_for(data), ["model.detect_labels: 'chain' is not in classes"])

    def test_disk_percent_strictly_increasing(self):
        data = self.config_data()
        data["retention"]["disk_percent"]["mail"] = 85
        self.assertProblem(self.problems_for(data), r"^retention\.disk_percent: must increase strictly")

    def test_wheel_stand_counts(self):
        data = self.config_data()
        data["machine"]["wheel_stand_required_counts"] = {34: 3}
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.machine.wheel_stand_required_counts, {34: 3})
        data["machine"]["wheel_stand_required_counts"] = {500: 3, "x": 1}
        problems = self.problems_for(data)
        self.assertProblem(problems, r"wheel_stand_required_counts\[500\]: wheel number is above")
        self.assertProblem(problems, r"wheel_stand_required_counts\['x'\]: wheel number must be an integer")


class CommandLine(_TempDirTest):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sc.main(list(args))
        return code, out.getvalue()

    def test_ok(self):
        code, out = self.run_cli(EXAMPLE_YAML)
        self.assertEqual((code, out.strip()), (0, "OK"))

    def test_problems_and_exit_1(self):
        data = self.config_data()
        del data["db"]["name"]
        code, out = self.run_cli(self.write_yaml(data))
        self.assertEqual(code, 1)
        self.assertIn("db.name: missing", out)

    def test_with_secrets_flag(self):
        path = self.write_yaml(self.config_data())
        code, out = self.run_cli(path, "--with-secrets")
        self.assertEqual(code, 1)
        self.assertIn("file not found", out)


# ----------------------------------------------------------------------
GOOD_SECRETS = {
    "DPC_DB_USER": "dpc_app", "DPC_DB_PASS": "s3cret-db-value",
    "DPC_MS_TENANT_ID": "tenant", "DPC_MS_CLIENT_ID": "client",
    "DPC_MS_CLIENT_SECRET": "s3cret-graph-value", "DPC_MAIL_SENDER": "reports@example.com",
}


class SecretsFile(_TempDirTest):                                    # done when (d)
    def setUp(self):
        super().setUp()
        self.cfg = sc.load_site_config(self.write_yaml(self.config_data()))

    def write_secrets(self, values, mode=0o600, extra_lines=()):
        path = self.cfg.paths.secrets_file
        with open(path, "w", encoding="utf-8") as f:
            f.write("# comment\n\n")
            for key, value in values.items():
                f.write(f"{key}={value}\n")
            for line in extra_lines:
                f.write(line + "\n")
        os.chmod(path, mode)
        return path

    def problems(self, values, **kwargs):
        self.write_secrets(values, **kwargs)
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_secrets(self.cfg)
        self.assertNotIn("s3cret", str(ctx.exception))     # never print a value
        return ctx.exception.problems

    def test_valid_file_loads(self):
        self.write_secrets(GOOD_SECRETS)
        secrets = sc.load_secrets(self.cfg)
        self.assertEqual(secrets["DPC_DB_USER"], "dpc_app")
        self.assertIsNone(secrets.get("DPC_MQTT_USER"))
        self.assertNotIn("s3cret", repr(secrets))

    def test_quotes_are_stripped(self):
        self.write_secrets({**GOOD_SECRETS, "DPC_DB_PASS": '"with space"'})
        self.assertEqual(sc.load_secrets(self.cfg)["DPC_DB_PASS"], "with space")

    def test_refused_unless_private(self):
        for mode in (0o644, 0o640, 0o604):
            problems = self.problems(GOOD_SECRETS, mode=mode)
            self.assertProblem(problems, rf"permissions are {mode:o}; run: chmod 600")

    def test_missing_file(self):
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_secrets(self.cfg)
        self.assertProblem(ctx.exception.problems, r"^file not found")

    def test_required_keys_follow_the_mail_sender(self):
        values = dict(GOOD_SECRETS)
        del values["DPC_MS_CLIENT_SECRET"]
        self.assertEqual(self.problems(values), ["DPC_MS_CLIENT_SECRET: missing"])

        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        self.cfg = sc.load_site_config(self.write_yaml(data))
        smtp_values = {"DPC_DB_USER": "u", "DPC_DB_PASS": "s3cret", "DPC_MAIL_SENDER": "a@example.com"}
        self.assertEqual(self.problems(smtp_values), ["DPC_SMTP_PASSWORD: missing"])

    def test_placeholder_is_refused(self):
        self.assertEqual(self.problems({**GOOD_SECRETS, "DPC_DB_PASS": "CHANGE_ME"}),
                         ["DPC_DB_PASS: still the placeholder CHANGE_ME"])

    def test_unknown_key_and_bad_line(self):
        problems = self.problems({**GOOD_SECRETS, "DPC_DB_PASSWORD": "s3cret"}, extra_lines=["garbage"])
        self.assertProblem(problems, r"^DPC_DB_PASSWORD: unknown key \(did you mean 'DPC_DB_PASS'\?\)")
        self.assertProblem(problems, r"^line \d+: expected KEY=VALUE")

    def test_mqtt_login_needs_both(self):
        self.assertEqual(self.problems({**GOOD_SECRETS, "DPC_MQTT_USER": "u"}),
                         ["DPC_MQTT_USER and DPC_MQTT_PASS: set both or neither"])

    def test_retired_site_name_is_a_warning(self):
        self.write_secrets({**GOOD_SECRETS, "DPC_SITE_NAME": "Old name"})
        secrets = sc.load_secrets(self.cfg)
        self.assertEqual(len(secrets.warnings), 1)
        self.assertIn("DPC_SITE_NAME", secrets.warnings[0])

    def test_secrets_are_not_read_from_site_yaml(self):
        data = self.config_data()
        data["db"]["password"] = "s3cret"
        self.assertEqual(self.problems_for(data), ["db.password: unknown key"])


class ExamplesHaveNoRealValues(unittest.TestCase):                  # done when (f)
    def test_secrets_example_has_only_placeholders(self):
        with open(EXAMPLE_SECRETS, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lstrip("#").strip()
                if not re.match(r"^DPC_[A-Z_]+=", line):
                    continue
                key, value = line.split("=", 1)
                self.assertIn(key, sc.SECRET_KEYS)
                ok = value == "CHANGE_ME" or all(v.strip().endswith("@example.com") for v in value.split(","))
                self.assertTrue(ok, f"{key} in the example is not a placeholder")

    def test_site_example_has_placeholder_serials_and_ip(self):
        cfg = sc.load_site_config(EXAMPLE_YAML)
        for cam in cfg.cameras:
            self.assertRegex(cam.serial, r"^0+\d$", "example serials must be placeholders like 00000001")
        self.assertTrue(cfg.plc.ip.startswith("192.0.2."), "use the documentation range 192.0.2.x")
        for address in cfg.mail.recipients:
            self.assertTrue(address.endswith("@example.com"))


if __name__ == "__main__":
    unittest.main()
