"""Tests for common/config/setup_draft.py (STATUS S2, D-103 wizard draft).

Run from the repo root:
    python3 -m unittest tests.unit.test_setup_draft -v
"""

import json
import os
import stat
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common.config import setup_draft as sd  # noqa: E402
from common.config import site_writer as sw  # noqa: E402
from common.config import workers_file as wf  # noqa: E402
from common.config.site_config import load_site_config  # noqa: E402

SIZES = {"wheel_w": 530, "wheel_h": 495, "bucket_clip_w": 300, "chain_w": 880, "half_bucket_clip_w": 150}
SECRETS = {"db.user": "dpc_app", "db.password": "s3cret-db-Dr1",
           "graph.tenant_id": "t", "graph.client_id": "c", "graph.client_secret": "s3cret-graph-Dr2"}


def answers(**changes):
    base = {
        "site": {"id": "baga", "name": "Baga DPC"},
        "database": {"host": "localhost", "port": 3306, "name": "baga_dpc"},
        "cameras": [
            {"id": "CAM1", "side": "LEFT", "driver": "basler", "serial": "40112233", "ip": "192.168.1.102",
             "fps": 4.0, "exposure_us": 1500, "rotate": 0, "motion": "ltr"},
            {"id": "CAM2", "side": "RIGHT", "driver": "basler", "serial": "40112234", "fps": 4.0,
             "exposure_us": 2000, "rotate": 0, "motion": "rtl"}],
        "plc": {"ip": "192.168.1.5", "light_register": 4109, "cleaning_register": 4106,
                "conveyor_status_register": 4098},
        "machine": {"total_wheel_count": 156},
        "models": {"LEFT": "dpc_maskrcnn", "RIGHT": "dpc_maskrcnn"},
        "object_sizes": {"LEFT": SIZES, "RIGHT": dict(SIZES, wheel_w=560)},
        "mail": {"sender": "graph", "sender_address": "reports@example.com", "project_name": "Baga_dpc",
                 "recipients": ["alerts@example.com"]},
        "transfer": {"drive": {"site": "contoso.sharepoint.com:/sites/DPC"}, "upload": True},
    }
    base.update(changes)
    return base


