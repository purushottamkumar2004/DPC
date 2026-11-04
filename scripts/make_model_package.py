#!/usr/bin/env python3
"""Package a trained model as a model package (STATUS 6b, F49, CONTRACTS 12, D-056, D-060, D-088).

Builds <models-dir>/<name>/<version>/ on the site with:
    <weights>.pth          copied from --weights
    <config>.yaml          the shared config models/configs/mask_rcnn_R101_FPN_3x.yaml (or
                           --detectron-yaml) with MODEL.ROI_HEADS.NUM_CLASSES = the class count
    classes.json           {"classes": [...], "label_map": {...}}
    manifest.json          name, version, trained_at, sha256 of every file (written last)
    ready                  marker: verified after building
and points <name>/current at it (first install only; afterwards the Supervisor moves `current`).

Normal way (D-088): the weights and the training dataset JSON of that model, both loaded on the site:
    make_model_package.py --name dpc_maskrcnn --version 2026.11.10 \
        --weights /path/to/model_final.pth --classes-from-coco /path/to/train.json [--legacy-label-map]
The class order is the COCO `categories` sorted by id. Other class sources (--classes,
--classes-file, --classes-from-xml) still work but are not the normal way.

Also:
  - a version kept in git under models/<name>/<version>/ (D-065, history only since D-088):
       make_model_package.py --from-repo models/dpc_maskrcnn/2026.10.12 --weights /path/to/model_final.pth
  - --export-to-repo is retired (D-088): new versions are not stored in git.

Class order matters: index i of the model output is class i, so the list is kept exactly as
given. Source files are only read and copied, never changed. Nothing is written unless every
input is valid; the version folder appears in one rename, so a half-built package is never seen.
"""

import argparse
import ast
import datetime
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common.config import model_package as mp  # noqa: E402

import yaml  # noqa: E402

SHARED_DETECTRON_YAML = os.path.join(REPO_ROOT, "models", "configs", "mask_rcnn_R101_FPN_3x.yaml")   # D-088

DEFAULT_MODELS_DIR = "~/INSIGHTZZ/data/models"

# The label mapping hardcoded today in changeLabelName() of workers/inspection/LeftAlgorithm.py
# and RightAlgorithm.py (copied, not imported: those files are scripts with side effects).
# Business logic is unchanged: these become label_map entries in classes.json (D-056).
LEGACY_LABEL_MAP = {
    "dusty_chain_base": "bucket_clip",
    "dusty_chain_base_nut_bolt": "nut_bolt",
    "dusty_chain_bolt": "chain_bolt",
    "dusty_chain": "chain",
    "missing_wheel_bolt": "bolt_missing",
    "dusty_wheel_bolt": "wheel_bolt",
}


class PackagingError(Exception):
    pass


# ======================================================================
# Inputs
# ======================================================================

def classes_from_xml(path: str) -> List[str]:
    """The <ALL_CLASS_NAMES> list of a legacy leftConfig.xml / rightConfig.xml (found by tag,
    not by position)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise PackagingError(f"--classes-from-xml: cannot read {path} ({type(exc).__name__})") from None
    element = root if root.tag == "ALL_CLASS_NAMES" else root.find(".//ALL_CLASS_NAMES")
    if element is None or not (element.text or "").strip():
        raise PackagingError(f"--classes-from-xml: no <ALL_CLASS_NAMES> in {path}")
    try:
        value = ast.literal_eval(element.text.strip())
    except (ValueError, SyntaxError):
        raise PackagingError(f"--classes-from-xml: <ALL_CLASS_NAMES> in {path} is not a list") from None
    return list(value) if isinstance(value, (list, tuple)) else []


def classes_from_coco(path: str) -> List[str]:
    """The class list of a training dataset in COCO format: `categories` sorted by `id` (D-088).
    Refuses a file without categories, ids that are not unique integers, and missing or
    duplicate names: any of those would shift every class index silently."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise PackagingError(f"--classes-from-coco: cannot read {path} ({type(exc).__name__})") from None
    categories = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(categories, list) or not categories:
        raise PackagingError(f"--classes-from-coco: {path} has no `categories` list")
    problems, ids, names = [], set(), set()
    for i, cat in enumerate(categories):
        cat_id = cat.get("id") if isinstance(cat, dict) else None
        name = cat.get("name") if isinstance(cat, dict) else None
        if isinstance(cat_id, bool) or not isinstance(cat_id, int):
            problems.append(f"categories[{i}]: id must be an integer")
        elif cat_id in ids:
            problems.append(f"categories[{i}]: id {cat_id} appears more than once")
        else:
            ids.add(cat_id)
        if not isinstance(name, str) or not name.strip():
            problems.append(f"categories[{i}]: name missing")
        elif name in names:
            problems.append(f"categories[{i}]: name {name!r} appears more than once")
        else:
            names.add(name)
    if problems:
        raise PackagingError("--classes-from-coco: " + "; ".join(problems))
    return [cat["name"] for cat in sorted(categories, key=lambda c: c["id"])]


