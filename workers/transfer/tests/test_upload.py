"""Packing and resumable upload with a fake Graph: resume after a cut and after a restart,
expired sessions, checksum mismatch rejected, manifest last, staging deleted only at the end."""
import hashlib
import io
import json
import os
import tarfile
import unittest

from workers.transfer.tests import helpers as H
from workers.transfer.tests.test_cycle_capture import Base
import uploader

REMOTE = "DPC/data/tadipatri/2026-11-10_cycle1"


class Upload(Base):
    def setUp(self):
        super().setUp()
        saved = uploader.UPLOAD_FRAGMENT_BYTES
        uploader.UPLOAD_FRAGMENT_BYTES = 320 * 1024          # ~1 MB chunks -> several fragments each
        self.addCleanup(setattr, uploader, "UPLOAD_FRAGMENT_BYTES", saved)
        self.completed = []
        put = self.graph.put_file
        self.graph.put_file = lambda path, data, modified_ago_s=3600: (self.completed.append(path),
                                                                       put(path, data, modified_ago_s))
        for side in self.plate_dir:
            self.frames.write(self.plate_dir[side], 10, size=1000)

    def run_until(self, condition, limit=3000):
        for _ in range(limit):
            if condition(self.svc.upload_job):
                return
            self.clock.advance(1)
            self.svc.tick()
        self.fail(f"not reached: {self.svc.upload_job['state']} {self.svc.upload_job['message']}")

    def done(self, job):
        return job["state"] == "done"

    def assert_remote_matches(self, expected):
        manifest = json.loads(self.graph.files[f"{REMOTE}/manifest.json"])
        for side in ("LEFT", "RIGHT"):
            names = []
            for chunk in manifest["sides"][side]["chunks"]:
                data = self.graph.files[f"{REMOTE}/{side}/{chunk['name']}"]
                self.assertEqual(hashlib.sha256(data).hexdigest(), chunk["sha256"])
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    members = tar.getnames()
                self.assertEqual(len(members), chunk["frames"])
                if chunk["kind"] == "part":
                    names += members
            self.assertEqual(names, expected[side])
            self.assertIn(f"{REMOTE}/{side}/cycle.json", self.graph.files)

    # ---------------------------------------------------------------- the normal path
    def test_cycle_packed_uploaded_manifest_last_staging_deleted(self):
        expected = self.run_to_staged(frames_per_side=8)
        cycle_dir = self.cycle_dir()
        self.run_until(self.done)
        self.assert_remote_matches(expected)
        self.assertEqual(self.completed[-1], f"{REMOTE}/manifest.json", "manifest uploaded last")
        manifest = json.loads(self.graph.files[f"{REMOTE}/manifest.json"])
        parts = [c for c in manifest["sides"]["LEFT"]["chunks"] if c["kind"] == "part"]
        self.assertGreater(len(parts), 1, "chunk_mb 1 with 9 x 200 kB frames -> several chunks")
        self.assertTrue(all(c["size"] <= 1024 * 1024 + 20_000 for c in parts))
        plates = [c for c in manifest["sides"]["LEFT"]["chunks"] if c["kind"] == "plate"]
        self.assertEqual(sum(c["frames"] for c in plates), 5)
        self.assertFalse(os.path.exists(cycle_dir), "staging deleted after the confirmed upload")
        self.assertTrue(os.listdir(self.raw["LEFT"]), "RAW frames are never touched by the transfer")
        event = self.events.last("cycle_uploaded")
        self.assertEqual(event["remote"], REMOTE)
        self.assertEqual(self.svc.heartbeat_fields()["state"], "idle")
        self.assertIsNotNone(self.svc.heartbeat_fields()["last_upload_at"])

    def test_frames_are_on_disk_once_while_packed(self):
        self.run_to_staged(frames_per_side=8)
        self.svc.tick()                                    # one chunk packed
        left = os.path.join(self.cycle_dir(), "LEFT")
        in_tar = self.svc.upload_job["chunks"][0]
        remaining = os.listdir(os.path.join(left, "frames"))
        self.assertTrue(all(n > in_tar["last"] for n in remaining), "packed frames removed from frames/")

    # ---------------------------------------------------------------- interruptions
    def test_a_cut_connection_waits_then_continues_where_it_stopped(self):
        expected = self.run_to_staged(frames_per_side=8)
        self.graph.cut_at_fragment = 3
        self.run_until(lambda j: j["state"] == "waiting_retry")
        job = self.svc.upload_job
        self.assertEqual(job["attempts"], 1)
        self.assertIn("network", job["message"])
        sent_before = self.graph.bytes_sent
        self.clock.advance(30)
        self.svc.tick()
        self.assertEqual(self.graph.bytes_sent, sent_before, "nothing sent before the retry time")
        self.run_until(self.done)
        total = sum(len(d) for d in self.graph.files.values())
        self.assertEqual(self.graph.bytes_sent, total, "no byte sent twice")
        self.assert_remote_matches(expected)

    def test_a_restart_mid_file_asks_graph_and_continues(self):
        expected = self.run_to_staged(frames_per_side=8)
        self.run_until(lambda j: j["phase"] == "uploading" and any(u["offset"] > 0 and u["status"] != "done"
                                                                   for u in j["uploads"]))
        self.svc = self.new_service()                        # program restarted
        self.run_until(self.done)
        total = sum(len(d) for d in self.graph.files.values())
        self.assertEqual(self.graph.bytes_sent, total)
        self.assert_remote_matches(expected)

    def test_an_expired_session_restarts_only_that_file(self):
        expected = self.run_to_staged(frames_per_side=8)
        self.run_until(lambda j: j["phase"] == "uploading" and j["uploads"][0]["status"] == "done"
                       and j["uploads"][1]["offset"] > 0)
        first_file = self.completed[0]
        self.graph.expire_next = True
        self.run_until(self.done)
        self.assertEqual(self.completed.count(first_file), 1, "finished files are not sent again")
        self.assert_remote_matches(expected)

    def test_already_on_onedrive_is_not_sent_again(self):
        self.run_to_staged(frames_per_side=8)
        self.run_until(lambda j: j["phase"] == "uploading" and j["uploads"][0]["status"] == "done")
        job = self.svc.upload_job
        job["uploads"][0].update(status="pending", session_url=None, offset=0)   # state lost after the upload
        sent = self.graph.bytes_sent
        self.svc.tick()
        self.assertEqual(job["uploads"][0]["status"], "done")
        self.assertEqual(self.graph.bytes_sent, sent)

    # ---------------------------------------------------------------- checksums
    def test_a_checksum_mismatch_is_rejected_and_sent_again(self):
        expected = self.run_to_staged(frames_per_side=8)
        self.graph.corrupt_uploads = 1
        self.run_until(self.done)
        self.assertEqual(len(self.graph.deleted), 1, "the corrupt remote file was deleted")
        self.assert_remote_matches(expected)

    def test_three_mismatches_fail_the_job_and_a_click_continues(self):
        expected = self.run_to_staged(frames_per_side=8)
        self.graph.corrupt_uploads = 3
        self.run_until(lambda j: j["state"] == "failed")
        job = self.svc.upload_job
        self.assertIn("checksum mismatch", job["message"])
        self.assertIn("checksum", self.events.last("upload_failed")["reason"])
        self.assertTrue(os.path.isdir(self.cycle_dir()), "staging kept for the next click")
        self.click()
        self.run_until(self.done)
        self.assert_remote_matches(expected)

    # ---------------------------------------------------------------- names
    def test_a_cycle_already_complete_on_onedrive_is_not_overwritten(self):
        self.graph.put_file(f"{REMOTE}/manifest.json", b"{}")
        self.run_to_staged(frames_per_side=8)
        self.run_until(self.done)
        self.assertEqual(self.graph.files[f"{REMOTE}/manifest.json"], b"{}")
        self.assertEqual(self.svc.upload_job["cycle"], "2026-11-10_cycle2")
        self.assertIn("DPC/data/tadipatri/2026-11-10_cycle2/manifest.json", self.graph.files)


class Limiter(unittest.TestCase):
    def test_average_rate(self):
        slept = []
        clock = H.Clock(0)
        limiter = uploader.RateLimiter(8.0, clock=clock, sleep=lambda s: (slept.append(s), clock.advance(s)))
        for _ in range(4):
            limiter.wait(1_000_000)                     # 8 Mbit at 8 Mbit/s = 1 s each
        self.assertAlmostEqual(sum(slept), 3.0)        # the first goes at once
        self.assertEqual(clock.now, 3.0)

    def test_unlimited(self):
        limiter = uploader.RateLimiter(None, sleep=lambda s: self.fail("must not sleep"))
        limiter.wait(10 ** 9)


if __name__ == "__main__":
    unittest.main()
