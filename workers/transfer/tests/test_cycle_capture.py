"""Cycle boundaries from fake plate heartbeats (F76 fields), staging and its cap."""
import json
import os
import unittest

from workers.transfer.tests import helpers as H
from transfer import TransferService
import cycle_capture


class Base(unittest.TestCase):
    def setUp(self):
        self.root = H.temp_root(self)
        self.s = H.make_settings(self.root)
        self.clock = H.Clock()
        self.events = H.Events()
        self.plates = H.FakePlates()
        self.graph = H.FakeGraph(self.clock)
        self.frames = H.FrameMaker()
        self.svc = self.new_service()
        self.raw = {side.side: side.raw_images for side in self.s.sides}
        self.plate_dir = {side.side: side.plate for side in self.s.sides}
        self.pid = {"LEFT": 100, "RIGHT": 200}
        self.seen = {"LEFT": 5, "RIGHT": 9}

    def new_service(self):
        return TransferService(self.s, H.LOG, self.graph, self.plates, self.events, clock=self.clock,
                               limiter=None)

    def tick(self, n=1):
        for _ in range(n):
            self.clock.advance(cycle_capture.SCAN_INTERVAL_S + 1)
            self.svc.tick()

    def click(self):
        open(os.path.join(self.s.run, "transfer.upload_cycle"), "w").close()

    def heartbeat(self, side, frame=None, wheels=None):
        self.plates.set(side, self.pid[side], self.seen[side], frame, wheels)

    def new_plate(self, side):
        """A plate frame arrives in RAW and inspection reports it."""
        frame = self.frames.write(self.raw[side])[0]
        self.seen[side] += 1
        self.heartbeat(side, frame, wheels=190)
        return frame

    def cycle_dir(self):
        return os.path.join(self.s.staging, self.svc.upload_job["cycle"])

    def run_to_staged(self, frames_per_side=6):
        """Click, plate, frames, plate -> both sides closed. Returns {side: [expected frames]}."""
        for side in self.raw:
            self.heartbeat(side, "2026_11_10_08_00_00_000000.jpg")
        self.click()
        self.tick()
        for side in self.raw:
            self.frames.write(self.raw[side], 2)          # before the next plate: not in the cycle
        expected = {}
        for side in self.raw:
            expected[side] = [self.new_plate(side)]
        self.tick()
        for side in self.raw:
            expected[side] += self.frames.write(self.raw[side], frames_per_side)
        self.tick()
        for side in self.raw:
            self.frames.write(self.raw[side])             # the closing plate frame ...
            self.frames.write(self.raw[side], 2)          # ... and frames of the next cycle,
        self.tick()                                       # linked before inspection reports the plate
        for side in self.raw:
            self.seen[side] += 1
            names = sorted(n for n in os.listdir(self.raw[side]) if n > expected[side][-1])
            self.heartbeat(side, names[0], wheels=190)
        self.tick()
        return expected


