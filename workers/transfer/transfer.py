#!/usr/bin/env python3
"""Transfer service (P12, D-104): data to and from OneDrive, started by a click.

  Upload one cycle   request <root>/run/transfer.upload_cycle (UI button, or `touch`)
                     -> stage the next full revolution (plate to plate) of LEFT and RIGHT plus
                        transfer.upload.plate_images plate frames per side, pack chunks, upload
                        resumably to <transfer.upload.remote_folder>/<date>_cycle<N>/<SIDE>/
  Model update       request <root>/run/transfer.model_update
                     -> newest dated zip in <transfer.models_folder>/<model name>/, download,
                        package, verify, mark ready (never switch: the Supervisor decides)
  Permission check   python3 transfer.py --check [--json]   (rendered transfer.json, or the
                     settings on the command line for the setup wizard: graph_check.py, F91)
  Cancel             request <root>/run/transfer.cancel (content: upload_cycle | model_update;
                     empty = both) -> stop and delete the partial data

Each click runs one job to the end. Temporary problems (network, Graph 5xx/429) retry by
themselves (1, 2, 5, 10, then every 30 min) from where they stopped, also across a restart of
this program or the machine (state in <root>/data/state/transfer/). Permanent problems
(permission, bad zip, staging full, disk) stop the job as `failed` with the reason; clicking the
same button again continues from where it stopped. One network transfer at a time (a model
download goes first); a cycle can be staging meanwhile.

Settings: the rendered JSON in env DPC_WORKER_CONFIG (transfer_settings.py). Heartbeat every
10 s on dpc/worker/transfer/health (common/heartbeat.py, D-099); events on
dpc/worker/transfer/event: cycle_staged, cycle_uploaded, upload_failed, model_downloaded,
model_download_failed, model_update_done, job_cancelled. An unexpected error writes
<root>/run/transfer.last_error.json and exits 1 (the Supervisor restarts it). SIGTERM: exit 0.
"""