def classes_from_file(path: str) -> List[str]:
    """A JSON list of names, or a classes.json object with a "classes" list."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise PackagingError(f"--classes-file: cannot read {path} ({type(exc).__name__})") from None
    return list(data.get("classes", [])) if isinstance(data, dict) else list(data) if isinstance(data, list) else []


def parse_label_map(text: str) -> Dict[str, str]:
    """"a=b,c=d" -> {"a": "b", "c": "d"}."""
    result = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        if pair.count("=") != 1 or not all(part.strip() for part in pair.split("=")):
            raise PackagingError(f"--label-map: {pair!r} is not model_class=label")
        key, value = (part.strip() for part in pair.split("="))
        result[key] = value
    return result


def collect_classes(args) -> List[str]:
    sources = [s for s in (args.classes_from_coco, args.classes, args.classes_file, args.classes_from_xml) if s]
    if len(sources) != 1:
        raise PackagingError("give exactly one of --classes-from-coco, --classes, --classes-file, --classes-from-xml")
    if args.classes_from_coco:
        return classes_from_coco(args.classes_from_coco)
    if args.classes:
        return [c.strip() for c in args.classes.split(",") if c.strip()]
    if args.classes_file:
        return classes_from_file(args.classes_file)
    return classes_from_xml(args.classes_from_xml)


def collect_label_map(args, classes: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """The label map, and the legacy entries left out because the model has no such class."""
    label_map, skipped = {}, []
    if args.legacy_label_map:
        for key, value in LEGACY_LABEL_MAP.items():
            if key in classes:
                label_map[key] = value
            else:
                skipped.append(key)
    if args.label_map:
        label_map.update(parse_label_map(args.label_map))   # explicit entries win
    return label_map, skipped


def check_inputs(args, classes: List[str], label_map: Dict[str, str]) -> None:
    problems = []
    for flag, value in (("--name", args.name), ("--version", args.version)):
        if not mp._PART_RE.match(value) or value in (mp.CURRENT_POINTER, mp.PINNED_MARKER):
            problems.append(f"{flag}: {value!r} must be a folder name (letters, digits, '.', '_', '-')")
    if not os.path.isfile(args.weights) or not args.weights.lower().endswith(mp.WEIGHTS_EXTENSIONS):
        problems.append(f"--weights: {args.weights} is not an existing .pth file")
    if not os.path.isfile(args.detectron_yaml) or not args.detectron_yaml.lower().endswith(mp.CONFIG_EXTENSIONS):
        problems.append(f"--detectron-yaml: {args.detectron_yaml} is not an existing .yaml/.yml file")
    reserved = {mp.MANIFEST_FILE, mp.CLASSES_FILE, mp.READY_FILE}
    for flag, path in (("--weights", args.weights), ("--detectron-yaml", args.detectron_yaml)):
        if os.path.basename(path) in reserved:
            problems.append(f"{flag}: the file name {os.path.basename(path)} is reserved in a package")
    if not classes or not all(isinstance(c, str) and c.strip() for c in classes):
        problems.append("classes: the list is empty or has a non-name entry")
    elif len(set(classes)) != len(classes):
        problems.append("classes: a name appears more than once")
    for key in label_map:
        if key not in classes:
            problems.append(f"label map: {key!r} is not one of the classes")
    if problems:
        raise PackagingError("\n".join(problems))


def config_with_num_classes(text: str, num_classes: int) -> str:
    """The Detectron config text with MODEL.ROI_HEADS.NUM_CLASSES set (only that key: other
    heads such as RETINANET keep theirs). Line edit, so comments and layout stay; checked by
    parsing the result."""
    out, in_model, in_roi, done = [], False, False, False
    for line in text.splitlines(keepends=True):
        if line[:1] not in (" ", "\t", "\n", "#", ""):
            in_model, in_roi = line.startswith("MODEL:"), False
        elif in_model and line.startswith("  ") and line[2:3] not in (" ", "#", "\n", "-"):
            in_roi = line.startswith("  ROI_HEADS:")
        if in_roi and not done and line.startswith("    NUM_CLASSES:"):
            line = f"    NUM_CLASSES: {num_classes}\n"
            done = True
        out.append(line)
    result = "".join(out)
    try:
        parsed = yaml.safe_load(result)
        ok = done and parsed["MODEL"]["ROI_HEADS"]["NUM_CLASSES"] == num_classes
    except (yaml.YAMLError, KeyError, TypeError):
        ok = False
    if not ok:
        raise PackagingError("the Detectron config has no MODEL: ROI_HEADS: NUM_CLASSES: line to set "
                             "(use a self-contained config, e.g. models/configs/mask_rcnn_R101_FPN_3x.yaml)")
    return result


def base_config_warning(path: str) -> Optional[str]:
    """Detectron configs can include another file with _BASE_; that file is not packaged."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if any(line.lstrip().startswith("_BASE_") for line in f):
            return (f"{os.path.basename(path)} uses _BASE_: the base file is not in the package; "
                    f"make sure inspection can still find it, or package a merged config")
    return None


