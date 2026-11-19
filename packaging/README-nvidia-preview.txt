DLSS Studio v1.5.0-preview.1 — NVIDIA 视频超分与补帧预览（Windows x64）

请把整个 ZIP 解压到新的可写目录，再双击 DLSS Studio.exe。无需另外安装 Python。原有增强和 NVENC 编码可以直接使用；视频硬件编码器在导出时失败，会复用已完成的增强片段改用 CPU 编码。

Maxine VSR/VFG 和 FRUC 不随本 ZIP 分发。启用 Maxine 时，请先退出程序，按 GitHub README 的指引将官方 NVIDIA VFX wheel 和两个依赖解压到本目录的 app/out/nvidia-vfx，然后重开程序。

超分：上传 SDR 视频，在原来的输出尺寸中选择比源片更大的尺寸，例如 720p 到 1080p。已配置 Maxine 且输入匹配时会自动尝试 VSR High，没有独立开关；可查看任务日志确认。

补帧：在视频设置的“媒体与时间”下勾选“补帧 ×2”，再点“开始处理”。支持的 SDR 恒定帧率素材可从 24 fps 变为 48 fps、30 fps 变为 60 fps，音轨和片长保持原时间线。该选项默认关闭。没有补帧组件、素材格式不适用时会保留原帧率。

RTX 40/50 的补帧方案是 Maxine VFG；仅在 RTX PRO 6000 Blackwell 实机验证了 1080p 和 4K 短样片，RTX 40 尚未实机验证。RTX 30 的 FRUC 仍是开发预览，需 NVIDIA Optical Flow SDK 5.0.7，尚未实机验证；不要将它视为已交付的补帧功能。

发布页：https://github.com/LeoSasion/DLSS5-Studio-Windows/releases/tag/v1.5.0-preview.1
准备方法：https://github.com/LeoSasion/DLSS5-Studio-Windows#使用-nvidia-超分和补帧
