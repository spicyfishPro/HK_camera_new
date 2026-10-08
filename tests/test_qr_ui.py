from __future__ import annotations

import os
import sys
import types
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from core.capture_pipeline import CaptureEvent
from core.spike_workflow import StepStatus
from test_acquisition_metadata import sample_metadata

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtNetwork import QNetworkReply
    from PyQt6.QtWidgets import QApplication, QLineEdit

    # 界面测试不依赖物理相机或平台专用 MVS 动态库。
    camera_module = types.ModuleType("core.camera")

    class CameraError(RuntimeError):
        pass

    @dataclass
    class DeviceInfo:
        index: int = 0
        model: str = ""
        serial: str = ""
        transport: str = ""
        manufacturer: str = ""
        ip: str = ""

    class HikCamera:
        def is_connected(self):
            return False

        def disconnect(self):
            return None

    camera_module.CameraError = CameraError
    camera_module.DeviceInfo = DeviceInfo
    camera_module.HikCamera = HikCamera
    sys.modules["core.camera"] = camera_module

    from ui.capture_widget import CaptureWidget
    from ui.main_window import MainWindow
except (ImportError, OSError):
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True


class _FakeScorer:
    def compute(self, _image):
        return 0.0

    def prepare_ndarray(self, image):
        return image.copy()

    def score_prepared(self, _image):
        return 0.0


class _FakeReply:
    def __init__(self, status, payload=b"", error=None):
        self.status = status
        self.payload = payload
        self.network_error = (
            QNetworkReply.NetworkError.NoError if error is None else error
        )
        self.deleted = False
        self.aborted = False

    def attribute(self, _attribute):
        return self.status

    def error(self):
        return self.network_error

    def readAll(self):
        return self.payload

    def deleteLater(self):
        self.deleted = True

    def abort(self):
        self.aborted = True


