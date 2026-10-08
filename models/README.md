# BRISQUE 模型来源

这两个文件是 OpenCV contrib 的 BRISQUE 官方示例模型，不是本项目训练的麦穗模型。
2026-10-08 与上游文件进行内容校验，结果完全一致。

固定来源：[`opencv/opencv_contrib` 的 `6484ab27bd332762f36262bf3b3f743720f4ca1b`](https://github.com/opencv/opencv_contrib/tree/6484ab27bd332762f36262bf3b3f743720f4ca1b/modules/quality/samples)。
这是本次校验时的上游快照，不推定为最初复制文件的日期。

| 文件 | SHA-256 | 上游 Git blob |
| --- | --- | --- |
| `brisque_model_live.yml` | `4c44c7eec9e5139830c0bb0e4a044c9cd351ba5bbc9e3798beca3243e2782ff4` | `8cdc854713488d13bd7a14483c0875e0cedf5599` |
| `brisque_range_live.yml` | `a18427f4f7ad087524bd0d63389649a3899b51ca6d44a4d5f8d8171f1f914a9c` | `6fec552525b5dd55aea33c362911962ffbee0039` |

上游仓库使用 [Apache-2.0](../licenses/OpenCV-Apache-2.0.txt)。
BRISQUE 实现另外保留 University of Texas at Austin / LIVE / CPS 的
[原始许可和科研引用要求](../licenses/BRISQUE-UT-Austin.txt)，此处一并保留，
不以本项目 GPL 许可证替换。两个 YAML 本身没有单独的许可头，
本说明据其上游仓库许可及关联实现的原始声明记录来源，不推定 LIVE 数据集本身的使用权。

研究中使用 BRISQUE 时，应按上述原始声明致谢 LIVE/CPS 并引用原始软件及论文。
本项目仅将其用于图像质量辅助检查；分数不是麦穗表型识别模型的性能指标。
