# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — RGB Capture(海康相机采集软件)。

构建方式:
    pyinstaller build.spec --distpath J:\ --workpath build_tmp --noconfirm

产物结构:
    J:\RGB_Capture\
    ├── RGB_Capture.exe
    ├── _internal\          (Python 运行时 + 依赖 + models + MvImport)
    │   ├── models\
    │   └── MvImport\
    ├── logs\               (运行时自动生成)
    └── calibration\        (运行时自动生成)
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH).resolve()

# 生成到 PyInstaller 工作目录，不修改源码树中的版本文件。EXE 运行时从
# bundle 读取这份快照，因此在没有 .git 的干净 Windows 电脑上也可追溯。
version_source = (project_root / 'core' / 'build_info.py').read_text(encoding='utf-8')
version_match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', version_source, re.MULTILINE)
app_version = version_match.group(1) if version_match else 'unknown'
try:
    commit_result = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=project_root,
        check=False, capture_output=True, text=True, timeout=5,
    )
    git_commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None
except (OSError, subprocess.SubprocessError):
    git_commit = None
work_path_value = globals().get('workpath')
generated_dir = Path(work_path_value) if work_path_value else project_root / 'build'
generated_dir.mkdir(parents=True, exist_ok=True)
build_info_file = generated_dir / 'build_info.json'
build_info_file.write_text(json.dumps({
    'app_version': app_version,
    'built_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'git_commit': git_commit,
}, ensure_ascii=False, indent=2), encoding='utf-8')

a = Analysis(
    ['main.py'],
    pathex=[
        str(project_root),
        str(project_root / 'MvImport'),
    ],
    binaries=[],
    datas=[
        # 海康 Python 绑定(import 不出来,要靠 datas 拷进去)
        ('MvImport', 'MvImport'),
        # BRISQUE 模型文件
        ('models', 'models'),
        # 构建版本快照
        (str(build_info_file), '.'),
    ],
    hiddenimports=[
        # 海康 SDK 内部模块,PyInstaller 静态扫描扫不到
        'MvCameraControl_class',
        'CameraParams_const',
        'CameraParams_header',
        'MvErrorDefine_const',
        'MvISPErrorDefine_const',
        'PixelType_header',
        # 挂牌 API：显式触发 PyInstaller 的 QtNetwork hook，
        # 一并收集 Qt6Network、networkinformation 与 TLS 插件。
        'PyQt6.QtNetwork',
        # TIFF 单次编码与标准 XMP(XMLPacket tag 700)
        'tifffile',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 拒绝把无关大包打进来,节约体积
        'tkinter', 'matplotlib', 'scipy', 'pandas',
        'IPython', 'pytest', 'sphinx',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RGB_Capture',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 经常和 PyQt 闹脾气,不用
    console=False,             # GUI 模式,不开黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon=None,  # 后续如果有 .ico 文件,写在这里
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='RGB_Capture',
)
