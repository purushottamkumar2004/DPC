"""Tests for common/config/site_writer.py (STATUS S2, D-103 Apply).

Run from the repo root:
    python3 -m unittest tests.unit.test_site_writer -v
"""

import copy
import os
import stat
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from common.config import render as rd  # noqa: E402
from common.config import site_writer as sw  # noqa: E402
from common.config.site_config import SiteConfigError, load_site_config  # noqa: E402

EXAMPLE_YAML = os.path.join(REPO_ROOT, "config", "examples", "site.example.yaml")
DB_PASSWORD = "s3cret-db-Sw1"


class _Site(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "INSIGHTZZ")
        self.path = os.path.join(self.root, "config", "site.yaml")
        with open(EXAMPLE_YAML, encoding="utf-8") as f:
            self.data = yaml.safe_load(f)
        self.data["paths"]["root"] = self.root
        self.data["db"]["password"] = DB_PASSWORD

    def tearDown(self):
        self._tmp.cleanup()

    def edited(self, edit):
        data = copy.deepcopy(self.data)
        edit(data)
        return data


class Write(_Site):
    def test_first_write(self):
        result = sw.write_site(self.data, self.path)
        self.assertTrue(result.written)
        self.assertIsNone(result.backup)                         # nothing to back up yet
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(load_site_config(self.path).site.id, "example_site")
        self.assertEqual(set(result.workers), {w for w in rd.worker_configs(load_site_config(self.path))})
        self.assertTrue(all(s == "written" for s in result.workers.values()))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.startswith(".")], [])

    def test_invalid_dict_changes_nothing(self):
        sw.write_site(self.data, self.path)
        with open(self.path, "rb") as f:
            before = f.read()
        bad = self.edited(lambda d: d["plc"].update(ip="not-an-ip"))
        with self.assertRaises(SiteConfigError) as ctx:
            sw.write_site(bad, self.path)
        self.assertEqual(ctx.exception.source, self.path)        # names the real file, not the temp file
        self.assertIn("plc.ip: must be an IPv4 address, got 'not-an-ip'", ctx.exception.problems)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(sw.list_backups(self.path), [])
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.startswith(".")], [])

    def test_backup_and_atomic_replace(self):
        sw.write_site(self.data, self.path)
        with open(self.path, encoding="utf-8") as f:
            first = f.read()
        result = sw.write_site(self.edited(lambda d: d["inspection"].update(threshold=0.6)), self.path)
        self.assertEqual(result.backup, sw.list_backups(self.path)[-1])
        self.assertEqual(stat.S_IMODE(os.stat(result.backup).st_mode), 0o600)
        with open(result.backup, encoding="utf-8") as f:
            self.assertEqual(f.read(), first)
        self.assertEqual(load_site_config(self.path).inspection.threshold, 0.6)

    def test_only_the_last_20_backups_are_kept(self):
        sw.write_site(self.data, self.path)
        for i in range(23):
            sw.write_site(self.edited(lambda d, i=i: d["cleanup"].update(plate_days=i + 1)), self.path)
        backups = sw.list_backups(self.path)
        self.assertEqual(len(backups), 20)
        with open(backups[-1], encoding="utf-8") as f:              # the newest backup = the 22nd write
            self.assertEqual(yaml.safe_load(f)["cleanup"]["plate_days"], 22)

    def test_dry_run_writes_nothing(self):
        result = sw.write_site(self.data, self.path, dry_run=True)
        self.assertFalse(result.written)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("inspection_left", result.workers)


class DiffAndWorkers(_Site):
    def setUp(self):
        super().setUp()
        sw.write_site(self.data, self.path)
        rd.render(self.path)                                     # the site as it runs

    def test_changed_keys_and_affected_workers(self):
        new = self.edited(lambda d: (d["inspection"].update(threshold=0.6),
                                     d["capture"]["cameras"][1].update(exposure_us=1800)))
        result = sw.write_site(new, self.path, dry_run=True)
        self.assertEqual([c.text() for c in result.changes],
                         ["capture.cameras[1].exposure_us: 2000 -> 1800", "inspection.threshold: 0.5 -> 0.6"])
        self.assertEqual(set(result.workers), {"frame_capture_cam2", "inspection_left", "inspection_right",
                                               "transfer"})          # transfer.json holds camera settings

    def test_secrets_shown_as_changed_only(self):
        new = self.edited(lambda d: (d["db"].update(password="s3cret-NEW-Sw2"),
                                     d["graph"].update(client_secret="s3cret-graph-Sw3")))
        result = sw.write_site(new, self.path, dry_run=True)
        texts = [c.text() for c in result.changes]
        self.assertEqual(texts, ["db.password: changed", "graph.client_secret: changed"])
        self.assertTrue(all(c.secret and c.old is None and c.new is None for c in result.changes))
        self.assertNotIn("s3cret", repr(result))

    def test_added_and_removed(self):
        new = self.edited(lambda d: (d.pop("transfer"), d["mqtt"].update(user="dpc_broker", password="s3cret-mq")))
        texts = [c.text() for c in sw.write_site(new, self.path, dry_run=True).changes]
        self.assertIn("mqtt.user: changed", texts)                # user is a secret path too
        self.assertIn("mqtt.password: changed", texts)
        self.assertTrue(any(t.startswith("transfer: removed") for t in texts))
        result = sw.write_site(new, self.path, dry_run=True)
        self.assertEqual(result.workers["transfer"], "removed")

    def test_no_change(self):
        result = sw.write_site(self.data, self.path, dry_run=True)
        self.assertEqual((result.changes, result.workers), ([], {}))

    def test_diff_site_lists(self):
        changes = sw.diff_site({"a": [1, 2]}, {"a": [1, 2, 3]})
        self.assertEqual([c.text() for c in changes], ["a: [1, 2] -> [1, 2, 3]"])


if __name__ == "__main__":
    unittest.main()
