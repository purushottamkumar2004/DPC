"""Click-started jobs (developer, 2026-11-10): one record per kind, kept in
<root>/data/state/transfer/<kind>.json, so a restart of the program or the machine continues
where it stopped.

Requests are files in <root>/run/ written by a UI button (UI-4) or by hand (`touch`):
    transfer.upload_cycle   upload one cycle
    transfer.model_update   check OneDrive for a newer model and download it
    transfer.cancel         stop a job and delete its partial data; content: upload_cycle |
                            model_update (empty = both)
A request is accepted by writing the job record first and deleting the file second, so a click
is never lost and never done twice.

Job record (common keys):
    kind, phase (the work step), state (= phase while working | waiting_retry | failed | done |
    cancelled), requested_at, attempts, next_retry_at, message
"""

import datetime
import os
from typing import Optional

from transfer_errors import TemporaryError
from transfer_files import load_json, save_json

UPLOAD = "upload_cycle"
MODEL = "model_update"
KINDS = (UPLOAD, MODEL)
REQUEST_FILES = {UPLOAD: "transfer.upload_cycle", MODEL: "transfer.model_update"}
CANCEL_FILE = "transfer.cancel"

FINISHED = ("done", "failed", "cancelled")
# Waits between retries after a temporary problem: 1, 2, 5, 10 minutes, then every 30 minutes.
BACKOFF_S = (60, 120, 300, 600, 1800)


def iso(ts: Optional[float]) -> Optional[str]:
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def new_job(kind: str, first_phase: str, now: float) -> dict:
    return {"kind": kind, "phase": first_phase, "state": first_phase, "requested_at": iso(now),
            "attempts": 0, "next_retry_at": None, "message": ""}


def is_active(job: Optional[dict]) -> bool:
    return job is not None and job.get("state") not in FINISHED


def retry_later(job: dict, exc: TemporaryError, now: float) -> float:
    """Wait, then continue the same phase. Returns the wait in seconds."""
    job["attempts"] = int(job.get("attempts", 0)) + 1
    wait = max(BACKOFF_S[min(job["attempts"], len(BACKOFF_S)) - 1], float(exc.retry_after_s or 0))
    job["state"] = "waiting_retry"
    job["next_retry_at"] = now + wait
    job["message"] = f"retry {job['attempts']} in {int(wait)} s: {exc}"[:300]
    return wait


def fail(job: dict, message: str) -> None:
    job["state"] = "failed"
    job["next_retry_at"] = None
    job["message"] = str(message)[:300]


def resume(job: dict, note: str) -> None:
    """A failed job, clicked again: continue the phase it stopped in."""
    job["state"] = job["phase"]
    job["attempts"] = 0
    job["next_retry_at"] = None
    job["message"] = note


def due(job: dict, now: float) -> bool:
    """True when the job may do work now (switches waiting_retry back to its phase)."""
    if not is_active(job):
        return False
    if job["state"] == "waiting_retry":
        if now < float(job.get("next_retry_at") or 0):
            return False
        job["state"] = job["phase"]
    return now >= float(job.get("not_before") or 0)


def set_phase(job: dict, phase: str, message: str = "") -> None:
    job["phase"] = job["state"] = phase
    job["message"] = message


class JobStore:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.state_dir, f"{name}.json")

    def load(self, kind: str) -> Optional[dict]:
        job = load_json(self.path(kind))
        return job if isinstance(job, dict) and job.get("kind") == kind else None

    def save(self, job: dict) -> None:
        save_json(self.path(job["kind"]), job)

    def load_other(self, name: str, default):
        value = load_json(self.path(name), default)
        return value if isinstance(value, type(default)) else default

    def save_other(self, name: str, value) -> None:
        save_json(self.path(name), value)


def read_request(run_dir: str, file_name: str) -> Optional[str]:
    """The request file's text ('' when empty), or None when there is no request."""
    path = os.path.join(run_dir, file_name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def delete_request(run_dir: str, file_name: str) -> None:
    try:
        os.remove(os.path.join(run_dir, file_name))
    except FileNotFoundError:
        pass
