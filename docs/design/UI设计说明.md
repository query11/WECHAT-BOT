# 运行中心界面与操作设计

## 2026-11-08 配色统一与文字可读性

布局坐标、控件、入口和文案职责不变，只调整颜色、层级和次要按钮样式。

- **共享色板**：所有颜色集中在 `tray_theme.cs` 的 `TrayDesign`，界面脚本只引用 `[TrayDesign]::名称`。此前运行中心主色为 `#4A90E2`、三个设置窗口为 `#2563EB`，另有 3 种辅助灰、3 种红色、2 种黄色并存。
- **文字对比度**：文字色在白色卡片和页面底色上均不低于 4.5:1。主色保留原色相并加深为 `#206CC6`（白字 5.2:1），辅助文字 `#95A5A6`（2.4:1）改为 `#5F6E82`（4.8:1），禁用文字由 2.0:1 提高到 2.8:1。成功、警告、危险文字分别为 `#2F7D46`、`#996000`、`#B42318`。
- **状态色分层**：状态圆点和托盘图标保留明亮的指示色；连接徽章文字由 `TrayDesign.Readable()` 自动加深到可读色（原绿色文字仅 2.3:1）。错误状态的标题改用危险文字色，状态卡描边统一取指示色的 25% 浅色。
- **页面与卡片**：运行中心由“白底 + `#F8F9FA` 卡片”改为“`#F3F5F9` 页面 + 白色卡片”，与其他窗口和概念图一致。
- **层级**：品牌标题从 18pt 降到 16pt，让 18pt 的助手状态成为首要信息；快捷任务和模板窗口标题由 17pt 统一为 16pt。
- **次要操作**：卡片标题行的“定时任务、任务模板、后端配置、继续修改、全部结果、最近动态”改为无边框的链接样式（`TrayButton.Link`），悬停显示浅蓝底；右上角入口卡不再挤满描边按钮。原“配置”改为“后端配置”，与托盘菜单和窗口标题一致。
- **细节**：任务统计改为“等待任务 0 · 附件处理 0”，不再用空格对齐；删除 `Set-Indicator` 中未使用的 `$mark`。

`tests/selftest_ui_palette.py` 检查界面脚本没有写死颜色、引用的 `TrayDesign` 成员都存在，以及文字色对比度达标。`tray_theme.cs` 已按 C# 5 编译检查。WinForms 实际渲染需在 Windows 上运行 `tray_app.ps1 -VerifyUX` 并查看 `.runtime/workbench-20261107/` 下的截图。

## 2026-11-07 定时任务管理

运行中心常用入口标题行增加“定时任务”，与“任务模板”并列，底部四个原入口保持可见。快捷任务增加第六个页签；左侧切换计划清单/最近触发，右侧集中显示类型、重复方式、本机时间、内容和核对结果。

创建与编辑都经“核对计划 → 确认保存计划”。预览前部展示微信用户、自动执行说明和下次时间；计划暂停状态不会被编辑隐式清除。触发历史为只读，关联任务提供“任务记录”按钮。切换微信用户清空草稿和确认；原生时间选择控件避免手工输入日期格式。

窄窗口中两个标题按钮分别为 88 逻辑像素；定时页沿用统一 DPI 布局及窗口/列宽偏好。旧后台、提醒未启用、文件不可读显示各自提示。隔离截图和实测证据见 `.runtime/schedules-20261107/`。

## 2026-11-07 本机文件任务

运行中心常用入口底行增加“文件任务”，与附件核对、常用应用、固定记忆并排；托盘提供同名入口。快捷任务新增“文件任务”页签，沿用现有左右布局：左侧拖放区域、文件选择/移出按钮和清单，右侧模板/自定义选择、要求、结果去向和预览确认。

预览首先显示微信用户、交付方式和本次独立输出目录，其余详细要求可滚动查看。文件名悬停显示完整路径，小文件以 B/KB 显示，避免显示成 0 MB。输入、模板、去向变化使旧确认失效，切换用户清空草稿；保存的窗口偏好不包含文件路径或执行确认。验证和边界见 [本机文件任务](../features/功能改进-本机文件任务.md)，证据位于 `.runtime/local-files-20261107/`。

## 2026-11-07 结果继续修改

