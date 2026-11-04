# 视频处理优化预览 — 2026-11-05

本轮为人工反馈预览，尚未运行自动化测试、GPU 处理、桌面运行或性能测量。
编译和源码审查不构成画质、速度或硬件兼容性验证。

## 已接入

- NVOFA 运动矢量先与“没有运动”的解释比较；静止或匹配无改善的位置输出零运动。关闭猜测的 cost 阈值门控，切镜后同时清除硬件时序提示。LK 回退路径继续可用。
- 普通与高精度视频共用缩略图平移对齐残差、亮度直方图和弱信号防重复触发。缩略图长边上限 64 像素；强信号立即重置，弱信号间隔按源帧率计算。
- 高精度路径改由 GPU 生成 SDR 模型代理、缩小推理输入、提取增强差值、进行时序稳定与原画合成，最后打包 RGBA8/16。原画、代理和差值使用 FP32，模型输入输出保留 NR 所需 FP16。
- 时序稳定使用有界 Catmull–Rom 历史采样，并根据原始画面的局部亮度增益判断历史可信度。保护区域、肤色、高光、PQ/HLG 和支持色域沿用现有含义；强度为零的输出保留主机侧打包路径。
- 不再为普通时序稳定读回运动矢量；不写模型缓存时也不读回整帧浮点 NR 结果。命中模型缓存后仍重新合成；写原始模型缓存所需的读回继续保留。解码、编码和放大路径并非全链路零拷贝。
- 更新流水线身份、原始模型缓存算法版本；本轮产生的结果不混用旧模型中间缓存或旧接续段。原有成片文件继续保留。

切镜阈值与过滤策略仍需人工评价，不能承诺不会误判闪光、快切或快速运动。GPU 后处理增加设备侧历史纹理；与旧 CPU 路径不承诺逐字节一致。默认关闭的时序稳定仍由已有开关控制，未新增工作台选项。

## RTX VSR 的实际状态

已加入共享 NGX 会话的条件编译适配器：仅对 SDR 8-bit 视频放大使用 RTX VSR High（3）；高位深和 HDR 继续原放大路径。不能创建或执行已报告可用的 VSR 时明确失败，不在一段视频中悄悄混用不同放大器。不可用的能力使用 DLSS SR，并写入日志。

**当前预览没有启用 VSR。** 本地缺少 NVIDIA RTX Video SDK；2026-11-05 官方下载链接跳转到 NVIDIA 开发者登录，未取得 SDK 头文件及原版运行库。VSR 条件分支尚未在带 SDK 的构建中编译或运行，不能视为已可用。

从 [NVIDIA 官方页面](https://developer.nvidia.com/rtx-video-sdk/getting-started)取得 RTX Video SDK 1.1 后，指定完整解压根目录构建到空目录：

```powershell
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/build_engine.py --rtx-video-sdk "C:\路径\RTX-Video-SDK" --out source/out-vsr
```

构建入口检查 `include/nvsdk_ngx_helpers_vsr.h`、`bin/Windows/x64/rel/nvngx_vsr.dll` 和 NVIDIA 有效签名，并把运行库放到输出目录。遵守 SDK 自身条款并随分发保留适用许可。VSR DLL 的有无及哈希参与应用缓存身份，实际选择也参与原始模型缓存身份。基础构建不需要 SDK。

## 参考与归属

本轮参考 [2600th/dlss5-video-player](https://github.com/2600th/dlss5-video-player) 的 MIT 源码与测量资料；具体涉及 `NvofResolveShader.h`、`SceneCut.h`、`TemporalStabilityShader.h`、`TemporalStabilityPolicy.h` 和 `VsrEngine.cpp`。策略按本项目的 D3D12 compute、带符号增强差值和 HDR 契约重新实现；原项目 MIT 许可另附 `THIRD-PARTY-video-player-LICENSE.txt`。未合入 Merserk 自定义许可代码或 ComfyUI 的 GPL 代码。

310.9.16 的 Network B 是速度与画面变化程度的取舍，不描述为与 A 相同画面的无损提速。本轮未更换 NR DLL 或模型网络默认值。
