"""图像采集 widget。麦穗(wheat)/籽粒(grain)共用,只在 side 控件上区分。"""
from __future__ import annotations

import csv
import logging
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from typing import Literal, Optional

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QImage, QPixmap, QShortcut, QKeySequence, QPainter, QColor, QPen,
    QPolygonF, QBrush,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QGroupBox, QPushButton,
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QLabel, QCheckBox,
    QListWidget, QListWidgetItem, QTabWidget, QFileDialog, QMessageBox,
    QSizePolicy, QApplication, QAbstractSpinBox, QButtonGroup,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsPolygonItem,
    QFrame, QScrollArea, QSplitter,
)

from core import naming, wb_config
from core.acquisition_metadata import (
    CaptureMetadata,
    FrameSnapshot,
)
from core.camera import HikCamera, CameraError, DeviceInfo
from core.capture_pipeline import CapturePipeline, CaptureJob, CaptureEvent
from core.correction import FlatFieldCorrection
from core.field_tag_lookup import (
    FieldTagDetection,
    FieldTagError,
    build_field_tag_api_url,
    detect_field_tag_qr,
    parse_accession_response,
)
from core.paths import CALIB_DIR as _CALIB_DIR, LOGS_DIR as _LOG_DIR, CAPTURE_LOG as _CAPTURE_LOG
from core.quality import BRISQUEScorer, DEFAULT_THRESHOLD
from .spike_panel import SpikePanel, WHEAT_SIDES, GRAIN_SIDES
from .stream_worker import StreamWorker
from .photo_gallery import PhotoGallery

_BRISQUE_HISTORY_LEN = 10

log = logging.getLogger(__name__)

Mode = Literal["wheat", "grain"]