@unittest.skipUnless(QT_AVAILABLE, "当前 Python 环境没有可用的 PyQt6")
class QrUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.scorer_patch = patch(
            "ui.capture_widget.BRISQUEScorer", return_value=_FakeScorer()
        )
        cls.scorer_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.scorer_patch.stop()

    def setUp(self):
        self.widget = CaptureWidget("wheat")

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close()
        self.app.processEvents()

    def _select_qr_mode(self):
        self.widget.btn_prefix_qr.click()
        self.app.processEvents()

    def test_default_manual_and_qr_mode_locks_prefix(self):
        self.assertTrue(self.widget.btn_prefix_manual.isChecked())
        self.assertTrue(self.widget.le_prefix.isEnabled())
        self.assertTrue(self.widget.le_bearer_key.isHidden())
        self._select_qr_mode()
        self.assertFalse(self.widget.le_prefix.isEnabled())
        self.assertFalse(self.widget.le_bearer_key.isHidden())
        self.assertEqual(
            self.widget.le_bearer_key.echoMode(), QLineEdit.EchoMode.Password
        )

    def test_only_metadata_capable_formats_are_available(self):
        formats = [
            self.widget.cb_format.itemText(i)
            for i in range(self.widget.cb_format.count())
        ]
        self.assertEqual(formats, ["PNG", "JPG", "TIFF"])

    def test_rotation_defaults_to_180_degrees(self):
        self.assertEqual(self.widget.cb_rotate.currentData(), 180)
        self.assertEqual(self.widget._rotation_deg, 180)

    def test_manual_mode_starts_with_existing_trimmed_prefix_behavior(self):
        start = Mock(return_value=True)
        self.widget.spike_panel.start_with_prefix = start
        self.widget.le_prefix.setText("  SX001  ")
        self.widget._on_workflow_start_requested()
        start.assert_called_once_with("SX001")

    def test_empty_key_and_missing_preview_do_not_start_detection(self):
        self._select_qr_mode()
        with patch("ui.capture_widget.threading.Thread") as thread:
            self.widget._on_workflow_start_requested()
            thread.assert_not_called()
            self.widget.le_bearer_key.setText("wft_test")
            self.widget._on_workflow_start_requested()
            thread.assert_not_called()
        self.assertFalse(self.widget._qr_request_pending)

    def test_repeated_start_is_ignored_while_pending(self):
        self._select_qr_mode()
        self.widget.le_bearer_key.setText("wft_test")
        self.widget._latest_preview_bgr = np.zeros((20, 20, 3), dtype=np.uint8)
        with patch("ui.capture_widget.threading.Thread") as thread:
            thread.return_value.start = Mock()
            self.widget._on_workflow_start_requested()
            self.widget._on_workflow_start_requested()
            self.assertEqual(thread.call_count, 1)
        self.assertTrue(self.widget._qr_request_pending)

    def test_success_response_fills_original_accession_and_starts(self):
        self._select_qr_mode()
        start = Mock(return_value=True)
        self.widget.spike_panel.start_with_prefix = start
        self.widget._qr_request_id = 5
        self.widget._qr_request_code = "PABC123"
        self.widget._set_qr_request_pending(True)
        reply = _FakeReply(
            200, b'{"material": {"accession_no": "CK"}}'
        )
        self.widget._on_field_tag_reply_finished(5, reply)
        self.assertEqual(self.widget.le_prefix.text(), "CK")
        start.assert_called_once_with("CK")
        self.assertFalse(self.widget._qr_request_pending)
        self.assertTrue(reply.deleted)
        self.assertEqual(self.widget._active_field_tag_code, "PABC123")

    def _event(self, state, **kwargs):
        wf = self.widget.spike_panel.workflow
        meta = replace(sample_metadata(), accession_no='A', capture_id='capture-a')
        return CaptureEvent('capture-a', self.widget._owner, wf.session_id, 1, meta,
                            Path('sample.png'), None, state, **kwargs)

    def test_metadata_warning_is_shown_without_turning_save_into_failure(self):
        self.widget.view.show_warning = Mock()
        self.widget._on_pipeline_event(self._event(
            'saved', message='照片已保存，但未写入采集元数据'))
        self.widget.view.show_warning.assert_called_once_with(
            '照片已保存，但未写入采集元数据', 5000)
        self.assertFalse(self.widget._snap_in_flight)

    def test_quality_advances_before_save_but_not_green(self):
        wf = self.widget.spike_panel.workflow
        wf.start('A', total=1)
        wf.bind_capture('capture-a')
        self.widget._snap_in_flight = True
        self.widget._preparing_id = 'capture-a'
        self.widget._on_pipeline_event(self._event('quality', score=5, passed=True))
        self.assertEqual(wf.current_step().side, 'side')
        self.assertEqual(wf.steps()[0].status, StepStatus.SAVING)
        self.assertFalse(wf.is_complete())
        self.assertFalse(self.widget._snap_in_flight)
        with patch.object(self.widget, '_append_capture_log'):
            self.widget._on_pipeline_event(self._event('saved', score=5, passed=True))
        self.assertEqual(wf.steps()[0].status, StepStatus.PASSED)
        self.assertEqual(wf.current_step().side, 'side')

    def test_failed_save_marks_original_position_without_moving_cursor(self):
        wf = self.widget.spike_panel.workflow
        wf.start('A', total=1)
        wf.bind_capture('capture-a')
        self.widget._on_pipeline_event(self._event('quality', score=5, passed=True))
        self.widget._on_pipeline_event(self._event('failed', message='disk full'))
        self.assertEqual(wf.steps()[0].status, StepStatus.SAVE_FAILED)
        self.assertEqual(wf.current_step().side, 'side')
        self.assertFalse(wf.is_complete())

    def test_http_and_response_errors_never_start(self):
        self._select_qr_mode()
        start = Mock(return_value=True)
        self.widget.spike_panel.start_with_prefix = start
        cases = [
            _FakeReply(401),
            _FakeReply(403),
            _FakeReply(404),
            _FakeReply(429),
            _FakeReply(500),
            _FakeReply(200, b"not-json"),
            _FakeReply(200, b'{"material": {"accession_no": "CON"}}'),
            _FakeReply(
                0,
                error=QNetworkReply.NetworkError.ConnectionRefusedError,
            ),
            _FakeReply(
                0,
                error=QNetworkReply.NetworkError.TimeoutError,
            ),
        ]
        for index, reply in enumerate(cases, start=1):
            with self.subTest(index=index):
                self.widget._qr_request_id = index
                self.widget._set_qr_request_pending(True)
                self.widget._on_field_tag_reply_finished(index, reply)
                self.assertFalse(self.widget._qr_request_pending)
        start.assert_not_called()

    def test_explicit_ten_second_timeout_invalidates_request(self):
        self._select_qr_mode()
        reply = _FakeReply(0)
        self.widget._qr_request_id = 8
        self.widget._qr_reply = reply
        self.widget._qr_timeout_request_id = 8
        self.widget._set_qr_request_pending(True)
        self.widget._on_field_tag_request_timeout()
        self.assertFalse(self.widget._qr_request_pending)
        self.assertEqual(self.widget._qr_request_id, 9)
        self.assertTrue(reply.aborted)
        self.assertTrue(reply.deleted)

    def test_outline_uses_five_second_timer_and_clears(self):
        points = np.array(
            [[1, 1], [10, 1], [10, 10], [1, 10]], dtype=float
        )
        self.widget.view.show_qr_outline(points)
        self.assertTrue(self.widget.view._qr_outline_item.isVisible())
        self.assertGreater(self.widget.view._qr_outline_timer.remainingTime(), 4500)
        self.widget.view._qr_outline_timer.timeout.emit()
        self.assertFalse(self.widget.view._qr_outline_item.isVisible())


@unittest.skipUnless(QT_AVAILABLE, "当前 Python 环境没有可用的 PyQt6")
class SharedBearerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.scorer_patch = patch(
            "ui.capture_widget.BRISQUEScorer", return_value=_FakeScorer()
        )
        cls.scorer_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.scorer_patch.stop()

    def test_bearer_syncs_between_tabs_but_not_across_windows(self):
        first = MainWindow()
        first.wheat_tab.le_bearer_key.setText("wft_memory_only")
        self.app.processEvents()
        self.assertEqual(
            first.grain_tab.le_bearer_key.text(), "wft_memory_only"
        )
        first.close()
        self.app.processEvents()

        second = MainWindow()
        self.assertEqual(second.wheat_tab.le_bearer_key.text(), "")
        self.assertEqual(second.grain_tab.le_bearer_key.text(), "")
        second.close()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
