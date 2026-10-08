"""在无相机环境渲染 Windows/Qt 界面验收图，仅使用明确标记的测试图像。"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from test_qr_ui import QApplication, MainWindow, _FakeScorer  # noqa: E402
from test_acquisition_metadata import sample_metadata  # noqa: E402
from core.capture_pipeline import CaptureEvent  # noqa: E402
from core.spike_workflow import StepStatus  # noqa: E402
from ui.style import build_dark_palette, DARK_QSS  # noqa: E402


def settle(app):
    for _ in range(15):
        app.processEvents()
        time.sleep(0.01)


def render(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setPalette(build_dark_palette())
    app.setStyleSheet(DARK_QSS)
    with patch("ui.capture_widget.BRISQUEScorer", return_value=_FakeScorer()):
        window = MainWindow()
    with tempfile.TemporaryDirectory(prefix="capture-ui-") as directory:
        tab = window.wheat_tab
        tab.camera.is_connected = lambda: True
        tab._save_dir = Path(directory)
        tab.lbl_save_dir.setText(directory)
        tab.le_prefix.setText("RIL245")
        wf = tab.spike_panel.workflow
        wf.start("RIL245", 5)
        tab.spike_panel._rebuild_grid()
        tab._sync_gallery_context()
        tab.lbl_model.setText("设备型号：界面验证用模拟相机")
        frame = np.full((800, 1000, 3), 30, np.uint8)
        cv2.putText(
            frame,
            "LIVE PREVIEW (TEST)",
            (150, 390),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.3,
            (180, 190, 180),
            2,
        )
        for n, state in enumerate(
            ["saved", "saved", "saved", "saving", "queued", "failed"]
        ):
            rep = n // 2 + 1
            side = "front" if n % 2 == 0 else "side"
            image = np.full((320, 320, 3), 25, np.uint8)
            cv2.putText(
                image,
                f"Rep {rep}",
                (60, 145),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (50 + n * 15, 180, 210),
                2,
            )
            cv2.putText(
                image, side, (65, 205), cv2.FONT_HERSHEY_SIMPLEX, 1, (180, 180, 180), 2
            )
            path = Path(directory) / f"RIL245_Rep{rep}_{side}.png"
            cv2.imencode(".png", image)[1].tofile(str(path))
            meta = replace(
                sample_metadata(),
                capture_id=str(n),
                accession_no="RIL245",
                rep=rep,
                side=side,
                filename=path.name,
            )
            event = CaptureEvent(
                str(n),
                tab._owner,
                wf.session_id,
                1,
                meta,
                path,
                None,
                state,
                score=41 + n,
                thumbnail=image,
                message="模拟磁盘满" if state == "failed" else "",
            )
            tab.gallery.update_event(event)
            wf.steps()[n].status = {
                "saved": StepStatus.PASSED,
                "failed": StepStatus.SAVE_FAILED,
            }.get(state, StepStatus.SAVING)
        wf.jump_to(4, "front")
        tab.spike_panel._refresh()
        tab.gallery.locate_current()
        scale = os.environ.get("QT_SCALE_FACTOR", "1")
        for width, height in ((1000, 600), (1366, 768), (1600, 900)):
            window.resize(width, height)
            window.show()
            tab._capture_splitter.setSizes([height - 255, 190])
            settle(app)
            tab._show_preview_frame(frame)
            tab.view.fit_to_window()
            tab.gallery.list.horizontalScrollBar().setValue(0)
            settle(app)
            target = output / f"capture-{width}x{height}-scale{scale}.png"
            if not window.grab().save(str(target)):
                raise RuntimeError(f"无法输出截图: {target}")
            print(target)
        tab.gallery.viewer.open_entry(tab.gallery.model.entries[3])
        settle(app)
        window.wheat_tab.gallery.viewer.grab().save(
            str(output / f"viewer-scale{scale}.png")
        )
        window.close()
        settle(app)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    render(parser.parse_args().output)
