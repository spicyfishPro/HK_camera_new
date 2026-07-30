from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from core.field_tag_lookup import (
    FieldTagError,
    build_field_tag_api_url,
    detect_field_tag_qr,
    normalize_field_tag_code,
    parse_accession_response,
    validate_windows_filename_segment,
)


class _Detector:
    def __init__(self, result):
        self.result = result

    def detectAndDecodeMulti(self, _image):
        return self.result


class FieldTagDetectionTests(unittest.TestCase):
    def setUp(self):
        self.image = np.zeros((300, 400, 3), dtype=np.uint8)
        self.points = np.array(
            [[[10, 20], [110, 20], [110, 120], [10, 120]]],
            dtype=np.float32,
        )

    def _detect(self, result, image=None, max_side=2048):
        with patch(
            "core.field_tag_lookup.cv2.QRCodeDetector",
            return_value=_Detector(result),
        ):
            return detect_field_tag_qr(
                self.image if image is None else image,
                max_side=max_side,
            )

    def test_no_qr(self):
        with self.assertRaisesRegex(FieldTagError, "未找到二维码"):
            self._detect((False, (), None, ()))

    def test_single_valid_qr(self):
        result = self._detect((True, ("P12ABC",), self.points, ()))
        self.assertEqual(result.code, "P12ABC")
        np.testing.assert_array_equal(result.points, self.points[0])

    def test_lowercase_and_whitespace_are_normalized(self):
        result = self._detect((True, ("  p9az  ",), self.points, ()))
        self.assertEqual(result.code, "P9AZ")

    def test_invalid_qr_content(self):
        with self.assertRaisesRegex(FieldTagError, "不是有效的挂牌编号"):
            self._detect((True, ("HTTPS://EXAMPLE.COM",), self.points, ()))

    def test_multiple_qr_codes_are_rejected(self):
        points = np.concatenate([self.points, self.points + 100], axis=0)
        with self.assertRaisesRegex(FieldTagError, "多个二维码"):
            self._detect((True, ("P1", "P2"), points, ()))

    def test_downscaled_points_map_back_to_preview_coordinates(self):
        large = np.zeros((2000, 4000, 3), dtype=np.uint8)
        small_points = np.array(
            [[[10, 20], [110, 20], [110, 120], [10, 120]]],
            dtype=np.float32,
        )
        result = self._detect(
            (True, ("P123",), small_points, ()),
            image=large,
            max_side=1000,
        )
        np.testing.assert_allclose(result.points, small_points[0] * 4)

    @unittest.skipUnless(
        hasattr(cv2, "QRCodeEncoder_create"),
        "当前 OpenCV 没有测试用二维码编码器",
    )
    def test_real_opencv_qr_round_trip(self):
        qr = cv2.QRCodeEncoder_create().encode("p12ab")
        qr = cv2.resize(qr, (500, 500), interpolation=cv2.INTER_NEAREST)
        canvas = np.full((800, 1000, 3), 255, dtype=np.uint8)
        canvas[150:650, 250:750] = cv2.cvtColor(
            qr, cv2.COLOR_GRAY2BGR
        )
        result = detect_field_tag_qr(canvas)
        self.assertEqual(result.code, "P12AB")
        self.assertEqual(result.points.shape, (4, 2))


class ResponseAndFilenameTests(unittest.TestCase):
    def test_normalize_code_length_and_alphabet(self):
        self.assertEqual(normalize_field_tag_code(" p0z "), "P0Z")
        for value in ("P", "Q123", "P12-3", "P" + "A" * 24):
            with self.subTest(value=value):
                with self.assertRaises(FieldTagError):
                    normalize_field_tag_code(value)

    def test_parse_accession_returns_original_value(self):
        payload = json.dumps(
            {"material": {"accession_no": " CK"}}
        ).encode("utf-8")
        self.assertEqual(parse_accession_response(payload), " CK")

    def test_response_structure_errors(self):
        bad_payloads = [
            b"not-json",
            b"[]",
            b"{}",
            b'{"material": null}',
            b'{"material": {"accession_no": ""}}',
            b'{"material": {"accession_no": 123}}',
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(FieldTagError):
                    parse_accession_response(payload)

    def test_windows_filename_segment_accepts_normal_accessions(self):
        for value in ("CK", "HWWP-001", "中麦123", "CON2", "A.B.C"):
            with self.subTest(value=value):
                self.assertEqual(validate_windows_filename_segment(value), value)

    def test_windows_filename_segment_rejects_unsafe_values(self):
        unsafe = [
            "",
            ".",
            "..",
            "ABC.",
            "ABC ",
            "A/B",
            "A\\B",
            "A:B",
            "A*B",
            "A\x1fB",
            "CON",
            "con.txt",
            "PRN.csv",
            "COM1",
            "LPT9.data",
        ]
        for value in unsafe:
            with self.subTest(value=value):
                with self.assertRaises(FieldTagError):
                    validate_windows_filename_segment(value)

    def test_api_url_is_fixed(self):
        self.assertEqual(
            build_field_tag_api_url("p12"),
            "https://wheat.cau.edu.cn/seed-inventory/api/v1/field-tags/P12/",
        )


if __name__ == "__main__":
    unittest.main()
