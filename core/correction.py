"""平场校正(Flat-Field Correction, FFC) — 暗场 + 亮场两步法。

教科书数学:
    采集:
        D(x,y) = 盖镜头拍多帧取平均 — 相机暗电流 + 读出噪声
        F(x,y) = 均匀白场拍多帧取平均 — 镜头暗角 + PRNU + 光照不均
    计算 gain map(只在设置参考时算一次):
        signal(x,y) = F(x,y) - D(x,y)
        gain(x,y)   = mean(signal) / signal(x,y)
    应用(每帧):
        corrected(x,y) = (raw(x,y) - D(x,y)) * gain(x,y)

向后兼容:
    - 暗场可缺省 — 缺省时按 D=0 算,退化为纯白场校正(老版本行为)
    - 亮场缺省时 has_reference=False, 不做任何校正
    - 老存档(只有 'gain' + 'mean')仍能加载;新存档同时存 'flat' + 'dark'
"""
from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorrectionSnapshot:
    enabled: bool
    gain: np.ndarray | None
    dark: np.ndarray | None
    reference_id: str | None

    def apply(self, bgr: np.ndarray) -> np.ndarray:
        if not self.enabled or self.gain is None:
            return bgr
        if bgr.shape != self.gain.shape:
            raise ValueError("照片尺寸与拍摄时的 FFC 参考不一致")
        work = bgr.astype(np.float32)
        if self.dark is not None:
            np.subtract(work, self.dark, out=work)
        np.multiply(work, self.gain, out=work)
        np.clip(work, 0, 255, out=work)
        return work.astype(np.uint8)


