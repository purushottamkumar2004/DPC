"""Read and verify model packages (CONTRACTS 12, D-056).

A model version is a folder:
    <root>/data/models/<name>/<version>/
        manifest.json    {"name", "version", "trained_at", "files": {"<file>": "<sha256>"}, ...}
        classes.json     {"classes": [...], "label_map": {"<model class>": "<label>"}}
        <weights>.pth    exactly one
        <config>.yaml    exactly one (Detectron config)
        ready            optional marker written by the transfer service after verifying (D-057)
    <root>/data/models/<name>/current  -> <version>   (symlink; the running version)
    <root>/data/models/<name>/pinned                  (optional; holds the pinned version)
`current` and `pinned` are written only by the Supervisor (D-060, one-writer rule D-054);
the packaging tool sets `current` only when installing a model for the first time.

Public API
    verify_package(version_dir, name=None, version=None) -> ModelPackage   (checks every sha256)
    read_manifest(version_dir) -> Manifest                                  (no checksums)
    read_current(model_root) -> Optional[str]      version `current` points to, None if not set
    read_pinned(model_root) -> Optional[str]       pinned version, None if not pinned
    set_current(model_root, version)               atomic switch of `current` (for the Supervisor)
    running_version(config) -> Optional[str]       the version `current` points to (D-060)
    load_model_package(config) -> ModelPackage     verified package `current` points to
    ModelPackageError (.problems)

Why so strict: a half-copied or corrupted weights file still loads in Detectron2 and gives
wrong detections without any error, so every file is checked against its sha256 first.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

MANIFEST_FILE = "manifest.json"
CLASSES_FILE = "classes.json"
READY_FILE = "ready"
CURRENT_POINTER = "current"
PINNED_MARKER = "pinned"
WEIGHTS_EXTENSIONS = (".pth",)
CONFIG_EXTENSIONS = (".yaml", ".yml")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")      # same rule as site_config for name/version
_HASH_CHUNK_BYTES = 1024 * 1024


class ModelPackageError(Exception):
    """The package is unusable. `problems` lists every problem found."""

    def __init__(self, source: str, problems: List[str]):
        self.source = source
        self.problems = list(problems)
        lines = [f"{source}: {len(self.problems)} problem(s)"] + [f"  - {p}" for p in self.problems]
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    trained_at: str
    files: Dict[str, str]          # relative file name -> sha256 (lowercase hex)
    extra: Dict[str, Any]          # any other keys (append-only contract)


@dataclass(frozen=True)
class ModelPackage:
    name: str
    version: str
    dir: str
    trained_at: str
    weights: str                   # absolute path of the .pth file
    detectron_yaml: str            # absolute path of the Detectron config
    classes: Tuple[str, ...]
    label_map: Dict[str, str]      # e.g. {"dusty_chain_base": "bucket_clip"}
    ready: bool                    # the transfer service marked it ready (D-057)


# ======================================================================
# Manifest and classes.json
# ======================================================================

def _load_json(path: str, problems: List[str], label: str) -> Any:
    if not os.path.isfile(path):
        problems.append(f"{label}: missing")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        problems.append(f"{label}: not valid JSON ({type(exc).__name__})")
        return None


def _is_plain_relative(name: str) -> bool:
    """A file inside the version folder: no absolute path, no '..', no backslash."""
    if not name or os.path.isabs(name) or "\\" in name:
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _check_manifest(data: Any, problems: List[str]) -> Optional[Manifest]:
    if not isinstance(data, dict):
        problems.append(f"{MANIFEST_FILE}: must be a JSON object")
        return None
    values = {}
    for key in ("name", "version", "trained_at"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{MANIFEST_FILE}: {key} must be a non-empty string")
        values[key] = value
    for key in ("name", "version"):
        if isinstance(values[key], str) and values[key] and not _PART_RE.match(values[key]):
            problems.append(f"{MANIFEST_FILE}: {key} {values[key]!r} is not a valid folder name")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        problems.append(f"{MANIFEST_FILE}: files must be a non-empty object of file -> sha256")
        files = {}
    clean_files = {}
    for name, digest in files.items():
        if not _is_plain_relative(name) or name in (MANIFEST_FILE, READY_FILE):
            problems.append(f"{MANIFEST_FILE}: files: {name!r} is not a file inside the version folder")
        elif not isinstance(digest, str) or not _SHA256_RE.match(digest.lower()):
            problems.append(f"{MANIFEST_FILE}: files: {name}: not a sha256 (64 hex characters)")
        else:
            clean_files[name] = digest.lower()
    if CLASSES_FILE not in files:
        problems.append(f"{MANIFEST_FILE}: files must list {CLASSES_FILE}")
    for kind, extensions in (("weights", WEIGHTS_EXTENSIONS), ("Detectron config", CONFIG_EXTENSIONS)):
        matches = [n for n in files if n.lower().endswith(extensions)]
        if len(matches) != 1:
            problems.append(f"{MANIFEST_FILE}: files must list exactly one {kind} file "
                            f"({'/'.join(extensions)}), found {len(matches)}")
    if problems:
        return None
    extra = {k: v for k, v in data.items() if k not in ("name", "version", "trained_at", "files")}
    return Manifest(name=values["name"], version=values["version"], trained_at=values["trained_at"],
                    files=clean_files, extra=extra)


def read_manifest(version_dir: str) -> Manifest:
    """Read and check manifest.json (structure only; verify_package also checks the files)."""
    problems: List[str] = []
    data = _load_json(os.path.join(version_dir, MANIFEST_FILE), problems, MANIFEST_FILE)
    manifest = _check_manifest(data, problems) if data is not None else None
    if problems:
        raise ModelPackageError(version_dir, problems)
    return manifest


def _check_classes(data: Any, problems: List[str]) -> Tuple[Tuple[str, ...], Dict[str, str]]:
    """classes.json: {"classes": [...], "label_map": {...}} (format assumed, see CONTRACTS 12)."""
    if not isinstance(data, dict):
        problems.append(f"{CLASSES_FILE}: must be a JSON object with classes and label_map")
        return (), {}
    classes = data.get("classes")
    if (not isinstance(classes, list) or not classes
            or not all(isinstance(c, str) and c.strip() for c in classes)):
        problems.append(f"{CLASSES_FILE}: classes must be a non-empty list of names")
        classes = []
    elif len(set(classes)) != len(classes):
        problems.append(f"{CLASSES_FILE}: classes contains a name more than once")
    label_map = data.get("label_map", {})
    if not isinstance(label_map, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in label_map.items()):
        problems.append(f"{CLASSES_FILE}: label_map must map class names to label names")
        label_map = {}
    for key in label_map:
        if classes and key not in classes:
            problems.append(f"{CLASSES_FILE}: label_map key {key!r} is not in classes")
    unknown = sorted(set(data) - {"classes", "label_map"})
    if unknown:
        problems.append(f"{CLASSES_FILE}: unknown key(s) {', '.join(unknown)}")
    return tuple(classes), dict(label_map)


# ======================================================================
# Verify a version folder
# ======================================================================

def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_package(version_dir: str, name: Optional[str] = None, version: Optional[str] = None) -> ModelPackage:
    """Check manifest, every file's sha256, classes.json, and that name/version match the folder
    (and `name`/`version` when given). Raises ModelPackageError listing every problem."""
    version_dir = os.path.abspath(os.path.expanduser(version_dir))
    if not os.path.isdir(version_dir):
        raise ModelPackageError(version_dir, ["version folder not found"])
    manifest = read_manifest(version_dir)
    problems: List[str] = []

    folder_version = os.path.basename(version_dir)
    folder_name = os.path.basename(os.path.dirname(version_dir))
    for key, value, expected, where in (("name", manifest.name, folder_name, "the folder"),
                                        ("version", manifest.version, folder_version, "the folder"),
                                        ("name", manifest.name, name, "site.yaml"),
                                        ("version", manifest.version, version, "site.yaml")):
        if expected is not None and value != expected:
            problems.append(f"{MANIFEST_FILE}: {key} is {value!r} but {where} says {expected!r}")

    for file_name, expected_sha in sorted(manifest.files.items()):
        path = os.path.join(version_dir, file_name)
        if not os.path.isfile(path):
            problems.append(f"{file_name}: missing")
        elif sha256_of(path) != expected_sha:
            problems.append(f"{file_name}: checksum does not match the manifest (corrupt or incomplete copy)")

    classes, label_map = ((), {})
    if os.path.isfile(os.path.join(version_dir, CLASSES_FILE)):
        data = _load_json(os.path.join(version_dir, CLASSES_FILE), problems, CLASSES_FILE)
        if data is not None:
            classes, label_map = _check_classes(data, problems)
    listed = manifest.extra.get("classes")
    if listed is not None and classes and list(listed) != list(classes):
        problems.append(f"{MANIFEST_FILE}: classes differ from {CLASSES_FILE}")

    if problems:
        raise ModelPackageError(version_dir, problems)
    weights = next(n for n in manifest.files if n.lower().endswith(WEIGHTS_EXTENSIONS))
    config = next(n for n in manifest.files if n.lower().endswith(CONFIG_EXTENSIONS))
    return ModelPackage(name=manifest.name, version=manifest.version, dir=version_dir,
                        trained_at=manifest.trained_at,
                        weights=os.path.join(version_dir, weights),
                        detectron_yaml=os.path.join(version_dir, config),
                        classes=classes, label_map=label_map,
                        ready=os.path.isfile(os.path.join(version_dir, READY_FILE)))


# ======================================================================
# `current` pointer
# ======================================================================

def read_current(model_root: str) -> Optional[str]:
    """The version `<model_root>/current` points to, or None when it is not set.
    A symlink is the standard; a plain file holding the version name is also read."""
    pointer = os.path.join(model_root, CURRENT_POINTER)
    if os.path.islink(pointer):
        version = os.path.basename(os.path.normpath(os.readlink(pointer)))
    elif os.path.isfile(pointer):
        with open(pointer, "r", encoding="utf-8") as f:
            version = f.read().strip()
    elif os.path.exists(pointer):
        raise ModelPackageError(pointer, ["must be a symlink to a version folder"])
    else:
        return None
    if not _PART_RE.match(version or "") or version == CURRENT_POINTER:
        raise ModelPackageError(pointer, [f"points to {version!r}, which is not a version name"])
    return version


def read_pinned(model_root: str) -> Optional[str]:
    """The version `<model_root>/pinned` holds, or None when not pinned. The Supervisor writes it
    (`model pin <version>`, D-060); while it exists, no automatic switch happens."""
    marker = os.path.join(model_root, PINNED_MARKER)
    if not os.path.exists(marker):
        return None
    if not os.path.isfile(marker):
        raise ModelPackageError(marker, ["must be a file holding the pinned version"])
    with open(marker, "r", encoding="utf-8") as f:
        version = f.read().strip()
    if not _PART_RE.match(version) or version in (CURRENT_POINTER, PINNED_MARKER):
        raise ModelPackageError(marker, [f"holds {version!r}, which is not a version name"])
    return version


def set_current(model_root: str, version: str) -> None:
    """Point `current` at `version` atomically (new symlink, then rename over the old one),
    so a reader sees either the old or the new version, never nothing. For the Supervisor's
    model switch (D-056); verify the package first."""
    if not _PART_RE.match(version) or version in (CURRENT_POINTER, PINNED_MARKER):
        raise ModelPackageError(model_root, [f"{version!r} is not a version name"])
    if not os.path.isdir(os.path.join(model_root, version)):
        raise ModelPackageError(model_root, [f"version folder {version} not found"])
    pointer = os.path.join(model_root, CURRENT_POINTER)
    temp = os.path.join(model_root, f".{CURRENT_POINTER}.tmp-{os.getpid()}")
    if os.path.lexists(temp):
        os.remove(temp)
    os.symlink(version, temp)          # relative target: the folder can move as a whole
    os.replace(temp, pointer)


# ======================================================================
# From a loaded site.yaml
# ======================================================================

def running_version(config) -> Optional[str]:
    """The version `current` points to; None when no version is installed yet (D-060:
    site.yaml names only the model, the Supervisor manages the version)."""
    return read_current(config.inspection.model.root)


def load_model_package(config) -> ModelPackage:
    """The verified package `current` points to. Raises ModelPackageError, also when no
    version is installed yet."""
    model = config.inspection.model
    version = read_current(model.root)
    if version is None:
        raise ModelPackageError(model.root, [f"no {CURRENT_POINTER} pointer: no version of {model.name} "
                                             f"is installed (scripts/make_model_package.py, or the Supervisor)"])
    return verify_package(os.path.join(model.root, version), name=model.name)
