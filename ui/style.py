"""深色主题:用 QPalette 控制基础配色,Fusion 风格自动渲染控件原生箭头;
QSS 只覆盖 palette 控不到的地方(tab 下划线、GroupBox 边框、状态栏等)。"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette


# ---- 调色板 -------------------------------------------------------------

_BG = QColor(43, 43, 43)         # 主背景 #2b2b2b
_BG_ALT = QColor(35, 35, 35)     # 输入框背景 #232323
_BG_BTN = QColor(58, 58, 58)     # 按钮背景 #3a3a3a
_BG_TIP = QColor(53, 53, 53)     # tooltip 背景 #353535
_FG = QColor(216, 216, 216)      # 主文字 #d8d8d8
_FG_DIS = QColor(120, 120, 120)  # 禁用文字
_FG_PLACE = QColor(120, 120, 120)
_ACCENT = QColor(242, 148, 41)   # 海康风的橙色 #f29429
_ACCENT_FG = QColor(26, 26, 26)  # 选中态前景(黑底字)


def build_dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, _BG)
    p.setColor(QPalette.ColorRole.WindowText, _FG)
    p.setColor(QPalette.ColorRole.Base, _BG_ALT)
    p.setColor(QPalette.ColorRole.AlternateBase, _BG)
    p.setColor(QPalette.ColorRole.ToolTipBase, _BG_TIP)
    p.setColor(QPalette.ColorRole.ToolTipText, _FG)
    p.setColor(QPalette.ColorRole.Text, _FG)
    p.setColor(QPalette.ColorRole.Button, _BG_BTN)
    p.setColor(QPalette.ColorRole.ButtonText, _FG)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 100, 100))
    p.setColor(QPalette.ColorRole.Highlight, _ACCENT)
    p.setColor(QPalette.ColorRole.HighlightedText, _ACCENT_FG)
    p.setColor(QPalette.ColorRole.PlaceholderText, _FG_PLACE)
    p.setColor(QPalette.ColorRole.Link, _ACCENT)
    p.setColor(QPalette.ColorRole.Mid, QColor(80, 80, 80))
    p.setColor(QPalette.ColorRole.Light, QColor(70, 70, 70))
    p.setColor(QPalette.ColorRole.Dark, QColor(25, 25, 25))
    p.setColor(QPalette.ColorRole.Shadow, QColor(15, 15, 15))

    # 禁用态
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, _FG_DIS)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(38, 38, 38))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor(47, 47, 47))
    return p


# ---- 极简 QSS:不动 SpinBox/ComboBox 内部子控件,只补 palette 缺位 ----------

DARK_QSS = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", "Helvetica Neue", sans-serif;
    font-size: 12px;
}

QMainWindow, QWidget {
    background-color: #2b2b2b;
}

/* ---------- TabWidget ---------- */
QTabWidget::pane {
    border: 1px solid #3a3a3a;
    background: #2b2b2b;
    top: -1px;
}
QTabBar { background: transparent; }
QTabBar::tab {
    background: #2b2b2b;
    color: #b0b0b0;
    padding: 8px 18px;
    border: none;
    min-width: 80px;
}
QTabBar::tab:hover { color: #ffffff; }
QTabBar::tab:selected {
    color: #ffffff;
    border-bottom: 2px solid #f29429;
}

/* ---------- GroupBox ---------- */
QGroupBox {
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 8px;
    background-color: #2b2b2b;
    color: #c8c8c8;
    font-weight: 500;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: -2px;
    padding: 0 6px;
    background-color: #2b2b2b;
    color: #c8c8c8;
}

/* ---------- PushButton(QPalette 覆盖不到圆角和 padding) ---------- */
QPushButton {
    border: 1px solid #4a4a4a;
    border-radius: 3px;
    padding: 5px 14px;
    min-height: 22px;
}
QPushButton:hover {
    border-color: #f29429;
}
QPushButton:disabled {
    border-color: #3a3a3a;
}

/* ---------- 输入框聚焦时的橙色高亮(palette 不管这个) ---------- */
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #f29429;
}

/* ---------- ListWidget 行间距等(配色走 palette) ---------- */
QListWidget {
    border: 1px solid #3a3a3a;
    border-radius: 3px;
    padding: 2px;
    outline: 0;
}
QListWidget::item { padding: 4px 6px; }

/* ---------- StatusBar ---------- */
QStatusBar {
    background-color: #232323;
    color: #b0b0b0;
    border-top: 1px solid #3a3a3a;
}
QStatusBar QLabel {
    color: #b0b0b0;
    padding: 0 8px;
}

/* ---------- TabWidget Pane 与 Tab 衔接 ---------- */
QTabWidget > QWidget { background: #2b2b2b; }

/* ---------- ComboBox 下拉列表(覆盖 palette,避免选中项黑字) ---------- */
QComboBox QAbstractItemView {
    background-color: #2b2b2b;
    color: #d8d8d8;
    selection-background-color: #4a4a4a;
    selection-color: #ffffff;
    border: 1px solid #4a4a4a;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 4px 8px;
    color: #d8d8d8;
}
QComboBox QAbstractItemView::item:selected,
QComboBox QAbstractItemView::item:hover {
    background-color: #4a4a4a;
    color: #ffffff;
}

/* ---------- ToolTip ---------- */
QToolTip {
    background-color: #353535;
    color: #e0e0e0;
    border: 1px solid #4a4a4a;
    padding: 4px 8px;
}
"""
