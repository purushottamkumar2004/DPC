"""Model update on click (D-104, developer 2026-11-10): one job checks OneDrive for a newer
model of THIS site, downloads it, packages it and marks it ready. It never switches models: the
Supervisor's models.auto_update decides (download = a person switches; install = the Supervisor
switches with the self-test; never a pinned model; D-060, D-087).

OneDrive layout (developer, 2026-11-10): one folder per site, one zip per version, named by date:
    <transfer.models_folder = DPC/models/<site.id>>/<model name>/2026.11.20.zip
        -> model_final.pth + the class file (index map {"0": "nut_bolt", ...} or COCO)
Only the model names this site uses (models_in_use) are looked at; other sites' folders never.

Which zip:
  - name = a year-first date: 2026.11.20 / 2026-11-20 / 2026_11_20, optional letter for a 2nd
    one the same day (2026.11.20b); stored locally as 2026.11.20[b]. Other names are ignored.
  - only the newest, and only if newer than the newest installed version (no downgrade: going
    back is the Supervisor's rollback); a zip that failed before is skipped until it is replaced
    (its eTag changes).
  - only when its upload is finished: last modified at least STABLE_S ago (OneDrive shows a file
    only after its upload is committed; the wait covers a re-upload in progress).
Checks: free disk for zip + unpacked + package; quickXorHash of the download = OneDrive's;
zipfile CRC of every member; exactly one .pth and one class file (else refused: a Detectron
output folder has several .pth, and picking the wrong one silently is worse than stopping).
Packaging: build_package() of scripts/make_model_package.py (read-only use), the shared
Detectron config from git (D-088), verify_package(), then `ready`. `current`/`pinned` untouched.
"""

import datetime
import importlib.util
import json
import os
import re
import shutil
import time
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from common.config import model_package as mp
from graph_client import item_quickxor
from jobs import set_phase
from quickxor import quickxor_of_file
from transfer_errors import PermanentError
from transfer_files import remove_tree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
PACKAGER = os.path.join(REPO_ROOT, "scripts", "make_model_package.py")
SHARED_DETECTRON_YAML = os.path.join(REPO_ROOT, "models", "configs", "mask_rcnn_R101_FPN_3x.yaml")   # D-088

STABLE_S = 10 * 60
DOWNLOAD_BLOCK_BYTES = 10 * 1024 * 1024
DISK_FACTOR = 3.3            # zip + unpacked weights + the package copy, +10 %
MAX_DOWNLOAD_CHECKSUM_FAILURES = 2
VERSION_NAME = re.compile(r"^(\d{4})[._-](\d{2})[._-](\d{2})([a-z]?)$")


# ======================================================================
# Names and class files
# ======================================================================

def version_from_zip_name(name: str) -> Optional[str]:
    """'2026-11-20.zip' -> '2026.11.20'; None for anything that is not a dated zip."""
    if not name.lower().endswith(".zip"):
        return None
    return normalise_version(name[:-4])


def normalise_version(text: str) -> Optional[str]:
    m = VERSION_NAME.match(text)
    if not m:
        return None
    year, month, day, letter = m.groups()
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    return f"{year}.{month}.{day}{letter}"


def installed_versions(models_dir: str, name: str) -> List[str]:
    """Dated versions with a manifest in data/models/<name>/ (any state)."""
    root = os.path.join(models_dir, name)
    found = []
    if os.path.isdir(root):
        for entry in os.listdir(root):
            version = normalise_version(entry)
            if version == entry and os.path.isfile(os.path.join(root, entry, mp.MANIFEST_FILE)):
                found.append(entry)
    return sorted(found)


