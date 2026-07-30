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
from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH).resolve()

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
