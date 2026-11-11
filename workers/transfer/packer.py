"""Pack a staged cycle into upload chunks (D-104).

Per side, two kinds, each in name order:
    frames/ -> <SIDE>/part_0001.tar, part_0002.tar, ...   (the cycle)
    PLATE/  -> <SIDE>/plate_0001.tar, ...                 (the spread plate frames)
A chunk holds whole frames up to transfer.upload.chunk_mb (at least one frame). Plain tar,
no compression: JPEG does not compress.

One chunk per call, so a stop or a cancel is seen between chunks. A chunk is written to a temp
name, flushed and renamed; its sha256 (for the manifest) and quickXorHash (to check what Graph
stored) are recorded in the job; then its frames are removed from frames/ or PLATE/ (the tar
is now the staging copy: each frame is on disk once). The record keeps the first and last frame
name, so after a crash between "recorded" and "frames removed" the leftovers are removed and
never packed twice. When nothing is left, the upload list is built: every chunk, both
cycle.json, and manifest.json last.
"""

import datetime
import hashlib
import os
import tarfile
import time
from typing import List, Tuple

from jobs import set_phase
from quickxor import QuickXorHash
from transfer_files import save_json

KINDS = (("frames", "part"), ("PLATE", "plate"))
MB = 1024 * 1024


def hashes_of(path: str) -> Tuple[str, str]:
    """(sha256 hex, quickXorHash base64) in one read of the file."""
    sha, qx = hashlib.sha256(), QuickXorHash()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * MB), b""):
            sha.update(block)
            qx.update(block)
    return sha.hexdigest(), qx.base64()


def _pick(folder: str, names: List[str], limit_bytes: int) -> List[str]:
    picked, total = [], 0
    for name in names:
        size = os.path.getsize(os.path.join(folder, name))
        if picked and total + size > limit_bytes:
            break
        picked.append(name)
        total += size
    return picked


class Packer:
    def __init__(self, settings, log, clock=None):
        self.s = settings
        self.log = log
        self.clock = clock

    def step(self, job: dict) -> bool:
        """Make the next chunk, or (nothing left) the upload list. Always True (work was done)."""
        job.setdefault("chunks", [])
        cycle_dir = os.path.join(self.s.staging, job["cycle"])
        for side in sorted(job["sides"]):
            for folder_name, prefix in KINDS:
                if self._pack_one(job, cycle_dir, side, folder_name, prefix):
                    return True
        self._finish(job, cycle_dir)
        return True

    def _pack_one(self, job, cycle_dir, side, folder_name, prefix) -> bool:
        folder = os.path.join(cycle_dir, side, folder_name)
        if not os.path.isdir(folder):
            return False
        done = [c for c in job["chunks"] if c["side"] == side and c["kind"] == prefix]
        last_packed = done[-1]["last"] if done else None
        names = sorted(os.listdir(folder))
        for name in [n for n in names if last_packed and n <= last_packed]:
            os.remove(os.path.join(folder, name))              # packed before a crash: remove only
        names = [n for n in names if not last_packed or n > last_packed]
        if not names:
            os.rmdir(folder)
            return False
        members = _pick(folder, names, self.s.upload.chunk_mb * MB)
        chunk_name = f"{prefix}_{len(done) + 1:04d}.tar"
        target = os.path.join(cycle_dir, side, chunk_name)
        temp = os.path.join(cycle_dir, side, f".{chunk_name}.tmp")
        with tarfile.open(temp, "w", format=tarfile.PAX_FORMAT) as tar:
            for name in members:
                tar.add(os.path.join(folder, name), arcname=name)
        with open(temp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(temp, target)
        sha, qx = hashes_of(target)
        job["chunks"].append({"side": side, "kind": prefix, "name": chunk_name, "frames": len(members),
                              "first": members[0], "last": members[-1], "size": os.path.getsize(target),
                              "sha256": sha, "quickxor": qx})
        for name in members:
            os.remove(os.path.join(folder, name))
        job["message"] = f"packed {side}/{chunk_name} ({len(members)} frames)"
        self.log.info(f"{job['cycle']}: {job['message']}, {os.path.getsize(target) / MB:.0f} MB")
        return True

    def _finish(self, job, cycle_dir):
        manifest = {"site": self.s.site_id, "cycle": job["cycle"],
                    "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "sides": {side: {"cycle_json": f"{side}/cycle.json",
                                     "chunks": [{k: c[k] for k in ("name", "kind", "frames", "first", "last",
                                                                    "size", "sha256")}
                                                for c in job["chunks"] if c["side"] == side]}
                              for side in sorted(job["sides"])}}
        save_json(os.path.join(cycle_dir, "manifest.json"), manifest)
        uploads = [{"rel": f"{c['side']}/{c['name']}", "size": c["size"], "quickxor": c["quickxor"]}
                   for c in job["chunks"]]
        for rel in [f"{side}/cycle.json" for side in sorted(job["sides"])] + ["manifest.json"]:   # manifest LAST
            path = os.path.join(cycle_dir, rel)
            uploads.append({"rel": rel, "size": os.path.getsize(path), "quickxor": hashes_of(path)[1]})
        for item in uploads:
            item.update(status="pending", session_url=None, offset=0, resync=False, checksum_failures=0)
        job["uploads"] = uploads
        job["bytes_total"] = sum(u["size"] for u in uploads)
        job["bytes_done"] = 0
        job["upload_started_at"] = time.time()
        set_phase(job, "uploading", f"uploading {len(uploads)} files")