def classes_from_class_file(path: str) -> List[str]:
    """The class list in model output order, from the training tool's file:
       index map {"0": name, ...} (D-089): keys 0..n-1, no gaps, unique names;
       COCO {"categories": [...]}: categories sorted by id (make_model_package's rule);
       {"classes": [...]} or a plain list."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise PermanentError(f"class file {os.path.basename(path)} is not readable JSON ({type(exc).__name__})")
    if isinstance(data, dict) and "categories" in data:
        try:
            return _packager().classes_from_coco(path)
        except Exception as exc:                       # PackagingError: wrong ids or names
            raise PermanentError(f"class file: {exc}")
    if isinstance(data, dict) and isinstance(data.get("classes"), list):
        names = data["classes"]
    elif isinstance(data, list):
        names = data
    elif isinstance(data, dict) and data:
        problems = [k for k in data if not (isinstance(k, str) and k.isdigit() and str(int(k)) == k)]
        if problems:
            raise PermanentError(f"class file: keys must be 0, 1, 2, ...; not {problems[:3]}")
        indexes = sorted(int(k) for k in data)
        if indexes != list(range(len(indexes))):
            missing = sorted(set(range(indexes[-1] + 1)) - set(indexes))
            raise PermanentError(f"class file: index {missing[:3]} missing (indexes must be 0..{len(indexes) - 1})")
        names = [data[str(i)] for i in indexes]
    else:
        raise PermanentError("class file: expected {\"0\": name, ...}, COCO categories or a list")
    if not all(isinstance(n, str) and n.strip() for n in names):
        raise PermanentError("class file: every class needs a name")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise PermanentError(f"class file: {duplicates} appear more than once")
    return list(names)


_PACKAGER_MODULE = None


def _packager():
    """scripts/make_model_package.py as a module (owned by CONFIG; only read and called here)."""
    global _PACKAGER_MODULE
    if _PACKAGER_MODULE is None:
        spec = importlib.util.spec_from_file_location("make_model_package", PACKAGER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PACKAGER_MODULE = module
    return _PACKAGER_MODULE


# ======================================================================
# The zip
# ======================================================================

def _ignored(member: str) -> bool:
    base = os.path.basename(member)
    return member.endswith("/") or member.startswith("__MACOSX/") or base.startswith(".") or not base


def pick_members(zf: zipfile.ZipFile) -> Tuple[str, str]:
    """(weights member, class-file member). Files may be at the top or inside ONE folder."""
    names = [n for n in zf.namelist() if not _ignored(n)]
    for name in names:
        if name.startswith("/") or ".." in name.split("/") or ":" in name:
            raise PermanentError(f"zip: unsafe path {name!r} inside")
        if name.count("/") > 1:
            raise PermanentError(f"zip: {name!r} is nested too deep (put the files at the top or in one folder)")
    weights = [n for n in names if n.lower().endswith(".pth")]
    classes = [n for n in names if n.lower().endswith(".json") and os.path.basename(n) != mp.MANIFEST_FILE]
    if len(weights) != 1:
        raise PermanentError(f"zip: needs exactly one .pth, found {len(weights)} "
                             f"({', '.join(os.path.basename(w) for w in weights) or 'none'})")
    if len(classes) != 1:
        raise PermanentError(f"zip: needs exactly one class .json, found {len(classes)} "
                             f"({', '.join(os.path.basename(c) for c in classes) or 'none'})")
    return weights[0], classes[0]


def unpack(zip_path: str, folder: str) -> Tuple[str, str]:
    """Check every CRC, extract only the two needed files into folder. Returns their paths."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise PermanentError(f"zip: {bad} is corrupt (CRC check failed)")
            weights, classes = pick_members(zf)
            remove_tree(folder)
            os.makedirs(folder)
            paths = []
            for member in (weights, classes):
                target = os.path.join(folder, os.path.basename(member))
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                paths.append(target)
            return paths[0], paths[1]
    except zipfile.BadZipFile as exc:
        raise PermanentError(f"zip: not a valid zip file ({exc})")


# ======================================================================
# The job
# ======================================================================

