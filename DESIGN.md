# DLSS Studio 现有界面约定

本项目为中文 Windows 本地图片/视频增强工具。沿用现有深浅主题、玻璃偏好、工作台布局和原生控件，不因底层优化增加设置项或重做视觉。

主题与颜色由 `app/web/studio.css`、`dark-polish.css`、`desktop-shell.css`、`glass-mode.css` 及现有主题切换拥有；本轮不新增视觉 token。

共享参数弹窗由 `app/web/workspace.js` 拥有，图片与视频复用同一份表单控件和监听器。值变化不得丢失键盘焦点或编辑位置；重新分页时保留当前编辑控件所在页，关闭后返回原触发控件。

原生 range/select 的平台键盘行为是现有产品约定。上传、处理、预览和下载由 `studio.js` 及共享播放器拥有，保持既有成功、等待和失败反馈。窄窗口通过参数弹窗分页保持可达，不增加新的操作层级。

验证参照 `design-qa.md`、现有桌面 smoke 流程及本轮真实工作台 E2E。截图和测试素材保存在工作区外的专用验证目录。

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Form | 共享设置表单与 workspace 参数弹窗 | app/web/index.html、workspace.js | 图片/视频按已有条件显示控件 | 真实键盘调节、分页、关闭后焦点返回 |
| Select/Listbox | 浏览器原生 select | app/web/index.html | 沿用原生键盘选择与主题样式 | 选择后值和焦点保留、条件字段可达 |

设置表单使用浏览器原生 min/max/step 等约束，由共享提交入口调用 `reportValidity()`；保留原生无效控件聚焦和错误反馈，不为通用审查规则关闭这套校验。按钮事件由外部共享脚本按 ID/data 属性绑定。
