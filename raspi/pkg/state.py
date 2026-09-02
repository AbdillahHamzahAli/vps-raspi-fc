from __future__ import annotations
import threading
import time
from typing import Optional

class MAVState:
    def __init__(self, master) -> None:
        self._master = master
        self._lock = threading.Lock()
        self._latest: dict[str, tuple[object, float]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mavstate")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._master.recv_match(blocking=True, timeout=1)
                if msg is None:
                    continue
                with self._lock:
                    self._latest[msg.get_type()] = (msg, time.time())
            except Exception:
                time.sleep(0.2)

    def get(self, msg_type: str, max_age: float = 1.0):
        with self._lock:
            entry = self._latest.get(msg_type)
        if entry is None:
            return None
        msg, ts = entry
        if time.time() - ts > max_age:
            return None
        return msg

    def wait(self, msg_type: str, timeout_s: float = 5, max_age: float = 2.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = self.get(msg_type, max_age=max_age)
            if msg is not None:
                return msg
            time.sleep(0.1)
        return None

    def latest_age(self, msg_type: str) -> float | None:
        with self._lock:
            entry = self._latest.get(msg_type)
        if entry is None:
            return None
        _, ts = entry
        return time.time() - ts
