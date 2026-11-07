# 快速开始

## 1. 准备环境

- Windows 10/11，使用能打开本机桌面应用的 Windows 用户。
- Python 3.10+，已加入 `PATH`。
- Windows PowerShell 5.1（系统自带）及 Git。
- 已安装、可正常使用的 Claude Code CLI；选择 DeepSeek 时也需要该 CLI。
- 可扫码登录 iLink 的微信账号，以及可用的网络和模型服务账号。

先在普通终端检查 `python --version` 和 `claude --version`。Claude Code 的账号、API 或模型服务配置需在本机完成；本仓库不提供登录凭据或 API Key。

## 2. 下载并准备本机文件

```powershell
git clone https://github.com/query11/WECHAT-BOT.git
cd WECHAT-BOT
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item BOT_MEMORY.example.md BOT_MEMORY.md
New-Item -ItemType Directory -Force C:\WeChatBot\workspace\inbox | Out-Null
```

以上复制命令用于首次安装；已有配置不要重新覆盖。桥接和桌面功能主要使用 Python 标准库与 Windows 自带程序集，`segno` 用来生成登录二维码图片。

## 3. 检查配置

使用 UTF-8 编辑器打开项目根目录的 `config.json`：

| 字段 | 配置方式 |
| --- | --- |
| `workdir` | 已存在的具体工作目录，示例为 `C:\WeChatBot\workspace`；不能填磁盘根或用户主目录 |
| `inbox.dir` | 附件保存目录，建议放在 `workdir` 下，以便模型读取 |
| `claude_bin` | 默认 `claude`；找不到时填写实际可执行文件或命令数组 |
| `claude_extra_args` | 默认 `--permission-mode acceptEdits`；额外工具权限和目录按实际需要配置 |
| `allowed_user_ids` | 初次留空以获取自己的 ID；填入后仅白名单用户可执行任务 |
| `standby_on_start` | 默认 `true`，启动后在微信发“开始对话”唤醒 |
| `bot_memory_file` | 默认 `BOT_MEMORY.md`，按自己的目录和偏好编辑 |
| `backend` | `claude` 或 `deepseek`，两者都通过本机 Claude Code 执行工具 |

JSON 中 Windows 反斜杠需要写两次，例如 `"workdir": "C:\\WeChatBot\\workspace"`。示例配置不包含任何有效令牌。登录后 `bot_token` 由程序保存；不要手动复制其他电脑的加密凭据。

`acceptEdits` 不等于允许所有工具。网页搜索、命令执行等需要遵循 CLI 的权限设置。扩大文件范围时，`workdir`、`--add-dir` 与附件目录要配套核对，不能仅通过固定记忆声称获得权限。

## 4. 登录并设置白名单

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_wechat.ps1 -Login
python wechat_bridge.py run
```

按终端提示扫码。登录成功后启动长轮询，从微信向机器人发送一条消息。白名单为空时只返回用户 ID，不执行任务。

将该 ID 填入 `config.json`，例如：

```json
"allowed_user_ids": ["这里填写程序返回的自己的用户ID"]
```

按 `Ctrl+C` 退出前台进程，再运行配置检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_wechat.ps1 -Check
```

检查会读取本机能力；其中模型设置检查不等于真实 API 调用已成功。如果提示找不到 Claude、目录不存在或尚未登录，按对应项目处理后再开始任务。

## 5. 启动和使用

双击根目录的 `微信助手.bat`。双击托盘图标可打开运行中心；关闭窗口仍在后台运行，菜单“退出程序”才完全退出。

在微信依次发送：

```text
开始对话
帮我在工作目录写一份今天的待办清单
/brief
/tasks
结束对话
```

桌面可以直接使用“本机文件任务”“任务模板”“任务记录”和“定时任务”等入口。多附件处理先核对清单再执行。任务结束后检查结果文件；微信接口接受发送请求不等于手机已下载文件。

可选设置当前用户登录后自启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

这不是登录前运行的系统服务。电脑睡眠、休眠或关机后不会响应微信。

## 6. 切换模型

在运行中心或托盘菜单配置模型后端和 API。DeepSeek 使用兼容接口，由 Claude Code 执行本机工具；需要有效的 DeepSeek Key 和账号支持的模型名称。

CLI 也可以保存后端选择：

```powershell
python tools/set_backend.py deepseek
python tools/set_backend.py claude
```

上面两行分别表示两个选择，按需执行其中一行。保存配置后，待任务空闲时重读配置或正常重启。API Key 保存在本机 `.runtime/` 下，不放进 Git。更详细的隔离、会话与图片支持条件见[模型后端说明](reference/DeepSeek后端说明.md)。

## 已有安装如何更新

1. 在运行中心查看当前任务、排队任务和附件，等待处理结束。
2. 本地备份 `config.json`、`BOT_MEMORY.md` 和 `.runtime/` 中需要保留的任务数据。
3. 更新代码。通过 Git 安装时执行 `git pull --ff-only`；有本地代码改动时先提交或妥善保存。
4. 保留已有配置和固定记忆，按示例配置核对新增字段，不直接覆盖。
5. 正常退出并重新打开 `微信助手.bat`。若只有后台 Python 更新，可使用运行中心的正常重启；桌面脚本更新需要重新打开程序。

异常重启后不要盲目重发。未开始且符合条件的文字任务可手动恢复；已开始或效果不确定的任务，先检查文件和外部应用。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| 微信不执行任务 | 是否已加入白名单、是否已“开始对话”、后台是否连接 |
| 找不到 `claude` | 在同一 Windows 用户的终端运行 `claude --version`，核对 `PATH` 或 `claude_bin` |
| 模型不能读取附件 | `inbox.dir` 是否位于允许目录、文件是否实际下载到本机 |
| 提示“路径解析后发生重定向” | 使用完整目录名称，避免 `RUNNER~1` 一类 Windows 短名称；文件路径中也不要使用符号链接或目录联接 |
| 持续黄色或 TLS 报错 | 运行中心故障提示、网络与代理设置、本机 `bridge.log` |
| 搬到另一台电脑后令牌不可用 | Windows 加密凭据不能直接跨用户/电脑使用，重新扫码并重新填写 API Key |
| 运行中心提示代码更新 | 等任务结束后正常重启；更新桌面脚本则退出程序后重开 |

完整指令见[使用速查](reference/使用速查.md)，更多部署细节见[部署参考](reference/部署指南.md)。