# ======================================================================
# Build
# ======================================================================

def _assemble(models_dir: str, name: str, version: str, copies: List[Tuple[str, str]],
              classes_doc: Optional[Dict] = None, manifest: Optional[Dict] = None, trained_at: str = "",
              packaged_from: Optional[Dict[str, str]] = None, texts: Optional[Dict[str, str]] = None) -> str:
    """Copy and write everything in a temp folder next to the target, then rename it into place.
    copies: (source, name in the package). manifest None = write a new one with fresh sha256s;
    given = copy it as it is (from the repo, already checked)."""
    model_root = os.path.join(models_dir, name)
    target = os.path.join(model_root, version)
    if os.path.lexists(target):
        raise PackagingError(f"{target} already exists; a version is never overwritten, choose a new --version")
    os.makedirs(model_root, exist_ok=True)
    temp = os.path.join(model_root, f".{version}.building-{os.getpid()}")
    shutil.rmtree(temp, ignore_errors=True)
    os.makedirs(temp)
    try:
        for source, dest in copies:
            shutil.copyfile(source, os.path.join(temp, dest))                  # read-only on the source
        for file_name, text in (texts or {}).items():
            with open(os.path.join(temp, file_name), "w", encoding="utf-8") as f:
                f.write(text)
        if classes_doc is not None:
            with open(os.path.join(temp, mp.CLASSES_FILE), "w", encoding="utf-8") as f:
                json.dump(classes_doc, f, indent=2)
        if manifest is None:
            files = {n: mp.sha256_of(os.path.join(temp, n)) for n in sorted(os.listdir(temp))}
            manifest = {"name": name, "version": version, "trained_at": trained_at, "files": files,
                        "packaged_at": datetime.datetime.now().isoformat(timespec="seconds")}
            if packaged_from:
                manifest["packaged_from"] = packaged_from
        with open(os.path.join(temp, mp.MANIFEST_FILE), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)                                     # last, as on OneDrive
        os.rename(temp, target)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return target


def build_package(models_dir: str, name: str, version: str, weights: str, detectron_yaml: str,
                  classes: List[str], label_map: Dict[str, str], trained_at: str) -> str:
    """A new package: the weights, the config with NUM_CLASSES = the class count, classes.json."""
    with open(detectron_yaml, "r", encoding="utf-8") as f:
        config_text = config_with_num_classes(f.read(), len(classes))
    return _assemble(models_dir, name, version,
                     copies=[(weights, os.path.basename(weights))],
                     texts={os.path.basename(detectron_yaml): config_text},
                     classes_doc={"classes": classes, "label_map": label_map}, trained_at=trained_at,
                     packaged_from={"weights": os.path.abspath(weights),
                                    "detectron_yaml": os.path.abspath(detectron_yaml)})


