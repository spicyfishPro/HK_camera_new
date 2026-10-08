# RGB Capture

面向海康工业相机的 Windows 图像采集软件。应用使用 Python 3.12、PyQt6、OpenCV 和 PyInstaller，正式运行时需要安装海康 MVS Runtime。

## 自动测试

每次推送和 Pull Request 都会在 GitHub Actions 的 Windows Runner 上执行：

```powershell
python -m compileall -q core ui tests scripts
python -m unittest discover -s tests -v
```

这些测试覆盖纯逻辑和无相机 UI 行为，不代替真实 Windows 设备、MVS DLL、相机取流及保存性能验收。

## 构建与发布

应用版本定义在 `core/build_info.py` 的 `APP_VERSION`。发布前应先更新版本号，然后创建与之匹配的标签：

```powershell
git tag v2.2.0
git push origin v2.2.0
```

`Build and release` 工作流会在 Windows Runner 上重新运行测试，随后使用 `build.spec` 生成 onedir 包，并发布以下 Release 附件：

- `RGB_Capture-v<版本>-windows-x86_64.zip`
- 对应的 `.sha256` 校验文件

也可以在 Actions 页面手动运行该工作流进行构建。手动运行只上传短期 Artifact，不创建 Release；只有版本标签会发布 Release。

解压 ZIP 后运行 `RGB_Capture.exe`。目标电脑必须预先安装与相机兼容的海康 MVS Runtime，确保可以找到 `MvCameraControl.dll`。

## 连续快拍与照片预览（2.2）

连续采集时，抓拍只等待新帧、FFC/旋转和质量检查。质量通过即可拍下一张，
PNG 继续在后台保存；提示音表示“可以切换姿势”，照片变绿才表示保存完成。
质量不合格停在当前 Rep/视角重拍。整轮照片全部落盘后才自动重置。

底部照片带按 Rep 排列正面与侧面，支持折叠、调高度和“定位当前”。已保存照片、
待保存照片及失败任务统一显示；点击可打开独立预览，主画面继续取流。
已保存照片可查看原图 1:1，`_raw` 在预览窗口中切换。历史照片按当前目录与精确
材料前缀读取，磁盘已有照片的质量状态显示为未知；`+N` 表示同位置还有其他版本，
可在预览窗口的文件选择框切换。自动重置后保留刚完成材料的照片带。

全应用共享一个预处理线程、两个保存线程，最多 8 个未完成任务和 768 MiB 在途
图像预算（包含处理阶段预留、主图及原图；不等于整个进程内存上限）。FFC 浮点
工作区、编码缓冲、SDK 与界面还会使用额外内存；`capture memory` 日志单独记录
FFC 工作区/参考及实际编码缓冲字节数，供长期快拍核对。队列满时需等待，不会偷偷排队
快门指令。失败照片保留在内存中，在照片带点击“重试保存”；磁盘空间或权限问题
需先处理。正常关闭等待保存完毕，有失败则保留窗口供重试；不提供断电恢复保证。

运行环境只安装 `opencv-contrib-python`，不要同时安装 `opencv-python`；BRISQUE
依赖 contrib 的 `cv2.quality`。已有开发环境升级前可先卸载旧 `opencv-python`，
再按 `requirements.txt` 安装。

## Windows 相机性能验收

使用 5120×5120、PNG 压缩级别 1、180°、连续采集、本地 SSD，关闭 FFC 和
“同时存原图”。先预热 5 张，再以 0.5 秒间隔拍 8 张，重复 5 组。队列未满时，
点击到允许下一张的 P95 目标为 0.5 秒；后台文件保存耗时单独统计。
继续快拍至少两分钟，检查队列满提示、所有文件与 Rep/视角一致、无静默丢图，
并用任务管理器确认内存不随拍摄数量持续增长。再分别测试 FFC、原图、磁盘满、
断开相机和退出等待。0.5 秒目标需要真实 Windows 相机验证，CI 不作此结论。

日志中 `capture ready` 是主线程实际恢复抓拍的计时，`capture perf` 是后台
完成后的分阶段计时，原 `capture_log.csv` 列结构保持不变。可只读汇总：

```powershell
python scripts/summarize_capture_performance.py logs/2026-10-08.log
```

界面回归使用相机替身覆盖 1000×600、1366×768、1600×900 与 125%/150% 的 Qt
缩放；交付前仍需在目标 Windows 电脑检查显示缩放和真实取流。
Windows CI 同时上传 `capture-ui-preview` 截图附件，图像为明确标记的测试画面。
也可以运行 `python scripts/render_capture_ui.py --output <截图目录>` 复现布局检查。
