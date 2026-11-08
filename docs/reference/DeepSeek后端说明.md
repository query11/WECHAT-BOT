# DeepSeek 官方后端

> 本文保留详细行为说明及历史维护记录；首次安装请先阅读[快速开始](../QUICKSTART.md)。本机路径已改为示例，历史验证结论只代表当时环境。

本项目现在可在 `deepseek` 与 `claude` 两个后端之间切换。DeepSeek 模型请求直连 `https://api.deepseek.com/anthropic`，使用官方 `deepseek-flash`；本机仍由 Claude Code 执行文件与工具操作。这是 DeepSeek 官方文档支持的兼容接口接入，不使用原中转站，也不自动回退到 Claude 模型。

## 配置和切换

在运行中心“当前模型”卡片的下拉选择器中选择后端；托盘菜单“设置 → 模型后端”也可切换。勾选项表示后台实际正在使用的后端。空闲时点击后立即生效并保存，无需重启；有任务、附件或收尾操作时暂时禁用。配置校验或保存失败时保留原后端；有其他尚未加载的配置修改时，先重读配置。

`config.json` 中的 `backend` 选择后端，`deepseek.model` 默认 `deepseek-flash`，`deepseek.key_file` 默认 `.runtime/deepseek-api.key`。密钥只放本机 `.key` 文件，不写入示例配置、命令行参数或版本控制。也支持 `DEEPSEEK_API_KEY` 环境变量；设置时优先使用该变量。

在项目目录执行：

```powershell
python tools/set_backend.py deepseek
# 或切回原 Claude：
python tools/set_backend.py claude
# 或切换到后端配置中保存的服务（使用其 id，例如 kimi）：
python tools/set_backend.py kimi
```

脚本只原子保存配置，不打断正在运行的任务。任务空闲时，在微信对话中发送 `/reload`，或正常关闭后台再从现有入口启动。不要用强制停止来切换忙碌中的后台。运行状态 `.runtime/status.json` 的 `backend` 显示当前实际加载的后端。

原 `claude_bin`、`claude_extra_args` 和用户 Claude 设置保持不变。DeepSeek 子进程使用隔离配置目录 `.runtime/deepseek-cli`；不加载原用户/项目设置中的中转地址、认证、插件和 MCP 配置。工具权限仍沿用 `claude_extra_args`，没有打开跳过权限开关。需要运行命令时，应显式配置所需的工具许可；默认不能把任意 PowerShell 命令当作已授权。

DeepSeek 会话索引是 `.runtime/deepseek-sessions.json`，原 Claude 会话仍是 `sessions.json`，两者互不覆盖。切换后不会自动把另一模型的聊天历史上传。固定记忆和当前用户最近收件路径仍由桥注入；附件名和文件内容只作为资料处理。

## 支持范围

- 微信收发、待机、任务队列、提醒、取消、文件上传和回执流程保持原实现。
- `deepseek-flash` 可读取图片；可配置的 `deepseek-v4-pro` 不支持图片，截图入口会明确提示。
- 文本文件读取、写入、工具调用和连续对话已使用真实官方模型验收。
- 原样发送附件使用原始文件路径，不通过模型重新生成内容。Write 工具可能改变文本换行格式，不能把文本一致等同于字节完全一致。
- 超时和 `/stop` 继续终止本机执行进程树。已完成的文件修改不会自动撤销。
- 官方拒绝、认证失败、权限拒绝和执行错误不能当作任务成功；不自动切回收费的备用模型重跑。

## 验证依据

本轮实际调用证据保存于 `.runtime/deepseek-acceptance/`，只使用独立测试文件：

- `harness-first.json`：真实 Read/Write，中文与验收码一致；源文件 CRLF 与输出 LF 差异单独记录。
- `harness-continuity.json`：真实续接会话，返回上一轮验收码。
- `bridge/report.json`：真实 `Bridge._run_task` 与官方模型，使用模拟微信发送端；最近收件路径、内容码、回传路径和回执顺序通过。没有据此宣称手机送达。
- `vision-report.json`：真实读取人工生成的测试图片，识别指定文字和数字。
- `command-report.json`：未授权命令被拦截。`command-allowed-report.json`：隔离测试仅许可 `python --version` 后，真实命令执行通过，生产权限未扩大。
- `cancel/report.json`：真实模型启动测试脚本后取消，进程退出且进度文件停止变化。

CLI 返回的 `modelUsage` 用于确认所选模型，不作为实际账单；兼容接口中的费用估算可能不准确，以 DeepSeek 官方账单为准。

官方资料（2026-09-29 核对）：

