"""挂牌二维码识别、API 响应解析与 Windows 文件名校验。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


FIELD_TAG_API_URL = (
    "https://wheat.cau.edu.cn/seed-inventory/api/v1/field-tags/{code}/"
)
FIELD_TAG_CODE_RE = re.compile(r"P[0-9A-Z]{1,23}\Z")
QR_DETECT_MAX_SIDE = 2048

_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class FieldTagError(ValueError):
    """可直接显示给用户的挂牌识别/响应错误。"""


@dataclass(frozen=True)
class FieldTagDetection:
    """唯一、有效的挂牌二维码及其预览坐标四边形。"""

    code: str
    points: np.ndarray


def normalize_field_tag_code(value: Any) -> str:
    """规范化并校验挂牌编号。"""
    if not isinstance(value, str):
        raise FieldTagError("二维码内容不是文本")
    code = value.strip().upper()
    if not FIELD_TAG_CODE_RE.fullmatch(code):
        raise FieldTagError("二维码内容不是有效的挂牌编号")
    return code


def _scaled_for_detection(
    bgr: np.ndarray, max_side: int
) -> tuple[np.ndarray, float]:
    if not isinstance(bgr, np.ndarray) or bgr.size == 0 or bgr.ndim not in (2, 3):
        raise FieldTagError("当前预览画面不可用")
    if max_side <= 0:
        raise ValueError("max_side 必须大于 0")
    height, width = bgr.shape[:2]
    scale = min(1.0, float(max_side) / max(height, width))
    if scale == 1.0:
        return bgr, scale
    resized = cv2.resize(
        bgr,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def detect_field_tag_qr(
    bgr: np.ndarray, max_side: int = QR_DETECT_MAX_SIDE
) -> FieldTagDetection:
    """从预览帧识别恰好一个挂牌二维码。

    OpenCV 在不同版本中对没有二维码时可能返回 ``None``、空元组或空数组，
    这里统一转换为面向用户的错误。
    """
    image, scale = _scaled_for_detection(bgr, max_side)
    detector = cv2.QRCodeDetector()
    try:
        ok, decoded_info, points, _ = detector.detectAndDecodeMulti(image)
    except cv2.error as exc:
        raise FieldTagError("二维码识别失败") from exc

    point_array = None if points is None else np.asarray(points, dtype=np.float32)
    count = 0 if point_array is None or point_array.size == 0 else len(point_array)
    if count > 1:
        raise FieldTagError("画面中检测到多个二维码，请只保留一个挂牌二维码")
    if count == 0:
        raise FieldTagError("画面中未找到二维码")
    if not ok or not decoded_info or not str(decoded_info[0]).strip():
        raise FieldTagError("检测到二维码，但无法解码")

    code = normalize_field_tag_code(decoded_info[0])
    quadrilateral = point_array[0].reshape(-1, 2)
    if quadrilateral.shape != (4, 2) or not np.isfinite(quadrilateral).all():
        raise FieldTagError("二维码区域坐标无效")
    if scale != 1.0:
        quadrilateral = quadrilateral / scale
    return FieldTagDetection(code=code, points=quadrilateral.astype(np.float32))


def parse_accession_response(payload: bytes | bytearray | str) -> str:
    """解析 API 成功响应并返回未改写的 ``material.accession_no``。"""
    try:
        if isinstance(payload, (bytes, bytearray)):
            data = json.loads(bytes(payload).decode("utf-8"))
        elif isinstance(payload, str):
            data = json.loads(payload)
        else:
            raise TypeError
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FieldTagError("服务器返回的内容不是有效 JSON") from exc

    material = data.get("material") if isinstance(data, dict) else None
    accession = material.get("accession_no") if isinstance(material, dict) else None
    if not isinstance(accession, str) or not accession.strip():
        raise FieldTagError("服务器响应中缺少有效的 material.accession_no")
    validate_windows_filename_segment(accession)
    return accession


def validate_windows_filename_segment(value: str) -> str:
    """验证字符串可安全作为一个 Windows 文件名段，不做自动替换。"""
    if not isinstance(value, str) or not value:
        raise FieldTagError("品种名为空")
    if value in {".", ".."}:
        raise FieldTagError("品种名不能是 . 或 ..")
    if value[-1] in {" ", "."}:
        raise FieldTagError("品种名不能以空格或句点结尾")
    if any(char in _WINDOWS_FORBIDDEN_CHARS or ord(char) < 32 for char in value):
        raise FieldTagError("品种名包含 Windows 文件名禁用字符")
    # Windows 对保留设备名的判断不区分大小写，并且扩展名前仍然保留。
    stem = value.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise FieldTagError("品种名是 Windows 保留名称")
    if len(value.encode("utf-16-le")) // 2 > 255:
        raise FieldTagError("品种名超过 Windows 文件名长度限制")
    return value


def build_field_tag_api_url(code: str) -> str:
    """为已校验的挂牌编号生成固定 API 地址。"""
    return FIELD_TAG_API_URL.format(code=normalize_field_tag_code(code))
