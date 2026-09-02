import logging
from typing import List, Dict
import cv2
import numpy as np

logger = logging.getLogger(__name__)

try:
    from ultralytics import YOLO
    HAS_ULTRALYTICS = True
except ImportError:
    HAS_ULTRALYTICS = False
    YOLO = None

from common.config import YOLO_MODEL, YOLO_CONF, YOLO_CLASSES

class Detector:
    def __init__(self, model_path: str = YOLO_MODEL, conf: float = YOLO_CONF, classes: str = YOLO_CLASSES):
        self.conf = conf
        self.model_path = model_path
        self.filter_classes: set[int] | None = None
        if classes.strip():
            try:
                self.filter_classes = set(int(x.strip()) for x in classes.split(",") if x.strip())
            except:
                pass
        self.model = None
        self.names: Dict[int, str] = {}
        self._load()

    def _load(self):
        if not HAS_ULTRALYTICS:
            logger.warning("[detector] ultralytics missing, dummy mode")
            return
        try:
            logger.info(f"[detector] loading {self.model_path}")
            self.model = YOLO(self.model_path)
            self.names = self.model.names
        except Exception as e:
            logger.exception(f"[detector] load failed {e}")
            self.model = None

    def infer(self, frame_bgr: np.ndarray) -> List[Dict]:
        if self.model is None:
            return []
        try:
            results = self.model.predict(
                source=frame_bgr, conf=self.conf, verbose=False,
                classes=list(self.filter_classes) if self.filter_classes else None,
            )
            if not results or results[0].boxes is None or len(results[0].boxes) == 0:
                return []
            detections = []
            for box in results[0].boxes:
                xyxy = box.xyxy[0].tolist()
                detections.append({
                    "cls": self.names.get(int(box.cls[0].item()), str(int(box.cls[0].item()))),
                    "cls_id": int(box.cls[0].item()),
                    "conf": float(box.conf[0].item()),
                    "xyxy": [int(v) for v in xyxy],
                })
            return detections
        except Exception as e:
            logger.warning(f"[detector] infer error {e}")
            return []

_detector: Detector | None = None

def get_detector() -> Detector:
    global _detector
    if _detector is None:
        _detector = Detector()
    return _detector