class FlatFieldCorrection:
    """每个 CaptureWidget 持有一个。
    线程安全约定:写操作(set_*/clear)只在主线程;读(apply_*)在工作线程。
    引用替换是 GIL 原子的,不会读到半成品 gain map。
    """

    def __init__(self) -> None:
        # 原始参考帧(uint8 BGR, 与相机原始 shape 一致)
        self._flat_full: Optional[np.ndarray] = None
        self._dark_full: Optional[np.ndarray] = None
        # 派生:gain map(float32, 与帧 shape 一致),由 _recompute 维护
        self._gain_full: Optional[np.ndarray] = None
        # 显示尺寸缓存
        self._gain_display: Optional[np.ndarray] = None
        self._dark_display: Optional[np.ndarray] = None
        self._display_size: Optional[tuple[int, int]] = None  # (h, w)
        # 显示路径专用 float32 工作 buffer — 一次分配,反复 out= 原地用,
        # 避免 30fps × ~50MB 的分配/回收引起 Python GC 抖动。
        self._display_signal_buf: Optional[np.ndarray] = None
        # 抓拍(全分辨率)路径专用 float32 buffer — 同理,避免每张 ~300MB 的
        # 分配/释放挤占 OS 文件缓存(长会话后写盘变慢的元凶之一)。
        # 抓拍可能并发(连续快拍),用锁保护这块共享 buffer。
        self._full_signal_buf: Optional[np.ndarray] = None
        self._full_buf_lock = threading.Lock()

        self._enabled: bool = False
        # 给状态显示用
        self._flat_mean: Optional[np.ndarray] = None  # mean(F-D)
        self._dark_mean: Optional[np.ndarray] = None  # mean(D)
        # 参考内容的稳定标识；仅在采集/加载/清除参考时计算，不进入逐张抓拍热路径。
        self._reference_id: Optional[str] = None

    # ---------------- 状态 ----------------

    @property
    def has_flat(self) -> bool:
        return self._flat_full is not None

    def snapshot(self) -> CorrectionSnapshot:
        # 主线程调用；参考数组只替换、不原地修改，强引用固定本次参数。
        return CorrectionSnapshot(self.enabled, self._gain_full,
                                  self._dark_full, self._reference_id)

    @property
    def has_dark(self) -> bool:
        return self._dark_full is not None

    @property
    def has_reference(self) -> bool:
        """只要有亮场就算有参考(暗场可选)。"""
        return self._flat_full is not None

    @property
    def enabled(self) -> bool:
        return self._enabled and self.has_reference

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)

    @property
    def flat_mean(self) -> Optional[np.ndarray]:
        return self._flat_mean

    @property
    def dark_mean(self) -> Optional[np.ndarray]:
        return self._dark_mean

    @property
    def reference_shape(self) -> Optional[tuple[int, ...]]:
        return None if self._flat_full is None else self._flat_full.shape

    @property
    def reference_id(self) -> Optional[str]:
        return self._reference_id

    def _refresh_reference_id(self) -> None:
        digest = hashlib.sha256()
        present = False
        for name, array in (
            ("flat", self._flat_full),
            ("dark", self._dark_full),
            ("legacy_gain", self._gain_full if self._flat_full is None else None),
        ):
            if array is None:
                continue
            present = True
            contiguous = np.ascontiguousarray(array)
            digest.update(name.encode("ascii"))
            digest.update(str(contiguous.dtype).encode("ascii"))
            digest.update(repr(contiguous.shape).encode("ascii"))
            digest.update(memoryview(contiguous).cast("B"))
        self._reference_id = digest.hexdigest() if present else None

    # ---------------- 多帧平均(可选辅助) ----------------

    @staticmethod
    def average_frames(frames: list[np.ndarray]) -> np.ndarray:
        """对多帧 BGR uint8 取均值,降低 shot/read noise。返回 uint8。"""
        if not frames:
            raise ValueError("frames 不能为空")
        stack = np.stack([f.astype(np.float32) for f in frames])
        return np.clip(stack.mean(axis=0), 0, 255).astype(np.uint8)

    # ---------------- 设置/清除参考 ----------------

    def set_dark(self, dark_bgr: np.ndarray) -> dict:
        """设置暗场参考(盖镜头采集)。会自动重算 gain map(如果已有亮场)。

        返回:summary dict 含 mean/min/max/shape — UI 提示用。
        典型合格暗场:per-channel mean < 10,max < 30。
        """
        if dark_bgr.ndim != 3 or dark_bgr.shape[2] != 3:
            raise ValueError("dark 必须是 3 通道 BGR uint8")
        # 与已有亮场尺寸不一致则拒绝,避免逐元素运算错位
        if self._flat_full is not None and dark_bgr.shape != self._flat_full.shape:
            raise ValueError(
                f"暗场 shape {dark_bgr.shape} 与已采亮场 shape {self._flat_full.shape} 不一致"
            )

        self._dark_full = dark_bgr.copy()
        d = self._dark_full.astype(np.float32)
        self._dark_mean = d.mean(axis=(0, 1))
        per_chan_min = d.min(axis=(0, 1))
        per_chan_max = d.max(axis=(0, 1))

        self._recompute_gain()
        self._invalidate_display_cache()
        self._refresh_reference_id()

        log.info(
            "FFC dark set: shape=%s mean(BGR)=%s max=%s",
            dark_bgr.shape, self._dark_mean.round(1), per_chan_max,
        )
        return {
            "mean": self._dark_mean,
            "min": per_chan_min,
            "max": per_chan_max,
            "shape": dark_bgr.shape,
        }

    def set_flat(self, flat_bgr: np.ndarray) -> dict:
        """设置亮场参考(均匀白板/灰板覆盖视野)。自动重算 gain map。

        若已有暗场,使用 (F-D) 计算 signal;否则按 D=0 退化。
        返回:summary dict 含 mean(F-D 或 F)/min/max/shape。
        典型合格亮场:per-channel mean ∈ [128, 230],max < 254(不饱和)。
        """
        if flat_bgr.ndim != 3 or flat_bgr.shape[2] != 3:
            raise ValueError("flat 必须是 3 通道 BGR uint8")
        if self._dark_full is not None and flat_bgr.shape != self._dark_full.shape:
            raise ValueError(
                f"亮场 shape {flat_bgr.shape} 与已采暗场 shape {self._dark_full.shape} 不一致"
            )

        self._flat_full = flat_bgr.copy()
        f = self._flat_full.astype(np.float32)
        # 原始亮场的 min/max(用于饱和检查)
        per_chan_min = f.min(axis=(0, 1))
        per_chan_max = f.max(axis=(0, 1))

        self._recompute_gain()
        self._invalidate_display_cache()
        self._enabled = True
        self._refresh_reference_id()

        log.info(
            "FFC flat set: shape=%s flat_mean(BGR)=%s min=%s max=%s dark_present=%s",
            flat_bgr.shape, f.mean(axis=(0, 1)).round(1), per_chan_min, per_chan_max,
            self._dark_full is not None,
        )
        return {
            "mean": self._flat_mean,    # 这里是 signal(F-D) 的均值
            "raw_mean": f.mean(axis=(0, 1)),
            "min": per_chan_min,
            "max": per_chan_max,
            "shape": flat_bgr.shape,
        }

    # 向后兼容:旧代码 / 旧自动加载路径仍调用 set_reference
    def set_reference(self, flat_bgr: np.ndarray) -> dict:
        return self.set_flat(flat_bgr)

    def _recompute_gain(self) -> None:
        """根据当前 _flat_full 和 _dark_full 重建 gain map 和 _flat_mean。"""
        if self._flat_full is None:
            self._gain_full = None
            self._flat_mean = None
            return
        f = self._flat_full.astype(np.float32)
        if self._dark_full is not None:
            signal = f - self._dark_full.astype(np.float32)
        else:
            signal = f
        # 防 0 / 防负:把 < 1 的位置设为 1(暗场比亮场还亮的位置等于无效)
        signal_safe = np.maximum(signal, 1.0)
        # 分子用 clamp 后的均值,与分母口径一致 —— 否则暗场≥亮场时 signal.mean
        # 可能 ≤0,gain 全为 0/负,校正后整幅变黑且无任何提示。
        signal_mean = signal_safe.mean(axis=(0, 1))  # shape (3,)
        self._flat_mean = signal_mean
        self._gain_full = (signal_mean / signal_safe).astype(np.float32)
        if np.any(signal_mean < 2.0):
            log.warning(
                "FFC: 信号(F-D)均值过低 %s — 暗场可能≥亮场或亮场过暗,校正可能不可靠",
                signal_mean.round(2),
            )

    def clear(self) -> None:
        self._flat_full = None
        self._dark_full = None
        self._gain_full = None
        self._flat_mean = None
        self._dark_mean = None
        self._invalidate_display_cache()
        with self._full_buf_lock:
            self._full_signal_buf = None   # 释放抓拍 buffer(~300MB)
        self._enabled = False
        self._refresh_reference_id()
        log.info("FFC references cleared (both dark and flat)")

    def clear_dark(self) -> None:
        """只清暗场,保留亮场。会重算 gain map。"""
        self._dark_full = None
        self._dark_mean = None
        self._recompute_gain()
        self._invalidate_display_cache()
        self._refresh_reference_id()
        log.info("FFC dark cleared (flat retained)")

    def clear_flat(self) -> None:
        """只清亮场;无亮场就没法校正,enabled 会被设为 False。"""
        self._flat_full = None
        self._flat_mean = None
        self._gain_full = None
        self._invalidate_display_cache()
        with self._full_buf_lock:
            self._full_signal_buf = None   # 释放抓拍 buffer,清亮场后不再需要
        self._enabled = False
        self._refresh_reference_id()
        log.info("FFC flat cleared")

    def _invalidate_display_cache(self) -> None:
        self._gain_display = None
        self._dark_display = None
        self._display_size = None
        self._display_signal_buf = None

    # ---------------- 应用 ----------------

    def apply_full(self, bgr: np.ndarray) -> np.ndarray:
        """全分辨率校正(抓拍时用)。

        合并 (uint8→float32 + 减暗场) 为一次 cv2.subtract;
        后续乘 gain、clip 全用 out= 原地,峰值内存约 (input + float32 工作区 + output)。
        25MP:~150MB,原写法 ~600MB。
        """
        # 先把 gain/dark 取到局部变量:主线程的 clear()/set_* 可能在本函数
        # 执行期间把 self._gain_full 置空,若直接用成员会读到 None 而崩。
        gain = self._gain_full
        dark = self._dark_full
        if not self.enabled or gain is None:
            return bgr
        if bgr.shape != gain.shape:
            log.warning(
                "FFC apply_full shape mismatch: bgr=%s gain=%s — 跳过校正",
                bgr.shape, gain.shape,
            )
            return bgr
        # 复用持久 float32 buffer(锁保护:并发快拍时第二张等第一张算完,
        # 既保证正确又避免两块 300MB 同时存在)。
        with self._full_buf_lock:
            buf = self._full_signal_buf
            if buf is None or buf.shape != bgr.shape:
                buf = np.empty(bgr.shape, dtype=np.float32)
                self._full_signal_buf = buf
            if dark is not None:
                # uint8 - uint8 → float32,写入持久 buffer
                np.subtract(bgr, dark, out=buf, casting="unsafe")
            else:
                np.copyto(buf, bgr, casting="unsafe")
            np.multiply(buf, gain, out=buf)
            np.clip(buf, 0, 255, out=buf)
            # 输出必须新分配:save/brisque 线程异步用它,而 buffer 下张会被覆写
            return buf.astype(np.uint8)

    def apply_display(self, bgr: np.ndarray) -> np.ndarray:
        """显示路径校正(降采样后)。在 30fps 流上跑,所以用预分配 buffer。

        gain_display / dark_display / 工作 buffer 都按当前显示尺寸缓存,
        尺寸变了才重建一次。bgr 改变内容时 buffer 通过 out= 原地写入。
        """
        # 本函数在取流线程跑;主线程的 set_*/clear 会调用 _invalidate_display_cache
        # 把这些成员同时置 None。先全部快照到局部变量,用局部变量贯穿计算,
        # 这样即便中途被置空,本次调用也不会拿到 None 而崩(下一帧自然重建)。
        gain_full = self._gain_full
        dark_full = self._dark_full
        if not self.enabled or gain_full is None:
            return bgr
        h, w = bgr.shape[:2]
        gain = self._gain_display
        dark = self._dark_display
        buf = self._display_signal_buf
        if (self._display_size != (h, w) or gain is None
                or buf is None or buf.shape != bgr.shape):
            gain = cv2.resize(
                gain_full, (w, h), interpolation=cv2.INTER_LINEAR,
            )
            if dark_full is not None:
                dark = cv2.resize(
                    dark_full.astype(np.float32), (w, h),
                    interpolation=cv2.INTER_LINEAR,
                )
            else:
                dark = None
            # 工作 buffer:float32,与 bgr 同 shape
            buf = np.empty(bgr.shape, dtype=np.float32)
            self._gain_display = gain
            self._dark_display = dark
            self._display_signal_buf = buf
            self._display_size = (h, w)

        # uint8 → float32:用 out= 原地写,避免分配
        if dark is not None:
            np.subtract(bgr, dark, out=buf, casting="unsafe")
        else:
            np.copyto(buf, bgr, casting="unsafe")
        np.multiply(buf, gain, out=buf)
        np.clip(buf, 0, 255, out=buf)
        # 输出新分配 — 因为下一帧会原地覆盖 buf,信号 emit 给主线程后还要被读
        return buf.astype(np.uint8)

    # ---------------- 持久化 ----------------
    # 新版 npz 含 'flat'(uint8) + 'dark'(uint8, 可缺) + 'mean'(signal mean, 状态显示用)
    # 老版 npz 只有 'gain'(float32) + 'mean' — 也能加载,但会失去暗场,且无法重算
    #                                          (因为没存 flat 原图);这里保留兼容。

    def save(self, path: Path) -> None:
        if self._flat_full is None:
            raise RuntimeError("没有亮场参考可保存")
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {
            "flat": self._flat_full,
            "mean": self._flat_mean,  # = mean(F - D)
        }
        if self._dark_full is not None:
            kwargs["dark"] = self._dark_full
        np.savez_compressed(str(path), **kwargs)
        log.info(
            "FFC reference saved to %s (dark=%s)",
            path, "yes" if self._dark_full is not None else "no",
        )

    def load(self, path: Path) -> None:
        data = np.load(str(path))
        keys = set(data.files)
        if "flat" in keys:
            # 新格式
            self._flat_full = data["flat"].astype(np.uint8)
            if "dark" in keys:
                self._dark_full = data["dark"].astype(np.uint8)
                self._dark_mean = self._dark_full.astype(np.float32).mean(axis=(0, 1))
            else:
                self._dark_full = None
                self._dark_mean = None
            self._recompute_gain()
            self._invalidate_display_cache()
            self._enabled = True
            self._refresh_reference_id()
            log.info(
                "FFC loaded (new format) %s shape=%s dark=%s",
                path, self._flat_full.shape, "yes" if self._dark_full is not None else "no",
            )
        elif "gain" in keys:
            # 老格式 — 没法重算 gain,只能直接用;不携带暗场
            self._gain_full = data["gain"].astype(np.float32)
            self._flat_mean = data["mean"].astype(np.float32) if "mean" in keys else None
            self._flat_full = None
            self._dark_full = None
            self._dark_mean = None
            self._invalidate_display_cache()
            self._enabled = True
            self._refresh_reference_id()
            log.warning(
                "FFC loaded (legacy format, no flat raw) %s shape=%s — 升级提示:重新采集一次以启用暗场",
                path, self._gain_full.shape,
            )
        else:
            raise ValueError(f"FFC 存档格式无法识别: keys={keys}")
