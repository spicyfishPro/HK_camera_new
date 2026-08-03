"""应用版本与构建来源信息。

开发模式下读取当前 Git 提交；PyInstaller 构建会把 ``build_info.json``
打进只读资源目录，从而让离线 Windows EXE 仍能报告准确的构建来源。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from .paths import ROOT_BUNDLE


APP_VERSION = "2.1.0"


@dataclass(frozen=True)
class BuildInfo:
    app_version: str
    built_at_utc: str | None
    git_commit: str | None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    bundled = ROOT_BUNDLE / "build_info.json"
    if bundled.is_file():
        try:
            data = json.loads(bundled.read_text(encoding="utf-8"))
            return BuildInfo(
                app_version=_clean_text(data.get("app_version")) or APP_VERSION,
                built_at_utc=_clean_text(data.get("built_at_utc")),
                git_commit=_clean_text(data.get("git_commit")),
            )
        except (OSError, ValueError, TypeError):
            pass

    commit: str | None = None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT_BUNDLE,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if completed.returncode == 0:
            commit = _clean_text(completed.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return BuildInfo(
        app_version=APP_VERSION,
        built_at_utc=None,
        git_commit=commit,
    )
