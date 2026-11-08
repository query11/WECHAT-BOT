# WECHAT-BOT · 微信助手

通过个人微信给 Windows 电脑发送任务：处理文件、取回结果、管理任务队列，以及控制本机应用。桌面运行中心提供任务记录、附件核对、常用模板、结果继续修改和定时任务管理。

消息经 iLink 长轮询传到本机，文件与工具操作由 Claude Code 执行；模型后端可选 Claude 或 DeepSeek。无需公网 IP 或内网穿透。

**这是运行在 Windows 上的微信桥接助手，不是微信小程序前端，也不是腾讯官方产品。** 使用前需要可用的 iLink 登录能力、Claude Code 和模型服务账号。

## 能做什么

| 场景 | 用法 |
| --- | --- |
| 微信任务 | 发送“开始对话”，随后直接描述任务；“结束对话”收尾并待机 |
| 文件处理 | 从微信上传附件，或在电脑选择文件，确认输入和要求后排队 |
| 结果管理 | 查看完整回复、取回产物、继续修改，保留关联任务和版本记录 |
| 任务工作台 | 查看进度和队列，取消待办，核对失败或发送未确认的结果 |
| 常用模板 | 保存要求、输出位置及可选结果检查，重复运行同类任务 |
| 定时安排 | 设置一次性或每天重复的提醒、自动任务 |
| 本机控制 | 打开应用、调节音量、截图与窗口交互、腾讯会议及电源操作 |

## 快速开始

需要 Windows 10/11、Python 3.10+、Windows PowerShell 5.1，以及已配置可用的 Claude Code CLI（使用引导可一键安装，不需要 Node.js）。使用 DeepSeek 后端仍需要 Claude Code 执行本机工具。

```powershell
git clone https://github.com/query11/WECHAT-BOT.git
cd WECHAT-BOT
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item BOT_MEMORY.example.md BOT_MEMORY.md
New-Item -ItemType Directory -Force C:\WeChatBot\workspace\inbox | Out-Null
```

**更简单的方式**：完成上面的 `git clone` 与依赖安装后，直接双击 `微信助手.bat`，首次运行会进入“使用引导”，按步骤完成目录、模型、扫码登录和微信绑定（发送一次性绑定码即可加入白名单）。

手动配置时，打开 `config.json`，按实际环境填写 `workdir` 和 `inbox.dir`；示例使用上面的目录。先在终端确认 `claude --version` 可运行，并完成模型账号配置。然后扫码登录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\start_wechat.ps1 -Login
python app\wechat_bridge.py run
```

第一次运行时白名单为空，微信消息只会获得用户 ID 提示。将自己的 ID 加入 `allowed_user_ids`，在终端按 `Ctrl+C` 退出，再双击 **`微信助手.bat`** 启动桌面运行中心。从微信发送“开始对话”，再发送任务。

完整配置、模型切换、登录自启动和卸载见 **[快速开始](docs/QUICKSTART.md)**。新版本在 GitHub 发布后，运行中心会提示并支持一键更新，见[发布与更新](docs/reference/发布与更新.md)。

## 常用指令

| 指令或消息 | 作用 |
| --- | --- |
| `开始对话` / `结束对话` | 唤醒并进入对话 / 收尾后待机 |
| `/help` | 查看支持的指令 |
| `/brief` | 查看当前进度与最近结果 |
| `/tasks` | 查看任务记录及对应操作指令 |
| `/queue` | 查看排队任务 |
| `/stop` / `/stopall` | 请求停止当前任务 / 停止并清空普通任务队列 |
| `/artifacts latest` | 查看最近已结束任务的产物 |
| `/getfile latest all` | 请求发送最近任务的全部可发送产物 |
| `开始收集附件` | 建立多附件批次，发齐后核对并确认执行 |
| `/version` | 查看当前版本与是否有新版本 |

更多指令和适用条件见[使用速查](docs/reference/使用速查.md)。

## 项目结构

```text
WECHAT-BOT/
├── 微信助手.bat               # 双击启动（首次运行进入使用引导）
├── app/                       # 后台程序（Python）：微信连接、消息路由、任务、模型后端
├── desktop/                   # Windows 桌面端（PowerShell）：托盘、各窗口、启动/开关/卸载脚本、图标
├── tests/                     # 离线测试与模拟服务（tests/selftest.py 为旧版链路自测入口）
├── tools/                     # 验证、配置维护及开发工具
├── docs/                      # 使用、架构、功能和验收文档
├── .github/workflows/         # Windows 自动回归与发布
├── config.example.json        # 通用配置示例，无登录凭据
├── BOT_MEMORY.example.md      # 固定记忆示例
├── VERSION / requirements.txt # 版本号与 Python 依赖
└── LICENSE / DISCLAIMER.md    # MIT 许可证与使用说明、免责声明
```

运行后生成的 `config.json`、`BOT_MEMORY.md`、`.runtime/` 和日志都保存在项目根目录（安装版即安装目录），不提交 Git。

`app/` 中的模块按原名互相导入（`import wechat_bridge`），由 `desktop/` 的脚本以 `python app\xxx.py` 启动。模块职责和数据流见[架构说明](docs/ARCHITECTURE.md)，完整阅读入口见[文档索引](docs/README.md)。

## 验证与开发

在项目根目录执行：

```powershell
python tools/verify.py --list
python tools/verify.py --suite offline
python tools/verify.py --suite integration
```

`offline` 使用模拟微信服务、模拟模型及临时目录；`integration` 检查 Windows 进程隔离。报告写入 `.runtime/verification/`。测试通过不等于手机端功能已验收，真实收发请按[实机验收](docs/testing/实机验收.md)操作。开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 运行与数据边界

- 本机需要持续开机并联网；待机仍可从微信唤醒，睡眠、休眠、关机或完全退出程序后无法响应。
- `allowed_user_ids` 控制可发任务的账号；模型文件范围由 `workdir`、额外目录及 CLI 权限共同决定。示例不授权整个磁盘。
- 运行账号能够操作本机文件和应用。停止任务不会撤销已完成的文件改动；已开始任务异常中断后，应先核对实际结果。
- 本地保留配置、固定记忆、会话索引、任务库和日志；任务内容及必要资料仍会发给所选模型服务处理。
- `config.json`、`BOT_MEMORY.md`、`.runtime/`、API 密钥、会话文件和日志不上传 Git。仓库只提供配置与记忆示例。
- iLink、模型服务和 Windows 桌面能力会影响可用性；文件大小、发送数量及结果核验也有明确范围，详见[功能详解](docs/reference/功能详解.md)。

## 许可证与免责声明

本项目以 [MIT 许可证](LICENSE) 发布，可自由使用、修改和再分发，需保留版权声明。微信助手是个人开发的非官方工具，与腾讯、微信及各模型服务商无关；使用前请阅读[使用说明与免责声明](DISCLAIMER.md)，了解账号、本机权限、模型费用与数据方面的风险。
