"""目录归属与扫码分目录；标记中不保存凭证。"""
from __future__ import annotations

import hashlib
import json
import re
import os
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from .field_tag_lookup import validate_windows_filename_segment
from pathlib import Path

MARKER = '.capture-context.json'


class StorageError(ValueError):
    pass


def safe_segment(text: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(text)).strip(' .') or '未命名'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '_' + value
    # 按 UTF-16 控制 Windows 路径长度，保留可辨认名称及稳定摘要。
    if len(value.encode('utf-16-le')) > 100:
        prefix = value.encode('utf-16-le')[:80].decode('utf-16-le', errors='ignore')
        value = prefix + '_' + hashlib.sha256(value.encode()).hexdigest()[:8]
    return value


def read_binding(folder: Path):
    marker = folder / MARKER
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('version') not in (1, 2):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise StorageError('目录归属记录损坏，请选择其他目录。') from None


def bind(folder: Path, context: dict) -> Path:
    context = {'version': 2, **context}
    if folder.is_symlink():
        raise StorageError('保存目录不能是符号链接，请选择实际目录。')
    folder.mkdir(parents=True, exist_ok=True)
    existing = read_binding(folder)
    if existing is not None:
        if existing != context:
            raise StorageError('目录属于旧版或其他种植表、类型、拍摄人，请选择新的空根目录。')
        return folder
    if any(p.name not in ('.DS_Store', 'Thumbs.db', 'desktop.ini') for p in folder.iterdir()):
        raise StorageError('没有归属记录的非空目录不能启用统计，请选择新的空目录。')
    try:
        with (folder / MARKER).open('x', encoding='utf-8') as stream:
            json.dump(context, stream, ensure_ascii=False, sort_keys=True)
    except FileExistsError:
        if read_binding(folder) != context:
            raise StorageError('目录归属发生变化，请重新选择目录。') from None
    return folder


def resolve_directory(root: Path, *, info, mode: str) -> Path:
    if info.is_ck:
        raise StorageError('CK 不需要采集照片。')
    if mode not in ('wheat', 'grain'):
        raise StorageError('采集类型无效。')
    scope = {'planting_plan_id': info.planting_plan_id, 'year': info.year, 'location': info.location}
    population, photographer = info.planting_population, info.photographer
    bind(root, {**scope, 'kind': 'planting-root'})
    child_context = {'version': 2, **scope, 'kind': 'planting-group', 'mode': mode,
                     'population_id': population['id'] if population else None,
                     'photographer_id': photographer['id']}
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink() and read_binding(child) == child_context:
            return child
    name = '_'.join((safe_segment(population['name'] if population else '无群体'),
                     '麦穗' if mode == 'wheat' else '籽粒', safe_segment(photographer['display_name'])))
    folder = root / name
    if any(p.name.casefold() == name.casefold() for p in root.iterdir()):
        digest = hashlib.sha256(json.dumps(child_context, sort_keys=True).encode()).hexdigest()[:10]
        folder = root / (name + '_' + digest)
    if len(str(folder).encode('utf-16-le')) // 2 > 200:
        raise StorageError('群体目录路径过长，请选择更短的保存根目录。')
    return bind(folder, {k: v for k, v in child_context.items() if k != 'version'})


@contextmanager
def _rows_lock(folder):
    with (folder / '.capture-rows.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class RowStorage:
    folder: Path
    prefix: str
    extension: str
    notice: str = ''


def prepare_row_storage(root: Path, *, info, mode: str, extension: str) -> RowStorage:
    """Reserve a human-readable prefix for exactly one verified planting row."""
    extension = extension.lower()
    if extension not in ('png', 'jpg', 'tiff'):
        raise StorageError('照片格式无效。')
    folder = resolve_directory(root, info=info, mode=mode)
    path = folder / '.capture-rows.json'
    identity = {**info.identity_payload(), 'field_tag_code': info.code}
    if path.is_symlink():
        raise StorageError('种植行归属文件不能是符号链接。')
    key = str(info.planting_entry_id)
    prefix = info.filename_prefix
    notice = ''
    with _rows_lock(folder):
        try:
            index = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version': 2, 'rows': {}}
            if index.get('version') != 2 or not isinstance(index.get('rows'), dict):
                raise ValueError
        except (ValueError, OSError, AttributeError) as exc:
            raise StorageError('种植行文件归属记录损坏，不能猜测续拍。') from exc
        old = index['rows'].get(key)
        if old is not None:
            if not isinstance(old, dict) or old.get('extension') not in ('png', 'jpg', 'tiff'):
                raise StorageError('种植行文件归属记录损坏，不能猜测续拍。')
            validate_windows_filename_segment(old.get('prefix'))
            if old.get('identity') != identity:
                raise StorageError('该田间行的材料或群体关联已变更，请核对旧照片后使用新根目录。')
            if old['prefix'] != prefix or old['extension'] != extension:
                notice = f"沿用此田间行已绑定的文件名 {old['prefix']} 和 {old['extension'].upper()} 格式；当前成员号为 {info.accession_no}，第 {info.row_no} 行。"
            return RowStorage(folder, old['prefix'], old['extension'], notice)
        if any(row['prefix'].casefold() == prefix.casefold() for row in index['rows'].values()):
            raise StorageError('文件名前缀已归属于另一田间行，不能覆盖或续拍。')
        if any(p.name.casefold().startswith((prefix + '_Rep').casefold()) for p in folder.iterdir()):
            raise StorageError('该文件名前缀已有未绑定照片，不能认定为本行已拍，请选择新根目录。')
        longest = folder / f'{prefix}_Rep20_front_raw.tiff'
        if len(str(longest).encode('utf-16-le')) // 2 > 240:
            raise StorageError('照片完整路径过长，请选择更短的保存根目录。')
        index['rows'][key] = {'identity': identity, 'prefix': prefix, 'extension': extension,
                             'row_no': info.row_no, 'current_material_no': info.accession_no}
        temporary = path.with_name('.capture-rows.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(index, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return RowStorage(folder, prefix, extension, notice)
