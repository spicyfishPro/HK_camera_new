# Changelog

## [unreleased] - 2026-06-10

### Features
- **采集元数据内嵌**:PNG/JPG/TIFF 在首次编码时写入 `wheat.capture.v1` XMP，保存样本、工作流、相机逐帧信息、参数、FFC/旋转和软件构建来源；主图与 `_raw` 图共用采集 ID，且明确 `_raw` 仍是去马赛克 BGR 图。
- **帧与元数据配对**:取流缓存改为全分辨率图像和 SDK 帧信息的原子快照；FFC 参考生成稳定校准 ID。元数据写入失败时回退普通图片并警告，不阻塞原工作流或 BRISQUE 并行评分。
- **保存格式收敛**:移除不支持标准内嵌元数据的 BMP，保留 PNG、JPG 和无压缩 TIFF。
- **挂牌二维码自动填写品种名**:麦穗与籽粒页签均可在手动输入和二维码识别间切换；二维码模式从当前已旋转预览帧识别唯一挂牌编号、绿色框选 5 秒，并通过带 Bearer 鉴权的挂牌 API 获取 `material.accession_no` 后启动原工作流。
- **安全与失败护栏**:Bearer 密钥仅在两个页签间内存同步并以密码方式显示；识别、网络或文件名校验失败均只显示预览横幅，不修改前缀或推进工作流。请求期间锁定控件，带 10 秒绝对超时和过期回调防护。

### Fixes
- **看板格子遮挡(籽粒/麦穗)**:`SpikePanel` 网格容器改用 `QSizePolicy.Fixed`（恒等 `sizeHint` 高度）+ `setVisible` 折叠，先 `setVisible(True)` 再 `activate()`，并逐级同步重排祖先布局；网格底部内嵌 10px 余量。彻底消除子按钮溢出遮挡 `lbl_current` 的竞态，跨 100%/150% DPI 验证 16px 稳定留白。
- **工作流防覆盖**:`SpikeWorkflow.on_pass` 推进时跳过已存在(SKIPPED)的格子，不再把它置为 CURRENT 诱导覆盖已拍照片。
- **中途起拍可完成**:`is_complete` 只判定起点(`_start_idx`)及之后的格子，避免从 RepN 起拍时因前面灰格永远完不成、前缀不自动清空。
- **失败格跳转**:`jump_to` 把原 FAILED 当前格一并回退 PENDING，消除跳走后残留的红色死格。
- **工作流卡死缝隙**:`_on_snap_done` 的放行条件与 `do_brisque` 对齐(`_brisque_ok and brisque is not None`)，杜绝无人调用 `apply_pass` 导致永久卡死。
- **FFC 显示路径崩溃竞态**:`apply_display`/`apply_full` 把 gain/dark/buffer 先快照到局部变量，主线程 `clear()`/`set_*` 置空成员不再导致取流线程 `out=None` 崩溃或读到 None gain。
- **FFC 增益数值**:gain 分子改用 clamp 后均值，暗场≥亮场时不再静默出黑图，并打印告警。
- **BRISQUE 并发**:`score_ndarray` 的 `compute` 加锁串行，防非工作流连续快拍时两线程共享同一不可重入对象。
- **取流线程删除安全**:`_stop_stream` 仅在线程已退出时 `deleteLater`，2s 未停则放任泄漏而非崩溃。
- **杂项**:`clear_flat` 释放 300MB 抓拍 buffer;`single_capture` 抓拍中不再并行取帧打架;BRISQUE 阈值提示文案修正为实际默认值 65。

### Notes
- 新增二维码/API/Windows 文件名、Qt offscreen 模拟网络及原有命名/续拍/FFC/BRISQUE 回归测试，共 31 项通过。
- 新增纯逻辑回归覆盖工作流状态机(19 项断言全过);FFC/质量模块做了往返正确性校验。
- 已打包到 `D:\Pycode\HK_camera_soft\v2\RGB_Capture\RGB_Capture.exe`。
