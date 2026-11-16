"""挂牌二维码识别、API 响应解析与 Windows 文件名校验。"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

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


@dataclass(frozen=True)
class FieldTagInfo:
    accession_no: str
    year: int
    location: str
    planting_population: dict | None
    photographer: dict
    is_ck: bool
    code: str
    planting_entry_id: int
    planting_plan_id: int
    row_no: str
    global_material: dict | None
    membership: dict | None
    section: dict | None
    source_reference: dict

    @property
    def filename_prefix(self):
        return validate_windows_filename_segment(f"{self.accession_no}_行{self.row_no}")

    def identity_payload(self):
        return dict(schema_version=2, planting_entry_id=self.planting_entry_id,
                    planting_plan_id=self.planting_plan_id,
                    global_material_id=self.global_material['id'] if self.global_material else None,
                    membership_id=self.membership['id'] if self.membership else None,
                    planting_population_id=self.planting_population['id'] if self.planting_population else None,
                    section_id=self.section['id'] if self.section else None)

    def snapshot(self):
        return asdict(self)


def parse_field_tag_response(payload) -> FieldTagInfo:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise FieldTagError('服务器返回的内容不是有效 JSON') from exc
    required = {'schema_version', 'code', 'planting_entry_id', 'planting_plan_id',
                'current_material_no', 'global_material', 'membership', 'planting_population',
                'section', 'row_no', 'year', 'location', 'photographer', 'is_ck', 'source_reference'}
    if not isinstance(data, dict) or not required <= data.keys() or type(data['schema_version']) is not int or data['schema_version'] != 2:
        raise FieldTagError('挂牌服务缺少新版种植身份字段，请先升级平台接口')
    def positive(value):
        return type(value) is int and value > 0
    for key in ('planting_entry_id', 'planting_plan_id'):
        if not positive(data[key]):
            raise FieldTagError('挂牌缺少有效的种植表或田间行 ID')
    year, location = data['year'], data['location']
    if type(year) is not int or not 1900 <= year <= 2200 or not isinstance(location, str) or not location.strip() or len(location) > 120:
        raise FieldTagError('挂牌缺少有效的种植年份、地点')
    person, population = data['photographer'], data['planting_population']
    if not isinstance(person, dict) or not positive(person.get('id')) or not isinstance(person.get('display_name'), str) or not person['display_name'].strip():
        raise FieldTagError('挂牌服务未返回密钥所有者')
    if type(data['is_ck']) is not bool:
        raise FieldTagError('挂牌类型无效')
    is_ck = data['is_ck']
    material, member, section = data['global_material'], data['membership'], data['section']
    if population is not None and (not isinstance(population, dict) or not positive(population.get('id')) or not isinstance(population.get('name'), str) or not population['name'].strip()):
        raise FieldTagError('本次种植群体字段无效')
    if member is not None and (not isinstance(member, dict) or not positive(member.get('id')) or not population or member.get('population_id') != population['id'] or not isinstance(member.get('accession_no'), str) or not member['accession_no'].strip()):
        raise FieldTagError('当前成员与种植群体不一致')
    if section is not None and (not isinstance(section, dict) or not positive(section.get('id')) or not isinstance(section.get('label'), str) or not isinstance(section.get('description'), str) or (section.get('replicate') is not None and not positive(section['replicate']))):
        raise FieldTagError('种植区段字段无效')
    if not is_ck:
        try:
            if not isinstance(material, dict) or not isinstance(material.get('name'), str):
                raise ValueError
            UUID(material['id'])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise FieldTagError('挂牌缺少有效的全局材料身份') from exc
        if population is not None and member is None:
            raise FieldTagError('种植群体缺少当前成员关联')
        for value in (material, member, population):
            if value is not None and value.get('is_active') is False:
                raise FieldTagError('材料、当前成员或种植群体已停用，不能开始采集')
    number, row_no = data['current_material_no'], data['row_no']
    if not isinstance(number, str) or not number.strip() or len(number) > 160:
        raise FieldTagError('挂牌缺少当前成员编号或独立材料标识')
    if member and number != member['accession_no']:
        raise FieldTagError('挂牌当前编号与成员编号不一致')
    if not isinstance(row_no, str) or not row_no.strip() or len(row_no) > 80:
        raise FieldTagError('挂牌缺少有效田间行号')
    if not isinstance(data['source_reference'], dict):
        raise FieldTagError('挂牌来源字段无效')
    info = FieldTagInfo(number, year, location, population, person, is_ck,
                        normalize_field_tag_code(data['code']), data['planting_entry_id'],
                        data['planting_plan_id'], row_no, material, member, section, data['source_reference'])
    if not is_ck:
        info.filename_prefix
    return info


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
