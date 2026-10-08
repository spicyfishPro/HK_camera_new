# 第三方来源、许可证与分发范围

核验日期：2026-10-08。

## 本项目原创内容

Copyright (C) 2026 RGB Capture contributors.

除有独立声明的第三方内容外，本仓库原创 Python 代码、测试、构建脚本和项目文档
采用 **GNU General Public License version 3 only，SPDX：GPL-3.0-only**，全文见 [LICENSE](LICENSE)。
软件按该许可“不提供担保”条款提供。贡献者仅许可自己有权许可的内容。

这个授权不覆盖 `MvImport/` 中的海康原始文件、`models/` 中的第三方模型、
`licenses/` 中复制的上游声明或任何第三方依赖；它们继续按各自条款授权。
本项目没有授予海康 SDK 例外，也没有声称能替 PyQt 或其他第三方授予例外。
此声明针对包含它的版本，不追溯更改旧标签或历史快照的许可。

**当前完整软件尚不具备已确认的公开再分发条件。**
海康安装包协议限制分发和组合使用，尚未发现适用于 Python 绑定的许可例外；
GPL 版 PyQt6 与专有 SDK 的组合分发也需要解决。
完整证据见 [海康 SDK 核验](docs/licensing/HIKROBOT.md)。添加 GPL 文本不等于这些问题已获授权。

## 组件清单

