# 许可范围与第三方组件

## 本项目原创部分：MIT

项目维护者已选择 MIT 许可，全文见 [LICENSE](LICENSE)。该许可适用于 DLSS-Studio-Windows 贡献者原创的界面、服务桥接、启动器、构建脚本和文档，包括：

- `app/web/index.html`、`app/web/studio.css`、`app/web/dark-polish.css`、`app/web/studio.js`、`app/web/desktop-shell.js`、`app/web/desktop-shell.css`。
- `app/gpu_runtime.py`、`app/studio_server.py`、`app/serve_local.py`、`app/ui_style.py`、`app/test_studio_server.py`。
- `start_studio.py`、`start-web.bat`。
- `packaging/StudioService.cs`、`packaging/DesktopShell.cs`、本项目原创的打包与验证脚本。
- 本项目原创文档；不包括其中引用的第三方文本。

上述 MIT 许可仅授予本项目贡献者有权授予的权利，不改变以下第三方内容的许可，也不为包含它们的整个发行包授予统一的 MIT 许可。

## 上游代码和引擎

来源：[DaniilSokolyuk/video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr)。本地依据提交 `55a4ceb588a419b9b56497aa0b563d0c9e2b6c77` 和 v1.3 发行包进行适配。

`app/app.py`、`app/nr_video.py`、`app/render_engine.py` 与 `source/` 中保留的基础代码包含该项目贡献。原作者已在提交 `ea5465762923f5a198e4b7399ff0645d96bb5786` 明确提供 MIT 许可；保留其 Copyright (c) 2026 Daniil Sokolyuk 归属，许可证副本见 `source/THIRD-PARTY-engine-LICENSE.txt`，便携包中见 `licenses/engine-MIT-LICENSE.txt`。新增的 DLSS Studio 视频处理、任务、界面及内核优化按本项目 MIT 条款提供；第三方原有版权声明继续保留。

`app/out/video2dlssnr.exe` 由本项目保留归属的源码重新构建；链接的 NVIDIA SDK 与 NVIDIA NGX / DLSS DLL 仍受厂商自己的条款约束，不能因包装代码使用 MIT 就将第三方二进制重新许可为 MIT。构建时的 Microsoft MSVC/Windows SDK 只下载并解压到本地构建缓存，不包含在应用发行包中；其厂商条款继续有效。

## 其他第三方内容

- **Phosphor 图标**：保留其独立 MIT 许可和原作者版权声明，见 `app/web/assets/icons/LICENSE`。
- **Python**：遵循其自带许可，便携包中见 `runtime/LICENSE.txt`。
- **Python 依赖**：各自适用其许可证，见便携包 `runtime/Lib/site-packages/*dist-info/`。
- **FFmpeg / FFprobe**：适用所捆绑构建的许可及分发要求，便携包中保留 `licenses/ffmpeg-license.txt`。
- **示例图片**：本项目制作流程中生成的演示素材，不属于上游渲染结果。

本项目并非 NVIDIA 官方产品。MIT 许可不授予第三方商标权。

- **Microsoft WebView2**：SDK 与运行时引导组件遵循微软各自的适用许可；便携包保留 `licenses/WebView2-LICENSE.txt` 和 `licenses/WebView2-NOTICE.txt`，不纳入本项目 MIT 授权。

## 自动显卡适配

统一使用 DLSS 5 神经渲染。旧 E/F/J/K/L/M 是内部 SR 预设，并非分别验证过的 DLSS 5 模型，现已移除；内部放大阶段保持驱动默认预设。

自动识别 RTX 30 / 40 / 50 系及可识别的同架构专业卡，通过 DXGI 高性能排序选择显卡，使用 CUDA LUID 核对架构，显式传入 DXGI 编号和对应 DLL 目录。每次提交任务前重新识别，避免多卡或热插拔造成编号混淆。运行时三套组件已内置，无需手动替换。未知显卡、缺失或校验失败的组件会显示原因，不会猜测其他架构 DLL。

三套 DLL 来自 [purkatyy/DLSS5- 的 dlss 发布](https://github.com/purkatyy/DLSS5-/releases/tag/dlss)。下载地址、附件及 DLL 的 SHA-256 记录在 `app/dlss5-components.json`；仅提取各包中的 `nvngx_dlssnr.dll`。它们不属于本项目的 MIT 授权范围，原有版权及适用条款继续有效。

已在 RTX PRO 6000 Blackwell 工作站使用 50 系 DLL 实测。30 / 40 系已有型号识别、显卡编号、组件匹配的自动化测试，尚未在实体卡上验证渲染。原发布者也注明 30 / 50 系替换包未经其测试。界面区分“已匹配”和本次启动“已成功渲染”。