- [Claude Code 官方接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code)
- [Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api)
- [模型能力](https://api-docs.deepseek.com/quick_start/pricing)

本轮发布：2031 项全量离线测试、86 项专项通过。正式后台已于 2026-09-29 正常重启并以 DeepSeek 在线待机，部署记录为 `.runtime/deepseek-acceptance/deployment.json`。手机端尚未在新后端下实测。

## 在界面中配置 API

运行中心右上角“后端配置”或托盘菜单可打开配置页。

- **DeepSeek 官方**：填写新 API Key，选择 deepseek-flash / deepseek-v4-pro，官方接口地址固定。
- **Claude**：默认沿用原 Claude 设置。需要自己管理 API 时勾选“使用自定义 Claude API”，填写 **Anthropic 兼容**的 HTTPS 基础地址、模型名和 Key；不支持把 OpenAI-only 的 `/chat/completions` 地址当成 Claude 接口。
- Key 输入框不会预填旧值。留空保留已配置的 Key，填入新值则替换；“显示”只显示本次新输入。
- “测试连接”只用当前编辑值验证一次简短模型请求，会产生少量 API 用量，不保存配置，不验证所有工具能力。
- “保存并应用”先保存，再请求后台空闲时加载。不会切换当前选中的后端；忙碌时旧配置继续执行，稍后再次点击即可应用。后台未启动时，下次启动读取保存值。
- 配置文件被其他窗口修改时，拒绝覆盖，请重开配置页。保存失败保留旧配置；加载失败会说明已保存但尚未应用，原运行后端保持不变。
- DeepSeek 环境变量 `DEEPSEEK_API_KEY` 优先于文件。界面会提示；设置了该变量时，应先移除再通过界面替换 Key。

## 更多模型服务与自定义接口

除 Claude 与 DeepSeek 外，可在“后端配置”中点击“添加服务”保存更多 **Anthropic 兼容**的模型服务，最多 12 个：

| 预设 | 默认地址（中国大陆站） | 建议模型 |
| --- | --- | --- |
| Kimi（月之暗面） | `https://api.moonshot.cn/anthropic` | kimi-k3、kimi-k2.7-code、kimi-k2.6 |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | glm-5.2、glm-4.7 |
| 通义千问（阿里云百炼北京） | `https://dashscope.aliyuncs.com/apps/anthropic` | qwen3.7-plus、qwen3.7-max、qwen3.6-flash |
| MiniMax | `https://api.minimaxi.com/anthropic` | MiniMax-M2.5、MiniMax-M2.1 |
| 自定义接口 | 自行填写 HTTPS 地址 | 自行填写 |

预设只预填地址和建议模型，二者均可修改：服务商更新型号较快，国际站账号也使用其他地址（如 `api.moonshot.ai`、`api.minimax.io`），新加坡等地域的百炼地址与北京不同。请以服务商控制台为准，并用“测试连接”确认后再保存。只支持 Anthropic Messages 接口，OpenAI-only 的 `/chat/completions` 地址不能使用。

每个服务保存在 `config.json` 的 `api_profiles` 中（名称、地址、模型、Key 文件、是否支持图片），`backend` 填该服务的 id 即选中它。运行时与 DeepSeek 相同：只在子进程内设置接口地址、模型与 Key，使用独立的 `.runtime/api-cli/<id>` 配置目录和 `.runtime/api-sessions-<id>.json` 会话索引，不修改原 Claude 设置。勾选“模型支持图片”后才提供截图识别。正在使用的服务不能删除；删除其他服务时同时删除其 `.key` 文件。

新的 Key 写入 `.runtime` 中独立 `.key` 文件，不进入配置 JSON、进程命令行参数或日志；旧文件保留以便原任务及回退使用。原 Claude 用户设置不被修改；自定义 Claude 使用独立 `.runtime/claude-api-cli` 与 `.runtime/claude-api-sessions.json`，取消自定义选项后恢复原设置和原会话。

## API Key 与 bot_token 的 Windows 加密保护

本程序管理的 DeepSeek / 自定义 Claude Key 使用 Windows 当前用户 DPAPI 加密后写入 `.key` 文件。原明文格式可以兼容读取，当前使用的 Key 已完成原位迁移并验证解密与真实连接；迁移不改 Key 值和配置引用，不保留新的明文备份。

`config.json` 里的 `bot_token`（微信登录凭据）用同一套 DPAPI 保护，直接加密存在该字段里，不额外开文件——同一个目录里的两份凭据不该是两个标准。旧配置里的明文照样读；`python tools/secure_bot_token.py` 做一次性迁移，改完当场回读校验，其余字段和 UTF-8 BOM 都不动。

换 Windows 账户或重装系统后，可能需要重新填写 Key。沿用原 Claude 设置或环境变量的凭据仍由原工具/系统管理，本程序不改写它们。配置页会显示“受 Windows 用户加密保护”；无法解密时可以输入新 Key 保存。
