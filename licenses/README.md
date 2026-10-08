# 许可原文与来源记录

本目录保存原始许可文本；这些文本不被根目录的项目许可证重新授权。
`SOURCES.json` 记录来源、版本、提取范围和 SHA-256。

- `OpenCV-Apache-2.0.txt`：固定 OpenCV contrib 提交中的原始许可证。
- `BRISQUE-UT-Austin.txt`：同一提交的 `qualitybrisque.cpp` 中完整原始版权与许可注释。
- `Qt-LGPL-3.0.txt`：所用 Qt wheel 的许可证；所引用的 GPLv3 全文在根目录 `LICENSE`。
- `Qt-6.11.2-THIRD-PARTY.txt`：Qt 官方对应版本文档列出的 Core、Gui、Network、
  Image Formats、SVG、PDF 模块第三方声明，共 61 项。仅提取版权与许可原文代码块，
  不复制 Qt 文档正文；来源页面逐项记录在 `SOURCES.json`。
- SIP、OpenCV Python 打包层、tifffile、PyInstaller 文本从所标记版本的分发包原样复制。
- `Python-3.12.10.txt`：官方 CPython v3.12.10 的完整许可证，供同版本开发环境使用。
  正式 Windows 包另外从实际解释器安装目录复制对应版本的 `LICENSE.txt`。

构建时，实际安装的依赖许可将收集到产物的 `licenses/installed/`，包含完整的
OpenCV 第三方声明、NumPy 嵌套许可树和平台相关文本。该目录由构建脚本生成，
不在仓库中维护另一份可能过期的 wheel 副本。

静态文件更新后须同步 `SOURCES.json`；Qt 升级还须重新核验附带组件。
`.gitattributes` 禁止 Git 对许可原文进行换行转换，以便 Windows 和其他平台使用相同校验值。

来源、适用范围与尚未解决的海康授权问题见 [第三方声明](../THIRD_PARTY_NOTICES.md)。
