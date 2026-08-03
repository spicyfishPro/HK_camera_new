from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from core.acquisition_metadata import (
    XMP_NAMESPACE,
    CaptureMetadata,
    FrameMetadata,
    FrameSnapshot,
    save_bgr_with_metadata,
)
from core.correction import FlatFieldCorrection


def sample_frame() -> FrameMetadata:
    return FrameMetadata(
        received_at="2026-08-03T12:34:56.789+08:00",
        width=12,
        height=8,
        pixel_type_code=0x0108000B,
        pixel_format="BayerBG8",
        frame_number=42,
        frame_length=96,
        device_timestamp=123456,
        host_timestamp=654321,
        exposure_time_us=8000.0,
        gain=0.0,
        average_brightness=127,
        white_balance_red=1980,
        white_balance_green=1024,
        white_balance_blue=2259,
        frame_counter=42,
        trigger_index=0,
        offset_x=0,
        offset_y=0,
        lost_packets=0,
        second_count=None,
        cycle_count=None,
        cycle_offset=None,
    )


def sample_metadata(
    *, filename: str = "材料<&_Rep1_front.png", output_format: str = "PNG",
) -> CaptureMetadata:
    return CaptureMetadata(
        capture_id="12345678-1234-5678-1234-567812345678",
        captured_at="2026-08-03T12:34:56.789+08:00",
        accession_no="材料<&",
        prefix_source="qr",
        field_tag_code="PABC123",
        mode="wheat",
        workflow_active=True,
        rep=1,
        side="front",
        filename=filename,
        image_variant="processed",
        device_manufacturer="Hikrobot",
        device_model="MV-CH250-90UC",
        device_serial="SN001",
        device_transport="USB3",
        device_ip=None,
        frame=sample_frame(),
        exposure_readback_us=8000.0,
        gain_readback=0.0,
        exposure_auto_code=0,
        gain_auto_code=0,
        trigger_mode_code=0,
        white_balance_mode="Off",
        white_balance_red=1980,
        white_balance_green=1024,
        white_balance_blue=2259,
        brightness=None,
        gamma=None,
        saturation=None,
        contrast=None,
        sharpness=None,
        output_format=output_format,
        jpeg_quality=92 if output_format == "JPG" else None,
        png_compression=1 if output_format == "PNG" else None,
        tiff_compression="none" if output_format == "TIFF" else None,
        rotation_deg=90,
        ffc_applied=True,
        ffc_has_flat=True,
        ffc_has_dark=True,
        ffc_calibration_id="abc123",
        output_width=8,
        output_height=12,
    )


def xmp_json(xmp: bytes) -> dict:
    root = ET.fromstring(xmp.decode("utf-8"))
    element = root.find(f".//{{{XMP_NAMESPACE}}}CaptureMetadata")
    if element is None or element.text is None:
        raise AssertionError("XMP 中缺少 CaptureMetadata")
    return json.loads(element.text)


class MetadataSchemaTests(unittest.TestCase):
    def test_xmp_roundtrip_and_sensitive_field_whitelist(self):
        metadata = sample_metadata()
        payload = xmp_json(metadata.to_xmp_bytes())
        self.assertEqual(payload["schema_version"], "wheat.capture.v1")
        self.assertEqual(payload["sample"]["accession_no"], "材料<&")
        self.assertEqual(payload["sample"]["field_tag_code"], "PABC123")
        self.assertEqual(
            payload["processing"]["demosaic_mapping"],
            "GenICam BayerBG8 -> OpenCV COLOR_BayerRG2BGR",
        )
        self.assertFalse(payload["processing"]["native_bayer_saved"])

        serialized = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in ("bearer", "authorization", "api_response", "brisque"):
            self.assertNotIn(forbidden, serialized)

    def test_raw_companion_shares_capture_but_marks_processing(self):
        main = sample_metadata()
        raw = replace(
            main,
            filename="材料_Rep1_front_raw.png",
            image_variant="unffc_demosaiced",
            ffc_applied=False,
        )
        self.assertEqual(main.capture_id, raw.capture_id)
        payload = raw.to_dict()
        self.assertEqual(payload["image"]["variant"], "unffc_demosaiced")
        self.assertFalse(payload["processing"]["ffc_applied"])
        self.assertTrue(payload["processing"]["demosaiced"])


