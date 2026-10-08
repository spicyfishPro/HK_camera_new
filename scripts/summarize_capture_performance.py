"""汇总 2.2 抓拍日志；区分界面再次可拍与后台保存完成时间。只读。"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import statistics
from pathlib import Path


def stats(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "median_s": round(statistics.median(ordered), 4),
        "p95_s": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 4),
        "max_s": round(ordered[-1], 4),
    }


def summarize(text):
    ready = []
    blocked = 0
    fields = {
        k: []
        for k in (
            "snapshot_s",
            "ffc_s",
            "rotate_s",
            "quality_s",
            "ready_s",
            "queue_s",
            "encode_s",
            "write_s",
            "total_s",
        )
    }
    pending = []
    memory = []
    for line in text.splitlines():
        match = re.search(
            r"capture ready .*?ready=([0-9.]+)s available=(True|False)", line
        )
        if match:
            if match[2] == "True":
                ready.append(float(match[1]))
            else:
                blocked += 1
        match = re.search(
            r"capture perf .*?timings=(\{.*?\}) pending=(\d+) image_bytes=(\d+)", line
        )
        if match:
            try:
                timings = ast.literal_eval(match[1])
            except (ValueError, SyntaxError):
                continue
            for name in fields:
                if name in timings:
                    fields[name].append(float(timings[name]))
            pending.append(int(match[2]))
            memory.append(int(match[3]))
    return {
        "ui_ready_when_available": stats(ready),
        "unavailable_after_quality_count": blocked,
        "stages": {k: stats(v) for k, v in fields.items()},
        "observed_pending_max": max(pending, default=0),
        "observed_image_mib_max": round(max(memory, default=0) / 1024**2, 1),
        "note": "队列数和内存为保存完成时采样，不能代表进程峰值；真机另监测任务管理器。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            summarize(args.log.read_text(encoding="utf-8", errors="replace")),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
