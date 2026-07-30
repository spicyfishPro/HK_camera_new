from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

from core.correction import FlatFieldCorrection
from core.naming import build_filename, next_serial, resolve_conflict
from core.quality import is_pass
from core.spike_workflow import SpikeWorkflow


class NamingRegressionTests(unittest.TestCase):
    def test_legacy_filename_shape_is_unchanged(self):
        name = build_filename(
            "SX001",
            repeat=2,
            side="front",
            serial=7,
            ext="PNG",
            now=datetime(2026, 7, 30, 12, 34, 56),
        )
        self.assertEqual(
            name, "SX001_R2_front_2026-07-30-12-34-56_007.png"
        )

    def test_serial_resume_and_conflict_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "SX001_2026-07-30-12-00-00_001.png").touch()
            (folder / "SX001_2026-07-30-12-00-01_004.jpg").touch()
            self.assertEqual(next_serial(folder, "SX001"), 5)

            target = folder / "SX001_Rep1.png"
            target.touch()
            self.assertEqual(
                resolve_conflict(target), folder / "SX001_Rep1_2.png"
            )


class WorkflowRegressionTests(unittest.TestCase):
    def test_wheat_and_grain_naming_and_progress(self):
        wheat = SpikeWorkflow()
        wheat.start("A1", total=2)
        self.assertEqual(wheat.filename_for_current(), "A1_Rep1_front.png")
        self.assertTrue(wheat.on_pass(10.0))
        self.assertEqual(wheat.filename_for_current(), "A1_Rep1_side.png")

        grain = SpikeWorkflow()
        grain.start("CK", total=2, start_side="", sides=("",))
        self.assertEqual(grain.filename_for_current("jpg"), "CK_Rep1.jpg")
        self.assertTrue(grain.on_pass(8.0))
        self.assertEqual(grain.filename_for_current("jpg"), "CK_Rep2.jpg")
        self.assertFalse(grain.on_pass(7.0))
        self.assertTrue(grain.is_complete())

    def test_workflow_can_preserve_api_accession_exactly(self):
        workflow = SpikeWorkflow()
        workflow.start(" CK", total=1)
        self.assertEqual(workflow.prefix, " CK")
        self.assertEqual(workflow.filename_for_current(), " CK_Rep1_front.png")

    def test_resume_skips_existing_files_without_overwriting(self):
        workflow = SpikeWorkflow()
        workflow.start("A1", total=2)
        existing = {(1, "front"), (1, "side")}
        workflow.resume_from_disk(lambda rep, side: (rep, side) in existing)
        current = workflow.current_step()
        self.assertIsNotNone(current)
        self.assertEqual((current.rep, current.side), (2, "front"))

    def test_complete_existing_set_has_no_current_file(self):
        workflow = SpikeWorkflow()
        workflow.start("A1", total=1)
        workflow.resume_from_disk(lambda _rep, _side: True)
        self.assertTrue(workflow.is_complete())
        self.assertIsNone(workflow.current_step())
        self.assertIsNone(workflow.filename_for_current())


class FfcAndQualityRegressionTests(unittest.TestCase):
    def test_uniform_flat_field_keeps_uniform_image(self):
        correction = FlatFieldCorrection()
        image = np.full((10, 12, 3), 100, dtype=np.uint8)
        correction.set_flat(image)
        corrected = correction.apply_full(image)
        np.testing.assert_array_equal(corrected, image)

    def test_dark_field_is_subtracted(self):
        correction = FlatFieldCorrection()
        dark = np.full((8, 8, 3), 10, dtype=np.uint8)
        flat = np.full((8, 8, 3), 110, dtype=np.uint8)
        image = np.full((8, 8, 3), 60, dtype=np.uint8)
        correction.set_dark(dark)
        correction.set_flat(flat)
        corrected = correction.apply_full(image)
        np.testing.assert_array_equal(
            corrected, np.full_like(image, 50)
        )

    def test_brisque_threshold_boundary(self):
        self.assertTrue(is_pass(65.0, 65.0))
        self.assertFalse(is_pass(65.01, 65.0))


if __name__ == "__main__":
    unittest.main()
