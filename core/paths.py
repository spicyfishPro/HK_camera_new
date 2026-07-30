"""运行时路径解析,兼容 dev 模式和 PyInstaller 打包后。

两类路径:
    user_data_dir(): **可写**的用户数据目录(logs、calibration、capture_log.csv)
        - dev:           项目根 HK_camera_new/
        - frozen (exe):  exe 所在目录(用户能直接看到)

    bundle_dir():    **只读**的资源目录(BRISQUE 模型 等)
        - dev:           项目根 HK_camera_new/
        - frozen:        sys._MEIPASS(PyInstaller 解压目录)

设计原因:
    - 用户数据放 exe 旁边:用户右键 exe → "打开文件位置" 就能看到 logs/、calibration/
    - 只读资源进 PyInstaller bundle:让打包后整个程序自包含,模型文件不会丢
"""
from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    """是不是 PyInstaller 打包运行?"""
    return getattr(sys, "frozen", False)


def user_data_dir() -> Path:
    """可写的用户数据目录。打包后 = exe 所在目录;dev = 项目根。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    # 项目根 = core/paths.py 的爷爷目录
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """打包进 exe 的只读资源目录。"""
    if _is_frozen():
        # PyInstaller 把 datas 解压到 sys._MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# ---- 派生路径(模块导入时一次性求值)----
ROOT_USER: Path = user_data_dir()
ROOT_BUNDLE: Path = bundle_dir()

LOGS_DIR: Path = ROOT_USER / "logs"
CALIB_DIR: Path = ROOT_USER / "calibration"
CAPTURE_LOG: Path = LOGS_DIR / "capture_log.csv"

MODELS_DIR: Path = ROOT_BUNDLE / "models"
BRISQUE_MODEL: Path = MODELS_DIR / "brisque_model_live.yml"
BRISQUE_RANGE: Path = MODELS_DIR / "brisque_range_live.yml"
MVIMPORT_DIR: Path = ROOT_BUNDLE / "MvImport"
