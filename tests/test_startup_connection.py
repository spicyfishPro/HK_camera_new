from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_qr_ui import QT_AVAILABLE, CameraError, MainWindow, QApplication, _FakeScorer


def device(serial="camera-1"):
    return SimpleNamespace(index=0, model="Test", serial=serial, transport="USB3",
                           manufacturer="Test", ip=None, display=f"Test {serial}")


@unittest.skipUnless(QT_AVAILABLE, "PyQt6 unavailable")
class StartupConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = self.enterContext(patch("ui.main_window.time.monotonic", return_value=100.0))
        self.enterContext(patch("ui.capture_widget.BRISQUEScorer", return_value=_FakeScorer()))
        self.enumerate = self.enterContext(patch("ui.capture_widget.HikCamera.enumerate", create=True))
        self.enumerate.return_value = [device()]
        self.worker = self.enterContext(patch("ui.capture_widget.StreamWorker"))
        self.worker.return_value.isRunning.return_value = False
        self.window = MainWindow()
        self.addCleanup(self.window.close)
        for tab in (self.window.wheat_tab, self.window.grain_tab):
            tab.camera = Mock()
            tab.camera.is_connected.return_value = False
            tab.camera.get_float.return_value = None
            tab.camera.get_int.return_value = None
            tab.camera.get_enum.return_value = 0
            tab.camera.pixel_format_name.return_value = "BayerBG8"
            tab.camera.disconnect.side_effect = lambda t=tab: t.camera.is_connected.configure_mock(return_value=False)
            tab.camera.connect.side_effect = lambda *_args, t=tab, **_kw: self.connect(t)
            tab._err = Mock()
        self.window.show()

    def connect(self, tab):
        tab.camera.is_connected.return_value = True
        return device()

    def attempt(self, elapsed=1):
        self.clock.return_value = 100.0 + elapsed
        self.window._try_auto_connect()

    def test_single_camera_starts_stream_after_separate_100ms_timer(self):
        self.enumerate.assert_not_called()
        self.assertEqual(self.window._auto_connect_timer.interval(), 1000)
        self.assertEqual(self.window._auto_deadline_timer.interval(), 60000)
        self.attempt()
        tab = self.window.wheat_tab
        tab.camera.connect.assert_called_once_with(0, single_device_only=True)
        self.window.grain_tab.camera.connect.assert_not_called()
        self.worker.assert_not_called()
        self.assertFalse(self.window._auto_connect_active)
        self.assertTrue(self.window._auto_stream_timer.isActive())
        self.assertEqual(self.window._auto_stream_timer.interval(), 100)
        self.window._start_auto_stream()
        self.worker.return_value.start.assert_called_once()
        self.assertEqual(tab.btn_stream.text(), "停止采集")
        self.window._start_auto_stream()
        self.worker.return_value.start.assert_called_once()

    def test_no_camera_retries_until_minute_without_modal_errors(self):
        self.enumerate.return_value = []
        for elapsed in range(1, 60, 5):
            self.attempt(elapsed)
            self.assertTrue(self.window._auto_connect_timer.isActive())
            self.assertEqual(self.window._auto_connect_timer.interval(), 5000)
        self.assertEqual(self.enumerate.call_count, 12)
        self.attempt(60)
        self.assertEqual(self.enumerate.call_count, 12)
        self.assertFalse(self.window._auto_connect_active)
        self.assertFalse(self.window._auto_connect_timer.isActive())
        self.window.wheat_tab.camera.connect.assert_not_called()
        self.window.wheat_tab._err.assert_not_called()
        self.assertIn("已满 1 分钟", self.window._right_status.text())

    def test_camera_appears_on_retry_in_current_grain_tab(self):
        self.enumerate.side_effect = [[], [device()]]
        self.attempt()
        self.window.tabs.setCurrentWidget(self.window.grain_tab)
        self.attempt(6)
        self.window.grain_tab.camera.connect.assert_called_once_with(0, single_device_only=True)
        self.window.wheat_tab.camera.connect.assert_not_called()
        self.window._start_auto_stream()
        self.assertIs(self.window.grain_tab._stream, self.worker.return_value)

    def test_multiple_cameras_stop_automatic_flow(self):
        self.enumerate.return_value = [device("one"), device("two")]
        self.attempt()
        self.assertFalse(self.window._auto_connect_active)
        self.assertEqual(self.window.wheat_tab.lst_devices.currentRow(), -1)
        self.window.wheat_tab.camera.connect.assert_not_called()
        self.enumerate.return_value = [device()]
        self.attempt(6)
        self.assertEqual(self.enumerate.call_count, 1)
        self.assertIn("手动选择", self.window._right_status.text())

    def test_second_camera_appears_between_refresh_and_connect(self):
        error = CameraError("设备数量变化")
        error.device_count = 2
        self.window.wheat_tab.camera.connect.side_effect = error
        self.enumerate.side_effect = [[device()], [device("one"), device("two")]]
        self.attempt()
        self.assertFalse(self.window._auto_connect_active)
        self.worker.assert_not_called()
        self.assertEqual(self.window.wheat_tab.lst_devices.count(), 2)
        self.window.wheat_tab._err.assert_not_called()

    def test_enumeration_and_connection_errors_retry_without_dialog(self):
        self.enumerate.side_effect = [CameraError("枚举失败"), [device()], [device()]]
        self.attempt()
        tab = self.window.wheat_tab
        tab.camera.connect.side_effect = CameraError("设备忙")
        self.attempt(6)
        self.assertTrue(self.window._auto_connect_active)
        tab.camera.connect.side_effect = lambda *_a, **_kw: self.connect(tab)
        self.attempt(11)
        self.assertTrue(self.window._auto_stream_timer.isActive())
        tab._err.assert_not_called()

    def test_delayed_refresh_cannot_connect_past_deadline(self):
        def slow_refresh():
            self.clock.return_value = 161.0
            return [device()]
        self.enumerate.side_effect = slow_refresh
        self.attempt(56)
        self.window.wheat_tab.camera.connect.assert_not_called()
        self.assertFalse(self.window._auto_connect_active)

    def test_manual_refresh_and_connect_take_control(self):
        tab = self.window.wheat_tab
        tab.btn_refresh.click()
        self.assertFalse(self.window._auto_connect_active)
        tab.btn_connect.click()
        tab.camera.connect.assert_called_once_with(0, single_device_only=False)
        self.window._start_auto_stream()
        self.worker.assert_not_called()

    def test_manual_disconnect_cancels_pending_stream(self):
        self.attempt()
        self.window.wheat_tab.btn_disconnect.click()
        self.window._start_auto_stream()
        self.worker.assert_not_called()
        self.assertFalse(self.window._auto_connect_timer.isActive())

    def test_manual_stream_is_not_toggled_off_by_pending_callback(self):
        self.attempt()
        self.window.wheat_tab.btn_stream.click()
        self.window._start_auto_stream()
        self.worker.return_value.start.assert_called_once()
        self.worker.return_value.stop.assert_not_called()

    def test_other_tab_connected_or_close_prevents_auto_connection(self):
        self.window.grain_tab.camera.is_connected.return_value = True
        self.attempt()
        self.enumerate.assert_not_called()
        self.window.close()
        self.window.show()
        self.assertFalse(self.window._auto_connect_timer.isActive())

    def test_closing_cancels_pending_stream_and_retry(self):
        self.attempt()
        self.window.close()
        self.window._start_auto_stream()
        self.attempt(6)
        self.worker.assert_not_called()
        self.assertFalse(self.window._auto_stream_timer.isActive())
        self.assertFalse(self.window._auto_deadline_timer.isActive())

    def test_close_before_first_attempt_never_touches_device(self):
        self.window.close()
        self.attempt()
        self.enumerate.assert_not_called()
        self.window.wheat_tab.camera.connect.assert_not_called()

    def test_real_qt_startup_timers_do_not_fire_early(self):
        # Restore a real clock for one end-to-end timer check; other tests fast-forward time.
        self.clock.side_effect = time.perf_counter
        self.window._auto_connect_deadline = time.perf_counter() + 60
        started = time.perf_counter()
        self.window._auto_connect_timer.start(1000)
        connections = []
        streams = []
        def connect(*_args, **_kwargs):
            connections.append(time.perf_counter())
            return self.connect(self.window.wheat_tab)
        self.window.wheat_tab.camera.connect.side_effect = connect
        self.worker.return_value.start.side_effect = lambda: streams.append(time.perf_counter())
        while not streams and time.perf_counter() - started < 4:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertEqual(len(connections), 1)
        self.assertEqual(len(streams), 1)
        self.assertGreaterEqual(connections[0] - started, 0.99)
        self.assertGreaterEqual(streams[0] - connections[0], 0.095)


if __name__ == "__main__":
    unittest.main()
