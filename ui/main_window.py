"""主窗口 — 麦穗采集 / 籽粒采集 两个 tab。"""
from __future__ import annotations

import logging
import time

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMainWindow, QTabWidget, QLabel, QStatusBar

from .capture_widget import CaptureWidget
from core.capture_pipeline import CapturePipeline
from core.camera import CameraError

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    capture_event = pyqtSignal(object)
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RGB Capture")
        # 自适应屏幕:取可用区域的 90%(上限 1600×900),不超过屏幕,居中。
        # 控制栏已包进滚动区,矮屏也能完整访问所有控件。
        self.setMinimumSize(1000, 600)
        self._fit_to_screen()

        self.tabs = QTabWidget()
        self.pipeline = CapturePipeline(self.capture_event.emit)
        self.wheat_tab = CaptureWidget(mode="wheat", pipeline=self.pipeline)
        self.grain_tab = CaptureWidget(mode="grain", pipeline=self.pipeline)
        self.capture_event.connect(self.wheat_tab._on_pipeline_event)
        self.capture_event.connect(self.grain_tab._on_pipeline_event)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(200)
        self._close_timer.timeout.connect(self._continue_close)
        self.wheat_tab.bearer_key_changed.connect(
            self.grain_tab.set_bearer_key
        )
        self.grain_tab.bearer_key_changed.connect(
            self.wheat_tab.set_bearer_key
        )
        self.tabs.addTab(self.wheat_tab, "麦穗采集")
        self.tabs.addTab(self.grain_tab, "籽粒采集")
        self.setCentralWidget(self.tabs)

        bar = QStatusBar()
        self.setStatusBar(bar)
        self._right_status = QLabel("")
        self._right_status.setStyleSheet("color: #f29429; font-weight: 500;")
        bar.addPermanentWidget(self._right_status)
        bar.showMessage("准备就绪")

        self._startup_started = False
        self._auto_connect_active = False
        self._auto_connect_deadline = 0.0
        self._auto_stream_target = None
        self._auto_connect_timer = QTimer(self)
        self._auto_deadline_timer = QTimer(self)
        self._auto_stream_timer = QTimer(self)
        for timer in (self._auto_connect_timer, self._auto_deadline_timer, self._auto_stream_timer):
            timer.setSingleShot(True)
            timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._auto_connect_timer.timeout.connect(self._try_auto_connect)
        self._auto_deadline_timer.timeout.connect(self._expire_auto_connect)
        self._auto_stream_timer.timeout.connect(self._start_auto_stream)
        for tab in (self.wheat_tab, self.grain_tab):
            tab.device_control_requested.connect(self._cancel_auto_connect)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._startup_started:
            self._startup_started = True
            self._auto_connect_active = True
            self._auto_connect_deadline = time.monotonic() + 60.0
            self._auto_connect_timer.start(1000)
            self._auto_deadline_timer.start(60000)
            self.set_right_status("启动后 1 秒自动查找并连接相机")

    def _cancel_auto_connect(self) -> None:
        self._startup_started = True
        self._auto_connect_active = False
        self._auto_stream_target = None
        self._auto_connect_timer.stop()
        self._auto_deadline_timer.stop()
        self._auto_stream_timer.stop()

    def _expire_auto_connect(self) -> None:
        if not self._auto_connect_active:
            return
        self._cancel_auto_connect()
        log.info("启动自动连接已在 60 秒时停止")
        self.set_right_status("自动连接已满 1 分钟，请手动刷新并连接相机")

    def _retry_auto_connect(self, message: str) -> None:
        if not self._auto_connect_active:
            return
        if time.monotonic() >= self._auto_connect_deadline:
            self._expire_auto_connect()
            return
        self.set_right_status(f"{message}；每 5 秒重试，启动满 1 分钟停止")
        self._auto_connect_timer.start(5000)

    def _try_auto_connect(self) -> None:
        self._auto_connect_timer.stop()
        if not self._auto_connect_active:
            return
        if time.monotonic() >= self._auto_connect_deadline:
            self._expire_auto_connect()
            return
        tabs = (self.wheat_tab, self.grain_tab)
        if any(t.camera.is_connected() or t._closing or t._shutting_down for t in tabs):
            self._cancel_auto_connect()
            return
        tab = self.tabs.currentWidget()
        log.info("[%s] 启动自动连接：刷新设备", tab.mode)
        devices = tab.refresh_devices(automatic=True)
        if not self._auto_connect_active:
            return
        # SDK 调用可能较慢，返回后也不能越过重试截止时间继续发起连接。
        if time.monotonic() >= self._auto_connect_deadline:
            self._expire_auto_connect()
            return
        if devices is None or not devices:
            self._retry_auto_connect("刷新失败" if devices is None else "未检测到相机")
            return
        if len(devices) > 1:
            self._multiple_auto_devices(tab)
            return
        try:
            connected = tab.connect_device(automatic=True)
        except CameraError as exc:
            log.warning("启动自动连接失败: %s", exc)
            if (getattr(exc, "device_count", None) or 0) > 1:
                # SDK 最后一次枚举才发现多设备时，同样停止自动连接。
                tab.refresh_devices(automatic=True)
                self._multiple_auto_devices(tab)
            else:
                self._retry_auto_connect(f"自动连接失败: {exc}")
            return
        if not connected:
            self._retry_auto_connect("自动连接未完成")
            return
        self._cancel_auto_connect()
        self._auto_stream_target = tab
        self._auto_stream_timer.start(100)
        log.info("[%s] 启动自动连接成功，100 ms 后进入连续采集", tab.mode)
        self.set_right_status("相机已自动连接，即将进入连续采集")

    def _multiple_auto_devices(self, tab) -> None:
        self._cancel_auto_connect()
        tab.lst_devices.setCurrentRow(-1)
        log.info("检测到多台相机，停止启动自动连接")
        self.set_right_status("检测到多台相机，请手动选择设备并连接")

    def _start_auto_stream(self) -> None:
        tab = self._auto_stream_target
        self._auto_stream_target = None
        self._auto_stream_timer.stop()
        if (tab is None or tab is not self.tabs.currentWidget()
                or tab._closing or tab._shutting_down or not tab.camera.is_connected()):
            return
        tab.start_streaming()

    def _fit_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1280, 800)
            return
        avail = screen.availableGeometry()
        w = min(1600, int(avail.width() * 0.9))
        h = min(900, int(avail.height() * 0.9))
        # 不小于最小尺寸,也不超过屏幕
        w = max(min(w, avail.width()), 1000)
        h = max(min(h, avail.height()), 600)
        self.resize(w, h)
        self.move(
            avail.x() + (avail.width() - w) // 2,
            avail.y() + (avail.height() - h) // 2,
        )

    def set_right_status(self, msg: str) -> None:
        self._right_status.setText(msg)

    def closeEvent(self, event):  # noqa: N802
        self._cancel_auto_connect()
        if self.pipeline.pending_count:
            event.ignore()
            self.pipeline.begin_drain()
            for tab in (self.wheat_tab, self.grain_tab):
                tab._closing = True
            self._close_timer.start()
            self._continue_close()
            return
        self._close_timer.stop()
        try:
            self.wheat_tab.shutdown()
            self.grain_tab.shutdown()
            self.pipeline.shutdown()
        except Exception:
            log.exception("close shutdown error")
        super().closeEvent(event)

    def _continue_close(self):
        if not self.pipeline.pending_count:
            self.close()
        else:
            self.set_right_status(
                f"关闭前等待保存 {self.pipeline.pending_count} 张" +
                ("；有保存失败，请在照片带重试" if self.pipeline.has_failures else "…")
            )
