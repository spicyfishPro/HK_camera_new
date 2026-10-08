from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


class _BoundSignal:
    def __init__(self) -> None:
        self.emissions: list[tuple[object, ...]] = []

    def emit(self, *args) -> None:
        self.emissions.append(args)


class _SignalDescriptor:
    def __set_name__(self, _owner, name: str) -> None:
        self._name = name

    def __get__(self, instance, _owner):
        if instance is None:
            return self
        key = f"_test_signal_{self._name}"
        if key not in instance.__dict__:
            instance.__dict__[key] = _BoundSignal()
        return instance.__dict__[key]


class _QThread:
    def __init__(self, parent=None) -> None:
        self.parent = parent


def _pyqt_signal(*_args, **_kwargs):
    return _SignalDescriptor()


def _load_stream_worker_module():
    pyqt_package = types.ModuleType("PyQt6")
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QThread = _QThread
    qtcore.pyqtSignal = _pyqt_signal
    camera_module = types.ModuleType("core.camera")
    camera_module.HikCamera = object
    module_name = "_stream_worker_test_module"
    path = Path(__file__).parents[1] / "ui" / "stream_worker.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load ui/stream_worker.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "PyQt6": pyqt_package,
            "PyQt6.QtCore": qtcore,
            "core.camera": camera_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


stream_worker_module = _load_stream_worker_module()
StreamWorker = stream_worker_module.StreamWorker


class _FakeCamera:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.grabbing = False
        self.frame_number = 0
        self._lock = threading.Lock()

    def start_grabbing(self) -> None:
        with self._lock:
            self.calls.append("start")
            self.grabbing = True

    def stop_grabbing(self) -> None:
        with self._lock:
            self.calls.append("stop")
            self.grabbing = False

    def grab_frame_bgr(self, timeout_ms=500):
        del timeout_ms
        time.sleep(0.005)
        with self._lock:
            if not self.grabbing:
                return None
            self.frame_number += 1
            number = self.frame_number
        bgr = np.full((12, 16, 3), number % 255, dtype=np.uint8)
        return bgr, number

    @staticmethod
    def copy_frame_metadata(raw_frame):
        return raw_frame

    @staticmethod
    def free_frame(_raw_frame) -> None:
        return None


def _wait_for(predicate, timeout=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class StreamWorkerControlTests(unittest.TestCase):
    def _start(self):
        camera = _FakeCamera()
        worker = StreamWorker(camera)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        self.assertTrue(_wait_for(lambda: worker.latest_frame is not None))
        return camera, worker, thread

    def test_pause_resume_and_snapshot_lifetime(self):
        camera, worker, thread = self._start()
        snapshot = worker.latest_frame
        self.assertIsNotNone(snapshot)
        self.assertIs(worker.latest_frame, snapshot)
        self.assertFalse(snapshot.bgr.flags.writeable)
        old_pixels = snapshot.bgr.copy()
        old_number = snapshot.metadata

        self.assertTrue(worker.request_pause())
        self.assertTrue(worker.wait_until_paused(1.0))
        self.assertFalse(camera.grabbing)
        self.assertEqual(camera.calls[:2], ["start", "stop"])

        worker.resume()
        self.assertTrue(_wait_for(lambda: camera.calls.count("start") >= 2))
        self.assertTrue(_wait_for(
            lambda: (
                worker.latest_frame is not None
                and worker.latest_frame.metadata != old_number
            )
        ))
        np.testing.assert_array_equal(snapshot.bgr, old_pixels)

        worker.stop()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())

    def test_stop_while_paused_never_deadlocks(self):
        _camera, worker, thread = self._start()
        self.assertTrue(worker.request_pause())
        self.assertTrue(worker.wait_until_paused(1.0))
        worker.stop()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(worker.wait_until_paused(0.01))

    def test_preview_signal_has_single_outstanding_slot(self):
        _camera, worker, thread = self._start()
        self.assertTrue(_wait_for(lambda: len(worker.frame_ready.emissions) == 1))
        time.sleep(0.08)
        self.assertEqual(len(worker.frame_ready.emissions), 1)

        worker.display_consumed()
        self.assertTrue(_wait_for(lambda: len(worker.frame_ready.emissions) == 2))
        worker.stop()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())

    def test_wait_for_pause_times_out_without_blocking_forever(self):
        worker = StreamWorker(_FakeCamera())
        started = time.perf_counter()
        self.assertFalse(worker.wait_until_paused(0.02))
        self.assertLess(time.perf_counter() - started, 0.2)
        worker.stop()


