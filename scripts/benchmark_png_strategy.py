"""用真实采集照片比较 PNG 压缩级别 1 的 DEFAULT/RLE 策略。

只读取输入图片并在内存中重新编码，不会修改或生成采集文件。
RLE 只在编码中位耗时至少降低 15%、中位体积增幅不超过 10%且
所有图像解码后逐像素一致时给出启用建议。
"""
from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import cv2
import numpy as np


SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, int(np.ceil(len(ordered) * 0.95)) - 1)
    return ordered[index]


def _read_bgr(path: Path) -> np.ndarray:
    encoded = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法解码: {path}")
    return image


def _encode(image: np.ndarray, strategy: int) -> tuple[float, np.ndarray]:
    params = [
        cv2.IMWRITE_PNG_COMPRESSION, 1,
        cv2.IMWRITE_PNG_STRATEGY, strategy,
    ]
    started = time.perf_counter()
    ok, encoded = cv2.imencode(".png", image, params)
    elapsed = time.perf_counter() - started
    if not ok:
        raise RuntimeError("cv2.imencode PNG 失败")
    return elapsed, encoded


def benchmark(paths: list[Path]) -> dict[str, float | int | bool | str]:
    default_times: list[float] = []
    rle_times: list[float] = []
    default_sizes: list[int] = []
    rle_sizes: list[int] = []
    exact = True

    for path in paths:
        image = _read_bgr(path)
        default_s, default_buf = _encode(
            image, cv2.IMWRITE_PNG_STRATEGY_DEFAULT,
        )
        rle_s, rle_buf = _encode(image, cv2.IMWRITE_PNG_STRATEGY_RLE)
        default_times.append(default_s)
        rle_times.append(rle_s)
        default_sizes.append(int(default_buf.nbytes))
        rle_sizes.append(int(rle_buf.nbytes))
        default_decoded = cv2.imdecode(default_buf, cv2.IMREAD_COLOR)
        rle_decoded = cv2.imdecode(rle_buf, cv2.IMREAD_COLOR)
        exact = exact and bool(
            default_decoded is not None
            and rle_decoded is not None
            and np.array_equal(default_decoded, image)
            and np.array_equal(rle_decoded, image)
        )

    default_median = statistics.median(default_times)
    rle_median = statistics.median(rle_times)
    default_size_median = statistics.median(default_sizes)
    rle_size_median = statistics.median(rle_sizes)
    speedup = 1.0 - rle_median / default_median
    size_change = rle_size_median / default_size_median - 1.0
    recommend = exact and speedup >= 0.15 and size_change <= 0.10
    return {
        "count": len(paths),
        "default_median_s": default_median,
        "default_p95_s": _percentile95(default_times),
        "rle_median_s": rle_median,
        "rle_p95_s": _percentile95(rle_times),
        "speedup": speedup,
        "size_change": size_change,
        "pixels_exact": exact,
        "recommendation": "RLE" if recommend else "DEFAULT",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="评估真实采集图上 PNG level=1 的 DEFAULT/RLE 策略",
    )
    parser.add_argument("图片目录", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    folder: Path = getattr(args, "图片目录")
    paths = sorted(
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )[:max(1, args.limit)]
    if not paths:
        parser.error("目录中没有可读取的图片")
    result = benchmark(paths)
    for key, value in result.items():
        if key in {"speedup", "size_change"}:
            print(f"{key}={float(value):.1%}")
        elif key.endswith("_s"):
            print(f"{key}={float(value):.3f}")
        else:
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
