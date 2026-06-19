"""Shared single-camera frame producer.

Several perception modules want frames from the same overhead camera.
`cv2.VideoCapture` does not allow concurrent readers, so we run *one*
background thread that owns the capture and cache the latest frame; every
consumer just calls `stream.read()` to grab it.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import cv2
import numpy as np


class CameraStream:
    """Thread-safe latest-frame cache for a single camera.

    Usage:
        cam = CameraStream(0).start()
        frame = cam.read()        # latest BGR frame, or None
        cam.stop()
    """

    def __init__(self, index: int = 0,
                 width: Optional[int] = None,
                 height: Optional[int] = None):
        self.index = index
        self.width = width
        self.height = height
        self._cap: Optional[cv2.VideoCapture] = None
        self._frame: Optional[np.ndarray] = None
        self._frame_t: float = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "CameraStream":
        if self._thread is not None:
            return self
        self._cap = cv2.VideoCapture(self.index)
        if not self._cap.isOpened():
            self._cap = None
            raise RuntimeError(f"camera {self.index} did not open")
        if self.width:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # Wait briefly for the first frame so callers don't get None.
        deadline = time.time() + 2.0
        while time.time() < deadline and self.read() is None:
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "CameraStream":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def read_with_t(self) -> tuple[Optional[np.ndarray], float]:
        with self._lock:
            f = None if self._frame is None else self._frame.copy()
            return f, self._frame_t

    def _loop(self) -> None:
        assert self._cap is not None
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            with self._lock:
                self._frame = frame
                self._frame_t = time.time()
