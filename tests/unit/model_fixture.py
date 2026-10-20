"""Builds small model packages (CONTRACTS 12) for the tests. Not a test module itself."""

import hashlib
import json
import os

CLASSES = ["wheel", "half_wheel", "plate", "bucket_clip", "nut_bolt", "dusty_chain_base"]
LABEL_MAP = {"dusty_chain_base": "bucket_clip"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_package(models_root, name, version, *, manifest_overrides=None, classes_doc=None,
                 files=None, set_current=True, ready=True):
    """Write <models_root>/<name>/<version>/ with weights, Detectron yaml, classes.json and a
    manifest whose checksums match. Returns the version folder."""
    folder = os.path.join(models_root, name, version)
    os.makedirs(folder, exist_ok=True)
    contents = files if files is not None else {
        "model_final.pth": b"fake weights " * 100,
        "mask_rcnn.yaml": b"MODEL:\n  ROI_HEADS:\n    NUM_CLASSES: 6\n",
    }
    classes_doc = classes_doc if classes_doc is not None else {"classes": CLASSES, "label_map": LABEL_MAP}
    contents = dict(contents, **{"classes.json": json.dumps(classes_doc).encode()})
    for file_name, data in contents.items():
        with open(os.path.join(folder, file_name), "wb") as f:
            f.write(data)
    manifest = {"name": name, "version": version, "trained_at": "2026-10-01",
                "files": {n: sha256_bytes(d) for n, d in contents.items()}}
    manifest.update(manifest_overrides or {})
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    if ready:
        open(os.path.join(folder, "ready"), "w").close()
    if set_current:
        pointer = os.path.join(models_root, name, "current")
        if os.path.lexists(pointer):
            os.remove(pointer)
        os.symlink(version, pointer)
    return folder