运行中心最近结果、快捷任务结果页和任务记录增加“继续修改”入口。快捷任务新增第四个页签：左侧版本列表与选择按钮，右侧依次为来源、文件/文字模式、文件复选清单、修改要求、预览与确认。保持既有配色、原生控件与 DPI 布局；历史“输入与模板”展示版本关联和本次要求，文件清单展示可定位的旧版本副本。

编辑内容、切换模式、选择文件或来源时使旧确认失效；读取和提交期间禁用重复操作。版本列表包含状态，未结束的版本不能作为修改来源。按任务编号直接进入时先解析所属用户，再载入来源。实现范围见 [结果继续修改](../features/功能改进-结果继续修改.md)，截图与证据位于 `.runtime/revisions-20261107/`。

## 2026-11-07 任务工作台

顶部状态压缩为横向栏，任务/结果与待处理清单占据主区域；右侧依次保留常用入口、当前模型和服务维护。待处理清单可切换为最近动态，支持按编号直达任务记录和附件核对。结果卡片按有文件、纯文字和失败状态提供操作，失败与检查问题分别使用红色、橙色强调，辅助文字加深。原阶段进度、窗口记忆和服务控制继续保留。最小逻辑尺寸为 880×640，首次默认尺寸为 960×720，已有偏好优先。

范围、入口行为与后续顺序见 [任务工作台与后续安排](../features/功能改进-任务工作台.md)。隔离界面证据位于 `.runtime/workbench-20261107/`，实际加载与验证结果见 [可靠性检查与验收](../testing/可靠性检查与验收.md)。

## 2026-11-07 阶段进度与界面记忆

当前任务卡片在执行时收起空态图标，展示实际阶段、任务名、具体操作及总耗时/阶段耗时；底部队列、附件与停止按钮保留。取消时保留阶段并显示停止提示，空闲后隐藏旧计时。阶段由执行路径直接报告，当前阶段的提示展示实际经过的节点，不生成估算百分比。紧凑和常规布局均经过控件边界检查及截图确认。

四个窗口共用本地偏好存储，按窗口分别保存逻辑尺寸、手动列宽及适用的筛选与页签；自动布局尊重用户列宽。明确入口页签优先于上次页签，不恢复任务执行或待确认操作。验证使用隔离目录、真实窗口和独立进程重开，另外检查损坏配置回退和超大尺寸收敛。证据在 `.runtime/progress-memory-20261107/`，最终加载与边界见 [可靠性检查与验收](../testing/可靠性检查与验收.md)。

## 2026-11-07 快捷任务与结果卡片

“常用入口”保留模板管理按钮，增加固定模板选择框、“运行模板”和“附件核对”，常用应用与固定记忆改为并排按钮。紧凑布局中附件核对、常用应用、固定记忆同行显示，保留所有入口。

新增“快捷任务”窗口，以“运行模板 / 附件核对 / 任务结果”三个页签组织操作。左侧选择对象，右侧展示定义、输入、核对清单或文件结果；用户先预览再明确确认运行。读取和写入异步执行，等待回执期间禁用重复提交，失败提示重新核对。高 DPI 下重新计算页签内控件位置，避免锚点与缩放叠加导致裁切。

运行中心空闲时用最近结果卡片替换空任务占位，显示摘要、独立检查结论和文件操作；当前任务、排队或附件处理优先。结果文件不存在时禁用打开；常用文档使用系统默认应用，其他文件只定位目录。现有状态、连接、日志与服务维护区域继续独立刷新。

窗口截图和隔离操作证明位于 `.runtime/quick-tasks-20261107/`。实际加载、回归结果及实机边界见 [可靠性检查与验收](../testing/可靠性检查与验收.md)。

## 2026-11-07 模板窗口与任务详情

运行中心“常用入口”卡片右上角和托盘菜单增加“任务模板”。模板窗口左侧是名称与版本列表，右侧依次填写输入要求、处理步骤、输出目录、输出格式和可选检查条件；支持新建、修改、复制、删除、Ctrl+S 和未保存提示。多用户按微信用户区分，读取与保存异步执行，操作期间禁用编辑。

任务记录上方保留搜索、筛选和任务列表，下方先显示摘要、执行/发送/结清状态与文件清单，再按“完整回复”“输入与模板”“检查结果”分区。增加文件定位和已结束任务“保存为模板”入口。原操作下拉框保留，执行按钮显示具体动作，旁边显示不可用原因或必要提示。

