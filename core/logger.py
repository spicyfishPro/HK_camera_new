"""日志配置 — 写到 logs/YYYY-MM-DD.log,同时输出到控制台。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .paths import LOGS_DIR as _LOG_DIR


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    _LOG_DIR.mkdir(exist_ok=True)
    log_file = _LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        # 强制 utf-8,避免 Windows 控制台默认 GBK 把中文打成乱码
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, OSError):
            pass
        sh = logging.StreamHandler(stream=sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    return root