if __name__ == "__main__":
    unittest.main()


class FreshSnapshotTests(unittest.TestCase):
    def test_equal_clock_ticks_still_require_a_new_read(self):
        camera = _FakeCamera()
        started = threading.Event()
        release = threading.Event()
        original_grab = camera.grab_frame_bgr

        def grab(timeout_ms=500):
            if camera.frame_number == 0:
                started.set()
                release.wait(1)
            return original_grab(timeout_ms)

        camera.grab_frame_bgr = grab
        camera.clear_image_buffer = lambda: camera.calls.append('clear') or True
        worker = StreamWorker(camera)
        received = []
        # 模拟 Windows 上两次 monotonic 读取落在同一个 tick。
        with patch.object(stream_worker_module, 'time', types.SimpleNamespace(monotonic=lambda: 1.0)):
            thread = threading.Thread(target=worker.run, daemon=True)
            request = threading.Thread(target=lambda: received.append(worker.fresh_snapshot('new', 1)))
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                request.start()
                self.assertTrue(_wait_for(lambda: 'new' in worker._fresh_requests))
                release.set()
                request.join(1)
                self.assertFalse(request.is_alive())
                self.assertEqual(received[0].metadata, 2)
                self.assertIn('clear', camera.calls)
            finally:
                release.set()
                worker.stop()
                thread.join(1)
                if request.ident is not None:
                    request.join(1)

    def test_fresh_request_does_not_reuse_cached_frame(self):
        camera = _FakeCamera()
        camera.set_latest_frame_strategy = lambda: True
        worker = StreamWorker(camera)
        worker.set_saving(True)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(_wait_for(lambda: worker.latest_frame is not None))
            previous = worker.latest_frame.metadata
            first = worker.fresh_snapshot('first', 1)
            second = worker.fresh_snapshot('second', 1)
            self.assertGreater(first.metadata, previous)
            self.assertGreater(second.metadata, first.metadata)
            self.assertEqual(camera.calls.count('stop'), 0)
            self.assertEqual(worker._fresh_requests, {})
        finally:
            worker.stop(); thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_fresh_request_timeout_removes_request(self):
        worker = StreamWorker(_FakeCamera())
        with self.assertRaises(RuntimeError):
            worker.fresh_snapshot('timeout', .02)
        self.assertEqual(worker._fresh_requests, {})
        worker.stop()

    def test_sdk_cache_is_cleared_before_requested_snapshot(self):
        camera=_FakeCamera()
        camera.set_latest_frame_strategy=lambda: True
        camera.clear_image_buffer=lambda: camera.calls.append('clear') or True
        worker=StreamWorker(camera)
        thread=threading.Thread(target=worker.run,daemon=True);thread.start()
        try:
            self.assertTrue(_wait_for(lambda:worker.latest_frame is not None))
            worker.fresh_snapshot('new',1)
            self.assertIn('clear',camera.calls)
            self.assertEqual(camera.calls.count('stop'),0)
        finally:
            worker.stop();thread.join(1)

    def test_failed_cache_clear_does_not_accept_old_picture(self):
        camera=_FakeCamera();camera.clear_image_buffer=lambda:False
        worker=StreamWorker(camera)
        thread=threading.Thread(target=worker.run,daemon=True);thread.start()
        try:
            with self.assertRaisesRegex(RuntimeError,'旧帧缓存'):
                worker.fresh_snapshot('new',1)
            self.assertEqual(worker._fresh_errors,{})
        finally:
            worker.stop();thread.join(1)
