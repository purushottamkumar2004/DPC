"""Tests for common/config/site_config.py (STATUS items 1 and 1b, schema v2).

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


# Known secret values used to prove that nothing prints them.
SECRETS = {
    ("db", "user"): "dpc_app_s3cret_user",
    ("db", "password"): "s3cret-db-pass",
    ("mail", "graph", "tenant_id"): "s3cret-tenant",
    ("mail", "graph", "client_id"): "s3cret-client",
    ("mail", "graph", "client_secret"): "s3cret-graph",
}


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

    def write_text(self, text, name="site.yaml", mode=0o600):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def write_yaml(self, data, name="site.yaml", mode=0o600):
        return self.write_text(yaml.safe_dump(data, sort_keys=False), name, mode)

    def load_example(self):
        """The repo copy is 644 (git cannot store 600), so load a private copy."""
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            return sc.load_site_config(self.write_text(f.read(), "example.yaml"))

    def config_data(self):
        """The example, with paths.root pointing into the temp dir and known secrets."""
        data = example_data()
        data["paths"]["root"] = self.root
        for key_path, value in SECRETS.items():
            node = data
            for key in key_path[:-1]:
                node = node[key]
            node[key_path[-1]] = value
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
    def test_example_file_loads_into_typed_object(self):          # 1b done when (a)
        cfg = self.load_example()
        self.assertIsInstance(cfg, sc.SiteConfig)
        self.assertEqual(cfg.schema_version, 2)
        self.assertEqual(cfg.mail.sender, "graph")
        self.assertIsNone(cfg.mail.smtp)
        self.assertIsNone(cfg.mqtt.user)          # "" means no login
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
        for key in ("report_dir", "cc", "disk_report_to"):
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
        self.assertEqual(cfg.mail.disk_report_to, cfg.mail.recipients)
        self.assertEqual(cfg.retention.disk_percent,
                         {"delete": 75, "raw_delete": 85, "mail": 90, "inference_delete": 95})
        self.assertEqual((cfg.retention.repeated.days, cfg.retention.repeated.items), (20, 200))
        self.assertEqual(cfg.machine.wheel_stand_required_counts, {})
        self.assertIsNone(cfg.machine.expected_cycle_total)

    def test_smtp_defaults_when_sender_is_smtp(self):
        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        del data["mail"]["graph"]
        data["mail"]["smtp"] = {"password": "s3cret-smtp"}
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual((cfg.mail.smtp.host, cfg.mail.smtp.port, cfg.mail.smtp.password),
                         ("smtp.gmail.com", 587, "s3cret-smtp"))
        self.assertIsNone(cfg.mail.graph)


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
        data["schema_version"] = 3
        data["site"]["id"] = "Bad Site"
        data["cameras"][0]["rotate"] = 45
        data["model"]["threshold"] = 1.0
        data["plc"]["ip"] = "192.168.1"
        data["mail"]["recipients"] = ["not-an-address"]
        problems = self.problems_for(data)
        self.assertEqual(len(problems), 6, problems)
        for pattern in (r"^schema_version: must be 2", r"^site\.id: use lowercase", r"^cameras\[0\]\.rotate",
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
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            text = f.read().replace("  name: example_db", "  name: example_db\n  name: other_db")
        path = self.write_text(text, "dup.yaml")
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
        code, out = self.run_cli(self.write_yaml(self.config_data()))
        self.assertEqual((code, out.strip()), (0, "OK"))

    def test_root_db_user_prints_ok_and_one_warning(self):        # follow-up 9 done when
        data = self.config_data()
        data["db"]["user"] = "root"
        code, out = self.run_cli(self.write_yaml(data))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "OK",
            "WARNING db.user is root; a dedicated user with limited rights is recommended (D-030)",
        ])
        self.assertNotIn(SECRETS[("db", "password")], out)

    def test_problems_and_exit_1(self):
        data = self.config_data()
        del data["db"]["name"]
        code, out = self.run_cli(self.write_yaml(data))
        self.assertEqual(code, 1)
        self.assertIn("db.name: missing", out)


# ----------------------------------------------------------------------
# Schema v2: secrets in site.yaml (STATUS item 1b)
# ----------------------------------------------------------------------
class V1Files(_TempDirTest):                                         # 1b done when (a)
    def v1_data(self):
        data = self.config_data()
        data["schema_version"] = 1
        for key in ("user", "password"):
            del data["db"][key]
        for key in ("sender_address", "disk_report_to", "graph"):
            del data["mail"][key]
        return data

    def test_v1_file_says_which_keys_to_add(self):
        problems = self.problems_for(self.v1_data())
        self.assertEqual(len(problems), 1, problems)
        for key in ("db.user", "db.password", "mail.sender_address", "mail.graph", "schema_version: 2"):
            self.assertIn(key, problems[0])

    def test_v1_smtp_file_asks_for_smtp_password(self):
        data = self.v1_data()
        data["mail"]["sender"] = "smtp"
        problems = self.problems_for(data)
        self.assertIn("mail.smtp.password", problems[0])
        self.assertNotIn("mail.graph", problems[0])

    def test_v1_file_with_keys_added_only_needs_the_bump(self):
        data = self.config_data()
        data["schema_version"] = 1
        problems = self.problems_for(data)
        self.assertEqual(problems, ["schema_version: this is a v1 file; set schema_version: 2 (CONTRACTS 7, D-048)"])

    def test_other_versions(self):
        data = self.config_data()
        data["schema_version"] = 3
        self.assertProblem(self.problems_for(data), r"^schema_version: must be 2, got 3")


class Permissions(_TempDirTest):                                     # 1b done when (b)
    def test_not_chmod_600_is_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o660):
            path = self.write_yaml(self.config_data(), mode=mode)
            with self.assertRaises(sc.SiteConfigError) as ctx:
                sc.load_site_config(path)
            self.assertEqual(len(ctx.exception.problems), 1)
            self.assertIn(f"permissions are {mode:o}", ctx.exception.problems[0])
            self.assertIn("chmod 600", ctx.exception.problems[0])

    def test_refused_before_reading(self):
        # Broken YAML in a world-readable file: only the permission problem is reported.
        path = self.write_text("db: [unclosed", mode=0o644)
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(path)
        self.assertProblem(ctx.exception.problems, r"^permissions are 644")
        self.assertEqual(len(ctx.exception.problems), 1)

    def test_400_is_accepted(self):
        sc.load_site_config(self.write_yaml(self.config_data(), mode=0o400))


class SecretFields(_TempDirTest):                                    # 1b done when (e)
    def test_secrets_are_loaded(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        self.assertEqual(cfg.db.user, SECRETS[("db", "user")])
        self.assertEqual(cfg.db.password, SECRETS[("db", "password")])
        self.assertEqual(cfg.mail.graph.client_secret, SECRETS[("mail", "graph", "client_secret")])

    def test_db_login_required(self):
        data = self.config_data()
        del data["db"]["password"]
        self.assertEqual(self.problems_for(data), ["db.password: missing"])

    def test_root_db_user_is_a_warning(self):                    # follow-up 9 (D-050)
        data = self.config_data()
        data["db"]["user"] = "root"
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.db.user, "root")
        self.assertEqual(cfg.warnings,
                         ("db.user is root; a dedicated user with limited rights is recommended (D-030)",))

    def test_no_warning_for_a_dedicated_user(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        self.assertEqual(cfg.warnings, ())

    def test_graph_block_required_for_graph(self):
        data = self.config_data()
        del data["mail"]["graph"]
        self.assertEqual(self.problems_for(data), ["mail.graph: missing"])
        data = self.config_data()
        del data["mail"]["graph"]["client_secret"]
        self.assertEqual(self.problems_for(data), ["mail.graph.client_secret: missing"])

    def test_graph_block_not_allowed_for_smtp(self):
        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        data["mail"]["smtp"] = {"password": "s3cret-smtp"}
        self.assertEqual(self.problems_for(data), ["mail.graph: only allowed when sender is graph"])

    def test_smtp_block_required_for_smtp(self):
        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        del data["mail"]["graph"]
        self.assertEqual(self.problems_for(data), ["mail.smtp: missing"])
        data["mail"]["smtp"] = {"host": "smtp.example.com"}
        self.assertEqual(self.problems_for(data), ["mail.smtp.password: missing"])

    def test_sender_address_required(self):
        data = self.config_data()
        del data["mail"]["sender_address"]
        self.assertEqual(self.problems_for(data), ["mail.sender_address: missing"])
        data["mail"]["sender_address"] = "not-an-address"
        self.assertProblem(self.problems_for(data), r"^mail\.sender_address: must be an email")

    def test_mqtt_login_both_or_neither(self):
        data = self.config_data()
        data["mqtt"]["user"] = "broker_user"
        self.assertEqual(self.problems_for(data), ["mqtt.user: give both user and password, or neither"])
        data["mqtt"]["password"] = "s3cret-mqtt"
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual((cfg.mqtt.user, cfg.mqtt.password), ("broker_user", "s3cret-mqtt"))


class NoSecretInMessages(_TempDirTest):                              # 1b done when (d)
    def assertNoSecret(self, text):
        for value in list(SECRETS.values()) + ["s3cret"]:
            self.assertNotIn(value, text)

    def test_wrong_type_does_not_print_the_value(self):
        data = self.config_data()
        data["db"]["password"] = 123456789
        data["mail"]["graph"]["client_secret"] = ["s3cret-in-a-list"]
        problems = self.problems_for(data)
        self.assertIn("db.password: must be a string (quote it), got ***", problems)
        self.assertIn("mail.graph.client_secret: must be a string, got ***", problems)
        self.assertNotIn("123456789", "\n".join(problems))
        self.assertNoSecret("\n".join(problems))

    def test_broken_files_never_print_a_secret(self):
        breakers = [
            lambda d: d["db"].update(port="x"),
            lambda d: d["db"].update(name="bad name"),
            lambda d: d.update(schema_version=1),
            lambda d: d["mail"].update(sender="smtp"),
            lambda d: d["mail"]["graph"].update(extra_key=SECRETS[("db", "password")]),
            lambda d: d["mqtt"].update(user="u"),
        ]
        for breaker in breakers:
            data = self.config_data()
            breaker(data)
            with self.assertRaises(sc.SiteConfigError) as ctx:
                sc.load_site_config(self.write_yaml(data))
            self.assertNoSecret(str(ctx.exception))

    def test_yaml_syntax_error_does_not_quote_the_line(self):
        path = self.write_text('db:\n  password: "s3cret-db-pass\n  name: x\n')
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(path)
        self.assertProblem(ctx.exception.problems, r"^not valid YAML: .*\(line \d+, column \d+\)")
        self.assertNoSecret(str(ctx.exception))

    def test_repr_hides_secrets(self):
        data = self.config_data()
        data["mqtt"].update(user="mqtt_s3cret_user", password="s3cret-mqtt")
        cfg = sc.load_site_config(self.write_yaml(data))
        text = repr(cfg) + str(cfg)
        self.assertNoSecret(text)
        self.assertIn("example_db", text)          # the rest is still visible


class Redacted(_TempDirTest):                                        # 1b done when (c)
    def secret_file(self):
        """The example text with known secrets written in (comments kept)."""
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("user: dpc_app ", f"user: {SECRETS[('db', 'user')]} ")
        text = text.replace('password: "CHANGE_ME"', f'password: "{SECRETS[("db", "password")]}"', 1)
        text = text.replace('tenant_id: "CHANGE_ME"', f"tenant_id: '{SECRETS[('mail', 'graph', 'tenant_id')]}'")
        text = text.replace('client_id: "CHANGE_ME"', f"client_id: {SECRETS[('mail', 'graph', 'client_id')]}")
        text = text.replace('client_secret: "CHANGE_ME"',
                            f'client_secret: "{SECRETS[("mail", "graph", "client_secret")]}"')
        for value in SECRETS.values():
            self.assertIn(value, text)               # the fixture really holds them
        return self.write_text(text)

    def test_redacted_copy_has_no_secret_value(self):
        out = sc.redacted_copy(self.secret_file())
        for value in SECRETS.values():
            self.assertNotIn(value, out)
        self.assertEqual(out.count('"***"'), 5)

    def test_redacted_copy_keeps_the_rest(self):
        out = sc.redacted_copy(self.secret_file())
        self.assertIn("# DPC site config, schema v2", out)          # comments kept
        self.assertIn('serial: "00000001"', out)
        self.assertIn('user: ""', out)                              # empty mqtt login stays visible
        data = yaml.safe_load(out)
        self.assertEqual(data["db"]["password"], "***")
        self.assertEqual(data["db"]["name"], "example_db")

    def test_cli_redacted(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sc.main([self.secret_file(), "--redacted"])
        self.assertEqual(code, 0)
        for value in SECRETS.values():
            self.assertNotIn(value, out.getvalue())

    def test_misspelt_and_unexpected_secret_keys_are_hidden(self):
        text = ("db:\n  pasword: s3cret-a\n  name: x\nextra:\n  api_key: s3cret-b\n"
                "mail:\n  graph:\n    client_secret: |\n      s3cret-c\n    list: [s3cret-d]\n"
                "anchors:\n  pw: &p s3cret-e\n  password: *p\n")
        out = sc.redact_text(text)
        for value in ("s3cret-a", "s3cret-b", "s3cret-c", "s3cret-e"):
            self.assertNotIn(value, out)
        self.assertIn("name: x", out)
        data = yaml.safe_load(out)                    # still valid YAML, aliases still resolve
        self.assertEqual(data["anchors"]["password"], "***")
        self.assertEqual(data["mail"]["graph"]["list"], ["s3cret-d"])   # not a secret key: kept

    def test_broken_yaml_prints_nothing_of_the_file(self):
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.redact_text('db:\n  password: "s3cret-x\n')
        self.assertNotIn("s3cret", str(ctx.exception))


class LegacyEnv(_TempDirTest):
    def test_env_vars_for_graph_site(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        env = sc.legacy_env_vars(cfg)
        self.assertEqual(env["DPC_DB_PASS"], SECRETS[("db", "password")])
        self.assertEqual(env["DPC_MS_CLIENT_SECRET"], SECRETS[("mail", "graph", "client_secret")])
        self.assertEqual(env["DPC_DISK_REPORT_TO"], "alerts@example.com")
        self.assertEqual(env["DPC_SITE_NAME"], "Example DPC")
        self.assertNotIn("DPC_SMTP_PASSWORD", env)
        self.assertNotIn("DPC_MQTT_USER", env)

    def test_env_vars_for_smtp_site(self):
        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        del data["mail"]["graph"]
        data["mail"]["smtp"] = {"password": "s3cret-smtp"}
        env = sc.legacy_env_vars(sc.load_site_config(self.write_yaml(data)))
        self.assertEqual(env["DPC_SMTP_PASSWORD"], "s3cret-smtp")
        self.assertNotIn("DPC_MS_TENANT_ID", env)


class ExampleHasNoRealValues(_TempDirTest):                          # 1 done when (f)
    def test_placeholders_only(self):
        cfg = self.load_example()
        for cam in cfg.cameras:
            self.assertRegex(cam.serial, r"^0+\d$", "example serials must be placeholders like 00000001")
        self.assertTrue(cfg.plc.ip.startswith("192.0.2."), "use the documentation range 192.0.2.x")
        for address in cfg.mail.recipients + cfg.mail.disk_report_to + (cfg.mail.sender_address,):
            self.assertTrue(address.endswith("@example.com"))
        self.assertEqual(cfg.db.password, "CHANGE_ME")
        g = cfg.mail.graph
        self.assertEqual({g.tenant_id, g.client_id, g.client_secret}, {"CHANGE_ME"})

    def test_no_secrets_env_left(self):
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, "config", "examples", "secrets.example.env")))


if __name__ == "__main__":
    unittest.main()
