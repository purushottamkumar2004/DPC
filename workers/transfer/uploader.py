"""Upload a packed cycle to <remote_folder>/<cycle>/ with resumable Graph upload sessions.

One fragment (10 MiB) per call, in list order: chunks, cycle.json of each side, manifest.json
LAST (its presence on OneDrive means "this cycle is complete"). Per file, kept in the job:
session_url, offset, resync, status.

  - not started: if OneDrive already has the file with the same size and quickXorHash (sent
    before a crash, before we could note it), it is done; else a new upload session.
  - resync (after a restart or any network error): ask the session where to continue.
    A session that is gone (expired) is replaced by a new one for that file only.
  - finished: the quickXorHash Graph reports must equal ours. A mismatch deletes the remote
    file and sends it again; the 3rd mismatch stops the job (PermanentError).
When every file is done the staging copy of the cycle is deleted (never earlier).
"""

import os
import time
from typing import Callable, Optional

from graph_client import UPLOAD_FRAGMENT_BYTES, item_quickxor
from jobs import set_phase
from transfer_errors import PermanentError, TemporaryError
from transfer_files import remove_tree

MAX_CHECKSUM_FAILURES = 3


class RateLimiter:
    """Average bandwidth cap: each send of n bytes may start only when the previous ones would
    have finished at max_mbps (megabits per second). None = no limit."""

    def __init__(self, max_mbps: Optional[float], clock=time.monotonic, sleep=time.sleep):
        self.max_mbps = max_mbps
        self.clock = clock
        self.sleep = sleep
        self._next_free = 0.0

    def wait(self, nbytes: int) -> None:
        if not self.max_mbps:
            return
        now = self.clock()
        start = max(now, self._next_free)
        self._next_free = start + nbytes * 8 / (self.max_mbps * 1e6)
        if start > now:
            self.sleep(start - now)


class Uploader:
    def __init__(self, settings, graph, limiter: RateLimiter, log, publish: Callable, clock=time.time):
        self.s = settings
        self.graph = graph
        self.limiter = limiter
        self.log = log
        self.publish = publish
        self.clock = clock

    def remote_root(self, job: dict) -> str:
        return f"{self.s.upload.remote_folder}/{job['cycle']}"

    @staticmethod
    def mark_resync(job: dict) -> None:
        """At program start and after a network error: the session state on our side is unsure."""
        for item in job.get("uploads", []):
            if item["status"] != "done" and item.get("session_url"):
                item["resync"] = True

    def step(self, job: dict) -> bool:
        try:
            return self._step(job)
        except TemporaryError:
            self.mark_resync(job)
            raise

    def _step(self, job: dict) -> bool:
        if not job.get("remote_ready"):
            self._prepare_remote(job)
            return True
        item = next((u for u in job["uploads"] if u["status"] != "done"), None)
        if item is None:
            self._finish(job)
            return True
        local = os.path.join(self.s.staging, job["cycle"], item["rel"])
        remote = f"{self.remote_root(job)}/{item['rel']}"
        if not item["session_url"]:
            existing = self.graph.get_item(remote)
            if existing and existing.get("size") == item["size"] and item_quickxor(existing) == item["quickxor"]:
                self._done(job, item, "already on OneDrive")
                return True
            item["session_url"], _expires = self.graph.create_upload_session(remote)
            item["offset"], item["resync"] = 0, False
            return True
        if item["resync"]:
            offset = self.graph.upload_status(item["session_url"])
            if offset is None:                                   # session expired: new one, this file only
                item["session_url"] = None
                return True
            item["offset"], item["resync"] = offset, False
        with open(local, "rb") as f:
            f.seek(item["offset"])
            data = f.read(UPLOAD_FRAGMENT_BYTES)
        self.limiter.wait(len(data))
        outcome, value = self.graph.upload_fragment(item["session_url"], data, item["offset"], item["size"])
        if outcome == "more":
            item["offset"] = value
        elif outcome == "resync":
            item["resync"] = True
        elif outcome == "expired":
            item["session_url"] = None
        else:
            self._check_finished(job, item, remote, value)
        self._progress(job)
        return True

    def _prepare_remote(self, job):
        """Create <remote>/<cycle>/<SIDE>/. A cycle folder that already holds a manifest belongs to
        an earlier upload whose state was lost: never overwrite it, use the next cycle number."""
        while not job.get("uploaded_any") and self.graph.get_item(f"{self.remote_root(job)}/manifest.json"):
            self._rename_cycle(job)
        for side in sorted(job["sides"]):
            self.graph.ensure_folder(f"{self.remote_root(job)}/{side}")
        job["remote_ready"] = True

    def _rename_cycle(self, job):
        date, _, number = job["cycle"].rpartition("_cycle")
        new = f"{date}_cycle{int(number) + 1}"
        os.replace(os.path.join(self.s.staging, job["cycle"]), os.path.join(self.s.staging, new))
        self.log.warning(f"{job['cycle']} already complete on OneDrive: this cycle is uploaded as {new}")
        job["cycle"] = new

    def _check_finished(self, job, item, remote, remote_item):
        reported = item_quickxor(remote_item) or item_quickxor(self.graph.get_item(remote))
        size_ok = remote_item.get("size") in (None, item["size"])
        if size_ok and (reported is None or reported == item["quickxor"]):
            if reported is None:
                self.log.warning(f"{remote}: Graph gave no quickXorHash; size checked only")
            self._done(job, item, "uploaded")
            return
        item["checksum_failures"] += 1
        self.graph.delete_item(remote)
        item.update(session_url=None, offset=0, resync=False)
        message = (f"checksum mismatch on {item['rel']} (ours {item['quickxor']}, OneDrive {reported}, "
                   f"size {remote_item.get('size')}/{item['size']})")
        if item["checksum_failures"] >= MAX_CHECKSUM_FAILURES:
            raise PermanentError(f"{message}, {item['checksum_failures']} times")
        self.log.warning(f"{message}: sending it again")

    def _done(self, job, item, how):
        item.update(status="done", session_url=None, resync=False, offset=item["size"])
        job["uploaded_any"] = True
        self.log.info(f"{job['cycle']}/{item['rel']}: {how}")

    def _progress(self, job):
        job["bytes_done"] = sum(u["size"] if u["status"] == "done" else u["offset"] for u in job["uploads"])
        job["message"] = f"uploading {job['bytes_done'] / 1e6:.0f} / {job['bytes_total'] / 1e6:.0f} MB"

    def _finish(self, job):
        remove_tree(os.path.join(self.s.staging, job["cycle"]))
        job["bytes_done"] = job["bytes_total"]
        job["uploaded_at"] = self.clock()
        set_phase(job, "uploaded")
        job["state"] = "done"
        chunks = sum(1 for u in job["uploads"] if u["rel"].endswith(".tar"))
        seconds = int(self.clock() - job.get("upload_started_at", self.clock()))
        self.log.info(f"{job['cycle']} uploaded to {self.remote_root(job)}: {chunks} chunks, "
                      f"{job['bytes_total'] / 1e9:.2f} GB; staging deleted")
        self.publish("cycle_uploaded", {"cycle": job["cycle"], "remote": self.remote_root(job), "chunks": chunks,
                                        "bytes": job["bytes_total"], "seconds": seconds})
