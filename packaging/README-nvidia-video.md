# Local NVIDIA video processing preview

Video enlargement uses Maxine Video Super Resolution High when the local SDK is available. Video export has one optional control, `补帧 ×2`: Maxine Video Frame Generation High on RTX 40/50 and NVIDIA Optical Flow FRUC on RTX 30. The current combined path is limited to SDR; frame generation also requires a constant-rate source. FRUC takes 8-bit SDR frames. Unsupported input keeps its original cadence and the job explains why.

For Windows x64 local testing, use the official [NVIDIA VFX Python package](https://pypi.nvidia.com/nvidia-vfx/) matching the bundled Python (3.10); the validated package version is `0.2.0.0`. Use CuPy CUDA 12 (`13.6.0`) and `fastrlock` (`0.8.3`) as the Python CUDA buffer adapters. Extract/install these wheels into this copy's `app/out/nvidia-vfx`. Restart the local app after changing the runtime. The model choices are automatic.

For the RTX 30 FRUC preview, obtain and extract the official [NVIDIA Optical Flow SDK 5.0.7](https://developer.nvidia.com/opticalflow/download) after signing into NVIDIA Developer. Build the local adapter with `python packaging/build_fruc.py --sdk <extracted-sdk-dir>`. The build checks the vendor DLL signature. This adapter has not yet been built or tested with that SDK and an RTX 30 GPU. The proprietary runtime directories are excluded from the default GitHub package builder.

References: [Maxine Video Super Resolution](https://docs.nvidia.com/maxine/vfx/latest/Filters/VideoSuperResolution.html), [Maxine Video Frame Generation](https://docs.nvidia.com/maxine/vfx/latest/Filters/VideoFrameGeneration.html), [FRUC programming guide](https://docs.nvidia.com/video-technologies/optical-flow-sdk/nvfruc-programming-guide/index.html).
