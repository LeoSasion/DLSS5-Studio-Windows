# DLSS-Studio-Windows

面向 Windows 的本地 DLSS 图片与视频增强工作台，提供中文界面、深浅主题和无边框独立窗口。

> 2026-10-26 优化构建已在本地产出，尚未上传 GitHub Release。下述新功能对应当前源码和本地待发布包，历史 [v1.2.0](https://github.com/LeoSasion/DLSS-Studio-Windows/releases/tag/v1.2.0) 附件不包含本轮改动。
>
> 本项目原创部分采用 MIT 许可。引擎来源代码与 NVIDIA、FFmpeg 等第三方组件保留各自适用条款，详见 [许可说明](THIRD-PARTY-NOTICES.md) 和 [引擎 MIT 许可](source/THIRD-PARTY-engine-LICENSE.txt)。这不是 NVIDIA 官方项目。

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

本地待发布文件为：

- `DLSS-Studio-Optimized-20261026-Windows-x64.zip`：完整便携环境。
- `DLSS-Studio-Optimized-20261026-update.zip`：应用更新，不包含 Python、FFmpeg 或模型 DLL。
- `DLSS-Studio-update.py`：供没有工具目录的旧包使用，功能与完整包 `tools/incremental_update.py` 相同。

这些文件尚未出现在历史 Release 附件中。GitHub 自动生成的 Source code 压缩包只有源码，不含可直接运行的完整环境。

将完整包解压到可写目录，双击 `DLSS Studio.exe`。Python 与依赖已内置；独立窗口需要 Microsoft WebView2，缺少时可选择通过微软引导组件联网安装。不要在 ZIP 内直接运行或单独移动 EXE。

窗口右上角“浏览器”打开当前实例的实际地址，通常为 `http://127.0.0.1:7860/`。7860 被占用时，EXE 在 7860–7959 选择可用端口。服务只监听 `127.0.0.1`。外部浏览器与独立窗口各自保存工作台，共用处理服务；请保持 EXE 开启或最小化，关闭时会停止它启动的服务。手动 `start-web.bat` 默认使用 7860，不提供 EXE 的端口自动避让。

需要 Windows 10/11 x64 和兼容的 NVIDIA 显卡、驱动。当前实机验证环境为 Windows 11、RTX PRO 6000 Blackwell、591.86 驱动；CPU 编码不能代替神经渲染所需显卡。RTX 30/40 系已测试识别和组件匹配，尚不能据此承诺对应实体卡或其他驱动全部成功。

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

应用更新前暂停视频并关闭程序。下面从完整包根目录运行，路径按实际修改；旧包可把工具路径替换为另附 `DLSS-Studio-update.py`：

```powershell
.\runtime\python.exe -X utf8 -B .\tools\incremental_update.py apply "D:\Apps\DLSS Studio" "D:\Downloads\DLSS-Studio-Optimized-20261026-update.zip"
.\runtime\python.exe -X utf8 -B .\tools\incremental_update.py rollback "D:\Apps\DLSS Studio" "更新输出的回退编号"
.\runtime\python.exe -X utf8 -B .\tools\components.py --app .\app list
```

更新工具校验、备份并事务应用，备份位于 `updates/history/`，不覆盖媒体、设置、模型、Python 或 FFmpeg；不能通过直接解压更新 ZIP 代替工具。`tools/components.py import` 需要本地 x64 DLL、完整可信 SHA-256、版本、来源及对应 `rtx30|rtx40|rtx50` 配置，随后运行连续视频自检；支持 `rollback`。详细示例见 [包内说明源码](packaging/README-package.txt)。校验哈希不代替可信来源或数字签名。

## 源码、构建与复验

当前 C++ 引擎源码已收录在 `source/`，不需要以其他项目的旧 EXE 替代本版实现。源码仓库不包含模型、FFmpeg、完整 Python 环境与发布 ZIP。

1. 安装 Python 3.10 x64，在根目录创建环境并安装锁定依赖。
2. 取得适用条款允许的 FFmpeg/FFprobe、SR 和 NR 运行 DLL，放在 `app/out/`。`packaging/fetch_dlss5.py` 可下载清单固定的三套架构 NR DLL 并校验；RAR 解包需要 UnRAR/WinRAR，仅构建时使用。它不会提供 SR DLL 或 FFmpeg，不应将任意 DLL 当成相互兼容。
3. 如需从源码构建引擎，运行 `bootstrap_compiler.py` 下载并校验官方 MSVC/Windows SDK 和 NGX 链接库，解压到工作区缓存，不安装系统编译器。Microsoft/NVIDIA 组件保留原许可。
4. 构建输出在 `source/out/`。把兼容的 SR DLL 放在该目录以运行 GPU 测试；缺显卡或 DLL 时检查测试的跳过数，不能把跳过当作 GPU 验证成功。测试通过后复制引擎 EXE 和转发 DLL 到 `app/out/`，再启动服务。

```powershell
python -m venv app/.venv
.\app\.venv\Scripts\python.exe -m pip install -r app/requirements-installed.txt
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/bootstrap_compiler.py
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/build_engine.py --tests
Copy-Item source/out/video2dlssnr.exe app/out/video2dlssnr.exe
Copy-Item source/out/nvngx.dll_dlssnr.dll app/out/nvngx.dll_dlssnr.dll
.\start-web.bat
```

当前源码回归入口：

```powershell
.\app\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py"
node --test tests/frontend-state.cjs
```

Node 测试使用内置测试框架和真实前端源码，不需要安装前端依赖。Python 部分测试需要 `app/out/ffmpeg.exe`、`ffprobe.exe`；原生 GPU 回归还需要兼容设备和 DLL。

准备好依赖与 `app/out/` 后，可构建到新暂存目录，再归档及独立解压验证：

```powershell
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/build_package.py --package packaging/build/DLSS-Studio-Optimized-20261026
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/archive_package.py --package packaging/build/DLSS-Studio-Optimized-20261026 --output outputs/DLSS-Studio-Optimized-20261026-Windows-x64.zip
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/verify_package.py --archive outputs/DLSS-Studio-Optimized-20261026-Windows-x64.zip --report outputs/optimization-package-verification-20261026.json
.\app\.venv\Scripts\python.exe -X utf8 -B packaging/incremental_update.py create packaging/build/DLSS-Studio-Optimized-20261026 outputs/DLSS-Studio-Optimized-20261026-update.zip
```

构建脚本拒绝覆盖非空暂存目录；已有目录请换新路径。打包会获取固定 WebView2 SDK 并验证微软引导安装器签名。独立验证逐文件核对清单，使用包内 Python，测试端口避让、服务所有权、真实图片/视频增强及完整原生工作台，需要兼容显卡和交互桌面。4K 回归夹具由验证脚本在核验清单后加入测试目录，不进入发行 ZIP。

## 已验证与限制

2026-10-26 源码回归为 90 项 Python、9 项 Node 通过；原生套件 31 项、0 跳过、4695 断言通过，独立后处理数学测试 26658 断言通过。另行完成 SDR8/16、PQ/HLG、P3-D65、缓存免推理重合成、PTS/媒体封装、真实 192 帧暂停重开接续和两轮完整桌面测试。最终完整 ZIP 另经中文空格路径独立解压，9087 个载荷文件校验、包内 Python、端口避让、真实图片/视频处理及完整原生桌面工作流均通过；报告位于 `outputs/optimization-package-verification-20261026.json`。

指定原始 16 bit 样片 detail=0 逐样本一致，有损编码不作同样承诺。192 帧样片暂停接续与同分段配置不中断运行的 33,177,600 字节一致，不意味着任意分段与连续模型隐藏历史一致。

640×360、60 帧引擎对照中，传输缓冲复用与安全提交合并带来排除初始化的稳定吞吐提升：运动补偿开启约 22.6%，关闭约 27.3%，前后输出字节一致。仅指指定机器的小尺寸原始引擎，不外推完整导出、4K 或其他显卡。更多硬件、长电影、主观人像质量和动态 HDR 重建不在本轮已验证承诺内。

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

引擎来源为 [DaniilSokolyuk/video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr)，来源及本地修改已明确归属；优化顺序依据当前代码问题和实测，不依据历史上游版本距离。初始三套 NR DLL 来源、字节数和 SHA-256 见 `app/dlss5-components.json`，适用条款独立于本项目。自动组件匹配不会扩大 NVIDIA 官方支持范围。

第三方组件、Python 依赖、FFmpeg、NVIDIA DLL、WebView2 和图标保留各自条款，不统一套用 MIT。[LICENSE](LICENSE)、[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) 和各组件附带许可提供具体范围。
