"""麦穗工作流状态机。

每个品种 N 个生物学重复,每个重复采 front + side 两张,共 2N 步。
顺序固定:Rep1.front → Rep1.side → Rep2.front → ... → RepN.side。
BRISQUE 不合格则停在原格、覆盖式重拍。

"绿色"语义 = 该图实际存在于磁盘上(PASSED:本次拍的;SKIPPED:已存在的);
非 CURRENT/PASSED/FAILED 的格子,任何时候都按"磁盘上有没有这张图"判定灰/绿。

纯数据 + 逻辑,**不依赖 Qt**;主线程独占调用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional
from uuid import uuid4


class StepStatus(Enum):
    PENDING = "pending"   # 灰:还没到
    CURRENT = "current"   # 橙:当前要拍
    PASSED = "passed"     # 绿:实拍合格
    SKIPPED = "skipped"   # 绿:起点之前手动跳过(未拍)
    FAILED = "failed"     # 红:实拍不合格,需覆盖式重拍
    SAVING = "saving"
    SAVE_FAILED = "save_failed"


# 默认相位(麦穗:正/反两张)。籽粒用 ("",) 表示"每个重复一张、无正反"。
DEFAULT_SIDES: tuple[str, ...] = ("front", "side")


@dataclass
class Step:
    rep: int                       # 1-based
    side: str                      # "front" / "side";籽粒为 ""
    status: StepStatus = StepStatus.PENDING
    score: Optional[float] = None  # 实拍后的 BRISQUE,SKIPPED/PENDING 时为 None
    capture_id: str | None = None
    quality_passed: bool | None = None
    saved: bool = False
    overwrite_allowed: bool = False

    @property
    def label(self) -> str:
        """短标签,UI 看板用。"""
        if not self.side:
            return f"R{self.rep}"
        s = "正" if self.side == "front" else "侧"
        return f"R{self.rep}·{s}"


def _step_index(rep: int, side: str, total: int, sides: tuple[str, ...]) -> int:
    if not 1 <= rep <= total:
        raise ValueError(f"rep {rep} 超出 [1, {total}]")
    if side not in sides:
        raise ValueError(f"side 必须属于 {sides}: {side!r}")
    return (rep - 1) * len(sides) + sides.index(side)


class SpikeWorkflow:
    """麦穗采集工作流。

    使用约定:
      - 主线程独占调用
      - 不知道相机/磁盘,只负责"现在该拍什么、命名是什么、推进还是失败"
      - 完成后 active 保持 True,直到调用 reset()
    """

    def __init__(self) -> None:
        self._prefix: str = ""
        self._total: int = 0
        self._sides: tuple[str, ...] = DEFAULT_SIDES
        self._steps: list[Step] = []
        self._cur: int = -1   # 当前 step 在 _steps 中的索引;-1 表示未启动
        self._start_idx: int = 0  # 用户选定的起点索引;is_complete 只看起点及之后
        self._active: bool = False
        self.session_id = uuid4().hex

    # ---------- 配置 / 启动 ----------

    def start(
        self,
        prefix: str,
        total: int,
        start_rep: int = 1,
        start_side: str = "front",
        sides: tuple[str, ...] = DEFAULT_SIDES,
    ) -> None:
        if not prefix or not prefix.strip():
            raise ValueError("prefix 不能为空")
        if total < 1:
            raise ValueError("total 必须 >= 1")
        if not sides:
            raise ValueError("sides 不能为空")
        idx = _step_index(start_rep, start_side, total, sides)

        self._prefix = prefix
        self.session_id = uuid4().hex
        self._total = total
        self._sides = tuple(sides)
        # 构造 len(sides)*N 个 step,默认 PENDING;
        # 真正的 SKIPPED(绿) 由 reevaluate_disk() 按磁盘是否存在文件判定
        self._steps = [
            Step(rep=r, side=s)
            for r in range(1, total + 1)
            for s in self._sides
        ]
        self._steps[idx].status = StepStatus.CURRENT
        self._cur = idx
        self._start_idx = idx
        self._active = True

    def reset(self) -> None:
        self._prefix = ""
        self._total = 0
        self._sides = DEFAULT_SIDES
        self._steps = []
        self._cur = -1
        self._start_idx = 0
        self._active = False

    @property
    def sides(self) -> tuple[str, ...]:
        return self._sides

    # ---------- 查询 ----------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def total(self) -> int:
        return self._total

    @property
    def current_index(self) -> int:
        return self._cur

    def current_step(self) -> Optional[Step]:
        """返回当前要拍的格子。

        CURRENT(待拍)或 FAILED(评分没过、需覆盖式重拍)都算"当前要拍的"。
        若格子是 PASSED/SKIPPED/PENDING(已完成、整套都已存在等)则返回 None
        —— 防止 filename_for_current 拿到已拍格子的文件名而误覆盖。"""
        if not self._active or self._cur < 0 or self._cur >= len(self._steps):
            return None
        step = self._steps[self._cur]
        if step.status in (StepStatus.CURRENT, StepStatus.FAILED):
            return step
        return None

    def steps(self) -> list[Step]:
        """浅拷贝;读者不要原地改 status,用 on_pass / on_fail / jump_to。"""
        return list(self._steps)

    def is_complete(self) -> bool:
        """起点(含)之后的格子都是 PASSED 或 SKIPPED 时视为完成。

        只看 _start_idx 及之后:用户若从 Rep4 起拍,Rep1-3 即便磁盘上无文件
        (灰/PENDING)也不应阻止"本次任务完成"的判定,否则会永远完不成、
        前缀也永远不自动清空。
        """
        if not self._active or not self._steps:
            return False
        return all(
            s.status in (StepStatus.PASSED, StepStatus.SKIPPED)
            for s in self._steps[self._start_idx:]
        )

    def filename_for_current(self, ext: str = "png") -> Optional[str]:
        """生成当前格子的固定文件名(无时间戳、无 serial)。
        麦穗:prefix_Rep{N}_{side}.ext;籽粒(side=''):prefix_Rep{N}.ext。"""
        s = self.current_step()
        if s is None:
            return None
        ext_clean = ext.lstrip(".").lower()
        suffix = f"_{s.side}" if s.side else ""
        return f"{self._prefix}_Rep{s.rep}{suffix}.{ext_clean}"

    # ---------- 推进 ----------

    def bind_capture(self, capture_id: str) -> None:
        step = self.current_step()
        if step is None:
            raise RuntimeError("当前没有待拍位置")
        step.capture_id = capture_id
        step.quality_passed = None
        step.saved = False

    def apply_capture_result(self, session: str, capture_id: str, *,
                             passed: bool | None = None, score: float | None = None,
                             saved: bool = False, save_failed: bool = False) -> bool:
        if session != self.session_id or not self.active:
            return False
        step = next((s for s in self._steps if s.capture_id == capture_id), None)
        if step is None:
            return False  # 重拍旧任务不能更新新任务状态。
        if passed is not None:
            first_quality = step.quality_passed is None
            step.quality_passed = passed
            step.score = score
            step.status = StepStatus.SAVING if passed else StepStatus.FAILED
            if passed and first_quality and self._steps[self._cur] is step:
                nxt = self._cur + 1
                while nxt < len(self._steps) and self._steps[nxt].status in (
                        StepStatus.SKIPPED, StepStatus.PASSED, StepStatus.SAVING):
                    nxt += 1
                if nxt < len(self._steps):
                    self._cur = nxt
                    self._steps[nxt].status = StepStatus.CURRENT
        if saved:
            step.saved = True
            step.status = StepStatus.PASSED if step.quality_passed else StepStatus.FAILED
        if save_failed:
            step.status = StepStatus.SAVE_FAILED
        return True

    def on_pass(self, score: float) -> bool:
        """当前格变 PASSED,推进到下一格。
        返回:还有下一格 = True;已是最后一格(整体完成) = False。"""
        if not self._active or self._cur < 0:
            return False
        self._steps[self._cur].status = StepStatus.PASSED
        self._steps[self._cur].score = score
        # 推进时跳过已存在(SKIPPED,磁盘上已有照片)的格子 —— 否则会把它设成
        # CURRENT 诱导用户覆盖已拍好的照片(违反防覆盖)。
        nxt = self._cur + 1
        while nxt < len(self._steps) and self._steps[nxt].status == StepStatus.SKIPPED:
            nxt += 1
        if nxt < len(self._steps):
            self._cur = nxt
            self._steps[self._cur].status = StepStatus.CURRENT
            return True
        # 完成 — _cur 留在最后一格(已 PASSED),不再前进
        return False

    def on_fail(self, score: float) -> None:
        """当前格变 FAILED,不推进,等覆盖式重拍。"""
        if not self._active or self._cur < 0:
            return
        self._steps[self._cur].status = StepStatus.FAILED
        self._steps[self._cur].score = score

    def jump_to(self, rep: int, side: str) -> None:
        """跳到任意格。原 CURRENT 格回退成 PENDING(留给 reevaluate_disk 决定灰/绿)。
        目标格变 CURRENT,已 PASSED/FAILED 状态在其它格上保留。"""
        if not self._active:
            raise RuntimeError("工作流未启动")
        idx = _step_index(rep, side, self._total, self._sides)
        if idx == self._cur:
            return
        cur = self._steps[self._cur]
        # 原当前格(CURRENT 或失败 FAILED)都回退,交给 reevaluate_disk 判灰/绿,
        # 否则跳走后会残留一个红色 FAILED 死格。
        if cur.status in (StepStatus.CURRENT, StepStatus.FAILED):
            cur.status = StepStatus.PENDING
        self._steps[idx].status = StepStatus.CURRENT
        self._steps[idx].score = None
        self._steps[idx].overwrite_allowed = True
        self._cur = idx

    def reevaluate_disk(self, checker: Callable[[int, str], bool]) -> None:
        """按 checker 重新判定每个 PENDING/SKIPPED 格子的状态。
        PASSED/FAILED/CURRENT 不动 — 这三种是本会话的真实状态,不被磁盘覆盖。

        checker(rep, side) → True 表示对应文件已存在。
        """
        if not self._active:
            return
        for s in self._steps:
            if s.status in (StepStatus.PASSED, StepStatus.FAILED, StepStatus.CURRENT,
                            StepStatus.SAVING, StepStatus.SAVE_FAILED):
                continue
            s.status = StepStatus.SKIPPED if checker(s.rep, s.side) else StepStatus.PENDING

    def resume_from_disk(self, checker: Callable[[int, str], bool]) -> None:
        """断点续拍 — 仅在 start() 之后调用一次。

        与 reevaluate_disk 不同:本方法会**移动 CURRENT 指针**到
        "从用户选定的起点往后、磁盘上第一张不存在的照片",从而实现
        关软件再打开、设置相同前缀+文件夹后,自动跳到该拍的下一张。

        - 磁盘已存在的格子 → SKIPPED(绿)
        - 起点后第一张缺失的 → CURRENT(橙)
        - 起点后全部已存在 → 无 CURRENT(is_complete() 返回 True)

        本会话已 PASSED/FAILED 的状态保留(不被磁盘判定覆盖)。
        """
        if not self._active:
            return
        start_idx = self._cur
        # 1) 按磁盘标记所有非本会话状态的格子(含原 CURRENT)
        for s in self._steps:
            if s.status in (StepStatus.PASSED, StepStatus.FAILED):
                continue
            s.status = StepStatus.SKIPPED if checker(s.rep, s.side) else StepStatus.PENDING
        # 2) 从起点往后找第一张未拍(PENDING)→ 设为 CURRENT
        new_cur = next(
            (i for i in range(start_idx, len(self._steps))
             if self._steps[i].status == StepStatus.PENDING),
            None,
        )
        if new_cur is not None:
            self._steps[new_cur].status = StepStatus.CURRENT
            self._cur = new_cur
        # else: 起点后全部已拍 → 不设 CURRENT,is_complete() 自然为 True
