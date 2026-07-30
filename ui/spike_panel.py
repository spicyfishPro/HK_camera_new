"""采集工作流看板组件 — 总 Rep + 起点配置 + 开始/重置 + 进度网格。

可配置 sides 支持两种形态:
  - 麦穗:sides = (("front","正"), ("side","侧")) → 每个重复正/反两张,2 行网格
  - 籽粒:sides = (("",""),)                      → 每个重复一张,无正反,1 行网格

前缀直接复用 CaptureWidget 的"文件名前缀"输入(避免命名冗余)。
绿/灰由磁盘状态决定(set_disk_checker 注入 checker)。

颜色:灰 PENDING / 橙 CURRENT / 绿 PASSED & SKIPPED / 红 FAILED。
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from core.spike_workflow import SpikeWorkflow, StepStatus


# 5 态颜色(SKIPPED 与 PASSED 共色)
_COLOR = {
    StepStatus.PENDING: "#444",
    StepStatus.CURRENT: "#f29429",
    StepStatus.PASSED:  "#4caf50",
    StepStatus.SKIPPED: "#4caf50",
    StepStatus.FAILED:  "#ef5350",
}
_BORDER = {
    StepStatus.CURRENT: "#FFEB3B",  # 当前格亮色边框,一眼看到
    StepStatus.FAILED:  "#FFEB3B",
}

# 默认麦穗配置
WHEAT_SIDES = (("front", "正"), ("side", "侧"))
# 籽粒:单相位、无正反
GRAIN_SIDES = (("", ""),)


class _CellButton(QPushButton):
    """单格按钮 — 固定大小、纯色背景、点击可跳转。"""

    __slots__ = ("rep", "side")

    def __init__(self, rep: int, side: str, parent=None) -> None:
        super().__init__(parent)
        self.rep = rep
        self.side = side
        self.setFixedSize(24, 24)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def apply_status(self, status: StepStatus, tip: str) -> None:
        bg = _COLOR[status]
        border = _BORDER.get(status, "#222")
        self.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; border: 1.5px solid {border};"
            f" border-radius: 4px; }}"
            f"QPushButton:hover {{ border: 2px solid #fff; }}"
        )
        self.setToolTip(tip)


class SpikePanel(QGroupBox):
    """采集工作流看板(麦穗/籽粒共用,由 sides 配置区分)。

    使用方式:
      panel = SpikePanel(title="籽粒工作流", sides=GRAIN_SIDES)
      panel.workflow_state_changed.connect(...)
      panel.apply_pass(score) / panel.apply_fail(score)
      panel.workflow   # 直接访问 SpikeWorkflow
    """

    workflow_state_changed = pyqtSignal(bool)
    workflow_completed = pyqtSignal()
    start_requested = pyqtSignal()

    _MAX_REPS = 20
    _AUTO_RESET_DELAY_MS = 1500

    def __init__(self, title: str = "麦穗工作流",
                 sides: tuple = WHEAT_SIDES, default_reps: int = 5,
                 parent=None) -> None:
        super().__init__(title, parent)
        self.workflow = SpikeWorkflow()
        self._side_keys: tuple[str, ...] = tuple(k for k, _ in sides)
        self._side_zh: dict[str, str] = {k: zh for k, zh in sides}
        self._single_side: bool = len(sides) == 1
        self._default_reps: int = default_reps

        self._cells: list[_CellButton] = []
        self._disk_checker: Optional[Callable[[int, str], bool]] = None
        self._prefix_clearer: Optional[Callable[[], None]] = None
        self._precheck: Optional[Callable[[], tuple[bool, str]]] = None
        self._start_busy: bool = False
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._auto_reset_after_complete)

        self._build_ui()
        self._refresh()

    # ---------------- 外部注入 ----------------

    def set_prefix_clearer(self, clearer: Callable[[], None]) -> None:
        self._prefix_clearer = clearer

    def set_precheck(self, precheck: Callable[[], tuple[bool, str]]) -> None:
        self._precheck = precheck

    def set_disk_checker(self, checker: Optional[Callable[[int, str], bool]]) -> None:
        self._disk_checker = checker
        if self.workflow.active:
            self._refresh()

    def request_refresh(self) -> None:
        if self.workflow.active:
            self._refresh()

    def set_start_busy(self, busy: bool) -> None:
        """锁定/解锁启动阶段的配置控件。"""
        self._start_busy = bool(busy)
        self._refresh()

    # ---------------- 显示辅助 ----------------

    def _side_text(self, side: str) -> str:
        """该相位的中文短标签;籽粒为空串。"""
        return self._side_zh.get(side, side)

    def _step_desc(self, rep: int, side: str) -> str:
        """'Rep3 · 正' / 籽粒 'Rep3'。"""
        zh = self._side_text(side)
        return f"Rep{rep} · {zh}" if zh else f"Rep{rep}"

    # ---------------- UI 构造 ----------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 12, 8, 8)
        root.setSpacing(6)

        # 总 Rep + 起点(前缀复用 CaptureWidget 的"文件名前缀")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("总Rep:"))
        self.sp_total = QSpinBox()
        self.sp_total.setRange(1, self._MAX_REPS)
        self.sp_total.setValue(self._default_reps)
        self.sp_total.setFixedWidth(56)
        row2.addWidget(self.sp_total)
        row2.addSpacing(6)
        row2.addWidget(QLabel("起点 R"))
        self.sp_start_rep = QSpinBox()
        self.sp_start_rep.setRange(1, self._MAX_REPS)
        self.sp_start_rep.setValue(1)
        self.sp_start_rep.setFixedWidth(56)
        row2.addWidget(self.sp_start_rep)
        # 多相位(麦穗)才有"正/侧"起点选择;籽粒隐藏
        self.cb_start_side: Optional[QComboBox] = None
        if not self._single_side:
            self.cb_start_side = QComboBox()
            for key, zh in self._side_zh.items():
                self.cb_start_side.addItem(zh, key)
            self.cb_start_side.setFixedWidth(56)
            row2.addWidget(self.cb_start_side)
        row2.addStretch(1)
        root.addLayout(row2)

        # 按钮
        row3 = QHBoxLayout()
        self.btn_start = QPushButton("开始")
        self.btn_start.setMinimumHeight(28)
        self.btn_reset = QPushButton("重置")
        self.btn_reset.setMinimumHeight(28)
        row3.addWidget(self.btn_start, stretch=1)
        row3.addWidget(self.btn_reset, stretch=1)
        root.addLayout(row3)

        # 网格容器。
        # 关键:垂直用 Fixed 策略且**绝不**手动 setFixedHeight ——
        # Fixed 策略让控件恒等于 sizeHint() 高度(由网格内容精确计算),
        # 子按钮永远落在容器内,不会溢出盖住下方 lbl_current。
        # 未启动时用 setVisible(False) 折叠(隐藏控件不占布局空间),
        # 而非把高度强设为 0 —— 后者会导致按钮溢出遮挡。
        self._grid_host = QWidget()
        self._grid_host.setSizePolicy(QSizePolicy.Policy.Preferred,
                                     QSizePolicy.Policy.Fixed)
        self._grid_layout = QGridLayout(self._grid_host)
        # 底部留 10px 余量(算进 sizeHint)→ 最后一行格子结构性地远离下方文字,
        # 即便不同 DPI/字体下有几像素几何误差也不会触到 lbl_current。
        self._grid_layout.setContentsMargins(0, 4, 0, 10)
        self._grid_layout.setHorizontalSpacing(2)
        self._grid_layout.setVerticalSpacing(2)
        self._grid_host.setVisible(False)   # 未启动时隐藏,不占空间
        root.addWidget(self._grid_host)

        # 当前步状态
        self.lbl_current = QLabel("尚未启动")
        self.lbl_current.setStyleSheet("color: #aaa; font-size: 11px;")
        self.lbl_current.setWordWrap(True)
        root.addWidget(self.lbl_current)

        # 防覆盖提示行
        self.lbl_collision = QLabel("")
        self.lbl_collision.setWordWrap(True)
        self.lbl_collision.setStyleSheet("font-size: 12px; font-weight: 600;")
        self.lbl_collision.hide()
        root.addWidget(self.lbl_collision)

        self.sp_total.valueChanged.connect(self._on_total_changed)
        self.btn_start.clicked.connect(self.start_requested.emit)
        self.btn_reset.clicked.connect(self._on_reset_clicked)

    def _on_total_changed(self, v: int) -> None:
        if self.sp_start_rep.value() > v:
            self.sp_start_rep.setValue(v)
        self.sp_start_rep.setMaximum(v)

    # ---------------- 启动 / 重置 ----------------

    def start_with_prefix(self, prefix: str) -> bool:
        """使用已确定的品种前缀真正启动现有工作流。"""
        if not prefix or not prefix.strip():
            QMessageBox.warning(self, self.title(),
                                "请先在「文件名前缀」中输入品种前缀")
            return False
        if self._precheck is not None:
            ok, reason = self._precheck()
            if not ok:
                QMessageBox.warning(self, self.title(), reason or "未就绪,已取消开始")
                return False
        start_side = (
            self._side_keys[0] if self._single_side
            else self.cb_start_side.currentData()
        )
        try:
            self.workflow.start(
                prefix=prefix,
                total=self.sp_total.value(),
                start_rep=self.sp_start_rep.value(),
                start_side=start_side,
                sides=self._side_keys,
            )
        except Exception as e:
            QMessageBox.critical(self, self.title(), f"启动失败: {e}")
            return False
        # 扫盘:标绿已拍、CURRENT 跳到第一张未拍;判断是否整套已拍完整
        if self._disk_checker is not None:
            self.workflow.resume_from_disk(self._disk_checker)
            if self.workflow.is_complete():
                self.workflow.reset()
                self._clear_grid()
                self._show_collision(
                    "⛔ 当前文件夹已含此前缀的完整照片,请审核或修改前缀后再开始。",
                    ok=False,
                )
                self.workflow_state_changed.emit(False)
                return False

        self._rebuild_grid()
        self.workflow_state_changed.emit(True)
        cur = self.workflow.current_step()
        done = sum(1 for s in self.workflow.steps() if s.status == StepStatus.SKIPPED)
        if done > 0 and cur is not None:
            self._show_collision(
                f"✓ 检测到 {done} 张已拍,将从 {self._step_desc(cur.rep, cur.side)} 续拍。",
                ok=True,
            )
        else:
            self._show_collision("", ok=True)
        self._refresh()
        return True

    def _show_collision(self, text: str, ok: bool) -> None:
        if not text:
            self.lbl_collision.hide()
            self.lbl_collision.setText("")
            return
        color = "#4caf50" if ok else "#ef5350"
        self.lbl_collision.setText(text)
        self.lbl_collision.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {color};"
        )
        self.lbl_collision.show()

    def _on_reset_clicked(self) -> None:
        if self.workflow.active and not self.workflow.is_complete():
            ok = QMessageBox.question(
                self, "确认", "当前进度将丢失,确定重置?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
        self._reset_timer.stop()
        self.workflow.reset()
        self._clear_grid()
        self._show_collision("", ok=True)
        self._refresh()
        self.workflow_state_changed.emit(False)

    def _auto_reset_after_complete(self) -> None:
        was_active = self.workflow.active
        self.workflow.reset()
        self._clear_grid()
        self._show_collision("", ok=True)
        self._refresh()
        if was_active:
            self.workflow_state_changed.emit(False)
        if self._prefix_clearer is not None:
            self._prefix_clearer()

    # ---------------- 外部驱动接口 ----------------

    def apply_pass(self, score: float) -> None:
        if not self.workflow.active:
            return
        has_next = self.workflow.on_pass(score)
        self._refresh()
        if not has_next:
            self.workflow_completed.emit()
            self._reset_timer.start(self._AUTO_RESET_DELAY_MS)

    def apply_fail(self, score: float) -> None:
        if not self.workflow.active:
            return
        self.workflow.on_fail(score)
        self._refresh()

    # ---------------- 网格构造 / 更新 ----------------

    def _clear_grid(self) -> None:
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._cells = []
        self._grid_host.setVisible(False)   # 无网格时隐藏,不占空白

    def _rebuild_grid(self) -> None:
        """按 workflow.total 和 sides 重建网格(麦穗 2 行,籽粒 1 行)。"""
        self._clear_grid()
        total = self.workflow.total
        if total <= 0:
            return

        # 表头(R1, R2, ..., RN)。籽粒无行标签,角位也不留宽。
        col0 = 1 if not self._single_side else 0
        if not self._single_side:
            lbl_corner = QLabel("")
            lbl_corner.setFixedSize(24, 18)
            self._grid_layout.addWidget(lbl_corner, 0, 0)
        for c in range(total):
            lbl = QLabel(f"R{c + 1}")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #aaa; font-size: 10px;")
            lbl.setFixedHeight(18)
            self._grid_layout.addWidget(lbl, 0, col0 + c)

        # 行标签(麦穗:正/侧;籽粒:无)
        if not self._single_side:
            for row, key in enumerate(self._side_keys, start=1):
                lbl = QLabel(self._side_zh.get(key, key))
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet("color: #aaa; font-size: 11px;")
                lbl.setFixedWidth(24)
                self._grid_layout.addWidget(lbl, row, 0)

        # 单元格:与 workflow.steps() 顺序一致
        side_row = {key: i for i, key in enumerate(self._side_keys)}
        for step in self.workflow.steps():
            row = (side_row[step.side] + 1) if not self._single_side else 1
            col = (step.rep - 1) + col0
            btn = _CellButton(step.rep, step.side, parent=self._grid_host)
            btn.clicked.connect(
                lambda _checked=False, b=btn: self._on_cell_clicked(b)
            )
            self._grid_layout.addWidget(btn, row, col)
            self._cells.append(btn)

        # 显示容器:Fixed 垂直策略让它恒等于网格 sizeHint 高度,
        # 子按钮严格落在容器内,绝不溢出遮挡下方 lbl_current。
        # 不再手动 setFixedHeight —— 那是之前遮挡 bug 的根源。
        #
        # 关键顺序:先 setVisible(True) 再 activate() ——
        # 对隐藏控件 activate() 可能跳过几何计算,导致 sizeHint 取到 0/陈旧值,
        # 父布局据此把 lbl_current 摆到格子上方造成遮挡(籽粒面板更矮,
        # 启动时需增高更多,这个竞态更明显)。
        self._grid_host.setVisible(True)
        self._grid_layout.activate()
        self._grid_host.updateGeometry()
        # 同步强制所有祖先布局重排(SpikePanel → left_widget → 滚动区内容),
        # 否则只 activate 自身布局时,外层几何仍是"未含网格"的旧值,
        # 内层在被压小的旧高度里排版 → lbl_current 落到格子上。
        self._relayout_ancestors()

    def _relayout_ancestors(self) -> None:
        """从本面板向上,逐级同步重排各祖先布局,确保新网格高度即时传播。"""
        w: Optional[QWidget] = self
        for _ in range(6):
            if w is None:
                break
            lay = w.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()
            w.updateGeometry()
            w = w.parentWidget()

    def _on_cell_clicked(self, btn: _CellButton) -> None:
        if not self.workflow.active:
            return
        cur = self.workflow.current_step()
        if cur is not None and cur.rep == btn.rep and cur.side == btn.side:
            return
        desc = self._step_desc(btn.rep, btn.side)
        ok = QMessageBox.question(
            self, "跳转",
            f"跳到 {desc}?\n(继续后该格变成 当前,需覆盖式拍照)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            self.workflow.jump_to(btn.rep, btn.side)
        except Exception as e:
            QMessageBox.critical(self, "跳转", f"失败: {e}")
            return
        self._refresh()

    def _refresh(self) -> None:
        active = self.workflow.active

        if active and self._disk_checker is not None:
            self.workflow.reevaluate_disk(self._disk_checker)

        configurable = not active and not self._start_busy
        self.sp_total.setEnabled(configurable)
        self.sp_start_rep.setEnabled(configurable)
        if self.cb_start_side is not None:
            self.cb_start_side.setEnabled(configurable)
        self.btn_start.setEnabled(configurable)
        self.btn_reset.setEnabled(active)

        if active and self._cells:
            steps = self.workflow.steps()
            for cell, step in zip(self._cells, steps):
                cell.apply_status(step.status, self._tooltip_for(step))

        cur = self.workflow.current_step()
        if cur is None and not self.workflow.active:
            self.lbl_current.setText("尚未启动 — 输入品种前缀和总 Rep,点[开始]")
            self.lbl_current.setStyleSheet("color: #aaa; font-size: 11px;")
        elif self.workflow.is_complete():
            self.lbl_current.setText(
                f"✓ 完成({self.workflow.prefix},共 {self.workflow.total} Rep)"
                f" — 1.5s 后自动重置"
            )
            self.lbl_current.setStyleSheet(
                "color: #4caf50; font-size: 12px; font-weight: 600;"
            )
        elif cur is not None:
            fname = self.workflow.filename_for_current("png")
            self.lbl_current.setText(
                f"当前: {self._step_desc(cur.rep, cur.side)}  →  {fname}"
            )
            color = "#f29429" if cur.status == StepStatus.CURRENT else "#ef5350"
            self.lbl_current.setStyleSheet(
                f"color: {color}; font-size: 12px; font-weight: 600;"
            )
        else:
            # active 但既未完成又无当前格(异常态)— 明确提示,不停留在旧文案
            self.lbl_current.setText("⚠ 工作流状态异常,请点[重置]")
            self.lbl_current.setStyleSheet(
                "color: #ef5350; font-size: 12px; font-weight: 600;"
            )

    def _tooltip_for(self, step) -> str:
        head = self._step_desc(step.rep, step.side)
        if step.status == StepStatus.PENDING:
            return f"{head}\n未拍"
        if step.status == StepStatus.CURRENT:
            return f"{head}\n当前要拍"
        if step.status == StepStatus.PASSED:
            sc = f"BRISQUE={step.score:.2f}" if step.score is not None else "已拍"
            return f"{head}\n✓ 合格 ({sc})"
        if step.status == StepStatus.SKIPPED:
            return f"{head}\n已存在(未本次拍)"
        if step.status == StepStatus.FAILED:
            sc = f"BRISQUE={step.score:.2f}" if step.score is not None else ""
            return f"{head}\n✗ 不合格 {sc} — 需重拍"
        return head