真实 WinForms 隔离验收覆盖模板增改复制删除、任务转模板草稿、详情内容、文件清单和直接操作；运行中心验证覆盖七种状态、十组尺寸及五个工具窗口的打开、复用和托盘入口。模板窗口修正了初次显示时的 DPI 缩放，列表列宽随实际宽度调整。样例证据在 `.runtime/template-ui-20261107/rich/`，最终回归和实际加载记录见 [可靠性检查与验收](../testing/可靠性检查与验收.md)。跨显示器 DPI 切换仍需现场确认。

## 2026-10-03 层次与状态颜色微调

保留现有布局、尺寸、文案和功能。运行中心的卡片增加约 3 个逻辑像素的轻薄底部阴影及内侧高光；按钮增加克制的明暗渐变，悬停时阴影略加深，按下时收起阴影。模型选择按钮和禁用按钮保持平面样式。深度样式按窗口启用，其他设置窗口不启用。

在线待机的状态点与托盘图标改为蓝灰 `#71869B`，标题使用深蓝灰 `#526A80`；已唤醒保持绿色状态点 `#52C41A`，标题使用深绿 `#2F7D46`。状态卡保留原底色，描边随状态轻微区分。右上角“微信已连接”仍为绿色，独立表达连接正常。

验证通过七种状态、待机/唤醒颜色区分、十组窗口尺寸及原有操作边界；四个工具入口的可见窗口、重复点击复用和托盘入口检查通过。本轮只重新加载托盘界面，后台 PID 5752、启动时间与配置/偏好/固定记忆哈希保持不变。实际窗口可见且响应正常，截图为 `.runtime/ui-depth-20261003/live-running-center.png`，加载记录为同目录 `live-ui.json`，两种状态样例为 `tray-standby.png` 与 `tray-working.png`。状态样例使用隔离数据；未切换真实后台状态。跨显示器 DPI 与现场鼠标操作未在本轮验证。

## 2026-10-03 极简视觉调整

按现有截图调整实际 WinForms 界面，保留六张卡片、全部入口、文案及后台命令。左侧约 60% 显示助手状态、当前任务和最近动态；右侧约 40% 显示模型配置、常用入口和服务维护。

- 配色统一为柔和蓝 `#4A90E2`、在线绿 `#52C41A`、白色页面、`#F8F9FA` 卡片、`#2C3E50` 正文和 `#95A5A6` 辅助文字。状态以小圆点和连接徽章呈现，移除整张状态卡的彩色底及 Logo 渐变。
- 使用 8px 间距体系，卡片约 12px 圆角、按钮约 8px 圆角；细灰边框，无大面积阴影或装饰。页面标题约 24px，卡片标题约 18px，正文约 14px，辅助文字约 13px；WinForms 字号以 pt 设置，按当前显示器 DPI 绘制。
- 唤醒助手保留唯一填色主按钮层级；待机、启动和继续接收仍使用原有状态驱动规则。运行时长和最近微信通信改为统一的线性图标、标签、数值横向排列。
- 当前任务空态使用简洁文件线稿；DeepSeek / Claude 使用浅色 Toggle Chip，并保留原 `Primary` 选中状态及忙时禁用规则。
- 后端配置、常用应用和固定记忆统一入口尺寸、左侧线性图标和右侧箭头。服务维护保留原有重启与日志操作。
- 最近动态使用细线和圆点呈现时间线，继续使用原生只读 TextBox，保留滚动、选中、复制和阅读期间暂停刷新。时间线标记跟随实际文本视口滚动。
- 默认窗口增加留白，小屏或较矮窗口使用紧凑布局；初始尺寸按显示器工作区收敛。布局调整仅影响控件位置与尺寸。

本轮源码修改范围为 `tray_app.ps1`、共享 `tray_theme.cs` 和本说明。界面验证、样例预览及 UI 重新加载记录位于 `.runtime/ui-polish-20261003/`。样例截图中的任务、时间和事件为验证数据，真实运行中心始终读取实际后台状态。

验证结果：七种样例运行状态、操作锁、反馈保持、动态复制及阅读保护通过；880/1040 两种逻辑宽度与 640/656/800/816/824 五种高度，共十组布局边界检查通过，覆盖长任务、长模型名、两行模型调用状态和未通信占位文案。四个工具入口均验证了实际可见窗口、重复点击复用和隐藏运行中心后的托盘入口；另以隔离配置和示例记忆检查共享按钮渲染。未触发真实模型 API、保存配置或重启后台。

