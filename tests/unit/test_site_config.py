"""Tests for common/config/site_config.py, schema v3 (STATUS Phase 3 item 6, CONTRACTS 7.1, D-055).

Run from the repo root (only PyYAML needed):
    python3 -m unittest tests.unit.test_site_config -v
"""

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
V2_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "site_v2.yaml")

# Known secret values used to prove that nothing prints them.
SECRETS = {
    ("db", "user"): "dpc_app_s3cret_user",
    ("db", "password"): "s3cret-db-pass",
    ("graph", "tenant_id"): "s3cret-tenant",
    ("graph", "client_id"): "s3cret-client",
    ("graph", "client_secret"): "s3cret-graph",
}


def example_data():
    with open(EXAMPLE_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def set_path(data, key_path, value):
    for key in key_path[:-1]:
        data = data[key]
    data[key_path[-1]] = value


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
        """The example, with paths.root in the temp dir and known secrets."""
        data = example_data()
        data["paths"]["root"] = self.root
        for key_path, value in SECRETS.items():
            set_path(data, key_path, value)
        return data

    def problems_for(self, data):
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(self.write_yaml(data))
        return ctx.exception.problems

    def assertProblem(self, problems, pattern):
        self.assertTrue(any(re.search(pattern, p) for p in problems),
                        f"no problem matches {pattern!r} in {problems}")


# ======================================================================
# Item 6 done when: v3 example loads
# ======================================================================
class ExampleLoads(_TempDirTest):
    def test_example_loads_into_program_sections(self):
        cfg = self.load_example()
        self.assertEqual(cfg.schema_version, 3)
        self.assertEqual([c.id for c in cfg.capture.cameras], ["CAM1", "CAM2"])
        self.assertIs(cfg.cameras, cfg.capture.cameras)           # convenience for older callers
        self.assertEqual(cfg.camera_for_side("RIGHT").serial, "00000002")
        self.assertEqual(cfg.inspection.model.name, "example_model")
        self.assertFalse(hasattr(cfg.inspection.model, "version"))           # D-060
        self.assertEqual(cfg.inspection.sides["LEFT"].object_sizes.wheel_w, 530)
        self.assertEqual(cfg.inspection.auto_roi.mode, "observe")
        self.assertEqual(cfg.mail.sender, "graph")
        self.assertIsNotNone(cfg.graph)
        self.assertEqual(cfg.transfer.upload.remote_folder, "DPC/data/example_site")   # <site.id> replaced
        self.assertEqual(cfg.transfer.models_folder, "DPC/models")
        self.assertFalse(hasattr(cfg, "models"))                              # D-060
        for section in (cfg.capture, cfg.inspection, cfg.plc, cfg.mail, cfg.cleanup, cfg.transfer):
            self.assertEqual(section.log.backup_count, 5)

    def test_contracts_7_1_example_loads(self):
        """The YAML block in CONTRACTS 7.1, with placeholders filled, is a valid v3 file."""
        with open(os.path.join(REPO_ROOT, "docs", "CONTRACTS.md"), encoding="utf-8") as f:
            text = f.read()
        section = text[text.index("## 7.1"):]
        block = section[section.index("```yaml") + 7:section.index("```", section.index("```yaml") + 7)]
        data = yaml.safe_load(block.replace("<secret>", "x"))
        data["paths"]["root"] = self.root
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.inspection.model.name, "dpc_maskrcnn")
        self.assertEqual(cfg.transfer.models_folder, "DPC/models")
        self.assertEqual(cfg.transfer.upload.remote_folder, "DPC/data/tadipatri")
        self.assertEqual(cfg.warnings, ("db.user is root; a dedicated user with limited rights "
                                        "is recommended (D-030)",))

    def test_model_folders_are_derived(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        model_root = os.path.join(self.root, "data", "models", "example_model")
        self.assertEqual(cfg.inspection.model.root, model_root)

    def test_defaults(self):
        data = self.config_data()
        for key in ("mqtt", "cleanup", "transfer"):
            del data[key]
        for program in ("capture", "inspection", "plc", "mail"):
            del data[program]["log"]
        del data["inspection"]["auto_roi"]
        for key in ("disk_report_to", "smtp", "cc", "report_dir"):
            del data["mail"][key]
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertIsNone(cfg.transfer)
        self.assertEqual((cfg.inspection.auto_roi.mode, cfg.inspection.auto_roi.learn_images), ("observe", 30))
        self.assertEqual(cfg.cleanup.interval_seconds, 10)
        self.assertEqual(cfg.cleanup.disk_percent["delete"], 75)
        self.assertEqual(cfg.mail.disk_report_to, cfg.mail.recipients)
        self.assertEqual(cfg.mail.report_dir, os.path.join(self.root, "data", "reports"))
        self.assertEqual(cfg.plc.log.backup_count, 5)

    def test_empty_disk_report_to_means_recipients(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        self.assertEqual(cfg.mail.disk_report_to, ("alerts@example.com",))

    def test_transfer_without_upload(self):
        data = self.config_data()
        data["transfer"] = {"log": {"backup_count": 3}}
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertIsNone(cfg.transfer.upload)
        self.assertEqual(cfg.transfer.models_folder, "DPC/models")         # default


# ======================================================================
# Item 6 done when: a v2 file is rejected with the moves listed
# ======================================================================
class V2Files(_TempDirTest):
    def v2_problems(self):
        with open(V2_FIXTURE, encoding="utf-8") as f:
            path = self.write_text(f.read())
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(path)
        return ctx.exception.problems

    def test_every_moved_key_is_named(self):
        problems = self.v2_problems()
        self.assertIn("this is a v2 file", problems[0])
        expected = [
            "logging.backup_count -> <program>.log.backup_count",
            "cameras -> capture.cameras",
            "machine.total_wheel_count -> inspection.machine.total_wheel_count",
            "machine.object_sizes.LEFT -> inspection.sides.LEFT.object_sizes",
            "machine.object_sizes.RIGHT -> inspection.sides.RIGHT.object_sizes",
            "model.threshold -> inspection.threshold",
            "model.dir -> remove: it comes from the model package now",
            "model.classes -> remove",
            "mail.graph.tenant_id -> graph.tenant_id",
            "mail.graph.client_secret -> graph.client_secret",
            "retention.cleanup_interval_seconds -> cleanup.interval_seconds",
            "retention.disk_percent -> cleanup.disk_percent",
            "inspection.model -> add {name} of the model package",
        ]
        text = "\n".join(problems)
        for line in expected:
            self.assertIn(line, text)

    def test_v2_message_has_no_values(self):
        with open(V2_FIXTURE, encoding="utf-8") as f:
            text = f.read().replace('password: "CHANGE_ME"', 'password: "s3cret-db-pass"')
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(self.write_text(text))
        self.assertNotIn("s3cret", str(ctx.exception))
        self.assertNotIn("CHANGE_ME", str(ctx.exception))

    def test_v1_and_other_versions(self):
        data = self.config_data()
        data["schema_version"] = 1
        self.assertProblem(self.problems_for(data), r"^schema_version: this is a v1 file")
        data["schema_version"] = 4
        self.assertProblem(self.problems_for(data), r"^schema_version: must be 3, got 4")

    def test_converted_v2_file_loads(self):
        with open(V2_FIXTURE, encoding="utf-8") as f:
            v2 = yaml.safe_load(f)
        v2["paths"]["root"] = self.root
        v3 = sc.convert_v2_to_v3(v2, "example_model")
        cfg = sc.load_site_config(self.write_yaml(v3))
        self.assertEqual(cfg.inspection.model.name, "example_model")
        self.assertEqual(cfg.capture.cameras[0].serial, "00000001")
        self.assertEqual(cfg.inspection.threshold, 0.5)
        self.assertEqual(cfg.inspection.sides["RIGHT"].object_sizes.chain_w, 880)
        self.assertEqual(cfg.graph.client_id, "CHANGE_ME")
        self.assertEqual(cfg.cleanup.interval_seconds, 10)
        self.assertIsNone(cfg.transfer)

    def test_converter_keeps_unknown_keys_for_the_check(self):
        with open(V2_FIXTURE, encoding="utf-8") as f:
            v2 = yaml.safe_load(f)
        v2["paths"]["root"] = self.root
        v2["extra_thing"] = 1
        problems = self.problems_for(sc.convert_v2_to_v3(v2, "m"))
        self.assertEqual(problems, ["extra_thing: unknown key"])


# ======================================================================
# Grouping rules (D-055)
# ======================================================================
class Grouping(_TempDirTest):
    def test_graph_required_when_mail_uses_graph(self):
        data = self.config_data()
        del data["graph"]
        self.assertEqual(self.problems_for(data), ["graph: missing (needed by mail (sender graph), transfer)"])

    def test_graph_required_by_transfer_alone(self):
        data = self.config_data()
        del data["graph"]
        data["mail"]["sender"] = "smtp"
        data["mail"]["smtp"] = {"password": "s3cret-smtp"}
        self.assertEqual(self.problems_for(data), ["graph: missing (needed by transfer)"])

    def test_graph_optional_when_nobody_uses_it(self):
        data = self.config_data()
        del data["graph"]
        del data["transfer"]
        data["mail"]["sender"] = "smtp"
        data["mail"]["smtp"] = {"password": "s3cret-smtp"}
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertIsNone(cfg.graph)
        self.assertEqual(cfg.mail.smtp.password, "s3cret-smtp")

    def test_graph_under_mail_is_unknown_now(self):
        data = self.config_data()
        data["mail"]["graph"] = dict(data["graph"])
        self.assertEqual(self.problems_for(data), ["mail.graph: unknown key"])

    def test_smtp_rules(self):
        data = self.config_data()
        data["mail"]["sender"] = "smtp"
        self.assertEqual(self.problems_for(data), ["mail.smtp: missing"])
        data["mail"]["smtp"] = {"host": "smtp.example.com"}
        self.assertEqual(self.problems_for(data), ["mail.smtp.password: missing"])
        data = self.config_data()
        data["mail"]["smtp"] = {"password": "x"}
        self.assertEqual(self.problems_for(data),
                         ["mail.smtp: only allowed when sender is smtp (use null or leave it out)"])

    def test_old_top_level_sections_are_unknown(self):
        data = self.config_data()
        data["logging"] = {"backup_count": 5}
        data["retention"] = {}
        problems = self.problems_for(data)
        self.assertIn("logging: unknown key", problems)
        self.assertIn("retention: unknown key", problems)

    def test_model_files_are_not_site_config(self):
        data = self.config_data()
        data["inspection"]["model"]["classes"] = ["wheel"]
        self.assertEqual(self.problems_for(data), ["inspection.model.classes: unknown key"])

    def test_model_name_is_a_folder_name(self):
        data = self.config_data()
        data["inspection"]["model"]["name"] = "a/b"
        self.assertProblem(self.problems_for(data), r"^inspection\.model\.name: use letters")


class MovedToSupervisor(_TempDirTest):                                # 6d done when (D-060)
    def test_model_version_moved(self):
        data = self.config_data()
        data["inspection"]["model"]["version"] = "2026.10.1"
        self.assertEqual(self.problems_for(data), [
            "inspection.model.version: moved to the Supervisor (D-060): the running version is "
            "data/models/<name>/current, managed with the Supervisor's model commands"])

    def test_models_section_moved(self):
        data = self.config_data()
        data["models"] = {"remote_folder": "DPC/models", "check_interval_hours": 6, "auto_update": "off"}
        self.assertEqual(self.problems_for(data), [
            "models: moved to the Supervisor config (workers.yaml section models, D-060); "
            "the OneDrive folder is transfer.models_folder"])

    def test_both_at_once_and_any_value(self):
        data = self.config_data()
        data["inspection"]["model"]["version"] = None          # even an empty key is reported
        data["models"] = None
        problems = self.problems_for(data)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("D-060" in p for p in problems))

    def test_models_folder(self):
        data = self.config_data()
        data["transfer"]["models_folder"] = "Shared/DPC/models"
        self.assertEqual(sc.load_site_config(self.write_yaml(data)).transfer.models_folder, "Shared/DPC/models")
        data["transfer"]["models_folder"] = 5
        self.assertProblem(self.problems_for(data), r"^transfer\.models_folder: must be a string")

    def test_sides_must_match_cameras(self):
        data = self.config_data()
        del data["capture"]["cameras"][1]
        self.assertEqual(self.problems_for(data),
                         ["inspection.sides.RIGHT: no camera is configured for this side"])
        data = self.config_data()
        del data["inspection"]["sides"]["LEFT"]
        self.assertEqual(self.problems_for(data), ["inspection.sides.LEFT: missing"])

    def test_single_camera_site(self):
        data = self.config_data()
        del data["capture"]["cameras"][1]
        del data["inspection"]["sides"]["RIGHT"]
        self.assertEqual(len(sc.load_site_config(self.write_yaml(data)).cameras), 1)

    def test_auto_roi_choices(self):
        data = self.config_data()
        data["inspection"]["auto_roi"]["mode"] = False            # unquoted off in YAML
        self.assertEqual(sc.load_site_config(self.write_yaml(data)).inspection.auto_roi.mode, "off")
        data["inspection"]["auto_roi"]["mode"] = "learn"
        self.assertProblem(self.problems_for(data), r"^inspection\.auto_roi\.mode: must be one of off, observe, filter")
        data["inspection"]["auto_roi"]["mode"] = True             # "on" is not a choice
        self.assertProblem(self.problems_for(data), r"^inspection\.auto_roi\.mode: must be a string")

    def test_transfer_upload_values(self):
        data = self.config_data()
        data["transfer"]["upload"].update(weekday="Sunday", chunk_mb=0)
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^transfer\.upload\.weekday: must be one of monday")
        self.assertProblem(problems, r"^transfer\.upload\.chunk_mb: must be between 1")

    def test_disk_percent_and_interval_in_cleanup(self):
        data = self.config_data()
        data["cleanup"]["disk_percent"]["mail"] = 80
        data["cleanup"]["cleanup_interval_seconds"] = 5
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^cleanup\.disk_percent: must increase strictly")
        self.assertProblem(problems, r"^cleanup\.cleanup_interval_seconds: unknown key \(did you mean 'interval_seconds'")

    def test_per_program_log(self):
        data = self.config_data()
        data["plc"]["log"] = {"backup_count": 0}
        data["capture"]["log"] = {"backup_count": 7, "level": "debug"}
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^plc\.log\.backup_count: must be between 1 and 365")
        self.assertProblem(problems, r"^capture\.log\.level: unknown key")


# ======================================================================
# Rules kept from v2 (CONTRACTS 7.1: same rules as v2)
# ======================================================================
class KeptRules(_TempDirTest):
    def test_missing_and_unknown_keys_named_with_path(self):
        data = self.config_data()
        del data["capture"]["cameras"][1]["serial"]
        data["plc"]["light_regster"] = data["plc"].pop("light_register")
        problems = self.problems_for(data)
        self.assertIn("capture.cameras[1].serial: missing", problems)
        self.assertProblem(problems, r"^plc\.light_regster: unknown key \(did you mean 'light_register'\?\)")

    def test_camera_cross_checks(self):
        data = self.config_data()
        data["capture"]["cameras"][0]["side"] = "RIGHT"
        data["capture"]["cameras"][1]["serial"] = "00000001"
        problems = self.problems_for(data)
        self.assertProblem(problems, r"^capture\.cameras\[0\]\.side: CAM1 must be LEFT")
        self.assertProblem(problems, r"^capture\.cameras: serial '00000001' is used by more than one")

    def test_flir_block(self):
        data = self.config_data()
        data["capture"]["cameras"][1]["driver"] = "flir"
        self.assertEqual(self.problems_for(data), ["capture.cameras[1].flir: missing"])

    def test_wheel_stand_counts(self):
        data = self.config_data()
        data["inspection"]["machine"]["wheel_stand_required_counts"] = {500: 3}
        self.assertProblem(self.problems_for(data),
                           r"^inspection\.machine\.wheel_stand_required_counts\[500\]: wheel number is above")

    def test_chmod_600_required(self):
        path = self.write_yaml(self.config_data(), mode=0o644)
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(path)
        self.assertEqual(len(ctx.exception.problems), 1)
        self.assertIn("chmod 600", ctx.exception.problems[0])

    def test_duplicate_key(self):
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            text = f.read().replace("  name: example_db", "  name: example_db\n  name: other_db")
        with self.assertRaises(sc.SiteConfigError) as ctx:
            sc.load_site_config(self.write_text(text))
        self.assertIn("duplicate key 'name'", ctx.exception.problems[0])

    def test_root_db_user_warning(self):
        data = self.config_data()
        data["db"]["user"] = "root"
        self.assertEqual(len(sc.load_site_config(self.write_yaml(data)).warnings), 1)

    def test_env_var_location(self):
        path = self.write_yaml(self.config_data(), "other.yaml")
        with mock.patch.dict(os.environ, {sc.CONFIG_ENV_VAR: path}):
            self.assertEqual(sc.load_site_config().source_path, path)


class D064Keys(_TempDirTest):                                         # P4 / 6g
    def test_defaults(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        self.assertIsNone(cfg.cameras[0].ip)
        self.assertEqual(cfg.plc.light_on_value, 1)

    def test_camera_ip_and_inverted_light(self):
        data = self.config_data()
        data["capture"]["cameras"][0]["ip"] = "192.168.1.102"
        data["plc"]["light_on_value"] = 0
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual(cfg.camera("CAM1").ip, "192.168.1.102")
        self.assertEqual(cfg.plc.light_on_value, 0)

    def test_bad_values(self):
        data = self.config_data()
        data["capture"]["cameras"][0]["ip"] = "192.168.1"
        data["plc"]["light_on_value"] = 2
        problems = self.problems_for(data)
        self.assertIn("capture.cameras[0].ip: must be an IPv4 address, got '192.168.1'", problems)
        self.assertProblem(problems, r"^plc\.light_on_value: must be one of 0, 1, got 2")
        data["plc"]["light_on_value"] = True
        self.assertProblem(self.problems_for(data), r"^plc\.light_on_value: must be an integer")

    def test_duplicate_camera_ip(self):
        data = self.config_data()
        for cam in data["capture"]["cameras"]:
            cam["ip"] = "192.168.1.102"
        self.assertProblem(self.problems_for(data), r"^capture\.cameras: ip '192\.168\.1\.102' is used by more than one")

    def test_tadipatri_reference_with_6g_keys(self):
        with open(os.path.join(REPO_ROOT, "docs", "sites", "tadipatri.site.reference.yaml"), encoding="utf-8") as f:
            text = f.read()
        text = (text.replace("# ip: 192.168.1.102", "ip: 192.168.1.102").replace("# ip: 192.168.1.103", "ip: 192.168.1.103")
                    .replace("# light_on_value: 0", "light_on_value: 0").replace('"<secret>"', '"x"'))
        data = yaml.safe_load(text)
        data["paths"]["root"] = self.root
        cfg = sc.load_site_config(self.write_yaml(data))
        self.assertEqual([c.ip for c in cfg.cameras], ["192.168.1.102", "192.168.1.103"])
        self.assertEqual(cfg.plc.light_on_value, 0)


class Secrets(_TempDirTest):
    def assertNoSecret(self, text):
        for value in list(SECRETS.values()) + ["s3cret"]:
            self.assertNotIn(value, text)

    def test_graph_secrets_load(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        self.assertEqual(cfg.graph.client_secret, SECRETS[("graph", "client_secret")])

    def test_wrong_type_does_not_print_the_value(self):
        data = self.config_data()
        data["graph"]["client_secret"] = 123456789
        problems = self.problems_for(data)
        self.assertIn("graph.client_secret: must be a string (quote it), got ***", problems)
        self.assertNotIn("123456789", "\n".join(problems))

    def test_repr_hides_secrets(self):
        cfg = sc.load_site_config(self.write_yaml(self.config_data()))
        self.assertNoSecret(repr(cfg))
        self.assertIn("example_db", repr(cfg))

    def test_redacted_copy_hides_graph_and_db(self):
        data = self.config_data()
        path = self.write_yaml(data)
        out = sc.redacted_copy(path)
        self.assertNoSecret(out)
        redacted = yaml.safe_load(out)
        self.assertEqual(redacted["graph"]["client_secret"], "***")
        self.assertEqual(redacted["db"]["password"], "***")
        self.assertEqual(redacted["capture"]["cameras"][0]["serial"], "00000001")

    def test_redacted_keeps_comments(self):
        out = sc.redacted_copy(self.write_text(open(EXAMPLE_YAML, encoding="utf-8").read()))
        self.assertIn("# ---------- programs ----------", out)
        self.assertEqual(out.count('"***"'), 5)       # db.user, db.password, 3 graph keys

    def test_legacy_env_vars_from_shared_graph(self):
        env = sc.legacy_env_vars(sc.load_site_config(self.write_yaml(self.config_data())))
        self.assertEqual(env["DPC_MS_CLIENT_SECRET"], SECRETS[("graph", "client_secret")])
        self.assertEqual(env["DPC_DB_PASS"], SECRETS[("db", "password")])
        self.assertEqual(env["DPC_DISK_REPORT_TO"], "alerts@example.com")
        self.assertEqual(env["DPC_SITE_NAME"], "Example DPC")
        self.assertNotIn("DPC_SMTP_PASSWORD", env)

    def test_broken_files_never_print_a_secret(self):
        breakers = [
            lambda d: d.update(schema_version=2),
            lambda d: d["mail"].update(sender="smtp"),
            lambda d: d["graph"].update(extra=SECRETS[("db", "password")]),
            lambda d: d.pop("graph"),
            lambda d: d["db"].update(port="x"),
        ]
        for breaker in breakers:
            data = self.config_data()
            breaker(data)
            with self.assertRaises(sc.SiteConfigError) as ctx:
                sc.load_site_config(self.write_yaml(data))
            self.assertNoSecret(str(ctx.exception))


class CommandLine(_TempDirTest):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sc.main(list(args))
        return code, out.getvalue()

    def test_ok_and_problems(self):
        self.assertEqual(self.run_cli(self.write_yaml(self.config_data())), (0, "OK\n"))
        data = self.config_data()
        del data["inspection"]["model"]
        code, out = self.run_cli(self.write_yaml(data))
        self.assertEqual(code, 1)
        self.assertIn("inspection.model: missing", out)


class ExampleHasNoRealValues(_TempDirTest):
    def test_placeholders_only(self):
        cfg = self.load_example()
        for cam in cfg.cameras:
            self.assertRegex(cam.serial, r"^0+\d$")
        self.assertTrue(cfg.plc.ip.startswith("192.0.2."))
        for address in cfg.mail.recipients + (cfg.mail.sender_address,):
            self.assertTrue(address.endswith("@example.com"))
        self.assertEqual(cfg.db.password, "CHANGE_ME")
        self.assertEqual({cfg.graph.tenant_id, cfg.graph.client_id, cfg.graph.client_secret}, {"CHANGE_ME"})


if __name__ == "__main__":
    unittest.main()
