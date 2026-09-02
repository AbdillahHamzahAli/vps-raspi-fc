import time
import logging
import cv2
import numpy as np
from av import VideoFrame
from aiortc import VideoStreamTrack
from common.config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS

logger = logging.getLogger(__name__)

class UsbCameraTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, device: int | str = 0, width: int = VIDEO_WIDTH, height: int = VIDEO_HEIGHT, fps: int = VIDEO_FPS):
        super().__init__()
        self.width = width
        self.height = height
        self.fps = fps
        self.device = device
        self.cap: cv2.VideoCapture | None = None
        self._frame_idx = 0
        self.frame_id = 0  # public for telemetry sync
        self._open_camera()

    def _open_camera(self):
        try:
            self.cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.device)
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.cap.set(cv2.CAP_PROP_FPS, self.fps)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                logger.info(f"[camera] opened {self.device} {self.width}x{self.height}@{self.fps}")
            else:
                logger.warning(f"[camera] cannot open {self.device}, black frames")
                self.cap = None
        except Exception as e:
            logger.warning(f"[camera] open error {e}")
            self.cap = None

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame_bgr = None
        if self.cap is not None:
            ret, frame_bgr = self.cap.read()
            if not ret or frame_bgr is None:
                frame_bgr = None
        if frame_bgr is None:
            frame_bgr = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            cv2.putText(frame_bgr, f"NO CAM {self._frame_idx}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.putText(frame_bgr, time.strftime("%H:%M:%S"), (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        if frame_bgr.shape[1] != self.width or frame_bgr.shape[0] != self.height:
            frame_bgr = cv2.resize(frame_bgr, (self.width, self.height))
        vf = VideoFrame.from_ndarray(frame_bgr, format="bgr24")
        vf.pts = pts
        vf.time_base = time_base
        self._frame_idx += 1
        self.frame_id = self._frame_idx
        return vf

    def get_frame_id(self) -> int:
        return self.frame_id

    def stop(self):
        super().stop()
        if self.cap is not None:
            try:
                self.cap.release()
            except:
                pass