本轮仅重新加载托盘界面，加载记录确认后台 PID 与启动时间不变，配置、应用偏好及固定记忆哈希不变。已确认新版窗口实际可见并响应，真实窗口内容截图为 `.runtime/ui-polish-20261003/live-running-center.png`，由已加载窗口的原生 PrintWindow 获取。桌面自动化未枚举出该 WinForms 窗口，本轮入口检查使用程序自身验证模式，不记为真实鼠标点击；跨显示器 DPI 切换及不同系统缩放下的现场鼠标操作仍需实机确认。

## 2026-10-02 视觉细化

保留现有双列、所有操作入口和窗口尺寸，细化冷蓝配色、卡片边框与文字层级。状态卡随实际状态使用浅蓝、浅绿、浅黄或浅红底色；模型选择改为等宽按钮，常用入口采用浅蓝图标底和居中的双行文字。主次、危险、禁用、悬停及键盘焦点样式统一；任务空态略收紧，最近动态区域增加 20 个逻辑像素高度，默认可显示更多行。

- 修改仅涉及 `tray_app.ps1` 和共享 `tray_theme.cs`；后端、命令、配置和保存规则保持原样。
- `tray_app.ps1 -VerifyUX` 通过七种状态、操作反馈保持、忙时禁用、版本提示、动态复制等检查；补充 760/1040 逻辑宽度下的长任务、长模型名、两行模型状态和卡片子控件边界检查。
- 检查实际控件绘制的 200% 缩放预览：待机、错误及窄窗口长文案。后端配置、固定记忆、任务历史使用合成配置或示例数据完成预览；常用应用使用内置假目录验证。
- 相关 Python 回归 `tests.selftest_runtime tests.selftest_standby tests.selftest_experience tests.selftest_identity` 共 46 项通过。本轮为界面修改，没有重复全量后端回归。
- 本轮只重新加载托盘进程，已确认新版“微信助手 · 运行中心”窗口有有效句柄且响应正常；后台 PID 22880 及启动时间保持不变、仍在线待机，配置、应用偏好和固定记忆的哈希未改变。证据为 `.runtime/ui-polish-20261001/live-ui.json`。
- 截图与布局验证使用隔离渲染；Windows 窗口自动化工具初始化失败，因此没有把这轮结果记为真实鼠标点击验收。125%/150% 与跨显示器 DPI 切换仍需现场检查。

预览和本轮证据位于 `.runtime/ui-polish-20261001/`，根目录兼容预览路径仍为 `.runtime/tray-preview.png`。以下记录保留其原日期与验收范围。

概念图由内置 imagegen 工具于 2026-09-30 生成：[运行中心概念图](wechat-assistant-concept-20260930.png)。它用于指导现有 Windows Forms 界面的布局、层级和样式，最终显示内容以真实后台状态为准。

这轮仅调整界面和操作方式，复用已有启动、唤醒、待机、取消、重启、模型切换及设置服务。

- 左侧依次显示助手状态、当前任务和最近动态，右侧集中显示模型配置、常用入口及服务维护。
- 主按钮根据状态呈现唤醒、进入待机或继续接收；服务停止时呈现启动入口。
- 无当前任务时禁用停止按钮；有任务、排队任务或附件处理时暂不能重启。
- 持续状态说明和操作反馈各占一处，避免周期刷新覆盖点击结果。
- 设置窗口统一颜色、文字层级、主次按钮和保存反馈，保留已有保存及关闭规则。

## 实现与验证

实际界面保留原有气泡标识，使用原生 Windows Forms 控件实现。新操作复用原后台命令，Python 服务及配置结构均未修改。

- `tray_app.ps1 -VerifyUX`：七种样例状态、反馈保持、操作提交期间锁定、仅排队附件的边界、重复启动防护，以及 760/1040 逻辑宽度和动态复制行为均通过。样例截图位于 `.runtime/ui-review-20260930`，属于界面验证，不表示真实通信状态。
- 后端配置、常用应用、任务记录与固定记忆的界面保存/读取检查使用隔离数据；未读取真实密钥到输入框，也未测试模型 API。
- 四个窗口入口的 8 条检查通过，覆盖按钮打开、重复点击复用，以及运行中心隐藏后从托盘打开窗口。
- 相关离线回归 35 项通过，覆盖配置、应用偏好、任务详情/历史、运行状态与待机。
- 200% 缩放下已检查实际控件渲染。多显示器 DPI 切换仍待现场检查。

