DLSS Studio — NVENC 与视频导出预览（2026-11-18）

将本目录完整解压到可写位置，双击 DLSS Studio.exe。素材、任务和设置存放在本目录中。
沿用现有界面。兼容的显卡自动使用已有硬件编码选项；导出期间硬件编码失败时，会用已增强的片段自动改走 CPU 编码。
H.264 回退到 CPU H.264，AV1 回退到 CPU AV1；HEVC 回退到 CPU AV1（MP4/MKV）或 ProRes（MOV）。容器和音轨策略保留。
无须分别读取神经渲染缓存的帧，会合并增强结果的合成和读回，减少 GPU 等待。
FFmpeg / FFprobe 使用经校验的 LGPL 构建，兼容 NVENC API 13.0。此包不安装或更新显卡驱动。
