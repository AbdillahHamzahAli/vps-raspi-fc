import json
import time
import asyncio
import logging
import re
from pathlib import Path

import cv2
import numpy as np

from common.config import DETECTION_SAVE_DIR

logger = logging.getLogger(__name__)

SAVE_LOCK = asyncio.Lock()
_last_save_ms = 0

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _ensure_dir_for_ts(ts: float) -> Path:
    day_str = time.strftime("%Y-%m-%d", time.localtime(ts))
    p = Path(DETECTION_SAVE_DIR) / day_str
    p.mkdir(parents=True, exist_ok=True)
    return p

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
    Nama: YYYYMMDD_HHMMSS_{ms}_{frame_id}_{cls}_{conf:.2f}.jpg/.json
    Lokasi: data/detections/YYYY-MM-DD/{name}.jpg+json
    """
    global _last_save_ms
    from common.config import DETECTION_SAVE_THROTTLE_MS

    now_ms = int(time.time() * 1000)
    if now_ms - _last_save_ms < DETECTION_SAVE_THROTTLE_MS:
        return None

    async with SAVE_LOCK:
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
        ms_part = int((ts % 1) * 1000)
        name = f"{ts_str}_{ms_part:03d}_{frame_id}_{cls}_{conf:.2f}"
        name = "".join(c if c.isalnum() or c in "_-." else "_" for c in name)

        out_dir = _ensure_dir_for_ts(ts)
        jpg_path = out_dir / f"{name}.jpg"
        json_path = out_dir / f"{name}.json"

        annotated = _annotate(bgr, detections)
        day_str = time.strftime("%Y-%m-%d", time.localtime(ts))

        def _write():
            cv2.imwrite(str(jpg_path), annotated)
            payload = {
                "id": name,
                "ts": ts,
                "ts_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                "date": day_str,
                "frame_id": frame_id,
                "coords": coords,
                "detections": detections,
                "image": f"/detections/{day_str}/{jpg_path.name}",
                "image_path": str(jpg_path),
            }
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
        logger.info(f"[storage] saved {day_str}/{jpg_path.name} coords={coords} dets={len(detections)}")
        return payload

def _all_json_files() -> list[Path]:
    base = Path(DETECTION_SAVE_DIR)
    if not base.exists():
        return []
    # support both flat lama dan YYYY-MM-DD baru
    return list(base.rglob("*.json"))

def list_days() -> list[dict]:
    base = Path(DETECTION_SAVE_DIR)
    if not base.exists():
        return []
    # cari subdir YYYY-MM-DD
    days: dict[str, dict] = {}
    for p in base.rglob("*.json"):
        try:
            data = json.loads(p.read_text())
            date = data.get("date")
            if not date or not DATE_RE.match(str(date)):
                # fallback dari ts atau mtime
                ts = data.get("ts") or p.stat().st_mtime
                date = time.strftime("%Y-%m-%d", time.localtime(ts))
            # also fallback dari parent dir name if matches
            parent = p.parent.name
            if DATE_RE.match(parent):
                date = parent
            if date not in days:
                days[date] = {"date": date, "count": 0, "size_mb": 0.0, "latest_ts": 0}
            days[date]["count"] += 1
            try:
                sz = p.stat().st_size
                # also jpg size
                jpg = p.with_suffix(".jpg")
                if jpg.exists():
                    sz += jpg.stat().st_size
                days[date]["size_mb"] += sz
                ts_val = data.get("ts") or p.stat().st_mtime
                if ts_val > days[date]["latest_ts"]:
                    days[date]["latest_ts"] = ts_val
            except:
                pass
        except:
            continue
    # also include empty day dirs without json? not needed
    for d in base.iterdir():
        if d.is_dir() and DATE_RE.match(d.name) and d.name not in days:
            days[d.name] = {"date": d.name, "count": 0, "size_mb": 0.0, "latest_ts": 0}
    result = []
    for v in days.values():
        v["size_mb"] = round(v["size_mb"] / 1024 / 1024, 2)
        v["latest_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v["latest_ts"])) if v["latest_ts"] else ""
        result.append(v)
    result.sort(key=lambda x: x["date"], reverse=True)
    return result

def list_detections(limit: int = 50, offset: int = 0, date: str | None = None) -> list:
    if date is not None and not DATE_RE.match(date):
        raise ValueError("date must be YYYY-MM-DD")
    base = Path(DETECTION_SAVE_DIR)
    if not base.exists():
        return []
    if date:
        files = sorted((base / date).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if (base / date).exists() else []
        # also include migrated? no need
    else:
        files = sorted(_all_json_files(), key=lambda p: p.stat().st_mtime, reverse=True)
    sliced = files[offset: offset+limit]
    result = []
    for p in sliced:
        try:
            data = json.loads(p.read_text())
            if "image" not in data:
                # reconstruct per hari
                parent = p.parent.name
                if DATE_RE.match(parent):
                    data["image"] = f"/detections/{parent}/{p.stem}.jpg"
                else:
                    data["image"] = f"/detections/{p.stem}.jpg"
            if "date" not in data:
                parent = p.parent.name
                if DATE_RE.match(parent):
                    data["date"] = parent
                else:
                    data["date"] = time.strftime("%Y-%m-%d", time.localtime(data.get("ts", p.stat().st_mtime)))
            result.append(data)
        except Exception as e:
            logger.warning(f"[storage] read {p} failed {e}")
    return result

def get_detection(id: str) -> dict | None:
    base = Path(DETECTION_SAVE_DIR)
    if id.endswith(".json"):
        id = id[:-5]
    # cari **/{id}.json
    candidates = list(base.rglob(f"{id}.json"))
    # also try sanitized? already
    if not candidates:
        # fallback exact flat
        p = base / f"{id}.json"
        if p.exists():
            candidates = [p]
    if not candidates:
        return None
    p = sorted(candidates, key=lambda x: x.stat().st_mtime, reverse=True)[0]
    try:
        data = json.loads(p.read_text())
        if "image" not in data:
            parent = p.parent.name
            if DATE_RE.match(parent):
                data["image"] = f"/detections/{parent}/{p.stem}.jpg"
            else:
                data["image"] = f"/detections/{p.stem}.jpg"
        if "date" not in data:
            parent = p.parent.name
            if DATE_RE.match(parent):
                data["date"] = parent
            else:
                data["date"] = time.strftime("%Y-%m-%d", time.localtime(data.get("ts", p.stat().st_mtime)))
        return data
    except Exception as e:
        logger.warning(f"[storage] get {id} failed {e}")
        return None

def count_detections(date: str | None = None) -> int:
    base = Path(DETECTION_SAVE_DIR)
    if not base.exists():
        return 0
    if date:
        if not DATE_RE.match(date):
            return 0
        return len(list((base / date).glob("*.json")) if (base / date).exists() else [])
    return len(_all_json_files())

def migrate_flat_to_days() -> dict:
    """Pindahkan file flat lama (data/detections/*.json) ke YYYY-MM-DD/. Idempotent."""
    base = Path(DETECTION_SAVE_DIR)
    if not base.exists():
        return {"moved": 0, "skipped": 0}
    flat_jsons = list(base.glob("*.json"))
    if not flat_jsons:
        return {"moved": 0, "skipped": 0}
    moved = 0
    skipped = 0
    for j in flat_jsons:
        try:
            data = json.loads(j.read_text())
            ts = data.get("ts") or j.stat().st_mtime
            day_str = time.strftime("%Y-%m-%d", time.localtime(ts))
            # also check if parent already day? flat so not
            dest_dir = base / day_str
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_j = dest_dir / j.name
            dest_jpg = dest_dir / (j.stem + ".jpg")
            src_jpg = j.with_suffix(".jpg")
            # skip if dest exists
            if dest_j.exists():
                skipped += 1
                # optionally remove flat if both exist? keep flat to avoid data loss, then user can delete
                continue
            j.rename(dest_j)
            if src_jpg.exists() and not dest_jpg.exists():
                src_jpg.rename(dest_jpg)
            # update json image/date field
            try:
                data = json.loads(dest_j.read_text())
                data["date"] = day_str
                data["image"] = f"/detections/{day_str}/{dest_j.stem}.jpg"
                data["image_path"] = str(dest_j.with_suffix(".jpg"))
                dest_j.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            except:
                pass
            moved += 1
        except Exception as e:
            logger.warning(f"[storage] migrate {j} failed {e}")
            skipped += 1
    logger.info(f"[storage] migrate flat->days moved={moved} skipped={skipped}")
    return {"moved": moved, "skipped": skipped}