class Boundaries(Base):
    def test_one_cycle_plate_to_plate_both_sides(self):
        for side in self.plate_dir:
            self.frames.write(self.plate_dir[side], 40, size=1000)
        expected = self.run_to_staged()
        job = self.svc.upload_job
        self.assertEqual(job["phase"], "packing")
        self.assertRegex(job["cycle"], r"^2026-11-10_cycle1$")
        for side in ("LEFT", "RIGHT"):
            staged = sorted(os.listdir(os.path.join(self.cycle_dir(), side, "frames")))
            self.assertEqual(staged, expected[side], f"{side}: from the start plate, up to the end plate")
            plate = sorted(os.listdir(os.path.join(self.cycle_dir(), side, "PLATE")))
            self.assertEqual(len(plate), 5)
            all_plate = sorted(os.listdir(self.plate_dir[side]))
            self.assertEqual(plate, [all_plate[i * 8] for i in range(5)], "spread over the folder")
            with open(os.path.join(self.cycle_dir(), side, "cycle.json")) as f:
                info = json.load(f)
            self.assertEqual(info["frames"], len(expected[side]))
            self.assertEqual(info["start_frame"], expected[side][0])
            self.assertEqual(info["camera"]["serial"], "111" if side == "LEFT" else "222")
            self.assertEqual(info["cycle_wheels"], 190)
            self.assertEqual(info["total_wheel_count"], 190)
            self.assertEqual(info["model"]["name"], "dpc_maskrcnn")
            # hard links: the staging copy is the same file (no extra disk)
            first = expected[side][0]
            self.assertTrue(os.path.samefile(os.path.join(self.raw[side], first),
                                             os.path.join(self.cycle_dir(), side, "frames", first)))
        staged = self.events.last("cycle_staged")
        self.assertEqual(staged["frames"], {"LEFT": 7, "RIGHT": 7})

    def test_the_plate_already_seen_at_the_click_does_not_start_the_cycle(self):
        for side in self.raw:
            self.heartbeat(side, self.frames.write(self.raw[side])[0])
        self.click()
        self.tick(3)
        self.assertEqual(self.svc.upload_job["phase"], "waiting_plate")
        self.assertIn("waiting for the next plate", self.svc.upload_job["message"])

    def test_without_plate_signal_the_job_says_why(self):
        self.click()
        self.tick()
        self.assertIn("plate signal", self.svc.upload_job["message"])
        self.assertIn("F76", self.svc.upload_job["message"])

    def test_inspection_restart_starts_that_side_again(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        for side in self.raw:
            self.new_plate(side)
        self.tick()
        self.frames.write(self.raw["LEFT"], 3)
        self.tick()
        self.pid["LEFT"], self.seen["LEFT"] = 101, 0               # LEFT inspection restarted
        self.heartbeat("LEFT", None)
        self.tick()
        job = self.svc.upload_job
        self.assertEqual(job["sides"]["LEFT"]["phase"], "armed")
        self.assertIn("inspection restarted", job["last_restart"])
        self.assertFalse(os.path.exists(os.path.join(self.cycle_dir(), "LEFT")), "partial side deleted")
        self.assertEqual(job["sides"]["RIGHT"]["phase"], "collecting", "the other side is not touched")
        start = self.new_plate("LEFT")                              # LEFT starts again at its next plate
        self.tick()
        self.assertEqual(job["sides"]["LEFT"]["start_frame"], start)

    def test_a_missed_revolution_starts_the_side_again(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        for side in self.raw:
            self.new_plate(side)
        self.tick()
        self.seen["RIGHT"] += 2
        self.heartbeat("RIGHT", self.frames.write(self.raw["RIGHT"])[0])
        self.tick()
        self.assertEqual(self.svc.upload_job["sides"]["RIGHT"]["phase"], "armed")
        self.assertIn("revolution", self.svc.upload_job["last_restart"])

    def test_a_cycle_open_too_long_starts_again(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        for side in self.raw:
            self.new_plate(side)
        self.tick()
        self.clock.advance(cycle_capture.MAX_CYCLE_S + 1)
        self.tick()
        self.assertEqual(self.svc.upload_job["sides"]["LEFT"]["phase"], "armed")

    def test_transfer_restart_mid_cycle_closes_at_the_plate_it_missed(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        expected = {side: [self.new_plate(side)] for side in self.raw}
        self.tick()
        for side in self.raw:
            expected[side] += self.frames.write(self.raw[side], 3)
        self.tick()
        # transfer stops; meanwhile 4 more frames and the closing plate arrive
        for side in self.raw:
            expected[side] += self.frames.write(self.raw[side], 4)
        ends = {side: self.new_plate(side) for side in self.raw}
        self.svc = self.new_service()                  # restart: state from disk, plates via heartbeat
        self.tick()
        job = self.svc.upload_job
        self.assertEqual(job["phase"], "packing")
        for side in self.raw:
            staged = sorted(os.listdir(os.path.join(self.cycle_dir(), side, "frames")))
            self.assertEqual(staged, expected[side])
            self.assertEqual(job["sides"][side]["end_frame"], ends[side])

    def test_a_frame_deleted_before_linking_is_counted_not_fatal(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        for side in self.raw:
            self.new_plate(side)
        self.tick()
        names = self.frames.write(self.raw["LEFT"], 3)
        real_link = cycle_capture.link_or_copy
        cycle_capture.link_or_copy = lambda src, dst: False if src.endswith(names[1]) else real_link(src, dst)
        self.addCleanup(setattr, cycle_capture, "link_or_copy", real_link)
        self.tick()
        self.assertEqual(self.svc.upload_job["sides"]["LEFT"]["missing"], 1)
        self.assertEqual(self.svc.upload_job["sides"]["LEFT"]["frames"], 3)

    def test_cycle_number_counts_per_date(self):
        self.run_to_staged()
        first = self.svc.upload_job["cycle"]
        self.svc.upload_job["state"] = "done"                     # pretend it was uploaded
        self.svc.store.save(self.svc.upload_job)
        self.frames.t = self.frames.t.replace(hour=11)
        self.run_to_staged()
        self.assertEqual((first, self.svc.upload_job["cycle"]), ("2026-11-10_cycle1", "2026-11-10_cycle2"))


class StagingCap(Base):
    def setUp(self):
        super().setUp()
        self.s = H.make_settings(self.root, upload={"chunk_mb": 1, "remote_folder": "DPC/data/x",
                                                    "staging_max_gb": 0.0005, "max_mbps": None, "plate_images": 0})
        self.svc = self.new_service()

    def test_cap_reached_while_staging_fails_the_job_and_deletes_the_partial_cycle(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        for side in self.raw:
            self.new_plate(side)
        self.tick()
        self.frames.write(self.raw["LEFT"], 4)                     # 4 x 200 kB > 0.5 MB
        self.tick()
        job = self.svc.upload_job
        self.assertEqual(job["state"], "failed")
        self.assertIn("staging full", job["message"])
        self.assertEqual(os.listdir(self.s.staging), [])
        self.assertIn("staging full", self.events.last("upload_failed")["reason"])

    def test_a_new_click_is_refused_while_staging_is_full(self):
        with open(os.path.join(self.s.staging, "left_over.bin"), "wb") as f:
            f.write(b"x" * 600_000)
        self.click()
        self.tick()
        self.assertIsNone(self.svc.upload_job)
        self.assertFalse(os.path.exists(os.path.join(self.s.run, "transfer.upload_cycle")), "request consumed")
        self.assertIn("staging full", self.events.last("upload_failed")["reason"])


class Requests(Base):
    def test_click_is_accepted_once_and_a_second_click_is_ignored(self):
        self.click()
        self.tick()
        first = self.svc.upload_job["requested_at"]
        self.assertTrue(os.path.isfile(os.path.join(self.s.state, "upload_cycle.json")), "saved before the file goes")
        self.clock.advance(100)
        self.click()
        self.tick()
        self.assertEqual(self.svc.upload_job["requested_at"], first)
        self.assertFalse(os.path.exists(os.path.join(self.s.run, "transfer.upload_cycle")))

    def test_cancel_deletes_the_partial_cycle(self):
        for side in self.raw:
            self.heartbeat(side, "x")
        self.click()
        self.tick()
        for side in self.raw:
            self.new_plate(side)
        self.tick()
        cycle = self.cycle_dir()
        with open(os.path.join(self.s.run, "transfer.cancel"), "w") as f:
            f.write("upload_cycle")
        self.tick()
        self.assertEqual(self.svc.upload_job["state"], "cancelled")
        self.assertFalse(os.path.exists(cycle))
        self.assertIn("job_cancelled", self.events.names())

    def test_no_upload_section_means_a_click_does_nothing(self):
        self.s.upload = None
        self.svc = self.new_service()
        self.click()
        self.tick()
        self.assertIsNone(self.svc.upload_job)
        self.assertIn("no transfer.upload", self.events.last("upload_failed")["reason"])

    def test_heartbeat_fields(self):
        fields = self.svc.heartbeat_fields()
        self.assertEqual(fields["state"], "idle")
        self.click()
        self.tick()
        fields = self.svc.heartbeat_fields()
        self.assertEqual(fields["state"], "staging")
        self.assertEqual(fields["upload_cycle"]["state"], "waiting_plate")
        for key in ("bytes_uploaded", "bytes_total", "last_upload_at", "last_model_check_at", "last_error"):
            self.assertIn(key, fields)
        json.dumps(fields)                                      # must be publishable


if __name__ == "__main__":
    unittest.main()
