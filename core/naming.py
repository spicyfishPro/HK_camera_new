"""命名规则与序号续接。

格式:
    {prefix}[_R{repeat}][_{side}]_{YYYY-MM-DD-HH-MM-SS}_{NNN}.{ext}

字段:
    prefix:    必填,文件名前缀
    repeat:    可选,启用时为正整数,字段段为 "_R<n>"
    side:      可选(仅麦穗),"front" 或 "side"
    timestamp: 抓拍时刻,固定 14 段格式
    NNN:       同 (prefix, repeat, side) 组合下的递增序号,3 位 0 填充
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional


_VALID_EXTS = {"png", "jpg", "jpeg", "bmp", "tif", "tiff"}
_TIMESTAMP_FMT = "%Y-%m-%d-%H-%M-%S"


def build_filename(
    prefix: str,
    *,
    repeat: Optional[int] = None,
    side: Optional[str] = None,
    serial: int = 1,
    ext: str = "png",
    now: Optional[datetime] = None,
) -> str:
    """构造文件名(不含目录)。"""
    if not prefix:
        raise ValueError("prefix 不能为空")
    ext = ext.lower().lstrip(".")
    if ext not in _VALID_EXTS:
        raise ValueError(f"不支持的扩展名: {ext}")
    if side is not None and side not in ("front", "side"):
        raise ValueError(f"side 必须为 front/side,得到 {side!r}")
    if repeat is not None and repeat < 1:
        raise ValueError("repeat 须为正整数")

    now = now or datetime.now()
    parts = [prefix]
    if repeat is not None:
        parts.append(f"R{repeat}")
    if side is not None:
        parts.append(side)
    parts.append(now.strftime(_TIMESTAMP_FMT))
    parts.append(f"{serial:03d}")
    return "_".join(parts) + "." + ext


def next_serial(
    save_dir: str | Path,
    prefix: str,
    *,
    repeat: Optional[int] = None,
    side: Optional[str] = None,
) -> int:
    """扫描目录,找到同 (prefix, repeat, side) 组合下最大的 NNN,返回 NNN+1。
    没匹配 → 返回 1。"""
    d = Path(save_dir)
    if not d.is_dir():
        return 1

    # 构造正则:prefix_[Rn_]?[side_]?ts_NNN.ext
    # 用占位 "TS" 表示时间戳段(14 字符的固定模式)
    body_parts: list[str] = [re.escape(prefix)]
    if repeat is not None:
        body_parts.append(f"R{repeat}")
    if side is not None:
        body_parts.append(side)
    body_parts.append(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}")
    pattern = "_".join(body_parts) + r"_(\d{3})\.[A-Za-z]+$"
    rx = re.compile(pattern)

    max_n = 0
    for name in (p.name for p in d.iterdir() if p.is_file()):
        m = rx.match(name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n + 1


def resolve_conflict(path: Path) -> Path:
    """同名文件已存在 → 在 stem 后加 _2 / _3 ... 直到不冲突。"""
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    parent = path.parent
    n = 2
    while True:
        candidate = parent / f"{stem}_{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1
