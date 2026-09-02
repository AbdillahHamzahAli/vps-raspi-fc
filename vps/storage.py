import json
import time
import asyncio
import logging
from pathlib import Path

import cv2
import numpy as np

from common.config import DETECTION_SAVE_DIR

logger = logging.getLogger(__name__)

SAVE_LOCK = asyncio.Lock()
_last_save_ms = 0

def _ensure_dir():
    p = Path(DETECTION_SAVE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p

def _annotate(bgr: np.ndarray, detections: list) -> np.ndarray:
    ann = bgr.copy()
    for d in detections:
        x1, y1, x2, y2 = d.get("xyxy", [0, 0, 0, 0])
        label = f"{d.get('cls','obj')} {d.get('conf',0):.2f}"
        cv2.rectangle(ann, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
        cv2.putText(ann, label, (int(x1), max(0, int(y1)-6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    return ann

async def save_detection(bgr: np.ndarray, detections: list, coords: dict | None, frame_id: int) -> dict | None:
    """
    Simpan frame terannotasi + json koordinat.
    Nama: YYYYMMDD_HHMMSS_{frame_id}_{cls}_{conf:.2f}.jpg/.json
    Returns info dict atau None jika throttle.
    """
    global _last_save_ms
    from common.config import DETECTION_SAVE_THROTTLE_MS

    now_ms = int(time.time() * 1000)
    if now_ms - _last_save_ms < DETECTION_SAVE_THROTTLE_MS:
        return None

    async with SAVE_LOCK:
        # double check after lock
        now_ms2 = int(time.time() * 1000)
        if now_ms2 - _last_save_ms < DETECTION_SAVE_THROTTLE_MS:
            return None
        _last_save_ms = now_ms2

        if not detections:
            return None
        top = max(detections, key=lambda d: d.get("conf", 0))
        cls = str(top.get("cls", "obj")).replace(" ", "_")
        conf = float(top.get("conf", 0))
        ts = time.time()
        ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
        # include ms to avoid collision if multiple in same second
        ms_part = int((ts % 1) * 1000)
        name = f"{ts_str}_{ms_part:03d}_{frame_id}_{cls}_{conf:.2f}"
        # sanitize filename
        name = "".join(c if c.isalnum() or c in "_-." else "_" for c in name)

        out_dir = _ensure_dir()
        jpg_path = out_dir / f"{name}.jpg"
        json_path = out_dir / f"{name}.json"

        annotated = _annotate(bgr, detections)

        # write jpg in thread pool (blocking)
        def _write():
            cv2.imwrite(str(jpg_path), annotated)
            # coords handling: ensure JSON serializable
            payload = {
                "id": name,
                "ts": ts,
                "ts_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                "frame_id": frame_id,
                "coords": coords,
                "detections": detections,
                "image": f"/detections/{jpg_path.name}",
                "image_path": str(jpg_path),
            }
            # add stale flag if coords too old
            if coords is not None:
                stale_ms = abs((coords.get("ts", ts) - ts) * 1000)
                payload["coords_stale_ms"] = stale_ms
                payload["coords_stale"] = stale_ms > 100
            else:
                payload["coords_stale"] = True
                payload["coords_stale_ms"] = None
            with open(json_path, "w") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            return payload

        payload = await asyncio.to_thread(_write)
        logger.info(f"[storage] saved {jpg_path.name} coords={coords} dets={len(detections)}")
        return payload

def list_detections(limit: int = 50, offset: int = 0) -> list:
    out_dir = Path(DETECTION_SAVE_DIR)
    if not out_dir.exists():
        return []
    files = sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    sliced = files[offset: offset+limit]
    result = []
    for p in sliced:
        try:
            data = json.loads(p.read_text())
            # ensure image url
            if "image" not in data:
                data["image"] = f"/detections/{p.stem}.jpg"
            result.append(data)
        except Exception as e:
            logger.warning(f"[storage] read {p} failed {e}")
    return result

def get_detection(id: str) -> dict | None:
    out_dir = Path(DETECTION_SAVE_DIR)
    # allow id with or without .json
    if id.endswith(".json"):
        p = out_dir / id
    else:
        p = out_dir / f"{id}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if "image" not in data:
            data["image"] = f"/detections/{p.stem}.jpg"
        return data
    except Exception as e:
        logger.warning(f"[storage] get {id} failed {e}")
        return None

def count_detections() -> int:
    out_dir = Path(DETECTION_SAVE_DIR)
    if not out_dir.exists():
        return 0
    return len(list(out_dir.glob("*.json")))
