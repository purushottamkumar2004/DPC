"""Model loading and inference, LeftAlgorithm.py L:166-239 (INSPECTION.md 4).

The model comes from the package `<models_dir>/current` points to (CONTRACTS 12, D-060):
weights, Detectron yaml, class list and label map, every file checked against its sha256.
Detectron2 is imported only here, so the rest of the program can be tested without a GPU.
"""

import os
import time

from common.config.model_package import ModelPackageError, read_current, verify_package

SCORE_FILTER = 0.5     # hardcoded second filter after SCORE_THRESH_TEST (L:218, B4 kept)

# Label mapping of LeftAlgorithm.py changeLabelName (L:393-403), applied after the package's
# own label_map. None of these names is a class of the Tadipatri model (its label_map is empty);
# kept so a model that still has them behaves as before. Better home: that model's classes.json.
LEGACY_LABEL_MAP = {
    "dusty_chain_base": "bucket_clip",
    "dusty_chain_base_nut_bolt": "nut_bolt",
    "dusty_chain_bolt": "chain_bolt",
    "dusty_chain": "chain",
    "missing_wheel_bolt": "bolt_missing",
    "dusty_wheel_bolt": "wheel_bolt",
}


def load_package(models_dir: str, name: str):
    """The verified package `current` points to. Raises ModelPackageError."""
    version = read_current(models_dir)
    if version is None:
        raise ModelPackageError(models_dir, ["no 'current' version set (install the model with "
                                             "scripts/make_model_package.py, or switch with the Supervisor)"])
    return verify_package(os.path.join(models_dir, version), name=name)


class Detector:
    def __init__(self, package, threshold: float, logger):
        from detectron2.config import get_cfg                 # GPU libraries only here
        from detectron2.data import MetadataCatalog
        from detectron2.engine import DefaultPredictor

        self.log = logger
        self.package = package
        self.classes = list(package.classes)
        self.label_map = dict(package.label_map)
        MetadataCatalog.get(f"{package.name}_{package.version}").set(thing_classes=self.classes)
        cfg = get_cfg()
        cfg.merge_from_file(package.detectron_yaml)
        cfg.MODEL.ROI_HEADS.NUM_CLASSES = len(self.classes)
        cfg.OUTPUT_DIR = package.dir
        cfg.MODEL.WEIGHTS = package.weights
        cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = threshold
        self.predictor = DefaultPredictor(cfg)
        logger.info(f"Model Loaded... {package.name} {package.version} ({len(self.classes)} classes)")

    def detect(self, image):
        """[[score, ymin, ymax, xmin, xmax, class_name], ...] as L:218-234. Errors raise."""
        start = time.time() * 1000
        instances = self.predictor(image)["instances"].to("cpu")
        self.log.info(f"Time Taken For One Image to Predict: {time.time() * 1000 - start}")
        boxes = instances.pred_boxes.tensor.numpy()
        classes = instances.pred_classes.numpy()
        scores = instances.scores.numpy()
        detections = []
        for box, cls, score in zip(boxes, classes, scores):
            if score > SCORE_FILTER:
                detections.append([score, int(box[1]), int(box[3]), int(box[0]), int(box[2]), self.classes[cls]])
        return detections

    def label(self, class_name: str) -> str:
        """Model class name -> the name the checks use."""
        mapped = self.label_map.get(class_name, class_name)
        return LEGACY_LABEL_MAP.get(mapped, mapped)
