"""主窗口 — 麦穗采集 / 籽粒采集 两个 tab。"""
from __future__ import annotations

import logging

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QMainWindow, QTabWidget, QLabel, QStatusBar

from .capture_widget import CaptureWidget
from core.capture_pipeline import CapturePipeline

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
