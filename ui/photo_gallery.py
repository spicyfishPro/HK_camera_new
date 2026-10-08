"""底部当前材料照片带。目录与图像读取在低优先级线程进行。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import (
    Qt,
    QSize,
    QRect,
    QAbstractListModel,
    QModelIndex,
    QTimer,
    QObject,
    QThread,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QImage, QPixmap, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListView,
    QDialog,
    QComboBox,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QStyledItemDelegate,
    QStyle,
)

from core.capture_pipeline import CaptureEvent, preview
from core.photo_catalog import scan_material, ThumbnailCache

log = logging.getLogger(__name__)
STATES = {
    "missing": "未拍",
    "history": "已有照片",
    "preparing": "取帧 / 评分中",
    "quality": "待保存",
    "queued": "等待保存",
    "saving": "保存中",
    "saved": "已保存",
    "failed": "保存失败",
    "capture_failed": "抓拍失败",
}


def qimage(bgr):
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()


@dataclass
class PhotoEntry:
    key: str
    path: Path | None
    rep: int | None
    side: str | None
    state: str = "history"
    raw_path: Path | None = None
    score: float | None = None
    passed: bool = True
    message: str = ""
    modified: float = 0.0

    @property
    def label(self):
        rep = f"Rep{self.rep}" if self.rep else "未指定"
        return rep + {"front": " · 正面", "side": " · 侧面"}.get(self.side, "")

    @property
    def quality(self):
        if self.score is None:
            return (
                "质量未知"
                if self.state == "history"
                else "无评分"
                if self.state in ("saved", "queued", "saving")
                else ""
            )
        return f"{'质量合格' if self.passed else '质量不合格'} {self.score:.1f}"


class _Loader(QObject):
    finished = pyqtSignal(object)

    def __init__(self, pipeline):
        super().__init__()
        self.pipeline = pipeline

    def run(self, request):
        generation, token, kind, payload = request
        started = time.perf_counter()
        try:
            if kind == "scan":
                result = scan_material(*payload)
            else:
                key, path, raw, side = payload
                result = (
                    self.pipeline.image_preview(key, raw, side)
                    if self.pipeline
                    else None
                )
                if result is None:
                    if path is None:
                        raise RuntimeError("照片尚未生成预览")
                    buf = np.fromfile(str(path), np.uint8)
                    result = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                    if result is None:
                        raise RuntimeError("无法读取图片，文件可能损坏")
                    if side:
                        result = preview(result, side)
                result = qimage(result)
            error = ""
        except Exception as exc:
            result, error = None, str(exc)
        log.debug(
            "gallery load kind=%s elapsed=%.3fs", kind, time.perf_counter() - started
        )
        self.finished.emit((generation, token, kind, result, error))


class PhotoListModel(QAbstractListModel):
    def __init__(self, gallery):
        super().__init__(gallery)
        self.gallery = gallery
        self.entries: list[PhotoEntry] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self.entries):
            return None
        e = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            count = len(self.gallery.variants(e))
            return f"{e.label}{'  +' + str(count - 1) if count > 1 else ''}\n{STATES[e.state]}\n{e.quality}"
        if role == Qt.ItemDataRole.DecorationRole:
            image = self.gallery.cache.get(e.key)
            if image is not None:
                return QPixmap.fromImage(image).scaled(
                    108,
                    86,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self.gallery.request_thumbnail(e)
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(
                filter(
                    None,
                    [
                        e.label,
                        STATES[e.state],
                        e.quality,
                        str(e.path) if e.path else "",
                        e.message,
                    ],
                )
            )
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(
                "#ef5350"
                if e.state in ("failed", "capture_failed") or not e.passed
                else "#4caf50"
                if e.state in ("saved", "history")
                else "#67b3eb"
            )
        if (
            role == Qt.ItemDataRole.BackgroundRole
            and (e.rep, e.side or "") == self.gallery.current
        ):
            return QColor("#533c23")
        return None

    def replace_entries(self, entries):
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()


class PhotoDelegate(QStyledItemDelegate):
    """固定尺寸，异步图片到达后也不改变文字/图片区域的布局。"""

    def sizeHint(self, option, index):
        return QSize(148, 132)

    def paint(self, painter, option, index):
        gallery = index.model().gallery
        entry = index.model().entries[index.row()]
        rect = option.rect.adjusted(2, 2, -2, -2)
        painter.save()
        painter.fillRect(rect, QColor("#242424" if (entry.rep or 0) % 2 else "#303030"))
        current = (entry.rep, entry.side or "") == gallery.current
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(
            QColor("#f29429" if current else "#d8d8d8" if selected else "#444444")
        )
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        image = gallery.cache.get(entry.key)
        image_box = QRect(rect.x() + 6, rect.y() + 4, rect.width() - 12, 70)
        if image is not None:
            fitted = image.size().scaled(
                image_box.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRect(0, 0, fitted.width(), fitted.height())
            target.moveCenter(image_box.center())
            painter.drawImage(target, image)
        else:
            painter.setPen(QColor("#888888"))
            painter.drawText(
                image_box,
                Qt.AlignmentFlag.AlignCenter,
                "未拍" if entry.path is None else "预览加载中",
            )
            gallery.request_thumbnail(entry)
        count = len(gallery.variants(entry))
        texts = (
            entry.label + (f"  +{count - 1}" if count > 1 else ""),
            STATES[entry.state],
            entry.quality,
        )
        for y, text, color in zip(
            (78, 94, 110),
            texts,
            (
                "#f5b56d" if current else "#d8d8d8",
                "#ef5350"
                if entry.state in ("failed", "capture_failed")
                else "#4caf50"
                if entry.state in ("saved", "history")
                else "#67b3eb",
                "#ef5350" if not entry.passed else "#b8b8b8",
            ),
        ):
            painter.setPen(QColor(color))
            painter.drawText(
                QRect(rect.x() + 4, rect.y() + y, rect.width() - 8, 16),
                Qt.AlignmentFlag.AlignCenter,
                text,
            )
        painter.restore()


class PhotoViewer(QDialog):
    def __init__(self, gallery):
        super().__init__(gallery, Qt.WindowType.Window)
        self.gallery = gallery
        self.setWindowTitle("照片预览")
        self.resize(860, 760)
        self.entry = None
        self.token = 0
        self._original = False
        root = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setWordWrap(True)
        root.addWidget(self.title)
        row = QHBoxLayout()
        self.previous = QPushButton("上一张")
        self.next = QPushButton("下一张")
        self.versions = QComboBox()
        self.variant = QComboBox()
        self.variant.addItems(["主图", "原图 (_raw)"])
        self.fit = QPushButton("适应窗口")
        self.actual = QPushButton("原图 1:1")
        self.plus = QPushButton("放大")
        self.minus = QPushButton("缩小")
        for w in (
            self.previous,
            self.next,
            self.versions,
            self.variant,
            self.fit,
            self.actual,
            self.plus,
            self.minus,
        ):
            row.addWidget(w)
        root.addLayout(row)
        self.scene = QGraphicsScene(self)
        self.item = QGraphicsPixmapItem()
        self.scene.addItem(self.item)
        self.view = QGraphicsView(self.scene)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        root.addWidget(self.view)
        self.previous.clicked.connect(lambda: self.navigate(-1))
        self.next.clicked.connect(lambda: self.navigate(1))
        self.variant.currentIndexChanged.connect(lambda: self.load(False))
        self.versions.currentIndexChanged.connect(self.choose_version)
        self.fit.clicked.connect(self.fit_image)
        self.actual.clicked.connect(lambda: self.load(True))
        self.plus.clicked.connect(lambda: self.view.scale(1.25, 1.25))
        self.minus.clicked.connect(lambda: self.view.scale(0.8, 0.8))
        # 子窗口截获空格，绝不把浏览动作路由到采集快捷键。
        self.space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.space.activated.connect(lambda: None)

    def open_entry(self, entry):
        self.entry = entry
        self.versions.blockSignals(True)
        self.versions.clear()
        for version in self.gallery.variants(entry):
            self.versions.addItem(
                version.path.name if version.path else version.label, version.key
            )
        self.versions.setCurrentIndex(max(0, self.versions.findData(entry.key)))
        self.versions.blockSignals(False)
        self.variant.blockSignals(True)
        self.variant.setCurrentIndex(0)
        self.variant.setEnabled(entry.raw_path is not None)
        self.variant.blockSignals(False)
        self.load(False)
        self.show()
        self.raise_()

    def choose_version(self, index):
        key = self.versions.itemData(index)
        if key in self.gallery.entries:
            self.entry = self.gallery.entries[key]
            self.variant.setEnabled(self.entry.raw_path is not None)
            if self.entry.raw_path is None:
                self.variant.setCurrentIndex(0)
            self.load(False)

    def navigate(self, offset):
        entries = [e for e in self.gallery.model.entries if e.path is not None]
        if not entries or self.entry is None:
            return
        current = next(
            (
                i
                for i, e in enumerate(entries)
                if (e.rep, e.side) == (self.entry.rep, self.entry.side)
            ),
            0,
        )
        self.open_entry(entries[(current + offset) % len(entries)])

    def load(self, original=False):
        e = self.entry
        if e is None:
            return
        self.token += 1
        self._original = original
        raw = bool(self.variant.currentIndex())
        path = e.raw_path if raw else e.path
        saved = e.state in ("saved", "history")
        self.actual.setEnabled(saved)
        self.title.setText(
            f"{self.gallery.prefix} / {e.label} / {STATES[e.state]} / {e.quality}\n"
            f"{path.name if path else ''} — 正在加载"
        )
        thumb = self.gallery.cache.get(e.key + (":raw" if raw else ""))
        if thumb is not None:
            self.show_image(thumb)
        self.gallery.request_load(
            (
                self.gallery.generation,
                self.token,
                "viewer",
                (e.key, path, raw, 0 if original and saved else 2048),
            ),
            priority=True,
        )

    def show_image(self, image):
        self.item.setPixmap(QPixmap.fromImage(image))
        self.scene.setSceneRect(self.item.boundingRect())
        if self._original:
            self.view.resetTransform()
        else:
            self.fit_image()

    def fit_image(self):
        self.view.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

    def closeEvent(self, event):
        self.token += 1
        self.item.setPixmap(QPixmap())
        super().closeEvent(event)


class PhotoGallery(QWidget):
    load_requested = pyqtSignal(object)
    retry_requested = pyqtSignal(str)

    def __init__(self, pipeline, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self.prefix = ""
        self.folder = None
        self.mode = "wheat"
        self.ext = "png"
        self.total = 0
        self.generation = 0
        self.current = (None, "")
        self.entries: dict[str, PhotoEntry] = {}
        self.cache = ThumbnailCache()
        self._requested = set()
        self._load_queue = []
        self._loading = False
        self._closed = False
        self._follow = True
        self._complete = False
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 0, 4, 0)
        row = QHBoxLayout()
        self.header = QLabel("当前材料照片 — 开始采集后显示")
        row.addWidget(self.header, 1)
        self.locate = QPushButton("定位当前")
        self.refresh = QPushButton("刷新")
        self.retry = QPushButton("重试保存")
        self.retry.setEnabled(False)
        self.collapse = QPushButton("折叠")
        for w in (self.locate, self.refresh, self.retry, self.collapse):
            row.addWidget(w)
        root.addLayout(row)
        self.list = QListView()
        self.model = PhotoListModel(self)
        self.list.setModel(self.model)
        self.list.setItemDelegate(PhotoDelegate(self.list))
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setFlow(QListView.Flow.LeftToRight)
        self.list.setWrapping(False)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setIconSize(QSize(108, 86))
        self.list.setGridSize(QSize(148, 132))
        self.list.setUniformItemSizes(True)
        self.list.setMinimumHeight(150)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.list)
        self.viewer = PhotoViewer(self)
        self.list.clicked.connect(self._clicked)
        self.list.horizontalScrollBar().sliderPressed.connect(self._stop_follow)
        self.list.horizontalScrollBar().actionTriggered.connect(
            lambda _: self._stop_follow()
        )
        self.locate.clicked.connect(self.locate_current)
        self.refresh.clicked.connect(self.scan)
        self.retry.clicked.connect(self.retry_selected)
        self.collapse.clicked.connect(self.toggle_collapsed)
        self._thread = QThread(self)
        self._loader = _Loader(pipeline)
        self._loader.moveToThread(self._thread)
        self.load_requested.connect(self._loader.run)
        self._loader.finished.connect(self._loaded)
        self._thread.finished.connect(self._loader.deleteLater)
        self._thread.start(QThread.Priority.LowPriority)

    def _stop_follow(self):
        self._follow = False

    def toggle_collapsed(self):
        collapsed = not self.list.isHidden()
        self.list.setVisible(not collapsed)
        self.collapse.setText("展开" if collapsed else "折叠")
        self.setMaximumHeight(
            16777215 if not collapsed else self.header.sizeHint().height() + 18
        )

    def set_context(self, folder, prefix, mode, total=0, ext="png"):
        if not prefix or folder is None:
            return  # 完成后的自动清空前缀保留刚拍完的照片。
        context = (Path(folder), prefix, mode)
        if (
            context == (self.folder, self.prefix, self.mode)
            and total == self.total
            and ext.lower() == self.ext
        ):
            return
        if context != (self.folder, self.prefix, self.mode):
            self.generation += 1
            self.entries.clear()
            self.cache.clear()
            self._requested.clear()
            self._load_queue.clear()
            self.viewer.hide()
            self.viewer.token += 1
            self.viewer.item.setPixmap(QPixmap())
            self._follow = True
            self._complete = False
            self.folder, self.prefix, self.mode = context
        self.total, self.ext = total, ext.lower()
        self.rebuild()
        self.scan()

    def mark_complete(self):
        self._complete = True
        self.rebuild()

    def scan(self):
        if self.folder and self.prefix and not self._closed:
            self.request_load(
                (self.generation, "scan", "scan", (self.folder, self.prefix))
            )

    def request_load(self, request, priority=False):
        if self._closed:
            return
        if request not in self._load_queue:
            if priority:
                self._load_queue.insert(0, request)
            else:
                self._load_queue.append(request)
        self._dispatch()

    def _dispatch(self):
        if not self._loading and self._load_queue and not self._closed:
            request = self._load_queue.pop(0)
            self._loading = True
            self.load_requested.emit(request)

    def variants(self, entry):
        found = [
            e
            for e in self.entries.values()
            if (e.rep, e.side or "") == (entry.rep, entry.side or "")
        ]
        return sorted(
            found,
            key=lambda e: (
                e.state != "history",
                e.path is not None and e.path.suffix.lower() == "." + self.ext,
                e.modified,
            ),
            reverse=True,
        ) or [entry]

    def rebuild(self):
        groups = {}
        for e in self.entries.values():
            groups.setdefault((e.rep, e.side or ""), e)
        sides = ("front", "side") if self.mode == "wheat" else ("",)
        for rep in range(1, self.total + 1):
            for side in sides:
                groups.setdefault(
                    (rep, side),
                    PhotoEntry(f"missing:{rep}:{side}", None, rep, side, "missing"),
                )
        entries = [
            self.variants(e)[0]
            for _, e in sorted(
                groups.items(),
                key=lambda x: (x[0][0] or 10**9, x[0][1] == "side", x[0][1]),
            )
        ]
        position = self.list.horizontalScrollBar().value()
        self.model.replace_entries(entries)
        if not self._follow:
            self.list.horizontalScrollBar().setValue(position)
        counts = {s: sum(e.state == s for e in entries) for s in STATES}
        pending = sum(
            e.state in ("preparing", "quality", "queued", "saving")
            for e in self.entries.values()
        )
        failed = sum(
            e.state in ("failed", "capture_failed") for e in self.entries.values()
        )
        self.header.setText(
            f"{'已完成材料' if self._complete else '材料'}：{self.prefix or '—'}    "
            f"已保存 {counts['saved'] + counts['history']}    待保存 {pending}    失败 {failed}"
        )
        self.retry.setEnabled(any(e.state == "failed" for e in self.entries.values()))

    def update_event(self, event: CaptureEvent):
        if (
            self.folder != event.path.parent
            or self.prefix != event.metadata.accession_no
        ):
            return
        # 同路径历史项/旧重拍项不再重复计数，当前尝试拥有该位置。
        for key, e in list(self.entries.items()):
            if e.path == event.path and key != event.id:
                if e.state == "history":
                    del self.entries[key]
        existing = self.entries.get(event.id)
        modified = existing.modified if existing else time.time()
        for key, entry in list(self.entries.items()):
            if (
                entry.path == event.path
                and key != event.id
                and entry.modified <= modified
                and entry.state in ("saved", "capture_failed")
            ):
                del self.entries[key]
                if self.viewer.entry and self.viewer.entry.key == key:
                    self.viewer.close()
        self.entries[event.id] = PhotoEntry(
            event.id,
            event.path,
            event.metadata.rep,
            event.metadata.side,
            event.state,
            event.raw_path,
            event.score,
            event.passed,
            event.message,
            modified,
        )
        for key, arr in (
            (event.id, event.thumbnail),
            (event.id + ":raw", event.raw_thumbnail),
        ):
            if arr is not None:
                image = qimage(arr)
                self.cache.put(key, image, image.sizeInBytes())
        self.rebuild()
        if self._follow:
            for row, e in enumerate(self.model.entries):
                if e.key == event.id:
                    self.list.scrollTo(
                        self.model.index(row), QListView.ScrollHint.EnsureVisible
                    )
                    break
        if (
            self.viewer.isVisible()
            and self.viewer.entry
            and self.viewer.entry.key == event.id
        ):
            self.viewer.entry = self.entries[event.id]
            self.viewer.actual.setEnabled(event.state == "saved")
            self.viewer.title.setText(
                f"{self.prefix} / {self.viewer.entry.label} / {STATES[event.state]} / "
                f"{self.viewer.entry.quality}\n{event.path.name}"
            )

    def request_thumbnail(self, entry):
        if entry.path is None or entry.key in self._requested or self._closed:
            return
        self._requested.add(entry.key)
        generation = self.generation

        def when_laid_out():
            if generation != self.generation or self._closed:
                return
            row = next(
                (i for i, e in enumerate(self.model.entries) if e.key == entry.key), -1
            )
            if row < 0 or not self.list.visualRect(self.model.index(row)).intersects(
                self.list.viewport().rect()
            ):
                self._requested.discard(entry.key)
                return
            self.request_load(
                (generation, entry.key, "thumb", (entry.key, entry.path, False, 320))
            )

        QTimer.singleShot(0, when_laid_out)

    def _loaded(self, reply):
        generation, token, kind, result, error = reply
        self._loading = False
        if generation == self.generation and not self._closed:
            if kind == "scan":
                if error:
                    self.header.setToolTip(error)
                else:
                    live_paths = {
                        e.path for e in self.entries.values() if e.state != "history"
                    }
                    self.entries = {
                        k: e for k, e in self.entries.items() if e.state != "history"
                    }
                    for p in result:
                        if p.path not in live_paths:
                            key = f"{p.path}@{p.modified}"
                            self.entries[key] = PhotoEntry(
                                key,
                                p.path,
                                p.rep,
                                p.side,
                                "history",
                                p.raw_path,
                                modified=p.modified,
                            )
                    self.rebuild()
            elif kind == "thumb":
                if result is not None:
                    self.cache.put(token, result, result.sizeInBytes())
                    self._requested.discard(token)
                elif token in self.entries:
                    self.entries[token].message = error
                if self.model.entries:
                    self.model.dataChanged.emit(
                        self.model.index(0),
                        self.model.index(len(self.model.entries) - 1),
                    )
            elif kind == "viewer" and token == self.viewer.token:
                if result is not None:
                    self.viewer.show_image(result)
                e = self.viewer.entry
                if e:
                    self.viewer.title.setText(
                        f"{self.prefix} / {e.label} / {STATES[e.state]} / {e.quality}\n"
                        f"{e.path.name if e.path else ''} — {error or ('原图 1:1' if self.viewer._original else '预览')}"
                    )
        self._dispatch()

    def _clicked(self, index):
        e = self.model.entries[index.row()]
        if e.path:
            self.viewer.open_entry(e)

    def retry_selected(self):
        index = self.list.currentIndex()
        e = self.model.entries[index.row()] if index.isValid() else None
        if e is None or e.state != "failed":
            e = next((e for e in self.entries.values() if e.state == "failed"), None)
        if e:
            self.retry_requested.emit(e.key)

    def locate_current(self):
        self._follow = True
        for i, e in enumerate(self.model.entries):
            if (e.rep, e.side or "") == self.current:
                self.list.scrollTo(self.model.index(i))
                return
        if self.model.entries:
            self.list.scrollTo(self.model.index(len(self.model.entries) - 1))

    def shutdown(self):
        self._closed = True
        self._load_queue.clear()
        self.viewer.close()
        self._thread.quit()
        # 读盘最多只允许一个在途请求；窗口关闭期间保留线程对象直到结束。
        self._thread.wait()
