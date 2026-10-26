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

2026-10-27 当前运行库均为 **310.9.16.0**，三套 GPU 配置对应两份不同的 `nvngx_dlssnr.dll`：

| GPU 配置 | 来源和身份 | 大小与 DLL SHA-256 |
| --- | --- | --- |
| RTX 30 / 40 | ShortFuse / OpenNR 社区兼容版，从 [RankFTW/rhi-repo 固定发布](https://github.com/RankFTW/rhi-repo/releases/tag/dlssnr-310.9.16) 取得；未签名，不标为 NVIDIA 签名原版 | 276,162,560 bytes；`CDFA6C1EC784867182164C7836F324F087B5C9F374D0C9FE46F54999FEABF3ED` |
| RTX 50 | NVIDIA 原版，从 [LoreanXavier 镜像](https://github.com/LoreanXavier/pt-dlss5-runtime/releases/tag/dlssnr-310.9.16) 取得；NVIDIA Authenticode 签名有效，文件 SHA-256 与 [RTX Remix 1.6.1 官方 CRC 清单](https://github.com/NVIDIAGameWorks/rtx-remix/releases/download/remix-1.6.1/rtx-remix-for-x64-games-release-remix-1.6.1-crc.txt) 完全一致 | 266,424,944 bytes；`2BC19BC09A7B89DD44F73E2AC0108FCC586E2154BDD0830F242F4F040E28789F` |

固定下载地址、GitHub 资产 API、附件大小、附件 SHA-256 与 DLL SHA-256 见 `packaging/dlss5-catalog.json`；实际运行选择及文件身份记录在 `app/dlss5-components.json`。构建下载工具按该目录校验 HTTPS 下载与目标 DLL，并在全部配置就绪后原子切换，保留原文件和回退记录。上述文件不属于本项目的 MIT 授权范围；NVIDIA 软件及其社区兼容修改仍受各自原有版权和适用条款约束。固定哈希与有效签名不扩大 NVIDIA 官方硬件支持，也不代表已获得额外分发权利。

新默认网络选择为 `DLSSNR.Hint.Render.Preset` 的 I32 值 2（Preset B），与既有默认 / 自然 / 电影外观模型独立，不增加界面选项。采用依据为 [NVIDIA RTX Remix 的 NR 创建逻辑](https://github.com/NVIDIAGameWorks/dxvk-remix/blob/bbd74a1287c2fa887f4a2b0e46a1530177890b64/src/dxvk/rtx_render/rtx_ngx_wrapper.cpp)。本次完成文件身份核对与引擎、转发 DLL 编译，按更新要求未运行测试、GPU 处理或性能测量，兼容性和效果待人工反馈。

2026-10-26 在 RTX PRO 6000 Blackwell、591.86 驱动上的处理验证及 RTX 30 / 40 型号识别、编号、组件匹配测试属于旧 310.8 运行库历史，不能用于证明新版通过。本机系统驱动未变更；310.9.16.0 能否在 591.86 运行尚未确认，可能需要更高驱动。NVIDIA 初版产品 616.64 和 RTX Remix 产品 616.92 的基线不作为该 DLL 精确最低驱动要求。

旧发布包及制作工作区中的旧 310.8 文件、回退记录保留；新 `DLSS-Studio-Runtime-20261027-Windows-x64.zip` 为减少体积仅包含当前两份 NR DLL，不附带旧 DLL 或历史备份。应用增量机制不替换模型，本次不提供模型增量包。界面继续区分“已匹配”和本次实际“已成功渲染”。
