# 海康机器人 SDK 授权核验

核验日期：2026-10-08。结论：**已确认来源，尚未取得公开再分发授权。**
本说明记录实际取得的版本和协议，不推定其他版本具有相同条款，也不代表海康的授权。

## 来源与版本

在用户提供的 MVS 安装包中只读提取文件，没有运行安装程序：

| 项目 | 证据 |
| --- | --- |
| 外层安装包 | `MVS_STD_5.0.0_260417.exe`，MVS 5.0.0 |
| SHA-256 | `5ef033ff781a3ae171bd352ec9a525da19a47a88774826c08118c0b66dfa6bc2` |
| 开发 SDK | 内嵌 `Compress.7z` → `TEMP/MVS_SDK_V4_7_0_3_Development_STD.exe` |
| 开发 SDK SHA-256 | `d1d33f8275911b250b8a474e3db16818b5141552cfe01de7f45e415fe1351617` |
| Python 绑定原位置 | 开发 SDK 的 `Samples/Python/MvImport/` |
| 协议位置 | 外层安装包的 `$APPDATA/MVS_5.0.0/res/LicenseChs.rtf` 和 `LicenseEng.rtf` |

仓库中原有 7 个 Python 文件均与上述 SDK 文件逐字节一致：

| 文件 | SHA-256 |
| --- | --- |
| `CameraParams_const.py` | `da91935451cd7ea00f8a55870f815c1ccefc4bf1dabf2f8f230527feeb609cfc` |
| `CameraParams_header.py` | `94fc0f912e26985a5b1fb8b472fc3a750ff16bc0d4b048254bb576ab79eb4b17` |
| `MvCameraControl_class.py` | `56ba9431c4e692e0f082d6b9675b75a20ae04c02b6bb362f93a5bdbd36ef61cd` |
| `MvErrorDefine_const.py` | `5534d50fea7b50fd30b912b424ba9974035cbee3440a12b5b2a115906c5d0ee8` |
| `MvISPErrorDefine_const.py` | `593325e016e3339b7593f57c839d16db7d19ef7697d5343cfd64690b9f2de57f` |
| `PixelType_header.py` | `73831ae42253a227895642c133524807e428f4d4e5d0d108b506178a59e65a31` |
| `__init__.py` | `3201c3bb2aa1069a1c2aab13438b5b5c08a39907c8e40acdd78af35acb1c64c0` |

官方产品及软件下载入口：[海康机器人 MVS](https://www.hikrobotics.com/en/machinevision/service/download/?module=0)。
网站可能提供更新版本；上表对应本次实际核验的安装包。

## 协议意味着什么

实际安装包中英文协议的对应条款为：

- 第 1.3 条：协议适用于正在安装的版本，不能据此推定后续版本的授权。
- 第 2.1 条：授予有限、非独占、不可转让的使用权；没有明示授予的权利仍由权利人保留。
- 第 3.1(3) 条 / 3.1(c)：限制出售、出租、转许可、出借、分销全部或部分软件。
- 第 3.1(4) 条 / 3.1(d)：修改、组合或并入其他产品/程序需要海康明确同意。
- 第 3.1(6) 条 / 3.1(f)：向他人提供任何部分，包括源代码或目标代码，需要事先书面许可。
- 第 6.2 条：第三方和开源组件各按自身条款授权；这不构成整个 SDK 的开源许可。
- 第 11.4 条：协议变更或弃权需要书面签署。

提取的开发 SDK 中未发现覆盖这些 Python 文件的独立再分发例外。
MVS 客户端的 `License/CLIENT_MVS_Win_license_notice.txt` 中，
`MvCameraControl.Net` 的 Apache-2.0 声明属于 **.NET 组件**，不能套用到 Python 的
`MvImport` 或原生 `MvCameraControl.dll`。客户软件使用的 Qt 授权同样不能转授本项目。

因此，“可以下载 SDK”“是示例 Python 文件”“本机已安装 MVS”均不足以证明可公开分发。
本项目的 GPL-3.0-only 不覆盖这些海康文件，也不替代厂商书面许可。

## 公开前尚需解决

1. 向海康确认以上准确版本、文件清单、公开 Git 仓库及 Windows 二进制分发方式，
   取得允许再分发/组合使用的书面授权，或可核验的专门许可条款。
   安装包协议第 10 条列出的联系地址为 `tech_support@hikrobotics.com`；本次未代发邮件。
2. 解决同一进程中 GPL 版 PyQt6 与专有 SDK 的组合授权问题。
   仅给本项目原创代码添加 SDK 例外，不能替 PyQt 的权利人授予例外；
   让用户单独安装 SDK 也不自动解决这一问题。
   参见 [Riverbank 授权说明](https://www.riverbankcomputing.com/software/pyqt/)
   和 [GNU 关于 GPL 不兼容库的说明](https://www.gnu.org/licenses/gpl-faq.html#GPLIncompatibleLibs)。
3. 核对将公开的历史提交和已有 Release：当前文档不改变旧提交中海康文件的权利状态。
   本次没有重写 Git 历史、删除 SDK、公开仓库或发布新版 Release。

可以继续在现有授权范围内研究和验证。仓库可见性设为 private 并不自行扩大分发权限。
公开完整程序前，仍须以适用的书面授权和组合分发条件为准。
