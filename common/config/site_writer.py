"""Write site.yaml from a dict, safely (S2, D-103 Apply, D-062).

    from common.config.site_writer import write_site
    result = write_site(data)                 # validates, backs up, writes; raises SiteConfigError
    result = write_site(data, dry_run=True)   # the same answers, nothing written
    result.changes   -> [KeyChange("inspection.threshold", "changed", 0.5, 0.6), ...]
    result.workers   -> {"inspection_left": "written", ...}   workers whose rendered file would change
    result.backup    -> <root>/config/backup/site.yaml.<time> (None when there was no file)

Order (nothing is replaced until the new content is known to be valid):
  1. the dict is dumped into a temp file next to site.yaml (mode 600 from the first byte) and
     loaded with load_site_config: every loader rule applies, problems are raised as SiteConfigError;
  2. the diff against the current file and a dry-run render say what would change;
  3. the current file is copied to <root>/config/backup/ (last 20 kept, mode 600);
  4. the temp file is renamed over site.yaml (atomic) and kept at mode 600.
Rendering itself is left to the caller (the Supervisor's reload renders, D-062).

Secrets never leave this module as values: a changed secret shows as "changed" only
(db.user / db.password, mqtt login, graph keys, mail.smtp.password, or any key that looks secret).
Comments of a hand-written site.yaml are not kept (the old file stays in the backup).
"""

import dataclasses
import datetime
import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

from common.config import render as renderer
from common.config.site_config import (
    SECRET_KEY_PATHS, SiteConfigError, _SECRET_NAME_RE, default_config_path, load_site_config,
)

BACKUP_KEEP = 20
BACKUP_PREFIX = "site.yaml."
HEADER = ("# DPC site config, written by common/config/site_writer.py (setup / UI) at {at}.\n"
          "# Holds secrets: keep chmod 600, never commit. The previous version is in config/backup/.\n")
SECRET_VALUE = "***"


@dataclass(frozen=True)
class KeyChange:
    key: str              # dotted path, list items as [i]: capture.cameras[1].exposure_us
    kind: str             # added | removed | changed
    old: Any = None       # None for a secret (never the value)
    new: Any = None
    secret: bool = False

    def text(self) -> str:
        if self.secret:
            return f"{self.key}: {self.kind}"
        if self.kind == "added":
            return f"{self.key}: added ({self.new!r})"
        if self.kind == "removed":
            return f"{self.key}: removed (was {self.old!r})"
        return f"{self.key}: {self.old!r} -> {self.new!r}"


@dataclass(frozen=True)
class WriteResult:
    path: str
    written: bool                 # False for a dry run
    backup: Optional[str]         # the copy of the previous file, None if there was none (or dry run)
    changes: List[KeyChange]
    workers: Dict[str, str]       # worker_id -> written | removed (unchanged ones left out)


# ======================================================================
# Diff
# ======================================================================

def _is_secret(path: Tuple[Any, ...]) -> bool:
    names = tuple(p for p in path if isinstance(p, str))
    return names in SECRET_KEY_PATHS or any(_SECRET_NAME_RE.search(n) for n in names)


def _key(path: Tuple[Any, ...]) -> str:
    text = ""
    for part in path:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


def diff_site(old: Any, new: Any, path: Tuple[Any, ...] = ()) -> List[KeyChange]:
    """Key-level differences between two site.yaml dicts. Mappings and equally long lists are
    compared item by item; anything else as one value."""
    if isinstance(old, dict) and isinstance(new, dict):
        changes = []
        for key in list(old) + [k for k in new if k not in old]:
            changes += diff_site(old.get(key, _ABSENT), new.get(key, _ABSENT), path + (key,))
        return changes
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        changes = []
        for i, (a, b) in enumerate(zip(old, new)):
            changes += diff_site(a, b, path + (i,))
        return changes
    if old == new:
        return []
    secret = _is_secret(path)
    kind = "added" if old is _ABSENT else "removed" if new is _ABSENT else "changed"
    if secret:
        return [KeyChange(_key(path), kind, secret=True)]
    return [KeyChange(_key(path), kind, None if old is _ABSENT else old, None if new is _ABSENT else new)]


class _Absent:
    def __repr__(self):
        return "<absent>"


_ABSENT = _Absent()


# ======================================================================
# Backup and write
# ======================================================================

def backup_dir_for(path: str) -> str:
    return os.path.join(os.path.dirname(path), "backup")


def list_backups(path: Optional[str] = None) -> List[str]:
    """Backups of this site.yaml, oldest first."""
    folder = backup_dir_for(os.path.abspath(os.path.expanduser(path)) if path else default_config_path())
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.startswith(BACKUP_PREFIX))


def _backup(path: str, keep: int) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    folder = backup_dir_for(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(folder, BACKUP_PREFIX + stamp)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
        shutil.copyfileobj(src, out)
    for old in list_backups(path)[:-keep] if keep > 0 else []:
        os.remove(old)                                        # keep the newest `keep`
    return target


def _dump(data: Dict[str, Any]) -> str:
    at = datetime.datetime.now().isoformat(timespec="seconds")
    return HEADER.format(at=at) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _read_current(path: str) -> Dict[str, Any]:
    """The current file for the diff (no permission rule here: it is only compared)."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def write_site(data: Dict[str, Any], path: Optional[str] = None, dry_run: bool = False,
               keep: int = BACKUP_KEEP) -> WriteResult:
    """Validate `data` as site.yaml, then (unless dry_run) back up and replace the file.
    Raises SiteConfigError naming the target file; nothing is changed then."""
    path = os.path.abspath(os.path.expanduser(path)) if path else default_config_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    temp = os.path.join(folder, f".site.yaml.new-{os.getpid()}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_dump(data))
            f.flush()
            os.fsync(f.fileno())
        try:
            cfg = load_site_config(temp)
        except SiteConfigError as exc:
            raise SiteConfigError(path, exc.problems) from None
        cfg = dataclasses.replace(cfg, source_path=path)       # as it will be once in place
        changes = diff_site(_read_current(path), data)
        workers = {c.worker_id: c.status for c in renderer.plan(cfg) if c.status != renderer.UNCHANGED}
        if dry_run:
            return WriteResult(path, False, None, changes, workers)
        backup = _backup(path, keep)
        os.replace(temp, path)
        os.chmod(path, 0o600)
        return WriteResult(path, True, backup, changes, workers)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
