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
git tag v2.1.0
git push origin v2.1.0
```

`Build and release` 工作流会在 Windows Runner 上重新运行测试，随后使用 `build.spec` 生成 onedir 包，并发布以下 Release 附件：

- `RGB_Capture-v<版本>-windows-x86_64.zip`
- 对应的 `.sha256` 校验文件

也可以在 Actions 页面手动运行该工作流进行构建。手动运行只上传短期 Artifact，不创建 Release；只有版本标签会发布 Release。

解压 ZIP 后运行 `RGB_Capture.exe`。目标电脑必须预先安装与相机兼容的海康 MVS Runtime，确保可以找到 `MvCameraControl.dll`。
