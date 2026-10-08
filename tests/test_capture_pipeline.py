from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from core.acquisition_metadata import FrameSnapshot, SaveResult, save_bgr_atomic
from core.capture_pipeline import CaptureJob, CapturePipeline
from core.correction import FlatFieldCorrection
from core.spike_workflow import SpikeWorkflow, StepStatus
from test_acquisition_metadata import sample_frame, sample_metadata


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class Scorer:
    def __init__(self, score=5):
        self.score = score

    def prepare_ndarray(self, image):
        return image.copy()

    def score_prepared(self, image):
        return self.score


def job(folder, number, *, path=None, raw=False, overwrite=False, score=5):
    image = np.arange(8 * 12 * 3, dtype=np.uint8).reshape(8, 12, 3) + np.uint8(number)
    metadata = replace(
        sample_metadata(),
        capture_id=str(number),
        accession_no="A",
        filename=f"A_Rep{number}_front.png",
        rotation_deg=180,
        ffc_applied=False,
        frame=replace(sample_frame(), frame_number=number),
    )
    destination = path or folder / metadata.filename
    metadata = replace(metadata, filename=destination.name)
    snapshot = FrameSnapshot(image, metadata.frame)
    return CaptureJob(
        metadata,
        "owner",
        "session",
        number,
        destination,
        destination.with_stem(destination.stem + "_raw") if raw else None,
        lambda: snapshot,
        FlatFieldCorrection().snapshot(),
        Scorer(score),
        60,
        image.nbytes * (4 if raw else 3),
        overwrite,
    )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.events = []
        self.gates = []
        self.pipelines = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for p in self.pipelines:
            for _ in range(3):
                with p._lock:
                    failures = [k for k, r in p._records.items() if r.state == "failed"]
                for k in failures:
                    p.retry(k)
                if wait_for(lambda: p.pending_count == 0, 2):
                    break
            self.assertTrue(p.shutdown())
        self.temp.cleanup()

    def pipeline(self, **kwargs):
        p = CapturePipeline(self.events.append, **kwargs)
        self.pipelines.append(p)
        return p

    def test_three_second_writes_do_not_block_quality_of_next_capture(self):
        def slow_save(image, path, metadata, **kwargs):
            time.sleep(3)
            return SaveResult(path, True, "XMP")

        p = self.pipeline(saver=slow_save)
        started = time.monotonic()
        self.assertTrue(p.submit(job(self.folder, 1)))
        self.assertTrue(
            wait_for(
                lambda: any(e.id == "1" and e.state == "quality" for e in self.events)
            )
        )
        self.assertTrue(p.submit(job(self.folder, 2)))
        self.assertTrue(
            wait_for(
                lambda: any(e.id == "2" and e.state == "quality" for e in self.events)
            )
        )
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(any(e.state == "saved" for e in self.events))
        self.assertTrue(wait_for(lambda: p.pending_count == 0))

    def test_capacity_no_silent_drop_and_release(self):
        gate = threading.Event()
        self.gates.append(gate)

        def save(image, path, meta, **kw):
            gate.wait(5)
            return SaveResult(path, True, "XMP")

        p = self.pipeline(saver=save, max_jobs=2, max_bytes=5000)
        self.assertTrue(p.submit(job(self.folder, 1)))
        self.assertTrue(p.submit(job(self.folder, 2)))
        self.assertFalse(p.submit(job(self.folder, 3)))
        self.assertEqual(p.pending_count, 2)
        gate.set()
        self.assertTrue(wait_for(lambda: p.pending_count == 0))
        self.assertEqual(p.reserved_bytes, 0)
        self.assertTrue(p.can_accept(864))

    def test_partial_raw_failure_retained_and_retry_skips_main(self):
        calls = []
        fail = [True]

        def save(image, path, meta, **kw):
            calls.append(path.name)
            if path.stem.endswith("_raw") and fail[0]:
                fail[0] = False
                raise OSError("disk full")
            return save_bgr_atomic(image, path, meta, **kw)

        p = self.pipeline(saver=save)
        j = job(self.folder, 1, raw=True)
        self.assertTrue(p.submit(j))
        self.assertTrue(wait_for(lambda: p.has_failures))
        self.assertTrue(j.path.exists())
        self.assertFalse(j.raw_path.exists())
        self.assertFalse(p.can_accept())
        self.assertFalse(p.shutdown())
        self.assertIsNotNone(p.image_preview(j.id))
        self.assertTrue(p.retry(j.id))
        self.assertTrue(wait_for(lambda: p.pending_count == 0))
        self.assertEqual(calls.count(j.path.name), 1)
        self.assertEqual(calls.count(j.raw_path.name), 2)

    def test_same_path_retake_order_and_pixel_identity(self):
        gate = threading.Event()
        self.gates.append(gate)
        order = []

        def save(image, path, meta, **kw):
            if meta.capture_id == "1":
                gate.wait(5)
            order.append(meta.capture_id)
            return save_bgr_atomic(image, path, meta, **kw)

        p = self.pipeline(saver=save)
        first = job(self.folder, 1)
        second = job(self.folder, 2, path=first.path, overwrite=True)
        self.assertTrue(p.submit(first))
        self.assertTrue(p.submit(second))
        self.assertTrue(
            wait_for(
                lambda: any(e.id == "2" and e.state == "quality" for e in self.events)
            )
        )
        self.assertEqual(order, [])
        gate.set()
        self.assertTrue(wait_for(lambda: p.pending_count == 0))
        self.assertEqual(order, ["1", "2"])
        actual = cv2.imdecode(np.fromfile(str(first.path), np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(actual, cv2.flip(second.get_frame().bgr, -1))
        self.assertIn(b"2", first.path.read_bytes())

    def test_independent_images_and_ffc_snapshot(self):
        ffc = FlatFieldCorrection()
        ffc.set_flat(np.full((8, 12, 3), 100, np.uint8))
        ffc.set_enabled(True)
        correction = ffc.snapshot()
        before = correction.apply(np.full((8, 12, 3), 80, np.uint8))
        ffc.set_dark(np.full((8, 12, 3), 20, np.uint8))
        np.testing.assert_array_equal(
            correction.apply(np.full((8, 12, 3), 80, np.uint8)), before
        )
        p = self.pipeline()
        jobs = [job(self.folder, n) for n in (1, 2, 3)]
        for j in jobs:
            self.assertTrue(p.submit(j))
        self.assertTrue(wait_for(lambda: p.pending_count == 0))
        for j in jobs:
            actual = cv2.imdecode(np.fromfile(str(j.path), np.uint8), cv2.IMREAD_COLOR)
            np.testing.assert_array_equal(actual, cv2.flip(j.get_frame().bgr, -1))
            self.assertIn(b"<x:xmpmeta", j.path.read_bytes())

    def test_timeout_releases_capacity_and_drain_rejects_new_work(self):
        p = self.pipeline()

        def timeout():
            raise RuntimeError("fresh frame timeout")

        j = replace(job(self.folder, 1), get_frame=timeout)
        self.assertTrue(p.submit(j))
        self.assertTrue(wait_for(lambda: p.pending_count == 0))
        self.assertTrue(any(e.state == "capture_failed" for e in self.events))
        p.begin_drain()
        self.assertFalse(p.submit(job(self.folder, 2)))

    def test_atomic_failure_preserves_existing_file(self):
        target = self.folder / "A.png"
        target.write_bytes(b"old")
        j = job(self.folder, 1, path=target)
        with self.assertRaises(FileExistsError):
            save_bgr_atomic(j.get_frame().bgr, target, j.metadata)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(self.folder.glob("*.partial*")), [])


class AsyncWorkflowTests(unittest.TestCase):
    def test_out_of_order_save_and_late_retake_callback(self):
        wf = SpikeWorkflow()
        wf.start("A", 1)
        session = wf.session_id
        wf.bind_capture("front")
        wf.apply_capture_result(session, "front", passed=True, score=5)
        self.assertEqual(wf.current_step().side, "side")
        wf.bind_capture("bad")
        wf.apply_capture_result(session, "bad", passed=False, score=90)
        wf.bind_capture("good")
        self.assertFalse(wf.apply_capture_result(session, "bad", saved=True))
        wf.apply_capture_result(session, "good", passed=True, score=5)
        self.assertIsNone(wf.current_step())
        wf.apply_capture_result(session, "good", saved=True)
        self.assertFalse(wf.is_complete())
        wf.reevaluate_disk(lambda *_: True)
        self.assertEqual(wf.steps()[0].status, StepStatus.SAVING)
        wf.apply_capture_result(session, "front", saved=True)
        self.assertTrue(wf.is_complete())
        wf.start("B", 1)
        self.assertFalse(wf.apply_capture_result(session, "front", saved=True))


class RealQualityTests(unittest.TestCase):
    def test_packaged_opencv_has_working_brisque(self):
        from core.quality import BRISQUEScorer

        self.assertTrue(
            hasattr(cv2, "quality"), "运行依赖必须使用 opencv-contrib-python"
        )
        scorer = BRISQUEScorer()
        image = np.random.default_rng(42).integers(
            0, 256, (128, 192, 3), dtype=np.uint8
        )
        result = scorer.score_ndarray(image)
        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result))
