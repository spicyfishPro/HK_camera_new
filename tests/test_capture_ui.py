from __future__ import annotations

import tempfile
import time
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import cv2

# 同现有 UI 测试使用相机替身，不加载本机 MVS DLL。
from test_qr_ui import QT_AVAILABLE, QApplication, CaptureWidget, _FakeScorer
from test_acquisition_metadata import sample_metadata, sample_frame
from core.acquisition_metadata import FrameSnapshot, SaveResult
from core.capture_pipeline import CaptureEvent


def pump(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


@unittest.skipUnless(QT_AVAILABLE, "PyQt6 unavailable")
class CaptureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        with patch("ui.capture_widget.BRISQUEScorer", return_value=_FakeScorer()):
            self.widget = CaptureWidget("wheat")
        self.widget._append_capture_log = Mock()
        self.widget._save_dir = self.folder
        self.widget.camera.is_connected = lambda: True
        self.widget.camera.device = None
        self.widget._err = Mock()
        self.widget.le_prefix.setText("A")
        self.widget.spike_panel.workflow.start("A", 1)
        self.widget.spike_panel._rebuild_grid()
        self.widget._sync_gallery_context()
        self.widget.resize(1100, 700)
        self.widget.show()
        self.events = []
        self.widget._pipeline_event.connect(self.events.append)
        self.gate = threading.Event()

    def tearDown(self):
        self.gate.set()
        pump(lambda: self.widget.pipeline.pending_count == 0)
        self.widget._stream = None
        self.widget.shutdown()
        self.widget.close()
        self.temp.cleanup()

    def test_end_to_end_two_views_while_encoder_blocked(self):
        owner = self

        class Stream:
            def __init__(self):
                self.n = 0
                self.latest_frame = FrameSnapshot(
                    np.zeros((8, 12, 3), np.uint8), sample_frame()
                )

            def set_saving(self, value):
                pass

            def fresh_snapshot(self, request_id):
                self.n += 1
                return FrameSnapshot(
                    np.full((8, 12, 3), self.n, np.uint8),
                    replace(sample_frame(), frame_number=self.n),
                )

        self.widget._stream = Stream()
        saved = []

        def save(image, path, meta, **kw):
            owner.gate.wait(5)
            saved.append((path.name, int(image[0, 0, 0]), meta.frame.frame_number))
            return SaveResult(path, True, "XMP")

        self.widget.pipeline._saver = save
        self.widget.snap()
        self.widget._err.assert_not_called()
        self.assertTrue(pump(lambda: not self.widget._snap_in_flight))
        self.assertEqual(self.widget.spike_panel.workflow.current_step().side, "side")
        self.assertTrue(self.widget.btn_snap.isEnabled())
        self.widget.snap()
        self.assertTrue(pump(lambda: not self.widget._snap_in_flight))
        self.assertEqual(saved, [])
        self.assertEqual(self.widget.pipeline.pending_count, 2)
        entries = self.widget.gallery.model.entries
        self.assertEqual(
            [(e.rep, e.side) for e in entries], [(1, "front"), (1, "side")]
        )
        self.assertTrue(all(e.state in ("queued", "saving") for e in entries))
        self.assertTrue(
            all(self.widget.gallery.cache.get(e.key) is not None for e in entries)
        )
        self.widget.gallery.viewer.open_entry(entries[0])
        self.assertTrue(
            pump(lambda: not self.widget.gallery.viewer.item.pixmap().isNull())
        )
        self.assertFalse(self.widget.gallery.viewer.actual.isEnabled())
        self.gate.set()
        self.assertTrue(pump(lambda: self.widget.pipeline.pending_count == 0))
        self.assertTrue(pump(lambda: self.widget.spike_panel.workflow.is_complete()))
        self.assertCountEqual(
            saved, [("A_Rep1_front.png", 1, 1), ("A_Rep1_side.png", 2, 2)]
        )
        self.assertEqual(self.widget._append_capture_log.call_count, 2)

    def test_history_context_and_corrupt_file(self):
        (self.folder / "A_Rep1_front.png").write_bytes(b"bad png")
        cv2.imwrite(
            str(self.folder / "A_Rep1_side.png"), np.ones((20, 30, 3), np.uint8)
        )
        cv2.imwrite(
            str(self.folder / "A1_Rep1_front.png"), np.ones((20, 30, 3), np.uint8)
        )
        g = self.widget.gallery
        g.scan()
        self.assertTrue(
            pump(lambda: sum(e.state == "history" for e in g.entries.values()) == 2)
        )
        self.assertTrue(all(e.score is None for e in g.entries.values()))
        old_generation = g.generation
        g.set_context(self.folder, "B", "wheat", 1)
        g._loaded((old_generation, "scan", "scan", [], ""))
        self.assertEqual(g.prefix, "B")
        self.assertFalse(g.entries)
        g.mark_complete()
        g.set_context(self.folder, "", "wheat")
        self.assertEqual(g.prefix, "B")
        self.assertTrue(g._complete)

    def test_retake_old_failure_remains_retryable_and_latest_is_primary(self):
        g = self.widget.gallery
        meta = replace(sample_metadata(), accession_no="A", capture_id="old")

        def event(id, state):
            return CaptureEvent(
                id,
                self.widget._owner,
                "s",
                1,
                replace(meta, capture_id=id),
                self.folder / "A_Rep1_front.png",
                None,
                state,
            )

        with patch("ui.photo_gallery.time.time", return_value=1.0):
            g.update_event(event("old", "preparing"))
            g.update_event(event("new", "preparing"))
            g.update_event(event("old", "failed"))
        self.assertEqual(g.model.entries[0].key, "new")
        self.assertTrue(g.retry.isEnabled())
        retried = []
        g.retry_requested.connect(retried.append)
        g.retry_selected()
        self.assertEqual(retried, ["old"])
        self.assertEqual(len(g.variants(g.model.entries[0])), 2)

    def test_normal_close_waits_asynchronously_for_pending_images(self):
        from test_qr_ui import MainWindow
        from test_capture_pipeline import job
        from PyQt6.QtCore import QTimer

        with patch("ui.capture_widget.BRISQUEScorer", return_value=_FakeScorer()):
            window = MainWindow()
        try:

            def slow_save(image, path, metadata, **kw):
                self.gate.wait(4)
                return SaveResult(path, True, "XMP")

            window.pipeline._saver = slow_save
            j = replace(job(self.folder, 7), owner=window.wheat_tab._owner)
            window.pipeline.submit(j)
            window.show()
            window.close()
            self.assertTrue(window.isVisible())
            self.assertFalse(window.pipeline.can_accept())
            tick = []
            QTimer.singleShot(0, lambda: tick.append(True))
            self.assertTrue(pump(lambda: bool(tick)))
            self.gate.set()
            self.assertTrue(pump(lambda: not window.isVisible()))
            self.assertEqual(window.pipeline.pending_count, 0)
        finally:
            self.gate.set()
            pump(lambda: window.pipeline.pending_count == 0)
            window.close()

    def test_thumbnail_labels_fit_small_window_and_collapse(self):
        from PyQt6.QtWidgets import QScrollArea
        self.widget.lbl_save_dir.setText(
            r"C:\Users\RUNNER~1\AppData\Local\Temp\capture-ui-long-directory-name"
        )
        self.widget.resize(1000, 600)
        self.assertTrue(pump(lambda: self.widget.gallery.height() >= 180))
        self.assertTrue(pump(lambda: all(
            area.widget().width() <= area.viewport().width()
            for area in self.widget.findChildren(QScrollArea)
        )))
        view = self.widget.view
        self.widget._show_preview_frame(np.zeros((800, 1000, 3), np.uint8))
        view.fit_to_window()
        QApplication.processEvents()
        self.assertTrue(view.parentWidget().rect().contains(view.geometry()))
        visible = view.mapToScene(view.viewport().rect()).boundingRect()
        self.assertTrue(visible.contains(view._pixmap_item.boundingRect()))
        g = self.widget.gallery
        g.toggle_collapsed()
        QApplication.processEvents()
        self.assertTrue(g.list.isHidden())
        self.assertLess(g.height(), 80)
        g.toggle_collapsed()
        QApplication.processEvents()
        self.assertFalse(g.list.isHidden())
        self.assertGreaterEqual(g.list.height(), 150)