class FrameBindingTests(unittest.TestCase):
    def test_sdk_frame_metadata_is_copied_before_buffer_release(self):
        info = SimpleNamespace(
            nWidth=12, nHeight=8, nExtendWidth=0, nExtendHeight=0,
            enPixelType=0x0108000B, nFrameNum=42, nFrameLen=96,
            nFrameLenEx=0, nDevTimeStampHigh=1, nDevTimeStampLow=2,
            nHostTimeStamp=3, fExposureTime=8000.0, fGain=0.0,
            nAverageBrightness=127, nRed=1980, nGreen=1024, nBlue=2259,
            nFrameCounter=42, nTriggerIndex=0, nOffsetX=0, nOffsetY=0,
            nLostPacket=0, nSecondCount=0, nCycleCount=0, nCycleOffset=0,
        )
        frame = SimpleNamespace(stFrameInfo=info)
        copied = FrameMetadata.from_mvs_frame(
            frame,
            lambda value: "BayerBG8" if value == 0x0108000B else hex(value),
        )
        info.nFrameNum = 999
        info.fExposureTime = 1.0
        self.assertEqual(copied.frame_number, 42)
        self.assertEqual(copied.exposure_time_us, 8000.0)
        self.assertEqual(copied.device_timestamp, (1 << 32) | 2)
        self.assertEqual(copied.pixel_format, "BayerBG8")

    def test_missing_sdk_watermark_fields_become_null(self):
        info = SimpleNamespace(
            nWidth=12, nHeight=8, nExtendWidth=0, nExtendHeight=0,
            enPixelType=0x0108000B, nFrameNum=0, nFrameLen=96,
            nFrameLenEx=0, nDevTimeStampHigh=0, nDevTimeStampLow=0,
            nHostTimeStamp=0, fExposureTime=0.0, fGain=0.0,
            nAverageBrightness=0, nRed=0, nGreen=0, nBlue=0,
            nFrameCounter=0, nTriggerIndex=0, nOffsetX=0, nOffsetY=0,
            nLostPacket=0, nSecondCount=0, nCycleCount=0, nCycleOffset=0,
        )
        copied = FrameMetadata.from_mvs_frame(
            SimpleNamespace(stFrameInfo=info), lambda _value: "BayerBG8",
        )
        self.assertIsNone(copied.exposure_time_us)
        self.assertIsNone(copied.gain)
        self.assertIsNone(copied.average_brightness)
        self.assertIsNone(copied.frame_counter)

    def test_frame_snapshot_copy_keeps_image_and_metadata_paired(self):
        original = np.full((2, 3, 3), 7, dtype=np.uint8)
        stored = FrameSnapshot(original, sample_frame())
        first = stored.copy()
        first.bgr[:] = 99
        second = stored.copy()
        np.testing.assert_array_equal(second.bgr, original)
        self.assertEqual(second.metadata.frame_number, 42)


class FfcCalibrationIdTests(unittest.TestCase):
    def test_reference_id_is_stable_and_changes_with_reference(self):
        correction = FlatFieldCorrection()
        flat = np.full((5, 7, 3), 100, dtype=np.uint8)
        correction.set_flat(flat)
        first = correction.reference_id
        correction.set_flat(flat.copy())
        self.assertEqual(correction.reference_id, first)
        changed = flat.copy()
        changed[0, 0, 0] = 101
        correction.set_flat(changed)
        self.assertNotEqual(correction.reference_id, first)
        correction.clear()
        self.assertIsNone(correction.reference_id)


class ImageMetadataWriteTests(unittest.TestCase):
    def test_tiff_xmp_and_pixels_roundtrip(self):
        import tifffile

        bgr = np.zeros((12, 8, 3), dtype=np.uint8)
        bgr[:, :, 0] = 10
        bgr[:, :, 1] = 20
        bgr[:, :, 2] = 30
        metadata = sample_metadata(filename="sample.tiff", output_format="TIFF")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.tiff"
            result = save_bgr_with_metadata(bgr, path, metadata)
            self.assertTrue(result.metadata_embedded)
            with tifffile.TiffFile(path) as tif:
                xmp = bytes(tif.pages[0].tags[700].value)
                rgb = tif.asarray()
            self.assertEqual(xmp_json(xmp)["capture_id"], metadata.capture_id)
            np.testing.assert_array_equal(rgb, bgr[:, :, ::-1])

    def test_jpeg_png_embed_when_opencv_supports_it_otherwise_warn(self):
        bgr = np.full((12, 8, 3), 120, dtype=np.uint8)
        supports_metadata = (
            hasattr(cv2, "imencodeWithMetadata")
            and hasattr(cv2, "IMAGE_METADATA_XMP")
        )
        with tempfile.TemporaryDirectory() as tmp:
            for suffix, fmt in ((".png", "PNG"), (".jpg", "JPG")):
                with self.subTest(suffix=suffix):
                    path = Path(tmp) / f"sample{suffix}"
                    metadata = sample_metadata(
                        filename=path.name, output_format=fmt,
                    )
                    result = save_bgr_with_metadata(bgr, path, metadata)
                    self.assertTrue(path.is_file())
                    decoded = cv2.imread(str(path), cv2.IMREAD_COLOR)
                    self.assertEqual(decoded.shape, bgr.shape)
                    if supports_metadata:
                        self.assertTrue(result.metadata_embedded)
                        self.assertIn(b"<x:xmpmeta", path.read_bytes())
                        if suffix == ".png" and hasattr(cv2, "imdecodeWithMetadata"):
                            _, metadata_types, chunks = cv2.imdecodeWithMetadata(
                                np.fromfile(path, dtype=np.uint8)
                            )
                            self.assertEqual(metadata_types, [cv2.IMAGE_METADATA_XMP])
                            chunk = bytes(np.asarray(chunks[0]).reshape(-1)).rstrip(b"\x00")
                            self.assertEqual(
                                xmp_json(chunk)["capture_id"], metadata.capture_id,
                            )
                    else:
                        self.assertFalse(result.metadata_embedded)
                        self.assertEqual(
                            result.warning,
                            "照片已保存，但未写入采集元数据",
                        )

    def test_metadata_failure_falls_back_but_plain_save_failure_raises(self):
        bgr = np.zeros((3, 4, 3), dtype=np.uint8)
        metadata = sample_metadata(filename="sample.png", output_format="PNG")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.png"
            with mock.patch(
                "core.acquisition_metadata._save_jpeg_or_png_with_xmp",
                side_effect=RuntimeError("metadata only"),
            ):
                result = save_bgr_with_metadata(bgr, path, metadata)
            self.assertTrue(path.is_file())
            self.assertFalse(result.metadata_embedded)

            with mock.patch(
                "core.acquisition_metadata._save_jpeg_or_png_with_xmp",
                side_effect=RuntimeError("metadata only"),
            ), mock.patch(
                "core.acquisition_metadata.save_bgr_plain",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    save_bgr_with_metadata(bgr, path, metadata)


if __name__ == "__main__":
    unittest.main()
