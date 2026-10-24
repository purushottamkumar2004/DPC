# Model metadata (D-065)

One folder per packaged model version: `models/<name>/<version>/` with
- `manifest.json`: version, training date, and the sha256 of every file, **including the weights**;
- `classes.json`: class list in the model's output order, plus the label map;
- the self-contained Detectron config (`.yaml`).

**The weights (`.pth`) are never here.** They come from OneDrive (D-057) or a manual copy and are checked
against the sha256 in `manifest.json`. `.gitignore` blocks `*.pth`, `*.pt`, `*.pkl`, `*.onnx`.

On a site, the running package lives in `~/INSIGHTZZ/data/models/<name>/<version>/` (weights included);
the Supervisor's `current` pointer selects the version (D-060).