class ModelUpdater:
    def __init__(self, settings, graph, limiter, store, log, publish: Callable, clock=time.time):
        self.s = settings
        self.graph = graph
        self.limiter = limiter
        self.store = store
        self.log = log
        self.publish = publish
        self.clock = clock

    # ---------------------------------------------------------------- phases
    def step(self, job: dict) -> bool:
        if job["phase"] == "checking":
            return self._check(job)
        if job["phase"] == "downloading":
            return self._download(job)
        return self._package(job)

    def failed_versions(self) -> Dict[str, dict]:
        return self.store.load_other("failed_models", {})

    def _check(self, job: dict) -> bool:
        """Find the newest usable zip per model name in use."""
        job["checked_at"] = self.clock()
        failed = self.failed_versions()
        candidates, results, wait_until = [], {}, 0.0
        for name in sorted(self.s.models_in_use):
            folder = f"{self.s.models_folder}/{name}"
            items = self.graph.list_children(folder)
            if items is None:
                results[name] = f"no folder {folder} on OneDrive"
                continue
            zips = sorted(((version_from_zip_name(i.get("name", "")), i) for i in items if "file" in i),
                          key=lambda pair: pair[0] or "")
            zips = [(v, i) for v, i in zips if v]
            installed = installed_versions(self.s.models, name)
            newest_installed = installed[-1] if installed else None
            if not zips or (newest_installed and zips[-1][0] <= newest_installed):
                results[name] = f"up to date ({newest_installed or 'nothing installed'})"
                continue
            version, item = zips[-1]
            key = f"{name}/{version}"
            if key in failed and failed[key].get("etag") == item.get("eTag"):
                results[name] = (f"{version} skipped: it failed before ({failed[key].get('reason')}); "
                                 f"upload a corrected zip")
                continue
            modified = _timestamp(item.get("lastModifiedDateTime"))
            if modified and self.clock() - modified < STABLE_S:
                wait_until = max(wait_until, modified + STABLE_S)
                results[name] = f"{version}: waiting until its upload looks finished"
                continue
            candidates.append({"name": name, "version": version, "remote": f"{folder}/{item['name']}",
                               "size": int(item.get("size") or 0), "etag": item.get("eTag"),
                               "quickxor": item_quickxor(item), "checksum_failures": 0})
            results[name] = f"{version}: new"
        job["results"] = results
        if wait_until:
            job["not_before"] = wait_until
            job["message"] = "; ".join(results.values())[:300]
            return True
        job["not_before"] = None
        if not candidates:
            self._end(job, "up_to_date")
            return True
        job.update(candidates=candidates, current=0, bytes_total=sum(c["size"] for c in candidates), bytes_done=0)
        set_phase(job, "downloading", f"downloading {candidates[0]['name']} {candidates[0]['version']}")
        return True

    def _paths(self, cand: dict) -> Tuple[str, str, str]:
        folder = os.path.join(self.s.downloads, cand["name"])
        return (os.path.join(folder, f"{cand['version']}.zip.part"), os.path.join(folder, f"{cand['version']}.zip"),
                os.path.join(folder, f"{cand['version']}.unpacked"))

    def _download(self, job: dict) -> bool:
        cand = job["candidates"][job["current"]]
        part, done, _ = self._paths(cand)
        os.makedirs(os.path.dirname(part), exist_ok=True)
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        if offset == 0:
            free = shutil.disk_usage(os.path.dirname(part)).free
            need = int(cand["size"] * DISK_FACTOR)
            if free < need:
                raise PermanentError(f"not enough free disk for {cand['name']} {cand['version']}: "
                                     f"{free / 1e9:.1f} GB free, {need / 1e9:.1f} GB needed")
        if offset < cand["size"]:
            end = min(offset + DOWNLOAD_BLOCK_BYTES, cand["size"]) - 1
            data = self.graph.download_range(cand["remote"], offset, end)
            self.limiter.wait(len(data))
            with open(part, "ab") as f:
                f.write(data)
            job["bytes_done"] = sum(c["size"] for c in job["candidates"][:job["current"]]) + offset + len(data)
            job["message"] = (f"downloading {cand['name']} {cand['version']}: "
                              f"{(offset + len(data)) / 1e6:.0f} / {cand['size'] / 1e6:.0f} MB")
            return True
        return self._verify_download(job, cand, part, done)

    def _verify_download(self, job, cand, part, done) -> bool:
        ours = quickxor_of_file(part)
        if cand["quickxor"] and ours != cand["quickxor"]:
            os.remove(part)
            item = self.graph.get_item(cand["remote"]) or {}
            if item.get("eTag") and item.get("eTag") != cand["etag"]:        # replaced meanwhile: start over
                cand.update(size=int(item.get("size") or 0), etag=item.get("eTag"), quickxor=item_quickxor(item))
                self.log.warning(f"{cand['remote']} was replaced during the download; downloading the new one")
                return True
            cand["checksum_failures"] += 1
            if cand["checksum_failures"] >= MAX_DOWNLOAD_CHECKSUM_FAILURES:
                self._reject(job, cand, f"checksum mismatch after {cand['checksum_failures']} downloads")
            self.log.warning(f"{cand['remote']}: checksum mismatch, downloading again")
            return True
        if not cand["quickxor"]:
            self.log.warning(f"{cand['remote']}: OneDrive gave no quickXorHash; the zip CRCs are the check")
        os.replace(part, done)
        set_phase(job, "packaging", f"packaging {cand['name']} {cand['version']}")
        return True

    def _package(self, job: dict) -> bool:
        cand = job["candidates"][job["current"]]
        _, zip_path, unpacked = self._paths(cand)
        name, version = cand["name"], cand["version"]
        target = os.path.join(self.s.models, name, version)
        if not os.path.isdir(target):
            try:
                weights, class_file = unpack(zip_path, unpacked)
                classes = classes_from_class_file(class_file)
                packager = _packager()
                packager.build_package(self.s.models, name, version, weights, SHARED_DETECTRON_YAML,
                                       classes, {}, trained_at=version[:10].replace(".", "-"))
            except PermanentError as exc:
                self._reject(job, cand, str(exc))
            except Exception as exc:                    # PackagingError and friends: the zip's content
                if type(exc).__name__ != "PackagingError":
                    raise
                self._reject(job, cand, str(exc))
        try:
            mp.verify_package(target, name, version)
        except mp.ModelPackageError as exc:
            raise PermanentError(f"{name} {version}: the package does not verify ({exc}); "
                                 f"a person must look at {target}")
        open(os.path.join(target, mp.READY_FILE), "w").close()
        remove_tree(os.path.dirname(zip_path))
        self.log.info(f"model {name} {version} downloaded, packaged, verified and marked ready "
                      f"(the Supervisor decides the switch)")
        self.publish("model_downloaded", {"name": name, "version": version})
        job.setdefault("downloaded", []).append({"name": name, "version": version})
        job["results"][name] = f"{version}: downloaded and ready"
        job["current"] += 1
        if job["current"] < len(job["candidates"]):
            nxt = job["candidates"][job["current"]]
            set_phase(job, "downloading", f"downloading {nxt['name']} {nxt['version']}")
        else:
            self._end(job, "downloaded")
        return True

    def _reject(self, job, cand, reason):
        """A bad version: remembered (until the zip is replaced), event, the job stops."""
        failed = self.failed_versions()
        failed[f"{cand['name']}/{cand['version']}"] = {"etag": cand["etag"], "reason": reason[:200]}
        self.store.save_other("failed_models", failed)
        remove_tree(os.path.join(self.s.downloads, cand["name"]))
        self.publish("model_download_failed", {"name": cand["name"], "version": cand["version"], "reason": reason[:300]})
        set_phase(job, "checking")                       # a new click checks again from the start
        raise PermanentError(f"{cand['name']} {cand['version']}: {reason}")

    def _end(self, job, result):
        job["result"] = result
        set_phase(job, "finished", "; ".join(job.get("results", {}).values())[:300])
        job["state"] = "done"
        self.log.info(f"model update: {result} ({job['message']})")
        self.publish("model_update_done", {"result": result, "models": job.get("results", {})})


def _timestamp(text: Optional[str]) -> Optional[float]:
    """Graph's '2026-11-10T09:15:02Z' (or with fraction) -> seconds since 1970 (UTC)."""
    if not text:
        return None
    try:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.timestamp()
