"""抓拍热路径中可复用的图像处理缓冲。

这个模块不依赖 Qt/MVS，方便对旋转像素正确性和缓冲复用做纯逻辑测试。
"""
from __future__ import annotations

import threading

import cv2
import numpy as np


class ReusableImageRotator:
    """对抓拍图做旋转，180° 路径复用预分配的全分辨率缓冲。

    CaptureWidget 保证同一时刻只有一次抓拍在处理。``slot`` 用于
    区分主图和可选 ``_raw`` 图，避免后者在编码/评分结束前覆写主图。
    """

    def __init__(self) -> None:
        self._buffers: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def rotate(
        self, bgr: np.ndarray, degrees: int, *, slot: str = "main",
    ) -> np.ndarray:
        if degrees == 0:
            return bgr
        if degrees == 180:
            with self._lock:
                dst = self._buffers.get(slot)
                if (
                    dst is None
                    or dst.shape != bgr.shape
                    or dst.dtype != bgr.dtype
                ):
                    dst = np.empty_like(bgr)
                    self._buffers[slot] = dst
                cv2.flip(bgr, -1, dst=dst)
                return dst
        if degrees == 90:
            return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
        if degrees == 270:
            return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
        raise ValueError(f"不支持的旋转角度: {degrees}")

    def clear(self) -> None:
        """相机断开/窗口关闭时释放最多两张全分辨率缓冲。"""
        with self._lock:
            self._buffers.clear()
