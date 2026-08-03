"""采集帧元数据、XMP 序列化与一次编码保存。

本模块刻意不接收任意字典作为顶层输入：``CaptureMetadata.to_dict``
只输出显式声明的采集字段，避免 Bearer、HTTP 头或响应正文意外进入图片。
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from xml.sax.saxutils import escape

import cv2
import numpy as np

from .build_info import get_build_info


log = logging.getLogger(__name__)

SCHEMA_VERSION = "wheat.capture.v1"
XMP_NAMESPACE = "https://wheat.cau.edu.cn/ns/rgb-capture/1.0/"


@dataclass(frozen=True)
class FrameMetadata:
    """脱离 MVS 缓冲区后仍然有效的逐帧信息副本。"""

    received_at: str
    width: int
    height: int
    pixel_type_code: int
    pixel_format: str
    frame_number: int
    frame_length: int
    device_timestamp: int | None
    host_timestamp: int | None
    exposure_time_us: float | None
    gain: float | None
    average_brightness: int | None
    white_balance_red: int | None
    white_balance_green: int | None
    white_balance_blue: int | None
    frame_counter: int | None
    trigger_index: int | None
    offset_x: int
    offset_y: int
    lost_packets: int | None
    second_count: int | None
    cycle_count: int | None
    cycle_offset: int | None

    @classmethod
    def from_mvs_frame(cls, frame, pixel_format_name) -> "FrameMetadata":
        """从 duck-typed ``MV_FRAME_OUT`` 复制信息，不持有任何 SDK 指针。"""
        info = frame.stFrameInfo

        def nonzero_int(value) -> int | None:
            number = int(value)
            return number if number != 0 else None

        def finite_float(value, *, positive: bool = False) -> float | None:
            number = float(value)
            if not math.isfinite(number) or (positive and number <= 0):
                return None
            return number

        device_timestamp_value = (
            (int(info.nDevTimeStampHigh) << 32) | int(info.nDevTimeStampLow)
        )
        exposure_time = finite_float(info.fExposureTime, positive=True)
        watermark_available = bool(
            exposure_time is not None
            or int(info.nRed) or int(info.nGreen) or int(info.nBlue)
            or int(info.nSecondCount) or int(info.nCycleCount)
            or int(info.nCycleOffset) or int(info.nFrameCounter)
            or int(info.nTriggerIndex)
        )
        return cls(
            received_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            width=int(info.nExtendWidth) or int(info.nWidth),
            height=int(info.nExtendHeight) or int(info.nHeight),
            pixel_type_code=int(info.enPixelType),
            pixel_format=pixel_format_name(int(info.enPixelType)),
            frame_number=int(info.nFrameNum),
            frame_length=int(info.nFrameLenEx) or int(info.nFrameLen),
            device_timestamp=(device_timestamp_value or None),
            host_timestamp=nonzero_int(info.nHostTimeStamp),
            exposure_time_us=exposure_time,
            # 0 dB 是本项目的正常设置，不能用 0 判断“未提供”。
            gain=finite_float(info.fGain) if watermark_available else None,
            average_brightness=(
                int(info.nAverageBrightness) if watermark_available else None
            ),
            white_balance_red=nonzero_int(info.nRed),
            white_balance_green=nonzero_int(info.nGreen),
            white_balance_blue=nonzero_int(info.nBlue),
            frame_counter=(int(info.nFrameCounter) if watermark_available else None),
            trigger_index=(int(info.nTriggerIndex) if watermark_available else None),
            offset_x=int(info.nOffsetX),
            offset_y=int(info.nOffsetY),
            lost_packets=int(info.nLostPacket),
            second_count=nonzero_int(info.nSecondCount),
            cycle_count=nonzero_int(info.nCycleCount),
            cycle_offset=nonzero_int(info.nCycleOffset),
        )


@dataclass(frozen=True)
class FrameSnapshot:
    """全分辨率未 FFC 的 BGR 图与同一帧的元数据。"""

    bgr: np.ndarray
    metadata: FrameMetadata

    def copy(self) -> "FrameSnapshot":
        return FrameSnapshot(self.bgr.copy(), self.metadata)


@dataclass(frozen=True)
class CaptureMetadata:
    capture_id: str
    captured_at: str
    accession_no: str
    prefix_source: Literal["manual", "qr"]
    field_tag_code: str | None
    mode: Literal["wheat", "grain"]
    workflow_active: bool
    rep: int | None
    side: str | None
    filename: str
    image_variant: Literal["processed", "unffc_demosaiced"]
    device_manufacturer: str | None
    device_model: str | None
    device_serial: str | None
    device_transport: str | None
    device_ip: str | None
    frame: FrameMetadata
    exposure_readback_us: float | None
    gain_readback: float | None
    exposure_auto_code: int | None
    gain_auto_code: int | None
    trigger_mode_code: int | None
    white_balance_mode: str | None
    white_balance_red: int | None
    white_balance_green: int | None
    white_balance_blue: int | None
    brightness: int | None
    gamma: float | None
    saturation: int | None
    contrast: int | None
    sharpness: int | None
    output_format: str
    jpeg_quality: int | None
    png_compression: int | None
    tiff_compression: str | None
    rotation_deg: int
    ffc_applied: bool
    ffc_has_flat: bool
    ffc_has_dark: bool
    ffc_calibration_id: str | None
    output_width: int
    output_height: int

    @classmethod
    def new_capture_id(cls) -> str:
        return str(uuid4())

    def to_dict(self) -> dict[str, object]:
        build = get_build_info()
        pixel_format = self.frame.pixel_format
        demosaic_map = {
            "BayerBG8": "GenICam BayerBG8 -> OpenCV COLOR_BayerRG2BGR",
            "BayerRG8": "GenICam BayerRG8 -> OpenCV COLOR_BayerBG2BGR",
            "BayerGB8": "GenICam BayerGB8 -> OpenCV COLOR_BayerGR2BGR",
            "BayerGR8": "GenICam BayerGR8 -> OpenCV COLOR_BayerGB2BGR",
        }
        demosaic = demosaic_map.get(pixel_format)
        return {
            "schema_version": SCHEMA_VERSION,
            "capture_id": self.capture_id,
            "captured_at": self.captured_at,
            "sample": {
                "accession_no": self.accession_no,
                "prefix_source": self.prefix_source,
                "field_tag_code": self.field_tag_code,
            },
            "workflow": {
                "mode": self.mode,
                "active": self.workflow_active,
                "rep": self.rep,
                "side": self.side,
            },
            "image": {
                "filename": self.filename,
                "variant": self.image_variant,
                "format": self.output_format,
                "width": self.output_width,
                "height": self.output_height,
                "stored_color_model": "RGB",
                "bits_per_channel": 8,
                "encoding": {
                    "jpeg_quality": self.jpeg_quality,
                    "png_compression": self.png_compression,
                    "tiff_compression": self.tiff_compression,
                },
            },
            "camera": {
                "manufacturer": self.device_manufacturer,
                "model": self.device_model,
                "serial": self.device_serial,
                "transport": self.device_transport,
                "ip": self.device_ip,
                "frame": asdict(self.frame),
                "readback": {
                    "exposure_time_us": self.exposure_readback_us,
                    "gain": self.gain_readback,
                    "exposure_auto_code": self.exposure_auto_code,
                    "gain_auto_code": self.gain_auto_code,
                    "trigger_mode_code": self.trigger_mode_code,
                },
            },
            "settings": {
                "white_balance": {
                    "mode": self.white_balance_mode,
                    "red": self.white_balance_red,
                    "green": self.white_balance_green,
                    "blue": self.white_balance_blue,
                },
                "brightness": self.brightness,
                "gamma": self.gamma,
                "saturation": self.saturation,
                "contrast": self.contrast,
                "sharpness": self.sharpness,
            },
            "processing": {
                "source_pixel_format": pixel_format,
                "demosaiced": demosaic is not None,
                "demosaic_mapping": demosaic,
                "native_bayer_saved": False,
                "ffc_applied": self.ffc_applied,
                "ffc_has_flat": self.ffc_has_flat,
                "ffc_has_dark": self.ffc_has_dark,
                "ffc_calibration_id": self.ffc_calibration_id,
                "rotation_deg": self.rotation_deg,
            },
            "software": {
                "name": "RGB Capture",
                "version": build.app_version,
                "built_at_utc": build.built_at_utc,
                "git_commit": build.git_commit,
            },
        }

    def to_xmp_bytes(self) -> bytes:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        created = escape(self.captured_at)
        identifier = escape(self.capture_id)
        creator = escape(f"RGB Capture {get_build_info().app_version}")
        json_xml = escape(payload)
        xmp = (
            '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:rgbc="{XMP_NAMESPACE}">'
            f'<xmp:CreateDate>{created}</xmp:CreateDate>'
            f'<xmp:CreatorTool>{creator}</xmp:CreatorTool>'
            f'<dc:identifier><rdf:Bag><rdf:li>{identifier}</rdf:li></rdf:Bag></dc:identifier>'
            f'<rgbc:CaptureMetadata>{json_xml}</rgbc:CaptureMetadata>'
            '</rdf:Description></rdf:RDF></x:xmpmeta>\n'
            '<?xpacket end="w"?>'
        )
        return xmp.encode("utf-8")


@dataclass(frozen=True)
class SaveResult:
    path: Path
    metadata_embedded: bool
    metadata_format: str | None
    warning: str | None = None


def _encoding_params(ext: str) -> list[int]:
    if ext in (".jpg", ".jpeg"):
        return [cv2.IMWRITE_JPEG_QUALITY, 92, cv2.IMWRITE_JPEG_OPTIMIZE, 0]
    if ext == ".png":
        return [cv2.IMWRITE_PNG_COMPRESSION, 1]
    if ext in (".tif", ".tiff"):
        return [cv2.IMWRITE_TIFF_COMPRESSION, 1]
    raise ValueError(f"不支持的图片格式: {ext}")


def save_bgr_plain(bgr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    ok, buf = cv2.imencode(ext, bgr, _encoding_params(ext))
    if not ok:
        raise RuntimeError(f"cv2.imencode 失败({ext})")
    buf.tofile(str(path))


def _save_jpeg_or_png_with_xmp(
    bgr: np.ndarray, path: Path, xmp: bytes,
) -> None:
    encoder = getattr(cv2, "imencodeWithMetadata", None)
    metadata_type = getattr(cv2, "IMAGE_METADATA_XMP", None)
    if encoder is None or metadata_type is None:
        raise RuntimeError("当前 OpenCV 不支持 XMP 元数据编码")
    ext = path.suffix.lower()
    # OpenCV 4.13 的 PNG 后端把 XMP 当 C 字符串交给 libpng；显式补 NUL
    # 防止编码器读取到 numpy 缓冲末尾之后的随机字节。NUL 本身不会写进 iTXt。
    metadata_payload = xmp + b"\x00" if ext == ".png" else xmp
    metadata = np.frombuffer(metadata_payload, dtype=np.uint8)
    ok, buf = encoder(
        ext, bgr, [metadata_type], [metadata], _encoding_params(ext),
    )
    if not ok:
        raise RuntimeError("OpenCV XMP 元数据编码失败")
    buf.tofile(str(path))


def _save_tiff_with_xmp(bgr: np.ndarray, path: Path, xmp: bytes) -> None:
    import tifffile

    # TIFF photometric=RGB 要求通道次序为 RGB；这是首次且唯一一次编码。
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    tifffile.imwrite(
        str(path),
        rgb,
        photometric="rgb",
        compression=None,
        metadata=None,
        software=f"RGB Capture {get_build_info().app_version}",
        extratags=[(700, "B", len(xmp), xmp, False)],
    )


def save_bgr_with_metadata(
    bgr: np.ndarray, path: Path, metadata: CaptureMetadata,
) -> SaveResult:
    """单次编码写入图片和 XMP；失败时安全回退为普通图片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
        raise ValueError(f"不支持的图片格式: {ext}")

    try:
        xmp = metadata.to_xmp_bytes()
        if ext in (".tif", ".tiff"):
            _save_tiff_with_xmp(bgr, path, xmp)
            metadata_format = "TIFF tag 700 (XMP)"
        else:
            _save_jpeg_or_png_with_xmp(bgr, path, xmp)
            metadata_format = "XMP"
        return SaveResult(path, True, metadata_format)
    except Exception as exc:
        # 不把对象/请求上下文写入日志，只记录异常类型与安全固定文案。
        log.warning(
            "metadata embedding failed for %s (%s); saving plain image",
            path.name, type(exc).__name__,
        )
        save_bgr_plain(bgr, path)
        return SaveResult(
            path=path,
            metadata_embedded=False,
            metadata_format=None,
            warning="照片已保存，但未写入采集元数据",
        )
