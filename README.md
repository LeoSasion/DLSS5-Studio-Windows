# DLSS Studio v1.3.0

面向 Windows 的本地 DLSS 图片与视频增强工作台，提供中文界面、深浅主题和无边框独立窗口。

**[下载 Windows x64 完整包](https://github.com/LeoSasion/DLSS5-Studio-Windows/releases/download/v1.3.0/DLSS-Studio-Windows-x64.zip)** · [v1.3.0 更新说明](https://github.com/LeoSasion/DLSS5-Studio-Windows/releases/tag/v1.3.0) · [SHA-256 校验文件](https://github.com/LeoSasion/DLSS5-Studio-Windows/releases/download/v1.3.0/DLSS-Studio-Windows-x64.zip.sha256)

## 快速使用

1. 下载 `DLSS-Studio-Windows-x64.zip`，完整解压到可写文件夹；请保留整个目录，不要直接在 ZIP 中运行。
2. 双击 `DLSS Studio.exe`。Python、依赖、FFmpeg 和处理引擎已内置，无需另装 Python。
3. 上传图片或视频，选择尺寸、风格和强度，点击“开始处理”；在预览中比较并下载结果。

需要 Windows 10/11 x64、兼容的 NVIDIA 显卡与驱动。独立窗口使用 Microsoft WebView2，缺少时可通过包内微软引导组件联网安装。从旧版升级请解压到新目录并保留旧包。

v1.3.0 包含处理、缓存、视频与工作台优化，并将 RTX 30 / 40 / 50 的 NR 运行库更新至 **310.9.16.0**。本次新版完成文件身份核对、静态审查和编译，**未运行测试、GPU 处理或性能测量**，效果与驱动兼容性等待人工反馈。2026-10-26 的验证仅属旧 310.8 运行库历史，不是新版稳定性承诺。

## 当前功能

- 图片与视频增强，按实际 GPU 身份选择并校验组件；强度保持 0–200%，色彩融合 0–100%。
- 图片原画、结果和拖动对比；视频整段或选区处理、同步播放、实际源 PTS 暂停对齐、Alt 逐帧步进和浏览器显示差分。
- 适应、100%、滚轮缩放与平移，原始、720p、1080p、1440p、4K 输出保持素材比例。视频的 100% 指兼容预览像素，预览可能低于下载结果。
- 矩形原画保护、肤色/高光保护、增强差值时序稳定，以及 75%/50% 推理尺寸试调。默认完整精度，额外稳定与保护关闭。
- 按内容、有效参数和实际运行环境复用成片；复用无损增强段重新编码；在缓存预算内复用模型预测重新合成。
- 单主增强任务串行队列、批量添加、视频分段暂停/继续和服务重开接续。图片运行中可取消，暂不支持中途暂停。
- PQ/HLG 和高位深原始帧链路，支持 HDR BT.709、BT.2020、P3-D65；原片与结果使用一致的 SDR 浏览器显示代理。
- 按目标容器能力保留多音轨、字幕、章节、附件和适用静态 HDR 元数据；明确不沿用原画专属动态 HDR 映射。
- 任务日志、阶段耗时、实际组件身份、连续视频自检、组件本地校验导入/回退和应用增量更新/回退。
- 保存自定义设置，历史命名、满意标记和复用设置；缓存清理先预览并保护当前、满意、活动与暂停任务。
- 无边框 WebView2 窗口、深浅主题、普通窗口实时玻璃；最大化使用不透明主题底色，还原恢复玻璃偏好。适配小窗口与 200% 显示缩放。

![工作台界面示意](docs/desktop.png)

## 获取与运行

正式完整附件为：

- [`DLSS-Studio-Windows-x64.zip`](https://github.com/LeoSasion/DLSS5-Studio-Windows/releases/download/v1.3.0/DLSS-Studio-Windows-x64.zip)：包含当前 310.9.16.0 运行库的完整便携环境。
- [`DLSS-Studio-Windows-x64.zip.sha256`](https://github.com/LeoSasion/DLSS5-Studio-Windows/releases/download/v1.3.0/DLSS-Studio-Windows-x64.zip.sha256)：ZIP SHA-256，见附件校验文件。

本次没有模型增量包。旧版应用增量工具不替换模型 DLL，不能用旧应用增量包完成本次升级。GitHub 自动生成的 Source code 压缩包只有源码，不含可直接运行的完整环境。

将完整包解压到可写目录，双击 `DLSS Studio.exe`。Python 与依赖已内置；独立窗口需要 Microsoft WebView2，缺少时可选择通过微软引导组件联网安装。不要在 ZIP 内直接运行或单独移动 EXE。

窗口右上角“浏览器”打开当前实例的实际地址，通常为 `http://127.0.0.1:7860/`。7860 被占用时，EXE 在 7860–7959 选择可用端口。服务只监听 `127.0.0.1`。外部浏览器与独立窗口各自保存工作台，共用处理服务；请保持 EXE 开启或最小化，关闭时会停止它启动的服务。手动 `start-web.bat` 默认使用 7860，不提供 EXE 的端口自动避让。

需要 Windows 10/11 x64 和兼容的 NVIDIA 显卡、驱动；CPU 编码不能代替神经渲染所需显卡。2026-10-26 曾在 Windows 11、RTX PRO 6000 Blackwell、591.86 驱动验证旧 310.8 运行库，并完成当时的 RTX 30/40 识别和组件匹配测试；这些结果不证明新版可用。本机驱动未修改，310.9.16.0 在 591.86 的兼容性尚未运行确认，可能需要更高驱动。NVIDIA 初版产品的 616.64、RTX Remix 1.6.1 产品的 616.92 是产品基线，不能当作此 DLL 已核实的精确最低要求。

三套配置对应两份 NR DLL：

| 配置 | 当前运行库身份 |
| --- | --- |
| RTX 30 / 40 | 共用 ShortFuse / OpenNR 社区兼容版，310.9.16.0，276,162,560 bytes，未签名；SHA-256 `CDFA6C1EC784867182164C7836F324F087B5C9F374D0C9FE46F54999FEABF3ED` |
| RTX 50 | NVIDIA 原版，310.9.16.0，266,424,944 bytes，NVIDIA Authenticode 签名有效；SHA-256 `2BC19BC09A7B89DD44F73E2AC0108FCC586E2154BDD0830F242F4F040E28789F` |

RTX 50 文件从 [LoreanXavier 镜像](https://github.com/LoreanXavier/pt-dlss5-runtime/releases/tag/dlssnr-310.9.16) 取得，与 [NVIDIA RTX Remix 1.6.1 官方 CRC](https://github.com/NVIDIAGameWorks/rtx-remix/releases/download/remix-1.6.1/rtx-remix-for-x64-games-release-remix-1.6.1-crc.txt) 完全匹配；RTX 30/40 文件来自 [RankFTW 固定发布](https://github.com/RankFTW/rhi-repo/releases/tag/dlssnr-310.9.16)。下载资产、附件与 DLL 的固定哈希见 `packaging/dlss5-catalog.json`，运行身份见 `app/dlss5-components.json`。文件匹配或签名有效只确认身份，不扩大官方支持范围或保证实际渲染成功。

新默认使用较轻量的 NR 网络 **Preset B（I32 值 2）**；它与既有默认 / 自然 / 电影外观风格独立，运行库更新不新增界面选项。

## 参数、复用与接续

修改设置后，现有预览与下载仍属于已经生成的版本，界面显示“设置待应用”，重新处理才生成新版本。“差分”显示浏览器中两路显示画面的差别，不是原始 HDR 像素误差测量。暂停时根据实际源 PTS 和输出映射对齐；帧映射不可用会退回媒体时间，播放同步不承诺每个刷新瞬间严格同帧。

矩形保护固定在画面坐标中，不自动跟踪人物；肤色保护是颜色判断，不是人脸识别。时序稳定只作用于增强改变量，遮挡、运动不可靠和切镜头时降低复用或重置，过强可能减慢变化响应。降低推理比例保留输出尺寸及原画底图，但可能减少模型细节，默认完整精度。

| 设置变化 | 可复用阶段 |
| --- | --- |
| 素材、范围、有效参数和环境相同 | 直接复用成片，不调用模型 |
| 视频仅改编码、码率、容器等导出选项 | 已增强无损段，重做编码和封装 |
| 图片改细节、色彩或保护 | 预算内模型阶段，重新合成 |
| 视频改细节、色彩、时序稳定或保护 | 预算内原始模型预测，重新合成与编码，仍可能解码和计算光流 |
| 改尺寸、模型风格/强度、运动、推理比例或实际运行环境 | 通常需要重算模型 |

原始模型预测缓存按单视频段预计不超过 1 GiB 启用；图片模型中间缓存总预算 1 GiB，视频模型预测总预算 2 GiB。超预算淘汰可重算中间项，成片及接续无损段不随这项自动淘汰删除。4K 长片不保证后处理始终免推理。时间轴区间与按钮提示仅说明复用机会，实际执行仍校验源及运行身份。

主增强任务串行执行，最多 64 个等待任务。批量添加按当时设置处理同类素材的完整范围，不接管当前工作台。视频暂停会等待当前段保存；继续仅补未完成段。关闭后已提交段保留，重开从最近结果继续；未提交当前段可能重算。等待项恢复排队，手动暂停项仍等待继续。图片中断、旧记录缺接续信息时重新处理。更换驱动、模型、工具或改动源后会拒绝混用旧段。

分段采用前置预热和同源 PTS 重叠过渡，减少边界硬切，但不保证与任意连续 NGX 隐藏历史完全相同。

## 编码与 HDR 边界

默认 MP4：Windows 10 选 H.264，Windows 11 选 AV1；逐项实测 NVENC，不可用时采用同格式 CPU 编码。推荐码率 1080p 及以下 6 Mbps、1440p 9 Mbps、4K 12 Mbps，保留恒定画质与自定义选项。

PQ/HLG 视频自动使用 16 bit RGBA 原始帧，在浮点线性域构造模型代理并回加增强差值，不依赖旧“HDR 线性色彩”复选框。支持 BT.709、BT.2020、P3-D65；未知 HDR 色域明确记录按 BT.2020，其他未验证已知色域会提示。SDR 代理沿用现有 sRGB 近似，不宣称逐片母版校准。

HDR 选支持的 10 bit 编码或高精度母版格式；H.264 导出明确映射为 SDR。浏览器原片和结果预览使用一致的 SDR BT.709 映射，通常限制在 1920×1080 范围，不是原生 HDR 显示。源预览后台生成，准备时仍可提交增强，失败可重试，原片不被替换。

保留适用的静态 Mastering Display 和 MaxCLL/MaxFALL 元数据，**不沿用或重建 Dolby Vision、HDR10+、HDR Vivid 动态映射**。多音轨、字幕、章节、附件按容器兼容性处理，不兼容流在日志说明；请检查重要素材导出后的媒体信息。

## 维护和数据

数据位于 `app/uploads/`、`app/ui_out/` 与 `app/studio-state.json`，窗口数据位于 `desktop-profile/`，便携服务日志在 `logs/desktop-server.log`。迁移时先关闭旧包，再一起复制上述素材、输出和任务索引；旧 v1.0.0 没有任务索引，文件可留存但不会自动变成历史记录。不要用旧程序文件覆盖新程序。

底部状态按钮可查看日志、复制诊断及运行真实连续视频自检。组件“已匹配”与“实际成功渲染”分别记录；自检跳过缓存。模型耗时 0 和命中帧数可核对是否真正免推理，整次任务仍可能需要其他阶段。

本次升级使用新完整包：先关闭旧程序，将新包解压到独立目录，保留旧包。制作工作区保留旧 310.8 文件及组件回退记录；新包只带当前两份 NR DLL，根目录旧备用库和历史备份不进入发行包。要恢复旧运行库，可使用保留的旧包。

应用增量机制仍只更新应用、界面、启动器和渲染程序，不覆盖媒体、设置、模型、Python 或 FFmpeg，因此本次不提供模型增量包。`tools/components.py` 仍可列出、本地校验导入与回退已有组件，不联网下载。操作与数据位置见 [包内说明源码](packaging/README-package.txt)；人工使用遇到失败时可提供日志与复制诊断的信息。

## 本次发布审查修复

- 移除发行包中的旧 310.8 根目录备用 DLL；启动器改按组件清单与转发 DLL 检查环境。
- 打包使用单次已核对的组件清单快照，复制并校验快照中的对应文件，避免并发变更混入不同版本。
- 视频直接命中完整成片缓存时，继承实际片段起点和帧映射，保留原片与结果的 PTS 对齐信息。
- 原生 NGX 参数调用改用 SDK 正式类型，替换猜测的虚函数表调用。接口核对依据包括同编译器汇编与 SDK 静态库反汇编；这些静态证据及编译成功不等于 GPU 运行验证。

## 源码与构建

当前 C++ 引擎源码已收录在 `source/`，不需要以其他项目的旧 EXE 替代本版实现。源码仓库不包含模型、FFmpeg、完整 Python 环境与发布 ZIP。

1. 安装 Python 3.10 x64，在根目录创建环境并安装锁定依赖。
2. 取得适用条款允许的 FFmpeg/FFprobe、SR 和 NR 运行 DLL，放在 `app/out/`。`packaging/fetch_dlss5.py` 按 `packaging/dlss5-catalog.json` 的固定资产 API 和哈希，通过 HTTPS 下载并核对附件、DLL；全部配置就绪后原子切换，保留旧文件与回退历史。当前三套配置使用两份不同 NR DLL。它不提供 SR DLL 或 FFmpeg。
3. 如需从源码构建引擎，运行 `bootstrap_compiler.py` 下载并校验官方 MSVC/Windows SDK 和 NGX 链接库，解压到工作区缓存，不安装系统编译器。Microsoft/NVIDIA 组件保留原许可。
4. 构建输出在 `source/out/`，复制引擎 EXE 和转发 DLL 到 `app/out/` 后可启动服务。编译成功不表示新版运行库已通过运行验证；本次按要求未运行测试或 GPU 处理。

```powershell
python -m venv app/.venv
.\app\.venv\Scripts\python.exe -m pip install -r app/requirements-installed.txt
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/bootstrap_compiler.py
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/build_engine.py
Copy-Item source/out/video2dlssnr.exe app/out/video2dlssnr.exe
Copy-Item source/out/nvngx.dll_dlssnr.dll app/out/nvngx.dll_dlssnr.dll
.\start-web.bat
```

准备好依赖与 `app/out/` 后，可构建到新暂存目录并归档：

```powershell
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/build_package.py --package packaging/build/DLSS-Studio-v1.3.0
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/archive_package.py --package packaging/build/DLSS-Studio-v1.3.0 --output outputs/releases/v1.3.0/DLSS-Studio-Windows-x64.zip
```

构建脚本拒绝覆盖非空暂存目录；已有目录请换新路径。打包会获取固定 WebView2 SDK 并验证微软引导安装器签名。上述命令是构建说明，不代表本次运行库已通过运行测试。历史测试媒体与大型验证输出不进入发行 ZIP。

## 历史验证与本次限制

2026-10-27 的运行库更新及发布审查完成了文件身份核对、源码静态审查与新引擎、转发 DLL 编译，**未运行自动化测试、GPU 测试、性能测试或新版完整包运行验证**。本次效果和硬件兼容性等待人工反馈，不承诺新版已经过稳定性验证。

以下为 **2026-10-26 旧 310.8 运行库**的历史验证，不能作为 310.9.16.0 通过的依据：源码回归 90 项 Python、9 项 Node 通过；原生套件 31 项、0 跳过、4695 断言通过，独立后处理数学测试 26658 断言通过。另行完成 SDR8/16、PQ/HLG、P3-D65、缓存免推理重合成、PTS/媒体封装、真实 192 帧暂停重开接续和两轮完整桌面测试。当日完整 ZIP 经中文空格路径独立解压，9087 个载荷文件校验、包内 Python、端口避让、真实图片/视频处理及完整原生桌面工作流通过；历史报告位于 `outputs/optimization-package-verification-20261026.json`。

当时指定原始 16 bit 样片 detail=0 逐样本一致，有损编码不作同样承诺。192 帧样片暂停接续与同分段配置不中断运行的 33,177,600 字节一致，不意味着任意分段与连续模型隐藏历史一致，也不表示新版已完成相同检查。

旧运行库的 640×360、60 帧引擎对照中，传输缓冲复用与安全提交合并带来排除初始化的稳定吞吐提升：运动补偿开启约 22.6%，关闭约 27.3%，前后输出字节一致。仅指当时指定机器的小尺寸原始引擎，不外推新版运行库、完整导出、4K 或其他显卡。更多硬件、长电影、主观人像质量和动态 HDR 重建不在历史已验证承诺内。

原始帧协议与缓存限制见 [ENGINE-PROTOCOL.md](source/ENGINE-PROTOCOL.md)。本地证据位于 `outputs/core-optimization-validation/`、`outputs/optimization-video-2026-10-26/` 和独立包验证报告；这些测试媒体、输出与大型证据不随源码或发行包附带。

## 目录与许可

| 路径 | 用途 |
| --- | --- |
| `app/web/` | 中文工作台、播放器、参数和历史 |
| `app/studio_server.py`、`studio_storage.py` | API、串行队列、状态与历史 |
| `app/studio_cache.py`、`process_runner.py` | 内容缓存、接续与自有进程管理 |
| `app/nr_video.py`、`video_pipeline.py`、`render_engine.py` | 视频时间戳、媒体、高精度处理和编码 |
| `app/gpu_runtime.py`、`component_store.py` | 实际 GPU/模型身份与本地组件管理 |
| `source/` | C++ 引擎、转发器、原生测试和协议 |
| `packaging/` | 便携编译、桌面壳、构建、更新及说明 |
| `tests/` | Python、Node 和工作台回归 |

引擎来源为 [DaniilSokolyuk/video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr)，来源及本地修改已明确归属。当前三套配置、两份 310.9.16.0 NR DLL 的来源、字节数和 SHA-256 见 `app/dlss5-components.json` 与 `packaging/dlss5-catalog.json`，适用条款独立于本项目。自动组件匹配不会扩大 NVIDIA 官方支持范围。

第三方组件、Python 依赖、FFmpeg、NVIDIA DLL、WebView2 和图标保留各自条款，不统一套用 MIT。[LICENSE](LICENSE)、[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) 和各组件附带许可提供具体范围。

本项目原创部分采用 MIT 许可；[引擎 MIT 许可](source/THIRD-PARTY-engine-LICENSE.txt)及第三方原有归属继续保留。本项目并非 NVIDIA 官方产品。