import logging
import os
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))           # <root>/code
for path in (REPO_ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from common.heartbeat import Heartbeat                               # noqa: E402
from common.last_error import clear_last_error, write_last_error   # noqa: E402

import jobs                                                          # noqa: E402
from cycle_capture import CycleCapture                               # noqa: E402
from graph_client import GraphClient                                 # noqa: E402
from model_update import ModelUpdater                                # noqa: E402
from packer import Packer                                            # noqa: E402
from plate_watch import PlateWatch                                   # noqa: E402
from transfer_errors import PermanentError, TemporaryError           # noqa: E402
from transfer_files import dir_size, remove_tree                     # noqa: E402
from transfer_settings import WORKER_ID, ConfigError, build_settings, read_raw_config, secrets_in  # noqa: E402
from uploader import RateLimiter, Uploader                           # noqa: E402

IDLE_SLEEP_S = 1.0
LOCAL_PHASES = ("waiting_plate", "staging", "packing")
GB = 1024 ** 3
_stop = False


class TransferService:
    """One tick = look at the request files, then one step of each job that may run now.
    Tests drive tick() with a fake Graph, fake plates and a fake clock."""

    def __init__(self, settings, log, graph, plates, publish: Callable[[str, dict], object],
                 clock=time.time, limiter: Optional[RateLimiter] = None):
        self.s = settings
        self.log = log
        self.graph = graph
        self.plates = plates
        self.publish = publish
        self.clock = clock
        self.store = jobs.JobStore(settings.state)
        max_mbps = settings.upload.max_mbps if settings.upload else None
        self.limiter = limiter or RateLimiter(max_mbps)
        self.capture = CycleCapture(settings, plates, self.store, log, publish, clock)
        self.packer = Packer(settings, log)
        self.uploader = Uploader(settings, graph, self.limiter, log, publish, clock)
        self.models = ModelUpdater(settings, graph, self.limiter, self.store, log, publish, clock)
        self.upload_job = self.store.load(jobs.UPLOAD)
        self.model_job = self.store.load(jobs.MODEL)
        self.status = self.store.load_other("status", {})
        self.last_tick_at = clock()
        if self.upload_job:
            self.uploader.mark_resync(self.upload_job)       # after a restart: ask Graph where we are
            self.log.info(f"continuing upload job: {self.upload_job['state']} {self.upload_job.get('cycle') or ''}")
        if jobs.is_active(self.model_job):
            self.log.info(f"continuing model update: {self.model_job['state']}")

    # ================================================================ requests
    def handle_requests(self) -> None:
        run = self.s.run
        cancel = jobs.read_request(run, jobs.CANCEL_FILE)
        if cancel is not None:
            for kind in ([cancel] if cancel in jobs.KINDS else jobs.KINDS):
                self._cancel(kind)
            jobs.delete_request(run, jobs.CANCEL_FILE)
        if jobs.read_request(run, jobs.REQUEST_FILES[jobs.UPLOAD]) is not None:
            self._accept_upload()
            jobs.delete_request(run, jobs.REQUEST_FILES[jobs.UPLOAD])
        if jobs.read_request(run, jobs.REQUEST_FILES[jobs.MODEL]) is not None:
            self._accept_model()
            jobs.delete_request(run, jobs.REQUEST_FILES[jobs.MODEL])

    def _accept_upload(self):
        job = self.upload_job
        if self.s.upload is None:
            self.log.warning("upload request ignored: site.yaml has no transfer.upload section")
            self.publish("upload_failed", {"cycle": None, "reason": "no transfer.upload in site.yaml"})
            return
        if jobs.is_active(job):
            self.log.info(f"upload request ignored: a cycle upload is already running ({job['state']})")
            return
        if job and job["state"] == "failed":
            if job["phase"] in ("waiting_plate", "staging"):
                self.capture.rearm_open_sides(job)
            for item in job.get("uploads", []):
                item["checksum_failures"] = 0
            self.uploader.mark_resync(job)
            jobs.resume(job, "continuing after a new click")
            self.log.info(f"upload job resumed in phase {job['phase']}")
        else:
            used = dir_size(self.s.staging)
            if used >= self.s.upload.staging_max_gb * GB:
                reason = (f"staging full: {used / GB:.1f} GB >= staging_max_gb {self.s.upload.staging_max_gb:g} "
                          f"(cycles still waiting in {self.s.staging})")
                self.log.error(f"upload request refused: {reason}")
                self.publish("upload_failed", {"cycle": None, "reason": reason})
                return
            job = jobs.new_job(jobs.UPLOAD, "waiting_plate", self.clock())
            self.capture.start_job(job)
            self.log.info("upload request accepted: waiting for the next plate on every side")
        self.upload_job = job
        self.store.save(job)

    def _accept_model(self):
        job = self.model_job
        if jobs.is_active(job):
            self.log.info(f"model update request ignored: one is already running ({job['state']})")
            return
        if job and job["state"] == "failed":
            jobs.resume(job, "continuing after a new click")
            self.log.info(f"model update resumed in phase {job['phase']}")
        else:
            job = jobs.new_job(jobs.MODEL, "checking", self.clock())
            self.log.info("model update request accepted")
        self.model_job = job
        self.store.save(job)

    def _cancel(self, kind):
        job = self.upload_job if kind == jobs.UPLOAD else self.model_job
        if not job or job["state"] in ("done", "cancelled"):
            return
        if kind == jobs.UPLOAD:
            for item in job.get("uploads", []):
                if item.get("session_url"):
                    try:
                        self.graph.cancel_upload(item["session_url"])
                    except (TemporaryError, PermanentError):
                        pass
            if job.get("cycle"):
                remove_tree(os.path.join(self.s.staging, job["cycle"]))
        else:
            remove_tree(self.s.downloads)
        job["state"] = "cancelled"
        job["message"] = "cancelled; partial data deleted"
        self.store.save(job)
        self.log.info(f"{kind} cancelled; partial data deleted")
        self.publish("job_cancelled", {"job": kind})

    # ================================================================ tick
    def tick(self) -> bool:
        """One round. Returns True when work was done (the caller then does not sleep)."""
        self.last_tick_at = self.clock()
        self.handle_requests()
        busy = False
        up = self.upload_job
        if up and up["phase"] in LOCAL_PHASES:               # staging needs no network: always runs
            busy |= self._run(up, self._local_upload_step, "upload")
        if self.model_job and jobs.due(self.model_job, self.clock()):   # one network job at a time,
            busy |= self._run(self.model_job, self.models.step, "model update")   # the model first
        elif up and up["phase"] == "uploading":
            busy |= self._run(up, self.uploader.step, "upload")
        return busy

    def _local_upload_step(self, job) -> bool:
        if job["phase"] == "packing":
            return self.packer.step(job)
        return self.capture.step(job)

    def _run(self, job, step, label) -> bool:
        """Run one step of a job with the retry rules (jobs.py). Saves the job afterwards."""
        now = self.clock()
        if not jobs.due(job, now):
            return False
        busy = False
        try:
            busy = bool(step(job))
            if job["state"] not in jobs.FINISHED:
                job["attempts"] = 0
            if job["kind"] == jobs.UPLOAD and job["state"] == "done":
                self._note("last_upload_at", self.clock())
            if job["kind"] == jobs.MODEL and job.get("checked_at"):
                self._note("last_model_check_at", job["checked_at"])
        except TemporaryError as exc:
            wait = jobs.retry_later(job, exc, now)
            self.log.warning(f"{label}: {exc}; retry in {int(wait)} s (attempt {job['attempts']})")
            self._note("last_error", str(exc)[:120])
        except PermanentError as exc:
            jobs.fail(job, str(exc))
            self.log.error(f"{label} failed: {exc} (click again to continue after fixing it)")
            self._note("last_error", str(exc)[:120])
            if job["kind"] == jobs.UPLOAD:
                self.publish("upload_failed", {"cycle": job.get("cycle"), "reason": str(exc)[:300],
                                               "phase": job["phase"]})
        except OSError as exc:                                   # disk full or a folder gone: stop the job
            jobs.fail(job, f"{type(exc).__name__}: {exc}")
            self.log.error(f"{label} failed: {type(exc).__name__}: {exc}")
            self._note("last_error", f"{type(exc).__name__}: {exc}"[:120])
            if job["kind"] == jobs.UPLOAD:
                self.publish("upload_failed", {"cycle": job.get("cycle"), "reason": str(exc)[:300],
                                               "phase": job["phase"]})
        self.store.save(job)
        return busy

    def _note(self, key, value):
        if self.status.get(key) != value:
            self.status[key] = value
            self.store.save_other("status", self.status)

    # ================================================================ heartbeat
    def heartbeat_fields(self) -> dict:
        up, model = self.upload_job, self.model_job
        state = "idle"
        if jobs.is_active(up):
            state = "uploading" if up["phase"] == "uploading" else "staging"
        if jobs.is_active(model):
            state = "downloading"
        return {"state": state,
                "upload_cycle": self._job_view(up, ("cycle", "bytes_done", "bytes_total"),
                                               frames={s: st["frames"] for s, st in (up or {}).get("sides", {}).items()}),
                "model_update": self._job_view(model, ("bytes_done", "bytes_total", "result"),
                                               downloaded=(model or {}).get("downloaded")),
                "bytes_uploaded": (up or {}).get("bytes_done", 0) if up and up["phase"] == "uploading" else 0,
                "bytes_total": (up or {}).get("bytes_total", 0) if up and up["phase"] == "uploading" else 0,
                "last_upload_at": jobs.iso(self.status.get("last_upload_at")),
                "last_model_check_at": jobs.iso(self.status.get("last_model_check_at")),
                "last_error": self.status.get("last_error")}

    @staticmethod
    def _job_view(job, keys, **extra):
        if not job:
            return {"state": "idle"}
        view = {"state": job["state"], "phase": job["phase"], "attempts": job.get("attempts", 0),
                "next_retry_at": jobs.iso(job.get("next_retry_at")), "message": job.get("message", "")}
        view.update({k: job.get(k) for k in keys})
        view.update({k: v for k, v in extra.items() if v is not None})
        return view

    def healthy(self) -> bool:
        return self.clock() - self.last_tick_at < 120


# ==================================================================== program

def _on_signal(_signum, _frame):
    global _stop
    _stop = True


def setup_logging(logs_dir, backup_count) -> logging.Logger:
    os.makedirs(logs_dir, exist_ok=True)
    log = logging.getLogger("transfer")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    for handler in (logging.StreamHandler(),
                    TimedRotatingFileHandler(os.path.join(logs_dir, "transfer.log"), when="midnight",
                                             backupCount=backup_count)):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    return log


def run(s, log) -> None:
    for folder in (s.run, s.state, s.staging, s.downloads):
        os.makedirs(folder, exist_ok=True)
    plates = PlateWatch({side.side: side.inspection_worker for side in s.sides}, s.mqtt, log)
    plates.start()
    heartbeat = Heartbeat(WORKER_ID, mqtt=s.mqtt, logger=log)
    service = TransferService(s, log, GraphClient(s.graph, s.drive), plates, heartbeat.publish_event)
    heartbeat.extra, heartbeat.healthy = service.heartbeat_fields, service.healthy
    heartbeat.start()
    log.info(f"transfer started: site {s.site_id}, drive {next(iter(s.drive))}, models {sorted(s.models_in_use)} "
             f"from {s.models_folder}, upload {'to ' + s.upload.remote_folder if s.upload else 'off'}")
    try:
        while not _stop:
            if not service.tick():
                time.sleep(IDLE_SLEEP_S)
        log.info("stop signal: transfer stopped (jobs continue at the next start)")
    finally:
        heartbeat.stop()
        plates.stop()


def main() -> int:
    if "--check" in sys.argv[1:]:                       # graph_check.py: rendered or direct (F91)
        from graph_check import check_main
        return check_main(sys.argv[1:])
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    root = os.path.dirname(REPO_ROOT)
    try:
        raw = read_raw_config()
    except ConfigError as exc:
        clear_last_error(root, WORKER_ID)
        write_last_error(root, WORKER_ID, exc)
        print(f"transfer: {exc}", file=sys.stderr)
        return 1
    secrets = secrets_in(raw)
    paths = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    root = os.path.expanduser(paths.get("root") or root)
    clear_last_error(root, WORKER_ID)
    backup = (raw.get("log") or {}).get("backup_count") if isinstance(raw.get("log"), dict) else None
    log = setup_logging(paths.get("logs") or os.path.join(root, "logs"), backup if isinstance(backup, int) else 5)
    if not os.environ.get("DPC_WORKER_CONFIG"):
        log.warning("DPC_WORKER_CONFIG is not set (transfer.json not rendered yet)")
    try:
        settings = build_settings(raw)
        for warning in settings.warnings:
            log.warning(warning)
        run(settings, log)
        return 0
    except Exception as exc:
        record = write_last_error(root, WORKER_ID, exc, secrets)
        message = str(exc)
        for secret in sorted(set(secrets), key=len, reverse=True):
            message = message.replace(secret, "***")
        log.error(f"stopped by {type(exc).__name__}: {message[:500]} (details in {record})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
