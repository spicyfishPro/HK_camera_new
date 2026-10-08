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
        self._state = threading.Condition()
        self._pause_requested = False
        self._paused = False
        self._lock = threading.Lock()
        self._latest_frame: FrameSnapshot | None = None
        # Qt queued signal 没有自带背压。只允许一张预览帧处于
        # "已 emit、主线程尚未显示"状态，避免 25MP 长时取流时堆积。
        self._display_slot_available = threading.Event()
        self._display_slot_available.set()
        self._last_emit_ts: float = 0.0
        self._emit_interval: float = 1.0 / _DISPLAY_MAX_FPS
        self._saving = False
        self._fresh_requests: dict[str, tuple[float, FrameSnapshot | None]] = {}
        self._fresh_errors: dict[str, str] = {}
        self._latest_strategy = False

    def set_saving(self, saving: bool) -> None:
        with self._state:
            self._saving = saving
            self._state.notify_all()

    def fresh_snapshot(self, request_id: str, timeout_s: float = 1.0) -> FrameSnapshot:
        """由预处理线程调用；只接受请求之后开始读取的帧。"""
        with self._state:
            if self._stop:
                raise RuntimeError("取流已停止")
            self._fresh_requests[request_id] = (time.monotonic(), None)
            self._state.notify_all()
            try:
                arrived = self._state.wait_for(
                    lambda: self._stop or request_id in self._fresh_errors
                    or self._fresh_requests[request_id][1] is not None,
                    timeout=timeout_s,
                )
                snapshot = self._fresh_requests[request_id][1]
                if request_id in self._fresh_errors:
                    raise RuntimeError(self._fresh_errors[request_id])
                if not arrived or snapshot is None:
                    raise RuntimeError("等待新的相机帧超时，请重新抓拍")
                return snapshot
            finally:
                self._fresh_requests.pop(request_id, None)
                self._fresh_errors.pop(request_id, None)

    def stop(self) -> None:
        with self._state:
            self._stop = True
            self._pause_requested = False
            self._state.notify_all()
        self._display_slot_available.set()

    def request_pause(self) -> bool:
        """请求取流线程在自身线程内 StopGrabbing。

        返回 False 表示线程已在停止，调用方不应再等待暂停确认。
        """
        with self._state:
            if self._stop:
                return False
            self._pause_requested = True
            self._state.notify_all()
            return True

    def wait_until_paused(self, timeout_s: float = 1.0) -> bool:
        """等待 StopGrabbing 完成；超时或线程停止时返回 False。"""
        with self._state:
            self._state.wait_for(
                lambda: self._paused or self._stop,
                timeout=max(0.0, float(timeout_s)),
            )
            return self._paused and not self._stop

    def resume(self) -> None:
        """撤销暂停请求，由取流线程自行 StartGrabbing。"""
        with self._state:
            self._pause_requested = False
            self._state.notify_all()

    def display_consumed(self) -> None:
        """主线程已显示/丢弃上一帧，允许下次预览 emit。"""
        self._display_slot_available.set()

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
        """原子取得图像与帧信息的只读强引用。

        取流线程只会用新 FrameSnapshot 替换缓存，不会覆写旧
        ndarray；因此调用方持有返回值时旧帧仍然有效，无需再复制 75MiB。
        """
        with self._lock:
            return self._latest_frame

    def run(self) -> None:
        with self._state:
            if self._stop:
                self._state.notify_all()
                return
            self._paused = False
            self._state.notify_all()
        consecutive_fail = 0
        grabbing = False
        try:
            configure = getattr(self._cam, "set_latest_frame_strategy", None)
            self._latest_strategy = bool(configure and configure())
            self._cam.start_grabbing()
            grabbing = True
        except Exception as e:
            self.error.emit(f"开始取流失败: {e}")
            with self._state:
                self._stop = True
                self._state.notify_all()
            return

        last_read = 0.0
        while True:
            with self._state:
                stop_requested = self._stop
                pause_requested = self._pause_requested
            if stop_requested:
                break

            if pause_requested:
                try:
                    if grabbing:
                        self._cam.stop_grabbing()
                        grabbing = False
                except Exception as e:
                    self.error.emit(f"暂停取流失败: {e}")
                    break

                with self._state:
                    self._paused = True
                    self._state.notify_all()
                    self._state.wait_for(
                        lambda: not self._pause_requested or self._stop
                    )
                    self._paused = False
                    stop_requested = self._stop
                    self._state.notify_all()
                if stop_requested:
                    break
                try:
                    self._cam.start_grabbing()
                    grabbing = True
                except Exception as e:
                    self.error.emit(f"恢复取流失败: {e}")
                    break
                # 恢复后立即取一帧，不受上次显示节流时间影响。
                self._last_emit_ts = 0.0
                continue

            with self._state:
                if self._saving and self._latest_strategy:
                    delay = max(0.0, 1.0 / 15 - (time.monotonic() - last_read))
                    self._state.wait_for(
                        lambda: self._stop or self._pause_requested or not self._saving
                        or any(s is None for _, s in self._fresh_requests.values()),
                        timeout=delay,
                    )
                if self._stop or self._pause_requested:
                    continue
            read_started = time.monotonic()
            last_read = read_started
            try:
                with self._state:
                    to_clear = [key for key, (requested, arrived) in self._fresh_requests.items()
                                if arrived is None and key not in self._fresh_errors]
                if to_clear:
                    clear = getattr(self._cam, "clear_image_buffer", None)
                    # 相机替身可省略；正式 SDK wrapper 始终提供此方法。
                    if clear is not None and not clear():
                        with self._state:
                            for key in to_clear:
                                if key in self._fresh_requests:
                                    self._fresh_errors[key] = "无法清理相机旧帧缓存，请重新连接后抓拍"
                            self._state.notify_all()
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
                # 快照路径会直接持有这个 ndarray，用只读标志固化
                # "只替换引用、不覆写旧帧"的约定。
                bgr.setflags(write=False)
                with self._lock:
                    self._latest_frame = FrameSnapshot(
                        bgr=bgr,
                        metadata=frame_metadata,
                    )
                    snapshot = self._latest_frame
                with self._state:
                    # 只交付本轮读取开始前已登记并清过缓存的请求。
                    # Windows monotonic 时钟可能出现相等时间戳，不能用时间比较
                    # 将读取中途到达的请求误绑定到当前帧。
                    for request_id in to_clear:
                        if request_id not in self._fresh_requests:
                            continue
                        requested_at, arrived = self._fresh_requests[request_id]
                        if arrived is None and request_id not in self._fresh_errors:
                            self._fresh_requests[request_id] = (requested_at, snapshot)
                    self._state.notify_all()
                # 限频 + 降采样 + 校正后再 emit
                now = time.monotonic()
                due = now - self._last_emit_ts >= self._emit_interval
                if due and self._display_slot_available.is_set():
                    # 只有取流线程会 clear，主线程只会 set。
                    self._display_slot_available.clear()
                    try:
                        disp = self._downscale_for_display(bgr)
                        if self._correction is not None and self._correction.enabled:
                            disp = self._correction.apply_display(disp)
                        self.frame_ready.emit(disp)
                        self._last_emit_ts = now
                    except Exception:
                        # emit/预览处理失败不能永久占用单槽。
                        self._display_slot_available.set()
                        raise
            except Exception as e:
                self.error.emit(f"帧处理异常: {e}")
                break
            finally:
                self._cam.free_frame(raw_frame)

        if grabbing:
            try:
                self._cam.stop_grabbing()
            except Exception as e:
                log.warning("StopGrabbing 异常: %s", e)
        with self._state:
            self._paused = False
            self._stop = True
            self._state.notify_all()

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
