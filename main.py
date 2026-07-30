"""HK Camera 图像采集软件 — 入口。

启动:
    python main.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 把项目根目录加进 sys.path,让 `from core...` / `from ui...` 跑得通
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.logger import setup_logging  # noqa: E402
from core.camera import init_sdk, finalize_sdk  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.style import DARK_QSS, build_dark_palette  # noqa: E402


def main() -> int:
    setup_logging()
    init_sdk()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(build_dark_palette())
    app.setStyleSheet(DARK_QSS)

    win = MainWindow()
    win.show()
    try:
        return app.exec()
    finally:
        finalize_sdk()


if __name__ == "__main__":
    sys.exit(main())
