"""有界抓拍流水线。仅传递不可变任务事件，Qt/SDK 由调用者负责。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .acquisition_metadata import CaptureMetadata, FrameSnapshot, save_bgr_atomic
from .correction import CorrectionSnapshot

log = logging.getLogger(__name__)


def preview(image: np.ndarray, side: int = 320) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(1.0, side / max(h, w))
    result = (
        cv2.resize(
            image,
            (max(1, round(w * scale)), max(1, round(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
        if scale < 1
        else image.copy()
    )
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class CaptureJob:
    metadata: CaptureMetadata
    owner: str
    session: str
    attempt: int
    path: Path
    raw_path: Path | None
    get_frame: Callable[[], FrameSnapshot]
    correction: CorrectionSnapshot
    scorer: object | None
    threshold: float
    reserved_bytes: int
    overwrite: bool = False
    submitted_at: float = field(default_factory=time.perf_counter)

    @property
    def id(self) -> str:
        return self.metadata.capture_id


@dataclass(frozen=True)
class CaptureEvent:
    id: str
    owner: str
    session: str
    attempt: int
    metadata: CaptureMetadata
    path: Path
    raw_path: Path | None
    state: str
    score: float | None = None
    passed: bool = True
    message: str = ""
    thumbnail: np.ndarray | None = field(default=None, compare=False, repr=False)
    raw_thumbnail: np.ndarray | None = field(default=None, compare=False, repr=False)
    timings: dict[str, float] = field(default_factory=dict)
    threshold: float = 60.0
    submitted_at: float = 0.0


@dataclass
class _Record:
    job: CaptureJob
    state: str = "preparing"
    images: list[tuple[Path, np.ndarray, CaptureMetadata]] = field(default_factory=list)
    completed: set[Path] = field(default_factory=set)
    score: float | None = None
    passed: bool = True
    message: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    thumbnail: np.ndarray | None = None
    raw_thumbnail: np.ndarray | None = None
    quality_ready: bool = False
    failure_stage: str = ""
    warnings: list[str] = field(default_factory=list)


class CapturePipeline:
    """一个预处理线程、两个编码线程；失败照片保留直到重试成功。

    同路径仅队首可进入保存池，不让等待前驱的任务占住编码线程。
    所有容量统计均在锁内，包含预处理、保存中、失败保留的任务。
    """

    def __init__(
        self,
        notify: Callable[[CaptureEvent], None],
        *,
        max_jobs=8,
        max_bytes=768 * 1024 * 1024,
        saver=save_bgr_atomic,
    ):
        self.notify = notify
        self.max_jobs, self.max_bytes = max_jobs, max_bytes
        self._saver = saver
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._preparer = ThreadPoolExecutor(1, thread_name_prefix="capture-prepare")
        self._writers = ThreadPoolExecutor(2, thread_name_prefix="capture-save")
        self._accepting = True
        self._closed = False

    @property
    def pending_count(self):
        with self._lock:
            return len(self._records)

    @property
    def reserved_bytes(self):
        with self._lock:
            return sum(r.job.reserved_bytes for r in self._records.values())

    @property
    def has_failures(self):
        with self._lock:
            return any(r.state == "failed" for r in self._records.values())

    def owner_pending(self, owner):
        with self._lock:
            return sum(r.job.owner == owner for r in self._records.values())

    def can_accept(self, size=0):
        with self._lock:
            return (
                self._accepting
                and not self._closed
                and not self.has_failures
                and len(self._records) < self.max_jobs
                and self.reserved_bytes + size <= self.max_bytes
            )

    def submit(self, job: CaptureJob) -> bool:
        with self._lock:
            if not self.can_accept(job.reserved_bytes) or job.id in self._records:
                return False
            # 不同材料/模式的任务也不能意外占用同名输出。
            conflicts = [
                r
                for r in self._records.values()
                if ({job.path, job.raw_path} & {r.job.path, r.job.raw_path}) - {None}
            ]
            if conflicts and not job.overwrite:
                return False
            record = _Record(job)
            self._records[job.id] = record
            self._emit(record)
            self._preparer.submit(self._prepare, record)
            return True

    def reserved_paths(self):
        with self._lock:
            return {
                p
                for r in self._records.values()
                for p in (r.job.path, r.job.raw_path)
                if p is not None
            }

    def _emit(self, record, state=None, message=None):
        job = record.job
        event = CaptureEvent(
            job.id,
            job.owner,
            job.session,
            job.attempt,
            job.metadata,
            job.path,
            job.raw_path,
            state or record.state,
            record.score,
            record.passed,
            record.message if message is None else message,
            record.thumbnail,
            record.raw_thumbnail,
            dict(record.timings),
            job.threshold,
            job.submitted_at,
        )
        self.notify(event)

    def _prepare(self, record):
        job = record.job
        try:
            started = time.perf_counter()
            snapshot = job.get_frame()
            if snapshot is None:
                raise RuntimeError("没有取得新的相机帧")
            record.timings["snapshot_s"] = time.perf_counter() - started
            # 保守预留 input + corrected + main/raw，转换完成后回收多余额度。
            required = snapshot.bgr.nbytes * (4 if job.raw_path else 3)
            with self._lock:
                if self.reserved_bytes - job.reserved_bytes + required > self.max_bytes:
                    raise RuntimeError("新帧尺寸超过图像缓冲预算，请降低队列占用后重拍")
                job = replace(job, reserved_bytes=required)
                record.job = job
            started = time.perf_counter()
            corrected = job.correction.apply(snapshot.bgr)
            record.timings["ffc_s"] = time.perf_counter() - started
            started = time.perf_counter()

            def rotate(arr):
                angle = job.metadata.rotation_deg
                if angle == 180:
                    return cv2.flip(arr, -1)
                if angle in (90, 270):
                    return cv2.rotate(
                        arr,
                        cv2.ROTATE_90_CLOCKWISE
                        if angle == 90
                        else cv2.ROTATE_90_COUNTERCLOCKWISE,
                    )
                return arr

            image = rotate(corrected)
            image.setflags(write=False)
            meta = replace(
                job.metadata,
                frame=snapshot.metadata,
                captured_at=snapshot.metadata.received_at,
                output_height=image.shape[0],
                output_width=image.shape[1],
            )
            if (
                meta.exposure_auto_code not in (None, 0)
                and snapshot.metadata.exposure_time_us is not None
            ):
                meta = replace(
                    meta, exposure_readback_us=snapshot.metadata.exposure_time_us
                )
            if (
                meta.gain_auto_code not in (None, 0)
                and snapshot.metadata.gain is not None
            ):
                meta = replace(meta, gain_readback=snapshot.metadata.gain)
            record.job = job = replace(job, metadata=meta)
            record.images = [(job.path, image, meta)]
            if job.raw_path:
                raw = rotate(snapshot.bgr)
                raw.setflags(write=False)
                raw_meta = replace(
                    meta,
                    filename=job.raw_path.name,
                    image_variant="unffc_demosaiced",
                    ffc_applied=False,
                )
                record.images.append((job.raw_path, raw, raw_meta))
                record.raw_thumbnail = preview(raw)
            record.timings["rotate_s"] = time.perf_counter() - started
            record.thumbnail = preview(image)
            started = time.perf_counter()
            if job.scorer is not None:
                try:
                    record.score = job.scorer.score_prepared(
                        job.scorer.prepare_ndarray(image)
                    )
                except Exception:
                    log.exception("capture quality failed id=%s", job.id)
                if record.score is None:
                    record.message = "评分失败，默认通过"
                else:
                    record.passed = bool(record.score <= job.threshold)
            else:
                record.message = "无质量评分"
            record.timings["quality_s"] = time.perf_counter() - started
            # 不通过也保存，与既有覆盖式重拍行为一致。
            record.quality_ready = True
            del snapshot, corrected, image
            with self._lock:
                record.job = replace(
                    job,
                    get_frame=lambda: None,
                    reserved_bytes=sum(x.nbytes for _, x, _ in record.images),
                )
                record.state = "queued"
                record.timings["ready_s"] = time.perf_counter() - job.submitted_at
                self._emit(record, "quality")
                self._emit(record)
                self._schedule()
        except Exception as exc:
            log.exception("capture preparation failed id=%s", job.id)
            with self._lock:
                # 取帧/预处理失败没有可重试保存的成品，让用户重新抓拍。
                record.state = "capture_failed"
                record.message = str(exc)
                self._emit(record)
                self._records.pop(job.id, None)
                self._schedule()

    def _schedule(self):
        busy = sum(r.state == "saving" for r in self._records.values())
        preceding: set[Path] = set()
        for record in self._records.values():
            paths = {record.job.path}
            if record.job.raw_path:
                paths.add(record.job.raw_path)
            if record.state == "queued" and not paths & preceding and busy < 2:
                record.state = "saving"
                busy += 1
                self._emit(record)
                self._writers.submit(self._save, record)
            preceding.update(paths)

    def _save(self, record):
        job = record.job
        started = time.perf_counter()
        record.timings["queue_s"] = max(
            0.0, started - job.submitted_at - record.timings.get("ready_s", 0)
        )
        try:
            for path, image, metadata in record.images:
                if path in record.completed:
                    continue
                result = self._saver(image, path, metadata, overwrite=job.overwrite)
                record.completed.add(path)
                record.timings["encode_s"] = (
                    record.timings.get("encode_s", 0) + result.encode_s
                )
                record.timings["write_s"] = (
                    record.timings.get("write_s", 0) + result.write_s
                )
                if result.warning:
                    record.warnings.append(result.warning)
            with self._lock:
                record.state = "saved"
                record.message = "；".join(
                    dict.fromkeys(filter(None, [record.message, *record.warnings]))
                )
                record.timings["total_s"] = time.perf_counter() - job.submitted_at
                self._emit(record)
                self._records.pop(job.id, None)
                record.images.clear()
                self._schedule()
            log.info(
                "capture perf id=%s frame=%s timings=%s pending=%s image_bytes=%s path=%s",
                job.id,
                job.metadata.frame.frame_number,
                record.timings,
                self.pending_count,
                self.reserved_bytes,
                job.path,
            )
        except Exception as exc:
            log.exception("capture save failed id=%s", job.id)
            with self._lock:
                record.state = "failed"
                record.message = str(exc)
                self._emit(record)
                self._schedule()

    def retry(self, job_id):
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.state != "failed":
                return False
            record.state = "queued"
            record.message = ""
            self._emit(record)
            self._schedule()
            return True

    def image_preview(self, job_id, raw=False, side=2048):
        with self._lock:
            record = self._records.get(job_id)
            if not record or not record.images:
                return None
            image = record.images[-1 if raw else 0][1]
        return preview(image, side)

    def begin_drain(self):
        with self._lock:
            self._accepting = False

    def shutdown(self):
        with self._lock:
            if self._records:
                return False
            self._closed = True
            self._accepting = False
        self._preparer.shutdown(wait=True)
        self._writers.shutdown(wait=True)
        return True