# ======================================================================
# Git (D-065, history only since D-088): install a version kept under models/<name>/<version>/
# ======================================================================

def _weights_name(manifest: "mp.Manifest") -> str:
    return next(n for n in manifest.files if n.lower().endswith(mp.WEIGHTS_EXTENSIONS))


def build_from_repo(models_dir: str, repo_version_dir: str, weights: str) -> str:
    """Install a version that is already in git: the repo's small files plus the weights,
    which must have the sha256 the repo manifest records."""
    repo_version_dir = os.path.abspath(os.path.expanduser(repo_version_dir))
    if not os.path.isdir(repo_version_dir):
        raise PackagingError(f"--from-repo: {repo_version_dir} is not a folder")
    try:
        manifest = mp.read_manifest(repo_version_dir)
    except mp.ModelPackageError as exc:
        raise PackagingError("--from-repo: " + "; ".join(exc.problems)) from None
    problems = []
    folder_version = os.path.basename(repo_version_dir)
    folder_name = os.path.basename(os.path.dirname(repo_version_dir))
    if (manifest.name, manifest.version) != (folder_name, folder_version):
        problems.append(f"--from-repo: manifest says {manifest.name}/{manifest.version}, "
                        f"the folder is {folder_name}/{folder_version}")
    weights_name = _weights_name(manifest)
    for file_name, expected in manifest.files.items():
        if file_name == weights_name:
            continue
        path = os.path.join(repo_version_dir, file_name)
        if not os.path.isfile(path):
            problems.append(f"--from-repo: {file_name} is missing in the repo folder")
        elif mp.sha256_of(path) != expected:
            problems.append(f"--from-repo: {file_name} in the repo does not match its manifest sha256")
    if not os.path.isfile(weights):
        problems.append(f"--weights: {weights} is not a file")
    elif mp.sha256_of(weights) != manifest.files[weights_name]:
        problems.append(f"--weights: sha256 differs from the repo manifest for {weights_name}; "
                        f"these are not the weights of {manifest.name}/{manifest.version}")
    if problems:
        raise PackagingError("\n".join(problems))
    with open(os.path.join(repo_version_dir, mp.MANIFEST_FILE), "r", encoding="utf-8") as f:
        manifest_doc = json.load(f)                       # copied as it is: the repo is the reference
    copies = [(os.path.join(repo_version_dir, n), n) for n in manifest.files if n != weights_name]
    copies.append((weights, weights_name))
    return _assemble(models_dir, manifest.name, manifest.version, copies, manifest=manifest_doc)


def _set_current_first_install(models_dir: str, name: str, version: str, no_current: bool) -> None:
    model_root = os.path.join(models_dir, name)
    if no_current:
        print("  current not changed (--no-current)")
        return
    pinned = mp.read_pinned(model_root)
    current = mp.read_current(model_root)
    if pinned is not None or current is not None:
        # After the first install the Supervisor owns `current` (D-060, D-061).
        print(f"  current not changed: it is already set ({current}{', pinned' if pinned else ''}); "
              f"switch with the Supervisor (supervisor.py model switch {name} {version})")
        return
    mp.set_current(model_root, version)
    print(f"  current -> {version}")


