# Models (D-056, D-086, D-088)

## `models/configs/`: the shared Detectron config (in git)
`models/configs/mask_rcnn_R101_FPN_3x.yaml` is the one network configuration used by every model and site (self-contained, flattened). A package gets a copy with `MODEL.ROI_HEADS.NUM_CLASSES` set to its class count; inspection also sets `MODEL.WEIGHTS` from the package and does not use `DATASETS` (`workers/inspection/model.py`).

## Weights and class list: on the site, never in git
Every trained model brings its own weights (`.pth`) and its own training dataset JSON (class list and order). They are copied to the site (later by the transfer service, P12) and packaged there:

```
python3 scripts/make_model_package.py --name <model name> --version <yyyy.mm.dd> \
    --weights <path to .pth> --classes-from-coco <path to the training dataset .json>
```
The class order is the COCO `categories` sorted by `id`. The package lands in `~/INSIGHTZZ/data/models/<name>/<version>/` (weights, config copy, `classes.json`, `manifest.json` with checksums). Switch to it with `supervisor.py model switch <name> <version>`.

## `models/<name>/<version>/`: history only
`models/dpc_maskrcnn/2026.10.12/` holds the small files of the first Tadipatri package (D-065, superseded by D-088). New versions are not added here. `make_model_package.py --from-repo` still rebuilds that version from its weights.

**Never commit `.pth`, `.pt`, `.pkl`, `.onnx`** (blocked by `.gitignore`).
