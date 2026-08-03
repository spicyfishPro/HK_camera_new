"""取流后台线程。

性能要点:
- 主线程接收已经转好的 BGR ndarray,但**降采样后**(显示用),
  避免在主线程做大图的 cvtColor/scale。
- 显示频率限制在 ~30 FPS,即使相机更快也只 emit 这么多帧。
- 全分辨率图像与同一帧元数据缓存在 `latest_frame` 供抓拍读取。
"""
from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from core.acquisition_metadata import FrameSnapshot
from core.camera import HikCamera
from core.correction import FlatFieldCorrection

log = logging.getLogger(__name__)


_DISPLAY_MAX_FPS = 30          # 显示节流上限
# 显示降采样:最长边像素。
# 2048 在 25MP 相机下经验上 CPU 占用 ~30%,画质对人眼基本无损,
# 允许 ImageView 缩放到 1:1 时还看得到细节。提到 2560+ 一颗核能跑满,慎调。
_DISPLAY_MAX_SIDE = 2048


class StreamWorker(QThread):
    """循环取流,把降采样后的 BGR 帧通过 signal 发给主线程显示。
    `latest_frame` 是全分辨率**未校正** BGR 与逐帧信息的配对快照
    (抓拍读后自行 apply_full)。
    显示路径在 emit 前 apply_display。"""

    frame_ready = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)

    def __init__(self, camera: HikCamera,
                 correction: FlatFieldCorrection | None = None,
                 parent=None):
        super().__init__(parent)
        self._cam = camera
        self._correction = correction
        self._stop = False
        self._lock = threading.Lock()
        self._latest_frame: FrameSnapshot | None = None
        self._last_emit_ts: float = 0.0
        self._emit_interval: float = 1.0 / _DISPLAY_MAX_FPS

    def stop(self) -> None:
        self._stop = True

    @property
    def latest_bgr(self) -> np.ndarray | None:
        """兼容显示/校准代码；只返回图像副本。"""
        with self._lock:
            return (
                None if self._latest_frame is None
                else self._latest_frame.bgr.copy()
            )

    @property
    def latest_frame(self) -> FrameSnapshot | None:
        """原子取得图像与其帧信息，防止二者来自不同帧。"""
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def run(self) -> None:
        self._stop = False
        consecutive_fail = 0
        try:
            self._cam.start_grabbing()
        except Exception as e:
            self.error.emit(f"开始取流失败: {e}")
            return

        while not self._stop:
            try:
                result = self._cam.grab_frame_bgr(timeout_ms=500)
            except Exception as e:
                self.error.emit(f"取流异常: {e}")
                break

            if result is None:
                consecutive_fail += 1
                if consecutive_fail >= 20:
                    self.error.emit("连续取流超时,可能相机已断开")
                    break
                continue

            consecutive_fail = 0
            bgr, raw_frame = result
            try:
                frame_metadata = self._cam.copy_frame_metadata(raw_frame)
                with self._lock:
                    self._latest_frame = FrameSnapshot(
                        bgr=bgr,
                        metadata=frame_metadata,
                    )
                # 限频 + 降采样 + 校正后再 emit
                now = time.monotonic()
                if now - self._last_emit_ts >= self._emit_interval:
                    disp = self._downscale_for_display(bgr)
                    if self._correction is not None and self._correction.enabled:
                        disp = self._correction.apply_display(disp)
                    self.frame_ready.emit(disp)
                    self._last_emit_ts = now
            finally:
                self._cam.free_frame(raw_frame)

        try:
            self._cam.stop_grabbing()
        except Exception as e:
            log.warning("StopGrabbing 异常: %s", e)

    @staticmethod
    def _downscale_for_display(bgr: np.ndarray) -> np.ndarray:
        h, w = bgr.shape[:2]
        long_side = max(h, w)
        if long_side <= _DISPLAY_MAX_SIDE:
            return bgr
        scale = _DISPLAY_MAX_SIDE / long_side
        new_w = int(w * scale)
        new_h = int(h * scale)
        return cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