| 组件及版本 | 来源 | 许可与说明 |
| --- | --- | --- |
| MvImport，开发 SDK 4.7.0.3 / MVS 5.0.0 | [海康机器人](https://www.hikrobotics.com/en/machinevision/service/download/?module=0) | 专有；7 个文件与安装包一致；公开再分发未获确认 |
| MvCameraControl 原生运行库 | 用户另行安装的 MVS Runtime | 专有，按用户实际安装版本的协议；本项目未捆绑海康运行库安装程序 |
| PyQt6 6.11.0 | [Riverbank](https://www.riverbankcomputing.com/software/pyqt/)、[源码分发](https://pypi.org/project/PyQt6/6.11.0/#files) | 本项目使用 PyPI 的 GPLv3 版本，没有商业许可证凭据；PyQt 不是 LGPL |
| Qt 6.11.2（PyQt6-Qt6） | [Riverbank wheel](https://pypi.org/project/PyQt6-Qt6/6.11.2/#files)、[Qt 源码](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/) | 所用 Qt 库采用开源 LGPLv3 路径；另有组件专属及第三方条款，不能将所有 Qt 模块一概视为 LGPL |
| PyQt6-sip 13.13.0 | [Riverbank / PyPI](https://pypi.org/project/PyQt6-sip/13.13.0/#files) | BSD-2-Clause；与 PyQt6 本体授权分开 |
| opencv-contrib-python 4.13.0.92 | [OpenCV Python 构建仓库](https://github.com/opencv/opencv-python)、[源码/wheel](https://pypi.org/project/opencv-contrib-python/4.13.0.92/#files) | Python 打包层 MIT；OpenCV/OpenCV contrib 4.13 为 Apache-2.0；wheel 内编解码等另有许可 |
| BRISQUE 模型及算法来源 | [模型溯源](models/README.md)、[原始实现声明](licenses/BRISQUE-UT-Austin.txt) | 上游 OpenCV contrib 许可；保留 UT Austin 原始声明和科研引用要求，不替换为项目 GPL |
| NumPy 2.4.4 | [NumPy](https://github.com/numpy/numpy/tree/v2.4.4)、[分发包](https://pypi.org/project/numpy/2.4.4/#files) | BSD-3-Clause 为主，附带代码还涉及 0BSD、MIT、Zlib、CC0 等；以所用 wheel 的完整许可树为准 |
| tifffile 2026.7.14 | [上游](https://github.com/cgohlke/tifffile)、[分发包](https://pypi.org/project/tifffile/2026.7.14/#files) | BSD-3-Clause |
| CPython 3.12 | [Python](https://www.python.org/downloads/)、[源码](https://github.com/python/cpython/tree/3.12) | PSF 及历史许可、随附第三方许可；实际补丁版本见构建清单 |
| PyInstaller 6.20.0 | [上游](https://github.com/pyinstaller/pyinstaller/tree/v6.20.0)、[许可说明](https://pyinstaller.org/en/stable/license.html) | GPL-2.0-or-later，附 bootloader/loader/runtime hook 例外；例外不改变打包应用自身及其依赖的授权 |
| pyinstaller-hooks-contrib | [上游](https://github.com/pyinstaller/pyinstaller-hooks-contrib) | 标准 hooks 为 GPL-2.0-or-later，runtime hooks 为 Apache-2.0；保留完整原始许可，实际版本见清单 |

## Qt 与图像库的附带组件

Windows 验证包实际包含 QtCore、Gui、Widgets、Network、Svg、Pdf 和平台、TLS、图像格式插件。
其中 qpdf 插件带入 QtPdf/PDFium，即使用户不打开 PDF，随包分发也需要保留其声明。
[Qt-6.11.2-THIRD-PARTY.txt](licenses/Qt-6.11.2-THIRD-PARTY.txt) 保存
Qt 6.11.2 官方对这些模块列出的第三方许可，含 PDFium/Chromium、字体与图像编解码等。
该文本是这些模块跨平台声明的保守合集，不表示每个条目都实际进入 Windows 二进制，
也不是完整的逐二进制 SBOM。来源和校验值见 [静态来源清单](licenses/SOURCES.json)。

OpenCV 的 `LICENSE.txt` 仅是 wheel 打包层 MIT，不能替代
`LICENSE-3RD-PARTY.txt`。构建脚本从**实际构建机器安装的 wheel**复制二者，
同时完整复制 NumPy 的嵌套许可目录、PyQt、Qt、SIP、tifffile、Python 及打包工具的声明。
这可以避免把 macOS 的 OpenCV/NumPy 附带库误写成 Windows 包的依赖。

## 交付位置与复核

Windows onedir 包中，`_internal/LICENSE`、`_internal/THIRD_PARTY_NOTICES.md` 和
`_internal/licenses/` 随程序交付；SDK 核验位于 `_internal/docs/licensing/HIKROBOT.md`。
构建生成的 `_internal/licenses/installed/manifest.json` 记录实际版本、平台、
wheel 标记、声明文件路径和 SHA-256。若必要声明缺失或 Qt 版本与静态核验不符，构建失败。
单独生成用于检查：

```powershell
python scripts/collect_third_party_licenses.py --output build/license-check
```

升级依赖时更新锁定版本、对应来源及许可文本，并重新检查最终产物。
本次记录不承诺后续版本维持相同授权。所有复制的原始许可文本保持其原始语言和条款。

## 再分发与对应源码

如未来解决海康与 PyQt 的组合授权并向他人交付二进制，还需要按各许可条件提供
与二进制匹配的对应源码、修改、构建脚本和所需安装信息；只有许可证文本或一个可能
失效的上游链接不足以自动满足这些义务。
本项目版本可用 `build_info.json` 的 Git 提交定位，依赖版本以 installed 清单为准。
应归档对应项目源码、PyQt sdist、Qt 模块源码及其他有源码提供义务的附带组件，
并确保二进制接收者实际可以取得这些文件。

Qt 使用共享库的 onedir 形式；分发时应遵守 LGPL 对替换/重新链接、调试修改版库等
权利的要求，不能用自有 EULA 限制这些权利。不得把 PyQt 的 GPL 误当成 Qt 的 LGPL，
或把 PyInstaller 的打包例外误当成全部依赖的许可证例外。
详见 [Qt 授权说明](https://doc.qt.io/qt-6.11/licensing.html) 和
[Riverbank 授权说明](https://www.riverbankcomputing.com/software/pyqt/)。