class _MiniMap(QWidget):
    """左上角全景缩略图(放大后显示)。画缩略图 + 当前可视区黄框。
    只读不交互,鼠标事件穿透给底下的画面。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thumb: Optional[QPixmap] = None
        self._view_rect: Optional[QRectF] = None  # thumb 坐标系
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def update_content(self, thumb: QPixmap, view_rect: QRectF) -> None:
        self._thumb = thumb
        self._view_rect = view_rect
        if self.size() != thumb.size():
            self.setFixedSize(thumb.size())
        self.update()

    def paintEvent(self, event):  # noqa: N802
        if self._thumb is None:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._thumb)
        # 外边框
        p.setPen(QPen(QColor("#cccccc"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        # 可视区黄框
        if self._view_rect is not None:
            p.setPen(QPen(QColor("#FFEB3B"), 2))
            p.setBrush(QColor(255, 235, 59, 50))
            p.drawRect(self._view_rect)


class ImageView(QGraphicsView):
    """图像显示区,基于 QGraphicsView。

    交互(类似海康 MVS):
      - 滚轮:以鼠标为锚点缩放(0.05× ~ 50×)
      - 左键拖动:平移
      - 双击:在「适应窗口」与「100% 实际像素」之间切换
      - 右上角:显示当前缩放百分比
      - 左上角:放大后显示全景缩略图 + 可视区框

    保存路径不受显示缩放影响 — snap 走 stream.latest_frame 全分辨率快照。
    """

    _MIN_ZOOM = 0.02
    _MAX_ZOOM = 50.0
    _ZOOM_STEP = 1.2
    display_cleared = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Scene + pixmap item
        self._scene = QGraphicsScene(self)
        self._pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._pixmap_item)
        self._qr_outline_item = QGraphicsPolygonItem()
        qr_pen = QPen(QColor("#32CD32"), 4)
        qr_pen.setCosmetic(True)
        self._qr_outline_item.setPen(qr_pen)
        self._qr_outline_item.setBrush(QBrush(QColor(50, 205, 50, 35)))
        self._qr_outline_item.setZValue(10)
        self._qr_outline_item.hide()
        self._scene.addItem(self._qr_outline_item)
        self._qr_outline_timer = QTimer(self)
        self._qr_outline_timer.setSingleShot(True)
        self._qr_outline_timer.timeout.connect(self.clear_qr_outline)
        self.setScene(self._scene)

        # 渲染 + 锚点 + 拖动
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setBackgroundBrush(QColor("#000"))
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.setMinimumSize(640, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._has_first_frame: bool = False
        self._last_pix_size = None  # 记录尺寸,变化时重置 scene rect

        # 缩放百分比指示(右上角)
        self._zoom_label = QLabel(self.viewport())
        self._zoom_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 150);"
            "color: #ddd; padding: 4px 10px; border-radius: 4px;"
            "font-size: 11px;"
        )
        self._zoom_label.setText("—")
        self._zoom_label.hide()

        # 左上角全景缩略图(放大后才显示)
        self._minimap = _MiniMap(self.viewport())
        self._minimap.hide()
        self._MINIMAP_MAX = 180   # 缩略图长边像素

        # Overlay 警告 label(顶部居中)
        self._overlay = QLabel(self.viewport())
        self._overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay.setStyleSheet(
            "background-color: rgba(180, 0, 0, 235);"
            "color: #FFEB3B;"
            "font-size: 24px;"
            "font-weight: 700;"
            "padding: 14px 28px;"
            "border-radius: 10px;"
            "border: 3px solid #FFEB3B;"
        )
        self._overlay.hide()
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setSingleShot(True)
        self._overlay_timer.timeout.connect(self._overlay.hide)

    # ---------------- 公共 API(与旧 ImageView 兼容) ----------------

    def set_bgr(self, bgr: np.ndarray) -> None:
        if bgr is None or bgr.size == 0:
            return
        h, w = bgr.shape[:2]
        if bgr.ndim == 2:
            qimg = QImage(bgr.data, w, h, w, QImage.Format.Format_Grayscale8)
        else:
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            rgb_contig = np.ascontiguousarray(rgb)
            qimg = QImage(rgb_contig.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        # copy() 因为 numpy buffer 出函数就失效
        pixmap = QPixmap.fromImage(qimg.copy())
        self._pixmap_item.setPixmap(pixmap)

        # 尺寸变了或第一帧 → 更新 scene rect,首帧自动适配窗口
        if self._last_pix_size != pixmap.size():
            self._scene.setSceneRect(self._pixmap_item.boundingRect())
            self._last_pix_size = pixmap.size()
            if not self._has_first_frame:
                self._has_first_frame = True
                self.fit_to_window()
        # 放大状态下刷新缩略图内容(跟着实时画面更新)
        if self._minimap.isVisible():
            self._update_minimap()

    def clear_display(self) -> None:
        self.clear_qr_outline()
        self._pixmap_item.setPixmap(QPixmap())
        self._has_first_frame = False
        self._last_pix_size = None
        self._zoom_label.hide()
        self._minimap.hide()
        self.display_cleared.emit()

    def show_qr_outline(
        self, points: np.ndarray, duration_ms: int = 5000
    ) -> None:
        """按图像坐标显示二维码四边形，并在指定时间后清除。"""
        array = np.asarray(points, dtype=float).reshape(-1, 2)
        if array.shape != (4, 2) or not np.isfinite(array).all():
            return
        polygon = QPolygonF([QPointF(float(x), float(y)) for x, y in array])
        self._qr_outline_item.setPolygon(polygon)
        self._qr_outline_item.show()
        self._qr_outline_timer.start(duration_ms)

    def clear_qr_outline(self) -> None:
        self._qr_outline_timer.stop()
        self._qr_outline_item.hide()
        self._qr_outline_item.setPolygon(QPolygonF())

    def show_warning(self, text: str, duration_ms: int = 3000) -> None:
        """画面顶部居中叠加红底黄字警告,持续 duration_ms 毫秒。"""
        self._overlay.setText(text)
        self._overlay.adjustSize()
        self._overlay.raise_()
        self._overlay.show()
        self._position_overlay()
        QTimer.singleShot(0, self._position_overlay)
        self._overlay_timer.start(duration_ms)

    # ---------------- 缩放控制 ----------------

    def fit_to_window(self) -> None:
        if self._pixmap_item.pixmap().isNull():
            return
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._update_zoom_label()
        self._update_minimap()

    def zoom_actual(self) -> None:
        """1:1 实际像素。"""
        self.resetTransform()
        self._update_zoom_label()
        self._update_minimap()

    def wheelEvent(self, event):  # noqa: N802
        if self._pixmap_item.pixmap().isNull():
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = self._ZOOM_STEP if delta > 0 else (1.0 / self._ZOOM_STEP)
        cur = self.transform().m11()
        new = cur * factor
        if new < self._MIN_ZOOM or new > self._MAX_ZOOM:
            return
        self.scale(factor, factor)
        self._update_zoom_label()
        self._update_minimap()
        event.accept()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if self._pixmap_item.pixmap().isNull():
            return super().mouseDoubleClickEvent(event)
        cur = self.transform().m11()
        # 接近 100% 则切到 fit,否则切到 100%
        if abs(cur - 1.0) < 0.01:
            self.fit_to_window()
        else:
            self.zoom_actual()

    # ---------------- 内部:位置/标签 ----------------

    def _update_zoom_label(self) -> None:
        pct = int(round(self.transform().m11() * 100))
        self._zoom_label.setText(f"缩放 {pct}%  (双击切换 fit/100%)")
        self._zoom_label.adjustSize()
        self._zoom_label.show()
        self._zoom_label.raise_()
        self._position_zoom_label()

    def _position_zoom_label(self) -> None:
        vw = self.viewport().width()
        self._zoom_label.move(
            max(0, vw - self._zoom_label.width() - 10),
            10,
        )

    def _position_overlay(self) -> None:
        if self._overlay.isHidden():
            return
        self._overlay.adjustSize()
        x = max(0, (self.viewport().width() - self._overlay.width()) // 2)
        self._overlay.move(x, 20)

    def _update_minimap(self) -> None:
        """放大(整图装不下)时,左上角显示全景缩略图 + 可视区黄框;否则隐藏。"""
        pm = self._pixmap_item.pixmap()
        if pm.isNull():
            self._minimap.hide()
            return
        scene_rect = self._scene.sceneRect()
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        # 整图都看得见(没放大)→ 不需要缩略图
        if (visible.width() >= scene_rect.width() - 1
                and visible.height() >= scene_rect.height() - 1):
            self._minimap.hide()
            return
        # 缩略图
        long_side = max(pm.width(), pm.height())
        scale = self._MINIMAP_MAX / long_side if long_side > 0 else 1.0
        tw = max(1, int(pm.width() * scale))
        th = max(1, int(pm.height() * scale))
        thumb = pm.scaled(
            tw, th, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 可视区(裁到图内)→ 缩略图坐标
        vis = visible.intersected(scene_rect)
        vx = (vis.x() - scene_rect.x()) * scale
        vy = (vis.y() - scene_rect.y()) * scale
        vw = vis.width() * scale
        vh = vis.height() * scale
        self._minimap.update_content(thumb, QRectF(vx, vy, vw, vh))
        self._minimap.move(10, 10)
        self._minimap.show()
        self._minimap.raise_()

    def scrollContentsBy(self, dx, dy):  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._update_minimap()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._position_overlay()
        self._position_zoom_label()
        self._update_minimap()


class CaptureWidget(QWidget):
    """采集 tab。mode='wheat' 显示 side 控件,'grain' 隐藏。"""

    # 抓拍后台线程 → 主线程的通讯信号(Qt 自动 QueuedConnection)
    # 挂牌二维码后台线程 → 主线程
    _qr_detection_done = pyqtSignal(int, object)       # request_id, FieldTagDetection
    _qr_detection_failed = pyqtSignal(int, str)        # request_id, safe_message
    # 两个页签之间仅在内存中同步，不接入任何持久化配置。
    bearer_key_changed = pyqtSignal(str)

    _pipeline_event = pyqtSignal(object)

    def __init__(self, mode: Mode, parent=None, pipeline=None):
        super().__init__(parent)
        self.mode: Mode = mode
        self.camera = HikCamera()
        self._stream: Optional[StreamWorker] = None
        self._save_dir: Optional[Path] = None
        self._devices: list[DeviceInfo] = []
        self._rotation_deg: int = 180
        self._owner = uuid4().hex
        self._owns_pipeline = pipeline is None
        self.pipeline = pipeline or CapturePipeline(self._pipeline_event.emit)
        self._pipeline_event.connect(self._on_pipeline_event)
        self._capture_attempts = {}
        self._preparing_id = None
        self._closing = False
        self._capacity_timer = QTimer(self)
        self._capacity_timer.setInterval(100)
        self._capacity_timer.timeout.connect(self._refresh_capture_capacity)
        self._capacity_timer.start()
        self._latest_preview_bgr: Optional[np.ndarray] = None
        self._prefix_source: Literal["manual", "qr"] = "manual"
        self._active_field_tag_code: Optional[str] = None
        self._qr_request_code: Optional[str] = None
        self._qr_request_id: int = 0
        self._qr_request_pending: bool = False
        self._qr_reply: Optional[QNetworkReply] = None
        self._shutting_down: bool = False
        self._network_manager = QNetworkAccessManager(self)
        self._qr_timeout_request_id: Optional[int] = None
        self._qr_timeout_timer = QTimer(self)
        self._qr_timeout_timer.setSingleShot(True)
        self._qr_timeout_timer.timeout.connect(self._on_field_tag_request_timeout)
        # 防止参数回填触发 setter
        self._loading_params: bool = False
        # 元数据热路径不再每张通过 SDK 回读。连接/刷新/成功设置时
        # 更新这些值，抓拍时只快照内存字典。
        self._metadata_float_cache: dict[str, float | None] = {
            "ExposureTime": None,
            "Gain": None,
        }
        self._metadata_enum_cache: dict[str, int | None] = {
            "ExposureAuto": None,
            "GainAuto": None,
            "TriggerMode": None,
        }
        # 抓拍并发护栏 —— 防 spacebar / 按钮叠加触发
        self._snap_in_flight: bool = False
        # 平场校正(FFC) — 每个 tab 一份(麦穗/籽粒可能光照不同)
        self.correction = FlatFieldCorrection()
        # BRISQUE 评分器(初始化便宜,但 compute 不线程安全,只在工作线程独占使用)
        try:
            self.brisque = BRISQUEScorer()
            self._brisque_ok = True
        except Exception:
            log.exception("BRISQUE 初始化失败,质量评分功能将禁用")
            self.brisque = None
            self._brisque_ok = False
        self._brisque_threshold: float = DEFAULT_THRESHOLD
        self._brisque_history: deque[float] = deque(maxlen=_BRISQUE_HISTORY_LEN)

        # 麦穗工作流面板 — 只在 wheat 模式存在;grain 永远为 None
        self.spike_panel: Optional[SpikePanel] = None

        self._build_ui()
        self._bind_signals()
        self._qr_detection_done.connect(self._on_qr_detection_done)
        self._qr_detection_failed.connect(self._on_qr_detection_failed)
        self.view.display_cleared.connect(self._on_preview_cleared)
        # 启动时尝试加载持久化的白场参考
        self._try_load_persisted_ffc()
        self._update_button_states()
        self._refresh_ffc_status()
        self._refresh_wb_preset_label()
        self._update_prefix_source_controls()

    # ============================== UI ==============================

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._capture_splitter = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(self._capture_splitter)
        top = QWidget()
        root = QHBoxLayout(top)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- 左栏 ----
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._build_device_ops_group())
        left.addWidget(self._build_filename_group())
        # 麦穗:正/反两张;籽粒:每重复一张、无正反。两者都有工作流面板。
        if self.mode == "wheat":
            self.spike_panel = SpikePanel(title="麦穗工作流", sides=WHEAT_SIDES,
                                          default_reps=5)
        else:
            self.spike_panel = SpikePanel(title="籽粒工作流", sides=GRAIN_SIDES,
                                          default_reps=3)
        self.spike_panel.set_prefix_clearer(self._clear_prefix_after_workflow)
        self.spike_panel.set_disk_checker(self._spike_file_exists)
        self.spike_panel.set_precheck(self._spike_precheck)
        self.spike_panel.start_requested.connect(self._on_workflow_start_requested)
        self.spike_panel.workflow_state_changed.connect(self._on_workflow_state_changed)
        self.spike_panel.workflow_completed.connect(self._on_workflow_completed)
        # 前缀或格式变了 → 面板要重新扫盘
        self.le_prefix.textChanged.connect(
            lambda _: self.spike_panel.request_refresh()
            if self.spike_panel is not None else None
        )
        self.cb_format.currentIndexChanged.connect(
            lambda _: self.spike_panel.request_refresh()
            if self.spike_panel is not None else None
        )
        left.addWidget(self.spike_panel)
        dev_list = self._build_device_list_group()
        dev_list.setMinimumHeight(110)   # 给设备列表一个下限,矮屏不被压没
        left.addWidget(dev_list)
        left.addWidget(self._build_device_info_group())
        # 尾部弹性:大屏时多余空间留在底部(控件顶部对齐、完整显示);
        # 矮屏时内容超出 → 外层滚动区出现滚动条,绝不裁剪。
        left.addStretch(1)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_scroll = self._wrap_scroll(left_widget)

        # ---- 中间显示 ----
        self.view = ImageView()

        # ---- 右栏 ----
        right = QVBoxLayout()
        right.setSpacing(6)
        right.addLayout(self._build_capture_buttons())
        # 保存成功提示音(默认不开)— 勾选框带白色轮廓,深色背景下醒目
        self.cb_sound = QCheckBox("质量通过提示音（可拍下一张）")
        self.cb_sound.setChecked(False)
        self.cb_sound.setToolTip("质量检查通过后提示可以切换姿势；图片继续后台保存")
        self.cb_sound.setStyleSheet(
            "QCheckBox::indicator {"
            "  width: 15px; height: 15px;"
            "  border: 2px solid #ffffff;"
            "  border-radius: 3px;"
            "  background: transparent;"
            "}"
            "QCheckBox::indicator:checked {"
            "  background: #4caf50;"           # 勾上后绿色填充
            "  border: 2px solid #ffffff;"
            "}"
        )
        right.addWidget(self.cb_sound)
        # 参数页用 tab 自带滚动,这里 stretch=1 让它吃掉竖向空间
        right.addWidget(self._build_param_tabs(), stretch=1)

        right_widget = QWidget()
        right_widget.setLayout(right)
        right_scroll = self._wrap_scroll(right_widget)

        root.addWidget(left_scroll)
        root.addWidget(self.view, stretch=1)
        root.addWidget(right_scroll)
        self._capture_splitter.addWidget(top)
        self.gallery = PhotoGallery(self.pipeline, self)
        self.gallery.retry_requested.connect(self.pipeline.retry)
        self._capture_splitter.addWidget(self.gallery)
        self._capture_splitter.setStretchFactor(0, 1)
        self._capture_splitter.setStretchFactor(1, 0)
        self._capture_splitter.setSizes([610, 190])
        self.le_prefix.editingFinished.connect(self._sync_gallery_context)
        self.cb_format.currentTextChanged.connect(lambda _: self._sync_gallery_context())

    @staticmethod
    def _wrap_scroll(inner: QWidget) -> QScrollArea:
        """把控制栏包进竖向滚动区:放不下就滚动,保证所有控件/按键都能显示完整。
        固定宽度 ~312(300 内容 + 滚动条余量),横向不滚动。"""
        sa = QScrollArea()
        sa.setWidget(inner)
        sa.setWidgetResizable(True)
        sa.setFixedWidth(314)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        return sa

    def _build_device_ops_group(self) -> QGroupBox:
        gb = QGroupBox("设备操作")
        lay = QHBoxLayout(gb)
        self.btn_refresh = QPushButton("刷新设备")
        self.btn_connect = QPushButton("连接")
        self.btn_disconnect = QPushButton("断开")
        for b in (self.btn_refresh, self.btn_connect, self.btn_disconnect):
            lay.addWidget(b)
        return gb

    def _build_filename_group(self) -> QGroupBox:
        gb = QGroupBox("文件名设置")
        lay = QGridLayout(gb)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setVerticalSpacing(6)

        row = 0
        lay.addWidget(QLabel("品种名来源:"), row, 0, 1, 3)
        source_buttons = QHBoxLayout()
        source_buttons.setSpacing(4)
        self.btn_prefix_manual = QPushButton("手动输入品种名")
        self.btn_prefix_qr = QPushButton("识别标签二维码")
        self.btn_prefix_manual.setCheckable(True)
        self.btn_prefix_qr.setCheckable(True)
        source_style = (
            "QPushButton:checked {"
            " background-color: #f29429; color: #1a1a1a;"
            " border: 1px solid #ffb45e; font-weight: 600;"
            "}"
        )
        self.btn_prefix_manual.setStyleSheet(source_style)
        self.btn_prefix_qr.setStyleSheet(source_style)
        self.btn_prefix_manual.setChecked(True)
        self.prefix_source_group = QButtonGroup(self)
        self.prefix_source_group.setExclusive(True)
        self.prefix_source_group.addButton(self.btn_prefix_manual)
        self.prefix_source_group.addButton(self.btn_prefix_qr)
        source_buttons.addWidget(self.btn_prefix_manual)
        source_buttons.addWidget(self.btn_prefix_qr)
        row += 1
        lay.addLayout(source_buttons, row, 0, 1, 3)

        row += 1
        # 麦穗/籽粒都用工作流接管命名,前缀统一叫"品种前缀"
        lay.addWidget(QLabel("品种前缀:"), row, 0)
        self.le_prefix = QLineEdit()
        self.le_prefix.setPlaceholderText("如 SX001")
        lay.addWidget(self.le_prefix, row, 1, 1, 2)

        row += 1
        self.lbl_bearer_key = QLabel("Bearer 密钥:")
        lay.addWidget(self.lbl_bearer_key, row, 0)
        self.le_bearer_key = QLineEdit()
        self.le_bearer_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.le_bearer_key.setPlaceholderText("仅保存在本次运行内存中")
        lay.addWidget(self.le_bearer_key, row, 1, 1, 2)
        self.lbl_bearer_key.hide()
        self.le_bearer_key.hide()

        # 重复序号和"正/侧"由工作流接管,这里不再展示;两者均为 None
        self.sp_repeat = None  # type: ignore
        self.cb_side = None    # type: ignore

        row += 1
        lay.addWidget(QLabel("文件格式:"), row, 0)
        self.cb_format = QComboBox()
        self.cb_format.addItems(["PNG", "JPG", "TIFF"])
        lay.addWidget(self.cb_format, row, 1, 1, 2)

        row += 1
        lay.addWidget(QLabel("保存路径:"), row, 0)
        self.lbl_save_dir = QLabel("(未设置)")
        self.lbl_save_dir.setWordWrap(True)
        self.lbl_save_dir.setStyleSheet("color: #777; font-style: italic;")
        lay.addWidget(self.lbl_save_dir, row, 1, 1, 2)

        row += 1
        self.btn_choose_dir = QPushButton("更换路径")
        lay.addWidget(self.btn_choose_dir, row, 1, 1, 2)

        return gb

    def _build_device_list_group(self) -> QGroupBox:
        gb = QGroupBox("设备列表")
        lay = QVBoxLayout(gb)
        self.lst_devices = QListWidget()
        lay.addWidget(self.lst_devices)
        return gb

    def _build_device_info_group(self) -> QGroupBox:
        gb = QGroupBox("设备信息")
        lay = QVBoxLayout(gb)
        lay.setSpacing(3)
        self.lbl_model = QLabel("设备型号: -")
        self.lbl_transport = QLabel("接口: -")
        self.lbl_manufacturer = QLabel("制造商: -")
        self.lbl_serial = QLabel("SN: -")
        for lab in (self.lbl_model, self.lbl_transport,
                    self.lbl_manufacturer, self.lbl_serial):
            lab.setStyleSheet("font-size: 11px;")
            lay.addWidget(lab)
        return gb

    def _build_capture_buttons(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        self.btn_stream = QPushButton("连续采集")
        self.btn_single = QPushButton("单次采集")
        self.btn_snap = QPushButton("抓拍")
        for b in (self.btn_stream, self.btn_single, self.btn_snap):
            b.setMinimumHeight(36)
            lay.addWidget(b)
        return lay

    def _build_param_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_camera_param_tab(), "相机参数")
        tabs.addTab(self._build_image_process_tab(), "图像处理")
        return tabs

    def _build_camera_param_tab(self) -> QWidget:
        w = QWidget()
        lay = QGridLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setVerticalSpacing(10)

        r = 0
        lay.addWidget(QLabel("曝光时间(μs):"), r, 0)
        self.sp_exposure = QDoubleSpinBox()
        self.sp_exposure.setRange(1.0, 10_000_000.0)
        self.sp_exposure.setDecimals(2)
        self.sp_exposure.setValue(8000.0)
        self.sp_exposure.setKeyboardTracking(False)
        lay.addWidget(self.sp_exposure, r, 1, 1, 2)

        r += 1
        lay.addWidget(QLabel("增益:"), r, 0)
        self.sp_gain = QDoubleSpinBox()
        self.sp_gain.setRange(0.0, 30.0)
        self.sp_gain.setDecimals(2)
        self.sp_gain.setValue(0.0)  # 默认增益 0,连相机后会强制写回相机
        self.sp_gain.setKeyboardTracking(False)
        lay.addWidget(self.sp_gain, r, 1, 1, 2)

        r += 1
        lay.addWidget(QLabel("白平衡模式:"), r, 0)
        self.cb_wb = QComboBox()
        # data 用枚举字符串,避数值依赖
        self.cb_wb.addItem("手动 (Off)", "Off")
        self.cb_wb.addItem("自动连续 (Continuous)", "Continuous")
        lay.addWidget(self.cb_wb, r, 1, 1, 2)

        r += 1
        self.btn_wb_once = QPushButton("一键白平衡")
        self.btn_wb_once.setToolTip(
            "对当前画面执行一次自动白平衡校准。\n"
            "需要相机已在采集状态(连续采集开启),把白色参考物或色卡放在画面中再点。\n"
            "校准后会切回手动并锁定本次结果,同时保存为用户白平衡(下次启动自动加载)。"
        )
        lay.addWidget(self.btn_wb_once, r, 0, 1, 2)

        self.btn_wb_restore = QPushButton("恢复出厂")
        self.btn_wb_restore.setToolTip(
            "丢弃当前的用户白平衡,回到软件出厂默认值。"
        )
        lay.addWidget(self.btn_wb_restore, r, 2)

        r += 1
        self.lbl_wb_preset = QLabel("白平衡来源: —")
        self.lbl_wb_preset.setStyleSheet("color: #888; font-size: 11px;")
        lay.addWidget(self.lbl_wb_preset, r, 0, 1, 3)

        r += 1
        # 手动 R/G/B 增益(整数节点,典型 0-4095,1024 ≈ unity)
        lay.addWidget(QLabel("R 增益:"), r, 0)
        self.sp_wb_r = QSpinBox()
        self.sp_wb_r.setRange(0, 4095)
        self.sp_wb_r.setSingleStep(16)
        self.sp_wb_r.setKeyboardTracking(False)
        lay.addWidget(self.sp_wb_r, r, 1, 1, 2)

        r += 1
        lay.addWidget(QLabel("G 增益:"), r, 0)
        self.sp_wb_g = QSpinBox()
        self.sp_wb_g.setRange(0, 4095)
        self.sp_wb_g.setSingleStep(16)
        self.sp_wb_g.setKeyboardTracking(False)
        lay.addWidget(self.sp_wb_g, r, 1, 1, 2)

        r += 1
        lay.addWidget(QLabel("B 增益:"), r, 0)
        self.sp_wb_b = QSpinBox()
        self.sp_wb_b.setRange(0, 4095)
        self.sp_wb_b.setSingleStep(16)
        self.sp_wb_b.setKeyboardTracking(False)
        lay.addWidget(self.sp_wb_b, r, 1, 1, 2)

        r += 1
        lay.addWidget(QLabel("图像旋转角度:"), r, 0)
        self.cb_rotate = QComboBox()
        for d in (0, 90, 180, 270):
            self.cb_rotate.addItem(f"{d}°", d)
        self.cb_rotate.setCurrentIndex(self.cb_rotate.findData(180))
        lay.addWidget(self.cb_rotate, r, 1, 1, 2)

        r += 1
        lay.setRowStretch(r, 1)
        return w

    def _build_image_process_tab(self) -> QWidget:
        w = QWidget()
        lay = QGridLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setVerticalSpacing(10)

        _ISP_TIP = (
            "本相机在 BayerBG8 像素格式下相机端 ISP 关闭,该参数无法修改。\n"
            "项目走色卡校正路线 — 保存的图保持 raw,后期用色卡做严格颜色校正。"
        )

        r = 0
        lay.addWidget(QLabel("亮度:"), r, 0)
        self.sp_brightness = QSpinBox()
        self.sp_brightness.setRange(0, 4095)
        self.sp_brightness.setValue(50)
        self.sp_brightness.setKeyboardTracking(False)
        self.sp_brightness.setToolTip(_ISP_TIP)
        lay.addWidget(self.sp_brightness, r, 1)

        r += 1
        lay.addWidget(QLabel("Gamma:"), r, 0)
        self.sp_gamma = QDoubleSpinBox()
        self.sp_gamma.setRange(0.0, 4.0)
        self.sp_gamma.setDecimals(2)
        self.sp_gamma.setValue(1.0)
        self.sp_gamma.setKeyboardTracking(False)
        self.sp_gamma.setToolTip(_ISP_TIP)
        lay.addWidget(self.sp_gamma, r, 1)

        r += 1
        lay.addWidget(QLabel("色度:"), r, 0)
        self.sp_saturation = QSpinBox()
        self.sp_saturation.setRange(0, 4095)
        self.sp_saturation.setValue(128)
        self.sp_saturation.setKeyboardTracking(False)
        self.sp_saturation.setToolTip(_ISP_TIP)
        lay.addWidget(self.sp_saturation, r, 1)

        r += 1
        lay.addWidget(QLabel("对比度:"), r, 0)
        self.sp_contrast = QSpinBox()
        self.sp_contrast.setRange(0, 4095)
        self.sp_contrast.setValue(50)
        self.sp_contrast.setKeyboardTracking(False)
        self.sp_contrast.setToolTip(_ISP_TIP)
        lay.addWidget(self.sp_contrast, r, 1)

        r += 1
        lay.addWidget(QLabel("锐度:"), r, 0)
        self.sp_sharpness = QSpinBox()
        self.sp_sharpness.setRange(0, 4095)
        self.sp_sharpness.setValue(0)
        self.sp_sharpness.setKeyboardTracking(False)
        self.sp_sharpness.setToolTip(_ISP_TIP)
        lay.addWidget(self.sp_sharpness, r, 1)

        r += 1
        lay.addWidget(QLabel("帧率:"), r, 0)
        self.lbl_fps = QLabel("-")
        lay.addWidget(self.lbl_fps, r, 1)

        r += 1
        lay.addWidget(QLabel("像素格式:"), r, 0)
        self.lbl_pixel = QLabel("-")
        lay.addWidget(self.lbl_pixel, r, 1)

        r += 1
        self.btn_reload_params = QPushButton("获取参数")
        lay.addWidget(self.btn_reload_params, r, 0, 1, 2)

        # ---- 平场校正(FFC) ----
        r += 1
        sep = QLabel("光场校正(去暗角)")
        sep.setStyleSheet("color: #c8c8c8; font-weight: 500; margin-top: 8px;")
        lay.addWidget(sep, r, 0, 1, 2)

        r += 1
        self.btn_capture_dark = QPushButton("采集暗场")
        self.btn_capture_dark.setToolTip(
            "盖镜头盖(或关掉光源),用当前曝光/增益采集一张暗场。\n"
            "暗场用于扣除相机暗电流和读出噪声,可选(短曝光下贡献小)。\n"
            "采集前请确保曝光/增益已锁定为实际工作值。"
        )
        lay.addWidget(self.btn_capture_dark, r, 0)

        self.btn_capture_flat = QPushButton("采集亮场")
        self.btn_capture_flat.setToolTip(
            "把均匀白板(或灰板)完整盖住视野,曝光不要饱和(亮度 128-230)。\n"
            "采集后保存到 calibration/ 目录,下次启动自动加载。\n"
            "若已采暗场,会自动按 (F-D) 公式计算 gain。"
        )
        lay.addWidget(self.btn_capture_flat, r, 1)

        r += 1
        self.cb_ffc_enable = QCheckBox("启用校正")
        self.cb_ffc_enable.setChecked(True)
        self.cb_ffc_enable.setEnabled(False)  # 没参考时禁用
        lay.addWidget(self.cb_ffc_enable, r, 0)

        self.cb_save_raw = QCheckBox("同时存原图(_raw)")
        self.cb_save_raw.setChecked(False)
        self.cb_save_raw.setToolTip("勾上后,抓拍会额外保存一张未校正的原图,文件名加 _raw 后缀")
        lay.addWidget(self.cb_save_raw, r, 1)

        r += 1
        self.btn_clear_flat = QPushButton("清除参考")
        lay.addWidget(self.btn_clear_flat, r, 0, 1, 2)

        r += 1
        self.lbl_ffc_status = QLabel("校正状态: 未采集参考")
        self.lbl_ffc_status.setStyleSheet("color: #888; font-size: 11px;")
        lay.addWidget(self.lbl_ffc_status, r, 0, 1, 2)

        # ---- 图像质量(BRISQUE)----
        r += 1
        sep2 = QLabel("图像质量(BRISQUE)")
        sep2.setStyleSheet("color: #c8c8c8; font-weight: 500; margin-top: 8px;")
        lay.addWidget(sep2, r, 0, 1, 2)

        r += 1
        self.lbl_brisque_last = QLabel("本张: —")
        self.lbl_brisque_last.setStyleSheet("font-size: 14px; font-weight: 600;")
        lay.addWidget(self.lbl_brisque_last, r, 0, 1, 2)

        r += 1
        lay.addWidget(QLabel("不合格阈值:"), r, 0)
        self.sp_brisque_threshold = QDoubleSpinBox()
        self.sp_brisque_threshold.setRange(1.0, 100.0)
        self.sp_brisque_threshold.setDecimals(1)
        self.sp_brisque_threshold.setSingleStep(1.0)
        self.sp_brisque_threshold.setValue(DEFAULT_THRESHOLD)
        self.sp_brisque_threshold.setKeyboardTracking(False)
        self.sp_brisque_threshold.setToolTip(
            "BRISQUE 分数 > 此阈值视为不合格,会提示重拍。\n"
            f"分数越低越好;阈值越小越严格。默认 {DEFAULT_THRESHOLD:.0f}。"
        )
        lay.addWidget(self.sp_brisque_threshold, r, 1)

        r += 1
        self.lbl_brisque_stats = QLabel(f"最近 0 张 | 阈值 {DEFAULT_THRESHOLD:.0f}")
        self.lbl_brisque_stats.setStyleSheet("color: #aaa; font-size: 11px;")
        lay.addWidget(self.lbl_brisque_stats, r, 0, 1, 2)

        r += 1
        self.lbl_brisque_hint = QLabel(
            "评分原理:BRISQUE 越低越好(0=最佳,超过阈值不合格)\n"
            "日志: logs/capture_log.csv"
        )
        self.lbl_brisque_hint.setStyleSheet("color: #777; font-size: 10px;")
        self.lbl_brisque_hint.setWordWrap(True)
        lay.addWidget(self.lbl_brisque_hint, r, 0, 1, 2)

        r += 1
        lay.setRowStretch(r, 1)
        return w

    # ============================== 信号绑定 ==============================

    def _bind_signals(self) -> None:
        self.btn_refresh.clicked.connect(self.refresh_devices)
        self.btn_connect.clicked.connect(self.connect_device)
        self.btn_disconnect.clicked.connect(self.disconnect_device)

        self.btn_stream.clicked.connect(self.toggle_streaming)
        self.btn_single.clicked.connect(self.single_capture)
        self.btn_snap.clicked.connect(self.snap)

        self.btn_choose_dir.clicked.connect(self.choose_save_dir)
        self.btn_reload_params.clicked.connect(self.reload_params_from_camera)
        self.btn_prefix_manual.toggled.connect(
            lambda checked: self._set_prefix_source("manual") if checked else None
        )
        self.btn_prefix_qr.toggled.connect(
            lambda checked: self._set_prefix_source("qr") if checked else None
        )
        self.le_bearer_key.textChanged.connect(self.bearer_key_changed.emit)

        # BRISQUE 阈值
        self.sp_brisque_threshold.valueChanged.connect(self._on_brisque_threshold_changed)

        # 平场校正控件
        self.btn_capture_dark.clicked.connect(self._capture_dark_field)
        self.btn_capture_flat.clicked.connect(self._capture_flat_field)
        self.btn_clear_flat.clicked.connect(self._clear_flat_field)
        self.cb_ffc_enable.toggled.connect(self._on_ffc_toggled)

        self.sp_exposure.editingFinished.connect(
            lambda: self._safe_set_float("ExposureTime", self.sp_exposure.value())
        )
        self.sp_gain.editingFinished.connect(
            lambda: self._safe_set_float("Gain", self.sp_gain.value())
        )
        self.cb_wb.currentIndexChanged.connect(
            lambda _: self._safe_set_enum_string("BalanceWhiteAuto", self.cb_wb.currentData())
        )
        self.btn_wb_once.clicked.connect(self._trigger_wb_once)
        self.btn_wb_restore.clicked.connect(self._restore_factory_wb)
        self.sp_wb_r.editingFinished.connect(
            lambda: self._set_balance_ratio("Red", int(self.sp_wb_r.value()))
        )
        self.sp_wb_g.editingFinished.connect(
            lambda: self._set_balance_ratio("Green", int(self.sp_wb_g.value()))
        )
        self.sp_wb_b.editingFinished.connect(
            lambda: self._set_balance_ratio("Blue", int(self.sp_wb_b.value()))
        )
        self.cb_rotate.currentIndexChanged.connect(self._on_rotation_changed)

        self.sp_brightness.editingFinished.connect(
            lambda: self._safe_set_int("Brightness", self.sp_brightness.value())
        )
        self.sp_gamma.editingFinished.connect(
            lambda: self._safe_set_float("Gamma", self.sp_gamma.value())
        )
        self.sp_saturation.editingFinished.connect(
            lambda: self._safe_set_int("Saturation", self.sp_saturation.value())
        )
        self.sp_contrast.editingFinished.connect(
            lambda: self._safe_set_int("Contrast", self.sp_contrast.value())
        )
        self.sp_sharpness.editingFinished.connect(
            lambda: self._safe_set_int("Sharpness", self.sp_sharpness.value())
        )

        # 空格抓拍快捷键(在文本输入控件上时让出,防止打字误触发)
        sc_snap = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc_snap.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_snap.activated.connect(self._snap_via_shortcut)
        # Esc 释放任何输入控件的焦点(让 editingFinished 触发)
        sc_esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        sc_esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_esc.activated.connect(self._clear_input_focus)

    # ============================== 品种名来源 ==============================

    def set_bearer_key(self, value: str) -> None:
        """由另一个页签同步 Bearer 密钥；阻断信号以避免回发循环。"""
        if value == self.le_bearer_key.text():
            return
        blocked = self.le_bearer_key.blockSignals(True)
        try:
            self.le_bearer_key.setText(value)
        finally:
            self.le_bearer_key.blockSignals(blocked)

    def _set_prefix_source(self, source: Literal["manual", "qr"]) -> None:
        if source not in ("manual", "qr"):
            return
        changed = source != self._prefix_source
        self._prefix_source = source
        if source == "manual":
            self._active_field_tag_code = None
            self._qr_request_code = None
            self.view.clear_qr_outline()
            if changed:
                self._invalidate_qr_request()
        self._update_prefix_source_controls()

    def _update_prefix_source_controls(self) -> None:
        active = self.spike_panel is not None and self.spike_panel.workflow.active
        locked = active or self._qr_request_pending
        qr_mode = self._prefix_source == "qr"
        self.btn_prefix_manual.setEnabled(not locked)
        self.btn_prefix_qr.setEnabled(not locked)
        self.le_prefix.setEnabled(not locked and not qr_mode)
        self.lbl_bearer_key.setVisible(qr_mode)
        self.le_bearer_key.setVisible(qr_mode)
        self.le_bearer_key.setEnabled(qr_mode and not locked)

    def _set_qr_request_pending(self, pending: bool) -> None:
        self._qr_request_pending = bool(pending)
        if not pending:
            self._qr_timeout_timer.stop()
            self._qr_timeout_request_id = None
        if self.spike_panel is not None:
            self.spike_panel.set_start_busy(self._qr_request_pending)
        self._update_prefix_source_controls()

    def _invalidate_qr_request(self) -> None:
        """让检测线程/网络回调失效，并取消当前网络请求。"""
        self._qr_request_id += 1
        self._qr_request_code = None
        reply = self._qr_reply
        self._qr_reply = None
        self._set_qr_request_pending(False)
        if reply is not None:
            try:
                reply.abort()
            except RuntimeError:
                pass
            reply.deleteLater()

    def _on_preview_cleared(self) -> None:
        self._latest_preview_bgr = None
        self._invalidate_qr_request()

    # ============================== 设备 ==============================

    def refresh_devices(self) -> None:
        try:
            devs = HikCamera.enumerate()
        except CameraError as e:
            self._err(str(e))
            return
        self._devices = devs
        self.lst_devices.clear()
        if not devs:
            item = QListWidgetItem("未检测到相机")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.lst_devices.addItem(item)
            self._status("未检测到相机", side=True)
            return
        for d in devs:
            self.lst_devices.addItem(QListWidgetItem(d.display))
        self.lst_devices.setCurrentRow(0)
        self._status(f"已发现 {len(devs)} 台相机", side=True)

    def connect_device(self) -> None:
        if self.camera.is_connected():
            return
        row = self.lst_devices.currentRow()
        if row < 0 or row >= len(self._devices):
            self._err("请先在设备列表中选择一台相机")
            return
        try:
            info = self.camera.connect(row)
        except CameraError as e:
            self._err(str(e))
            return
        self._fill_device_info(info)
        self.reload_params_from_camera()
        # 默认增益强制为 0:
        #   1) 先关 GainAuto(若 Continuous 会立刻把 Gain 拉回去)
        #   2) 写 Gain=0
        #   3) 回读相机实际 Gain → 同步 UI(UI 永远等于相机真实值)
        try:
            self.camera.set_enum_string("GainAuto", "Off")
        except Exception as e:
            log.debug("[%s] GainAuto=Off 失败(节点可能不存在): %s", self.mode, e)
        try:
            self.camera.set_float("Gain", 0.0)
            log.info("[%s] 连接后写 Gain=0.0", self.mode)
        except Exception as e:
            log.warning(
                "[%s] 写 Gain=0 失败 — 相机可能 min>0 或拒绝: %s", self.mode, e
            )
        # 回读 → 同步 UI,UI 反映相机真实状态
        self._loading_params = True
        try:
            self._load_float("Gain", self.sp_gain)
            v = self.camera.get_float("Gain")
            if v is not None:
                log.info("[%s] Gain 回读 = %.3f (range %.2f-%.2f)",
                         self.mode, v[0], v[1], v[2])
        finally:
            self._loading_params = False
        # 应用用户/出厂白平衡预设
        wb, source = wb_config.active_wb()
        log.info("[%s] 连接后应用白平衡: %s (来源=%s)", self.mode, wb, source)
        self._apply_wb_to_camera(wb)
        self._refresh_metadata_enum_cache()
        self._refresh_wb_preset_label()
        self._update_button_states()
        self._status("相机已连接", side=True)

    def disconnect_device(self) -> None:
        if self._stream is not None:
            self._stop_stream()
        if self.camera.is_connected():
            try:
                self.camera.disconnect()
            except CameraError as e:
                self._err(str(e))
        self.view.clear_display()
        self._clear_metadata_camera_cache()
        self._fill_device_info(None)
        self._update_button_states()
        self._status("相机已断开", side=True)

    def _fill_device_info(self, info: Optional[DeviceInfo]) -> None:
        if info is None:
            self.lbl_model.setText("设备型号: -")
            self.lbl_transport.setText("接口: -")
            self.lbl_manufacturer.setText("制造商: -")
            self.lbl_serial.setText("SN: -")
        else:
            self.lbl_model.setText(f"设备型号: {info.model}")
            extra = f" / {info.ip}" if info.ip else ""
            self.lbl_transport.setText(f"接口: {info.transport}{extra}")
            self.lbl_manufacturer.setText(f"制造商: {info.manufacturer or '-'}")
            self.lbl_serial.setText(f"SN: {info.serial or '-'}")

    # ============================== 采集 ==============================

    def toggle_streaming(self) -> None:
        if self._stream is not None:
            self._stop_stream()
            return
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        self._stream = StreamWorker(self.camera, correction=self.correction, parent=self)
        self._stream.frame_ready.connect(self._on_frame)
        self._stream.error.connect(self._on_stream_error)
        self._stream.finished.connect(self._on_stream_finished)
        self._stream.start()
        self.btn_stream.setText("停止采集")
        self._update_button_states(streaming=True)
        self._status("正在采集图像")

    def _stop_stream(self) -> None:
        if self._stream is None:
            return
        s = self._stream
        self._stream = None
        s.stop()
        s.wait(2000)
        # 断开所有信号 + 交还 Qt 删除 — 否则 parent=self 会让旧 QThread
        # 一直挂在对象树上,反复开关采集会累积泄漏。
        try:
            s.frame_ready.disconnect(self._on_frame)
            s.error.disconnect(self._on_stream_error)
            s.finished.disconnect(self._on_stream_finished)
        except (TypeError, RuntimeError):
            pass
        if s.isRunning():
            # 2s 内没退出(SDK 卡在底层取流)— 删一个仍在 run() 的 QThread 会崩。
            # 宁可放任它泄漏(进程退出时随之回收),也不要 deleteLater 触发崩溃。
            log.error("StreamWorker 2s 未停止,放弃删除以避免崩溃")
        else:
            s.setParent(None)
            s.deleteLater()
        self.btn_stream.setText("连续采集")
        self._update_button_states(streaming=False)

    def _on_frame(self, bgr: np.ndarray) -> None:
        # 保存与预览独立；无论显示是否成功都必须归还单槽。
        sender = self.sender()
        stream = sender if isinstance(sender, StreamWorker) else self._stream
        try:
            self._show_preview_frame(self._apply_rotation(bgr))
        finally:
            if stream is not None:
                stream.display_consumed()

    def _show_preview_frame(self, bgr: np.ndarray) -> None:
        """缓存与画面实际一致的已旋转预览帧。"""
        self._latest_preview_bgr = bgr
        self.view.set_bgr(bgr)

    def _on_stream_error(self, msg: str) -> None:
        log.error("stream error: %s", msg)
        self._status(f"取流异常: {msg}")
        self._err(msg)

    def _on_stream_finished(self) -> None:
        self.btn_stream.setText("连续采集")
        self._update_button_states(streaming=False)

    def single_capture(self) -> None:
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        # 抓拍进行中(非取流态下会临时 start/stop_grabbing)时不要再单独取帧,
        # 否则两条主线程路径会互相 start/stop_grabbing 打架,导致超时/漏 free。
        if self._snap_in_flight:
            return
        if self._stream is not None:
            # 已经在采集 → 直接用最新帧刷新显示即可
            bgr = self._stream.latest_bgr
            if bgr is not None:
                self._show_preview_frame(self._apply_rotation(bgr))
            return
        try:
            self.camera.start_grabbing()
            result = self.camera.grab_frame_bgr(timeout_ms=2000)
            if result is None:
                self._err("单次采集超时")
                return
            bgr, raw = result
            try:
                self._show_preview_frame(self._apply_rotation(bgr))
            finally:
                self.camera.free_frame(raw)
        except CameraError as e:
            self._err(str(e))
        finally:
            try:
                self.camera.stop_grabbing()
            except CameraError:
                pass

    def _snap_via_shortcut(self) -> None:
        """空格触发的抓拍 — 文本输入控件持有焦点时让出,允许输入空格。"""
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QAbstractSpinBox)):
            # 把空格直接送回输入控件
            focused.insert(" ") if isinstance(focused, QLineEdit) else None
            return
        self.snap()

    def _estimated_capture_bytes(self) -> int:
        snapshot = self._stream.latest_frame if self._stream else None
        size = snapshot.bgr.nbytes if snapshot is not None else 5120 * 5120 * 3
        return size * (4 if self.correction.enabled and self.cb_save_raw.isChecked() else 3)

    def _sync_gallery_context(self) -> None:
        panel = self.spike_panel
        wf = panel.workflow if panel else None
        prefix = wf.prefix if wf and wf.active else self.le_prefix.text().strip()
        total = wf.total if wf and wf.active else 0
        self.gallery.set_context(self._save_dir, prefix, self.mode, total,
                                 self.cb_format.currentText())

    def _refresh_capture_capacity(self) -> None:
        if self._shutting_down:
            return
        can_take = self.pipeline.can_accept(self._estimated_capture_bytes())
        panel = self.spike_panel
        wf = panel.workflow if panel else None
        has_step = not (wf and wf.active) or wf.current_step() is not None
        self.btn_snap.setEnabled(self.camera.is_connected() and not self._snap_in_flight
                                 and not self._closing and can_take and has_step)
        self.btn_snap.setText("抓拍" if can_take else "保存失败，请重试" if self.pipeline.has_failures
                             else "后台保存中，请稍候")
        pending = self.pipeline.owner_pending(self._owner) > 0
        if panel:
            panel.set_capture_busy(pending)
            current = wf.current_step() if wf.active else None
            next_current = (current.rep, current.side) if current else (None, '')
            if next_current != self.gallery.current:
                self.gallery.current = next_current
                self.gallery.list.viewport().update()
        if self._stream:
            self._stream.set_saving(self.pipeline.pending_count > 0)
        # 固定一轮的保存位置和格式，防止断点续拍检查与在途任务不一致。
        self.btn_choose_dir.setEnabled(not pending)
        self.cb_format.setEnabled(not pending)

    def snap(self) -> None:
        click_t0 = time.perf_counter()
        if self._snap_in_flight or self._closing or self._shutting_down:
            return
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        if not self.pipeline.can_accept(self._estimated_capture_bytes()):
            self._status("后台保存中，请稍候；保存失败时请在照片带重试", side=True)
            return
        self._clear_input_focus()
        panel = self.spike_panel
        wf = panel.workflow if panel else None
        active = bool(wf and wf.active)
        step = wf.current_step() if active else None
        if active and step is None:
            return
        prefix = wf.prefix if active else self.le_prefix.text().strip()
        if not prefix:
            self._err("请输入文件名前缀")
            return
        if self._save_dir is None and not self._prompt_save_dir():
            return
        self._snap_in_flight = True
        self.btn_snap.setEnabled(False)
        try:
            stream = self._stream
            template_frame = stream.latest_frame if stream else self._get_current_frame_snapshot_for_snap()
            if template_frame is None:
                raise RuntimeError("尚未取得相机画面，请稍候再拍")
            ext = self.cb_format.currentText().lower()
            if active:
                path = self._save_dir / wf.filename_for_current(ext)
                overwrite = step.status.value == 'failed' or step.overwrite_allowed
            else:
                serial = naming.next_serial(self._save_dir, prefix)
                reserved = self.pipeline.reserved_paths()
                while True:
                    path = self._save_dir / naming.build_filename(prefix, serial=serial, ext=ext)
                    if path not in reserved and not path.exists():
                        break
                    serial += 1
                overwrite = False
            raw_path = path.with_stem(path.stem + "_raw") if (
                self.correction.enabled and self.cb_save_raw.isChecked()) else None
            correction = self.correction.snapshot()
            h,w = template_frame.bgr.shape[:2]
            if self._rotation_deg in (90,270):
                h,w = w,h
            metadata = self._create_capture_metadata(
                frame_snapshot=template_frame, prefix=prefix, workflow_active=active,
                rep=step.rep if step else None, side=step.side or None if step else None,
                path=path, rotation_deg=self._rotation_deg, ffc_applied=correction.enabled,
                output_width=w, output_height=h)
            session = wf.session_id if active else self._owner
            attempt_key = (session, path)
            attempt = self._capture_attempts.get(attempt_key, 0) + 1
            job_id = metadata.capture_id
            get_frame = (lambda: stream.fresh_snapshot(job_id)) if stream else (lambda: template_frame)
            job = CaptureJob(metadata,self._owner,session,attempt,path,raw_path,
                             get_frame,correction,self.brisque if self._brisque_ok else None,
                             self._brisque_threshold,self._estimated_capture_bytes(),overwrite,
                             submitted_at=click_t0)
            self._sync_gallery_context()
            if not self.pipeline.submit(job):
                raise RuntimeError("保存队列已满或文件名已被占用，请稍候再拍")
            self._capture_attempts[attempt_key] = attempt
            self._preparing_id = job.id
            if active:
                wf.bind_capture(job.id)
            self._refresh_capture_capacity()
        except Exception as exc:
            self._snap_in_flight = False
            self._preparing_id = None
            self._refresh_capture_capacity()
            self._err(str(exc))

    def _on_pipeline_event(self, event: CaptureEvent) -> None:
        if self._shutting_down:
            return
        if event.owner != self._owner:
            self._refresh_capture_capacity()
            return
        self.gallery.update_event(event)
        panel = self.spike_panel
        if panel:
            panel.set_capture_busy(self.pipeline.owner_pending(self._owner) > 0)
        if event.state in ('quality', 'capture_failed'):
            if event.id == self._preparing_id:
                self._snap_in_flight = False
                self._preparing_id = None
        if event.state == 'quality':
            if event.score is not None:
                self._brisque_history.append(event.score)
                self.lbl_brisque_last.setText(f"{event.path.stem}: {event.score:.2f}  "
                                              f"[{'合格' if event.passed else '不合格'}]")
                self.lbl_brisque_last.setStyleSheet('color: '+ ('#4caf50' if event.passed else '#ef5350'))
                self._on_brisque_threshold_changed(self._brisque_threshold)
            if event.passed:
                self._play_save_sound()
            else:
                self.view.show_warning(f"{event.path.stem} 质量不合格，请重拍")
            if event.metadata.workflow_active and panel:
                panel.capture_result(event.session,event.id,passed=event.passed,score=event.score)
            ready_text = ("可拍下一张" if self.pipeline.can_accept(self._estimated_capture_bytes())
                          else "质量通过，后台保存中请稍候") if event.passed else "请重拍当前视角"
            self._status(f"{ready_text}；后台待保存 {self.pipeline.pending_count} 张", side=True)
        elif event.state == 'saved':
            if event.metadata.workflow_active and panel:
                panel.capture_result(event.session,event.id,saved=True)
            if event.score is not None:
                self._append_capture_log(event.path,event.score,event.passed,
                                         event.timings.get('quality_s',0),
                                         threshold=event.threshold,
                                         ffc_enabled=event.metadata.ffc_applied)
            if event.message and '元数据' in event.message:
                self.view.show_warning(event.message, 5000)
        elif event.state == 'failed':
            if event.metadata.workflow_active and panel:
                panel.capture_result(event.session,event.id,save_failed=True)
            self.view.show_warning(f"{event.path.stem} 保存失败：{event.message}；请在照片带重试", 5000)
        elif event.state == 'capture_failed':
            if event.metadata.workflow_active and panel:
                panel.capture_result(event.session,event.id,passed=False)
            self.view.show_warning(f"抓拍失败：{event.message}，请重拍", 5000)
        self._refresh_capture_capacity()

        if event.state == 'quality' and event.submitted_at:
            log.info("capture ready id=%s ready=%.3fs available=%s frame=%s pending=%s",
                     event.id, time.perf_counter()-event.submitted_at,
                     self.btn_snap.isEnabled(), event.metadata.frame.frame_number,
                     self.pipeline.pending_count)

    def _play_save_sound(self) -> None:
        """质量通过、可切换姿势的提示音(勾选才响)。

        故意不用 winsound.MessageBeep(那是 Windows 对话框那个 'ding',
        和程序里的弹窗音一样,容易混淆)。改用 winsound.Beep 现场合成一段
        双音上升 '叮—咚'(G5→C6),系统里没有这个音,辨识度高。

        winsound.Beep 是阻塞的(两音约 250ms),放后台线程播,不卡 UI。
        """
        if not self.cb_sound.isChecked():
            return
        if sys.platform == "win32":
            def _chime():
                try:
                    import winsound
                    winsound.Beep(784, 100)    # G5
                    winsound.Beep(1047, 160)   # C6(上升,正向"成功"感)
                except Exception:
                    log.debug("提示音播放失败", exc_info=True)
            threading.Thread(target=_chime, daemon=True).start()
        else:
            try:
                QApplication.beep()
            except Exception:
                log.debug("提示音播放失败", exc_info=True)

    # ============================== BRISQUE 回调 ==============================

    def _on_brisque_threshold_changed(self, value: float) -> None:
        """用户改阈值 → 更新内部阈值并刷新统计行。"""
        self._brisque_threshold = float(value)
        if self._brisque_history:
            arr = list(self._brisque_history)
            self.lbl_brisque_stats.setText(
                f"最近 {len(arr)} 张 | 均 {sum(arr) / len(arr):.1f} | "
                f"最差 {max(arr):.1f} | 最好 {min(arr):.1f} | 阈值 {self._brisque_threshold:.0f}"
            )
        else:
            self.lbl_brisque_stats.setText(f"最近 0 张 | 阈值 {self._brisque_threshold:.0f}")

    def _append_capture_log(
        self, path: Path, score: float, passed: bool, elapsed: float,
        *, threshold: float | None = None, ffc_enabled: bool | None = None,
    ) -> None:
        """追加到 logs/capture_log.csv。首次写入加表头。"""
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            new_file = not _CAPTURE_LOG.exists()
            with _CAPTURE_LOG.open("a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow([
                        "timestamp", "tab", "filename", "brisque",
                        "verdict", "threshold", "elapsed_s",
                        "ffc_enabled",
                    ])
                w.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    self.mode,
                    path.name,
                    f"{score:.3f}",
                    "合格" if passed else "不合格",
                    f"{self._brisque_threshold if threshold is None else threshold:.1f}",
                    f"{elapsed:.3f}",
                    str(self.correction.enabled if ffc_enabled is None else ffc_enabled),
                ])
        except Exception:
            log.exception("写 capture_log.csv 失败")

    def _get_current_frame_snapshot_for_snap(self) -> FrameSnapshot | None:
        """取得图像与同一帧信息；SDK 缓冲释放后返回值仍可安全使用。"""
        if self._stream is not None:
            return self._stream.latest_frame
        # 没在采集 → 临时取一帧
        try:
            self.camera.start_grabbing()
            result = self.camera.grab_frame_bgr(timeout_ms=2000)
            if result is None:
                return None
            bgr, raw = result
            try:
                frame_metadata = self.camera.copy_frame_metadata(raw)
                return FrameSnapshot(bgr=bgr.copy(), metadata=frame_metadata)
            finally:
                self.camera.free_frame(raw)
        except CameraError as e:
            self._err(str(e))
            return None
        finally:
            try:
                self.camera.stop_grabbing()
            except CameraError:
                pass

    def _get_current_frame_for_snap(self) -> np.ndarray | None:
        """兼容 FFC 参考采集：只返回图像，抓拍保存使用配对快照接口。"""
        snapshot = self._get_current_frame_snapshot_for_snap()
        return None if snapshot is None else snapshot.bgr

    def _create_capture_metadata(
        self,
        *,
        frame_snapshot: FrameSnapshot,
        prefix: str,
        workflow_active: bool,
        rep: int | None,
        side: str | None,
        path: Path,
        rotation_deg: int,
        ffc_applied: bool,
        output_width: int,
        output_height: int,
    ) -> CaptureMetadata:
        """在主线程快照所有 UI/相机状态，后台保存线程只读该不可变对象。"""
        device = self.camera.device

        def enabled_int(widget: QSpinBox) -> int | None:
            return int(widget.value()) if widget.isEnabled() else None

        def enabled_float(widget: QDoubleSpinBox) -> float | None:
            return float(widget.value()) if widget.isEnabled() else None

        ext = path.suffix.lower()
        wb_mode = self.cb_wb.currentData()
        exposure_readback = self._metadata_float_cache["ExposureTime"]
        gain_readback = self._metadata_float_cache["Gain"]
        # 自动曝光/增益可在两次 UI 参数刷新之间变化；优先使用
        # SDK 随帧水印中的实际值，仍然不需要在抓拍热路径访问相机。
        if (
            self._metadata_enum_cache["ExposureAuto"] not in (None, 0)
            and frame_snapshot.metadata.exposure_time_us is not None
        ):
            exposure_readback = frame_snapshot.metadata.exposure_time_us
        if (
            self._metadata_enum_cache["GainAuto"] not in (None, 0)
            and frame_snapshot.metadata.gain is not None
        ):
            gain_readback = frame_snapshot.metadata.gain
        return CaptureMetadata(
            capture_id=CaptureMetadata.new_capture_id(),
            captured_at=frame_snapshot.metadata.received_at,
            accession_no=prefix,
            prefix_source=self._prefix_source,
            field_tag_code=(
                self._active_field_tag_code
                if self._prefix_source == "qr" else None
            ),
            mode=self.mode,
            workflow_active=workflow_active,
            rep=rep,
            side=side,
            filename=path.name,
            image_variant="processed",
            device_manufacturer=(device.manufacturer or None) if device else None,
            device_model=(device.model or None) if device else None,
            device_serial=(device.serial or None) if device else None,
            device_transport=(device.transport or None) if device else None,
            device_ip=(device.ip or None) if device else None,
            frame=frame_snapshot.metadata,
            exposure_readback_us=exposure_readback,
            gain_readback=gain_readback,
            exposure_auto_code=self._metadata_enum_cache["ExposureAuto"],
            gain_auto_code=self._metadata_enum_cache["GainAuto"],
            trigger_mode_code=self._metadata_enum_cache["TriggerMode"],
            white_balance_mode=str(wb_mode) if wb_mode is not None else None,
            white_balance_red=int(self.sp_wb_r.value()),
            white_balance_green=int(self.sp_wb_g.value()),
            white_balance_blue=int(self.sp_wb_b.value()),
            brightness=enabled_int(self.sp_brightness),
            gamma=enabled_float(self.sp_gamma),
            saturation=enabled_int(self.sp_saturation),
            contrast=enabled_int(self.sp_contrast),
            sharpness=enabled_int(self.sp_sharpness),
            output_format=ext.lstrip(".").upper(),
            jpeg_quality=92 if ext in (".jpg", ".jpeg") else None,
            png_compression=1 if ext == ".png" else None,
            tiff_compression="none" if ext in (".tif", ".tiff") else None,
            rotation_deg=rotation_deg,
            ffc_applied=ffc_applied,
            ffc_has_flat=self.correction.has_flat,
            ffc_has_dark=self.correction.has_dark,
            ffc_calibration_id=self.correction.reference_id,
            output_width=output_width,
            output_height=output_height,
        )

    # ============================== 路径 ==============================

    def choose_save_dir(self) -> None:
        self._prompt_save_dir()

    def _prompt_save_dir(self) -> bool:
        start = str(self._save_dir) if self._save_dir else ""
        while True:
            d = QFileDialog.getExistingDirectory(self, "选择保存文件夹(请用全英文路径)", start)
            if not d:
                return False
            # 路径必须全 ASCII(英文/数字/符号)。含中文等非 ASCII 字符会导致
            # 部分图像处理(如质量评分模型读取)异常,强制重选。
            try:
                d.encode("ascii")
            except UnicodeEncodeError:
                QMessageBox.warning(
                    self, "路径不合法",
                    "保存路径包含中文或非英文字符:\n"
                    f"{d}\n\n"
                    "请改用全英文(ASCII)路径,例如 D:/wheat_data/2026 。",
                )
                start = d
                continue
            break
        self._save_dir = Path(d)
        self._sync_gallery_context()
        self.lbl_save_dir.setText(str(self._save_dir))
        self.lbl_save_dir.setStyleSheet("color: #d8d8d8;")
        if self.spike_panel is not None:
            self.spike_panel.request_refresh()
        return True

    # ============================== 参数 ==============================

    def _on_rotation_changed(self, _idx: int) -> None:
        self._rotation_deg = int(self.cb_rotate.currentData() or 0)

    def _apply_rotation(self, bgr: np.ndarray) -> np.ndarray:
        return self._rotate_static(bgr, self._rotation_deg)

    @staticmethod
    def _rotate_static(bgr: np.ndarray, deg: int) -> np.ndarray:
        """无状态版,方便从工作线程调用(避免读 self._rotation_deg 的竞态)。"""
        if deg == 0:
            return bgr
        if deg == 90:
            return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
        if deg == 180:
            return cv2.rotate(bgr, cv2.ROTATE_180)
        if deg == 270:
            return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return bgr

    def _unlock_image_quality_params(self) -> None:
        """把图像质量参数的 Enable 节点全部打开。海康相机默认 Gamma/Saturation 等
        是 disabled,Bool 节点没打开时对应的值节点不可访问。
        Gamma 还需要把 GammaSelector 设为 'User',才能用我们写入的值。
        每个开关都 best-effort,失败不报错(相机可能不支持就跳过)。"""
        if not self.camera.is_connected():
            return
        enable_keys = (
            "GammaEnable", "BrightnessEnable", "SaturationEnable",
            "ContrastEnable", "SharpnessEnable", "HueEnable",
            "BlackLevelEnable", "ColorTransformationEnable",
            # 一些海康相机用 ColorCorrection 这个统一开关
            "ColorCorrectionEnable", "LUTEnable",
        )
        ok, fail = [], []
        for k in enable_keys:
            try:
                self.camera.set_bool(k, True)
                ok.append(k)
            except CameraError as e:
                fail.append(f"{k}({e.code:#x})")
        log.info("unlock 成功: %s", ok)
        log.info("unlock 失败/不支持: %s", fail)
        # Gamma 默认是 sRGB(只读曲线),改成 User 才能写自定义值
        try:
            self.camera.set_enum_string("GammaSelector", "User")
            log.info("GammaSelector=User OK")
        except CameraError as e:
            log.info("GammaSelector=User 失败: %s", e)

    def reload_params_from_camera(self) -> None:
        if not self.camera.is_connected():
            return
        self._loading_params = True
        try:
            # 注:Gamma/Saturation/Contrast/Sharpness 在 BayerBG8 像素格式下相机 ISP 关闭,
            # 这些节点访问会返回 0x80000106。不再尝试解锁,失败 → SpinBox 自动 disabled。
            # 项目走"色卡校正"路线,这些参数不动,保留 disabled 即可。
            self._load_float("ExposureTime", self.sp_exposure)
            self._load_float("Gain", self.sp_gain)
            self._load_int("Brightness", self.sp_brightness)
            self._load_float("Gamma", self.sp_gamma)
            self._load_int("Saturation", self.sp_saturation)
            self._load_int("Contrast", self.sp_contrast)
            self._load_int("Sharpness", self.sp_sharpness)

            # 白平衡当前模式 — 用数值映射回字符串
            wb = self.camera.get_enum("BalanceWhiteAuto")
            wb_str = {0: "Off", 1: "Once", 2: "Continuous"}.get(wb if wb is not None else 0, "Off")
            idx = self.cb_wb.findData(wb_str)
            self.cb_wb.setCurrentIndex(idx if idx >= 0 else 0)

            # R/G/B 增益(BalanceRatioSelector + BalanceRatio)
            self._reload_balance_ratios()

            fps = self.camera.get_float("ResultingFrameRate")
            self.lbl_fps.setText(f"{fps[0]:.1f}" if fps else "-")
            self.lbl_pixel.setText(self.camera.pixel_format_name())
            self._refresh_metadata_enum_cache()
        finally:
            self._loading_params = False

    def _refresh_metadata_enum_cache(self) -> None:
        """参数刷新/连接时一次性回读元数据所需的枚举节点。"""
        if not self.camera.is_connected():
            return
        for key in self._metadata_enum_cache:
            self._metadata_enum_cache[key] = self.camera.get_enum(key)

    def _clear_metadata_camera_cache(self) -> None:
        for key in self._metadata_float_cache:
            self._metadata_float_cache[key] = None
        for key in self._metadata_enum_cache:
            self._metadata_enum_cache[key] = None

    # Red=0, Green=1, Blue=2 — 海康 BalanceRatioSelector 枚举常量
    _WB_CHANNEL_IDX = {"Red": 0, "Green": 1, "Blue": 2}

    def _select_wb_channel(self, channel: str) -> bool:
        """选 R/G/B 通道。优先用数值枚举(更稳),fallback 到字符串。"""
        if not self.camera.is_connected():
            return False
        idx = self._WB_CHANNEL_IDX.get(channel)
        if idx is None:
            return False
        try:
            self.camera.set_enum("BalanceRatioSelector", idx)
            return True
        except CameraError as e1:
            log.debug("BalanceRatioSelector=%d num failed: %s,试字符串", idx, e1)
        try:
            self.camera.set_enum_string("BalanceRatioSelector", channel)
            return True
        except CameraError as e2:
            log.warning("BalanceRatioSelector=%s string also failed: %s", channel, e2)
            return False

    def _reload_balance_ratios(self) -> None:
        """从相机读 R/G/B BalanceRatio(整数节点)回填三个 SpinBox。"""
        mapping = (("Red", self.sp_wb_r), ("Green", self.sp_wb_g), ("Blue", self.sp_wb_b))
        for channel, spin in mapping:
            if not self._select_wb_channel(channel):
                spin.setEnabled(False)
                continue
            v = self.camera.get_int("BalanceRatio")
            if v is None:
                spin.setEnabled(False)
                continue
            cur, lo, hi = v
            spin.setEnabled(True)
            spin.blockSignals(True)
            spin.setRange(int(lo), int(hi))
            spin.setValue(int(cur))
            spin.blockSignals(False)

    def _load_float(self, key: str, spin: QDoubleSpinBox) -> None:
        v = self.camera.get_float(key)
        if v is None:
            spin.setEnabled(False)
            if key in self._metadata_float_cache:
                self._metadata_float_cache[key] = None
            return
        cur, lo, hi = v
        if key in self._metadata_float_cache:
            self._metadata_float_cache[key] = float(cur)
        spin.setEnabled(True)
        spin.blockSignals(True)
        spin.setRange(lo, hi)
        spin.setValue(cur)
        spin.blockSignals(False)

    def _load_int(self, key: str, spin: QSpinBox) -> None:
        v = self.camera.get_int(key)
        if v is None:
            spin.setEnabled(False)
            return
        cur, lo, hi = v
        spin.setEnabled(True)
        spin.blockSignals(True)
        spin.setRange(int(lo), int(hi))
        spin.setValue(int(cur))
        spin.blockSignals(False)

    def _safe_set_float(self, key: str, value: float) -> None:
        if self._loading_params or not self.camera.is_connected():
            return
        try:
            self.camera.set_float(key, value)
            if key in self._metadata_float_cache:
                readback = self.camera.get_float(key)
                self._metadata_float_cache[key] = (
                    float(readback[0]) if readback is not None else float(value)
                )
        except CameraError as e:
            log.warning("set %s=%s failed: %s", key, value, e)

    def _safe_set_int(self, key: str, value: int) -> None:
        if self._loading_params or not self.camera.is_connected():
            return
        try:
            self.camera.set_int(key, value)
        except CameraError as e:
            log.warning("set %s=%s failed: %s", key, value, e)

    def _safe_set_enum(self, key: str, value) -> None:
        if self._loading_params or value is None or not self.camera.is_connected():
            return
        try:
            self.camera.set_enum(key, int(value))
            if key in self._metadata_enum_cache:
                readback = self.camera.get_enum(key)
                self._metadata_enum_cache[key] = (
                    int(readback) if readback is not None else int(value)
                )
        except CameraError as e:
            log.warning("set enum %s=%s failed: %s", key, value, e)

    def _safe_set_enum_string(self, key: str, value) -> None:
        if self._loading_params or not value or not self.camera.is_connected():
            return
        try:
            self.camera.set_enum_string(key, str(value))
            if key in self._metadata_enum_cache:
                # 字符串枚举的数值由相机型号决定，只在参数修改时
                # 回读一次，不把 SDK 访问留在每张抓拍的热路径。
                self._metadata_enum_cache[key] = self.camera.get_enum(key)
        except CameraError as e:
            log.warning("set enum-str %s=%s failed: %s", key, value, e)

    def _set_balance_ratio(self, channel: str, value: int) -> None:
        """手动 R/G/B 增益调整 — 先选通道(数值枚举),再写整数值。"""
        if self._loading_params or not self.camera.is_connected():
            return
        if not self._select_wb_channel(channel):
            return
        try:
            self.camera.set_int("BalanceRatio", int(value))
        except CameraError as e:
            log.warning("set BalanceRatio[%s]=%s failed: %s", channel, value, e)

    def _trigger_wb_once(self) -> None:
        """触发一次自动白平衡 — 相机会基于当前画面校准,然后自动切回 Off。"""
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        if self._stream is None:
            QMessageBox.information(
                self, "提示",
                "请先点'连续采集'让相机有画面输入,再执行一键白平衡。"
            )
            return
        try:
            self.camera.set_enum_string("BalanceWhiteAuto", "Once")
        except CameraError as e:
            self._err(str(e))
            return
        self._status("一键白平衡已触发,2 秒后回读结果", side=False)
        # 等相机算完,把状态回填到 UI
        QTimer.singleShot(2000, self._after_wb_once)

    def _after_wb_once(self) -> None:
        """Once 计算完后:显式 Off 锁住 → 读 R/G/B → 重写值 → 存用户预设。"""
        if not self.camera.is_connected():
            return

        # 1) 先显式把 BalanceWhiteAuto 切回 Off 锁住自动模式
        #    某些相机不会自动从 Once 退回,导致 selector 锁死无法读 R/G/B
        try:
            self.camera.set_enum_string("BalanceWhiteAuto", "Off")
        except CameraError as e:
            log.warning("强制 BalanceWhiteAuto=Off 失败: %s", e)

        # 2) 读出相机算好的 R/G/B(整数节点)
        rgb: dict[str, int] = {}
        for ch in ("Red", "Green", "Blue"):
            if not self._select_wb_channel(ch):
                log.warning("read BalanceRatio[%s] selector failed", ch)
                continue
            v = self.camera.get_int("BalanceRatio")
            if v is not None:
                rgb[ch] = int(v[0])
        if len(rgb) != 3:
            self._err("读取 R/G/B 增益失败,无法保存为用户白平衡")
            return

        # 3) 重写一遍锁住(防御性)
        for ch, val in rgb.items():
            if not self._select_wb_channel(ch):
                continue
            try:
                self.camera.set_int("BalanceRatio", val)
            except CameraError as e:
                log.warning("回写 BalanceRatio[%s]=%d 失败: %s", ch, val, e)

        # 4) 存用户预设
        try:
            wb_config.save_user_wb(wb_config.WBParams(
                red=rgb["Red"], green=rgb["Green"], blue=rgb["Blue"],
            ))
        except Exception:
            log.exception("保存用户白平衡失败")

        # 5) 刷新 UI
        self._loading_params = True
        try:
            self._reload_balance_ratios()
            idx = self.cb_wb.findData("Off")
            self.cb_wb.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._loading_params = False
        self._refresh_wb_preset_label()
        self._status(
            f"白平衡完成并保存: R={rgb['Red']} G={rgb['Green']} B={rgb['Blue']}",
            side=False,
        )

    def _restore_factory_wb(self) -> None:
        """删用户预设 → 写出厂值到相机 → 刷新 UI。"""
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        ok = QMessageBox.question(
            self, "恢复出厂白平衡",
            "确定要丢弃当前用户白平衡,恢复软件出厂默认?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        wb_config.delete_user_wb()
        self._apply_wb_to_camera(wb_config.factory_wb())
        self._refresh_wb_preset_label()
        self._status("已恢复出厂白平衡", side=False)

    def _apply_wb_to_camera(self, wb: "wb_config.WBParams") -> None:
        """把一组 R/G/B 写入相机(强制 Off + 三通道分别写)。"""
        if not self.camera.is_connected():
            return
        # 强制手动模式
        try:
            self.camera.set_enum_string("BalanceWhiteAuto", "Off")
        except CameraError as e:
            log.warning("BalanceWhiteAuto=Off failed: %s", e)
        # 写三通道(整数节点)
        for ch, val in wb.as_camera_dict().items():
            if not self._select_wb_channel(ch):
                log.warning("apply WB: select %s failed", ch)
                continue
            try:
                self.camera.set_int("BalanceRatio", int(val))
            except CameraError as e:
                log.warning("apply WB[%s]=%d failed: %s", ch, val, e)
        # 写完回读 UI
        self._loading_params = True
        try:
            self._reload_balance_ratios()
            idx = self.cb_wb.findData("Off")
            self.cb_wb.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._loading_params = False

    def _refresh_wb_preset_label(self) -> None:
        wb, source = wb_config.active_wb()
        if source == "user":
            self.lbl_wb_preset.setText(
                f"白平衡来源: 用户保存 | {wb}"
            )
            self.lbl_wb_preset.setStyleSheet("color: #f29429; font-size: 11px;")
        else:
            self.lbl_wb_preset.setText(
                f"白平衡来源: 出厂默认 | {wb}"
            )
            self.lbl_wb_preset.setStyleSheet("color: #aaa; font-size: 11px;")

    def _clear_input_focus(self) -> None:
        """把当前输入控件的焦点收回,触发 editingFinished;按 Esc 或抓拍前调用。"""
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QAbstractSpinBox)):
            focused.clearFocus()
            self.view.setFocus()

    # ============================== 通用 ==============================

    def _update_button_states(self, *, streaming: Optional[bool] = None) -> None:
        connected = self.camera.is_connected()
        if streaming is None:
            streaming = self._stream is not None
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.btn_stream.setEnabled(connected)
        self.btn_single.setEnabled(connected)
        self.btn_snap.setEnabled(connected and not self._snap_in_flight
                                 and not self._closing and self.pipeline.can_accept())
        self.btn_reload_params.setEnabled(connected)

    def _status(self, msg: str, *, side: bool = False) -> None:
        win = self.window()
        bar = getattr(win, "statusBar", None)
        if bar is None:
            return
        bar = bar()
        if side:
            # 右下角 - 用 permanent widget
            setter = getattr(win, "set_right_status", None)
            if setter is not None:
                setter(msg)
                return
        bar.showMessage(msg)

    def _err(self, msg: str) -> None:
        QMessageBox.warning(self, "错误", msg)

    # ============================== 平场校正 (FFC) ==============================

    def _ffc_persist_path(self) -> Path:
        return _CALIB_DIR / f"flat_field_{self.mode}.npz"

    def _try_load_persisted_ffc(self) -> None:
        path = self._ffc_persist_path()
        if not path.exists():
            return
        try:
            self.correction.load(path)
            log.info("[%s] FFC 自动加载: %s", self.mode, path)
        except Exception:
            log.exception("FFC 加载失败 %s", path)

    def _refresh_ffc_status(self) -> None:
        has_flat = self.correction.has_flat
        has_dark = self.correction.has_dark

        if not has_flat:
            # 没亮场就没法校正,无论有没有暗场
            if has_dark:
                self.lbl_ffc_status.setText("校正状态: 仅有暗场,缺亮场 — 请采集亮场")
            else:
                self.lbl_ffc_status.setText("校正状态: 未采集参考")
            self.lbl_ffc_status.setStyleSheet("color: #888; font-size: 11px;")
            self.cb_ffc_enable.setEnabled(False)
            self.cb_ffc_enable.blockSignals(True)
            self.cb_ffc_enable.setChecked(False)
            self.cb_ffc_enable.blockSignals(False)
            self.btn_clear_flat.setEnabled(has_dark)  # 有暗场也允许清
            return

        self.cb_ffc_enable.setEnabled(True)
        self.btn_clear_flat.setEnabled(True)
        self.cb_ffc_enable.blockSignals(True)
        self.cb_ffc_enable.setChecked(self.correction.enabled)
        self.cb_ffc_enable.blockSignals(False)

        m = self.correction.flat_mean       # mean(F - D)
        d = self.correction.dark_mean       # mean(D), 可能为 None
        shape = self.correction.reference_shape
        state = '启用' if self.correction.enabled else '禁用'
        dark_tag = (
            f"D={d[0]:.0f}/{d[1]:.0f}/{d[2]:.0f}" if d is not None else "D=无"
        )
        if m is not None and shape is not None:
            txt = (
                f"校正状态: {state} | {shape[1]}×{shape[0]} | "
                f"信号(F-D) B={m[0]:.0f} G={m[1]:.0f} R={m[2]:.0f} | {dark_tag}"
            )
        else:
            txt = f"校正状态: {state} | {dark_tag}"
        color = "#f29429" if self.correction.enabled else "#888"
        self.lbl_ffc_status.setText(txt)
        self.lbl_ffc_status.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _capture_dark_field(self) -> None:
        """采集暗场(盖镜头)。会立刻持久化(如果同时有亮场)。"""
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        bgr = self._get_current_frame_for_snap()
        if bgr is None:
            self._err("没有可用画面 — 先点连续采集或单次采集")
            return
        try:
            summary = self.correction.set_dark(bgr)
        except Exception as e:
            self._err(f"采集暗场失败: {e}")
            return

        mean = summary["mean"]   # BGR
        chan_max = summary["max"]
        notes = []
        if float(np.mean(mean)) > 20:
            notes.append("⚠️ 暗场均值 > 20,可能没盖好镜头或环境漏光")
        if (chan_max >= 50).any():
            notes.append("⚠️ 有像素 > 50,确认镜头盖严实")

        # 只有同时存在亮场时才落盘(npz 至少要 flat 字段)
        if self.correction.has_flat:
            try:
                self.correction.save(self._ffc_persist_path())
            except Exception:
                log.exception("FFC 保存失败(暗场更新后)")

        self._refresh_ffc_status()
        msg = (
            f"暗场已采集\n"
            f"分辨率: {summary['shape'][1]}×{summary['shape'][0]}\n"
            f"BGR 均值: {mean.round(2).tolist()}  (越低越好)\n"
            f"BGR 最大值: {chan_max.tolist()}\n"
        )
        if not self.correction.has_flat:
            msg += "\n下一步:盖好白板再点「采集亮场」"
        if notes:
            msg += "\n\n" + "\n".join(notes)
        QMessageBox.information(self, "暗场", msg)

    def _capture_flat_field(self) -> None:
        if not self.camera.is_connected():
            self._err("请先连接相机")
            return
        bgr = self._get_current_frame_for_snap()
        if bgr is None:
            self._err("没有可用画面 — 先点连续采集或单次采集")
            return
        try:
            summary = self.correction.set_flat(bgr)
        except Exception as e:
            self._err(f"采集亮场失败: {e}")
            return

        # summary["mean"] = mean(F-D),原始亮场亮度看 raw_mean
        signal_mean = summary["mean"]
        raw_mean = summary.get("raw_mean", signal_mean)
        chan_max = summary["max"]
        avg = float(np.mean(raw_mean))
        notes = []
        if avg < 80:
            notes.append("⚠️ 整体偏暗,建议加大曝光重新采集")
        elif avg > 235:
            notes.append("⚠️ 接近饱和,角落像素可能被裁掉,建议降低曝光重新采集")
        if (chan_max >= 254).any():
            notes.append("⚠️ 部分像素已饱和(=255),校正在这些位置不可靠")
        if not self.correction.has_dark:
            notes.append("ℹ️ 未采暗场,当前按 D=0 计算 — 短曝光下影响可忽略")

        try:
            self.correction.save(self._ffc_persist_path())
        except Exception:
            log.exception("FFC 保存失败")

        self._refresh_ffc_status()
        msg = (
            f"亮场参考已采集\n"
            f"分辨率: {summary['shape'][1]}×{summary['shape'][0]}\n"
            f"原始 BGR 均值: {raw_mean.round(1).tolist()}\n"
            f"信号(F-D) BGR 均值: {signal_mean.round(1).tolist()}\n"
            f"BGR 最暗角: {summary['min'].tolist()}\n"
            f"BGR 最亮处: {chan_max.tolist()}"
        )
        if notes:
            msg += "\n\n" + "\n".join(notes)
        QMessageBox.information(self, "亮场", msg)

    def _clear_flat_field(self) -> None:
        self.correction.clear()
        try:
            p = self._ffc_persist_path()
            if p.exists():
                p.unlink()
        except Exception:
            log.exception("删除 FFC 文件失败")
        self._refresh_ffc_status()
        self._status("已清除平场参考", side=False)

    def _on_ffc_toggled(self, on: bool) -> None:
        self.correction.set_enabled(on)
        self._refresh_ffc_status()

    # ============================== 麦穗工作流 ==============================

    def _on_workflow_start_requested(self) -> None:
        if self.spike_panel is None or self.spike_panel.workflow.active:
            return
        if self._prefix_source == "manual":
            # 手动模式保持旧行为：启动时去除输入框两端空白。
            self._active_field_tag_code = None
            self.spike_panel.start_with_prefix(self.le_prefix.text().strip())
            return
        self._start_from_field_tag_qr()

    def _start_from_field_tag_qr(self) -> None:
        """以当前预览帧启动一次不可重入的挂牌识别请求。"""
        if self._qr_request_pending or self._shutting_down:
            return
        self._active_field_tag_code = None
        self._qr_request_code = None
        bearer = self.le_bearer_key.text()
        if not bearer:
            self.view.show_warning("请先输入 Bearer 密钥", 5000)
            return
        try:
            bearer_bytes = bearer.encode("ascii")
        except UnicodeEncodeError:
            self.view.show_warning("Bearer 密钥格式无效", 5000)
            return
        if (
            bearer != bearer.strip()
            or any(byte < 33 or byte == 127 for byte in bearer_bytes)
            or b"\r" in bearer_bytes
            or b"\n" in bearer_bytes
        ):
            self.view.show_warning("Bearer 密钥格式无效", 5000)
            return
        frame = self._latest_preview_bgr
        if frame is None or frame.size == 0:
            self.view.show_warning("当前没有可用于识别的预览画面", 5000)
            return

        self.view.clear_qr_outline()
        self._qr_request_id += 1
        request_id = self._qr_request_id
        self._set_qr_request_pending(True)
        # 后台线程只读取这份快照，避免实时取流更新同一引用。
        frame_snapshot = frame.copy()

        def worker() -> None:
            try:
                result = detect_field_tag_qr(frame_snapshot)
            except FieldTagError as exc:
                if not self._shutting_down:
                    self._qr_detection_failed.emit(request_id, str(exc))
            except Exception:
                log.exception("[%s] 二维码识别发生未预期错误", self.mode)
                if not self._shutting_down:
                    self._qr_detection_failed.emit(request_id, "二维码识别失败")
            else:
                if not self._shutting_down:
                    self._qr_detection_done.emit(request_id, result)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"field-tag-qr-{self.mode}-{request_id}",
        ).start()

    def _on_qr_detection_failed(self, request_id: int, message: str) -> None:
        if request_id != self._qr_request_id or not self._qr_request_pending:
            return
        self._set_qr_request_pending(False)
        self.view.show_warning(message, 5000)

    def _on_qr_detection_done(
        self, request_id: int, result: FieldTagDetection
    ) -> None:
        if request_id != self._qr_request_id or not self._qr_request_pending:
            return
        if self._prefix_source != "qr" or self._shutting_down:
            self._invalidate_qr_request()
            return
        self._qr_request_code = result.code
        self.view.show_qr_outline(result.points, 5000)
        self._request_field_tag_api(request_id, result.code)

    def _request_field_tag_api(self, request_id: int, code: str) -> None:
        """发送固定端点请求；Bearer 不进入状态栏、异常文本或日志。"""
        if request_id != self._qr_request_id or not self._qr_request_pending:
            return
        bearer_bytes = self.le_bearer_key.text().encode("ascii")
        request = QNetworkRequest(QUrl(build_field_tag_api_url(code)))
        request.setRawHeader(b"Authorization", b"Bearer " + bearer_bytes)
        request.setRawHeader(b"Accept", b"application/json")
        request.setTransferTimeout(10_000)
        reply = self._network_manager.get(request)
        self._qr_reply = reply
        self._qr_timeout_request_id = request_id
        self._qr_timeout_timer.start(10_000)
        reply.finished.connect(
            lambda rid=request_id, current=reply:
            self._on_field_tag_reply_finished(rid, current)
        )

    def _on_field_tag_request_timeout(self) -> None:
        request_id = self._qr_timeout_request_id
        reply = self._qr_reply
        if (
            request_id is None
            or request_id != self._qr_request_id
            or not self._qr_request_pending
            or reply is None
        ):
            return
        self._qr_reply = None
        # 先让随后到达的 finished 回调过期，再安全中止请求。
        self._qr_request_id += 1
        self._set_qr_request_pending(False)
        try:
            reply.abort()
        except RuntimeError:
            pass
        reply.deleteLater()
        self.view.show_warning("挂牌服务请求超时（10 秒）", 5000)

    @staticmethod
    def _field_tag_http_error(status: int) -> str:
        messages = {
            401: "Bearer 密钥无效或已过期（401）",
            403: "当前 Bearer 密钥没有挂牌查询权限（403）",
            404: "未找到该挂牌编号（404）",
            429: "挂牌服务请求过于频繁，请稍后再试（429）",
        }
        return messages.get(status, f"挂牌服务返回错误（HTTP {status}）")

    @staticmethod
    def _field_tag_network_error(error: QNetworkReply.NetworkError) -> str:
        if error == QNetworkReply.NetworkError.TimeoutError:
            return "挂牌服务请求超时（10 秒）"
        if error == QNetworkReply.NetworkError.SslHandshakeFailedError:
            return "挂牌服务 TLS 安全连接失败"
        return "无法连接挂牌服务，请检查网络后重试"

    def _on_field_tag_reply_finished(
        self, request_id: int, reply: QNetworkReply
    ) -> None:
        if self._qr_reply is reply:
            self._qr_reply = None
        if request_id != self._qr_request_id or not self._qr_request_pending:
            reply.deleteLater()
            return
        self._qr_timeout_timer.stop()
        self._qr_timeout_request_id = None

        status_value = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        status = int(status_value) if status_value is not None else 0
        network_error = reply.error()
        payload = bytes(reply.readAll())
        reply.deleteLater()

        if status != 200:
            self._set_qr_request_pending(False)
            if status:
                message = self._field_tag_http_error(status)
            else:
                message = self._field_tag_network_error(network_error)
            self.view.show_warning(message, 5000)
            return
        if network_error != QNetworkReply.NetworkError.NoError:
            self._set_qr_request_pending(False)
            self.view.show_warning(
                self._field_tag_network_error(network_error), 5000
            )
            return

        try:
            accession = parse_accession_response(payload)
        except FieldTagError as exc:
            self._set_qr_request_pending(False)
            self.view.show_warning(str(exc), 5000)
            return

        if (
            request_id != self._qr_request_id
            or self._prefix_source != "qr"
            or self._shutting_down
        ):
            self._invalidate_qr_request()
            return
        self.le_prefix.setText(accession)
        started = self.spike_panel.start_with_prefix(accession)
        self._active_field_tag_code = self._qr_request_code if started else None
        self._set_qr_request_pending(False)

    def _spike_precheck(self) -> tuple[bool, str]:
        """工作流开始前置检查:保存文件夹必须已设(续拍检测和抓拍都依赖它)。
        没设则弹对话框让用户选;用户取消则返回未就绪。"""
        if self._save_dir is None:
            if not self._prompt_save_dir():
                return False, "未设置保存文件夹,已取消开始"
        return True, ""

    def _spike_file_exists(self, rep: int, side: str) -> bool:
        """工作流面板用:对当前 prefix+save_dir+ext,某 (rep, side) 文件是否存在。
        麦穗 side 非空 → prefix_Rep{N}_{side};籽粒 side 空 → prefix_Rep{N}。"""
        if self._save_dir is None or self.spike_panel is None:
            return False
        prefix = self.spike_panel.workflow.prefix
        if not prefix:
            prefix = self.le_prefix.text().strip()
        if not prefix:
            return False
        ext = self.cb_format.currentText().lower()
        suffix = f"_{side}" if side else ""
        candidate = self._save_dir / f"{prefix}_Rep{rep}{suffix}.{ext}"
        return candidate.exists()

    def _on_workflow_state_changed(self, active: bool) -> None:
        """工作流启用/禁用时,锁定/解锁普通的文件名设置控件。"""
        if not active:
            self._active_field_tag_code = None
        self._update_prefix_source_controls()
        if active:
            self._sync_gallery_context()
        if self.sp_repeat is not None:
            self.sp_repeat.setEnabled(not active)
        if self.cb_side is not None:
            self.cb_side.setEnabled(not active)

    def _on_workflow_completed(self) -> None:
        """全部 PASSED 后由 SpikePanel 发出 — 这里只做提示。"""
        self.gallery.mark_complete()
        self._status("✓ 当前品种已全部保存,1.5s 后看板自动重置", side=True)

    def _clear_prefix_after_workflow(self) -> None:
        """整轮工作流完成自动重置时清空品种前缀,防止下一品种忘改覆盖照片。"""
        self._active_field_tag_code = None
        self.le_prefix.clear()
        if self._prefix_source == "manual":
            self.le_prefix.setFocus()
            message = "已清空品种前缀 — 请输入下一品种再开始"
        else:
            message = "已清空品种前缀 — 请识别下一张挂牌二维码"
        self._status(message, side=True)

    # ============================== 关闭 ==============================

    def shutdown(self) -> None:
        """关闭程序前调用,确保线程和设备资源被释放。"""
        if self.pipeline.owner_pending(self._owner):
            return False
        self._shutting_down = True
        self._capacity_timer.stop()
        self.gallery.shutdown()
        if self._owns_pipeline:
            self.pipeline.shutdown()
        self._latest_preview_bgr = None
        self._clear_metadata_camera_cache()
        self._invalidate_qr_request()
        try:
            if self._stream is not None:
                self._stop_stream()
            if self.camera.is_connected():
                self.camera.disconnect()
        except Exception:
            log.exception("shutdown error")
