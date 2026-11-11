"""Model update on click with a fake Graph: newest dated zip of THIS site, resumable download,
checksum, zip rules, packaging with make_model_package, `ready`, never `current`."""
import io
import json
import os
import unittest
import zipfile

from workers.transfer.tests import helpers as H
from transfer import TransferService
from common.config import model_package as mp
import model_update

SITE = "DPC/models/tadipatri/dpc_maskrcnn"
CLASSES = ["nut_bolt", "bucket_clip", "wheel", "plate"]
WEIGHTS = os.urandom(700_000)                 # random: does not compress, so several download ranges


def model_zip(files=None, folder="", weights=WEIGHTS, classes=None):
    """A zip as the training PC makes it: model_final.pth + an index-map class file."""
    if files is None:
        files = {"model_final.pth": weights,
                 "classes.json": json.dumps(classes or {str(i): n for i, n in enumerate(CLASSES)}).encode()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(f"{folder}{name}", data)
    return buffer.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = H.temp_root(self)
        self.s = H.make_settings(self.root)
        self.clock = H.Clock()
        self.events = H.Events()
        self.graph = H.FakeGraph(self.clock)
        self.graph.ensure_folder(SITE)
        saved = model_update.DOWNLOAD_BLOCK_BYTES
        model_update.DOWNLOAD_BLOCK_BYTES = 200_000                     # several blocks per zip
        self.addCleanup(setattr, model_update, "DOWNLOAD_BLOCK_BYTES", saved)
        self.svc = self.new_service()

    def new_service(self):
        return TransferService(self.s, H.LOG, self.graph, H.FakePlates(), self.events, clock=self.clock)

    def click(self):
        open(os.path.join(self.s.run, "transfer.model_update"), "w").close()

    def run_until(self, condition, limit=500):
        for _ in range(limit):                      # tick first: a fresh click is seen before the check
            self.clock.advance(1)
            self.svc.tick()
            job = self.svc.model_job
            if job and condition(job):
                return job
        self.fail(f"not reached: {self.svc.model_job}")

    def finished(self, job):
        return job["state"] in ("done", "failed")

    def update(self):
        self.click()
        return self.run_until(self.finished)

    def package(self, version, name="dpc_maskrcnn"):
        return os.path.join(self.s.models, name, version)


class Update(Base):
    def test_new_zip_is_downloaded_packaged_and_marked_ready(self):
        self.graph.put_file(f"{SITE}/2026.10.29.zip", model_zip())
        self.graph.put_file(f"{SITE}/2026-11-20.zip", model_zip(folder="2026-11-20/"))    # zip with a folder inside
        job = self.update()
        self.assertEqual((job["state"], job["result"]), ("done", "downloaded"))
        package = mp.verify_package(self.package("2026.11.20"), "dpc_maskrcnn", "2026.11.20")
        self.assertEqual(list(package.classes), CLASSES, "index-map order kept")
        self.assertTrue(os.path.isfile(os.path.join(self.package("2026.11.20"), "ready")))
        self.assertFalse(os.path.exists(self.package("2026.10.29")), "only the newest")
        self.assertIsNone(mp.read_current(os.path.join(self.s.models, "dpc_maskrcnn")), "never sets current")
        self.assertEqual(self.events.last("model_downloaded"), {"name": "dpc_maskrcnn", "version": "2026.11.20"})
        self.assertEqual(self.events.last("model_update_done")["result"], "downloaded")
        self.assertFalse(os.path.exists(os.path.join(self.s.downloads, "dpc_maskrcnn")), "download cleaned up")
        self.assertGreater(len(self.graph.download_calls), 1, "downloaded in ranges")

    def test_only_this_sites_folder_is_looked_at(self):
        self.graph.put_file("DPC/models/baga/dpc_maskrcnn/2027.01.01.zip", model_zip())
        job = self.update()
        self.assertEqual(job["result"], "up_to_date")
        self.assertEqual(self.graph.listed, [SITE])

    def test_nothing_newer_is_up_to_date(self):
        self.graph.put_file(f"{SITE}/2026.11.20.zip", model_zip())
        self.assertEqual(self.update()["result"], "downloaded")
        job = self.update()
        self.assertEqual(job["result"], "up_to_date")
        self.assertEqual(self.events.last("model_update_done")["result"], "up_to_date")

    def test_names_that_are_not_year_first_dates_are_ignored(self):
        for name in ("old.zip", "20-11-2026.zip", "2026.13.01.zip", "2026.11.20.pth", "notes.txt"):
            self.graph.put_file(f"{SITE}/{name}", model_zip())
        self.assertEqual(self.update()["result"], "up_to_date")

    def test_a_zip_still_fresh_waits_until_its_upload_looks_finished(self):
        self.graph.put_file(f"{SITE}/2026.11.20.zip", model_zip(), modified_ago_s=60)
        self.click()
        self.clock.advance(1)
        self.svc.tick()
        job = self.svc.model_job
        self.assertEqual(job["phase"], "checking")
        self.assertIn("waiting until its upload looks finished", job["message"])
        self.assertEqual(self.graph.download_calls, [])
        job = self.run_until(self.finished, limit=model_update.STABLE_S + 50)
        self.assertEqual(job["result"], "downloaded")

    def test_a_cut_download_continues_from_the_last_byte(self):
        data = model_zip()
        self.graph.put_file(f"{SITE}/2026.11.20.zip", data)
        self.graph.cut_download_at = 3
        self.click()
        job = self.run_until(lambda j: j["state"] == "waiting_retry")
        self.assertIn("network", job["message"])
        job = self.run_until(self.finished, limit=200)
        self.assertEqual(job["result"], "downloaded")
        starts = [start for _, start, _ in self.graph.download_calls]
        self.assertEqual(starts[2], starts[3], "the cut range is asked again, not the file from 0")
        self.assertEqual(sum(e - s + 1 for _, s, e in self.graph.download_calls[3:]) + starts[2], len(data))

    def test_per_side_models_are_each_updated(self):
        self.s.models_in_use = {"dpc_maskrcnn_left": ["LEFT"], "dpc_maskrcnn_right": ["RIGHT"]}
        for name in self.s.models_in_use:
            self.graph.put_file(f"DPC/models/tadipatri/{name}/2026.11.20.zip", model_zip())
        job = self.update()
        self.assertEqual(job["result"], "downloaded")
        for name in self.s.models_in_use:
            self.assertTrue(os.path.isfile(os.path.join(self.package("2026.11.20", name), "ready")))


class Refused(Base):
    def assert_refused(self, zip_bytes, reason):
        self.graph.put_file(f"{SITE}/2026.11.20.zip", zip_bytes)
        job = self.update()
        self.assertEqual(job["state"], "failed")
        self.assertIn(reason, job["message"])
        self.assertIn(reason, self.events.last("model_download_failed")["reason"])
        self.assertFalse(os.path.exists(self.package("2026.11.20")), "nothing installed")
        return job

    def test_two_pth_files(self):
        self.assert_refused(model_zip(files={"model_final.pth": b"a", "model_0004999.pth": b"b",
                                             "classes.json": b'{"0": "wheel"}'}), "exactly one .pth")

    def test_no_class_file(self):
        self.assert_refused(model_zip(files={"model_final.pth": b"a"}), "exactly one class .json")

    def test_class_file_with_a_gap(self):
        self.assert_refused(model_zip(classes={"0": "a", "2": "b"}), "index [1] missing")

    def test_class_file_with_a_duplicate(self):
        self.assert_refused(model_zip(classes={"0": "a", "1": "a"}), "more than once")

    def test_class_file_with_bad_keys(self):
        self.assert_refused(model_zip(classes={"0": "a", "x": "b"}), "keys must be 0, 1, 2")

    def test_not_a_zip(self):
        self.assert_refused(b"this is not a zip", "not a valid zip")

    def test_unsafe_path(self):
        self.assert_refused(model_zip(files={"../model_final.pth": b"a", "classes.json": b'{"0": "w"}'}),
                            "unsafe path")

    def test_a_failed_version_is_skipped_until_the_zip_is_replaced(self):
        self.assert_refused(model_zip(files={"model_final.pth": b"a"}), "exactly one class .json")
        self.click()
        job = self.run_until(lambda j: j["state"] == "done")
        self.assertEqual(job["result"], "up_to_date")
        self.assertIn("failed before", job["message"])
        self.graph.put_file(f"{SITE}/2026.11.20.zip", model_zip())      # corrected: new eTag
        self.click()
        job = self.run_until(lambda j: j["state"] == "done" and j["result"] == "downloaded")
        self.assertTrue(os.path.isfile(os.path.join(self.package("2026.11.20"), "ready")))

    def test_checksum_mismatch_downloads_again_then_rejects(self):
        self.graph.put_file(f"{SITE}/2026.11.20.zip", model_zip())
        real = self.graph.download_range
        self.graph.download_range = lambda p, s, e: b"X" + real(p, s, e)[1:] if s == 0 else real(p, s, e)
        job = self.update()
        self.assertEqual(job["state"], "failed")
        self.assertIn("checksum mismatch after 2 downloads", job["message"])

    def test_not_enough_disk_stops_before_downloading(self):
        self.graph.put_file(f"{SITE}/2026.11.20.zip", model_zip())
        real = model_update.shutil.disk_usage
        model_update.shutil.disk_usage = lambda p: type("U", (), {"free": 10})()
        self.addCleanup(setattr, model_update.shutil, "disk_usage", real)
        job = self.update()
        self.assertIn("not enough free disk", job["message"])
        self.assertEqual(self.graph.download_calls, [])

    def test_failed_then_clicked_again_continues_the_download(self):
        self.graph.put_file(f"{SITE}/2026.11.20.zip", model_zip())
        self.click()
        self.run_until(lambda j: j["phase"] == "downloading" and j["bytes_done"] > 0)
        job = self.svc.model_job
        job["phase"] = "downloading"
        job["state"] = "failed"                     # e.g. stopped by a permission error, now fixed
        calls = len(self.graph.download_calls)
        self.click()
        job = self.run_until(lambda j: j["state"] == "done")
        self.assertEqual(job["result"], "downloaded")
        self.assertNotEqual(self.graph.download_calls[calls][1], 0, "continued, not from byte 0")


class Names(unittest.TestCase):
    def test_version_names(self):
        self.assertEqual(model_update.version_from_zip_name("2026-11-20.zip"), "2026.11.20")
        self.assertEqual(model_update.version_from_zip_name("2026_11_20b.ZIP"), "2026.11.20b")
        for bad in ("20-11-2026.zip", "2026.11.20.tar", "2026.02.40.zip", "model.zip"):
            self.assertIsNone(model_update.version_from_zip_name(bad), bad)

    def test_coco_class_file(self):
        root = H.temp_root(self)
        path = os.path.join(root, "train.json")
        with open(path, "w") as f:
            json.dump({"categories": [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}]}, f)
        self.assertEqual(model_update.classes_from_class_file(path), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
