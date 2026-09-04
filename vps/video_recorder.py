import time
import json
import logging
import asyncio
from pathlib import Path
from typing import Optional

import cv2  # only for resize if needed
import numpy as np

logger = logging.getLogger(__name__)

try:
    import av  # PyAV
    HAS_AV = True
except ImportError:
    HAS_AV = False
    av = None  # type: ignore

from common.config import VIDEO_SAVE_DIR, VIDEO_SAVE_FPS, VIDEO_MAX_S, VIDEO_MAX_GB

# marker file for cross-process state (signaling <-> main_vps separate processes)
def _marker_path() -> Path:
    return Path(VIDEO_SAVE_DIR) / ".recording.json"

def _ensure_dir():
    p = Path(VIDEO_SAVE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p

def get_state() -> dict:
    """Read marker, return {recording:bool, id, path, start_ts, elapsed_s, max_s, fps}"""
    mp = _marker_path()
    if not mp.exists():
        return {"recording": False}
    try:
        data = json.loads(mp.read_text())
        start_ts = float(data.get("start_ts", 0))
        elapsed = time.time() - start_ts if start_ts else 0
        data["elapsed_s"] = elapsed
        data["recording"] = True
        # auto-expire check
        max_s = int(data.get("max_s", VIDEO_MAX_S))
        if elapsed > max_s:
            data["recording"] = False
            data["expired"] = True
        return data
    except Exception as e:
        logger.warning(f"[video] state read failed {e}")
        return {"recording": False, "error": str(e)}

def is_recording() -> bool:
    s = get_state()
    return bool(s.get("recording") and not s.get("expired"))

def set_state(path: str, start_ts: float) -> dict:
    _ensure_dir()
    data = {
        "recording": True,
        "id": Path(path).stem,
        "path": path,
        "start_ts": start_ts,
        "max_s": VIDEO_MAX_S,
        "fps": VIDEO_SAVE_FPS,
        "container": "mkv",
        "codec": "h264",
    }
    _marker_path().write_text(json.dumps(data, indent=2))
    return data

def clear_state() -> Optional[dict]:
    mp = _marker_path()
    if not mp.exists():
        return None
    try:
        data = json.loads(mp.read_text())
        mp.unlink()
        return data
    except:
        try:
            mp.unlink()
        except:
            pass
        return None

def list_videos(limit: int = 50, offset: int = 0) -> list:
    d = Path(VIDEO_SAVE_DIR)
    if not d.exists():
        return []
    files = sorted([p for p in d.glob("*.mkv")], key=lambda p: p.stat().st_mtime, reverse=True)
    sliced = files[offset: offset+limit]
    out = []
    for p in sliced:
        try:
            st = p.stat()
            out.append({
                "id": p.stem,
                "file": p.name,
                "path": f"/videos/{p.name}",
                "size_mb": round(st.st_size / 1024 / 1024, 2),
                "mtime": st.st_mtime,
                "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            })
        except:
            pass
    return out

def count_videos() -> int:
    d = Path(VIDEO_SAVE_DIR)
    if not d.exists():
        return 0
    return len(list(d.glob("*.mkv")))

def total_size_mb() -> float:
    d = Path(VIDEO_SAVE_DIR)
    if not d.exists():
        return 0
    total = 0
    for p in d.glob("*.mkv"):
        try:
            total += p.stat().st_size
        except:
            pass
    return round(total / 1024 / 1024, 2)

def enforce_retention():
    """Hapus tertua jika > VIDEO_MAX_GB"""
    max_mb = VIDEO_MAX_GB * 1024
    if max_mb <= 0:
        return
    d = Path(VIDEO_SAVE_DIR)
    files = sorted(d.glob("*.mkv"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files) / 1024 / 1024
    removed = []
    for p in files:
        if total <= max_mb:
            break
        try:
            sz = p.stat().st_size / 1024 / 1024
            p.unlink()
            total -= sz
            removed.append(p.name)
        except:
            pass
    if removed:
        logger.info(f"[video] retention removed {removed}")

class VideoRecorder:
    """Actual AV writer, used only in main_vps process. Polls marker file."""
    def __init__(self):
        self.container = None
        self.stream = None
        self.path: Optional[Path] = None
        self.start_ts: Optional[float] = None
        self.frame_idx = 0
        self._lock = asyncio.Lock()
        self._pts = 0

    async def poll_and_update(self):
        """Call every ~0.5s from main_vps: open if marker appeared, close if removed/expired."""
        state = get_state()
        rec = state.get("recording")
        # auto-expire: marker says expired but file still exists
        if state.get("expired") and self.container is not None:
            logger.info("[video] auto-stop 1 jam reached")
            await self._close_internal()
            clear_state()
            return
        if rec and self.container is None:
            # start
            path = state.get("path")
            if path:
                await self._open_internal(Path(path))
        elif not rec and self.container is not None:
            await self._close_internal()

    async def _open_internal(self, path: Path):
        if not HAS_AV:
            logger.error("[video] PyAV not installed, cannot record mkv")
            return
        async with self._lock:
            if self.container is not None:
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # try libx264, fallback h264
                self.container = av.open(str(path), mode="w", format="matroska")
                fps = VIDEO_SAVE_FPS
                import common.config as cfg
                w = int(cfg.VIDEO_WIDTH)
                h = int(cfg.VIDEO_HEIGHT)
                # stream
                try:
                    self.stream = self.container.add_stream("libx264", rate=fps)
                except:
                    self.stream = self.container.add_stream("h264", rate=fps)
                self.stream.width = w
                self.stream.height = h
                self.stream.pix_fmt = "yuv420p"
                # x264 options hemat: crf 23, preset ultrafast for real-time
                try:
                    self.stream.options = {"crf": "23", "preset": "ultrafast"}
                except:
                    pass
                self.path = path
                self.start_ts = time.time()
                self.frame_idx = 0
                self._pts = 0
                logger.info(f"[video] start {path} {w}x{h}@{fps} h264 mkv")
            except Exception as e:
                logger.exception(f"[video] open failed {e}")
                self.container = None
                self.stream = None

    async def write(self, bgr: np.ndarray):
        if self.container is None or self.stream is None:
            return
        async with self._lock:
            try:
                # ensure size
                if bgr.shape[1] != self.stream.width or bgr.shape[0] != self.stream.height:
                    bgr = cv2.resize(bgr, (self.stream.width, self.stream.height))
                frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
                frame.pts = self._pts
                self._pts += 1
                for packet in self.stream.encode(frame):
                    self.container.mux(packet)
                self.frame_idx += 1
            except Exception as e:
                logger.warning(f"[video] write failed {e}")

    async def _close_internal(self):
        async with self._lock:
            if self.container is None:
                return
            try:
                # flush
                for packet in self.stream.encode():
                    self.container.mux(packet)
                self.container.close()
                logger.info(f"[video] stop {self.path} frames={self.frame_idx}")
                enforce_retention()
            except Exception as e:
                logger.warning(f"[video] close failed {e}")
                try:
                    self.container.close()
                except:
                    pass
            finally:
                self.container = None
                self.stream = None
                self.path = None
                self.start_ts = None

    async def close(self):
        await self._close_internal()
