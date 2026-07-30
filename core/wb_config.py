"""白平衡预设管理。

两层结构:
    FACTORY_WB:写死在代码里 = 软件出厂默认
    user-saved: calibration/wb_user.json,一键白平衡后自动写

获取顺序 = user → factory(fallback)。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .paths import CALIB_DIR

log = logging.getLogger(__name__)


# ============================================================
# 出厂默认 — MV-CH250-90UC 的 BalanceRatio 是整数节点
# (典型范围 0-4095,1024 约对应 1.0× 增益)
# 现在写的是 unity gain(R=G=B=1024)≈ 不校正
# 你校好后把 wb_user.json 里的三个数复制到这里,即可成为软件出厂默认
# ============================================================

# 这组是 2026-05-16 在 MV-CH250-90UC + 环形灯环境下,一键白平衡稳定收敛的结果。
# 任何人点"恢复出厂"都回到这一组。
FACTORY_RED: int = 1980
FACTORY_GREEN: int = 1024
FACTORY_BLUE: int = 2259


_USER_FILE = CALIB_DIR / "wb_user.json"


@dataclass
class WBParams:
    red: int
    green: int
    blue: int

    def as_camera_dict(self) -> dict[str, int]:
        """返回 {'Red': ..., 'Green': ..., 'Blue': ...},方便循环写相机。"""
        return {"Red": self.red, "Green": self.green, "Blue": self.blue}

    def __str__(self) -> str:
        return f"R={self.red} G={self.green} B={self.blue}"


def factory_wb() -> WBParams:
    return WBParams(FACTORY_RED, FACTORY_GREEN, FACTORY_BLUE)


def load_user_wb() -> Optional[WBParams]:
    if not _USER_FILE.exists():
        return None
    try:
        data = json.loads(_USER_FILE.read_text(encoding="utf-8"))
        return WBParams(int(data["red"]), int(data["green"]), int(data["blue"]))
    except Exception:
        log.exception("读取用户白平衡失败: %s", _USER_FILE)
        return None


def save_user_wb(wb: WBParams) -> Path:
    _USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USER_FILE.write_text(json.dumps(asdict(wb), indent=2), encoding="utf-8")
    log.info("用户白平衡已保存: %s -> %s", wb, _USER_FILE)
    return _USER_FILE


def delete_user_wb() -> bool:
    if _USER_FILE.exists():
        _USER_FILE.unlink()
        log.info("用户白平衡已删除")
        return True
    return False


def active_wb() -> tuple[WBParams, str]:
    """返回当前应当使用的白平衡参数和来源('user' / 'factory')。"""
    u = load_user_wb()
    if u is not None:
        return u, "user"
    return factory_wb(), "factory"