原始验证日志保存在工作区 `.runtime/ui-design-*.txt` 及 `.runtime/ui-review-20260930-verification.txt`。

## 实际界面载入

2026-09-30 18:09 已重新打开实际运行中心，确认窗口可见，新分组和按钮已载入。原后台进程 PID 5308 保持运行，心跳正常；`config.json`、`app_preferences.json` 和 `BOT_MEMORY.md` 与修改前的哈希或不存在标记一致。载入证据保存在 `.runtime/ui-design-live.json`。

当前后台报告“微信加密连接中断”，实际界面显示“连接需要检查”，主按钮为禁用的“等待微信连接…”。因此本轮没有完成真实微信通信下的唤醒、待机验证；七种状态的操作规则已通过样例验证。未为此修改网络、代理或 Python 后台。

后续连接修复：2026-09-30 18:42 已确认实际后台多轮微信长轮询成功，运行中心显示“微信已连接”“助手已唤醒”。根因是代理虚拟 DNS 地址及部分公网地址的握手失败，已在 iLink 客户端加入官方域名专用的加密 DNS 解析和握手地址回退，未修改系统代理或用户配置。同时修正恢复提示：必须确认真实通信成功，重启期间暂时清空错误不再显示恢复，后续断线也会撤下旧的恢复提示。详见 `.runtime/connection-fix-live.json`。

## 生成提示词

使用内置 imagegen，无透明背景。原始提示词如下：

```text
Use case: ui-mockup. Asset type: a high fidelity concept image for an existing Chinese Windows desktop app. Primary request: redesign the existing WeChat assistant operation center as a calm, polished, practical native desktop control panel, without adding features. Render exactly one front-on application window, filling the image, with an approximately 4:3 aspect ratio. No surrounding desktop, no perspective, no multiple screens. App title is 微信助手 · 运行中心. Keep a small blue rounded double chat-bubble icon, a very light cool gray background, white rounded panels, subtle light blue borders, restrained cobalt blue #2563EB accents, dark navy headings and muted slate captions. Crisp Microsoft YaHei UI Chinese typography, generous but efficient spacing, modest 12px corners, flat native controls, no glossy material or dramatic gradients. Design for an approximately 840 by 650 logical-pixel window. Header: small brand icon and 微信助手, subtitle 运行中心 · 让微信连接你的电脑, a discreet blue dot and 微信已连接 on the right. Two columns under the header, main column about 540 pixels and a right column about 230 pixels, 16 pixel gap. Main column top: pale blue service card with small caption 助手状态, clear but moderate headline 在线待机, supporting sentence 唤醒后即可接收微信任务, small metrics 运行时长 02:18:36 and 最近通信 16:20:30, one blue primary button 唤醒助手 and a small line 在微信发送“开始对话”也可唤醒. Main column middle: white 当前任务 card, clean empty state 暂无执行中的任务, counts 等待任务 0 and 附件处理 0, a disabled quiet red outlined 停止当前任务 button, small supporting text 任务执行时可在这里查看进度. Main column bottom: white 最近动态 card, 3 compact time-stamped rows with subtle blue dots; top right small text button 任务记录. Right column top: white 模型与配置 card, compact segmented choices DeepSeek and Claude, DeepSeek selected pale blue; model label deepseek-flash; muted status 尚无调用记录; neutral full-width 后端配置 button. Right column middle: white 常用入口 card with two full-width easy-to-click neutral buttons 常用应用 and 固定记忆, each with a simple line icon and a short muted one-line description. Right column bottom: compact white 服务维护 card with small secondary buttons 重启服务 and 打开日志. Footer across the window: soft gray small text 关闭窗口后仍在托盘运行, with a slim separate unobtrusive feedback line ready to show action acknowledgements. Every button readable, no overlapping text, no fake charts, no extra navigation, no new cloud/sync/AI capabilities, no decorative stock imagery. A thoughtful Chinese productivity desktop UI that is realistically implementable with Windows Forms.
```
