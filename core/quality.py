"""BRISQUE 无参考图像质量评估。

封装 OpenCV contrib 的 `cv2.quality.QualityBRISQUE`:
- 模型文件位于 models/brisque_*.yml
- 评分前**先降采样到 LONG_SIDE_TARGET 像素**,把 25MP 图的耗时从 ~1500ms 压到 ~150ms
- 分数低 = 质量好;> 50 视为不合格(用户配置可调)

BRISQUE 实例**线程安全约定**:cv2.quality.QualityBRISQUE 不保证内部线程安全,
所以每个线程持有自己的实例。`BRISQUEScorer.score_ndarray` 就在调用线程算。
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .paths import BRISQUE_MODEL as _MODEL_FILE, BRISQUE_RANGE as _RANGE_FILE

log = logging.getLogger(__name__)


# ---------------- ASCII 安全路径(修中文安装路径下 BRISQUE 无法加载) ----------------
# cv2.quality.QualityBRISQUE_create 内部用 OpenCV FileStorage 读 yml,
# 在 Windows 上读非 ASCII 路径会失败 → BRISQUE 初始化失败 → 全程无评分。
# 解决:模型路径含非 ASCII 时,先转 Windows 短路径(8.3),仍不行则复制到 ASCII 临时目录。

def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _short_path(p: Path) -> str:
    """Windows 8.3 短路径(纯 ASCII);失败或非 win 返回原字符串。"""
    s = str(p)
    if sys.platform != "win32":
        return s
    try:
        import ctypes
        from ctypes import wintypes
        _get = ctypes.windll.kernel32.GetShortPathNameW
        _get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        _get.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if _get(s, buf, 1024):
            return buf.value
    except Exception:
        log.debug("GetShortPathName 失败: %s", s, exc_info=True)
    return s


def _ascii_temp_dir() -> Path:
    """返回一个纯 ASCII 的可写目录(优先短路径 temp,再 ProgramData,最后 C 盘)。"""
    candidates = [
        Path(_short_path(Path(tempfile.gettempdir()))),
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RGBCapture",
        Path(r"C:\RGBCapture_tmp"),
    ]
    for c in candidates:
        if not _is_ascii(str(c)):
            continue
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except Exception:
            continue
    fallback = Path(tempfile.gettempdir())
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _ascii_safe_model_paths(model: Path, rng: Path) -> tuple[str, str]:
    """返回 cv2 能读的 (model, range) 路径字符串,必要时复制到 ASCII 目录。"""
    sm, sr = str(model), str(rng)
    if _is_ascii(sm) and _is_ascii(sr):
        return sm, sr
    # 先试短路径
    short_m, short_r = _short_path(model), _short_path(rng)
    if _is_ascii(short_m) and _is_ascii(short_r):
        log.info("BRISQUE 模型路径含非 ASCII,改用短路径加载")
        return short_m, short_r
    # 复制到 ASCII 临时目录
    dst = _ascii_temp_dir()
    m2 = dst / "brisque_model.yml"
    r2 = dst / "brisque_range.yml"
    shutil.copyfile(model, m2)
    shutil.copyfile(rng, r2)
    log.info("BRISQUE 模型路径含非 ASCII,已复制到 %s 加载", dst)
    return str(m2), str(r2)

# 评分前的降采样目标(长边像素)
# 与 BRISQUE 论文常见做法一致,512-1024 之间都行,1024 在工业相机分辨率下经验比较稳。
LONG_SIDE_TARGET: int = 1024

# 默认不合格阈值。BRISQUE 在 [0, 100] 之间,数值越低越好。
DEFAULT_THRESHOLD: float = 65.0


class BRISQUEScorer:
    """轻量封装。每个使用方持有一个实例(实例化便宜,但 compute 不线程安全)。"""

    def __init__(
        self,
        model: Path | None = None,
        rng: Path | None = None,
        long_side_target: int = LONG_SIDE_TARGET,
    ) -> None:
        self._model_path = model or _MODEL_FILE
        self._range_path = rng or _RANGE_FILE
        if not self._model_path.exists() or not self._range_path.exists():
            raise FileNotFoundError(
                f"BRISQUE 模型文件缺失: {self._model_path}, {self._range_path}"
            )
        # ASCII 安全:中文安装路径下直接传会让 OpenCV 读不到模型 → 无评分
        model_str, range_str = _ascii_safe_model_paths(
            self._model_path, self._range_path,
        )
        self._brisque = cv2.quality.QualityBRISQUE_create(model_str, range_str)
        self._long_side = long_side_target
        # compute 不保证线程安全;非工作流下连续快拍可能两条线程同时评分,
        # 用锁串行化(评分很短且稀疏,串行几乎无感)。
        self._compute_lock = threading.Lock()

    # ---------------- 评分 ----------------

    def _prepare(self, bgr: np.ndarray) -> np.ndarray:
        """降采样至长边 = LONG_SIDE_TARGET(若已小于则跳过)。返回新数组,不动入参。"""
        h, w = bgr.shape[:2]
        long_side = max(h, w)
        if long_side <= self._long_side:
            return bgr
        scale = self._long_side / long_side
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        return cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)

    def prepare_ndarray(self, bgr: np.ndarray) -> np.ndarray:
        """生成与全分辨率/复用旋转缓冲脱离的评分快照。"""
        arr = self._prepare(bgr)
        # 原图已小于目标时 _prepare 会返回入参本身；这里仍需
        # 复制，否则下一次抓拍可能覆写 ReusableImageRotator 的缓冲。
        return np.ascontiguousarray(arr).copy() if arr is bgr else arr

    def score_prepared(self, bgr: np.ndarray) -> Optional[float]:
        """对已准备好的小图评分，不再读取全分辨率缓冲。"""
        if bgr is None or bgr.size == 0:
            return None
        try:
            with self._compute_lock:
                result = self._brisque.compute(bgr)
            return float(result[0])
        except Exception:
            log.exception("BRISQUE compute 失败")
            return None

    def score_ndarray(self, bgr: np.ndarray) -> Optional[float]:
        """在内存里的 BGR 图算 BRISQUE。失败返回 None。"""
        if bgr is None or bgr.size == 0:
            return None
        return self.score_prepared(self.prepare_ndarray(bgr))

    def score_file(self, path: Path | str) -> Optional[float]:
        """从磁盘读图后评分。中文路径用 np.fromfile + imdecode 兜底。"""
        path = Path(path)
        if not path.exists():
            log.warning("BRISQUE 读取失败 — 文件不存在: %s", path)
            return None
        try:
            data = np.fromfile(str(path), dtype=np.uint8)
            bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            log.exception("BRISQUE 读图失败: %s", path)
            return None
        if bgr is None:
            log.warning("BRISQUE 解码失败: %s", path)
            return None
        return self.score_ndarray(bgr)


def is_pass(score: float, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """BRISQUE 越低越好。score <= threshold 视为合格。"""
    return score <= threshold
