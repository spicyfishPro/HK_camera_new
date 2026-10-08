"""海康 MVS SDK 封装。

不依赖 Qt,可独立测试。线程安全只保证最低限度(SDK 内部多数接口
本身线程安全),取流和参数读写预期不会并发。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import byref, cast, memset, POINTER, sizeof
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .acquisition_metadata import FrameMetadata
from .paths import MVIMPORT_DIR as _MV_IMPORT_DIR

if str(_MV_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(_MV_IMPORT_DIR))


def _register_mvs_dll_search_path() -> None:
    """Windows 上,把 MVS Runtime 目录注册到 DLL 搜索路径。
    打包后(PyInstaller frozen)PATH 不一定能传过来,必须显式注册。
    dev 模式下也跑一遍,确保多机器一致。"""
    if sys.platform != "win32":
        return
    candidates: list[Path] = []
    # 1. 标准位置(海康 MVS Runtime DLL 装在 Common Files 下)
    candidates.append(Path(r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"))
    candidates.append(Path(r"C:\Program Files\Common Files\MVS\Runtime\Win64_x64"))
    # 2. 环境变量(有些版本 MVS 安装会设)
    env = os.environ.get("MVCAM_COMMON_RUNENV")
    if env:
        candidates.append(Path(env) / "Runtime" / "Win64_x64")
        candidates.append(Path(env) / ".." / "Common Files" / "MVS" / "Runtime" / "Win64_x64")

    for p in candidates:
        try:
            if p.exists() and (p / "MvCameraControl.dll").exists():
                # Python 3.8+ 推荐的 DLL 注册方式
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(p))
                # 同时加到 PATH,给 ctypes 兜底
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")
                logging.getLogger(__name__).info("MVS Runtime DLL 路径已注册: %s", p)
                return
        except OSError:
            continue
    logging.getLogger(__name__).warning(
        "未找到 MVS Runtime 目录。请确认安装 MVS 客户端,或手动把 DLL 路径加到 PATH。"
    )


_register_mvs_dll_search_path()

from MvCameraControl_class import (  # type: ignore  # noqa: E402
    MvCamera,
    MV_CC_DEVICE_INFO,
    MV_CC_DEVICE_INFO_LIST,
    MV_FRAME_OUT,
    MV_SAVE_IMAGE_TO_FILE_PARAM_EX,
    MVCC_FLOATVALUE,
    MVCC_INTVALUE_EX,
    MVCC_ENUMVALUE,
)
from CameraParams_const import (  # type: ignore  # noqa: E402
    MV_ACCESS_Exclusive,
    MV_GIGE_DEVICE,
    MV_USB_DEVICE,
    MV_GENTL_GIGE_DEVICE,
    MV_GENTL_CAMERALINK_DEVICE,
    MV_GENTL_CXP_DEVICE,
    MV_GENTL_XOF_DEVICE,
)
from CameraParams_header import MV_TRIGGER_MODE_OFF  # type: ignore  # noqa: E402
from PixelType_header import (  # type: ignore  # noqa: E402
    PixelType_Gvsp_BayerBG8,
    PixelType_Gvsp_BayerRG8,
    PixelType_Gvsp_BayerGB8,
    PixelType_Gvsp_BayerGR8,
    PixelType_Gvsp_Mono8,
    PixelType_Gvsp_RGB8_Packed,
    PixelType_Gvsp_BGR8_Packed,
    PixelType_Gvsp_YUV422_Packed,
    PixelType_Gvsp_YUV422_YUYV_Packed,
)
from CameraParams_header import (  # type: ignore  # noqa: E402
    MV_Image_Png,
    MV_Image_Jpeg,
    MV_Image_Bmp,
    MV_Image_Tif,
)

log = logging.getLogger(__name__)

_TLAYER_ALL = (
    MV_GIGE_DEVICE | MV_USB_DEVICE | MV_GENTL_CAMERALINK_DEVICE
    | MV_GENTL_CXP_DEVICE | MV_GENTL_XOF_DEVICE
)

_IMG_TYPE_MAP = {
    "png": MV_Image_Png,
    "jpg": MV_Image_Jpeg,
    "jpeg": MV_Image_Jpeg,
    "bmp": MV_Image_Bmp,
    "tif": MV_Image_Tif,
    "tiff": MV_Image_Tif,
}


@dataclass
class DeviceInfo:
    """枚举到的设备摘要。"""

    index: int
    transport: str          # "GigE" / "USB3" / "CXP" / ...
    model: str
    serial: str
    manufacturer: str
    ip: Optional[str] = None  # GigE 才有

    @property
    def display(self) -> str:
        suffix = f" (SN: {self.serial})" if self.serial else ""
        return f"[{self.transport} {self.index}] {self.model}{suffix}"


def _decode(char_array) -> str:
    """海康 SDK 的 ctypes char array → str,容错处理 GBK/UTF-8。"""
    raw = memoryview(char_array).tobytes()
    nul = raw.find(b"\x00")
    if nul != -1:
        raw = raw[:nul]
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def hex_err(code: int) -> str:
    """错误码常用翻译表 + 16 进制兜底。"""
    table = {
        0x00000000: "成功",
        0x80000001: "错误或无效的句柄",
        0x80000002: "不支持的功能",
        0x80000003: "缓存已满",
        0x80000004: "函数调用顺序错误",
        0x80000005: "函数调用所需参数错误",
        0x80000006: "运行时资源申请失败",
        0x80000007: "通用错误(无相机)",
        0x80000008: "未知错误",
        0x80000009: "参数不合理",
        0x8000000A: "未实现",
        0x8000000B: "数据值溢出",
        0x8000000C: "软件触发命令失败",
        0x80000100: "节点未实现",
        0x80000101: "节点不存在",
        0x80000102: "节点不可访问",
        0x80000103: "节点为只读",
        0x80000104: "节点为只写",
        0x80000105: "节点的值超过最大值或者最小值",
        0x80000106: "节点的IInteger类型值不能整除Increment",
        0x80000107: "节点的IEnumeration类型设置的值不在节点的描述列表中",
        0x80000108: "节点缓冲区溢出",
        0x80000109: "对节点参数化失败",
        0x80000110: "节点访问条件错误",
        0x80000111: "时序错误",
        0x80000112: "Genicam通用错误",
        0x80000200: "输入参数不合法",
        0x80000201: "调用接口顺序错误,或没有调用必备的接口",
        0x80000202: "资源申请失败",
        0x80000203: "无数据",
        0x80000204: "调用前函数返回错误",
        0x80000205: "不支持",
        0x80000206: "缓存正在被使用",
        0x80000207: "下载缓存失败",
        0x80000208: "下载缓存超界",
        0x80000300: "未连接相机",
        0x80000301: "相机已经连接",
        0x80000302: "相机未上电",
        0x80000303: "相机不在线",
        0x80000400: "未发现网卡",
        0x80000401: "网卡不可达",
        0x80000402: "驱动不匹配或未安装",
    }
    msg = table.get(code, "未知")
    return f"0x{code:08X}({msg})"


def _pixel_format_name(pix: int) -> str:
    """像素格式枚举值 → 名字(只覆盖常见)。"""
    table = {
        PixelType_Gvsp_Mono8: "Mono8",
        PixelType_Gvsp_BayerBG8: "BayerBG8",
        PixelType_Gvsp_BayerRG8: "BayerRG8",
        PixelType_Gvsp_BayerGB8: "BayerGB8",
        PixelType_Gvsp_BayerGR8: "BayerGR8",
        PixelType_Gvsp_RGB8_Packed: "RGB8",
        PixelType_Gvsp_BGR8_Packed: "BGR8",
        PixelType_Gvsp_YUV422_Packed: "YUV422",
        PixelType_Gvsp_YUV422_YUYV_Packed: "YUV422_YUYV",
    }
    return table.get(pix, f"0x{pix:08X}")


# ============================================================
# SDK 全局初始化(进程级)
# ============================================================

_sdk_initialized = False


def init_sdk() -> None:
    global _sdk_initialized
    if not _sdk_initialized:
        MvCamera.MV_CC_Initialize()
        _sdk_initialized = True
        log.info("MVS SDK initialized")


def finalize_sdk() -> None:
    global _sdk_initialized
    if _sdk_initialized:
        MvCamera.MV_CC_Finalize()
        _sdk_initialized = False
        log.info("MVS SDK finalized")


# ============================================================
# Camera
# ============================================================


class CameraError(Exception):
    def __init__(self, op: str, code: int):
        super().__init__(f"{op} 失败: {hex_err(code)}")
        self.op = op
        self.code = code


class HikCamera:
    """海康相机封装。同一时间只能连接一个设备。"""

    def __init__(self) -> None:
        self._cam: Optional[MvCamera] = None
        self._dev_info_raw = None     # 原始 MV_CC_DEVICE_INFO,用于断线检测
        self._transport: str = ""
        self._device_summary: Optional[DeviceInfo] = None
        self._payload_size: int = 0
        self._grabbing: bool = False

    # ---------------- 设备枚举 ----------------

    @staticmethod
    def enumerate() -> list[DeviceInfo]:
        init_sdk()
        dev_list = MV_CC_DEVICE_INFO_LIST()
        ret = MvCamera.MV_CC_EnumDevices(_TLAYER_ALL, dev_list)
        if ret != 0:
            raise CameraError("枚举设备", ret)

        result: list[DeviceInfo] = []
        for i in range(dev_list.nDeviceNum):
            info = cast(dev_list.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
            t = info.nTLayerType
            if t == MV_GIGE_DEVICE or t == MV_GENTL_GIGE_DEVICE:
                gi = info.SpecialInfo.stGigEInfo
                ip = ".".join(str((gi.nCurrentIp >> shift) & 0xFF) for shift in (24, 16, 8, 0))
                result.append(DeviceInfo(
                    index=i, transport="GigE",
                    model=_decode(gi.chModelName),
                    serial=_decode(gi.chSerialNumber),
                    manufacturer=_decode(gi.chManufacturerName),
                    ip=ip,
                ))
            elif t == MV_USB_DEVICE:
                ui = info.SpecialInfo.stUsb3VInfo
                result.append(DeviceInfo(
                    index=i, transport="USB3",
                    model=_decode(ui.chModelName),
                    serial=_decode(ui.chSerialNumber),
                    manufacturer=_decode(ui.chManufacturerName),
                ))
            else:
                # CXP / XoF / CameraLink 也列出来,字段尽力填
                result.append(DeviceInfo(
                    index=i, transport="Other",
                    model="UnknownTransport",
                    serial="",
                    manufacturer="",
                ))
        return result

    # ---------------- 连接 / 断开 ----------------

    def connect(self, dev_index: int) -> DeviceInfo:
        if self._cam is not None:
            raise CameraError("连接", -1)

        init_sdk()
        dev_list = MV_CC_DEVICE_INFO_LIST()
        ret = MvCamera.MV_CC_EnumDevices(_TLAYER_ALL, dev_list)
        if ret != 0:
            raise CameraError("枚举设备", ret)
        if dev_index >= dev_list.nDeviceNum:
            raise CameraError(f"设备索引越界({dev_index}/{dev_list.nDeviceNum})", -1)

        info = cast(dev_list.pDeviceInfo[dev_index], POINTER(MV_CC_DEVICE_INFO)).contents

        cam = MvCamera()
        ret = cam.MV_CC_CreateHandle(info)
        if ret != 0:
            raise CameraError("创建句柄", ret)

        ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
        if ret != 0:
            cam.MV_CC_DestroyHandle()
            raise CameraError("打开设备", ret)

        # GigE 调整包大小
        if info.nTLayerType in (MV_GIGE_DEVICE, MV_GENTL_GIGE_DEVICE):
            ps = cam.MV_CC_GetOptimalPacketSize()
            if ps > 0:
                rc = cam.MV_CC_SetIntValue("GevSCPSPacketSize", ps)
                if rc != 0:
                    log.warning("设置最佳包大小失败: %s", hex_err(rc))

        # 触发模式关
        ret = cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
        if ret != 0:
            log.warning("关闭触发模式失败: %s", hex_err(ret))

        # 读 PayloadSize
        param = MVCC_INTVALUE_EX()
        memset(byref(param), 0, sizeof(param))
        rc = cam.MV_CC_GetIntValueEx("PayloadSize", param)
        if rc == 0:
            self._payload_size = int(param.nCurValue)

        self._cam = cam
        self._dev_info_raw = info
        self._transport = "GigE" if info.nTLayerType in (MV_GIGE_DEVICE, MV_GENTL_GIGE_DEVICE) else (
            "USB3" if info.nTLayerType == MV_USB_DEVICE else "Other"
        )

        # 摘要返回上层
        summary = self.enumerate()[dev_index]
        self._device_summary = summary
        log.info("连接成功: %s", summary.display)
        return summary

    def disconnect(self) -> None:
        if self._cam is None:
            return
        if self._grabbing:
            try:
                self.stop_grabbing()
            except Exception as e:
                log.warning("停止取流时出错: %s", e)
        try:
            self._cam.MV_CC_CloseDevice()
        finally:
            self._cam.MV_CC_DestroyHandle()
            self._cam = None
            self._dev_info_raw = None
            self._device_summary = None
            self._grabbing = False
            log.info("已断开连接")

    def is_connected(self) -> bool:
        if self._cam is None:
            return False
        try:
            return bool(self._cam.MV_CC_IsDeviceConnected())
        except Exception:
            return False

    @property
    def device(self) -> Optional[DeviceInfo]:
        return self._device_summary

    # ---------------- 取流 ----------------

    def set_latest_frame_strategy(self) -> bool:
        """在取流所属线程、StartGrabbing 之前配置最新帧策略。"""
        if self._cam is None:
            return False
        try:
            ret = self._cam.MV_CC_SetGrabStrategy(1)  # LatestImagesOnly
            if ret != 0:
                log.warning("最新帧策略不可用: %s；不启用取流限速", hex_err(ret))
            return ret == 0
        except (AttributeError, OSError):
            log.warning("MVS 不支持最新帧策略；不启用取流限速")
            return False

    def clear_image_buffer(self) -> bool:
        """由取流线程在抓拍请求后清理 SDK 中请求前的缓存帧。"""
        if self._cam is None:
            return False
        try:
            return self._cam.MV_CC_ClearImageBuffer() == 0
        except (AttributeError, OSError):
            return False

    def start_grabbing(self) -> None:
        if self._cam is None:
            raise CameraError("开始取流", -1)
        if self._grabbing:
            return
        ret = self._cam.MV_CC_StartGrabbing()
        if ret != 0:
            raise CameraError("开始取流", ret)
        self._grabbing = True

    def stop_grabbing(self) -> None:
        if self._cam is None or not self._grabbing:
            return
        ret = self._cam.MV_CC_StopGrabbing()
        self._grabbing = False
        if ret != 0:
            raise CameraError("停止取流", ret)

    @property
    def is_grabbing(self) -> bool:
        return self._grabbing

    def grab_frame_bgr(self, timeout_ms: int = 1000) -> Optional[tuple[np.ndarray, "MV_FRAME_OUT"]]:
        """取一帧 → (BGR ndarray, 原始 frame 对象)。失败返回 None。
        调用方拿到原始 frame 后,如果**不**还要再用,**必须**自己调用 free_frame。
        典型用法:取流循环只要 ndarray;抓拍保存可以拿原始 frame 给 save_frame_to_file 用,然后 free。
        """
        if self._cam is None:
            return None
        frame = MV_FRAME_OUT()
        memset(byref(frame), 0, sizeof(frame))
        ret = self._cam.MV_CC_GetImageBuffer(frame, timeout_ms)
        if ret != 0 or not frame.pBufAddr:
            return None
        try:
            arr = self._frame_to_bgr(frame)
            return arr, frame
        except Exception:
            self._cam.MV_CC_FreeImageBuffer(frame)
            raise

    def free_frame(self, frame: "MV_FRAME_OUT") -> None:
        if self._cam is not None and frame is not None:
            self._cam.MV_CC_FreeImageBuffer(frame)

    @staticmethod
    def copy_frame_metadata(frame: "MV_FRAME_OUT") -> FrameMetadata:
        """在释放 SDK 缓冲前复制逐帧信息，返回值不再依赖 ctypes 指针。"""
        return FrameMetadata.from_mvs_frame(frame, _pixel_format_name)

    def _frame_to_bgr(self, frame: "MV_FRAME_OUT") -> np.ndarray:
        import cv2

        w = frame.stFrameInfo.nWidth
        h = frame.stFrameInfo.nHeight
        pix = frame.stFrameInfo.enPixelType
        n_bytes = frame.stFrameInfo.nFrameLen

        buf = (ctypes.c_ubyte * n_bytes)()
        ctypes.memmove(buf, frame.pBufAddr, n_bytes)
        raw = np.frombuffer(buf, dtype=np.uint8)

        if pix == PixelType_Gvsp_Mono8:
            img = raw.reshape(h, w)
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        # GenICam Bayer 命名(以左上角像素为准) 与 OpenCV 命名(以第 2 行第 2 列为准)
        # 互为"R/B 翻转"。下表为正确对应关系:
        #   GenICam BayerBG (左上 B) → OpenCV BayerRG
        #   GenICam BayerRG (左上 R) → OpenCV BayerBG
        #   GenICam BayerGB           → OpenCV BayerGR
        #   GenICam BayerGR           → OpenCV BayerGB
        if pix == PixelType_Gvsp_BayerBG8:
            return cv2.cvtColor(raw.reshape(h, w), cv2.COLOR_BayerRG2BGR)
        if pix == PixelType_Gvsp_BayerRG8:
            return cv2.cvtColor(raw.reshape(h, w), cv2.COLOR_BayerBG2BGR)
        if pix == PixelType_Gvsp_BayerGB8:
            return cv2.cvtColor(raw.reshape(h, w), cv2.COLOR_BayerGR2BGR)
        if pix == PixelType_Gvsp_BayerGR8:
            return cv2.cvtColor(raw.reshape(h, w), cv2.COLOR_BayerGB2BGR)
        if pix == PixelType_Gvsp_RGB8_Packed:
            return cv2.cvtColor(raw.reshape(h, w, 3), cv2.COLOR_RGB2BGR)
        if pix == PixelType_Gvsp_BGR8_Packed:
            return raw.reshape(h, w, 3).copy()
        if pix == PixelType_Gvsp_YUV422_Packed:
            return cv2.cvtColor(raw.reshape(h, w, 2), cv2.COLOR_YUV2BGR_UYVY)
        if pix == PixelType_Gvsp_YUV422_YUYV_Packed:
            return cv2.cvtColor(raw.reshape(h, w, 2), cv2.COLOR_YUV2BGR_YUYV)
        # 不识别 → 灰度兜底
        log.warning("未识别像素格式 %s,按灰度处理", _pixel_format_name(pix))
        return cv2.cvtColor(raw.reshape(h, -1), cv2.COLOR_GRAY2BGR)

    # ---------------- 保存 ----------------

    def save_frame_to_file(self, frame: "MV_FRAME_OUT", path: str | Path,
                            quality: int = 95) -> None:
        """用 SDK 内置接口把帧存为图片,根据扩展名选格式。"""
        if self._cam is None:
            raise CameraError("保存图像", -1)
        path = Path(path)
        ext = path.suffix.lstrip(".").lower()
        if ext not in _IMG_TYPE_MAP:
            raise ValueError(f"不支持的图片格式: .{ext}")

        # SDK 需要 ANSI 路径,中文路径走 fallback(转 cv2 写)
        try:
            c_path = str(path).encode("ascii")
        except UnicodeEncodeError:
            self._save_via_cv2(frame, path, quality)
            return

        param = MV_SAVE_IMAGE_TO_FILE_PARAM_EX()
        param.enPixelType = frame.stFrameInfo.enPixelType
        param.nWidth = frame.stFrameInfo.nWidth
        param.nHeight = frame.stFrameInfo.nHeight
        param.nDataLen = frame.stFrameInfo.nFrameLen
        param.pData = frame.pBufAddr
        param.enImageType = _IMG_TYPE_MAP[ext]
        param.pcImagePath = ctypes.create_string_buffer(c_path)
        param.iMethodValue = 1
        param.nQuality = quality
        ret = self._cam.MV_CC_SaveImageToFileEx(param)
        if ret != 0:
            raise CameraError("保存图像", ret)

    def _save_via_cv2(self, frame: "MV_FRAME_OUT", path: Path, quality: int) -> None:
        """中文路径 fallback:转 numpy 再用 cv2 写。"""
        import cv2

        bgr = self._frame_to_bgr(frame)
        ext = path.suffix.lower()
        # cv2.imwrite 不支持中文路径,用 imencode + ndarray.tofile
        params: list[int] = []
        if ext in (".jpg", ".jpeg"):
            params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
        encoded, buf = cv2.imencode(ext, bgr, params)
        if not encoded:
            raise CameraError("cv2 编码保存", -1)
        buf.tofile(str(path))

    # ---------------- 参数读写 ----------------

    def get_float(self, key: str) -> Optional[tuple[float, float, float]]:
        """返回 (cur, min, max),失败返回 None。"""
        if self._cam is None:
            return None
        v = MVCC_FLOATVALUE()
        memset(byref(v), 0, sizeof(v))
        ret = self._cam.MV_CC_GetFloatValue(key, v)
        if ret != 0:
            log.info("读 float %s 失败: %s", key, hex_err(ret))
            return None
        return float(v.fCurValue), float(v.fMin), float(v.fMax)

    def set_float(self, key: str, value: float) -> None:
        if self._cam is None:
            raise CameraError("写参数", -1)
        ret = self._cam.MV_CC_SetFloatValue(key, float(value))
        if ret != 0:
            raise CameraError(f"设置 {key}", ret)

    def get_int(self, key: str) -> Optional[tuple[int, int, int]]:
        if self._cam is None:
            return None
        v = MVCC_INTVALUE_EX()
        memset(byref(v), 0, sizeof(v))
        ret = self._cam.MV_CC_GetIntValueEx(key, v)
        if ret != 0:
            log.info("读 int %s 失败: %s", key, hex_err(ret))
            return None
        return int(v.nCurValue), int(v.nMin), int(v.nMax)

    def set_int(self, key: str, value: int) -> None:
        if self._cam is None:
            raise CameraError("写参数", -1)
        ret = self._cam.MV_CC_SetIntValueEx(key, int(value))
        if ret != 0:
            raise CameraError(f"设置 {key}", ret)

    def get_enum(self, key: str) -> Optional[int]:
        if self._cam is None:
            return None
        v = MVCC_ENUMVALUE()
        memset(byref(v), 0, sizeof(v))
        ret = self._cam.MV_CC_GetEnumValue(key, v)
        if ret != 0:
            log.debug("读 enum %s 失败: %s", key, hex_err(ret))
            return None
        return int(v.nCurValue)

    def set_enum(self, key: str, value: int) -> None:
        if self._cam is None:
            raise CameraError("写参数", -1)
        ret = self._cam.MV_CC_SetEnumValue(key, int(value))
        if ret != 0:
            raise CameraError(f"设置 {key}", ret)

    def set_enum_string(self, key: str, value: str) -> None:
        """按字符串设枚举(比数值依赖更稳;BalanceWhiteAuto/TriggerMode 等推荐用这个)。"""
        if self._cam is None:
            raise CameraError("写参数", -1)
        ret = self._cam.MV_CC_SetEnumValueByString(key, value)
        if ret != 0:
            raise CameraError(f"设置 {key}={value}", ret)

    def get_bool(self, key: str) -> Optional[bool]:
        if self._cam is None:
            return None
        v = ctypes.c_bool(False)
        ret = self._cam.MV_CC_GetBoolValue(key, v)
        if ret != 0:
            log.debug("读 bool %s 失败: %s", key, hex_err(ret))
            return None
        return bool(v.value)

    def set_bool(self, key: str, value: bool) -> None:
        if self._cam is None:
            raise CameraError("写参数", -1)
        ret = self._cam.MV_CC_SetBoolValue(key, bool(value))
        if ret != 0:
            raise CameraError(f"设置 {key}={value}", ret)

    def pixel_format_name(self) -> str:
        v = self.get_enum("PixelFormat")
        if v is None:
            return "—"
        return _pixel_format_name(v)