def _mode(args) -> str:
    """from_repo | build, with the flags each mode does not take refused."""
    if args.export_to_repo:
        raise PackagingError("--export-to-repo is retired (D-088): new versions are not stored in git; "
                             "the shared config is models/configs/mask_rcnn_R101_FPN_3x.yaml")
    loose = [f for f, v in (("--detectron-yaml", args.detectron_yaml), ("--classes-from-coco", args.classes_from_coco),
                            ("--classes", args.classes),
                            ("--classes-file", args.classes_file), ("--classes-from-xml", args.classes_from_xml),
                            ("--label-map", args.label_map), ("--legacy-label-map", args.legacy_label_map),
                            ("--trained-at", args.trained_at)) if v]
    if args.from_repo:
        extra = loose + [f for f, v in (("--name", args.name), ("--version", args.version)) if v]
        if extra:
            raise PackagingError(f"--from-repo takes only --weights (and --models-dir, --no-current); "
                                 f"remove {', '.join(extra)}")
        if not args.weights:
            raise PackagingError("--from-repo needs --weights")
        return "from_repo"
    if not args.name or not args.version:
        raise PackagingError("give --name and --version (or --from-repo)")
    if not args.weights:
        raise PackagingError("a new package needs --weights")
    return "build"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="make_model_package.py",
                                     description="Package a model (CONTRACTS 12, D-088).")
    parser.add_argument("--name", help="model name, as in site.yaml inspection.model.name")
    parser.add_argument("--version", help="version folder name, e.g. 2026.10.1")
    parser.add_argument("--weights", help="the .pth weights file (copied, never changed)")
    parser.add_argument("--detectron-yaml", help="override the shared config models/configs/mask_rcnn_R101_FPN_3x.yaml "
                                                 "(self-contained; NUM_CLASSES is set from the classes)")
    parser.add_argument("--classes-from-coco", help="training dataset JSON (COCO): categories sorted by id (normal way)")
    parser.add_argument("--classes", help="class names in model output order, comma separated")
    parser.add_argument("--classes-file", help="JSON list of class names (or a classes.json)")
    parser.add_argument("--classes-from-xml", help="legacy leftConfig.xml/rightConfig.xml: uses <ALL_CLASS_NAMES>")
    parser.add_argument("--label-map", default="", help="model_class=label pairs, comma separated")
    parser.add_argument("--legacy-label-map", action="store_true",
                        help="add today's changeLabelName() mapping (entries for classes the model has)")
    parser.add_argument("--trained-at", help="training date (default: date of the weights file)")
    parser.add_argument("--from-repo", help="models/<name>/<version> folder in git: install it with --weights")
    parser.add_argument("--export-to-repo", action="store_true", help=argparse.SUPPRESS)   # retired (D-088)
    parser.add_argument("--models-dir", default=DEFAULT_MODELS_DIR, help=f"default {DEFAULT_MODELS_DIR}")
    parser.add_argument("--no-current", action="store_true", help="do not point current at the new version")
    args = parser.parse_args(argv)
    models_dir = os.path.abspath(os.path.expanduser(args.models_dir))

    try:
        mode = _mode(args)
        if mode == "from_repo":
            target = build_from_repo(models_dir, args.from_repo, args.weights)
            package = mp.verify_package(target)
            open(os.path.join(target, mp.READY_FILE), "w").close()
            print(f"Installed {package.name}/{package.version} from {args.from_repo} in {target}")
            print(f"  weights match the repo manifest; {len(package.classes)} classes")
            _set_current_first_install(models_dir, package.name, package.version, args.no_current)
            return 0

        args.detectron_yaml = args.detectron_yaml or SHARED_DETECTRON_YAML     # D-088: shared by default
        classes = collect_classes(args)
        label_map, skipped = collect_label_map(args, classes)
        check_inputs(args, classes, label_map)
        trained_at = args.trained_at or datetime.date.fromtimestamp(os.path.getmtime(args.weights)).isoformat()
        warning = base_config_warning(args.detectron_yaml)
        target = build_package(models_dir, args.name, args.version, args.weights, args.detectron_yaml,
                               classes, label_map, trained_at)
        package = mp.verify_package(target, name=args.name, version=args.version)
        open(os.path.join(target, mp.READY_FILE), "w").close()          # verified: ready to run
    except (PackagingError, mp.ModelPackageError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"Packaged {args.name}/{args.version} in {target}")
    print(f"  {len(package.classes)} classes, label map {package.label_map or '{}'}, trained_at {trained_at}")
    if skipped:
        print(f"  legacy label map entries skipped (no such class in this model): {', '.join(skipped)}")
    if warning:
        print(f"  WARNING {warning}")
    print(f"  config {os.path.basename(args.detectron_yaml)} with NUM_CLASSES {len(package.classes)}"
          + (" (shared, D-088)" if os.path.abspath(args.detectron_yaml) == SHARED_DETECTRON_YAML else ""))
    _set_current_first_install(models_dir, args.name, args.version, args.no_current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