class _Root(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")

    def tearDown(self):
        self._tmp.cleanup()


class DraftFile(_Root):
    def test_save_and_load(self):
        path = sd.save_draft(self.root, answers(), step="machine", secrets_set=["db.password"])
        self.assertEqual(path, os.path.join(self.root, "data", "state", "ui", "setup_draft.json"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = sd.load_draft(self.root)
        self.assertEqual((doc["format"], doc["step"], doc["secrets_set"]), (1, "machine", ["db.password"]))
        self.assertEqual(doc["answers"]["machine"], {"total_wheel_count": 156})

    def test_no_draft_yet(self):
        self.assertIsNone(sd.load_draft(self.root))

    def test_secrets_are_refused(self):
        for bad in ({"database": {"name": "x", "password": "p"}}, {"database": {"name": "x", "user": "root"}},
                    {"graph": {"client_secret": "s"}}, {"mail": {"smtp": {"password": "p"}}}):
            with self.assertRaises(sd.DraftError) as ctx:
                sd.save_draft(self.root, answers(**bad))
            self.assertIn("a secret is never stored in the draft", str(ctx.exception))
        self.assertIsNone(sd.load_draft(self.root))
        sd.save_draft(self.root, answers(transfer={"drive": {"user": "dpc@example.com"}, "upload": False}))
        with self.assertRaises(sd.DraftError):
            sd.save_draft(self.root, answers(), secrets_set=["db.pasword"])

    def test_broken_file(self):
        os.makedirs(os.path.dirname(sd.draft_path(self.root)))
        with open(sd.draft_path(self.root), "w") as f:
            f.write("{")
        with self.assertRaises(sd.DraftError):
            sd.load_draft(self.root)

    def test_missing_answers(self):
        partial = {"site": {"id": "baga"}, "cameras": [{"id": "CAM1", "side": "LEFT"}],
                   "mail": {"sender": "graph"}, "transfer": {"upload": True}}
        missing = sd.missing_answers(partial)
        for item in ("database", "plc", "machine", "models", "object_sizes", "site.name", "cameras[0].serial",
                     "cameras[0].motion", "models.LEFT", "object_sizes.LEFT", "mail.sender_address",
                     "transfer.drive"):
            self.assertIn(item, missing)
        self.assertEqual(sd.missing_answers(answers()), [])


class ToSiteYaml(_Root):
    def build(self, ans=None, secrets=None):
        return sd.site_from_draft({"answers": ans or answers()}, secrets or SECRETS, root=self.root)

    def write(self, data):
        path = os.path.join(self.root, "config", "site.yaml")
        sw.write_site(data, path)
        return load_site_config(path)

    def test_complete_draft_gives_a_valid_site_yaml(self):           # S2 done when
        cfg = self.write(self.build())
        self.assertEqual((cfg.site.id, cfg.db.name, cfg.db.password), ("baga", "baga_dpc", "s3cret-db-Dr1"))
        self.assertEqual((cfg.inspection.machine.total_wheel_count, cfg.inspection.machine.expected_cycle_total),
                         (156, 156))                                   # any wheel count; expected = count
        self.assertEqual((cfg.inspection.sides["LEFT"].motion, cfg.inspection.sides["RIGHT"].motion), ("ltr", "rtl"))
        self.assertEqual(cfg.inspection.sides["RIGHT"].object_sizes.wheel_w, 560)
        self.assertEqual(cfg.camera("CAM1").ip, "192.168.1.102")
        self.assertIsNone(cfg.camera("CAM2").ip)
        self.assertEqual(cfg.transfer.drive, {"site": "contoso.sharepoint.com:/sites/DPC"})
        self.assertIsNotNone(cfg.transfer.upload)
        self.assertEqual(cfg.mail.sender, "graph")
        self.assertEqual(cfg.graph.client_secret, "s3cret-graph-Dr2")

    def test_expected_cycle_total_kept_when_given(self):
        cfg = self.write(self.build(answers(machine={"total_wheel_count": 170, "expected_cycle_total": 168})))
        self.assertEqual(cfg.inspection.machine.expected_cycle_total, 168)

    def test_per_side_models(self):
        cfg = self.write(self.build(answers(models={"LEFT": "dpc_left", "RIGHT": "dpc_right"})))
        self.assertEqual(cfg.inspection.model.name, "dpc_left")
        self.assertEqual(cfg.inspection.sides["RIGHT"].model.name, "dpc_right")
        self.assertEqual(cfg.inspection.sides["LEFT"].model.name, "dpc_left")

    def test_no_mail_no_transfer_needs_no_graph(self):
        ans = answers(mail=None, transfer=None)
        secrets = {"db.user": "dpc_app", "db.password": "s3cret-db-Dr1"}
        self.assertEqual(sd.secrets_needed(ans), ["db.user", "db.password"])
        data = self.build(ans, secrets)
        self.assertNotIn("mail", data)
        self.assertNotIn("graph", data)
        self.assertNotIn("transfer", data)
        cfg = self.write(data)
        self.assertIsNone(cfg.mail)
        mail = next(w for w in wf.generate_workers(cfg)["workers"] if w["id"] == "mail")
        self.assertFalse(mail["enabled"])

    def test_transfer_download_only_and_smtp_mail(self):
        ans = answers(transfer={"drive": {"user": "dpc@example.com"}, "upload": False},
                      mail={"sender": "smtp", "sender_address": "r@example.com", "project_name": "B",
                            "recipients": ["a@example.com"], "smtp": {"host": "smtp.example.com", "port": 587}})
        self.assertIn("mail.smtp.password", sd.secrets_needed(ans))
        cfg = self.write(self.build(ans, dict(SECRETS, **{"mail.smtp.password": "s3cret-smtp"})))
        self.assertIsNone(cfg.transfer.upload)
        self.assertEqual((cfg.mail.smtp.host, cfg.mail.smtp.password), ("smtp.example.com", "s3cret-smtp"))

    def test_incomplete_draft_or_missing_secret(self):
        with self.assertRaises(sd.DraftError) as ctx:
            self.build(answers(machine={}), {"db.user": "u"})
        problems = ctx.exception.problems
        self.assertIn("machine", problems)
        self.assertIn("secret db.password not given", problems)
        self.assertIn("secret graph.client_secret not given", problems)
        self.assertNotIn("s3cret", json.dumps(problems))

    def test_bad_value_is_caught_by_the_loader(self):
        data = self.build(answers(plc={"ip": "192.168.1", "light_register": 4109, "cleaning_register": 4106,
                                       "conveyor_status_register": 4098}))
        with self.assertRaises(Exception) as ctx:
            self.write(data)
        self.assertIn("plc.ip: must be an IPv4 address", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
