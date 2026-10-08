"""当前材料照片索引：精确命名识别、主图/原图配对和有界缩略图缓存。"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CatalogPhoto:
    path: Path
    rep: int | None
    side: str | None
    raw_path: Path | None = None
    modified: float = 0.0


def scan_material(folder: Path, prefix: str) -> list[CatalogPhoto]:
    if not prefix or not folder.is_dir():
        return []
    head = re.escape(prefix)
    fixed = re.compile(
        rf"^{head}_Rep(?P<rep>\d+)(?:_(?P<side>front|side))?(?P<raw>_raw)?$"
    )
    legacy = re.compile(
        rf"^{head}(?:_R(?P<rep>\d+))?(?:_(?P<side>front|side))?"
        r"_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}_\d+(?:_\d+)?(?P<raw>_raw)?$"
    )
    photos = []
    paths = {
        p.name: p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    }
    for path in paths.values():
        match = fixed.fullmatch(path.stem) or legacy.fullmatch(path.stem)
        if not match:
            if path.stem == prefix:
                photos.append(
                    CatalogPhoto(path, None, None, modified=path.stat().st_mtime)
                )
            continue
        if match["raw"]:
            continue
        raw = paths.get(path.stem + "_raw" + path.suffix)
        photos.append(
            CatalogPhoto(
                path,
                int(match["rep"]) if match["rep"] else None,
                match["side"],
                raw,
                path.stat().st_mtime,
            )
        )
    return sorted(
        photos,
        key=lambda p: (p.rep or 10**9, p.side == "side", -p.modified, p.path.name),
    )


class ThumbnailCache:
    def __init__(self, max_bytes=32 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.bytes = 0
        self._items = OrderedDict()

    def get(self, key):
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
            return item[0]
        return None

    def put(self, key, value, size):
        old = self._items.pop(key, None)
        if old:
            self.bytes -= old[1]
        if size > self.max_bytes:
            return
        self._items[key] = (value, size)
        self.bytes += size
        while self.bytes > self.max_bytes:
            _, (_, removed) = self._items.popitem(last=False)
            self.bytes -= removed

    def clear(self):
        self._items.clear()
        self.bytes = 0
